import datetime as _dt
import ipaddress
import json
import os
import re
import shutil
import shlex
import socket
import subprocess
import tempfile
import threading
import time
import uuid
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, Response, abort, jsonify, render_template, request, send_file

app = Flask(__name__)


@app.errorhandler(Exception)
def api_error_handler(exc: Exception):
    # Evita que la web reciba una página HTML de Flask con "500 Internal Server Error".
    # En rutas API siempre devolvemos JSON legible para los logs de la pestaña.
    if request.path.startswith("/api/"):
        append_log("Error interno API", ok=False, output=f"{request.path}: {exc}")
        return jsonify({"ok": False, "message": f"Error interno en {request.path}: {exc}"}), 200
    raise exc

DATA_DIR = Path(os.environ.get("ADB_DATA_DIR", "/data"))
PROFILES_FILE = DATA_DIR / "profiles.json"
STATE_FILE = DATA_DIR / "state.json"
SCRCPY_SETTINGS_FILE = DATA_DIR / "scrcpy_settings.json"
VISOR_API_URL = os.environ.get("VISOR_API_URL", "http://127.0.0.1:19999")
VISOR_PUBLIC_PORT = int(os.environ.get("VISOR_PUBLIC_PORT", "20010"))
APP_LABEL_CACHE_FILE = DATA_DIR / "app_labels.json"
SCRIPTS_FILE = DATA_DIR / "scripts.json"
LOG_DIR = DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "adb.log"
SCREENSHOT_DIR = DATA_DIR / "screenshots"
SCREENRECORD_DIR = DATA_DIR / "screenrecords"
APK_PULL_DIR = DATA_DIR / "apks"
FILES_DIR = DATA_DIR / "files"
UPLOADS_DIR = DATA_DIR / "uploads"
FASTBOOT_DIR = DATA_DIR / "fastboot"
TEMP_DOWNLOAD_DIR = Path("/tmp/9adb_transfers")
ANDROID_HOME_DIR = DATA_DIR / ".android"
TOOLS_DIR = Path(__file__).resolve().parent / "tools"
WALLPAPER_AGENT_APK = TOOLS_DIR / "WallpaperAgent.apk"
WALLPAPER_AGENT_PACKAGE = "com.example.wallpaperchanger"
WALLPAPER_AGENT_RECEIVER = "com.example.wallpaperchanger/.WallpaperReceiver"

for folder in (DATA_DIR, LOG_DIR, SCREENSHOT_DIR, SCREENRECORD_DIR, APK_PULL_DIR, FILES_DIR, UPLOADS_DIR, FASTBOOT_DIR, TEMP_DOWNLOAD_DIR, ANDROID_HOME_DIR):
    folder.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()
_screenrecord_processes: Dict[str, Dict[str, Any]] = {}
_screencap_lock = threading.Lock()
_cache_job: Dict[str, Any] = {"running": False, "started_at": "", "finished_at": "", "message": "", "serial": ""}

DEFAULT_STATE = {
    "active_device": "",
    "active_profile_id": "",
    "last_network_scan": [],
    "last_network_scan_at": "",
    "last_network_range": "",
    "recent_devices": [],
    "screen_mode": "none",
    "screen_interval_ms": 1200,
    "screen_started_at": "",
}

DEFAULT_SCRCPY_SETTINGS = {
    "max_size": 800,
    "max_fps": 20,
    "video_bit_rate": "4M",
    "audio": False,
    "control": True,
    "turn_screen_off": False,
    "stay_awake": True,
    "fullscreen": True,
}


def scrcpy_settings() -> Dict[str, Any]:
    data = read_json(SCRCPY_SETTINGS_FILE, DEFAULT_SCRCPY_SETTINGS.copy())
    if not isinstance(data, dict):
        data = {}
    merged = DEFAULT_SCRCPY_SETTINGS.copy()
    merged.update(data)

    def int_allowed(value: Any, allowed: List[int], fallback: int) -> int:
        try:
            value = int(value)
        except Exception:
            return fallback
        return value if value in allowed else fallback

    merged["max_size"] = int_allowed(merged.get("max_size"), [480, 720, 800, 1024, 1280], 800)
    merged["max_fps"] = int_allowed(merged.get("max_fps"), [15, 20, 30, 60], 20)
    if str(merged.get("video_bit_rate")) not in {"1M", "2M", "4M", "6M", "8M", "12M"}:
        merged["video_bit_rate"] = "4M"
    for key in ("audio", "control", "turn_screen_off", "stay_awake", "fullscreen"):
        merged[key] = bool(merged.get(key))
    return merged


def save_scrcpy_settings(data: Dict[str, Any]) -> Dict[str, Any]:
    current = scrcpy_settings()
    current.update(data or {})
    normalized = DEFAULT_SCRCPY_SETTINGS.copy()
    normalized.update(current)
    # Re-normalize through scrcpy_settings by temporary direct validation.
    write_json(SCRCPY_SETTINGS_FILE, normalized)
    normalized = scrcpy_settings()
    write_json(SCRCPY_SETTINGS_FILE, normalized)
    return normalized


def visor_proxy(path: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 4.0) -> Dict[str, Any]:
    url = VISOR_API_URL.rstrip("/") + path
    data = None
    headers = {"Content-Type": "application/json"}
    method = "GET"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        method = "POST"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = {"ok": 200 <= res.status < 300, "message": raw}
            return parsed
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"ok": False, "message": raw or str(exc)}
        parsed["ok"] = False
        return parsed
    except Exception as exc:
        return {"ok": False, "message": f"Visor ADB no responde en {VISOR_API_URL}: {exc}"}


QUICK_COMMANDS = {
    "home": {"label": "Home", "cmd": ["shell", "input", "keyevent", "3"]},
    "back": {"label": "Atrás", "cmd": ["shell", "input", "keyevent", "4"]},
    "recents": {"label": "Recientes", "cmd": ["shell", "input", "keyevent", "187"]},
    "power": {"label": "Power", "cmd": ["shell", "input", "keyevent", "26"]},
    "volume_up": {"label": "Volumen +", "cmd": ["shell", "input", "keyevent", "24"]},
    "volume_down": {"label": "Volumen -", "cmd": ["shell", "input", "keyevent", "25"]},
    "mute": {"label": "Mute", "cmd": ["shell", "input", "keyevent", "164"]},
    "notifications": {"label": "Notificaciones", "cmd": ["shell", "cmd", "statusbar", "expand-notifications"]},
    "quick_settings": {"label": "Ajustes rápidos", "cmd": ["shell", "cmd", "statusbar", "expand-settings"]},
    "settings": {"label": "Ajustes", "cmd": ["shell", "am", "start", "-a", "android.settings.SETTINGS"]},
    "play_store": {"label": "Play Store", "packages": ["com.android.vending"]},
    "youtube": {"label": "YouTube", "packages": ["com.google.android.youtube"]},
    "chrome": {"label": "Chrome", "packages": ["com.android.chrome", "com.chrome.beta"]},
    "gmail": {"label": "Gmail", "packages": ["com.google.android.gm"]},
    "maps": {"label": "Maps", "packages": ["com.google.android.apps.maps"]},
    "photos": {"label": "Fotos/Galería", "packages": ["com.google.android.apps.photos", "com.sec.android.gallery3d", "com.miui.gallery", "com.android.gallery3d"]},
    "camera": {"label": "Cámara", "cmds": [
        ["shell", "am", "start", "-a", "android.media.action.IMAGE_CAPTURE"],
        ["shell", "am", "start", "-a", "android.media.action.STILL_IMAGE_CAMERA"],
        ["shell", "am", "start", "-a", "android.media.action.STILL_IMAGE_CAMERA", "-c", "android.intent.category.DEFAULT"],
        ["shell", "monkey", "-p", "com.motorola.camera3", "-c", "android.intent.category.LAUNCHER", "1"],
        ["shell", "monkey", "-p", "com.motorola.camera", "-c", "android.intent.category.LAUNCHER", "1"],
        ["shell", "monkey", "-p", "com.motorola.cameraone", "-c", "android.intent.category.LAUNCHER", "1"],
        ["shell", "monkey", "-p", "com.android.camera2", "-c", "android.intent.category.LAUNCHER", "1"],
        ["shell", "monkey", "-p", "com.google.android.GoogleCamera", "-c", "android.intent.category.LAUNCHER", "1"],
        ["shell", "monkey", "-p", "com.sec.android.app.camera", "-c", "android.intent.category.LAUNCHER", "1"],
        ["shell", "monkey", "-p", "com.miui.camera", "-c", "android.intent.category.LAUNCHER", "1"],
        ["shell", "monkey", "-p", "com.android.camera", "-c", "android.intent.category.LAUNCHER", "1"],
    ]},
    "phone": {"label": "Teléfono", "packages": ["com.google.android.dialer", "com.android.dialer", "com.samsung.android.dialer"]},
    "messages": {"label": "Mensajes", "packages": ["com.google.android.apps.messaging", "com.samsung.android.messaging", "com.android.mms"]},
    "contacts": {"label": "Contactos", "packages": ["com.google.android.contacts", "com.android.contacts", "com.samsung.android.contacts"]},
    "calendar": {"label": "Calendario", "packages": ["com.google.android.calendar", "com.samsung.android.calendar", "com.android.calendar"]},
    "clock": {"label": "Reloj", "packages": ["com.google.android.deskclock", "com.android.deskclock", "com.sec.android.app.clockpackage"]},
    "calculator": {"label": "Calculadora", "packages": ["com.google.android.calculator", "com.android.calculator2", "com.sec.android.app.popupcalculator", "com.miui.calculator"]},
}


DEFAULT_SCRIPTS = {}

LEGACY_DEFAULT_SCRIPT_IDS = {
    "home_back_power",
    "tcpip_5555",
    "open_notifications",
    "unlock_swipe",
}




def now_text() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_json_file(path: Path, default: Any) -> None:
    if not path.exists():
        path.write_text(json.dumps(default, indent=2, ensure_ascii=False), encoding="utf-8")


def read_json(path: Path, default: Any) -> Any:
    ensure_json_file(path, default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        backup = path.with_suffix(path.suffix + f".broken-{int(time.time())}")
        try:
            path.rename(backup)
        except Exception:
            pass
        path.write_text(json.dumps(default, indent=2, ensure_ascii=False), encoding="utf-8")
        return default


def write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def profiles() -> Dict[str, Any]:
    data = read_json(PROFILES_FILE, {})
    if isinstance(data, dict):
        return data
    return {}


def save_profiles(data: Dict[str, Any]) -> None:
    write_json(PROFILES_FILE, data)


def scripts() -> Dict[str, Any]:
    data = read_json(SCRIPTS_FILE, {})
    if not isinstance(data, dict):
        data = {}

    # Fase 5.1: los cuatro scripts semilla de la Fase 5 ya no deben reaparecer.
    # Si quedaron guardados en /srv/data/adb/scripts.json, se eliminan una sola vez.
    changed = False
    for sid in list(data.keys()):
        item = data.get(sid) or {}
        if sid in LEGACY_DEFAULT_SCRIPT_IDS and item.get("created_at") == "default":
            data.pop(sid, None)
            changed = True
    if changed:
        save_scripts(data)
    return data


def save_scripts(data: Dict[str, Any]) -> None:
    write_json(SCRIPTS_FILE, data)


def clean_script_id(text: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(text or "script").strip().lower()).strip("-") or "script"
    return base[:64]


def normalize_script_commands(raw: Any) -> List[str]:
    if isinstance(raw, list):
        lines = [str(x).strip() for x in raw]
    else:
        lines = str(raw or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    result: List[str] = []
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        result.append(text)
    return result


def parse_adb_script_line(line: str) -> Tuple[bool, List[str], str]:
    text = str(line or "").strip()
    if not text:
        return False, [], "Comando vacío."
    try:
        args = shlex.split(text)
    except ValueError as exc:
        return False, [], f"No se pudo leer la línea: {exc}"
    if not args:
        return False, [], "Comando vacío."
    if args[0] == "adb":
        args = args[1:]
    if args and args[0] == "-s":
        return False, [], "No pongas adb -s en scripts. 9ADB añade el dispositivo activo automáticamente."
    forbidden = {"shell;", "&&", "||", "|"}
    if args[0] in forbidden:
        return False, [], "Operador no permitido al inicio. Escribe solo argumentos de adb."
    return True, args, ""


def script_to_public(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": item.get("id", ""),
        "name": item.get("name", "Sin nombre"),
        "description": item.get("description", ""),
        "commands": item.get("commands", []),
        "created_at": item.get("created_at", ""),
        "updated_at": item.get("updated_at", ""),
    }



def state() -> Dict[str, Any]:
    data = read_json(STATE_FILE, DEFAULT_STATE.copy())
    if not isinstance(data, dict):
        return DEFAULT_STATE.copy()
    merged = DEFAULT_STATE.copy()
    merged.update(data)
    return merged


def save_state(data: Dict[str, Any]) -> None:
    merged = DEFAULT_STATE.copy()
    merged.update(data)
    write_json(STATE_FILE, merged)


def append_log(title: str, ok: bool = True, command: Optional[List[str]] = None, output: str = "") -> None:
    status = "OK" if ok else "ERROR"
    cmd_text = ""
    if command:
        cmd_text = " | " + " ".join(command)
    clean_output = (output or "").replace("\r", "").strip()
    with _lock:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"[{now_text()}] {status} · {title}{cmd_text}\n")
            if clean_output:
                for line in clean_output.splitlines()[:80]:
                    f.write(f"  {line}\n")
            f.write("\n")


def run_cmd(args: List[str], timeout: int = 20, log_title: Optional[str] = None) -> Tuple[bool, str, int]:
    env = os.environ.copy()
    env["HOME"] = str(DATA_DIR)
    try:
        completed = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            env=env,
        )
        output = completed.stdout or ""
        ok = completed.returncode == 0
        if log_title:
            append_log(log_title, ok=ok, command=args, output=output)
        return ok, output, completed.returncode
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        output += "\nTiempo agotado."
        if log_title:
            append_log(log_title, ok=False, command=args, output=output)
        return False, output, 124
    except Exception as exc:
        output = str(exc)
        if log_title:
            append_log(log_title, ok=False, command=args, output=output)
        return False, output, 1


def adb_base() -> List[str]:
    return ["adb"]


def adb_version() -> str:
    ok, out, _ = run_cmd(["adb", "version"], timeout=8)
    return out.strip() if ok else out.strip()


def parse_adb_devices(output: str) -> List[Dict[str, str]]:
    devices: List[Dict[str, str]] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial = parts[0]
        status = parts[1]
        detail = " ".join(parts[2:])
        item = {
            "serial": serial,
            "status": status,
            "detail": detail,
            "model": "",
            "device": "",
            "transport_id": "",
            "kind": "wifi" if re.match(r"^\d+\.\d+\.\d+\.\d+:\d+$", serial) else "usb",
        }
        for part in parts[2:]:
            if part.startswith("model:"):
                item["model"] = part.split(":", 1)[1]
            elif part.startswith("device:"):
                item["device"] = part.split(":", 1)[1]
            elif part.startswith("transport_id:"):
                item["transport_id"] = part.split(":", 1)[1]
        devices.append(item)
    return devices


def adb_devices(log: bool = False) -> List[Dict[str, str]]:
    ok, out, _ = run_cmd(["adb", "devices", "-l"], timeout=15, log_title="Listar dispositivos ADB" if log else None)
    if not ok:
        return []
    return parse_adb_devices(out)


def normalize_endpoint(host: str, port: Any = 5555) -> str:
    host = (host or "").strip()
    if not host:
        return ""
    if ":" in host and re.match(r"^[^\s:]+:\d+$", host):
        return host
    try:
        port_int = int(port or 5555)
    except Exception:
        port_int = 5555
    return f"{host}:{port_int}"


def find_device(serial: str) -> Optional[Dict[str, str]]:
    for item in adb_devices():
        if item["serial"] == serial:
            return item
    return None


def active_serial() -> str:
    return state().get("active_device", "") or ""


def require_active() -> Tuple[str, str]:
    serial = active_serial()
    if not serial:
        return "", "No hay dispositivo activo. Selecciona uno conectado o conecta un perfil."
    dev = find_device(serial)
    if not dev:
        return "", f"El dispositivo activo ({serial}) ya no aparece en adb devices -l."
    if dev.get("status") != "device":
        return "", f"El dispositivo activo aparece como {dev.get('status')}, no como device."
    return serial, ""


def first_usb_device() -> str:
    for d in adb_devices():
        if d.get("kind") == "usb" and d.get("status") == "device":
            return d["serial"]
    return ""


def set_active(serial: str, profile_id: str = "") -> Tuple[bool, str]:
    serial = (serial or "").strip()
    if not serial:
        s = state()
        s["active_device"] = ""
        s["active_profile_id"] = ""
        save_state(s)
        append_log("Dispositivo activo limpiado", ok=True)
        return True, "Dispositivo activo limpiado."

    dev = find_device(serial)
    if not dev:
        append_log(f"No se pudo activar {serial}", ok=False, output="No aparece en adb devices -l.")
        return False, "Ese dispositivo no aparece en adb devices -l."
    if dev.get("status") != "device":
        msg = f"El dispositivo aparece como {dev.get('status')}, no como device."
        append_log(f"No se pudo activar {serial}", ok=False, output=msg)
        return False, msg

    s = state()
    s["active_device"] = serial
    s["active_profile_id"] = profile_id or ""
    save_state(s)
    append_log(f"Dispositivo activo: {serial}", ok=True)
    return True, f"Activo: {serial}"


def adb_for_active(extra: List[str], timeout: int = 20, title: str = "Comando ADB") -> Tuple[bool, str, int]:
    serial = active_serial()
    if not serial:
        msg = "No hay dispositivo activo. Selecciona uno conectado o conecta un perfil."
        append_log(title, ok=False, output=msg)
        return False, msg, 1
    return run_cmd(["adb", "-s", serial] + extra, timeout=timeout, log_title=title)


def connect_endpoint(endpoint: str, activate: bool = True, profile_id: str = "") -> Tuple[bool, str]:
    endpoint = (endpoint or "").strip()
    if not endpoint:
        return False, "Falta IP:puerto."

    ok, out, _ = run_cmd(["adb", "connect", endpoint], timeout=20, log_title=f"Conectar por Wi-Fi a {endpoint}")
    time.sleep(0.8)
    dev = find_device(endpoint)
    output_lower = out.lower()
    connected_like = dev is not None or "connected to" in output_lower or "already connected" in output_lower

    if not connected_like:
        return False, out.strip() or f"No se pudo conectar a {endpoint}."

    if dev and dev.get("status") == "device" and activate:
        set_active(endpoint, profile_id=profile_id)
        return True, f"Conectado y activo: {endpoint}"

    if dev and dev.get("status") != "device":
        return False, f"El dispositivo responde, pero está en estado {dev.get('status')}. Mira la pantalla del móvil y acepta la autorización RSA si aparece."

    if activate:
        return False, f"ADB dijo que conectó, pero {endpoint} todavía no aparece listo en adb devices -l."
    return True, out.strip() or f"Conectado: {endpoint}"


def port_open(host: str, port: int, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False



def is_noise_network_ip(ip_text: str) -> bool:
    """Hide container/VPN ranges from the UI network scan.

    The user's useful Android devices are on the home LAN. Docker commonly
    creates 172.x ranges and Tailscale uses 100.64.0.0/10; showing those made
    the network tab noisy and confusing.
    """
    try:
        ip_obj = ipaddress.ip_address(ip_text)
    except Exception:
        return True
    if ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_multicast or ip_obj.is_unspecified:
        return True
    if ip_text.startswith("172."):
        return True
    try:
        if ip_obj in ipaddress.ip_network("100.64.0.0/10"):
            return True
    except Exception:
        pass
    return False


def is_useful_lan_ip(ip_text: str) -> bool:
    try:
        ip_obj = ipaddress.ip_address(ip_text)
    except Exception:
        return False
    return bool(ip_obj.is_private and not is_noise_network_ip(ip_text))


def normalize_mac(mac_text: str) -> str:
    cleaned = re.sub(r"[^0-9a-fA-F]", "", str(mac_text or "")).lower()
    if len(cleaned) != 12:
        return ""
    return ":".join(cleaned[i:i + 2] for i in range(0, 12, 2))


def _is_probably_lan_iface(name: str, ip_text: str) -> bool:
    name = (name or "").lower()
    if name in {"lo"} or name.startswith(("docker", "br-", "veth", "virbr", "tun", "wg", "tailscale")):
        return False
    return is_useful_lan_ip(ip_text)


def local_ipv4_networks() -> List[str]:
    """Return real LAN ranges, avoiding Docker/Tailscale-style noise where possible."""
    ok, out, _ = run_cmd(["ip", "-o", "-4", "addr", "show", "scope", "global"], timeout=8)
    networks: List[str] = []
    if ok:
        for line in out.splitlines():
            # Example: 2: eth0    inet 192.168.1.250/24 brd ...
            iface_match = re.match(r"\d+:\s+([^\s]+)\s+", line)
            iface = (iface_match.group(1) if iface_match else "").split("@")[0]
            m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", line)
            if not m:
                continue
            ip_text, prefix_text = m.group(1), m.group(2)
            if not _is_probably_lan_iface(iface, ip_text):
                continue
            try:
                net = ipaddress.ip_network(f"{ip_text}/{prefix_text}", strict=False)
                # A home scan should be useful, not endless. Keep broad masks to host /24.
                if net.prefixlen < 24:
                    net = ipaddress.ip_network(f"{ip_text}/24", strict=False)
                networks.append(str(net))
            except Exception:
                pass

    # Prefer common home LANs first.
    networks = sorted(networks, key=lambda n: (0 if n.startswith("192.168.") else 1, n))
    seen = set()
    result = []
    for item in networks:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result or ["192.168.1.0/24"]


def default_network() -> str:
    return local_ipv4_networks()[0]


def parse_ip_neigh() -> Dict[str, Dict[str, str]]:
    found: Dict[str, Dict[str, str]] = {}
    ok, out, _ = run_cmd(["ip", "neigh", "show"], timeout=8)
    if not ok:
        return found
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        ip_text = parts[0]
        if not re.match(r"^\d+\.\d+\.\d+\.\d+$", ip_text):
            continue
        mac = ""
        state_text = ""
        iface = ""
        if "dev" in parts:
            idx = parts.index("dev")
            if idx + 1 < len(parts):
                iface = parts[idx + 1]
        if "lladdr" in parts:
            idx = parts.index("lladdr")
            if idx + 1 < len(parts):
                mac = parts[idx + 1].lower()
        if parts[-1].isupper():
            state_text = parts[-1]
        found[ip_text] = {"ip": ip_text, "mac": mac, "state": state_text, "iface": iface, "source": "arp"}
    return found


def nmap_ping_scan(network: str) -> Dict[str, Dict[str, str]]:
    found: Dict[str, Dict[str, str]] = {}
    if not shutil.which("nmap"):
        return found
    # -PR forces ARP discovery on local LAN and finds many devices that ignore ICMP ping.
    ok, out, _ = run_cmd(
        ["nmap", "-n", "-sn", "-PR", "--max-retries", "1", "--host-timeout", "5s", network],
        timeout=70,
        log_title=f"Escaneo LAN {network}",
    )
    if not ok and not out:
        return found
    current_ip = ""
    for line in out.splitlines():
        m = re.search(r"Nmap scan report for\s+(\d+\.\d+\.\d+\.\d+)", line)
        if m:
            current_ip = m.group(1)
            found.setdefault(current_ip, {"ip": current_ip, "mac": "", "vendor": "", "source": "nmap"})
            continue
        m = re.search(r"MAC Address:\s+([0-9A-Fa-f:]{17})\s*(?:\((.*?)\))?", line)
        if m and current_ip:
            found.setdefault(current_ip, {"ip": current_ip, "mac": "", "vendor": "", "source": "nmap"})
            found[current_ip]["mac"] = m.group(1).lower()
            found[current_ip]["vendor"] = m.group(2) or ""
    return found


def nmap_adb_open(network: str, port: int = 5555) -> set:
    open_ips = set()
    if not shutil.which("nmap"):
        return open_ips
    ok, out, _ = run_cmd(
        ["nmap", "-n", "-p", str(port), "--open", "--max-retries", "1", "--host-timeout", "5s", network],
        timeout=80,
        log_title=f"Buscar ADB puerto {port} en {network}",
    )
    if not ok and not out:
        return open_ips
    current_ip = ""
    for line in out.splitlines():
        m = re.search(r"Nmap scan report for\s+(\d+\.\d+\.\d+\.\d+)", line)
        if m:
            current_ip = m.group(1)
            continue
        if current_ip and re.search(rf"{port}/tcp\s+open", line):
            open_ips.add(current_ip)
    return open_ips


def fallback_ping_scan(network: str) -> Dict[str, Dict[str, str]]:
    found: Dict[str, Dict[str, str]] = {}
    try:
        net = ipaddress.ip_network(network, strict=False)
    except Exception:
        return found
    hosts = list(net.hosts())
    if len(hosts) > 512:
        hosts = hosts[:512]

    def ping(ip_obj: ipaddress.IPv4Address) -> Optional[str]:
        ip_text = str(ip_obj)
        ok, _, _ = run_cmd(["ping", "-c", "1", "-W", "1", ip_text], timeout=2)
        return ip_text if ok else None

    with ThreadPoolExecutor(max_workers=96) as executor:
        for future in as_completed([executor.submit(ping, host) for host in hosts]):
            ip_text = future.result()
            if ip_text:
                found[ip_text] = {"ip": ip_text, "mac": "", "vendor": "", "source": "ping"}
    return found


def enrich_open_ports(items: Dict[str, Dict[str, str]], port: int = 5555) -> set:
    open_ips = set()
    ips = list(items.keys())
    with ThreadPoolExecutor(max_workers=96) as executor:
        future_map = {executor.submit(port_open, ip_text, port, 0.45): ip_text for ip_text in ips}
        for future in as_completed(future_map):
            ip_text = future_map[future]
            try:
                if future.result():
                    open_ips.add(ip_text)
            except Exception:
                pass
    return open_ips


def scan_network(network: str = "", port: int = 5555) -> List[Dict[str, Any]]:
    network = (network or "").strip() or default_network()
    try:
        net_obj = ipaddress.ip_network(network, strict=False)
    except Exception:
        network = default_network()
        net_obj = ipaddress.ip_network(network, strict=False)

    all_items: Dict[str, Dict[str, Any]] = {}

    nmap_items = nmap_ping_scan(network)
    ping_items = fallback_ping_scan(network) if not nmap_items else {}
    neigh = parse_ip_neigh()

    for source in (nmap_items, ping_items, neigh):
        for ip_text, data in source.items():
            try:
                ip_obj = ipaddress.ip_address(ip_text)
            except Exception:
                continue
            if ip_obj not in net_obj or not is_useful_lan_ip(ip_text):
                continue
            all_items.setdefault(ip_text, {"ip": ip_text, "mac": "", "vendor": "", "source": data.get("source", "red")})
            # Prefer richer sources without deleting existing info.
            if data.get("source") and all_items[ip_text].get("source") in {"ping", "red"}:
                all_items[ip_text]["source"] = data.get("source")
            for key in ("mac", "vendor", "state", "iface"):
                if data.get(key) and not all_items[ip_text].get(key):
                    all_items[ip_text][key] = data.get(key)

    adb_open_ips = nmap_adb_open(network, port)
    if all_items:
        adb_open_ips.update(enrich_open_ports(all_items, port))

    connected = adb_devices()
    active = active_serial()
    connected_by_ip = {}
    for dev in connected:
        serial = dev.get("serial", "")
        m = re.match(r"^(\d+\.\d+\.\d+\.\d+):(\d+)$", serial)
        if m:
            ip_text = m.group(1)
            try:
                ip_obj = ipaddress.ip_address(ip_text)
            except Exception:
                continue
            if ip_obj in net_obj and is_useful_lan_ip(ip_text):
                connected_by_ip[ip_text] = dev
                all_items.setdefault(ip_text, {"ip": ip_text, "mac": "", "vendor": "", "source": "adb"})

    saved_profiles = profiles()
    profiles_by_ip: Dict[str, List[str]] = {}
    profiles_by_mac: Dict[str, List[str]] = {}
    for profile in saved_profiles.values():
        profile_name = profile.get("name") or profile.get("id") or "Perfil"
        ip_text = (profile.get("ip") or "").strip()
        mac_text = normalize_mac(profile.get("mac", ""))
        if ip_text:
            try:
                ip_obj = ipaddress.ip_address(ip_text)
            except Exception:
                ip_obj = None
            if ip_obj and is_useful_lan_ip(ip_text):
                profiles_by_ip.setdefault(ip_text, []).append(profile_name)
                # Aunque no haya respondido al escaneo, enséñalo como perfil guardado.
                if ip_obj in net_obj:
                    all_items.setdefault(ip_text, {"ip": ip_text, "mac": mac_text or profile.get("mac", ""), "vendor": "", "source": "perfil"})
        if mac_text:
            profiles_by_mac.setdefault(mac_text, []).append(profile_name)

    for ip_text, item in all_items.items():
        dev = connected_by_ip.get(ip_text)
        endpoint = f"{ip_text}:{port}"
        mac_norm = normalize_mac(item.get("mac", ""))
        profile_names = list(profiles_by_ip.get(ip_text, []))
        for name in profiles_by_mac.get(mac_norm, []):
            if name not in profile_names:
                profile_names.append(name)
        item["profile_names"] = profile_names
        item["has_profile"] = bool(profile_names)
        item["adb_port_open"] = ip_text in adb_open_ips
        item["adb_connected"] = bool(dev and dev.get("status") == "device")
        item["adb_status"] = dev.get("status", "") if dev else ""
        item["adb_serial"] = dev.get("serial", "") if dev else endpoint
        item["active"] = bool(active and (active == endpoint or active.startswith(f"{ip_text}:")))
        if item["active"]:
            item["ui_state"] = "active"
            item["label"] = "Activo"
        elif item["adb_connected"]:
            item["ui_state"] = "connected"
            item["label"] = "ADB conectado"
        elif item["adb_port_open"]:
            item["ui_state"] = "open"
            item["label"] = f"ADB {port} abierto"
        elif item["has_profile"]:
            item["ui_state"] = "profile"
            item["label"] = "Perfil guardado"
        else:
            item["ui_state"] = "detected"
            item["label"] = "Detectado"

    def ip_sort_key(x: Dict[str, Any]):
        try:
            return tuple(int(p) for p in x["ip"].split("."))
        except Exception:
            return (999, 999, 999, 999)

    result = sorted(all_items.values(), key=ip_sort_key)
    s = state()
    s["last_network_scan"] = result
    s["last_network_scan_at"] = now_text()
    s["last_network_range"] = network
    save_state(s)
    append_log("Escaneo de red terminado", ok=True, output=f"{network}: {len(result)} dispositivos. {len(adb_open_ips)} con puerto {port} abierto.")
    return result


def _extract_private_ips(text: str) -> List[str]:
    ips = []
    for ip_text in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text or ""):
        try:
            ip_obj = ipaddress.ip_address(ip_text)
        except Exception:
            continue
        if not ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local or is_noise_network_ip(ip_text):
            continue
        if ip_text not in ips:
            ips.append(ip_text)
    return ips


def detect_android_ip(serial: str) -> str:
    """Detect the phone Wi-Fi/LAN IP before or after adb tcpip.

    The old version only checked `ip route` and wlan0. Many Androids use another
    Wi-Fi interface name or only expose the useful IP through route get/dumpsys.
    """
    if not serial:
        return ""

    commands = [
        ["adb", "-s", serial, "shell", "ip", "route", "get", "1.1.1.1"],
        ["adb", "-s", serial, "shell", "ip", "route", "get", "8.8.8.8"],
        ["adb", "-s", serial, "shell", "ip", "-f", "inet", "addr", "show"],
        ["adb", "-s", serial, "shell", "ip", "addr", "show", "wlan0"],
        ["adb", "-s", serial, "shell", "ifconfig", "wlan0"],
        ["adb", "-s", serial, "shell", "getprop", "dhcp.wlan0.ipaddress"],
        ["adb", "-s", serial, "shell", "dumpsys", "wifi"],
    ]

    preferred = []
    fallback = []
    for cmd in commands:
        ok, out, _ = run_cmd(cmd, timeout=12, log_title=None)
        if not ok and not out:
            continue
        # Route output has the best answer after `src`.
        m = re.search(r"\bsrc\s+(\d+\.\d+\.\d+\.\d+)", out or "")
        if m:
            ip_text = m.group(1)
            if ip_text not in preferred:
                preferred.append(ip_text)
        for ip_text in _extract_private_ips(out):
            if ip_text not in fallback:
                fallback.append(ip_text)

    local_networks = []
    for net_text in local_ipv4_networks():
        try:
            local_networks.append(ipaddress.ip_network(net_text, strict=False))
        except Exception:
            pass

    for ip_text in preferred + fallback:
        try:
            ip_obj = ipaddress.ip_address(ip_text)
        except Exception:
            continue
        if any(ip_obj in net for net in local_networks):
            return ip_text

    return (preferred + fallback + [""])[0]


@app.get("/")
def index():
    return render_template("index.html")


def download_response(base_dir: Path, filename: str):
    safe = safe_filename(filename)
    path = (base_dir / safe).resolve()
    try:
        path.relative_to(base_dir.resolve())
    except ValueError:
        abort(404)
    if not path.exists() or not path.is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=path.name)


@app.get("/api/download/screenshots/<filename>")
def api_download_screenshot(filename: str):
    return download_response(SCREENSHOT_DIR, filename)


@app.get("/api/download/screenrecords/<filename>")
def api_download_screenrecord(filename: str):
    return download_response(SCREENRECORD_DIR, filename)


@app.get("/api/download/apks/<filename>")
def api_download_apk(filename: str):
    return download_response(APK_PULL_DIR, filename)


@app.get("/health")
def health():
    return "ok"


def recent_devices() -> List[Dict[str, Any]]:
    items = state().get("recent_devices", [])
    return items if isinstance(items, list) else []


def add_recent_device(serial: str, label: str = "") -> None:
    serial = (serial or "").strip()
    if not serial or not re.match(r"^\d+\.\d+\.\d+\.\d+:\d+$", serial):
        return
    s = state()
    items = [x for x in recent_devices() if x.get("serial") != serial]
    items.insert(0, {"serial": serial, "label": label or serial, "last_seen": now_text()})
    s["recent_devices"] = items[:20]
    save_state(s)


def remove_recent_device(serial: str) -> None:
    s = state()
    s["recent_devices"] = [x for x in recent_devices() if x.get("serial") != serial]
    save_state(s)


@app.get("/api/status")
def api_status():
    s = state()
    devs = adb_devices()
    active = s.get("active_device", "")
    active_info = None
    for d in devs:
        if d["serial"] == active:
            active_info = d
            break
    return jsonify({
        "ok": True,
        "adb_version": adb_version(),
        "profiles": profiles(),
        "state": s,
        "devices": devs,
        "active": active,
        "active_info": active_info,
        "quick_commands": {key: value["label"] for key, value in QUICK_COMMANDS.items()},
        "recent_devices": recent_devices(),
        "networks": local_ipv4_networks(),
        "screenrecord": active_screenrecord_info(),
        "screen": screen_state_info(),
    })


@app.get("/api/logs")
def api_logs():
    if not LOG_FILE.exists():
        return jsonify({"ok": True, "text": ""})
    text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
    # keep browser light
    lines = text.splitlines()[-260:]
    return jsonify({"ok": True, "text": "\n".join(lines)})


@app.post("/api/logs/clear")
def api_clear_logs():
    LOG_FILE.write_text("", encoding="utf-8")
    append_log("Logs limpiados", ok=True)
    return jsonify({"ok": True})


@app.get("/api/devices")
def api_devices():
    return jsonify({"ok": True, "devices": adb_devices(log=True), "active": active_serial()})


@app.post("/api/active")
def api_set_active():
    data = request.get_json(silent=True) or {}
    ok, msg = set_active(data.get("serial", ""), profile_id=data.get("profile_id", ""))
    return jsonify({"ok": ok, "message": msg, "devices": adb_devices(), "state": state()})


@app.post("/api/adb/restart")
def api_adb_restart():
    run_cmd(["adb", "kill-server"], timeout=10, log_title="ADB kill-server")
    ok, out, code = run_cmd(["adb", "start-server"], timeout=15, log_title="ADB start-server")
    return jsonify({"ok": ok, "message": out.strip() or "ADB reiniciado.", "code": code})


@app.post("/api/connect/manual")
def api_connect_manual():
    data = request.get_json(silent=True) or {}
    endpoint = normalize_endpoint(data.get("host", ""), data.get("port", 5555))
    ok, msg = connect_endpoint(endpoint, activate=bool(data.get("activate", True)))
    return jsonify({"ok": ok, "message": msg, "devices": adb_devices(), "state": state()})


@app.post("/api/disconnect")
def api_disconnect():
    data = request.get_json(silent=True) or {}
    serial = data.get("serial") or active_serial()
    if not serial:
        return jsonify({"ok": False, "message": "No hay dispositivo para desconectar."})
    ok, out, _ = run_cmd(["adb", "disconnect", serial], timeout=15, log_title=f"Desconectar {serial}")
    if ok:
        add_recent_device(serial)
    s = state()
    if s.get("active_device") == serial:
        s["active_device"] = ""
        s["active_profile_id"] = ""
        save_state(s)
    return jsonify({"ok": ok, "message": out.strip() or f"Desconectado: {serial}", "devices": adb_devices(), "state": state()})


@app.post("/api/disconnect/all")
def api_disconnect_all():
    for dev in adb_devices():
        if dev.get("kind") == "wifi":
            add_recent_device(dev.get("serial", ""))
    ok, out, _ = run_cmd(["adb", "disconnect"], timeout=15, log_title="Desconectar todos los ADB Wi-Fi")
    s = state()
    s["active_device"] = ""
    s["active_profile_id"] = ""
    save_state(s)
    return jsonify({"ok": ok, "message": out.strip() or "Desconectados.", "devices": adb_devices(), "state": state()})


@app.post("/api/wifi/prepare")
def api_wifi_prepare():
    data = request.get_json(silent=True) or {}
    port = int(data.get("port") or 5555)
    serial = data.get("serial") or active_serial() or first_usb_device()
    if not serial:
        msg = "No hay ningún dispositivo USB listo. Conecta el móvil por USB, acepta la autorización RSA y vuelve a probar."
        append_log("Preparar ADB Wi-Fi desde USB", ok=False, output=msg)
        return jsonify({"ok": False, "message": msg})

    # Detect first. After `adb tcpip`, some phones drop the USB transport very fast.
    ip_before = detect_android_ip(serial)

    ok, out, _ = run_cmd(["adb", "-s", serial, "tcpip", str(port)], timeout=20, log_title=f"Activar ADB Wi-Fi en {serial}")
    if not ok:
        return jsonify({"ok": False, "message": out.strip() or "No se pudo activar adb tcpip."})

    time.sleep(2.2)
    ip_after = detect_android_ip(serial)
    ip_text = ip_before or ip_after

    if not ip_text:
        msg = (
            "ADB Wi-Fi se activó, pero no he podido sacar la IP del móvil. "
            "Mira la IP Wi-Fi del Android o escanea la red; si aparece con ADB abierto, conecta desde Red."
        )
        append_log("Detectar IP Android", ok=False, output=msg)
        return jsonify({"ok": False, "message": msg, "tcpip_output": out.strip()})

    endpoint = f"{ip_text}:{port}"
    ok2, msg2 = connect_endpoint(endpoint, activate=True)
    extra = ""
    if ip_before and ip_after and ip_before != ip_after:
        extra = "\nAviso: antes era " + ip_before + ", después era " + ip_after + "."
    return jsonify({
        "ok": ok2,
        "message": (msg2 + extra).strip(),
        "ip": ip_text,
        "endpoint": endpoint,
        "ip_before": ip_before,
        "ip_after": ip_after,
        "devices": adb_devices(),
        "state": state(),
    })


@app.post("/api/wifi/pair")
def api_wifi_pair():
    data = request.get_json(silent=True) or {}
    host = (data.get("host") or "").strip()
    port = str(data.get("port") or "").strip()
    code = (data.get("code") or "").strip()
    if not host or not port or not code:
        return jsonify({"ok": False, "message": "Falta IP, puerto de emparejamiento o código."})
    endpoint = normalize_endpoint(host, port)
    ok, out, _ = run_cmd(["adb", "pair", endpoint, code], timeout=30, log_title=f"Emparejar ADB inalámbrico {endpoint}")
    return jsonify({"ok": ok, "message": out.strip() or "Emparejamiento enviado."})


@app.get("/api/profiles")
def api_profiles():
    return jsonify({"ok": True, "profiles": profiles(), "active_profile_id": state().get("active_profile_id", "")})


@app.post("/api/profiles")
def api_create_profile():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "message": "El perfil necesita nombre."})
    profile_id = str(uuid.uuid4())[:8]
    item = {
        "id": profile_id,
        "name": name,
        "ip": (data.get("ip") or "").strip(),
        "port": int(data.get("port") or 5555),
        "mac": (data.get("mac") or "").strip(),
        "notes": (data.get("notes") or "").strip(),
        "color": (data.get("color") or "").strip(),
        "created_at": now_text(),
        "updated_at": now_text(),
    }
    p = profiles()
    p[profile_id] = item
    save_profiles(p)
    append_log(f"Perfil creado: {name}", ok=True)
    return jsonify({"ok": True, "message": "Perfil creado.", "profile": item, "profiles": p})


@app.route("/api/profiles/<profile_id>", methods=["PUT", "POST"])
def api_update_profile(profile_id: str):
    data = request.get_json(silent=True) or {}
    p = profiles()
    if profile_id not in p:
        return jsonify({"ok": False, "message": "Perfil no encontrado."})
    item = p[profile_id]
    for key in ("name", "ip", "mac", "notes", "color"):
        if key in data:
            item[key] = (data.get(key) or "").strip()
    if "port" in data:
        item["port"] = int(data.get("port") or 5555)
    item["updated_at"] = now_text()
    p[profile_id] = item
    save_profiles(p)
    append_log(f"Perfil actualizado: {item.get('name', profile_id)}", ok=True)
    return jsonify({"ok": True, "message": "Perfil actualizado.", "profile": item, "profiles": p})


@app.route("/api/profiles/<profile_id>/delete", methods=["POST"])
def api_delete_profile_post(profile_id: str):
    return api_delete_profile(profile_id)


@app.delete("/api/profiles/<profile_id>")
def api_delete_profile(profile_id: str):
    p = profiles()
    item = p.pop(profile_id, None)
    if not item:
        return jsonify({"ok": False, "message": "Perfil no encontrado."})
    save_profiles(p)
    s = state()
    if s.get("active_profile_id") == profile_id:
        s["active_profile_id"] = ""
        save_state(s)
    append_log(f"Perfil borrado: {item.get('name', profile_id)}", ok=True)
    return jsonify({"ok": True, "message": "Perfil borrado.", "profiles": p})


@app.post("/api/profiles/<profile_id>/connect")
def api_connect_profile(profile_id: str):
    p = profiles()
    item = p.get(profile_id)
    if not item:
        return jsonify({"ok": False, "message": "Perfil no encontrado."})
    endpoint = normalize_endpoint(item.get("ip", ""), item.get("port", 5555))
    if not endpoint:
        return jsonify({"ok": False, "message": "Este perfil no tiene IP."})
    ok, msg = connect_endpoint(endpoint, activate=True, profile_id=profile_id)
    return jsonify({"ok": ok, "message": msg, "devices": adb_devices(), "state": state()})


@app.post("/api/network/scan")
def api_network_scan():
    data = request.get_json(silent=True) or {}
    network = (data.get("network") or "").strip()
    port = int(data.get("port") or 5555)
    result = scan_network(network, port=port)
    return jsonify({"ok": True, "devices": result, "state": state(), "network": state().get("last_network_range", network or default_network())})


@app.get("/api/network/last")
def api_network_last():
    s = state()
    return jsonify({
        "ok": True,
        "devices": s.get("last_network_scan", []),
        "at": s.get("last_network_scan_at", ""),
        "networks": local_ipv4_networks(),
        "default_network": default_network(),
        "last_network_range": s.get("last_network_range", ""),
    })


@app.post("/api/network/connect")
def api_network_connect():
    data = request.get_json(silent=True) or {}
    ip_text = (data.get("ip") or "").strip()
    port = data.get("port") or 5555
    endpoint = normalize_endpoint(ip_text, port)
    ok, msg = connect_endpoint(endpoint, activate=True)
    return jsonify({"ok": ok, "message": msg, "devices": adb_devices(), "state": state()})


@app.post("/api/network/profile")
def api_network_profile():
    data = request.get_json(silent=True) or {}
    ip_text = (data.get("ip") or "").strip()
    mac = (data.get("mac") or "").strip()
    name = (data.get("name") or ip_text or "Android").strip()
    if not ip_text:
        return jsonify({"ok": False, "message": "Falta IP."})
    profile_id = str(uuid.uuid4())[:8]
    item = {
        "id": profile_id,
        "name": name,
        "ip": ip_text,
        "port": 5555,
        "mac": mac,
        "notes": "Creado desde escaneo de red",
        "color": "",
        "created_at": now_text(),
        "updated_at": now_text(),
    }
    p = profiles()
    p[profile_id] = item
    save_profiles(p)
    append_log(f"Perfil creado desde red: {name}", ok=True)
    return jsonify({"ok": True, "message": "Perfil creado desde red.", "profile": item, "profiles": p})


@app.post("/api/recent/connect")
def api_recent_connect():
    data = request.get_json(silent=True) or {}
    serial = (data.get("serial") or "").strip()
    if not serial:
        return jsonify({"ok": False, "message": "Falta dispositivo reciente."})
    ok, msg = connect_endpoint(serial, activate=True)
    if ok:
        remove_recent_device(serial)
    return jsonify({"ok": ok, "message": msg, "devices": adb_devices(), "state": state(), "recent_devices": recent_devices()})


@app.post("/api/recent/remove")
def api_recent_remove():
    data = request.get_json(silent=True) or {}
    serial = (data.get("serial") or "").strip()
    remove_recent_device(serial)
    append_log(f"Reciente eliminado: {serial}", ok=True)
    return jsonify({"ok": True, "message": "Eliminado de recientes.", "recent_devices": recent_devices(), "state": state()})


def safe_filename(name: str, fallback: str = "file") -> str:
    raw = Path(name or fallback).name
    raw = re.sub(r"[^a-zA-Z0-9_.-]+", "_", raw).strip("._")
    return raw or fallback


def run_adb_for_serial(serial: str, extra: List[str], timeout: int = 30, title: str = "Comando ADB") -> Tuple[bool, str, int]:
    if not serial:
        msg = "No hay dispositivo activo."
        append_log(title, ok=False, output=msg)
        return False, msg, 1
    return run_cmd(["adb", "-s", serial] + extra, timeout=timeout, log_title=title)


def open_app_from_candidates(label: str, packages: List[str]) -> Tuple[bool, str, int]:
    last_output = ""
    for pkg in packages:
        ok, out, code = adb_for_active(["shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"], timeout=12, title=f"Abrir {label}: {pkg}")
        last_output = out.strip() or last_output
        low = (out or "").lower()
        if ok and "no activities found" not in low and "monkey aborted" not in low and "error:" not in low:
            return True, out, code
    msg = f"No he podido abrir {label}. Probé: {', '.join(packages)}. {last_output}".strip()
    append_log(f"Abrir {label}", ok=False, output=msg)
    return False, msg, 1




# =========================
# Fase 7 · Fastboot
# =========================

FASTBOOT_SAFE_PARTITIONS = {
    "boot", "recovery", "vendor_boot", "init_boot", "dtbo",
    "vbmeta", "vbmeta_system", "vbmeta_vendor",
    "logo", "modem", "bluetooth",
}


def fastboot_available() -> bool:
    return shutil.which("fastboot") is not None


def parse_fastboot_devices(output: str) -> List[Dict[str, str]]:
    devices: List[Dict[str, str]] = []
    for raw_line in (output or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("waiting for") or low.startswith("< waiting"):
            continue
        if "no permissions" in low:
            devices.append({"serial": "", "status": "error", "detail": line})
            continue
        parts = line.split()
        if not parts:
            continue
        serial = parts[0]
        status = "fastboot"
        detail_parts = parts[1:]
        if detail_parts and ":" not in detail_parts[0]:
            status = detail_parts[0]
            detail_parts = detail_parts[1:]
        devices.append({
            "serial": serial,
            "status": status,
            "detail": " ".join(detail_parts),
        })
    return devices


def fastboot_devices(log: bool = False) -> List[Dict[str, str]]:
    if not fastboot_available():
        append_log("Fastboot no disponible", ok=False, output="No existe el binario fastboot en el contenedor.")
        return []
    ok, out, _ = run_cmd(["fastboot", "devices", "-l"], timeout=18, log_title="Listar dispositivos fastboot" if log else None)
    if not ok and not out.strip():
        return []
    return parse_fastboot_devices(out)


def fastboot_version() -> str:
    if not fastboot_available():
        return "fastboot no instalado"
    ok, out, _ = run_cmd(["fastboot", "--version"], timeout=8)
    return out.strip() if out.strip() else ("fastboot disponible" if ok else "fastboot sin versión")


def selected_fastboot_serial(data: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    data = data or {}
    serial = (data.get("serial") or "").strip()
    if serial:
        return serial, ""
    devices = [d for d in fastboot_devices() if d.get("serial")]
    if not devices:
        return "", "No hay ningún dispositivo en modo fastboot. Entra al bootloader y pulsa Actualizar."
    return devices[0]["serial"], ""


def run_fastboot(extra: List[str], serial: str = "", timeout: int = 60, title: str = "Fastboot") -> Tuple[bool, str, int]:
    if not fastboot_available():
        msg = "fastboot no está instalado en este contenedor."
        append_log(title, ok=False, output=msg)
        return False, msg, 1
    cmd = ["fastboot"]
    if serial:
        cmd += ["-s", serial]
    cmd += extra
    return run_cmd(cmd, timeout=timeout, log_title=title)


def save_fastboot_file(file_obj: Any, fallback: str = "image.img") -> Path:
    name = safe_filename(getattr(file_obj, "filename", "") or fallback, fallback=fallback)
    target = FASTBOOT_DIR / f"{int(time.time())}_{uuid.uuid4().hex[:8]}_{name}"
    file_obj.save(target)
    return target


def normalize_partition(value: str) -> str:
    value = (value or "").strip()
    if value in FASTBOOT_SAFE_PARTITIONS:
        return value
    if re.match(r"^[a-zA-Z0-9_-]{1,64}$", value):
        return value
    return ""


def fastboot_json(ok: bool, out: str, code: int, extra: Optional[Dict[str, Any]] = None):
    payload = {
        "ok": ok,
        "message": (out or "").strip() or ("Hecho." if ok else "Error fastboot."),
        "code": code,
        "devices": fastboot_devices(),
    }
    if extra:
        payload.update(extra)
    return jsonify(payload)


def screen_state_info() -> Dict[str, Any]:
    s = state()
    mode = s.get("screen_mode", "none") or "none"
    if mode not in {"none", "light", "scrcpy"}:
        mode = "none"
    serial = active_serial()
    visor = visor_proxy("/api/scrcpy/status", timeout=0.6)
    scrcpy_running = bool(visor.get("ok") and visor.get("running"))
    if mode == "scrcpy" and not scrcpy_running:
        mode = "none"
    return {
        "mode": mode,
        "active": (mode == "light" and bool(serial)) or (mode == "scrcpy" and scrcpy_running),
        "serial": serial,
        "interval_ms": int(s.get("screen_interval_ms") or 1200),
        "started_at": s.get("screen_started_at", ""),
        "scrcpy_settings": scrcpy_settings(),
        "visor": {
            "ok": bool(visor.get("ok")),
            "message": visor.get("message", ""),
            "running": scrcpy_running,
            "serial": visor.get("serial", ""),
            "started_at": visor.get("started_at", ""),
            "public_port": VISOR_PUBLIC_PORT,
            "viewer_url": f"http://{{host}}:{VISOR_PUBLIC_PORT}/vnc.html?autoconnect=true&resize=scale&reconnect=true",
        },
    }


@app.post("/api/screen/light/start")
def api_screen_light_start():
    serial = active_serial()
    if not serial:
        msg = "No hay dispositivo activo para ver pantalla."
        append_log("Modo pantalla ligera", ok=False, output=msg)
        return jsonify({"ok": False, "message": msg})
    data = request.get_json(silent=True) or {}
    try:
        interval = int(data.get("interval_ms") or 1200)
    except Exception:
        interval = 1200
    interval = max(500, min(interval, 5000))
    # Test rápido: así el botón falla al momento si el móvil no permite capturas.
    ok, out, _ = run_adb_for_serial(serial, ["shell", "echo", "screen-ok"], timeout=8, title="Comprobar pantalla ligera")
    if not ok:
        return jsonify({"ok": False, "message": out.strip() or "No se puede hablar con el dispositivo activo."})
    s = state()
    s["screen_mode"] = "light"
    s["screen_interval_ms"] = interval
    s["screen_started_at"] = now_text()
    save_state(s)
    append_log("Modo pantalla ligera iniciado", ok=True, output=f"{serial} · refresco {interval} ms")
    return jsonify({"ok": True, "message": f"Modo ligero iniciado en {serial}.", "screen": screen_state_info()})


@app.get("/api/scrcpy/settings")
def api_scrcpy_settings_get():
    return jsonify({"ok": True, "settings": scrcpy_settings()})


@app.post("/api/scrcpy/settings")
def api_scrcpy_settings_save():
    data = request.get_json(silent=True) or {}
    settings = save_scrcpy_settings(data)
    return jsonify({"ok": True, "message": "Ajustes de scrcpy guardados.", "settings": settings})


@app.post("/api/scrcpy/start")
def api_scrcpy_start():
    serial = active_serial()
    if not serial:
        msg = "No hay dispositivo activo para iniciar scrcpy."
        append_log("Scrcpy", ok=False, output=msg)
        return jsonify({"ok": False, "message": msg})
    payload = {
        "serial": serial,
        "settings": scrcpy_settings(),
    }
    result = visor_proxy("/api/scrcpy/start", payload, timeout=20)
    ok = bool(result.get("ok"))
    if ok:
        s = state()
        s["screen_mode"] = "scrcpy"
        s["screen_started_at"] = now_text()
        save_state(s)
    append_log("Scrcpy", ok=ok, output=result.get("message", ""))
    result["screen"] = screen_state_info()
    result["viewer_url"] = f"http://{{host}}:{VISOR_PUBLIC_PORT}/vnc.html?autoconnect=true&resize=scale&reconnect=true"
    return jsonify(result)


@app.post("/api/scrcpy/stop")
def api_scrcpy_stop():
    result = visor_proxy("/api/scrcpy/stop", {}, timeout=10)
    ok = bool(result.get("ok"))
    s = state()
    if s.get("screen_mode") == "scrcpy":
        s["screen_mode"] = "none"
        s["screen_started_at"] = ""
        save_state(s)
    append_log("Scrcpy detenido", ok=ok, output=result.get("message", ""))
    result["screen"] = screen_state_info()
    return jsonify(result)


@app.get("/api/scrcpy/status")
def api_scrcpy_status():
    result = visor_proxy("/api/scrcpy/status", timeout=2)
    result["settings"] = scrcpy_settings()
    result["viewer_url"] = f"http://{{host}}:{VISOR_PUBLIC_PORT}/vnc.html?autoconnect=true&resize=scale&reconnect=true"
    return jsonify(result)


@app.get("/api/scrcpy/logs")
def api_scrcpy_logs():
    return jsonify(visor_proxy("/api/logs", timeout=3))




@app.post("/api/screen/stop")
def api_screen_stop():
    s = state()
    old_mode = s.get("screen_mode", "none")
    if old_mode == "scrcpy":
        visor_proxy("/api/scrcpy/stop", {}, timeout=8)
    s["screen_mode"] = "none"
    s["screen_started_at"] = ""
    save_state(s)
    append_log("Pantalla cerrada", ok=True, output=f"Modo anterior: {old_mode}")
    return jsonify({"ok": True, "message": "Pantalla cerrada.", "screen": screen_state_info()})


@app.get("/api/screen/frame")
def api_screen_frame():
    info = screen_state_info()
    serial = info.get("serial", "")
    if info.get("mode") != "light" or not serial:
        return jsonify({"ok": False, "message": "La pantalla ligera no está activa."}), 409
    acquired = _screencap_lock.acquire(blocking=False)
    if not acquired:
        return jsonify({"ok": False, "message": "Ya hay una captura en curso."}), 429
    try:
        env = os.environ.copy()
        env["HOME"] = str(DATA_DIR)
        completed = subprocess.run(
            ["adb", "-s", serial, "exec-out", "screencap", "-p"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=18,
            env=env,
        )
        data = completed.stdout or b""
        stderr = completed.stderr.decode("utf-8", errors="replace") if completed.stderr else ""
        if completed.returncode != 0 or len(data) < 32:
            append_log("Frame pantalla ligera", ok=False, output=stderr or "Captura vacía.")
            return jsonify({"ok": False, "message": stderr or "No se pudo capturar pantalla."}), 500
        response = Response(data, mimetype="image/png")
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response
    except subprocess.TimeoutExpired:
        append_log("Frame pantalla ligera", ok=False, output="Tiempo agotado capturando pantalla.")
        return jsonify({"ok": False, "message": "Tiempo agotado capturando pantalla."}), 504
    except Exception as exc:
        append_log("Frame pantalla ligera", ok=False, output=str(exc))
        return jsonify({"ok": False, "message": str(exc)}), 500
    finally:
        _screencap_lock.release()


def active_screenrecord_info() -> Dict[str, Any]:
    serial = active_serial()
    if not serial:
        return {"active": False}
    item = _screenrecord_processes.get(serial)
    proc = item.get("process") if item else None
    active = bool(proc and proc.poll() is None)
    if item and not active:
        _screenrecord_processes.pop(serial, None)
    return {"active": active, "serial": serial, "remote": item.get("remote") if item else ""}


@app.post("/api/commands/<command_id>")
def api_quick_command(command_id: str):
    command = QUICK_COMMANDS.get(command_id)
    if not command:
        return jsonify({"ok": False, "message": "Comando no encontrado."})
    if command.get("cmds"):
        last_output = ""
        for cmd in command["cmds"]:
            ok, out, _ = adb_for_active(cmd, timeout=20, title=f"Comando rápido: {command['label']}")
            last_output = out.strip() or last_output
            low = (out or "").lower()
            if ok and "error" not in low and "exception" not in low and "not found" not in low and "no activities found" not in low and "monkey aborted" not in low:
                return jsonify({"ok": True, "message": out.strip() or command["label"]})
        return jsonify({"ok": False, "message": last_output or f"No he podido ejecutar {command['label']}."})
    if command.get("packages"):
        ok, out, _ = open_app_from_candidates(command["label"], command["packages"])
        return jsonify({"ok": ok, "message": out.strip() or command["label"]})
    ok, out, _ = adb_for_active(command["cmd"], timeout=20, title=f"Comando rápido: {command['label']}")
    return jsonify({"ok": ok, "message": out.strip() or command["label"]})


@app.post("/api/commands/reboot")
def api_reboot():
    ok, out, _ = adb_for_active(["reboot"], timeout=15, title="Reiniciar Android")
    return jsonify({"ok": ok, "message": out.strip() or "Reboot enviado."})


@app.post("/api/commands/open-link")
def api_open_link():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "message": "No has puesto ningún link."})
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url
    ok, out, _ = adb_for_active(["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url], timeout=15, title=f"OpenLink {url}")
    return jsonify({"ok": ok, "message": out.strip() or f"OpenLink enviado: {url}"})


@app.post("/api/commands/clear-all-cache")
def api_clear_all_cache():
    try:
        serial, msg = require_active()
        if not serial:
            return jsonify({"ok": False, "message": msg})

        global _cache_job
        with _lock:
            if _cache_job.get("running"):
                return jsonify({"ok": True, "message": "Ya hay una limpieza de caché ejecutándose en segundo plano."})
            _cache_job = {"running": True, "started_at": now_text(), "finished_at": "", "message": "Iniciando limpieza...", "serial": serial}

        def worker(job_serial: str) -> None:
            global _cache_job
            lines: List[str] = []

            def step(text: str) -> None:
                lines.append(text)
                with _lock:
                    _cache_job["message"] = "\n".join(lines[-30:])
                append_log("Borrar caché global", ok=True, output=text)

            try:
                step("[1/3] listando solo apps de usuario")
                ok, out, _ = run_adb_for_serial(job_serial, ["shell", "pm", "list", "packages", "-3"], timeout=20, title="Listar apps usuario para caché")
                packages: List[str] = []
                if ok:
                    for line in out.splitlines():
                        pkg = line.strip().replace("package:", "", 1).strip()
                        if pkg and pkg not in packages:
                            packages.append(pkg)

                step(f"[2/3] limpiando caché externa de {len(packages)} apps de usuario")
                if not packages:
                    step("No se han encontrado apps de usuario.")
                for index, pkg in enumerate(packages, start=1):
                    if index == 1 or index % 5 == 0 or index == len(packages):
                        step(f"App {index}/{len(packages)} · {pkg}")
                    # Método seguro: no usa trim-caches ni pm clear, porque en tu móvil provocó soft reboot.
                    run_adb_for_serial(job_serial, [
                        "shell", "rm", "-rf",
                        f"/sdcard/Android/data/{pkg}/cache",
                        f"/sdcard/Android/media/{pkg}/cache",
                        f"/storage/emulated/0/Android/data/{pkg}/cache",
                        f"/storage/emulated/0/Android/media/{pkg}/cache",
                    ], timeout=4, title=f"Caché externa {pkg}")

                step("[3/3] miniaturas comunes")
                run_adb_for_serial(job_serial, [
                    "shell", "rm", "-rf",
                    "/sdcard/.thumbnails", "/sdcard/DCIM/.thumbnails",
                    "/storage/emulated/0/.thumbnails", "/storage/emulated/0/DCIM/.thumbnails",
                    "/sdcard/Pictures/.thumbnails", "/storage/emulated/0/Pictures/.thumbnails",
                ], timeout=12, title="Miniaturas comunes")

                step("Limpieza segura terminada. Sin root no se puede vaciar toda la caché interna de Android.")
                with _lock:
                    _cache_job.update({"running": False, "finished_at": now_text(), "message": "\n".join(lines[-30:])})
            except Exception as exc:
                append_log("Borrar caché global", ok=False, output=str(exc))
                with _lock:
                    _cache_job.update({"running": False, "finished_at": now_text(), "message": f"ERROR: {exc}"})

        threading.Thread(target=worker, args=(serial,), daemon=True).start()
        return jsonify({"ok": True, "message": "Limpieza de caché iniciada. Se ejecuta en segundo plano para evitar timeouts."})
    except Exception as exc:
        append_log("Borrar caché global", ok=False, output=str(exc))
        return jsonify({"ok": False, "message": f"No se pudo iniciar la limpieza de caché: {exc}"})


@app.get("/api/commands/clear-all-cache/status")
def api_clear_all_cache_status():
    with _lock:
        return jsonify({"ok": True, "job": dict(_cache_job)})


@app.post("/api/commands/info")
def api_device_info():
    props = [
        "ro.product.manufacturer",
        "ro.product.model",
        "ro.product.device",
        "ro.build.version.release",
        "ro.build.version.sdk",
        "ro.serialno",
    ]
    lines = []
    ok_all = True
    for prop in props:
        ok, out, _ = adb_for_active(["shell", "getprop", prop], timeout=8, title=None)
        if ok:
            lines.append(f"{prop}: {out.strip()}")
        else:
            ok_all = False
            lines.append(f"{prop}: ERROR")
    output = "\n".join(lines)
    append_log("Info dispositivo", ok=ok_all, output=output)
    return jsonify({"ok": ok_all, "message": output})


@app.post("/api/commands/logcat")
def api_logcat():
    ok, out, _ = adb_for_active(["logcat", "-d", "-t", "120"], timeout=20, title="Logcat corto")
    return jsonify({"ok": ok, "message": out.strip()[-8000:] if out else ""})


@app.post("/api/commands/screenshot")
def api_screenshot():
    serial = active_serial()
    if not serial:
        msg = "No hay dispositivo activo."
        append_log("Screenshot", ok=False, output=msg)
        return jsonify({"ok": False, "message": msg})
    safe_serial = re.sub(r"[^a-zA-Z0-9_.-]+", "_", serial)
    remote_dir = "/sdcard/Pictures/9ADB"
    remote = f"{remote_dir}/screenshot_{safe_serial}_{int(time.time())}.png"
    run_adb_for_serial(serial, ["shell", "mkdir", "-p", remote_dir], timeout=10, title=None)
    ok, out, _ = run_adb_for_serial(serial, ["shell", "screencap", "-p", remote], timeout=25, title="Screenshot en Android")
    if ok:
        return jsonify({"ok": True, "message": f"Screenshot guardado en el móvil: {remote}", "remote": remote})
    return jsonify({"ok": False, "message": out.strip() or "No se pudo crear la captura en el Android."})


@app.post("/api/commands/screenshot-pull")
def api_screenshot_pull():
    serial = active_serial()
    if not serial:
        msg = "No hay dispositivo activo."
        append_log("Screenshot & pull", ok=False, output=msg)
        return jsonify({"ok": False, "message": msg})
    safe_serial = re.sub(r"[^a-zA-Z0-9_.-]+", "_", serial)
    filename = f"screenshot_pull_{safe_serial}_{int(time.time())}.png"
    remote = "/sdcard/screen.png"
    local = SCREENSHOT_DIR / filename
    ok1, out1, _ = run_adb_for_serial(serial, ["shell", "screencap", "-p", remote], timeout=20, title="Screenshot en Android")
    if not ok1:
        return jsonify({"ok": False, "message": out1.strip() or "No se pudo crear la captura en Android."})
    ok2, out2, _ = run_adb_for_serial(serial, ["pull", remote, str(local)], timeout=40, title="Screenshot & pull")
    run_adb_for_serial(serial, ["shell", "rm", remote], timeout=8, title=None)
    if ok2 and local.exists() and local.stat().st_size > 0:
        return jsonify({
            "ok": True,
            "message": f"Screenshot listo para descargar: {filename}",
            "file": filename,
            "download_url": f"/api/download/screenshots/{filename}",
            "download_name": filename,
        })
    return jsonify({"ok": False, "message": out2.strip() or "No se pudo descargar la captura."})


@app.post("/api/commands/screenrecord/start")
def api_screenrecord_start():
    serial = active_serial()
    if not serial:
        msg = "No hay dispositivo activo."
        append_log("Start screenrecord", ok=False, output=msg)
        return jsonify({"ok": False, "message": msg})
    current = _screenrecord_processes.get(serial)
    if current and current.get("process") and current["process"].poll() is None:
        return jsonify({"ok": False, "message": "Ya hay un screenrecord en ejecución para este dispositivo."})
    safe_serial = re.sub(r"[^a-zA-Z0-9_.-]+", "_", serial)
    stamp = int(time.time())
    remote = f"/sdcard/record_{safe_serial}_{stamp}.mp4"
    env = os.environ.copy()
    env["HOME"] = str(DATA_DIR)
    try:
        proc = subprocess.Popen(["adb", "-s", serial, "shell", "screenrecord", remote], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        _screenrecord_processes[serial] = {"process": proc, "remote": remote, "started_at": now_text(), "stamp": stamp}
        append_log("Start screenrecord", ok=True, command=["adb", "-s", serial, "shell", "screenrecord", remote], output=remote)
        return jsonify({"ok": True, "message": f"Grabando en {remote}. Puedes usar Stop screenrecord para parar o Stop screenrecord & pull para descargar."})
    except Exception as exc:
        append_log("Start screenrecord", ok=False, output=str(exc))
        return jsonify({"ok": False, "message": str(exc)})


@app.post("/api/commands/screenrecord/stop-only")
def api_screenrecord_stop_only():
    serial = active_serial()
    if not serial:
        msg = "No hay dispositivo activo."
        append_log("Stop screenrecord", ok=False, output=msg)
        return jsonify({"ok": False, "message": msg})
    item = _screenrecord_processes.get(serial)
    if not item or not item.get("process"):
        msg = "No hay screenrecord iniciado desde esta web para el dispositivo activo."
        append_log("Stop screenrecord", ok=False, output=msg)
        return jsonify({"ok": False, "message": msg})
    proc = item["process"]
    remote = item.get("remote") or "/sdcard/record.mp4"
    if proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=6)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    _screenrecord_processes.pop(serial, None)
    append_log("Stop screenrecord", ok=True, output=f"Grabación detenida. Archivo en Android: {remote}")
    return jsonify({"ok": True, "message": f"Grabación detenida. Se queda en el móvil: {remote}"})


@app.post("/api/commands/screenrecord/stop")
def api_screenrecord_stop():
    serial = active_serial()
    if not serial:
        msg = "No hay dispositivo activo."
        append_log("Stop screenrecord & pull", ok=False, output=msg)
        return jsonify({"ok": False, "message": msg})
    item = _screenrecord_processes.get(serial)
    if not item or not item.get("process"):
        msg = "No hay screenrecord iniciado desde esta web para el dispositivo activo."
        append_log("Stop screenrecord & pull", ok=False, output=msg)
        return jsonify({"ok": False, "message": msg})
    proc = item["process"]
    remote = item.get("remote") or "/sdcard/record.mp4"
    if proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=6)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    time.sleep(1.2)
    safe_serial = re.sub(r"[^a-zA-Z0-9_.-]+", "_", serial)
    filename = f"record_{safe_serial}_{item.get('stamp') or int(time.time())}.mp4"
    local = SCREENRECORD_DIR / filename
    ok, out, _ = run_adb_for_serial(serial, ["pull", remote, str(local)], timeout=90, title="Stop screenrecord & pull")
    run_adb_for_serial(serial, ["shell", "rm", remote], timeout=8, title=None)
    _screenrecord_processes.pop(serial, None)
    if ok and local.exists() and local.stat().st_size > 0:
        return jsonify({
            "ok": True,
            "message": f"Grabación lista para descargar: {filename}",
            "file": filename,
            "download_url": f"/api/download/screenrecords/{filename}",
            "download_name": filename,
        })
    return jsonify({"ok": False, "message": out.strip() or "No se pudo descargar la grabación."})


@app.post("/api/commands/install-apk")
def api_install_apk():
    serial = active_serial()
    if not serial:
        msg = "No hay dispositivo activo."
        append_log("Install APK", ok=False, output=msg)
        return jsonify({"ok": False, "message": msg})
    file = request.files.get("apk")
    if not file or not file.filename:
        return jsonify({"ok": False, "message": "Selecciona un archivo .apk."})
    filename = safe_filename(file.filename, "app.apk")
    if not filename.lower().endswith(".apk"):
        return jsonify({"ok": False, "message": "El archivo debe ser .apk."})
    path = UPLOADS_DIR / f"{int(time.time())}_{filename}"
    file.save(path)
    ok, out, _ = run_adb_for_serial(serial, ["install", "-r", str(path)], timeout=180, title=f"Install APK {filename}")
    return jsonify({"ok": ok, "message": out.strip() or ("APK instalado." if ok else "No se pudo instalar el APK.")})


@app.post("/api/commands/wallpaper")
def api_wallpaper():
    serial = active_serial()
    if not serial:
        msg = "No hay dispositivo activo."
        append_log("Elegir fondo", ok=False, output=msg)
        return jsonify({"ok": False, "message": msg})
    if not WALLPAPER_AGENT_APK.exists():
        msg = "No se encuentra tools/WallpaperAgent.apk dentro del proyecto."
        append_log("Elegir fondo", ok=False, output=msg)
        return jsonify({"ok": False, "message": msg})
    file = request.files.get("image")
    if not file or not file.filename:
        return jsonify({"ok": False, "message": "Selecciona una imagen."})
    filename = safe_filename(file.filename, "wallpaper.jpg")
    ext = Path(filename).suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        return jsonify({"ok": False, "message": "Usa una imagen .jpg, .jpeg, .png o .webp."})
    local = UPLOADS_DIR / f"wallpaper_{int(time.time())}_{filename}"
    file.save(local)

    ok_pkg, out_pkg, _ = run_adb_for_serial(serial, ["shell", "pm", "list", "packages", WALLPAPER_AGENT_PACKAGE], timeout=15, title="Comprobar WallpaperAgent")
    if WALLPAPER_AGENT_PACKAGE not in (out_pkg or ""):
        ok_install, out_install, _ = run_adb_for_serial(serial, ["install", "-r", str(WALLPAPER_AGENT_APK)], timeout=120, title="Instalar WallpaperAgent.apk")
        if not ok_install:
            return jsonify({"ok": False, "message": out_install.strip() or "No se pudo instalar WallpaperAgent.apk."})

    remote = f"/data/local/tmp/{safe_filename(local.name, 'fondo.jpg')}"
    ok_push, out_push, _ = run_adb_for_serial(serial, ["push", str(local), remote], timeout=60, title="Subir fondo al Android")
    if not ok_push:
        return jsonify({"ok": False, "message": out_push.strip() or "No se pudo subir la imagen al Android."})
    ok_broadcast, out_broadcast, _ = run_adb_for_serial(serial, ["shell", "am", "broadcast", "-n", WALLPAPER_AGENT_RECEIVER, "--es", "path", remote], timeout=30, title="Aplicar fondo con WallpaperAgent")
    run_adb_for_serial(serial, ["shell", "rm", remote], timeout=8, title=None)
    return jsonify({"ok": ok_broadcast, "message": out_broadcast.strip() or ("Fondo aplicado mediante WallpaperAgent.apk." if ok_broadcast else "No se pudo aplicar el fondo.")})



def read_app_label_cache() -> Dict[str, Any]:
    data = read_json(APP_LABEL_CACHE_FILE, {})
    return data if isinstance(data, dict) else {}


def save_app_label_cache(data: Dict[str, Any]) -> None:
    write_json(APP_LABEL_CACHE_FILE, data)


KNOWN_APP_LABELS = {
    "ch.protonvpn.android": "Proton VPN",
    "com.android.vending": "Play Store",
    "com.google.android.youtube": "YouTube",
    "com.android.chrome": "Chrome",
    "com.google.android.gm": "Gmail",
    "com.google.android.apps.maps": "Maps",
    "com.google.android.apps.photos": "Fotos",
    "com.google.android.dialer": "Teléfono",
    "com.android.dialer": "Teléfono",
    "com.google.android.apps.messaging": "Mensajes",
    "com.google.android.contacts": "Contactos",
    "com.google.android.calendar": "Calendario",
    "com.google.android.deskclock": "Reloj",
    "com.google.android.calculator": "Calculadora",
    "com.motorola.camera3": "Cámara",
    "com.motorola.camera": "Cámara",
    "com.motorola.cameraone": "Cámara",
    "com.google.android.GoogleCamera": "Cámara",
}


def humanize_package_name(package: str) -> str:
    package = (package or "").strip()
    if not package:
        return "App"
    if package in KNOWN_APP_LABELS:
        return KNOWN_APP_LABELS[package]
    # Remove common namespaces and turn meaningful tokens into a readable fallback.
    cleaned = re.sub(r"^(com|org|net|io|app|android|google|ch|es|me)\.", "", package, flags=re.I)
    parts = [p for p in re.split(r"[._-]+", cleaned) if p]
    noise = {"android", "google", "app", "apps", "mobile", "client", "service", "services", "main", "provider", "overlay", "module", "config", "launcher"}
    useful = [p for p in parts if p.lower() not in noise]
    chosen = useful[-2:] if len(useful) >= 2 else (useful or parts[-2:] or [package])
    words = []
    acronyms = {"vpn": "VPN", "adb": "ADB", "nfc": "NFC", "sms": "SMS", "gps": "GPS", "pdf": "PDF", "ui": "UI"}
    for token in chosen:
        token = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", token).replace("_", " ").replace("-", " ")
        for piece in token.split():
            low = piece.lower()
            words.append(acronyms.get(low, piece[:1].upper() + piece[1:]))
    text = " ".join(words).strip()
    return text or package


def extract_label_from_aapt(output: str) -> str:
    """Return the launcher/home label when possible.

    `application-label` can be generic or wrong on some APKs. The name the
    user sees on Android's launcher usually comes from `launchable-activity`,
    so prefer that and only fall back to the application label.
    """
    output = output or ""
    launchable_patterns = [
        r"launchable-activity:[^\n]*label='([^']+)'",
        r"launchable activity name='[^']+'[^\n]*label='([^']+)'",
    ]
    for pattern in launchable_patterns:
        m = re.search(pattern, output)
        if m and m.group(1).strip():
            return m.group(1).strip()

    patterns = [
        r"application-label-es-ES:'([^']+)'",
        r"application-label-es:'([^']+)'",
        r"application-label:'([^']+)'",
        r"application:\s+label='([^']+)'",
    ]
    for pattern in patterns:
        m = re.search(pattern, output)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return ""


def label_for_app(serial: str, package: str, apk_path: str, cache: Dict[str, Any]) -> str:
    fallback = humanize_package_name(package)
    if package in KNOWN_APP_LABELS:
        cache[package] = {"apk_path": apk_path, "label": fallback, "label_strategy": "known_v1", "updated_at": now_text()}
        return fallback
    if not serial or not package or not apk_path:
        return fallback
    cached = cache.get(package)
    if (
        isinstance(cached, dict)
        and cached.get("apk_path") == apk_path
        and cached.get("label")
        and cached.get("label_strategy") == "launchable_v3"
    ):
        return str(cached["label"])
    if not shutil.which("aapt"):
        return fallback

    label_tmp_dir = TEMP_DOWNLOAD_DIR / "app_labels"
    label_tmp_dir.mkdir(parents=True, exist_ok=True)
    local = label_tmp_dir / safe_filename(f"{package}.apk")
    try:
        ok_pull, out_pull, _ = run_adb_for_serial(serial, ["pull", apk_path, str(local)], timeout=35, title=None)
        if not ok_pull or not local.exists() or local.stat().st_size <= 0:
            return fallback
        ok_aapt, out_aapt, _ = run_cmd(["aapt", "dump", "badging", str(local)], timeout=12, log_title=None)
        label = extract_label_from_aapt(out_aapt if ok_aapt else "") or fallback
        if not label or label == package or label.startswith("@") or "." in label and len(label) > 24:
            label = fallback
        cache[package] = {"apk_path": apk_path, "label": label, "label_strategy": "launchable_v3", "updated_at": now_text()}
        return label
    except Exception:
        return fallback
    finally:
        try:
            local.unlink(missing_ok=True)
        except Exception:
            pass


def enrich_app_labels(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    serial = active_serial()
    cache = read_app_label_cache()
    changed = False
    start_time = time.time()
    for item in items:
        package = item.get("package", "")
        apk_path = item.get("apk_path", "")
        before = cache.get(package)
        label = label_for_app(serial, package, apk_path, cache)
        item["label"] = label
        item["name"] = label or humanize_package_name(package)
        if cache.get(package) != before:
            changed = True
        # Avoid making the first load unbearably slow on huge system-app lists.
        if time.time() - start_time > 55:
            for remaining in items[items.index(item)+1:]:
                remaining["label"] = remaining.get("label") or humanize_package_name(remaining.get("package", ""))
                remaining["name"] = remaining["label"]
            break
    if changed:
        save_app_label_cache(cache)
    return items


def validate_package_name(package: str) -> str:
    package = (package or "").strip()
    if not re.match(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$", package):
        return ""
    return package


def parse_package_list(output: str, kind: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for raw_line in (output or "").splitlines():
        line = raw_line.strip().replace("\r", "")
        if not line.startswith("package:"):
            continue
        value = line[len("package:"):]
        apk_path = ""
        package = value
        if "=" in value:
            apk_path, package = value.rsplit("=", 1)
        package = package.strip()
        if not validate_package_name(package):
            continue
        short_name = package.split(".")[-1]
        items.append({
            "package": package,
            "name": short_name,
            "apk_path": apk_path.strip(),
            "kind": kind,
        })
    return items


def list_android_apps(kind: str = "user", query: str = "") -> Tuple[bool, List[Dict[str, Any]], str]:
    kind = kind if kind in {"user", "system", "all"} else "user"
    query = (query or "").strip().lower()
    if not active_serial():
        msg = "No hay dispositivo activo."
        append_log("Listar apps", ok=False, output=msg)
        return False, [], msg

    if kind == "all":
        ok_user, out_user, _ = adb_for_active(["shell", "pm", "list", "packages", "-f", "-3"], timeout=35, title="Listar apps de usuario")
        ok_system, out_system, _ = adb_for_active(["shell", "pm", "list", "packages", "-f", "-s"], timeout=45, title="Listar apps de sistema")
        items = parse_package_list(out_user if ok_user else "", "user") + parse_package_list(out_system if ok_system else "", "system")
        ok = ok_user or ok_system
        raw_msg = "" if ok else ((out_user or "") + "\n" + (out_system or "")).strip()
    else:
        flag = "-3" if kind == "user" else "-s"
        ok, out, _ = adb_for_active(["shell", "pm", "list", "packages", "-f", flag], timeout=45, title=f"Listar apps {'de usuario' if kind == 'user' else 'de sistema'}")
        items = parse_package_list(out if ok else "", kind)
        raw_msg = out.strip() if not ok else ""

    items = enrich_app_labels(items)
    if query:
        items = [
            item for item in items
            if query in item["package"].lower()
            or query in item.get("name", "").lower()
            or query in item.get("label", "").lower()
        ]
    items.sort(key=lambda item: (item.get("kind", ""), item.get("name", "").lower(), item.get("package", "")))
    return ok, items, raw_msg


@app.get("/api/apps")
def api_apps_list():
    kind = request.args.get("kind", "user")
    query = request.args.get("q", "")
    ok, items, message = list_android_apps(kind, query)
    return jsonify({"ok": ok, "apps": items, "count": len(items), "kind": kind, "query": query, "message": message or f"{len(items)} apps encontradas."})


@app.post("/api/apps/<package>/open")
def api_app_open(package: str):
    package = validate_package_name(package)
    if not package:
        return jsonify({"ok": False, "message": "Paquete inválido."})
    ok, out, _ = open_app_from_candidates(package, [package])
    return jsonify({"ok": ok, "message": out.strip() or (f"Abierta: {package}" if ok else f"No se pudo abrir: {package}")})


@app.post("/api/apps/<package>/stop")
def api_app_stop(package: str):
    package = validate_package_name(package)
    if not package:
        return jsonify({"ok": False, "message": "Paquete inválido."})
    ok, out, _ = adb_for_active(["shell", "am", "force-stop", package], timeout=15, title=f"Cerrar app {package}")
    return jsonify({"ok": ok, "message": out.strip() or (f"App cerrada: {package}" if ok else f"No se pudo cerrar: {package}")})


@app.post("/api/apps/<package>/kill")
def api_app_kill(package: str):
    package = validate_package_name(package)
    if not package:
        return jsonify({"ok": False, "message": "Paquete inválido."})

    # `am kill` only kills background processes on many Android versions and may
    # appear to do nothing. Try it first, then force-stop as a reliable fallback.
    ok_kill, out_kill, _ = adb_for_active(["shell", "am", "kill", package], timeout=15, title=f"Kill app {package}")
    check_ok, pid_out, _ = adb_for_active(["shell", "pidof", package], timeout=8, title=None)
    if check_ok and pid_out.strip():
        ok_force, out_force, _ = adb_for_active(["shell", "am", "force-stop", package], timeout=15, title=f"Kill fallback force-stop {package}")
        combined = (out_kill.strip() + "\n" + out_force.strip()).strip()
        return jsonify({"ok": ok_force, "message": combined or (f"Kill forzado: {package}" if ok_force else f"No se pudo matar: {package}")})
    return jsonify({"ok": ok_kill, "message": out_kill.strip() or (f"Kill enviado: {package}" if ok_kill else f"No se pudo hacer kill: {package}")})


@app.post("/api/apps/<package>/cache")
def api_app_cache(package: str):
    package = validate_package_name(package)
    if not package:
        return jsonify({"ok": False, "message": "Paquete inválido."})

    # Newer Androids support cache-only clear. Do not fall back to `pm clear`,
    # because that would wipe user data. External cache cleanup is best-effort.
    ok_cache, out_cache, _ = adb_for_active(["shell", "pm", "clear", "--cache-only", package], timeout=30, title=f"Borrar caché app {package}")
    if ok_cache and "unknown option" not in (out_cache or "").lower() and "exception" not in (out_cache or "").lower():
        return jsonify({"ok": True, "message": out_cache.strip() or f"Caché borrada: {package}"})

    external_targets = [
        f"/sdcard/Android/data/{package}/cache",
        f"/sdcard/Android/media/{package}/cache",
    ]
    outputs = [out_cache.strip()] if out_cache.strip() else []
    any_ok = False
    for target in external_targets:
        ok_rm, out_rm, _ = adb_for_active(["shell", "rm", "-rf", target], timeout=15, title=f"Borrar caché externa {package}")
        any_ok = any_ok or ok_rm
        if out_rm.strip():
            outputs.append(out_rm.strip())

    if any_ok:
        extra = "\n".join(x for x in outputs if x)
        return jsonify({"ok": True, "message": (extra + "\n" if extra else "") + f"Caché externa borrada si Android permitió acceso: {package}"})
    return jsonify({"ok": False, "message": "Este Android no permite borrar solo caché por ADB sin root. No he usado pm clear porque eso borraría datos."})


@app.post("/api/apps/<package>/clear")
def api_app_clear(package: str):
    package = validate_package_name(package)
    if not package:
        return jsonify({"ok": False, "message": "Paquete inválido."})
    ok, out, _ = adb_for_active(["shell", "pm", "clear", package], timeout=30, title=f"Borrar datos app {package}")
    return jsonify({"ok": ok, "message": out.strip() or (f"Datos borrados: {package}" if ok else f"No se pudieron borrar datos: {package}")})


@app.post("/api/apps/<package>/uninstall")
def api_app_uninstall(package: str):
    package = validate_package_name(package)
    if not package:
        return jsonify({"ok": False, "message": "Paquete inválido."})
    ok, out, _ = adb_for_active(["uninstall", package], timeout=80, title=f"Desinstalar app {package}")
    return jsonify({"ok": ok, "message": out.strip() or (f"App desinstalada: {package}" if ok else f"No se pudo desinstalar: {package}")})


@app.get("/api/apps/<package>/path")
def api_app_path(package: str):
    package = validate_package_name(package)
    if not package:
        return jsonify({"ok": False, "message": "Paquete inválido."})
    ok, out, _ = adb_for_active(["shell", "pm", "path", package], timeout=15, title=f"Ruta APK {package}")
    clean = out.strip()
    return jsonify({"ok": ok, "message": clean or (f"Sin ruta para {package}" if ok else f"No se pudo leer ruta de {package}"), "path": clean})


@app.post("/api/apps/<package>/pull-apk")
def api_app_pull_apk(package: str):
    package = validate_package_name(package)
    if not package:
        return jsonify({"ok": False, "message": "Paquete inválido."})
    ok_path, out_path, _ = adb_for_active(["shell", "pm", "path", package], timeout=15, title=f"Localizar APK {package}")
    if not ok_path or not out_path.strip():
        return jsonify({"ok": False, "message": out_path.strip() or f"No se encontró APK para {package}."})
    paths = []
    for line in out_path.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            paths.append(line[len("package:"):])
    if not paths:
        return jsonify({"ok": False, "message": f"No se encontró ruta APK válida para {package}."})
    # Pull the first/base APK. Split APKs can be added in a later phase if needed.
    remote = paths[0]
    filename = safe_filename(f"{package}_{int(time.time())}.apk")
    local = APK_PULL_DIR / filename
    ok_pull, out_pull, _ = adb_for_active(["pull", remote, str(local)], timeout=90, title=f"Pull APK {package}")
    if ok_pull and local.exists() and local.stat().st_size > 0:
        return jsonify({
            "ok": True,
            "message": f"APK listo para descargar: {filename}",
            "file": filename,
            "download_url": f"/api/download/apks/{filename}",
            "download_name": filename,
        })
    return jsonify({"ok": False, "message": out_pull.strip() or f"No se pudo descargar APK de {package}."})


# -----------------------------
# Fase 4.1 · Explorador Android temporal
# -----------------------------

ANDROID_ALLOWED_ROOTS = ("/sdcard", "/storage/emulated/0", "/data/local/tmp")
ANDROID_ROOTS = {"/sdcard", "/storage/emulated/0", "/data/local/tmp"}


def sh_quote(value: str) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"


def normalize_android_path(path_text: str, default: str = "/sdcard") -> str:
    raw = (path_text or default).strip() or default
    raw = raw.replace("\\", "/")
    if raw in {"~", "$EXTERNAL_STORAGE"}:
        raw = "/sdcard"
    if not raw.startswith("/"):
        raw = "/sdcard/" + raw.lstrip("/")
    parts = []
    for part in raw.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    clean = "/" + "/".join(parts)
    if clean == "/storage/emulated/0":
        return clean
    if not clean.startswith(ANDROID_ALLOWED_ROOTS):
        return default
    return clean.rstrip("/") or default


def android_parent(path_text: str) -> str:
    path = normalize_android_path(path_text)
    if path in ANDROID_ROOTS:
        return path
    parent = "/".join(path.rstrip("/").split("/")[:-1]) or "/sdcard"
    return normalize_android_path(parent, "/sdcard")


def android_join(base: str, name: str) -> str:
    base = normalize_android_path(base)
    name = str(name or "").replace("/", "").replace("\\", "").strip()
    return normalize_android_path(base.rstrip("/") + "/" + name, base)


def parse_android_file_table(output: str, base_path: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for line in (output or "").splitlines():
        if not line or line.startswith("__OK__"):
            continue
        if line.startswith("__ERR__"):
            continue
        parts = line.split("\t", 4)
        if len(parts) != 5:
            continue
        kind, is_link, size_text, mtime_text, name = parts
        if not name or name in {".", ".."}:
            continue
        try:
            size = int(size_text or 0)
        except Exception:
            size = 0
        try:
            mtime = int(mtime_text or 0)
        except Exception:
            mtime = 0
        child_path = android_join(base_path, name)
        date_text = ""
        if mtime > 0:
            try:
                date_text = _dt.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
            except Exception:
                date_text = ""
        items.append({
            "name": name,
            "path": child_path,
            "is_dir": kind == "d",
            "is_link": is_link == "1",
            "size": size,
            "date": date_text,
        })
    items.sort(key=lambda item: (not item.get("is_dir"), item.get("name", "").lower()))
    return items


def list_android_files(path_text: str) -> Tuple[bool, Dict[str, Any], str]:
    path = normalize_android_path(path_text)
    # No usamos ls -la porque cambia bastante entre fabricantes/versiones.
    # Este script devuelve una tabla simple: tipo, enlace, tamaño, mtime, nombre.
    script = f"""
DIR={sh_quote(path)}
if [ ! -d "$DIR" ]; then
  echo "__ERR__NOT_DIRECTORY__"
  exit 2
fi
LIST=$(ls -A1 "$DIR" 2>&1)
RC=$?
if [ "$RC" != "0" ]; then
  echo "__ERR__$LIST"
  exit "$RC"
fi
printf '%s\n' "$LIST" | while IFS= read -r name; do
  [ -z "$name" ] && continue
  f="$DIR/$name"
  if [ -d "$f" ]; then kind="d"; else kind="f"; fi
  if [ -L "$f" ]; then link="1"; else link="0"; fi
  size=$(stat -c '%s' "$f" 2>/dev/null || echo 0)
  mtime=$(stat -c '%Y' "$f" 2>/dev/null || echo 0)
  printf '%s\t%s\t%s\t%s\t%s\n' "$kind" "$link" "$size" "$mtime" "$name"
done
"""
    ok, out, _ = adb_for_active(["shell", "sh", "-c", script], timeout=45, title=f"Listar Android {path}")
    if not ok or "__ERR__" in out:
        err_line = next((line for line in out.splitlines() if line.startswith("__ERR__")), "")
        msg = err_line.replace("__ERR__", "", 1).strip("_ ") or out.strip() or f"No se pudo listar {path}."
        return False, {"path": path, "parent": android_parent(path), "items": []}, msg
    items = parse_android_file_table(out, path)
    return True, {"path": path, "parent": android_parent(path), "items": items}, ""


@app.get("/api/scripts")
def api_scripts_list():
    data = scripts()
    return jsonify({"ok": True, "scripts": [script_to_public(item) for item in data.values()]})


@app.post("/api/scripts")
def api_scripts_create():
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    if not name:
        return jsonify({"ok": False, "message": "El script necesita nombre."}), 400
    commands = normalize_script_commands(payload.get("commands", ""))
    if not commands:
        return jsonify({"ok": False, "message": "Añade al menos un comando ADB."}), 400
    data = scripts()
    base = clean_script_id(name)
    sid = base
    counter = 2
    while sid in data:
        sid = f"{base}-{counter}"
        counter += 1
    item = {
        "id": sid,
        "name": name,
        "description": str(payload.get("description", "")).strip(),
        "commands": commands,
        "created_at": now_text(),
        "updated_at": now_text(),
    }
    data[sid] = item
    save_scripts(data)
    return jsonify({"ok": True, "message": "Script creado.", "script": script_to_public(item), "scripts": [script_to_public(x) for x in data.values()]})


@app.route("/api/scripts/<script_id>", methods=["POST", "PUT"])
def api_scripts_update(script_id: str):
    data = scripts()
    if script_id not in data:
        return jsonify({"ok": False, "message": "Script no encontrado."}), 404
    payload = request.get_json(silent=True) or {}
    item = data[script_id]
    item["name"] = str(payload.get("name", item.get("name", "Script"))).strip() or item.get("name", "Script")
    item["description"] = str(payload.get("description", item.get("description", ""))).strip()
    commands = normalize_script_commands(payload.get("commands", item.get("commands", [])))
    if not commands:
        return jsonify({"ok": False, "message": "El script necesita comandos."}), 400
    item["commands"] = commands
    item["updated_at"] = now_text()
    data[script_id] = item
    save_scripts(data)
    return jsonify({"ok": True, "message": "Script actualizado.", "script": script_to_public(item), "scripts": [script_to_public(x) for x in data.values()]})


@app.post("/api/scripts/<script_id>/delete")
def api_scripts_delete(script_id: str):
    data = scripts()
    if script_id not in data:
        return jsonify({"ok": False, "message": "Script no encontrado."}), 404
    item = data.pop(script_id)
    save_scripts(data)
    return jsonify({"ok": True, "message": f"Script borrado: {item.get('name', script_id)}", "scripts": [script_to_public(x) for x in data.values()]})


@app.post("/api/scripts/<script_id>/duplicate")
def api_scripts_duplicate(script_id: str):
    data = scripts()
    if script_id not in data:
        return jsonify({"ok": False, "message": "Script no encontrado."}), 404
    source = data[script_id]
    base_name = f"{source.get('name', 'Script')} copia"
    base = clean_script_id(base_name)
    sid = base
    counter = 2
    while sid in data:
        sid = f"{base}-{counter}"
        counter += 1
    item = {
        "id": sid,
        "name": base_name,
        "description": source.get("description", ""),
        "commands": list(source.get("commands", [])),
        "created_at": now_text(),
        "updated_at": now_text(),
    }
    data[sid] = item
    save_scripts(data)
    return jsonify({"ok": True, "message": "Script duplicado.", "script": script_to_public(item), "scripts": [script_to_public(x) for x in data.values()]})


@app.post("/api/scripts/<script_id>/run")
def api_scripts_run(script_id: str):
    serial, msg = require_active()
    if not serial:
        return jsonify({"ok": False, "message": msg}), 400
    data = scripts()
    item = data.get(script_id)
    if not item:
        return jsonify({"ok": False, "message": "Script no encontrado."}), 404
    commands = normalize_script_commands(item.get("commands", []))
    outputs: List[str] = []
    append_log(f"Script: {item.get('name', script_id)}", ok=True, output=f"Ejecutando {len(commands)} comandos")
    for index, line in enumerate(commands, start=1):
        parsed_ok, args, parse_msg = parse_adb_script_line(line)
        if not parsed_ok:
            text = f"Línea {index}: {parse_msg}\n{line}"
            append_log(f"Script: {item.get('name', script_id)}", ok=False, output=text)
            return jsonify({"ok": False, "message": "\n".join(outputs + [text])}), 400
        ok, out, _ = run_adb_for_serial(serial, args, timeout=60, title=f"Script {item.get('name', script_id)} · línea {index}")
        line_out = f"$ adb -s {serial} {' '.join(args)}\n{out.strip()}".strip()
        outputs.append(line_out)
        if not ok:
            # Fallar una línea de ADB no es un error interno del servidor.
            # Devuelve JSON normal para que la web no enseñe "500 Internal Server Error".
            return jsonify({"ok": False, "message": "\n\n".join(outputs) or f"Falló la línea {index}."})
    return jsonify({"ok": True, "message": "\n\n".join(outputs) or "Script ejecutado."})



# =========================
# API Fase 7 · Fastboot
# =========================

@app.get("/api/fastboot/status")
def api_fastboot_status():
    return jsonify({
        "ok": True,
        "available": fastboot_available(),
        "version": fastboot_version(),
        "devices": fastboot_devices(log=True),
        "adb_active": active_serial(),
    })


@app.post("/api/fastboot/refresh")
def api_fastboot_refresh():
    return jsonify({
        "ok": True,
        "message": "Dispositivos fastboot actualizados.",
        "available": fastboot_available(),
        "version": fastboot_version(),
        "devices": fastboot_devices(log=True),
        "adb_active": active_serial(),
    })


@app.post("/api/fastboot/reboot-bootloader")
def api_fastboot_reboot_bootloader():
    serial, err = require_active()
    if err:
        return jsonify({"ok": False, "message": err, "devices": fastboot_devices()})
    visor_proxy("/api/scrcpy/stop", {}, timeout=3)
    ok, out, code = run_adb_for_serial(serial, ["reboot", "bootloader"], timeout=20, title="ADB reboot bootloader")
    msg = out.strip() or f"Orden enviada a {serial}. Espera unos segundos y pulsa Actualizar fastboot."
    return jsonify({"ok": ok, "message": msg, "code": code, "devices": fastboot_devices()})


@app.post("/api/fastboot/reboot-system")
def api_fastboot_reboot_system():
    data = request.get_json(silent=True) or {}
    serial, err = selected_fastboot_serial(data)
    if err:
        return jsonify({"ok": False, "message": err, "devices": fastboot_devices()})
    ok, out, code = run_fastboot(["reboot"], serial=serial, timeout=25, title="Fastboot reboot")
    return fastboot_json(ok, out or "Reiniciando Android desde fastboot.", code)


@app.post("/api/fastboot/reboot-bootloader-fastboot")
def api_fastboot_reboot_bootloader_fastboot():
    data = request.get_json(silent=True) or {}
    serial, err = selected_fastboot_serial(data)
    if err:
        return jsonify({"ok": False, "message": err, "devices": fastboot_devices()})
    ok, out, code = run_fastboot(["reboot", "bootloader"], serial=serial, timeout=25, title="Fastboot reboot bootloader")
    return fastboot_json(ok, out or "Reiniciando bootloader.", code)


@app.post("/api/fastboot/continue")
def api_fastboot_continue():
    data = request.get_json(silent=True) or {}
    serial, err = selected_fastboot_serial(data)
    if err:
        return jsonify({"ok": False, "message": err, "devices": fastboot_devices()})
    ok, out, code = run_fastboot(["continue"], serial=serial, timeout=25, title="Fastboot continue")
    return fastboot_json(ok, out or "Continue enviado.", code)


@app.post("/api/fastboot/getvar")
def api_fastboot_getvar():
    data = request.get_json(silent=True) or {}
    serial, err = selected_fastboot_serial(data)
    if err:
        return jsonify({"ok": False, "message": err, "devices": fastboot_devices()})
    var = (data.get("var") or "all").strip()
    if not re.match(r"^[a-zA-Z0-9_.:-]{1,80}$", var):
        return jsonify({"ok": False, "message": "Variable fastboot no válida.", "devices": fastboot_devices()})
    ok, out, code = run_fastboot(["getvar", var], serial=serial, timeout=45, title=f"Fastboot getvar {var}")
    return fastboot_json(ok or bool(out.strip()), out, code)


@app.post("/api/fastboot/oem-info")
def api_fastboot_oem_info():
    data = request.get_json(silent=True) or {}
    serial, err = selected_fastboot_serial(data)
    if err:
        return jsonify({"ok": False, "message": err, "devices": fastboot_devices()})
    outputs = []
    ok_any = False
    for cmd in (["oem", "device-info"], ["flashing", "get_unlock_ability"], ["getvar", "unlocked"], ["getvar", "secure"]):
        ok, out, code = run_fastboot(cmd, serial=serial, timeout=25, title="Fastboot info bloqueo")
        if out.strip():
            outputs.append("$ fastboot " + " ".join(cmd) + "\n" + out.strip())
        ok_any = ok_any or ok or bool(out.strip())
    return fastboot_json(ok_any, "\n\n".join(outputs), 0 if ok_any else 1)


@app.post("/api/fastboot/set-active")
def api_fastboot_set_active():
    data = request.get_json(silent=True) or {}
    serial, err = selected_fastboot_serial(data)
    if err:
        return jsonify({"ok": False, "message": err, "devices": fastboot_devices()})
    slot = (data.get("slot") or "").strip().lower()
    if slot not in {"a", "b"}:
        return jsonify({"ok": False, "message": "Slot no válido. Usa a o b.", "devices": fastboot_devices()})
    if data.get("confirm") != f"SLOT {slot.upper()}":
        return jsonify({"ok": False, "message": f"Confirmación requerida: SLOT {slot.upper()}", "devices": fastboot_devices()})
    ok, out, code = run_fastboot([f"--set-active={slot}"], serial=serial, timeout=35, title=f"Fastboot set-active {slot}")
    return fastboot_json(ok, out, code)


@app.post("/api/fastboot/boot-image")
def api_fastboot_boot_image():
    serial, err = selected_fastboot_serial(request.form)
    if err:
        return jsonify({"ok": False, "message": err, "devices": fastboot_devices()})
    file_obj = request.files.get("image")
    if not file_obj:
        return jsonify({"ok": False, "message": "Sube una imagen .img.", "devices": fastboot_devices()})
    path = save_fastboot_file(file_obj, fallback="boot.img")
    ok, out, code = run_fastboot(["boot", str(path)], serial=serial, timeout=180, title=f"Fastboot boot {path.name}")
    return fastboot_json(ok, out or "Imagen enviada con fastboot boot.", code, {"uploaded": path.name})


@app.post("/api/fastboot/flash-image")
def api_fastboot_flash_image():
    serial, err = selected_fastboot_serial(request.form)
    if err:
        return jsonify({"ok": False, "message": err, "devices": fastboot_devices()})
    confirm = (request.form.get("confirm") or "").strip()
    if confirm != "FLASH":
        return jsonify({"ok": False, "message": "Para flashear escribe FLASH en la confirmación.", "devices": fastboot_devices()})
    partition = normalize_partition(request.form.get("partition") or "")
    if not partition:
        return jsonify({"ok": False, "message": "Partición no válida.", "devices": fastboot_devices()})
    file_obj = request.files.get("image")
    if not file_obj:
        return jsonify({"ok": False, "message": "Sube una imagen .img.", "devices": fastboot_devices()})
    path = save_fastboot_file(file_obj, fallback=f"{partition}.img")
    ok, out, code = run_fastboot(["flash", partition, str(path)], serial=serial, timeout=300, title=f"Fastboot flash {partition}")
    return fastboot_json(ok, out, code, {"uploaded": path.name, "partition": partition})


@app.post("/api/fastboot/erase")
def api_fastboot_erase():
    data = request.get_json(silent=True) or {}
    serial, err = selected_fastboot_serial(data)
    if err:
        return jsonify({"ok": False, "message": err, "devices": fastboot_devices()})
    partition = normalize_partition(data.get("partition") or "")
    if not partition:
        return jsonify({"ok": False, "message": "Partición no válida.", "devices": fastboot_devices()})
    if data.get("confirm") != "ERASE":
        return jsonify({"ok": False, "message": "Para borrar escribe ERASE.", "devices": fastboot_devices()})
    ok, out, code = run_fastboot(["erase", partition], serial=serial, timeout=120, title=f"Fastboot erase {partition}")
    return fastboot_json(ok, out, code)


@app.post("/api/fastboot/danger")
def api_fastboot_danger():
    data = request.get_json(silent=True) or {}
    serial, err = selected_fastboot_serial(data)
    if err:
        return jsonify({"ok": False, "message": err, "devices": fastboot_devices()})
    action = (data.get("action") or "").strip()
    commands = {
        "flashing_unlock": ["flashing", "unlock"],
        "flashing_lock": ["flashing", "lock"],
        "oem_unlock": ["oem", "unlock"],
        "oem_lock": ["oem", "lock"],
    }
    if action not in commands:
        return jsonify({"ok": False, "message": "Acción peligrosa no válida.", "devices": fastboot_devices()})
    if data.get("confirm") != "BORRAR DATOS":
        return jsonify({"ok": False, "message": "Confirmación requerida: BORRAR DATOS", "devices": fastboot_devices()})
    ok, out, code = run_fastboot(commands[action], serial=serial, timeout=90, title=f"Fastboot peligro {action}")
    return fastboot_json(ok, out, code)


@app.post("/api/fastboot/custom")
def api_fastboot_custom():
    data = request.get_json(silent=True) or {}
    serial, err = selected_fastboot_serial(data)
    if err:
        return jsonify({"ok": False, "message": err, "devices": fastboot_devices()})
    raw = (data.get("args") or "").strip()
    if not raw:
        return jsonify({"ok": False, "message": "Escribe argumentos fastboot.", "devices": fastboot_devices()})
    try:
        args = shlex.split(raw)
    except Exception as exc:
        return jsonify({"ok": False, "message": f"Argumentos no válidos: {exc}", "devices": fastboot_devices()})
    if not args:
        return jsonify({"ok": False, "message": "Sin argumentos.", "devices": fastboot_devices()})
    danger_words = {"flash", "erase", "format", "unlock", "lock", "flashing", "oem"}
    if any(x in danger_words for x in args) and data.get("confirm") != "DANGER":
        return jsonify({"ok": False, "message": "Comando peligroso. Para ejecutarlo escribe DANGER.", "devices": fastboot_devices()})
    ok, out, code = run_fastboot(args, serial=serial, timeout=180, title=f"Fastboot custom {raw}")
    return fastboot_json(ok, out, code)


@app.get("/api/files/android/list")
def api_files_android_list():
    ok, data, msg = list_android_files(request.args.get("path", "/sdcard"))
    return jsonify({"ok": ok, "message": msg or f"{len(data.get('items', []))} elementos.", **data})


@app.post("/api/files/android/mkdir")
def api_files_android_mkdir():
    data = request.get_json(silent=True) or {}
    base = normalize_android_path(data.get("path", "/sdcard"))
    name = safe_filename(data.get("name", "Nueva_carpeta"), "Nueva_carpeta")
    remote = android_join(base, name)
    ok, out, _ = adb_for_active(["shell", "mkdir", "-p", remote], timeout=20, title=f"Crear carpeta Android {remote}")
    return jsonify({"ok": ok, "message": out.strip() or (f"Carpeta creada: {remote}" if ok else f"No se pudo crear: {remote}"), "path": remote})


@app.post("/api/files/android/delete")
def api_files_android_delete():
    data = request.get_json(silent=True) or {}
    remote = normalize_android_path(data.get("path", ""))
    if remote in ANDROID_ROOTS:
        return jsonify({"ok": False, "message": "No borro la raíz de una zona permitida."})
    ok, out, _ = adb_for_active(["shell", "rm", "-rf", remote], timeout=60, title=f"Borrar Android {remote}")
    return jsonify({"ok": ok, "message": out.strip() or (f"Borrado: {remote}" if ok else f"No se pudo borrar: {remote}")})


@app.post("/api/files/android/upload")
def api_files_android_upload():
    file = request.files.get("file")
    remote_dir = normalize_android_path(request.form.get("path", "/sdcard/Download"), "/sdcard/Download")
    if not file or not file.filename:
        return jsonify({"ok": False, "message": "Selecciona un archivo."})
    filename = safe_filename(file.filename, "upload.bin")
    remote = android_join(remote_dir, filename)
    tmp_dir = Path(tempfile.mkdtemp(prefix="9adb_push_", dir=str(TEMP_DOWNLOAD_DIR)))
    local = tmp_dir / filename
    try:
        file.save(local)
        adb_for_active(["shell", "mkdir", "-p", remote_dir], timeout=20, title=f"Preparar carpeta Android {remote_dir}")
        ok, out, _ = adb_for_active(["push", str(local), remote], timeout=300, title=f"Subir a Android {remote}")
        return jsonify({"ok": ok, "message": out.strip() or (f"Subido a Android: {remote}" if ok else f"No se pudo subir a {remote}."), "remote": remote})
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.post("/api/files/android/download")
def api_files_android_download():
    data = request.get_json(silent=True) or {}
    remote = normalize_android_path(data.get("path", ""))
    if remote in ANDROID_ROOTS:
        return jsonify({"ok": False, "message": "No descargo una raíz completa. Entra en una carpeta concreta o elige un archivo."}), 400
    base_name = safe_filename(Path(remote).name or f"android_{int(time.time())}", "android_file")
    tmp_dir = Path(tempfile.mkdtemp(prefix="9adb_pull_", dir=str(TEMP_DOWNLOAD_DIR)))
    local = tmp_dir / base_name
    ok, out, _ = adb_for_active(["pull", remote, str(local)], timeout=600, title=f"Descargar de Android {remote}")
    if not ok or not local.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"ok": False, "message": out.strip() or f"No se pudo descargar {remote}."}), 500

    file_path = local
    download_name = base_name
    if local.is_dir():
        archive_path = Path(shutil.make_archive(str(tmp_dir / base_name), "zip", root_dir=local))
        file_path = archive_path
        download_name = base_name + ".zip"

    response = send_file(file_path, as_attachment=True, download_name=download_name)
    response.call_on_close(lambda: shutil.rmtree(tmp_dir, ignore_errors=True))
    return response


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "20009"))
    app.run(host="0.0.0.0", port=port, debug=False)
