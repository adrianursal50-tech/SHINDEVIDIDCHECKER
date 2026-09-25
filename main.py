# drakvex_bot.py
# DRAKVEX Telegram bot — port of DRAKVEX_ML.py
# python-telegram-bot v20+, zstandard, pycryptodome

import os
import re
import io
import sys
import json
import time
import zlib
import uuid
import html
import socket
import struct
import sqlite3
import random
import asyncio
import datetime
import threading
import traceback
from enum import Enum
from typing import Any
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import zstandard as zstd
from Crypto.Cipher import AES

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile,
)
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)


# ═══════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════

BOT_TOKEN   = os.environ.get("DRAKVEX_BOT_TOKEN", "8702549007:AAE9KjW1MmPxdcZtyi8IozCQnEQrZ3uaNYM")
DB_PATH     = os.environ.get("DRAKVEX_DB", "drakvex.db")

# FIX: default to Sam's id, split on commas, not on the literal admin id.
DEFAULT_ADMINS = "8621676055"
_admin_env  = os.environ.get("DRAKVEX_ADMINS", DEFAULT_ADMINS).strip()
BOOT_ADMINS = {int(x) for x in _admin_env.split(",") if x.strip().isdigit()}
if not BOOT_ADMINS:
    # never run with an empty admin set — Sam is the principal, always.
    BOOT_ADMINS = {8621676055}

THREADS     = int(os.environ.get("DRAKVEX_THREADS", "20"))
RETRIES     = int(os.environ.get("DRAKVEX_RETRIES", "3"))
BULK_CAP    = int(os.environ.get("DRAKVEX_BULK_CAP", "10000"))
PROGRESS_MS = int(os.environ.get("DRAKVEX_PROGRESS_MS", "2500"))

# 20 worker threads shared by everything — single check, ban check, bulk.
EXECUTOR = ThreadPoolExecutor(max_workers=THREADS, thread_name_prefix="drkvx")

if not BOT_TOKEN:
    print("[!] set DRAKVEX_BOT_TOKEN before running.", file=sys.stderr)
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════════

class DB:
    """Thread-safe sqlite wrapper. One connection per op, single write lock."""

    _lock = threading.RLock()

    def __init__(self, path: str):
        self.path = path
        self._init()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    def _init(self):
        with self._lock, self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                tg_id       INTEGER PRIMARY KEY,
                username    TEXT,
                first_seen  INTEGER,
                key         TEXT,
                is_admin    INTEGER DEFAULT 0,
                checks_used INTEGER DEFAULT 0,
                banned      INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS keys (
                key         TEXT PRIMARY KEY,
                tier        TEXT,
                max_uses    INTEGER,
                uses        INTEGER DEFAULT 0,
                expires_at  INTEGER,
                owner_id    INTEGER,
                created_by  INTEGER,
                created_at  INTEGER,
                revoked     INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS usage_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id       INTEGER,
                kind        TEXT,
                device_id   TEXT,
                ts          INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_usage_tg ON usage_log(tg_id);
            """)
            for admin_id in BOOT_ADMINS:
                c.execute(
                    "INSERT OR IGNORE INTO users(tg_id, username, first_seen, is_admin) "
                    "VALUES(?,?,?,1)", (admin_id, "boot_admin", int(time.time()))
                )
                # force-promote on every boot so a stale is_admin=0 gets repaired.
                c.execute("UPDATE users SET is_admin=1, banned=0 WHERE tg_id=?", (admin_id,))

    # ---- users ----

    def ensure_user(self, tg_id: int, username: str) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO users(tg_id, username, first_seen) VALUES(?,?,?)",
                (tg_id, username or "", int(time.time()))
            )
            c.execute("UPDATE users SET username=? WHERE tg_id=?", (username or "", tg_id))
            # repair admin flags on every ensure — cheap, idempotent.
            if tg_id in BOOT_ADMINS:
                c.execute("UPDATE users SET is_admin=1, banned=0 WHERE tg_id=?", (tg_id,))

    def get_user(self, tg_id: int):
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
            return dict(row) if row else None

    def is_admin(self, tg_id: int) -> bool:
        if tg_id in BOOT_ADMINS:
            return True
        u = self.get_user(tg_id)
        return bool(u and u["is_admin"])

    def toggle_admin(self, tg_id: int, on: bool) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE users SET is_admin=? WHERE tg_id=?", (1 if on else 0, tg_id))

    def set_banned(self, tg_id: int, on: bool) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE users SET banned=? WHERE tg_id=?", (1 if on else 0, tg_id))

    def list_users(self, limit=30):
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM users ORDER BY first_seen DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ---- auth ----

    def auth_status(self, tg_id: int):
        """Return (ok, reason, user_row_or_None)."""
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

    def consume_use(self, tg_id: int) -> None:
        if tg_id in BOOT_ADMINS or self.is_admin(tg_id):
            return  # admins never burn uses.
        with self._lock, self._conn() as c:
            u = c.execute("SELECT key FROM users WHERE tg_id=?", (tg_id,)).fetchone()
            c.execute("UPDATE users SET checks_used=checks_used+1 WHERE tg_id=?", (tg_id,))
            if u and u["key"]:
                c.execute("UPDATE keys SET uses=uses+1 WHERE key=?", (u["key"],))

    def log_usage(self, tg_id: int, kind: str, device_id: str) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO usage_log(tg_id, kind, device_id, ts) VALUES(?,?,?,?)",
                (tg_id, kind, device_id or "", int(time.time()))
            )

    # ---- keys ----

    def generate_key(self, tier: str, max_uses: int, days: int, created_by: int) -> str:
        raw = uuid.uuid4().hex.upper()
        key = "-".join(["DRKV"] + [raw[i:i+4] for i in range(0, 20, 4)])
        exp = 0 if days <= 0 else int(time.time()) + days * 86400
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO keys(key, tier, max_uses, expires_at, created_by, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (key, tier, max_uses, exp, created_by, int(time.time()))
            )
        return key

    def get_key(self, key: str):
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM keys WHERE key=?", (key,)).fetchone()
            return dict(row) if row else None

    def redeem_key(self, tg_id: int, key: str) -> tuple[bool, str]:
        key = key.strip().upper()
        with self._lock, self._conn() as c:
            k = c.execute("SELECT * FROM keys WHERE key=?", (key,)).fetchone()
            if not k:
                return False, "key not found."
            k = dict(k)
            if k["revoked"]:
                return False, "key revoked."
            if k["owner_id"] and k["owner_id"] != tg_id:
                return False, "key already owned by someone else."
            if k["expires_at"] and k["expires_at"] < int(time.time()):
                return False, "key expired."
            if k["max_uses"] > 0 and k["uses"] >= k["max_uses"]:
                return False, "key out of uses."
            c.execute("UPDATE keys SET owner_id=? WHERE key=?", (tg_id, key))
            c.execute("UPDATE users SET key=? WHERE tg_id=?", (key, tg_id))
            return True, f"redeemed ({k['tier']})."

    def revoke_key(self, key: str) -> bool:
        with self._lock, self._conn() as c:
            cur = c.execute("UPDATE keys SET revoked=1 WHERE key=?", (key.strip().upper(),))
            return cur.rowcount > 0

    def list_keys(self, limit=40):
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM keys ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def stats(self):
        with self._lock, self._conn() as c:
            users  = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
            keys   = c.execute("SELECT COUNT(*) AS n FROM keys").fetchone()["n"]
            active = c.execute(
                "SELECT COUNT(*) AS n FROM keys WHERE revoked=0 AND "
                "(expires_at=0 OR expires_at>?) AND (max_uses=0 OR uses<max_uses)",
                (int(time.time()),)
            ).fetchone()["n"]
            checks = c.execute("SELECT COUNT(*) AS n FROM usage_log").fetchone()["n"]
            today0 = int(datetime.datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0).timestamp())
            today  = c.execute(
                "SELECT COUNT(*) AS n FROM usage_log WHERE ts>=?", (today0,)
            ).fetchone()["n"]
            return {"users": users, "keys": keys, "active": active,
                    "checks": checks, "today": today}


DBX = DB(DB_PATH)


# ═══════════════════════════════════════════════════════════════════
# GAME CORE — ported from DRAKVEX_ML.py
# ═══════════════════════════════════════════════════════════════════

class SdpType(Enum):
    INT_POS = 0
    INT_NEG = 1
    FLOAT = 2
    DOUBLE = 3
    STRING = 4
    LIST = 5
    DICT = 6
    STRUCT_BEGIN = 7
    STRUCT_END = 8


class SdpError(Exception):
    pass


def _sdp_sort_key(kv):
    k = kv[0]
    if isinstance(k, int):
        return (0, k)
    return (1, str(k))


class SdpStruct(dict):
    def __init__(self, data=None):
        super().__init__()
        self.data = b''
        self.offset = 0
        if isinstance(data, bytes):
            self.data = data
            self.offset = 0
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
        if not self.data:
            return
        if self.data[0] >> 4 == SdpType.STRUCT_BEGIN.value:
            self.offset = 1
        while self.offset < len(self.data):
            tag, value = self._unpack()
            if isinstance(value, SdpType) and value == SdpType.STRUCT_END:
                break
            self[tag] = value

    def _write_number(self, value):
        result = bytearray()
        while value >= 0x80:
            result.append((value & 0x7F) | 0x80)
            value >>= 7
        result.append(value & 0x7F)
        return bytes(result)

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
            self.data += self._write_number(len(packed))
            self.data += packed
        elif isinstance(value, (str, bytes)):
            self._pack_header(tag, SdpType.STRING)
            encoded = value.encode('utf-8') if isinstance(value, str) else value
            self.data += self._write_number(len(encoded))
            self.data += encoded
        elif isinstance(value, list):
            self._pack_header(tag, SdpType.LIST)
            self.data += self._write_number(len(value))
            for item in value:
                self._pack(0, item)
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
                    self._pack(0, k)
                    self._pack(0, v)
        else:
            raise SdpError(f'unsupported type {type(value)}')

    def _unpack(self):
        try:
            if self.offset >= len(self.data):
                return 0, None
            header = self.data[self.offset]
            tag = header & 0xF
            dtype = SdpType(header >> 4)
            self.offset += 1
            if tag == 15:
                tag = self._read_number()

            if dtype == SdpType.INT_POS:
                return tag, self._read_number()
            if dtype == SdpType.INT_NEG:
                return tag, -self._read_number()
            if dtype == SdpType.FLOAT:
                raw = self._read_number().to_bytes(4, 'little')
                return tag, struct.unpack("<f", raw)[0]
            if dtype == SdpType.DOUBLE:
                raw = self._read_number().to_bytes(8, 'little')
                return tag, struct.unpack("<d", raw)[0]
            if dtype == SdpType.STRING:
                length = self._read_number()
                chunk = self.data[self.offset:self.offset + length]
                try:
                    val = chunk.decode('utf-8')
                except UnicodeDecodeError:
                    val = chunk
                self.offset += length
                return tag, val
            if dtype == SdpType.LIST:
                length = self._read_number()
                val = []
                for _ in range(length):
                    _, item = self._unpack()
                    val.append(item)
                return tag, val
            if dtype == SdpType.DICT:
                length = self._read_number()
                val = {}
                for _ in range(length):
                    _, k = self._unpack()
                    _, v = self._unpack()
                    val[k] = v
                return tag, val
            if dtype == SdpType.STRUCT_BEGIN:
                sub = {}
                while True:
                    st, sv = self._unpack()
                    if isinstance(sv, SdpType) and sv == SdpType.STRUCT_END:
                        break
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

LOGIN_HOST     = 'login.ml.youngjoygame.com'
LOGIN_PORT     = 30021
CLIENT_VERSION = '2.2.16.1232.1'
CHANNEL        = 'and_usa'


BAN_REASONS = {
    '21': 'Plug-in / unfair advantage',
    '22': 'Account sharing / boosting',
    '23': 'Verbal abuse / toxic behavior',
    '24': 'Cheating in ranked',
    '25': 'Payment fraud / chargeback',
    '26': 'Account trading / sale',
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

EMBLEM_MAP = {1: "Fighter", 2: "Assassin", 3: "Mage", 4: "Marksman",
              5: "Support", 6: "Tank", 7: "Common"}

AFFINITY_MAP = {0: "None", 1: "Bronze", 2: "Silver", 3: "Gold",
                4: "Platinum", 5: "Diamond"}

MODE_MAP = {0: "Classic", 1: "Ranked", 2: "Brawl", 3: "AI",
            4: "Custom", 5: "Mayhem", 6: "Arcade"}

SKIN_TIER_MAP = {1: "Common", 2: "Exceptional", 3: "Deluxe",
                 4: "Exquisite", 5: "Grand", 6: "Supreme"}

COLLECTOR_TIERS = [
    (1000, 4000, "Amateur Collector"),
    (4000, 10000, "Junior Collector"),
    (10000, 22000, "Seasoned Collector"),
    (22000, 44000, "Expert Collector"),
    (44000, 84000, "Renowned Collector"),
    (84000, 160000, "Exalted Collector"),
    (160000, 280000, "Mega Collector"),
    (280000, float('inf'), "World Collector"),
]


def map_rank(p):
    try:
        p = int(p)
    except (TypeError, ValueError):
        return "Unranked"
    if p <= 0:
        return "Unranked"
    table = [
        (0, 4, "Warrior III"), (5, 9, "Warrior II"), (10, 14, "Warrior I"),
        (15, 19, "Elite IV"), (20, 24, "Elite III"), (25, 29, "Elite II"),
        (30, 34, "Elite I"),
        (35, 39, "Master IV"), (40, 44, "Master III"), (45, 49, "Master II"),
        (50, 54, "Master I"),
        (55, 59, "Grandmaster IV"), (60, 64, "Grandmaster III"),
        (65, 69, "Grandmaster II"), (70, 74, "Grandmaster I"),
        (75, 81, "Epic IV"), (82, 88, "Epic III"), (89, 95, "Epic II"),
        (96, 107, "Epic I"),
        (108, 114, "Legend IV"), (115, 121, "Legend III"),
        (122, 128, "Legend II"), (129, 135, "Legend I"),
    ]
    for lo, hi, name in table:
        if lo <= p <= hi:
            return name
    if 136 <= p <= 160: return f"Mythic {p - 135}"
    if 161 <= p <= 195: return f"Mythical Honor {p - 135}"
    if 196 <= p <= 235: return f"Mythical Glory {p - 195}"
    if p >= 236:        return f"Mythical Immortal {p - 235}"
    return "Unranked"


def map_collector(p):
    try:
        p = int(p)
    except (TypeError, ValueError):
        return "No Tier"
    if p < 1000:
        return "No Tier"
    for lo, hi, name in COLLECTOR_TIERS:
        if lo <= p < hi:
            if name == "World Collector":
                return name
            span = (hi - lo) / 5
            lvl = max(0, min(4, int((p - lo) // span)))
            roman = ["V", "IV", "III", "II", "I"][lvl]
            return f"{name} {roman}"
    return "No Tier"


def as_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def fmt_ts(ts):
    if not isinstance(ts, int) or ts < 100000000:
        return None
    try:
        utc = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        pht = utc + datetime.timedelta(hours=8)
        return pht.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return None


def fmt_ts_full(ts):
    if not isinstance(ts, int) or ts < 100000000:
        return None
    try:
        utc = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        pht = utc + datetime.timedelta(hours=8)
        return pht.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def human_dur(sec):
    sec = float(max(0, sec))
    if sec < 1:   return f'{int(sec * 1000)}ms'
    if sec < 60:  return f'{sec:.1f}s'
    m, s = divmod(int(sec), 60)
    if m < 60:    return f'{m}m{s:02d}s'
    h, rem = divmod(m, 60)
    return f'{h}h{rem:02d}m'


def human_bytes(n, prec=1):
    n = float(n)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(n) < 1024.0:
            return f'{int(n)} {unit}' if unit == 'B' else f'{n:.{prec}f} {unit}'
        n /= 1024.0
    return f'{n:.{prec}f} PB'


# ─── ban inspection ────────────────────────────────────────────────

def _walk_ban(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == 'ban_reason':
                code = str(v)
                out['ban_code'] = code
                out['reason_name'] = BAN_REASONS.get(code, f'Reason Code {code}')
            elif k == 'endtime_day':
                out['endtime_day'] = v
            elif k == 'endtime_hour':
                out['endtime_hour'] = v
            elif k == 'endtime_min':
                out['endtime_min'] = v
            elif k == 'endtime_sec':
                out['endtime_sec'] = v
            elif isinstance(k, str) and 'ban' in k.lower():
                out[str(k)] = v
            if isinstance(v, (dict, list)):
                _walk_ban(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _walk_ban(item, out)


def inspect_ban(sdp):
    info = {}
    if sdp:
        _walk_ban(dict(sdp), info)
    has_end  = 'endtime_day' in info and info['endtime_day'] is not None
    has_code = 'ban_code' in info and info['ban_code'] not in (None, '', '0')
    banned = has_end or (has_code and info.get('reason_name'))
    return banned, info


def format_ban_duration(info):
    day = info.get('endtime_day')
    if day is None:
        return None
    h = as_int(info.get('endtime_hour', 0))
    m = as_int(info.get('endtime_min', 0))
    s = as_int(info.get('endtime_sec', 0))
    return f"day {day}, {h:02d}:{m:02d}:{s:02d}"


# ─── connection ────────────────────────────────────────────────────

class GameConn:
    def __init__(self, device_id):
        self.device_id = device_id
        self.host = LOGIN_HOST
        self.port = LOGIN_PORT
        self.sequence = 1
        self.socket = None
        self.queue = b''
        self.last_header_size = 0

        parts = device_id.split('_')
        if len(parts) >= 2:
            info = parts[1]
            if len(parts) >= 3 and len(info) < 32:
                info = info + '_' + parts[2]
            if len(info) >= 32:
                self.imei_md5       = info[:32]
                self.android_id     = info[32:48] if len(info) >= 48 else ''
                self.advertising_id = info[48:] if len(info) > 48 else ''
            else:
                self.imei_md5       = info
                self.android_id     = ''
                self.advertising_id = ''
        else:
            self.imei_md5       = device_id
            self.android_id     = ''
            self.advertising_id = ''

        self.account_id  = 0
        self.session_key = ''
        self.zone_id     = 0
        self.game_host   = ''
        self.game_port   = 0
        self.creation_ts = 0
        self.ban_seen    = False
        self.ban_info    = {}

    def _sniff(self, res):
        if self.ban_seen or res is None:
            return
        banned, info = inspect_ban(res)
        if banned:
            self.ban_seen = True
            self.ban_info = dict(info)

    def connect(self, host=None, port=None):
        if host: self.host = host
        if port: self.port = port
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.settimeout(6)
        self.socket.connect((self.host, self.port))

    def close(self):
        if self.socket:
            try: self.socket.close()
            except Exception: pass
        self.socket = None
        self.sequence = 1
        self.queue = b''

    def send(self, pid, sdp):
        packet = SdpStruct({0: pid, 1: self.sequence, 5: sdp.data}).data
        buf  = zstd.compress(packet)
        size = len(buf) + 4
        if size > 0xFFFFFF:
            raise SdpError('packet too large')
        flags = size | (16 << 24)
        self.socket.send(flags.to_bytes(4, 'big') + buf)
        self.sequence += 1

    def recv(self):
        try:
            while len(self.queue) < 4:
                d = self.socket.recv(4096)
                if not d: return None, None
                self.queue += d
            flags = int.from_bytes(self.queue[:4], 'big')
            size  = flags & 0xFFFFFF
            ctype = flags >> 24
            self.last_header_size = size
            while len(self.queue) < size:
                d = self.socket.recv(4096)
                if not d: return None, None
                self.queue += d
            data = self.queue[4:size]
            self.queue = self.queue[size:]

            if ctype == 1:   data = zlib.decompress(data)
            elif ctype == 16: data = zstd.decompress(data)
            elif ctype == 2:  data = _aes_decrypt(data)
            elif ctype == 3:  data = zlib.decompress(_aes_decrypt(data))
            elif ctype == 18: data = zstd.decompress(_aes_decrypt(data))

            result = SdpStruct(data)
            pid = result[0]
            if pid is None:
                return None, None
            body = result.get(6)
            if not isinstance(body, bytes):
                body = result.get(5)
                if not isinstance(body, bytes):
                    return pid, None
            parsed = SdpStruct(body)
            self._sniff(parsed)
            return pid, parsed
        except socket.timeout:
            return -1, None
        except Exception:
            return None, None

    def login(self):
        self.connect(LOGIN_HOST, LOGIN_PORT)
        self.send(1, SdpStruct({
            0: self.device_id,
            1: f'gps_adid={self.advertising_id}&android_id={self.android_id}&device_unique_id={self.imei_md5}',
            2: CLIENT_VERSION,
            3: CHANNEL,
            4: 'en',
        }))
        pid, res = self.recv()
        if pid == 2 and res:
            self.account_id  = res.get(0)
            self.session_key = res.get(1)
            zd = res.get(2)
            if isinstance(zd, dict):
                self.zone_id = zd.get(0, 0)
            elif isinstance(zd, list) and zd:
                self.zone_id = zd[0] if not isinstance(zd[0], dict) else zd[0].get(0, 0)
            else:
                self.zone_id = zd or 0
            self.creation_ts = res.get(19, 0)
            return True, res
        return False, res

    def get_server(self):
        self.send(5, SdpStruct({
            0: self.account_id, 1: self.session_key, 2: CLIENT_VERSION,
            5: self.zone_id, 6: CHANNEL,
        }))
        pid, res = self.recv()
        if pid == 6 and res:
            addr = res[1]
            self.game_host, port_str = addr.split(':')
            self.game_port = int(port_str)
            return True, res
        return False, res

    def enter_game(self):
        self.close()
        self.connect(self.game_host, self.game_port)
        self.send(10001, SdpStruct({
            0: self.account_id, 1: self.session_key, 2: self.zone_id,
            4: CLIENT_VERSION, 13: CHANNEL, 15: self.device_id,
        }))
        self.send(10101, SdpStruct({0: 0, 2: 2}))

    def handshake(self, tries=25):
        for _ in range(tries):
            pid, _ = self.recv()
            if pid is None or pid == -1: return False
            if pid == 10002: return True
        return False

    def lookup(self, value, kind='id'):
        if kind == 'id':
            try:
                payload = SdpStruct({1: int(value)})
            except (TypeError, ValueError):
                return None
        else:
            payload = SdpStruct({0: str(value).strip()})
        self.send(11153, payload)
        miss = 0
        while True:
            pid, res = self.recv()
            if pid is None or pid == -1:
                return None
            if pid == 11154:
                return res
            if pid == 20001:
                miss += 1
                if self.last_header_size < 100 and miss >= 2:
                    return None

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
                    if int(res.get(9, 0) or 0) > 0:
                        return res
                except (TypeError, ValueError):
                    pass
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


# ─── extractors ────────────────────────────────────────────────────

def _find_last_match_ts(entry):
    if not isinstance(entry, dict): return 0
    now = int(time.time())
    lower = now - 3 * 365 * 86400
    upper = now + 2 * 86400

    def _norm(v):
        if not isinstance(v, int) or isinstance(v, bool):
            return None
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
            if isinstance(v, (dict, list)) and v:
                containers.append(v)
    entry = None
    for c in containers:
        if isinstance(c, list) and c and isinstance(c[0], dict):
            entry = c[0]; break
        if isinstance(c, dict):
            for k in sorted(c.keys(), key=lambda x: (isinstance(x, str), x)):
                if isinstance(c[k], dict):
                    entry = c[k]; break
            if entry: break
    if not entry: return None

    hero_id = entry.get(0, 0) or entry.get(1, 0) or 0
    hero    = HERO_ID_MAP.get(hero_id, f"Hero#{hero_id}") if hero_id else "?"
    kills    = as_int(entry.get(2, 0))
    deaths   = as_int(entry.get(3, 0))
    assists  = as_int(entry.get(4, 0))
    duration = as_int(entry.get(5, 0))
    result_raw = entry.get(6, entry.get(7, 0))
    mode_raw   = entry.get(8, 0)
    ts         = _find_last_match_ts(entry)
    mvp        = entry.get(11, 0)
    gold       = as_int(entry.get(12, 0))

    if result_raw in (1, '1', 'win', 'WIN', True):      result = "WIN"
    elif result_raw in (0, '0', 'loss', 'LOSS', False): result = "LOSS"
    else:                                                result = "?"

    mode = MODE_MAP.get(as_int(mode_raw, -1), f"Mode {mode_raw}")

    return {
        'hero': hero, 'kills': kills, 'deaths': deaths, 'assists': assists,
        'kda': f"{kills}/{deaths}/{assists}",
        'kda_ratio': round((kills + assists) / max(deaths, 1), 2),
        'result': result, 'mode': mode,
        'duration': human_dur(duration) if duration else "?",
        'gold': gold, 'mvp': bool(mvp),
        'played_at': fmt_ts(ts), 'played_ts': ts,
    }


def extract_player(result, role_info=None, creation_ts=0, v2l=None):
    if not result or not result.get(0) or len(result[0]) == 0:
        return None
    try:
        p  = result[0][0]
        ri = role_info if isinstance(role_info, dict) else {}

        nickname     = p.get(2, "") or "Unknown"
        player_id    = p.get(0, 0)
        server       = p.get(1, 0)
        level        = as_int(p.get(3, 0))
        skin_count   = as_int(p.get(83, 0))
        hero_count   = as_int(p.get(4, 0))
        matches      = as_int(p.get(17, 0))
        rating_score = as_int(p.get(9, 0))
        if ri:
            hero_count = as_int(ri.get(9, hero_count)) or hero_count
            matches    = as_int(ri.get(22, matches)) or matches

        loc = p.get(71)
        location = ", ".join(str(x) for x in loc if x) if isinstance(loc, list) and len(loc) >= 2 else None

        last_login     = fmt_ts(p.get(5, 0))
        last_country   = p.get(87, "")
        reg_country    = p.get(97, "")
        squad_icon     = p.get(31, "")
        squad_name     = str(p.get(30, "")).replace("`", "").strip()
        squad          = f"{squad_icon} {squad_name}".strip() if squad_name else None
        rank_now       = map_rank(p.get(8))
        rank_top       = map_rank(p.get(95))
        ach            = p.get(7, 0)

        t136     = p.get(136, {})
        coll_pts = t136.get(9, 0) if isinstance(t136, dict) else 0

        t91 = p.get(91, [])
        history = [HERO_ID_MAP.get(h, f"Hero#{h}") for h in reversed(t91)] if t91 else []

        v2l_status = None
        if v2l and isinstance(v2l, dict):
            src   = v2l.get("_src", 0)
            data  = v2l.get("_data", {})
            probes = (10, 11) if src == 10208 else (0, 2, 3, 5)
            for tag in probes:
                v = data.get(tag)
                if v is not None:
                    try:
                        v2l_status = "Enabled" if int(v) > 0 else "Disabled"
                        break
                    except (ValueError, TypeError):
                        pass

        followers = ri.get(23, 0) or p.get(15, 0) or 0
        likes = 0
        motto = None
        ri24 = ri.get(24)
        if isinstance(ri24, int):   likes = ri24
        elif isinstance(ri24, str) and ri24.strip(): motto = ri24.strip()
        if not likes: likes = p.get(61, 0) or 0

        credits = None
        cs = ri.get(20, 0) or p.get(80, 0)
        if isinstance(cs, int) and cs > 0:
            credits = f"{cs}/110"

        restriction = None
        t117 = ri.get(117, p.get(117))
        if t117 is not None:
            raw = t117.get(0, 0) if isinstance(t117, dict) else (t117 if isinstance(t117, int) else 0)
            flags = int(raw) + 1
            pctv  = round((flags / 7) * 100, 1)
            if pctv < 30:   restriction = f"{pctv}% low risk"
            elif pctv < 60: restriction = f"{pctv}% medium risk"
            else:           restriction = f"{pctv}% high risk"

        affinity = None
        names = [e.get(2, "") for e in (ri.get(82, []) or [])
                 if isinstance(e, dict) and isinstance(e.get(2, ""), str) and e.get(2, "")]
        if names:
            affinity = ", ".join(names)
        else:
            t135 = p.get(135, {})
            aff_lvl = t135.get(1, 0) if isinstance(t135, dict) else 0
            if aff_lvl:
                affinity = AFFINITY_MAP.get(aff_lvl, f"Level {aff_lvl}")

        sl_exp = 0
        for tag in (21, 47, 50):
            v = ri.get(tag) or p.get(tag) or 0
            if isinstance(v, int) and v > 1700000000:
                sl_exp = v; break
        starlight         = "Yes" if sl_exp > time.time() else "No"
        starlight_expiry  = fmt_ts(sl_exp) if sl_exp else None
        starlight_months  = p.get(60, 0) or 0
        tickets           = ri.get(49, 0) or p.get(49, 0) or 0
        total_wins        = p.get(18, 0) or 0

        min_ts   = 1451577600
        fallback = p.get(6, 0)
        if creation_ts and creation_ts >= min_ts:
            created = fmt_ts_full(creation_ts); age_ts = creation_ts
        elif fallback and fallback >= min_ts:
            created = fmt_ts_full(fallback); age_ts = fallback
        else:
            created = None; age_ts = 0

        age = None
        if age_ts:
            now   = datetime.datetime.now(datetime.timezone.utc)
            start = datetime.datetime.fromtimestamp(age_ts, datetime.timezone.utc)
            d = (now - start).days
            y, m, r = d // 365, (d % 365) // 30, d % 30
            age = f"{y}y {m}m {r}d" if y else (f"{m}m {r}d" if m else f"{d}d")

        win_count     = ri.get(22, 0) or 0
        total_battles = ri.get(77, 0) or p.get(17, 0) or 0
        win_rate = None
        wins_for_rate = win_count if win_count else total_wins
        if total_battles > 0 and wins_for_rate > 0:
            wr = (wins_for_rate / total_battles) * 100
            win_rate = f"~{min(wr, 100):.1f}%" if wr > 100 else f"{wr:.1f}%"

        diamonds = 0
        bp = 0
        cur = ri.get(111)
        if isinstance(cur, dict):
            diamonds = as_int(cur.get(0, 0)); bp = as_int(cur.get(1, 0))
        elif isinstance(cur, int):
            diamonds = cur
        elif isinstance(cur, list) and cur:
            diamonds = as_int(cur[0]) if len(cur) > 0 else 0
            bp       = as_int(cur[1]) if len(cur) > 1 else 0
        if not bp: bp = int(p.get(83, 0) or 0)

        last_dia  = fmt_ts(p.get(42, 0))
        mcl_wins  = ri.get(46, 0) or p.get(104, p.get(103, 0)) or 0

        skin_counts = {
            "Supreme Skins": 0, "Grand Skins": 0, "Exquisite Skins": 0,
            "Deluxe Skins": 0, "Exceptional Skins": 0, "Common Skins": 0,
        }
        t118 = ri.get(118) or p.get(118)
        if t118 and isinstance(t118, dict):
            sd = None
            for k in (4, 0, 1):
                v = t118.get(k)
                if isinstance(v, dict) and v: sd = v; break
            if sd is None: sd = t118
            counts = {6: "Supreme Skins", 5: "Grand Skins", 4: "Exquisite Skins",
                      3: "Deluxe Skins", 2: "Exceptional Skins", 1: "Common Skins"}
            for sid, cnt in sd.items():
                try: sid_i = int(sid)
                except (ValueError, TypeError): continue
                if sid_i in counts: skin_counts[counts[sid_i]] = cnt

        last_heroes = []
        t45 = p.get(45, {})
        if isinstance(t45, dict) and t45:
            entries = []
            for e in t45.values():
                if isinstance(e, dict):
                    entries.append((e.get(1, 0), HERO_ID_MAP.get(e.get(0, 0), f"Hero#{e.get(0, 0)}")))
            entries.sort(key=lambda x: x[1] if False else x[0], reverse=True)
            last_heroes = [h for _, h in entries[:8]]

        last_match = extract_last_match(ri, p)

        return {
            'last_match': last_match,
            'nickname': nickname, 'player_id': player_id, 'server': server,
            'level': level, 'skin_count': skin_count, 'hero_count': hero_count,
            'matches': matches, 'rating_score': rating_score,
            'location': location, 'last_login': last_login,
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
        return None


# ═══════════════════════════════════════════════════════════════════
# HIGH-LEVEL OPERATIONS (raw, unretried)
# ═══════════════════════════════════════════════════════════════════

def _op_ban_check(device_id: str) -> dict:
    conn = GameConn(device_id)
    try:
        ok, _ = conn.login()
        if not ok:
            return {'status': 'error', 'error': 'login failed'}
        if conn.ban_seen:
            return {'status': 'banned',
                    'ban_info': dict(conn.ban_info),
                    'reason':   conn.ban_info.get('reason_name'),
                    'code':     conn.ban_info.get('ban_code'),
                    'duration': format_ban_duration(conn.ban_info)}
        conn.get_server()
        if conn.ban_seen:
            return {'status': 'banned',
                    'ban_info': dict(conn.ban_info),
                    'reason':   conn.ban_info.get('reason_name'),
                    'code':     conn.ban_info.get('ban_code'),
                    'duration': format_ban_duration(conn.ban_info)}
        return {'status': 'clean', 'account_id': conn.account_id, 'zone_id': conn.zone_id}
    except Exception as e:
        return {'status': 'error', 'error': f'{type(e).__name__}: {e}'}
    finally:
        conn.close()


def _op_creation_check(device_id: str) -> dict:
    conn = GameConn(device_id)
    try:
        ok, _ = conn.login()
        if not ok:
            return {'status': 'error', 'error': 'login failed'}
        ts = conn.creation_ts
        if not ts or ts < 1451577600:
            return {'status': 'error', 'error': 'no creation timestamp on login packet',
                    'account_id': conn.account_id}
        now   = datetime.datetime.now(datetime.timezone.utc)
        start = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        d = (now - start).days
        y, m, r = d // 365, (d % 365) // 30, d % 30
        age = f"{y}y {m}m {r}d" if y else (f"{m}m {r}d" if m else f"{d}d")
        return {'status': 'ok', 'creation_ts': ts,
                'created_at': fmt_ts_full(ts), 'age': age, 'age_days': d,
                'account_id': conn.account_id, 'zone_id': conn.zone_id}
    except Exception as e:
        return {'status': 'error', 'error': f'{type(e).__name__}: {e}'}
    finally:
        conn.close()


def _op_single_check(device_id: str) -> dict:
    conn = GameConn(device_id)
    try:
        ok, _ = conn.login()
        if not ok:
            return {'status': 'error', 'error': 'login failed'}
        if conn.ban_seen:
            return _banned_result(conn, device_id)
        account_id  = conn.account_id
        zone_id     = conn.zone_id
        creation_ts = conn.creation_ts

        ok, _ = conn.get_server()
        if not ok:
            return {'status': 'error', 'error': 'no game server assigned'}
        if conn.ban_seen:
            return _banned_result(conn, device_id)

        conn.enter_game()
        if conn.ban_seen:
            return _banned_result(conn, device_id)
        if not conn.handshake():
            if conn.ban_seen:
                return _banned_result(conn, device_id)
            return {'status': 'error', 'error': 'handshake failed'}

        result = conn.lookup(int(account_id), 'id')
        if not result:
            return {'status': 'error', 'error': 'lookup returned nothing'}
        if conn.ban_seen:
            return _banned_result(conn, device_id, result)

        role_id, zone = account_id, zone_id
        if result.get(0) and len(result[0]) > 0:
            first = result[0][0]
            if isinstance(first, dict):
                role_id = first.get(0, account_id)
                zone    = first.get(1, zone_id)

        role_info = None
        try:
            si = conn.skin_info(role_id, zone)
            if si and isinstance(si, dict):
                role_info = dict(si)
        except Exception:
            pass
        try:
            ri = conn.role_info(role_id, zone)
            if ri and isinstance(ri, dict):
                if role_info is None:
                    role_info = dict(ri)
                else:
                    for k, v in ri.items():
                        if k not in role_info or not role_info[k]:
                            role_info[k] = v
        except Exception:
            pass

        v2l = None
        try: v2l = conn.v2l_status(role_id, zone)
        except Exception: pass

        player = extract_player(result, role_info, creation_ts, v2l)
        if not player:
            return {'status': 'error', 'error': 'extract failed'}

        player['_device']  = device_id
        player['_account'] = account_id
        player['_zone']    = zone_id
        return {'status': 'success', 'player': player,
                'device_id': device_id, 'account_id': account_id, 'zone_id': zone_id}
    except Exception as e:
        return {'status': 'error', 'error': f'{type(e).__name__}: {e}'}
    finally:
        conn.close()


def _banned_result(conn, device_id, result=None):
    info = conn.ban_info or {}
    player = None
    if result is not None:
        try: player = extract_player(result, None, conn.creation_ts, None)
        except Exception: player = None
    if player is None:
        player = {
            'nickname': None, 'level': 0, 'current_rank': 'Unknown',
            'high_rank': 'Unknown', 'skin_count': 0, 'hero_count': 0,
            'collector_tier': 'No Tier', 'collector_point': 0,
            'skin_breakdown': {}, 'hero_history': [], 'last_heroes_purchase': [],
            'emblem_levels': [], 'skin_history': [], 'last_match': None,
            'region_country': None, 'location': None, 'last_login': None,
            'creation_date': None, 'account_age': None, 'win_rate': None,
            'v2l_status': None, 'starlight_user': 'No', 'starlight_expiry': None,
            'starlight_months': 0, 'followers': 0, 'likes': 0, 'affinity': None,
            'credits_score': None, 'squad': None, 'squad_motto': None,
            'restriction_flags': None, 'achievement_points': 0,
            'mcl_champion_wins': 0, 'total_battles': 0, 'diamonds': 0,
            'battle_points': 0, 'tickets': 0, 'latest_skin_id': None,
            'latest_skin_date': None, 'last_diamond_purchase': None,
            'ban_code': None, 'ban_duration': None,
        }
    player['_device']      = device_id
    player['_account']     = conn.account_id
    player['_zone']        = conn.zone_id
    player['ban_status']   = 'BANNED'
    player['ban_reason']   = info.get('reason_name') or BAN_REASONS.get('21')
    player['ban_code']     = info.get('ban_code')
    player['ban_duration'] = format_ban_duration(info)
    return {'status': 'success', 'player': player, 'device_id': device_id,
            'account_id': conn.account_id, 'zone_id': conn.zone_id}


def _op_probe(device_id: str) -> dict:
    """Light probe for bulk — level, rank, ban, last login, collector."""
    conn = GameConn(device_id)
    try:
        ok, _ = conn.login()
        if not ok:
            return {'status': 'error', 'device_id': device_id, 'error': 'login failed'}
        if conn.ban_seen:
            return {'status': 'banned', 'device_id': device_id,
                    'ban_info': dict(conn.ban_info),
                    'reason': conn.ban_info.get('reason_name'),
                    'duration': format_ban_duration(conn.ban_info),
                    'account_id': conn.account_id, 'zone_id': conn.zone_id}
        ok, _ = conn.get_server()
        if not ok:
            return {'status': 'error', 'device_id': device_id, 'error': 'no server'}
        if conn.ban_seen:
            return {'status': 'banned', 'device_id': device_id,
                    'ban_info': dict(conn.ban_info),
                    'reason': conn.ban_info.get('reason_name'),
                    'duration': format_ban_duration(conn.ban_info),
                    'account_id': conn.account_id, 'zone_id': conn.zone_id}
        conn.enter_game()
        if not conn.handshake():
            return {'status': 'error', 'device_id': device_id, 'error': 'handshake failed'}
        result = conn.lookup(int(conn.account_id), 'id')
        if not result:
            return {'status': 'error', 'device_id': device_id, 'error': 'lookup empty'}
        if conn.ban_seen:
            return {'status': 'banned', 'device_id': device_id,
                    'ban_info': dict(conn.ban_info),
                    'reason': conn.ban_info.get('reason_name'),
                    'duration': format_ban_duration(conn.ban_info),
                    'account_id': conn.account_id, 'zone_id': conn.zone_id}
        p = result[0][0] if (result.get(0) and len(result[0]) > 0) else {}
        if not isinstance(p, dict):
            return {'status': 'error', 'device_id': device_id, 'error': 'bad payload'}
        lvl   = as_int(p.get(3, 0))
        rank  = map_rank(p.get(8)) if p.get(8) is not None else 'Unranked'
        lts   = as_int(p.get(5, 0))
        coll  = 0
        t136  = p.get(136, {})
        if isinstance(t136, dict): coll = as_int(t136.get(9, 0))
        created = conn.creation_ts or as_int(p.get(6, 0))
        return {'status': 'clean', 'device_id': device_id,
                'account_id': conn.account_id, 'zone_id': conn.zone_id,
                'level': lvl, 'rank': rank, 'last_login_ts': lts,
                'creation_ts': created,
                'collector_point': coll, 'collector_tier': map_collector(coll)}
    except Exception as e:
        return {'status': 'error', 'device_id': device_id, 'error': f'{type(e).__name__}'}
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════
# RETRY WRAPPER — 3 attempts with linear backoff
# ═══════════════════════════════════════════════════════════════════

def with_retries(fn, *args, retries: int = RETRIES, **kwargs) -> dict:
    last = None
    for attempt in range(retries):
        try:
            r = fn(*args, **kwargs)
        except Exception as e:
            r = {'status': 'error', 'error': f'{type(e).__name__}: {e}'}
        if r and r.get('status') in ('success', 'ok', 'clean', 'banned'):
            return r
        last = r
        if attempt < retries - 1:
            time.sleep(0.4 * (attempt + 1))
    return last if last is not None else {'status': 'error', 'error': 'all retries failed'}


# ═══════════════════════════════════════════════════════════════════
# RENDER HELPERS
# ═══════════════════════════════════════════════════════════════════

def esc(s) -> str:
    return html.escape(str(s if s is not None else "—"))


def fv(val, fallback='—'):
    if val is None: return fallback
    if isinstance(val, str):
        return val if val.strip() and val.strip().upper() != 'N/A' else fallback
    return val


def render_profile_html(p: dict) -> str:
    sb = p.get('skin_breakdown') or {}
    lm = p.get('last_match') or {}
    ban = p.get('ban_status', 'CLEAN')

    lines = []
    lines.append(f"<b>👤 {esc(fv(p.get('nickname'), '?'))}</b>")
    lines.append(f"<code>{esc(p.get('_device',''))}</code>")
    lines.append("")
    lines.append(f"🆔 acc <code>{p.get('_account',0)}</code> · zone <code>{p.get('_zone',0)}</code>")
    lines.append(f"🎚 level <b>{p.get('level',0)}</b>   📅 age {esc(fv(p.get('account_age')))}")
    lines.append(f"🏆 rank <b>{esc(fv(p.get('current_rank')))}</b> · peak {esc(fv(p.get('high_rank')))}")
    lines.append(f"💎 collector <b>{esc(fv(p.get('collector_tier')))}</b> ({p.get('collector_point',0):,} pts)")
    lines.append("")
    lines.append(f"🎨 heroes {p.get('hero_count',0):,} · skins {p.get('skin_count',0):,}")
    lines.append(f"   Supreme {sb.get('Supreme Skins',0)} · Grand {sb.get('Grand Skins',0)} · "
                 f"Exquisite {sb.get('Exquisite Skins',0)}")
    lines.append(f"   Deluxe {sb.get('Deluxe Skins',0)} · Exceptional {sb.get('Exceptional Skins',0)} · "
                 f"Common {sb.get('Common Skins',0)}")
    lines.append("")
    lines.append(f"⚔ battles {p.get('total_battles',0):,} · win rate {esc(fv(p.get('win_rate')))}")
    lines.append(f"💰 diamonds {p.get('diamonds',0):,} · BP {p.get('battle_points',0):,} · "
                 f"tickets {p.get('tickets',0):,}")
    lines.append("")
    lines.append(f"👥 followers {p.get('followers',0):,} · likes {p.get('likes',0):,}")
    lines.append(f"🏳 squad {esc(fv(p.get('squad')))}")
    lines.append(f"🌍 region {esc(fv(p.get('region_country')))} · loc {esc(fv(p.get('location')))}")
    lines.append("")
    lines.append(f"🕒 last login {esc(fv(p.get('last_login')))}")
    lines.append(f"📅 created {esc(fv(p.get('creation_date')))}")
    lines.append(f"🔓 v2l {esc(fv(p.get('v2l_status'), 'Unknown'))} · starlight {esc(fv(p.get('starlight_user')))}")
    lines.append(f"🚩 ban <b>{esc(ban)}</b>")
    if ban == 'BANNED':
        lines.append(f"   reason {esc(fv(p.get('ban_reason')))}")
        lines.append(f"   duration {esc(fv(p.get('ban_duration')))}")

    if lm:
        mvp = " ⭐MVP" if lm.get('mvp') else ""
        lines.append("")
        lines.append(f"🎮 last match · {esc(lm.get('hero','?'))} · "
                     f"<b>{esc(lm.get('result','?'))}</b> · {esc(lm.get('mode','?'))}{mvp}")
        lines.append(f"   KDA {esc(lm.get('kda','?'))} ({lm.get('kda_ratio',0)}) · "
                     f"{esc(lm.get('duration','?'))} · {esc(fv(lm.get('played_at')))}")

    return "\n".join(lines)


def render_ban_html(device_id: str, r: dict) -> str:
    if r.get('status') == 'clean':
        return (f"🚫 <b>BAN CHECK</b>\n"
                f"<code>{esc(device_id)}</code>\n\n"
                f"✅ <b>CLEAN</b> — no ban record seen.\n"
                f"acc <code>{r.get('account_id',0)}</code> · zone <code>{r.get('zone_id',0)}</code>")
    if r.get('status') == 'banned':
        return (f"🚫 <b>BAN CHECK</b>\n"
                f"<code>{esc(device_id)}</code>\n\n"
                f"⛔ <b>BANNED</b>\n"
                f"reason   <b>{esc(fv(r.get('reason')))}</b>\n"
                f"code     <code>{esc(fv(r.get('code')))}</code>\n"
                f"duration {esc(fv(r.get('duration')))}")
    return (f"🚫 <b>BAN CHECK</b>\n"
            f"<code>{esc(device_id)}</code>\n\n"
            f"⚠️ error: <code>{esc(r.get('error','unknown'))}</code>")


def render_creation_html(device_id: str, r: dict) -> str:
    if r.get('status') == 'ok':
        return (f"📅 <b>CREATION DATE</b>\n"
                f"<code>{esc(device_id)}</code>\n\n"
                f"🗓 created <b>{esc(r.get('created_at'))}</b>\n"
                f"⏳ age <b>{esc(r.get('age'))}</b>  ({r.get('age_days',0)} days)\n"
                f"acc <code>{r.get('account_id',0)}</code> · zone <code>{r.get('zone_id',0)}</code>")
    return (f"📅 <b>CREATION DATE</b>\n"
            f"<code>{esc(device_id)}</code>\n\n"
            f"⚠️ error: <code>{esc(r.get('error','unknown'))}</code>")


def render_bulk_summary(stats: dict, elapsed: float, path: str) -> str:
    total = max(stats.get('total', 0), 1)
    return (
        f"📦 <b>BULK CHECK</b> · <code>{esc(os.path.basename(path))}</code>\n\n"
        f"processed <b>{stats.get('done',0):,}</b> / {total:,}\n"
        f"✅ clean    <b>{stats.get('clean',0):,}</b>\n"
        f"⛔ banned   <b>{stats.get('banned',0):,}</b>\n"
        f"⚠️ failed   <b>{stats.get('fail',0):,}</b>\n\n"
        f"⏱ {human_dur(elapsed)} · {stats.get('done',0)/max(elapsed,0.001):.1f}/s · "
        f"{THREADS} threads · {RETRIES} retries"
    )


# ═══════════════════════════════════════════════════════════════════
# FILE HELPERS
# ═══════════════════════════════════════════════════════════════════

def parse_id_file(raw: str) -> list[str]:
    seen = set()
    ids  = []
    for line in raw.splitlines():
        line = line.strip().strip('"').strip("'")
        if not line or line.startswith('#'):
            continue
        if line.startswith(('and_', 'ios_')):
            key = line
        elif DEVICE_ID_RE.fullmatch(line):
            key = line
        elif len(line) >= 32 and re.match(r'^[0-9a-fA-F_\-]+$', line):
            key = line
        else:
            continue
        if key in seen:
            continue
        seen.add(key)
        ids.append(key)
    return ids


def file_from_lines(lines: list[str], name: str) -> InputFile:
    buf = io.BytesIO()
    buf.write("\n".join(lines).encode("utf-8"))
    buf.seek(0)
    return InputFile(buf, filename=name)


# ═══════════════════════════════════════════════════════════════════
# MENUS
# ═══════════════════════════════════════════════════════════════════

def main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🔍 Single Check",  callback_data="menu:single")],
        [InlineKeyboardButton("📦 Bulk Check",    callback_data="menu:bulk")],
        [InlineKeyboardButton("🚫 Ban Check",     callback_data="menu:ban"),
         InlineKeyboardButton("📅 Creation Date", callback_data="menu:created")],
        [InlineKeyboardButton("🔑 Redeem Key",    callback_data="menu:redeem"),
         InlineKeyboardButton("❓ Help",          callback_data="menu:help")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("👑 Admin Panel", callback_data="menu:admin")])
    return InlineKeyboardMarkup(rows)


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="menu:home")]])


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats",      callback_data="admin:stats"),
         InlineKeyboardButton("👥 Users",       callback_data="admin:users")],
        [InlineKeyboardButton("🔑 List Keys",  callback_data="admin:keys")],
        [InlineKeyboardButton("➕ Generate Key", callback_data="admin:gen")],
        [InlineKeyboardButton("🗑 Revoke Key",  callback_data="admin:revoke")],
        [InlineKeyboardButton("📣 Broadcast",   callback_data="admin:broadcast")],
        [InlineKeyboardButton("◀️ Back",        callback_data="menu:home")],
    ])


# ═══════════════════════════════════════════════════════════════════
# USER STATE (which prompt is user currently answering)
# ═══════════════════════════════════════════════════════════════════

USER_STATE: dict[int, str] = {}
STATE_LOCK = threading.Lock()


def set_state(tg_id: int, s: str | None):
    with STATE_LOCK:
        if s is None: USER_STATE.pop(tg_id, None)
        else:         USER_STATE[tg_id] = s


def get_state(tg_id: int) -> str | None:
    with STATE_LOCK:
        return USER_STATE.get(tg_id)


def clear_state(tg_id: int):
    with STATE_LOCK:
        USER_STATE.pop(tg_id, None)


# ═══════════════════════════════════════════════════════════════════
# HANDLERS — general
# ═══════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    DBX.ensure_user(u.id, u.username or u.first_name or "")
    is_admin = DBX.is_admin(u.id)
    ok, reason, _ = DBX.auth_status(u.id)

    head = (
        "🥔 <b>DRAKVEX</b> bot\n\n"
        "checks: single · bulk · ban · creation\n"
        "threads: 20 · retries: 3\n\n"
    )
    if is_admin:
        head += "👑 admin access confirmed\n— /admin to open the panel\n"
    elif ok:
        head += "🔓 key active\n"
    else:
        head += f"🔒 <b>{esc(reason)}</b>\n— tap Redeem Key to unlock\n"

    await update.message.reply_text(head, parse_mode=ParseMode.HTML, reply_markup=main_menu(is_admin))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🥔 <b>DRAKVEX</b>\n\n"
        "<b>Single Check</b> — one device id, full profile dump.\n"
        "<b>Bulk Check</b> — reply to a .txt with device ids (one per line), full parallel run.\n"
        "<b>Ban Check</b> — one id, ban only.\n"
        "<b>Creation Date</b> — one id, creation timestamp + account age.\n"
        "<b>Redeem Key</b> — attach a key to your account.\n\n"
        "<b>User commands</b>\n"
        "/start · /help · /menu\n"
        "/check <code>&lt;id&gt;</code> · /ban <code>&lt;id&gt;</code> · /created <code>&lt;id&gt;</code>\n"
        "/bulk (reply to file) · /key <code>&lt;key&gt;</code>\n"
        "/whoami · /usage\n\n"
        "<b>Admin commands</b>\n"
        "/admin — open the admin panel\n"
        "/genkey <code>&lt;tier&gt; &lt;uses&gt; &lt;days&gt;</code> — mint a key\n"
        "/revoke <code>&lt;key&gt;</code> — kill a key\n"
        "/broadcast <code>&lt;text&gt;</code> — message every non-admin user\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=back_kb())


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    DBX.ensure_user(u.id, u.username or "")
    await update.message.reply_text(
        "menu:",
        reply_markup=main_menu(DBX.is_admin(u.id))
    )


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    DBX.ensure_user(u.id, u.username or "")
    row = DBX.get_user(u.id) or {}
    key = row.get('key') or '—'
    kinfo = DBX.get_key(key) if key != '—' else None
    lines = [
        f"tg id <code>{u.id}</code>",
        f"admin {'✅' if DBX.is_admin(u.id) else '❌'}",
        f"banned {'✅' if row.get('banned') else '❌'}",
        f"key <code>{esc(key)}</code>",
    ]
    if kinfo:
        lines.append(f"tier {esc(kinfo['tier'])} · uses {kinfo['uses']}/{kinfo['max_uses'] or '∞'}")
        if kinfo['expires_at']:
            exp = datetime.datetime.fromtimestamp(kinfo['expires_at']).strftime("%Y-%m-%d")
            lines.append(f"expires {exp}")
    lines.append(f"checks used {row.get('checks_used', 0)}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_usage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    with DBX._lock, DBX._conn() as c:
        rows = c.execute(
            "SELECT kind, COUNT(*) AS n FROM usage_log WHERE tg_id=? GROUP BY kind",
            (u.id,)
        ).fetchall()
    if not rows:
        await update.message.reply_text("no usage yet.")
        return
    txt = "\n".join(f"{r['kind']}: {r['n']}" for r in rows)
    await update.message.reply_text(f"your usage:\n{txt}")


# ═══════════════════════════════════════════════════════════════════
# HANDLERS — admin commands
# ═══════════════════════════════════════════════════════════════════

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    DBX.ensure_user(u.id, u.username or "")
    if not DBX.is_admin(u.id):
        await update.message.reply_text("not admin.")
        return
    await update.message.reply_text(
        "👑 <b>Admin Panel</b>\n\n"
        "buttons below, or use commands:\n"
        "/genkey &lt;tier&gt; &lt;uses&gt; &lt;days&gt;\n"
        "/revoke &lt;key&gt;\n"
        "/broadcast &lt;text&gt;",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_menu(),
    )


async def cmd_genkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    DBX.ensure_user(u.id, u.username or "")
    if not DBX.is_admin(u.id):
        await update.message.reply_text("not admin.")
        return
    args = context.args or []
    if len(args) < 3:
        await update.message.reply_text(
            "usage: /genkey <tier> <uses> <days>\n"
            "e.g. /genkey pro 1000 30\n"
            "tiers: trial · basic · pro · unlimited"
        )
        return
    await _do_admin_gen(update, context, " ".join(args))


async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    DBX.ensure_user(u.id, u.username or "")
    if not DBX.is_admin(u.id):
        await update.message.reply_text("not admin.")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("usage: /revoke <key>")
        return
    await _do_admin_revoke(update, context, " ".join(args))


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    DBX.ensure_user(u.id, u.username or "")
    if not DBX.is_admin(u.id):
        await update.message.reply_text("not admin.")
        return
    text = " ".join(context.args or []).strip()
    if not text:
        await update.message.reply_text("usage: /broadcast <text>")
        return
    await _do_admin_broadcast(update, context, text)


# ─── callbacks ─────────────────────────────────────────────────────

async def cb_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    u = update.effective_user
    DBX.ensure_user(u.id, u.username or "")
    is_admin = DBX.is_admin(u.id)
    data = q.data or ""

    if data == "menu:home":
        clear_state(u.id)
        await q.edit_message_text("menu:", reply_markup=main_menu(is_admin))
        return

    ok, reason, _ = DBX.auth_status(u.id)

    if data == "menu:help":
        await q.edit_message_text(
            "🥔 <b>DRAKVEX</b>\n\n"
            "🔍 single check → send one device id → full profile\n"
            "📦 bulk check → send a .txt file with device ids (one per line)\n"
            "🚫 ban check → one id, ban only\n"
            "📅 creation date → one id, creation timestamp\n"
            "🔑 redeem key → attach a key to your account\n",
            parse_mode=ParseMode.HTML, reply_markup=back_kb()
        )
        return

    if not ok and data not in ("menu:redeem",):
        await q.edit_message_text(
            f"🔒 <b>{esc(reason)}</b>",
            parse_mode=ParseMode.HTML, reply_markup=main_menu(is_admin)
        )
        return

    if data == "menu:single":
        set_state(u.id, "single")
        await q.edit_message_text(
            "🔍 <b>Single Check</b>\n\nsend one device id now.",
            parse_mode=ParseMode.HTML, reply_markup=back_kb()
        )
    elif data == "menu:bulk":
        set_state(u.id, "bulk")
        await q.edit_message_text(
            f"📦 <b>Bulk Check</b>\n\nsend a <code>.txt</code> file with device ids, "
            f"one per line. cap {BULK_CAP:,} per run.",
            parse_mode=ParseMode.HTML, reply_markup=back_kb()
        )
    elif data == "menu:ban":
        set_state(u.id, "ban")
        await q.edit_message_text(
            "🚫 <b>Ban Check</b>\n\nsend one device id now.",
            parse_mode=ParseMode.HTML, reply_markup=back_kb()
        )
    elif data == "menu:created":
        set_state(u.id, "created")
        await q.edit_message_text(
            "📅 <b>Creation Date</b>\n\nsend one device id now.",
            parse_mode=ParseMode.HTML, reply_markup=back_kb()
        )
    elif data == "menu:redeem":
        set_state(u.id, "redeem")
        await q.edit_message_text(
            "🔑 <b>Redeem Key</b>\n\nsend the key now. format "
            "<code>DRKV-XXXX-XXXX-XXXX-XXXX-XXXX</code>",
            parse_mode=ParseMode.HTML, reply_markup=back_kb()
        )
    elif data == "menu:admin":
        if not is_admin:
            await q.edit_message_text("not admin.", reply_markup=main_menu(is_admin))
            return
        await q.edit_message_text("👑 <b>Admin Panel</b>",
                                  parse_mode=ParseMode.HTML, reply_markup=admin_menu())


async def cb_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    u = update.effective_user
    DBX.ensure_user(u.id, u.username or "")
    if not DBX.is_admin(u.id):
        await q.edit_message_text("not admin.")
        return
    data = q.data

    if data == "admin:stats":
        s = DBX.stats()
        text = (
            f"📊 <b>Stats</b>\n\n"
            f"users     <b>{s['users']:,}</b>\n"
            f"keys      <b>{s['keys']:,}</b>  (active {s['active']:,})\n"
            f"checks    <b>{s['checks']:,}</b>\n"
            f"today     <b>{s['today']:,}</b>\n"
            f"threads   <b>{THREADS}</b>  retries <b>{RETRIES}</b>\n"
        )
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=admin_menu())
    elif data == "admin:users":
        users = DBX.list_users(30)
        lines = ["👥 <b>Recent Users</b>\n"]
        for r in users:
            flag = "👑" if r['is_admin'] else ("🚫" if r['banned'] else "·")
            key = r['key'] or '—'
            lines.append(f"{flag} <code>{r['tg_id']}</code> {esc(r['username'] or '')} "
                         f"· {r['checks_used']} · {esc(key[:14])}")
        await q.edit_message_text("\n".join(lines),
                                  parse_mode=ParseMode.HTML, reply_markup=admin_menu())
    elif data == "admin:keys":
        keys = DBX.list_keys(40)
        lines = ["🔑 <b>Recent Keys</b>\n"]
        for k in keys:
            rv = "🗑" if k['revoked'] else "✅"
            exp = "∞" if not k['expires_at'] else datetime.datetime.fromtimestamp(
                k['expires_at']).strftime("%m-%d")
            lines.append(
                f"{rv} <code>{k['key']}</code>\n"
                f"   tier {esc(k['tier'])} · uses {k['uses']}/{k['max_uses'] or '∞'} · exp {exp}"
            )
        await q.edit_message_text("\n".join(lines),
                                  parse_mode=ParseMode.HTML, reply_markup=admin_menu())
    elif data == "admin:gen":
        set_state(u.id, "admin_gen")
        await q.edit_message_text(
            "➕ <b>Generate Key</b>\n\nsend tier · uses · days\n"
            "e.g. <code>pro 1000 30</code>\n\n"
            "tiers: trial · basic · pro · unlimited",
            parse_mode=ParseMode.HTML, reply_markup=back_kb()
        )
    elif data == "admin:revoke":
        set_state(u.id, "admin_revoke")
        await q.edit_message_text(
            "🗑 <b>Revoke Key</b>\n\nsend the key to revoke.",
            parse_mode=ParseMode.HTML, reply_markup=back_kb()
        )
    elif data == "admin:broadcast":
        set_state(u.id, "admin_broadcast")
        await q.edit_message_text(
            "📣 <b>Broadcast</b>\n\nsend the message text to broadcast to all non-admin users.",
            parse_mode=ParseMode.HTML, reply_markup=back_kb()
        )


# ═══════════════════════════════════════════════════════════════════
# DEVICE-ID EXTRACTION FROM MESSAGE
# ═══════════════════════════════════════════════════════════════════

def first_device_id(text: str) -> str | None:
    if not text: return None
    m = DEVICE_ID_RE.search(text)
    if m: return m.group(0)
    t = text.strip()
    if t.startswith(('and_', 'ios_')) and len(t) > 40:
        return t.split()[0]
    return None


# ═══════════════════════════════════════════════════════════════════
# HANDLERS — text messages
# ═══════════════════════════════════════════════════════════════════

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    msg = update.message
    DBX.ensure_user(u.id, u.username or "")
    state = get_state(u.id)
    text = (msg.text or "").strip()

    # slash commands first
    if text.startswith("/check") or text.startswith("/single"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await msg.reply_text("usage: /check <device_id>")
            return
        await _do_single(update, context, parts[1].strip())
        return
    if text.startswith("/ban"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await msg.reply_text("usage: /ban <device_id>")
            return
        await _do_ban(update, context, parts[1].strip())
        return
    if text.startswith("/created"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await msg.reply_text("usage: /created <device_id>")
            return
        await _do_created(update, context, parts[1].strip())
        return
    if text.startswith("/key"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await msg.reply_text("usage: /key <your_key>")
            return
        await _do_redeem(update, context, parts[1].strip())
        return

    # state-routed
    if state == "redeem":
        clear_state(u.id)
        await _do_redeem(update, context, text)
        return
    if state == "single":
        clear_state(u.id)
        await _do_single(update, context, text)
        return
    if state == "ban":
        clear_state(u.id)
        await _do_ban(update, context, text)
        return
    if state == "created":
        clear_state(u.id)
        await _do_created(update, context, text)
        return
    if state == "admin_gen" and DBX.is_admin(u.id):
        clear_state(u.id)
        await _do_admin_gen(update, context, text)
        return
    if state == "admin_revoke" and DBX.is_admin(u.id):
        clear_state(u.id)
        await _do_admin_revoke(update, context, text)
        return
    if state == "admin_broadcast" and DBX.is_admin(u.id):
        clear_state(u.id)
        await _do_admin_broadcast(update, context, text)
        return

    # idle nudge
    if first_device_id(text):
        await msg.reply_text(
            "pick a mode first, then send the id.\n"
            "or use /check · /ban · /created.",
            reply_markup=main_menu(DBX.is_admin(u.id))
        )
    else:
        await msg.reply_text(
            "menu:",
            reply_markup=main_menu(DBX.is_admin(u.id))
        )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    DBX.ensure_user(u.id, u.username or "")
    state = get_state(u.id)

    caption = (update.message.caption or "").lower()
    is_bulk = state == "bulk" or "/bulk" in caption

    ok, reason, _ = DBX.auth_status(u.id)
    if not ok and not DBX.is_admin(u.id):
        await update.message.reply_text(f"🔒 {reason}")
        return
    if not is_bulk:
        await update.message.reply_text(
            "to bulk-check, tap 📦 Bulk Check then send the file — or reply with /bulk caption."
        )
        return
    clear_state(u.id)

    doc = update.message.document
    if not doc:
        await update.message.reply_text("no document.")
        return
    if doc.file_size and doc.file_size > 5 * 1024 * 1024:
        await update.message.reply_text("file too big (max 5 MB).")
        return

    tg_file = await doc.get_file()
    buf = io.BytesIO()
    await tg_file.download_to_memory(buf)
    raw = buf.getvalue().decode("utf-8", errors="replace")

    ids = parse_id_file(raw)
    if not ids:
        await update.message.reply_text("no valid device ids found in file.")
        return
    if len(ids) > BULK_CAP:
        ids = ids[:BULK_CAP]
        await update.message.reply_text(f"capped at {BULK_CAP:,} ids per run.")

    await _run_bulk(update, context, ids, doc.file_name or "bulk.txt")


# ═══════════════════════════════════════════════════════════════════
# OPERATION RUNNERS
# ═══════════════════════════════════════════════════════════════════

async def _do_redeem(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str):
    u = update.effective_user
    ok, msg = DBX.redeem_key(u.id, key)
    if ok:
        await update.message.reply_text(
            f"🔓 <b>key redeemed</b>\n\n{esc(msg)}",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu(DBX.is_admin(u.id))
        )
    else:
        await update.message.reply_text(f"❌ {esc(msg)}")


async def _do_single(update: Update, context: ContextTypes.DEFAULT_TYPE, raw: str):
    u = update.effective_user
    ok, reason, _ = DBX.auth_status(u.id)
    if not ok and not DBX.is_admin(u.id):
        await update.message.reply_text(f"🔒 {reason}")
        return
    device_id = first_device_id(raw)
    if not device_id:
        await update.message.reply_text("no valid device id in that message.")
        return

    msg = await update.message.reply_text(f"🔍 checking <code>{esc(device_id[:32])}…</code>",
                                           parse_mode=ParseMode.HTML)
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(EXECUTOR, lambda: with_retries(_op_single_check, device_id))
    except Exception as e:
        res = {'status': 'error', 'error': str(e)}

    DBX.log_usage(u.id, 'single', device_id)
    if not DBX.is_admin(u.id):
        DBX.consume_use(u.id)

    if res.get('status') != 'success':
        await msg.edit_text(f"⚠️ <b>error</b>\n<code>{esc(res.get('error','unknown'))}</code>",
                            parse_mode=ParseMode.HTML)
        return

    html_out = render_profile_html(res['player'])
    if len(html_out) > 3900:
        html_out = html_out[:3900] + "…"
    try:
        await msg.edit_text(html_out, parse_mode=ParseMode.HTML)
    except Exception:
        await msg.edit_text(html_out[:3800])


async def _do_ban(update: Update, context: ContextTypes.DEFAULT_TYPE, raw: str):
    u = update.effective_user
    ok, reason, _ = DBX.auth_status(u.id)
    if not ok and not DBX.is_admin(u.id):
        await update.message.reply_text(f"🔒 {reason}")
        return
    device_id = first_device_id(raw)
    if not device_id:
        await update.message.reply_text("no valid device id in that message.")
        return

    msg = await update.message.reply_text(f"🚫 checking ban for <code>{esc(device_id[:32])}…</code>",
                                           parse_mode=ParseMode.HTML)
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(EXECUTOR, lambda: with_retries(_op_ban_check, device_id))
    except Exception as e:
        res = {'status': 'error', 'error': str(e)}

    DBX.log_usage(u.id, 'ban', device_id)
    if not DBX.is_admin(u.id):
        DBX.consume_use(u.id)

    await msg.edit_text(render_ban_html(device_id, res), parse_mode=ParseMode.HTML)


async def _do_created(update: Update, context: ContextTypes.DEFAULT_TYPE, raw: str):
    u = update.effective_user
    ok, reason, _ = DBX.auth_status(u.id)
    if not ok and not DBX.is_admin(u.id):
        await update.message.reply_text(f"🔒 {reason}")
        return
    device_id = first_device_id(raw)
    if not device_id:
        await update.message.reply_text("no valid device id in that message.")
        return

    msg = await update.message.reply_text(f"📅 checking creation for <code>{esc(device_id[:32])}…</code>",
                                           parse_mode=ParseMode.HTML)
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(EXECUTOR, lambda: with_retries(_op_creation_check, device_id))
    except Exception as e:
        res = {'status': 'error', 'error': str(e)}

    DBX.log_usage(u.id, 'created', device_id)
    if not DBX.is_admin(u.id):
        DBX.consume_use(u.id)

    await msg.edit_text(render_creation_html(device_id, res), parse_mode=ParseMode.HTML)


# ─── bulk ──────────────────────────────────────────────────────────

async def _run_bulk(update: Update, context: ContextTypes.DEFAULT_TYPE,
                    ids: list[str], filename: str):
    u = update.effective_user
    total = len(ids)

    stats = {'total': total, 'done': 0, 'clean': 0, 'banned': 0, 'fail': 0}
    lock = threading.Lock()
    results: list[dict] = []

    status_msg = await update.message.reply_text(
        f"📦 starting bulk · {total:,} ids · {THREADS} threads · {RETRIES} retries",
    )
    loop = asyncio.get_running_loop()
    t0 = time.time()

    def run_one(device_id: str) -> dict:
        try:
            r = with_retries(_op_probe, device_id)
        except Exception as e:
            r = {'status': 'error', 'device_id': device_id, 'error': str(e)}
        r['device_id'] = device_id
        with lock:
            stats['done'] += 1
            st = r.get('status')
            if st == 'clean':   stats['clean'] += 1
            elif st == 'banned': stats['banned'] += 1
            else:                stats['fail'] += 1
        return r

    async def progress_loop():
        last = time.time()
        while True:
            await asyncio.sleep(1.0)
            now = time.time()
            if now - last >= PROGRESS_MS / 1000:
                last = now
                try:
                    await status_msg.edit_text(
                        f"📦 running…  {stats['done']:,}/{total:,}\n"
                        f"✅ {stats['clean']:,}   ⛔ {stats['banned']:,}   ⚠️ {stats['fail']:,}\n"
                        f"⏱ {human_dur(now - t0)}"
                    )
                except Exception:
                    pass
            if stats['done'] >= total:
                return

    tasks = [
        asyncio.create_task(loop.run_in_executor(EXECUTOR, run_one, d))
        for d in ids
    ]
    prog_task = asyncio.create_task(progress_loop())

    try:
        for coro in asyncio.as_completed(tasks):
            try:
                r = await coro
                if r: results.append(r)
            except Exception:
                pass
    finally:
        try: prog_task.cancel()
        except Exception: pass

    elapsed = time.time() - t0

    DBX.log_usage(u.id, f'bulk:{total}', filename)
    if not DBX.is_admin(u.id):
        for _ in range(total):
            DBX.consume_use(u.id)

    summary = render_bulk_summary(stats, elapsed, filename)
    try:
        await status_msg.edit_text(summary, parse_mode=ParseMode.HTML)
    except Exception:
        await update.message.reply_text(summary, parse_mode=ParseMode.HTML)

    hit_lines    = []
    clean_lines  = []
    banned_lines = []
    all_lines    = []

    for r in results:
        d  = r.get('device_id', '')
        st = r.get('status', 'error')
        if st == 'clean':
            clean_lines.append(d)
            hit_lines.append(d)
            all_lines.append(
                f"{d} | clean | lv{r.get('level',0)} | {r.get('rank','?')} | "
                f"coll={r.get('collector_point',0)} | "
                f"last={fmt_ts(r.get('last_login_ts',0)) or '?'}"
            )
        elif st == 'banned':
            banned_lines.append(d)
            hit_lines.append(d)
            all_lines.append(f"{d} | banned | {r.get('reason','?')} | {r.get('duration','?')}")
        else:
            all_lines.append(f"{d} | error | {r.get('error','?')}")

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = re.sub(r'[^A-Za-z0-9_\-]', '_', os.path.splitext(filename)[0])[:32] or "bulk"

    try:
        if hit_lines:
            await update.message.reply_document(
                document=file_from_lines(hit_lines, f"{stem}_{ts}_hits.txt"),
                caption=f"🎯 hits ({len(hit_lines):,})"
            )
        if clean_lines:
            await update.message.reply_document(
                document=file_from_lines(clean_lines, f"{stem}_{ts}_clean.txt"),
                caption=f"✅ clean ({len(clean_lines):,})"
            )
        if banned_lines:
            await update.message.reply_document(
                document=file_from_lines(banned_lines, f"{stem}_{ts}_banned.txt"),
                caption=f"⛔ banned ({len(banned_lines):,})"
            )
        if all_lines:
            await update.message.reply_document(
                document=file_from_lines(all_lines, f"{stem}_{ts}_all.txt"),
                caption=f"📄 full report ({len(all_lines):,})"
            )
    except Exception as e:
        await update.message.reply_text(f"file send failed: {esc(str(e))}")


# ─── admin helpers ─────────────────────────────────────────────────

async def _do_admin_gen(update: Update, context: ContextTypes.DEFAULT_TYPE, raw: str):
    u = update.effective_user
    parts = raw.split()
    if len(parts) < 3:
        await update.message.reply_text("usage: <tier> <uses> <days>  e.g. pro 1000 30")
        return
    tier = parts[0].lower()
    if tier not in ("trial", "basic", "pro", "unlimited"):
        await update.message.reply_text("tier must be one of: trial · basic · pro · unlimited")
        return
    try:
        uses = int(parts[1]); days = int(parts[2])
    except ValueError:
        await update.message.reply_text("uses and days must be integers.")
        return
    key = DBX.generate_key(tier, uses, days, u.id)
    await update.message.reply_text(
        f"🔑 <b>key generated</b>\n\n<code>{esc(key)}</code>\n\n"
        f"tier {esc(tier)} · uses {uses or '∞'} · days {days or '∞'}",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_menu()
    )


async def _do_admin_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE, raw: str):
    key = raw.strip().upper()
    if DBX.revoke_key(key):
        await update.message.reply_text(f"🗑 revoked <code>{esc(key)}</code>",
                                        parse_mode=ParseMode.HTML,
                                        reply_markup=admin_menu())
    else:
        await update.message.reply_text("no such key.")


async def _do_admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, raw: str):
    u = update.effective_user
    text = raw.strip()
    if not text:
        await update.message.reply_text("empty message.")
        return
    users = DBX.list_users(5000)
    sent = 0; failed = 0
    for r in users:
        if r['tg_id'] == u.id or r['is_admin']:
            continue
        try:
            await context.bot.send_message(chat_id=r['tg_id'], text=text)
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(
        f"📣 broadcast done · sent {sent} · failed {failed}",
        reply_markup=admin_menu()
    )


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("menu",      cmd_menu))
    app.add_handler(CommandHandler("whoami",    cmd_whoami))
    app.add_handler(CommandHandler("usage",     cmd_usage))

    # admin commands — previously missing entirely
    app.add_handler(CommandHandler("admin",     cmd_admin))
    app.add_handler(CommandHandler("panel",     cmd_admin))   # alias
    app.add_handler(CommandHandler("genkey",    cmd_genkey))
    app.add_handler(CommandHandler("revoke",    cmd_revoke))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))

    app.add_handler(CallbackQueryHandler(cb_admin, pattern=r"^admin:"))
    app.add_handler(CallbackQueryHandler(cb_menu,  pattern=r"^menu:"))

    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print(f"[+] DRAKVEX bot online · {THREADS} threads · {RETRIES} retries · db={DB_PATH}")
    print(f"[+] boot admins: {sorted(BOOT_ADMINS)}")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
