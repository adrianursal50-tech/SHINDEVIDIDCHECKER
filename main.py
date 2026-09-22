# bot.py
"""
🥔 Potato MLBB Telegram Bot — single-engine build.
Imports everything it needs from engine.py.

Commands
  /check <device_id>          single ban check          (1 credit)
  /bulk  (upload .txt)        bulk ban check, live HUD  (1 credit / id)
  /info  <role_id> <zone_id>  full player lookup        (5 credits)
  /redeem <key>               activate key
  /mystats                    credits + expiry
  /admin                      admin panel (OWNER_ID only)

Env: BOT_TOKEN, OWNER_ID  (optional: DB_PATH, BULK_WORKERS, ENGINE_DEBUG)
Deps: python-telegram-bot==21.*
"""
from __future__ import annotations

import asyncio
import html
import io
import logging
import os
import secrets
import sqlite3
import string
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application, ApplicationBuilder, CallbackQueryHandler,
    CommandHandler, ContextTypes, MessageHandler, filters,
)

import engine
from engine import check_device_ban_silent, lookup_player_data

# ── CONFIG ─────────────────────────────────────────────────────────────
BOT_TOKEN        = os.environ.get("BOT_TOKEN", "8702549007:AAHe3d-RSBaYs4wX4D4x4rkLpevipByEPqs").strip()
OWNER_ID         = int(os.environ.get("OWNER_ID", "8621676055"))
DB_PATH          = os.environ.get("DB_PATH", "potato_bot.db")
BULK_WORKERS     = int(os.environ.get("BULK_WORKERS", "20"))
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
PROGRESS_EVERY   = 3.0
SINGLE_COST      = 1
INFO_COST        = 5
RATE_WINDOW      = 10.0
RATE_MAX         = 6

if not BOT_TOKEN:
    raise SystemExit("[FATAL] BOT_TOKEN env var is empty.")
if OWNER_ID == 0:
    raise SystemExit("[FATAL] OWNER_ID env var is empty.")

if os.environ.get("ENGINE_DEBUG", "").strip() in ("1", "true", "yes"):
    engine.DEBUG_MODE = True

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("potato-bot")


# ── DB ─────────────────────────────────────────────────────────────────
_db_lock = threading.Lock()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    with _db_lock, _db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id      INTEGER PRIMARY KEY,
                username     TEXT,
                first_name   TEXT,
                joined_at    INTEGER NOT NULL,
                is_banned    INTEGER NOT NULL DEFAULT 0,
                credits      INTEGER NOT NULL DEFAULT 0,
                expires_at   INTEGER NOT NULL DEFAULT 0,
                total_checks INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS keys (
                key         TEXT PRIMARY KEY,
                credits     INTEGER NOT NULL,
                days        INTEGER NOT NULL,
                created_by  INTEGER NOT NULL,
                created_at  INTEGER NOT NULL,
                redeemed_by INTEGER,
                redeemed_at INTEGER,
                revoked     INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS logs (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      INTEGER NOT NULL,
                user_id INTEGER,
                action  TEXT NOT NULL,
                detail  TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts DESC);
            """
        )


def ensure_user(tg_user) -> sqlite3.Row:
    uid = int(tg_user.id)
    now = int(time.time())
    with _db_lock, _db() as c:
        c.execute("INSERT OR IGNORE INTO users(user_id, username, first_name, joined_at) "
                  "VALUES (?, ?, ?, ?)",
                  (uid, tg_user.username or "", tg_user.first_name or "", now))
        c.execute("UPDATE users SET username=?, first_name=? WHERE user_id=?",
                  (tg_user.username or "", tg_user.first_name or "", uid))
        return c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()


def log_action(user_id: int | None, action: str, detail: str = "") -> None:
    with _db_lock, _db() as c:
        c.execute("INSERT INTO logs(ts, user_id, action, detail) VALUES (?, ?, ?, ?)",
                  (int(time.time()), user_id, action, detail[:500]))


# ── KEYS / CREDITS ─────────────────────────────────────────────────────
_KEY_ALPHABET = string.ascii_uppercase + string.digits


def _gen_key() -> str:
    seg = lambda: "".join(secrets.choice(_KEY_ALPHABET) for _ in range(5))
    return f"POTATO-{seg()}-{seg()}-{seg()}"


def create_keys(credits: int, days: int, count: int, admin_id: int) -> list[str]:
    out: list[str] = []
    now = int(time.time())
    with _db_lock, _db() as c:
        for _ in range(count):
            for _try in range(3):
                k = _gen_key()
                try:
                    c.execute("INSERT INTO keys(key, credits, days, created_by, created_at) "
                              "VALUES (?, ?, ?, ?, ?)",
                              (k, int(credits), int(days), int(admin_id), now))
                    out.append(k)
                    break
                except sqlite3.IntegrityError:
                    continue
    return out


def redeem_key(key: str, user_id: int) -> tuple[bool, str]:
    key = key.strip().upper()
    now = int(time.time())
    with _db_lock, _db() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM keys WHERE key=?", (key,)).fetchone()
        if row is None:
            c.execute("ROLLBACK"); return False, "Key not found."
        if row["revoked"]:
            c.execute("ROLLBACK"); return False, "Key revoked."
        if row["redeemed_by"]:
            c.execute("ROLLBACK"); return False, "Already redeemed."
        expires = now + row["days"] * 86400 if row["days"] > 0 else 0
        c.execute("UPDATE keys SET redeemed_by=?, redeemed_at=? WHERE key=?",
                  (user_id, now, key))
        c.execute("UPDATE users SET credits = credits + ?, "
                  "expires_at = CASE WHEN ? > expires_at THEN ? ELSE expires_at END "
                  "WHERE user_id=?",
                  (row["credits"], expires, expires, user_id))
        c.execute("COMMIT")
    return True, f"+{row['credits']} credits, {row['days']} days"


def revoke_key(key: str) -> bool:
    with _db_lock, _db() as c:
        cur = c.execute("UPDATE keys SET revoked=1 WHERE key=? AND revoked=0",
                        (key.strip().upper(),))
        return cur.rowcount > 0


def add_credits(user_id: int, amount: int) -> bool:
    with _db_lock, _db() as c:
        cur = c.execute("UPDATE users SET credits = credits + ? WHERE user_id=?",
                        (int(amount), int(user_id)))
        return cur.rowcount > 0


def set_ban(user_id: int, banned: bool) -> bool:
    with _db_lock, _db() as c:
        cur = c.execute("UPDATE users SET is_banned=? WHERE user_id=?",
                        (1 if banned else 0, int(user_id)))
        return cur.rowcount > 0


def try_charge(user_id: int, amount: int) -> bool:
    if amount <= 0:
        return True
    with _db_lock, _db() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT credits FROM users WHERE user_id=?", (int(user_id),)).fetchone()
        if row is None or row["credits"] < amount:
            c.execute("ROLLBACK"); return False
        c.execute("UPDATE users SET credits = credits - ?, "
                  "total_checks = total_checks + ? WHERE user_id=?",
                  (amount, amount, int(user_id)))
        c.execute("COMMIT")
    return True


def user_has_access(row: sqlite3.Row) -> bool:
    if row is None or row["is_banned"]:
        return False
    if row["credits"] <= 0:
        return False
    exp = row["expires_at"]
    if exp and exp < int(time.time()):
        return False
    return True


# ── RATE LIMIT ─────────────────────────────────────────────────────────
_rate_lock = threading.Lock()
_rate: dict[int, list[float]] = {}


def rate_ok(uid: int) -> bool:
    now = time.time()
    with _rate_lock:
        bucket = _rate.setdefault(uid, [])
        bucket[:] = [t for t in bucket if now - t < RATE_WINDOW]
        if len(bucket) >= RATE_MAX:
            return False
        bucket.append(now)
        return True


# ── DECORATORS ─────────────────────────────────────────────────────────
def admin_only(func: Callable[..., Awaitable[Any]]):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **kw):
        uid = update.effective_user.id if update.effective_user else 0
        if uid != OWNER_ID:
            if update.effective_message:
                await update.effective_message.reply_text("⛔")
            return
        return await func(update, context, *a, **kw)
    return wrapper


def require_key(func: Callable[..., Awaitable[Any]]):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **kw):
        u = update.effective_user
        if u is None:
            return
        row = ensure_user(u)
        if row["is_banned"]:
            await update.effective_message.reply_text("🚫 You are banned.")
            return
        if not user_has_access(row):
            await update.effective_message.reply_text(
                "🔑 No active key or out of credits.\n"
                "Use <code>/redeem KEY</code>.", parse_mode=ParseMode.HTML)
            return
        return await func(update, context, *a, **kw)
    return wrapper


# ── UI ─────────────────────────────────────────────────────────────────
def esc(s: Any) -> str:
    return html.escape(str(s))


def _bar(done: int, total: int, width: int = 18) -> str:
    if total <= 0:
        return "░" * width
    filled = max(0, min(width, int(round(width * done / total))))
    return "█" * filled + "░" * (width - filled)


def render_bulk_hud(stats: dict, done: bool = False) -> str:
    d = stats["banned"] + stats["clean"] + stats["unknown"]
    t = stats["total"]
    pct = (d / t * 100) if t else 0.0
    elapsed = time.time() - stats["start_ts"]
    eta = ""
    if 0 < d < t and elapsed > 0:
        rate = d / elapsed
        if rate > 0:
            rem = (t - d) / rate
            eta = f"\nETA         : <code>{int(rem // 60):02d}:{int(rem % 60):02d}</code>"
    head = "✅ BULK CHECK DONE" if done else "🔄 BULK CHECK RUNNING"
    return (
        f"<b>{head}</b>\n"
        f"<code>{_bar(d, t)}  {pct:5.1f}%</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Processed   : <code>{d}/{t}</code>\n"
        f"🚫 Banned    : <code>{stats['banned']}</code>\n"
        f"✅ Clean     : <code>{stats['clean']}</code>\n"
        f"❔ Unknown   : <code>{stats['unknown']}</code>\n"
        f"⏱ Elapsed   : <code>{int(elapsed // 60):02d}:{int(elapsed % 60):02d}</code>{eta}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>last:</i> <code>{esc(stats.get('last', '')[:42])}</code>"
    )


# ── BULK WORKER ────────────────────────────────────────────────────────
def _bulk_run(device_ids: list[str], stats: dict, stop_flag: dict) -> None:
    def one(dev: str) -> None:
        try:
            status, payload = check_device_ban_silent(dev)
        except Exception:
            status, payload = "UNKNOWN", dev
        with stats["lock"]:
            if status == "BANNED":
                stats["banned"] += 1
                stats["banned_lines"].append(payload)
            elif status == "CLEAN":
                stats["clean"] += 1
                stats["clean_lines"].append(payload)
            else:
                stats["unknown"] += 1
            stats["last"] = dev

    workers = max(1, min(BULK_WORKERS, len(device_ids) or 1))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bulk") as ex:
        futs = [ex.submit(one, d) for d in device_ids]
        for f in as_completed(futs):
            if stop_flag.get("cancel"):
                break
            try:
                f.result()
            except Exception:
                pass


async def _progress_loop(bot, chat_id: int, msg_id: int, stats: dict,
                         stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=msg_id,
                                        text=render_bulk_hud(stats),
                                        parse_mode=ParseMode.HTML)
        except TelegramError:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=PROGRESS_EVERY)
        except asyncio.TimeoutError:
            continue


def parse_device_lines(raw: bytes) -> list[str]:
    text = raw.decode("utf-8", errors="ignore")
    out, seen = [], set()
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if not (s.startswith("and_") or s.startswith("ios_")):
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


# ── USER COMMANDS ──────────────────────────────────────────────────────
WELCOME = (
    "<b>🥔 Potato MLBB Checker</b>\n\n"
    "<b>User</b>\n"
    "• <code>/check &lt;device_id&gt;</code> — single ban check\n"
    "• <code>/bulk</code> — upload .txt of device IDs\n"
    "• <code>/info &lt;role_id&gt; &lt;zone_id&gt;</code> — player lookup\n"
    "• <code>/redeem &lt;key&gt;</code>\n"
    "• <code>/mystats</code>\n"
    "• <code>/help</code>\n\n"
    "Need a key? Ask the admin."
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_user(update.effective_user)
    await update.effective_message.reply_text(WELCOME, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(WELCOME, parse_mode=ParseMode.HTML)


async def cmd_redeem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_user(update.effective_user)
    if not context.args:
        await update.effective_message.reply_text("Usage: <code>/redeem KEY</code>",
                                                  parse_mode=ParseMode.HTML)
        return
    ok, msg = redeem_key(context.args[0], update.effective_user.id)
    if ok:
        log_action(update.effective_user.id, "redeem", context.args[0])
        await update.effective_message.reply_text(f"✅ Activated. {esc(msg)}",
                                                  parse_mode=ParseMode.HTML)
    else:
        await update.effective_message.reply_text(f"❌ {esc(msg)}")


async def cmd_mystats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    row = ensure_user(update.effective_user)
    exp = "never" if not row["expires_at"] else datetime.fromtimestamp(
        row["expires_at"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    status = "🚫 banned" if row["is_banned"] else "✅ active"
    await update.effective_message.reply_text(
        f"<b>Your stats</b>\n"
        f"Status      : {status}\n"
        f"Credits     : <code>{row['credits']}</code>\n"
        f"Expires     : <code>{esc(exp)}</code>\n"
        f"Total checks: <code>{row['total_checks']}</code>",
        parse_mode=ParseMode.HTML)


@require_key
async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not rate_ok(update.effective_user.id):
        await update.effective_message.reply_text("⏳ Slow down a sec.")
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: <code>/check &lt;device_id&gt;</code>",
                                                  parse_mode=ParseMode.HTML)
        return
    dev = context.args[0].strip()
    if not (dev.startswith("and_") or dev.startswith("ios_")):
        await update.effective_message.reply_text("❌ device_id must start with and_ / ios_",
                                                  parse_mode=ParseMode.HTML)
        return
    if not try_charge(update.effective_user.id, SINGLE_COST):
        await update.effective_message.reply_text("💸 Out of credits.")
        return

    msg = await update.effective_message.reply_text("🔎 Checking…")
    try:
        status, payload = await asyncio.to_thread(check_device_ban_silent, dev)
    except Exception as e:
        await msg.edit_text(f"⚠️ <code>{esc(e)}</code>", parse_mode=ParseMode.HTML)
        return

    head = {"BANNED": "🚫 <b>BANNED</b>", "CLEAN": "✅ <b>CLEAN</b>"}.get(
        status, "❔ <b>UNKNOWN</b>")
    await msg.edit_text(
        f"{head}\n<code>{esc(dev[:60])}</code>\n\n<pre>{esc(payload)}</pre>",
        parse_mode=ParseMode.HTML)
    log_action(update.effective_user.id, "check", f"{dev[:30]}:{status}")


@require_key
async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not rate_ok(update.effective_user.id):
        await update.effective_message.reply_text("⏳ Slow down a sec.")
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text(
            "Usage: <code>/info &lt;role_id&gt; &lt;zone_id&gt;</code>",
            parse_mode=ParseMode.HTML)
        return
    try:
        role_id = int(context.args[0]); zone_id = int(context.args[1])
    except ValueError:
        await update.effective_message.reply_text("❌ role_id and zone_id must be ints.")
        return
    if not try_charge(update.effective_user.id, INFO_COST):
        await update.effective_message.reply_text(f"💸 /info costs {INFO_COST} credits.")
        return

    msg = await update.effective_message.reply_text("🔎 Looking up player…")
    try:
        result = await asyncio.to_thread(lookup_player_data, role_id, zone_id)
    except Exception as e:
        await msg.edit_text(f"⚠️ <code>{esc(e)}</code>", parse_mode=ParseMode.HTML)
        return
    if not isinstance(result, dict) or result.get("status") != "success":
        err = result.get("error", "unknown") if isinstance(result, dict) else "malformed"
        await msg.edit_text(f"❌ Lookup failed: <code>{esc(err)}</code>",
                            parse_mode=ParseMode.HTML)
        return

    p = result["player_data"]
    await msg.edit_text(
        f"<b>🎮 Player Info</b>\n"
        f"Nickname      : <code>{esc(p.get('nickname', '?'))}</code>\n"
        f"Level         : <code>{esc(p.get('level', '?'))}</code>\n"
        f"Country       : <code>{esc(p.get('create_account_country', '?'))}</code>\n"
        f"Created       : <code>{esc(p.get('creation_date', '?'))}</code> ({esc(p.get('account_age','?'))})\n"
        f"Heroes        : <code>{esc(p.get('hero_count', 0))}</code>\n"
        f"Skins         : <code>{esc(p.get('skin_count', 0))}</code>\n"
        f"Current rank  : <code>{esc(p.get('current_rank', '?'))}</code>\n"
        f"Highest rank  : <code>{esc(p.get('high_rank', '?'))}</code>\n"
        f"Collector     : <code>{esc(p.get('collector_tier', '?'))}</code> ({esc(p.get('collector_point', 0))} pts)\n"
        f"Squad         : <code>{esc(p.get('squad', '—'))}</code>\n"
        f"Last login    : <code>{esc(p.get('last_login', '?'))}</code>\n"
        f"Location      : <code>{esc(p.get('location', '?'))}</code>\n"
        f"Battles       : <code>{esc(p.get('total_battles', 0))}</code>  |  "
        f"WR <code>{esc(p.get('win_rate', 'N/A'))}</code>\n"
        f"V2L           : <code>{esc(p.get('v2l_status', 'N/A'))}</code>\n"
        f"Restriction   : <code>{esc(p.get('restriction_flags', 'None'))}</code>",
        parse_mode=ParseMode.HTML)
    log_action(update.effective_user.id, "info", f"{role_id}/{zone_id}")


# ── BULK ───────────────────────────────────────────────────────────────
@require_key
async def cmd_bulk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "📎 Send a <b>.txt</b> file — one device ID per line "
        "(<code>and_…</code> / <code>ios_…</code>). Max 2 MiB.",
        parse_mode=ParseMode.HTML)


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    row = ensure_user(u)
    if row["is_banned"]:
        return
    if not user_has_access(row):
        await update.effective_message.reply_text(
            "🔑 No active key. <code>/redeem KEY</code>", parse_mode=ParseMode.HTML)
        return

    doc = update.effective_message.document
    if doc is None:
        return
    if doc.file_size and doc.file_size > MAX_UPLOAD_BYTES:
        await update.effective_message.reply_text("❌ File too large (2 MiB max).")
        return
    if not (doc.file_name or "").lower().endswith(".txt"):
        await update.effective_message.reply_text("❌ Only .txt files.")
        return

    status_msg = await update.effective_message.reply_text("📥 Downloading…")
    try:
        tg_file = await doc.get_file()
        raw = bytes(await tg_file.download_as_bytearray())
    except Exception as e:
        await status_msg.edit_text(f"⚠️ Download failed: <code>{esc(e)}</code>",
                                   parse_mode=ParseMode.HTML)
        return

    device_ids = parse_device_lines(raw)
    if not device_ids:
        await status_msg.edit_text("❌ No valid device IDs in file.")
        return

    if not try_charge(u.id, len(device_ids)):
        await status_msg.edit_text(
            f"💸 Needs <b>{len(device_ids)}</b> credits; you don't have enough.",
            parse_mode=ParseMode.HTML)
        return

    stats: dict = {
        "total": len(device_ids),
        "banned": 0, "clean": 0, "unknown": 0,
        "banned_lines": [], "clean_lines": [],
        "last": "",
        "start_ts": time.time(),
        "lock": threading.Lock(),
    }
    stop_flag = {"cancel": False}

    await status_msg.edit_text(render_bulk_hud(stats), parse_mode=ParseMode.HTML)
    stop_evt = asyncio.Event()
    progress_task = asyncio.create_task(
        _progress_loop(context.bot, status_msg.chat_id, status_msg.message_id, stats, stop_evt))

    try:
        await asyncio.to_thread(_bulk_run, device_ids, stats, stop_flag)
    except Exception:
        log.exception("bulk worker crashed")
        stop_flag["cancel"] = True
    finally:
        stop_evt.set()
        await progress_task

    try:
        await context.bot.edit_message_text(
            chat_id=status_msg.chat_id, message_id=status_msg.message_id,
            text=render_bulk_hud(stats, done=True), parse_mode=ParseMode.HTML)
    except TelegramError:
        pass

    header = (f"# Bulk check — {datetime.utcnow().isoformat()}Z\n"
              f"# Total {stats['total']}  Banned {stats['banned']}  "
              f"Clean {stats['clean']}  Unknown {stats['unknown']}\n\n")
    if stats["banned_lines"]:
        buf = io.BytesIO((header + "\n".join(stats["banned_lines"])).encode("utf-8"))
        buf.name = f"banned_{int(time.time())}.txt"
        await update.effective_message.reply_document(buf,
            caption=f"🚫 Banned ({stats['banned']})")
    if stats["clean_lines"]:
        buf = io.BytesIO((header + "\n".join(stats["clean_lines"])).encode("utf-8"))
        buf.name = f"clean_{int(time.time())}.txt"
        await update.effective_message.reply_document(buf,
            caption=f"✅ Clean ({stats['clean']})")

    log_action(u.id, "bulk", f"n={stats['total']} b={stats['banned']} c={stats['clean']}")


# ── ADMIN ──────────────────────────────────────────────────────────────
ADMIN_HELP = (
    "<b>🛠 Admin</b>\n"
    "• <code>/genkey &lt;credits&gt; &lt;days&gt; &lt;count&gt;</code>\n"
    "• <code>/revoke &lt;key&gt;</code>\n"
    "• <code>/addcredits &lt;user_id&gt; &lt;n&gt;</code>\n"
    "• <code>/ban &lt;user_id&gt;</code>  <code>/unban &lt;user_id&gt;</code>\n"
    "• <code>/broadcast &lt;msg&gt;</code>\n"
    "• <code>/stats</code>  <code>/recent</code>\n"
    "• <code>/admin</code>"
)


def _admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats", callback_data="a:stats"),
         InlineKeyboardButton("👥 Recent users", callback_data="a:recent")],
        [InlineKeyboardButton("🆕 Latest keys", callback_data="a:keys"),
         InlineKeyboardButton("📘 Commands", callback_data="a:help")],
        [InlineKeyboardButton("❌ Close", callback_data="a:close")],
    ])


async def _admin_stats_text() -> str:
    with _db() as c:
        tot_u = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        act_u = c.execute(
            "SELECT COUNT(*) FROM users WHERE is_banned=0 AND credits>0 "
            "AND (expires_at=0 OR expires_at>?)", (int(time.time()),)).fetchone()[0]
        checks = c.execute("SELECT COALESCE(SUM(total_checks),0) FROM users").fetchone()[0]
        tot_k = c.execute("SELECT COUNT(*) FROM keys").fetchone()[0]
        used = c.execute("SELECT COUNT(*) FROM keys WHERE redeemed_by IS NOT NULL").fetchone()[0]
        rev = c.execute("SELECT COUNT(*) FROM keys WHERE revoked=1").fetchone()[0]
    return (f"<b>📊 Bot stats</b>\n"
            f"Users total    : <code>{tot_u}</code>\n"
            f"Users active   : <code>{act_u}</code>\n"
            f"Checks total   : <code>{checks}</code>\n"
            f"Keys total     : <code>{tot_k}</code>\n"
            f"Keys redeemed  : <code>{used}</code>\n"
            f"Keys revoked   : <code>{rev}</code>")


async def _admin_recent_text() -> str:
    with _db() as c:
        rows = c.execute("SELECT user_id, username, first_name, credits, expires_at, "
                         "is_banned, total_checks FROM users "
                         "ORDER BY joined_at DESC LIMIT 10").fetchall()
    if not rows:
        return "No users yet."
    now = int(time.time())
    lines = ["<b>👥 Last 10 users</b>"]
    for r in rows:
        exp = "—" if not r["expires_at"] else (
            "expired" if r["expires_at"] < now else
            datetime.fromtimestamp(r["expires_at"], timezone.utc).strftime("%Y-%m-%d"))
        flag = " 🚫" if r["is_banned"] else ""
        uname = f"@{r['username']}" if r["username"] else esc(r["first_name"] or "")
        lines.append(f"<code>{r['user_id']}</code> {uname}{flag}\n"
                     f"   cr <code>{r['credits']}</code>  exp <code>{exp}</code>  "
                     f"checks <code>{r['total_checks']}</code>")
    return "\n".join(lines)


async def _admin_keys_text() -> str:
    with _db() as c:
        rows = c.execute("SELECT key, credits, days, redeemed_by, revoked FROM keys "
                         "ORDER BY created_at DESC LIMIT 15").fetchall()
    if not rows:
        return "No keys yet."
    lines = ["<b>🆕 Latest 15 keys</b>"]
    for r in rows:
        state = "revoked" if r["revoked"] else (
            f"used by {r['redeemed_by']}" if r["redeemed_by"] else "unused")
        lines.append(f"<code>{r['key']}</code>\n   +{r['credits']}cr / {r['days']}d  •  {esc(state)}")
    return "\n".join(lines)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        return
    await update.effective_message.reply_text("<b>🛠 Admin panel</b>",
                                              reply_markup=_admin_kb(),
                                              parse_mode=ParseMode.HTML)


async def on_admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("⛔")
        return
    await q.answer()
    data = q.data or ""
    if data == "a:stats":
        await q.edit_message_text(await _admin_stats_text(), reply_markup=_admin_kb(),
                                  parse_mode=ParseMode.HTML)
    elif data == "a:recent":
        await q.edit_message_text(await _admin_recent_text(), reply_markup=_admin_kb(),
                                  parse_mode=ParseMode.HTML)
    elif data == "a:keys":
        await q.edit_message_text(await _admin_keys_text(), reply_markup=_admin_kb(),
                                  parse_mode=ParseMode.HTML)
    elif data == "a:help":
        await q.edit_message_text(ADMIN_HELP, reply_markup=_admin_kb(),
                                  parse_mode=ParseMode.HTML)
    elif data == "a:close":
        try:
            await q.delete_message()
        except TelegramError:
            pass


@admin_only
async def cmd_genkey(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 3:
        await update.effective_message.reply_text(
            "Usage: <code>/genkey &lt;credits&gt; &lt;days&gt; &lt;count&gt;</code>",
            parse_mode=ParseMode.HTML)
        return
    try:
        credits, days, count = int(context.args[0]), int(context.args[1]), int(context.args[2])
    except ValueError:
        await update.effective_message.reply_text("❌ integers only.")
        return
    count = max(1, min(count, 100))
    keys = create_keys(credits, days, count, update.effective_user.id)
    log_action(update.effective_user.id, "genkey", f"c={credits} d={days} n={len(keys)}")
    body = "\n".join(f"<code>{esc(k)}</code>" for k in keys)
    await update.effective_message.reply_text(
        f"✅ Generated <b>{len(keys)}</b> keys (+{credits}cr, {days}d):\n{body}",
        parse_mode=ParseMode.HTML)


@admin_only
async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Usage: <code>/revoke KEY</code>",
                                                  parse_mode=ParseMode.HTML)
        return
    ok = revoke_key(context.args[0])
    await update.effective_message.reply_text("✅ revoked" if ok else "❌ not found / already revoked")


@admin_only
async def cmd_addcredits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await update.effective_message.reply_text(
            "Usage: <code>/addcredits &lt;user_id&gt; &lt;n&gt;</code>",
            parse_mode=ParseMode.HTML)
        return
    try:
        uid, n = int(context.args[0]), int(context.args[1])
    except ValueError:
        await update.effective_message.reply_text("❌ integers only.")
        return
    await update.effective_message.reply_text(
        "✅ done" if add_credits(uid, n) else "❌ user not found")


@admin_only
async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args: return
    try: uid = int(context.args[0])
    except ValueError: return
    await update.effective_message.reply_text("🚫 banned" if set_ban(uid, True) else "❌ not found")


@admin_only
async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args: return
    try: uid = int(context.args[0])
    except ValueError: return
    await update.effective_message.reply_text("✅ unbanned" if set_ban(uid, False) else "❌ not found")


@admin_only
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Usage: <code>/broadcast &lt;msg&gt;</code>",
                                                  parse_mode=ParseMode.HTML)
        return
    text = " ".join(context.args)
    with _db() as c:
        uids = [r["user_id"] for r in c.execute(
            "SELECT user_id FROM users WHERE is_banned=0").fetchall()]
    sent = failed = 0
    await update.effective_message.reply_text(f"📢 Sending to {len(uids)} users…")
    for uid in uids:
        try:
            await context.bot.send_message(uid, text)
            sent += 1
        except TelegramError:
            failed += 1
        await asyncio.sleep(0.05)
    await update.effective_message.reply_text(f"✅ {sent}  ❌ {failed}")


@admin_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(await _admin_stats_text(),
                                              parse_mode=ParseMode.HTML)


@admin_only
async def cmd_recent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(await _admin_recent_text(),
                                              parse_mode=ParseMode.HTML)


# ── ENTRY ──────────────────────────────────────────────────────────────
def build_app() -> Application:
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()

    # user
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("redeem", cmd_redeem))
    app.add_handler(CommandHandler("mystats", cmd_mystats))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("info", cmd_info))
    app.add_handler(CommandHandler("bulk", cmd_bulk))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))

    # admin
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(on_admin_cb, pattern=r"^a:"))
    app.add_handler(CommandHandler("genkey", cmd_genkey))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("addcredits", cmd_addcredits))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("recent", cmd_recent))

    return app


def main() -> None:
    log.info("Bot starting — DB=%s owner=%s", DB_PATH, OWNER_ID)
    app = build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
