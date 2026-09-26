#!/usr/bin/env python3
"""
bot.py — MLBB Telegram bot
Wraps simple.py (ban checker) + info.py (full info extractor).

Install deps:
    pip install python-telegram-bot zstandard pycryptodome colorama requests curl_cffi

Layout (same folder):
    bot.py
    simple.py
    info.py

Retry policy:
    simple.py returns "UNKNOWN" for every transient failure
    (socket timeout, reset, login reject, half-read packet).
    Every UNKNOWN is retried up to MAX_RETRIES_PER_DEVICE (8) with
    exponential backoff + jitter before being written to UNKNOWN.txt.

Commands:
    /start         help
    /stop          stop the running bulk job
    /status        live stats snapshot
    /check <id>    single device check
    /result        resend last zip

Flow:
    .txt upload  →  ban check (100 threads, 8-retry on UNKNOWN)
                 →  if CLEAN: info.lookup_player_data(account_id, zone_id)
                 →  per-device FULL_INFO/<device>.txt
                 →  CLEAN_DEVICES.txt / BANNED.txt / UNKNOWN.txt
                 →  zipped and sent at the end.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ─────────────────────────────────────────────────────────────────────
# Local modules
# ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

try:
    import simple  # ban checker
except Exception as e:
    print(f"[FATAL] import simple.py failed: {e}")
    raise

try:
    import info    # full-info extractor
except Exception as e:
    print(f"[FATAL] import info.py failed: {e}")
    raise

# ─────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────
BOT_TOKEN   = "8702549007:AAHxNCGdtyEdGihy7FfoXhNTtx3mh1UN4W0"
ADMIN_ID    = 8621676055
MAX_THREADS = 100

# Retry policy for UNKNOWN results
MAX_RETRIES_PER_DEVICE = 8
STARTUP_JITTER_MAX     = 1.2   # seconds; spreads 100 threads off t=0

RESULTS_DIR = BASE_DIR / "bot_results"
RESULTS_DIR.mkdir(exist_ok=True)

FILE_LOCK = threading.Lock()
JOBS: dict[int, "Job"] = {}
JOBS_LOCK = threading.Lock()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
)
log = logging.getLogger("mlbb-bot")

# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────
def parse_clean_str(s: str) -> dict:
    """Parse simple.py's format_clean_string() output into a dict."""
    out: dict[str, str] = {}
    for line in (s or "").strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]", "_", s)[:80] or "device"


def make_bar(frac: float, width: int = 20) -> str:
    frac = max(0.0, min(1.0, frac))
    filled = int(width * frac)
    return "█" * filled + "░" * (width - filled)


def is_admin(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id == ADMIN_ID)


def _backoff(attempt: int) -> float:
    """Exponential with jitter. attempt starts at 1. Capped at ~2.4s."""
    base = min(2.0, 0.2 * (2 ** min(attempt - 1, 3)))
    return base + random.uniform(0.0, 0.4)


# ─────────────────────────────────────────────────────────────────────
# Job state
# ─────────────────────────────────────────────────────────────────────
class Job:
    def __init__(self, user_id: int, chat_id: int, devices: list[str]) -> None:
        self.user_id = user_id
        self.chat_id = chat_id
        self.devices = devices
        self.total = len(devices)

        self.processed = 0
        self.clean = 0
        self.banned = 0
        self.unknown = 0
        self.errors = 0
        self.skipped = 0
        self.retry_attempts = 0

        self.stop_event = threading.Event()
        self.started_at = time.time()
        self.finished = False
        self.lock = threading.Lock()

        self.status_msg_id: int | None = None

        stamp = time.strftime("%Y%m%d_%H%M%S")
        self.out_dir = RESULTS_DIR / f"u{user_id}_{stamp}"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.full_dir = self.out_dir / "FULL_INFO"
        self.full_dir.mkdir(exist_ok=True)

        self.clean_txt   = self.out_dir / "CLEAN_DEVICES.txt"
        self.banned_txt  = self.out_dir / "BANNED.txt"
        self.unknown_txt = self.out_dir / "UNKNOWN.txt"
        self.errors_txt  = self.out_dir / "ERRORS.txt"

        self._clean_f   = open(self.clean_txt,   "a", encoding="utf-8")
        self._banned_f  = open(self.banned_txt,  "a", encoding="utf-8")
        self._unknown_f = open(self.unknown_txt, "a", encoding="utf-8")
        self._errors_f  = open(self.errors_txt,  "a", encoding="utf-8")

    def tick(self, kind: str) -> None:
        with self.lock:
            self.processed += 1
            if kind == "clean":
                self.clean += 1
            elif kind == "banned":
                self.banned += 1
            elif kind == "unknown":
                self.unknown += 1
            elif kind == "error":
                self.errors += 1
            elif kind == "skipped":
                self.skipped += 1

    def note_attempt(self) -> None:
        with self.lock:
            self.retry_attempts += 1

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "total": self.total,
                "processed": self.processed,
                "clean": self.clean,
                "banned": self.banned,
                "unknown": self.unknown,
                "errors": self.errors,
                "skipped": self.skipped,
                "retry_attempts": self.retry_attempts,
            }

    def close_writers(self) -> None:
        for f in (self._clean_f, self._banned_f, self._unknown_f, self._errors_f):
            try:
                f.close()
            except Exception:
                pass


def get_job(uid: int) -> Job | None:
    with JOBS_LOCK:
        return JOBS.get(uid)


def set_job(uid: int, job: Job) -> None:
    with JOBS_LOCK:
        JOBS[uid] = job


# ─────────────────────────────────────────────────────────────────────
# Per-device worker — ban check with UNKNOWN retry, then full info
# ─────────────────────────────────────────────────────────────────────
def _try_full_info(aid: int, zid: int) -> dict:
    try:
        return info.lookup_player_data(aid, zid)
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def process_one(device_id: str, job: Job) -> tuple[str, object]:
    """
    kind ∈ {clean, banned, unknown, error, skipped}
    payload: str for banned/unknown/error, dict for clean.

    Retries the ban check on UNKNOWN up to MAX_RETRIES_PER_DEVICE.
    Also retries a "CLEAN but missing ids" response — that means we
    got a partially mangled reply, not a real clean verdict.
    """
    # Spread 100 workers off t=0 so the login server doesn't get slammed.
    time.sleep(random.uniform(0.0, STARTUP_JITTER_MAX))

    if job.stop_event.is_set():
        return ("skipped", None)

    last_result: str | None = None

    for attempt in range(1, MAX_RETRIES_PER_DEVICE + 1):
        if job.stop_event.is_set():
            return ("skipped", None)

        job.note_attempt()

        try:
            status, result_str = simple.check_device_ban_silent(device_id)
        except Exception as e:
            status, result_str = "UNKNOWN", f"{device_id} | EXCEPTION: {type(e).__name__}: {e}"

        last_result = result_str

        # ── Hard verdicts ──────────────────────────────────────────
        if status == "BANNED":
            return ("banned", result_str)

        if status == "CLEAN":
            parsed = parse_clean_str(result_str)
            aid_s = parsed.get("Account ID")
            zid_s = parsed.get("Zone ID")

            if not aid_s or not zid_s:
                # Mangled reply — treat as retriable
                if attempt < MAX_RETRIES_PER_DEVICE:
                    time.sleep(_backoff(attempt))
                    continue
                return ("unknown",
                        f"{device_id} | CLEAN without ids after {attempt} attempts | last={result_str}")

            try:
                aid, zid = int(aid_s), int(zid_s)
            except ValueError:
                if attempt < MAX_RETRIES_PER_DEVICE:
                    time.sleep(_backoff(attempt))
                    continue
                return ("error",
                        f"{device_id} | non-numeric ids aid={aid_s} zid={zid_s}")

            full = _try_full_info(aid, zid)
            return ("clean", {
                "device_id":  device_id,
                "account_id": aid,
                "zone_id":    zid,
                "full_info":  full,
                "attempts":   attempt,
            })

        # ── UNKNOWN → retry ────────────────────────────────────────
        if attempt < MAX_RETRIES_PER_DEVICE:
            time.sleep(_backoff(attempt))
            continue

        return ("unknown",
                f"{device_id} | UNKNOWN after {MAX_RETRIES_PER_DEVICE} attempts | last={last_result}")

    return ("unknown", f"{device_id} | exhausted retries | last={last_result}")


def write_clean(job: Job, payload: dict) -> None:
    dev = payload["device_id"]
    aid = payload["account_id"]
    zid = payload["zone_id"]
    full = payload.get("full_info") or {}
    attempts = payload.get("attempts", 1)

    block = (
        f"Device ID: {dev}\n"
        f"Account ID: {aid}\n"
        f"Zone ID: {zid}\n"
    )
    with FILE_LOCK:
        job._clean_f.write(block + "\n")
        job._clean_f.flush()

    fn = job.full_dir / f"{safe_name(dev)}.txt"
    try:
        with open(fn, "w", encoding="utf-8") as f:
            f.write(f"Device ID:  {dev}\n")
            f.write(f"Account ID: {aid}\n")
            f.write(f"Zone ID:    {zid}\n")
            f.write(f"Attempts:   {attempts}\n")
            f.write("=" * 70 + "\n")

            if full.get("status") == "success":
                pd = full.get("player_data", {}) or {}
                for k, v in pd.items():
                    if isinstance(v, dict):
                        f.write(f"\n{k}:\n")
                        for kk, vv in v.items():
                            f.write(f"  {kk}: {vv}\n")
                    elif isinstance(v, list):
                        f.write(f"{k}: {', '.join(map(str, v))}\n")
                    else:
                        f.write(f"{k}: {v}\n")
            else:
                f.write(f"FULL_INFO_ERROR: {full.get('error', 'unknown')}\n")
    except Exception:
        log.exception(f"write_clean failed for {dev}")


def run_bulk(job: Job) -> None:
    log.info(f"[u{job.user_id}] bulk start: {job.total} devices / {MAX_THREADS} threads / "
             f"{MAX_RETRIES_PER_DEVICE} retries on UNKNOWN")

    def worker(dev: str) -> None:
        kind, payload = process_one(dev, job)

        if kind == "skipped":
            job.tick("skipped")
            return

        if kind == "banned":
            with FILE_LOCK:
                job._banned_f.write(str(payload) + "\n")
                job._banned_f.flush()
            job.tick("banned")
            return

        if kind == "clean":
            write_clean(job, payload)  # type: ignore[arg-type]
            job.tick("clean")
            return

        if kind == "unknown":
            with FILE_LOCK:
                job._unknown_f.write(str(payload) + "\n")
                job._unknown_f.flush()
            job.tick("unknown")
            return

        # error
        with FILE_LOCK:
            job._errors_f.write(str(payload) + "\n")
            job._errors_f.flush()
        job.tick("error")

    try:
        with ThreadPoolExecutor(max_workers=MAX_THREADS) as ex:
            futures = [ex.submit(worker, d) for d in job.devices]
            for _ in as_completed(futures):
                pass
    except Exception:
        log.exception("bulk runner crashed")
    finally:
        job.close_writers()
        job.finished = True
        s = job.snapshot()
        log.info(
            f"[u{job.user_id}] done: clean={s['clean']} banned={s['banned']} "
            f"unknown={s['unknown']} errors={s['errors']} skipped={s['skipped']} "
            f"attempts={s['retry_attempts']}"
        )


# ─────────────────────────────────────────────────────────────────────
# Stats rendering + loop
# ─────────────────────────────────────────────────────────────────────
def render_stats(job: Job, final: bool = False) -> str:
    s = job.snapshot()
    p, t = s["processed"], s["total"]
    frac = (p / t) if t else 0.0
    bar = make_bar(frac)

    elapsed = max(0.0, time.time() - job.started_at)
    rate = (p / elapsed) if elapsed > 0 else 0.0
    eta = ((t - p) / rate) if rate > 0 else 0.0

    if final and job.stop_event.is_set():
        head = "🛑 STOPPED"
    elif final:
        head = "✅ COMPLETE"
    elif job.stop_event.is_set():
        head = "🛑 STOPPING…"
    else:
        head = "⏳ RUNNING"

    lines = [
        f"{head}  {p}/{t}",
        f"[{bar}] {frac * 100:5.1f}%",
        f"🟢 Clean  : {s['clean']}",
        f"🔴 Banned : {s['banned']}",
        f"❓ Unknown: {s['unknown']}",
        f"⚠️ Errors : {s['errors']}",
    ]
    if s["skipped"]:
        lines.append(f"⏭ Skipped: {s['skipped']}")
    lines.append(f"⏱ {elapsed:6.1f}s   🔁 {s['retry_attempts']} attempts")
    if not final and rate > 0:
        lines.append(f"⚡ {rate:5.1f}/s   ETA {eta:4.0f}s")
    return "\n".join(lines)


async def stats_loop(bot, job: Job) -> None:
    while not job.finished:
        try:
            if job.status_msg_id is not None:
                await bot.edit_message_text(
                    chat_id=job.chat_id,
                    message_id=job.status_msg_id,
                    text=render_stats(job),
                )
        except Exception:
            pass
        await asyncio.sleep(2)

    try:
        if job.status_msg_id is not None:
            await bot.edit_message_text(
                chat_id=job.chat_id,
                message_id=job.status_msg_id,
                text=render_stats(job, final=True),
            )
    except Exception:
        pass

    await send_zip(bot, job)


# ─────────────────────────────────────────────────────────────────────
# Result packaging
# ─────────────────────────────────────────────────────────────────────
async def send_zip(bot, job: Job) -> None:
    zip_path = RESULTS_DIR / f"u{job.user_id}_{int(job.started_at)}.zip"
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for f in job.out_dir.rglob("*"):
                if f.is_file():
                    z.write(f, f.relative_to(job.out_dir))
    except Exception as e:
        log.exception("zip failed")
        await bot.send_message(chat_id=job.chat_id, text=f"Zip failed: {e}")
        return

    s = job.snapshot()
    cap = (
        f"📦 Results — {s['total']} devices\n"
        f"🟢 Clean  : {s['clean']}\n"
        f"🔴 Banned : {s['banned']}\n"
        f"❓ Unknown: {s['unknown']}\n"
        f"⚠️ Errors : {s['errors']}\n"
        + (f"⏭ Skipped: {s['skipped']}\n" if s["skipped"] else "")
        + f"🔁 Attempts: {s['retry_attempts']}\n\n"
        f"• CLEAN_DEVICES.txt   (info.py-compatible)\n"
        f"• BANNED.txt\n"
        f"• UNKNOWN.txt         (failed after {MAX_RETRIES_PER_DEVICE} retries)\n"
        f"• ERRORS.txt\n"
        f"• FULL_INFO/<device>.txt  (1 file per clean device)"
    )
    try:
        with open(zip_path, "rb") as fh:
            await bot.send_document(
                chat_id=job.chat_id,
                document=fh,
                filename=f"mlbb_results_{job.user_id}.zip",
                caption=cap,
            )
    except Exception as e:
        log.exception("send_document failed")
        await bot.send_message(chat_id=job.chat_id, text=f"Send failed: {e}")


# ─────────────────────────────────────────────────────────────────────
# Command handlers
# ─────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("⛔ Not authorized.")
        return
    text = (
        "🥔 MLBB Bot — Ban Check + Full Info\n"
        "\n"
        "📄 Send a .txt file (one Device ID per line) to start a bulk check.\n"
        f"⚙️  Threads: {MAX_THREADS}\n"
        f"🔁 UNKNOWN retries per device: {MAX_RETRIES_PER_DEVICE}\n"
        "\n"
        "Commands:\n"
        "  /start              — this help\n"
        "  /stop               — stop the running bulk job\n"
        "  /status             — live stats snapshot\n"
        "  /check <device_id>  — single device check\n"
        "  /result             — resend the last result zip"
    )
    await update.message.reply_text(text)


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    uid = update.effective_user.id
    job = get_job(uid)
    if not job or job.finished:
        await update.message.reply_text("No job running.")
        return
    job.stop_event.set()
    await update.message.reply_text("🛑 Stop requested — in-flight tasks will drain, remaining will skip.")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    uid = update.effective_user.id
    job = get_job(uid)
    if not job:
        await update.message.reply_text("No job running.")
        return
    await update.message.reply_text(render_stats(job, final=job.finished))


async def cmd_result(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    uid = update.effective_user.id
    job = get_job(uid)
    if not job:
        await update.message.reply_text("No previous job.")
        return
    if not job.finished:
        await update.message.reply_text("Still running — try again once finished.")
        return
    await send_zip(ctx.bot, job)


async def _do_single_check(bot, chat_id: int, dev: str) -> None:
    loop = asyncio.get_running_loop()

    msg = await bot.send_message(chat_id=chat_id, text=f"🔍 Checking {dev} …")

    def do_ban_with_retry():
        last = ("UNKNOWN", f"{dev} | no attempt")
        for attempt in range(1, MAX_RETRIES_PER_DEVICE + 1):
            try:
                status, result_str = simple.check_device_ban_silent(dev)
            except Exception as e:
                status, result_str = "UNKNOWN", f"{type(e).__name__}: {e}"
            last = (status, result_str, attempt)
            if status in ("CLEAN", "BANNED"):
                return last
            if attempt < MAX_RETRIES_PER_DEVICE:
                time.sleep(_backoff(attempt))
        return last

    status, result_str, attempts = await loop.run_in_executor(None, do_ban_with_retry)

    if status == "BANNED":
        await msg.edit_text(f"🔴 BANNED (after {attempts} attempt(s))\n\n{result_str}")
        return
    if status != "CLEAN":
        await msg.edit_text(
            f"❓ UNKNOWN after {attempts} attempt(s)\n\n{result_str}"
        )
        return

    parsed = parse_clean_str(result_str)
    aid_s, zid_s = parsed.get("Account ID"), parsed.get("Zone ID")
    if not aid_s or not zid_s:
        await msg.edit_text(f"🟢 CLEAN but ids missing:\n{result_str}")
        return

    try:
        aid, zid = int(aid_s), int(zid_s)
    except ValueError:
        await msg.edit_text(f"🟢 CLEAN but bad ids:\n{result_str}")
        return

    full = await loop.run_in_executor(None, _try_full_info, aid, zid)

    lines = [
        "🟢 CLEAN",
        f"Device ID : {dev}",
        f"Account ID: {aid}",
        f"Zone ID   : {zid}",
        f"Attempts  : {attempts}",
        "",
    ]
    if full.get("status") == "success":
        pd = full.get("player_data", {}) or {}
        for k in (
            "nickname", "level", "hero_count", "skin_count",
            "current_rank", "high_rank", "collector_tier",
            "collector_point", "location", "last_login",
            "creation_date", "win_rate", "total_battles",
        ):
            v = pd.get(k)
            if v is not None:
                lines.append(f"{k}: {v}")
    else:
        lines.append(f"full info error: {full.get('error', 'unknown')}")

    await msg.edit_text("\n".join(lines))


async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /check <device_id>")
        return
    dev = ctx.args[0].strip()
    if not dev:
        await update.message.reply_text("Empty device id.")
        return
    await _do_single_check(ctx.bot, update.effective_chat.id, dev)


# ─────────────────────────────────────────────────────────────────────
# File / text handlers
# ─────────────────────────────────────────────────────────────────────
def _extract_devices(text: str) -> list[str]:
    devices: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#") or line.startswith("//"):
            continue
        if " " in line and "\t" not in line:
            line = line.split(" ", 1)[0]
        if line in seen:
            continue
        seen.add(line)
        devices.append(line)
    return devices


async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    uid = update.effective_user.id

    existing = get_job(uid)
    if existing and not existing.finished:
        await update.message.reply_text("A job is already running. /stop first.")
        return

    doc = update.message.document
    if not doc:
        await update.message.reply_text("Send a .txt file.")
        return
    if not (doc.file_name or "").lower().endswith(".txt"):
        await update.message.reply_text("Only .txt files.")
        return

    try:
        tg_file = await doc.get_file()
        data = await tg_file.download_as_bytearray()
    except Exception as e:
        await update.message.reply_text(f"Download failed: {e}")
        return

    try:
        text = bytes(data).decode("utf-8", errors="ignore")
    except Exception:
        text = bytes(data).decode("latin-1", errors="ignore")

    devices = _extract_devices(text)
    if not devices:
        await update.message.reply_text("No device IDs found in file.")
        return

    status_msg = await update.message.reply_text("🚀 Starting job …")

    job = Job(uid, update.effective_chat.id, devices)
    job.status_msg_id = status_msg.message_id
    set_job(uid, job)

    asyncio.create_task(stats_loop(ctx.bot, job))
    threading.Thread(target=run_bulk, args=(job,), daemon=True).start()


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    if "\n" in text or len(text) > 220:
        await update.message.reply_text(
            "Send a .txt file for bulk, or a single device id as text."
        )
        return
    await _do_single_check(ctx.bot, update.effective_chat.id, text)


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────
def main() -> None:
    log.info("Booting MLBB Telegram bot…")
    log.info(f"Admin ID: {ADMIN_ID} | Threads: {MAX_THREADS} | "
             f"Retries/device: {MAX_RETRIES_PER_DEVICE} | Results: {RESULTS_DIR}")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_start))
    app.add_handler(CommandHandler("stop",   cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("check",  cmd_check))
    app.add_handler(CommandHandler("result", cmd_result))

    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Bot online.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
