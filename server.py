"""
Daily Planner Backend
- REST API for events (used by the frontend)
- APScheduler for timed Telegram notifications
- Claude generates smart summaries for each notification
"""

import os, sqlite3, requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
import anthropic

# ── Cross-platform timezone (Windows / macOS / Linux) ─────────────────────────
def _detect_tz():
    try:
        import pytz
        from tzlocal import get_localzone_name
        return pytz.timezone(get_localzone_name())
    except Exception:
        try:
            import pytz
            return pytz.utc
        except Exception:
            from datetime import timezone as _tz
            return _tz.utc

LOCAL_TZ = _detect_tz()
# ─────────────────────────────────────────────────────────────────────────────

# ── Cross-platform date formatting ───────────────────────────────────────────
def fmt_day(dt):
    """Returns e.g. 'Wednesday, March 25' — works on Windows, Mac, Linux."""
    return dt.strftime("%A, %B ") + str(dt.day)

def fmt_day_short(dt):
    """Returns e.g. 'Mar 25' """
    return dt.strftime("%b ") + str(dt.day)
# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
CORS(app)

# ── Config (set these as env vars or edit directly) ──────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")
ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY", "YOUR_ANTHROPIC_KEY_HERE")

MORNING_HOUR   = int(os.getenv("MORNING_HOUR",  "8"))   # 08:00
EVENING_HOUR   = int(os.getenv("EVENING_HOUR",  "21"))  # 21:00
WEEKLY_DAY     = os.getenv("WEEKLY_DAY", "sun")          # Sunday nudge
WEEKLY_HOUR    = int(os.getenv("WEEKLY_HOUR", "19"))     # 19:00
# ─────────────────────────────────────────────────────────────────────────────


# ── Database ──────────────────────────────────────────────────────────────────
# On Render, use the persistent disk at /data. Locally, use the script directory.
DB_PATH = os.path.join(os.getenv("RENDER_DISK_PATH", os.path.dirname(os.path.abspath(__file__))), "planner.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id    TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                date  TEXT NOT NULL,
                hour  INTEGER NOT NULL,
                cat   TEXT DEFAULT '',
                dur   INTEGER DEFAULT 60,
                done  INTEGER DEFAULT 0
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                id         TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                role       TEXT DEFAULT '',
                notes      TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id         TEXT PRIMARY KEY,
                content    TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
    print("✅ Database ready")


# ── Context builders (shared by chat + notifications) ─────────────────────────
def get_contacts_context() -> str:
    with get_db() as db:
        rows = db.execute("SELECT * FROM contacts ORDER BY name").fetchall()
    if not rows:
        return ""
    lines = []
    for r in rows:
        line = f"- {r['name']}"
        if r['role']:  line += f" ({r['role']})"
        if r['notes']: line += f": {r['notes']}"
        lines.append(line)
    return "People the user works with:\n" + "\n".join(lines)

def get_memories_context() -> str:
    with get_db() as db:
        rows = db.execute("SELECT content FROM memories ORDER BY created_at DESC LIMIT 30").fetchall()
    if not rows:
        return ""
    return "Things to remember about this user:\n" + "\n".join(f"- {r['content']}" for r in rows)


# ── Telegram helper ───────────────────────────────────────────────────────────
def send_telegram(text: str):
    if TELEGRAM_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print(f"[TELEGRAM - not configured]\n{text}\n")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       text,
        "parse_mode": "Markdown"
    }, timeout=10)
    if not resp.ok:
        print(f"Telegram error: {resp.text}")


# ── Claude helper ─────────────────────────────────────────────────────────────
def build_notif_system() -> str:
    parts = [
        "You are a smart, warm daily planning assistant sending Telegram notifications.",
        get_contacts_context(),
        get_memories_context(),
    ]
    return "\n\n".join(p for p in parts if p.strip())

def claude_say(prompt: str, max_tokens: int = 200) -> str:
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=max_tokens,
            system=build_notif_system(),
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return f"(Claude unavailable: {e})"


# ── Event helpers ─────────────────────────────────────────────────────────────
def events_for_date(date_str: str):
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM events WHERE date = ? ORDER BY hour", (date_str,)
        ).fetchall()
    return [dict(r) for r in rows]

def fmt_event_list(evs):
    if not evs:
        return "  _(none)_"
    return "\n".join(
        f"  • `{str(e['hour']).zfill(2)}:00` — {e['title']}"
        + (f" _{e['cat']}_" if e.get("cat") else "")
        for e in evs
    )


# ── Notification jobs ─────────────────────────────────────────────────────────

def job_morning_summary():
    """08:00 — Claude writes a motivating briefing with today's schedule."""
    today     = datetime.now().strftime("%Y-%m-%d")
    today_fmt = fmt_day(datetime.now())
    evs       = events_for_date(today)

    if evs:
        ev_text  = ", ".join(f"{str(e['hour']).zfill(2)}:00 {e['title']}" for e in evs)
        prompt   = (
            f"Write a punchy, energising morning briefing (max 80 words) for {today_fmt}. "
            f"The user has {len(evs)} event(s): {ev_text}. "
            "Mention the first event and give one practical tip to start strong. No emojis in the body text."
        )
    else:
        prompt = (
            f"Write a short, warm morning message (max 60 words) for {today_fmt}. "
            "The day has no scheduled events — encourage the user to use the free time intentionally. "
            "No emojis in the body text."
        )

    summary   = claude_say(prompt)
    ev_lines  = fmt_event_list(evs)
    count_str = f"*{len(evs)} event{'s' if len(evs) != 1 else ''}* today" if evs else "*Nothing scheduled yet*"

    msg = (
        f"☀️ *Good morning — {today_fmt}*\n\n"
        f"{count_str}:\n{ev_lines}\n\n"
        f"_{summary}_"
    )
    send_telegram(msg)
    print(f"[{datetime.now().strftime('%H:%M')}] Morning summary sent")


def job_event_reminder():
    """Runs every 5 min — sends a nudge 30 min before any upcoming event."""
    now      = datetime.now()
    today    = now.strftime("%Y-%m-%d")
    evs      = events_for_date(today)
    target   = now + timedelta(minutes=30)

    for ev in evs:
        if ev["done"]:
            continue
        ev_time = now.replace(hour=ev["hour"], minute=0, second=0, microsecond=0)
        diff    = (ev_time - now).total_seconds() / 60
        if 25 <= diff <= 35:
            cat_tag = f" _{ev['cat']}_" if ev.get("cat") else ""
            msg = (
                f"⏰ *Heads up!*\n\n"
                f"*{ev['title']}*{cat_tag} starts in ~30 minutes "
                f"(`{str(ev['hour']).zfill(2)}:00`)\n\n"
                f"_Time to wrap up what you're doing and get ready._"
            )
            send_telegram(msg)
            print(f"[{now.strftime('%H:%M')}] Reminder sent for: {ev['title']}")


def job_evening_review():
    """21:00 — Claude summarises the day and encourages planning tomorrow."""
    today     = datetime.now().strftime("%Y-%m-%d")
    today_fmt = fmt_day(datetime.now())
    evs       = events_for_date(today)

    done    = [e for e in evs if     e["done"]]
    pending = [e for e in evs if not e["done"]]

    prompt = (
        f"Write a warm, concise evening review (max 90 words) for {today_fmt}. "
        f"Completed tasks: {[e['title'] for e in done] or 'none'}. "
        f"Incomplete tasks: {[e['title'] for e in pending] or 'none'}. "
        "Be encouraging, briefly acknowledge wins, and nudge the user to plan tomorrow. "
        "No emojis in body text."
    )
    summary = claude_say(prompt)

    done_lines    = fmt_event_list(done)
    pending_lines = fmt_event_list(pending)

    msg = (
        f"🌙 *Evening Review — {today_fmt}*\n\n"
        f"✅ *Completed ({len(done)}):*\n{done_lines}\n\n"
    )
    if pending:
        msg += f"📋 *Carried over ({len(pending)}):*\n{pending_lines}\n\n"
    msg += f"_{summary}_"

    send_telegram(msg)
    print(f"[{datetime.now().strftime('%H:%M')}] Evening review sent")


def job_weekly_nudge():
    """Sunday 19:00 — nudge to plan the upcoming week."""
    sunday_fmt = fmt_day_short(datetime.now())

    # Collect next 7 days of events
    lines = []
    for i in range(1, 8):
        d    = datetime.now() + timedelta(days=i)
        evs  = events_for_date(d.strftime("%Y-%m-%d"))
        if evs:
            day_label = (d.strftime("%a ") + str(d.day))
            lines.append(f"  *{day_label}*: " + ", ".join(e["title"] for e in evs))

    prompt = (
        "Write a brief, motivating weekly planning nudge (max 80 words) for Sunday evening. "
        "Encourage the user to review next week, set 3 priorities, and protect time for rest. "
        "Tone: calm and grounded. No emojis in body text."
    )
    summary  = claude_say(prompt)
    week_str = "\n".join(lines) if lines else "  _(nothing scheduled yet)_"

    msg = (
        f"📅 *Weekly Planning — w/c {sunday_fmt}*\n\n"
        f"*Next 7 days:*\n{week_str}\n\n"
        f"_{summary}_"
    )
    send_telegram(msg)
    print(f"[{datetime.now().strftime('%H:%M')}] Weekly nudge sent")



# ── Telegram incoming message handler ─────────────────────────────────────────
import threading, json as _json

def handle_telegram_message(text: str, chat_id: str) -> str:
    """
    Receives a message from the user via Telegram.
    Passes it to Claude with full context, parses any EVENT blocks,
    saves them to the DB, and returns a reply string for Telegram.
    """
    today_str  = datetime.now().strftime("%Y-%m-%d")
    week_lines = []
    for i in range(7):
        d   = datetime.now() + timedelta(days=i)
        evs = events_for_date(d.strftime("%Y-%m-%d"))
        if evs:
            label = "Today" if i == 0 else (d.strftime("%A ") + str(d.day) + d.strftime(" %b"))
            week_lines.append(label + ": " + ", ".join(
                str(e["hour"]).zfill(2) + ":00 " + e["title"] +
                (" [" + e["cat"] + "]" if e.get("cat") else "") +
                (" ✓" if e.get("done") else "")
                for e in evs
            ))

    schedule_ctx = "Today is " + fmt_day(datetime.now()) + ", " + datetime.now().strftime("%Y") + ".\n"
    schedule_ctx += ("Schedule:\n" + "\n".join(week_lines)) if week_lines else "No events this week yet."

    system = "\n\n".join(filter(None, [
        """You are a smart daily planning assistant accessible via Telegram.
Be concise — Telegram messages should be short and scannable.
Use plain text (no HTML). You can use emojis sparingly.

When the user asks you to add, schedule, or block any event, include this block:

<<<EVENT>>>
{
  "title": "Event title",
  "date": "YYYY-MM-DD",
  "hour": 9,
  "cat": "work",
  "dur": 60
}
<<<END>>>

Rules:
- date must be YYYY-MM-DD (today is REPLACE_TODAY)
- hour is 0-23 integer
- cat: work, personal, health, or "" for general
- dur in minutes: 30, 60, 90, 120, 180
- You can emit multiple blocks
- Keep your text reply under 120 words""",
        schedule_ctx,
        get_contacts_context(),
        get_memories_context(),
    ])).replace("REPLACE_TODAY", today_str)

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        resp   = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            system=system,
            messages=[{"role": "user", "content": text}]
        )
        reply = resp.content[0].text.strip()
    except Exception as e:
        return f"Sorry, Claude is unavailable: {e}"

    # Parse and save any EVENT blocks
    import re
    saved_events = []
    def replace_block(m):
        try:
            data = _json.loads(m.group(1))
            ev = {
                "id":    str(int(datetime.now().timestamp() * 1000)) + str(len(saved_events)),
                "title": data.get("title", "Untitled"),
                "date":  data.get("date",  today_str),
                "hour":  int(data.get("hour",  9)),
                "cat":   data.get("cat",   ""),
                "dur":   int(data.get("dur",  60)),
                "done":  0,
            }
            with get_db() as db:
                db.execute(
                    "INSERT OR REPLACE INTO events (id,title,date,hour,cat,dur,done) VALUES (?,?,?,?,?,?,?)",
                    (ev["id"], ev["title"], ev["date"], ev["hour"], ev["cat"], ev["dur"], ev["done"])
                )
            saved_events.append(ev)
        except Exception as ex:
            print(f"Event parse error: {ex}")
        return ""   # remove the block from the reply text

    clean_reply = re.sub(r"<<<EVENT>>>\s*([\s\S]*?)\s*<<<END>>>", replace_block, reply).strip()

    # Append a summary of saved events
    if saved_events:
        lines = []
        for ev in saved_events:
            d = datetime.strptime(ev["date"], "%Y-%m-%d")
            day_label = "Today" if ev["date"] == today_str else (d.strftime("%a ") + str(d.day) + d.strftime(" %b"))
            lines.append(f"  ✅ {ev['title']} — {day_label} at {str(ev['hour']).zfill(2)}:00")
        clean_reply += "\n\n📅 Added to your planner:\n" + "\n".join(lines)

    return clean_reply or "Done!"


def _poll_telegram():
    """Long-poll Telegram for new messages and reply to them."""
    if TELEGRAM_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("⚠️  Telegram polling skipped — token not set")
        return

    offset = None
    print("📨  Telegram polling started — message your bot to add events!")
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset:
                params["offset"] = offset
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params=params, timeout=35
            )
            if not r.ok:
                import time; time.sleep(5); continue

            for update in r.json().get("result", []):
                offset = update["update_id"] + 1
                msg    = update.get("message", {})
                text   = msg.get("text", "").strip()
                cid    = str(msg.get("chat", {}).get("id", ""))

                if not text or not cid:
                    continue
                if text.startswith("/start"):
                    send_telegram("👋 Hey! I'm your planner bot. Tell me what to schedule, ask about your day, or say 'what\'s on today?'")
                    continue

                print(f"[Telegram ←] {text}")
                reply = handle_telegram_message(text, cid)
                print(f"[Telegram →] {reply[:80]}...")
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                    json={"chat_id": cid, "text": reply},
                    timeout=10
                )
        except Exception as e:
            print(f"Polling error: {e}")
            import time; time.sleep(5)


def start_polling():
    t = threading.Thread(target=_poll_telegram, daemon=True)
    t.start()
# ── End Telegram polling ──────────────────────────────────────────────────────


# ── Render keep-alive (pings self every 10 min to prevent spin-down) ──────────
def job_keepalive():
    render_url = os.getenv("RENDER_EXTERNAL_URL", "")
    if not render_url:
        return
    try:
        requests.get(render_url + "/ping", timeout=10)
        print(f"[keepalive] pinged {render_url}/ping")
    except Exception as e:
        print(f"[keepalive] failed: {e}")
# ─────────────────────────────────────────────────────────────────────────────

# ── Scheduler ────────────────────────────────────────────────────────────────
scheduler = BackgroundScheduler(timezone=LOCAL_TZ)
scheduler.add_job(job_morning_summary, "cron", hour=MORNING_HOUR, minute=0,   id="morning")
scheduler.add_job(job_event_reminder,  "interval", minutes=5,                 id="reminder")
scheduler.add_job(job_evening_review,  "cron", hour=EVENING_HOUR, minute=0,   id="evening")
scheduler.add_job(job_weekly_nudge,    "cron", day_of_week=WEEKLY_DAY,
                  hour=WEEKLY_HOUR, minute=0,                                  id="weekly")
scheduler.add_job(job_keepalive,       "interval", minutes=10,                id="keepalive")


# ── REST API ──────────────────────────────────────────────────────────────────
@app.route("/ping")
def ping():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})

@app.route("/events", methods=["GET"])
def list_events():
    with get_db() as db:
        rows = db.execute("SELECT * FROM events ORDER BY date, hour").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/events", methods=["POST"])
def upsert_event():
    ev = request.get_json()
    with get_db() as db:
        db.execute(
            "INSERT OR REPLACE INTO events (id,title,date,hour,cat,dur,done) VALUES (?,?,?,?,?,?,?)",
            (ev["id"], ev["title"], ev["date"], ev["hour"],
             ev.get("cat", ""), ev.get("dur", 60), int(ev.get("done", False)))
        )
    return jsonify({"ok": True})

@app.route("/events/<ev_id>", methods=["DELETE"])
def delete_event(ev_id):
    with get_db() as db:
        db.execute("DELETE FROM events WHERE id = ?", (ev_id,))
    return jsonify({"ok": True})

@app.route("/events/<ev_id>/done", methods=["PATCH"])
def toggle_done(ev_id):
    data = request.get_json()
    with get_db() as db:
        db.execute("UPDATE events SET done = ? WHERE id = ?",
                   (1 if data.get("done") else 0, ev_id))
    return jsonify({"ok": True})

# Manual trigger endpoints (useful for testing)
@app.route("/trigger/morning",  methods=["POST"])
def trigger_morning():  job_morning_summary(); return jsonify({"ok": True})

@app.route("/trigger/evening",  methods=["POST"])
def trigger_evening():  job_evening_review();  return jsonify({"ok": True})

@app.route("/trigger/weekly",   methods=["POST"])
def trigger_weekly():   job_weekly_nudge();    return jsonify({"ok": True})


# ── Contacts API ──────────────────────────────────────────────────────────────
@app.route("/contacts", methods=["GET"])
def list_contacts():
    with get_db() as db:
        rows = db.execute("SELECT * FROM contacts ORDER BY name").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/contacts", methods=["POST"])
def upsert_contact():
    c = request.get_json()
    with get_db() as db:
        db.execute(
            "INSERT OR REPLACE INTO contacts (id,name,role,notes) VALUES (?,?,?,?)",
            (c["id"], c["name"], c.get("role",""), c.get("notes",""))
        )
    return jsonify({"ok": True})

@app.route("/contacts/<cid>", methods=["DELETE"])
def delete_contact(cid):
    with get_db() as db:
        db.execute("DELETE FROM contacts WHERE id = ?", (cid,))
    return jsonify({"ok": True})


# ── Memories API ──────────────────────────────────────────────────────────────
@app.route("/memories", methods=["GET"])
def list_memories():
    with get_db() as db:
        rows = db.execute("SELECT * FROM memories ORDER BY created_at DESC").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/memories", methods=["POST"])
def add_memory():
    m = request.get_json()
    with get_db() as db:
        db.execute(
            "INSERT INTO memories (id,content) VALUES (?,?)",
            (m["id"], m["content"])
        )
    return jsonify({"ok": True})

@app.route("/memories/<mid>", methods=["DELETE"])
def delete_memory(mid):
    with get_db() as db:
        db.execute("DELETE FROM memories WHERE id = ?", (mid,))
    return jsonify({"ok": True})


# ── Chat proxy — sliding window + full context injection ──────────────────────
CHAT_WINDOW = 20   # keep last N messages to avoid context overflow

@app.route("/chat", methods=["POST"])
def chat():
    """
    Receives { messages: [...] } from the frontend.
    Trims to the last CHAT_WINDOW messages, injects a rich system prompt
    (schedule + contacts + memories), and proxies to Claude.
    Returns { reply: "..." }.
    """
    body     = request.get_json()
    messages = body.get("messages", [])
    today_str    = datetime.now().strftime("%Y-%m-%d")
    today_fmt    = fmt_day(datetime.now()) + datetime.now().strftime(", %Y")
    today_evs    = events_for_date(today_str)

    # Build week snapshot (today + next 6 days)
    week_lines = []
    for i in range(7):
        d    = datetime.now() + timedelta(days=i)
        evs  = events_for_date(d.strftime("%Y-%m-%d"))
        if evs:
            label = "Today" if i == 0 else (d.strftime("%A ") + str(d.day) + d.strftime(" %b"))
            week_lines.append(f"{label}: " + ", ".join(
                f"{str(e['hour']).zfill(2)}:00 {e['title']}" +
                (f" [{e['cat']}]" if e.get('cat') else "") +
                (" ✓" if e.get('done') else "")
                for e in evs
            ))

    schedule_ctx = f"Today is {today_fmt}.\n"
    schedule_ctx += ("Schedule:\n" + "\n".join(week_lines)) if week_lines else "No events scheduled this week yet."

    system = "\n\n".join(filter(None, [
        """You are a smart, warm daily planning assistant. Be concise and practical.
When the user asks you to remember something, confirm you will — the frontend will save it.
Keep responses under 200 words unless asked for more.

CRITICAL — ADDING EVENTS:
When the user asks you to add, schedule, block, or create any event/task, you MUST include
a special event block in your reply using EXACTLY this format (one per event):

<<<EVENT>>>
{
  "title": "Event title",
  "date": "YYYY-MM-DD",
  "hour": 9,
  "cat": "work",
  "dur": 60
}
<<<END>>>

Rules:
- date must be YYYY-MM-DD (today is REPLACE_TODAY)
- hour is 0-23 integer (no quotes)
- cat is one of: work, personal, health, or "" for general
- dur is minutes as integer: 30, 60, 90, 120, 180
- You can include multiple <<<EVENT>>> blocks in one reply
- Always include a natural language confirmation too, e.g. "Here's what I've prepared for you:"
- If the user says "add a standup every day this week", emit one block per day""",
        schedule_ctx,
        get_contacts_context(),
        get_memories_context(),
    ])).replace("REPLACE_TODAY", datetime.now().strftime("%Y-%m-%d"))

    # Sliding window — never send more than CHAT_WINDOW messages
    windowed = messages[-CHAT_WINDOW:]

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        resp   = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=system,
            messages=windowed,
        )
        return jsonify({"reply": resp.content[0].text.strip()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    scheduler.start()
    start_polling()
    print("🗓️  Planner backend running  →  http://localhost:5050")
    print(f"🔔  Notifications: morning={MORNING_HOUR}:00  evening={EVENING_HOUR}:00  weekly={WEEKLY_DAY}@{WEEKLY_HOUR}:00")
    print("📬  Telegram configured:", TELEGRAM_TOKEN != "YOUR_BOT_TOKEN_HERE")
    try:
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5050)), debug=False, use_reloader=False)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        print("\n👋 Server stopped")
