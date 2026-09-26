#!/usr/bin/env python3
"""
DRAKVEX Telegram bot v4 — plain-text quote-format MLBB lookup, no JSON.

Install:
  pip install -r requirements.txt

Run:
  python drakvex_bot.py
"""
from __future__ import annotations

import asyncio
import datetime
import html
import io
import json
import logging
import os
import socket
import struct
import threading
import time
import zlib
from enum import Enum

import zstandard as zstd
from Crypto.Cipher import AES
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
)


# ═══════════════════════════════════════════════════════════════════════
# HARDCODED CONFIG
# ═══════════════════════════════════════════════════════════════════════
BOT_TOKEN = "8889570647:AAHigHsrWZ059UnZeRP4MVbld0vZt5c5yyA"
ADMIN_ID  = 8621676055

LOGIN_DEVICES = [
    "and_cd9e459ea708a948d5c2f5a6ca8838cf648efdbc9a8c3ce703fa556d"
    "-34a4-4cde-a061-34938d08a26e",
]
_env_devs = os.environ.get("DRAKVEX_LOGIN_DEVICES", "").strip()
if _env_devs:
    LOGIN_DEVICES = [d.strip() for d in _env_devs.split(",") if d.strip()]

MAX_RETRIES = 10
RETRY_DELAY = 3.0

LOGIN_HOST     = "login.ml.youngjoygame.com"
LOGIN_PORT     = 30021
CLIENT_VERSION = "2.2.16.1232.1"
CHANNEL        = "and_usa"

AES_KEY = bytes.fromhex("f5a193d50ade553e9835595f5cd75ddd")
AES_IV  = b"\x00" * 16

DEBUG = os.environ.get("DRAKVEX_DEBUG", "0") == "1"

GROUPS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "drakvex_groups.json")
STATS_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "drakvex_stats.json")

_log = logging.getLogger("drakvex")


# ═══════════════════════════════════════════════════════════════════════
# SDP CODEC
# ═══════════════════════════════════════════════════════════════════════
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
    return (0, k) if isinstance(k, int) else (1, str(k))


class SdpStruct(dict):
    def __init__(self, data=None):
        super().__init__()
        self.data = b""
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
        if not self.data:
            return
        if self.data[0] >> 4 == SdpType.STRUCT_BEGIN.value:
            self.offset = 1
        while self.offset < len(self.data):
            tag, value = self._unpack()
            if isinstance(value, SdpValue) if False else isinstance(value, SdpType) and value == SdpType.STRUCT_END:
                break
            self[tag] = value

    def _write_number(self, value):
        out = bytearray()
        while value >= 0x80:
            out.append((value & 0x7F) | 0x80)
            value >>= 7
        out.append(value & 0x7F)
        return bytes(out)

    def _read_number(self):
        if self.offset >= len(self.data):
            raise SdpError("varint eof")
        n = 1
        val = self.data[self.offset] & 0x7F
        while self.data[self.offset + n - 1] >= 0x80:
            if self.offset + n >= len(self.data):
                raise SdpError("varint truncated")
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
            self.data += self._write_number(len(packed)) + packed
        elif isinstance(value, (str, bytes)):
            self._pack_header(tag, SdpType.STRING)
            encoded = value.encode("utf-8") if isinstance(value, str) else value
            self.data += self._write_number(len(encoded)) + encoded
        elif isinstance(value, list):
            self._pack_header(tag, SdpType.LIST)
            self.data += self._write_number(len(value))
            for item in value:
                self._pack(0, item)
        elif isinstance(value, dict):
            self._pack_header(tag, SdpType.DICT)
            self.data += self._write_number(len(value))
            for k, v in sorted(value.items(), key=_sdp_sort_key):
                self._pack(0, k)
                self._pack(0, v)
        else:
            raise SdpError(f"unsupported type {type(value)}")

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
                raw = self._read_number().to_bytes(4, "little")
                return tag, struct.unpack("<f", raw)[0]
            if dtype == SdpType.DOUBLE:
                raw = self._read_number().to_bytes(8, "little")
                return tag, struct.unpack("<d", raw)[0]
            if dtype == SdpType.STRING:
                length = self._read_number()
                chunk = self.data[self.offset:self.offset + length]
                try:
                    val = chunk.decode("utf-8")
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
            raise SdpError("bad data type")
        except SdpError:
            raise
        except Exception:
            raise SdpError("unpack failed")


def _aes_decrypt(data):
    if not data or len(data) % 16:
        raise SdpError(f"bad cipher length {len(data) if data else 0}")
    return AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV).decrypt(data).rstrip(b"\x00")


# ═══════════════════════════════════════════════════════════════════════
# MAPS
# ═══════════════════════════════════════════════════════════════════════
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

MODE_MAP = {0: "Classic", 1: "Ranked", 2: "Brawl",
            3: "AI", 4: "Custom", 5: "Mayhem", 6: "Arcade"}

COLLECTOR_TIERS = [
    (1000, 4000, "Amateur Collector"),
    (4000, 10000, "Junior Collector"),
    (10000, 22000, "Seasoned Collector"),
    (22000, 44000, "Expert Collector"),
    (44000, 84000, "Renowned Collector"),
    (84000, 160000, "Exalted Collector"),
    (160000, 280000, "Mega Collector"),
    (280000, float("inf"), "World Collector"),
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
    if 136 <= p <= 160:
        return f"Mythic {p - 135}"
    if 161 <= p <= 195:
        return f"Mythical Honor {p - 135}"
    if 196 <= p <= 235:
        return f"Mythical Glory {p - 195}"
    if p >= 236:
        return f"Mythical Immortal {p - 235}"
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


PHT = datetime.timezone(datetime.timedelta(hours=8))


def fmt_ts(ts):
    if not isinstance(ts, int) or ts < 100000000:
        return None
    try:
        utc = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        return (utc + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return None


def fmt_ts_full(ts):
    if not isinstance(ts, int) or ts < 100000000:
        return None
    try:
        utc = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        return (utc + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def human_dur(sec):
    sec = float(max(0, sec))
    if sec < 1:
        return f"{int(sec * 1000)}ms"
    if sec < 60:
        return f"{sec:.1f}s"
    m, s = divmod(int(sec), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, rem = divmod(m, 60)
    return f"{h}h{rem:02d}m"


def _rel(ts, now=None):
    """Return "0d 0h 29m ago" or "in 4d 13h 48m" style relative string."""
    if not isinstance(ts, int) or ts < 100000000:
        return None
    if now is None:
        now = int(time.time())
    diff = now - ts
    future = diff < 0
    diff = abs(diff)
    d = diff // 86400
    h = (diff % 86400) // 3600
    m = (diff % 3600) // 60
    body = f"{d}d {h}h {m}m"
    return f"in {body}" if future else f"{body} ago"


def as_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ═══════════════════════════════════════════════════════════════════════
# GAME CONNECTION
# ═══════════════════════════════════════════════════════════════════════
def _device_fingerprint(device_id):
    parts = device_id.split("_")
    info = parts[1] if len(parts) >= 2 else device_id
    if len(parts) >= 3 and len(info) < 32:
        info = info + "_" + parts[2]
    if len(info) >= 48:
        return info[:32], info[32:48], info[48:]
    if len(info) >= 32:
        return info[:32], info[32:], ""
    return info, "", ""


class GameConn:
    def __init__(self, device_id):
        self.device_id = device_id
        self.host = LOGIN_HOST
        self.port = LOGIN_PORT
        self.sequence = 1
        self.socket = None
        self.queue = b""
        self.last_header_size = 0

        self.imei_md5, self.android_id, self.advertising_id = \
            _device_fingerprint(device_id)

        self.account_id = 0
        self.session_key = ""
        self.zone_id = 0
        self.game_host = ""
        self.game_port = 0
        self.creation_ts = 0
        self.last_login_diag = {}

    def connect(self, host=None, port=None, timeout=10):
        if host:
            self.host = host
        if port:
            self.port = port
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.settimeout(timeout)
        self.socket.connect((self.host, self.port))

    def close(self):
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
        self.socket = None
        self.sequence = 1
        self.queue = b""

    def send(self, pid, sdp):
        packet = SdpStruct({0: pid, 1: self.sequence, 5: sdp.data}).data
        buf = zstd.compress(packet)
        size = len(buf) + 4
        if size > 0xFFFFFF:
            raise SdpError("packet too large")
        flags = size | (16 << 24)
        self.socket.send(flags.to_bytes(4, "big") + buf)
        self.sequence += 1

    def recv(self):
        try:
            while len(self.queue) < 4:
                d = self.socket.recv(4096)
                if not d:
                    return None, None
                self.queue += d
            flags = int.from_bytes(self.queue[:4], "big")
            size = flags & 0xFFFFFF
            ctype = flags >> 24
            self.last_header_size = size
            while len(self.queue) < size:
                d = self.socket.recv(4096)
                if not d:
                    return None, None
                self.queue += d
            data = self.queue[4:size]
            self.queue = self.queue[size:]

            if ctype == 1:
                data = zlib.decompress(data)
            elif ctype == 16:
                data = zstd.decompress(data)
            elif ctype == 2:
                data = _aes_decrypt(data)
            elif ctype == 3:
                data = zlib.decompress(_aes_decrypt(data))
            elif ctype == 18:
                data = zstd.decompress(_aes_decrypt(data))

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
            return pid, parsed
        except socket.timeout:
            return -1, None
        except Exception:
            return None, None

    def login(self, inner_tries=3, debug=None):
        if debug is None:
            debug = DEBUG
        last_err = "no attempt ran"
        last_pid = None
        last_keys = None

        for attempt in range(inner_tries):
            try:
                self.connect(LOGIN_HOST, LOGIN_PORT, timeout=12)
            except Exception as e:
                last_err = f"connect {type(e).__name__}: {e}"
                if debug:
                    _log.warning("[login %d/%d] %s",
                                 attempt + 1, inner_tries, last_err)
                self.close()
                time.sleep(0.8)
                continue

            try:
                self.send(1, SdpStruct({
                    0: self.device_id,
                    1: (f"gps_adid={self.advertising_id}"
                        f"&android_id={self.android_id}"
                        f"&device_unique_id={self.imei_md5}"),
                    2: CLIENT_VERSION,
                    3: CHANNEL,
                    4: "en",
                }))
            except Exception as e:
                last_err = f"send {type(e).__name__}: {e}"
                if debug:
                    _log.warning("[login %d/%d] %s",
                                 attempt + 1, inner_tries, last_err)
                self.close()
                time.sleep(0.8)
                continue

            got_anything = False
            for _ in range(10):
                pid, res = self.recv()
                if pid is None:
                    last_err = "connection closed by server"
                    break
                if pid == -1:
                    last_err = "recv timeout"
                    continue

                got_anything = True
                last_pid = pid
                last_keys = list(res.keys()) if res else None
                if debug:
                    _log.info("[login %d/%d] pid=%s keys=%s",
                              attempt + 1, inner_tries, pid, last_keys)

                if pid == 2 and res:
                    self.account_id = res.get(0)
                    self.session_key = res.get(1)
                    zd = res.get(2)
                    if isinstance(zd, dict):
                        self.zone_id = zd.get(0, 0)
                    elif isinstance(zd, list) and zd:
                        self.zone_id = (zd[0]
                                        if not isinstance(zd[0], dict)
                                        else zd[0].get(0, 0))
                    else:
                        self.zone_id = zd or 0
                    self.creation_ts = res.get(19, 0)
                    self.last_login_diag = {
                        "pid": pid, "attempt": attempt + 1, "keys": last_keys,
                    }
                    return True, dict(self.last_login_diag)

                self.last_login_diag = {
                    "pid": pid, "attempt": attempt + 1, "keys": last_keys,
                }
                return False, dict(self.last_login_diag)

            if not got_anything and debug:
                _log.warning("[login %d/%d] no packets: %s",
                             attempt + 1, inner_tries, last_err)

            self.close()
            if attempt < inner_tries - 1:
                time.sleep(1.0 * (attempt + 1))

        self.last_login_diag = {
            "pid": last_pid, "keys": last_keys, "error": last_err,
        }
        return False, dict(self.last_login_diag)

    def get_server(self):
        self.send(5, SdpStruct({
            0: self.account_id, 1: self.session_key, 2: CLIENT_VERSION,
            5: self.zone_id, 6: CHANNEL,
        }))
        pid, res = self.recv()
        if pid == 6 and res:
            addr = res[1]
            self.game_host, port_str = addr.split(":")
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
            if pid is None or pid == -1:
                return False
            if pid == 10002:
                return True
        return False

    def lookup(self, value, kind="id"):
        if kind == "id":
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
            if pid is None or pid == -1:
                break
            if pid == 20001:
                continue
            if pid == 10129 and res is not None:
                if best is None:
                    best = res
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
                if pid is None:
                    break
                if pid == -1:
                    timeouts += 1
                elif pid == 10144:
                    return res
                elif pid == 20001:
                    continue
        return None

    def v2l_status(self, role_id, zone_id, tries=2):
        for _ in range(tries):
            self.send(10208, SdpStruct({0: int(role_id), 1: int(zone_id)}))
            timeouts = 0
            while timeouts < 2:
                pid, res = self.recv()
                if pid is None:
                    break
                if pid == -1:
                    timeouts += 1
                elif pid == 10208 and res:
                    return {"_src": 10208, "_data": dict(res)}
                elif pid == 20001:
                    continue
            self.send(10145, SdpStruct({0: int(role_id), 1: int(zone_id)}))
            timeouts = 0
            while timeouts < 2:
                pid, res = self.recv()
                if pid is None:
                    break
                if pid == -1:
                    timeouts += 1
                elif pid in (10146, 10160) and res:
                    return {"_src": pid, "_data": dict(res)}
                elif pid == 20001:
                    continue
        return None


# ═══════════════════════════════════════════════════════════════════════
# EXTRACTOR
# ═══════════════════════════════════════════════════════════════════════
def _find_last_match_ts(entry):
    if not isinstance(entry, dict):
        return 0
    now = int(time.time())
    lower, upper = now - 3 * 365 * 86400, now + 2 * 86400

    def _norm(v):
        if not isinstance(v, int) or isinstance(v, bool):
            return None
        if v > 10 ** 15:
            v //= 1_000_000
        elif v > 10 ** 12:
            v //= 1_000
        return v if lower <= v <= upper else None

    for key in (9, 10, 6, 7, 8, 11, 12, 13, 14, 15, 16, 17, 18):
        t = _norm(entry.get(key))
        if t:
            return t
    for v in entry.values():
        t = _norm(v)
        if t:
            return t
    return 0


def extract_last_match(ri, p):
    containers = []
    for src in (ri, p):
        if not isinstance(src, dict):
            continue
        for tag in (120, 130, 145, 150, 190, 200):
            v = src.get(tag)
            if isinstance(v, (dict, list)) and v:
                containers.append(v)
    entry = None
    for c in containers:
        if isinstance(c, list) and c and isinstance(c[0], dict):
            entry = c[0]
            break
        if isinstance(c, dict):
            for k in sorted(c.keys(), key=lambda x: (isinstance(x, str), x)):
                if isinstance(c[k], dict):
                    entry = c[k]
                    break
            if entry:
                break
    if not entry:
        return None
    hero_id = entry.get(0, 0) or entry.get(1, 0) or 0
    hero = HERO_ID_MAP.get(hero_id, f"Hero#{hero_id}") if hero_id else "?"
    kills   = as_int(entry.get(2, 0))
    deaths  = as_int(entry.get(3, 0))
    assists = as_int(entry.get(4, 0))
    duration = as_int(entry.get(5, 0))
    result_raw = entry.get(6, entry.get(7, 0))
    mode_raw = entry.get(8, 0)
    ts = _find_last_match_ts(entry)
    mvp = entry.get(11, 0)
    gold = as_int(entry.get(12, 0))
    if result_raw in (1, "1", "win", "WIN", True):
        result = "WIN"
    elif result_raw in (0, "0", "loss", "LOSS", False):
        result = "LOSS"
    else:
        result = "?"
    mode_key = as_int(mode_raw, -1)
    mode = MODE_MAP.get(mode_key, f"Mode {mode_raw}")
    return {
        "hero": hero,
        "kills": kills, "deaths": deaths, "assists": assists,
        "kda": f"{kills}/{deaths}/{assists}",
        "kda_ratio": round((kills + assists) / max(deaths, 1), 2),
        "result": result,
        "mode": mode,
        "duration": human_dur(duration) if duration else "?",
        "gold": gold,
        "mvp": bool(mvp),
        "played_at": fmt_ts(ts),
    }


def extract_player(result, role_info=None, creation_ts=0, v2l=None):
    if not result or not result.get(0) or len(result[0]) == 0:
        return None
    try:
        p = result[0][0]
        ri = role_info if isinstance(role_info, dict) else {}

        nickname  = p.get(2, "") or "Unknown"
        player_id = p.get(0, 0)
        level     = as_int(p.get(3, 0))
        skin_count = as_int(p.get(83, 0))
        hero_count = as_int(p.get(4, 0))
        matches   = as_int(p.get(17, 0))
        if ri:
            hero_count = as_int(ri.get(9, hero_count)) or hero_count
            matches = as_int(ri.get(22, matches)) or matches

        loc = p.get(71)
        if isinstance(loc, list) and len(loc) >= 2:
            location = ", ".join(str(x) for x in loc if x)
        else:
            location = None

        rank_now = map_rank(p.get(8))
        rank_top = map_rank(p.get(95))
        ach      = p.get(7, 0)
        t136 = p.get(136, {})
        coll_pts = t136.get(9, 0) if isinstance(t136, dict) else 0

        v2l_status = None
        if v2l and isinstance(v2l, dict):
            src = v2l.get("_src", 0)
            data = v2l.get("_data", {})
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
        ri24 = ri.get(24)
        if isinstance(ri24, int):
            likes = ri24
        if not likes:
            likes = p.get(61, 0) or 0

        # squad
        squad_icon = p.get(31, "")
        squad_name = str(p.get(30, "")).replace("`", "").strip()
        squad = f"{squad_icon} {squad_name}".strip() if squad_name else None

        # starlight (tag 21/47/50 hold expiry ts)
        sl_exp = 0
        for tag in (21, 47, 50):
            v = ri.get(tag) or p.get(tag) or 0
            if isinstance(v, int) and v > 1700000000:
                sl_exp = v
                break
        starlight_user = "Yes" if sl_exp > time.time() else "No"
        starlight_months = p.get(60, 0) or 0

        # bindings (rough — tag 91 is hero list, bindings typically elsewhere)
        bindings = None
        tbind = p.get(91)
        # leave None when no clean signal

        # tickets / currency
        tickets = ri.get(49, 0) or p.get(49, 0) or 0

        min_ts = 1451577600
        fallback = p.get(6, 0)
        if creation_ts and creation_ts >= min_ts:
            created = fmt_ts_full(creation_ts)
            age_ts = creation_ts
        elif fallback and fallback >= min_ts:
            created = fmt_ts_full(fallback)
            age_ts = fallback
        else:
            created = None
            age_ts = 0

        age = None
        if age_ts:
            now = datetime.datetime.now(datetime.timezone.utc)
            start = datetime.datetime.fromtimestamp(age_ts, datetime.timezone.utc)
            d = (now - start).days
            y, m, r = d // 365, (d % 365) // 30, d % 30
            age = f"{y}y {m}m {r}d" if y else (f"{m}m {r}d" if m else f"{d}d")

        win_count = ri.get(22, 0) or 0
        total_battles = ri.get(77, 0) or p.get(17, 0) or 0
        total_wins = p.get(18, 0) or 0
        win_rate = None
        win_rate_approx = False
        wins_for_rate = win_count if win_count else total_wins
        if total_battles > 0 and wins_for_rate > 0:
            wr = (wins_for_rate / total_battles) * 100
            if wr > 100:
                win_rate = f"{min(wr, 100):.1f}%"
                win_rate_approx = True
            else:
                win_rate = f"{wr:.1f}%"

        diamonds = 0
        bp = 0
        cur = ri.get(111)
        if isinstance(cur, dict):
            diamonds = as_int(cur.get(0, 0))
            bp = as_int(cur.get(1, 0))
        elif isinstance(cur, int):
            diamonds = cur
        elif isinstance(cur, list) and cur:
            diamonds = as_int(cur[0]) if len(cur) > 0 else 0
            bp = as_int(cur[1]) if len(cur) > 1 else 0
        if not bp:
            bp = int(p.get(83, 0) or 0)

        skin_counts = {
            "Supreme Skins": 0, "Grand Skins": 0, "Exquisite Skins": 0,
            "Deluxe Skins": 0, "Exceptional Skins": 0, "Common Skins": 0,
        }
        t118 = ri.get(118) or p.get(118)
        if isinstance(t118, dict):
            counts_keys = {6: "Supreme Skins", 5: "Grand Skins",
                           4: "Exquisite Skins", 3: "Deluxe Skins",
                           2: "Exceptional Skins", 1: "Common Skins"}
            src = t118.get(4) or t118.get(0) or t118.get(1) or t118
            if isinstance(src, dict):
                for sid, cnt in src.items():
                    try:
                        sid_i = int(sid)
                    except (ValueError, TypeError):
                        continue
                    if sid_i in counts_keys:
                        skin_counts[counts_keys[sid_i]] = cnt

        last_login_ts = as_int(p.get(5, 0))
        last_login_country = p.get(87, "") or None
        region_country = p.get(97, "") or None

        return {
            "nickname": nickname,
            "player_id": player_id,
            "level": level,
            "skin_count": skin_count,
            "hero_count": hero_count,
            "matches": matches,
            "current_rank": rank_now,
            "high_rank": rank_top,
            "achievement_points": ach,
            "collector_point": coll_pts,
            "collector_tier": map_collector(coll_pts),
            "location": location,
            "region_country": region_country,
            "last_login_country": last_login_country,
            "followers": followers,
            "likes": likes,
            "squad": squad,
            "bindings": bindings,
            "creation_date": created,
            "account_age": age,
            "total_battles": total_battles,
            "win_rate": win_rate,
            "win_rate_approx": win_rate_approx,
            "diamonds": diamonds,
            "battle_points": bp,
            "tickets": tickets,
            "v2l_status": v2l_status,
            "starlight_user": starlight_user,
            "starlight_months": starlight_months,
            "starlight_expiry_ts": sl_exp or None,
            "starlight_expiry": fmt_ts(sl_exp) if sl_exp else None,
            "last_login_ts": last_login_ts or None,
            "last_login": fmt_ts(last_login_ts) if last_login_ts else None,
            "skin_breakdown": skin_counts,
            "last_match": extract_last_match(ri, p),
        }
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════
# LOOKUP PIPELINE
# ═══════════════════════════════════════════════════════════════════════
def _try_one_device(account_id, zone_id, login_device):
    conn = GameConn(login_device)
    try:
        ok, diag = conn.login()
        if not ok:
            pid = (diag or {}).get("pid")
            keys = (diag or {}).get("keys")
            err = (diag or {}).get("error")
            detail = err or (f"pid={pid} keys={keys}" if pid else "no response")
            return {"status": "error", "error": f"login failed ({detail})"}

        ok, _ = conn.get_server()
        if not ok:
            return {"status": "error", "error": "no game server assigned"}

        conn.enter_game()
        if not conn.handshake():
            return {"status": "error", "error": "game server handshake failed"}

        result = conn.lookup(int(account_id), "id")
        if not result:
            return {"status": "error",
                    "error": "lookup returned nothing (id not found)"}

        role_id = int(account_id)
        if result.get(0) and len(result[0]) > 0:
            first = result[0][0]
            if isinstance(first, dict):
                role_id = first.get(0, role_id)

        role_info = None
        try:
            si = conn.skin_info(role_id, zone_id)
            if si and isinstance(si, dict):
                role_info = dict(si)
        except Exception:
            pass

        try:
            ri = conn.role_info(role_id, zone_id)
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
        try:
            v2l = conn.v2l_status(role_id, zone_id)
        except Exception:
            pass

        player = extract_player(result, role_info, conn.creation_ts, v2l) or {}
        player["_account"] = int(account_id)
        player["_zone"] = int(zone_id)

        return {
            "status": "success",
            "player": player,
            "account_id": int(account_id),
            "zone_id": int(zone_id),
        }
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    finally:
        conn.close()


def fetch_info_by_id_zone(account_id: int, zone_id: int) -> dict:
    last = {"status": "error", "error": "no login devices configured"}
    for dev in LOGIN_DEVICES:
        res = _try_one_device(account_id, zone_id, dev)
        if res.get("status") == "success":
            return res
        last = res
        if DEBUG:
            _log.warning("device %s… failed: %s", dev[:24], res.get("error"))
    return last


def lookup_with_retries(account_id: int, zone_id: int, attempts: int = MAX_RETRIES):
    if attempts < 1:
        attempts = 1
    last = {"error": "no attempt ran", "status": "error"}
    for i in range(attempts):
        res = fetch_info_by_id_zone(account_id, zone_id)
        if res.get("status") == "success":
            return res, i + 1
        last = res
        if i < attempts - 1:
            time.sleep(RETRY_DELAY * (i + 1))
    return last, attempts


# ═══════════════════════════════════════════════════════════════════════
# QUOTE-FORMAT RENDER
# ═══════════════════════════════════════════════════════════════════════
def _dash(x):
    return "—" if x is None or x == "" else str(x)


def _num(v):
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return str(v)


def build_quote_report(p, account_id, zone_id):
    now = int(time.time())
    sb = p.get("skin_breakdown") or {}

    # starlight
    sl_user = p.get("starlight_user") or "No"
    sl_exp_ts = p.get("starlight_expiry_ts")
    sl_exp_str = p.get("starlight_expiry") or "—"
    sl_rel = _rel(sl_exp_ts, now) if sl_exp_ts else None
    sl_months = p.get("starlight_months", 0)
    if sl_user == "Yes" and sl_exp_ts:
        starlight_line = (
            f"{sl_user} | Expiry: {sl_exp_str} "
            f"({sl_rel}) PHT | Months: {sl_months}"
        )
    else:
        starlight_line = f"{sl_user} | Expiry: — | Months: —"

    # last login
    ll_ts = p.get("last_login_ts")
    ll_str = p.get("last_login") or "—"
    ll_rel = _rel(ll_ts, now) if ll_ts else None
    last_login_line = f"{ll_str} ({ll_rel}) PHT" if ll_rel else ll_str

    # win rate
    wr = p.get("win_rate")
    if wr and p.get("win_rate_approx"):
        win_rate_line = f"{wr} (approx)"
    else:
        win_rate_line = wr or "—"

    # squad
    squad = p.get("squad") or "—"

    # bindings
    bindings = p.get("bindings") or "N/A"

    # v2l
    v2l = p.get("v2l_status") or "N/A"

    lines = [
        f"Account ID: {account_id}",
        f"Zone ID: {zone_id}",
        "",
        "PLAYER INFO:",
        f"Nickname: {_dash(p.get('nickname'))}",
        f"Level: {p.get('level', 0)}",
        f"Heroes: {p.get('hero_count', 0)}",
        f"Skins: {p.get('skin_count', 0)}",
        f"Current Rank: {_dash(p.get('current_rank'))}",
        f"Highest Rank: {_dash(p.get('high_rank'))}",
        f"Collector: {_dash(p.get('collector_tier'))}",
        f"Collector Pts: {_num(p.get('collector_point', 0))}",
        f"Bindings: {bindings}",
        f"Starlight: {starlight_line}",
        f"V2L: {v2l}",
        f"Location: {_dash(p.get('location'))}",
        f"Last Login: {last_login_line}",
        f"Last Login Country: {_dash(p.get('last_login_country'))}",
        f"Creation Date: {_dash(p.get('creation_date'))}",
        f"Account Age: {_dash(p.get('account_age'))}",
        f"Squad: {squad}",
        f"Win Rate: {win_rate_line}",
        f"Total Battles: {_num(p.get('total_battles', 0))}",
        f"Tickets: {_num(p.get('tickets', 0))}",
        f"Diamonds: {_num(p.get('diamonds', 0))}",
        f"Battle Points: {_num(p.get('battle_points', 0))}",
        f"Followers: {_num(p.get('followers', 0))}",
        f"Likes: {_num(p.get('likes', 0))}",
        "Skin Breakdown:",
        f"  Supreme Skins: {sb.get('Supreme Skins', 0)}",
        f"  Grand Skins: {sb.get('Grand Skins', 0)}",
        f"  Exquisite Skins: {sb.get('Exquisite Skins', 0)}",
        f"  Deluxe Skins: {sb.get('Deluxe Skins', 0)}",
        f"  Exceptional Skins: {sb.get('Exceptional Skins', 0)}",
        f"  Common Skins: {sb.get('Common Skins', 0)}",
    ]
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
# PERSISTENCE
# ═══════════════════════════════════════════════════════════════════════
_lock = threading.Lock()


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


def register_group(chat_id, title):
    with _lock:
        g = _load_json(GROUPS_FILE, {})
        g[str(chat_id)] = {"title": title, "added_at": int(time.time())}
        _save_json(GROUPS_FILE, g)


def unregister_group(chat_id):
    with _lock:
        g = _load_json(GROUPS_FILE, {})
        g.pop(str(chat_id), None)
        _save_json(GROUPS_FILE, g)


def list_groups():
    with _lock:
        return _load_json(GROUPS_FILE, {})


def bump_stats(key):
    with _lock:
        s = _load_json(STATS_FILE, {"lookups": 0, "hits": 0, "fails": 0})
        s[key] = int(s.get(key, 0)) + 1
        _save_json(STATS_FILE, s)


def get_stats():
    return _load_json(STATS_FILE, {"lookups": 0, "hits": 0, "fails": 0})


# ═══════════════════════════════════════════════════════════════════════
# GUARDS
# ═══════════════════════════════════════════════════════════════════════
def is_admin(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id == ADMIN_ID)


# ═══════════════════════════════════════════════════════════════════════
# PUBLIC HANDLERS
# ═══════════════════════════════════════════════════════════════════════
HELP_TEXT = (
    "<b>DRAKVEX · MLBB lookup</b>\n\n"
    "<code>/lookup &lt;id&gt; &lt;zone&gt;</code>\n"
    "example: <code>/lookup 77750048 2146</code>\n\n"
    "lookup is free for everyone."
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await update.effective_message.reply_text(HELP_TEXT,
                                                  parse_mode=ParseMode.HTML)
    except Exception:
        pass


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)


async def cmd_lookup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None:
        return
    args = ctx.args or []

    if len(args) < 2:
        await msg.reply_text(
            "usage: <code>/lookup &lt;id&gt; &lt;zone&gt;</code>\n"
            "example: <code>/lookup 77750048 2146</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        account_id = int(args[0])
        zone_id = int(args[1])
    except (ValueError, TypeError):
        await msg.reply_text("id and zone must be integers")
        return

    if account_id <= 0 or zone_id <= 0:
        await msg.reply_text("id and zone must be positive")
        return

    bump_stats("lookups")

    notice = await msg.reply_text(
        f"🔍 looking up <code>{account_id}</code> · zone <code>{zone_id}</code>…",
        parse_mode=ParseMode.HTML,
    )

    try:
        res, used = await asyncio.to_thread(
            lookup_with_retries, account_id, zone_id, MAX_RETRIES
        )
    except Exception as e:
        try:
            await notice.edit_text(
                f"❌ lookup crashed: <code>{html.escape(str(e))}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return

    if not res or res.get("status") != "success":
        bump_stats("fails")
        err = (res or {}).get("error", "unknown")
        try:
            await notice.edit_text(
                f"❌ lookup failed after {used} attempt(s)\n"
                f"<code>{html.escape(err)}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return

    bump_stats("hits")
    body = build_quote_report(res["player"], account_id, zone_id)

    # telegram hard cap 4096; keep headroom for HTML escaping
    if len(body) <= 3600:
        try:
            await notice.edit_text(
                f"<pre>{html.escape(body)}</pre>",
                parse_mode=ParseMode.HTML,
            )
            return
        except Exception:
            try:
                await notice.edit_text(body)
                return
            except Exception:
                pass

    try:
        await notice.edit_text("📎 result too large — sent as file")
    except Exception:
        pass

    buf = io.BytesIO(body.encode("utf-8"))
    buf.name = f"lookup_{account_id}_{zone_id}.txt"
    try:
        await msg.reply_document(document=buf, filename=buf.name)
    except Exception as e:
        try:
            await msg.reply_text(f"❌ could not send result: {html.escape(str(e))}")
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════
# ADMIN HANDLERS
# ═══════════════════════════════════════════════════════════════════════
async def cmd_adminhelp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.effective_message.reply_text(
        "<b>admin commands</b>\n\n"
        "<code>/groups</code> — list registered groups\n"
        "<code>/removegroup &lt;chat_id&gt;</code> — unregister + leave\n"
        "<code>/leave &lt;chat_id&gt;</code> — leave group\n"
        "<code>/stats</code> — bot counters\n"
        "<code>/broadcast &lt;msg&gt;</code> — send to all registered groups\n"
        "<code>/id</code> — show your id and chat id",
        parse_mode=ParseMode.HTML,
    )


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    c = update.effective_chat
    await update.effective_message.reply_text(
        f"user id: <code>{u.id if u else '?'}</code>\n"
        f"chat id: <code>{c.id if c else '?'}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_groups(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    groups = list_groups()
    if not groups:
        await update.effective_message.reply_text("no registered groups.")
        return
    lines = ["<b>registered groups</b>", ""]
    for cid, meta in groups.items():
        title = html.escape(str(meta.get("title", "?")))
        lines.append(f"<code>{cid}</code>  {title}")
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML
    )


async def cmd_removegroup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    args = ctx.args or []
    if not args:
        await update.effective_message.reply_text("usage: /removegroup <chat_id>")
        return
    try:
        cid = int(args[0])
    except ValueError:
        await update.effective_message.reply_text("chat_id must be an integer")
        return
    unregister_group(cid)
    try:
        await ctx.bot.leave_chat(cid)
    except Exception:
        pass
    await update.effective_message.reply_text(
        f"removed <code>{cid}</code>.", parse_mode=ParseMode.HTML
    )


async def cmd_leave(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    args = ctx.args or []
    if not args:
        await update.effective_message.reply_text("usage: /leave <chat_id>")
        return
    try:
        cid = int(args[0])
    except ValueError:
        await update.effective_message.reply_text("chat_id must be an integer")
        return
    try:
        await ctx.bot.leave_chat(cid)
        await update.effective_message.reply_text(
            f"left <code>{cid}</code>.", parse_mode=ParseMode.HTML
        )
    except Exception as e:
        await update.effective_message.reply_text(
            f"failed: {html.escape(str(e))}"
        )


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    s = get_stats()
    await update.effective_message.reply_text(
        "<b>stats</b>\n"
        f"lookups: <code>{s.get('lookups', 0)}</code>\n"
        f"hits:    <code>{s.get('hits', 0)}</code>\n"
        f"fails:   <code>{s.get('fails', 0)}</code>\n"
        f"groups:  <code>{len(list_groups())}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    text = " ".join(ctx.args or []).strip()
    if not text:
        await update.effective_message.reply_text("usage: /broadcast <message>")
        return
    ok, fail = 0, 0
    for cid in list_groups().keys():
        try:
            await ctx.bot.send_message(int(cid), text)
            ok += 1
        except Exception:
            fail += 1
    await update.effective_message.reply_text(
        f"broadcast: {ok} ok, {fail} failed"
    )


# ═══════════════════════════════════════════════════════════════════════
# GROUP JOIN GATE
# ═══════════════════════════════════════════════════════════════════════
async def on_my_chat_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cm = update.my_chat_member
    if cm is None:
        return
    chat = cm.chat
    new_status = cm.new_chat_member.status
    old_status = cm.old_chat_member.status
    actor = cm.from_user

    if chat.type == "private":
        return

    joined = new_status in ("member", "administrator") and \
             old_status in ("left", "kicked")
    left = new_status in ("left", "kicked") and \
           old_status in ("member", "administrator")

    if left:
        unregister_group(chat.id)
        return

    if not joined:
        return

    if not actor or actor.id != ADMIN_ID:
        try:
            await ctx.bot.send_message(
                actor.id if actor else ADMIN_ID,
                f"⛔ only the configured admin may add this bot to groups.\n"
                f"left <b>{html.escape(chat.title or str(chat.id))}</b>.",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        try:
            await ctx.bot.leave_chat(chat.id)
        except Exception:
            pass
        return

    register_group(chat.id, chat.title or str(chat.id))
    try:
        await ctx.bot.send_message(
            chat.id,
            "✅ DRAKVEX online.\n"
            "lookups are free for everyone:\n"
            "<code>/lookup &lt;id&gt; &lt;zone&gt;</code>\n"
            "example: <code>/lookup 77750048 2146</code>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════
# GLOBAL ERROR HOOK
# ═══════════════════════════════════════════════════════════════════════
async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    _log.exception("unhandled", exc_info=ctx.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ internal error — try again in a moment."
            )
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════
# ENTRY
# ═══════════════════════════════════════════════════════════════════════
def main():
    logging.basicConfig(
        format="%(asctime)s  %(levelname)-7s %(name)s | %(message)s",
        level=logging.INFO,
    )

    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is empty")
    if not LOGIN_DEVICES:
        raise SystemExit("LOGIN_DEVICES is empty")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("lookup", cmd_lookup))

    app.add_handler(CommandHandler("adminhelp",   cmd_adminhelp))
    app.add_handler(CommandHandler("id",          cmd_id))
    app.add_handler(CommandHandler("groups",      cmd_groups))
    app.add_handler(CommandHandler("removegroup", cmd_removegroup))
    app.add_handler(CommandHandler("leave",       cmd_leave))
    app.add_handler(CommandHandler("stats",       cmd_stats))
    app.add_handler(CommandHandler("broadcast",   cmd_broadcast))

    app.add_handler(ChatMemberHandler(on_my_chat_member,
                                      ChatMemberHandler.MY_CHAT_MEMBER))

    app.add_error_handler(on_error)

    _log.info("drakvex bot up · admin=%s · devices=%d",
              ADMIN_ID, len(LOGIN_DEVICES))
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
