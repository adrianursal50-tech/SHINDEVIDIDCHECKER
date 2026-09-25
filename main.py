#!/usr/bin/env python3
"""
mlbb_bot.py — Telegram MLBB device checker.

Pipeline per device ID:
  1. Login handshake → extract identity
  2. Scan every response for ban markers
  3. Banned → ban card + full profile (if info stage succeeds)
  4. Clean  → full profile

Features:
  • Retry layer (capped backoff)
  • Banned devices still get profile fetched
  • Key system + admin panel
  • Per-user result files: /stop (hard socket kill) + /results + auto-send
  • Bulk runs on a background thread so /stop lands within ~1s

Env:
  MLBB_BOT_TOKEN   required
  MLBB_ADMINS      comma-separated telegram user IDs
  MLBB_WORKERS     default 8
  MLBB_RETRIES     default 4
  MLBB_FREE        "1" → no key required
"""

import os, sys, time, json, random, socket, struct, zlib
import threading, datetime, traceback, secrets
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum
from typing import Any, Tuple, Optional, Dict, List

import requests, urllib3
import zstandard as zstd
from Crypto.Cipher import AES

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── CONFIG ──────────────────────────────────────────────────────────
BOT_TOKEN  = os.environ.get("MLBB_BOT_TOKEN", "8702549007:AAGNiI_-iZyzzKE4OHafSwgl3SjR0Ht3VRM")
WORKERS    = int(os.environ.get("MLBB_WORKERS", "8"))
MAX_ATTEMPTS = int(os.environ.get("MLBB_RETRIES", "4"))
BASE_BACKOFF = 0.6
SOCK_TIMEOUT = 8
API        = "https://api.telegram.org/bot" + BOT_TOKEN

ADMIN_IDS: set = set()
for _x in os.environ.get("MLBB_ADMINS", "8621676055").split(","):
    _x = _x.strip()
    if _x.isdigit():
        ADMIN_IDS.add(int(_x))

FREE_MODE = os.environ.get("MLBB_FREE", "0") == "1"

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR  = os.path.join(BASE_DIR, "bot_results")
os.makedirs(RESULTS_DIR, exist_ok=True)
BAN_FILE     = os.path.join(RESULTS_DIR, "bans.txt")
CLEAN_FILE   = os.path.join(RESULTS_DIR, "clean.txt")
UNKNOWN_FILE = os.path.join(RESULTS_DIR, "unknown.txt")

SESSIONS_DIR = os.path.join(RESULTS_DIR, "sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

KEYS_FILE  = os.path.join(BASE_DIR, "keys.json")
USERS_FILE = os.path.join(BASE_DIR, "users.json")
STATS_FILE = os.path.join(BASE_DIR, "stats.json")

CLIENT_VERSION = "2.2.16.1232.1"
CHANNEL        = "and_usa"

# ── PER-USER STATE ──────────────────────────────────────────────────
_user_last_session: Dict[int, str] = {}
_user_last_session_lock = threading.Lock()

_uid_events: Dict[int, threading.Event] = {}
_uid_events_lock = threading.Lock()

_uid_connections: Dict[int, set] = {}
_uid_connections_lock = threading.Lock()

def _register_conn(uid: int, conn):
    with _uid_connections_lock:
        _uid_connections.setdefault(uid, set()).add(conn)

def _unregister_conn(uid: int, conn):
    with _uid_connections_lock:
        s = _uid_connections.get(uid)
        if s is not None:
            s.discard(conn)
            if not s:
                _uid_connections.pop(uid, None)

def _get_or_make_event(uid: int) -> threading.Event:
    with _uid_events_lock:
        ev = _uid_events.get(uid)
        if ev is None:
            ev = threading.Event()
            _uid_events[uid] = ev
        return ev

def _abort_uid(uid: int) -> int:
    with _uid_events_lock:
        ev = _uid_events.get(uid)
    if ev is not None:
        ev.set()
    with _uid_connections_lock:
        conns = list(_uid_connections.get(uid, ()))
    killed = 0
    for c in conns:
        try:
            s = getattr(c, "socket", None)
            if s is not None:
                try: s.shutdown(socket.SHUT_RDWR)
                except Exception: pass
                try: s.close()
                except Exception: pass
                killed += 1
        except Exception:
            pass
    return killed

def _clear_uid(uid: int):
    with _uid_events_lock:
        _uid_events.pop(uid, None)
    with _uid_connections_lock:
        _uid_connections.pop(uid, None)

# ── STORAGE ─────────────────────────────────────────────────────────
_store_lock = threading.Lock()

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

def load_keys() -> Dict:
    with _store_lock:
        return _load_json(KEYS_FILE, {}).get("KEYS", {})

def save_keys(keys: Dict):
    with _store_lock:
        _save_json(KEYS_FILE, {"KEYS": keys})

def load_users() -> Dict:
    with _store_lock:
        return _load_json(USERS_FILE, {}).get("USERS", {})

def save_users(users: Dict):
    with _store_lock:
        _save_json(USERS_FILE, {"USERS": users})

def load_stats() -> Dict:
    with _store_lock:
        return _load_json(STATS_FILE, {
            "total_checks": 0, "banned": 0, "clean": 0, "unknown": 0,
            "started": int(time.time()),
        })

def bump_stats(banned=0, clean=0, unknown=0):
    with _store_lock:
        s = _load_json(STATS_FILE, {
            "total_checks": 0, "banned": 0, "clean": 0, "unknown": 0,
            "started": int(time.time()),
        })
        s["total_checks"] += banned + clean + unknown
        s["banned"]  += banned
        s["clean"]   += clean
        s["unknown"] += unknown
        _save_json(STATS_FILE, s)

# ── KEYS ────────────────────────────────────────────────────────────
def gen_key(uses: int, note: str = "", bound_to: Optional[int] = None) -> str:
    keys = load_keys()
    for _ in range(20):
        k = "SPUD-" + secrets.token_hex(4).upper()
        if k not in keys:
            keys[k] = {"uses": uses, "used": 0, "note": note,
                       "created": int(time.time()), "bound_to": bound_to}
            save_keys(keys)
            return k
    raise RuntimeError("Could not generate unique key")

def get_key(k: str) -> Optional[Dict]:
    return load_keys().get(k)

def revoke_key(k: str) -> bool:
    keys = load_keys()
    if k in keys:
        del keys[k]; save_keys(keys); return True
    return False

# ── USERS ───────────────────────────────────────────────────────────
def get_user(tg_id: int) -> Optional[Dict]:
    return load_users().get(str(tg_id))

def upsert_user(tg_id: int, **fields):
    users = load_users()
    u = users.get(str(tg_id), {"key": None, "checks": 0, "total_used": 0,
                               "joined": int(time.time())})
    u.update(fields)
    users[str(tg_id)] = u
    save_users(users)

def user_consume(tg_id: int, n: int = 1) -> bool:
    if tg_id in ADMIN_IDS or FREE_MODE:
        return True
    users = load_users()
    u = users.get(str(tg_id))
    if not u: return False
    if u.get("checks", 0) == -1:
        u["total_used"] = u.get("total_used", 0) + n
        save_users(users); return True
    if u.get("checks", 0) < n:
        return False
    u["checks"] -= n
    u["total_used"] = u.get("total_used", 0) + n
    save_users(users); return True

def redeem_key(tg_id: int, k: str) -> Tuple[bool, str]:
    keys = load_keys()
    if k not in keys:
        return False, "Key not found."
    entry = keys[k]
    if entry.get("bound_to") and entry["bound_to"] != tg_id:
        return False, "Key already bound to another user."
    remaining = entry["uses"]
    entry["bound_to"] = tg_id
    save_keys(keys)
    users = load_users()
    u = users.get(str(tg_id), {"key": None, "checks": 0, "total_used": 0,
                               "joined": int(time.time())})
    if remaining == -1:
        u["checks"] = -1
    else:
        cur = u.get("checks", 0)
        u["checks"] = remaining if cur in (0, -1) else cur + remaining
    u["key"] = k
    users[str(tg_id)] = u
    save_users(users)
    keys = load_keys()
    if k in keys:
        keys[k]["uses"] = 0
        save_keys(keys)
    label = "unlimited" if remaining == -1 else f"{remaining} checks"
    return True, f"✅ Bound `{k}` → {label}."

# ── SDP ─────────────────────────────────────────────────────────────
class SdpDataType(Enum):
    INTEGER_POSITIVE = 0; INTEGER_NEGATIVE = 1; FLOAT = 2; DOUBLE = 3
    STRING = 4; LIST = 5; DICT = 6; STRUCT_BEGIN = 7; STRUCT_END = 8

class SdpException(Exception):
    pass

class SdpStruct(dict):
    def __init__(self, data=None):
        super().__init__(); self.data = b""; self.offset = 0
        if isinstance(data, bytes):
            self.data = data; self._unpack_from_binary()
        elif data is not None:
            super().update(data); self._pack_to_binary()

    def _pack_to_binary(self):
        self.data = bytes([SdpDataType.STRUCT_BEGIN.value << 4])
        for tag, value in sorted(self.items()):
            self._pack(tag, value)
        self.data += bytes([SdpDataType.STRUCT_END.value << 4])

    def _unpack_from_binary(self):
        if not self.data: return
        if self.data[0] >> 4 == SdpDataType.STRUCT_BEGIN.value:
            self.offset = 1
        while self.offset < len(self.data):
            tag, value = self._unpack()
            if isinstance(value, SdpDataType) and value == SdpDataType.STRUCT_END:
                break
            self[tag] = value

    def _write_number(self, value: int) -> bytes:
        r = bytearray()
        while value >= 0x80:
            r.append((value & 0x7F) | 0x80); value >>= 7
        r.append(value & 0x7F)
        return bytes(r)

    def _read_number(self) -> int:
        n = 1
        val = self.data[self.offset] & 0x7F
        while self.data[self.offset + n - 1] >= 0x80:
            val |= (self.data[self.offset + n] & 0x7F) << (7 * n); n += 1
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
            self.data += self._write_number(len(packed)); self.data += packed
        elif isinstance(value, (str, bytes)):
            self._pack_header(tag, SdpDataType.STRING)
            enc = value.encode("utf-8") if isinstance(value, str) else value
            self.data += self._write_number(len(enc)); self.data += enc
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
                    self._pack(0, k); self._pack(0, v)
        else:
            raise SdpException("Unsupported type")

    def _unpack(self) -> Tuple[int, Any]:
        try:
            if self.offset >= len(self.data):
                return 0, None
            h = self.data[self.offset]
            tag = h & 0xF
            dt = SdpDataType(h >> 4)
            self.offset += 1
            if tag == 15:
                tag = self._read_number()
            if dt == SdpDataType.INTEGER_POSITIVE: return tag, self._read_number()
            elif dt == SdpDataType.INTEGER_NEGATIVE: return tag, -self._read_number()
            elif dt == SdpDataType.FLOAT:
                v = self._read_number().to_bytes(4, "little")
                return tag, struct.unpack("<f", v)[0]
            elif dt == SdpDataType.DOUBLE:
                v = self._read_number().to_bytes(8, "little")
                return tag, struct.unpack("<d", v)[0]
            elif dt == SdpDataType.STRING:
                n = self._read_number()
                try: v = self.data[self.offset:self.offset+n].decode("utf-8")
                except UnicodeDecodeError: v = self.data[self.offset:self.offset+n]
                self.offset += n; return tag, v
            elif dt == SdpDataType.LIST:
                n = self._read_number(); v = []
                for _ in range(n):
                    _, item = self._unpack(); v.append(item)
                return tag, v
            elif dt == SdpDataType.DICT:
                n = self._read_number(); v = {}
                for _ in range(n):
                    _, k = self._unpack(); _, vv = self._unpack(); v[k] = vv
                return tag, v
            elif dt == SdpDataType.STRUCT_BEGIN:
                sd = {}
                while True:
                    st, sv = self._unpack()
                    if isinstance(sv, SdpDataType) and sv == SdpDataType.STRUCT_END:
                        break
                    sd[st] = sv
                return tag, SdpStruct(sd)
            elif dt == SdpDataType.STRUCT_END:
                return tag, SdpDataType.STRUCT_END
            raise SdpException("Unknown data type")
        except Exception:
            raise SdpException("Unpack error")

AES_KEY = bytes.fromhex("f5a193d50ade553e9835595f5cd75ddd")
AES_IV  = b"\x00" * 16

# ── MLBB CONNECTION ─────────────────────────────────────────────────
class MLBBConnection:
    def __init__(self, device_id: str, uid: Optional[int] = None,
                 cancel_event: Optional[threading.Event] = None):
        self.host = "login.ml.youngjoygame.com"
        self.port = 30021
        self.sequence = 1
        self.socket: Optional[socket.socket] = None
        self.queue_data = b""
        self.device_id = device_id
        self.uid = uid
        self.cancel_event = cancel_event
        self.cancelled = False

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

    def _is_cancelled(self) -> bool:
        return self.cancel_event is not None and self.cancel_event.is_set()

    def connect(self, host=None, port=None):
        if self._is_cancelled():
            self.cancelled = True
            raise SdpException("cancelled")
        if host: self.host = host
        if port: self.port = port
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.settimeout(SOCK_TIMEOUT)
        self.socket.connect((self.host, self.port))
        self.queue_data = b""; self.sequence = 1
        if self.uid is not None:
            _register_conn(self.uid, self)

    def cleanup(self):
        if self.uid is not None:
            _unregister_conn(self.uid, self)
        if self.socket:
            try: self.socket.close()
            except Exception: pass
            self.socket = None

    def send_data(self, pkt_id: int, sdp: SdpStruct):
        if self._is_cancelled():
            self.cancelled = True
            raise SdpException("cancelled")
        packet = SdpStruct({0: pkt_id, 1: self.sequence, 5: sdp.data}).data
        buf = zstd.compress(packet)
        flags = (len(buf) + 4) | (16 << 24)
        buf = flags.to_bytes(4, "big") + buf
        self.socket.send(buf); self.sequence += 1

    def recv_data(self) -> Tuple[Optional[int], Any]:
        if self._is_cancelled():
            self.cancelled = True
            return None, None
        try:
            while len(self.queue_data) < 4:
                if self._is_cancelled():
                    self.cancelled = True
                    return None, None
                d = self.socket.recv(4096)
                if not d: return None, None
                self.queue_data += d
            flags = int.from_bytes(self.queue_data[:4], "big")
            size = flags & 0xFFFFFF
            ctype = flags >> 24
            self.last_header_size = size
            while len(self.queue_data) < size:
                if self._is_cancelled():
                    self.cancelled = True
                    return None, None
                d = self.socket.recv(4096)
                if not d: return None, None
                self.queue_data += d
            data = self.queue_data[4:size]
            self.queue_data = self.queue_data[size:]
            if ctype == 1: data = zlib.decompress(data)
            elif ctype == 16: data = zstd.decompress(data)
            elif ctype == 2:
                c = AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV)
                data = c.decrypt(data if len(data) % 16 == 0 else data[:-1]).rstrip(b"\x00")
            elif ctype == 3:
                c = AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV)
                data = c.decrypt(data if len(data) % 16 == 0 else data[:-1]).rstrip(b"\x00")
                data = zlib.decompress(data)
            elif ctype == 18:
                c = AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV)
                dec = c.decrypt(data if len(data) % 16 == 0 else data[:-1])
                data = zstd.decompress(dec.rstrip(b"\x00"))
            result = SdpStruct(data)
            pkt_id = result[0]
            if pkt_id is None: return None, None
            body = result.get(6) or result.get(5)
            if not body or not isinstance(body, bytes):
                return pkt_id, None
            return pkt_id, SdpStruct(body)
        except socket.timeout:
            return -1, None
        except Exception:
            if self._is_cancelled():
                self.cancelled = True
            return None, None

# ── BAN INSPECTION ──────────────────────────────────────────────────
BAN_REASON_MAP = {
    "21": "Using Plug-in Apps to Compromise Competitive Fairness",
}

def inspect_for_ban(payload: Any) -> Tuple[bool, Dict]:
    details: Dict[str, Any] = {}
    def scan(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "ban_reason":
                    code = str(v)
                    details["ban_code"] = code
                    details["reason_name"] = BAN_REASON_MAP.get(code, BAN_REASON_MAP["21"])
                elif isinstance(k, str) and "ban" in k.lower():
                    details[str(k)] = v
                if k in ("endtime_day","endtime_hour","endtime_min","endtime_sec"):
                    details[k] = v
                if isinstance(v, (dict, list)): scan(v)
        elif isinstance(obj, list):
            for item in obj: scan(item)
    if payload:
        scan(dict(payload) if isinstance(payload, dict) else payload)
    banned = details.get("endtime_day") is not None or "ban_reason" in details
    return banned, details

# ── STATIC MAPS ─────────────────────────────────────────────────────
HERO_ID_MAP = {
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

_SKIN_DB_PATH = os.path.join(BASE_DIR, "skin_db.json")
try:
    with open(_SKIN_DB_PATH, "r", encoding="utf-8") as _f:
        _raw = _f.read().strip()
        if _raw and not _raw.startswith("{"): _raw = "{" + _raw
        SKIN_DB = json.loads(_raw) if _raw else {}
except Exception:
    SKIN_DB = {}

SKIN_TIER_LABELS = {"common":"Common","exceptional":"Exceptional","deluxe":"Deluxe",
                    "exquisite":"Exquisite","grand":"Grand","supreme":"Supreme"}

def get_skin_tier(skin_id):
    t = SKIN_DB.get(str(skin_id))
    return SKIN_TIER_LABELS.get(t, t.capitalize()) if t else None

def map_collector_point(point: int) -> str:
    if point < 1000: return "No Tier"
    tiers = [
        (1000,4000,"Amateur Collector"),(4000,10000,"Junior Collector"),
        (10000,22000,"Seasoned Collector"),(22000,44000,"Expert Collector"),
        (44000,84000,"Renowned Collector"),(84000,160000,"Exalted Collector"),
        (160000,280000,"Mega Collector"),(280000,float("inf"),"World Collector"),
    ]
    for mn, mx, name in tiers:
        if mn <= point < mx:
            if name == "World Collector": return name
            per = (mx - mn) / 5
            lvl = int((point - mn) // per)
            return f"{name} {['V','IV','III','II','I'][lvl]}"
    return "Unknown"

def map_rank(p):
    RD = [(0,4,"Warrior III"),(5,9,"Warrior II"),(10,14,"Warrior I"),
          (15,19,"Elite IV"),(20,24,"Elite III"),(25,29,"Elite II"),(30,34,"Elite I"),
          (35,39,"Master IV"),(40,44,"Master III"),(45,49,"Master II"),(50,54,"Master I"),
          (55,59,"Grandmaster IV"),(60,64,"Grandmaster III"),(65,69,"Grandmaster II"),(70,74,"Grandmaster I"),
          (75,81,"Epic IV"),(82,88,"Epic III"),(89,95,"Epic II"),(96,107,"Epic I"),
          (108,114,"Legend IV"),(115,121,"Legend III"),(122,128,"Legend II"),(129,135,"Legend I")]
    for mn, mx, name in RD:
        if mn <= p <= mx: return name
    if 136 <= p <= 160: return f"Mythic {p - 135}"
    if 161 <= p <= 195: return f"Mythical Honor {p - 135}"
    if 196 <= p <= 235: return f"Mythical Glory {p - 157}"
    if 236 <= p <= 999: return f"Mythical Immortal {p - 157}"
    return "Unknown"

def parse_skin_counts(tag_118):
    if not tag_118 or not isinstance(tag_118, dict): return {}
    sd = None
    for k in (4, "4"):
        v = tag_118.get(k)
        if isinstance(v, dict) and v: sd = v; break
    if sd is None: sd = tag_118
    if not isinstance(sd, dict): return {}
    types = {6:"Supreme Skins",5:"Grand Skins",4:"Exquisite Skins",
             3:"Deluxe Skins",2:"Exceptional Skins",1:"Common Skins"}
    out = {}
    for sid, cnt in sd.items():
        try: si = int(sid)
        except (ValueError, TypeError): continue
        if si in types: out[types[si]] = cnt
    return out

def format_timestamp(ts):
    try:
        utc = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        pht = utc + datetime.timedelta(hours=8)
        base = pht.strftime("%Y-%m-%d %H:%M")
        now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
        secs = int((pht - now).total_seconds())
        if secs >= 0:
            d,h,m = secs//86400,(secs%86400)//3600,(secs%3600)//60
            return f"{base} (in {d}d {h}h {m}m)"
        secs = abs(secs)
        d,h,m = secs//86400,(secs%86400)//3600,(secs%3600)//60
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
    if not isinstance(tag_91, list): return []
    return [HERO_ID_MAP.get(h, f"Unknown({h})") for h in reversed(tag_91)]

# ── PLAYER EXTRACTION ───────────────────────────────────────────────
def extract_player_data(result, role_info=None, creation_ts=0, v2l_data=None):
    if not result or not result.get(0) or len(result[0]) == 0: return None
    try:
        pd = result[0][0]
        nickname    = pd.get(2, "Unknown")
        player_id   = pd.get(0, "Unknown")
        server      = pd.get(1, "Unknown")
        level       = pd.get(3, "Unknown")
        skin_count  = pd.get(83, 0)
        hero_count  = pd.get(4, 0)
        matches     = pd.get(17, 0)
        rating_score = pd.get(9, 0)
        if role_info:
            hero_count = role_info.get(9, hero_count)
            matches    = role_info.get(22, matches)

        location = "NOT FOUND"
        loc = pd.get(71)
        if loc and isinstance(loc, list) and len(loc) >= 2:
            location = ", ".join(str(x) for x in loc)

        last_login = format_timestamp(pd.get(5, 0))
        last_login_country = pd.get(87, "Unknown")
        create_country = pd.get(97, "Unknown")

        squad_icon = pd.get(31, "")
        squad_name = (pd.get(30, "") or "").replace("`", "").strip()
        squad = f"{squad_icon} {squad_name}".strip() if squad_name else "—"

        tag_95 = pd.get(95); tag_8 = pd.get(8)
        high_rank = map_rank(tag_95) if tag_95 is not None else "Unknown"
        current_rank = map_rank(tag_8) if tag_8 is not None else "Unknown"
        achievement_points = pd.get(7, 0)

        tag_136 = pd.get(136, {})
        collector_point = tag_136.get(9, 0) if isinstance(tag_136, dict) else 0
        collector_tier  = map_collector_point(collector_point)

        tag_91 = pd.get(91, [])
        hero_history = get_hero_history(tag_91) if tag_91 else []

        v2l_status = "N/A"
        if v2l_data and isinstance(v2l_data, dict):
            src = v2l_data.get("_source", 0)
            d = v2l_data.get("_data", {}) or {}
            for t in ((10, 11) if src == 10208 else (0, 2, 3, 5)):
                v = d.get(t)
                if v is not None:
                    try:
                        v2l_status = "Enabled" if int(v) > 0 else "Disabled"; break
                    except (ValueError, TypeError):
                        pass

        followers = 0
        if role_info: followers = role_info.get(23, 0)
        if not followers: followers = pd.get(15, 0)

        popularity = pd.get(14, 0)
        bio = pd.get(24, "")
        bio = bio.strip() if isinstance(bio, str) else ""

        likes = 0
        if role_info: likes = role_info.get(24, 0)
        if not likes: likes = pd.get(61, 0)

        credits_score = None
        if role_info:
            cs = role_info.get(20, 0)
            if isinstance(cs, int) and cs > 0: credits_score = f"{cs}/110"

        restriction_flags = "None"
        _t117 = (role_info or {}).get(117) if role_info else None
        if _t117 is None: _t117 = pd.get(117)
        if _t117 is not None:
            raw = _t117.get(0, 0) if isinstance(_t117, dict) else int(_t117 or 0)
            pct = round(((int(raw) + 1) / 7) * 100, 1)
            risk = "Low" if pct < 30 else ("Medium" if pct < 60 else "High")
            restriction_flags = f"{pct}% ({risk} Risk)"

        _t135 = pd.get(135, {})
        _affl = _t135.get(1, 0) if isinstance(_t135, dict) else 0
        _affmap = {0:"None",1:"Bronze",2:"Silver",3:"Gold",4:"Platinum",5:"Diamond"}
        affinity = _affmap.get(_affl, f"Level {_affl}")

        _skin_ts = pd.get(176, 0)
        latest_skin_date = format_timestamp(_skin_ts) if _skin_ts else "N/A"
        _skin_id = pd.get(175, 0)
        latest_skin_tier = get_skin_tier(_skin_id) if _skin_id else "N/A"

        _now = time.time(); _sl_expiry = 0
        for t in (21, 47, 50):
            for src in ((role_info or {}), pd):
                v = src.get(t, 0) or 0
                if isinstance(v, int) and v > 1700000000:
                    _sl_expiry = v; break
            if _sl_expiry: break
        if _sl_expiry:
            starlight_user = "Yes" if _sl_expiry > _now else "No"
            starlight_expiry = format_timestamp(_sl_expiry)
        else:
            starlight_user = "No"; starlight_expiry = "N/A"

        starlight_months = pd.get(60, 0)

        tickets = 0
        if role_info: tickets = role_info.get(49, 0)
        if not tickets: tickets = pd.get(49, 0)

        total_wins = pd.get(18, 0)
        _MIN_TS = 1451577600
        _create_fb = pd.get(6, 0)
        if creation_ts and creation_ts >= _MIN_TS:
            creation_date = format_timestamp_full(creation_ts)
        elif _create_fb and _create_fb >= _MIN_TS:
            creation_date = format_timestamp_full(_create_fb)
        else:
            creation_date = "N/A"

        account_age = "N/A"
        age_ts = (creation_ts if (creation_ts and creation_ts >= _MIN_TS)
                  else (_create_fb if (_create_fb and _create_fb >= _MIN_TS) else 0))
        if age_ts:
            now = datetime.datetime.now(datetime.timezone.utc)
            created = datetime.datetime.fromtimestamp(age_ts, datetime.timezone.utc)
            delta = now - created
            y, m, d = delta.days // 365, (delta.days % 365) // 30, delta.days % 30
            account_age = f"{y}y {m}m {d}d" if y else (f"{m}m {d}d" if m else f"{delta.days}d")

        mcl_wins = role_info.get(46, 0) if role_info else 0
        if not mcl_wins: mcl_wins = pd.get(104, pd.get(103, 0))

        win_count = role_info.get(22, 0) if role_info else 0
        total_battles = (role_info.get(77, 0) if role_info else 0) or pd.get(17, 0)
        wins_for_rate = win_count if win_count else total_wins
        if total_battles > 0 and wins_for_rate > 0:
            wr = (wins_for_rate / total_battles) * 100
            win_rate = f"{min(wr, 100):.1f}%"
        else:
            win_rate = "N/A"

        diamonds = 0; bp = 0
        if role_info:
            cur = role_info.get(111)
            if isinstance(cur, dict):
                diamonds = int(cur.get(0, 0) or 0); bp = int(cur.get(1, 0) or 0)
            elif isinstance(cur, int): diamonds = int(cur or 0)
        if not bp: bp = int(pd.get(83, 0) or 0)

        _dbuy = pd.get(42, 0)
        last_diamond = format_timestamp(_dbuy) if isinstance(_dbuy, int) and _dbuy > 1000000000 else "N/A"

        skin_breakdown = {"Supreme Skins":0,"Grand Skins":0,"Exquisite Skins":0,
                          "Deluxe Skins":0,"Exceptional Skins":0,"Common Skins":0}
        _t118 = (role_info or {}).get(118) or pd.get(118)
        if _t118: skin_breakdown.update(parse_skin_counts(_t118))

        return {
            "nickname":nickname,"player_id":player_id,"server":server,"level":level,
            "skin_count":skin_count,"hero_count":hero_count,"matches":matches,
            "rating_score":rating_score,"location":location,"last_login":last_login,
            "last_login_country":last_login_country,"create_account_country":create_country,
            "high_rank":high_rank,"current_rank":current_rank,
            "achievement_points":achievement_points,"collector_point":collector_point,
            "collector_tier":collector_tier,"hero_history":hero_history,"squad":squad,
            "skin_breakdown":skin_breakdown,"affinity":affinity,"likes":likes,
            "credits_score":credits_score,"followers":followers,"popularity":popularity,
            "bio":bio or None,"latest_skin_date":latest_skin_date,
            "starlight_user":starlight_user,"starlight_expiry":starlight_expiry,
            "starlight_months":starlight_months or None,"tickets":tickets or None,
            "total_wins":total_wins or None,"restriction_flags":restriction_flags,
            "mcl_champion_wins":mcl_wins,"v2l_status":v2l_status,"creation_date":creation_date,
            "account_age":account_age,"win_count":win_count,"total_battles":total_battles,
            "win_rate":win_rate,"battle_points":bp or None,"diamonds":diamonds or None,
            "last_diamond_purchase":last_diamond if last_diamond != "N/A" else None,
        }
    except Exception:
        return None

# ── PIPELINE ────────────────────────────────────────────────────────
def _backoff(attempt: int, cancel_event: Optional[threading.Event] = None):
    total = BASE_BACKOFF * (2 ** attempt) + random.uniform(0, 0.4)
    if cancel_event is not None:
        cancel_event.wait(total)
    else:
        time.sleep(total)

def _check_ban_once(device_id: str, uid: Optional[int] = None,
                    cancel_event: Optional[threading.Event] = None) -> Dict[str, Any]:
    out = {"banned":False,"ban_info":{},"account_id":0,"zone_id":0,
           "session_key":"","creation_ts":0,"reason":"ok","cancelled":False}
    conn = MLBBConnection(device_id, uid=uid, cancel_event=cancel_event)
    try:
        conn.connect("login.ml.youngjoygame.com", 30021)
        conn.send_data(1, SdpStruct({
            0: conn.device_id,
            1: f"gps_adid={conn.advertising_id}&android_id={conn.android_id}&device_unique_id={conn.imei_md5}",
            2: CLIENT_VERSION, 3: CHANNEL, 4: "en",
        }))
        pid, res = conn.recv_data()

        if conn.cancelled:
            out["cancelled"] = True; out["reason"] = "cancelled"; return out

        if res:
            try: out["account_id"] = int(res.get(0) or 0)
            except Exception: pass
            if res.get(1) is not None: out["session_key"] = res.get(1, "")
            try: out["creation_ts"] = int(res.get(19) or 0)
            except Exception: pass
            z = res.get(2)
            if isinstance(z, dict):
                try: out["zone_id"] = int(z.get(0, 0) or 0)
                except Exception: pass
            elif isinstance(z, list) and z:
                first = z[0]
                try:
                    out["zone_id"] = int((first.get(0, 0) if isinstance(first, dict) else first) or 0)
                except Exception: pass
            elif z is not None:
                try: out["zone_id"] = int(z or 0)
                except Exception: pass

        b, info = inspect_for_ban(res)
        if b:
            out["banned"], out["ban_info"] = True, info
            return out
        if pid != 2 or not res:
            out["reason"] = "login_no_response"; return out
        if not out["account_id"] or not out["zone_id"]:
            out["reason"] = "login_no_response"; return out

        conn.send_data(5, SdpStruct({
            0: out["account_id"], 1: out["session_key"], 2: CLIENT_VERSION,
            5: out["zone_id"], 6: CHANNEL,
        }))
        pid, res = conn.recv_data()
        if conn.cancelled:
            out["cancelled"] = True; out["reason"] = "cancelled"; return out
        b, info = inspect_for_ban(res)
        if b: out["banned"], out["ban_info"] = True, info; return out
        if pid != 6 or not res: out["reason"] = "server_select_fail"; return out

        host, port = res[1].split(":")
        conn.cleanup(); conn.connect(host, int(port))
        conn.send_data(10001, SdpStruct({
            0: out["account_id"], 1: out["session_key"], 2: out["zone_id"],
            4: CLIENT_VERSION, 13: CHANNEL, 15: conn.device_id,
        }))
        conn.send_data(10101, SdpStruct({0: 0, 2: 2}))

        role_req = False; handshake_ok = False
        deadline = time.time() + 20
        while time.time() < deadline:
            if conn.cancelled:
                out["cancelled"] = True; out["reason"] = "cancelled"; return out
            pid, res = conn.recv_data()
            if conn.cancelled:
                out["cancelled"] = True; out["reason"] = "cancelled"; return out
            b, info = inspect_for_ban(res)
            if b: out["banned"], out["ban_info"] = True, info; return out
            if pid is None or pid == -1: break
            if pid == 10002 and not role_req:
                conn.send_data(10003, SdpStruct({
                    0: out["account_id"], 1: out["session_key"], 2: out["zone_id"],
                    3: CLIENT_VERSION, 4: CHANNEL, 5: conn.device_id,
                }))
                role_req = True
            elif pid in (10004, 10008):
                handshake_ok = True; break
        if not handshake_ok and not out["banned"] and not out["cancelled"]:
            out["reason"] = "handshake_fail"
    except SdpException:
        if conn.cancelled:
            out["cancelled"] = True; out["reason"] = "cancelled"
        else:
            out["reason"] = "unexpected"
    except socket.timeout:
        out["reason"] = "socket"
    except Exception:
        out["reason"] = "unexpected"
    finally:
        conn.cleanup()
    return out

def check_ban_only(device_id: str, attempts: int = MAX_ATTEMPTS,
                   uid: Optional[int] = None,
                   cancel_event: Optional[threading.Event] = None) -> Dict[str, Any]:
    last = None
    for i in range(attempts):
        if cancel_event is not None and cancel_event.is_set():
            return {"banned":False,"ban_info":{},"account_id":0,"zone_id":0,
                    "session_key":"","creation_ts":0,"reason":"cancelled","cancelled":True}
        last = _check_ban_once(device_id, uid=uid, cancel_event=cancel_event)
        if last.get("cancelled"): return last
        if last["banned"]: return last
        if last.get("account_id") and last.get("zone_id") and last["reason"] == "ok":
            return last
        if i < attempts - 1:
            _backoff(i, cancel_event)
    return last or {"banned":False,"ban_info":{},"account_id":0,"zone_id":0,
                    "session_key":"","creation_ts":0,"reason":"unexpected","cancelled":False}

def _fetch_player_info_once(device_id: str, role_id: int, zone_id: int,
                            creation_ts: int = 0,
                            uid: Optional[int] = None,
                            cancel_event: Optional[threading.Event] = None) -> Optional[Dict]:
    if cancel_event is not None and cancel_event.is_set():
        return None
    conn = MLBBConnection(device_id, uid=uid, cancel_event=cancel_event)
    try:
        conn.connect("login.ml.youngjoygame.com", 30021)
        conn.send_data(1, SdpStruct({
            0: conn.device_id,
            1: f"gps_adid={conn.advertising_id}&android_id={conn.android_id}&device_unique_id={conn.imei_md5}",
            2: CLIENT_VERSION, 3: CHANNEL, 4: "en",
        }))
        pid, res = conn.recv_data()
        if conn.cancelled: return None
        if pid != 2 or not res: return None
        acc_id = res.get(0); sess = res.get(1)
        z = res.get(2)
        if isinstance(z, dict): my_zone = int(z.get(0, 0) or 0)
        elif isinstance(z, list) and z:
            first = z[0]
            my_zone = int((first.get(0, 0) if isinstance(first, dict) else first) or 0)
        else: my_zone = int(z or 0)
        if not creation_ts: creation_ts = res.get(19, 0) or 0

        conn.send_data(5, SdpStruct({
            0: acc_id, 1: sess, 2: CLIENT_VERSION, 5: my_zone, 6: CHANNEL,
        }))
        pid, res = conn.recv_data()
        if conn.cancelled: return None
        if pid != 6 or not res: return None
        host, port = res[1].split(":")
        conn.cleanup(); conn.connect(host, int(port))
        conn.send_data(10001, SdpStruct({
            0: acc_id, 1: sess, 2: my_zone, 4: CLIENT_VERSION,
            13: CHANNEL, 15: conn.device_id,
        }))
        conn.send_data(10101, SdpStruct({0: 0, 2: 2}))

        got = False; deadline = time.time() + 15
        while time.time() < deadline:
            if conn.cancelled: return None
            pid, res = conn.recv_data()
            if conn.cancelled: return None
            if pid == 10002: got = True; break
            if pid is None or pid == -1: return None
        if not got: return None

        lookup_res = None
        conn.send_data(11153, SdpStruct({1: int(role_id)}))
        deadline = time.time() + 15
        while time.time() < deadline:
            if conn.cancelled: return None
            pid, res = conn.recv_data()
            if conn.cancelled: return None
            if pid is None or pid == -1: break
            if pid == 11154: lookup_res = res; break
        if not lookup_res: return None

        merged = {}
        try:
            conn.send_data(10143, SdpStruct({0: int(role_id), 1: int(zone_id)}))
            dl = time.time() + 8
            while time.time() < dl:
                if conn.cancelled: return None
                pid, res = conn.recv_data()
                if conn.cancelled: return None
                if pid is None or pid == -1: break
                if pid == 10144 and res:
                    for k, v in res.items(): merged[k] = v
                    break
        except Exception: pass

        v2l_data = None
        try:
            conn.send_data(10208, SdpStruct({0: int(role_id), 1: int(zone_id)}))
            dl = time.time() + 6
            while time.time() < dl:
                if conn.cancelled: return None
                pid, res = conn.recv_data()
                if conn.cancelled: return None
                if pid is None or pid == -1: break
                if pid == 10208 and res:
                    v2l_data = {"_source": 10208, "_data": dict(res)}; break
        except Exception: pass

        return extract_player_data(lookup_res, role_info=merged or None,
                                   creation_ts=creation_ts, v2l_data=v2l_data)
    except Exception:
        return None
    finally:
        conn.cleanup()

def fetch_player_info(device_id: str, role_id: int, zone_id: int,
                      creation_ts: int = 0, attempts: int = MAX_ATTEMPTS,
                      uid: Optional[int] = None,
                      cancel_event: Optional[threading.Event] = None) -> Optional[Dict]:
    for i in range(attempts):
        if cancel_event is not None and cancel_event.is_set():
            return None
        p = _fetch_player_info_once(device_id, role_id, zone_id, creation_ts,
                                    uid=uid, cancel_event=cancel_event)
        if p: return p
        if i < attempts - 1:
            _backoff(i, cancel_event)
    return None

def process_device(device_id: str, uid: Optional[int] = None,
                   cancel_event: Optional[threading.Event] = None) -> Dict[str, Any]:
    device_id = device_id.strip()
    if not device_id:
        return {"status":"error","reason":"empty device id","device_id":device_id}

    if cancel_event is not None and cancel_event.is_set():
        return {"status":"cancelled","device_id":device_id}

    ban = check_ban_only(device_id, uid=uid, cancel_event=cancel_event)
    if ban.get("cancelled"):
        return {"status":"cancelled","device_id":device_id}

    acc_id      = ban.get("account_id") or 0
    zone_id     = ban.get("zone_id") or 0
    creation_ts = ban.get("creation_ts", 0)

    player = None
    if acc_id and zone_id:
        player = fetch_player_info(device_id, acc_id, zone_id,
                                   creation_ts=creation_ts,
                                   uid=uid, cancel_event=cancel_event)
        if cancel_event is not None and cancel_event.is_set():
            return {"status":"cancelled","device_id":device_id}

    if ban["banned"]:
        r = {"status":"banned","device_id":device_id,"ban_info":ban["ban_info"]}
        if acc_id:  r["account_id"]  = acc_id
        if zone_id: r["zone_id"]     = zone_id
        if player:  r["player"]      = player
        return r

    if not acc_id or not zone_id:
        return {"status":"unknown","device_id":device_id,
                "reason": f"login failed after {MAX_ATTEMPTS}x: {ban.get('reason','?')}"}

    if not player:
        return {"status":"unknown","device_id":device_id,
                "account_id":acc_id,"zone_id":zone_id,
                "reason": f"info failed after {MAX_ATTEMPTS}x (login OK)"}

    return {"status":"clean","device_id":device_id,
            "account_id":acc_id,"zone_id":zone_id,"player":player}

# ── FORMATTING ──────────────────────────────────────────────────────
def _esc(s) -> str:
    return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def _profile_block(p: Dict, result: Dict, header: str = "<b>— Profile —</b>") -> List[str]:
    bd = p.get("skin_breakdown", {})
    lines = [
        "", header,
        f"<b>Name:</b> {_esc(p.get('nickname'))}",
        f"<b>Account ID:</b> <code>{_esc(result.get('account_id') or p.get('player_id'))}</code>   "
        f"<b>Zone:</b> <code>{_esc(result.get('zone_id'))}</code>",
        f"<b>Level:</b> {_esc(p.get('level'))}   <b>Country:</b> {_esc(p.get('create_account_country'))}",
        f"<b>Age:</b> {_esc(p.get('account_age'))}   <b>Created:</b> {_esc(p.get('creation_date'))}",
        "",
        f"<b>Current Rank:</b> {_esc(p.get('current_rank'))}",
        f"<b>Highest Rank:</b> {_esc(p.get('high_rank'))}",
        f"<b>Win Rate:</b> {_esc(p.get('win_rate'))}   <b>Battles:</b> {_esc(p.get('total_battles'))}",
        "",
        f"<b>Heroes:</b> {_esc(p.get('hero_count'))}   <b>Skins:</b> {_esc(p.get('skin_count'))}",
        f"<b>Collector:</b> {_esc(p.get('collector_tier'))} ({_esc(p.get('collector_point'))} pts)",
        f"<b>Followers:</b> {_esc(p.get('followers'))}   <b>Likes:</b> {_esc(p.get('likes'))}",
        f"<b>Restriction:</b> {_esc(p.get('restriction_flags'))}",
        f"<b>V2L:</b> {_esc(p.get('v2l_status'))}   <b>Starlight:</b> {_esc(p.get('starlight_user'))}",
        "",
        "<b>Skin Breakdown</b>",
        f"  Supreme    : {bd.get('Supreme Skins',0)}",
        f"  Grand      : {bd.get('Grand Skins',0)}",
        f"  Exquisite  : {bd.get('Exquisite Skins',0)}",
        f"  Deluxe     : {bd.get('Deluxe Skins',0)}",
        f"  Exceptional: {bd.get('Exceptional Skins',0)}",
        f"  Common     : {bd.get('Common Skins',0)}",
        "",
        f"<b>Last Login:</b> {_esc(p.get('last_login'))}",
        f"<b>Location:</b> {_esc(p.get('location'))}",
    ]
    heroes = p.get("hero_history") or []
    if heroes:
        lines.append(f"<b>Recent Heroes:</b> {_esc(', '.join(heroes[:10]))}")
    if p.get("bio"):
        lines.append(f"<b>Bio:</b> {_esc(p['bio'])}")
    return lines

def format_ban_reply(result: Dict) -> str:
    info = result.get("ban_info", {})
    reason = info.get("reason_name", "Using Plug-in Apps to Compromise Competitive Fairness")
    code   = info.get("ban_code", "?")
    d = info.get("endtime_day","?"); h = info.get("endtime_hour","00")
    m = info.get("endtime_min","00"); s = info.get("endtime_sec","00")
    lines = [
        "<b>🚫 BANNED DEVICE</b>",
        f"<code>{_esc(result['device_id'])}</code>", "",
        f"<b>Reason:</b> {_esc(reason)}",
        f"<b>Code:</b> <code>{_esc(code)}</code>",
        f"<b>Duration:</b> Day {_esc(d)}, {_esc(h)}:{_esc(m)}:{_esc(s)}",
    ]
    p = result.get("player")
    if p:
        lines += _profile_block(p, result)
    else:
        lines += ["", "<i>Profile unavailable — info stage handshake failed.</i>"]
    return "\n".join(lines)

def format_clean_reply(result: Dict) -> str:
    p = result["player"]
    lines = ["<b>✅ CLEAN — PLAYER PROFILE</b>",
             f"<code>{_esc(result['device_id'])}</code>"]
    lines += _profile_block(p, result, header="")
    return "\n".join(lines)

def format_error_reply(result: Dict) -> str:
    return (f"<b>⚠️ LOOKUP FAILED</b>\n"
            f"<code>{_esc(result.get('device_id','?'))}</code>\n"
            f"Reason: {_esc(result.get('reason','unknown'))}")

# ── TELEGRAM ────────────────────────────────────────────────────────
def tg_api(method: str, **kwargs):
    try:
        r = requests.post(f"{API}/{method}", json=kwargs, timeout=40)
        return r.json()
    except Exception:
        return {"ok": False}

def tg_send(chat_id, text, reply_to=None, parse_mode="HTML"):
    p = {"chat_id": chat_id, "text": text[:4096],
         "parse_mode": parse_mode, "disable_web_page_preview": True}
    if reply_to: p["reply_to_message_id"] = reply_to
    return tg_api("sendMessage", **p)

def tg_edit(chat_id, msg_id, text, parse_mode="HTML"):
    return tg_api("editMessageText", chat_id=chat_id, message_id=msg_id,
                  text=text[:4096], parse_mode=parse_mode,
                  disable_web_page_preview=True)

def tg_get_file(file_id):
    r = tg_api("getFile", file_id=file_id)
    if not r.get("ok"): return None
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{r['result']['file_path']}"

def tg_download(url) -> Optional[bytes]:
    try:
        r = requests.get(url, timeout=60)
        return r.content if r.status_code == 200 else None
    except Exception:
        return None

def tg_send_document(chat_id, filepath, caption=None, reply_to=None):
    if not os.path.exists(filepath):
        return {"ok": False, "desc": "file missing"}
    try:
        with open(filepath, "rb") as fh:
            files = {"document": (os.path.basename(filepath), fh)}
            data = {"chat_id": str(chat_id)}
            if caption:
                data["caption"] = caption[:1024]
                data["parse_mode"] = "HTML"
            if reply_to:
                data["reply_to_message_id"] = str(reply_to)
            r = requests.post(f"{API}/sendDocument",
                              data=data, files=files, timeout=120)
        return r.json()
    except Exception as e:
        return {"ok": False, "desc": str(e)}

# ── LOGGING ─────────────────────────────────────────────────────────
_log_lock = threading.Lock()
def append_line(path, line):
    with _log_lock:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception: pass

# ── MENU TEXT ───────────────────────────────────────────────────────
HELP_TEXT = (
    "<b>MLBB Device Checker Bot</b>\n\n"
    "<b>User commands</b>\n"
    "• <code>/check &lt;device_id&gt;</code> — one device\n"
    "• send a .txt (one per line) — bulk\n"
    "• <code>/stop</code> — hard-abort running bulk\n"
    "• <code>/results</code> — resend your last results file\n"
    "• <code>/redeem KEY</code> — activate a key\n"
    "• <code>/me</code> — your balance\n"
    "• <code>/whoami</code> — your telegram ID\n"
    "• <code>/help</code> — this menu\n\n"
    "Device IDs start with <code>and_</code> or <code>ios_</code>."
)

ADMIN_HELP = (
    "<b>🛠 Admin Panel</b>\n\n"
    "<b>Keys</b>\n"
    "• <code>/genkey &lt;uses&gt; [note]</code> — new key (-1 = unlimited)\n"
    "• <code>/keys</code> — list all keys\n"
    "• <code>/revoke &lt;key&gt;</code> — delete key\n\n"
    "<b>Users</b>\n"
    "• <code>/users</code> — list users\n"
    "• <code>/give &lt;tg_id&gt; &lt;n&gt;</code> — add credits\n"
    "• <code>/take &lt;tg_id&gt; &lt;n&gt;</code> — remove credits\n"
    "• <code>/unbind &lt;tg_id&gt;</code> — detach key from user\n\n"
    "<b>Ops</b>\n"
    "• <code>/stats</code> — global counters\n"
    "• <code>/broadcast &lt;msg&gt;</code> — message every known user\n"
    "• <code>/stop &lt;tg_id&gt;</code> — hard-abort another user's bulk\n"
    "• <code>/results &lt;tg_id&gt;</code> — resend another user's last file"
)

# ── ADMIN HANDLERS ──────────────────────────────────────────────────
def is_admin(uid): return uid in ADMIN_IDS

def cmd_genkey(chat_id, uid, args, reply_to):
    if not args:
        tg_send(chat_id, "Usage: <code>/genkey &lt;uses&gt; [note]</code>  (-1 = unlimited)",
                reply_to=reply_to); return
    try:
        uses = int(args[0])
    except ValueError:
        tg_send(chat_id, "First arg must be a number.", reply_to=reply_to); return
    note = " ".join(args[1:])[:80]
    k = gen_key(uses, note)
    label = "unlimited" if uses == -1 else f"{uses} checks"
    tg_send(chat_id,
            f"<b>🔑 New Key</b>\n<code>{k}</code>\n"
            f"Uses: {label}\nNote: {_esc(note or '—')}",
            reply_to=reply_to)

def cmd_keys(chat_id, reply_to):
    keys = load_keys()
    if not keys:
        tg_send(chat_id, "No keys.", reply_to=reply_to); return
    lines = [f"<b>🔑 Keys ({len(keys)})</b>\n"]
    for k, e in list(keys.items())[:60]:
        u = "∞" if e["uses"] == -1 else str(e["uses"])
        b = e.get("bound_to") or "—"
        lines.append(f"<code>{k}</code> | uses:{u} used:{e.get('used',0)} | by:{b}")
    if len(keys) > 60: lines.append(f"... +{len(keys)-60} more")
    tg_send(chat_id, "\n".join(lines), reply_to=reply_to)

def cmd_revoke(chat_id, args, reply_to):
    if not args:
        tg_send(chat_id, "Usage: <code>/revoke &lt;key&gt;</code>", reply_to=reply_to); return
    ok = revoke_key(args[0].strip().upper())
    tg_send(chat_id, "✅ revoked" if ok else "❌ not found", reply_to=reply_to)

def cmd_users(chat_id, reply_to):
    users = load_users()
    if not users:
        tg_send(chat_id, "No users yet.", reply_to=reply_to); return
    lines = [f"<b>👥 Users ({len(users)})</b>\n"]
    for uid, u in list(users.items())[:60]:
        bal = "∞" if u.get("checks") == -1 else str(u.get("checks", 0))
        lines.append(f"<code>{uid}</code> | bal:{bal} used:{u.get('total_used',0)}")
    if len(users) > 60: lines.append(f"... +{len(users)-60} more")
    tg_send(chat_id, "\n".join(lines), reply_to=reply_to)

def cmd_give(chat_id, args, reply_to, sign=1):
    if len(args) < 2:
        tg_send(chat_id, "Usage: <code>/give &lt;tg_id&gt; &lt;n&gt;</code>", reply_to=reply_to); return
    try:
        tid = int(args[0]); n = int(args[1])
    except ValueError:
        tg_send(chat_id, "Both must be numbers.", reply_to=reply_to); return
    users = load_users(); k = str(tid)
    if k not in users:
        tg_send(chat_id, "Unknown user.", reply_to=reply_to); return
    cur = users[k].get("checks", 0)
    if cur == -1 and sign > 0:
        tg_send(chat_id, "User is unlimited — nothing to add.", reply_to=reply_to); return
    users[k]["checks"] = max(0, cur + sign * n)
    save_users(users)
    tg_send(chat_id, f"✅ {tid} → {users[k]['checks']}", reply_to=reply_to)

def cmd_unbind(chat_id, args, reply_to):
    if not args:
        tg_send(chat_id, "Usage: <code>/unbind &lt;tg_id&gt;</code>", reply_to=reply_to); return
    try: tid = int(args[0])
    except ValueError:
        tg_send(chat_id, "tg_id must be numeric.", reply_to=reply_to); return
    users = load_users(); k = str(tid)
    if k not in users:
        tg_send(chat_id, "Unknown user.", reply_to=reply_to); return
    users[k]["key"] = None
    save_users(users)
    tg_send(chat_id, f"✅ unbind done for {tid}", reply_to=reply_to)

def cmd_stats(chat_id, reply_to):
    s = load_stats()
    up = int(time.time()) - s.get("started", int(time.time()))
    d, h = up // 86400, (up % 86400) // 3600
    tg_send(chat_id,
            f"<b>📊 Stats</b>\n"
            f"Total checks : {s['total_checks']}\n"
            f"🚫 Banned    : {s['banned']}\n"
            f"✅ Clean     : {s['clean']}\n"
            f"⚠️ Unknown   : {s['unknown']}\n\n"
            f"Uptime       : {d}d {h}h\n"
            f"Workers      : {WORKERS}\n"
            f"Retries      : {MAX_ATTEMPTS}\n"
            f"Free mode    : {FREE_MODE}\n"
            f"Admins       : {len(ADMIN_IDS)}",
            reply_to=reply_to)

def cmd_broadcast(chat_id, text, reply_to):
    if not text.strip():
        tg_send(chat_id, "Usage: <code>/broadcast &lt;msg&gt;</code>", reply_to=reply_to); return
    users = load_users()
    ok = 0; fail = 0
    for uid in users:
        r = tg_send(uid, f"<b>📢 Announcement</b>\n\n{_esc(text)}")
        if r.get("ok"): ok += 1
        else: fail += 1
    tg_send(chat_id, f"Broadcast sent: ✅{ok} ❌{fail}", reply_to=reply_to)

# ── USER HANDLERS ───────────────────────────────────────────────────
def cmd_redeem(chat_id, uid, args, reply_to):
    if not args:
        tg_send(chat_id, "Usage: <code>/redeem &lt;key&gt;</code>", reply_to=reply_to); return
    ok, msg = redeem_key(uid, args[0].strip().upper())
    tg_send(chat_id, msg, reply_to=reply_to)

def cmd_me(chat_id, uid, reply_to):
    if is_admin(uid):
        tg_send(chat_id, "🛠 You are an admin (unlimited).", reply_to=reply_to); return
    if FREE_MODE:
        tg_send(chat_id, "Free mode is on — no key needed.", reply_to=reply_to); return
    u = get_user(uid) or {}
    bal = u.get("checks", 0)
    bal_s = "∞" if bal == -1 else str(bal)
    tg_send(chat_id,
            f"<b>👤 Your account</b>\n"
            f"Key: <code>{_esc(u.get('key') or '—')}</code>\n"
            f"Balance: {bal_s}\n"
            f"Total used: {u.get('total_used', 0)}",
            reply_to=reply_to)

# ── JOB EXECUTION ───────────────────────────────────────────────────
_executor = ThreadPoolExecutor(max_workers=WORKERS)
_bot_username = ""

def _gate(uid, chat_id, n_devices: int, reply_to) -> bool:
    if is_admin(uid) or FREE_MODE:
        return True
    u = get_user(uid)
    if not u:
        tg_send(chat_id,
                "⛔ You haven't redeemed a key yet.\nUse <code>/redeem &lt;key&gt;</code>.",
                reply_to=reply_to)
        return False
    bal = u.get("checks", 0)
    if bal == -1:
        return True
    if bal < n_devices:
        tg_send(chat_id,
                f"⛔ Not enough credits. Have <b>{bal}</b>, need <b>{n_devices}</b>.",
                reply_to=reply_to)
        return False
    return True

def run_single(chat_id, uid, device_id, reply_to=None):
    if not _gate(uid, chat_id, 1, reply_to):
        return
    user_consume(uid, 1)
    def job():
        try:
            result = process_device(device_id, uid=None, cancel_event=None)
            s = result.get("status")
            if s == "banned":
                tg_send(chat_id, format_ban_reply(result), reply_to=reply_to)
                _nick = (result.get("player") or {}).get("nickname", "?")
                append_line(BAN_FILE, f"{result['device_id']} | {_nick} | {result['ban_info']}")
                bump_stats(banned=1)
            elif s == "clean":
                tg_send(chat_id, format_clean_reply(result), reply_to=reply_to)
                append_line(CLEAN_FILE,
                            f"{result['device_id']} | acc={result['account_id']} "
                            f"zone={result['zone_id']} name={result['player'].get('nickname')}")
                bump_stats(clean=1)
            else:
                tg_send(chat_id, format_error_reply(result), reply_to=reply_to)
                append_line(UNKNOWN_FILE, result["device_id"])
                bump_stats(unknown=1)
        except Exception:
            tg_send(chat_id,
                    f"<b>❌ pipeline crash</b>\n<code>{_esc(traceback.format_exc()[-400:])}</code>",
                    reply_to=reply_to)
    _executor.submit(job)

def run_bulk(chat_id, uid, device_ids: List[str], reply_to=None):
    """Blocking on this thread only. Caller must dispatch to a background thread."""
    total = len(device_ids)
    if total == 0:
        tg_send(chat_id, "Empty list, chef.", reply_to=reply_to); return
    if not _gate(uid, chat_id, total, reply_to):
        return
    user_consume(uid, total)

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    session_path = os.path.join(SESSIONS_DIR, f"{uid}_{ts}.txt")
    with _user_last_session_lock:
        _user_last_session[uid] = session_path

    cancel_event = _get_or_make_event(uid)
    cancel_event.clear()

    def sess_write(line: str):
        try:
            with open(session_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    sess_write(f"# MLBB Bulk Session for {uid}")
    sess_write(f"# Started : {datetime.datetime.now().isoformat()}")
    sess_write(f"# Devices : {total}")
    sess_write("=" * 72)
    sess_write("")

    status = tg_send(chat_id, f"🔎 <b>Bulk starting</b>\nTotal: {total}\n"
                              f"Send <code>/stop</code> to abort.")
    msg_id = status.get("result", {}).get("message_id") if status.get("ok") else None

    counter = {"done": 0, "banned": 0, "clean": 0, "unknown": 0, "cancelled": 0, "stopped": False}
    lock = threading.Lock()

    def update(final=False):
        with lock:
            stop_tag = " (STOPPED)" if counter["stopped"] else ""
            txt = (f"{'✅ DONE' if final else '🔎 Bulk running'}{stop_tag}\n"
                   f"Processed: {counter['done']}/{total}\n"
                   f"🚫 Banned : {counter['banned']}\n"
                   f"✅ Clean  : {counter['clean']}\n"
                   f"⚠️ Unknown: {counter['unknown']}")
            if counter["cancelled"]:
                txt += f"\n⏹ Skipped : {counter['cancelled']}"
        if msg_id:
            tg_edit(chat_id, msg_id, txt)

    def job(dev):
        if cancel_event.is_set():
            with lock:
                counter["cancelled"] += 1
            return
        try:
            r = process_device(dev, uid=uid, cancel_event=cancel_event)
            s = r.get("status")
            with lock:
                if s == "cancelled":
                    counter["cancelled"] += 1
                    sess_write(f"[SKIP  ] {dev} | cancelled")
                    return
                counter["done"] += 1
                if s == "banned":
                    counter["banned"] += 1
                    _nick = (r.get("player") or {}).get("nickname", "?")
                    sess_write(f"[BANNED] {dev} | {_nick} | {r.get('ban_info')}")
                    append_line(BAN_FILE, f"{dev} | {_nick}")
                    tg_send(chat_id, format_ban_reply(r))
                elif s == "clean":
                    counter["clean"] += 1
                    p = r.get("player", {})
                    sess_write(f"[CLEAN ] {dev} | {p.get('nickname','?')} | "
                               f"acc={r.get('account_id')} zone={r.get('zone_id')} | "
                               f"rank={p.get('current_rank')} skins={p.get('skin_count')}")
                    append_line(CLEAN_FILE, dev)
                    tg_send(chat_id, format_clean_reply(r))
                else:
                    counter["unknown"] += 1
                    sess_write(f"[UNKWN ] {dev} | {r.get('reason','?')}")
                    append_line(UNKNOWN_FILE, dev)
            update()
        except Exception as e:
            with lock:
                counter["done"] += 1
                counter["unknown"] += 1
                sess_write(f"[ERROR ] {dev} | {e}")
            update()

    ex = ThreadPoolExecutor(max_workers=WORKERS)
    futures = [ex.submit(job, d) for d in device_ids]
    try:
        for _ in as_completed(futures):
            if cancel_event.is_set():
                for f in futures:
                    f.cancel()
    finally:
        ex.shutdown(wait=True)

    with lock:
        counter["stopped"] = cancel_event.is_set()

    bump_stats(banned=counter["banned"], clean=counter["clean"], unknown=counter["unknown"])
    update(final=True)

    sess_write("")
    sess_write("=" * 72)
    sess_write(f"# Finished : {datetime.datetime.now().isoformat()}")
    sess_write(f"# Done     : {counter['done']}/{total}")
    sess_write(f"# Banned   : {counter['banned']}")
    sess_write(f"# Clean    : {counter['clean']}")
    sess_write(f"# Unknown  : {counter['unknown']}")
    sess_write(f"# Skipped  : {counter['cancelled']}")
    sess_write(f"# Stopped  : {counter['stopped']}")

    _clear_uid(uid)

    done_tag = "STOPPED" if counter["stopped"] else "DONE"
    caption = (f"🏁 Bulk {done_tag} — {counter['done']}/{total}\n"
               f"🚫 {counter['banned']} | ✅ {counter['clean']} | ⚠️ {counter['unknown']}"
               + (f" | ⏹ {counter['cancelled']}" if counter["cancelled"] else ""))
    tg_send(chat_id, caption, reply_to=reply_to)
    tg_send_document(chat_id, session_path, caption="📄 Full results", reply_to=reply_to)

def _run_bulk_async(chat_id, uid, device_ids, reply_to=None):
    """Dispatch bulk onto a background thread so the poll loop stays free."""
    threading.Thread(
        target=run_bulk,
        args=(chat_id, uid, device_ids),
        kwargs={"reply_to": reply_to},
        daemon=True,
        name=f"bulk-{uid}",
    ).start()

# ── MESSAGE ROUTER ──────────────────────────────────────────────────
def handle_message(msg: Dict):
    chat_id = msg["chat"]["id"]
    uid = msg.get("from", {}).get("id")
    text = msg.get("text", "") or ""
    reply_to = msg.get("message_id")
    if uid is None: return

    # ── admin ────────────────────────────────────────────────────
    if text.startswith("/admin"):
        if not is_admin(uid):
            tg_send(chat_id, "⛔ admins only.", reply_to=reply_to); return
        tg_send(chat_id, ADMIN_HELP, reply_to=reply_to); return

    if text.startswith("/genkey"):
        if not is_admin(uid): return
        cmd_genkey(chat_id, uid, text.split()[1:], reply_to); return

    if text.startswith("/keys"):
        if not is_admin(uid): return
        cmd_keys(chat_id, reply_to); return

    if text.startswith("/revoke"):
        if not is_admin(uid): return
        cmd_revoke(chat_id, text.split()[1:], reply_to); return

    if text.startswith("/users"):
        if not is_admin(uid): return
        cmd_users(chat_id, reply_to); return

    if text.startswith("/give"):
        if not is_admin(uid): return
        cmd_give(chat_id, text.split()[1:], reply_to, sign=1); return

    if text.startswith("/take"):
        if not is_admin(uid): return
        cmd_give(chat_id, text.split()[1:], reply_to, sign=-1); return

    if text.startswith("/unbind"):
        if not is_admin(uid): return
        cmd_unbind(chat_id, text.split()[1:], reply_to); return

    if text.startswith("/stats"):
        if not is_admin(uid):
            tg_send(chat_id, "⛔ admins only.", reply_to=reply_to); return
        cmd_stats(chat_id, reply_to); return

    if text.startswith("/broadcast"):
        if not is_admin(uid): return
        cmd_broadcast(chat_id, text[len("/broadcast"):].strip(), reply_to); return

    # ── user ─────────────────────────────────────────────────────
    if text.startswith("/start") or text.startswith("/help"):
        tg_send(chat_id, HELP_TEXT, reply_to=reply_to); return

    if text.startswith("/whoami"):
        tg_send(chat_id, f"Your ID: <code>{uid}</code>", reply_to=reply_to); return

    if text.startswith("/me"):
        cmd_me(chat_id, uid, reply_to); return

    if text.startswith("/redeem"):
        cmd_redeem(chat_id, uid, text.split()[1:], reply_to); return

    if text.startswith("/stop"):
        parts = text.split()
        if len(parts) >= 2 and is_admin(uid):
            try:
                target = int(parts[1])
            except ValueError:
                tg_send(chat_id, "tg_id must be numeric.", reply_to=reply_to); return
            killed = _abort_uid(target)
            tg_send(chat_id,
                    f"⏹ Aborted user <code>{target}</code> — "
                    f"flag set, {killed} socket(s) killed.",
                    reply_to=reply_to)
            return

        with _uid_events_lock:
            has_run = uid in _uid_events
        if not has_run:
            tg_send(chat_id, "Nothing running.", reply_to=reply_to); return
        killed = _abort_uid(uid)
        tg_send(chat_id,
                f"⏹ <b>Stopping now.</b>\n"
                f"Flag set + {killed} active socket(s) force-closed.\n"
                f"Results file lands when the pool drains.",
                reply_to=reply_to)
        return

    if text.startswith("/results"):
        parts = text.split()
        target = uid
        if len(parts) >= 2 and is_admin(uid):
            try: target = int(parts[1])
            except ValueError: pass
        with _user_last_session_lock:
            path = _user_last_session.get(target)
        if not path or not os.path.exists(path):
            tg_send(chat_id, "No result file yet for that user.", reply_to=reply_to); return
        tg_send_document(chat_id, path, caption="📄 Latest results", reply_to=reply_to)
        return

    if text.startswith("/bulk"):
        tg_send(chat_id, "Send a .txt file (one device ID per line).", reply_to=reply_to); return

    if text.startswith("/check"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            tg_send(chat_id, "Usage: <code>/check and_xxxx...</code>", reply_to=reply_to); return
        dev = parts[1].strip().split()[0]
        if not (dev.startswith("and_") or dev.startswith("ios_")):
            tg_send(chat_id, "Device ID must start with <code>and_</code> or <code>ios_</code>.", reply_to=reply_to); return
        tg_send(chat_id, f"🔎 checking <code>{_esc(dev[:40])}...</code>", reply_to=reply_to)
        run_single(chat_id, uid, dev, reply_to=reply_to); return

    if "document" in msg:
        doc = msg["document"]
        fname = (doc.get("file_name") or "").lower()
        if not (fname.endswith(".txt") or fname.endswith(".csv")):
            tg_send(chat_id, "Send a .txt or .csv file.", reply_to=reply_to); return
        url = tg_get_file(doc["file_id"])
        if not url:
            tg_send(chat_id, "Couldn't fetch file.", reply_to=reply_to); return
        raw = tg_download(url)
        if not raw:
            tg_send(chat_id, "Couldn't download file.", reply_to=reply_to); return
        try: content = raw.decode("utf-8", errors="ignore")
        except Exception: content = ""
        ids = []
        for ln in content.splitlines():
            ln = ln.strip()
            if not ln: continue
            if "and_" in ln or "ios_" in ln:
                ids.append(ln.split()[0])
        if not ids:
            tg_send(chat_id, "No valid device IDs found.", reply_to=reply_to); return
        # ── FIX: dispatch to background thread, don't block the poll loop ──
        _run_bulk_async(chat_id, uid, ids, reply_to=reply_to); return

    t = text.strip()
    if t.startswith("and_") or t.startswith("ios_"):
        dev = t.split()[0]
        tg_send(chat_id, f"🔎 checking <code>{_esc(dev[:40])}...</code>", reply_to=reply_to)
        run_single(chat_id, uid, dev, reply_to=reply_to); return

    if t:
        tg_send(chat_id, HELP_TEXT, reply_to=reply_to)

# ── MAIN LOOP ───────────────────────────────────────────────────────
def main():
    global _bot_username
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_TOKEN_HERE":
        print("[!] Set MLBB_BOT_TOKEN first."); sys.exit(1)

    me = tg_api("getMe")
    if not me.get("ok"):
        print("[!] Invalid token:", me); sys.exit(1)
    _bot_username = me["result"].get("username","")

    print(f"[+] Bot: @{_bot_username}")
    print(f"[+] Results dir : {RESULTS_DIR}")
    print(f"[+] Sessions    : {SESSIONS_DIR}")
    print(f"[+] Workers     : {WORKERS}")
    print(f"[+] Retries     : {MAX_ATTEMPTS}")
    print(f"[+] Free mode   : {FREE_MODE}")
    print(f"[+] Admins      : {sorted(ADMIN_IDS) or 'NONE — set MLBB_ADMINS'}")
    if not ADMIN_IDS and not FREE_MODE:
        print("[!] No admins and not free mode — nobody can use the bot.")

    tg_api("deleteWebhook", drop_pending_updates=False)

    offset = None
    while True:
        try:
            resp = tg_api("getUpdates", offset=offset, timeout=25,
                          allowed_updates=["message"])
            if not resp.get("ok"):
                time.sleep(3); continue
            for upd in resp.get("result", []):
                offset = upd["update_id"] + 1
                m = upd.get("message")
                if not m: continue
                try: handle_message(m)
                except Exception as e:
                    print("[!] handler error:", e)
        except KeyboardInterrupt:
            print("\n[+] bye"); break
        except Exception as e:
            print("[!] poll error:", e); time.sleep(3)

if __name__ == "__main__":
    main()
