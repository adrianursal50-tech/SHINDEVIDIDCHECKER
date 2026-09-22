# engine.py
"""
🥔 Potato MLBB Engine — merged simple.py + info.py
  • SDP wire codec (SdpStruct)
  • MLBBConn — login → server-select → game-server handshake
  • Ban check   : check_device_ban() / check_device_ban_silent()
  • Player info : lookup_player_data()

Standalone CLI:   python engine.py
Library use:      from engine import check_device_ban_silent, lookup_player_data

Deps: colorama zstandard pycryptodome curl_cffi requests
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import random
import re
import socket
import struct
import sys
import threading
import time
import urllib3
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import zstandard as zstd
from Crypto.Cipher import AES
from colorama import Fore, Style, init as _cinit

try:
    import requests  # noqa: F401  (kept for parity / optional HTTP probes)
except Exception:
    requests = None

_cinit(autoreset=True)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger("potato.engine")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[engine] %(message)s"))
    log.addHandler(_h)
log.setLevel(logging.INFO)


# ── GLOBAL MODES ───────────────────────────────────────────────────────
DEBUG_MODE: bool = False
VERBOSE_MODE: bool = False


def dbg(label: str, data: Any = None, color: str = Fore.MAGENTA) -> None:
    if not DEBUG_MODE:
        return
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"{color}[DBG {ts}] {label}{Style.RESET_ALL}")
    if data is None:
        return
    if isinstance(data, (bytes, bytearray)):
        hx = data.hex()
        for i in range(0, len(hx), 64):
            print(f"  {Fore.CYAN}{hx[i:i+64]}{Style.RESET_ALL}")
    elif isinstance(data, dict):
        for k, v in data.items():
            print(f"  {Fore.CYAN}[{k}] => {repr(v)[:120]}{Style.RESET_ALL}")
    else:
        print(f"  {Fore.CYAN}{repr(data)[:200]}{Style.RESET_ALL}")


EMPTY = object()  # sentinel for debug prints


# ── SDP WIRE CODEC ─────────────────────────────────────────────────────
class SdpDataType(Enum):
    INTEGER_POSITIVE = 0
    INTEGER_NEGATIVE = 1
    FLOAT            = 2
    DOUBLE           = 3
    STRING           = 4
    LIST             = 5
    DICT             = 6
    STRUCT_BEGIN     = 7
    STRUCT_END       = 8


class SdpException(Exception):
    pass


class SdpStruct(dict):
    """Length-prefixed tagged wire struct. Pack/unpack round-trips."""

    def __init__(self, data: Any = None):
        super().__init__()
        self.data: bytes = b""
        self.offset: int = 0
        if isinstance(data, bytes):
            self.data = data
            self.offset = 0
            self._unpack_from_binary()
        elif data is not None:
            super().update(data)
            self._pack_to_binary()

    # -- pack --
    def _pack_to_binary(self) -> None:
        self.data = bytes([SdpDataType.STRUCT_BEGIN.value << 4])
        for tag, value in sorted(self.items()):
            self._pack(tag, value)
        self.data += bytes([SdpDataType.STRUCT_END.value << 4])

    def _write_number(self, value: int) -> bytes:
        out = bytearray()
        while value >= 0x80:
            out.append((value & 0x7F) | 0x80)
            value >>= 7
        out.append(value & 0x7F)
        return bytes(out)

    def _pack_header(self, tag: int, data_type: SdpDataType) -> None:
        if tag < 15:
            self.data += bytes([(data_type.value << 4) | tag])
        else:
            self.data += bytes([(data_type.value << 4) | 15])
            self.data += self._write_number(tag)

    def _pack(self, tag: int, value: Any) -> None:
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
            raise SdpException(f"Unsupported type: {type(value)}")

    # -- unpack --
    def _unpack_from_binary(self) -> None:
        if not self.data:
            return
        if self.data[0] >> 4 == SdpDataType.STRUCT_BEGIN.value:
            self.offset = 1
        while self.offset < len(self.data):
            tag, value = self._unpack()
            if isinstance(value, SdpDataType) and value == SdpDataType.STRUCT_END:
                break
            self[tag] = value

    def _read_number(self) -> int:
        n = 1
        val = self.data[self.offset] & 0x7F
        while self.data[self.offset + n - 1] >= 0x80:
            val |= (self.data[self.offset + n] & 0x7F) << (7 * n)
            n += 1
        self.offset += n
        return val

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
            if data_type == SdpDataType.INTEGER_NEGATIVE:
                return tag, -self._read_number()
            if data_type == SdpDataType.FLOAT:
                raw = self._read_number().to_bytes(4, "little")
                return tag, struct.unpack("<f", raw)[0]
            if data_type == SdpDataType.DOUBLE:
                raw = self._read_number().to_bytes(8, "little")
                return tag, struct.unpack("<d", raw)[0]
            if data_type == SdpDataType.STRING:
                length = self._read_number()
                try:
                    val: Any = self.data[self.offset:self.offset + length].decode("utf-8")
                except UnicodeDecodeError:
                    val = self.data[self.offset:self.offset + length]
                self.offset += length
                return tag, val
            if data_type == SdpDataType.LIST:
                length = self._read_number()
                out = []
                for _ in range(length):
                    _, item = self._unpack()
                    out.append(item)
                return tag, out
            if data_type == SdpDataType.DICT:
                length = self._read_number()
                out = {}
                for _ in range(length):
                    _, k = self._unpack()
                    _, v = self._unpack()
                    out[k] = v
                return tag, out
            if data_type == SdpDataType.STRUCT_BEGIN:
                inner = {}
                while True:
                    sub_tag, sub_val = self._unpack()
                    if isinstance(sub_val, SdpDataType) and sub_val == SdpDataType.STRUCT_END:
                        break
                    inner[sub_tag] = sub_val
                return tag, SdpStruct(inner)
            if data_type == SdpDataType.STRUCT_END:
                return tag, SdpDataType.STRUCT_END
            raise SdpException("Unknown data type")
        except Exception as e:
            raise SdpException(f"Unpack error: {e}") from e

    def copy(self) -> "SdpStruct":
        return SdpStruct(dict(self))

    def __repr__(self) -> str:
        return f"SdpStruct({dict(self)})"


# ── WIRE CONSTANTS ─────────────────────────────────────────────────────
AES_KEY   = bytes.fromhex("f5a193d50ade553e9835595f5cd75ddd")
AES_IV    = b"\x00" * 16
LOGIN_HOST = "login.ml.youngjoygame.com"
LOGIN_PORT = 30021
CLIENT_VERSION = "2.2.16.1232.1"
CHANNEL = "and_usa"


# ── DEVICE POOL ────────────────────────────────────────────────────────
DEVICE_IDS: List[str] = []          # caller populates; empty → anonymous
DEVICE_MODELS: List[str] = [
    "Xiaomi:23049PCD8G", "Xiaomi:Redmi Note 12", "Xiaomi:Redmi Note 13",
    "Xiaomi:POCO X6", "Xiaomi:Mi 11", "Xiaomi:Mi 14",
]


def load_device_ids() -> List[str]:
    if DEVICE_IDS:
        return DEVICE_IDS.copy()
    return ["ios_D08FE22A-7074-4DEB-BC67-78EEA4FD11B6_1714741163095700"]


def load_device_models() -> List[str]:
    return DEVICE_MODELS.copy() or ["Xiaomi:Redmi Note 12"]


def remove_failed_device_id(device_id: str) -> bool:
    if device_id in DEVICE_IDS:
        DEVICE_IDS.remove(device_id)
        return True
    return False


def _split_device_id(device_id: str) -> Tuple[str, str, str]:
    """Return (imei_md5, android_id, advertising_id)."""
    parts = device_id.split("_")
    info = parts[1] if len(parts) >= 2 else device_id
    if len(parts) >= 3 and len(info) < 32:
        info = info + "_" + parts[2]
    if len(info) >= 32:
        imei = info[:32]
        android = info[32:48] if len(info) >= 48 else ""
        adv = info[48:] if len(info) > 48 else ""
        return imei, android, adv
    return info, "", ""


# ── STATIC DATA ────────────────────────────────────────────────────────
HERO_ID_MAP: Dict[int, str] = {
    1:"Miya",2:"Balmond",3:"Saber",4:"Alice",5:"Nana",6:"Tigreal",7:"Alucard",8:"Karina",9:"Akai",
    10:"Franco",11:"Bane",12:"Bruno",13:"Clint",14:"Rafaela",15:"Eudora",16:"Zilong",17:"Fanny",
    18:"Layla",19:"Minotaur",20:"Lolita",21:"Hayabusa",22:"Freya",23:"Gord",24:"Natalia",25:"Kagura",
    26:"Chou",27:"Sun",28:"Alpha",29:"Ruby",30:"Yi Sun-shin",31:"Moskov",32:"Johnson",33:"Cyclops",
    34:"Estes",35:"Hilda",36:"Aurora",37:"Lapu-Lapu",38:"Vexana",39:"Roger",40:"Karrie",41:"Gatotkaca",
    42:"Harley",43:"Irithel",44:"Grock",45:"Argus",46:"Odette",47:"Lancelot",48:"Diggie",49:"Hylos",
    50:"Zhask",51:"Helcurt",52:"Pharsa",53:"Lesley",54:"Jawhead",55:"Angela",56:"Gusion",57:"Valir",
    58:"Martis",59:"Uranus",60:"Hanabi",61:"Chang'e",62:"Kaja",63:"Selena",64:"Aldous",65:"Claude",
    66:"Vale",67:"Leomord",68:"Lunox",69:"Hanzo",70:"Belerick",71:"Kimmy",72:"Thamuz",73:"Harith",
    74:"Minsitthar",75:"Kadita",76:"Faramis",77:"Badang",78:"Khufra",79:"Granger",80:"Guinevere",
    81:"Esmeralda",82:"Terizla",83:"X.Borg",84:"Ling",85:"Dyrroth",86:"Lylia",87:"Baxia",88:"Masha",
    89:"Wanwan",90:"Silvanna",91:"Cecilion",92:"Carmilla",93:"Atlas",94:"Popol and Kupa",95:"Yu Zhong",
    96:"Luo Yi",97:"Benedetta",98:"Khaleed",99:"Barats",100:"Brody",101:"Yve",102:"Mathilda",
    103:"Paquito",104:"Gloo",105:"Beatrix",106:"Phoveus",107:"Natan",108:"Aulus",109:"Aamon",
    110:"Valentina",111:"Edith",112:"Floryn",113:"Yin",114:"Melissa",115:"Xavier",116:"Julian",
    117:"Fredrinn",118:"Joy",119:"Novaria",120:"Arlott",121:"Ixia",122:"Nolan",123:"Cici",
    124:"Chip",125:"Zhuxin",126:"Suyou",127:"Lukas",128:"Kalea",129:"Zetian",130:"Obsidia",
}

_RANK_DEFS = [
    (0,4,"Warrior III"),(5,9,"Warrior II"),(10,14,"Warrior I"),
    (15,19,"Elite IV"),(20,24,"Elite III"),(25,29,"Elite II"),(30,34,"Elite I"),
    (35,39,"Master IV"),(40,44,"Master III"),(45,49,"Master II"),(50,54,"Master I"),
    (55,59,"Grandmaster IV"),(60,64,"Grandmaster III"),(65,69,"Grandmaster II"),(70,74,"Grandmaster I"),
    (75,81,"Epic IV"),(82,88,"Epic III"),(89,95,"Epic II"),(96,107,"Epic I"),
    (108,114,"Legend IV"),(115,121,"Legend III"),(122,128,"Legend II"),(129,135,"Legend I"),
    (136,160,lambda p: f"Mythic {p-135}"),
    (161,195,lambda p: f"Mythical Honor {p-135}"),
    (196,235,lambda p: f"Mythical Glory {p-157}"),
    (236,999,lambda p: f"Mythical Immortal {p-157}"),
]


def map_rank(p: int) -> str:
    for lo, hi, name in _RANK_DEFS:
        if lo <= p <= hi:
            return name(p) if callable(name) else name
    return "Unknown"


COLLECTOR_TIERS_ORDER = [
    "Amateur Collector","Junior Collector","Seasoned Collector","Expert Collector",
    "Renowned Collector","Exalted Collector","Mega Collector","World Collector",
]
_COLLECTOR_RANGES = [
    (1000, 4000), (4000, 10000), (10000, 22000), (22000, 44000),
    (44000, 84000), (84000, 160000), (160000, 280000), (280000, float("inf")),
]


def map_collector_point(point: int) -> str:
    if point < 1000:
        return "No Tier"
    for idx, (lo, hi) in enumerate(_COLLECTOR_RANGES):
        if lo <= point < hi:
            name = COLLECTOR_TIERS_ORDER[idx]
            if name == "World Collector":
                return name
            per_level = (hi - lo) / 5
            level = int((point - lo) // per_level)
            roman = ["V","IV","III","II","I"][level]
            return f"{name} {roman}"
    return "Unknown"


def _get_major_tier(tier_str: str) -> str:
    if not tier_str or tier_str in ("Unknown", ""):
        return "No Tier"
    for t in COLLECTOR_TIERS_ORDER:
        if tier_str.startswith(t):
            return t
    if tier_str == "No Tier":
        return "No Tier"
    return "Other"


# ── SKIN DB ────────────────────────────────────────────────────────────
SKIN_DB: Dict[str, str] = {}
_SKIN_TIER_LABELS = {
    "common":"Common","exceptional":"Exceptional","deluxe":"Deluxe",
    "exquisite":"Exquisite","grand":"Grand","supreme":"Supreme","unknown":"Unknown",
}
try:
    _here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(_here, "skin_db.json"), "r", encoding="utf-8") as _f:
        _raw = _f.read().strip()
        if not _raw.startswith("{"):
            _raw = "{" + _raw
        SKIN_DB = json.loads(_raw)
except Exception:
    SKIN_DB = {}


def get_skin_tier(skin_id: Any) -> Optional[str]:
    tier = SKIN_DB.get(str(skin_id))
    if not tier:
        return None
    return _SKIN_TIER_LABELS.get(str(tier).lower(), str(tier).capitalize())


def parse_skin_counts(tag_118: Any) -> Dict[str, int]:
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
    type_map = {
        6: "Supreme Skins", 5: "Grand Skins", 4: "Exquisite Skins",
        3: "Deluxe Skins", 2: "Exceptional Skins", 1: "Common Skins",
    }
    out: Dict[str, int] = {}
    for sid, count in skin_data.items():
        try:
            sid_i = int(sid)
        except (ValueError, TypeError):
            continue
        if sid_i in type_map:
            out[type_map[sid_i]] = count
    return out


# ── TIMESTAMP HELPERS ──────────────────────────────────────────────────
def format_timestamp(ts: int) -> str:
    try:
        utc = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        pht = utc + datetime.timedelta(hours=8)
        now_pht = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
        delta = pht - now_pht
        sec = int(delta.total_seconds())
        sign = "in " if sec >= 0 else ""
        sec = abs(sec)
        d, h, m = sec // 86400, (sec % 86400) // 3600, (sec % 3600) // 60
        rel = f"({sign}{d}d {h}h {m}m{' ' if sign else ' ago'})"
        if not sign:
            rel = rel.replace("  ago", " ago")
        return f"{pht.strftime('%Y-%m-%d %H:%M')} {rel} PHT"
    except Exception:
        return "Invalid timestamp"


def format_timestamp_full(ts: int) -> str:
    try:
        utc = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        pht = utc + datetime.timedelta(hours=8)
        return pht.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "Invalid timestamp"


# ── CONNECTION ─────────────────────────────────────────────────────────
class MLBBConn:
    """One connection lifecycle: login → server-select → game-server."""

    def __init__(self, device_id: str, device_model: Optional[str] = None):
        self.device_id = device_id
        self.device_model = device_model or "Xiaomi:Redmi Note 12"
        self.imei_md5, self.android_id, self.advertising_id = _split_device_id(device_id)

        self.host = LOGIN_HOST
        self.port = LOGIN_PORT
        self.sequence = 1
        self.socket: Optional[socket.socket] = None
        self.queue_data = b""
        self.last_header_size = 0

        self.account_id = 0
        self.session_key: Any = ""
        self.zone_id: Any = 0
        self.creation_ts = 0
        self.game_server_host = ""
        self.game_server_port = 0

    # context manager
    def __enter__(self):
        self.connect(self.host, self.port)
        return self

    def __exit__(self, exc_type, exc, tb):
        self.cleanup()

    # io
    def connect(self, host: str, port: int) -> None:
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.connect((host, port))
        self.socket.settimeout(5)
        self.host, self.port = host, port

    def cleanup(self) -> None:
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
        self.socket = None
        self.sequence = 1

    def send(self, pkt_id: int, sdp: SdpStruct) -> None:
        packet = SdpStruct({0: pkt_id, 1: self.sequence, 5: sdp.data}).data
        buf = zstd.compress(packet)
        flags = (len(buf) + 4) | (16 << 24)
        buf = flags.to_bytes(4, "big") + buf
        dbg(f"SEND id={pkt_id} seq={self.sequence} bytes={len(sdp.data)}")
        dbg("     fields", dict(sdp))
        assert self.socket is not None
        self.socket.send(buf)
        self.sequence += 1

    def recv(self) -> Tuple[Optional[int], Optional[SdpStruct]]:
        try:
            assert self.socket is not None
            while len(self.queue_data) < 4:
                chunk = self.socket.recv(4096)
                if not chunk:
                    return None, None
                self.queue_data += chunk

            flags = int.from_bytes(self.queue_data[:4], "big")
            size = flags & 0xFFFFFF
            ctype = flags >> 24
            self.last_header_size = size

            while len(self.queue_data) < size:
                chunk = self.socket.recv(4096)
                if not chunk:
                    return None, None
                self.queue_data += chunk

            data = self.queue_data[4:size]
            self.queue_data = self.queue_data[size:]

            if ctype == 1:
                data = zlib.decompress(data)
            elif ctype == 16:
                data = zstd.decompress(data)
            elif ctype in (2, 3, 18):
                cipher = AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV)
                data = cipher.decrypt(data[:-1] if len(data) % 16 != 0 else data)
                data = data.rstrip(b"\x00")
                if ctype == 3:
                    data = zlib.decompress(data)
                elif ctype == 18:
                    data = zstd.decompress(data)

            outer = SdpStruct(data)
            pkt_id = outer[0]
            if pkt_id is None:
                return None, None

            body = outer.get(6) or outer.get(5)
            if not body or not isinstance(body, bytes):
                return pkt_id, None
            parsed = SdpStruct(body)
            dbg(f"RECV id={pkt_id} ctype={ctype} bytes={len(body)}", color=Fore.CYAN)
            dbg("     fields", dict(parsed), color=Fore.CYAN)
            return pkt_id, parsed
        except socket.timeout:
            return -1, None
        except Exception:
            return None, None

    # handshake
    def login(self) -> bool:
        self.send(1, SdpStruct({
            0: self.device_id,
            1: f"gps_adid={self.advertising_id}&android_id={self.android_id}&device_unique_id={self.imei_md5}",
            2: CLIENT_VERSION,
            3: CHANNEL,
            4: "en",
        }))
        pid, res = self.recv()
        if pid == 2 and res:
            self.account_id = res.get(0)
            self.session_key = res.get(1)
            zone_data = res.get(2)
            if isinstance(zone_data, dict):
                self.zone_id = zone_data.get(0, 0)
            elif isinstance(zone_data, list) and zone_data:
                first = zone_data[0]
                self.zone_id = first.get(0, 0) if isinstance(first, dict) else first
            else:
                self.zone_id = zone_data or 0
            self.creation_ts = res.get(19, 0) or 0
            dbg(f"LOGIN OK account={self.account_id} zone={self.zone_id} "
                f"created={self.creation_ts}", color=Fore.GREEN)
            return True
        dbg(f"LOGIN FAILED pid={pid}", color=Fore.RED)
        return False

    def select_game_server(self) -> bool:
        self.send(5, SdpStruct({
            0: self.account_id, 1: self.session_key, 2: CLIENT_VERSION,
            5: self.zone_id, 6: CHANNEL,
        }))
        pid, res = self.recv()
        if pid == 6 and res and isinstance(res.get(1), str) and ":" in res[1]:
            host, port = res[1].split(":")
            self.game_server_host, self.game_server_port = host, int(port)
            dbg(f"GSEL OK {host}:{port}", color=Fore.GREEN)
            return True
        dbg(f"GSEL FAILED pid={pid}", color=Fore.RED)
        return False

    def open_game_server(self) -> bool:
        self.cleanup()
        self.connect(self.game_server_host, self.game_server_port)
        self.send(10001, SdpStruct({
            0: self.account_id, 1: self.session_key, 2: self.zone_id,
            4: CLIENT_VERSION, 13: CHANNEL, 15: self.device_id,
        }))
        self.send(10101, SdpStruct({0: 0, 2: 2}))
        while True:
            pid, _ = self.recv()
            if pid is None:
                return False
            if pid == 10002:
                dbg("GS HANDSHAKE OK", color=Fore.GREEN)
                return True
            if pid == -1:
                return False

    def full_login(self) -> bool:
        if not self.login():
            return False
        if not self.select_game_server():
            return False
        return True

    # players
    def lookup_player(self, value: Any, search_type: str = "id",
                      server_filter: Optional[int] = None) -> Optional[SdpStruct]:
        if search_type == "id":
            payload = SdpStruct({1: int(value)})
        else:
            payload = SdpStruct({0: str(value).strip()})
        self.send(11153, payload)

        interim = 0
        while True:
            pid, res = self.recv()
            if pid is None or pid == -1:
                return None
            if pid == 11154:
                if search_type == "nickname" and server_filter is not None:
                    return self._filter_by_server(res, server_filter)
                return res
            if pid == 20001:
                interim += 1
                if self.last_header_size < 100 and interim >= 2:
                    return None

    def _filter_by_server(self, result: Optional[SdpStruct],
                          target: int) -> Optional[SdpStruct]:
        if not result or not result.get(0):
            return None
        for player in result[0]:
            if isinstance(player, dict) and player.get(1) == target:
                return SdpStruct({0: [player]})
        return None

    def role_info(self, role_id: int, zone_id: int) -> Optional[SdpStruct]:
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
                if res.get(9, 0) > 0:
                    return res
        return best

    def skin_role_info(self, role_id: int, zone_id: int,
                       retries: int = 3) -> Optional[SdpStruct]:
        for _ in range(retries):
            self.send(10143, SdpStruct({0: int(role_id), 1: int(zone_id)}))
            timeouts = 0
            while timeouts < 3:
                pid, res = self.recv()
                if pid is None:
                    break
                if pid == -1:
                    timeouts += 1
                    continue
                if pid == 10144:
                    return res
        return None

    def v2l_status(self, role_id: int, zone_id: int,
                   retries: int = 2) -> Optional[Dict[str, Any]]:
        for _ in range(retries):
            self.send(10208, SdpStruct({0: int(role_id), 1: int(zone_id)}))
            timeouts = 0
            while timeouts < 2:
                pid, res = self.recv()
                if pid is None:
                    break
                if pid == -1:
                    timeouts += 1
                    continue
                if pid == 10208 and res:
                    return {"_source": 10208, "_data": dict(res)}

            self.send(10145, SdpStruct({0: int(role_id), 1: int(zone_id)}))
            timeouts = 0
            while timeouts < 2:
                pid, res = self.recv()
                if pid is None:
                    break
                if pid == -1:
                    timeouts += 1
                    continue
                if pid in (10146, 10160) and res:
                    return {"_source": pid, "_data": dict(res)}
        return None


# ── BAN CHECK ──────────────────────────────────────────────────────────
BAN_REASONS = {
    "21": "Using Plug-in Apps to Compromise Competitive Fairness",
}


def inspect_for_ban(sdp_data: Optional[SdpStruct]) -> Tuple[bool, Dict[str, Any]]:
    """Banned ONLY if explicit endtime_day is present. Returns (banned, details)."""
    details: Dict[str, Any] = {}
    if sdp_data:
        def scan(obj: Any) -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k == "ban_reason":
                        code = str(v)
                        details["ban_code"] = code
                        details["reason_name"] = BAN_REASONS.get(
                            code, "Using Plug-in Apps to Compromise Competitive Fairness")
                    elif k in ("ban_status", "ban_time") or (
                            isinstance(k, str) and "ban" in k.lower()):
                        details[str(k)] = v
                    if k in ("endtime_day", "endtime_hour", "endtime_min", "endtime_sec"):
                        details[str(k)] = v
                    if isinstance(v, (dict, list)):
                        scan(v)
            elif isinstance(obj, list):
                for item in obj:
                    scan(item)
        scan(dict(sdp_data))

    banned = details.get("endtime_day") is not None
    return banned, details


def _new_conn(device_id: str) -> MLBBConn:
    return MLBBConn(device_id, random.choice(load_device_models()))


def check_device_ban_silent(device_id: str) -> Tuple[str, str]:
    """
    Returns (status, payload).
      status ∈ {"BANNED","CLEAN","UNKNOWN"}
      payload is human-readable text.
    """
    conn = _new_conn(device_id)
    try:
        conn.connect(LOGIN_HOST, LOGIN_PORT)
        if not conn.full_login():
            return "UNKNOWN", device_id
        if not conn.open_game_server():
            return "UNKNOWN", device_id

        # Role verification handshake for ban verdict
        conn.send(10001, SdpStruct({
            0: conn.account_id, 1: conn.session_key, 2: conn.zone_id,
            4: CLIENT_VERSION, 13: CHANNEL, 15: conn.device_id,
        }))
        conn.send(10101, SdpStruct({0: 0, 2: 2}))

        role_requested = False
        while True:
            pid, res = conn.recv()
            banned, info = inspect_for_ban(res)
            if banned:
                return "BANNED", _ban_line(device_id, info)
            if pid is None or pid == -1:
                return "UNKNOWN", device_id
            if pid == 10002 and not role_requested:
                conn.send(10003, SdpStruct({
                    0: conn.account_id, 1: conn.session_key, 2: conn.zone_id,
                    3: CLIENT_VERSION, 4: CHANNEL, 5: conn.device_id,
                }))
                role_requested = True
            elif pid in (10004, 10008):
                return "CLEAN", _clean_line(device_id, conn.account_id, conn.zone_id)
    except Exception:
        return "UNKNOWN", device_id
    finally:
        conn.cleanup()


def _ban_line(device_id: str, info: Dict[str, Any]) -> str:
    reason = info.get("reason_name", BAN_REASONS["21"])
    day = info.get("endtime_day", "?")
    hh = info.get("endtime_hour", "00")
    mm = info.get("endtime_min", "00")
    ss = info.get("endtime_sec", "00")
    return f"{device_id} | Reason Name: {reason} | Duration: Day {day}, {hh}:{mm}:{ss}"


def _clean_line(device_id: str, account_id: int, zone_id: int) -> str:
    return (f"Device ID: {device_id}\n"
            f"Account ID: {account_id}\n"
            f"Zone ID: {zone_id}\n")


def check_device_ban(device_id: str) -> None:
    """CLI-printed version of check_device_ban_silent."""
    print(f"\n{Fore.MAGENTA}Checking {device_id[:60]}…{Style.RESET_ALL}")
    status, payload = check_device_ban_silent(device_id)
    color = {"BANNED": Fore.RED, "CLEAN": Fore.GREEN}.get(status, Fore.YELLOW)
    print(f"{color}[{status}]{Style.RESET_ALL} {payload}")


# ── PLAYER INFO ────────────────────────────────────────────────────────
def extract_player_data(result: Optional[SdpStruct],
                        role_info: Optional[SdpStruct] = None,
                        creation_ts: int = 0,
                        v2l_data: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    if not result or not result.get(0) or len(result[0]) == 0:
        return None
    try:
        p = result[0][0]
        nickname = p.get(2, "Unknown")
        player_id = p.get(0, "Unknown")
        server = p.get(1, "Unknown")
        level = p.get(3, "Unknown")
        skin = p.get(83, "Unknown")
        hero_count = p.get(4, 0)
        matches = p.get(17, 0)
        rating_score = p.get(9, 0)
        if role_info:
            hero_count = role_info.get(9, hero_count)
            matches = role_info.get(22, matches)

        loc_data = p.get(71)
        location = ", ".join(loc_data) if isinstance(loc_data, list) and len(loc_data) >= 2 else "NOT FOUND"

        last_login = format_timestamp(p.get(5, 0))
        last_country = p.get(87, "Unknown")
        create_country = p.get(97, "Unknown")

        squad_icon = p.get(31, "")
        squad_name = (p.get(30, "") or "").replace("`", "").strip()
        squad = f"{squad_icon} {squad_name}".strip() if squad_name else "—"
        squad_id = role_info.get(34, 0) if isinstance(role_info, dict) else 0
        if not squad_id:
            squad_id = p.get(34, p.get(28, 0))
        squad_id_display = f"Squad ID: {squad_id}" if squad_id else "N/A"

        tag_95 = p.get(95)
        tag_8 = p.get(8)
        high_rank = map_rank(tag_95) if tag_95 is not None else "Unknown"
        cur_rank = map_rank(tag_8) if tag_8 is not None else "Unknown"
        achievement = p.get(7, 0)

        tag_136 = p.get(136, {})
        coll_pts = tag_136.get(9, 0) if isinstance(tag_136, dict) else 0
        coll_rank = tag_136.get(10, 0) if isinstance(tag_136, dict) else 0
        coll_tier = map_collector_point(coll_pts)

        tag_91 = p.get(91, [])
        hero_hist = [HERO_ID_MAP.get(h, f"Unknown({h})") for h in reversed(tag_91)] if tag_91 \
                    else ["Private / Not Available"]

        v2l_status = "N/A"
        if v2l_data and isinstance(v2l_data, dict):
            src = v2l_data.get("_source", 0)
            data = v2l_data.get("_data", {})
            tags = (10, 11) if src == 10208 else (0, 2, 3, 5)
            for t in tags:
                v = data.get(t)
                if v is not None:
                    try:
                        v2l_status = "Enabled" if int(v) > 0 else "Disabled"
                        break
                    except (ValueError, TypeError):
                        pass

        followers = role_info.get(23, 0) if isinstance(role_info, dict) else 0
        followers = followers or p.get(15, 0)
        popularity = p.get(14, 0)
        bio_val = p.get(24, "")
        bio = bio_val.strip() if isinstance(bio_val, str) else ""

        likes = role_info.get(24, 0) if isinstance(role_info, dict) else 0
        likes = likes or p.get(61, 0)

        credits_score = "N/A"
        if isinstance(role_info, dict) and (role_info.get(20) or 0) > 0:
            credits_score = f"{role_info.get(20)}/110"
        elif isinstance(p.get(80), int) and p.get(80) > 0:
            credits_score = f"{p.get(80)}/110"

        # restriction flags
        t117 = role_info.get(117) if isinstance(role_info, dict) else None
        if t117 is None:
            t117 = p.get(117)
        restriction = "None"
        if t117 is not None:
            raw = t117.get(0, 0) if isinstance(t117, dict) else int(t117)
            pct = round((int(raw) + 1) / 7 * 100, 1)
            tag = "✅" if pct < 30 else ("⚠️" if pct < 60 else "🚨")
            label = "Low Risk" if pct < 30 else ("Medium Risk" if pct < 60 else "High Risk")
            restriction = f"{pct}% {tag} ({label})"

        # affinity
        t135 = p.get(135, {})
        aff_level = t135.get(1, 0) if isinstance(t135, dict) else 0
        aff_names = []
        if isinstance(role_info, dict):
            for entry in (role_info.get(82, []) or []):
                if isinstance(entry, dict) and isinstance(entry.get(2), str) and entry[2]:
                    aff_names.append(entry[2])
        affinity = ", ".join(aff_names) if aff_names else (
            {0:"None",1:"Bronze",2:"Silver",3:"Gold",4:"Platinum",5:"Diamond"}.get(aff_level, "None")
            if aff_level else "None")

        skin_ts = p.get(176, 0)
        latest_skin_date = format_timestamp(skin_ts) if skin_ts else "N/A"
        latest_skin_id = p.get(175, 0)
        latest_skin_tier = get_skin_tier(latest_skin_id) if latest_skin_id else "N/A"
        latest_skin_str = (f"{latest_skin_id} ({latest_skin_tier or 'Unknown Tier'})"
                           if latest_skin_id else "N/A")

        # starlight
        sl_expiry = 0
        for t in (21, 47, 50):
            if sl_expiry:
                break
            v = (role_info.get(t, 0) if isinstance(role_info, dict) else 0) or 0
            if isinstance(v, int) and v > 1_700_000_000:
                sl_expiry = v
        for t in (21, 47, 50):
            if sl_expiry:
                break
            v = p.get(t, 0) or 0
            if isinstance(v, int) and v > 1_700_000_000:
                sl_expiry = v
        if sl_expiry:
            starlight_user = "Yes ⭐" if sl_expiry > time.time() else "No"
            starlight_expiry = format_timestamp(sl_expiry)
        else:
            starlight_user = "No"
            starlight_expiry = "N/A"
        starlight_months = p.get(60, 0)

        tickets = (role_info.get(49, 0) if isinstance(role_info, dict) else 0) or p.get(49, 0)
        total_wins = p.get(18, 0)

        # creation date
        MIN_TS = 1_451_577_600
        create_fallback = p.get(6, 0)
        if creation_ts and creation_ts >= MIN_TS:
            creation_date = format_timestamp_full(creation_ts)
        elif create_fallback and create_fallback >= MIN_TS:
            creation_date = format_timestamp_full(create_fallback)
        else:
            creation_date = "N/A"

        account_age = "N/A"
        age_ts = creation_ts if (creation_ts and creation_ts >= MIN_TS) else (
            create_fallback if (create_fallback and create_fallback >= MIN_TS) else 0)
        if age_ts:
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            create_dt = datetime.datetime.fromtimestamp(age_ts, datetime.timezone.utc)
            days = (now_utc - create_dt).days
            y, m, d = days // 365, (days % 365) // 30, days % 30
            account_age = f"{y}y {m}m {d}d" if y else (f"{m}m {d}d" if m else f"{d}d")

        tag45 = p.get(45, {})
        if isinstance(tag45, dict) and tag45:
            entries = []
            for e in tag45.values():
                if isinstance(e, dict):
                    entries.append((e.get(1, 0), HERO_ID_MAP.get(e.get(0, 0), f"Unknown({e.get(0,0)})")))
            entries.sort(reverse=True)
            last_heroes = ", ".join(h for _, h in entries) if entries else "N/A"
        else:
            hts = p.get(175, 0)
            last_heroes = format_timestamp(hts) if hts else "N/A"

        mcl_wins = (role_info.get(46, 0) if isinstance(role_info, dict) else 0) or p.get(104, p.get(103, 0))
        win_count = role_info.get(22, 0) if isinstance(role_info, dict) else 0
        total_battles = (role_info.get(77, 0) if isinstance(role_info, dict) else 0) or p.get(17, 0)
        wins_for_rate = win_count or total_wins
        if total_battles > 0 and wins_for_rate > 0:
            wr = (wins_for_rate / total_battles) * 100
            win_rate = f"{min(wr, 100):.1f}%" + (" (approx)" if wr > 100 else "")
        else:
            win_rate = "N/A"

        squad_motto = "N/A"
        if isinstance(role_info, dict) and isinstance(role_info.get(24), str):
            squad_motto = role_info[24]

        diamonds = bp = 0
        if isinstance(role_info, dict):
            cur = role_info.get(111)
            if isinstance(cur, dict):
                diamonds = int(cur.get(0, 0) or 0)
                bp = int(cur.get(1, 0) or 0)
            elif isinstance(cur, int):
                diamonds = int(cur or 0)
        if not bp:
            bp = int(p.get(83, 0) or 0)

        diamond_ts = p.get(42, 0)
        last_diamond = format_timestamp(diamond_ts) if (
            isinstance(diamond_ts, int) and diamond_ts > 1_000_000_000) else "N/A"

        starlight_count = role_info.get(60, 0) if isinstance(role_info, dict) else 0

        # skin history
        skin_history = []
        if isinstance(role_info, dict):
            for e in (role_info.get(92, []) or [])[:5]:
                if isinstance(e, dict):
                    sid, tier, ts = e.get(0, 0), e.get(2, 0), e.get(4, 0)
                    tier_name = {1:"Common",2:"Exceptional",3:"Deluxe",
                                 4:"Exquisite",5:"Grand",6:"Supreme"}.get(tier, f"T{tier}")
                    skin_history.append(f"SkinID:{sid}({tier_name})|{format_timestamp(ts) if ts else '?'}")
        skin_history_str = ", ".join(skin_history) if skin_history else "N/A"

        # emblems
        emblem_levels = "N/A"
        emap = {1:"Fighter",2:"Assassin",3:"Mage",4:"Marksman",5:"Support",6:"Tank",7:"Common"}
        if isinstance(role_info, dict) and isinstance(role_info.get(101), dict) and role_info[101]:
            parts = [f"{emap.get(eid, f'E{eid}')}:Lv{role_info[101][eid]}"
                     for eid in sorted(role_info[101].keys())]
            emblem_levels = ", ".join(parts) if parts else "N/A"

        skin_counts = {"Supreme Skins":0,"Grand Skins":0,"Exquisite Skins":0,
                       "Deluxe Skins":0,"Exceptional Skins":0,"Common Skins":0}
        tag118 = (role_info.get(118) if isinstance(role_info, dict) else None) or p.get(118)
        if tag118:
            parsed = parse_skin_counts(tag118)
            if parsed:
                skin_counts.update(parsed)

        return {
            "nickname": nickname, "player_id": player_id, "server": server,
            "level": level, "skin_count": skin, "hero_count": hero_count,
            "matches": matches, "rating_score": rating_score, "location": location,
            "last_login": last_login, "last_login_country": last_country,
            "create_account_country": create_country,
            "high_rank": high_rank, "current_rank": cur_rank,
            "achievement_points": achievement,
            "collector_point": coll_pts, "collector_rank": coll_rank,
            "collector_tier": coll_tier, "hero_history": hero_hist,
            "squad": squad, "squad_id": squad_id_display, "skin_breakdown": skin_counts,
            "affinity": affinity, "likes": likes,
            "credits_score": credits_score if credits_score != "N/A" else None,
            "followers": followers, "popularity": popularity,
            "bio": bio or None,
            "latest_skin_date": latest_skin_date, "latest_skin_id": latest_skin_str,
            "starlight_user": starlight_user, "starlight_expiry": starlight_expiry,
            "starlight_months": starlight_months or None,
            "tickets": tickets or None, "total_wins": total_wins or None,
            "restriction_flags": restriction, "last_heroes_purchase": last_heroes,
            "mcl_champion_wins": mcl_wins, "v2l_status": v2l_status,
            "creation_date": creation_date, "account_age": account_age,
            "win_count": win_count, "total_battles": total_battles, "win_rate": win_rate,
            "battle_points": bp or None, "diamonds": diamonds or None,
            "last_diamond_purchase": last_diamond if last_diamond != "N/A" else None,
            "squad_motto": squad_motto if squad_motto != "N/A" else None,
            "starlight_count": starlight_count or None,
            "skin_history": skin_history_str if skin_history_str != "N/A" else None,
            "emblem_levels": emblem_levels if emblem_levels != "N/A" else None,
            "latest_skin_tier": latest_skin_tier,
        }
    except Exception:
        return None


def lookup_player_data(player_id_or_nickname: Any,
                       server_id: Optional[int] = None,
                       guid: Optional[str] = None,
                       session: Optional[str] = None,
                       combo_v2l: Optional[str] = None,
                       device_id: Optional[str] = None,
                       max_retries: int = 10) -> Dict[str, Any]:
    """
    Returns dict: {"status": "success", "player_data": {...}}
              or  {"status": "error",   "error": "..."}
    """
    try:
        try:
            search_value: Any = int(player_id_or_nickname)
            search_type = "id"
            server_filter = None
        except (ValueError, TypeError):
            search_type = "nickname"
            search_value = str(player_id_or_nickname).strip()
            if not server_id:
                return {"error": "server_id required for nickname search", "status": "error"}
            try:
                server_filter = int(server_id)
            except (ValueError, TypeError):
                return {"error": "server_id must be numeric", "status": "error"}

        pool = load_device_ids()
        if not pool:
            return {"error": "no device IDs available", "status": "error"}

        result = None
        creation_ts = 0
        role_info_data: Optional[Dict[str, Any]] = None
        v2l_data: Optional[Dict[str, Any]] = None
        last_error = "unknown"

        for attempt in range(max_retries):
            candidate_pool = [device_id] if device_id else list(pool)
            if not candidate_pool:
                break
            current = random.choice(candidate_pool)
            try:
                with _new_conn(current) as conn:
                    if not conn.full_login():
                        last_error = "login failed"
                        continue
                    if not conn.open_game_server():
                        last_error = "game server handshake failed"
                        continue

                    result = conn.lookup_player(search_value, search_type, server_filter)
                    creation_ts = conn.creation_ts

                    if result is None:
                        last_error = "lookup returned None"
                        continue

                    role_id = result[0][0].get(0, search_value) if result[0] else search_value
                    zone_id = result[0][0].get(1, 0) if result[0] else 0

                    try:
                        skin_role = conn.skin_role_info(role_id, zone_id)
                        if skin_role:
                            role_info_data = dict(skin_role)
                    except Exception:
                        pass

                    try:
                        v2l_data = conn.v2l_status(role_id, zone_id)
                    except Exception:
                        pass
                    break
            except ConnectionError:
                last_error = "connection error"
                continue
            except socket.timeout:
                last_error = "socket timeout"
                continue
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                continue

        if not result:
            return {"error": last_error, "status": "error"}

        player = extract_player_data(result, role_info=role_info_data,
                                     creation_ts=creation_ts, v2l_data=v2l_data)
        if not player:
            return {"error": "extract failed", "status": "error"}
        if player.get("v2l_status", "N/A") == "N/A" and combo_v2l:
            player["v2l_status"] = combo_v2l
        return {"status": "success", "player_data": player}
    except Exception as e:
        return {"error": f"critical: {type(e).__name__}: {e}", "status": "error"}


# ── CLI ────────────────────────────────────────────────────────────────
def _cli() -> int:
    print(f"{Fore.MAGENTA}🥔 Potato MLBB Engine — CLI{Style.RESET_ALL}")
    print(" 1. Single ban check (device_id)")
    print(" 2. Player info (role_id zone_id)")
    print(" 0. Exit")
    try:
        choice = input(f"{Fore.CYAN}> {Style.RESET_ALL}").strip()
    except (EOFError, KeyboardInterrupt):
        return 0
    if choice == "1":
        dev = input("device_id: ").strip()
        if dev:
            check_device_ban(dev)
    elif choice == "2":
        try:
            rid = int(input("role_id: ").strip())
            zid = int(input("zone_id: ").strip())
        except ValueError:
            print(f"{Fore.RED}bad ints{Style.RESET_ALL}")
            return 1
        res = lookup_player_data(rid, zid)
        if res.get("status") == "success":
            for k, v in res["player_data"].items():
                print(f"  {k}: {v}")
        else:
            print(f"{Fore.RED}{res.get('error')}{Style.RESET_ALL}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())