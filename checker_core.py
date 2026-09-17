"""
MLBB Device ID Checker — Core
Fetches full account info via PID protocol over TCP.

Fix v4 — aligned with working reference (Dev_Id_checker.py):
  - CLIENT_VERSION bumped to 2.1.99.1205.1 (old version breaks post-handshake)
  - Wake phase removed — it was counterproductive
  - Lookup follows the working order: skin(10143) → ban(10101) → lookup(11153) → role(10128)
  - Empty-body PID 2 recognised as a distinct refusal
  - Clean ✓ / ✗ step logging
"""
from __future__ import annotations
import datetime, logging, os, socket, struct, sys, time, zlib
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

# ─────────────────────────────────────────────
# Colored logger
# ─────────────────────────────────────────────
_USE_COLOR = sys.stdout.isatty() or os.environ.get("MLBB_COLOR", "1") == "1"
_G = "\033[92m"; _R = "\033[91m"; _Y = "\033[93m"; _C = "\033[96m"; _D = "\033[90m"; _X = "\033[0m"

def _c(txt, col):
    return f"{col}{txt}{_X}" if _USE_COLOR else txt

def ok(msg):   log.info("%s %s", _c("✓", _G), msg)
def fail(msg): log.info("%s %s", _c("✗", _R), msg)
def step(msg): log.info("%s %s", _c("•", _C), msg)
def warn(msg): log.info("%s %s", _c("!", _Y), msg)
def dbg(msg):  log.debug("%s %s", _c("·", _D), msg)

log = logging.getLogger("mlbb.core")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s | %(message)s", datefmt="%H:%M:%S"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
LOGIN_HOST      = os.environ.get("MLBB_LOGIN_HOST", "login.ml.youngjoygame.com")
LOGIN_PORT      = int(os.environ.get("MLBB_LOGIN_PORT", "30021"))
# ── UPDATED: matches the working reference ──
CLIENT_VERSION  = os.environ.get("MLBB_CLIENT_VERSION", "2.1.99.1205.1")
CHANNEL_AND     = os.environ.get("MLBB_CHANNEL", "and_usa")
CHANNEL_IOS     = "ios_usa"
LANGUAGE        = "en"
SOCK_CONNECT    = float(os.environ.get("MLBB_CONNECT_TIMEOUT", "8.0"))
SOCK_READ       = float(os.environ.get("MLBB_SOCK_TIMEOUT",   "6.0"))
RECV_CHUNK      = 8192
DEBUG_WIRE      = os.environ.get("MLBB_DEBUG_WIRE", "0") == "1"

# ─────────────────────────────────────────────
# Hero names
# ─────────────────────────────────────────────
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

# ─────────────────────────────────────────────
# Rank mapping
# ─────────────────────────────────────────────
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

def map_collector(point) -> str:
    if not point or not isinstance(point, (int, float)): return "No Tier"
    p = int(point)
    if p < 1000: return "No Tier"
    tiers = [(1000,4000,"Amateur Collector"),(4000,10000,"Junior Collector"),
             (10000,22000,"Seasoned Collector"),(22000,44000,"Expert Collector"),
             (44000,84000,"Renowned Collector"),(84000,160000,"Exalted Collector"),
             (160000,280000,"Mega Collector"),(280000,float("inf"),"World Collector")]
    for mn, mx, name in tiers:
        if mn <= p < mx:
            if name == "World Collector": return "World Collector"
            per = (mx - mn) / 5
            lvl = max(0, min(4, int((p - mn) // per)))
            return f"{name} {['V','IV','III','II','I'][lvl]}"
    return "Unknown"

def fmt_ts(ts) -> str:
    if not ts: return "N/A"
    try:
        return datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception: return "N/A"

def is_guest_account(acc) -> bool:
    if acc is None: return False
    s = str(acc).strip()
    return s.startswith("221") or s.startswith("222")

AES_KEY = bytes.fromhex("f5a193d50ade553e9835595f5cd75ddd")
AES_IV  = b"\x00" * 16

# ─────────────────────────────────────────────
# SDP
# ─────────────────────────────────────────────
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
        else: raise Exception(f"Unsupported: {type(val)}")
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

# ─────────────────────────────────────────────
# Device ID parsing
# ─────────────────────────────────────────────
def detect_platform(did: str) -> str:
    return "ios" if str(did or "").strip().lower().startswith("ios_") else "and"

def parse_device_id(device_id: str) -> Dict[str, Any]:
    raw = (device_id or "").strip()
    platform = detect_platform(raw)
    body = raw[4:] if raw[:4].lower() in ("and_", "ios_") else raw
    if platform == "and":
        clean = body.replace("-", "")
        imei    = clean[:32] if len(clean) >= 32 else clean
        android = clean[32:48] if len(clean) >= 48 else ""
        adid    = clean[48:]   if len(clean) > 48  else ""
        parsed = {"platform":"and","imei":imei,"android":android,"adid":adid,
                  "channel":CHANNEL_AND,
                  "auth_str": f"gps_adid={adid}&android_id={android}&device_unique_id={imei}"}
    else:
        imei    = body[:32] if len(body) >= 32 else body
        android = body[32:48] if len(body) >= 48 else ""
        adid    = body[48:]   if len(body) > 48  else ""
        parsed = {"platform":"ios","imei":imei,"android":android,"adid":adid,
                  "channel":CHANNEL_IOS,
                  "auth_str": f"idfa={adid}&idfv={android}&device_unique_id={imei}"}
    dbg(f"device_id parsed: platform={platform} imei={len(parsed['imei'])} "
        f"android={len(parsed['android'])} adid={len(parsed['adid'])}")
    return parsed

def validate_device_id(did: str) -> Tuple[bool, str]:
    if not isinstance(did, str): return False, "Not a string"
    did = did.strip()
    if not did: return False, "Empty"
    if not did.startswith(("and_", "ios_")): return False, "Missing and_/ios_ prefix"
    body = did[4:]
    if len(body) < 20: return False, f"Body too short ({len(body)})"
    return True, "OK"

# ─────────────────────────────────────────────
# Connection
# ─────────────────────────────────────────────
class BaseConnection:
    def __init__(self, host, port):
        self.host = host; self.port = port; self.sequence = 1
        self.socket = None; self.queue = b""

    def connect(self):
        dbg(f"connecting TCP {self.host}:{self.port}")
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try: self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception: pass
        self.socket.settimeout(SOCK_CONNECT)
        try:
            self.socket.connect((self.host, self.port))
        except Exception as e:
            try: self.socket.close()
            except Exception: pass
            self.socket = None
            raise ConnectionError(f"TCP connect failed: {e}")
        self.socket.settimeout(SOCK_READ)
        dbg(f"TCP connected, local={self.socket.getsockname()}")

    def cleanup(self):
        if self.socket:
            try: self.socket.close()
            except Exception: pass
            self.socket = None; self.sequence = 1; self.queue = b""

    def send_data(self, pid, sdp: SdpStruct):
        if not self.socket: raise ConnectionError("socket closed")
        pkt = SdpStruct({0: pid, 1: self.sequence, 5: sdp.data}).data
        comp = zstd.compress(pkt)
        flags = (len(comp) + 4) | (16 << 24)
        if DEBUG_WIRE:
            dbg(f"send PID={pid} seq={self.sequence} raw={len(pkt)} comp={len(comp)}")
        try:
            self.socket.sendall(flags.to_bytes(4, "big") + comp)
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            self.cleanup()
            raise ConnectionError(f"send PID {pid} failed: {e}")
        self.sequence += 1

    def recv_data(self):
        if not self.socket: return None, None
        try:
            while len(self.queue) < 4:
                d = self.socket.recv(RECV_CHUNK)
                if not d: return None, None
                self.queue += d
            flags = int.from_bytes(self.queue[:4], "big")
            size = flags & 0xFFFFFF; ctype = flags >> 24
            while len(self.queue) < size:
                d = self.socket.recv(RECV_CHUNK)
                if not d: return None, None
                self.queue += d
            data = self.queue[4:size]; self.queue = self.queue[size:]
            if DEBUG_WIRE:
                dbg(f"recv frame ctype={ctype} size={size}")
            if ctype == 1:   data = zlib.decompress(data)
            elif ctype == 16: data = zstd.decompress(data)
            elif ctype in (2, 3, 18):
                c = AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV)
                data = c.decrypt(data[:-1] if len(data) % 16 else data).rstrip(b"\x00")
                if ctype == 3:  data = zlib.decompress(data)
                elif ctype == 18: data = zstd.decompress(data)
            res = SdpStruct(data); pid = res.get(0)
            if pid is None: return None, None
            body = res.get(6) or res.get(5)
            if body and isinstance(body, bytes):
                try: return pid, SdpStruct(body)
                except Exception: return pid, None
            return pid, None
        except socket.timeout:
            return -1, None
        except ConnectionError:
            return None, None
        except Exception as e:
            dbg(f"recv error: {e!r}")
            return None, None


class GameConnection(BaseConnection):
    def __init__(self, device_id: str):
        super().__init__(LOGIN_HOST, LOGIN_PORT)
        self.device_id = device_id
        p = parse_device_id(device_id)
        self.platform = p["platform"]; self.imei = p["imei"]
        self.android = p["android"];   self.adid = p["adid"]
        self.channel = p["channel"];   self.auth_str = p["auth_str"]
        self.account_id = 0; self.session_key = ""; self.zone_id = 0
        self.game_host = ""; self.game_port = 0
        self.creation_ts = 0
        self.is_guest = False
        self.last_server_error: Optional[Dict[str, Any]] = None
        # cached results from warm-up phase
        self._skin_resp = None
        self._ban_stat  = "NORMAL"

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
    def _parse_hostport(raw) -> Tuple[Optional[str], Optional[int]]:
        if raw is None: return None, None
        if isinstance(raw, bytes): raw = raw.decode("utf-8", errors="ignore")
        if isinstance(raw, str):
            if ":" not in raw: return None, None
            h, p = raw.rsplit(":", 1)
            try: return h.strip(), int(p.strip())
            except Exception: return None, None
        if isinstance(raw, dict):
            for v in raw.values():
                if isinstance(v, str) and ":" in v:
                    h, p = GameConnection._parse_hostport(v)
                    if h and p: return h, p
            for v in raw.values():
                if isinstance(v, (list, tuple, dict)):
                    h, p = GameConnection._parse_hostport(v)
                    if h and p: return h, p
        if isinstance(raw, (list, tuple)) and len(raw) >= 2:
            try: return str(raw[0]).strip(), int(raw[1])
            except Exception: pass
        return None, None

    @staticmethod
    def _describe_error_packet(res) -> str:
        try:
            if not isinstance(res, dict): return f"raw={res!r}"
            parts = []
            inner = res.get(0)
            if isinstance(inner, dict):
                for k, v in inner.items(): parts.append(f"{k}={v!r}")
            elif inner is not None:
                parts.append(f"0={inner!r}")
            for k in (1, 2, 3, 4, 5):
                if k in res and not isinstance(res[k], (dict, list)):
                    parts.append(f"{k}={res[k]!r}")
            return "; ".join(parts) if parts else f"keys={list(res.keys())}"
        except Exception as e:
            return f"<unparsable: {e!r}>"

    # ── Login (PID 1 → 2) ──
    def login(self) -> bool:
        try:
            self.connect()
        except Exception as e:
            fail(f"Login — TCP connect failed ({e})")
            return False

        payload = SdpStruct({
            0: self.device_id, 1: self.auth_str,
            2: CLIENT_VERSION, 3: self.channel, 4: LANGUAGE})
        try:
            self.send_data(1, payload)
        except Exception as e:
            fail(f"Login — send PID 1 failed ({e})")
            return False

        for _ in range(6):
            pid, res = self.recv_data()
            if pid in (None, -1):
                continue
            if pid == 20001:
                desc = self._describe_error_packet(res)
                self.last_server_error = {"pid": 20001, "detail": desc}
                warn(f"Login — server error 20001: {desc}")
                continue
            if pid == 2:
                if not res or not isinstance(res, dict) or not res.get(0):
                    fail("Login — server returned empty PID 2 (device unknown or rate-limited)")
                    return False
                acc = res.get(0)
                if is_guest_account(acc):
                    fail(f"Login — guest / unregistered device (acc={acc})")
                    self.is_guest = True
                    return False
                self.account_id  = acc
                self.session_key = res.get(1) or ""
                self.zone_id     = self._coerce_zone(res.get(2))
                self.creation_ts = res.get(19, 0)
                if not self.account_id:
                    fail("Login — account_id is empty")
                    return False
                ok(f"Login — account={self.account_id} zone={self.zone_id} "
                   f"version={CLIENT_VERSION} channel={self.channel}")
                return True
        if self.last_server_error:
            fail(f"Login — {self.last_server_error['detail']}")
        else:
            fail("Login — no PID 2 received (timeout)")
        return False

    # ── Game server (PID 5 → 6) ──
    def resolve_server(self) -> bool:
        self.send_data(5, SdpStruct({0: self.account_id, 1: self.session_key,
                                     2: CLIENT_VERSION, 5: self.zone_id, 6: self.channel}))
        for _ in range(8):
            pid, res = self.recv_data()
            if pid in (None, -1): return False
            if pid == 6 and res:
                h, p = self._parse_hostport(res.get(1))
                if h and p:
                    self.game_host = h; self.game_port = p
                    ok(f"Server — resolved {h}:{p}")
                    return True
        fail("Server — resolve failed")
        return False

    # ── Handshake (PID 10001 → 10002) ──
    def handshake(self) -> bool:
        self.cleanup()
        self.host = self.game_host; self.port = self.game_port
        try:
            self.connect()
        except Exception as e:
            fail(f"Handshake — TCP connect failed ({e})")
            return False
        try:
            self.send_data(10001, SdpStruct({
                0: self.account_id, 1: self.session_key, 2: self.zone_id,
                4: CLIENT_VERSION, 13: self.channel, 15: self.device_id}))
        except Exception as e:
            fail(f"Handshake — send PID 10001 failed ({e})")
            return False
        for _ in range(20):
            pid, res = self.recv_data()
            if pid is None or pid == -1:
                fail("Handshake — no PID 10002 (timeout)")
                return False
            if pid == 10002:
                if isinstance(res, dict):
                    for cand in (1, 3, 5):
                        sv = res.get(cand)
                        if isinstance(sv, str) and len(sv) > 8:
                            self.session_key = sv
                            break
                ok(f"Handshake — session accepted on {self.game_host}:{self.game_port}")
                return True
            if pid == 20001:
                warn(f"Handshake — server error 20001: {self._describe_error_packet(res)}")
                continue
        fail("Handshake — exhausted 20 attempts")
        return False

    # ── Warm-up: skin info (PID 10143 → 10144) ──
    def _warm_skin(self) -> Optional[SdpStruct]:
        try:
            self.send_data(10143, SdpStruct({0: int(self.account_id), 1: int(self.zone_id)}))
            for _ in range(4):
                pid, res = self.recv_data()
                if pid in (None, -1): break
                if pid == 10144 and res:
                    self._skin_resp = res
                    return res
                if pid == 20001: continue
        except Exception: pass
        return None

    # ── Warm-up: ban check (PID 10101) ──
    def _warm_ban(self) -> str:
        try:
            self.send_data(10101, SdpStruct({0: 0, 2: 2}))
            for _ in range(3):
                pid, res = self.recv_data()
                if pid in (-1, None, 20002): break
                if pid == 20001 and res and isinstance(res, dict) and 0 in res and isinstance(res[0], dict):
                    b = res[0]
                    reason = b.get("ban_reason", "")
                    d = str(b.get("endtime_day","0") or "0")
                    h = str(b.get("endtime_hour","0") or "0")
                    m = str(b.get("endtime_min","0") or "0")
                    s = str(b.get("endtime_sec","0") or "0")
                    try: total = int(d)*86400 + int(h)*3600 + int(m)*60 + int(s)
                    except Exception: total = 0
                    if (reason and str(reason).strip()) or total > 0:
                        self._ban_stat = f"BANNED (Reason: {reason or '?'} | Remaining: {d}d {h}h {m}m {s}s)"
                        return self._ban_stat
        except Exception: pass
        return "NORMAL"

    # ── Lookup (PID 11153 → 11154) — matches working order ──
    def lookup(self) -> Optional[SdpStruct]:
        # Warm-up sequence copied from the working reference: skin → ban → lookup.
        # Skipping these makes the server silently drop 11153.
        self._warm_skin()
        self._warm_ban()

        for attempt in range(3):
            try:
                self.send_data(11153, SdpStruct({1: int(self.account_id)}))
            except ConnectionError:
                return None
            cnt = 0
            for _ in range(8):
                pid, res = self.recv_data()
                if pid in (-1, None): break
                if pid == 11154:
                    ok(f"Lookup — profile received (PID 11153 → 11154)")
                    return res
                if pid == 20001:
                    cnt += 1
                    if cnt >= 2:
                        fail(f"Lookup — server error 20001 x{cnt}")
                        return None
            time.sleep(0.3)
        fail("Lookup — no PID 11154 response")
        return None

    # ── Role info (PID 10128 → 10129) ──
    def role_info(self) -> Optional[SdpStruct]:
        try:
            self.send_data(10128, SdpStruct({1: int(self.account_id), 2: int(self.zone_id)}))
            best = None
            for _ in range(20):
                pid, res = self.recv_data()
                if pid in (None, -1): break
                if pid == 20001: continue
                if pid == 10129 and res:
                    if best is None: best = res
                    if res.get(9, 0) > 0: return res
            return best
        except Exception: return None

    # ── Skin info (cached) ──
    def skin_info(self) -> Optional[SdpStruct]:
        if self._skin_resp is not None:
            return self._skin_resp
        return self._warm_skin()

    # ── Ban status (cached) ──
    def ban_status(self) -> Dict[str, Any]:
        if self.is_guest or is_guest_account(self.account_id):
            return {"banned": False, "label": "UNREGISTERED"}
        stat = self._ban_stat
        if stat.startswith("BANNED"):
            return {"banned": True, "label": stat}
        return {"banned": False, "label": stat or "NORMAL"}


# ─────────────────────────────────────────────
# Extract player data
# ─────────────────────────────────────────────
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


def extract_player(result, role_info=None, creation_ts=0, skin_resp=None, ban=None) -> Optional[Dict[str, Any]]:
    if not result or 0 not in result: return None
    plist = result.get(0)
    if not isinstance(plist, list) or not plist: return None
    pd = plist[0]
    if not isinstance(pd, dict): return None
    if is_guest_account(pd.get(0)): return None

    try:
        nickname   = pd.get(2, "Unknown")
        player_id  = pd.get(0, "Unknown")
        server     = pd.get(1, "Unknown")
        level      = pd.get(3, "Unknown")
        skin_count = int(pd.get(83, 0) or 0)
        hero_count = int(pd.get(4, 0)  or 0)
        total_battles = int(pd.get(17, 0) or 0)

        if role_info and isinstance(role_info, dict):
            try: hero_count = int(role_info.get(9, hero_count) or hero_count)
            except Exception: pass
            try: total_battles = int(role_info.get(22, total_battles) or total_battles)
            except Exception: pass

        # Skin count can also come from 10144 tag 10 (skin_info); prefer the larger
        if skin_resp and isinstance(skin_resp, dict):
            try:
                s10 = int(skin_resp.get(10, 0) or 0)
                if s10 > skin_count: skin_count = s10
            except Exception: pass
            try:
                s9 = int(skin_resp.get(9, 0) or 0)
                if s9 > hero_count: hero_count = s9
            except Exception: pass

        location = None
        ld = pd.get(71)
        if isinstance(ld, list) and len(ld) >= 2:
            location = ", ".join(str(x) for x in ld)

        last_login = fmt_ts(pd.get(5, 0))
        last_login_country = str(pd.get(87)) if pd.get(87) else None
        create_country     = str(pd.get(97)) if pd.get(97) else None

        sn = str(pd.get(30, "")).replace("`", "").strip()
        si = str(pd.get(31, "")).strip()
        squad = f"{si} {sn}".strip() if sn else None

        cur_rank  = map_rank(pd.get(8))  if pd.get(8)  is not None else "Unranked"
        high_rank = map_rank(pd.get(95)) if pd.get(95) is not None else "N/A"

        t136 = pd.get(136, {})
        cpt = int(t136.get(9, 0) or 0) if isinstance(t136, dict) else 0
        ctier = map_collector(cpt) if cpt > 0 else "No Tier"

        skin_breakdown = _parse_skin_breakdown(None)
        if skin_resp and isinstance(skin_resp, dict):
            t118 = skin_resp.get(118) or skin_resp.get("118")
            if t118: skin_breakdown = _parse_skin_breakdown(t118)

        try: win = int(pd.get(18, 0) or 0)
        except Exception: win = 0
        try: loss = int(pd.get(155, 0) or 0)
        except Exception: loss = 0
        total_wl = win + loss
        win_rate = f"{win/total_wl*100:.2f}%" if total_wl > 0 else "N/A"

        t91 = pd.get(91, [])
        recent = []
        if isinstance(t91, list):
            seen = set()
            for hid in t91:
                try: hi = int(hid)
                except Exception: continue
                if hi in seen: continue
                seen.add(hi); recent.append(hero_name(hi))
                if len(recent) >= 5: break

        ban = ban or {"banned": False, "reason": "", "remaining": "", "label": "Not Banned"}

        return {
            "nickname": nickname, "player_id": player_id, "server": server, "level": level,
            "skin_count": skin_count, "hero_count": hero_count,
            "current_rank": cur_rank, "high_rank": high_rank,
            "collector_point": cpt, "collector_tier": ctier,
            "last_login": last_login, "last_login_country": last_login_country,
            "create_country": create_country, "location": location,
            "squad": squad, "total_battles": total_battles, "win_rate": win_rate,
            "hero_history": recent, "skin_breakdown": skin_breakdown,
            "creation_date": fmt_ts(creation_ts) if creation_ts else "N/A",
            "is_banned": ban.get("banned", False),
            "ban_reason": ban.get("reason", ""),
            "ban_remaining": ban.get("remaining", ""),
            "ban_label": ban.get("label", "Not Banned"),
        }
    except Exception as e:
        dbg(f"extract error: {e}")
        return None


# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────
def check_device_id(device_id: str, retries: int = 2) -> Dict[str, Any]:
    device_id = (device_id or "").strip()
    ok_flag, reason = validate_device_id(device_id)
    if not ok_flag:
        return {"status": "error", "device_id": device_id,
                "error": f"Invalid format: {reason}"}

    last_err = "unknown"
    for attempt in range(retries):
        conn: Optional[GameConnection] = None
        try:
            step(f"Check {device_id[:12]}… (attempt {attempt + 1}/{retries})")
            conn = GameConnection(device_id)

            if not conn.login():
                conn.cleanup()
                if conn.is_guest:
                    return {"status": "error", "device_id": device_id,
                            "error": "Guest / unregistered device — no real account"}
                last_err = "Login failed"
                if conn.last_server_error:
                    last_err = f"Login failed — {conn.last_server_error['detail']}"
                time.sleep(0.4); continue

            if not conn.resolve_server():
                conn.cleanup(); last_err = "Server resolve failed"
                time.sleep(0.4); continue

            if not conn.handshake():
                conn.cleanup(); last_err = "Handshake failed"
                time.sleep(0.4); continue

            result    = conn.lookup()            # also does skin+ban warm-up
            role_info = conn.role_info()
            skin_resp = conn.skin_info()         # cached
            ban       = conn.ban_status()        # cached
            creation  = conn.creation_ts
            conn.cleanup()

            if not result:
                last_err = "Lookup failed — device did not respond to profile request"
                time.sleep(0.4); continue

            player = extract_player(result, role_info=role_info, creation_ts=creation,
                                    skin_resp=skin_resp, ban=ban)
            if not player:
                last_err = "Parse failed"
                time.sleep(0.4); continue

            ok(f"Done — {player.get('nickname')} "
               f"(Lv{player.get('level')}, {player.get('current_rank')})")
            return {"status": "success", "device_id": device_id, "player_data": player}

        except ConnectionError as e:
            last_err = str(e)
            fail(f"Connection error: {last_err}")
            if conn:
                try: conn.cleanup()
                except Exception: pass
            time.sleep(0.4); continue
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            fail(f"Unexpected: {last_err}")
            if conn:
                try: conn.cleanup()
                except Exception: pass
            time.sleep(0.4); continue

    fail(f"Final failure for {device_id[:12]}: {last_err}")
    return {"status": "error", "device_id": device_id, "error": last_err}


def read_ids_from_text(text: str) -> List[str]:
    out = []
    for line in text.splitlines():
        r = line.strip()
        if r and not r.startswith("#"): out.append(r)
    return out


def save_line(did: str, p: Dict[str, Any]) -> str:
    recent = p.get("hero_history") or []
    lh = recent[0] if recent else "N/A"
    return (f"Device ID: {did} | Name: {p.get('nickname','N/A')} | "
            f"Role ID: {p.get('player_id','N/A')} | Server: {p.get('server','N/A')} | "
            f"Level: {p.get('level','N/A')} | Rank: {p.get('current_rank','N/A')} | "
            f"High Rank: {p.get('high_rank','N/A')} | Skins: {p.get('skin_count','N/A')} | "
            f"Heroes: {p.get('hero_count','N/A')} | WR: {p.get('win_rate','N/A')} | "
            f"Collector: {p.get('collector_tier','None')} | "
            f"Last Login: {p.get('last_login','N/A')} | Ban: {p.get('ban_label','Not Banned')}")