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
import uuid

import requests
from flask import Flask, request, jsonify, send_from_directory, render_template, send_file, render_template_string
from flask_socketio import SocketIO, emit
from werkzeug.utils import secure_filename  # (kept if you use it elsewhere)

from FireBeat.constants import ZIP_DIR, NON_MAP_DAT_FILES, PLC_IP, PLC_PORT, PUMPKIN_COILS, IGNITER_SHUTOFF
from FireBeat.logger import logger  # unified logger
from FireBeat.showrunner import run_blocking_action
from FireBeat.plc_controller import PLCController, PygamePLCController

# -----------------------------------------------------------------------------
# App & Socket.IO
# -----------------------------------------------------------------------------
app = Flask(__name__)
# Prefer eventlet if available (works with Flask-SocketIO); fall back to threading.
_ASYNC_MODE = os.getenv("ASYNC_MODE")
if not _ASYNC_MODE:
    try:
        import eventlet  # type: ignore
        _ASYNC_MODE = "eventlet"
    except Exception:
        _ASYNC_MODE = "threading"

socketio = SocketIO(app, cors_allowed_origins="*", async_mode=_ASYNC_MODE)

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
# Env-driven DRYRUN (default True so you don’t accidentally fire hardware).
DRYRUN = os.getenv("DRYRUN", "true").lower() in ("1", "true", "yes", "y")
ARM_REQUIRED = os.getenv("ARM_REQUIRED", "true").lower() in ("1","true","yes","y")
MAX_HOLD_SECONDS = float(os.getenv("MAX_HOLD_SECONDS", "0"))

app.config.update(SEND_FILE_MAX_AGE_DEFAULT=timedelta(hours=1))

# Ensure ZIP_DIR exists (normalize str -> Path)
if isinstance(ZIP_DIR, str):
    ZIP_DIR = Path(ZIP_DIR)
try:
    ZIP_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    logger.exception("Failed to create ZIP_DIR at %s", ZIP_DIR)

# -----------------------------------------------------------------------------
# PLC / Panel state
# -----------------------------------------------------------------------------
_sorted_pumpkin_items: List[Tuple[str,int]] = sorted(PUMPKIN_COILS.items(), key=lambda kv: kv[0])
PUMPKIN_COIL_LIST: List[int | None] = [v for _, v in _sorted_pumpkin_items][:4]
if len(PUMPKIN_COIL_LIST) < 4:
    PUMPKIN_COIL_LIST += [None] * (4 - len(PUMPKIN_COIL_LIST))

plc = PygamePLCController() if DRYRUN else PLCController(is_dryrun=False)
PLC_LOCK = threading.Lock()  # guard controller swaps when switching dry run from webui :)




STATE = {
    "igniter_armed": False,
    "pumpkins": [False, False, False, False],
}
_hold_timers = [None, None, None, None]

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


#dryrun toggle helpers

def _any_show_running() -> bool:
    with SHOWS_LOCK:
        return any(s.get("status") == SHOW_RUNNING for s in SHOWS.values())

def _set_dryrun(on: bool) -> tuple[bool, str]:
    """
    Switch DRYRUN mode safely. Returns (ok, message).
    - Blocks if a show is running.
    - All outputs off and igniter disarmed during swap.
    """
    global DRYRUN, plc
    if on == DRYRUN:
        return True, f"Dryrun already {'ON' if on else 'OFF'}"

    if _any_show_running():
        return False, "Cannot change Dryrun while a show is running"

    try:
        with PLC_LOCK:
            # Safety first: drop outputs on current controller
            _all_off_and_disarm()

            # Close current controller
            try:
                plc.close()
            except Exception:
                pass

            # Swap controllers
            if on:
                plc = PygamePLCController()
                logger.info("Dryrun -> ON (PygamePLCController)")
            else:
                plc = PLCController(is_dryrun=False)
                logger.info("Dryrun -> OFF (PLCController LIVE)")

            DRYRUN = on

            # Make sure state is consistent
            STATE["igniter_armed"] = False
            for i in range(4):
                STATE["pumpkins"][i] = False

        return True, f"Dryrun {'ENABLED' if on else 'DISABLED'}"
    except Exception as e:
        logger.exception("Failed switching dryrun -> %s", on)
        return False, f"Switch failed: {e}"


# -----------------------------------------------------------------------------
# Show runner state
# -----------------------------------------------------------------------------
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

# -----------------------------------------------------------------------------
# Constants & helpers
# -----------------------------------------------------------------------------
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024  # 200 MB ceiling
TIMEOUT = (5, 30)                       # connect, read
FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

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

def _safe_join_downloads(filename: str):
    """
    Validate that the requested file resolves under ZIP_DIR without traversal.
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
    """
    def _has_title(d: dict) -> bool:
        return isinstance(d, dict) and (d.get("_songName") or d.get("songName"))

    try:
        with ZipFile(path, "r") as z:
            names = z.namelist()

            strict = [n for n in names if os.path.basename(n).lower() == "info.dat"]
            strict.sort(key=lambda n: n.count("/"))

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
                        try:
                            text = raw.decode("utf-8-sig")
                        except UnicodeDecodeError:
                            text = raw.decode("utf-8", errors="replace")
                        data = json.loads(text)
                        if group_name == "strict" or _has_title(data):
                            base_dir = os.path.dirname(info_name).replace("\\", "/")
                            logger.debug("Using Info.dat from %s", info_name)
                            return data, base_dir
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
            info_name = next((n for n in names if os.path.basename(n).lower() == "info.dat"), None)
            if not info_name:
                info_name = next((n for n in names if n.lower().endswith("info.dat")), None)

            if info_name:
                try:
                    with z.open(info_name) as f:
                        info = json.loads(f.read().decode("utf-8", errors="ignore"))
                    cover_name = (info.get("_coverImageFilename") or "").replace("\\", "/")
                    if cover_name:
                        info_dir = os.path.dirname(info_name).replace("\\", "/")
                        if info_dir:
                            cover_name = f"{info_dir}/{cover_name}"
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

@app.context_processor
def inject_globals():
    return {
        "plc_ip": PLC_IP,
        "plc_port": PLC_PORT,
        "igniter_shutoff": IGNITER_SHUTOFF,
        "pumpkin_coils": PUMPKIN_COIL_LIST,
        "arm_required": ARM_REQUIRED,
        "dryrun": DRYRUN,
    }

# -----------------------------------------------------------------------------
# HTML (Search + Library + Panel + Show Control)
# -----------------------------------------------------------------------------
NAV_HTML = r"""
  <header class="flex items-center justify-between mb-4">
    <h1 class="text-2xl font-bold">The Fire Choir</h1>
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


@app.get("/")
def index():
    # templates/index.html
    return render_template("index.html")

@app.get("/library")
def library():
    # templates/library.html
    return render_template("library.html")

@app.get("/panel")
def pumpkin_panel():
    # templates/panel.html (Socket.IO connects on the client; globals are injected)
    return render_template("panel.html")

@app.get("/show/<show_id>")
def show_control(show_id):
    with SHOWS_LOCK:
        if show_id not in SHOWS:
            return jsonify({"error": "Not found"}), 404
    # templates/show_control.html
    return render_template("show_control.html")



# -----------------------------------------------------------------------------
# Socket.IO events (shared)
# -----------------------------------------------------------------------------
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

        #Web dryrun handling
@socketio.on("toggle_dryrun")
def toggle_dryrun(data):
    desired = bool(data.get("on", False))

    ok, msg = _set_dryrun(desired)
    if not ok:
        # tell just the requester it failed
        emit("error", {"message": msg})
        return

    # Broadcast the new dryrun state so all panels update
    emit("dryrun_state", {"on": DRYRUN}, broadcast=True)

    # Also re-broadcast the footer metadata to everyone (panel shows DRYRUN/arming text)
    emit("init", {
        "igniter_armed": STATE["igniter_armed"],
        "pumpkins": STATE["pumpkins"],
        "arm_required": ARM_REQUIRED,
        "plc": {"ip": PLC_IP, "port": PLC_PORT},
        "coils": {"pumpkins": PUMPKIN_COIL_LIST, "igniter_shutoff": IGNITER_SHUTOFF},
        "dryrun": DRYRUN,
    }, broadcast=True)


# -----------------------------------------------------------------------------
# Shutdown safety
# -----------------------------------------------------------------------------
@atexit.register
def _shutdown():
    _all_off_and_disarm()
    try:
        plc.close()
    except Exception:
        pass

# -----------------------------------------------------------------------------
# Downloader / Library APIs / Uploader
# -----------------------------------------------------------------------------
@app.post("/api/download")
def api_download():
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

from werkzeug.utils import secure_filename

@app.post("/api/upload")
def api_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file provided"}), 400

    # Basic extension check
    filename = secure_filename(f.filename)
    if not filename.lower().endswith(".zip"):
        return jsonify({"error": "Only .zip files are allowed"}), 400

    # Choose a non-colliding path in ZIP_DIR
    target = ZIP_DIR / filename
    if target.exists():
        stem, suffix, i = target.stem, target.suffix, 2
        while (ZIP_DIR / f"{stem}-{i}{suffix}").exists():
            i += 1
        target = ZIP_DIR / f"{stem}-{i}{suffix}"

    # Stream to disk with size guard
    written = 0
    try:
        with open(target, "wb") as out:
            while True:
                chunk = f.stream.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_DOWNLOAD_BYTES:
                    out.close()
                    target.unlink(missing_ok=True)
                    return jsonify({"error": "File too large"}), 413
                out.write(chunk)
    except Exception as e:
        logger.exception("api_upload: failed to save %s", filename)
        return jsonify({"error": "Failed to save"}), 500

    logger.info("api_upload: saved %s (%d bytes)", target.name, written)
    return jsonify({
        "ok": True,
        "rel": f"{ZIP_DIR}/{target.name}",
        "saved": str(target.resolve()),
        "bytes": written
    })


@app.get("/api/library")
def api_library():
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

@app.get("/covers/<path:zipname>")
def cover_from_zip(zipname):
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

# -----------------------------------------------------------------------------
# Map selection + Show runner APIs
# -----------------------------------------------------------------------------
@app.get("/api/maps")
def api_maps():
    raw = (request.args.get("zipRel") or "").strip()
    if not raw:
        return jsonify({"error": "Missing zipRel"}), 400

    try:
        base = ZIP_DIR.resolve()
        p = Path(raw)

        if p.is_absolute():
            # Absolute path: only accept if it lives under ZIP_DIR
            candidate = p.resolve()
            if base not in candidate.parents:
                return jsonify({"error": "Not found"}), 404
            path = candidate
        else:
            # Relative path: allow optional "<ZIP_DIR.name>/" prefix and normalize slashes
            rel = raw.lstrip("/\\")
            prefix = f"{ZIP_DIR.name}/"
            if rel.startswith(prefix):
                rel = rel[len(prefix):]
            # Normalize backslashes that might come from Windows-y callers
            rel = rel.replace("\\", "/")
            path = _safe_join_downloads(rel)

        with ZipFile(path, "r") as zf:
            names = zf.namelist()
        maps = [n for n in names if n.endswith(".dat") and n.lower() not in NON_MAP_DAT_FILES]
        maps.sort(key=lambda s: (s.count("/"), s.lower()))
        return jsonify({"ok": True, "zip_rel": f"{ZIP_DIR.name}/{path.name}", "maps": maps})

    except FileNotFoundError:
        return jsonify({"error": "Not found"}), 404
    except Exception:
        logger.exception("api_maps: failed for %s", raw)
        return jsonify({"error": "Unexpected error"}), 500


@app.post("/api/show")
def api_show_start():
    if not request.is_json:
        return jsonify({"error": "Expected JSON body"}), 400
    body = request.get_json(silent=True) or {}

    zip_rel  = (body.get("zipRel")  or "").strip()
    map_file = (body.get("mapFile") or "").strip() or None
    method   = (body.get("method")  or "").strip() or "normal"

    if not zip_rel:
        return jsonify({"error": "Missing zipRel"}), 400
    if not map_file:
        return jsonify({"error": "Missing mapFile"}), 400

    try:
        base = ZIP_DIR.resolve()
        p = Path(zip_rel)

        if p.is_absolute():
            candidate = p.resolve()
            if base not in candidate.parents:
                return jsonify({"error": "ZIP not found"}), 404
            path = candidate
        else:
            rel = zip_rel.lstrip("/\\")
            prefix = f"{ZIP_DIR.name}/"
            if rel.startswith(prefix):
                rel = rel[len(prefix):]
            rel = rel.replace("\\", "/")
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
            target=run_blocking_action,
            args=(path, stop_event, set_status, map_file, method),
            kwargs={"is_dryrun": DRYRUN},
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


# -----------------------------------------------------------------------------
# Entry point — IMPORTANT: use socketio.run to enable websockets
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    logger.info("Starting Flask-SocketIO on 0.0.0.0:%d (ZIP_DIR=%s, ASYNC_MODE=%s, DRYRUN=%s)", port, ZIP_DIR, _ASYNC_MODE, DRYRUN)
    # Werkzeug is fine for local dev with Flask-SocketIO if explicitly allowed.
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)
