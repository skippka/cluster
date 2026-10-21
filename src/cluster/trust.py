from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import secrets
import sqlite3
import ssl
import threading
import time
import urllib.error
from urllib.parse import parse_qs, urlencode, urlsplit
import uuid


def private_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')


def controller_identity(data_dir):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID

    directory = Path(data_dir) / 'identity'
    cert_path, key_path = directory / 'controller.pem', directory / 'controller.key'
    if cert_path.exists() != key_path.exists():
        raise ValueError('Incomplete controller identity. Restore both certificate and key from backup.')
    if not cert_path.exists():
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Cluster Controller')])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .sign(key, hashes.SHA256()))
        private_write(key_path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())
        private_write(cert_path, cert.public_bytes(serialization.Encoding.PEM).decode())
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    now = datetime.now(timezone.utc)
    if not cert.not_valid_before_utc <= now < cert.not_valid_after_utc:
        raise ValueError('Controller certificate is outside its validity period. Renew it and re-pair workers.')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_path, key_path)
    return context, cert.fingerprint(hashes.SHA256()).hex()


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class TrustStore:
    def __init__(self, data_dir, clock=time.time):
        self.clock = clock
        self.lock = threading.RLock()
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(Path(data_dir) / 'trust.db', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS invites (digest TEXT PRIMARY KEY, expires REAL, used INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, name TEXT, digest TEXT UNIQUE,
                created REAL, revoked INTEGER DEFAULT 0);
        ''')

    def invite(self, seconds=300):
        secret = secrets.token_urlsafe(24)
        with self.lock, self.db:
            self.db.execute('DELETE FROM invites WHERE expires < ? OR used=1', (self.clock(),))
            self.db.execute('INSERT INTO invites(digest,expires) VALUES(?,?)', (digest(secret), self.clock() + seconds))
        return secret

    def enroll(self, secret, name):
        if not isinstance(secret, str) or not isinstance(name, str) or not 1 <= len(name) <= 48 or not name.isprintable():
            raise ValueError('Use a printable node name of 1 to 48 characters')
        token, node_id = secrets.token_urlsafe(32), uuid.uuid4().hex
        with self.lock, self.db:
            consumed = self.db.execute('UPDATE invites SET used=1 WHERE digest=? AND used=0 AND expires>?', (digest(secret), self.clock())).rowcount
            if consumed != 1:
                raise ValueError('Invitation is invalid, expired, or already used')
            self.db.execute('INSERT INTO devices(id,name,digest,created) VALUES(?,?,?,?)', (node_id, name, digest(token), self.clock()))
        return {'node_id': node_id, 'token': token, 'name': name}

    def authenticate(self, token):
        if not token:
            return None
        with self.lock:
            row = self.db.execute('SELECT id FROM devices WHERE digest=? AND revoked=0', (digest(token),)).fetchone()
            return row['id'] if row else None

    def devices(self):
        with self.lock:
            return [dict(row) for row in self.db.execute('SELECT id,name,created,revoked FROM devices ORDER BY created')]

    def revoke(self, node_id):
        with self.lock, self.db:
            return self.db.execute('UPDATE devices SET revoked=1 WHERE id=? AND revoked=0', (node_id,)).rowcount == 1

    def close(self):
        self.db.close()


def invitation(address, port, fingerprint, secret):
    host = f'[{address}]' if ':' in address else address
    return f'cluster://{host}:{port}/join?' + urlencode({'fp': fingerprint, 'code': secret})


def parse_invitation(value):
    parsed = urlsplit(value.strip())
    params = parse_qs(parsed.query, strict_parsing=True)
    if parsed.scheme != 'cluster' or parsed.path != '/join' or not parsed.hostname or not parsed.port or parsed.username or parsed.password:
        raise ValueError('Paste the complete cluster:// invitation from your controller')
    fingerprint, code = params.get('fp', [''])[0], params.get('code', [''])[0]
    if len(fingerprint) != 64 or any(c not in '0123456789abcdef' for c in fingerprint) or len(code) < 24:
        raise ValueError('Incomplete invitation')
    return f'https://{parsed.netloc}', fingerprint, code


class NodeSession:
    def __init__(self, server, fingerprint, node_id='', token='', name=''):
        parsed = urlsplit(server)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
            raise ValueError('A paired connection requires an HTTPS controller address')
        if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(char not in '0123456789abcdef' for char in fingerprint):
            raise ValueError('Invalid controller fingerprint')
        self.server, self.fingerprint = server.rstrip('/'), fingerprint
        self.node_id, self.token, self.name = node_id, token, name

    def request(self, method, path, data=None):
        parsed = urlsplit(self.server)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=10, context=context)
        try:
            connection.connect()
            actual = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
            if not hmac.compare_digest(actual, self.fingerprint):
                raise ssl.SSLError('Controller identity changed. Connection stopped before sending credentials.')
            from cryptography import x509
            cert = x509.load_der_x509_certificate(connection.sock.getpeercert(binary_form=True))
            now = datetime.now(timezone.utc)
            if not cert.not_valid_before_utc <= now < cert.not_valid_after_utc:
                raise ssl.SSLError('Pinned controller certificate is outside its validity period.')
            body = json.dumps(data).encode('utf-8') if data is not None else None
            headers = {'Content-Type': 'application/json'}
            if self.token:
                headers['Authorization'] = 'Bearer ' + self.token
            connection.request(method, '/api/v1' + path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError('Controller response exceeds maximum size')
            if response.status >= 400:
                error = urllib.error.HTTPError(self.server + path, response.status, 'Controller rejected request', response.headers, None)
                error.close()
                raise error
            return json.loads(raw)
        finally:
            connection.close()

    @classmethod
    def pair(cls, invite, name, data_dir):
        server, fingerprint, code = parse_invitation(invite)
        session = cls(server, fingerprint)
        issued = session.request('POST', '/pair', {'invite': code, 'name': name})
        session.node_id, session.token, session.name = issued['node_id'], issued['token'], issued['name']
        private_write(Path(data_dir) / 'connection.json', json.dumps(vars(session), indent=2))
        return session

    @classmethod
    def load(cls, data_dir):
        return cls(**json.loads((Path(data_dir) / 'connection.json').read_text(encoding='utf-8')))
