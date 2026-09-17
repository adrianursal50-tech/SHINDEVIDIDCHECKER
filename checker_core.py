"""
MLBB Device ID Checker — Core Module
"""
from __future__ import annotations
import datetime, hashlib, logging, os, random, socket, struct, time, uuid, zlib
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
LOGIN_HOST     = os.environ.get("MLBB_LOGIN_HOST", "login.ml.youngjoygame.com")
LOGIN_PORT     = int(os.environ.get("MLBB_LOGIN_PORT", "30021"))
CLIENT_VERSION = os.environ.get("MLBB_CLIENT_VERSION", "2.1.88.1205.1")
CHANNEL_AND    = os.environ.get("MLBB_CHANNEL", "and_usa")
CHANNEL_IOS    = "ios_usa"
LANGUAGE       = os.environ.get("MLBB_LANG", "en")
SOCK_CONNECT   = float(os.environ.get("MLBB_CONNECT_TIMEOUT", "10.0"))
SOCK_READ      = float(os.environ.get("MLBB_SOCK_TIMEOUT", "6.0"))
RECV_CHUNK     = 8192
DEBUG          = os.environ.get("MLBB_DEBUG", "").lower() in ("1", "true", "yes")

if DEBUG:
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(message)s")

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

# ---------------- Rank / Collector ----------------
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
    if not point or not isinstance(point, (int, float)): return "N/A"
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

def fmt_ts(ts) -> str:
    if not ts: return "N/A"
    try:
        return datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception: return "N/A"

def is_guest_account(acc) -> bool:
    if acc is None: return False
    s = str(acc).strip()
    return s.startswith("221") or s.startswith("222")

# ---------------- AES ----------------
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
    body = raw[4:] if raw[:4].lower() in ("and_", "ios_") else raw
    imei    = body[:32] if len(body) >= 32 else body
    android = body[32:48] if len(body) >= 48 else ""
    adid    = body[48:] if len(body) > 48 else ""
    if platform == "ios":
        return {"platform":"ios","imei":imei,"android":android,"adid":adid,
                "channel":CHANNEL_IOS,
                "auth_str": f"idfa={adid}&idfv={android}&device_unique_id={imei}"}
    return {"platform":"and","imei":imei,"android":android,"adid":adid,
            "channel":CHANNEL_AND,
            "auth_str": f"gps_adid={adid}&android_id={android}&device_unique_id={imei}"}

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
    def __init__(self, host, port):
        self.host = host; self.port = port; self.sequence = 1
        self.socket: Optional[socket.socket] = None; self.queue = b""

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

    def __enter__(self): self.connect(); return self
    def __exit__(self, *a): self.cleanup()

    def send_data(self, pid, sdp: SdpStruct):
        if not self.socket:
            raise ConnectionError("socket closed")
        pkt = SdpStruct({0: pid, 1: self.sequence, 5: sdp.data}).data
        comp = zstd.compress(pkt)
        flags = (len(comp) + 4) | (16 << 24)
        try:
            self.socket.sendall(flags.to_bytes(4, "big") + comp)
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            self.cleanup()
            raise ConnectionError(f"socket send failed: {e}")
        self.sequence += 1

    # ============================================================
    # recv_data — clean, no bad fallback
    # ============================================================
    def recv_data(self):
        if not self.socket:
            return None, None
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
            if ctype == 1: data = zlib.decompress(data)
            elif ctype == 16: data = zstd.decompress(data)
            elif ctype in (2, 3, 18):
                c = AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV)
                data = c.decrypt(data[:-1] if len(data) % 16 else data).rstrip(b"\x00")
                if ctype == 3: data = zlib.decompress(data)
                elif ctype == 18: data = zstd.decompress(data)
            res = SdpStruct(data); pid = res.get(0)
            if pid is None: return None, None
            if DEBUG:
                keys = list(res.keys())
                logging.debug("recv pid=%s keys=%s", pid, keys)

            # Standard wrapper: field 6 or 5 is the payload
            body = res.get(6)
            if body is None:
                body = res.get(5)
            if body is not None:
                if isinstance(body, bytes) and len(body) > 0:
                    try:
                        inner = SdpStruct(body)
                        if len(inner) > 0:
                            return pid, inner
                    except Exception:
                        pass
                elif isinstance(body, dict) and len(body) > 0:
                    return pid, SdpStruct(dict(body))

            # NO fallback to outer packet — that corrupts login
            return pid, None
        except socket.timeout:
            return -1, None
        except (BrokenPipeError, ConnectionResetError, OSError):
            return None, None
        except Exception:
            return None, None


class GameConnection(BaseConnection):
    def __init__(self, device_id: str):
        super().__init__(LOGIN_HOST, LOGIN_PORT)
        self.device_id = device_id
        p = parse_device_id(device_id)
        self.platform = p["platform"]; self.imei = p["imei"]
        self.android = p["android"]; self.adid = p["adid"]
        self.channel = p["channel"]; self.auth_str = p["auth_str"]
        self.account_id = 0; self.session_key = ""; self.zone_id = 0
        self.game_host = ""; self.game_port = 0
        self.is_guest = False
        self.last_login_err = ""
        self.trace: List[Dict[str, Any]] = []

    def _trace(self, label, pid, res=None):
        entry = {"where": label, "pid": pid}
        if res is not None:
            entry["keys"] = list(res.keys())[:10]
        self.trace.append(entry)
        if DEBUG:
            logging.debug("[%s] pid=%s keys=%s", label, pid,
                          list(res.keys())[:10] if res is not None else None)

    @staticmethod
    def _coerce_zone(z) -> int:
        if z is None: return 0
        if isinstance(z, bool): return int(z)
        if isinstance(z, int): return z
        if isinstance(z, (list, tuple)):
            return GameConnection._coerce_zone(z[0]) if z else 0
        if isinstance(z, dict):
            for k in (0, 1, "0", "id", "zone", "zone_id"):
                if k in z: return GameConnection._coerce_zone(z[k])
            if z: return GameConnection._coerce_zone(next(iter(z.values())))
        try: return int(z)
        except Exception: return 0

    @staticmethod
    def _parse_hostport(raw) -> Tuple[Optional[str], Optional[int]]:
        if raw is None: return None, None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        if isinstance(raw, str):
            if ":" not in raw: return None, None
            host, port = raw.rsplit(":", 1)
            try: return host.strip(), int(port.strip())
            except Exception: return None, None
        if isinstance(raw, dict):
            for v in raw.values():
                if isinstance(v, str) and ":" in v:
                    h, p = GameConnection._parse_hostport(v)
                    if h and p: return h, p
            h = raw.get(1) or raw.get("host") or raw.get("ip") or raw.get(0)
            p = raw.get(2) or raw.get("port")
            if h and p:
                try: return str(h).strip(), int(p)
                except Exception: pass
            vals = list(raw.values())
            if len(vals) >= 2:
                try: return str(vals[0]).strip(), int(vals[1])
                except Exception: pass
            for v in raw.values():
                if isinstance(v, (dict, list, tuple)):
                    h, p = GameConnection._parse_hostport(v)
                    if h and p: return h, p
            return None, None
        if isinstance(raw, (list, tuple)) and len(raw) >= 2:
            try: return str(raw[0]).strip(), int(raw[1])
            except Exception: pass
            for item in raw:
                if isinstance(item, (dict, list, tuple)):
                    h, p = GameConnection._parse_hostport(item)
                    if h and p: return h, p
        return None, None

    # ---------- LOGIN — strict ----------
    def login_to_login_server(self) -> bool:
        if not self.socket or self.host != LOGIN_HOST:
            self.cleanup()
            self.host, self.port = LOGIN_HOST, LOGIN_PORT
            self.connect()
        self.send_data(1, SdpStruct({
            0: self.device_id, 1: self.auth_str,
            2: CLIENT_VERSION, 3: self.channel, 4: LANGUAGE,
        }))
        last_pid = None; saw_timeout = False; saw_close = False
        for _ in range(10):
            pid, res = self.recv_data()
            self._trace("login", pid, res)
            last_pid = pid
            if pid is None: saw_close = True; break
            if pid == -1: saw_timeout = True; continue
            if pid == 20001: continue
            if pid != 2: continue
            if not res: continue
            acc = res.get(0)
            # STRICT: reject garbage account IDs
            if acc is None: continue
            if not isinstance(acc, int): continue
            if acc <= 0 or acc > 10**15: continue
            if is_guest_account(acc):
                self.is_guest = True
                return False
            self.account_id = acc
            self.session_key = res.get(1) or ""
            self.zone_id = self._coerce_zone(res.get(2))
            return True
        if saw_timeout: self.last_login_err = "Login timeout — server didn't respond"
        elif saw_close: self.last_login_err = "Server closed connection"
        else: self.last_login_err = f"Login failed (last PID: {last_pid})"
        return False

    # ---------- GET GAME SERVER ----------
    def get_game_server(self) -> bool:
        for attempt in range(4):
            try:
                self.send_data(5, SdpStruct({
                    0: self.account_id, 1: self.session_key,
                    2: CLIENT_VERSION, 5: self.zone_id, 6: self.channel,
                }))
            except ConnectionError:
                return False
            for _ in range(12):
                pid, res = self.recv_data()
                self._trace("get_server", pid, res)
                if pid in (None, -1): break
                if pid == 20001: continue
                if pid == 6 and res:
                    host, port = self._parse_hostport(res.get(1))
                    if host and port:
                        self.game_host = host; self.game_port = port
                        return True
                    if isinstance(res, dict):
                        for v in res.values():
                            h, p = self._parse_hostport(v)
                            if h and p:
                                self.game_host = h; self.game_port = p
                                return True
            time.sleep(0.5)
        return False

    # ---------- HANDSHAKE ----------
    def connect_to_game_server(self) -> bool:
        self.cleanup()
        self.host, self.port = self.game_host, self.game_port
        self.connect()
        self.send_data(10001, SdpStruct({
            0: self.account_id, 1: self.session_key,
            2: self.zone_id, 4: CLIENT_VERSION,
            13: self.channel, 15: self.device_id,
        }))
        for _ in range(20):
            pid, _ = self.recv_data()
            self._trace("handshake", pid)
            if pid is None or pid == -1: return False
            if pid == 10002: return True
            if pid == 20001: continue
        return False

    # ---------- LOOKUP ----------
    def lookup_player(self, search_value) -> Optional[SdpStruct]:
        for _ in range(4):
            try:
                self.send_data(11153, SdpStruct({1: int(search_value)}))
            except ConnectionError:
                return None
            for _ in range(14):
                pid, res = self.recv_data()
                self._trace("lookup", pid, res)
                if pid == 11154: return res
                if pid in (-1, None): break
                if pid == 20001: continue
            time.sleep(0.5)
        return None

    # ---------- BAN CHECK ----------
    def check_ban_status(self) -> Dict[str, Any]:
        if self.is_guest or is_guest_account(self.account_id):
            return {"banned": False, "reason": "", "remaining": "", "label": "UNREGISTERED"}
        try: self.socket.settimeout(0.2)
        except Exception: pass
        try:
            for _ in range(20):
                try: pid, _r = self.recv_data()
                except Exception: break
                if pid in (-1, None): break
        finally:
            try: self.socket.settimeout(SOCK_READ)
            except Exception: pass
        try:
            self.send_data(10101, SdpStruct({0: 0, 2: 2}))
        except ConnectionError:
            return {"banned": False, "reason": "", "remaining": "", "label": "BAN CHECK FAILED"}
        for _ in range(6):
            pid, res = self.recv_data()
            if pid == 20002:
                return {"banned": False, "reason": "", "remaining": "", "label": "Not Banned"}
            if pid == 20001 and res and isinstance(res, dict) and 0 in res and isinstance(res[0], dict):
                b = res[0]
                reason = b.get("ban_reason", "")
                d = b.get("endtime_day","0"); h = b.get("endtime_hour","0")
                m = b.get("endtime_min","0"); s = b.get("endtime_sec","0")
                try: total = int(d)*86400 + int(h)*3600 + int(m)*60 + int(s)
                except Exception: total = 0
                if reason and str(reason).strip() and total > 0:
                    label = f"Banned | Reason: {reason} | Remaining: {d}d {h}h {m}m {s}s"
                    return {"banned": True, "reason": str(reason),
                            "remaining": f"{d}d {h}h {m}m {s}s", "label": label}
                return {"banned": False, "reason": "", "remaining": "", "label": "Not Banned"}
            if pid in (-1, None): break
        return {"banned": False, "reason": "", "remaining": "", "label": "Not Banned"}


# ---------------- Shared connection setup ----------------
def _setup_connection(device_id: str) -> GameConnection:
    conn = GameConnection(device_id)
    try:
        conn.connect()
    except Exception as e:
        raise ConnectionError(f"Cannot reach login server: {e}")
    if not conn.login_to_login_server():
        if conn.is_guest:
            raise ConnectionError("Guest / unregistered device — no real MLBB account")
        raise ConnectionError(conn.last_login_err or "Login failed")
    if not conn.get_game_server():
        raise ConnectionError("Server resolve failed")
    if not conn.connect_to_game_server():
        raise ConnectionError("Handshake failed")
    return conn


# ---------------- Extract ----------------
def extract_player_data(result) -> Optional[Dict[str, Any]]:
    if not result or 0 not in result: return None
    plist = result.get(0)
    if not isinstance(plist, list) or not plist: return None
    pd = plist[0]
    if not isinstance(pd, dict): return None
    if is_guest_account(pd.get(0)): return None
    try:
        try: skin_count = int(pd.get(83, 0))
        except Exception: skin_count = 0
        try: hero_count = int(pd.get(4, 0))
        except Exception: hero_count = 0
        try: win = int(pd.get(18, 0))
        except Exception: win = 0
        try: loss = int(pd.get(155, 0))
        except Exception: loss = 0
        total = win + loss
        wr = f"{win/total*100:.2f}%" if total > 0 else "N/A"
        loc = None
        ld = pd.get(71)
        if isinstance(ld, list) and len(ld) >= 2: loc = ", ".join(str(x) for x in ld)
        sn = str(pd.get(30, "")).replace("`", "").strip()
        si = str(pd.get(31, "")).strip()
        squad = f"{si} {sn}".strip() if sn else None
        hr = map_rank(pd.get(95)) if pd.get(95) is not None else None
        cr = map_rank(pd.get(8)) if pd.get(8) is not None else None
        cpt = 0
        t136 = pd.get(136, {})
        if isinstance(t136, dict):
            try: cpt = int(t136.get(9, 0))
            except Exception: cpt = 0
        ctier = map_collector_point(cpt) if cpt > 0 else None
        heroes_list = []
        t91 = pd.get(91, [])
        if isinstance(t91, list):
            seen = set()
            for hid in t91:
                try: hi = int(hid)
                except Exception: continue
                if hi in seen: continue
                seen.add(hi); heroes_list.append(hero_name(hi))
                if len(heroes_list) >= 5: break
        return {
            "nickname": pd.get(2, "Unknown"),
            "player_id": pd.get(0, "Unknown"),
            "server": pd.get(1, "Unknown"),
            "level": pd.get(3, 1),
            "skin_count": skin_count, "last_login": fmt_ts(pd.get(5, 0)),
            "last_login_country": str(pd.get(87)) if pd.get(87) else None,
            "create_country": str(pd.get(97)) if pd.get(97) else None,
            "hero_count": hero_count,
            "location": loc, "high_rank": hr, "current_rank": cr,
            "collector_tier": ctier, "collector_point": cpt if cpt > 0 else None,
            "squad": squad, "total_battles": total, "win_rate": wr,
            "hero_history": heroes_list,
        }
    except Exception as e:
        logging.debug("extract error: %s", e)
        return None


# ============================================================
# PUBLIC API
# ============================================================

def check_device_id(device_id: str, retries: int = 3) -> Dict[str, Any]:
    device_id = (device_id or "").strip()
    ok, reason = validate_device_id(device_id)
    if not ok:
        return {"status": "error", "device_id": device_id, "error": f"Invalid format: {reason}"}

    last_err = "unknown"
    for attempt in range(retries):
        conn: Optional[GameConnection] = None
        try:
            conn = _setup_connection(device_id)
            result = conn.lookup_player(conn.account_id)
            conn.cleanup()
            if not result:
                last_err = "No account data returned"
                time.sleep(0.8); continue
            player = extract_player_data(result)
            if not player:
                last_err = "Could not parse player data"
                time.sleep(0.8); continue
            return {"status": "success", "device_id": device_id, "player_data": player}
        except ConnectionError as e:
            last_err = str(e)
            if conn:
                try: conn.cleanup()
                except Exception: pass
            time.sleep(0.8); continue
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            if conn:
                try: conn.cleanup()
                except Exception: pass
            time.sleep(0.8); continue
    return {"status": "error", "device_id": device_id, "error": last_err}


def check_ban_only(device_id: str, retries: int = 3) -> Dict[str, Any]:
    device_id = (device_id or "").strip()
    ok, reason = validate_device_id(device_id)
    if not ok:
        return {"status": "error", "device_id": device_id,
                "error": f"Invalid format: {reason}",
                "banned": False, "label": "INVALID"}
    last_err = "unknown"
    for attempt in range(retries):
        conn: Optional[GameConnection] = None
        try:
            conn = _setup_connection(device_id)
            ban = conn.check_ban_status()
            conn.cleanup()
            return {"status": "success", "device_id": device_id, "ban": ban}
        except ConnectionError as e:
            last_err = str(e)
            if conn:
                try: conn.cleanup()
                except Exception: pass
            time.sleep(0.8); continue
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            if conn:
                try: conn.cleanup()
                except Exception: pass
            time.sleep(0.8); continue
    return {"status": "error", "device_id": device_id, "error": last_err,
            "ban": {"banned": False, "reason": "", "remaining": "", "label": "CHECK FAILED"}}


# ============================================================
# DEBUG PROBE — run this once to see what the server sends
# ============================================================
def debug_probe(device_id: str):
    """
    Diagnostic: prints every packet during login + get_server.
    Usage:
        python -c "from checker_core import debug_probe; debug_probe('and_...')"
    """
    print(f"=== Probing {device_id[:20]}…{device_id[-12:]} ===")
    conn = GameConnection(device_id)
    try:
        conn.connect()
        print("✓ TCP connected to", LOGIN_HOST, ":", LOGIN_PORT)
    except Exception as e:
        print("✗ Connect failed:", e)
        return

    # ---- LOGIN ----
    print("\n--- LOGIN ---")
    conn.send_data(1, SdpStruct({
        0: conn.device_id, 1: conn.auth_str,
        2: CLIENT_VERSION, 3: conn.channel, 4: LANGUAGE,
    }))
    print("sent PID 1; auth_str =", conn.auth_str[:80])
    for i in range(10):
        pid, res = conn.recv_data()
        print(f"  [{i}] pid={pid}", end="")
        if res:
            print(" keys=", sorted(res.keys())[:10])
            for k in sorted(res.keys())[:6]:
                v = repr(res[k])
                print(f"        [{k}] = {v[:80]}")
        else:
            print(" (no payload)")
        if pid in (-1, None): break
        if pid == 2 and res:
            conn.account_id = res.get(0) or 0
            conn.session_key = res.get(1) or ""
            conn.zone_id = conn._coerce_zone(res.get(2))
            print(f"  → account_id={conn.account_id}  zone_id={conn.zone_id}  "
                  f"session_key={conn.session_key[:20]}…")
            break

    if not conn.account_id:
        print("✗ Login failed, aborting.")
        conn.cleanup(); return

    # ---- GET SERVER ----
    print("\n--- GET SERVER ---")
    conn.send_data(5, SdpStruct({
        0: conn.account_id, 1: conn.session_key,
        2: CLIENT_VERSION, 5: conn.zone_id, 6: conn.channel,
    }))
    print(f"sent PID 5; acc={conn.account_id} zone={conn.zone_id}")
    for i in range(10):
        pid, res = conn.recv_data()
        print(f"  [{i}] pid={pid}", end="")
        if res:
            print(" keys=", sorted(res.keys())[:10])
            for k in sorted(res.keys())[:6]:
                v = repr(res[k])
                print(f"        [{k}] = {v[:120]}")
        else:
            print(" (no payload)")
        if pid in (-1, None): break
        if pid == 6 and res: break

    conn.cleanup()
    print("\n=== Trace ===")
    for t in conn.trace:
        print(" ", t)


# ---------------- Helpers ----------------
def generate_device_ids(count: int) -> List[str]:
    out = []
    for _ in range(count):
        imei = "".join(str(random.randint(0, 9)) for _ in range(15))
        md5 = hashlib.md5(imei.encode()).hexdigest()
        aid = "%016x" % random.getrandbits(64)
        adv = str(uuid.UUID(int=random.getrandbits(128)))
        out.append(f"and_{md5}{aid}{adv}")
    return out

def read_ids_from_text(text: str) -> List[str]:
    out = []
    for line in text.splitlines():
        r = line.strip()
        if r and not r.startswith("#"):
            out.append(r)
    return out

def save_line(did: str, p: Dict[str, Any]) -> str:
    hh = p.get("hero_history") or []
    lh = hh[0] if hh else "N/A"
    return (f"Device ID: {did} | Name: {p.get('nickname','N/A')} | Role ID: {p.get('player_id','N/A')} | "
            f"Server ID: {p.get('server','N/A')} | Level: {p.get('level','N/A')} | "
            f"Skin: {p.get('skin_count','N/A')} | Last Login: {p.get('last_login','N/A')} | "
            f"Country: {p.get('last_login_country','N/A')} | Rank: {p.get('current_rank','N/A')} | "
            f"High Rank: {p.get('high_rank','N/A')} | Win Rate: {p.get('win_rate','N/A')} | "
            f"Heroes: {p.get('hero_count',0)} | Matches: {p.get('total_battles',0)} | "
            f"Last Hero: {lh} | Squad: {p.get('squad','—')} | Collector: {p.get('collector_tier','None')} | "
            f"Reg: {p.get('create_country','N/A')}")

def save_ban_line(did: str, ban: Dict[str, Any]) -> str:
    if ban.get("banned"):
        return f"{did} | BANNED | Reason: {ban.get('reason','?')} | Remaining: {ban.get('remaining','?')}"
    return f"{did} | NOT BANNED"