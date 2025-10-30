"""
Flask + Socket.IO touch panel (single page) that uses your existing FireBeat PLC controllers:
- 4 *momentary* pumpkin controls (hold down = ON; release = OFF)
- 1 *toggle* igniter arm/disarm (uses PLCController.igniter_arm/disarm)
- Uses `FireBeat.plc_controller.PLCController` (live) or `PygamePLCController` (dryrun)

Safety:
- Interlock (configurable): pumpkins only fire when igniter is armed
- On client disconnect or shutdown: pumpkins OFF, igniter DISARMED

Run:
  pip install flask flask-socketio eventlet
  python app.py

Env overrides:
  DRYRUN=false        # true -> PygamePLCController
  ARM_REQUIRED=true   # requires ignite arm to fire pumpkins
  MAX_HOLD_SECONDS=0  # 0 = unlimited while held
"""

import os
import atexit
import threading
import time
from typing import List, Tuple

from flask import Flask, render_template_string
from flask_socketio import SocketIO, emit

# ---- Use your FireBeat stack ----
from FireBeat.plc_controller import PLCController, PygamePLCController
from FireBeat.constants import PLC_IP, PLC_PORT, PUMPKIN_COILS, IGNITER_SHUTOFF
from FireBeat.logger import logger

# ---- Config via env ----
DRYRUN = os.getenv("DRYRUN", "false").lower() in ("1","true","yes","y")
ARM_REQUIRED = os.getenv("ARM_REQUIRED", "true").lower() in ("1","true","yes","y")
MAX_HOLD_SECONDS = float(os.getenv("MAX_HOLD_SECONDS", "0"))

# Normalize pumpkin coil order (stable UI order). If your dict keys are like 'pumpkin1'..'pumpkin4',
# this will sort correctly; otherwise it falls back to key sort.
_sorted_pumpkin_items: List[Tuple[str,int]] = sorted(PUMPKIN_COILS.items(), key=lambda kv: kv[0])
PUMPKIN_COIL_LIST: List[int | None] = [v for _, v in _sorted_pumpkin_items][:4]
if len(PUMPKIN_COIL_LIST) < 4:
    # pad for UI with None (so coil 0 is valid if present)
    PUMPKIN_COIL_LIST += [None] * (4 - len(PUMPKIN_COIL_LIST))

# ---- PLC Controller selection ----
plc = PygamePLCController() if DRYRUN else PLCController(is_dryrun=False)

app = Flask(__name__)
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
            _pumpkin_off(idx)
            socketio.emit("pumpkin_state", {"index": idx, "on": False})
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


@app.route("/")
def index():
    return render_template_string(INDEX_HTML,
                                  plc_ip=PLC_IP,
                                  plc_port=PLC_PORT,
                                  igniter_shutoff=IGNITER_SHUTOFF,
                                  pumpkin_coils=PUMPKIN_COIL_LIST)


# ---- SocketIO events ----
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


# ---- Shutdown safety ----
@atexit.register
def _shutdown():
    _all_off_and_disarm()
    try:
        plc.close()
    except Exception:
        pass


# ---- Inline single-page UI ----
INDEX_HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Pumpkin Fire Panel</title>
  <style>
    :root { --bg:#0b0b0d; --card:#16161a; --accent:#2cb67d; --warn:#ef4444; --text:#eaeaea; --muted:#9ca3af; }
    * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
    body { margin:0; font-family: system-ui, -apple-system, Segoe UI, Roboto, Ubuntu, Cantarell, Noto Sans, sans-serif; background:var(--bg); color:var(--text); }
    .wrap { max-width: 960px; margin: 0 auto; padding: 16px; }
    .header { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:16px;}
    .card { background:var(--card); border-radius: 16px; padding: 16px; box-shadow: 0 10px 30px rgba(0,0,0,0.3); }
    .row { display:grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 16px; }
    .btn { user-select:none; -webkit-user-select:none; touch-action: manipulation; border: 2px solid rgba(255,255,255,0.06); border-radius: 18px; padding: 28px; font-size: 28px; font-weight: 700; text-align:center; background: linear-gradient(180deg, #1f2937, #0f172a); color:#fff; box-shadow: inset 0 -6px 0 rgba(255,255,255,0.04); }
    .btn:active { transform: scale(0.98); }
    .btn.on { outline: 3px solid var(--accent); box-shadow: 0 0 0 4px rgba(44,182,125,0.22), inset 0 -6px 0 rgba(255,255,255,0.04); }
    .btn.off { outline: 3px solid rgba(239,68,68,0.8); box-shadow: 0 0 0 4px rgba(239,68,68,0.2), inset 0 -6px 0 rgba(255,255,255,0.04); }
    .pill { display:inline-flex; align-items:center; gap:10px; border-radius: 999px; padding: 10px 14px; background: #111827; font-size: 14px; color: var(--muted); }
    .toggle { position:relative; width:70px; height:36px; background:#111827; border-radius: 20px; border:2px solid rgba(255,255,255,0.08); cursor:pointer; }
    .knob { position:absolute; top:2px; left:2px; width:30px; height:30px; border-radius:999px; background:#fff; transition: transform .18s ease; }
    .toggle.armed { background: #064e3b; border-color: rgba(44,182,125,0.4); }
    .toggle.armed .knob { transform: translateX(34px); background: #34d399; }
    .label { font-size:14px; color:var(--muted); }
    .status { font-weight:700; }
    .footer { margin-top: 12px; font-size: 12px; color: var(--muted); }
    .error { color: #fecaca; }
    button { all: unset; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="header">
      <div class="pill">PLC <span id="plc-host"></span> · Igniter shutoff coil <code id="ign-coil"></code></div>
      <div style="display:flex; align-items:center; gap:12px;">
        <div class="label">Igniter</div>
        <div id="igniter-toggle" class="toggle" role="switch" aria-checked="false" tabindex="0">
          <div class="knob"></div>
        </div>
        <div class="status" id="igniter-status">DISARMED</div>
      </div>
    </div>

    <div class="card">
      <div class="row">
        <button id="pumpkin-1" class="btn off" data-index="0">🎃 Pumpkin 1</button>
        <button id="pumpkin-2" class="btn off" data-index="1">🎃 Pumpkin 2</button>
        <button id="pumpkin-3" class="btn off" data-index="2">🎃 Pumpkin 3</button>
        <button id="pumpkin-4" class="btn off" data-index="3">🎃 Pumpkin 4</button>
      </div>
      <div class="footer" id="foot"></div>
      <div class="footer error" id="err"></div>
    </div>
  </div>

  <script src="https://cdn.socket.io/4.7.5/socket.io.min.js" crossorigin="anonymous"></script>
  <script>
    const socket = io();

    const ignToggle = document.getElementById('igniter-toggle');
    const ignStatus = document.getElementById('igniter-status');
    const errBox = document.getElementById('err');
    const foot = document.getElementById('foot');

    const pumpkins = [0,1,2,3].map(i => document.getElementById('pumpkin-' + (i+1)));
    const activePresses = new Set();

    function setIgniterUI(armed){
      ignToggle.classList.toggle('armed', armed);
      ignToggle.setAttribute('aria-checked', armed ? 'true':'false');
      ignStatus.textContent = armed ? 'ARMED' : 'DISARMED';
    }

    function setPumpkinUI(idx, on){
      const el = pumpkins[idx];
      el.classList.toggle('on', on);
      el.classList.toggle('off', !on);
    }

    function showError(msg){ errBox.textContent = msg || ''; if(msg){ setTimeout(()=>errBox.textContent='', 1500);} }

    function bindMomentary(button){
      const idx = parseInt(button.dataset.index);
      const down = (e)=>{ e.preventDefault(); if(activePresses.has(idx)) return; activePresses.add(idx); socket.emit('pumpkin_press', {index: idx}); };
      const up = (e)=>{ e.preventDefault(); if(!activePresses.has(idx)) return; activePresses.delete(idx); socket.emit('pumpkin_release', {index: idx}); };
      button.addEventListener('mousedown', down);
      button.addEventListener('mouseup', up);
      button.addEventListener('mouseleave', up);
      button.addEventListener('touchstart', down, {passive:false});
      button.addEventListener('touchend', up);
      button.addEventListener('touchcancel', up);
      button.addEventListener('keydown', (e)=>{ if(e.key===' '||e.key==='Enter'){ down(e);} });
      button.addEventListener('keyup', (e)=>{ if(e.key===' '||e.key==='Enter'){ up(e);} });
    }

    pumpkins.forEach(bindMomentary);

    function sendIgniter(desired){ socket.emit('toggle_igniter', {armed: !!desired}); }

    ignToggle.addEventListener('click', ()=>{
      const willArm = !ignToggle.classList.contains('armed');
      sendIgniter(willArm);
    });
    ignToggle.addEventListener('keydown', (e)=>{ if(e.key===' '||e.key==='Enter'){ e.preventDefault(); const willArm = !ignToggle.classList.contains('armed'); sendIgniter(willArm); } });

    socket.on('init', (s)=>{
      setIgniterUI(!!s.igniter_armed);
      (s.pumpkins||[]).forEach((on,i)=> setPumpkinUI(i, !!on));
      document.getElementById('plc-host').textContent = `${s.plc.ip}:${s.plc.port}`;
      document.getElementById('ign-coil').textContent = s.coils.igniter_shutoff;
      foot.textContent = (s.dryrun? 'DRYRUN · ':'') + (s.arm_required? 'Arming required':'Arming optional') + ' · Pumpkins ' + (s.coils.pumpkins||[]).join(',');
    });

    socket.on('igniter_state', ({armed})=> setIgniterUI(!!armed));
    socket.on('pumpkin_state', ({index,on})=> setPumpkinUI(index, !!on));
    socket.on('error', ({message})=> showError(message));

    window.addEventListener('beforeunload', ()=>{
      activePresses.forEach(idx=> socket.emit('pumpkin_release', {index: idx}));
    });
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    print("=== Pumpkin Fire Panel (FireBeat PLC) ===")
    print(f"PLC: {PLC_IP}:{PLC_PORT} | Dryrun={DRYRUN}")
    print(f"Pumpkin coils (ordered): {PUMPKIN_COIL_LIST} | Igniter shutoff coil: {IGNITER_SHUTOFF}")
    if ARM_REQUIRED:
        print("Interlock: Igniter must be ARMED for pumpkins to fire.")
    else:
        print("Interlock DISABLED: pumpkins can fire without arming (NOT RECOMMENDED).")
    # Use Werkzeug only for local/dev. Flask-SocketIO now blocks it unless explicitly allowed.
socketio.run(app, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)
