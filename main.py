# drakvex_bot.py
# DRAKVEX Telegram bot — port of DRAKVEX_ML.py + info.py
# python-telegram-bot v20+, zstandard, pycryptodome

import os
import re
import io
import sys
import time
import zlib
import uuid
import html
import socket
import struct
import base64
import random
import sqlite3
import asyncio
import ipaddress
import datetime
import threading
from enum import Enum
from typing import Optional, Callable
from concurrent.futures import ThreadPoolExecutor

import zstandard as zstd
from Crypto.Cipher import AES

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)


# ═══════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════

BOT_TOKEN   = os.environ.get("DRAKVEX_BOT_TOKEN", "8702549007:AAHxNCGdtyEdGihy7FfoXhNTtx3mh1UN4W0")
DB_PATH     = os.environ.get("DRAKVEX_DB", "drakvex.db")
PROXY_FILE  = os.environ.get("DRAKVEX_PROXIES", "proxies.txt")

DEFAULT_ADMINS = "8621676055"
_admin_env  = os.environ.get("DRAKVEX_ADMINS", DEFAULT_ADMINS).strip()
BOOT_ADMINS = {int(x) for x in _admin_env.split(",") if x.strip().isdigit()}
if not BOOT_ADMINS:
    BOOT_ADMINS = {8621676055}

THREADS            = int(os.environ.get("DRAKVEX_THREADS", "100"))
MAX_RETRIES        = int(os.environ.get("DRAKVEX_RETRIES", "8"))
PROXY_ROTATE_EVERY = int(os.environ.get("DRAKVEX_PROXY_ROTATE_EVERY", "3"))
BULK_CAP           = int(os.environ.get("DRAKVEX_BULK_CAP", "10000"))
LIVE_MS            = int(os.environ.get("DRAKVEX_LIVE_MS", "900"))
PROXY_COOLDOWN     = int(os.environ.get("DRAKVEX_PROXY_COOLDOWN", "90"))
CONN_TIMEOUT       = float(os.environ.get("DRAKVEX_CONN_TIMEOUT", "10"))

EXECUTOR = ThreadPoolExecutor(max_workers=THREADS, thread_name_prefix="drkvx")

if not BOT_TOKEN:
    print("[!] set DRAKVEX_BOT_TOKEN before running.", file=sys.stderr)
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════════
# THREAD-LOCAL — tracks last proxy used by the current thread so the
# retry wrapper can rotate it after N failed attempts.
# ═══════════════════════════════════════════════════════════════════

_tls = threading.local()

def _tls_set_proxy(p):
    _tls.proxy = p

def _tls_get_proxy():
    return getattr(_tls, "proxy", None)


# ═══════════════════════════════════════════════════════════════════
# PROXY POOL — SOCKS5 / SOCKS4 / HTTP CONNECT
# ═══════════════════════════════════════════════════════════════════

def _parse_proxy_line(line: str):
    s = (line or "").strip()
    if not s or s.startswith("#"):
        return None
    scheme = "socks5"
    if "://" in s:
        scheme, s = s.split("://", 1)
        scheme = scheme.lower().strip()
    user = pwd = None
    if "@" in s:
        auth, s = s.rsplit("@", 1)
        if ":" in auth:
            user, pwd = auth.split(":", 1)
        else:
            user = auth or None
    if ":" not in s:
        return None
    host, port_s = s.rsplit(":", 1)
    try: port = int(port_s)
    except (TypeError, ValueError): return None
    if not host or not (0 < port < 65536): return None
    if scheme in ("socks5h", "socks5", "socks"): scheme = "socks5"
    elif scheme in ("socks4", "socks4a"): scheme = "socks4"
    elif scheme in ("http", "https", "connect"): scheme = "http"
    else: scheme = "socks5"
    return {"scheme": scheme, "host": host, "port": port,
            "user": user, "pwd": pwd, "raw": line.strip()}


class ProxyPool:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._proxies: list[dict] = []
        self._idx = 0
        self._bad_until: dict[str, float] = {}
        self._stats: dict[str, dict] = {}
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read()
        except FileNotFoundError:
            with self._lock:
                self._proxies = []
            return
        pool = []
        for line in raw.splitlines():
            p = _parse_proxy_line(line)
            if p: pool.append(p)
        with self._lock:
            self._proxies = pool
            self._bad_until.clear()
            self._stats = {p["raw"]: {"ok": 0, "fail": 0} for p in pool}

    @property
    def enabled(self) -> bool:
        with self._lock: return bool(self._proxies)

    def size(self) -> int:
        with self._lock: return len(self._proxies)

    def acquire(self) -> Optional[dict]:
        with self._lock:
            if not self._proxies: return None
            now = time.time()
            n = len(self._proxies)
            for _ in range(n):
                p = self._proxies[self._idx % n]
                self._idx = (self._idx + 1) % n
                if self._bad_until.get(p["raw"], 0) <= now:
                    return p
            return min(self._proxies, key=lambda x: self._bad_until.get(x["raw"], 0))

    def mark_ok(self, p: Optional[dict]):
        if not p: return
        with self._lock:
            self._stats.setdefault(p["raw"], {"ok": 0, "fail": 0})["ok"] += 1

    def mark_bad(self, p: Optional[dict]):
        if not p: return
        with self._lock:
            self._stats.setdefault(p["raw"], {"ok": 0, "fail": 0})["fail"] += 1
            self._bad_until[p["raw"]] = time.time() + PROXY_COOLDOWN

    def snapshot(self):
        with self._lock:
            now = time.time()
            return [{"raw": p["raw"], "scheme": p["scheme"],
                     "cooldown": max(0, int(self._bad_until.get(p["raw"], 0) - now)),
                     "ok": self._stats.get(p["raw"], {}).get("ok", 0),
                     "fail": self._stats.get(p["raw"], {}).get("fail", 0)}
                    for p in self._proxies]


PROXIES = ProxyPool(PROXY_FILE)


def _socks5_handshake(s, host, port, user, pwd):
    s.sendall(b"\x05\x02\x00\x02" if user else b"\x05\x01\x00")
    resp = s.recv(2)
    if len(resp) < 2 or resp[0] != 5: raise OSError("socks5 negotiation failed")
    if resp[1] == 0x02:
        u = (user or "").encode()[:255]; pw = (pwd or "").encode()[:255]
        s.sendall(bytes([0x01, len(u)]) + u + bytes([len(pw)]) + pw)
        ar = s.recv(2)
        if len(ar) < 2 or ar[1] != 0x00: raise OSError("socks5 auth failed")
    elif resp[1] != 0x00:
        raise OSError("socks5 no acceptable auth")
    try:
        packed = ipaddress.ip_address(host).packed
        req = (b"\x05\x01\x00\x01" + packed) if len(packed) == 4 else (b"\x05\x01\x00\x04" + packed)
    except ValueError:
        hb = host.encode()
        req = b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb
    req += port.to_bytes(2, "big")
    s.sendall(req)
    head = s.recv(4)
    if len(head) < 4 or head[1] != 0x00:
        raise OSError(f"socks5 connect failed code={head[1] if len(head)>1 else '?'}")
    atyp = head[3]
    if atyp == 0x01: s.recv(4)
    elif atyp == 0x04: s.recv(16)
    elif atyp == 0x03:
        ln = s.recv(1)
        if ln: s.recv(ln[0])
    s.recv(2)


def _socks4_handshake(s, host, port, user):
    try:
        packed = ipaddress.ip_address(host).packed
        if len(packed) != 4: raise ValueError
        payload = b"\x04\x01" + port.to_bytes(2, "big") + packed + (user or "").encode() + b"\x00"
    except ValueError:
        payload = (b"\x04\x01" + port.to_bytes(2, "big") + b"\x00\x00\x00\x01"
                   + (user or "").encode() + b"\x00" + host.encode() + b"\x00")
    s.sendall(payload)
    resp = s.recv(8)
    if len(resp) < 8 or resp[1] != 0x5A: raise OSError("socks4 connect failed")


def _http_connect_handshake(s, host, port, user, pwd):
    lines = [f"CONNECT {host}:{port} HTTP/1.1", f"Host: {host}:{port}",
             "Proxy-Connection: keep-alive"]
    if user and pwd:
        tok = base64.b64encode(f"{user}:{pwd}".encode()).decode()
        lines.append(f"Proxy-Authorization: Basic {tok}")
    s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk: raise OSError("http connect closed")
        buf += chunk
        if len(buf) > 65536: raise OSError("http header too big")
    status = buf.split(b"\r\n", 1)[0]
    parts = status.split(b" ", 2)
    if len(parts) < 2 or parts[1] != b"200":
        raise OSError(f"http connect rejected: {status!r}")


def _tcp_open(host: str, port: int, timeout: float, proxy: Optional[dict]) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    if proxy is None:
        s.connect((host, port))
        return s
    try:
        s.connect((proxy["host"], proxy["port"]))
        sch = proxy["scheme"]
        if sch == "socks5":   _socks5_handshake(s, host, port, proxy.get("user"), proxy.get("pwd"))
        elif sch == "socks4": _socks4_handshake(s, host, port, proxy.get("user"))
        else:                 _http_connect_handshake(s, host, port, proxy.get("user"), proxy.get("pwd"))
        PROXIES.mark_ok(proxy)
        return s
    except Exception:
        try: s.close()
        except Exception: pass
        PROXIES.mark_bad(proxy)
        raise


# ═══════════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════════

class DB:
    _lock = threading.RLock()
    def __init__(self, path): self.path = path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    def _init(self):
        with self._lock, self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                tg_id INTEGER PRIMARY KEY, username TEXT, first_seen INTEGER,
                key TEXT, is_admin INTEGER DEFAULT 0, checks_used INTEGER DEFAULT 0,
                banned INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS keys (
                key TEXT PRIMARY KEY, tier TEXT, max_uses INTEGER,
                uses INTEGER DEFAULT 0, expires_at INTEGER, owner_id INTEGER,
                created_by INTEGER, created_at INTEGER, revoked INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS usage_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER,
                kind TEXT, device_id TEXT, ts INTEGER);
            CREATE INDEX IF NOT EXISTS idx_usage_tg ON usage_log(tg_id);
            """)
            for aid in BOOT_ADMINS:
                c.execute("INSERT OR IGNORE INTO users(tg_id, username, first_seen, is_admin) "
                          "VALUES(?,?,?,1)", (aid, "boot_admin", int(time.time())))
                c.execute("UPDATE users SET is_admin=1, banned=0 WHERE tg_id=?", (aid,))

    def ensure_user(self, tg_id, username):
        with self._lock, self._conn() as c:
            c.execute("INSERT OR IGNORE INTO users(tg_id, username, first_seen) VALUES(?,?,?)",
                      (tg_id, username or "", int(time.time())))
            c.execute("UPDATE users SET username=? WHERE tg_id=?", (username or "", tg_id))
            if tg_id in BOOT_ADMINS:
                c.execute("UPDATE users SET is_admin=1, banned=0 WHERE tg_id=?", (tg_id,))

    def get_user(self, tg_id):
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
            return dict(row) if row else None

    def is_admin(self, tg_id):
        if tg_id in BOOT_ADMINS: return True
        u = self.get_user(tg_id); return bool(u and u["is_admin"])

    def list_users(self, limit=30):
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM users ORDER BY first_seen DESC LIMIT ?",
                             (limit,)).fetchall()
            return [dict(r) for r in rows]

    def auth_status(self, tg_id):
        u = self.get_user(tg_id)
        if not u:
            if tg_id in BOOT_ADMINS:
                return True, "admin", {"tg_id": tg_id, "is_admin": 1, "banned": 0}
            return False, "no user — press /start", None
        if tg_id in BOOT_ADMINS: return True, "admin", u
        if u["banned"]: return False, "you are banned from this bot.", u
        if u["is_admin"]: return True, "admin", u
        if not u["key"]: return False, "no key. redeem one to use the bot.", u
        k = self.get_key(u["key"])
        if not k or k["revoked"]: return False, "your key is revoked or missing.", u
        if k["expires_at"] and k["expires_at"] < int(time.time()):
            return False, "your key expired.", u
        if k["max_uses"] > 0 and k["uses"] >= k["max_uses"]:
            return False, "your key is out of uses.", u
        return True, "ok", u

    def consume_use(self, tg_id):
        if tg_id in BOOT_ADMINS or self.is_admin(tg_id): return
        with self._lock, self._conn() as c:
            u = c.execute("SELECT key FROM users WHERE tg_id=?", (tg_id,)).fetchone()
            c.execute("UPDATE users SET checks_used=checks_used+1 WHERE tg_id=?", (tg_id,))
            if u and u["key"]:
                c.execute("UPDATE keys SET uses=uses+1 WHERE key=?", (u["key"],))

    def log_usage(self, tg_id, kind, device_id):
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO usage_log(tg_id, kind, device_id, ts) VALUES(?,?,?,?)",
                      (tg_id, kind, device_id or "", int(time.time())))

    def generate_key(self, tier, max_uses, days, created_by):
        raw = uuid.uuid4().hex.upper()
        key = "-".join(["DRKV"] + [raw[i:i+4] for i in range(0, 20, 4)])
        exp = 0 if days <= 0 else int(time.time()) + days * 86400
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO keys(key, tier, max_uses, expires_at, created_by, created_at) "
                      "VALUES(?,?,?,?,?,?)", (key, tier, max_uses, exp, created_by, int(time.time())))
        return key

    def get_key(self, key):
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM keys WHERE key=?", (key,)).fetchone()
            return dict(row) if row else None

    def redeem_key(self, tg_id, key):
        key = key.strip().upper()
        with self._lock, self._conn() as c:
            k = c.execute("SELECT * FROM keys WHERE key=?", (key,)).fetchone()
            if not k: return False, "key not found."
            k = dict(k)
            if k["revoked"]: return False, "key revoked."
            if k["owner_id"] and k["owner_id"] != tg_id: return False, "key already owned."
            if k["expires_at"] and k["expires_at"] < int(time.time()): return False, "key expired."
            if k["max_uses"] > 0 and k["uses"] >= k["max_uses"]: return False, "key out of uses."
            c.execute("UPDATE keys SET owner_id=? WHERE key=?", (tg_id, key))
            c.execute("UPDATE users SET key=? WHERE tg_id=?", (key, tg_id))
            return True, f"redeemed ({k['tier']})."

    def revoke_key(self, key):
        with self._lock, self._conn() as c:
            cur = c.execute("UPDATE keys SET revoked=1 WHERE key=?", (key.strip().upper(),))
            return cur.rowcount > 0

    def list_keys(self, limit=40):
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM keys ORDER BY created_at DESC LIMIT ?",
                             (limit,)).fetchall()
            return [dict(r) for r in rows]

    def stats(self):
        with self._lock, self._conn() as c:
            users = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
            keys = c.execute("SELECT COUNT(*) AS n FROM keys").fetchone()["n"]
            active = c.execute(
                "SELECT COUNT(*) AS n FROM keys WHERE revoked=0 AND "
                "(expires_at=0 OR expires_at>?) AND (max_uses=0 OR uses<max_uses)",
                (int(time.time()),)).fetchone()["n"]
            checks = c.execute("SELECT COUNT(*) AS n FROM usage_log").fetchone()["n"]
            today0 = int(datetime.datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0).timestamp())
            today = c.execute("SELECT COUNT(*) AS n FROM usage_log WHERE ts>=?",
                              (today0,)).fetchone()["n"]
            return {"users": users, "keys": keys, "active": active,
                    "checks": checks, "today": today}


DBX = DB(DB_PATH)


# ═══════════════════════════════════════════════════════════════════
# SDP
# ═══════════════════════════════════════════════════════════════════

class SdpType(Enum):
    INT_POS = 0; INT_NEG = 1; FLOAT = 2; DOUBLE = 3; STRING = 4
    LIST = 5; DICT = 6; STRUCT_BEGIN = 7; STRUCT_END = 8


class SdpError(Exception): pass


def _sdp_sort_key(kv):
    k = kv[0]
    return (0, k) if isinstance(k, int) else (1, str(k))


class SdpStruct(dict):
    def __init__(self, data=None):
        super().__init__()
        self.data = b''; self.offset = 0
        if isinstance(data, bytes):
            self.data = data; self.offset = 0; self._unpack_from_binary()
        elif data is not None:
            super().update(data); self._pack_to_binary()

    def _pack_to_binary(self):
        self.data = bytes([SdpType.STRUCT_BEGIN.value << 4])
        for tag, value in sorted(self.items(), key=_sdp_sort_key):
            self._pack(tag, value)
        self.data += bytes([SdpType.STRUCT_END.value << 4])

    def _unpack_from_binary(self):
        if not self.data: return
        if self.data[0] >> 4 == SdpType.STRUCT_BEGIN.value: self.offset = 1
        while self.offset < len(self.data):
            tag, value = self._unpack()
            if isinstance(value, SdpType) and value == SdpType.STRUCT_END: break
            self[tag] = value

    def _write_number(self, value):
        result = bytearray()
        while value >= 0x80:
            result.append((value & 0x7F) | 0x80); value >>= 7
        result.append(value & 0x7F); return bytes(result)

    def _read_number(self):
        if self.offset >= len(self.data): raise SdpError('varint eof')
        n = 1
        val = self.data[self.offset] & 0x7F
        while self.data[self.offset + n - 1] >= 0x80:
            if self.offset + n >= len(self.data): raise SdpError('varint truncated')
            val |= (self.data[self.offset + n] & 0x7F) << (7 * n); n += 1
        self.offset += n; return val

    def _pack_header(self, tag, dtype):
        if tag < 15: self.data += bytes([(dtype.value << 4) | tag])
        else:
            self.data += bytes([(dtype.value << 4) | 15]); self.data += self._write_number(tag)

    def _pack(self, tag, value):
        if isinstance(value, bool):
            self._pack_header(tag, SdpType.INT_POS); self.data += self._write_number(1 if value else 0)
        elif isinstance(value, int):
            if value < 0:
                self._pack_header(tag, SdpType.INT_NEG); self.data += self._write_number(-value)
            else:
                self._pack_header(tag, SdpType.INT_POS); self.data += self._write_number(value)
        elif isinstance(value, float):
            self._pack_header(tag, SdpType.DOUBLE)
            packed = struct.pack("<d", value)
            self.data += self._write_number(len(packed)); self.data += packed
        elif isinstance(value, (str, bytes)):
            self._pack_header(tag, SdpType.STRING)
            encoded = value.encode('utf-8') if isinstance(value, str) else value
            self.data += self._write_number(len(encoded)); self.data += encoded
        elif isinstance(value, list):
            self._pack_header(tag, SdpType.LIST); self.data += self._write_number(len(value))
            for item in value: self._pack(0, item)
        elif isinstance(value, dict):
            if isinstance(value, SdpStruct):
                self._pack_header(tag, SdpType.STRUCT_BEGIN)
                for k, v in sorted(value.items(), key=_sdp_sort_key): self._pack(k, v)
                self.data += bytes([SdpType.STRUCT_END.value << 4])
            else:
                self._pack_header(tag, SdpType.DICT); self.data += self._write_number(len(value))
                for k, v in sorted(value.items(), key=_sdp_sort_key):
                    self._pack(0, k); self._pack(0, v)
        else: raise SdpError(f'unsupported {type(value)}')

    def _unpack(self):
        try:
            if self.offset >= len(self.data): return 0, None
            header = self.data[self.offset]
            tag = header & 0xF; dtype = SdpType(header >> 4); self.offset += 1
            if tag == 15: tag = self._read_number()
            if dtype == SdpType.INT_POS: return tag, self._read_number()
            if dtype == SdpType.INT_NEG: return tag, -self._read_number()
            if dtype == SdpType.FLOAT:
                raw = self._read_number().to_bytes(4, 'little')
                return tag, struct.unpack("<f", raw)[0]
            if dtype == SdpType.DOUBLE:
                raw = self._read_number().to_bytes(8, 'little')
                return tag, struct.unpack("<d", raw)[0]
            if dtype == SdpType.STRING:
                length = self._read_number()
                chunk = self.data[self.offset:self.offset + length]
                try: val = chunk.decode('utf-8')
                except UnicodeDecodeError: val = chunk
                self.offset += length; return tag, val
            if dtype == SdpType.LIST:
                length = self._read_number(); val = []
                for _ in range(length):
                    _, item = self._unpack(); val.append(item)
                return tag, val
            if dtype == SdpType.DICT:
                length = self._read_number(); val = {}
                for _ in range(length):
                    _, k = self._unpack(); _, v = self._unpack(); val[k] = v
                return tag, val
            if dtype == SdpType.STRUCT_BEGIN:
                sub = {}
                while True:
                    st, sv = self._unpack()
                    if isinstance(sv, SdpType) and sv == SdpType.STRUCT_END: break
                    sub[st] = sv
                return tag, SdpStruct(sub)
            if dtype == SdpType.STRUCT_END: return tag, SdpType.STRUCT_END
            raise SdpError('bad data type')
        except SdpError: raise
        except Exception: raise SdpError('unpack failed')


AES_KEY = bytes.fromhex('f5a193d50ade553e9835595f5cd75ddd')
AES_IV  = b'\x00' * 16


def _aes_decrypt(data):
    if not data or len(data) % 16: raise SdpError(f'bad cipher len {len(data) if data else 0}')
    return AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV).decrypt(data).rstrip(b'\x00')


DEVICE_ID_RE = re.compile(
    r'and_[0-9A-Za-z]{56}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')

LOGIN_HOST = 'login.ml.youngjoygame.com'
LOGIN_PORT = 30021
CLIENT_VERSION = '2.2.16.1232.1'
CHANNEL = 'and_usa'

BAN_REASONS = {
    '21': 'Plug-in / unfair advantage', '22': 'Account sharing / boosting',
    '23': 'Verbal abuse / toxic behavior', '24': 'Cheating in ranked',
    '25': 'Payment fraud / chargeback', '26': 'Account trading / sale',
}

HERO_ID_MAP = {
    1: "Miya", 2: "Balmond", 3: "Saber", 4: "Alice", 5: "Nana", 6: "Tigreal",
    7: "Alucard", 8: "Karina", 9: "Akai", 10: "Franco", 11: "Bane", 12: "Bruno",
    13: "Clint", 14: "Rafaela", 15: "Eudora", 16: "Zilong", 17: "Fanny",
    18: "Layla", 19: "Minotaur", 20: "Lolita", 21: "Hayabusa", 22: "Freya",
    23: "Gord", 24: "Natalia", 25: "Kagura", 26: "Chou", 27: "Sun", 28: "Alpha",
    29: "Ruby", 30: "Yi Sun-shin", 31: "Moskov", 32: "Johnson", 33: "Cyclops",
    34: "Estes", 35: "Hilda", 36: "Aurora", 37: "Lapu-Lapu", 38: "Vexana",
    39: "Roger", 40: "Karrie", 41: "Gatotkaca", 42: "Harley", 43: "Irithel",
    44: "Grock", 45: "Argus", 46: "Odette", 47: "Lancelot", 48: "Diggie",
    49: "Hylos", 50: "Zhask", 51: "Helcurt", 52: "Pharsa", 53: "Lesley",
    54: "Jawhead", 55: "Angela", 56: "Gusion", 57: "Valir", 58: "Martis",
    59: "Uranus", 60: "Hanabi", 61: "Chang'e", 62: "Kaja", 63: "Selena",
    64: "Aldous", 65: "Claude", 66: "Vale", 67: "Leomord", 68: "Lunox",
    69: "Hanzo", 70: "Belerick", 71: "Kimmy", 72: "Thamuz", 73: "Harith",
    74: "Minsitthar", 75: "Kadita", 76: "Faramis", 77: "Badang", 78: "Khufra",
    79: "Granger", 80: "Guinevere", 81: "Esmeralda", 82: "Terizla", 83: "X.Borg",
    84: "Ling", 85: "Dyrroth", 86: "Lylia", 87: "Baxia", 88: "Masha",
    89: "Wanwan", 90: "Silvanna", 91: "Cecilion", 92: "Carmilla", 93: "Atlas",
    94: "Popol and Kupa", 95: "Yu Zhong", 96: "Luo Yi", 97: "Benedetta",
    98: "Khaleed", 99: "Barats", 100: "Brody", 101: "Yve", 102: "Mathilda",
    103: "Paquito", 104: "Gloo", 105: "Beatrix", 106: "Phoveus", 107: "Natan",
    108: "Aulus", 109: "Aamon", 110: "Valentina", 111: "Edith", 112: "Floryn",
    113: "Yin", 114: "Melissa", 115: "Xavier", 116: "Julian", 117: "Fredrinn",
    118: "Joy", 119: "Novaria", 120: "Arlott", 121: "Ixia", 122: "Nolan",
    123: "Cici", 124: "Chip", 125: "Zhuxin", 126: "Suyou", 127: "Lukas",
    128: "Kalea", 129: "Zetian", 130: "Obsidia",
}

MODE_MAP = {0: "Classic", 1: "Ranked", 2: "Brawl", 3: "AI",
            4: "Custom", 5: "Mayhem", 6: "Arcade"}

COLLECTOR_TIERS = [
    (1000, 4000, "Amateur Collector"), (4000, 10000, "Junior Collector"),
    (10000, 22000, "Seasoned Collector"), (22000, 44000, "Expert Collector"),
    (44000, 84000, "Renowned Collector"), (84000, 160000, "Exalted Collector"),
    (160000, 280000, "Mega Collector"), (280000, float('inf'), "World Collector"),
]


def map_rank(p):
    try: p = int(p)
    except (TypeError, ValueError): return "Unranked"
    if p <= 0: return "Unranked"
    table = [(0,4,"Warrior III"),(5,9,"Warrior II"),(10,14,"Warrior I"),
             (15,19,"Elite IV"),(20,24,"Elite III"),(25,29,"Elite II"),(30,34,"Elite I"),
             (35,39,"Master IV"),(40,44,"Master III"),(45,49,"Master II"),(50,54,"Master I"),
             (55,59,"Grandmaster IV"),(60,64,"Grandmaster III"),(65,69,"Grandmaster II"),(70,74,"Grandmaster I"),
             (75,81,"Epic IV"),(82,88,"Epic III"),(89,95,"Epic II"),(96,107,"Epic I"),
             (108,114,"Legend IV"),(115,121,"Legend III"),(122,128,"Legend II"),(129,135,"Legend I")]
    for lo, hi, name in table:
        if lo <= p <= hi: return name
    if 136 <= p <= 160: return f"Mythic {p - 135}"
    if 161 <= p <= 195: return f"Mythical Honor {p - 135}"
    if 196 <= p <= 235: return f"Mythical Glory {p - 195}"
    if p >= 236: return f"Mythical Immortal {p - 235}"
    return "Unranked"


def map_collector(p):
    try: p = int(p)
    except (TypeError, ValueError): return "No Tier"
    if p < 1000: return "No Tier"
    for lo, hi, name in COLLECTOR_TIERS:
        if lo <= p < hi:
            if name == "World Collector": return name
            span = (hi - lo) / 5
            lvl = max(0, min(4, int((p - lo) // span)))
            return f"{name} {['V','IV','III','II','I'][lvl]}"
    return "No Tier"


def as_int(v, default=0):
    try:
        if isinstance(v, bool): return int(v)
        if v is None: return default
        return int(v)
    except (TypeError, ValueError): return default


def fmt_ts(ts):
    ts = as_int(ts)
    if ts < 100000000: return None
    try:
        utc = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        return (utc + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")
    except Exception: return None


def fmt_ts_full(ts):
    ts = as_int(ts)
    if ts < 100000000: return None
    try:
        utc = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        return (utc + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception: return None


def human_dur(sec):
    sec = float(max(0, sec))
    if sec < 1: return f'{int(sec * 1000)}ms'
    if sec < 60: return f'{sec:.1f}s'
    m, s = divmod(int(sec), 60)
    if m < 60: return f'{m}m{s:02d}s'
    h, rem = divmod(m, 60); return f'{h}h{rem:02d}m'


def human_ago(sec):
    sec = int(max(0, sec))
    if sec < 60: return f"{sec}s"
    d, rem = divmod(sec, 86400); h, rem = divmod(rem, 3600); m, _ = divmod(rem, 60)
    if d: return f"{d}d {h}h {m}m"
    if h: return f"{h}h {m}m"
    return f"{m}m"


def progress_bar(done, total, width=12):
    if total <= 0: return "░" * width
    filled = max(0, min(width, int(width * done / total)))
    return "█" * filled + "░" * (width - filled)


# ═══════════════════════════════════════════════════════════════════
# SKIN BREAKDOWN — matches DRAKVEX_ML.py / info.py exactly
#
# Real payload shape: tag_118[4] = {tier_id: count}
#   tier_id 1 = Common, 2 = Exceptional, 3 = Deluxe,
#   4 = Exquisite, 5 = Grand, 6 = Supreme
# Fall back to tag_118 itself if [4] isn't there.
# ═══════════════════════════════════════════════════════════════════

SKIN_TIER_NAMES = ["Common Skins", "Exceptional Skins", "Deluxe Skins",
                   "Exquisite Skins", "Grand Skins", "Supreme Skins"]
SKIN_TAGS = (118,)

_SKIN_ID_TO_NAME = {
    6: "Supreme Skins",
    5: "Grand Skins",
    4: "Exquisite Skins",
    3: "Deluxe Skins",
    2: "Exceptional Skins",
    1: "Common Skins",
}


def _zero_skin_breakdown():
    return {n: 0 for n in SKIN_TIER_NAMES}


def parse_skin_counts(tag_118):
    """Exact port of info.py parse_skin_counts()."""
    if not tag_118 or not isinstance(tag_118, dict):
        return {}

    skin_data = None
    for k in (4, '4'):
        v = tag_118.get(k)
        if isinstance(v, dict) and v:
            skin_data = v
            break

    if skin_data is None:
        skin_data = tag_118

    if not isinstance(skin_data, dict):
        return {}

    out = {}
    for skin_id, count in skin_data.items():
        try:
            skin_id_int = int(skin_id)
        except (ValueError, TypeError):
            continue
        if skin_id_int in _SKIN_ID_TO_NAME:
            out[_SKIN_ID_TO_NAME[skin_id_int]] = as_int(count)
    return out


def extract_skin_breakdown(ri, p):
    """Returns full {tier_name: count} dict, matching info.py behaviour.

    Tries role_info tag 118 first, falls back to player tag 118. If no
    tag is present, or the parsed dict is empty, returns all zeros.
    Sanity-guards against a parse total wildly exceeding skin_count.
    """
    out = _zero_skin_breakdown()

    tag_118 = None
    if isinstance(ri, dict):
        tag_118 = ri.get(118)
    if not tag_118 and isinstance(p, dict):
        tag_118 = p.get(118)

    if not tag_118:
        return out

    parsed = parse_skin_counts(tag_118)
    if not parsed:
        return out

    total_skins = as_int(p.get(83, 0)) if isinstance(p, dict) else 0
    parsed_total = sum(v for v in parsed.values() if isinstance(v, int) and v >= 0)

    # sanity: parsed total shouldn't wildly exceed the reported skin count
    if total_skins > 0 and parsed_total > total_skins + max(10, total_skins // 4):
        return out

    for k, v in parsed.items():
        if k in out:
            out[k] = v
    return out


# ═══════════════════════════════════════════════════════════════════
# BAN SNIFF
# ═══════════════════════════════════════════════════════════════════

def _walk_ban(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == 'ban_reason':
                code = str(v); out['ban_code'] = code
                out['reason_name'] = BAN_REASONS.get(code, f'Reason Code {code}')
            elif k == 'endtime_day': out['endtime_day'] = v
            elif k == 'endtime_hour': out['endtime_hour'] = v
            elif k == 'endtime_min': out['endtime_min'] = v
            elif k == 'endtime_sec': out['endtime_sec'] = v
            elif isinstance(k, str) and 'ban' in k.lower(): out[str(k)] = v
            if isinstance(v, (dict, list)): _walk_ban(v, out)
    elif isinstance(obj, list):
        for item in obj: _walk_ban(item, out)


def inspect_ban(sdp):
    info = {}
    if sdp: _walk_ban(dict(sdp), info)
    has_end = 'endtime_day' in info and info['endtime_day'] is not None
    has_code = 'ban_code' in info and info['ban_code'] not in (None, '', '0')
    banned = has_end or (has_code and info.get('reason_name'))
    return banned, info


def format_ban_duration(info):
    day = info.get('endtime_day')
    if day is None: return None
    return (f"day {day}, {as_int(info.get('endtime_hour',0)):02d}:"
            f"{as_int(info.get('endtime_min',0)):02d}:"
            f"{as_int(info.get('endtime_sec',0)):02d}")


# ═══════════════════════════════════════════════════════════════════
# GAME CONNECTION
# ═══════════════════════════════════════════════════════════════════

class GameConn:
    def __init__(self, device_id):
        self.device_id = device_id
        self.host = LOGIN_HOST; self.port = LOGIN_PORT
        self.sequence = 1; self.socket = None; self.queue = b''
        self.last_header_size = 0; self._proxy = None; self.proxy_used = None

        parts = device_id.split('_')
        if len(parts) >= 2:
            info = parts[1]
            if len(parts) >= 3 and len(info) < 32: info = info + '_' + parts[2]
            if len(info) >= 32:
                self.imei_md5 = info[:32]
                self.android_id = info[32:48] if len(info) >= 48 else ''
                self.advertising_id = info[48:] if len(info) > 48 else ''
            else: self.imei_md5, self.android_id, self.advertising_id = info, '', ''
        else: self.imei_md5, self.android_id, self.advertising_id = device_id, '', ''

        self.account_id = 0; self.session_key = ''; self.zone_id = 0
        self.game_host = ''; self.game_port = 0; self.creation_ts = 0
        self.ban_seen = False; self.ban_info = {}

    def _sniff(self, res):
        if self.ban_seen or res is None: return
        banned, info = inspect_ban(res)
        if banned:
            self.ban_seen = True; self.ban_info = dict(info)

    def connect(self, host=None, port=None, use_proxy=True):
        if host: self.host = host
        if port: self.port = port
        proxy = PROXIES.acquire() if use_proxy and PROXIES.enabled else None
        _tls_set_proxy(proxy)   # record for retry-rotation
        self._proxy = proxy
        self.proxy_used = proxy["raw"] if proxy else None
        self.socket = _tcp_open(self.host, self.port, CONN_TIMEOUT, proxy)

    def close(self):
        if self.socket:
            try: self.socket.close()
            except Exception: pass
        self.socket = None; self.sequence = 1; self.queue = b''

    def send(self, pid, sdp):
        if self.socket is None: raise SdpError('socket closed')
        packet = SdpStruct({0: pid, 1: self.sequence, 5: sdp.data}).data
        buf = zstd.compress(packet)
        size = len(buf) + 4
        if size > 0xFFFFFF: raise SdpError('packet too large')
        flags = size | (16 << 24)
        self.socket.sendall(flags.to_bytes(4, 'big') + buf)
        self.sequence += 1

    def recv(self):
        if self.socket is None: return None, None
        try:
            while len(self.queue) < 4:
                d = self.socket.recv(8192)
                if not d: return None, None
                self.queue += d
            flags = int.from_bytes(self.queue[:4], 'big')
            size = flags & 0xFFFFFF; ctype = flags >> 24
            self.last_header_size = size
            while len(self.queue) < size:
                d = self.socket.recv(8192)
                if not d: return None, None
                self.queue += d
            data = self.queue[4:size]; self.queue = self.queue[size:]
            if   ctype == 1:  data = zlib.decompress(data)
            elif ctype == 16: data = zstd.decompress(data)
            elif ctype == 2:  data = _aes_decrypt(data)
            elif ctype == 3:  data = zlib.decompress(_aes_decrypt(data))
            elif ctype == 18: data = zstd.decompress(_aes_decrypt(data))
            result = SdpStruct(data)
            pid = result[0]
            if pid is None: return None, None
            body = result.get(6)
            if not isinstance(body, bytes):
                body = result.get(5)
                if not isinstance(body, bytes): return pid, None
            parsed = SdpStruct(body)
            self._sniff(parsed)
            return pid, parsed
        except socket.timeout: return -1, None
        except Exception: return None, None

    def login(self):
        self.connect(LOGIN_HOST, LOGIN_PORT)
        self.send(1, SdpStruct({
            0: self.device_id,
            1: f'gps_adid={self.advertising_id}&android_id={self.android_id}'
               f'&device_unique_id={self.imei_md5}',
            2: CLIENT_VERSION, 3: CHANNEL, 4: 'en'}))
        pid, res = self.recv()
        if pid == 2 and res:
            self.account_id = res.get(0); self.session_key = res.get(1)
            zd = res.get(2)
            if isinstance(zd, dict): self.zone_id = zd.get(0, 0)
            elif isinstance(zd, list) and zd:
                self.zone_id = zd[0] if not isinstance(zd[0], dict) else zd[0].get(0, 0)
            else: self.zone_id = zd or 0
            self.creation_ts = res.get(19, 0)
            return True, res
        return False, res

    def get_server(self):
        self.send(5, SdpStruct({0: self.account_id, 1: self.session_key,
                                2: CLIENT_VERSION, 5: self.zone_id, 6: CHANNEL}))
        pid, res = self.recv()
        if pid == 6 and res:
            addr = res[1]; self.game_host, port_str = addr.split(':')
            self.game_port = int(port_str)
            return True, res
        return False, res

    def enter_game(self):
        self.close()
        self.connect(self.game_host, self.game_port)
        self.send(10001, SdpStruct({0: self.account_id, 1: self.session_key,
                                    2: self.zone_id, 4: CLIENT_VERSION,
                                    13: CHANNEL, 15: self.device_id}))
        self.send(10101, SdpStruct({0: 0, 2: 2}))

    def handshake(self, tries=25):
        for _ in range(tries):
            pid, _ = self.recv()
            if pid is None or pid == -1: return False
            if pid == 10002: return True
        return False

    def lookup(self, value, kind='id'):
        if kind == 'id':
            try: payload = SdpStruct({1: int(value)})
            except (TypeError, ValueError): return None
        else: payload = SdpStruct({0: str(value).strip()})
        self.send(11153, payload)
        miss = 0
        while True:
            pid, res = self.recv()
            if pid is None or pid == -1: return None
            if pid == 11154: return res
            if pid == 20001:
                miss += 1
                if self.last_header_size < 100 and miss >= 2: return None

    def role_info(self, role_id, zone_id):
        self.send(10128, SdpStruct({1: int(role_id), 2: int(zone_id)}))
        best = None
        for _ in range(30):
            pid, res = self.recv()
            if pid is None or pid == -1: break
            if pid == 20001: continue
            if pid == 10129 and res is not None:
                if best is None: best = res
                try:
                    if int(res.get(9, 0) or 0) > 0: return res
                except (TypeError, ValueError): pass
        return best

    def skin_info(self, role_id, zone_id, tries=3):
        for _ in range(tries):
            self.send(10143, SdpStruct({0: int(role_id), 1: int(zone_id)}))
            timeouts = 0
            while timeouts < 3:
                pid, res = self.recv()
                if pid is None: break
                if pid == -1: timeouts += 1
                elif pid == 10144: return res
                elif pid == 20001: continue
        return None

    def v2l_status(self, role_id, zone_id, tries=2):
        for _ in range(tries):
            self.send(10208, SdpStruct({0: int(role_id), 1: int(zone_id)}))
            timeouts = 0
            while timeouts < 2:
                pid, res = self.recv()
                if pid is None: break
                if pid == -1: timeouts += 1
                elif pid == 10208 and res:
                    return {'_src': 10208, '_data': dict(res)}
                elif pid == 20001: continue
            self.send(10145, SdpStruct({0: int(role_id), 1: int(zone_id)}))
            timeouts = 0
            while timeouts < 2:
                pid, res = self.recv()
                if pid is None: break
                if pid == -1: timeouts += 1
                elif pid in (10146, 10160) and res:
                    return {'_src': pid, '_data': dict(res)}
                elif pid == 20001: continue
        return None


# ═══════════════════════════════════════════════════════════════════
# EXTRACTORS
# ═══════════════════════════════════════════════════════════════════

def _find_last_match_ts(entry):
    if not isinstance(entry, dict): return 0
    now = int(time.time()); lower = now - 3 * 365 * 86400; upper = now + 2 * 86400

    def _norm(v):
        if not isinstance(v, int) or isinstance(v, bool): return None
        if v > 10 ** 15: v //= 1_000_000
        elif v > 10 ** 12: v //= 1_000
        return v if lower <= v <= upper else None

    for key in (9, 10, 6, 7, 8, 11, 12, 13, 14, 15, 16, 17, 18):
        t = _norm(entry.get(key))
        if t: return t
    for v in entry.values():
        t = _norm(v)
        if t: return t
    for v in entry.values():
        if isinstance(v, dict):
            for vv in v.values():
                t = _norm(vv)
                if t: return t
    return 0


def extract_last_match(ri, p):
    containers = []
    for src in (ri, p):
        if not isinstance(src, dict): continue
        for tag in (120, 130, 145, 150, 190, 200):
            v = src.get(tag)
            if isinstance(v, (dict, list)) and v: containers.append(v)
    entry = None
    for c in containers:
        if isinstance(c, list) and c and isinstance(c[0], dict): entry = c[0]; break
        if isinstance(c, dict):
            for k in sorted(c.keys(), key=lambda x: (isinstance(x, str), x)):
                if isinstance(c[k], dict): entry = c[k]; break
            if entry: break
    if not entry: return None
    hero_id = entry.get(0, 0) or entry.get(1, 0) or 0
    kills = as_int(entry.get(2, 0)); deaths = as_int(entry.get(3, 0))
    assists = as_int(entry.get(4, 0)); duration = as_int(entry.get(5, 0))
    result_raw = entry.get(6, entry.get(7, 0)); mode_raw = entry.get(8, 0)
    ts = _find_last_match_ts(entry); mvp = entry.get(11, 0)
    if result_raw in (1, '1', 'win', 'WIN', True): result = "WIN"
    elif result_raw in (0, '0', 'loss', 'LOSS', False): result = "LOSS"
    else: result = "?"
    return {'hero': HERO_ID_MAP.get(hero_id, f"Hero#{hero_id}") if hero_id else "?",
            'kda': f"{kills}/{deaths}/{assists}", 'result': result,
            'mode': MODE_MAP.get(as_int(mode_raw, -1), f"Mode {mode_raw}"),
            'played_at': fmt_ts(ts)}


def extract_player(result, role_info=None, creation_ts=0, v2l=None):
    if not result or not result.get(0) or len(result[0]) == 0: return None
    try:
        p = result[0][0]; ri = role_info if isinstance(role_info, dict) else {}
        nickname = p.get(2, "") or "Unknown"; level = as_int(p.get(3, 0))
        skin_count = as_int(p.get(83, 0)); hero_count = as_int(p.get(4, 0))
        if ri: hero_count = as_int(ri.get(9, hero_count)) or hero_count
        loc = p.get(71)
        if isinstance(loc, list):
            parts = [str(x).strip() for x in loc if x and str(x).strip()]
            location = ", ".join(parts) if len(parts) >= 1 else None
        else:
            location = None
        last_login_ts = as_int(p.get(5, 0)); last_login = fmt_ts(last_login_ts)
        reg_country = p.get(97, ""); rank_now = map_rank(p.get(8)); rank_top = map_rank(p.get(95))
        t136 = p.get(136, {}); coll_pts = as_int(t136.get(9, 0)) if isinstance(t136, dict) else 0
        v2l_status = None
        if v2l and isinstance(v2l, dict):
            src = v2l.get("_src", 0); data = v2l.get("_data", {})
            probes = (10, 11) if src == 10208 else (0, 2, 3, 5)
            for tag in probes:
                v = data.get(tag)
                if v is not None:
                    try:
                        v2l_status = "Enabled" if int(v) > 0 else "Disabled"; break
                    except (ValueError, TypeError): pass
        min_ts = 1451577600; fallback = as_int(p.get(6, 0))
        if creation_ts and creation_ts >= min_ts: created, age_ts = fmt_ts_full(creation_ts), creation_ts
        elif fallback >= min_ts: created, age_ts = fmt_ts_full(fallback), fallback
        else: created, age_ts = None, 0
        age = None
        if age_ts:
            now = datetime.datetime.now(datetime.timezone.utc)
            start = datetime.datetime.fromtimestamp(age_ts, datetime.timezone.utc)
            d = (now - start).days
            y, m, r = d // 365, (d % 365) // 30, d % 30
            age = f"{y}y {m}m {r}d" if y else (f"{m}m {r}d" if m else f"{d}d")
        return {'last_match': extract_last_match(ri, p), 'nickname': nickname,
                'level': level, 'skin_count': skin_count, 'hero_count': hero_count,
                'location': location, 'last_login': last_login,
                'last_login_ts': last_login_ts, 'region_country': reg_country or None,
                'high_rank': rank_top, 'current_rank': rank_now,
                'collector_point': coll_pts, 'collector_tier': map_collector(coll_pts),
                'creation_date': created, 'account_age': age,
                'v2l_status': v2l_status,
                'skin_breakdown': extract_skin_breakdown(ri, p),
                'ban_status': 'CLEAN', 'ban_reason': None,
                'ban_code': None, 'ban_duration': None}
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════
# HIGH-LEVEL OPS — login failures surface as distinct status
# ═══════════════════════════════════════════════════════════════════

LOGIN_FAILED = {'status': 'login_failed', 'error': 'login failed'}


def _op_ban_check(device_id):
    conn = GameConn(device_id)
    try:
        ok, _ = conn.login()
        if not ok: return dict(LOGIN_FAILED)
        if conn.ban_seen: return _ban_return(conn)
        for _ in range(4):
            pid, _ = conn.recv()
            if conn.ban_seen: return _ban_return(conn)
            if pid is None: break
            if pid == -1: continue
        try: conn.get_server()
        except Exception: pass
        if conn.ban_seen: return _ban_return(conn)
        for _ in range(6):
            pid, _ = conn.recv()
            if conn.ban_seen: return _ban_return(conn)
            if pid is None: break
            if pid == -1: continue
        return {'status': 'clean', 'account_id': conn.account_id, 'zone_id': conn.zone_id}
    except Exception as e:
        return {'status': 'error', 'error': f'{type(e).__name__}: {e}'}
    finally: conn.close()


def _ban_return(conn):
    return {'status': 'banned', 'ban_info': dict(conn.ban_info),
            'reason': conn.ban_info.get('reason_name'),
            'code': conn.ban_info.get('ban_code'),
            'duration': format_ban_duration(conn.ban_info)}


def _op_creation_check(device_id):
    conn = GameConn(device_id)
    try:
        ok, _ = conn.login()
        if not ok: return dict(LOGIN_FAILED)
        ts = conn.creation_ts
        if not ts or ts < 1451577600:
            return {'status': 'error', 'error': 'no creation timestamp',
                    'account_id': conn.account_id}
        now = datetime.datetime.now(datetime.timezone.utc)
        start = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        d = (now - start).days
        y, m, r = d // 365, (d % 365) // 30, d % 30
        age = f"{y}y {m}m {r}d" if y else (f"{m}m {r}d" if m else f"{d}d")
        return {'status': 'ok', 'creation_ts': ts, 'created_at': fmt_ts_full(ts),
                'age': age, 'age_days': d,
                'account_id': conn.account_id, 'zone_id': conn.zone_id}
    except Exception as e:
        return {'status': 'error', 'error': f'{type(e).__name__}: {e}'}
    finally: conn.close()


def _op_single_check(device_id):
    conn = GameConn(device_id)
    try:
        ok, _ = conn.login()
        if not ok: return dict(LOGIN_FAILED)
        account_id, zone_id, creation_ts = conn.account_id, conn.zone_id, conn.creation_ts
        ok, _ = conn.get_server()
        if not ok: return {'status': 'error', 'error': 'no game server'}
        conn.enter_game()
        if not conn.handshake(): return {'status': 'error', 'error': 'handshake failed'}
        result = conn.lookup(int(account_id), 'id')
        if not result: return {'status': 'error', 'error': 'lookup empty'}
        role_id, zone = account_id, zone_id
        if result.get(0) and len(result[0]) > 0:
            first = result[0][0]
            if isinstance(first, dict):
                role_id = first.get(0, account_id); zone = first.get(1, zone_id)
        role_info = None
        try:
            si = conn.skin_info(role_id, zone)
            if si and isinstance(si, dict): role_info = dict(si)
        except Exception: pass
        try:
            ri = conn.role_info(role_id, zone)
            if ri and isinstance(ri, dict):
                if role_info is None: role_info = dict(ri)
                else:
                    for k, v in ri.items():
                        if k not in role_info or not role_info[k]: role_info[k] = v
        except Exception: pass
        v2l = None
        try: v2l = conn.v2l_status(role_id, zone)
        except Exception: pass
        player = extract_player(result, role_info, creation_ts, v2l)
        if not player: return {'status': 'error', 'error': 'extract failed'}
        player['_device'] = device_id
        player['_account'] = account_id
        player['_zone'] = zone_id
        return {'status': 'success', 'player': player, 'device_id': device_id,
                'account_id': account_id, 'zone_id': zone_id}
    except Exception as e:
        return {'status': 'error', 'error': f'{type(e).__name__}: {e}'}
    finally: conn.close()


def _op_probe(device_id):
    conn = GameConn(device_id)
    try:
        ok, _ = conn.login()
        if not ok: return dict(LOGIN_FAILED, device_id=device_id)
        account_id, zone_id, creation_ts = conn.account_id, conn.zone_id, conn.creation_ts
        ok, _ = conn.get_server()
        if not ok: return {'status': 'error', 'device_id': device_id, 'error': 'no server'}
        conn.enter_game()
        if not conn.handshake():
            return {'status': 'error', 'device_id': device_id, 'error': 'handshake failed'}
        result = conn.lookup(int(account_id), 'id')
        if not result: return {'status': 'error', 'device_id': device_id, 'error': 'lookup empty'}
        role_id, zone = account_id, zone_id
        if result.get(0) and len(result[0]) > 0:
            first = result[0][0]
            if isinstance(first, dict):
                role_id = first.get(0, account_id); zone = first.get(1, zone_id)
        role_info = None
        try:
            si = conn.skin_info(role_id, zone)
            if si and isinstance(si, dict): role_info = dict(si)
        except Exception: pass
        try:
            ri = conn.role_info(role_id, zone)
            if ri and isinstance(ri, dict):
                if role_info is None: role_info = dict(ri)
                else:
                    for k, v in ri.items():
                        if k not in role_info or not role_info[k]: role_info[k] = v
        except Exception: pass
        player = extract_player(result, role_info, creation_ts, None)
        if not player:
            return {'status': 'error', 'device_id': device_id, 'error': 'extract failed'}
        player['_device'] = device_id
        player['_account'] = account_id
        player['_zone'] = zone_id
        return {'status': 'clean', 'device_id': device_id, 'player': player,
                'account_id': account_id, 'zone_id': zone_id}
    except Exception as e:
        return {'status': 'error', 'device_id': device_id, 'error': f'{type(e).__name__}: {e}'}
    finally: conn.close()


# ═══════════════════════════════════════════════════════════════════
# RETRY WRAPPER — rotates the proxy every PROXY_ROTATE_EVERY attempts
# ═══════════════════════════════════════════════════════════════════

def with_retries(fn, *args, retries: int = MAX_RETRIES,
                 should_stop: Optional[Callable[[], bool]] = None,
                 rotate_every: int = PROXY_ROTATE_EVERY, **kwargs):
    """
    Retry fn up to `retries` times.

    Terminal statuses (returned immediately): success, ok, clean, banned.
    Retryable statuses (keep retrying): everything else, incl. login_failed.

    Every `rotate_every` failed attempts, marks the last-used proxy bad so the
    next `GameConn.connect()` picks a different one from the pool.
    """
    last = None
    _tls_set_proxy(None)

    for attempt in range(retries):
        if should_stop and should_stop():
            return {'status': 'stopped', 'error': 'stopped by user'}

        try:
            r = fn(*args, **kwargs)
        except Exception as e:
            r = {'status': 'error', 'error': f'{type(e).__name__}: {e}'}

        if r and r.get('status') in ('success', 'ok', 'clean', 'banned'):
            return r

        last = r

        # rotate proxy after every N failed attempts
        if attempt < retries - 1:
            if rotate_every > 0 and (attempt + 1) % rotate_every == 0 and PROXIES.enabled:
                bad = _tls_get_proxy()
                if bad:
                    PROXIES.mark_bad(bad)
                    _tls_set_proxy(None)

            if should_stop and should_stop():
                return {'status': 'stopped', 'error': 'stopped by user'}

            time.sleep(0.4 * (attempt + 1) + random.uniform(0, 0.2))

    return last if last is not None else {'status': 'error', 'error': 'all retries failed'}


# ═══════════════════════════════════════════════════════════════════
# RENDER
# ═══════════════════════════════════════════════════════════════════

def esc(s) -> str:
    return html.escape(str(s if s is not None else "—"))


def fv(val, fallback='—'):
    if val is None: return fallback
    if isinstance(val, str):
        return val if val.strip() and val.strip().upper() != 'N/A' else fallback
    return val


RESULT_SEP = "================"


def render_result_block(device_id, p):
    """
    Plain-text aligned block matching the info.py sample.
    """
    sb = p.get('skin_breakdown') or {}

    def row(label, val):
        return f"  {label:<12}: {val}"

    lines = [device_id]
    lines.append(row("Account ID", p.get('_account', 0)))
    lines.append(row("Zone ID", p.get('_zone', 0)))
    lines.append(row("Nickname", fv(p.get('nickname'), 'Unknown')))
    lines.append(row("Level", p.get('level', 0)))
    lines.append(row("Heroes", p.get('hero_count', 0)))
    lines.append(row("Skins", p.get('skin_count', 0)))
    lines.append(row("Cur Rank", fv(p.get('current_rank'), 'Unranked')))
    lines.append(row("High Rank", fv(p.get('high_rank'), 'Unranked')))
    lines.append(row("Collector", fv(p.get('collector_tier'), 'No Tier')))
    lines.append(row("Coll. Pts", p.get('collector_point', 0)))
    lines.append(row("V2L", fv(p.get('v2l_status'), 'N/A')))
    lines.append(row("Location", fv(p.get('location'), 'NOT FOUND')))

    ll_ts = as_int(p.get('last_login_ts', 0)); ll_str = p.get('last_login')
    if ll_str and ll_ts:
        lines.append(row("Last Login", f"{ll_str} ({human_ago(time.time() - ll_ts)} ago) PHT"))
    elif ll_str:
        lines.append(row("Last Login", f"{ll_str} PHT"))
    else:
        lines.append(row("Last Login", "N/A"))

    lines.append(row("Created", fv(p.get('creation_date'), 'N/A')))
    lines.append(row("Country", fv(p.get('region_country'), 'N/A')))
    lines.append("")
    lines.append("Skin Breakdown:")
    for name in SKIN_TIER_NAMES:
        lines.append(f"    {name}: {sb.get(name, 0)}")
    return "\n".join(lines)


def render_result_block_html(device_id, p):
    return f"<pre>{esc(render_result_block(device_id, p))}</pre>"


def render_ban_html(device_id, r):
    if r.get('status') == 'clean':
        return (f"🚫 <b>BAN CHECK</b>\n<code>{esc(device_id)}</code>\n\n"
                f"✅ <b>CLEAN</b>\nacc <code>{r.get('account_id',0)}</code> · "
                f"zone <code>{r.get('zone_id',0)}</code>")
    if r.get('status') == 'banned':
        return (f"🚫 <b>BAN CHECK</b>\n<code>{esc(device_id)}</code>\n\n"
                f"⛔ <b>BANNED</b>\nreason   <b>{esc(fv(r.get('reason')))}</b>\n"
                f"code     <code>{esc(fv(r.get('code')))}</code>\n"
                f"duration {esc(fv(r.get('duration')))}")
    if r.get('status') == 'login_failed':
        return (f"🚫 <b>BAN CHECK</b>\n<code>{esc(device_id)}</code>\n\n"
                f"⚠️ <b>LOGIN FAILED</b> — retried {MAX_RETRIES}× with proxy rotation.")
    return (f"🚫 <b>BAN CHECK</b>\n<code>{esc(device_id)}</code>\n\n"
            f"⚠️ error: <code>{esc(r.get('error','unknown'))}</code>")


def render_creation_html(device_id, r):
    if r.get('status') == 'ok':
        return (f"📅 <b>CREATION DATE</b>\n<code>{esc(device_id)}</code>\n\n"
                f"🗓 created <b>{esc(r.get('created_at'))}</b>\n"
                f"⏳ age <b>{esc(r.get('age'))}</b>  ({r.get('age_days',0)} days)\n"
                f"acc <code>{r.get('account_id',0)}</code> · zone <code>{r.get('zone_id',0)}</code>")
    if r.get('status') == 'login_failed':
        return (f"📅 <b>CREATION DATE</b>\n<code>{esc(device_id)}</code>\n\n"
                f"⚠️ <b>LOGIN FAILED</b> — retried {MAX_RETRIES}× with proxy rotation.")
    return (f"📅 <b>CREATION DATE</b>\n<code>{esc(device_id)}</code>\n\n"
            f"⚠️ error: <code>{esc(r.get('error','unknown'))}</code>")


def render_bulk_running(stats, elapsed, total, filename):
    done = stats.get('done', 0); clean = stats.get('clean', 0)
    fail = stats.get('fail', 0); stop = stats.get('stopped', 0)
    lf   = stats.get('login_failed', 0)
    bar = progress_bar(done, total, width=12)
    rate = done / elapsed if elapsed > 0 else 0.0
    remaining = max(0, total - done)
    eta_s = remaining / rate if rate > 0 else 0.0
    eta = human_dur(eta_s) if eta_s > 0 else '—'
    proxy_state = f"proxies {PROXIES.size()}" if PROXIES.enabled else "direct"
    stop_tag = " · ⏹ stopping" if stats.get('stop_flag') else ""
    return (f"📦 <b>BULK</b> · <code>{esc(os.path.basename(filename))[:32]}</code>{stop_tag}\n"
            f"<code>{bar}</code>  <b>{done:,}</b> / {total:,}\n"
            f"✅ clean <b>{clean:,}</b>   ⚠️ fail <b>{fail:,}</b>   "
            f"🔒 login_fail <b>{lf:,}</b>   ⏹ stop <b>{stop:,}</b>\n"
            f"⚡ {rate:.1f}/s · ⏱ {human_dur(elapsed)} · eta {eta}\n"
            f"{esc(proxy_state)} · {THREADS}t · {MAX_RETRIES}r · rot {PROXY_ROTATE_EVERY}")


def render_bulk_summary(stats, elapsed, path, stopped):
    total = max(stats.get('total', 0), 1); done = stats.get('done', 0)
    tag = "⏹ STOPPED" if stopped else "✅ DONE"
    return (f"📦 <b>BULK {tag}</b> · <code>{esc(os.path.basename(path))}</code>\n\n"
            f"processed <b>{done:,}</b> / {total:,}\n"
            f"✅ clean         <b>{stats.get('clean',0):,}</b>\n"
            f"⚠️ failed        <b>{stats.get('fail',0):,}</b>\n"
            f"🔒 login failed  <b>{stats.get('login_failed',0):,}</b>\n"
            f"⏹ skipped       <b>{stats.get('stopped',0):,}</b>\n\n"
            f"⏱ {human_dur(elapsed)} · {done/max(elapsed,0.001):.1f}/s · "
            f"{THREADS} threads · {MAX_RETRIES} retries · rot every {PROXY_ROTATE_EVERY}")


# ═══════════════════════════════════════════════════════════════════
# FILE HELPERS
# ═══════════════════════════════════════════════════════════════════

def parse_id_file(raw):
    seen = set(); ids = []
    for line in raw.splitlines():
        line = line.strip().strip('"').strip("'")
        if not line or line.startswith('#'): continue
        if line.startswith(('and_', 'ios_')): key = line
        elif DEVICE_ID_RE.fullmatch(line): key = line
        elif len(line) >= 32 and re.match(r'^[0-9a-fA-F_\-]+$', line): key = line
        else: continue
        if key in seen: continue
        seen.add(key); ids.append(key)
    return ids


def file_from_text(text, name):
    buf = io.BytesIO(); buf.write(text.encode("utf-8")); buf.seek(0)
    return InputFile(buf, filename=name)


# ═══════════════════════════════════════════════════════════════════
# BULK SESSIONS
# ═══════════════════════════════════════════════════════════════════

BULK_SESSIONS: dict[int, dict] = {}
BULK_LOCK = threading.Lock()


def _get_bulk_session(tg_id):
    with BULK_LOCK: return BULK_SESSIONS.get(tg_id)


def _set_bulk_session(tg_id, s):
    with BULK_LOCK:
        if s is None: BULK_SESSIONS.pop(tg_id, None)
        else:         BULK_SESSIONS[tg_id] = s


def _active_sessions():
    with BULK_LOCK: return list(BULK_SESSIONS.items())


def stop_all_sessions() -> int:
    n = 0
    with BULK_LOCK:
        for sess in BULK_SESSIONS.values():
            if sess and not sess.get('stop'):
                sess['stop'] = True; n += 1
    return n


def stop_session(tg_id: int) -> bool:
    with BULK_LOCK:
        s = BULK_SESSIONS.get(tg_id)
        if not s or s.get('stop'): return False
        s['stop'] = True; return True


# ═══════════════════════════════════════════════════════════════════
# MENUS
# ═══════════════════════════════════════════════════════════════════

def main_menu(is_admin=False):
    rows = [
        [InlineKeyboardButton("🔍 Single Check", callback_data="menu:single")],
        [InlineKeyboardButton("📦 Bulk Check", callback_data="menu:bulk")],
        [InlineKeyboardButton("🚫 Ban Check", callback_data="menu:ban"),
         InlineKeyboardButton("📅 Creation Date", callback_data="menu:created")],
        [InlineKeyboardButton("🔑 Redeem Key", callback_data="menu:redeem"),
         InlineKeyboardButton("❓ Help", callback_data="menu:help")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("👑 Admin Panel", callback_data="menu:admin")])
    return InlineKeyboardMarkup(rows)


def back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="menu:home")]])


def bulk_stop_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⏹ Stop", callback_data="bulk:stop")]])


def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats", callback_data="admin:stats"),
         InlineKeyboardButton("👥 Users", callback_data="admin:users")],
        [InlineKeyboardButton("🔑 List Keys", callback_data="admin:keys")],
        [InlineKeyboardButton("➕ Generate Key", callback_data="admin:gen")],
        [InlineKeyboardButton("🗑 Revoke Key", callback_data="admin:revoke")],
        [InlineKeyboardButton("📣 Broadcast", callback_data="admin:broadcast")],
        [InlineKeyboardButton("🌐 Proxies", callback_data="admin:proxies")],
        [InlineKeyboardButton("◀️ Back", callback_data="menu:home")],
    ])


# ═══════════════════════════════════════════════════════════════════
# USER STATE
# ═══════════════════════════════════════════════════════════════════

USER_STATE: dict[int, str] = {}
STATE_LOCK = threading.Lock()


def set_state(tg_id, s):
    with STATE_LOCK:
        if s is None: USER_STATE.pop(tg_id, None)
        else:         USER_STATE[tg_id] = s


def get_state(tg_id):
    with STATE_LOCK: return USER_STATE.get(tg_id)


def clear_state(tg_id):
    with STATE_LOCK: USER_STATE.pop(tg_id, None)


# ═══════════════════════════════════════════════════════════════════
# COMMAND HANDLERS
# ═══════════════════════════════════════════════════════════════════

async def cmd_start(update, context):
    u = update.effective_user
    DBX.ensure_user(u.id, u.username or u.first_name or "")
    is_admin = DBX.is_admin(u.id)
    ok, reason, _ = DBX.auth_status(u.id)
    head = (f"🥔 <b>DRAKVEX</b> bot\n\n"
            f"checks: single · bulk · ban · creation\n"
            f"threads: {THREADS} · retries: {MAX_RETRIES} · "
            f"proxies: {'on' if PROXIES.enabled else 'off'}\n\n")
    if is_admin: head += "👑 admin access confirmed\n— /admin to open the panel\n"
    elif ok:     head += "🔓 key active\n"
    else:        head += f"🔒 <b>{esc(reason)}</b>\n— tap Redeem Key to unlock\n"
    await update.message.reply_text(head, parse_mode=ParseMode.HTML,
                                     reply_markup=main_menu(is_admin))


async def cmd_help(update, context):
    text = ("🥔 <b>DRAKVEX</b>\n\n"
            "<b>Single Check</b> — one device id, full profile block.\n"
            "<b>Bulk Check</b> — .txt of ids (one per line), live stats + ⏹ Stop.\n"
            "<b>Ban Check</b> — one id, ban only (separate from single/bulk).\n"
            "<b>Creation Date</b> — one id, creation timestamp + account age.\n"
            "<b>Redeem Key</b> — attach a key.\n\n"
            "<b>User</b>\n"
            "/start · /help · /menu · /whoami · /usage\n"
            "/check &lt;id&gt; · /ban &lt;id&gt; · /created &lt;id&gt;\n"
            "/bulk (reply to file) · /key &lt;key&gt;\n"
            "/stop — stop every running bulk session\n\n"
            "<b>Admin</b>\n"
            "/admin · /genkey &lt;tier&gt; &lt;uses&gt; &lt;days&gt; · /revoke &lt;key&gt;\n"
            "/broadcast &lt;text&gt; · /reloadproxies\n")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=back_kb())


async def cmd_menu(update, context):
    u = update.effective_user
    DBX.ensure_user(u.id, u.username or "")
    await update.message.reply_text("menu:", reply_markup=main_menu(DBX.is_admin(u.id)))


async def cmd_whoami(update, context):
    u = update.effective_user
    DBX.ensure_user(u.id, u.username or "")
    row = DBX.get_user(u.id) or {}
    key = row.get('key') or '—'
    kinfo = DBX.get_key(key) if key != '—' else None
    lines = [f"tg id <code>{u.id}</code>",
             f"admin {'✅' if DBX.is_admin(u.id) else '❌'}",
             f"banned {'✅' if row.get('banned') else '❌'}",
             f"key <code>{esc(key)}</code>"]
    if kinfo:
        lines.append(f"tier {esc(kinfo['tier'])} · uses {kinfo['uses']}/{kinfo['max_uses'] or '∞'}")
        if kinfo['expires_at']:
            exp = datetime.datetime.fromtimestamp(kinfo['expires_at']).strftime("%Y-%m-%d")
            lines.append(f"expires {exp}")
    lines.append(f"checks used {row.get('checks_used', 0)}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_usage(update, context):
    u = update.effective_user
    with DBX._lock, DBX._conn() as c:
        rows = c.execute("SELECT kind, COUNT(*) AS n FROM usage_log WHERE tg_id=? GROUP BY kind",
                         (u.id,)).fetchall()
    if not rows: await update.message.reply_text("no usage yet."); return
    txt = "\n".join(f"{r['kind']}: {r['n']}" for r in rows)
    await update.message.reply_text(f"your usage:\n{txt}")


async def cmd_stop(update, context):
    u = update.effective_user
    DBX.ensure_user(u.id, u.username or "")
    is_admin = DBX.is_admin(u.id)
    active = _active_sessions()
    if not active:
        await update.message.reply_text("⏹ no running bulk sessions."); return
    if is_admin:
        n = stop_all_sessions()
        await update.message.reply_text(
            f"⏹ stop signal sent to <b>{n}</b> session(s).",
            parse_mode=ParseMode.HTML)
    else:
        ok = stop_session(u.id)
        if ok:
            await update.message.reply_text("⏹ stop signal sent to your bulk run.")
        else:
            await update.message.reply_text("no running session for you.")


async def cmd_admin(update, context):
    u = update.effective_user
    DBX.ensure_user(u.id, u.username or "")
    if not DBX.is_admin(u.id):
        await update.message.reply_text("not admin."); return
    await update.message.reply_text(
        "👑 <b>Admin Panel</b>\n\n"
        "/genkey &lt;tier&gt; &lt;uses&gt; &lt;days&gt;\n/revoke &lt;key&gt;\n"
        "/broadcast &lt;text&gt;\n/reloadproxies\n/stop",
        parse_mode=ParseMode.HTML, reply_markup=admin_menu())


async def cmd_genkey(update, context):
    u = update.effective_user
    DBX.ensure_user(u.id, u.username or "")
    if not DBX.is_admin(u.id):
        await update.message.reply_text("not admin."); return
    args = context.args or []
    if len(args) < 3:
        await update.message.reply_text(
            "usage: /genkey <tier> <uses> <days>\ne.g. /genkey pro 1000 30\n"
            "tiers: trial · basic · pro · unlimited"); return
    await _do_admin_gen(update, context, " ".join(args))


async def cmd_revoke(update, context):
    u = update.effective_user
    DBX.ensure_user(u.id, u.username or "")
    if not DBX.is_admin(u.id):
        await update.message.reply_text("not admin."); return
    args = context.args or []
    if not args: await update.message.reply_text("usage: /revoke <key>"); return
    await _do_admin_revoke(update, context, " ".join(args))


async def cmd_broadcast(update, context):
    u = update.effective_user
    DBX.ensure_user(u.id, u.username or "")
    if not DBX.is_admin(u.id):
        await update.message.reply_text("not admin."); return
    text = " ".join(context.args or []).strip()
    if not text: await update.message.reply_text("usage: /broadcast <text>"); return
    await _do_admin_broadcast(update, context, text)


async def cmd_reloadproxies(update, context):
    u = update.effective_user
    DBX.ensure_user(u.id, u.username or "")
    if not DBX.is_admin(u.id):
        await update.message.reply_text("not admin."); return
    PROXIES._load()
    await update.message.reply_text(f"🌐 proxies reloaded · {PROXIES.size()} entries",
                                     reply_markup=admin_menu())


# ─── callbacks ─────────────────────────────────────────────────────

async def cb_bulk_stop(update, context):
    q = update.callback_query
    u = update.effective_user
    sess = _get_bulk_session(u.id)
    if not sess or sess.get('stop'):
        await q.answer("already stopping…"); return
    sess['stop'] = True
    await q.answer("⏹ stopping after current batch…")
    try:
        await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⏹ stopping…", callback_data="bulk:noop")]]))
    except Exception: pass


async def cb_bulk_noop(update, context):
    await update.callback_query.answer("stopping…")


async def cb_menu(update, context):
    q = update.callback_query; await q.answer()
    u = update.effective_user
    DBX.ensure_user(u.id, u.username or "")
    is_admin = DBX.is_admin(u.id)
    data = q.data or ""

    if data == "menu:home":
        clear_state(u.id)
        await q.edit_message_text("menu:", reply_markup=main_menu(is_admin)); return

    ok, reason, _ = DBX.auth_status(u.id)

    if data == "menu:help":
        await q.edit_message_text(
            "🥔 <b>DRAKVEX</b>\n\n"
            "🔍 single check → full profile block\n"
            "📦 bulk check → .txt of ids, live stats + ⏹ Stop\n"
            "🚫 ban check → ban only (own button)\n"
            "📅 creation date → account age\n"
            "🔑 redeem key → attach a key\n"
            "⏹ /stop → kill running bulk",
            parse_mode=ParseMode.HTML, reply_markup=back_kb()); return

    if not ok and data not in ("menu:redeem",):
        await q.edit_message_text(f"🔒 <b>{esc(reason)}</b>",
                                   parse_mode=ParseMode.HTML, reply_markup=main_menu(is_admin)); return

    if data == "menu:single":
        set_state(u.id, "single")
        await q.edit_message_text("🔍 <b>Single Check</b>\n\nsend one device id now.",
                                   parse_mode=ParseMode.HTML, reply_markup=back_kb())
    elif data == "menu:bulk":
        set_state(u.id, "bulk")
        await q.edit_message_text(
            f"📦 <b>Bulk Check</b>\n\nsend a <code>.txt</code> file with device ids, "
            f"one per line. cap {BULK_CAP:,} per run. live stats + ⏹ Stop.",
            parse_mode=ParseMode.HTML, reply_markup=back_kb())
    elif data == "menu:ban":
        set_state(u.id, "ban")
        await q.edit_message_text("🚫 <b>Ban Check</b>\n\nsend one device id now.",
                                   parse_mode=ParseMode.HTML, reply_markup=back_kb())
    elif data == "menu:created":
        set_state(u.id, "created")
        await q.edit_message_text("📅 <b>Creation Date</b>\n\nsend one device id now.",
                                   parse_mode=ParseMode.HTML, reply_markup=back_kb())
    elif data == "menu:redeem":
        set_state(u.id, "redeem")
        await q.edit_message_text(
            "🔑 <b>Redeem Key</b>\n\nsend the key now. format "
            "<code>DRKV-XXXX-XXXX-XXXX-XXXX-XXXX</code>",
            parse_mode=ParseMode.HTML, reply_markup=back_kb())
    elif data == "menu:admin":
        if not is_admin:
            await q.edit_message_text("not admin.", reply_markup=main_menu(is_admin)); return
        await q.edit_message_text("👑 <b>Admin Panel</b>",
                                   parse_mode=ParseMode.HTML, reply_markup=admin_menu())


async def cb_admin(update, context):
    q = update.callback_query; await q.answer()
    u = update.effective_user
    DBX.ensure_user(u.id, u.username or "")
    if not DBX.is_admin(u.id):
        await q.edit_message_text("not admin."); return
    data = q.data

    if data == "admin:stats":
        s = DBX.stats()
        text = (f"📊 <b>Stats</b>\n\n"
                f"users     <b>{s['users']:,}</b>\n"
                f"keys      <b>{s['keys']:,}</b>  (active {s['active']:,})\n"
                f"checks    <b>{s['checks']:,}</b>\n"
                f"today     <b>{s['today']:,}</b>\n"
                f"threads   <b>{THREADS}</b>  retries <b>{MAX_RETRIES}</b>  "
                f"rot <b>{PROXY_ROTATE_EVERY}</b>\n"
                f"proxies   <b>{PROXIES.size()}</b>\n"
                f"active bulk <b>{len(_active_sessions())}</b>\n")
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=admin_menu())
    elif data == "admin:users":
        users = DBX.list_users(30)
        lines = ["👥 <b>Recent Users</b>\n"]
        for r in users:
            flag = "👑" if r['is_admin'] else ("🚫" if r['banned'] else "·")
            key = r['key'] or '—'
            lines.append(f"{flag} <code>{r['tg_id']}</code> {esc(r['username'] or '')} "
                         f"· {r['checks_used']} · {esc(key[:14])}")
        await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML,
                                   reply_markup=admin_menu())
    elif data == "admin:keys":
        keys = DBX.list_keys(40)
        lines = ["🔑 <b>Recent Keys</b>\n"]
        for k in keys:
            rv = "🗑" if k['revoked'] else "✅"
            exp = "∞" if not k['expires_at'] else datetime.datetime.fromtimestamp(
                k['expires_at']).strftime("%m-%d")
            lines.append(f"{rv} <code>{k['key']}</code>\n"
                         f"   tier {esc(k['tier'])} · uses {k['uses']}/{k['max_uses'] or '∞'} · exp {exp}")
        await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML,
                                   reply_markup=admin_menu())
    elif data == "admin:proxies":
        snap = PROXIES.snapshot()
        if not snap:
            await q.edit_message_text(f"🌐 no proxies loaded.\nfile: <code>{esc(PROXY_FILE)}</code>",
                                       parse_mode=ParseMode.HTML, reply_markup=admin_menu()); return
        lines = [f"🌐 <b>Proxies</b> · {len(snap)} loaded\n"]
        for p in snap[:40]:
            cd = f"· cd {p['cooldown']}s" if p['cooldown'] else ""
            lines.append(f"<code>{esc(p['raw'][:40])}</code>\n"
                         f"   {p['scheme']} · ok {p['ok']} · fail {p['fail']} {cd}")
        await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML,
                                   reply_markup=admin_menu())
    elif data == "admin:gen":
        set_state(u.id, "admin_gen")
        await q.edit_message_text(
            "➕ <b>Generate Key</b>\n\nsend tier · uses · days\n"
            "e.g. <code>pro 1000 30</code>\n\ntiers: trial · basic · pro · unlimited",
            parse_mode=ParseMode.HTML, reply_markup=back_kb())
    elif data == "admin:revoke":
        set_state(u.id, "admin_revoke")
        await q.edit_message_text("🗑 <b>Revoke Key</b>\n\nsend the key to revoke.",
                                   parse_mode=ParseMode.HTML, reply_markup=back_kb())
    elif data == "admin:broadcast":
        set_state(u.id, "admin_broadcast")
        await q.edit_message_text(
            "📣 <b>Broadcast</b>\n\nsend the message text to broadcast to all non-admin users.",
            parse_mode=ParseMode.HTML, reply_markup=back_kb())


# ═══════════════════════════════════════════════════════════════════
# TEXT / DOCUMENT HANDLERS
# ═══════════════════════════════════════════════════════════════════

def first_device_id(text):
    if not text: return None
    m = DEVICE_ID_RE.search(text)
    if m: return m.group(0)
    t = text.strip()
    if t.startswith(('and_', 'ios_')) and len(t) > 40: return t.split()[0]
    return None


async def handle_text(update, context):
    u = update.effective_user
    msg = update.message
    DBX.ensure_user(u.id, u.username or "")
    state = get_state(u.id)
    text = (msg.text or "").strip()

    if text.startswith("/check") or text.startswith("/single"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2: await msg.reply_text("usage: /check <device_id>"); return
        await _do_single(update, context, parts[1].strip()); return
    if text.startswith("/ban"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2: await msg.reply_text("usage: /ban <device_id>"); return
        await _do_ban(update, context, parts[1].strip()); return
    if text.startswith("/created"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2: await msg.reply_text("usage: /created <device_id>"); return
        await _do_created(update, context, parts[1].strip()); return
    if text.startswith("/key"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2: await msg.reply_text("usage: /key <your_key>"); return
        await _do_redeem(update, context, parts[1].strip()); return

    if state == "redeem":
        clear_state(u.id); await _do_redeem(update, context, text); return
    if state == "single":
        clear_state(u.id); await _do_single(update, context, text); return
    if state == "ban":
        clear_state(u.id); await _do_ban(update, context, text); return
    if state == "created":
        clear_state(u.id); await _do_created(update, context, text); return
    if state == "admin_gen" and DBX.is_admin(u.id):
        clear_state(u.id); await _do_admin_gen(update, context, text); return
    if state == "admin_revoke" and DBX.is_admin(u.id):
        clear_state(u.id); await _do_admin_revoke(update, context, text); return
    if state == "admin_broadcast" and DBX.is_admin(u.id):
        clear_state(u.id); await _do_admin_broadcast(update, context, text); return

    if first_device_id(text):
        await msg.reply_text(
            "pick a mode first, then send the id.\nor use /check · /ban · /created.",
            reply_markup=main_menu(DBX.is_admin(u.id)))
    else:
        await msg.reply_text("menu:", reply_markup=main_menu(DBX.is_admin(u.id)))


async def handle_document(update, context):
    u = update.effective_user
    DBX.ensure_user(u.id, u.username or "")
    state = get_state(u.id)
    caption = (update.message.caption or "").lower()
    is_bulk = state == "bulk" or "/bulk" in caption

    ok, reason, _ = DBX.auth_status(u.id)
    if not ok and not DBX.is_admin(u.id):
        await update.message.reply_text(f"🔒 {reason}"); return
    if not is_bulk:
        await update.message.reply_text(
            "to bulk-check, tap 📦 Bulk Check then send the file — or reply with /bulk caption."); return
    clear_state(u.id)

    doc = update.message.document
    if not doc: await update.message.reply_text("no document."); return
    if doc.file_size and doc.file_size > 5 * 1024 * 1024:
        await update.message.reply_text("file too big (max 5 MB)."); return

    tg_file = await doc.get_file()
    buf = io.BytesIO()
    await tg_file.download_to_memory(buf)
    raw = buf.getvalue().decode("utf-8", errors="replace")

    ids = parse_id_file(raw)
    if not ids:
        await update.message.reply_text("no valid device ids found in file."); return
    if len(ids) > BULK_CAP:
        ids = ids[:BULK_CAP]
        await update.message.reply_text(f"capped at {BULK_CAP:,} ids per run.")

    await _run_bulk(update, context, ids, doc.file_name or "bulk.txt")


# ═══════════════════════════════════════════════════════════════════
# OPERATION RUNNERS — retries apply to single, ban, created, bulk
# ═══════════════════════════════════════════════════════════════════

async def _do_redeem(update, context, key):
    u = update.effective_user
    ok, msg = DBX.redeem_key(u.id, key)
    if ok:
        await update.message.reply_text(f"🔓 <b>key redeemed</b>\n\n{esc(msg)}",
            parse_mode=ParseMode.HTML, reply_markup=main_menu(DBX.is_admin(u.id)))
    else:
        await update.message.reply_text(f"❌ {esc(msg)}")


async def _do_single(update, context, raw):
    u = update.effective_user
    ok, reason, _ = DBX.auth_status(u.id)
    if not ok and not DBX.is_admin(u.id):
        await update.message.reply_text(f"🔒 {reason}"); return
    device_id = first_device_id(raw)
    if not device_id:
        await update.message.reply_text("no valid device id in that message."); return

    msg = await update.message.reply_text(
        f"🔍 checking <code>{esc(device_id[:32])}…</code>\n"
        f"<i>{MAX_RETRIES} retries · proxy rot every {PROXY_ROTATE_EVERY}</i>",
        parse_mode=ParseMode.HTML)
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(
            EXECUTOR, lambda: with_retries(_op_single_check, device_id))
    except Exception as e:
        res = {'status': 'error', 'error': str(e)}

    DBX.log_usage(u.id, 'single', device_id)
    if not DBX.is_admin(u.id): DBX.consume_use(u.id)

    if res.get('status') == 'login_failed':
        await msg.edit_text(
            f"⚠️ <b>LOGIN FAILED</b>\n<code>{esc(device_id[:48])}</code>\n\n"
            f"retried {MAX_RETRIES}× with proxy rotation — all attempts returned login reject.",
            parse_mode=ParseMode.HTML); return

    if res.get('status') != 'success':
        await msg.edit_text(f"⚠️ <b>error</b>\n<code>{esc(res.get('error','unknown'))}</code>",
                             parse_mode=ParseMode.HTML); return

    out = render_result_block_html(device_id, res['player'])
    if len(out) > 4000: out = out[:3990] + "…</pre>"
    try: await msg.edit_text(out, parse_mode=ParseMode.HTML)
    except Exception: await msg.edit_text(out[:3990] + "…</pre>", parse_mode=ParseMode.HTML)


async def _do_ban(update, context, raw):
    u = update.effective_user
    ok, reason, _ = DBX.auth_status(u.id)
    if not ok and not DBX.is_admin(u.id):
        await update.message.reply_text(f"🔒 {reason}"); return
    device_id = first_device_id(raw)
    if not device_id:
        await update.message.reply_text("no valid device id in that message."); return

    msg = await update.message.reply_text(
        f"🚫 checking ban for <code>{esc(device_id[:32])}…</code>\n"
        f"<i>{MAX_RETRIES} retries · proxy rot every {PROXY_ROTATE_EVERY}</i>",
        parse_mode=ParseMode.HTML)
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(
            EXECUTOR, lambda: with_retries(_op_ban_check, device_id))
    except Exception as e:
        res = {'status': 'error', 'error': str(e)}

    DBX.log_usage(u.id, 'ban', device_id)
    if not DBX.is_admin(u.id): DBX.consume_use(u.id)

    await msg.edit_text(render_ban_html(device_id, res), parse_mode=ParseMode.HTML)


async def _do_created(update, context, raw):
    u = update.effective_user
    ok, reason, _ = DBX.auth_status(u.id)
    if not ok and not DBX.is_admin(u.id):
        await update.message.reply_text(f"🔒 {reason}"); return
    device_id = first_device_id(raw)
    if not device_id:
        await update.message.reply_text("no valid device id in that message."); return

    msg = await update.message.reply_text(
        f"📅 checking creation for <code>{esc(device_id[:32])}…</code>\n"
        f"<i>{MAX_RETRIES} retries · proxy rot every {PROXY_ROTATE_EVERY}</i>",
        parse_mode=ParseMode.HTML)
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(
            EXECUTOR, lambda: with_retries(_op_creation_check, device_id))
    except Exception as e:
        res = {'status': 'error', 'error': str(e)}

    DBX.log_usage(u.id, 'created', device_id)
    if not DBX.is_admin(u.id): DBX.consume_use(u.id)

    await msg.edit_text(render_creation_html(device_id, res), parse_mode=ParseMode.HTML)


# ─── bulk ──────────────────────────────────────────────────────────

async def _run_bulk(update, context, ids, filename):
    u = update.effective_user
    total = len(ids)

    session = {'stop': False}
    _set_bulk_session(u.id, session)

    stats = {'total': total, 'done': 0, 'clean': 0, 'fail': 0, 'stopped': 0,
             'login_failed': 0, 'stop_flag': False}
    lock = threading.Lock()
    results: list[dict] = []
    started = time.time()

    status_msg = await update.message.reply_text(
        render_bulk_running(stats, 0.01, total, filename),
        parse_mode=ParseMode.HTML, reply_markup=bulk_stop_kb())

    loop = asyncio.get_running_loop()

    def should_stop():
        return session.get('stop', False)

    def run_one(device_id):
        if should_stop():
            with lock:
                stats['done'] += 1; stats['stopped'] += 1; stats['stop_flag'] = True
            return {'status': 'stopped', 'device_id': device_id}
        try:
            r = with_retries(_op_probe, device_id, should_stop=should_stop)
        except Exception as e:
            r = {'status': 'error', 'device_id': device_id, 'error': str(e)}
        r['device_id'] = device_id
        with lock:
            stats['done'] += 1
            st = r.get('status')
            if st == 'clean':          stats['clean'] += 1
            elif st == 'stopped':      stats['stopped'] += 1; stats['stop_flag'] = True
            elif st == 'login_failed': stats['login_failed'] += 1
            else:                      stats['fail'] += 1
        return r

    async def progress_loop():
        interval = max(LIVE_MS, 800) / 1000.0
        edit_fails = 0
        while True:
            await asyncio.sleep(interval)
            with lock:
                snap = dict(stats)
                snap['stop_flag'] = stats.get('stop_flag') or session.get('stop', False)
            text = render_bulk_running(snap, time.time() - started, total, filename)
            try:
                await status_msg.edit_text(text, parse_mode=ParseMode.HTML)
                edit_fails = 0
            except Exception:
                edit_fails += 1
                if edit_fails > 6: return
            if snap['done'] >= total: return

    futures = [loop.run_in_executor(EXECUTOR, run_one, d) for d in ids]
    prog_task = asyncio.create_task(progress_loop())

    try:
        for coro in asyncio.as_completed(futures):
            try:
                r = await coro
                if r: results.append(r)
            except Exception:
                pass
    except Exception:
        pass
    finally:
        try: prog_task.cancel()
        except Exception: pass
        _set_bulk_session(u.id, None)

    elapsed = time.time() - started
    was_stopped = bool(session.get('stop'))

    DBX.log_usage(u.id, f'bulk:{total}', filename)
    if not DBX.is_admin(u.id):
        completed = sum(1 for r in results if r.get('status') not in ('stopped',))
        for _ in range(completed): DBX.consume_use(u.id)

    summary = render_bulk_summary(stats, elapsed, filename, was_stopped)
    try:
        await status_msg.edit_text(summary, parse_mode=ParseMode.HTML, reply_markup=back_kb())
    except Exception:
        try:
            await update.message.reply_text(summary, parse_mode=ParseMode.HTML, reply_markup=back_kb())
        except Exception:
            pass

    clean_blocks = []; all_blocks = []; clean_ids = []
    for r in results:
        d = r.get('device_id', ''); st = r.get('status', 'error')
        if st == 'clean' and r.get('player'):
            block = render_result_block(d, r['player'])
            clean_blocks.append(block); all_blocks.append(block); clean_ids.append(d)
        elif st == 'clean':
            clean_blocks.append(f"{d}\n  (no profile data)")
            all_blocks.append(f"{d}\n  (no profile data)"); clean_ids.append(d)
        elif st == 'stopped':
            all_blocks.append(f"{d}\n  STATUS: stopped")
        elif st == 'login_failed':
            all_blocks.append(f"{d}\n  STATUS: login_failed\n  reason: all {MAX_RETRIES} attempts returned login reject")
        else:
            all_blocks.append(f"{d}\n  STATUS: error\n  reason: {r.get('error','?')}")

    sep = f"\n{RESULT_SEP}\n"
    clean_text = (sep.join(clean_blocks) + "\n") if clean_blocks else ""
    all_text = (sep.join(all_blocks) + "\n") if all_blocks else ""

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = re.sub(r'[^A-Za-z0-9_\-]', '_', os.path.splitext(filename)[0])[:32] or "bulk"
    partial_tag = "_partial" if was_stopped else ""

    try:
        if clean_text:
            await update.message.reply_document(
                document=file_from_text(clean_text, f"{stem}_{ts}_clean{partial_tag}.txt"),
                caption=f"✅ clean ({len(clean_blocks):,})"
                        + (" · partial run" if was_stopped else ""))
        if all_text:
            await update.message.reply_document(
                document=file_from_text(all_text, f"{stem}_{ts}_all{partial_tag}.txt"),
                caption=f"📄 full report ({len(all_blocks):,})"
                        + (" · partial run" if was_stopped else ""))
        if clean_ids:
            await update.message.reply_document(
                document=file_from_text("\n".join(clean_ids) + "\n",
                                        f"{stem}_{ts}_ids{partial_tag}.txt"),
                caption=f"🆔 clean ids ({len(clean_ids):,})")
    except Exception as e:
        await update.message.reply_text(f"file send failed: {esc(str(e))}")


# ─── admin helpers ─────────────────────────────────────────────────

async def _do_admin_gen(update, context, raw):
    u = update.effective_user
    parts = raw.split()
    if len(parts) < 3:
        await update.message.reply_text("usage: <tier> <uses> <days>  e.g. pro 1000 30"); return
    tier = parts[0].lower()
    if tier not in ("trial", "basic", "pro", "unlimited"):
        await update.message.reply_text("tier must be one of: trial · basic · pro · unlimited"); return
    try:
        uses = int(parts[1]); days = int(parts[2])
    except ValueError:
        await update.message.reply_text("uses and days must be integers."); return
    key = DBX.generate_key(tier, uses, days, u.id)
    await update.message.reply_text(
        f"🔑 <b>key generated</b>\n\n<code>{esc(key)}</code>\n\n"
        f"tier {esc(tier)} · uses {uses or '∞'} · days {days or '∞'}",
        parse_mode=ParseMode.HTML, reply_markup=admin_menu())


async def _do_admin_revoke(update, context, raw):
    key = raw.strip().upper()
    if DBX.revoke_key(key):
        await update.message.reply_text(f"🗑 revoked <code>{esc(key)}</code>",
            parse_mode=ParseMode.HTML, reply_markup=admin_menu())
    else:
        await update.message.reply_text("no such key.")


async def _do_admin_broadcast(update, context, raw):
    u = update.effective_user
    text = raw.strip()
    if not text:
        await update.message.reply_text("empty message."); return
    users = DBX.list_users(5000)
    sent = 0; failed = 0
    for r in users:
        if r['tg_id'] == u.id or r['is_admin']: continue
        try:
            await context.bot.send_message(chat_id=r['tg_id'], text=text); sent += 1
        except Exception: failed += 1
    await update.message.reply_text(f"📣 broadcast done · sent {sent} · failed {failed}",
                                     reply_markup=admin_menu())


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("stop", cmd_stop))

    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("panel", cmd_admin))
    app.add_handler(CommandHandler("genkey", cmd_genkey))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("reloadproxies", cmd_reloadproxies))

    app.add_handler(CallbackQueryHandler(cb_bulk_stop, pattern=r"^bulk:stop$"))
    app.add_handler(CallbackQueryHandler(cb_bulk_noop, pattern=r"^bulk:noop$"))
    app.add_handler(CallbackQueryHandler(cb_admin, pattern=r"^admin:"))
    app.add_handler(CallbackQueryHandler(cb_menu, pattern=r"^menu:"))

    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print(f"[+] DRAKVEX bot online · {THREADS} threads · {MAX_RETRIES} retries · rot {PROXY_ROTATE_EVERY}")
    print(f"[+] admins: {sorted(BOOT_ADMINS)} · proxies: {PROXIES.size()} from {PROXY_FILE}")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
