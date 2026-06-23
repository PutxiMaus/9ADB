import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify, request

app = Flask(__name__)

DATA_DIR = Path(os.environ.get("ADB_DATA_DIR", "/data"))
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
ANDROID_HOME_DIR = DATA_DIR / ".android"
ANDROID_HOME_DIR.mkdir(parents=True, exist_ok=True)

VISOR_PUBLIC_PORT = int(os.environ.get("VISOR_PUBLIC_PORT", "20010"))
VISOR_API_PORT = int(os.environ.get("VISOR_API_PORT", "19999"))
VISOR_DISPLAY = os.environ.get("VISOR_DISPLAY", ":99")
VISOR_SCREEN = os.environ.get("VISOR_SCREEN", "540x960x24")
VISOR_VNC_PORT = int(os.environ.get("VISOR_VNC_PORT", "5901"))
NOVNC_WEB_DIR = Path("/app/novnc-web")

NOVNC_INDEX = """<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Visor ADB</title>
  <meta http-equiv="refresh" content="0; url=/vnc.html?autoconnect=true&resize=scale&reconnect=true">
  <style>
    html,body{height:100%;margin:0;background:#05070b;color:#e8f0ff;font-family:system-ui,-apple-system,Segoe UI,sans-serif}
    body{display:grid;place-items:center}
    .box{max-width:520px;padding:28px;border:1px solid #263244;border-radius:22px;background:#111923;text-align:center}
    a{color:#7db5ff}
  </style>
</head>
<body>
  <div class="box">
    <h1>Visor ADB</h1>
    <p>Abriendo noVNC...</p>
    <p><a href="/vnc.html?autoconnect=true&resize=scale&reconnect=true">Abrir visor manualmente</a></p>
  </div>
  <script>location.replace("/vnc.html?autoconnect=true&resize=scale&reconnect=true");</script>
</body>
</html>
"""


def ensure_novnc_index() -> None:
    try:
        NOVNC_WEB_DIR.mkdir(parents=True, exist_ok=True)
        (NOVNC_WEB_DIR / "index.html").write_text(NOVNC_INDEX, encoding="utf-8")
        (NOVNC_WEB_DIR / "health").write_text("ok", encoding="utf-8")
    except Exception as exc:
        append_log("novnc index", str(exc))


_lock = threading.Lock()
_processes: Dict[str, Any] = {
    "xvfb": None,
    "vnc": None,
    "novnc": None,
    "scrcpy": None,
}
_scrcpy: Dict[str, Any] = {
    "serial": "",
    "started_at": "",
    "settings": {},
    "last_message": "",
}


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def append_log(name: str, text: str) -> None:
    path = LOG_DIR / "visor.log"
    clean = (text or "").replace("\r", "").strip()
    with path.open("a", encoding="utf-8") as f:
        f.write(f"[{now_text()}] {name} · {clean}\n")


def process_alive(proc: Any) -> bool:
    return bool(proc and getattr(proc, "poll", lambda: 1)() is None)


def stop_process(proc: Any, name: str) -> None:
    if not proc:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception as exc:
        append_log(f"stop {name}", str(exc))


def open_log(name: str):
    return (LOG_DIR / f"visor_{name}.log").open("ab")


def env() -> Dict[str, str]:
    e = os.environ.copy()
    e["HOME"] = str(DATA_DIR)
    e["DISPLAY"] = VISOR_DISPLAY
    e["LIBGL_ALWAYS_SOFTWARE"] = "1"
    e["SDL_VIDEODRIVER"] = "x11"
    return e


def run(cmd: List[str], timeout: int = 10) -> Tuple[bool, str]:
    try:
        completed = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            env=env(),
            text=True,
            errors="replace",
        )
        return completed.returncode == 0, completed.stdout or ""
    except subprocess.TimeoutExpired:
        return False, "Tiempo agotado."
    except Exception as exc:
        return False, str(exc)


def ensure_display_stack() -> Tuple[bool, str]:
    with _lock:
        e = env()

        if not process_alive(_processes.get("xvfb")):
            _processes["xvfb"] = subprocess.Popen(
                ["Xvfb", VISOR_DISPLAY, "-screen", "0", VISOR_SCREEN, "-ac", "-nolisten", "tcp"],
                stdout=open_log("xvfb"),
                stderr=open_log("xvfb"),
                env=e,
            )
            time.sleep(0.8)

        if not process_alive(_processes.get("vnc")):
            _processes["vnc"] = subprocess.Popen(
                [
                    "x11vnc",
                    "-display", VISOR_DISPLAY,
                    "-rfbport", str(VISOR_VNC_PORT),
                    "-forever",
                    "-shared",
                    "-nopw",
                    "-quiet",
                ],
                stdout=open_log("x11vnc"),
                stderr=open_log("x11vnc"),
                env=e,
            )
            time.sleep(0.8)

        if not process_alive(_processes.get("novnc")):
            ensure_novnc_index()
            _processes["novnc"] = subprocess.Popen(
                [
                    "websockify",
                    f"--web={NOVNC_WEB_DIR}",
                    f"0.0.0.0:{VISOR_PUBLIC_PORT}",
                    f"127.0.0.1:{VISOR_VNC_PORT}",
                ],
                stdout=open_log("novnc"),
                stderr=open_log("novnc"),
                env=e,
            )
            time.sleep(0.8)

        ok = process_alive(_processes.get("xvfb")) and process_alive(_processes.get("vnc")) and process_alive(_processes.get("novnc"))
        return ok, "Display/VNC/noVNC listo." if ok else "No se pudo iniciar todo el stack de visor."


def bool_setting(settings: Dict[str, Any], key: str, default: bool = False) -> bool:
    value = settings.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on", "si", "sí"}
    return bool(value)


def build_scrcpy_cmd(serial: str, settings: Dict[str, Any]) -> List[str]:
    max_size = int(settings.get("max_size") or 800)
    max_fps = int(settings.get("max_fps") or 20)
    bit_rate = str(settings.get("video_bit_rate") or "4M")

    cmd = [
        "scrcpy",
        "-s", serial,
        "--max-size", str(max_size),
        "--max-fps", str(max_fps),
        "--video-bit-rate", bit_rate,
        "--window-title", "ADB-SERVER scrcpy",
        "--window-borderless",
    ]

    if not bool_setting(settings, "audio", False):
        cmd.append("--no-audio")
    if not bool_setting(settings, "control", True):
        cmd.append("--no-control")
    if bool_setting(settings, "turn_screen_off", False):
        cmd.append("--turn-screen-off")
    if bool_setting(settings, "stay_awake", True):
        cmd.append("--stay-awake")
    if bool_setting(settings, "fullscreen", True):
        cmd.append("--fullscreen")

    return cmd


def stop_scrcpy_only() -> None:
    with _lock:
        stop_process(_processes.get("scrcpy"), "scrcpy")
        _processes["scrcpy"] = None
        _scrcpy.update({"serial": "", "started_at": "", "last_message": "Scrcpy detenido."})


@app.get("/health")
def health():
    return "ok"


@app.get("/api/scrcpy/status")
def status():
    ensure_display_stack()
    running = process_alive(_processes.get("scrcpy"))
    return jsonify({
        "ok": True,
        "running": running,
        "serial": _scrcpy.get("serial") if running else "",
        "started_at": _scrcpy.get("started_at") if running else "",
        "settings": _scrcpy.get("settings") if running else {},
        "message": _scrcpy.get("last_message", ""),
        "viewer_url": f"/vnc.html?autoconnect=true&resize=scale&reconnect=true",
        "public_port": VISOR_PUBLIC_PORT,
    })


@app.post("/api/scrcpy/start")
def start_scrcpy():
    payload = request.get_json(silent=True) or {}
    serial = str(payload.get("serial") or "").strip()
    settings = payload.get("settings") or {}
    if not serial:
        return jsonify({"ok": False, "message": "No se recibió dispositivo activo."})

    ok, msg = ensure_display_stack()
    if not ok:
        return jsonify({"ok": False, "message": msg})

    ok_dev, out_dev = run(["adb", "-s", serial, "get-state"], timeout=8)
    if not ok_dev or "device" not in out_dev:
        msg = f"ADB no ve el dispositivo {serial}: {out_dev.strip()}"
        append_log("start", msg)
        return jsonify({"ok": False, "message": msg})

    with _lock:
        current = _processes.get("scrcpy")
        if process_alive(current):
            if _scrcpy.get("serial") == serial and _scrcpy.get("settings") == settings:
                return jsonify({"ok": True, "message": f"Scrcpy ya está activo en {serial}.", "running": True})
            stop_process(current, "scrcpy")
            _processes["scrcpy"] = None

        cmd = build_scrcpy_cmd(serial, settings)
        append_log("scrcpy cmd", " ".join(shlex.quote(x) for x in cmd))
        proc = subprocess.Popen(
            cmd,
            stdout=open_log("scrcpy"),
            stderr=open_log("scrcpy"),
            env=env(),
        )
        _processes["scrcpy"] = proc
        _scrcpy.update({
            "serial": serial,
            "started_at": now_text(),
            "settings": settings,
            "last_message": "Scrcpy arrancando.",
        })

    time.sleep(1.8)
    if not process_alive(_processes.get("scrcpy")):
        try:
            tail = (LOG_DIR / "visor_scrcpy.log").read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
            msg = "\n".join(tail) or "Scrcpy se cerró al arrancar."
        except Exception:
            msg = "Scrcpy se cerró al arrancar."
        append_log("scrcpy error", msg)
        stop_scrcpy_only()
        return jsonify({"ok": False, "message": msg})

    msg = f"Scrcpy iniciado en {serial}. Visor en puerto {VISOR_PUBLIC_PORT}."
    _scrcpy["last_message"] = msg
    append_log("start", msg)
    return jsonify({"ok": True, "message": msg, "running": True, "serial": serial})


@app.post("/api/scrcpy/stop")
def stop_scrcpy():
    stop_scrcpy_only()
    append_log("stop", "Scrcpy detenido.")
    return jsonify({"ok": True, "message": "Scrcpy detenido.", "running": False})


@app.get("/api/logs")
def logs():
    result: Dict[str, Any] = {"ok": True, "logs": {}}
    for name in ("visor.log", "visor_scrcpy.log", "visor_xvfb.log", "visor_x11vnc.log", "visor_novnc.log"):
        path = LOG_DIR / name
        if path.exists():
            result["logs"][name] = "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-120:])
        else:
            result["logs"][name] = ""
    return jsonify(result)


if __name__ == "__main__":
    ensure_novnc_index()
    ok, msg = ensure_display_stack()
    append_log("boot", msg)
    app.run(host="0.0.0.0", port=VISOR_API_PORT, threaded=True)
