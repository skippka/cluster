"""Безопасность кластера: TLS-сертификаты, отпечатки (pinning), инвайты и токены."""

import base64
import datetime
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


INVITE_TTL_SECONDS = 300
DISCOVERY_PORT = 45800
SHORT_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def scan_lan_controllers(timeout: float = 1.6) -> list[dict]:
    """Listens for UDP discovery beacons from Cluster controllers on the LAN."""
    found: dict[tuple[str, int], dict] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", DISCOVERY_PORT))
        sock.settimeout(0.25)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(1024)
                info = json.loads(data.decode("utf-8"))
                if info.get("app") == "cluster" and info.get("port") and info.get("pin"):
                    ip = info.get("ip") or addr[0]
                    port = int(info["port"])
                    found[(ip, port)] = {"ip": ip, "port": port, "pin": info["pin"], "name": info.get("name", "Controller")}
            except (OSError, ValueError, TypeError):
                continue
    except OSError:
        pass
    finally:
        sock.close()
    return list(found.values())


def _fingerprint_der(der_bytes: bytes) -> str:
    return hashlib.sha256(der_bytes).hexdigest()


def ensure_controller_tls(data_dir: Path) -> tuple[ssl.SSLContext, str]:
    """Создаёт или загружает самоподписанный TLS-сертификат контроллера."""
    data_dir.mkdir(parents=True, exist_ok=True)
    cert_path = data_dir / "controller.crt"
    key_path = data_dir / "controller.key"

    if not cert_path.exists() or not key_path.exists():
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.datetime.now(datetime.timezone.utc)
        subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "cluster-controller")])

        san_items: list[x509.GeneralName] = [
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        ]
        try:
            host_ip = socket.gethostbyname(socket.gethostname())
            ip_obj = ipaddress.ip_address(host_ip)
            if str(ip_obj) != "127.0.0.1":
                san_items.append(x509.IPAddress(ip_obj))
        except OSError:
            pass

        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=365 * 3))
            .add_extension(x509.SubjectAlternativeName(san_items), critical=False)
            .sign(key, hashes.SHA256())
        )
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )

    cert_obj = x509.load_pem_x509_certificate(cert_path.read_bytes())
    fingerprint = _fingerprint_der(cert_obj.public_bytes(serialization.Encoding.DER))

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return ctx, fingerprint


def make_invite_link(host: str, port: int, fingerprint: str, code: str) -> str:
    """Формирует приглашение вида cluster://IP:PORT?pin=...&code=..."""
    return f"cluster://{host}:{port}?pin={fingerprint}&code={code}"


def parse_invite_link(link: str) -> tuple[str, str, str]:
    """Разбирает ссылку приглашения -> (https_url, fingerprint, code)."""
    text = link.strip()
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme != "cluster" or not parsed.hostname or not parsed.port:
        raise ValueError("Неверный формат ссылки. Ожидается cluster://IP:PORT?pin=...&code=...")
    params = urllib.parse.parse_qs(parsed.query)
    pin = params.get("pin", [""])[0]
    code = params.get("code", [""])[0]
    if len(pin) != 64 or not code:
        raise ValueError("В приглашении отсутствует отпечаток (pin) или код.")
    return f"https://{parsed.hostname}:{parsed.port}", pin.lower(), code


class TrustStore:
    """Хранит одноразовые приглашения и токены доверенных воркеров."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "trust.json"
        self.lock = threading.RLock()
        self.invites: dict[str, float] = {}
        self.devices_map: dict[str, dict] = {}
        self.pending_requests: dict[str, dict] = {}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.invites = raw.get("invites", {})
                self.devices_map = raw.get("devices", {})
            except (OSError, ValueError):
                pass

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"invites": self.invites, "devices": self.devices_map}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def create_invite(self, ttl: int = INVITE_TTL_SECONDS) -> str:
        with self.lock:
            now = time.time()
            self.invites = {k: v for k, v in self.invites.items() if v > now}
            code = "".join(secrets.choice(SHORT_ALPHABET) for _ in range(6))
            self.invites[code] = now + ttl
            self._save()
            return code

    def redeem_invite(self, code: str, device_name: str) -> dict:
        with self.lock:
            now = time.time()
            clean_code = code.strip().upper()
            exp = self.invites.pop(clean_code, None)
            if exp is None:
                exp = self.invites.pop(code.strip(), None)
            if exp is None or exp < now:
                self._save()
                raise ValueError("Invite expired, invalid, or already used")
            return self._issue_device(device_name)

    def _issue_device(self, device_name: str) -> dict:
        node_id = "node-" + secrets.token_hex(4)
        token = secrets.token_urlsafe(24)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        self.devices_map[node_id] = {
            "id": node_id,
            "name": device_name[:48],
            "token_hash": token_hash,
            "revoked": False,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._save()
        return {"node_id": node_id, "token": token, "name": device_name[:48]}

    def create_access_request(self, name: str, ip: str) -> str:
        with self.lock:
            req_id = secrets.token_hex(4).upper()
            self.pending_requests[req_id] = {
                "id": req_id,
                "name": name[:48],
                "ip": ip,
                "status": "pending",
                "issued": None,
                "created_at": time.strftime("%H:%M:%S"),
            }
            return req_id

    def approve_request(self, req_id: str) -> dict | None:
        with self.lock:
            req = self.pending_requests.get(req_id)
            if not req or req["status"] != "pending":
                return None
            issued = self._issue_device(req["name"])
            req["status"] = "approved"
            req["issued"] = issued
            return issued

    def decline_request(self, req_id: str) -> bool:
        with self.lock:
            req = self.pending_requests.get(req_id)
            if not req or req["status"] != "pending":
                return False
            req["status"] = "declined"
            return True

    def get_request_status(self, req_id: str) -> dict:
        with self.lock:
            req = self.pending_requests.get(req_id)
            if not req:
                return {"status": "unknown"}
            out = {"status": req["status"]}
            if req["status"] == "approved" and req["issued"]:
                out["issued"] = req["issued"]
            return out

    def list_pending_requests(self) -> list[dict]:
        with self.lock:
            return [
                {"id": r["id"], "name": r["name"], "ip": r["ip"], "created_at": r["created_at"]}
                for r in self.pending_requests.values()
                if r["status"] == "pending"
            ]

    def authenticate(self, token: str) -> str | None:
        if not token:
            return None
        incoming_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            for node_id, info in self.devices_map.items():
                if not info.get("revoked") and hmac.compare_digest(info["token_hash"], incoming_hash):
                    return node_id
        return None

    def revoke(self, node_id: str) -> bool:
        with self.lock:
            info = self.devices_map.get(node_id)
            if not info or info.get("revoked"):
                return False
            info["revoked"] = True
            self._save()
            return True

    def list_devices(self) -> list[dict]:
        with self.lock:
            return [
                {"id": d["id"], "name": d["name"], "revoked": bool(d.get("revoked"))}
                for d in self.devices_map.values()
            ]


class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """Проверяет SHA-256 отпечаток сертификата контроллера при каждом запросе."""

    def __init__(self, expected_pin: str):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        super().__init__(context=ctx)
        self.expected_pin = expected_pin.lower()

    def https_open(self, req):
        return self.do_open(self._make_conn, req)

    def _make_conn(self, host, **kwargs):
        import http.client
        expected = self.expected_pin
        ctx = self._context

        class PinnedConn(http.client.HTTPSConnection):
            def connect(self):
                super().connect()
                der = self.sock.getpeercert(binary_form=True)
                actual = hashlib.sha256(der).hexdigest().lower()
                if not hmac.compare_digest(actual, expected):
                    self.close()
                    raise ssl.SSLError(f"Отпечаток сертификата не совпадает! Ожидался {expected[:12]}...")

        return PinnedConn(host, context=ctx, **kwargs)


class WorkerSession:
    """Клиентская сессия воркера для общения с контроллером по pinned HTTPS."""

    def __init__(self, server: str, pin: str, node_id: str, token: str, name: str):
        self.server = server.rstrip("/")
        self.pin = pin
        self.node_id = node_id
        self.token = token
        self.name = name
        self._opener = urllib.request.build_opener(PinnedHTTPSHandler(pin))

    def request_json(self, method: str, path: str, body: dict | None = None, timeout: float = 10.0) -> dict:
        url = f"{self.server}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            text = err.read().decode("utf-8", errors="replace")
            try:
                msg = json.loads(text).get("error", text)
            except ValueError:
                msg = text
            raise RuntimeError(f"HTTP {err.code}: {msg}") from err

    def save(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "connection.json").write_text(
            json.dumps(
                {
                    "server": self.server,
                    "pin": self.pin,
                    "node_id": self.node_id,
                    "token": self.token,
                    "name": self.name,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, data_dir: Path) -> "WorkerSession":
        raw = json.loads((Path(data_dir) / "connection.json").read_text(encoding="utf-8"))
        return cls(raw["server"], raw["pin"], raw["node_id"], raw["token"], raw["name"])

    @classmethod
    def pair(cls, invite_link: str, name: str, data_dir: Path) -> "WorkerSession":
        server, pin, code = parse_invite_link(invite_link)
        temp = cls(server, pin, "", "", name)
        issued = temp.request_json("POST", "/api/v1/pair", {"invite": code, "name": name})
        session = cls(server, pin, issued["node_id"], issued["token"], issued["name"])
        session.save(data_dir)
        return session

    @classmethod
    def pair_with_code(cls, ip: str, port: int, pin: str, code: str, name: str, data_dir: Path) -> "WorkerSession":
        server = f"https://{ip}:{port}"
        temp = cls(server, pin, "", "", name)
        issued = temp.request_json("POST", "/api/v1/pair", {"invite": code.strip(), "name": name})
        session = cls(server, pin, issued["node_id"], issued["token"], issued["name"])
        session.save(data_dir)
        return session

    @classmethod
    def request_access_and_wait(cls, ip: str, port: int, pin: str, name: str, data_dir: Path, timeout: float = 60.0) -> "WorkerSession":
        server = f"https://{ip}:{port}"
        temp = cls(server, pin, "", "", name)
        res = temp.request_json("POST", "/api/v1/pair/request", {"name": name})
        req_id = res["request_id"]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = temp.request_json("GET", f"/api/v1/pair/status/{req_id}")
            if st["status"] == "approved":
                issued = st["issued"]
                session = cls(server, pin, issued["node_id"], issued["token"], issued["name"])
                session.save(data_dir)
                return session
            if st["status"] == "declined":
                raise RuntimeError("Access request was declined by the controller")
            time.sleep(0.8)
        raise TimeoutError("Controller did not respond in time")
