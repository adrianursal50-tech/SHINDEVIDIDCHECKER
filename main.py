"""
MLBB Device ID Checker — Telegram Bot
Commands: /start /help /check /bulk /status /stop /redeem /mykey /admin
"""
from __future__ import annotations
import asyncio, html, io, logging, os, secrets, sqlite3, string, time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BotCommand, BotCommandScopeDefault, BufferedInputFile
)

from checker_core import check_device_id, generate_device_ids, read_ids_from_text, save_line

# ================== CONFIG ==================
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_IDS  = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
DB_PATH    = os.environ.get("DB_PATH", "bot.db")
BULK_THREADS = int(os.environ.get("BULK_THREADS", "30"))
UPDATE_INTERVAL = float(os.environ.get("UPDATE_INTERVAL", "2.0"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("mlbb-bot")

if not BOT_TOKEN:
    raise SystemExit("Set BOT_TOKEN env var")

# ================== STORAGE ==================
class Storage:
    def __init__(self, path: str):
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = asyncio.Lock()
        self._init()

    def _init(self):
        c = self._conn.cursor()
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            joined_at INTEGER, is_admin INTEGER DEFAULT 0, is_banned INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS keys (
            key TEXT PRIMARY KEY, duration_hours INTEGER, max_checks INTEGER,
            checks_used INTEGER DEFAULT 0, created_at INTEGER, created_by INTEGER,
            activated_by INTEGER, activated_at INTEGER, is_active INTEGER DEFAULT 1,
            is_revoked INTEGER DEFAULT 0, note TEXT
        );
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, total INTEGER,
            valid INTEGER, invalid INTEGER, filtered INTEGER, started_at INTEGER, ended_at INTEGER
        );
        """)
        self._conn.commit()

    # --- users ---
    def upsert_user(self, uid: int, username: str, first_name: str):
        with self._conn:
            self._conn.execute(
                "INSERT INTO users(user_id,username,first_name,joined_at,is_admin) "
                "VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,"
                "first_name=excluded.first_name",
                (uid, username or "", first_name or "", int(time.time()), 1 if uid in ADMIN_IDS else 0)
            )

    def is_admin(self, uid: int) -> bool:
        if uid in ADMIN_IDS: return True
        r = self._conn.execute("SELECT is_admin FROM users WHERE user_id=?", (uid,)).fetchone()
        return bool(r and r["is_admin"])

    def set_admin(self, uid: int, val: bool):
        with self._conn:
            self._conn.execute("UPDATE users SET is_admin=? WHERE user_id=?", (1 if val else 0, uid))

    def is_banned(self, uid: int) -> bool:
        r = self._conn.execute("SELECT is_banned FROM users WHERE user_id=?", (uid,)).fetchone()
        return bool(r and r["is_banned"])

    def set_banned(self, uid: int, val: bool):
        with self._conn:
            self._conn.execute("UPDATE users SET is_banned=? WHERE user_id=?", (1 if val else 0, uid))

    def list_users(self, limit: int = 50, offset: int = 0):
        return self._conn.execute(
            "SELECT * FROM users ORDER BY joined_at DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()

    def count_users(self) -> int:
        return self._conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]

    # --- keys ---
    @staticmethod
    def _new_key() -> str:
        alpha = string.ascii_uppercase + string.digits
        parts = ["".join(secrets.choice(alpha) for _ in range(4)) for _ in range(4)]
        return "MLBB-" + "-".join(parts)

    def generate_keys(self, count: int, duration_hours: int, max_checks: int,
                      admin_id: int, note: str = "") -> List[str]:
        created = []
        now = int(time.time())
        with self._conn:
            for _ in range(count):
                for attempt in range(5):
                    k = self._new_key()
                    try:
                        self._conn.execute(
                            "INSERT INTO keys(key,duration_hours,max_checks,created_at,created_by,note) "
                            "VALUES(?,?,?,?,?,?)",
                            (k, duration_hours, max_checks, now, admin_id, note)
                        )
                        created.append(k); break
                    except sqlite3.IntegrityError:
                        continue
        return created

    def get_key(self, key: str):
        return self._conn.execute("SELECT * FROM keys WHERE key=?", (key,)).fetchone()

    def revoke_key(self, key: str) -> bool:
        with self._conn:
            cur = self._conn.execute("UPDATE keys SET is_revoked=1,is_active=0 WHERE key=?", (key,))
            return cur.rowcount > 0

    def activate_key(self, uid: int, key: str) -> tuple[bool, str]:
        k = self.get_key(key)
        if not k: return False, "Key not found"
        if k["is_revoked"]: return False, "Key has been revoked"
        if not k["is_active"]: return False, "Key already used"
        if k["activated_by"] and k["activated_by"] != uid:
            return False, "Key already activated by another user"
        now = int(time.time())
        with self._conn:
            self._conn.execute(
                "UPDATE keys SET activated_by=?,activated_at=?,is_active=0 WHERE key=?",
                (uid, now, key)
            )
        # In our schema "is_active" means "still in pool". Once activated → user binds to it.
        # User's active access is derived by: activated_by=uid AND not expired AND checks_used<max_checks
        return True, "OK"

    def user_active_key(self, uid: int):
        now = int(time.time())
        row = self._conn.execute(
            "SELECT * FROM keys WHERE activated_by=? AND is_revoked=0 "
            "AND checks_used < max_checks "
            "AND (activated_at + duration_hours*3600) > ? "
            "ORDER BY activated_at DESC LIMIT 1",
            (uid, now)
        ).fetchone()
        return row

    def user_any_key(self, uid: int):
        return self._conn.execute(
            "SELECT * FROM keys WHERE activated_by=? ORDER BY activated_at DESC LIMIT 1", (uid,)
        ).fetchone()

    def consume_checks(self, key: str, n: int = 1) -> bool:
        with self._conn:
            cur = self._conn.execute(
                "UPDATE keys SET checks_used = checks_used + ? "
                "WHERE key=? AND checks_used + ? <= max_checks",
                (n, key, n)
            )
            return cur.rowcount > 0

    def list_keys(self, limit: int = 20, offset: int = 0, only_unused: bool = False):
        q = "SELECT * FROM keys "
        if only_unused: q += "WHERE activated_by IS NULL AND is_revoked=0 "
        q += "ORDER BY created_at DESC LIMIT ? OFFSET ?"
        return self._conn.execute(q, (limit, offset)).fetchall()

    def count_keys(self) -> int:
        return self._conn.execute("SELECT COUNT(*) c FROM keys").fetchone()["c"]

    def stats(self) -> dict:
        c = self._conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        k = self._conn.execute("SELECT COUNT(*) c FROM keys").fetchone()["c"]
        ku = self._conn.execute("SELECT COUNT(*) c FROM keys WHERE activated_by IS NULL AND is_revoked=0").fetchone()["c"]
        j = self._conn.execute("SELECT COALESCE(SUM(total),0) t, COALESCE(SUM(valid),0) v FROM jobs").fetchone()
        return {"users": c, "keys_total": k, "keys_unused": ku, "checks_total": j["t"], "checks_valid": j["v"]}

    def record_job(self, uid: int, total: int, valid: int, invalid: int, filtered: int,
                   started: int, ended: int):
        with self._conn:
            self._conn.execute(
                "INSERT INTO jobs(user_id,total,valid,invalid,filtered,started_at,ended_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (uid, total, valid, invalid, filtered, started, ended)
            )

storage = Storage(DB_PATH)

# ================== JOB MANAGER ==================
@dataclass
class Job:
    user_id: int
    chat_id: int
    message_id: int
    total: int
    checked: int = 0
    valid: int = 0
    invalid: int = 0
    filtered: int = 0
    stopped: bool = False
    start: float = field(default_factory=time.time)
    last_update: float = 0.0
    latest: List[str] = field(default_factory=list)   # last 3 result lines
    results: List[str] = field(default_factory=list)  # full valid output lines
    input_name: str = "input"

    @property
    def elapsed(self) -> float:
        return max(time.time() - self.start, 0.001)

    @property
    def pct(self) -> float:
        return (self.checked / self.total * 100.0) if self.total else 0.0

    @property
    def speed(self) -> float:
        return self.checked / self.elapsed

jobs: Dict[int, Job] = {}

# ================== UI HELPERS ==================
def progress_bar(pct: float, width: int = 20) -> str:
    filled = int(width * pct / 100.0)
    return "▓" * filled + "░" * (width - filled)

def fmt_result_line(ok: bool, did: str, info: str) -> str:
    tag = "✅" if ok else "❌"
    sid = did[-8:] if len(did) >= 8 else did
    return f"{tag} <code>…{html.escape(sid)}</code> │ {html.escape(info[:60])}"

def render_job(job: Job) -> str:
    pct = job.pct
    bar = progress_bar(pct, 20)
    eta = ""
    if job.speed > 0 and job.checked < job.total:
        remain = (job.total - job.checked) / job.speed
        eta = f"\n⏳ ETA: <b>{remain:.0f}s</b>"
    latest = "\n".join(job.latest[-3:]) if job.latest else "<i>waiting…</i>"
    return (
        f"⚡ <b>SCANNING</b> — <i>{html.escape(job.input_name)}</i>\n"
        f"<code>{bar}</code> <b>{pct:5.1f}%</b>\n"
        f"\n"
        f"📊 <b>Live stats</b>\n"
        f"├ Total: <b>{job.total}</b>\n"
        f"├ Checked: <b>{job.checked}</b>\n"
        f"├ ✅ Valid: <b>{job.valid}</b>\n"
        f"├ ❌ Invalid: <b>{job.invalid}</b>\n"
        f"└ 📂 Filtered (Lv&lt;9): <b>{job.filtered}</b>\n"
        f"\n"
        f"⚡ Speed: <b>{job.speed:.1f}/s</b>   🧵 Threads: <b>{BULK_THREADS}</b>\n"
        f"⏱ Elapsed: <b>{job.elapsed:.1f}s</b>{eta}\n"
        f"\n"
        f"📋 <b>Latest:</b>\n{latest}\n"
        f"\n<i>Send /stop to cancel • /status for snapshot</i>"
    )

async def edit_job(bot: Bot, job: Job):
    try:
        await bot.edit_message_text(
            chat_id=job.chat_id, message_id=job.message_id,
            text=render_job(job), parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.debug("edit failed: %s", e)

# ================== RUNNER ==================
async def run_bulk(bot: Bot, chat_id: int, message_id: int, user_id: int,
                   device_ids: List[str], input_name: str = "input.txt"):
    job = Job(user_id=user_id, chat_id=chat_id, message_id=message_id,
              total=len(device_ids), input_name=input_name)
    jobs[user_id] = job
    await edit_job(bot, job)

    loop = asyncio.get_running_loop()
    sem = asyncio.Semaphore(BULK_THREADS)

    async def one(did: str):
        if job.stopped: return
        async with sem:
            if job.stopped: return
            try:
                res = await asyncio.to_thread(check_device_id, did)
            except Exception as e:
                res = {"status": "error", "device_id": did, "error": str(e)}
            # Update job
            job.checked += 1
            if res.get("status") == "success":
                p = res["player_data"]
                try: lvl = int(p.get("level", 0))
                except Exception: lvl = 0
                if lvl < 9:
                    job.filtered += 1
                    job.latest.append(fmt_result_line(True, did, f"Filtered Lv{lvl}"))
                else:
                    job.valid += 1
                    line = save_line(did, p)
                    job.results.append(line)
                    info = (f"{p.get('nickname','?')[:14]} Lv:{lvl} "
                            f"🆔{p.get('player_id','?')} 🎨{p.get('skin_count','?')}")
                    job.latest.append(fmt_result_line(True, did, info))
            else:
                job.invalid += 1
                job.latest.append(fmt_result_line(False, did, res.get("error", "unknown")))
            # Throttle edits
            now = time.time()
            if now - job.last_update >= UPDATE_INTERVAL:
                job.last_update = now
                await edit_job(bot, job)

    tasks = [asyncio.create_task(one(did)) for did in device_ids]
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await edit_job(bot, job)
        # Save results file
        if job.results:
            data = ("\n".join(job.results) + "\n").encode("utf-8")
            filename = f"results_{job.user_id}_{int(time.time())}.txt"
            try:
                await bot.send_document(
                    chat_id=chat_id,
                    document=BufferedInputFile(data, filename=filename),
                    caption=(f"✅ <b>Done</b>\n"
                             f"Valid: <b>{job.valid}</b> │ Invalid: <b>{job.invalid}</b> │ "
                             f"Filtered: <b>{job.filtered}</b>\n"
                             f"⏱ {job.elapsed:.1f}s"),
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                log.warning("send_document failed: %s", e)
        # Record job
        storage.record_job(user_id, job.total, job.valid, job.invalid, job.filtered,
                           int(job.start), int(time.time()))
        # Consume key
        k = storage.user_active_key(user_id)
        if k:
            storage.consume_checks(k["key"], n=job.total)
        # Cleanup after some time
        await asyncio.sleep(60)
        jobs.pop(user_id, None)

# ================== ACCESS CHECK ==================
def has_access(uid: int) -> tuple[bool, str]:
    if uid in ADMIN_IDS: return True, ""
    if storage.is_admin(uid): return True, ""
    if storage.is_banned(uid): return False, "🚫 You are banned."
    k = storage.user_active_key(uid)
    if not k: return False, ("🚫 No active key.\n"
                              "Use <code>/redeem &lt;key&gt;</code> to activate one.")
    if k["checks_used"] >= k["max_checks"]:
        return False, "🚫 Key exhausted."
    return True, ""

# ================== BOT / DISPATCHER ==================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# ================== FSM ==================
class KeyGen(StatesGroup):
    count = State(); duration = State(); max_checks = State(); note = State()
class RevokeFSM(StatesGroup):
    key = State()
class BroadcastFSM(StatesGroup):
    msg = State()
class AdminFSM(StatesGroup):
    uid = State()

# ================== KEYBOARDS ==================
def kb_main(is_admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🔍 Check (single)", callback_data="ui:single"),
         InlineKeyboardButton(text="📦 Bulk check",     callback_data="ui:bulk")],
        [InlineKeyboardButton(text="📊 Status",         callback_data="ui:status"),
         InlineKeyboardButton(text="🛑 Stop",           callback_data="ui:stop")],
        [InlineKeyboardButton(text="🔑 Redeem key",     callback_data="ui:redeem"),
         InlineKeyboardButton(text="👤 My key",         callback_data="ui:mykey")],
        [InlineKeyboardButton(text="ℹ️ Help",           callback_data="ui:help")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="🛠 Admin panel", callback_data="admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Generate keys", callback_data="admin:gen"),
         InlineKeyboardButton(text="📋 List keys",     callback_data="admin:list")],
        [InlineKeyboardButton(text="🗑 Revoke key",    callback_data="admin:revoke")],
        [InlineKeyboardButton(text="👥 Users",         callback_data="admin:users"),
         InlineKeyboardButton(text="📈 Bot stats",     callback_data="admin:stats")],
        [InlineKeyboardButton(text="📢 Broadcast",     callback_data="admin:bc"),
         InlineKeyboardButton(text="👑 Set admin",     callback_data="admin:setadmin")],
        [InlineKeyboardButton(text="⬅️ Back",          callback_data="admin:back")],
    ])

# ================== COMMANDS ==================
@router.message(CommandStart())
async def cmd_start(msg: Message, command: CommandObject):
    u = msg.from_user
    storage.upsert_user(u.id, u.username or "", u.first_name or "")
    is_admin = storage.is_admin(u.id)
    welcome = (
        f"👋 <b>Welcome, {html.escape(u.first_name or 'user')}!</b>\n\n"
        "🤖 <b>MLBB Device ID Checker Bot</b>\n"
        "Fast, threaded, with live stats.\n\n"
        "<b>Quick commands</b>\n"
        "• <code>/check &lt;device_id&gt;</code> — single check\n"
        "• <code>/bulk</code> — send a .txt file with device IDs\n"
        "• <code>/status</code> — current job progress\n"
        "• <code>/stop</code> — cancel running job\n"
        "• <code>/redeem &lt;key&gt;</code> — activate access key\n"
        "• <code>/mykey</code> — see your active key"
    )
    if is_admin:
        welcome += "\n\n🛠 Admin: <code>/admin</code>"
    await msg.answer(welcome, reply_markup=kb_main(is_admin))

@router.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer(
        "📖 <b>Help</b>\n\n"
        "🔍 <b>Single check</b>: <code>/check and_xxxxxxxxxxxxxxxx</code>\n"
        "📦 <b>Bulk check</b>: <code>/bulk</code>, then send a <b>.txt</b> file — "
        f"one device ID per line. Runs with <b>{BULK_THREADS}</b> threads.\n"
        "📊 <b>/status</b> — snapshot of the current job\n"
        "🛑 <b>/stop</b> — cancel the current job\n"
        "🔑 <b>/redeem KEY</b> — activate a key\n"
        "👤 <b>/mykey</b> — your key status\n",
    )

@router.message(Command("mykey"))
async def cmd_mykey(msg: Message):
    k = storage.user_any_key(msg.from_user.id)
    if not k:
        return await msg.answer("🚫 You have no key yet. Use <code>/redeem &lt;key&gt;</code>.")
    now = int(time.time())
    exp = k["activated_at"] + k["duration_hours"] * 3600 if k["activated_at"] else 0
    left = max(exp - now, 0)
    status = "🟢 active" if storage.user_active_key(msg.from_user.id) else "🔴 expired/exhausted"
    await msg.answer(
        f"🔑 <b>Your key</b>\n"
        f"<code>{k['key']}</code>\n"
        f"Status: <b>{status}</b>\n"
        f"Used: <b>{k['checks_used']}</b> / <b>{k['max_checks']}</b>\n"
        f"Duration: <b>{k['duration_hours']}h</b>\n"
        f"Expires in: <b>{left//3600}h {(left%3600)//60}m</b>"
    )

@router.message(Command("redeem"))
async def cmd_redeem(msg: Message, command: CommandObject):
    storage.upsert_user(msg.from_user.id, msg.from_user.username or "", msg.from_user.first_name or "")
    key = (command.args or "").strip().upper()
    if not key:
        return await msg.answer("Usage: <code>/redeem MLBB-XXXX-XXXX-XXXX-XXXX</code>")
    ok, reason = storage.activate_key(msg.from_user.id, key)
    if not ok:
        return await msg.answer(f"❌ {reason}")
    await msg.answer("✅ Key activated! You can now use <code>/check</code> and <code>/bulk</code>.")

@router.message(Command("check"))
async def cmd_check(msg: Message, command: CommandObject):
    storage.upsert_user(msg.from_user.id, msg.from_user.username or "", msg.from_user.first_name or "")
    ok, reason = has_access(msg.from_user.id)
    if not ok: return await msg.answer(reason)
    did = (command.args or "").strip()
    if not did:
        return await msg.answer("Usage: <code>/check &lt;device_id&gt;</code>")

    ph = await msg.answer("🔍 <i>Checking…</i>")
    res = await asyncio.to_thread(check_device_id, did)
    if res.get("status") != "success":
        return await ph.edit_text(f"❌ <b>Invalid</b>\n<code>{html.escape(did[-12:])}</code>\n"
                                  f"<i>{html.escape(res.get('error','unknown'))}</i>")
    p = res["player_data"]
    # Consume 1 check from key
    k = storage.user_active_key(msg.from_user.id)
    if k: storage.consume_checks(k["key"], 1)
    text = (
        f"✅ <b>VALID</b>\n"
        f"🆔 Device: <code>…{html.escape(did[-12:])}</code>\n"
        f"👤 Nickname: <b>{html.escape(str(p.get('nickname','?')))}</b>\n"
        f"🎮 Role ID: <code>{p.get('player_id','?')}</code>\n"
        f"🌐 Server: <b>{p.get('server','?')}</b>\n"
        f"📊 Level: <b>{p.get('level','?')}</b>\n"
        f"🎨 Skins: <b>{p.get('skin_count','?')}</b>\n"
        f"🏆 Rank: <b>{html.escape(str(p.get('current_rank','?')))}</b>\n"
        f"⭐ High: <b>{html.escape(str(p.get('high_rank','?')))}</b>\n"
        f"⛔ Ban: <b>{html.escape(str(p.get('ban_status','?')))}</b>\n"
        f"📅 Last login: <b>{html.escape(str(p.get('last_login','?')))}</b>\n"
        f"🌍 Country: <b>{html.escape(str(p.get('last_login_country','?')))}</b>\n"
        f"🏅 Collector: <b>{html.escape(str(p.get('collector_tier','None')))}</b>"
    )
    await ph.edit_text(text)

@router.message(Command("bulk"))
async def cmd_bulk(msg: Message):
    storage.upsert_user(msg.from_user.id, msg.from_user.username or "", msg.from_user.first_name or "")
    ok, reason = has_access(msg.from_user.id)
    if not ok: return await msg.answer(reason)
    await msg.answer(
        f"📦 <b>Bulk mode</b>\n"
        f"Send a <b>.txt</b> file with one device ID per line.\n"
        f"Running with <b>{BULK_THREADS}</b> threads."
    )

@router.message(F.document)
async def on_document(msg: Message):
    storage.upsert_user(msg.from_user.id, msg.from_user.username or "", msg.from_user.first_name or "")
    ok, reason = has_access(msg.from_user.id)
    if not ok: return await msg.answer(reason)

    if msg.from_user.id in jobs and not jobs[msg.from_user.id].stopped:
        return await msg.answer("⚠️ You already have a job running. Use /stop first.")

    doc = msg.document
    if not doc.file_name.lower().endswith(".txt"):
        return await msg.answer("❌ Only .txt files are supported.")

    # Download
    f = await bot.get_file(doc.file_id)
    buf = io.BytesIO()
    await bot.download_file(f.file_path, buf)
    text = buf.getvalue().decode("utf-8", errors="ignore")
    ids = read_ids_from_text(text)
    if not ids:
        return await msg.answer("❌ No device IDs found in file.")
    if len(ids) > 10000:
        return await msg.answer(f"❌ Too many IDs ({len(ids)}). Max 10000 per job.")

    ph = await msg.answer(f"🚀 Starting bulk check of <b>{len(ids)}</b> IDs…")
    # Fire and forget — long-running
    asyncio.create_task(run_bulk(bot, msg.chat.id, ph.message_id,
                                 msg.from_user.id, ids, doc.file_name))

@router.message(Command("status"))
async def cmd_status(msg: Message):
    job = jobs.get(msg.from_user.id)
    if not job:
        return await msg.answer("📭 No job running.")
    await msg.answer(render_job(job))

@router.message(Command("stop"))
async def cmd_stop(msg: Message):
    job = jobs.get(msg.from_user.id)
    if not job:
        return await msg.answer("📭 No job running.")
    job.stopped = True
    await msg.answer("🛑 Stop requested — finishing running tasks…")

# ================== ADMIN ==================
@router.message(Command("admin"))
async def cmd_admin(msg: Message):
    if not storage.is_admin(msg.from_user.id):
        return await msg.answer("🚫 Admin only.")
    s = storage.stats()
    await msg.answer(
        f"🛠 <b>Admin panel</b>\n\n"
        f"👥 Users: <b>{s['users']}</b>\n"
        f"🔑 Keys: <b>{s['keys_total']}</b> (unused: <b>{s['keys_unused']}</b>)\n"
        f"📊 Checks total: <b>{s['checks_total']}</b> (valid: <b>{s['checks_valid']}</b>)\n"
        f"🧵 Threads: <b>{BULK_THREADS}</b>",
        reply_markup=kb_admin(),
    )

@router.callback_query(F.data == "admin:home")
async def cb_admin_home(cq: CallbackQuery):
    if not storage.is_admin(cq.from_user.id):
        return await cq.answer("Admin only", show_alert=True)
    await cq.message.edit_text("🛠 <b>Admin panel</b>", reply_markup=kb_admin())
    await cq.answer()

@router.callback_query(F.data == "admin:back")
async def cb_admin_back(cq: CallbackQuery):
    if not storage.is_admin(cq.from_user.id):
        return await cq.answer("Admin only", show_alert=True)
    await cq.message.edit_text("🏠 <b>Main menu</b>", reply_markup=kb_main(True))
    await cq.answer()

@router.callback_query(F.data == "admin:gen")
async def cb_admin_gen(cq: CallbackQuery, state: FSMContext):
    if not storage.is_admin(cq.from_user.id):
        return await cq.answer("Admin only", show_alert=True)
    await state.set_state(KeyGen.count)
    await cq.message.answer("🔢 How many keys to generate? (1–500)")
    await cq.answer()

@router.message(KeyGen.count)
async def kg_count(msg: Message, state: FSMContext):
    try:
        n = int(msg.text.strip())
        assert 1 <= n <= 500
    except Exception:
        return await msg.answer("❌ Enter a number 1–500.")
    await state.update_data(count=n)
    await state.set_state(KeyGen.duration)
    await msg.answer("⏱ Duration in hours (e.g. 24, 168):")

@router.message(KeyGen.duration)
async def kg_duration(msg: Message, state: FSMContext):
    try:
        h = int(msg.text.strip())
        assert 1 <= h <= 24 * 365
    except Exception:
        return await msg.answer("❌ Enter hours 1–8760.")
    await state.update_data(duration=h)
    await state.set_state(KeyGen.max_checks)
    await msg.answer("🔢 Max checks per key:")

@router.message(KeyGen.max_checks)
async def kg_max(msg: Message, state: FSMContext):
    try:
        m = int(msg.text.strip())
        assert 1 <= m <= 1_000_000
    except Exception:
        return await msg.answer("❌ Enter max checks (1–1000000).")
    await state.update_data(max_checks=m)
    await state.set_state(KeyGen.note)
    await msg.answer("📝 Note (or '-' to skip):")

@router.message(KeyGen.note)
async def kg_note(msg: Message, state: FSMContext):
    data = await state.get_data()
    note = "" if msg.text.strip() == "-" else msg.text.strip()[:80]
    keys = storage.generate_keys(data["count"], data["duration"], data["max_checks"],
                                 msg.from_user.id, note)
    await state.clear()
    body = "\n".join(f"<code>{k}</code>" for k in keys)
    out = f"✅ Generated <b>{len(keys)}</b> keys\n\n{body}"
    if len(out) > 3500:
        buf = io.BytesIO(("\n".join(keys) + "\n").encode())
        await msg.answer_document(BufferedInputFile(buf.getvalue(), filename="keys.txt"),
                                  caption=f"✅ {len(keys)} keys generated")
    else:
        await msg.answer(out)

@router.callback_query(F.data == "admin:list")
async def cb_admin_list(cq: CallbackQuery):
    if not storage.is_admin(cq.from_user.id):
        return await cq.answer("Admin only", show_alert=True)
    rows = storage.list_keys(limit=20)
    if not rows:
        return await cq.answer("No keys", show_alert=True)
    lines = ["📋 <b>Recent keys</b>\n"]
    for r in rows:
        st = "🟢" if r["activated_by"] is None and not r["is_revoked"] else ("🔴" if r["is_revoked"] else "🔵")
        exp = r["activated_at"] + r["duration_hours"] * 3600 if r["activated_at"] else 0
        lines.append(
            f"{st} <code>{r['key']}</code> "
            f"| {r['checks_used']}/{r['max_checks']} "
            f"| {r['duration_hours']}h "
            + (f"| by <code>{r['activated_by']}</code>" if r["activated_by"] else "")
        )
    await cq.message.answer("\n".join(lines))
    await cq.answer()

@router.callback_query(F.data == "admin:revoke")
async def cb_admin_revoke(cq: CallbackQuery, state: FSMContext):
    if not storage.is_admin(cq.from_user.id):
        return await cq.answer("Admin only", show_alert=True)
    await state.set_state(RevokeFSM.key)
    await cq.message.answer("🗑 Send the key to revoke:")
    await cq.answer()

@router.message(RevokeFSM.key)
async def rv_key(msg: Message, state: FSMContext):
    k = msg.text.strip().upper()
    ok = storage.revoke_key(k)
    await state.clear()
    await msg.answer("✅ Revoked." if ok else "❌ Key not found.")

@router.callback_query(F.data == "admin:users")
async def cb_admin_users(cq: CallbackQuery):
    if not storage.is_admin(cq.from_user.id):
        return await cq.answer("Admin only", show_alert=True)
    rows = storage.list_users(limit=30)
    lines = [f"👥 <b>Users</b> (showing {len(rows)} / {storage.count_users()})\n"]
    for r in rows:
        flags = []
        if r["is_admin"]: flags.append("👑")
        if r["is_banned"]: flags.append("🚫")
        uname = f"@{r['username']}" if r["username"] else "—"
        lines.append(f"<code>{r['user_id']}</code> {html.escape(r['first_name'] or '')} {uname} {' '.join(flags)}")
    await cq.message.answer("\n".join(lines))
    await cq.answer()

@router.callback_query(F.data == "admin:stats")
async def cb_admin_stats(cq: CallbackQuery):
    if not storage.is_admin(cq.from_user.id):
        return await cq.answer("Admin only", show_alert=True)
    s = storage.stats()
    await cq.message.answer(
        f"📈 <b>Bot stats</b>\n"
        f"👥 Users: <b>{s['users']}</b>\n"
        f"🔑 Keys: <b>{s['keys_total']}</b> (unused: <b>{s['keys_unused']}</b>)\n"
        f"🧪 Checks: <b>{s['checks_total']}</b>\n"
        f"✅ Valid: <b>{s['checks_valid']}</b>\n"
        f"🧵 Active jobs: <b>{len(jobs)}</b>"
    )
    await cq.answer()

@router.callback_query(F.data == "admin:bc")
async def cb_admin_bc(cq: CallbackQuery, state: FSMContext):
    if not storage.is_admin(cq.from_user.id):
        return await cq.answer("Admin only", show_alert=True)
    await state.set_state(BroadcastFSM.msg)
    await cq.message.answer("📢 Send broadcast text:")
    await cq.answer()

@router.message(BroadcastFSM.msg)
async def bc_send(msg: Message, state: FSMContext):
    await state.clear()
    users = storage.list_users(limit=100000)
    ok = fail = 0
    for u in users:
        try:
            await bot.send_message(u["user_id"], f"📢 <b>Broadcast</b>\n\n{msg.html_text}")
            ok += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1
    await msg.answer(f"✅ Sent: {ok} | ❌ Failed: {fail}")

@router.callback_query(F.data == "admin:setadmin")
async def cb_admin_setadmin(cq: CallbackQuery, state: FSMContext):
    if not storage.is_admin(cq.from_user.id):
        return await cq.answer("Admin only", show_alert=True)
    await state.set_state(AdminFSM.uid)
    await cq.message.answer("👑 Send user_id to toggle admin:")
    await cq.answer()

@router.message(AdminFSM.uid)
async def ad_uid(msg: Message, state: FSMContext):
    await state.clear()
    try: uid = int(msg.text.strip())
    except Exception: return await msg.answer("❌ Invalid ID.")
    new = not storage.is_admin(uid)
    storage.set_admin(uid, new)
    await msg.answer(f"👑 User <code>{uid}</code> admin = <b>{new}</b>")

# ================== UI CALLBACKS ==================
@router.callback_query(F.data == "ui:help")
async def cb_ui_help(cq: CallbackQuery):
    await cq.message.answer("Send /help for commands.")
    await cq.answer()

@router.callback_query(F.data == "ui:status")
async def cb_ui_status(cq: CallbackQuery):
    job = jobs.get(cq.from_user.id)
    if not job:
        return await cq.answer("No job running", show_alert=True)
    await cq.message.answer(render_job(job))
    await cq.answer()

@router.callback_query(F.data == "ui:stop")
async def cb_ui_stop(cq: CallbackQuery):
    job = jobs.get(cq.from_user.id)
    if not job:
        return await cq.answer("No job running", show_alert=True)
    job.stopped = True
    await cq.answer("🛑 Stopping…", show_alert=True)

@router.callback_query(F.data == "ui:redeem")
async def cb_ui_redeem(cq: CallbackQuery):
    await cq.message.answer("Use <code>/redeem MLBB-XXXX-XXXX-XXXX-XXXX</code>")
    await cq.answer()

@router.callback_query(F.data == "ui:mykey")
async def cb_ui_mykey(cq: CallbackQuery):
    k = storage.user_any_key(cq.from_user.id)
    if not k:
        return await cq.answer("No key", show_alert=True)
    now = int(time.time()); exp = k["activated_at"] + k["duration_hours"] * 3600 if k["activated_at"] else 0
    left = max(exp - now, 0)
    await cq.message.answer(
        f"🔑 <code>{k['key']}</code>\n"
        f"Used: <b>{k['checks_used']}/{k['max_checks']}</b>\n"
        f"Left: <b>{left//3600}h {(left%3600)//60}m</b>"
    )
    await cq.answer()

@router.callback_query(F.data == "ui:single")
async def cb_ui_single(cq: CallbackQuery):
    await cq.message.answer("Use <code>/check &lt;device_id&gt;</code>")
    await cq.answer()

@router.callback_query(F.data == "ui:bulk")
async def cb_ui_bulk(cq: CallbackQuery):
    await cq.message.answer("Use <code>/bulk</code>, then send a .txt file.")
    await cq.answer()

# ================== MAIN ==================
async def set_commands():
    cmds = [
        BotCommand(command="start",  description="Start"),
        BotCommand(command="help",   description="Help"),
        BotCommand(command="check",  description="Single check: /check <device_id>"),
        BotCommand(command="bulk",   description="Bulk check via .txt file"),
        BotCommand(command="status", description="Current job status"),
        BotCommand(command="stop",   description="Stop current job"),
        BotCommand(command="redeem", description="Activate a key"),
        BotCommand(command="mykey",  description="Show my key"),
        BotCommand(command="admin",  description="Admin panel (admins only)"),
    ]
    await bot.set_my_commands(cmds, scope=BotCommandScopeDefault())

async def main():
    await set_commands()
    log.info("Bot started. Threads=%d, DB=%s", BULK_THREADS, DB_PATH)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("bye")