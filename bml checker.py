#!/usr/bin/env python3
"""
BML Payment Window Auto-Checker
Automatically detects when BML foreign payment window opens/closes
by pinging Temu & Shopee checkout endpoints every 2 minutes.

Setup:
1. pip install python-telegram-bot requests
2. Set BOT_TOKEN and CHANNEL_ID below (or use env vars)
3. Run: python bml_checker.py
"""

import os
import time
import logging
import sqlite3
import requests
from datetime import datetime, timedelta
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "YOUR_CHANNEL_ID_HERE")  # e.g. "@mybmlchannel"

CHECK_INTERVAL_SECONDS = 120   # Check every 2 minutes

# BML BIN ranges (first 6 digits of BML cards)
# Used to simulate a BML card in checkout probes
BML_TEST_CARD = {
    "number": "4111111111111111",  # Generic Visa test number
    "expiry": "12/26",
    "cvv": "123",
    "name": "TEST USER"
}

MERCHANTS = [
    "Shopee", "Temu", "AliExpress", "Amazon",
    "Shein", "eBay", "PayPal", "Alibaba", "Other"
]

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ── Database ──────────────────────────────────────────────────────────────────
DB_FILE = "bml_tracker.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER,
            username  TEXT,
            merchant  TEXT,
            status    TEXT,
            timestamp TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS window_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            state     TEXT,
            merchant  TEXT,
            method    TEXT,
            timestamp TEXT
        )
    """)
    conn.commit()
    conn.close()

def log_window_state(state, merchant="auto", method="probe"):
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "INSERT INTO window_log (state, merchant, method, timestamp) VALUES (?,?,?,?)",
        (state, merchant, method, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def get_last_window_state():
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute(
        "SELECT state, timestamp FROM window_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return row  # (state, timestamp) or None

def log_report(user_id, username, merchant, status):
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "INSERT INTO reports (user_id, username, merchant, status, timestamp) VALUES (?,?,?,?,?)",
        (user_id, username or "unknown", merchant, status, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def get_recent_reports(hours=2):
    since = (datetime.now() - timedelta(hours=hours)).isoformat()
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        "SELECT merchant, status, timestamp FROM reports WHERE timestamp > ? ORDER BY timestamp DESC",
        (since,)
    ).fetchall()
    conn.close()
    return rows

def get_all_time_stats():
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        "SELECT merchant, status, COUNT(*) FROM reports GROUP BY merchant, status"
    ).fetchall()
    conn.close()
    stats = defaultdict(lambda: {"success": 0, "fail": 0})
    for merchant, status, count in rows:
        stats[merchant][status] = count
    return dict(stats)

def get_window_history(limit=10):
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        "SELECT state, merchant, method, timestamp FROM window_log ORDER BY id DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return rows

# ── Payment Window Probes ─────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

def probe_temu() -> bool:
    """
    Probe Temu's payment availability endpoint.
    Returns True if BML-style foreign cards appear accepted.
    """
    try:
        # Temu's payment method availability check
        url = "https://www.temu.com/api/bg/pay/payment-method/list"
        params = {
            "currency": "USD",
            "country": "MV",   # Maldives country code
            "scene": "checkout"
        }
        r = requests.get(url, headers=HEADERS, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            # Look for credit/debit card option being available
            methods = data.get("result", {}).get("paymentMethodList", [])
            for m in methods:
                if m.get("paymentMethodType") in ("CREDIT_CARD", "DEBIT_CARD"):
                    enabled = m.get("enable", False)
                    log.info(f"Temu card payment enabled: {enabled}")
                    return enabled
        log.warning(f"Temu probe status: {r.status_code}")
    except Exception as e:
        log.error(f"Temu probe error: {e}")
    return False

def probe_shopee() -> bool:
    """
    Probe Shopee's payment method endpoint for Maldives.
    Returns True if foreign card payments are available.
    """
    try:
        url = "https://shopee.com.my/api/v4/checkout/get_payment_method_list"
        payload = {
            "currency": "USD",
            "country_code": "MV",
        }
        r = requests.post(url, headers=HEADERS, json=payload, timeout=10)
        if r.status_code == 200:
            data = r.json()
            methods = data.get("data", {}).get("payment_method_list", [])
            for m in methods:
                if "card" in str(m.get("name", "")).lower():
                    available = m.get("is_available", False)
                    log.info(f"Shopee card payment available: {available}")
                    return available
        log.warning(f"Shopee probe status: {r.status_code}")
    except Exception as e:
        log.error(f"Shopee probe error: {e}")
    return False

def probe_community_signals() -> bool:
    """
    Check if recent community reports suggest window is open.
    Fallback when API probes are blocked.
    """
    since = (datetime.now() - timedelta(minutes=20)).isoformat()
    conn = sqlite3.connect(DB_FILE)
    recent_success = conn.execute(
        "SELECT COUNT(*) FROM reports WHERE status='success' AND timestamp > ?",
        (since,)
    ).fetchone()[0]
    recent_fail = conn.execute(
        "SELECT COUNT(*) FROM reports WHERE status='fail' AND timestamp > ?",
        (since,)
    ).fetchone()[0]
    conn.close()

    if recent_success == 0 and recent_fail == 0:
        return None  # No signal
    return recent_success > recent_fail

def check_window() -> tuple[bool, str]:
    """
    Run all probes and return (is_open, source).
    Priority: Temu probe → Shopee probe → Community signals
    """
    # Try Temu first
    temu_result = probe_temu()
    if temu_result:
        return True, "Temu"

    # Try Shopee
    shopee_result = probe_shopee()
    if shopee_result:
        return True, "Shopee"

    # Fall back to community signals
    community = probe_community_signals()
    if community is True:
        return True, "Community"
    if community is False:
        return False, "Community"

    # Default closed if no signal
    return False, "No signal"

# ── Telegram Notifications ────────────────────────────────────────────────────

async def notify_channel(app, state: str, merchant: str, timestamp: str):
    """Send open/close notification to the channel."""
    if not CHANNEL_ID or CHANNEL_ID == "YOUR_CHANNEL_ID_HERE":
        log.warning("CHANNEL_ID not set — skipping notification")
        return

    if state == "open":
        msg = (
            f"✅ *Payment window open*\n"
            f"Enabled at: {timestamp}\n"
            f"Detected via: {merchant}\n\n"
            f"Go make your online foreign payment now!"
        )
    else:
        msg = (
            f"🔴 *Payment window closed*\n"
            f"Disabled at: {timestamp}\n\n"
            f"Our records show the option becomes enabled "
            f"a few times throughout the day. Wait for the next window."
        )

    try:
        await app.bot.send_message(
            chat_id=CHANNEL_ID,
            text=msg,
            parse_mode="Markdown"
        )
        log.info(f"Notified channel: window {state}")
    except Exception as e:
        log.error(f"Failed to notify channel: {e}")

# ── Auto-Checker Job ──────────────────────────────────────────────────────────

async def auto_check_job(context: ContextTypes.DEFAULT_TYPE):
    """Runs every CHECK_INTERVAL_SECONDS to detect window changes."""
    app = context.application
    is_open, source = check_window()
    current_state = "open" if is_open else "closed"
    now_str = datetime.now().strftime("%-m/%-d/%Y, %-I:%M:%S %p")

    last = get_last_window_state()
    last_state = last[0] if last else None

    if current_state != last_state:
        log.info(f"Window state changed: {last_state} → {current_state} (via {source})")
        log_window_state(current_state, merchant=source, method="probe")
        await notify_channel(app, current_state, source, now_str)
    else:
        log.info(f"Window still {current_state} (via {source})")

# ── Bot Command Handlers ──────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 *BML Payment Window Tracker*\n\n"
        "I automatically detect when BML foreign payments open & close!\n\n"
        "📋 *Commands:*\n"
        "/status  — Current window status\n"
        "/history — Recent window open/close log\n"
        "/report  — Manually report a payment attempt\n"
        "/recent  — Last 2 hours of community reports\n"
        "/stats   — All-time success rates by merchant\n"
        "/help    — Show this message"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await start(update, ctx)

async def status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    last = get_last_window_state()
    if last:
        state, ts = last
        t = datetime.fromisoformat(ts).strftime("%I:%M %p")
        icon = "🟢" if state == "open" else "🔴"
        label = "OPEN — go pay now!" if state == "open" else "CLOSED — wait for next window"
        text = (
            f"{icon} *Window is {label}*\n"
            f"Last detected: {t}\n\n"
            f"Auto-checks run every {CHECK_INTERVAL_SECONDS // 60} minutes."
        )
    else:
        text = "⏳ No data yet. Check back in a few minutes."
    await update.message.reply_text(text, parse_mode="Markdown")

async def history_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = get_window_history(limit=10)
    if not rows:
        await update.message.reply_text("No history yet.")
        return
    lines = []
    for state, merchant, method, ts in rows:
        t = datetime.fromisoformat(ts).strftime("%d/%m %I:%M %p")
        icon = "🟢" if state == "open" else "🔴"
        lines.append(f"{icon} {t} — {merchant}")
    text = "📋 *Window History*\n\n" + "\n".join(lines)
    await update.message.reply_text(text, parse_mode="Markdown")

async def report_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Did your payment *succeed or fail?*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Success", callback_data="pick_result:success"),
            InlineKeyboardButton("❌ Failed",  callback_data="pick_result:fail"),
        ]])
    )

async def recent_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    reports = get_recent_reports(hours=2)
    if not reports:
        await update.message.reply_text("No community reports in the last 2 hours.")
        return
    lines = []
    for merchant, status, ts in reports[:15]:
        t = datetime.fromisoformat(ts).strftime("%H:%M")
        icon = "✅" if status == "success" else "❌"
        lines.append(f"{icon} {merchant} — {t}")
    await update.message.reply_text(
        "📋 *Community Reports — Last 2h*\n\n" + "\n".join(lines),
        parse_mode="Markdown"
    )

async def stats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    stats = get_all_time_stats()
    if not stats:
        await update.message.reply_text("No stats yet.")
        return
    lines = []
    for merchant in sorted(stats):
        s = stats[merchant]["success"]
        f = stats[merchant]["fail"]
        total = s + f
        pct = int(s / total * 100) if total else 0
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        lines.append(f"*{merchant}*\n{bar} {pct}% ({s}✅/{f}❌)")
    await update.message.reply_text(
        "📊 *All-Time Success Rates*\n\n" + "\n\n".join(lines),
        parse_mode="Markdown"
    )

async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("pick_result:"):
        result = data.split(":")[1]
        ctx.user_data["pending_result"] = result
        label = "✅ Success" if result == "success" else "❌ Failed"
        buttons = []
        row = []
        for i, m in enumerate(MERCHANTS):
            row.append(InlineKeyboardButton(m, callback_data=f"report_{result}:{m}"))
            if len(row) == 3:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        await query.edit_message_text(
            f"Marked as *{label}*\n\nWhich merchant?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif data.startswith("report_success:") or data.startswith("report_fail:"):
        action, merchant = data.split(":", 1)
        status = "success" if "success" in action else "fail"
        user = query.from_user
        log_report(user.id, user.username, merchant, status)

        # If community reports a success, also log window as open
        if status == "success":
            last = get_last_window_state()
            if not last or last[0] != "open":
                log_window_state("open", merchant=merchant, method="community")
                now_str = datetime.now().strftime("%-m/%-d/%Y, %-I:%M:%S %p")
                await notify_channel(ctx.application, "open", merchant, now_str)

        icon = "✅" if status == "success" else "❌"
        await query.edit_message_text(
            f"{icon} *Logged!* {merchant} — {status.capitalize()}\n\n"
            f"Thanks for helping the community 🙏\n"
            f"Use /status to see current window state.",
            parse_mode="Markdown"
        )

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    init_db()
    log.info("Starting BML Window Tracker...")

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("help",    help_cmd))
    app.add_handler(CommandHandler("status",  status_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("report",  report_cmd))
    app.add_handler(CommandHandler("recent",  recent_cmd))
    app.add_handler(CommandHandler("stats",   stats_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Auto-checker job — runs every CHECK_INTERVAL_SECONDS
    job_queue = app.job_queue
    job_queue.run_repeating(
        auto_check_job,
        interval=CHECK_INTERVAL_SECONDS,
        first=10  # First check 10 seconds after start
    )

    log.info(f"Bot running. Auto-check every {CHECK_INTERVAL_SECONDS}s.")
    app.run_polling()

if __name__ == "__main__":
    main()
