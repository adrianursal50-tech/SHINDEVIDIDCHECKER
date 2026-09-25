#!/usr/bin/env python3
"""
mlbb_bot.py — Telegram bot that chains:
  simple.py  (ban check via MLBB login/game-server protocol)
  info.py    (player info lookup via same protocol)

Pipeline per device ID:
  1. Login handshake → check every response for ban markers
  2. If banned → reply with ban reason + duration
  3. If clean  → run info lookup with returned account/zone → reply full profile

Termux quick start:
  pkg install python libffi openssl
  pip install requests curl_cffi zstandard pycryptodome colorama urllib3
  export MLBB_BOT_TOKEN="123:ABC..."
  python mlbb_bot.py
"""

import os
import io
import sys
import time
import json
import socket
import struct
import zlib
import threading
import datetime
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum
from typing import Any, Tuple, Optional, Dict, List

import requests
import urllib3
import zstandard as zstd
from Crypto.Cipher import AES

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── CONFIG ──────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ.get("MLBB_BOT_TOKEN", "8702549007:AAGNiI_-iZyzzKE4OHafSwgl3SjR0Ht3VRM")
ALLOWED_USERS: set = {8621676055}        # single-user allowlist            # empty = open to everyone
WORKERS      = int(os.environ.get("MLBB_WORKERS", "8"))
API          = "https://api.telegram.org/bot" + BOT_TOKEN

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR  = os.path.join(BASE_DIR, "bot_results")
os.makedirs(RESULTS_DIR, exist_ok=True)
BAN_FILE     = os.path.join(RESULTS_DIR, "bans.txt")
CLEAN_FILE   = os.path.join(RESULTS_DIR, "clean.txt")
UNKNOWN_FILE = os.path.join(RESULTS_DIR, "unknown.txt")

CLIENT_VERSION = "2.2.16.1232.1"
CHANNEL        = "and_usa"

# ── SDP PROTOCOL ────────────────────────────────────────────────────
class SdpDataType(Enum):
    INTEGER_POSITIVE = 0
    INTEGER_NEGATIVE = 1
    FLOAT = 2
    DOUBLE = 3
    STRING = 4
    LIST = 5
    DICT = 6
    STRUCT_BEGIN = 7
    STRUCT_END = 8

class SdpException(Exception):
    pass

class SdpStruct(dict):
    def __init__(self, data=None):
        super().__init__()
        self.data = b""
        self.offset = 0
        if isinstance(data, bytes):
            self.data = data
            self.offset = 0
            self._unpack_from_binary()
        elif data is not None:
            super().update(data)
            self._pack_to_binary()

    def _pack_to_binary(self):
        self.data = bytes([SdpDataType.STRUCT_BEGIN.value << 4])
        for tag, value in sorted(self.items()):
            self._pack(tag, value)
        self.data += bytes([SdpDataType.STRUCT_END.value << 4])

    def _unpack_from_binary(self):
        if not self.data:
            return
        if self.data[0] >> 4 == SdpDataType.STRUCT_BEGIN.value:
            self.offset = 1
        while self.offset < len(self.data):
            tag, value = self._unpack()
            if isinstance(value, SdpDataType) and value == SdpDataType.STRUCT_END:
                break
            self[tag] = value

    def _write_number(self, value: int) -> bytes:
        result = bytearray()
        while value >= 0x80:
            result.append((value & 0x7F) | 0x80)
            value >>= 7
        result.append(value & 0x7F)
        return bytes(result)

    def _read_number(self) -> int:
        n = 1
        val = self.data[self.offset] & 0x7F
        while self.data[self.offset + n - 1] >= 0x80:
            val |= (self.data[self.offset + n] & 0x7F) << (7 * n)
            n += 1
        self.offset += n
        return val

    def _pack_header(self, tag: int, dt: SdpDataType):
        if tag < 15:
            self.data += bytes([(dt.value << 4) | tag])
        else:
            self.data += bytes([(dt.value << 4) | 15])
            self.data += self._write_number(tag)

    def _pack(self, tag: int, value: Any):
        if isinstance(value, bool):
            self._pack_header(tag, SdpDataType.INTEGER_POSITIVE)
            self.data += self._write_number(1 if value else 0)
        elif isinstance(value, int):
            if value < 0:
                self._pack_header(tag, SdpDataType.INTEGER_NEGATIVE)
                self.data += self._write_number(-value)
            else:
                self._pack_header(tag, SdpDataType.INTEGER_POSITIVE)
                self.data += self._write_number(value)
        elif isinstance(value, float):
            self._pack_header(tag, SdpDataType.DOUBLE)
            packed = struct.pack("<d", value)
            self.data += self._write_number(len(packed))
            self.data += packed
        elif isinstance(value, (str, bytes)):
            self._pack_header(tag, SdpDataType.STRING)
            encoded = value.encode("utf-8") if isinstance(value, str) else value
            self.data += self._write_number(len(encoded))
            self.data += encoded
        elif isinstance(value, list):
            self._pack_header(tag, SdpDataType.LIST)
            self.data += self._write_number(len(value))
            for item in value:
                self._pack(0, item)
        elif isinstance(value, dict):
            if isinstance(value, SdpStruct):
                self._pack_header(tag, SdpDataType.STRUCT_BEGIN)
                for k, v in sorted(value.items()):
                    self._pack(k, v)
                self.data += bytes([SdpDataType.STRUCT_END.value << 4])
            else:
                self._pack_header(tag, SdpDataType.DICT)
                self.data += self._write_number(len(value))
                for k, v in sorted(value.items()):
                    self._pack(0, k)
                    self._pack(0, v)
        else:
            raise SdpException("Unsupported type: %r" % (type(value),))

    def _unpack(self) -> Tuple[int, Any]:
        try:
            if self.offset >= len(self.data):
                return 0, None
            header = self.data[self.offset]
            tag = header & 0xF
            data_type = SdpDataType(header >> 4)
            self.offset += 1
            if tag == 15:
                tag = self._read_number()

            if data_type == SdpDataType.INTEGER_POSITIVE:
                return tag, self._read_number()
            elif data_type == SdpDataType.INTEGER_NEGATIVE:
                return tag, -self._read_number()
            elif data_type == SdpDataType.FLOAT:
                v = self._read_number().to_bytes(4, "little")
                return tag, struct.unpack("<f", v)[0]
            elif data_type == SdpDataType.DOUBLE:
                v = self._read_number().to_bytes(8, "little")
                return tag, struct.unpack("<d", v)[0]
            elif data_type == SdpDataType.STRING:
                length = self._read_number()
                try:
                    v = self.data[self.offset:self.offset + length].decode("utf-8")
                except UnicodeDecodeError:
                    v = self.data[self.offset:self.offset + length]
                self.offset += length
                return tag, v
            elif data_type == SdpDataType.LIST:
                length = self._read_number()
                v = []
                for _ in range(length):
                    _, item = self._unpack()
                    v.append(item)
                return tag, v
            elif data_type == SdpDataType.DICT:
                length = self._read_number()
                v = {}
                for _ in range(length):
                    _, k = self._unpack()
                    _, vv = self._unpack()
                    v[k] = vv
                return tag, v
            elif data_type == SdpDataType.STRUCT_BEGIN:
                sd = {}
                while True:
                    st, sv = self._unpack()
                    if isinstance(sv, SdpDataType) and sv == SdpDataType.STRUCT_END:
                        break
                    sd[st] = sv
                return tag, SdpStruct(sd)
            elif data_type == SdpDataType.STRUCT_END:
                return tag, SdpDataType.STRUCT_END
            else:
                raise SdpException("Unknown data type")
        except Exception:
            raise SdpException("Unpack error")

# ── AES / wire ──────────────────────────────────────────────────────
AES_KEY = bytes.fromhex("f5a193d50ade553e9835595f5cd75ddd")
AES_IV  = b"\x00" * 16

# ── MLBB CONNECTION ─────────────────────────────────────────────────
class MLBBConnection:
    def __init__(self, device_id: str):
        self.host = "login.ml.youngjoygame.com"
        self.port = 30021
        self.sequence = 1
        self.socket: Optional[socket.socket] = None
        self.queue_data = b""
        self.device_id = device_id

        parts = device_id.split("_")
        di = parts[1] if len(parts) >= 2 else device_id
        if len(parts) >= 3 and len(di) < 32:
            di = di + "_" + parts[2]

        if len(di) >= 32:
            self.imei_md5 = di[:32]
            self.android_id = di[32:48] if len(di) >= 48 else ""
            self.advertising_id = di[48:] if len(di) > 48 else ""
        else:
            self.imei_md5 = device_id
            self.android_id = ""
            self.advertising_id = ""

        self.account_id = 0
        self.session_key: Any = ""
        self.zone_id = 0
        self.creation_ts = 0
        self.game_server_host = ""
        self.game_server_port = 0
        self.last_header_size = 0

    def connect(self, host: Optional[str] = None, port: Optional[int] = None):
        if host:
            self.host = host
        if port:
            self.port = port
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.settimeout(6)
        self.socket.connect((self.host, self.port))
        self.queue_data = b""
        self.sequence = 1

    def cleanup(self):
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
            self.socket = None

    def send_data(self, pkt_id: int, sdp: SdpStruct):
        packet = SdpStruct({0: pkt_id, 1: self.sequence, 5: sdp.data}).data
        buf = zstd.compress(packet)
        flags = (len(buf) + 4) | (16 << 24)
        buf = flags.to_bytes(4, "big") + buf
        self.socket.send(buf)
        self.sequence += 1

    def recv_data(self) -> Tuple[Optional[int], Any]:
        try:
            while len(self.queue_data) < 4:
                data = self.socket.recv(4096)
                if not data:
                    return None, None
                self.queue_data += data

            flags = int.from_bytes(self.queue_data[:4], "big")
            size = flags & 0xFFFFFF
            ctype = flags >> 24
            self.last_header_size = size

            while len(self.queue_data) < size:
                data = self.socket.recv(4096)
                if not data:
                    return None, None
                self.queue_data += data

            data = self.queue_data[4:size]
            self.queue_data = self.queue_data[size:]

            if ctype == 1:
                data = zlib.decompress(data)
            elif ctype == 16:
                data = zstd.decompress(data)
            elif ctype == 2:
                c = AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV)
                data = c.decrypt(data if len(data) % 16 == 0 else data[:-1]).rstrip(b"\x00")
            elif ctype == 3:
                c = AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV)
                data = c.decrypt(data if len(data) % 16 == 0 else data[:-1]).rstrip(b"\x00")
                data = zlib.decompress(data)
            elif ctype == 18:
                c = AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV)
                decrypted = c.decrypt(data if len(data) % 16 == 0 else data[:-1])
                data = zstd.decompress(decrypted.rstrip(b"\x00"))

            result = SdpStruct(data)
            pkt_id = result[0]
            if pkt_id is None:
                return None, None
            body = result.get(6) or result.get(5)
            if not body or not isinstance(body, bytes):
                return pkt_id, None
            return pkt_id, SdpStruct(body)
        except socket.timeout:
            return -1, None
        except Exception:
            return None, None

# ── BAN INSPECTION ──────────────────────────────────────────────────
BAN_REASON_MAP = {
    "21": "Using Plug-in Apps to Compromise Competitive Fairness",
}

def inspect_for_ban(payload: Any) -> Tuple[bool, Dict]:
    """Walk any parsed response and pull ban markers out of it."""
    details: Dict[str, Any] = {}

    def scan(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "ban_reason":
                    code = str(v)
                    details["ban_code"] = code
                    details["reason_name"] = BAN_REASON_MAP.get(
                        code, BAN_REASON_MAP["21"]
                    )
                elif isinstance(k, str) and "ban" in k.lower():
                    details[str(k)] = v
                if k == "endtime_day":
                    details["endtime_day"] = v
                if k == "endtime_hour":
                    details["endtime_hour"] = v
                if k == "endtime_min":
                    details["endtime_min"] = v
                if k == "endtime_sec":
                    details["endtime_sec"] = v
                if isinstance(v, (dict, list)):
                    scan(v)
        elif isinstance(obj, list):
            for item in obj:
                scan(item)

    if payload:
        scan(dict(payload) if isinstance(payload, dict) else payload)

    banned = details.get("endtime_day") is not None or "ban_reason" in details
    return banned, details

# ── STATIC MAPS ─────────────────────────────────────────────────────
HERO_ID_MAP = {
    1: "Miya", 2: "Balmond", 3: "Saber", 4: "Alice", 5: "Nana", 6: "Tigreal", 7: "Alucard", 8: "Karina", 9: "Akai",
    10: "Franco", 11: "Bane", 12: "Bruno", 13: "Clint", 14: "Rafaela", 15: "Eudora", 16: "Zilong", 17: "Fanny",
    18: "Layla", 19: "Minotaur", 20: "Lolita", 21: "Hayabusa", 22: "Freya", 23: "Gord", 24: "Natalia", 25: "Kagura",
    26: "Chou", 27: "Sun", 28: "Alpha", 29: "Ruby", 30: "Yi Sun-shin", 31: "Moskov", 32: "Johnson", 33: "Cyclops",
    34: "Estes", 35: "Hilda", 36: "Aurora", 37: "Lapu-Lapu", 38: "Vexana", 39: "Roger", 40: "Karrie", 41: "Gatotkaca",
    42: "Harley", 43: "Irithel", 44: "Grock", 45: "Argus", 46: "Odette", 47: "Lancelot", 48: "Diggie", 49: "Hylos",
    50: "Zhask", 51: "Helcurt", 52: "Pharsa", 53: "Lesley", 54: "Jawhead", 55: "Angela", 56: "Gusion", 57: "Valir",
    58: "Martis", 59: "Uranus", 60: "Hanabi", 61: "Chang'e", 62: "Kaja", 63: "Selena", 64: "Aldous", 65: "Claude",
    66: "Vale", 67: "Leomord", 68: "Lunox", 69: "Hanzo", 70: "Belerick", 71: "Kimmy", 72: "Thamuz", 73: "Harith",
    74: "Minsitthar", 75: "Kadita", 76: "Faramis", 77: "Badang", 78: "Khufra", 79: "Granger", 80: "Guinevere",
    81: "Esmeralda", 82: "Terizla", 83: "X.Borg", 84: "Ling", 85: "Dyrroth", 86: "Lylia", 87: "Baxia", 88: "Masha",
    89: "Wanwan", 90: "Silvanna", 91: "Cecilion", 92: "Carmilla", 93: "Atlas", 94: "Popol and Kupa", 95: "Yu Zhong",
    96: "Luo Yi", 97: "Benedetta", 98: "Khaleed", 99: "Barats", 100: "Brody", 101: "Yve", 102: "Mathilda",
    103: "Paquito", 104: "Gloo", 105: "Beatrix", 106: "Phoveus", 107: "Natan", 108: "Aulus", 109: "Aamon",
    110: "Valentina", 111: "Edith", 112: "Floryn", 113: "Yin", 114: "Melissa", 115: "Xavier", 116: "Julian",
    117: "Fredrinn", 118: "Joy", 119: "Novaria", 120: "Arlott", 121: "Ixia", 122: "Nolan", 123: "Cici",
    124: "Chip", 125: "Zhuxin", 126: "Suyou", 127: "Lukas", 128: "Kalea", 129: "Zetian", 130: "Obsidia",
}

_SKIN_DB_PATH = os.path.join(BASE_DIR, "skin_db.json")
try:
    with open(_SKIN_DB_PATH, "r", encoding="utf-8") as _f:
        _raw = _f.read().strip()
        if _raw and not _raw.startswith("{"):
            _raw = "{" + _raw
        SKIN_DB = json.loads(_raw) if _raw else {}
except Exception:
    SKIN_DB = {}

SKIN_TIER_LABELS = {
    "common": "Common", "exceptional": "Exceptional", "deluxe": "Deluxe",
    "exquisite": "Exquisite", "grand": "Grand", "supreme": "Supreme",
}

def get_skin_tier(skin_id):
    tier = SKIN_DB.get(str(skin_id))
    if not tier:
        return None
    return SKIN_TIER_LABELS.get(tier, tier.capitalize())

def map_collector_point(point: int) -> str:
    if point < 1000:
        return "No Tier"
    tiers = [
        (1000, 4000, "Amateur Collector"),
        (4000, 10000, "Junior Collector"),
        (10000, 22000, "Seasoned Collector"),
        (22000, 44000, "Expert Collector"),
        (44000, 84000, "Renowned Collector"),
        (84000, 160000, "Exalted Collector"),
        (160000, 280000, "Mega Collector"),
        (280000, float("inf"), "World Collector"),
    ]
    for mn, mx, name in tiers:
        if mn <= point < mx:
            if name == "World Collector":
                return name
            per = (mx - mn) / 5
            level = int((point - mn) // per)
            return f"{name} {['V','IV','III','II','I'][level]}"
    return "Unknown"

def map_rank(p):
    RD = [
        (0, 4, "Warrior III"), (5, 9, "Warrior II"), (10, 14, "Warrior I"),
        (15, 19, "Elite IV"), (20, 24, "Elite III"), (25, 29, "Elite II"), (30, 34, "Elite I"),
        (35, 39, "Master IV"), (40, 44, "Master III"), (45, 49, "Master II"), (50, 54, "Master I"),
        (55, 59, "Grandmaster IV"), (60, 64, "Grandmaster III"), (65, 69, "Grandmaster II"), (70, 74, "Grandmaster I"),
        (75, 81, "Epic IV"), (82, 88, "Epic III"), (89, 95, "Epic II"), (96, 107, "Epic I"),
        (108, 114, "Legend IV"), (115, 121, "Legend III"), (122, 128, "Legend II"), (129, 135, "Legend I"),
    ]
    for mn, mx, name in RD:
        if mn <= p <= mx:
            return name
    if 136 <= p <= 160:
        return f"Mythic {p - 135}"
    if 161 <= p <= 195:
        return f"Mythical Honor {p - 135}"
    if 196 <= p <= 235:
        return f"Mythical Glory {p - 157}"
    if 236 <= p <= 999:
        return f"Mythical Immortal {p - 157}"
    return "Unknown"

def parse_skin_counts(tag_118):
    if not tag_118 or not isinstance(tag_118, dict):
        return {}
    skin_data = None
    for k in (4, "4"):
        v = tag_118.get(k)
        if isinstance(v, dict) and v:
            skin_data = v
            break
    if skin_data is None:
        skin_data = tag_118
    if not isinstance(skin_data, dict):
        return {}
    types = {6: "Supreme Skins", 5: "Grand Skins", 4: "Exquisite Skins",
             3: "Deluxe Skins", 2: "Exceptional Skins", 1: "Common Skins"}
    out = {}
    for sid, cnt in skin_data.items():
        try:
            si = int(sid)
        except (ValueError, TypeError):
            continue
        if si in types:
            out[types[si]] = cnt
    return out

def format_timestamp(ts):
    try:
        utc = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        pht = utc + datetime.timedelta(hours=8)
        base = pht.strftime("%Y-%m-%d %H:%M")
        now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
        delta = pht - now
        secs = int(delta.total_seconds())
        if secs >= 0:
            d, h, m = secs // 86400, (secs % 86400) // 3600, (secs % 3600) // 60
            return f"{base} (in {d}d {h}h {m}m)"
        secs = abs(secs)
        d, h, m = secs // 86400, (secs % 86400) // 3600, (secs % 3600) // 60
        return f"{base} ({d}d {h}h {m}m ago)"
    except Exception:
        return "Invalid timestamp"

def format_timestamp_full(ts):
    try:
        utc = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        pht = utc + datetime.timedelta(hours=8)
        return pht.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "Invalid timestamp"

def get_hero_history(tag_91):
    if not isinstance(tag_91, list):
        return []
    return [HERO_ID_MAP.get(hid, f"Unknown({hid})") for hid in reversed(tag_91)]

# ── PLAYER DATA EXTRACTION ─────────────────────────────────────────
def extract_player_data(result, role_info=None, creation_ts=0, v2l_data=None):
    if not result or not result.get(0) or len(result[0]) == 0:
        return None
    try:
        player_data = result[0][0]
        nickname    = player_data.get(2, "Unknown")
        player_id   = player_data.get(0, "Unknown")
        server      = player_data.get(1, "Unknown")
        level       = player_data.get(3, "Unknown")
        skin_count  = player_data.get(83, 0)
        hero_count  = player_data.get(4, 0)
        matches     = player_data.get(17, 0)
        rating_score = player_data.get(9, 0)
        if role_info:
            hero_count = role_info.get(9, hero_count)
            matches    = role_info.get(22, matches)

        location = "NOT FOUND"
        loc = player_data.get(71)
        if loc and isinstance(loc, list) and len(loc) >= 2:
            location = ", ".join(str(x) for x in loc)

        last_login = format_timestamp(player_data.get(5, 0))
        last_login_country = player_data.get(87, "Unknown")
        create_country = player_data.get(97, "Unknown")

        squad_icon = player_data.get(31, "")
        squad_name = (player_data.get(30, "") or "").replace("`", "").strip()
        squad = f"{squad_icon} {squad_name}".strip() if squad_name else "—"

        tag_95 = player_data.get(95)
        tag_8  = player_data.get(8)
        high_rank = map_rank(tag_95) if tag_95 is not None else "Unknown"
        current_rank = map_rank(tag_8) if tag_8 is not None else "Unknown"
        achievement_points = player_data.get(7, 0)

        tag_136 = player_data.get(136, {})
        collector_point = tag_136.get(9, 0) if isinstance(tag_136, dict) else 0
        collector_tier  = map_collector_point(collector_point)

        tag_91 = player_data.get(91, [])
        hero_history = get_hero_history(tag_91) if tag_91 else []

        v2l_status = "N/A"
        if v2l_data and isinstance(v2l_data, dict):
            src = v2l_data.get("_source", 0)
            d = v2l_data.get("_data", {}) or {}
            keys = (10, 11) if src == 10208 else (0, 2, 3, 5)
            for t in keys:
                v = d.get(t)
                if v is not None:
                    try:
                        v2l_status = "Enabled" if int(v) > 0 else "Disabled"
                        break
                    except (ValueError, TypeError):
                        pass

        followers = 0
        if role_info:
            followers = role_info.get(23, 0)
        if not followers:
            followers = player_data.get(15, 0)

        popularity = player_data.get(14, 0)
        bio = player_data.get(24, "")
        bio = bio.strip() if isinstance(bio, str) else ""

        likes = 0
        if role_info:
            likes = role_info.get(24, 0)
        if not likes:
            likes = player_data.get(61, 0)

        credits_score = None
        if role_info:
            cs = role_info.get(20, 0)
            if isinstance(cs, int) and cs > 0:
                credits_score = f"{cs}/110"

        restriction_flags = "None"
        _t117 = (role_info or {}).get(117) if role_info else None
        if _t117 is None:
            _t117 = player_data.get(117)
        if _t117 is not None:
            raw = _t117.get(0, 0) if isinstance(_t117, dict) else int(_t117 or 0)
            pct = round(((int(raw) + 1) / 7) * 100, 1)
            if pct < 30:
                risk = "Low"
            elif pct < 60:
                risk = "Medium"
            else:
                risk = "High"
            restriction_flags = f"{pct}% ({risk} Risk)"

        _t135 = player_data.get(135, {})
        _affl = _t135.get(1, 0) if isinstance(_t135, dict) else 0
        _affmap = {0: "None", 1: "Bronze", 2: "Silver", 3: "Gold", 4: "Platinum", 5: "Diamond"}
        affinity = _affmap.get(_affl, f"Level {_affl}")

        _skin_ts = player_data.get(176, 0)
        latest_skin_date = format_timestamp(_skin_ts) if _skin_ts else "N/A"
        _skin_id = player_data.get(175, 0)
        latest_skin_tier = get_skin_tier(_skin_id) if _skin_id else "N/A"

        _time_now = time.time()
        _sl_expiry = 0
        for t in (21, 47, 50):
            for src in ((role_info or {}), player_data):
                v = src.get(t, 0) or 0
                if isinstance(v, int) and v > 1700000000:
                    _sl_expiry = v
                    break
            if _sl_expiry:
                break
        if _sl_expiry:
            starlight_user = "Yes" if _sl_expiry > _time_now else "No"
            starlight_expiry = format_timestamp(_sl_expiry)
        else:
            starlight_user = "No"
            starlight_expiry = "N/A"

        starlight_months = player_data.get(60, 0)

        tickets = 0
        if role_info:
            tickets = role_info.get(49, 0)
        if not tickets:
            tickets = player_data.get(49, 0)

        total_wins = player_data.get(18, 0)
        _MIN_TS = 1451577600
        _create_fb = player_data.get(6, 0)
        if creation_ts and creation_ts >= _MIN_TS:
            creation_date = format_timestamp_full(creation_ts)
        elif _create_fb and _create_fb >= _MIN_TS:
            creation_date = format_timestamp_full(_create_fb)
        else:
            creation_date = "N/A"

        account_age = "N/A"
        age_ts = creation_ts if (creation_ts and creation_ts >= _MIN_TS) else (_create_fb if (_create_fb and _create_fb >= _MIN_TS) else 0)
        if age_ts:
            now = datetime.datetime.now(datetime.timezone.utc)
            created = datetime.datetime.fromtimestamp(age_ts, datetime.timezone.utc)
            delta = now - created
            y = delta.days // 365
            m = (delta.days % 365) // 30
            d = delta.days % 30
            if y > 0:
                account_age = f"{y}y {m}m {d}d"
            elif m > 0:
                account_age = f"{m}m {d}d"
            else:
                account_age = f"{delta.days}d"

        mcl_wins = role_info.get(46, 0) if role_info else 0
        if not mcl_wins:
            mcl_wins = player_data.get(104, player_data.get(103, 0))

        win_count = role_info.get(22, 0) if role_info else 0
        total_battles = (role_info.get(77, 0) if role_info else 0) or player_data.get(17, 0)
        wins_for_rate = win_count if win_count else total_wins
        if total_battles > 0 and wins_for_rate > 0:
            wr = (wins_for_rate / total_battles) * 100
            win_rate = f"{min(wr, 100):.1f}%"
        else:
            win_rate = "N/A"

        diamonds = 0
        bp = 0
        if role_info:
            cur = role_info.get(111)
            if isinstance(cur, dict):
                diamonds = int(cur.get(0, 0) or 0)
                bp = int(cur.get(1, 0) or 0)
            elif isinstance(cur, int):
                diamonds = int(cur or 0)
        if not bp:
            bp = int(player_data.get(83, 0) or 0)

        _dbuy = player_data.get(42, 0)
        last_diamond = format_timestamp(_dbuy) if isinstance(_dbuy, int) and _dbuy > 1000000000 else "N/A"

        skin_breakdown = {
            "Supreme Skins": 0, "Grand Skins": 0, "Exquisite Skins": 0,
            "Deluxe Skins": 0, "Exceptional Skins": 0, "Common Skins": 0,
        }
        _t118 = (role_info or {}).get(118) or player_data.get(118)
        if _t118:
            skin_breakdown.update(parse_skin_counts(_t118))

        return {
            "nickname": nickname,
            "player_id": player_id,
            "server": server,
            "level": level,
            "skin_count": skin_count,
            "hero_count": hero_count,
            "matches": matches,
            "rating_score": rating_score,
            "location": location,
            "last_login": last_login,
            "last_login_country": last_login_country,
            "create_account_country": create_country,
            "high_rank": high_rank,
            "current_rank": current_rank,
            "achievement_points": achievement_points,
            "collector_point": collector_point,
            "collector_tier": collector_tier,
            "hero_history": hero_history,
            "squad": squad,
            "skin_breakdown": skin_breakdown,
            "affinity": affinity,
            "likes": likes,
            "credits_score": credits_score,
            "followers": followers,
            "popularity": popularity,
            "bio": bio or None,
            "latest_skin_date": latest_skin_date,
            "starlight_user": starlight_user,
            "starlight_expiry": starlight_expiry,
            "starlight_months": starlight_months or None,
            "tickets": tickets or None,
            "total_wins": total_wins or None,
            "restriction_flags": restriction_flags,
            "mcl_champion_wins": mcl_wins,
            "v2l_status": v2l_status,
            "creation_date": creation_date,
            "account_age": account_age,
            "win_count": win_count,
            "total_battles": total_battles,
            "win_rate": win_rate,
            "battle_points": bp or None,
            "diamonds": diamonds or None,
            "last_diamond_purchase": last_diamond if last_diamond != "N/A" else None,
        }
    except Exception:
        return None

# ── PIPELINE ────────────────────────────────────────────────────────
def check_ban_only(device_id: str) -> Dict[str, Any]:
    """simple.py flow. Returns {'banned', 'ban_info', 'account_id', 'zone_id', 'session_key', 'creation_ts'}"""
    out = {"banned": False, "ban_info": {}, "account_id": 0, "zone_id": 0,
           "session_key": "", "creation_ts": 0}
    conn = MLBBConnection(device_id)
    try:
        conn.connect("login.ml.youngjoygame.com", 30021)
        conn.send_data(1, SdpStruct({
            0: conn.device_id,
            1: f"gps_adid={conn.advertising_id}&android_id={conn.android_id}&device_unique_id={conn.imei_md5}",
            2: CLIENT_VERSION, 3: CHANNEL, 4: "en",
        }))
        pid, res = conn.recv_data()
        b, info = inspect_for_ban(res)
        if b:
            out["banned"], out["ban_info"] = True, info
            return out
        if pid != 2 or not res:
            return out

        out["account_id"]  = int(res.get(0, 0) or 0)
        out["session_key"] = res.get(1, "")
        out["creation_ts"] = res.get(19, 0) or 0
        z = res.get(2)
        if isinstance(z, dict):
            out["zone_id"] = int(z.get(0, 0) or 0)
        elif isinstance(z, list) and z:
            first = z[0]
            out["zone_id"] = int((first.get(0, 0) if isinstance(first, dict) else first) or 0)
        else:
            out["zone_id"] = int(z or 0)

        conn.send_data(5, SdpStruct({
            0: out["account_id"], 1: out["session_key"], 2: CLIENT_VERSION,
            5: out["zone_id"], 6: CHANNEL,
        }))
        pid, res = conn.recv_data()
        b, info = inspect_for_ban(res)
        if b:
            out["banned"], out["ban_info"] = True, info
            return out
        if pid != 6 or not res:
            return out
        host, port = res[1].split(":")
        conn.cleanup()
        conn.connect(host, int(port))
        conn.send_data(10001, SdpStruct({
            0: out["account_id"], 1: out["session_key"], 2: out["zone_id"],
            4: CLIENT_VERSION, 13: CHANNEL, 15: conn.device_id,
        }))
        conn.send_data(10101, SdpStruct({0: 0, 2: 2}))

        role_req = False
        deadline = time.time() + 20
        while time.time() < deadline:
            pid, res = conn.recv_data()
            b, info = inspect_for_ban(res)
            if b:
                out["banned"], out["ban_info"] = True, info
                return out
            if pid is None or pid == -1:
                break
            if pid == 10002 and not role_req:
                conn.send_data(10003, SdpStruct({
                    0: out["account_id"], 1: out["session_key"], 2: out["zone_id"],
                    3: CLIENT_VERSION, 4: CHANNEL, 5: conn.device_id,
                }))
                role_req = True
            elif pid in (10004, 10008):
                break
    except Exception:
        pass
    finally:
        conn.cleanup()
    return out

def fetch_player_info(device_id: str, role_id: int, zone_id: int, creation_ts: int = 0) -> Optional[Dict]:
    """info.py flow. Opens a fresh connection, handshakes, then pulls lookup + skin + v2l."""
    conn = MLBBConnection(device_id)
    try:
        conn.connect("login.ml.youngjoygame.com", 30021)
        conn.send_data(1, SdpStruct({
            0: conn.device_id,
            1: f"gps_adid={conn.advertising_id}&android_id={conn.android_id}&device_unique_id={conn.imei_md5}",
            2: CLIENT_VERSION, 3: CHANNEL, 4: "en",
        }))
        pid, res = conn.recv_data()
        if pid != 2 or not res:
            return None
        acc_id = res.get(0)
        sess   = res.get(1)
        z = res.get(2)
        if isinstance(z, dict):
            my_zone = int(z.get(0, 0) or 0)
        elif isinstance(z, list) and z:
            first = z[0]
            my_zone = int((first.get(0, 0) if isinstance(first, dict) else first) or 0)
        else:
            my_zone = int(z or 0)
        if not creation_ts:
            creation_ts = res.get(19, 0) or 0

        conn.send_data(5, SdpStruct({
            0: acc_id, 1: sess, 2: CLIENT_VERSION, 5: my_zone, 6: CHANNEL,
        }))
        pid, res = conn.recv_data()
        if pid != 6 or not res:
            return None
        host, port = res[1].split(":")
        conn.cleanup()
        conn.connect(host, int(port))
        conn.send_data(10001, SdpStruct({
            0: acc_id, 1: sess, 2: my_zone, 4: CLIENT_VERSION,
            13: CHANNEL, 15: conn.device_id,
        }))
        conn.send_data(10101, SdpStruct({0: 0, 2: 2}))

        for _ in range(10):
            pid, res = conn.recv_data()
            if pid == 10002:
                break
            if pid is None or pid == -1:
                return None

        # ── lookup by role_id (pkt 11153 → 11154)
        lookup_res = None
        conn.send_data(11153, SdpStruct({1: int(role_id)}))
        deadline = time.time() + 15
        while time.time() < deadline:
            pid, res = conn.recv_data()
            if pid is None:
                break
            if pid == -1:
                break
            if pid == 11154:
                lookup_res = res
                break
        if not lookup_res:
            return None

        # ── skin info (10143 → 10144)
        role_info_merged: Dict[Any, Any] = {}
        try:
            conn.send_data(10143, SdpStruct({0: int(role_id), 1: int(zone_id)}))
            dl = time.time() + 8
            while time.time() < dl:
                pid, res = conn.recv_data()
                if pid is None or pid == -1:
                    break
                if pid == 10144 and res:
                    for k, v in res.items():
                        role_info_merged[k] = v
                    break
        except Exception:
            pass

        # ── v2l status (10208 → 10208)
        v2l_data = None
        try:
            conn.send_data(10208, SdpStruct({0: int(role_id), 1: int(zone_id)}))
            dl = time.time() + 6
            while time.time() < dl:
                pid, res = conn.recv_data()
                if pid is None or pid == -1:
                    break
                if pid == 10208 and res:
                    v2l_data = {"_source": 10208, "_data": dict(res)}
                    break
        except Exception:
            pass

        return extract_player_data(
            lookup_res, role_info=role_info_merged or None,
            creation_ts=creation_ts, v2l_data=v2l_data,
        )
    except Exception:
        return None
    finally:
        conn.cleanup()

def process_device(device_id: str) -> Dict[str, Any]:
    """Full pipeline: ban check, then (if clean) player info."""
    device_id = device_id.strip()
    if not device_id:
        return {"status": "error", "reason": "empty device id"}

    ban = check_ban_only(device_id)
    if ban["banned"]:
        return {"status": "banned", "device_id": device_id, "ban_info": ban["ban_info"]}

    acc_id  = ban.get("account_id") or 0
    zone_id = ban.get("zone_id") or 0
    if not acc_id or not zone_id:
        return {"status": "error", "reason": "login returned no account", "device_id": device_id}

    player = fetch_player_info(device_id, acc_id, zone_id, creation_ts=ban.get("creation_ts", 0))
    if not player:
        return {"status": "unknown", "device_id": device_id,
                "account_id": acc_id, "zone_id": zone_id,
                "reason": "info lookup failed"}

    return {
        "status": "clean",
        "device_id": device_id,
        "account_id": acc_id,
        "zone_id": zone_id,
        "player": player,
    }

# ── FORMATTING ──────────────────────────────────────────────────────
def _esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def format_ban_reply(result: Dict) -> str:
    info = result.get("ban_info", {})
    reason = info.get("reason_name", "Using Plug-in Apps to Compromise Competitive Fairness")
    code   = info.get("ban_code", "?")
    d = info.get("endtime_day", "?")
    h = info.get("endtime_hour", "00")
    m = info.get("endtime_min", "00")
    s = info.get("endtime_sec", "00")
    return (
        f"<b>🚫 BANNED DEVICE</b>\n"
        f"<code>{_esc(result['device_id'])}</code>\n\n"
        f"<b>Reason:</b> {_esc(reason)}\n"
        f"<b>Code:</b> <code>{_esc(code)}</code>\n"
        f"<b>Duration:</b> Day {_esc(d)}, {_esc(h)}:{_esc(m)}:{_esc(s)}"
    )

def format_clean_reply(result: Dict) -> str:
    p = result["player"]
    bd = p.get("skin_breakdown", {})
    lines = [
        f"<b>✅ CLEAN — PLAYER PROFILE</b>",
        f"<code>{_esc(result['device_id'])}</code>",
        "",
        f"<b>Name:</b> {_esc(p.get('nickname'))}",
        f"<b>Account ID:</b> <code>{_esc(result.get('account_id'))}</code>   "
        f"<b>Zone:</b> <code>{_esc(result.get('zone_id'))}</code>",
        f"<b>Level:</b> {_esc(p.get('level'))}   <b>Country:</b> {_esc(p.get('create_account_country'))}",
        f"<b>Account Age:</b> {_esc(p.get('account_age'))}   <b>Created:</b> {_esc(p.get('creation_date'))}",
        "",
        f"<b>Current Rank:</b> {_esc(p.get('current_rank'))}",
        f"<b>Highest Rank:</b> {_esc(p.get('high_rank'))}",
        f"<b>Win Rate:</b> {_esc(p.get('win_rate'))}   <b>Battles:</b> {_esc(p.get('total_battles'))}",
        "",
        f"<b>Heroes:</b> {_esc(p.get('hero_count'))}   <b>Skins:</b> {_esc(p.get('skin_count'))}",
        f"<b>Collector:</b> {_esc(p.get('collector_tier'))} ({_esc(p.get('collector_point'))} pts)",
        f"<b>Achievement:</b> {_esc(p.get('achievement_points'))}   "
        f"<b>Followers:</b> {_esc(p.get('followers'))}   <b>Likes:</b> {_esc(p.get('likes'))}",
        f"<b>Restriction:</b> {_esc(p.get('restriction_flags'))}",
        f"<b>V2L:</b> {_esc(p.get('v2l_status'))}   <b>Starlight:</b> {_esc(p.get('starlight_user'))}",
        "",
        "<b>Skin Breakdown</b>",
        f"  Supreme   : {bd.get('Supreme Skins', 0)}",
        f"  Grand     : {bd.get('Grand Skins', 0)}",
        f"  Exquisite : {bd.get('Exquisite Skins', 0)}",
        f"  Deluxe    : {bd.get('Deluxe Skins', 0)}",
        f"  Exceptional: {bd.get('Exceptional Skins', 0)}",
        f"  Common    : {bd.get('Common Skins', 0)}",
        "",
        f"<b>Last Login:</b> {_esc(p.get('last_login'))}",
        f"<b>Location:</b> {_esc(p.get('location'))}",
    ]
    heroes = p.get("hero_history") or []
    if heroes:
        lines.append(f"<b>Recent Heroes:</b> {_esc(', '.join(heroes[:10]))}")
    if p.get("bio"):
        lines.append(f"<b>Bio:</b> {_esc(p['bio'])}")
    return "\n".join(lines)

def format_error_reply(result: Dict) -> str:
    return (
        f"<b>⚠️ LOOKUP FAILED</b>\n"
        f"<code>{_esc(result.get('device_id','?'))}</code>\n"
        f"Reason: {_esc(result.get('reason','unknown'))}"
    )

# ── TELEGRAM API ────────────────────────────────────────────────────
def tg_api(method: str, **kwargs):
    url = f"{API}/{method}"
    try:
        r = requests.post(url, json=kwargs, timeout=40)
        return r.json()
    except Exception:
        return {"ok": False}

def tg_send(chat_id, text, reply_to=None, parse_mode="HTML"):
    payload = {
        "chat_id": chat_id,
        "text": text[:4096],
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    return tg_api("sendMessage", **payload)

def tg_edit(chat_id, msg_id, text, parse_mode="HTML"):
    return tg_api("editMessageText", chat_id=chat_id, message_id=msg_id,
                  text=text[:4096], parse_mode=parse_mode, disable_web_page_preview=True)

def tg_get_file(file_id):
    r = tg_api("getFile", file_id=file_id)
    if not r.get("ok"):
        return None
    path = r["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}"
    return url

def tg_download(file_url) -> Optional[bytes]:
    try:
        r = requests.get(file_url, timeout=60)
        if r.status_code == 200:
            return r.content
    except Exception:
        pass
    return None

# ── LOGGING ─────────────────────────────────────────────────────────
_log_lock = threading.Lock()
def append_line(path, line):
    with _log_lock:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

# ── PER-USER JOBS ───────────────────────────────────────────────────
_executor = ThreadPoolExecutor(max_workers=WORKERS)
_bot_username = ""

def run_single(chat_id, device_id, reply_to=None):
    def job():
        try:
            result = process_device(device_id)
            s = result.get("status")
            if s == "banned":
                tg_send(chat_id, format_ban_reply(result), reply_to=reply_to)
                append_line(BAN_FILE, f"{result['device_id']} | {result['ban_info']}")
            elif s == "clean":
                tg_send(chat_id, format_clean_reply(result), reply_to=reply_to)
                append_line(CLEAN_FILE, f"{result['device_id']} | acc={result['account_id']} zone={result['zone_id']} name={result['player'].get('nickname')}")
            elif s == "unknown":
                tg_send(chat_id, format_error_reply(result), reply_to=reply_to)
                append_line(UNKNOWN_FILE, result["device_id"])
            else:
                tg_send(chat_id, format_error_reply(result), reply_to=reply_to)
        except Exception as e:
            tg_send(chat_id, f"<b>❌ pipeline crash</b>\n<code>{_esc(traceback.format_exc()[-400:])}</code>", reply_to=reply_to)
    _executor.submit(job)

def run_bulk(chat_id, device_ids: List[str], reply_to=None):
    total = len(device_ids)
    if total == 0:
        tg_send(chat_id, "Empty list, chef.", reply_to=reply_to)
        return

    status = tg_send(chat_id, f"🔎 <b>Bulk starting</b>\nTotal: {total}")
    msg_id = status.get("result", {}).get("message_id") if status.get("ok") else None

    counter = {"done": 0, "banned": 0, "clean": 0, "unknown": 0}
    lock = threading.Lock()

    def update_status(final=False):
        with lock:
            txt = (
                f"{'✅ DONE' if final else '🔎 Bulk running'}\n"
                f"Processed: {counter['done']}/{total}\n"
                f"🚫 Banned : {counter['banned']}\n"
                f"✅ Clean  : {counter['clean']}\n"
                f"⚠️ Unknown: {counter['unknown']}"
            )
        if msg_id:
            tg_edit(chat_id, msg_id, txt)

    def job(device_id):
        try:
            r = process_device(device_id)
            with lock:
                counter["done"] += 1
                if r.get("status") == "banned":
                    counter["banned"] += 1
                    append_line(BAN_FILE, device_id)
                    tg_send(chat_id, format_ban_reply(r))
                elif r.get("status") == "clean":
                    counter["clean"] += 1
                    append_line(CLEAN_FILE, device_id)
                    tg_send(chat_id, format_clean_reply(r))
                else:
                    counter["unknown"] += 1
                    append_line(UNKNOWN_FILE, device_id)
            update_status()
        except Exception:
            with lock:
                counter["done"] += 1
                counter["unknown"] += 1
            update_status()

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = [ex.submit(job, d) for d in device_ids]
        for _ in as_completed(futures):
            pass

    update_status(final=True)
    tg_send(chat_id, f"🏁 Bulk done. {total} devices processed.", reply_to=reply_to)

# ── MESSAGE ROUTING ─────────────────────────────────────────────────
HELP_TEXT = (
    "<b>MLBB Device Checker Bot</b>\n\n"
    "<b>/check &lt;device_id&gt;</b> — single device: ban check + full profile\n"
    "<b>/bulk</b> — send a .txt file, one device ID per line\n"
    "<b>/status</b> — bot status\n"
    "<b>/help</b> — this message\n\n"
    "Device IDs start with <code>and_</code> or <code>ios_</code>."
)

def handle_message(msg: Dict):
    chat_id = msg["chat"]["id"]
    from_id = msg.get("from", {}).get("id")
    text = msg.get("text", "") or ""
    reply_to = msg.get("message_id")

    if ALLOWED_USERS and from_id not in ALLOWED_USERS:
        tg_send(chat_id, "⛔ not authorized")
        return

    if "document" in msg:
        doc = msg["document"]
        fname = (doc.get("file_name") or "").lower()
        if not (fname.endswith(".txt") or fname.endswith(".csv")):
            tg_send(chat_id, "Send a .txt file, one device ID per line.", reply_to=reply_to)
            return
        url = tg_get_file(doc["file_id"])
        if not url:
            tg_send(chat_id, "Couldn't fetch file.", reply_to=reply_to)
            return
        raw = tg_download(url)
        if not raw:
            tg_send(chat_id, "Couldn't download file.", reply_to=reply_to)
            return
        try:
            content = raw.decode("utf-8", errors="ignore")
        except Exception:
            content = ""
        ids = [ln.strip() for ln in content.splitlines() if ln.strip() and ("and_" in ln or "ios_" in ln)]
        ids = [ln.split()[0] for ln in ids]
        if not ids:
            tg_send(chat_id, "No valid device IDs found in file.", reply_to=reply_to)
            return
        run_bulk(chat_id, ids, reply_to=reply_to)
        return

    if text.startswith("/start") or text.startswith("/help"):
        tg_send(chat_id, HELP_TEXT, reply_to=reply_to)
        return

    if text.startswith("/status"):
        tg_send(chat_id, f"🟢 alive\nworkers: {WORKERS}\nresults: <code>{RESULTS_DIR}</code>", reply_to=reply_to)
        return

    if text.startswith("/bulk"):
        tg_send(chat_id, "Send a .txt file (one device ID per line).", reply_to=reply_to)
        return

    if text.startswith("/check"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            tg_send(chat_id, "Usage: <code>/check and_xxxx...</code>", reply_to=reply_to)
            return
        dev = parts[1].strip().split()[0]
        if not (dev.startswith("and_") or dev.startswith("ios_")):
            tg_send(chat_id, "Device ID must start with <code>and_</code> or <code>ios_</code>.", reply_to=reply_to)
            return
        tg_send(chat_id, f"🔎 checking <code>{_esc(dev[:40])}...</code>", reply_to=reply_to)
        run_single(chat_id, dev, reply_to=reply_to)
        return

    # plain device id → treat as single check
    if text.strip().startswith("and_") or text.strip().startswith("ios_"):
        dev = text.strip().split()[0]
        tg_send(chat_id, f"🔎 checking <code>{_esc(dev[:40])}...</code>", reply_to=reply_to)
        run_single(chat_id, dev, reply_to=reply_to)
        return

    if text.strip():
        tg_send(chat_id, HELP_TEXT, reply_to=reply_to)

# ── MAIN LOOP ───────────────────────────────────────────────────────
def main():
    global _bot_username
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_TOKEN_HERE":
        print("[!] Set MLBB_BOT_TOKEN env var first.")
        sys.exit(1)

    me = tg_api("getMe")
    if not me.get("ok"):
        print("[!] Invalid bot token:", me)
        sys.exit(1)
    _bot_username = me["result"].get("username", "")
    print(f"[+] Bot up: @{_bot_username}")
    print(f"[+] Results dir: {RESULTS_DIR}")
    print(f"[+] Workers: {WORKERS}")

    # drop any stale webhook so long polling works
    tg_api("deleteWebhook", drop_pending_updates=False)

    offset = None
    while True:
        try:
            resp = tg_api("getUpdates", offset=offset, timeout=25,
                          allowed_updates=["message"])
            if not resp.get("ok"):
                time.sleep(3)
                continue
            for update in resp.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message")
                if not msg:
                    continue
                try:
                    handle_message(msg)
                except Exception as e:
                    print("[!] handler error:", e)
        except KeyboardInterrupt:
            print("\n[+] bye")
            break
        except Exception as e:
            print("[!] poll error:", e)
            time.sleep(3)

if __name__ == "__main__":
    main()
