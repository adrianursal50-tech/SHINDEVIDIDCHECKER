# shin_bot.py
# SHIN Telegram bot — MLBB device checker.
# python-telegram-bot 20.8+, zstandard, pycryptodome, optional pysocks
#
# v11 — fixes + cleanup:
#   • SINGLE CHECK PROTOCOL ERROR fixed: handshake now sends 10003 after
#     10002 and waits for 10004/10008 — same flow the old ban check had.
#   • BAN CHECK REMOVED as a mode. Ban info now rides inside single check
#     and inside bulk results. /ban is an alias for /check.
#   • /stop actually stops: GameConn.hard_close() calls shutdown(SHUT_RDWR)
#     before close(), which is what unblocks a peer blocked in recv() on
#     Linux. BulkCancel.set() fires hard_close on every registered conn.
#   • Rate-limit backoff capped at 8s with full jitter — no more 150s
#     stalls per device when the server throttles.
#   • Bulk result files carry full profile info for clean, banned, and
#     all-result buckets.
#   • Bot token is env-only; no hardcoded fallback.
# v10 retained: BulkCancel event + socket registry, cancel-aware ops.
# v9  retained: info.py skin parser, proxy pinning every N.
# v8  retained: strict tier parser replaced by info.py port.

import os, re, io, sys, json, time, zlib, uuid, html, base64
import socket, struct, sqlite3, random, logging, asyncio, datetime
import threading, traceback
from enum import Enum
from typing import Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import zstandard as zstd
from Crypto.Cipher import AES

import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("shin")

try:
    _PTB_VER = telegram.__version__
    _ptb_parts = _PTB_VER.split(".")
    _PTB_MAJOR = int(_ptb_parts[0])
    _PTB_MINOR = int(_ptb_parts[1]) if len(_ptb_parts) > 1 else 0
except Exception:
    _PTB_VER, _PTB_MAJOR, _PTB_MINOR = "unknown", 0, 0

if (_PTB_MAJOR, _PTB_MINOR) < (20, 8):
    print(f"[!] python-telegram-bot {_PTB_VER} not compatible with Python 3.13.",
          file=sys.stderr)
    print("[!] upgrade:  pip install 'python-telegram-bot>=20.8,<21'", file=sys.stderr)
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════

BOT_TOKEN = os.environ.get("SHIN_BOT_TOKEN", "8702549007:AAHxNCGdtyEdGihy7FfoXhNTtx3mh1UN4W0").strip()
DB_PATH   = os.environ.get("SHIN_DB", "shin.db")

DEFAULT_ADMINS = "8621676055"
_admin_env  = os.environ.get("SHIN_ADMINS", DEFAULT_ADMINS).strip()
BOOT_ADMINS = {int(x) for x in _admin_env.split(",") if x.strip().isdigit()}
if not BOOT_ADMINS:
    BOOT_ADMINS = {8621676055}

THREADS        = int(os.environ.get("SHIN_THREADS", "50"))
RETRIES        = int(os.environ.get("SHIN_RETRIES", "8"))
BULK_CAP       = int(os.environ.get("SHIN_BULK_CAP", "10000"))
LIVE_MS        = int(os.environ.get("SHIN_LIVE_MS", "900"))
SOCKET_TIMEOUT = int(os.environ.get("SHIN_SOCK_TIMEOUT", "20"))
MAX_INFLIGHT   = int(os.environ.get("SHIN_MAX_INFLIGHT", "10"))
MIN_GAP        = float(os.environ.get("SHIN_MIN_GAP", "0.2"))
BASE_BACKOFF   = float(os.environ.get("SHIN_BACKOFF", "0.6"))
MAX_ATTEMPTS   = int(os.environ.get("SHIN_RETRIES", "4"))
PROXIES_PATH   = os.environ.get(
    "SHIN_PROXIES",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxies.txt"),
)
PROXY_ROTATE_EVERY = int(os.environ.get("SHIN_PROXY_ROTATE_EVERY", "2"))
MAX_BACKOFF    = float(os.environ.get("SHIN_MAX_BACKOFF", "8.0"))

EXECUTOR = ThreadPoolExecutor(max_workers=THREADS, thread_name_prefix="shin")

if not BOT_TOKEN:
    print("[!] SHIN_BOT_TOKEN is not set. Refusing to boot.", file=sys.stderr)
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════════
# BULK CANCEL — immediate stop primitive
# ═══════════════════════════════════════════════════════════════════
#
# A BulkCancel holds:
#   - a threading.Event  → cheap is_set() / wait(timeout)
#   - a registry of GameConn objects currently in flight
#
# cancel.set() does two things in one shot:
#   1) flag the event → every cancel.is_set() check trips
#   2) hard_close() every registered socket — shutdown(SHUT_RDWR)
#      unblocks any peer stuck in recv() instead of waiting for
#      SOCKET_TIMEOUT to elapse.

class BulkCancel:
    __slots__ = ("event", "_conns", "_lock")

    def __init__(self):
        self.event = threading.Event()
        self._conns = set()
        self._lock = threading.Lock()

    def is_set(self) -> bool:
        return self.event.is_set()

    def wait(self, timeout: float) -> bool:
        return self.event.wait(timeout)

    def set(self):
        # 1) flag first so any check already in flight sees it
        self.event.set()
        # 2) snapshot and hard-close every active socket —
        #    shutdown(SHUT_RDWR) wakes a blocked recv() immediately.
        with self._lock:
            conns = list(self._conns)
        for c in conns:
            try:
                c.hard_close()
            except Exception:
                pass

    def clear(self):
        with self._lock:
            self._conns.clear()
        self.event.clear()

    def register(self, conn):
        with self._lock:
            if self.event.is_set():
                # Already cancelled — refuse registration and close the
                # conn so it doesn't leak.
                try:
                    conn.hard_close()
                except Exception:
                    pass
                return
            self._conns.add(conn)

    def unregister(self, conn):
        with self._lock:
            self._conns.discard(conn)


# ═══════════════════════════════════════════════════════════════════
# PROXY POOL
# ═══════════════════════════════════════════════════════════════════

class ProxyPool:
    def __init__(self, path: str):
        self.path = path
        self.proxies = []
        self._idx = 0
        self._pin_left = 0
        self._lock = threading.Lock()
        self._load()

    def _normalize(self, ln):
        s = ln.strip()
        if not s or s.startswith("#"):
            return None
        if "://" in s:
            return s
        if s.startswith("["):
            close = s.find("]")
            if close < 0:
                return None
            host = s[1:close]
            rest = s[close + 1:].lstrip(":")
            parts = [host] + (rest.split(":") if rest else [])
            if len(parts) == 2 and parts[1].isdigit():
                return f"http://[{host}]:{parts[1]}"
            if len(parts) == 4 and parts[1].isdigit():
                _, port, user, pw = parts
                return f"http://{user}:{pw}@[{host}]:{port}"
            return None
        parts = s.split(":")
        if len(parts) == 4:
            host, port, user, pw = parts
            if host and port.isdigit() and user and pw:
                return f"http://{user}:{pw}@{host}:{port}"
            return None
        if "@" in s:
            return "http://" + s
        if len(parts) == 2:
            host, port = parts
            if host and port.isdigit():
                return f"http://{host}:{port}"
            return None
        return None

    def _load(self):
        if not os.path.exists(self.path):
            log.warning("no proxies.txt at %s — running direct", self.path)
            return
        raw_count = 0
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw or raw.startswith("#"):
                        continue
                    raw_count += 1
                    norm = self._normalize(raw)
                    if norm:
                        self.proxies.append(norm)
                    else:
                        log.warning("unparseable proxy line: %r", raw[:60])
            log.info("loaded %d/%d proxies from %s",
                     len(self.proxies), raw_count, self.path)
        except Exception:
            log.exception("proxy load failed")

    def for_device(self):
        if not self.proxies:
            return None
        with self._lock:
            if self._pin_left <= 0:
                self._pin_left = max(1, PROXY_ROTATE_EVERY)
                self._idx += 1
            self._pin_left -= 1
            return self.proxies[(self._idx - 1) % len(self.proxies)]

    def size(self):
        return len(self.proxies)


PROXY_POOL = ProxyPool(PROXIES_PATH)

_inflight = threading.Semaphore(max(1, MAX_INFLIGHT))
_pace_lock = threading.Lock()
_last_start = [0.0]


def _pace():
    with _pace_lock:
        now = time.monotonic()
        wait = MIN_GAP - (now - _last_start[0])
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.08))
        _last_start[0] = time.monotonic()


def _make_proxied_socket(proxy_url, host, port, timeout):
    u = urlparse(proxy_url)
    scheme = (u.scheme or "http").lower()
    if scheme in ("socks5", "socks5h", "socks4", "socks4a"):
        try:
            import socks
        except ImportError as e:
            raise ConnectionError("pysocks not installed") from e
        stype = socks.SOCKS5 if scheme.startswith("socks5") else socks.SOCKS4
        s = socks.socksocket()
        s.set_proxy(stype, u.hostname, int(u.port),
                    username=u.username, password=u.password)
        s.settimeout(timeout)
        s.connect((host, port))
        return s
    if not u.hostname or not u.port:
        raise ConnectionError(f"malformed proxy url: {proxy_url}")
    s = socket.create_connection((u.hostname, int(u.port)), timeout=timeout)
    s.settimeout(timeout)
    auth = ""
    if u.username and u.password:
        token = base64.b64encode(
            f"{u.username}:{u.password}".encode("utf-8")
        ).decode("ascii")
        auth = f"Proxy-Authorization: Basic {token}\r\n"
    req = (f"CONNECT {host}:{port} HTTP/1.1\r\n"
           f"Host: {host}:{port}\r\n{auth}\r\n")
    s.sendall(req.encode("ascii"))
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(1024)
        if not chunk:
            s.close(); raise ConnectionError("proxy closed during CONNECT")
        buf += chunk
        if len(buf) > 8192:
            s.close(); raise ConnectionError("proxy CONNECT response too large")
    status_line = buf.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    if " 200 " not in status_line:
        s.close(); raise ConnectionError(f"proxy CONNECT rejected: {status_line}")
    return s


# ═══════════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════════

class DB:
    _lock = threading.RLock()
    def __init__(self, path):
        self.path = path
        self._init()

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
                key TEXT, is_admin INTEGER DEFAULT 0,
                checks_used INTEGER DEFAULT 0, banned INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS keys (
                key TEXT PRIMARY KEY, tier TEXT, max_uses INTEGER,
                uses INTEGER DEFAULT 0, expires_at INTEGER, owner_id INTEGER,
                created_by INTEGER, created_at INTEGER, revoked INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS usage_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER,
                kind TEXT, device_id TEXT, ts INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_usage_tg ON usage_log(tg_id);
            """)
            for admin_id in BOOT_ADMINS:
                c.execute("INSERT OR IGNORE INTO users(tg_id, username, first_seen, is_admin) "
                          "VALUES(?,?,?,1)", (admin_id, "boot_admin", int(time.time())))
                c.execute("UPDATE users SET is_admin=1, banned=0 WHERE tg_id=?", (admin_id,))

    def ensure_user(self, tg_id, username):
        with self._lock, self._conn() as c:
            c.execute("INSERT OR IGNORE INTO users(tg_id, username, first_seen) "
                      "VALUES(?,?,?)", (tg_id, username or "", int(time.time())))
            c.execute("UPDATE users SET username=? WHERE tg_id=?", (username or "", tg_id))
            if tg_id in BOOT_ADMINS:
                c.execute("UPDATE users SET is_admin=1, banned=0 WHERE tg_id=?", (tg_id,))

    def get_user(self, tg_id):
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
            return dict(row) if row else None

    def is_admin(self, tg_id):
        if tg_id in BOOT_ADMINS:
            return True
        u = self.get_user(tg_id)
        return bool(u and u["is_admin"])

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
        if tg_id in BOOT_ADMINS:
            return True, "admin", u
        if u["banned"]:
            return False, "you are banned from this bot.", u
        if u["is_admin"]:
            return True, "admin", u
        if not u["key"]:
            return False, "no key. redeem one to use the bot.", u
        k = self.get_key(u["key"])
        if not k or k["revoked"]:
            return False, "your key is revoked or missing.", u
        if k["expires_at"] and k["expires_at"] < int(time.time()):
            return False, "your key expired.", u
        if k["max_uses"] > 0 and k["uses"] >= k["max_uses"]:
            return False, "your key is out of uses.", u
        return True, "ok", u

    def consume_use(self, tg_id):
        if tg_id in BOOT_ADMINS or self.is_admin(tg_id):
            return
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
        key = "-".join(["SHIN"] + [raw[i:i+4] for i in range(0, 20, 4)])
        exp = 0 if days <= 0 else int(time.time()) + days * 86400
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO keys(key, tier, max_uses, expires_at, "
                      "created_by, created_at) VALUES(?,?,?,?,?,?)",
                      (key, tier, max_uses, exp, created_by, int(time.time())))
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
            if k["owner_id"] and k["owner_id"] != tg_id:
                return False, "key already owned by someone else."
            if k["expires_at"] and k["expires_at"] < int(time.time()):
                return False, "key expired."
            if k["max_uses"] > 0 and k["uses"] >= k["max_uses"]:
                return False, "key out of uses."
            c.execute("UPDATE keys SET owner_id=? WHERE key=?", (tg_id, key))
            c.execute("UPDATE users SET key=? WHERE tg_id=?", (key, tg_id))
            return True, f"redeemed ({k['tier']})."

    def revoke_key(self, key):
        with self._lock, self._conn() as c:
            cur = c.execute("UPDATE keys SET revoked=1 WHERE key=?",
                            (key.strip().upper(),))
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
    INT_POS = 0; INT_NEG = 1; FLOAT = 2; DOUBLE = 3
    STRING = 4; LIST = 5; DICT = 6; STRUCT_BEGIN = 7; STRUCT_END = 8

class SdpError(Exception):
    pass


def _sdp_sort_key(kv):
    k = kv[0]
    return (0, k) if isinstance(k, int) else (1, str(k))


class SdpStruct(dict):
    def __init__(self, data=None):
        super().__init__()
        self.data = b''
        self.offset = 0
        if isinstance(data, bytes):
            self.data = data
            self._unpack_from_binary()
        elif data is not None:
            super().update(data)
            self._pack_to_binary()

    def _pack_to_binary(self):
        self.data = bytes([SdpType.STRUCT_BEGIN.value << 4])
        for tag, value in sorted(self.items(), key=_sdp_sort_key):
            self._pack(tag, value)
        self.data += bytes([SdpType.STRUCT_END.value << 4])

    def _unpack_from_binary(self):
        if not self.data: return
        if self.data[0] >> 4 == SdpType.STRUCT_BEGIN.value:
            self.offset = 1
        while self.offset < len(self.data):
            tag, value = self._unpack()
            if isinstance(value, SdpType) and value == SdpType.STRUCT_END:
                break
            self[tag] = value

    def _write_number(self, value):
        r = bytearray()
        while value >= 0x80:
            r.append((value & 0x7F) | 0x80); value >>= 7
        r.append(value & 0x7F)
        return bytes(r)

    def _read_number(self):
        if self.offset >= len(self.data):
            raise SdpError('varint eof')
        n = 1
        val = self.data[self.offset] & 0x7F
        while self.data[self.offset + n - 1] >= 0x80:
            if self.offset + n >= len(self.data):
                raise SdpError('varint truncated')
            val |= (self.data[self.offset + n] & 0x7F) << (7 * n)
            n += 1
        self.offset += n
        return val

    def _pack_header(self, tag, dtype):
        if tag < 15:
            self.data += bytes([(dtype.value << 4) | tag])
        else:
            self.data += bytes([(dtype.value << 4) | 15])
            self.data += self._write_number(tag)

    def _pack(self, tag, value):
        if isinstance(value, bool):
            self._pack_header(tag, SdpType.INT_POS)
            self.data += self._write_number(1 if value else 0)
        elif isinstance(value, int):
            if value < 0:
                self._pack_header(tag, SdpType.INT_NEG)
                self.data += self._write_number(-value)
            else:
                self._pack_header(tag, SdpType.INT_POS)
                self.data += self._write_number(value)
        elif isinstance(value, float):
            self._pack_header(tag, SdpType.DOUBLE)
            packed = struct.pack("<d", value)
            self.data += self._write_number(len(packed)); self.data += packed
        elif isinstance(value, (str, bytes)):
            self._pack_header(tag, SdpType.STRING)
            enc = value.encode('utf-8') if isinstance(value, str) else value
            self.data += self._write_number(len(enc)); self.data += enc
        elif isinstance(value, list):
            self._pack_header(tag, SdpType.LIST)
            self.data += self._write_number(len(value))
            for item in value: self._pack(0, item)
        elif isinstance(value, dict):
            if isinstance(value, SdpStruct):
                self._pack_header(tag, SdpType.STRUCT_BEGIN)
                for k, v in sorted(value.items(), key=_sdp_sort_key):
                    self._pack(k, v)
                self.data += bytes([SdpType.STRUCT_END.value << 4])
            else:
                self._pack_header(tag, SdpType.DICT)
                self.data += self._write_number(len(value))
                for k, v in sorted(value.items(), key=_sdp_sort_key):
                    self._pack(0, k); self._pack(0, v)
        else:
            raise SdpError(f'unsupported type {type(value)}')

    def _unpack(self):
        try:
            if self.offset >= len(self.data): return 0, None
            header = self.data[self.offset]
            tag = header & 0xF
            dtype = SdpType(header >> 4)
            self.offset += 1
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
                self.offset += length
                return tag, val
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
            if dtype == SdpType.STRUCT_END:
                return tag, SdpType.STRUCT_END
            raise SdpError('bad data type')
        except SdpError:
            raise
        except Exception:
            raise SdpError('unpack failed')


AES_KEY = bytes.fromhex('f5a193d50ade553e9835595f5cd75ddd')
AES_IV  = b'\x00' * 16


def _aes_decrypt(data):
    if not data or len(data) % 16:
        raise SdpError(f'bad cipher length {len(data) if data else 0}')
    return AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV).decrypt(data).rstrip(b'\x00')


DEVICE_ID_RE = re.compile(
    r'and_[0-9A-Za-z]{56}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
    r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
)
_LENIENT_ID_RE = re.compile(r'^(and_|ios_)[A-Za-z0-9_\-]{32,}$')

LOGIN_HOST     = 'login.ml.youngjoygame.com'
LOGIN_PORT     = 30021
CLIENT_VERSION = '2.2.16.1232.1'
CHANNEL        = 'and_usa'

BAN_REASONS = {
    '21': 'Using Plug-in Apps to Compromise Competitive Fairness',
    '22': 'Account sharing / boosting',
    '23': 'Verbal abuse / toxic behavior',
    '24': 'Cheating in ranked',
    '25': 'Payment fraud / chargeback',
    '26': 'Account trading / sale',
}
DEFAULT_BAN_REASON = BAN_REASONS['21']

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

AFFINITY_MAP = {0: "None", 1: "Bronze", 2: "Silver", 3: "Gold",
                4: "Platinum", 5: "Diamond"}
MODE_MAP = {0: "Classic", 1: "Ranked", 2: "Brawl", 3: "AI",
            4: "Custom", 5: "Mayhem", 6: "Arcade"}
_SKIN_TIER_NAMES = {
    6: "Supreme Skins", 5: "Grand Skins", 4: "Exquisite Skins",
    3: "Deluxe Skins", 2: "Exceptional Skins", 1: "Common Skins",
}
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
    tbl = [(0,4,"Warrior III"),(5,9,"Warrior II"),(10,14,"Warrior I"),
           (15,19,"Elite IV"),(20,24,"Elite III"),(25,29,"Elite II"),(30,34,"Elite I"),
           (35,39,"Master IV"),(40,44,"Master III"),(45,49,"Master II"),(50,54,"Master I"),
           (55,59,"Grandmaster IV"),(60,64,"Grandmaster III"),(65,69,"Grandmaster II"),(70,74,"Grandmaster I"),
           (75,81,"Epic IV"),(82,88,"Epic III"),(89,95,"Epic II"),(96,107,"Epic I"),
           (108,114,"Legend IV"),(115,121,"Legend III"),(122,128,"Legend II"),(129,135,"Legend I")]
    for lo, hi, name in tbl:
        if lo <= p <= hi: return name
    if 136 <= p <= 160: return f"Mythic {p-135}"
    if 161 <= p <= 195: return f"Mythical Honor {p-135}"
    if 196 <= p <= 235: return f"Mythical Glory {p-195}"
    if p >= 236: return f"Mythical Immortal {p-235}"
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
    try: return int(v)
    except (TypeError, ValueError): return default

def _coerce_int(v):
    if isinstance(v, bool): return None
    if isinstance(v, int): return v
    if isinstance(v, float): return int(v)
    if isinstance(v, str):
        s = v.strip()
        if not s: return None
        try: return int(s)
        except (ValueError, TypeError): return None
    return None


def fmt_ts(ts):
    if not isinstance(ts, int) or ts < 100000000: return None
    try:
        utc = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        pht = utc + datetime.timedelta(hours=8)
        return pht.strftime("%Y-%m-%d %H:%M")
    except Exception: return None

def fmt_ts_full(ts):
    if not isinstance(ts, int) or ts < 100000000: return None
    try:
        utc = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        pht = utc + datetime.timedelta(hours=8)
        return pht.strftime("%Y-%m-%d %H:%M:%S")
    except Exception: return None

def human_dur(sec):
    sec = float(max(0, sec))
    if sec < 1: return f'{int(sec*1000)}ms'
    if sec < 60: return f'{sec:.1f}s'
    m, s = divmod(int(sec), 60)
    if m < 60: return f'{m}m{s:02d}s'
    h, rem = divmod(m, 60)
    return f'{h}h{rem:02d}m'

def human_ago(sec):
    sec = int(max(0, sec))
    if sec < 60: return f"{sec}s"
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d: return f"{d}d {h}h {m}m"
    if h: return f"{h}h {m}m"
    return f"{m}m"

def progress_bar(done, total, width=12):
    if total <= 0: return "░" * width
    filled = int(width * done / total)
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


# ─── ban inspection ────────────────────────────────────────────────

def _walk_ban(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == 'ban_reason':
                code = str(v)
                out['ban_reason'] = v
                out['ban_code'] = code
                out['reason_name'] = BAN_REASONS.get(code, DEFAULT_BAN_REASON)
            elif k in ('endtime_day', 'endtime_hour', 'endtime_min', 'endtime_sec'):
                out[k] = v
            elif isinstance(k, str) and 'ban' in k.lower():
                out[str(k)] = v
            if isinstance(v, (dict, list)): _walk_ban(v, out)
    elif isinstance(obj, list):
        for item in obj: _walk_ban(item, out)

def inspect_ban(sdp):
    info = {}
    if sdp: _walk_ban(dict(sdp), info)
    banned = (info.get('endtime_day') is not None) or ('ban_reason' in info)
    return banned, info

def format_ban_duration(info):
    day = info.get('endtime_day')
    if day is None: return None
    h = as_int(info.get('endtime_hour', 0))
    m = as_int(info.get('endtime_min', 0))
    s = as_int(info.get('endtime_sec', 0))
    return f"day {day}, {h:02d}:{m:02d}:{s:02d}"


# ─── SKIN BREAKDOWN ────────────────────────────────────────────────

def _parse_skin_breakdown(t118):
    if not t118 or not isinstance(t118, dict): return None
    skin_data = None
    for k in (4, '4'):
        v = t118.get(k)
        if isinstance(v, dict) and v:
            skin_data = v; break
    if skin_data is None: skin_data = t118
    if not isinstance(skin_data, dict): return None
    skin_types = {6:"Supreme Skins", 5:"Grand Skins", 4:"Exquisite Skins",
                  3:"Deluxe Skins", 2:"Exceptional Skins", 1:"Common Skins"}
    hits = {}
    for sid, cnt in skin_data.items():
        try: si = int(sid)
        except (ValueError, TypeError): continue
        if si in skin_types:
            try: hits[skin_types[si]] = int(cnt)
            except (ValueError, TypeError): hits[skin_types[si]] = cnt
    if not hits: return None
    out = {v: 0 for v in skin_types.values()}
    out.update(hits)
    return out

def _find_skin_breakdown(ri, p, skin_count=0):
    empty = {v: 0 for v in _SKIN_TIER_NAMES.values()}
    for src in (ri, p):
        if not isinstance(src, dict): continue
        t118 = src.get(118)
        if not isinstance(t118, dict) or not t118: continue
        r = _parse_skin_breakdown(t118)
        if r is not None: return r
    return empty

def _find_collector_points(p, ri):
    for src in (p, ri):
        if not isinstance(src, dict): continue
        t = src.get(136)
        if isinstance(t, dict):
            v = _coerce_int(t.get(9))
            if v is not None and v > 0: return v
    return 0


# ═══════════════════════════════════════════════════════════════════
# GAME CONNECTION
# ═══════════════════════════════════════════════════════════════════

class GameConn:
    def __init__(self, device_id):
        self.device_id = device_id
        self.host = LOGIN_HOST
        self.port = LOGIN_PORT
        self.sequence = 1
        self.socket = None
        self.queue = b''
        self.last_header_size = 0
        self._proxy_used = None
        self._closed = False

        parts = device_id.split('_')
        if len(parts) >= 2:
            info = parts[1]
            if len(parts) >= 3 and len(info) < 32:
                info = info + '_' + parts[2]
            if len(info) >= 32:
                self.imei_md5 = info[:32]
                self.android_id = info[32:48] if len(info) >= 48 else ''
                self.advertising_id = info[48:] if len(info) > 48 else ''
            else:
                self.imei_md5 = info
                self.android_id = ''
                self.advertising_id = ''
        else:
            self.imei_md5 = device_id
            self.android_id = ''
            self.advertising_id = ''

        self.account_id = 0
        self.session_key = ''
        self.zone_id = 0
        self.game_host = ''
        self.game_port = 0
        self.creation_ts = 0
        self.ban_seen = False
        self.ban_info = {}

    def _sniff(self, res):
        if self.ban_seen or res is None: return
        banned, info = inspect_ban(res)
        if banned:
            self.ban_seen = True
            self.ban_info = dict(info)

    def connect(self, host=None, port=None, proxy=None):
        if self._closed:
            raise ConnectionError("connection closed by cancel")
        if host: self.host = host
        if port: self.port = port
        if proxy:
            self.socket = _make_proxied_socket(proxy, self.host, self.port, SOCKET_TIMEOUT)
            self._proxy_used = proxy
        else:
            self._proxy_used = None
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(SOCKET_TIMEOUT)
            self.socket.connect((self.host, self.port))

    def hard_close(self):
        """Force any blocked recv() to return immediately.

        shutdown(SHUT_RDWR) wakes a peer blocked in recv() on Linux;
        plain close() does not. This is what /stop needs.
        """
        self._closed = True
        s = self.socket
        self.socket = None
        if s is not None:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                s.close()
            except Exception:
                pass
        self.sequence = 1
        self.queue = b''

    def close(self):
        self.hard_close()

    def send(self, pid, sdp):
        if self._closed:
            raise ConnectionError("connection closed by cancel")
        packet = SdpStruct({0: pid, 1: self.sequence, 5: sdp.data}).data
        buf = zstd.compress(packet)
        size = len(buf) + 4
        if size > 0xFFFFFF: raise SdpError('packet too large')
        flags = size | (16 << 24)
        self.socket.send(flags.to_bytes(4, 'big') + buf)
        self.sequence += 1

    def recv(self):
        try:
            while len(self.queue) < 4:
                if self._closed: return None, None
                d = self.socket.recv(4096)
                if not d: return None, None
                self.queue += d
            flags = int.from_bytes(self.queue[:4], 'big')
            size = flags & 0xFFFFFF
            ctype = flags >> 24
            self.last_header_size = size
            while len(self.queue) < size:
                if self._closed: return None, None
                d = self.socket.recv(4096)
                if not d: return None, None
                self.queue += d
            data = self.queue[4:size]
            self.queue = self.queue[size:]
            if ctype == 1: data = zlib.decompress(data)
            elif ctype == 16: data = zstd.decompress(data)
            elif ctype == 2: data = _aes_decrypt(data)
            elif ctype == 3: data = zlib.decompress(_aes_decrypt(data))
            elif ctype == 18: data = zstd.decompress(_aes_decrypt(data))
            result = SdpStruct(data)
            pid = result[0] if 0 in result else None
            self._sniff(result)
            if pid is None: return None, None
            body = result.get(6) or result.get(5)
            if isinstance(body, bytes):
                parsed = SdpStruct(body)
                self._sniff(parsed)
                return pid, parsed
            if isinstance(body, (dict, SdpStruct)):
                self._sniff(body)
                return pid, SdpStruct(body)
            return pid, None
        except socket.timeout: return -1, None
        except Exception: return None, None

    def login(self, proxy=None):
        try:
            self.connect(LOGIN_HOST, LOGIN_PORT, proxy=proxy)
        except (ConnectionError, socket.error, OSError) as e:
            log.debug("login connect failed: %s", e)
            return False, None, "connect_failed"
        try:
            self.send(1, SdpStruct({
                0: self.device_id,
                1: f'gps_adid={self.advertising_id}&android_id={self.android_id}&device_unique_id={self.imei_md5}',
                2: CLIENT_VERSION, 3: CHANNEL, 4: 'en',
            }))
        except Exception as e:
            log.debug("login send failed: %s", e)
            return False, None, "connect_failed"
        pid, res = self.recv()
        if pid is None: return False, None, "dropped"
        if pid == -1: return False, None, "timeout"
        if pid != 2 or not res: return False, res, f"pid_{pid}"
        self.account_id = res.get(0)
        self.session_key = res.get(1)
        zd = res.get(2)
        if isinstance(zd, dict): self.zone_id = zd.get(0, 0)
        elif isinstance(zd, list) and zd:
            self.zone_id = zd[0] if not isinstance(zd[0], dict) else zd[0].get(0, 0)
        else: self.zone_id = zd or 0
        self.creation_ts = res.get(19, 0)
        return True, res, "ok"

    def get_server(self):
        self.send(5, SdpStruct({0: self.account_id, 1: self.session_key,
                                2: CLIENT_VERSION, 5: self.zone_id, 6: CHANNEL}))
        pid, res = self.recv()
        if pid == 6 and res:
            addr = res[1]
            self.game_host, port_str = addr.split(':')
            self.game_port = int(port_str)
            return True, res
        return False, res

    def enter_game(self, proxy=None):
        p = proxy if proxy is not None else self._proxy_used
        self.close()
        # Re-arm for game-server connect — the previous close was intentional.
        self._closed = False
        self.connect(self.game_host, self.game_port, proxy=p)
        self.send(10001, SdpStruct({0: self.account_id, 1: self.session_key,
                                    2: self.zone_id, 4: CLIENT_VERSION,
                                    13: CHANNEL, 15: self.device_id}))
        self.send(10101, SdpStruct({0: 0, 2: 2}))

    def handshake(self, cancel=None, deadline_s=20):
        """Full enter-game handshake.

        Sequence: 10002 (server ready) -> we send 10003 -> 10004 or 10008
        (auth ok). Cancellable between every recv.
        """
        role_req = False
        deadline = time.time() + deadline_s
        while time.time() < deadline:
            if self._closed:
                return False
            if cancel is not None and cancel.is_set():
                return False
            pid, _ = self.recv()
            if pid is None or pid == -1:
                return False
            if pid == 20001:
                continue
            if pid == 10002 and not role_req:
                self.send(10003, SdpStruct({
                    0: self.account_id, 1: self.session_key,
                    2: self.zone_id,    3: CLIENT_VERSION,
                    4: CHANNEL,         5: self.device_id,
                }))
                role_req = True
            elif pid in (10004, 10008):
                return True
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
            if self._closed: break
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
            if self._closed: return None
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
            if self._closed: return None
            self.send(10208, SdpStruct({0: int(role_id), 1: int(zone_id)}))
            timeouts = 0
            while timeouts < 2:
                pid, res = self.recv()
                if pid is None: break
                if pid == -1: timeouts += 1
                elif pid == 10208 and res: return {'_src': 10208, '_data': dict(res)}
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


# ─── last-match ────────────────────────────────────────────────────

def _find_last_match_ts(entry):
    if not isinstance(entry, dict): return 0
    now = int(time.time())
    lower = now - 3 * 365 * 86400
    upper = now + 2 * 86400
    def _norm(v):
        if not isinstance(v, int) or isinstance(v, bool): return None
        if v > 10**15: v //= 1_000_000
        elif v > 10**12: v //= 1_000
        return v if lower <= v <= upper else None
    for key in (9,10,6,7,8,11,12,13,14,15,16,17,18):
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
        if isinstance(c, list) and c and isinstance(c[0], dict):
            entry = c[0]; break
        if isinstance(c, dict):
            for k in sorted(c.keys(), key=lambda x: (isinstance(x, str), x)):
                if isinstance(c[k], dict): entry = c[k]; break
            if entry: break
    if not entry: return None
    hero_id = entry.get(0, 0) or entry.get(1, 0) or 0
    hero = HERO_ID_MAP.get(hero_id, f"Hero#{hero_id}") if hero_id else "?"
    kills = as_int(entry.get(2, 0))
    deaths = as_int(entry.get(3, 0))
    assists = as_int(entry.get(4, 0))
    duration = as_int(entry.get(5, 0))
    result_raw = entry.get(6, entry.get(7, 0))
    mode_raw = entry.get(8, 0)
    ts = _find_last_match_ts(entry)
    mvp = entry.get(11, 0)
    gold = as_int(entry.get(12, 0))
    if result_raw in (1, '1', 'win', 'WIN', True): result = "WIN"
    elif result_raw in (0, '0', 'loss', 'LOSS', False): result = "LOSS"
    else: result = "?"
    mode = MODE_MAP.get(as_int(mode_raw, -1), f"Mode {mode_raw}")
    return {'hero': hero, 'kills': kills, 'deaths': deaths, 'assists': assists,
            'kda': f"{kills}/{deaths}/{assists}",
            'kda_ratio': round((kills + assists) / max(deaths, 1), 2),
            'result': result, 'mode': mode,
            'duration': human_dur(duration) if duration else "?",
            'gold': gold, 'mvp': bool(mvp),
            'played_at': fmt_ts(ts), 'played_ts': ts}


def extract_player(result, role_info=None, creation_ts=0, v2l=None):
    if not result or not result.get(0) or len(result[0]) == 0: return None
    try:
        p = result[0][0]
        ri = role_info if isinstance(role_info, dict) else {}
        nickname = p.get(2, "") or "Unknown"
        player_id = p.get(0, 0)
        server = p.get(1, 0)
        level = as_int(p.get(3, 0))
        skin_count = as_int(p.get(83, 0))
        hero_count = as_int(p.get(4, 0))
        matches = as_int(p.get(17, 0))
        rating_score = as_int(p.get(9, 0))
        if ri:
            hero_count = as_int(ri.get(9, hero_count)) or hero_count
            matches = as_int(ri.get(22, matches)) or matches
        loc = p.get(71)
        location = ", ".join(str(x) for x in loc if x) if isinstance(loc, list) and len(loc) >= 2 else None
        last_login_ts = as_int(p.get(5, 0))
        last_login = fmt_ts(last_login_ts)
        last_country = p.get(87, "")
        reg_country = p.get(97, "")
        squad_icon = p.get(31, "")
        squad_name = str(p.get(30, "")).replace("`", "").strip()
        squad = f"{squad_icon} {squad_name}".strip() if squad_name else None
        rank_now = map_rank(p.get(8))
        rank_top = map_rank(p.get(95))
        ach = p.get(7, 0)
        coll_pts = _find_collector_points(p, ri)
        t91 = p.get(91, [])
        history = [HERO_ID_MAP.get(h, f"Hero#{h}") for h in reversed(t91)] if t91 else []
        v2l_status = None
        if v2l and isinstance(v2l, dict):
            src = v2l.get("_src", 0)
            data = v2l.get("_data", {})
            probes = (10, 11) if src == 10208 else (0, 2, 3, 5)
            for tag in probes:
                v = data.get(tag)
                if v is not None:
                    try:
                        v2l_status = "Enabled" if int(v) > 0 else "Disabled"; break
                    except (ValueError, TypeError): pass
        followers = ri.get(23, 0) or p.get(15, 0) or 0
        likes = 0; motto = None
        ri24 = ri.get(24)
        if isinstance(ri24, int): likes = ri24
        elif isinstance(ri24, str) and ri24.strip(): motto = ri24.strip()
        if not likes: likes = p.get(61, 0) or 0
        credits = None
        cs = ri.get(20, 0) or p.get(80, 0)
        if isinstance(cs, int) and cs > 0: credits = f"{cs}/110"
        restriction = None
        t117 = ri.get(117, p.get(117))
        if t117 is not None:
            raw = t117.get(0, 0) if isinstance(t117, dict) else (t117 if isinstance(t117, int) else 0)
            pctv = round(((int(raw) + 1) / 7) * 100, 1)
            if pctv < 30: restriction = f"{pctv}% (Low Risk)"
            elif pctv < 60: restriction = f"{pctv}% (Medium Risk)"
            else: restriction = f"{pctv}% (High Risk)"
        affinity = None
        names = [e.get(2, "") for e in (ri.get(82, []) or [])
                 if isinstance(e, dict) and isinstance(e.get(2, ""), str) and e.get(2, "")]
        if names: affinity = ", ".join(names)
        else:
            t135 = p.get(135, {})
            aff_lvl = t135.get(1, 0) if isinstance(t135, dict) else 0
            if aff_lvl: affinity = AFFINITY_MAP.get(aff_lvl, f"Level {aff_lvl}")
        sl_exp = 0
        for tag in (21, 47, 50):
            v = ri.get(tag) or p.get(tag) or 0
            if isinstance(v, int) and v > 1700000000: sl_exp = v; break
        starlight = "Yes" if sl_exp > time.time() else "No"
        starlight_expiry = fmt_ts(sl_exp) if sl_exp else None
        starlight_months = p.get(60, 0) or 0
        tickets = ri.get(49, 0) or p.get(49, 0) or 0
        total_wins = p.get(18, 0) or 0
        min_ts = 1451577600
        fallback = p.get(6, 0)
        if creation_ts and creation_ts >= min_ts:
            created = fmt_ts_full(creation_ts); age_ts = creation_ts
        elif fallback and fallback >= min_ts:
            created = fmt_ts_full(fallback); age_ts = fallback
        else: created = None; age_ts = 0
        age = None
        if age_ts:
            now = datetime.datetime.now(datetime.timezone.utc)
            start = datetime.datetime.fromtimestamp(age_ts, datetime.timezone.utc)
            d = (now - start).days
            y, m, r = d // 365, (d % 365) // 30, d % 30
            age = f"{y}y {m}m {r}d" if y else (f"{m}m {r}d" if m else f"{d}d")
        win_count = ri.get(22, 0) or 0
        total_battles = ri.get(77, 0) or p.get(17, 0) or 0
        win_rate = None
        wfr = win_count if win_count else total_wins
        if total_battles > 0 and wfr > 0:
            wr = (wfr / total_battles) * 100
            win_rate = f"~{min(wr, 100):.1f}%" if wr > 100 else f"{wr:.1f}%"
        diamonds = 0; bp = 0
        cur = ri.get(111)
        if isinstance(cur, dict): diamonds = as_int(cur.get(0, 0)); bp = as_int(cur.get(1, 0))
        elif isinstance(cur, int): diamonds = cur
        elif isinstance(cur, list) and cur:
            diamonds = as_int(cur[0]) if len(cur) > 0 else 0
            bp = as_int(cur[1]) if len(cur) > 1 else 0
        if not bp: bp = int(p.get(83, 0) or 0)
        last_dia = fmt_ts(p.get(42, 0))
        mcl_wins = ri.get(46, 0) or p.get(104, p.get(103, 0)) or 0
        skin_counts = _find_skin_breakdown(ri, p, skin_count=skin_count)
        last_heroes = []
        t45 = p.get(45, {})
        if isinstance(t45, dict) and t45:
            entries = []
            for e in t45.values():
                if isinstance(e, dict):
                    entries.append((e.get(1, 0), HERO_ID_MAP.get(e.get(0, 0), f"Hero#{e.get(0, 0)}")))
            entries.sort(key=lambda x: x[1], reverse=True)
            last_heroes = [h for _, h in entries[:8]]
        last_match = extract_last_match(ri, p)
        return {
            'last_match': last_match,
            'nickname': nickname, 'player_id': player_id, 'server': server,
            'level': level, 'skin_count': skin_count, 'hero_count': hero_count,
            'matches': matches, 'rating_score': rating_score,
            'location': location,
            'last_login': last_login, 'last_login_ts': last_login_ts,
            'last_login_country': last_country or None,
            'region_country': reg_country or None,
            'high_rank': rank_top, 'current_rank': rank_now,
            'achievement_points': ach,
            'collector_point': coll_pts, 'collector_tier': map_collector(coll_pts),
            'hero_history': history, 'squad': squad,
            'affinity': affinity, 'likes': likes,
            'credits_score': credits, 'followers': followers,
            'starlight_user': starlight, 'starlight_expiry': starlight_expiry,
            'starlight_months': starlight_months, 'restriction_flags': restriction,
            'mcl_champion_wins': mcl_wins,
            'creation_date': created, 'account_age': age,
            'total_battles': total_battles, 'win_rate': win_rate,
            'diamonds': diamonds, 'battle_points': bp, 'tickets': tickets,
            'latest_skin_id': None, 'latest_skin_date': None,
            'v2l_status': v2l_status, 'last_diamond_purchase': last_dia,
            'squad_motto': motto, 'skin_breakdown': skin_counts,
            'skin_history': [], 'emblem_levels': [],
            'last_heroes_purchase': last_heroes,
            'ban_status': 'CLEAN', 'ban_reason': None,
            'ban_code': None, 'ban_duration': None,
        }
    except Exception:
        log.exception("extract_player failed")
        return None


# ═══════════════════════════════════════════════════════════════════
# LOGIN ORCHESTRATION — cancel-aware
# ═══════════════════════════════════════════════════════════════════

def _is_valid_device_id(device_id):
    if not device_id: return False
    if DEVICE_ID_RE.fullmatch(device_id): return True
    return bool(_LENIENT_ID_RE.match(device_id))


def _open_login_with_retry(device_id, cancel=None, max_attempts=None):
    if max_attempts is None: max_attempts = max(MAX_ATTEMPTS, 4)
    if not _is_valid_device_id(device_id): return None, "invalid_id"
    if cancel is not None and cancel.is_set(): return None, "cancelled"

    proxy = PROXY_POOL.for_device()
    last_reason = "rate_limited"
    for attempt in range(max_attempts):
        if cancel is not None and cancel.is_set():
            return None, "cancelled"
        _inflight.acquire()
        conn = None
        try:
            _pace()
            if cancel is not None and cancel.is_set():
                return None, "cancelled"
            conn = GameConn(device_id)
            if cancel is not None:
                cancel.register(conn)
            ok, _res, reason = conn.login(proxy=proxy)

            if os.environ.get("SHIN_DEBUG") == "1":
                log.info("login device=%s proxy=%s reason=%s ban=%s",
                         device_id[:24], (proxy or "direct")[:40],
                         reason, conn.ban_seen)

            if conn.ban_seen:
                # Success — caller owns conn, caller unregisters in finally.
                return conn, "banned"
            if ok:
                return conn, "ok"

            # Failure path — release before retrying.
            last_reason = reason
            if cancel is not None:
                cancel.unregister(conn)
            conn.close()
            conn = None
        except Exception as e:
            log.debug("login attempt %d crashed: %s", attempt + 1, e)
            last_reason = f"error:{type(e).__name__}"
            if conn is not None:
                if cancel is not None:
                    cancel.unregister(conn)
                try: conn.close()
                except Exception: pass
        finally:
            _inflight.release()

        if cancel is not None and cancel.is_set():
            return None, "cancelled"
        # Capped, jittered backoff — no 150s stalls.
        wait = min(MAX_BACKOFF, BASE_BACKOFF * (2 ** attempt))
        wait = random.uniform(0, wait) + 0.15
        if cancel is not None:
            if cancel.wait(wait):
                return None, "cancelled"
        else:
            time.sleep(wait)

    if last_reason in ("dropped", "timeout", "connect_failed"):
        return None, "rate_limited"
    if last_reason.startswith("pid_"):
        return None, "protocol_error"
    return None, last_reason


def _login_failure_result(device_id, reason):
    return {'status': 'error', 'device_id': device_id,
            'error': reason, 'reason': reason}


def _cancelled_result(device_id):
    return {'status': 'stopped', 'device_id': device_id, 'reason': 'cancelled'}


def _is_cancelled(cancel):
    return cancel is not None and cancel.is_set()


# ═══════════════════════════════════════════════════════════════════
# HIGH-LEVEL OPERATIONS — each checks cancel between stages
# ═══════════════════════════════════════════════════════════════════

def _ban_result(conn, device_id=None):
    info = dict(conn.ban_info or {})
    return {'status': 'banned', 'device_id': device_id or conn.device_id,
            'ban_info': info, 'reason': info.get('reason_name'),
            'code': info.get('ban_code'),
            'duration': format_ban_duration(info),
            'account_id': conn.account_id, 'zone_id': conn.zone_id}


def _fetch_role_info(conn, role_id, zone, cancel=None):
    merged = None
    try:
        si = conn.skin_info(role_id, zone)
        if si and isinstance(si, dict): merged = dict(si)
    except Exception: log.exception("skin_info failed")
    if _is_cancelled(cancel): return merged
    try:
        ri = conn.role_info(role_id, zone)
        if ri and isinstance(ri, dict):
            if merged is None: merged = dict(ri)
            else:
                for k, v in ri.items():
                    if k not in merged or not merged[k]: merged[k] = v
    except Exception: log.exception("role_info failed")
    return merged


def _op_creation_check(device_id, cancel=None):
    conn, reason = _open_login_with_retry(device_id, cancel=cancel)
    if conn is None:
        if reason == "cancelled": return _cancelled_result(device_id)
        return _login_failure_result(device_id, reason)
    try:
        if reason == "banned" or conn.ban_seen:
            return _ban_result(conn, device_id)
        if _is_cancelled(cancel): return _cancelled_result(device_id)
        ts = conn.creation_ts
        if not ts or ts < 1451577600:
            return _login_failure_result(device_id, "protocol_error")
        now = datetime.datetime.now(datetime.timezone.utc)
        start = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        d = (now - start).days
        y, m, r = d // 365, (d % 365) // 30, d % 30
        age = f"{y}y {m}m {r}d" if y else (f"{m}m {r}d" if m else f"{d}d")
        return {'status': 'ok', 'device_id': device_id, 'creation_ts': ts,
                'created_at': fmt_ts_full(ts), 'age': age, 'age_days': d,
                'account_id': conn.account_id, 'zone_id': conn.zone_id}
    except Exception as e:
        return _login_failure_result(device_id, f"error:{type(e).__name__}")
    finally:
        if cancel is not None: cancel.unregister(conn)
        conn.close()


def _op_single_check(device_id, cancel=None):
    conn, reason = _open_login_with_retry(device_id, cancel=cancel)
    if conn is None:
        if reason == "cancelled":
            return _cancelled_result(device_id)
        return _login_failure_result(device_id, reason)
    try:
        # Banned already at login stage — nothing else to fetch.
        if reason == "banned" or conn.ban_seen:
            return _ban_result(conn, device_id)
        if _is_cancelled(cancel):
            return _cancelled_result(device_id)

        account_id = conn.account_id
        zone_id = conn.zone_id
        creation_ts = conn.creation_ts

        ok, _ = conn.get_server()
        if conn.ban_seen:
            return _ban_result(conn, device_id)
        if _is_cancelled(cancel):
            return _cancelled_result(device_id)
        if not ok:
            return _login_failure_result(device_id, "protocol_error")

        conn.enter_game()
        if conn.ban_seen:
            return _ban_result(conn, device_id)
        if _is_cancelled(cancel):
            return _cancelled_result(device_id)

        # Full protocol handshake — sends 10003, waits for 10004/10008.
        if not conn.handshake(cancel=cancel):
            if conn.ban_seen:
                return _ban_result(conn, device_id)
            return _login_failure_result(device_id, "protocol_error")
        if _is_cancelled(cancel):
            return _cancelled_result(device_id)

        result = conn.lookup(int(account_id), 'id')
        if conn.ban_seen:
            # Try to still parse whatever we have, then bail.
            partial = None
            try:
                partial = extract_player(result or SdpStruct({}), None, creation_ts, None)
                if partial:
                    partial['_device'] = device_id
                    partial['_account'] = account_id
                    partial['_zone'] = zone_id
            except Exception:
                partial = None
            out = _ban_result(conn, device_id)
            if partial:
                out['player'] = partial
            return out
        if _is_cancelled(cancel):
            return _cancelled_result(device_id)
        if not result:
            return _login_failure_result(device_id, "protocol_error")

        role_id, zone = account_id, zone_id
        if result.get(0) and len(result[0]) > 0:
            first = result[0][0]
            if isinstance(first, dict):
                role_id = first.get(0, account_id)
                zone = first.get(1, zone_id)

        role_info = _fetch_role_info(conn, role_id, zone, cancel=cancel)
        if _is_cancelled(cancel):
            return _cancelled_result(device_id)

        v2l = None
        try:
            v2l = conn.v2l_status(role_id, zone)
        except Exception:
            pass
        if _is_cancelled(cancel):
            return _cancelled_result(device_id)

        player = extract_player(result, role_info, creation_ts, v2l)
        if not player:
            if conn.ban_seen:
                return _ban_result(conn, device_id)
            return _login_failure_result(device_id, "extract_failed")
        player['_device'] = device_id
        player['_account'] = account_id
        player['_zone'] = zone_id

        if conn.ban_seen:
            # Ban info merges into the result — same player payload, tagged.
            out = _ban_result(conn, device_id)
            out['player'] = player
            return out
        return {'status': 'success', 'player': player,
                'device_id': device_id,
                'account_id': account_id, 'zone_id': zone_id}
    except Exception as e:
        if conn.ban_seen:
            return _ban_result(conn, device_id)
        if _is_cancelled(cancel):
            return _cancelled_result(device_id)
        return _login_failure_result(device_id, f"error:{type(e).__name__}")
    finally:
        if cancel is not None:
            cancel.unregister(conn)
        conn.close()


def _op_probe(device_id, cancel=None):
    conn, reason = _open_login_with_retry(device_id, cancel=cancel)
    if conn is None:
        if reason == "cancelled": return _cancelled_result(device_id)
        return _login_failure_result(device_id, reason)
    try:
        if reason == "banned" or conn.ban_seen:
            return _ban_result(conn, device_id)
        if _is_cancelled(cancel): return _cancelled_result(device_id)

        ok, _ = conn.get_server()
        if conn.ban_seen: return _ban_result(conn, device_id)
        if _is_cancelled(cancel): return _cancelled_result(device_id)
        if not ok: return _login_failure_result(device_id, "protocol_error")

        conn.enter_game()
        if conn.ban_seen: return _ban_result(conn, device_id)
        if _is_cancelled(cancel): return _cancelled_result(device_id)

        if not conn.handshake(cancel=cancel):
            if conn.ban_seen: return _ban_result(conn, device_id)
            return _login_failure_result(device_id, "protocol_error")
        if _is_cancelled(cancel): return _cancelled_result(device_id)

        result = conn.lookup(int(conn.account_id), 'id')
        if conn.ban_seen: return _ban_result(conn, device_id)
        if _is_cancelled(cancel): return _cancelled_result(device_id)
        if not result: return _login_failure_result(device_id, "protocol_error")

        role_id, zone = conn.account_id, conn.zone_id
        if result.get(0) and len(result[0]) > 0:
            first = result[0][0]
            if isinstance(first, dict):
                role_id = first.get(0, conn.account_id)
                zone = first.get(1, conn.zone_id)

        role_info = _fetch_role_info(conn, role_id, zone, cancel=cancel)
        if _is_cancelled(cancel): return _cancelled_result(device_id)

        player = extract_player(result, role_info, conn.creation_ts, None)
        if not player: return _login_failure_result(device_id, "extract_failed")
        player['_device'] = device_id
        player['_account'] = conn.account_id
        player['_zone'] = conn.zone_id

        if conn.ban_seen:
            out = _ban_result(conn, device_id); out['player'] = player; return out
        return {'status': 'clean', 'device_id': device_id, 'player': player,
                'account_id': conn.account_id, 'zone_id': conn.zone_id}
    except Exception as e:
        if conn.ban_seen: return _ban_result(conn, device_id)
        if _is_cancelled(cancel): return _cancelled_result(device_id)
        return _login_failure_result(device_id, f"error:{type(e).__name__}")
    finally:
        if cancel is not None: cancel.unregister(conn)
        conn.close()


def with_retries(fn, *args, retries=RETRIES, cancel=None, **kwargs):
    last = None
    for attempt in range(retries):
        if _is_cancelled(cancel):
            return {'status': 'stopped', 'reason': 'cancelled'}
        try:
            r = fn(*args, cancel=cancel, **kwargs)
        except Exception as e:
            r = {'status': 'error', 'error': f'{type(e).__name__}: {e}'}
        if r and r.get('status') in ('success', 'ok', 'clean', 'banned'):
            return r
        if r and r.get('status') == 'stopped':
            return r
        if r and r.get('reason') in ('invalid_id', 'cancelled'):
            return r
        last = r
        if attempt < retries - 1:
            # Capped, jittered — no 150s stalls.
            wait = min(MAX_BACKOFF, BASE_BACKOFF * (attempt + 1))
            wait = random.uniform(0, wait) + 0.15
            if cancel is not None:
                if cancel.wait(wait):
                    return {'status': 'stopped', 'reason': 'cancelled'}
            else:
                time.sleep(wait)
    return last if last is not None else {'status': 'error', 'error': 'all retries failed'}


# ═══════════════════════════════════════════════════════════════════
# RENDER HELPERS
# ═══════════════════════════════════════════════════════════════════

def esc(s):
    return html.escape(str(s if s is not None else "—"))

def fv(val, fallback='—'):
    if val is None: return fallback
    if isinstance(val, str):
        return val if val.strip() and val.strip().upper() != 'N/A' else fallback
    return val

async def safe_edit(target, text, **kwargs):
    edit = getattr(target, "edit_message_text", None) or getattr(target, "edit_text", None)
    if edit is None: return
    try: await edit(text, **kwargs)
    except BadRequest as e:
        if "not modified" not in str(e).lower(): raise

def _last_login_line(p):
    ll_ts = p.get('last_login_ts') or 0
    ll_str = p.get('last_login')
    if ll_str and ll_ts:
        return f"{ll_str} ({human_ago(time.time() - ll_ts)} ago) PHT"
    if ll_str: return f"{ll_str} PHT"
    return "N/A"

def _reason_to_message(reason):
    return {
        'invalid_id': "❌ <b>INVALID DEVICE ID</b> — format is not a valid ML ID.",
        'rate_limited': ("⏳ <b>RATE LIMITED</b> — every attempt was throttled.\n"
                         "Add more proxies to <code>proxies.txt</code> "
                         "or lower <code>SHIN_MAX_INFLIGHT</code>."),
        'timeout': "⏱ <b>TIMEOUT</b> — server did not respond in time.",
        'protocol_error': "🔌 <b>PROTOCOL ERROR</b> — server rejected the handshake.",
        'extract_failed': "⚠️ <b>EXTRACT FAILED</b> — profile response was malformed.",
        'cancelled': "⏹ <b>STOPPED</b> — cancelled mid-flight.",
    }.get(reason, f"⚠️ error: <code>{esc(reason)}</code>")


def render_profile_text(p, device_id="", ban_info=None):
    sb = p.get('skin_breakdown') or {}
    dev = device_id or p.get('_device', '')
    lines = []

    # Ban banner — merged into single check output.
    banned = (ban_info and (ban_info.get('ban_reason') is not None
                            or ban_info.get('endtime_day') is not None)) \
             or p.get('ban_status') == 'BANNED'
    if banned:
        info = ban_info or {}
        reason = info.get('reason_name') or p.get('ban_reason') or DEFAULT_BAN_REASON
        code = info.get('ban_code') if info.get('ban_code') is not None else p.get('ban_code')
        dur = p.get('ban_duration')
        if info:
            dur = format_ban_duration(info) or dur
        lines.append("⛔ BANNED ACCOUNT")
        lines.append(f"reason   : {reason}")
        lines.append(f"code     : {code if code is not None else '—'}")
        if dur:
            lines.append(f"duration : {dur}")
        lines.append("")
    else:
        lines.append("✅ CLEAN — PLAYER PROFILE")
    if dev:
        lines.append(dev)
    lines.append("")
    lines.append("")
    lines.append(f"Name: {fv(p.get('nickname'), 'Unknown')}")
    lines.append(f"Account ID: {p.get('_account', 0)}   Zone: {p.get('_zone', 0)}")
    lines.append(f"Level: {p.get('level', 0)}   Country: {fv(p.get('region_country'), '—')}")
    lines.append(f"Age: {fv(p.get('account_age'), '—')}   Created: {fv(p.get('creation_date'), '—')}")
    lines.append("")
    lines.append(f"Current Rank: {fv(p.get('current_rank'), 'Unranked')}")
    lines.append(f"Highest Rank: {fv(p.get('high_rank'), 'Unranked')}")
    lines.append(f"Win Rate: {fv(p.get('win_rate'), 'N/A')}   Battles: {p.get('total_battles', 0)}")
    lines.append("")
    lines.append(f"Heroes: {p.get('hero_count', 0)}   Skins: {p.get('skin_count', 0)}")
    lines.append(f"Collector: {fv(p.get('collector_tier'), 'No Tier')} ({p.get('collector_point', 0)} pts)")
    lines.append(f"Followers: {p.get('followers', 0)}   Likes: {p.get('likes', 0)}")
    lines.append(f"Restriction: {fv(p.get('restriction_flags'), 'None')}")
    lines.append(f"V2L: {fv(p.get('v2l_status'), 'N/A')}   Starlight: {fv(p.get('starlight_user'), 'No')}")
    lines.append("")
    lines.append("Skin Breakdown")
    lines.append(f"  Supreme    : {sb.get('Supreme Skins', 0)}")
    lines.append(f"  Grand      : {sb.get('Grand Skins', 0)}")
    lines.append(f"  Exquisite  : {sb.get('Exquisite Skins', 0)}")
    lines.append(f"  Deluxe     : {sb.get('Deluxe Skins', 0)}")
    lines.append(f"  Exceptional: {sb.get('Exceptional Skins', 0)}")
    lines.append(f"  Common     : {sb.get('Common Skins', 0)}")
    lines.append("")
    lines.append(f"Last Login: {_last_login_line(p)}")
    lines.append(f"Location: {fv(p.get('location'), 'NOT FOUND')}")
    heroes = p.get('hero_history') or []
    if heroes:
        lines.append(f"Recent Heroes: {', '.join(heroes[:10])}")
    return "\n".join(lines)


def render_profile_telegram(p, device_id="", ban_info=None):
    return "<pre>" + render_profile_text(p, device_id, ban_info=ban_info) + "</pre>"


def render_ban_html(device_id, r):
    st = r.get('status')
    if st == 'clean':
        return (f"🚫 <b>BAN CHECK</b>\n<code>{esc(device_id)}</code>\n\n"
                f"✅ <b>CLEAN</b> — no ban record seen.\n"
                f"acc <code>{r.get('account_id',0)}</code> · zone <code>{r.get('zone_id',0)}</code>")
    if st == 'banned':
        info = r.get('ban_info') or {}
        reason = r.get('reason') or info.get('reason_name') or DEFAULT_BAN_REASON
        code = r.get('code') if r.get('code') is not None else info.get('ban_code')
        dur = r.get('duration') or format_ban_duration(info)
        lines = ["🚫 <b>BAN CHECK</b>", f"<code>{esc(device_id)}</code>", "",
                 "⛔ <b>BANNED</b>",
                 f"reason   <b>{esc(reason)}</b>",
                 f"code     <code>{esc(code) if code is not None else '—'}</code>"]
        if dur: lines.append(f"duration <code>{esc(dur)}</code>")
        return "\n".join(lines)
    reason = r.get('reason') or r.get('error') or 'unknown'
    return (f"🚫 <b>BAN CHECK</b>\n<code>{esc(device_id)}</code>\n\n"
            f"{_reason_to_message(reason)}")


def render_error_html(title, device_id, r):
    reason = r.get('reason') or r.get('error') or 'unknown'
    return (f"⚠️ <b>{esc(title)}</b>\n<code>{esc(device_id)}</code>\n\n"
            f"{_reason_to_message(reason)}")


def render_creation_html(device_id, r):
    if r.get('status') == 'ok':
        return (f"📅 <b>CREATION DATE</b>\n<code>{esc(device_id)}</code>\n\n"
                f"🗓 created <b>{esc(r.get('created_at'))}</b>\n"
                f"⏳ age <b>{esc(r.get('age'))}</b>  ({r.get('age_days',0)} days)\n"
                f"acc <code>{r.get('account_id',0)}</code> · zone <code>{r.get('zone_id',0)}</code>")
    return render_error_html("CREATION DATE", device_id, r)


def render_bulk_running(stats, elapsed, total, filename):
    done = stats.get('done', 0); clean = stats.get('clean', 0)
    banned = stats.get('banned', 0); fail = stats.get('fail', 0)
    stop = stats.get('stopped', 0)
    bar = progress_bar(done, total, width=12)
    rate = done / elapsed if elapsed > 0 else 0.0
    remaining = max(0, total - done)
    eta_s = remaining / rate if rate > 0 else 0.0
    eta = human_dur(eta_s) if eta_s > 0 else '—'
    last_dev = stats.get('last_device') or '—'
    last_st = stats.get('last_status') or '—'
    return (f"📦 <b>BULK</b> · <code>{esc(os.path.basename(filename))[:32]}</code>\n"
            f"<code>{bar}</code>  <b>{done:,}</b> / {total:,}\n"
            f"✅ clean <b>{clean:,}</b>   ⛔ banned <b>{banned:,}</b>   "
            f"⚠️ fail <b>{fail:,}</b>   ⏹ stop <b>{stop:,}</b>\n"
            f"⚡ {rate:.1f}/s · ⏱ {human_dur(elapsed)} · eta {eta}\n"
            f"last: <code>{esc(last_dev)}</code> → {esc(last_st)}")


def render_bulk_summary(stats, elapsed, path, stopped):
    total = max(stats.get('total', 0), 1)
    done = stats.get('done', 0)
    tag = "⏹ STOPPED" if stopped else "✅ DONE"
    return (f"📦 <b>BULK {tag}</b> · <code>{esc(os.path.basename(path))}</code>\n\n"
            f"processed <b>{done:,}</b> / {total:,}\n"
            f"✅ clean    <b>{stats.get('clean',0):,}</b>\n"
            f"⛔ banned   <b>{stats.get('banned',0):,}</b>\n"
            f"⚠️ failed   <b>{stats.get('fail',0):,}</b>\n"
            f"⏹ stopped  <b>{stats.get('stopped',0):,}</b>\n\n"
            f"⏱ {human_dur(elapsed)} · {done/max(elapsed,0.001):.1f}/s · "
            f"{THREADS} threads · {RETRIES} retries")


def render_bulk_entry(r):
    st = r.get('status', 'error')
    d = r.get('device_id', '')
    if st == 'clean':
        p = r.get('player') or {}
        return render_profile_text(p, d)
    if st == 'banned':
        info = r.get('ban_info') or {}
        reason = r.get('reason') or info.get('reason_name') or DEFAULT_BAN_REASON
        code = r.get('code') if r.get('code') is not None else info.get('ban_code')
        dur = r.get('duration') or format_ban_duration(info)
        lines = ["⛔ BANNED DEVICE", d, "", f"Reason: {reason}",
                 f"Code: {code if code is not None else '—'}"]
        if dur:
            lines.append(f"Duration: {dur}")
        if r.get('player'):
            lines.append("")
            lines.append(render_profile_text(r['player'], d, ban_info=info))
        return "\n".join(lines)
    if st == 'stopped':
        return f"⏹ STOPPED\n{d}\n(cancelled before completion)\n"
    reason = r.get('reason') or r.get('error') or 'unknown'
    tag = {'invalid_id': '❌ INVALID ID', 'rate_limited': '⏳ RATE LIMITED',
           'timeout': '⏱ TIMEOUT', 'protocol_error': '🔌 PROTOCOL ERROR',
           'extract_failed': '⚠️ EXTRACT FAILED'}.get(reason, '⚠️ ERROR')
    return f"{tag}\n{d}\nReason: {reason}\n"


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


def file_from_bytes(data, name):
    buf = io.BytesIO(); buf.write(data); buf.seek(0)
    return InputFile(buf, filename=name)


def file_from_lines(lines, name):
    return file_from_bytes("\n".join(lines).encode("utf-8"), name)


# ═══════════════════════════════════════════════════════════════════
# BULK SESSIONS
# ═══════════════════════════════════════════════════════════════════

BULK_SESSIONS = {}
BULK_LOCK = threading.Lock()

def _get_bulk_session(tg_id):
    with BULK_LOCK: return BULK_SESSIONS.get(tg_id)

def _set_bulk_session(tg_id, s):
    with BULK_LOCK:
        if s is None: BULK_SESSIONS.pop(tg_id, None)
        else: BULK_SESSIONS[tg_id] = s


# ═══════════════════════════════════════════════════════════════════
# MENUS
# ═══════════════════════════════════════════════════════════════════

def main_menu(is_admin=False):
    rows = [
        [InlineKeyboardButton("🔍 Single Check", callback_data="menu:single")],
        [InlineKeyboardButton("📦 Bulk Check", callback_data="menu:bulk")],
        [InlineKeyboardButton("📅 Creation Date", callback_data="menu:created")],
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
        [InlineKeyboardButton("◀️ Back", callback_data="menu:home")],
    ])


USER_STATE = {}
STATE_LOCK = threading.Lock()

def set_state(tg_id, s):
    with STATE_LOCK:
        if s is None: USER_STATE.pop(tg_id, None)
        else: USER_STATE[tg_id] = s

def get_state(tg_id):
    with STATE_LOCK: return USER_STATE.get(tg_id)

def clear_state(tg_id):
    with STATE_LOCK: USER_STATE.pop(tg_id, None)


async def on_error(update, context):
    log.error("update %r caused error", update, exc_info=context.error)


# ═══════════════════════════════════════════════════════════════════
# HANDLERS — general
# ═══════════════════════════════════════════════════════════════════

async def cmd_start(update, context):
    u = update.effective_user
    if u is None or update.message is None: return
    DBX.ensure_user(u.id, u.username or u.first_name or "")
    is_admin = DBX.is_admin(u.id)
    ok, reason, _ = DBX.auth_status(u.id)
    head = ("🥔 <b>SHIN</b> bot\n\n"
            "checks: single (profile + ban) · bulk · creation\n"
            f"threads: {THREADS} · retries: {RETRIES} · proxies: {PROXY_POOL.size()}\n\n")
    if is_admin: head += "👑 admin access confirmed\n— /admin to open the panel\n"
    elif ok: head += "🔓 key active\n"
    else: head += f"🔒 <b>{esc(reason)}</b>\n— tap Redeem Key to unlock\n"
    await update.message.reply_text(head, parse_mode=ParseMode.HTML,
                                     reply_markup=main_menu(is_admin))


async def cmd_help(update, context):
    if update.effective_user is None or update.message is None: return
    text = ("🥔 <b>SHIN</b>\n\n"
            "<b>Single Check</b> — one device id, full profile + ban + .txt.\n"
            "<b>Bulk Check</b> — reply to a .txt with device ids.\n"
            "<b>Creation Date</b> — one id, creation + age.\n"
            "<b>Redeem Key</b> — attach a key.\n\n"
            "<b>User commands</b>\n"
            "/start · /help · /menu\n"
            "/check &lt;id&gt; · /ban &lt;id&gt; (alias) · /created &lt;id&gt;\n"
            "/bulk (reply to file) · /key &lt;key&gt;\n"
            "/stop — abort a running bulk <b>immediately</b>\n"
            "/whoami · /usage\n\n"
            "<b>Admin commands</b>\n"
            "/admin · /genkey · /revoke · /broadcast\n")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=back_kb())


async def cmd_menu(update, context):
    u = update.effective_user
    if u is None or update.message is None: return
    DBX.ensure_user(u.id, u.username or "")
    await update.message.reply_text("menu:", reply_markup=main_menu(DBX.is_admin(u.id)))


async def cmd_stop(update, context):
    u = update.effective_user
    if u is None or update.message is None: return
    sess = _get_bulk_session(u.id)
    if not sess:
        await update.message.reply_text("nothing running."); return
    cancel = sess.get('cancel')
    if cancel is None or cancel.is_set():
        await update.message.reply_text("already stopping…"); return
    # hard_close + shutdown wakes every blocked recv() right now.
    cancel.set()
    sess['stop'] = True
    await update.message.reply_text(
        "⏹ <b>Stopping now.</b> All active sockets shut down; "
        "in-flight recv() unblocked. Results land in a moment.",
        parse_mode=ParseMode.HTML)


async def cmd_whoami(update, context):
    u = update.effective_user
    if u is None or update.message is None: return
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
    if u is None or update.message is None: return
    with DBX._lock, DBX._conn() as c:
        rows = c.execute("SELECT kind, COUNT(*) AS n FROM usage_log WHERE tg_id=? GROUP BY kind",
                         (u.id,)).fetchall()
    if not rows:
        await update.message.reply_text("no usage yet."); return
    txt = "\n".join(f"{r['kind']}: {r['n']}" for r in rows)
    await update.message.reply_text(f"your usage:\n{txt}")


# ═══════════════════════════════════════════════════════════════════
# HANDLERS — admin
# ═══════════════════════════════════════════════════════════════════

async def cmd_admin(update, context):
    u = update.effective_user
    if u is None or update.message is None: return
    DBX.ensure_user(u.id, u.username or "")
    if not DBX.is_admin(u.id):
        await update.message.reply_text("not admin."); return
    await update.message.reply_text(
        "👑 <b>Admin Panel</b>\n\nbuttons below, or use commands:\n"
        "/genkey &lt;tier&gt; &lt;uses&gt; &lt;days&gt;\n/revoke &lt;key&gt;\n/broadcast &lt;text&gt;",
        parse_mode=ParseMode.HTML, reply_markup=admin_menu())


async def cmd_genkey(update, context):
    u = update.effective_user
    if u is None or update.message is None: return
    DBX.ensure_user(u.id, u.username or "")
    if not DBX.is_admin(u.id):
        await update.message.reply_text("not admin."); return
    args = context.args or []
    if len(args) < 3:
        await update.message.reply_text("usage: /genkey <tier> <uses> <days>\n"
                                         "e.g. /genkey pro 1000 30\n"
                                         "tiers: trial · basic · pro · unlimited"); return
    await _do_admin_gen(update, context, " ".join(args))


async def cmd_revoke(update, context):
    u = update.effective_user
    if u is None or update.message is None: return
    DBX.ensure_user(u.id, u.username or "")
    if not DBX.is_admin(u.id):
        await update.message.reply_text("not admin."); return
    args = context.args or []
    if not args:
        await update.message.reply_text("usage: /revoke <key>"); return
    await _do_admin_revoke(update, context, " ".join(args))


async def cmd_broadcast(update, context):
    u = update.effective_user
    if u is None or update.message is None: return
    DBX.ensure_user(u.id, u.username or "")
    if not DBX.is_admin(u.id):
        await update.message.reply_text("not admin."); return
    text = " ".join(context.args or []).strip()
    if not text:
        await update.message.reply_text("usage: /broadcast <text>"); return
    await _do_admin_broadcast(update, context, text)


async def cb_bulk_stop(update, context):
    q = update.callback_query; u = update.effective_user
    if q is None or u is None: return
    sess = _get_bulk_session(u.id)
    if not sess:
        await q.answer("nothing running."); return
    cancel = sess.get('cancel')
    if cancel is None or cancel.is_set():
        await q.answer("already stopping…"); return
    cancel.set()
    sess['stop'] = True
    await q.answer("⏹ stopping now — sockets killed")
    try:
        await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⏹ stopping…", callback_data="bulk:noop")]]))
    except Exception: pass


async def cb_bulk_noop(update, context):
    q = update.callback_query
    if q is None: return
    await q.answer("stopping…")


async def cb_menu(update, context):
    q = update.callback_query; u = update.effective_user
    if q is None or u is None: return
    await q.answer()
    DBX.ensure_user(u.id, u.username or "")
    is_admin = DBX.is_admin(u.id)
    data = q.data or ""
    if data == "menu:home":
        clear_state(u.id)
        await safe_edit(q, "menu:", reply_markup=main_menu(is_admin)); return
    ok, reason, _ = DBX.auth_status(u.id)
    if data == "menu:help":
        await safe_edit(q, "🥔 <b>SHIN</b>\n\n"
                           "🔍 single check → full profile + ban + .txt\n"
                           "📦 bulk check → .txt of ids, live stats + ⏹ Stop\n"
                           "📅 creation date → account age\n"
                           "🔑 redeem key → attach a key\n",
                        parse_mode=ParseMode.HTML, reply_markup=back_kb()); return
    if not ok and data not in ("menu:redeem",):
        await safe_edit(q, f"🔒 <b>{esc(reason)}</b>",
                        parse_mode=ParseMode.HTML, reply_markup=main_menu(is_admin)); return
    if data == "menu:single":
        set_state(u.id, "single")
        await safe_edit(q, "🔍 <b>Single Check</b>\n\nsend one device id now.",
                        parse_mode=ParseMode.HTML, reply_markup=back_kb())
    elif data == "menu:bulk":
        set_state(u.id, "bulk")
        await safe_edit(q, f"📦 <b>Bulk Check</b>\n\nsend a <code>.txt</code> file with device ids, "
                           f"one per line. cap {BULK_CAP:,} per run. live stats + ⏹ Stop.\n\n"
                           f"<i>{THREADS} threads · {PROXY_POOL.size()} proxies · "
                           f"rotate every {PROXY_ROTATE_EVERY}</i>",
                        parse_mode=ParseMode.HTML, reply_markup=back_kb())
    elif data == "menu:created":
        set_state(u.id, "created")
        await safe_edit(q, "📅 <b>Creation Date</b>\n\nsend one device id now.",
                        parse_mode=ParseMode.HTML, reply_markup=back_kb())
    elif data == "menu:redeem":
        set_state(u.id, "redeem")
        await safe_edit(q, "🔑 <b>Redeem Key</b>\n\nsend the key now. format "
                           "<code>SHIN-XXXX-XXXX-XXXX-XXXX-XXXX</code>",
                        parse_mode=ParseMode.HTML, reply_markup=back_kb())
    elif data == "menu:admin":
        if not is_admin:
            await safe_edit(q, "not admin.", reply_markup=main_menu(is_admin)); return
        await safe_edit(q, "👑 <b>Admin Panel</b>",
                        parse_mode=ParseMode.HTML, reply_markup=admin_menu())


async def cb_admin(update, context):
    q = update.callback_query; u = update.effective_user
    if q is None or u is None: return
    await q.answer()
    DBX.ensure_user(u.id, u.username or "")
    if not DBX.is_admin(u.id):
        await safe_edit(q, "not admin."); return
    data = q.data
    if data == "admin:stats":
        s = DBX.stats()
        text = (f"📊 <b>Stats</b>\n\nusers <b>{s['users']:,}</b>\n"
                f"keys <b>{s['keys']:,}</b> (active {s['active']:,})\n"
                f"checks <b>{s['checks']:,}</b>\n"
                f"today <b>{s['today']:,}</b>\n"
                f"threads <b>{THREADS}</b> retries <b>{RETRIES}</b>\n"
                f"proxies <b>{PROXY_POOL.size()}</b> inflight <b>{MAX_INFLIGHT}</b>\n"
                f"rotate <b>{PROXY_ROTATE_EVERY}</b>\n")
        await safe_edit(q, text, parse_mode=ParseMode.HTML, reply_markup=admin_menu())
    elif data == "admin:users":
        users = DBX.list_users(30)
        lines = ["👥 <b>Recent Users</b>\n"]
        for r in users:
            flag = "👑" if r['is_admin'] else ("🚫" if r['banned'] else "·")
            key = r['key'] or '—'
            lines.append(f"{flag} <code>{r['tg_id']}</code> {esc(r['username'] or '')} "
                         f"· {r['checks_used']} · {esc(key[:14])}")
        await safe_edit(q, "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=admin_menu())
    elif data == "admin:keys":
        keys = DBX.list_keys(40)
        lines = ["🔑 <b>Recent Keys</b>\n"]
        for k in keys:
            rv = "🗑" if k['revoked'] else "✅"
            exp = "∞" if not k['expires_at'] else datetime.datetime.fromtimestamp(
                k['expires_at']).strftime("%m-%d")
            lines.append(f"{rv} <code>{k['key']}</code>\n"
                         f"   tier {esc(k['tier'])} · uses {k['uses']}/{k['max_uses'] or '∞'} · exp {exp}")
        await safe_edit(q, "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=admin_menu())
    elif data == "admin:gen":
        set_state(u.id, "admin_gen")
        await safe_edit(q, "➕ <b>Generate Key</b>\n\nsend tier · uses · days\n"
                           "e.g. <code>pro 1000 30</code>\n\n"
                           "tiers: trial · basic · pro · unlimited",
                        parse_mode=ParseMode.HTML, reply_markup=back_kb())
    elif data == "admin:revoke":
        set_state(u.id, "admin_revoke")
        await safe_edit(q, "🗑 <b>Revoke Key</b>\n\nsend the key to revoke.",
                        parse_mode=ParseMode.HTML, reply_markup=back_kb())
    elif data == "admin:broadcast":
        set_state(u.id, "admin_broadcast")
        await safe_edit(q, "📣 <b>Broadcast</b>\n\nsend the message text to broadcast to all non-admin users.",
                        parse_mode=ParseMode.HTML, reply_markup=back_kb())


def first_device_id(text):
    if not text: return None
    m = DEVICE_ID_RE.search(text)
    if m: return m.group(0)
    t = text.strip()
    if t.startswith(('and_', 'ios_')) and len(t) > 40: return t.split()[0]
    return None


# ═══════════════════════════════════════════════════════════════════
# HANDLERS — text / document
# ═══════════════════════════════════════════════════════════════════

async def handle_text(update, context):
    u = update.effective_user; msg = update.message
    if u is None or msg is None: return
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
        # Ban info is folded into single check now.
        await _do_single(update, context, parts[1].strip()); return
    if text.startswith("/created"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2: await msg.reply_text("usage: /created <device_id>"); return
        await _do_created(update, context, parts[1].strip()); return
    if text.startswith("/key"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2: await msg.reply_text("usage: /key <your_key>"); return
        await _do_redeem(update, context, parts[1].strip()); return
    if state == "redeem": clear_state(u.id); await _do_redeem(update, context, text); return
    if state == "single": clear_state(u.id); await _do_single(update, context, text); return
    if state == "created": clear_state(u.id); await _do_created(update, context, text); return
    if state == "admin_gen" and DBX.is_admin(u.id): clear_state(u.id); await _do_admin_gen(update, context, text); return
    if state == "admin_revoke" and DBX.is_admin(u.id): clear_state(u.id); await _do_admin_revoke(update, context, text); return
    if state == "admin_broadcast" and DBX.is_admin(u.id): clear_state(u.id); await _do_admin_broadcast(update, context, text); return
    if first_device_id(text):
        await msg.reply_text("pick a mode first, then send the id.\nor use /check · /created.",
                             reply_markup=main_menu(DBX.is_admin(u.id)))
    else:
        await msg.reply_text("menu:", reply_markup=main_menu(DBX.is_admin(u.id)))


async def handle_document(update, context):
    u = update.effective_user
    if u is None or update.message is None: return
    DBX.ensure_user(u.id, u.username or "")
    state = get_state(u.id)
    caption = (update.message.caption or "").lower()
    is_bulk = state == "bulk" or "/bulk" in caption
    ok, reason, _ = DBX.auth_status(u.id)
    if not ok and not DBX.is_admin(u.id):
        await update.message.reply_text(f"🔒 {reason}"); return
    if not is_bulk:
        await update.message.reply_text("to bulk-check, tap 📦 Bulk Check then send the file."); return
    clear_state(u.id)
    doc = update.message.document
    if not doc:
        await update.message.reply_text("no document."); return
    if doc.file_size and doc.file_size > 5 * 1024 * 1024:
        await update.message.reply_text("file too big (max 5 MB)."); return
    tg_file = await doc.get_file()
    buf = io.BytesIO()
    await tg_file.download_to_memory(buf)
    raw = buf.getvalue().decode("utf-8", errors="replace")
    ids = parse_id_file(raw)
    if not ids:
        await update.message.reply_text("no valid device ids found."); return
    if len(ids) > BULK_CAP:
        ids = ids[:BULK_CAP]
        await update.message.reply_text(f"capped at {BULK_CAP:,} ids per run.")
    await _run_bulk(update, context, ids, doc.file_name or "bulk.txt")


# ═══════════════════════════════════════════════════════════════════
# OPERATION RUNNERS
# ═══════════════════════════════════════════════════════════════════

async def _do_redeem(update, context, key):
    u = update.effective_user
    if u is None or update.message is None: return
    ok, msg = DBX.redeem_key(u.id, key)
    if ok:
        await update.message.reply_text(f"🔓 <b>key redeemed</b>\n\n{esc(msg)}",
                                        parse_mode=ParseMode.HTML,
                                        reply_markup=main_menu(DBX.is_admin(u.id)))
    else:
        await update.message.reply_text(f"❌ {esc(msg)}")


async def _do_single(update, context, raw):
    u = update.effective_user
    if u is None or update.message is None: return
    ok, reason, _ = DBX.auth_status(u.id)
    if not ok and not DBX.is_admin(u.id):
        await update.message.reply_text(f"🔒 {reason}"); return
    device_id = first_device_id(raw)
    if not device_id:
        await update.message.reply_text("no valid device id in that message."); return
    msg = await update.message.reply_text(f"🔍 checking <code>{esc(device_id[:32])}…</code>",
                                           parse_mode=ParseMode.HTML)
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(EXECUTOR,
                                          lambda: with_retries(_op_single_check, device_id))
    except Exception as e:
        res = {'status': 'error', 'error': str(e)}
    DBX.log_usage(u.id, 'single', device_id)
    if not DBX.is_admin(u.id): DBX.consume_use(u.id)
    st = res.get('status')
    if st == 'banned':
        player = res.get('player')
        if player:
            out = "<pre>" + render_profile_text(
                player, device_id, ban_info=res.get('ban_info')) + "</pre>"
            if len(out) > 4000:
                out = out[:3990] + "\n…</pre>"
            try:
                await safe_edit(msg, out, parse_mode=ParseMode.HTML)
            except Exception:
                await safe_edit(msg, out[:3990] + "…</pre>",
                                parse_mode=ParseMode.HTML)
            try:
                body = render_bulk_entry(res)
                await update.message.reply_document(
                    document=file_from_bytes(
                        body.encode('utf-8'), f"{device_id[:24]}_banned.txt"),
                    caption="📄 ban record + profile")
            except Exception:
                pass
        else:
            await safe_edit(msg, render_ban_html(device_id, res),
                            parse_mode=ParseMode.HTML)
        return
    if st != 'success':
        await safe_edit(msg, render_error_html("SINGLE CHECK", device_id, res),
                        parse_mode=ParseMode.HTML); return
    player = res['player']
    out = render_profile_telegram(player, device_id)
    if len(out) > 4000: out = out[:3990] + "\n…</pre>"
    try: await safe_edit(msg, out, parse_mode=ParseMode.HTML)
    except Exception: await safe_edit(msg, out[:3990] + "…</pre>", parse_mode=ParseMode.HTML)
    try:
        body = render_profile_text(player, device_id)
        await update.message.reply_document(
            document=file_from_bytes(body.encode('utf-8'),
                                     f"{device_id[:24]}_profile.txt"),
            caption="📄 full profile")
    except Exception as e:
        await update.message.reply_text(f"file attach failed: {esc(str(e))}")


async def _do_created(update, context, raw):
    u = update.effective_user
    if u is None or update.message is None: return
    ok, reason, _ = DBX.auth_status(u.id)
    if not ok and not DBX.is_admin(u.id):
        await update.message.reply_text(f"🔒 {reason}"); return
    device_id = first_device_id(raw)
    if not device_id:
        await update.message.reply_text("no valid device id in that message."); return
    msg = await update.message.reply_text(f"📅 checking creation for <code>{esc(device_id[:32])}…</code>",
                                           parse_mode=ParseMode.HTML)
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(EXECUTOR,
                                          lambda: with_retries(_op_creation_check, device_id))
    except Exception as e:
        res = {'status': 'error', 'error': str(e)}
    DBX.log_usage(u.id, 'created', device_id)
    if not DBX.is_admin(u.id): DBX.consume_use(u.id)
    await safe_edit(msg, render_creation_html(device_id, res), parse_mode=ParseMode.HTML)


# ─── bulk ──────────────────────────────────────────────────────────

async def _run_bulk(update, context, ids, filename):
    u = update.effective_user
    if u is None or update.message is None: return
    total = len(ids)

    cancel = BulkCancel()
    session = {'stop': False, 'cancel': cancel}
    _set_bulk_session(u.id, session)

    stats = {'total': total, 'done': 0, 'clean': 0, 'banned': 0, 'fail': 0,
             'stopped': 0, 'last_device': '', 'last_status': ''}
    lock = threading.Lock()
    results = []
    started = time.time()

    status_msg = await update.message.reply_text(
        render_bulk_running(stats, 0.01, total, filename),
        parse_mode=ParseMode.HTML, reply_markup=bulk_stop_kb())
    loop = asyncio.get_running_loop()

    def run_one(device_id):
        if cancel.is_set():
            with lock:
                stats['done'] += 1
                stats['stopped'] += 1
                stats['last_device'] = device_id[:24]
                stats['last_status'] = 'stopped'
            return {'status': 'stopped', 'device_id': device_id}
        try:
            r = with_retries(_op_probe, device_id, cancel=cancel)
        except Exception as e:
            r = {'status': 'error', 'device_id': device_id,
                 'error': f'{type(e).__name__}: {e}'}
        r['device_id'] = device_id
        st = r.get('status')
        # If cancel fired while we were mid-op, mark as stopped unless the
        # op already produced a real verdict (clean/banned).
        if cancel.is_set() and st not in ('clean', 'banned', 'success'):
            r['status'] = 'stopped'
            st = 'stopped'
        with lock:
            stats['done'] += 1
            if st == 'clean':
                stats['clean'] += 1
            elif st == 'banned':
                stats['banned'] += 1
            elif st == 'stopped':
                stats['stopped'] += 1
            else:
                stats['fail'] += 1
            stats['last_device'] = device_id[:24]
            stats['last_status'] = st or 'error'
        return r

    async def progress_loop():
        interval = max(LIVE_MS, 800) / 1000.0
        edit_fails = 0
        while True:
            await asyncio.sleep(interval)
            now = time.time()
            with lock: snap = dict(stats)
            text = render_bulk_running(snap, now - started, total, filename)
            try:
                await safe_edit(status_msg, text, parse_mode=ParseMode.HTML)
                edit_fails = 0
            except Exception:
                edit_fails += 1
                if edit_fails > 6: return
            if snap['done'] >= total or cancel.is_set(): return

    prog_task = None
    elapsed = 0.0
    fatal = None

    try:
        futures = [loop.run_in_executor(EXECUTOR, run_one, d) for d in ids]
        prog_task = asyncio.create_task(progress_loop())
        for coro in asyncio.as_completed(futures):
            try:
                r = await coro
                if r: results.append(r)
            except Exception: pass
    except Exception as e:
        fatal = f"{type(e).__name__}: {e}"
        log.exception("bulk crashed")
    finally:
        if prog_task is not None:
            try: prog_task.cancel()
            except Exception: pass
        elapsed = time.time() - started
        was_stopped = cancel.is_set() or bool(session.get('stop'))
        _set_bulk_session(u.id, None)

    if fatal:
        await safe_edit(status_msg, f"📦 <b>BULK CRASHED</b>\n\n<code>{esc(fatal)}</code>",
                        parse_mode=ParseMode.HTML, reply_markup=back_kb()); return

    DBX.log_usage(u.id, f'bulk:{total}', filename)
    if not DBX.is_admin(u.id):
        completed = sum(1 for r in results if r.get('status') != 'stopped')
        for _ in range(completed): DBX.consume_use(u.id)

    summary = render_bulk_summary(stats, elapsed, filename, was_stopped)
    try:
        await safe_edit(status_msg, summary, parse_mode=ParseMode.HTML, reply_markup=back_kb())
    except Exception:
        await update.message.reply_text(summary, parse_mode=ParseMode.HTML, reply_markup=back_kb())

    clean_blocks = []; banned_blocks = []; error_blocks = []; all_blocks = []
    for r in results:
        block = render_bulk_entry(r)
        all_blocks.append(block)
        st = r.get('status')
        if st == 'clean': clean_blocks.append(block)
        elif st == 'banned': banned_blocks.append(block)
        elif st == 'error': error_blocks.append(block)

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = re.sub(r'[^A-Za-z0-9_\-]', '_', os.path.splitext(filename)[0])[:32] or "bulk"
    partial_tag = "_partial" if was_stopped else ""
    try:
        if clean_blocks:
            await update.message.reply_document(
                document=file_from_lines(clean_blocks,
                                         f"{stem}_{ts}_clean{partial_tag}.txt"),
                caption=f"✅ clean ({len(clean_blocks):,})"
                        + (" · partial run" if was_stopped else ""))
        if banned_blocks:
            await update.message.reply_document(
                document=file_from_lines(banned_blocks,
                                         f"{stem}_{ts}_banned{partial_tag}.txt"),
                caption=f"⛔ banned ({len(banned_blocks):,})"
                        + (" · partial run" if was_stopped else ""))
        if error_blocks:
            await update.message.reply_document(
                document=file_from_lines(error_blocks,
                                         f"{stem}_{ts}_errors{partial_tag}.txt"),
                caption=f"⚠️ errors ({len(error_blocks):,})")
        if all_blocks:
            await update.message.reply_document(
                document=file_from_lines(all_blocks,
                                         f"{stem}_{ts}_all{partial_tag}.txt"),
                caption=f"📄 full report ({len(all_blocks):,})"
                        + (" · partial run" if was_stopped else ""))
    except Exception as e:
        await update.message.reply_text(f"file send failed: {esc(str(e))}")


# ─── admin helpers ─────────────────────────────────────────────────

async def _do_admin_gen(update, context, raw):
    u = update.effective_user
    if u is None or update.message is None: return
    parts = raw.split()
    if len(parts) < 3:
        await update.message.reply_text("usage: <tier> <uses> <days>  e.g. pro 1000 30"); return
    tier = parts[0].lower()
    if tier not in ("trial", "basic", "pro", "unlimited"):
        await update.message.reply_text("tier must be: trial · basic · pro · unlimited"); return
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
    if update.message is None: return
    key = raw.strip().upper()
    if DBX.revoke_key(key):
        await update.message.reply_text(f"🗑 revoked <code>{esc(key)}</code>",
                                        parse_mode=ParseMode.HTML, reply_markup=admin_menu())
    else:
        await update.message.reply_text("no such key.")


async def _do_admin_broadcast(update, context, raw):
    u = update.effective_user
    if u is None or update.message is None: return
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
    print(f"[+] SHIN bot online · {THREADS} threads · {RETRIES} retries · db={DB_PATH}")
    print(f"[+] boot admins: {sorted(BOOT_ADMINS)} · live_ms={LIVE_MS}")
    print(f"[+] python-telegram-bot: {_PTB_VER}")
    print(f"[+] proxies    : {PROXY_POOL.size()} loaded from {PROXIES_PATH}")
    print(f"[+] rotate     : every {PROXY_ROTATE_EVERY} device-ids")
    print(f"[+] in-flight  : {MAX_INFLIGHT}  gap {MIN_GAP}s  backoff {BASE_BACKOFF}s "
          f"(cap {MAX_BACKOFF}s)")
    if PROXY_POOL.size() == 0:
        print(f"[!] proxies.txt empty or missing — running DIRECT (no rotation)")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("panel", cmd_admin))
    app.add_handler(CommandHandler("genkey", cmd_genkey))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CallbackQueryHandler(cb_bulk_stop, pattern=r"^bulk:stop$"))
    app.add_handler(CallbackQueryHandler(cb_bulk_noop, pattern=r"^bulk:noop$"))
    app.add_handler(CallbackQueryHandler(cb_admin, pattern=r"^admin:"))
    app.add_handler(CallbackQueryHandler(cb_menu, pattern=r"^menu:"))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    except telegram.error.InvalidToken:
        print("[!] Telegram rejected the bot token.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
