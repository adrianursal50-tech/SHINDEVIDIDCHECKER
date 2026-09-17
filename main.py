"""
MLBB DevID Checker Bot — single + bulk check.
"""
from __future__ import annotations
import asyncio, html, io, logging, os, random, time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, Document
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.constants import ParseMode
from telegram.error import NetworkError
from telegram.request import HTTPXRequest

from checker_core import check_device_id, read_ids_from_text, save_line

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "8702549007:AAHe3d-RSBaYs4wX4D4x4rkLpevipByEPqs").strip()
OWNER_ID    = int(os.environ.get("OWNER_ID", "8621676055") or "0")
# ── 5 threads as requested ──
BULK_THREADS = int(os.environ.get("BULK_THREADS", "5"))
BRAND       = os.environ.get("BRAND", "@SSHIN")
BOT_NAME    = "MLBB DevID Checker"

if not BOT_TOKEN:
    raise SystemExit("Set BOT_TOKEN env var")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)-7s | %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
log = logging.getLogger("bot")

LINE = "━" * 28
LINE_DOT = "┈" * 28

# ─────────────────────────────────────────────
# Live job state (per user)
# ─────────────────────────────────────────────
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
    banned: int = 0
    stopped: bool = False
    start: float = field(default_factory=time.time)
    last_update: float = 0.0
    latest: List[str] = field(default_factory=list)
    results: List[str] = field(default_factory=list)

    @property
    def elapsed(self): return max(time.time() - self.start, 0.001)
    @property
    def pct(self):     return (self.checked / self.total * 100.0) if self.total else 0.0
    @property
    def speed(self):   return self.checked / self.elapsed

jobs: Dict[int, Job] = {}

# ─────────────────────────────────────────────
# Card renderer
# ─────────────────────────────────────────────
def _e(s): return html.escape(str(s if s not in (None, "") else "N/A"))

def render_card(did: str, p: Dict) -> str:
    sb = p.get("skin_breakdown") or {}
    recent = p.get("hero_history") or []
    recent_s = ", ".join(recent[:5]) if recent else "N/A"
    cpt = p.get("collector_point") or 0
    ban_line = f"✅ Not banned"
    if p.get("is_banned"):
        ban_line = f"🚫 <b>{_e(p.get('ban_label'))}</b>"
    return (
        f"◇  <b>DEVICE ID CHECK RESULT</b>  ◇\n{LINE}\n"
        f"┣ 🆔 Device : <code>{_e(did)}</code>\n"
        f"┣ 🎮 Role   : <code>{_e(p.get('player_id'))}</code>\n"
        f"┣ 🌐 Zone   : <code>{_e(p.get('server'))}</code>\n"
        f"┣ 🏷 Nick   : <b>{_e(p.get('nickname'))}</b>\n"
        f"┣ 📈 Level  : <code>{_e(p.get('level'))}</code>\n"
        f"┣ 🦸 Heroes : <code>{_e(p.get('hero_count'))}</code>\n"
        f"┗ 🎨 Skins  : <code>{_e(p.get('skin_count'))}</code>\n"
        f"\n◇  <b>RANKS</b>  ◇\n{LINE}\n"
        f"┣ 🏆 Current: <code>{_e(p.get('current_rank'))}</code>\n"
        f"┣ ⭐ High   : <code>{_e(p.get('high_rank'))}</code>\n"
        f"┣ 💠 Collector: <code>{_e(p.get('collector_tier'))}</code>\n"
        f"┗ 💎 Points : <code>{cpt:,}</code>\n"
        f"\n◇  <b>SKIN BREAKDOWN</b>  ◇\n{LINE}\n"
        f"┣ 💎 Supreme    : <code>{sb.get('Supreme Skins',0)}</code>\n"
        f"┣ 🟣 Grand      : <code>{sb.get('Grand Skins',0)}</code>\n"
        f"┣ 🟡 Exquisite  : <code>{sb.get('Exquisite Skins',0)}</code>\n"
        f"┣ 🔵 Deluxe     : <code>{sb.get('Deluxe Skins',0)}</code>\n"
        f"┣ 🟢 Exceptional: <code>{sb.get('Exceptional Skins',0)}</code>\n"
        f"┗ ⚪ Common     : <code>{sb.get('Common Skins',0)}</code>\n"
        f"\n◇  <b>ACCOUNT</b>  ◇\n{LINE}\n"
        f"┣ 🚫 Ban    : {ban_line}\n"
        f"┣ 📍 Loc    : <code>{_e(p.get('location') or 'NOT FOUND')}</code>\n"
        f"┣ 📅 Created: <code>{_e(p.get('creation_date'))}</code>\n"
        f"┣ ⏰ Login  : <code>{_e(p.get('last_login'))}</code>\n"
        f"┣ 🌍 Country: <code>{_e(p.get('last_login_country'))}</code>\n"
        f"┣ 👥 Squad  : <code>{_e(p.get('squad') or '—')}</code>\n"
        f"┣ 🎯 WR     : <code>{_e(p.get('win_rate'))}</code>\n"
        f"┗ ⚔ Battles: <code>{_e(p.get('total_battles',0))}</code>\n"
        f"\n{LINE_DOT}\n👑 {html.escape(BRAND)}"
    )

# ─────────────────────────────────────────────
# Bulk job renderer
# ─────────────────────────────────────────────
def _bar(pct: float, w: int = 24) -> str:
    f = int(w * pct / 100)
    return "█" * f + "░" * (w - f)

def _fmt_secs(s) -> str:
    s = int(max(0, s))
    if s < 60: return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60: return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"

def render_job(job: Job) -> str:
    bar = _bar(job.pct, 24)
    eta = ""
    if job.speed > 0 and job.checked < job.total:
        eta = f"\n⏳ ETA: <b>{_fmt_secs((job.total-job.checked)/job.speed)}</b>  ·  ⚡ <b>{job.speed:.1f}/s</b>"
    latest = "\n".join(job.latest[-3:]) if job.latest else "<i>waiting…</i>"
    return (
        f"✅  <b>Bulk checking</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<code>{bar}</code> <b>{job.pct:5.1f}%</b>\n\n"
        f"📄  Total:        <b>{job.total:,}</b>\n"
        f"📦  Checked:      <b>{job.checked:,}</b>\n"
        f"✅  Valid:        <b>{job.valid:,}</b>\n"
        f"🚫  Banned:       <b>{job.banned:,}</b>\n"
        f"⚠️  Filtered:     <b>{job.filtered:,}</b>\n"
        f"❌  Invalid:      <b>{job.invalid:,}</b>\n\n"
        f"⏱  Time:  <b>{_fmt_secs(job.elapsed)}</b>{eta}\n\n"
        f"📋  <b>Latest:</b>\n{latest}\n\n"
        f"<i>/stop to cancel · /status for snapshot</i>"
    )

async def edit_job(app: Application, job: Job):
    try:
        await app.bot.edit_message_text(
            chat_id=job.chat_id, message_id=job.message_id,
            text=render_job(job), parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception: pass

# ─────────────────────────────────────────────
# Bulk worker
# ─────────────────────────────────────────────
async def run_bulk(app: Application, chat_id: int, message_id: int, user_id: int,
                   device_ids: List[str], input_name: str = "input.txt"):
    job = Job(user_id=user_id, chat_id=chat_id, message_id=message_id,
              total=len(device_ids))
    jobs[user_id] = job
    await edit_job(app, job)

    sem = asyncio.Semaphore(BULK_THREADS)

    async def one(did: str):
        if job.stopped: return
        async with sem:
            if job.stopped: return
            await asyncio.sleep(0.15 + 0.1 * random.random())
            try:
                res = await asyncio.to_thread(check_device_id, did)
            except Exception as e:
                res = {"status": "error", "device_id": did, "error": str(e)}

            job.checked += 1
            if res.get("status") == "success":
                p = res["player_data"]
                try: lvl = int(p.get("level", 0))
                except Exception: lvl = 0
                if p.get("is_banned"):
                    job.banned += 1
                    job.latest.append(f"🚫 <code>…{html.escape(did[-8:])}</code> │ banned")
                elif lvl < 9:
                    job.filtered += 1
                    job.latest.append(f"⚠️ <code>…{html.escape(did[-8:])}</code> │ Lv{lvl} filtered")
                else:
                    job.valid += 1
                    job.results.append(save_line(did, p))
                    job.latest.append(
                        f"✅ <code>…{html.escape(did[-8:])}</code> │ "
                        f"{html.escape(str(p.get('nickname','?'))[:14])} "
                        f"Lv{lvl} 🎨{p.get('skin_count','?')}"
                    )
            else:
                err = str(res.get("error", "unknown"))
                low = err.lower()
                if "guest" in low or "genuinely unregistered" in low:
                    job.filtered += 1
                    job.latest.append(f"⚠️ <code>…{html.escape(did[-8:])}</code> │ unregistered")
                else:
                    job.invalid += 1
                    job.latest.append(f"❌ <code>…{html.escape(did[-8:])}</code> │ {html.escape(err[:40])}")

            now = time.time()
            if now - job.last_update >= 2.0:
                job.last_update = now
                await edit_job(app, job)

    tasks = [asyncio.create_task(one(d)) for d in device_ids]
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await edit_job(app, job)
        if job.results:
            data = ("\n".join(job.results) + "\n").encode("utf-8")
            try:
                await app.bot.send_document(
                    chat_id=chat_id,
                    document=io.BytesIO(data),
                    filename=f"results_{int(time.time())}.txt",
                    caption=(f"✅ <b>Done</b>\nValid: <b>{job.valid}</b> │ "
                             f"Banned: <b>{job.banned}</b> │ "
                             f"Filtered: <b>{job.filtered}</b> │ "
                             f"Invalid: <b>{job.invalid}</b>\n"
                             f"⏱ {_fmt_secs(job.elapsed)}"),
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                log.warning("send_document failed: %s", e)
        await asyncio.sleep(20)
        jobs.pop(user_id, None)

# ─────────────────────────────────────────────
# Command handlers
# ─────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    txt = (
        f"👋 <b>Welcome, {html.escape(u.first_name or 'user')}!</b>\n\n"
        f"🤖 <b>{BOT_NAME}</b>\n\n"
        "<b>Commands</b>\n"
        "• <code>/check &lt;device_id&gt;</code> — single account info\n"
        "• <code>/bulk</code> — send a .txt file with device IDs\n"
        "• <code>/status</code> — current bulk job progress\n"
        "• <code>/stop</code> — cancel running job\n\n"
        "Send a device ID that starts with <code>and_</code> or <code>ios_</code>."
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)

async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await update.message.reply_text(
            "Usage: <code>/check and_xxxxxxxx</code>", parse_mode=ParseMode.HTML)
    did = ctx.args[0].strip()
    ph = await update.message.reply_text("🔍 <i>Checking… (up to 20s)</i>",
                                          parse_mode=ParseMode.HTML)
    res = await asyncio.to_thread(check_device_id, did)
    if res.get("status") != "success":
        return await ph.edit_text(
            f"❌ <b>Check failed</b>\n"
            f"<code>…{html.escape(did[-12:])}</code>\n\n"
            f"💡 <i>{html.escape(res.get('error','unknown'))}</i>",
            parse_mode=ParseMode.HTML,
        )
    await ph.edit_text(render_card(did, res["player_data"]), parse_mode=ParseMode.HTML)

async def cmd_bulk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📦 <b>Bulk check</b>\n"
        f"Send a <b>.txt</b> file — one device ID per line.\n"
        f"Running with <b>{BULK_THREADS}</b> threads.",
        parse_mode=ParseMode.HTML,
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    job = jobs.get(update.effective_user.id)
    if not job:
        return await update.message.reply_text("📭 No job running.")
    await update.message.reply_text(render_job(job), parse_mode=ParseMode.HTML)

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    job = jobs.get(update.effective_user.id)
    if not job:
        return await update.message.reply_text("📭 No job running.")
    job.stopped = True
    await update.message.reply_text("🛑 Stop requested…")

# ─────────────────────────────────────────────
# Document handler
# ─────────────────────────────────────────────
async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in jobs and not jobs[uid].stopped:
        return await update.message.reply_text("⚠️ A job is already running. /stop first.")

    doc = update.message.document
    if not doc.file_name.lower().endswith(".txt"):
        return await update.message.reply_text("❌ Only .txt files are supported.")

    file = await ctx.bot.get_file(doc.file_id)
    buf = io.BytesIO()
    await file.download_to_memory(buf)
    text = buf.getvalue().decode("utf-8", errors="ignore")
    ids = read_ids_from_text(text)
    if not ids:
        return await update.message.reply_text("❌ No device IDs found in file.")
    if len(ids) > 5000:
        return await update.message.reply_text(f"❌ Too many IDs ({len(ids)}). Max 5000 per job.")

    ph = await update.message.reply_text(
        f"🚀 Starting bulk check of <b>{len(ids)}</b> IDs…",
        parse_mode=ParseMode.HTML,
    )
    asyncio.create_task(run_bulk(
        ctx.application, update.effective_chat.id, ph.message_id,
        uid, ids, doc.file_name,
    ))

# ─────────────────────────────────────────────
# Direct device-id text handler (bonus)
# ─────────────────────────────────────────────
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if txt.startswith(("and_", "ios_")) and len(txt) > 20:
        ctx.args = [txt]
        await cmd_check(update, ctx)

# ─────────────────────────────────────────────
# Post-init + main
# ─────────────────────────────────────────────
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start",  "Start"),
        BotCommand("check",  "Single device check"),
        BotCommand("bulk",   "Bulk check via .txt file"),
        BotCommand("status", "Job status"),
        BotCommand("stop",   "Stop job"),
        BotCommand("help",   "Help"),
    ])
    log.info("Commands registered")

def main():
    req = HTTPXRequest(connect_timeout=60, read_timeout=60,
                       write_timeout=60, pool_timeout=60)
    app = (Application.builder()
           .token(BOT_TOKEN)
           .request(req)
           .post_init(post_init)
           .build())

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("check",  cmd_check))
    app.add_handler(CommandHandler("bulk",   cmd_bulk))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stop",   cmd_stop))

    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("Bot starting… (BULK_THREADS=%d, versions=%s)",
             BULK_THREADS, os.environ.get("MLBB_CLIENT_VERSION", "default list"))
    try:
        app.run_polling(bootstrap_retries=10, allowed_updates=Update.ALL_TYPES)
    except NetworkError as e:
        log.error("Network error: %s — retrying in 5s", e)
        time.sleep(5)
        main()

if __name__ == "__main__":
    main()