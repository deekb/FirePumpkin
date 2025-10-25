#!/usr/bin/env python3
"""
Beat Saber Beatmap Transcoder (v2 <-> v3)

- Converts core gameplay objects between legacy v2 difficulty .dat files and v3 (3.0–3.3) schema.
- Focus: color notes, bomb notes, obstacles, BPM events, basic lighting events, and lane rotation events.
- Attempts to preserve unknown fields via pass-through when possible, and stores leftovers in `customData`/`_customData`.

This is NOT a full fidelity converter for every exotic/lightshow construct.
It aims to make legacy charts playable on modern schema and vice versa without crashing.

Usage
-----
python beatsaber_transcoder.py in.json --to v3 > out.json
python beatsaber_transcoder.py in.json --to v2 > out.json

You can also import `transcode_beatmap(data, target)` from Python.
"""

import json
import sys
from copy import deepcopy
from typing import Dict, Any, List, Tuple

V2_DEFAULT = "2.6.0"   # choose 2.6.0 to allow height/lineLayer on obstacles
V3_DEFAULT = "3.2.0"   # safe baseline across 3.x examples

def _ensure_list(d: Dict[str, Any], key: str) -> List[Any]:
    v = d.get(key) or []
    return v if isinstance(v, list) else []

# -------------------------
# v2 -> v3 helpers
# -------------------------

def v2_note_to_v3(entry: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """
    Returns (collection_name, v3_object)
    - "_type" 0/1 => colorNotes
    - "_type" 3   => bombNotes
    """
    t = entry.get("_type")
    if t == 3:  # bomb
        obj = {
            "b": entry.get("_time", 0),
            "x": entry.get("_lineIndex", 0),
            "y": entry.get("_lineLayer", 0),
        }
        # pass through custom
        cd = entry.get("_customData") or entry.get("customData")
        if cd:
            obj["customData"] = cd
        return "bombNotes", obj

    # color note
    c = 0 if t == 0 else 1 if t == 1 else 0  # default left if unknown
    obj = {
        "b": entry.get("_time", 0),
        "x": entry.get("_lineIndex", 0),
        "y": entry.get("_lineLayer", 0),
        "c": c,
        "d": entry.get("_cutDirection", 0),
        "a": entry.get("_angleOffset", 0) or 0,
    }
    cd = entry.get("_customData") or entry.get("customData")
    if cd:
        obj["customData"] = cd
    return "colorNotes", obj

def v2_obstacle_to_v3(entry: Dict[str, Any]) -> Dict[str, Any]:
    """
    v2 obstacle fields:
      - _type (0 vertical wall, 1 ceiling, 2 height/lineLayer-aware in v2.6)
      - _time, _duration, _lineIndex, _width
      - optional: _lineLayer, _height (>= 2.6.0)
    v3 obstacle fields:
      - b, d, x, y, w, h
    Heuristics for older v2:
      - if _lineLayer/_height missing:
          _type 0 -> y=0, h=5
          _type 1 -> y=2, h=3  (ceiling)
          else    -> y=0, h=5
    """
    otype = entry.get("_type", 0)
    y = entry.get("_lineLayer")
    h = entry.get("_height")
    if y is None or h is None:
        if otype == 1:  # ceiling
            y, h = 2, 3
        else:  # default full wall
            y, h = 0, 5

    obj = {
        "b": entry.get("_time", 0),
        "d": entry.get("_duration", 0),
        "x": entry.get("_lineIndex", 0),
        "y": y,
        "w": entry.get("_width", 0),
        "h": h,
    }
    cd = entry.get("_customData") or entry.get("customData")
    if cd:
        obj["customData"] = cd
    return obj

def v2_event_to_v3_basic(evt: Dict[str, Any]) -> Dict[str, Any]:
    """
    Basic lighting/value events (legacy) in v3: basicBeatmapEvents with keys b, et, i, f
    """
    obj = {
        "b": evt.get("_time", 0),
        "et": evt.get("_type", 0),
        "i": evt.get("_value", 0),
    }
    if "_floatValue" in evt:
        obj["f"] = evt.get("_floatValue", 0)
    # pass-through custom
    cd = evt.get("_customData") or evt.get("customData")
    if cd:
        obj["customData"] = cd
    return obj

def v2_event_to_v3(evt: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Any]]:
    """
    Split special cases:
      - BPM change: _type==100, _floatValue = BPM -> bpmEvents [{"b","m"}]
      - Lane rotation: _type in {14,15} -> rotationEvents [{"b","e","r"}]  e=0 early, e=1 late, r=_value
      - Otherwise basicBeatmapEvents
    """
    t = evt.get("_type")
    if t == 100:
        return "bpmEvents", {"b": evt.get("_time", 0), "m": evt.get("_floatValue", 0)}
    if t in (14, 15):
        e = 0 if t == 14 else 1
        return "rotationEvents", {"b": evt.get("_time", 0), "e": e, "r": evt.get("_value", 0)}
    return "basicBeatmapEvents", v2_event_to_v3_basic(evt)

def convert_v2_to_v3(data: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "version": V3_DEFAULT,
        "bpmEvents": [],
        "rotationEvents": [],
        "colorNotes": [],
        "bombNotes": [],
        "obstacles": [],
        # v3 also supports sliders/chains but we only pass through if already present in v2.6
    }

    # Notes -> colorNotes/bombNotes
    for n in _ensure_list(data, "_notes"):
        col, obj = v2_note_to_v3(n)
        out[col].append(obj)

    # Obstacles
    for o in _ensure_list(data, "_obstacles"):
        out["obstacles"].append(v2_obstacle_to_v3(o))

    # Events
    for e in _ensure_list(data, "_events"):
        target, obj = v2_event_to_v3(e)
        if target:
            out[target].append(obj)

    # Sliders (v2.6) -> sliders
    for s in _ensure_list(data, "_sliders"):
        # best-effort map to v3 "sliders"
        out.setdefault("sliders", []).append({
            "c": s.get("_colorType", 0),
            "b": s.get("_headTime", 0),
            "x": s.get("_headLineIndex", 0),
            "y": s.get("_headLineLayer", 0),
            "d": s.get("_headCutDirection", 0),
            "mu": s.get("_headControlPointLengthMultiplier", 1),
            "tb": s.get("_tailTime", 0),
            "tx": s.get("_tailLineIndex", 0),
            "ty": s.get("_tailLineLayer", 0),
            "tc": s.get("_tailCutDirection", 0),
            "tmu": s.get("_tailControlPointLengthMultiplier", 1),
            "m": s.get("_sliderMidAnchorMode", 0),
        })

    # waypoints -> not part of v3 interactable; retain as custom
    if "_waypoints" in data:
        out.setdefault("customData", {})["legacyWaypoints"] = data["_waypoints"]

    # copy unknown top-level fields into customData
    for k, v in data.items():
        if k in {"_version","_notes","_obstacles","_events","_sliders","_waypoints"}:
            continue
        out.setdefault("customData", {})[k] = deepcopy(v)

    return out

# -------------------------
# v3 -> v2 helpers
# -------------------------

def v3_colornote_to_v2(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "_time": entry.get("b", 0),
        "_lineIndex": entry.get("x", 0),
        "_lineLayer": entry.get("y", 0),
        "_type": 0 if entry.get("c", 0) == 0 else 1,
        "_cutDirection": entry.get("d", 0),
        "_angleOffset": entry.get("a", 0) or 0,
        **({"_customData": entry["customData"]} if "customData" in entry else {}),
    }

def v3_bomb_to_v2(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "_time": entry.get("b", 0),
        "_lineIndex": entry.get("x", 0),
        "_lineLayer": entry.get("y", 0),
        "_type": 3,
        "_cutDirection": 0,
        **({"_customData": entry["customData"]} if "customData" in entry else {}),
    }

def v3_obstacle_to_v2(entry: Dict[str, Any]) -> Dict[str, Any]:
    """
    If height/lineLayer exists -> use v2.6 style with _type=2
    Else fall back to classic:
      - if y>=2: overhead -> _type=1
      - else    : vertical -> _type=0
    """
    y = entry.get("y", 0)
    h = entry.get("h")
    supports_v26 = h is not None

    if supports_v26:
        otype = 2
        out = {
            "_type": otype,
            "_time": entry.get("b", 0),
            "_duration": entry.get("d", 0),
            "_lineIndex": entry.get("x", 0),
            "_lineLayer": y,
            "_width": entry.get("w", 0),
            "_height": h,
        }
    else:
        otype = 1 if y >= 2 else 0
        out = {
            "_type": otype,
            "_time": entry.get("b", 0),
            "_duration": entry.get("d", 0),
            "_lineIndex": entry.get("x", 0),
            "_width": entry.get("w", 0),
        }

    if "customData" in entry:
        out["_customData"] = entry["customData"]
    return out

def v3_basic_to_v2(evt: Dict[str, Any]) -> Dict[str, Any]:
    out = {
        "_time": evt.get("b", 0),
        "_type": evt.get("et", 0),
        "_value": evt.get("i", 0),
    }
    if "f" in evt:
        out["_floatValue"] = evt.get("f", 0)
    if "customData" in evt:
        out["_customData"] = evt["customData"]
    return out

def convert_v3_to_v2(data: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "_version": V2_DEFAULT,
        "_notes": [],
        "_obstacles": [],
        "_events": [],
    }

    for n in _ensure_list(data, "colorNotes"):
        out["_notes"].append(v3_colornote_to_v2(n))
    for n in _ensure_list(data, "bombNotes"):
        out["_notes"].append(v3_bomb_to_v2(n))
    for o in _ensure_list(data, "obstacles"):
        out["_obstacles"].append(v3_obstacle_to_v2(o))

    # bpmEvents -> _type 100
    for e in _ensure_list(data, "bpmEvents"):
        out["_events"].append({
            "_time": e.get("b", 0),
            "_type": 100,
            "_value": 0,
            "_floatValue": e.get("m", 0),
        })

    # rotationEvents -> types 14/15
    for e in _ensure_list(data, "rotationEvents"):
        t = 14 if e.get("e", 0) == 0 else 15
        out["_events"].append({
            "_time": e.get("b", 0),
            "_type": t,
            "_value": e.get("r", 0),
        })

    # basicBeatmapEvents -> legacy lighting
    for e in _ensure_list(data, "basicBeatmapEvents"):
        out["_events"].append(v3_basic_to_v2(e))

    # sliders/chains -> best-effort to v2.6 sliders
    for s in _ensure_list(data, "sliders"):
        out.setdefault("_sliders", []).append({
            "_colorType": s.get("c", 0),
            "_headTime": s.get("b", 0),
            "_headLineIndex": s.get("x", 0),
            "_headLineLayer": s.get("y", 0),
            "_headCutDirection": s.get("d", 0),
            "_headControlPointLengthMultiplier": s.get("mu", 1),
            "_tailTime": s.get("tb", 0),
            "_tailLineIndex": s.get("tx", 0),
            "_tailLineLayer": s.get("ty", 0),
            "_tailCutDirection": s.get("tc", 0),
            "_tailControlPointLengthMultiplier": s.get("tmu", 1),
            "_sliderMidAnchorMode": s.get("m", 0),
        })

    # If v3 had waypoints in customData legacy bucket, try to restore
    cd = data.get("customData") or {}
    if "legacyWaypoints" in cd:
        out["_waypoints"] = cd["legacyWaypoints"]

    # Copy unknown top-level fields
    for k, v in data.items():
        if k in {"version","bpmEvents","rotationEvents","colorNotes","bombNotes","obstacles","sliders","basicBeatmapEvents","customData"}:
            continue
        out.setdefault("_customData", {})[k] = deepcopy(v)

    return out

# -------------------------
# Public API
# -------------------------

def transcode_beatmap(data: Dict[str, Any], target: str) -> Dict[str, Any]:
    """
    target: "v2" or "v3"
    """
    if target not in {"v2", "v3"}:
        raise ValueError("target must be 'v2' or 'v3'")
    # detect if already that version (best-effort)
    if target == "v3":
        return convert_v2_to_v3(data) if "_version" in data else deepcopy(data)
    else:
        return convert_v3_to_v2(data) if "version" in data else deepcopy(data)

# -------------------------
# CLI
# -------------------------

def main(argv=None):
    argv = argv or sys.argv[1:]
    if not argv or "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0
    if "--to" not in argv:
        print("ERROR: missing --to {v2|v3}", file=sys.stderr)
        return 2
    to_idx = argv.index("--to")
    try:
        target = argv[to_idx+1]
    except Exception:
        print("ERROR: --to must be followed by v2 or v3", file=sys.stderr)
        return 2

    # read file or stdin
    in_path = None
    candidates = [a for a in argv if not a.startswith("-") and a != target]
    if candidates:
        in_path = candidates[0]
        with open(in_path, "r", encoding="utf-8") as f:
            inp = json.load(f)
    else:
        inp = json.load(sys.stdin)

    out = transcode_beatmap(inp, target.lower())
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=False)
    print()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
