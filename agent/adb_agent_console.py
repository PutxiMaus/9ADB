#!/usr/bin/env python3
import concurrent.futures
import getpass
import ipaddress
import json
import os
import platform
import queue
import socket
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

try:
    import paramiko
except Exception:
    paramiko = None

APP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "ADB-Agent"
APP_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = APP_DIR / "config.json"
CLIENT_ID_FILE = APP_DIR / "client_id.txt"


def clear():
    os.system("cls" if os.name == "nt" else "clear")


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_client_id():
    if CLIENT_ID_FILE.exists():
        value = CLIENT_ID_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = f"{platform.node() or 'client'}-{uuid.uuid4().hex[:8]}"
    CLIENT_ID_FILE.write_text(value, encoding="utf-8")
    return value


def ask(text, default=""):
    suffix = f" [{default}]" if default else ""
    value = input(f"{text}{suffix}: ").strip()
    return value or default


def local_ips():
    ips = set()
    try:
        hostname = socket.gethostname()
        for item in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = item[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return sorted(ips)


def networks_from_ips():
    nets = []
    for ip in local_ips():
        try:
            net = str(ipaddress.ip_network(f"{ip}/24", strict=False))
            if net not in nets:
                nets.append(net)
        except Exception:
            pass
    return nets or ["192.168.1.0/24"]


def choose_network():
    nets = networks_from_ips()
    print("\nRedes detectadas en este PC:")
    for i, net in enumerate(nets, 1):
        print(f"  {i}) {net}")
    print("  0) Escribir otra")
    raw = ask("Elige red", "1")
    try:
        idx = int(raw)
    except Exception:
        idx = 1
    if idx == 0:
        return ask("Red CIDR", nets[0])
    if 1 <= idx <= len(nets):
        return nets[idx - 1]
    return nets[0]


def port_open(ip, port, timeout=0.45):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def scan_network(network, adb_port=5555):
    net = ipaddress.ip_network(network, strict=False)
    hosts = [str(h) for h in net.hosts()]
    found = []
    print(f"\nEscaneando {network} puerto ADB {adb_port}...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=96) as executor:
        futures = {executor.submit(port_open, ip, adb_port): ip for ip in hosts}
        for fut in concurrent.futures.as_completed(futures):
            ip = futures[fut]
            try:
                if fut.result():
                    item = {"ip": ip, "port": adb_port, "serial": f"{ip}:{adb_port}", "adb_open": True, "status": "detectado"}
                    found.append(item)
                    print(f"  encontrado: {ip}:{adb_port}")
            except Exception:
                pass
    found.sort(key=lambda d: tuple(int(x) for x in d["ip"].split(".")))
    return found


def post_json(server, path, payload, token=""):
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-ADB-Agent-Token"] = token
        headers["X-9ADB-Agent-Token"] = token
    req = urllib.request.Request(server.rstrip("/") + path, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=8) as response:
        return response.status, response.read().decode("utf-8", errors="replace")


def parse_ssh_target(target):
    value = target.strip()
    port = 22
    if "@" in value:
        user, host = value.split("@", 1)
    else:
        user, host = getpass.getuser(), value
    if ":" in host:
        host, text = host.rsplit(":", 1)
        try:
            port = int(text)
        except Exception:
            port = 22
    return user, host, port


class ReverseTunnel:
    def __init__(self, ssh_target, password, remote_port, local_host, local_port):
        self.ssh_target = ssh_target
        self.password = password or None
        self.remote_port = int(remote_port)
        self.local_host = local_host
        self.local_port = int(local_port)
        self.client = None
        self.transport = None
        self.stop_event = threading.Event()

    def start(self):
        if paramiko is None:
            raise RuntimeError("El agente no incluye Paramiko. Recompila el EXE.")
        if self.transport and self.transport.is_active():
            return
        user, host, port = parse_ssh_target(self.ssh_target)
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        print(f"SSH: conectando a {user}@{host}:{port}")
        self.client.connect(hostname=host, port=port, username=user, password=self.password, look_for_keys=True, allow_agent=True, timeout=14)
        self.transport = self.client.get_transport()
        self.transport.request_port_forward("127.0.0.1", self.remote_port)
        threading.Thread(target=self.accept_loop, daemon=True).start()
        print(f"túnel: server 127.0.0.1:{self.remote_port} -> {self.local_host}:{self.local_port}")

    def accept_loop(self):
        while not self.stop_event.is_set():
            try:
                chan = self.transport.accept(1)
                if chan is None:
                    continue
                sock = socket.create_connection((self.local_host, self.local_port), timeout=8)
                threading.Thread(target=self.pipe, args=(chan, sock), daemon=True).start()
                threading.Thread(target=self.pipe, args=(sock, chan), daemon=True).start()
            except Exception as exc:
                if not self.stop_event.is_set():
                    print(f"error túnel {self.remote_port}: {exc}")
                    time.sleep(1)

    def pipe(self, src, dst):
        try:
            while not self.stop_event.is_set():
                data = src.recv(32768)
                if not data:
                    break
                dst.sendall(data)
        except Exception:
            pass
        for item in (src, dst):
            try:
                item.close()
            except Exception:
                pass

    def stop(self):
        self.stop_event.set()
        try:
            if self.transport:
                self.transport.cancel_port_forward("127.0.0.1", self.remote_port)
        except Exception:
            pass
        try:
            if self.client:
                self.client.close()
        except Exception:
            pass


def register(server, token, client_id, name, network, ssh_target, devices):
    payload = {
        "id": client_id,
        "name": name,
        "host": platform.node(),
        "user": getpass.getuser(),
        "platform": platform.platform(),
        "local_ip": local_ips()[0] if local_ips() else "",
        "network": network,
        "ssh_target": ssh_target,
        "devices": devices,
        "message": f"{len(devices)} dispositivo(s)",
    }
    if token:
        payload["token"] = token
    status, _ = post_json(server, "/api/client-agent/register", payload, token=token)
    return status


def main():
    clear()
    print("ADB Agent")
    print("=" * 48)
    print("Deja esta ventana abierta. Luego vuelve a la web.\n")

    cfg = load_json(CONFIG_FILE, {})
    name = ask("Nombre del cliente", cfg.get("name") or platform.node() or "PC")
    server = ask("Servidor ADB web", cfg.get("server") or "http://server:20009")
    ssh_target = ask("SSH del servidor", cfg.get("ssh") or "kucait@server")
    ssh_password = ask("Contraseña SSH opcional", cfg.get("password") or "")
    network = choose_network()
    adb_port = int(ask("Puerto ADB del Android", str(cfg.get("adb_port") or 5555)))
    remote_start = int(ask("Puerto túnel inicial", str(cfg.get("remote_start") or 15555)))
    token = cfg.get("token") or ""

    save_json(CONFIG_FILE, {
        "name": name,
        "server": server,
        "ssh": ssh_target,
        "password": ssh_password,
        "network": network,
        "adb_port": adb_port,
        "remote_start": remote_start,
        "token": token,
    })

    client_id = load_client_id()
    tunnels = []
    print("\nIniciando agente...")
    print("Pulsa Ctrl+C para cerrar.\n")

    try:
        while True:
            devices = scan_network(network, adb_port=adb_port)

            if not devices:
                print("\nNo he encontrado Androids con ADB abierto.")
                manual = ask("IP manual del Android o Enter para reintentar", "")
                if manual:
                    devices = [{"ip": manual, "port": adb_port, "serial": f"{manual}:{adb_port}", "adb_open": True, "status": "manual"}]

            for old in tunnels:
                old.stop()
            tunnels = []

            for index, device in enumerate(devices):
                remote_port = remote_start + index
                try:
                    tunnel = ReverseTunnel(ssh_target, ssh_password, remote_port, device["ip"], device["port"])
                    tunnel.start()
                    tunnels.append(tunnel)
                    device["tunnel_port"] = remote_port
                    device["tunnel_endpoint"] = f"127.0.0.1:{remote_port}"
                    device["status"] = "tunnel"
                except Exception as exc:
                    device["status"] = f"tunnel-error: {exc}"
                    print(f"ERROR túnel {device['ip']}:{device['port']}: {exc}")

            try:
                status = register(server, token, client_id, name, network, ssh_target, devices)
                print(f"\nServidor OK · {len(devices)} Android(s) · HTTP {status}")
                print("Vuelve a la web: ya debería aparecer conectado en la pestaña Agente/Red.")
            except Exception as exc:
                print(f"ERROR registrando en servidor: {exc}")

            print("\nEsperando 25s...\n")
            time.sleep(25)

    except KeyboardInterrupt:
        print("\nCerrando agente...")
    finally:
        for tunnel in tunnels:
            tunnel.stop()


if __name__ == "__main__":
    main()
