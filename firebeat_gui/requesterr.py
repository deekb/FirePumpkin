import os
import re
import io
import json
import mimetypes
from zipfile import ZipFile, BadZipFile
from urllib.parse import urlparse
from datetime import timedelta
from pathlib import Path
import atexit
import threading
import time
from typing import List, Tuple

import requests
from flask import Flask, request, jsonify, send_from_directory, render_template_string, send_file
from flask_socketio import SocketIO, emit
from werkzeug.utils import secure_filename

from FireBeat.constants import ZIP_DIR, NON_MAP_DAT_FILES, PLC_IP, PLC_PORT, PUMPKIN_COILS, IGNITER_SHUTOFF
from FireBeat.logger import logger  # <-- unified logger
import uuid
from FireBeat.showrunner import run_blocking_action
from FireBeat.plc_controller import PLCController, PygamePLCController

app = Flask(__name__)
SHOWS = {}  # show_id -> {id, path, status, message, thread, stop_event, started_at, ended_at, map_file}
SHOWS_LOCK = threading.Lock()

SHOW_QUEUED  = "queued"
SHOW_RUNNING = "running"
SHOW_STOPPED = "stopped"
SHOW_DONE    = "done"
SHOW_ERROR   = "error"

def _update_show(show_id, **kv):
    with SHOWS_LOCK:
        show = SHOWS.get(show_id)
        if show:
            show.update(kv)

# ---- Config via env (from gui.py) ----
DRYRUN = True
ARM_REQUIRED = os.getenv("ARM_REQUIRED", "true").lower() in ("1","true","yes","y")
MAX_HOLD_SECONDS = float(os.getenv("MAX_HOLD_SECONDS", "0"))

# Normalize pumpkin coil order (stable UI order). If your dict keys are like 'pumpkin1'..'pumpkin4',
# this will sort correctly; otherwise it falls back to key sort.
_sorted_pumpkin_items: List[Tuple[str,int]] = sorted(PUMPKIN_COILS.items(), key=lambda kv: kv[0])
PUMPKIN_COIL_LIST: List[int | None] = [v for _, v in _sorted_pumpkin_items][:4]
if len(PUMPKIN_COIL_LIST) < 4:
    # pad for UI with None (so coil 0 is valid if present)
    PUMPKIN_COIL_LIST += [None] * (4 - len(PUMPKIN_COIL_LIST))

# ---- PLC Controller selection (from gui.py) ----
plc = PygamePLCController() if DRYRUN else PLCController(is_dryrun=False)

# Auto-select async mode: prefer eventlet if available, else fallback to threading (works on Python 3.13/Windows)
_ASYNC_MODE = os.getenv("ASYNC_MODE")
if not _ASYNC_MODE:
    try:
        import eventlet  # type: ignore
        _ASYNC_MODE = "eventlet"
    except Exception:
        _ASYNC_MODE = "threading"

socketio = SocketIO(app, cors_allowed_origins="*", async_mode=_ASYNC_MODE)

STATE = {
    "igniter_armed": False,
    "pumpkins": [False, False, False, False],
}

_hold_timers = [None, None, None, None]  # per-channel max-hold timeouts


def _start_hold_timeout(idx: int):
    if MAX_HOLD_SECONDS <= 0:
        return
    def killer():
        time.sleep(MAX_HOLD_SECONDS)
        if STATE["pumpkins"][idx]:
            logger.warning(f"Auto-off pumpkin {idx+1} after {MAX_HOLD_SECONDS}s")
            socketio.emit("pumpkin_state", {"index": idx, "on": False})
            _pumpkin_off(idx)
    t = threading.Thread(target=killer, daemon=True)
    _hold_timers[idx] = t
    t.start()


def _pumpkin_on(idx: int):
    coil = PUMPKIN_COIL_LIST[idx]
    if coil is not None:
        logger.debug(f"Pumpkin {idx+1} -> set_coil({coil}, True)")
        plc.set_coil(coil, True)
        STATE["pumpkins"][idx] = True
        _start_hold_timeout(idx)
        return True
    logger.warning(f"No coil mapped for pumpkin index {idx}")
    return False


def _pumpkin_off(idx: int):
    coil = PUMPKIN_COIL_LIST[idx]
    if coil is not None:
        logger.debug(f"Pumpkin {idx+1} -> set_coil({coil}, False)")
        plc.set_coil(coil, False)
        STATE["pumpkins"][idx] = False
        return True
    logger.warning(f"No coil mapped for pumpkin index {idx}")
    return False


def _igniter_set(armed: bool):
    if armed:
        plc.igniter_arm()
    else:
        plc.igniter_disarm()
    STATE["igniter_armed"] = armed


def _all_off_and_disarm():
    try:
        for i, coil in enumerate(PUMPKIN_COIL_LIST):
            if coil:
                plc.set_coil(coil, False)
                STATE["pumpkins"][i] = False
        _igniter_set(False)
        logger.warning("[Safety] All pumpkins OFF; igniter DISARMED")
    except Exception as e:
        logger.error(f"[Safety] Error during shutdown: {e}")


app.config.update(SEND_FILE_MAX_AGE_DEFAULT=timedelta(hours=1))

MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024  # 200 MB ceiling
TIMEOUT = (5, 30)                       # connect, read
FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

# Ensure ZIP_DIR is a Path and exists
if isinstance(ZIP_DIR, str):
    ZIP_DIR = Path(ZIP_DIR)
try:
    ZIP_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    logger.exception("Failed to create ZIP_DIR at %s", ZIP_DIR)

# ----------------------------
# File download helpers
# ----------------------------
def _sanitize_filename(name: str) -> str:
    name = (name or "").strip().replace(" ", "_")
    name = FILENAME_SAFE.sub("", name) or "map.zip"
    return name if name.lower().endswith(".zip") else f"{name}.zip"

def _pick_filename_from_headers(url: str, resp: requests.Response) -> str:
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
    if m:
        return _sanitize_filename(m.group(1))
    tail = urlparse(url).path.rsplit("/", 1)[-1] or "map.zip"
    return _sanitize_filename(tail)

def _is_http_url(url: str) -> bool:
    try:
        u = urlparse(url)
        return u.scheme in ("http", "https")
    except Exception:
        return False

# ----------------------------
# ZIP reading helpers (no extraction)
# ----------------------------
def _safe_join_downloads(filename: str):
    """
    Validate that the requested file resolves under ZIP_DIR without
    rewriting the filename (no secure_filename here).
    """
    candidate = (ZIP_DIR / filename).resolve()
    base = ZIP_DIR.resolve()
    if candidate == base or base not in candidate.parents:
        logger.warning("Path traversal or invalid file request: %s", filename)
        raise FileNotFoundError("Invalid path")
    return candidate

def _read_info_from_zip(path):
    """
    Return (info_dict, base_dir) or (None, None).

    - Only accept files whose basename is exactly 'Info.dat' (case-insensitive)
      to avoid matching 'BPMInfo.dat' etc.
    - If multiple candidates exist, prefer the shallowest path.
    - As a fallback, try any remaining '...Info.dat' only if it actually has a song name key.
    """
    def _has_title(d: dict) -> bool:
        return isinstance(d, dict) and (d.get("_songName") or d.get("songName"))

    try:
        with ZipFile(path, "r") as z:
            names = z.namelist()

            strict = [n for n in names if os.path.basename(n).lower() == "info.dat"]
            strict.sort(key=lambda n: n.count("/"))  # prefer shallowest

            fallback = [
                n for n in names
                if n.lower().endswith("info.dat") and os.path.basename(n).lower() != "info.dat"
            ]
            fallback.sort(key=lambda n: n.count("/"))

            for group_name, group in (("strict", strict), ("fallback", fallback)):
                for info_name in group:
                    try:
                        with z.open(info_name) as f:
                            raw = f.read()
                        # utf-8-sig handles BOM; plain utf-8 covers the rest
                        try:
                            text = raw.decode("utf-8-sig")
                        except UnicodeDecodeError:
                            text = raw.decode("utf-8", errors="replace")
                        data = json.loads(text)
                        if group_name == "strict" or _has_title(data):
                            base_dir = os.path.dirname(info_name).replace("\\", "/")
                            logger.debug("Using Info.dat from %s (group=%s, keys=%s)",
                                         info_name, group_name, list(data.keys())[:8])
                            return data, base_dir
                        else:
                            logger.debug("Skipping candidate without title key: %s", info_name)
                    except Exception as e:
                        logger.debug("Candidate Info.dat failed (%s): %s", info_name, e)
            logger.warning("No usable Info.dat in %s", path)
            return None, None
    except BadZipFile:
        logger.warning("Bad ZIP file: %s", path)
        return None, None
    except Exception:
        logger.exception("Zip open/parse failed for %s", path)
        return None, None

def _read_cover_bytes_from_zip(path):
    """
    Return (bytes, mime) for the cover image inside the zip.
    Looks for Info.dat to find _coverImageFilename; falls back to first jpg/png/webp.
    """
    try:
        with ZipFile(path, "r") as z:
            names = z.namelist()

            # Prefer strict basename Info.dat
            info_name = next((n for n in names if os.path.basename(n).lower() == "info.dat"), None)
            if not info_name:
                # fallback to any *Info.dat
                info_name = next((n for n in names if n.lower().endswith("info.dat")), None)

            if info_name:
                try:
                    with z.open(info_name) as f:
                        info = json.loads(f.read().decode("utf-8", errors="ignore"))
                    cover_name = (info.get("_coverImageFilename") or "").replace("\\", "/")
                    if cover_name:
                        # resolve relative to Info.dat directory if necessary
                        info_dir = os.path.dirname(info_name).replace("\\", "/")
                        if info_dir:
                            cover_name = f"{info_dir}/{cover_name}"
                        # case-insensitive match
                        real = next((n for n in names if n.lower() == cover_name.lower()), None)
                        if real is None and cover_name in names:
                            real = cover_name
                        if real:
                            with z.open(real) as f:
                                data = f.read()
                            mime = mimetypes.guess_type(real)[0] or "image/jpeg"
                            return data, mime
                except Exception:
                    logger.debug("Cover lookup via Info.dat failed for %s", path)

            # Fallback: first common image
            fallback = next((n for n in names if n.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))), None)
            if fallback:
                with z.open(fallback) as f:
                    data = f.read()
                mime = mimetypes.guess_type(fallback)[0] or "image/jpeg"
                return data, mime

    except BadZipFile:
        logger.warning("Bad ZIP file when reading cover: %s", path)
    except Exception:
        logger.exception("Cover extraction failed for %s", path)
    return None, None

# Map selector
@app.get("/api/maps")
def api_maps():
    """
    Query params:
      - zipRel: e.g. 'downloads/Foo.zip' or 'ZIP_DIR/Foo.zip'
    Returns: { ok, zip_rel, maps: ["<path>.dat", ...] }
    """
    zip_rel = (request.args.get("zipRel") or "").strip()
    if not zip_rel:
        return jsonify({"error": "Missing zipRel"}), 400

    rel = zip_rel.lstrip("/")
    prefix = f"{ZIP_DIR.name}/"
    if rel.startswith(prefix):
        rel = rel[len(prefix):]

    try:
        path = _safe_join_downloads(rel)
        with ZipFile(path, "r") as zf:
            names = zf.namelist()
        # Only map files, exclude known non-map dats
        maps = [n for n in names if n.endswith(".dat") and n.lower() not in NON_MAP_DAT_FILES]
        maps.sort(key=lambda s: (s.count("/"), s.lower()))
        return jsonify({"ok": True, "zip_rel": f"{ZIP_DIR.name}/{rel}", "maps": maps})
    except FileNotFoundError:
        return jsonify({"error": "Not found"}), 404
    except Exception:
        logger.exception("api_maps: failed for %s", zip_rel)
        return jsonify({"error": "Unexpected error"}), 500


@app.get("/panel")
def pumpkin_panel():
    return render_template_string(PUMPKIN_PANEL_HTML,
                                  plc_ip=PLC_IP,
                                  plc_port=PLC_PORT,
                                  igniter_shutoff=IGNITER_SHUTOFF,
                                  pumpkin_coils=PUMPKIN_COIL_LIST)


# ---- SocketIO events (from gui.py) ----
@socketio.on("connect")
def on_connect():
    emit("init", {
        "igniter_armed": STATE["igniter_armed"],
        "pumpkins": STATE["pumpkins"],
        "arm_required": ARM_REQUIRED,
        "plc": {"ip": PLC_IP, "port": PLC_PORT},
        "coils": {"pumpkins": PUMPKIN_COIL_LIST, "igniter_shutoff": IGNITER_SHUTOFF},
        "dryrun": DRYRUN,
    })


@socketio.on("toggle_igniter")
def toggle_igniter(data):
    desired = bool(data.get("armed", False))
    _igniter_set(desired)
    emit("igniter_state", {"armed": desired}, broadcast=True)


@socketio.on("pumpkin_press")
def pumpkin_press(data):
    idx = int(data.get("index", -1))
    if idx < 0 or idx >= 4:
        emit("error", {"message": "Invalid pumpkin index"})
        return
    if ARM_REQUIRED and not STATE["igniter_armed"]:
        emit("error", {"message": "Igniter not armed"})
        return
    if _pumpkin_on(idx):
        emit("pumpkin_state", {"index": idx, "on": True}, broadcast=True)
    else:
        emit("error", {"message": f"Failed to turn on pumpkin {idx+1}"})


@socketio.on("pumpkin_release")
def pumpkin_release(data):
    idx = int(data.get("index", -1))
    if idx < 0 or idx >= 4:
        emit("error", {"message": "Invalid pumpkin index"})
        return
    if _pumpkin_off(idx):
        emit("pumpkin_state", {"index": idx, "on": False}, broadcast=True)
    else:
        emit("error", {"message": f"Failed to turn off pumpkin {idx+1}"})


# ---- Shutdown safety (from gui.py) ----
@atexit.register
def _shutdown():
    _all_off_and_disarm()
    try:
        plc.close()
    except Exception:
        pass


# ----------------------------
# HTML (Search + Library) with shared nav
# ----------------------------
NAV_HTML = r"""
  <header class="flex items-center justify-between mb-4">
    <h1 class="text-2xl font-bold">BeatSaver</h1>
    <nav class="flex gap-2">
      <a href="/" class="px-3 py-1 rounded-lg border border-slate-300 bg-white text-slate-900 hover:bg-slate-100
                        dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100 dark:hover:bg-slate-800">
        Search
      </a>
      <a href="/library" class="px-3 py-1 rounded-lg bg-indigo-600 text-white hover:bg-indigo-700">
        Library
      </a>
      <a href="/panel" class="px-3 py-1 rounded-lg border border-slate-300 bg-white text-slate-900 hover:bg-slate-100
                        dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100 dark:hover:bg-slate-800">
        Pumpkin Panel
      </a>
      <button id="toggle-theme"
        class="px-3 py-1 rounded-lg border border-slate-300 bg-white text-slate-900 hover:bg-slate-100
               dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100 dark:hover:bg-slate-800">
        Toggle theme
      </button>
    </nav>
  </header>
"""

INDEX_HTML = r"""<!doctype html>
<html lang="en" class="dark"> <!-- default dark -->
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>BeatSaver Search</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>tailwind.config = { darkMode: 'class' };</script>
  <script>
    (function () {
      const stored = localStorage.getItem('theme');
      const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
      const html = document.documentElement;
      if (stored === 'dark' || (!stored && prefersDark) || (!stored && !prefersDark)) html.classList.add('dark');
      else html.classList.remove('dark');
    })();
  </script>
</head>
<body class="bg-white text-slate-900 dark:bg-slate-950 dark:text-slate-100">
  <div class="max-w-6xl mx-auto p-6">
""" + NAV_HTML + r"""
    <form id="search-form"
      class="rounded-2xl shadow p-4 mb-6 grid gap-3 md:grid-cols-12
             bg-white text-slate-900 border border-slate-200
             dark:bg-slate-900 dark:text-slate-100 dark:border-slate-800">
      <input id="q" name="q" placeholder="Search songs, authors, tags…"
             class="md:col-span-8 col-span-12 rounded-xl px-3 py-2
                    border border-slate-300 bg-white text-slate-900 placeholder-slate-500
                    focus:outline-none focus:ring-2 focus:ring-indigo-500
                    dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100 dark:placeholder-slate-400"
             required />
      <button
        class="md:col-span-4 col-span-12 rounded-xl px-4 py-2
               bg-indigo-600 text-white hover:bg-indigo-700">
        Search
      </button>
    </form>

    <div id="meta" class="text-sm text-slate-600 dark:text-slate-400 mb-3"></div>
    <div id="pager" class="flex gap-2 mb-3"></div>
    <div id="grid" class="grid md:grid-cols-2 lg:grid-cols-3 gap-4"></div>
  </div>

<script>
const API_SEARCH = (page, q) => `https://beatsaver.com/api/search/text/${page}?q=${encodeURIComponent(q)}`;

const form   = document.getElementById('search-form');
const grid   = document.getElementById('grid');
const pager  = document.getElementById('pager');
const meta   = document.getElementById('meta');
const qInput = document.getElementById('q');

const themeBtn = document.getElementById('toggle-theme');
themeBtn.addEventListener('click', () => {
  const html = document.documentElement;
  const nowDark = !html.classList.contains('dark');
  html.classList.toggle('dark', nowDark);
  localStorage.setItem('theme', nowDark ? 'dark' : 'light');
});

let state = { q: "", page: 1, lastPage: 1, totalDocs: 0, docs: [] };

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  state.q = qInput.value.trim();
  state.page = 1;
  await search();
});

async function search() {
  if (!state.q) return;
  try {
    const res = await fetch(API_SEARCH(state.page, state.q), {
      headers: { "Accept": "application/json, text/plain, */*" }
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    state.docs = data.docs || [];
    state.totalDocs = data.totalDocs || (state.docs?.length || 0);
    state.lastPage  = data.lastPage || 1;

    render();
  } catch (err) {
    grid.innerHTML = alertBox("Error: " + err.message);
    pager.innerHTML = "";
    meta.textContent = "";
  }
}

function render() {
  meta.textContent = `~${state.totalDocs} results for “${state.q}” — page ${state.page}/${state.lastPage}`;

  pager.innerHTML = "";
  pager.append(makeBtn("Prev", state.page <= 1, async () => { state.page -= 1; await search(); }));
  const span = document.createElement('span');
  span.className = "px-3 py-1 text-sm text-slate-500 dark:text-slate-400 self-center";
  span.textContent = `Page ${state.page} / ${state.lastPage}`;
  pager.append(span);
  pager.append(makeBtn("Next", state.page >= state.lastPage, async () => { state.page += 1; await search(); }));

  grid.innerHTML = "";
  for (const d of state.docs) {
    const v0 = (d.versions && d.versions[0]) || {};
    const cover = v0.coverURL || "";
    const preview = v0.previewURL || "";
    const downloadURL = v0.downloadURL || "";

    const card = document.createElement('article');
    card.className = `
      rounded-2xl shadow overflow-hidden border
      bg-white text-slate-900 border-slate-200
      dark:bg-slate-900 dark:text-slate-100 dark:border-slate-800
    `;
    card.innerHTML = `
      ${cover ? `<img src="${cover}" alt="cover" class="w-full h-44 object-cover">` : ""}
      <div class="p-4">
        <h2 class="font-semibold text-lg">${escapeHTML(d.name || d.metadata?.songName || "Untitled")}</h2>
        <p class="text-sm text-slate-600 dark:text-slate-400">
          by ${escapeHTML(d.metadata?.songAuthorName || "Unknown")}
          · mapped by ${escapeHTML(d.metadata?.levelAuthorName || d.uploader?.name || "Unknown")}
        </p>
        <dl class="grid grid-cols-2 gap-2 text-sm mt-3">
          <div><dt class="text-slate-500 dark:text-slate-400">BPM</dt><dd>${d.metadata?.bpm ?? "—"}</dd></div>
          <div><dt class="text-slate-500 dark:text-slate-400">Dur (s)</dt><dd>${d.metadata?.duration ?? "—"}</dd></div>
          <div><dt class="text-slate-500 dark:text-slate-400">Score</dt><dd>${fmtScore(d.stats?.score)}</dd></div>
          <div><dt class="text-slate-500 dark:text-slate-400">Votes</dt><dd>▲ ${d.stats?.upvotes ?? 0} &nbsp; ▼ ${d.stats?.downvotes ?? 0}</dd></div>
          <div><dt class="text-slate-500 dark:text-slate-400">Plays</dt><dd>${d.stats?.plays ?? 0}</dd></div>
          <div><dt class="text-slate-500 dark:text-slate-400">Ranked</dt><dd>${d.ranked ? "Yes" : "No"}</dd></div>
        </dl>

        <div class="flex flex-col gap-2 mt-3">
          ${preview ? `<audio controls preload="none" class="w-full"><source src="${preview}" type="audio/mpeg"></audio>` : ""}
          ${downloadURL ? `
            <div class="flex gap-2">
              <a class="px-3 py-1 rounded-lg border
                        border-slate-300 bg-white text-slate-900 hover:bg-slate-100
                        dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100 dark:hover:bg-slate-700"
                 href="${downloadURL}">
                Direct download
              </a>
              <button class="px-3 py-1 rounded-lg
                             bg-indigo-600 text-white hover:bg-indigo-700"
                      data-url="${downloadURL}">
                Save ZIP on server
              </button>
            </div>` : ""}
          <div class="text-xs text-slate-500 dark:text-slate-400 save-result mt-1"></div>
        </div>
      </div>
    `;
    grid.append(card);
  }

  grid.querySelectorAll("button[data-url]").forEach(btn => {
    btn.addEventListener("click", () => saveZip(btn));
  });
}

function makeBtn(label, disabled, onclick) {
  const btn = document.createElement('button');
  btn.className = `
    px-3 py-1 rounded-lg border
    border-slate-300 bg-white text-slate-900 hover:bg-slate-100 disabled:opacity-50
    dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100 dark:hover:bg-slate-800
  `;
  btn.textContent = label;
  btn.disabled = disabled;
  btn.onclick = onclick;
  return btn;
}

async function saveZip(btn) {
  const url = btn.dataset.url;
  const statusEl = btn.closest("article").querySelector(".save-result");
  statusEl.textContent = "Saving on server…";
  try {
    const resp = await fetch("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ downloadURL: url })
    });
    const data = await resp.json().catch(() => null);
    if (!resp.ok || !data?.ok) throw new Error(data?.error || `HTTP ${resp.status}`);
    statusEl.innerHTML = `Saved: <code>${escapeHTML(data.rel)}</code> (${data.bytes} bytes)
      — <a class="underline" href="/${encodeURI(data.rel)}">download from server</a>`;
  } catch (e) {
    statusEl.textContent = "Failed: " + e.message;
  }
}

function fmtScore(s) { return (typeof s === "number") ? s.toFixed(4) : "—"; }

function escapeHTML(s) {
  if (s == null) return "";
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function alertBox(msg) {
  return `<div class="rounded-xl p-4 border
                  bg-red-50 text-red-700 border-red-200
                  dark:bg-red-900/30 dark:text-red-300 dark:border-red-800">
            ${escapeHTML(msg)}
          </div>`;
}
</script>
</body>
</html>
"""

LIBRARY_HTML = r"""
"""



SHOW_CONTROL_HTML = r"""<!doctype html>
<html lang="en" class="dark">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Show Control</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>tailwind.config = { darkMode: 'class' };</script>
  <script>
    (function(){
      const stored = localStorage.getItem('theme');
      const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
      const html = document.documentElement;
      if (stored === 'dark' || (!stored && prefersDark) || (!stored && !prefersDark)) html.classList.add('dark');
      else html.classList.remove('dark');
    })();
  </script>
</head>
<body class="bg-white text-slate-900 dark:bg-slate-950 dark:text-slate-100">
  <div class="max-w-3xl mx-auto p-6">
    """ + NAV_HTML + r"""
    <section class="rounded-2xl shadow p-6 border bg-white text-slate-900 border-slate-200
                    dark:bg-slate-900 dark:text-slate-100 dark:border-slate-800">
      <h2 class="text-xl font-semibold mb-2">Show Control</h2>
      <p class="text-sm text-slate-600 dark:text-slate-400 mb-4">Show ID: <code id="show-id"></code></p>
      <dl class="grid grid-cols-2 gap-3 text-sm">
        <div><dt class="text-slate-500 dark:text-slate-400">Status</dt><dd id="status">—</dd></div>
        <div><dt class="text-slate-500 dark:text-slate-400">Message</dt><dd id="message">—</dd></div>
        <div class="col-span-2"><dt class="text-slate-500 dark:text-slate-400">ZIP Path</dt><dd id="zip-path" class="break-all">—</dd></div>
        <div class="col-span-2"><dt class="text-slate-500 dark:text-slate-400">Map File</dt><dd id="map-file" class="break-all">—</dd></div>
      </dl>
      <div class="mt-5 flex gap-2">
        <button id="stop-btn"
                class="px-3 py-1 rounded-lg bg-red-600 text-white hover:bg-red-700 disabled:opacity-50">
          Stop Show
        </button>
        <a href="/library"
           class="px-3 py-1 rounded-lg border border-slate-300 bg-white text-slate-900 hover:bg-slate-100
                  dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100 dark:hover:bg-slate-700">
          Back to Library
        </a>
      </div>
    </section>
  </div>

<script>
const showId = location.pathname.split("/").pop();
document.getElementById('show-id').textContent = showId;

const $status  = document.getElementById('status');
const $message = document.getElementById('message');
const $zipPath = document.getElementById('zip-path');
const $mapFile = document.getElementById('map-file');
const $stopBtn = document.getElementById('stop-btn');

document.getElementById('toggle-theme').addEventListener('click', () => {
  const html = document.documentElement;
  const nowDark = !html.classList.contains('dark');
  html.classList.toggle('dark', nowDark);
  localStorage.setItem('theme', nowDark ? 'dark' : 'light');
});

async function poll(){
  try{
    const r = await fetch(`/api/show/${showId}/status`, { headers: { "Accept": "application/json" } });
    const d = await r.json();
    if(!r.ok || !d?.ok) throw new Error(d?.error || `HTTP ${r.status}`);
    $status.textContent  = d.status;
    $message.textContent = d.message || "";
    $zipPath.textContent = d.zip_path || "";
    $mapFile.textContent = d.map_file || "";
    const finished = ["done","stopped","error"].includes(d.status);
    $stopBtn.disabled = finished;
  }catch(e){
    $status.textContent = "error";
    $message.textContent = e.message;
    $stopBtn.disabled = true;
  }
}

$stopBtn.addEventListener('click', async () => {
  $stopBtn.disabled = true;
  try{
    const r = await fetch(`/api/show/${showId}/stop`, { method: "POST" });
    const d = await r.json().catch(() => ({}));
    if(!r.ok || !d?.ok) throw new Error(d?.error || `HTTP ${r.status}`);
  }catch(e){
    $message.textContent = "Stop failed: " + e.message;
  }finally{
    poll();
  }
});

poll();
setInterval(poll, 1000);
</script>
</body>
</html>
"""


# ----------------------------
# Routes
# ----------------------------
#Main search index page
@app.get("/")
def index():
    return render_template_string(INDEX_HTML)

#allows downloading of beatmap zips to server for playback
@app.post("/api/download")
def api_download():
    """
    Body JSON: { "downloadURL": "<versions[0].downloadURL>", "filename": "optional.zip" }
    Saves ZIP to downloads and returns JSON: { ok, rel, saved, bytes }
    """
    if not request.is_json:
        logger.warning("api_download: non-JSON request")
        return jsonify({"error": "Expected JSON body"}), 400

    body = request.get_json(silent=True) or {}
    url = str(body.get("downloadURL") or "").strip()
    override_name = body.get("filename")
    logger.info("api_download: url=%s, override_name=%s", url, override_name)

    if not url or not _is_http_url(url):
        logger.warning("api_download: invalid URL: %s", url)
        return jsonify({"error": "Invalid URL or scheme"}), 400

    try:
        with requests.get(url, stream=True, timeout=TIMEOUT, headers={
            "Accept": "application/zip,application/octet-stream,*/*",
            "User-Agent": "BeatSaver-Downloader/1.0",
        }) as r:
            r.raise_for_status()

            total = r.headers.get("Content-Length")
            if total:
                try:
                    if int(total) > MAX_DOWNLOAD_BYTES:
                        logger.warning("api_download: too large (Content-Length=%s)", total)
                        return jsonify({"error": "File too large"}), 413
                except ValueError:
                    logger.debug("api_download: non-integer Content-Length: %s", total)

            fname = _sanitize_filename(override_name or _pick_filename_from_headers(url, r))
            target = ZIP_DIR / fname
            if target.exists():
                stem, suffix, i = target.stem, target.suffix, 2
                while (ZIP_DIR / f"{stem}-{i}{suffix}").exists():
                    i += 1
                target = ZIP_DIR / f"{stem}-{i}{suffix}"
            logger.debug("api_download: saving to %s", target)

            written = 0
            with open(target, "wb") as f:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > MAX_DOWNLOAD_BYTES:
                        logger.warning("api_download: exceeded max bytes (%d)", written)
                        f.close()
                        try:
                            target.unlink(missing_ok=True)
                        except Exception:
                            logger.debug("api_download: cleanup failed for %s", target)
                        return jsonify({"error": "File too large"}), 413
                    f.write(chunk)

        logger.info("api_download: saved %s (%d bytes)", target.name, written)
        return jsonify({
            "ok": True,
            "rel": f"{ZIP_DIR}/{target.name}",
            "saved": str(target.resolve()),
            "bytes": written
        })
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else 502
        logger.warning("api_download: upstream HTTP error %s for %s", code, url)
        return jsonify({"error": f"Upstream HTTP {code}"}), 502
    except requests.RequestException as e:
        logger.warning("api_download: network error: %s", e)
        return jsonify({"error": f"Network error: {e}"}), 502
    except Exception:
        logger.exception("api_download: unexpected error")
        return jsonify({"error": "Unexpected error"}), 500

# shows downloaded songs (html)
@app.get("/library")
def library():
    return render_template_string(LIBRARY_HTML)

#shows downloaded songs (json)
@app.get("/api/library")
def api_library():
    """
    Returns metadata for all downloaded zips by reading Info.dat inside each.
    """
    items = []
    count = 0
    try:
        for entry in sorted(ZIP_DIR.glob("*.zip")):
            if not entry.is_file():
                logger.debug("api_library: skipping non-file %s", entry)
                continue
            count += 1
            info, _ = _read_info_from_zip(entry)
            data = {
                "zipName": entry.name,
                "rel": f"{ZIP_DIR}/{entry.name}",
                "bytes": entry.stat().st_size,
                "songName": (info or {}).get("_songName") or (info or {}).get("songName"),
                "songAuthorName": (info or {}).get("_songAuthorName") or (info or {}).get("songAuthorName"),
                "levelAuthorName": (info or {}).get("_levelAuthorName") or (info or {}).get("levelAuthorName"),
                "coverURL": f"/covers/{entry.name}",
            }
            if not data["songName"]:
                logger.debug("api_library: missing songName for %s", entry.name)
            items.append(data)
        logger.info("api_library: scanned %d zip(s), returning %d item(s)", count, len(items))
    except Exception:
        logger.exception("api_library: failed to enumerate ZIP_DIR=%s", ZIP_DIR)
        return jsonify({"error": "Failed to read ZIP_DIR"}), 500

    return jsonify({"items": items})
#util for serving cover images from zips
@app.get("/covers/<path:zipname>")
def cover_from_zip(zipname):
    """
    Streams the cover image directly from inside the ZIP without extracting.
    """
    try:
        path = _safe_join_downloads(zipname)
        img_bytes, mime = _read_cover_bytes_from_zip(path)
        if not img_bytes:
            logger.debug("cover_from_zip: no cover in %s (fallback)", zipname)
            fallback = (
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
                b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0bIDATx\x9cc``\x00\x00\x00\x02"
                b"\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
            )
            return send_file(io.BytesIO(fallback), mimetype="image/png", max_age=3600)
        logger.debug("cover_from_zip: served cover for %s (mime=%s, bytes=%d)", zipname, mime, len(img_bytes))
        return send_file(io.BytesIO(img_bytes), mimetype=mime, max_age=3600)
    except FileNotFoundError:
        logger.warning("cover_from_zip: not found %s", zipname)
        return jsonify({"error": "Not found"}), 404
    except Exception:
        logger.exception("cover_from_zip: failed for %s", zipname)
        return jsonify({"error": "Unexpected error"}), 500

# serves saved zip files if user wants to download them from local stash
@app.get("/downloads/<path:filename>")
def serve_saved(filename):
    try:
        path = _safe_join_downloads(filename)
        logger.debug("serve_saved: sending %s", path.name)
        return send_from_directory(ZIP_DIR, path.name, as_attachment=True, max_age=0)
    except FileNotFoundError:
        logger.warning("serve_saved: not found %s", filename)
        return jsonify({"error": "Not found"}), 404
    except Exception:
        logger.exception("serve_saved: failed for %s", filename)
        return jsonify({"error": "Unexpected error"}), 500


#MAIN SONG PLAYBACK ROUTE

@app.get("/show/<show_id>")
def show_control(show_id):
    with SHOWS_LOCK:
        if show_id not in SHOWS:
            return jsonify({"error": "Not found"}), 404
    return render_template_string(SHOW_CONTROL_HTML)

@app.post("/api/show")
def api_show_start():
    """
    Body JSON:
      { "zipRel": "<ZIP_DIR>/<file>.zip" or "file.zip",
        "mapFile": "<path/in/zip.dat>" }

    Spawns a background thread running run_blocking_action and returns a control URL.
    """
    if not request.is_json:
        return jsonify({"error": "Expected JSON body"}), 400
    body = request.get_json(silent=True) or {}

    zip_rel  = (body.get("zipRel")  or "").strip()
    map_file = (body.get("mapFile") or "").strip() or None
    if not zip_rel:
        return jsonify({"error": "Missing zipRel"}), 400
    if not map_file:
        return jsonify({"error": "Missing mapFile"}), 400

    # Normalize relative to ZIP_DIR
    rel = zip_rel.lstrip("/")
    prefix = f"{ZIP_DIR.name}/"
    if rel.startswith(prefix):
        rel = rel[len(prefix):]

    try:
        path = _safe_join_downloads(rel)
        if not path.exists() or not path.is_file():
            return jsonify({"error": "ZIP not found"}), 404

        show_id = uuid.uuid4().hex
        stop_event = threading.Event()

        def set_status(state, msg):
            _update_show(show_id, status=state, message=msg)
            if state in (SHOW_DONE, SHOW_STOPPED, SHOW_ERROR):
                _update_show(show_id, ended_at=time.time())

        show = {
            "id": show_id,
            "path": str(path.resolve()),
            "status": SHOW_QUEUED,
            "message": "Queued",
            "thread": None,
            "stop_event": stop_event,
            "started_at": time.time(),
            "ended_at": None,
            "map_file": map_file,
        }
        with SHOWS_LOCK:
            SHOWS[show_id] = show

        t = threading.Thread(
            target=run_blocking_action,                   # <- your unchanged worker
            args=(path, stop_event, set_status, map_file),
            kwargs={"is_dryrun": True},                  # flip to False when ready
            name=f"show-{show_id}",
            daemon=True,
        )
        show["thread"] = t
        t.start()

        control_url = f"/show/{show_id}"
        logger.info("api_show_start: started show %s for %s (map=%s)", show_id, path.name, map_file)
        return jsonify({
            "ok": True,
            "show_id": show_id,
            "zip_path": show["path"],
            "map_file": map_file,
            "control_url": control_url
        })
    except FileNotFoundError:
        return jsonify({"error": "Invalid path"}), 404
    except Exception:
        logger.exception("api_show_start: unexpected error")
        return jsonify({"error": "Unexpected error"}), 500

@app.get("/api/show/<show_id>/status")
def api_show_status(show_id):
    with SHOWS_LOCK:
        show = SHOWS.get(show_id)
        if not show:
            return jsonify({"error": "Not found"}), 404
        return jsonify({
            "ok": True,
            "show_id": show_id,
            "status": show["status"],
            "message": show["message"],
            "zip_path": show["path"],
            "map_file": show.get("map_file"),
            "started_at": show["started_at"],
            "ended_at": show["ended_at"],
        })

@app.post("/api/show/<show_id>/stop")
def api_show_stop(show_id):
    with SHOWS_LOCK:
        show = SHOWS.get(show_id)
        if not show:
            return jsonify({"error": "Not found"}), 404
        if show["status"] in (SHOW_DONE, SHOW_STOPPED, SHOW_ERROR):
            return jsonify({"ok": True, "status": show["status"], "message": "Already finished"})
        show["stop_event"].set()
    return jsonify({"ok": True, "message": "Stop requested"})







if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    logger.info("Starting Flask on 0.0.0.0:%d (ZIP_DIR=%s)", port, ZIP_DIR)
    app.run(host="0.0.0.0", port=port, debug=True)
