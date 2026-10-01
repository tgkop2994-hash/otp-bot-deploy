import concurrent.futures
import json
import os
import random
import re
import threading
import time

import requests

import config
import otp_bot
import ai_bot
from countries import COUNTRIES as ALL_COUNTRIES

API = f"https://api.telegram.org/bot{config.BOT_TOKEN}/{{method}}"
_session = requests.Session()  # reuse the TLS connection -> much faster
RATE = float(os.environ.get("OTP_RATE", "1.0"))  # target OTPs/sec (1.0 = one per second)
SEND_WORKERS = int(os.environ.get("OTP_WORKERS", "2"))  # parallel senders

PLATFORMS = [
    {"key": "whatsapp", "label": "💬 WhatsApp", "app": "whatsapp", "digits": 6},
    {"key": "facebook", "label": "📘 Facebook", "app": "facebook", "digits": 6},
    {"key": "telegram", "label": "✈️ Telegram", "app": "telegram", "digits": 5},
    {"key": "instagram", "label": "📸 Instagram", "app": "instagram", "digits": 6},
    {"key": "imo", "label": "💙 IMO", "app": "imo", "digits": 4},
    {"key": "chatgpt", "label": "🤖 ChatGPT", "app": "chatgpt", "digits": 6},
]
PLAT_BY_KEY = {p["key"]: p for p in PLATFORMS}

# short forms accepted in search input, e.g. "IN WA" -> WhatsApp + India
APP_ALIASES = {
    "wa": "whatsapp", "whatsapp": "whatsapp", "whats": "whatsapp",
    "fb": "facebook", "facebook": "facebook",
    "tg": "telegram", "telegram": "telegram", "tel": "telegram",
    "ig": "instagram", "insta": "instagram", "instagram": "instagram",
    "imo": "imo", "im": "imo",
    "chatgpt": "chatgpt", "gpt": "chatgpt",
}


def parse_search(text):
    """Split a search message into (platform_key, country_query).

    Order doesn't matter: 'IN WA', 'WA IN', 'facebook IN' all work.
    """
    app = None
    parts = []
    for token in (text or "").replace(",", " ").split():
        norm = "".join(ch for ch in token if ch.isalnum()).lower()
        if not norm:
            continue
        if norm in APP_ALIASES:
            app = APP_ALIASES[norm]
        else:
            parts.append(token)
    return app, " ".join(parts).strip()


def tg(method, **kw):
    try:
        r = _session.post(API.format(method=method), json=kw, timeout=60)
        return r.json()
    except Exception as e:
        print("tg error:", e)
        return None


# ---- build the full country list ----
def build_countries():
    items = [(dial, otp_bot.flag_emoji(short), short, name)
             for dial, short, name in ALL_COUNTRIES]
    items.sort(key=lambda x: x[3].lower())
    return items


COUNTRIES = build_countries()
PER_PAGE = 8


def platform_menu():
    rows = []
    for p in PLATFORMS:
        if p["key"] in ("instagram", "imo", "chatgpt"):
            continue  # these have their own one-click buttons below
        rows.append([{"text": p["label"], "callback_data": f"pl:{p['key']}"}])
    rows.append([{"text": "📸 Instagram OTP", "callback_data": "insta"}])
    rows.append([{"text": "💙 IMO OTP", "callback_data": "imo"}])
    rows.append([{"text": "🤖 ChatGPT OTP", "callback_data": "chatgpt"}])
    rows.append([{"text": "➕ OTP Group Add", "callback_data": "addgroup"}])
    rows.append([{"text": "📋 OTP Groups", "callback_data": "listgroups"}])
    rows.append([{"text": "🔍 Search country", "callback_data": "search"}])
    return {"inline_keyboard": rows}


def control_menu():
    return {
        "inline_keyboard": [
            [{"text": "▶️ START SENDING", "callback_data": "start"}],
            [{"text": "🛑 STOP", "callback_data": "stop"}],
            [{"text": "⬅️ Back to platforms", "callback_data": "back"}],
        ]
    }


def quick_keyboard():
    """Reply-markup buttons shown next to the text input."""
    return {
        "keyboard": [
            [{"text": "▶️ Start"}, {"text": "🛑 Stop"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }


def country_menu(plat_key, page=0):
    total = len(COUNTRIES)
    pages = (total + PER_PAGE - 1) // PER_PAGE
    page = max(0, min(page, pages - 1))
    start = page * PER_PAGE
    rows = []
    for p, flag, short, name in COUNTRIES[start:start + PER_PAGE]:
        rows.append([{"text": f"{flag} {name} ({short})",
                      "callback_data": f"sel:{plat_key}:{p}"}])
    nav = []
    if page > 0:
        nav.append({"text": "⬅️", "callback_data": f"pg:{plat_key}:{page - 1}"})
    nav.append({"text": f"{page + 1}/{pages}", "callback_data": "noop"})
    if page < pages - 1:
        nav.append({"text": "➡️", "callback_data": f"pg:{plat_key}:{page + 1}"})
    rows.append(nav)
    rows.append([{"text": "🔍 Search country", "callback_data": "search"}])
    rows.append([{"text": "⬅️ Back to platforms", "callback_data": "back"}])
    rows.append([{"text": "▶️ Start", "callback_data": "start"},
                 {"text": "🛑 Stop", "callback_data": "stop"}])
    return {"inline_keyboard": rows}


CURRENT = {"app": None, "flag": None, "short": None, "cc": None, "name": None}
AI_MODE = set()          # chat_ids currently chatting with the AI
SEARCH_MODE = set()      # chat_ids waiting for a search input
ADDGROUP_MODE = set()    # chat_ids waiting for a group link/username/id
PENDING_APP = {}         # chat_id -> platform chosen via search, waiting for country
MODEL_CHOICE = {}        # chat_id -> preferred ai_bot provider key
_thread = None
_send_stop = threading.Event()

BOT_ID = int(config.BOT_TOKEN.split(":")[0])

ADDGROUP_PROMPT = (
    "➕ <b>OTP Group Add</b>\n"
    "Send group <b>link(s)</b>, <b>@username(s)</b>, or <b>numeric ID(s)</b> —\n"
    "one per line (or space-separated) to add several at once:\n"
    "<code>https://t.me/group1\nhttps://t.me/group2</code>\n\n"
    "⚠️ First add @syrx77bot to each group (as admin). "
    "I cannot join by myself — a human admin must add me.\n"
    "📋 OTP Groups shows the added list. /cancel exits."
)


def groups_text():
    targets = otp_bot.get_targets()
    lines = [f"📋 <b>OTP Groups ({len(targets)}):</b>"]
    for i, (cid, title) in enumerate(targets, 1):
        name = title or "(no name)"
        lines.append(f"{i}. <b>{name}</b> — <code>{cid}</code>")
    lines.append("\nOTPs go to all of these at once. Tap ❌ to remove one.")
    return "\n".join(lines)


def groups_keyboard():
    rows = []
    for cid, title in otp_bot.get_targets():
        label = f"❌ {title}" if title else f"❌ {cid}"
        rows.append([{"text": label[:40], "callback_data": f"rmgroup:{cid}"}])
    rows.append([{"text": "⬅️ Back to platforms", "callback_data": "back"}])
    return {"inline_keyboard": rows}


def parse_group_input(text):
    """Accept a t.me link, @username, or numeric id. Returns str or int."""
    t = (text or "").strip()
    if t.lstrip("-").isdigit():
        return int(t)
    m = re.search(r"t\.me/(?:joinchat/|\+)?([A-Za-z0-9_]+)", t)
    if m:
        return "@" + m.group(1)
    if t.startswith("@") and len(t) > 1:
        return t
    if re.fullmatch(r"[A-Za-z0-9_]{5,}", t):
        return "@" + t
    return None


def handle_group_text(chat_id, text):
    tokens = [t for t in re.split(r"[\s,]+", (text or "").strip()) if t]
    if not tokens:
        tg_send(chat_id,
                "❌ Send at least one group link, @username, or numeric ID.",
                platform_menu())
        return
    added, existed, failed = [], [], []
    for tok in tokens:
        ident = parse_group_input(tok)
        if ident is None:
            failed.append((tok, "unreadable — use a link, @username, or ID"))
            continue
        chat = tg("getChat", chat_id=ident)
        if not chat or not chat.get("ok"):
            failed.append((tok, "group not found (am I added there? "
                                "private invite links need the numeric ID)"))
            continue
        info = chat["result"]
        gid = info.get("id")
        title = info.get("title", "")
        ctype = info.get("type", "")
        if ctype not in ("group", "supergroup"):
            failed.append((tok, f"that's a {ctype}, not a group"))
            continue
        member = tg("getChatMember", chat_id=gid, user_id=BOT_ID)
        status = (member.get("result", {}) or {}).get("status", "") \
            if member and member.get("ok") else ""
        if status not in ("creator", "administrator", "member"):
            failed.append((tok, f"found '{title}' but I'm not in it — "
                                "add @syrx77bot there first"))
            continue
        if otp_bot.add_target(gid, title):
            note = "" if status in ("creator", "administrator") else \
                " (member only — make me admin there)"
            added.append(f"{title}{note}")
        else:
            existed.append(title or str(gid))
    parts = []
    for t in added:
        parts.append(f"✅ Added <b>{t}</b>")
    for t in existed:
        parts.append(f"ℹ️ Already added: <b>{t}</b>")
    for tok, why in failed:
        parts.append(f"❌ <code>{tok}</code>: {why}")
    n = len(otp_bot.get_targets())
    parts.append(f"\n📋 Now sending to <b>{n}</b> OTP group(s). "
                "Press ▶️ Start or use search to begin.")
    tg_send(chat_id, "\n".join(parts), platform_menu())
    if not failed:
        ADDGROUP_MODE.discard(chat_id)


def stop_sender():
    global _thread, _send_stop
    _send_stop.set()
    if _thread and _thread.is_alive():
        _thread.join(timeout=3)
    _thread = None
    # keep CURRENT so 'Start' can resume the same country


def do_start(chat_id, msg_id=None):
    text = "👆 <b>Pick a platform, then a country</b> to start sending."
    if CURRENT.get("app") and CURRENT.get("cc"):
        start_sender(CURRENT["app"], CURRENT["cc"], CURRENT["flag"],
                     CURRENT["short"], CURRENT["name"], chat_id)
        text = (f"🚀 <b>Restarted</b>: {CURRENT['flag']} {CURRENT['name']} "
                f"({CURRENT['short']}) · OTPs → OTP group.")
    if msg_id:
        tg_edit(chat_id, msg_id, text, platform_menu())
    else:
        tg_send(chat_id, text, platform_menu())


def do_stop(chat_id, msg_id=None):
    stop_sender()
    text = "🛑 <b>Stopped sending OTPs.</b>"
    if msg_id:
        tg_edit(chat_id, msg_id, text, platform_menu())
    else:
        tg_send(chat_id, text, platform_menu())


def search_prompt():
    side = PLAT_BY_KEY.get(CURRENT.get("app") or "whatsapp")["label"]
    return ("🔍 <b>Search OTP</b>\n"
            "Type the <b>app</b> and <b>country</b> short forms in one "
            "message:\n"
            "<code>IN WA</code> → India · WhatsApp\n"
            "<code>PK FB</code> → Pakistan · Facebook\n"
            "<code>US TG</code> → USA · Telegram\n\n"
            "· Apps: 💬 <b>WA</b> WhatsApp · 📘 <b>FB</b> Facebook · "
            "✈️ <b>TG</b> Telegram · 📸 <b>IG</b> Instagram · "
            "💙 <b>IMO</b> · 🤖 <b>GPT</b>\n"
            "· Countries: 2-letter code like <code>IN</code>, "
            "<code>US</code>, <code>PK</code>\n"
            f"· No app given → uses: {side}\n"
            "/cancel_search exits search.")


def handle_search_text(chat_id, text):
    app, q = parse_search(text)
    q = q.lower()
    if not q:
        if app:
            PENDING_APP[chat_id] = app
            tg_send(chat_id,
                    f"✅ Platform: <b>{PLAT_BY_KEY[app]['label']}</b>.\n"
                    "Now type the country code, e.g. <code>IN</code> — or "
                    "just send the full <code>IN WA</code> again.",
                    platform_menu())
        return
    # resolve the country
    hits = [c for c in COUNTRIES if c[2].lower() == q]
    if not hits:
        cand = [c for c in COUNTRIES if q in c[2].lower() or q in c[3].lower()]
        if q.isdigit():
            cand = [c for c in cand if c[0] == q]
        if len(cand) == 1:
            hits = cand
        elif len(cand) > 1:
            names = ", ".join(f"<code>{c[2]}</code>" for c in cand[:12])
            tg_send(chat_id,
                    f"🤔 Multiple matches ({len(cand)}): {names}\n"
                    "Type the exact 2-letter short code (e.g. <code>IN</code>).",
                    platform_menu())
            return
    if not hits:
        tg_send(chat_id,
                f"❌ No country found for '<b>{text}</b>'.\n"
                "Use a format like <code>IN WA</code> (country + app) or a "
                "2-letter code like <code>IN</code>, <code>US</code>, "
                "<code>PK</code>.",
                platform_menu())
        return
    p, flag, short, name = hits[0]
    plat = app or PENDING_APP.pop(chat_id, None) or CURRENT.get("app") \
        or "whatsapp"
    if plat not in PLAT_BY_KEY:
        plat = "whatsapp"
    start_sender(plat, p, flag, short, name, chat_id)
    SEARCH_MODE.discard(chat_id)
    PENDING_APP.pop(chat_id, None)
    plat_label = PLAT_BY_KEY[plat]["label"]
    tg_send(chat_id,
            f"🚀 Search → started <b>{flag} {name} ({short})</b> "
            f"on {plat_label} · OTPs → OTP group.",
            platform_menu())


def start_sender(plat_key, cc, flag, short, name, chat_id):
    """Start sending test OTPs into the OTP group (config.CHAT_ID)."""
    global _thread, _send_stop
    stop_sender()
    _send_stop = threading.Event()
    plat = PLAT_BY_KEY[plat_key]
    otp_len = plat["digits"]
    per_worker = SEND_WORKERS / float(RATE)

    def send_one():
        while not _send_stop.is_set():
            local = "".join(str(random.randint(0, 9)) for _ in range(9))
            number = cc + local
            otp = str(random.randint(10 ** (otp_len - 1), 10 ** otp_len - 1))
            msg = f"Your {plat['app']} code is {otp}"
            rec = {"num": number, "cli": plat["app"], "message": msg}
            text = otp_bot.format_record(rec, flag=flag, short=short)
            ok = otp_bot.tg_send(text, otp)  # sends to the OTP group
            print(f"[{'OK' if ok else 'FAIL'}] {name} {plat['app']} "
                  f"OTP={otp} num=+{number}", flush=True)
            # throttle each worker so the aggregate rate stays near RATE
            slept = 0.0
            while slept < per_worker:
                if _send_stop.is_set():
                    return
                step = min(0.2, per_worker - slept)
                time.sleep(step)
                slept += step

    def worker():
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=SEND_WORKERS)
        try:
            futures = [pool.submit(send_one) for _ in range(SEND_WORKERS)]
            for f in futures:
                f.result()
        finally:
            pool.shutdown(wait=True)

    CURRENT.update(app=plat["key"], flag=flag, short=short, cc=cc, name=name)
    _thread = threading.Thread(target=worker, daemon=True)
    _thread.start()
    interval = 1.0 / float(RATE)
    tg_send(chat_id,
            f"🚀 Started {plat['label']} test OTPs for <b>{flag} {short}</b> "
            f"<b>{name}</b> (+{cc}), {otp_len} digits · 1 OTP every "
            f"{interval:g}s (1/sec), unlimited until 🛑 Stop.\n"
            f"OTPs are being sent to {len(otp_bot.get_targets())} OTP group(s). "
            f"Use ⏯️ /stop to halt.")


def tg_send(chat_id, text, markup=None):
    kw = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if markup:
        kw["reply_markup"] = markup
    return tg("sendMessage", **kw)


def tg_edit(chat_id, message_id, text, markup=None):
    kw = {"chat_id": chat_id, "message_id": message_id, "text": text,
          "parse_mode": "HTML"}
    if markup:
        kw["reply_markup"] = markup
    return tg("editMessageText", **kw)


DM_LOG = "dm_chat.log"


def log_chat(chat_id, username="", first=""):
    with open(DM_LOG, "a", encoding="utf-8") as f:
        f.write(f"{chat_id} | {username or '-'} | {first or '-'}\n")


def handle_command(chat_id, text):
    text = (text or "").strip()
    if text.startswith("/start"):
        tg_send(chat_id, "🌍 <b>Select a platform:</b>\n"
                         "Then choose a country — OTPs will be sent to the OTP group "
                         "for numbers in that country.",
                platform_menu())
        tg_send(chat_id,
                "▶️ <b>Start</b> / 🛑 <b>Stop</b> buttons are above the input box.",
                quick_keyboard())
        return True
    if text.startswith("/stop"):
        stop_sender()
        tg_send(chat_id, "🛑 Stopped sending OTPs.")
        return True
    if text.startswith("/status"):
        if CURRENT["cc"]:
            plat = PLAT_BY_KEY[CURRENT["app"]]["label"]
            tg_send(chat_id, f"Currently sending: {plat} · {CURRENT['flag']} "
                             f"{CURRENT['name']} (+{CURRENT['cc']}) · {RATE} OTP/s")
        else:
            tg_send(chat_id, "Not sending any OTPs right now.")
        return True
    if text.startswith("/ping"):
        tg_send(chat_id, "🟢 Bot is online and responding.")
        return True
    if text.startswith("/cancel_search"):
        SEARCH_MODE.discard(chat_id)
        PENDING_APP.pop(chat_id, None)
        tg_send(chat_id, "🔍 Search cancelled. Back to the menu.", platform_menu())
        return True
    if text.startswith("/cancel"):
        SEARCH_MODE.discard(chat_id)
        ADDGROUP_MODE.discard(chat_id)
        PENDING_APP.pop(chat_id, None)
        tg_send(chat_id, "Cancelled. Back to the menu.", platform_menu())
        return True
    if text.startswith("/addgroup"):
        ADDGROUP_MODE.add(chat_id)
        tg_send(chat_id, ADDGROUP_PROMPT, platform_menu())
        return True
    if text.startswith("/groups"):
        tg_send(chat_id, groups_text(), groups_keyboard())
        return True
    if text.startswith("/search"):
        SEARCH_MODE.add(chat_id)
        tg_send(chat_id, search_prompt(), platform_menu())
        return True
    if text.startswith("/ai") or text.startswith("/python"):
        if not ai_bot.available():
            tg_send(chat_id, "⚠️ No AI provider is configured yet.\n"
                             "Add a GEMINI_API_KEY / OPENAI_API_KEY / GROQ_API_KEY "
                             "or DEEPSEEK_API_KEY in Render → Environment.")
            return True
        AI_MODE.add(chat_id)
        ai_bot.reset_history(chat_id)
        tg_send(chat_id,
                "🤖 <b>AI assistant ready.</b>\n"
                "Ask me anything — or send a Python snippet like:\n<code>print(2**10)</code>\n\n"
                "I'll run and answer it. Send /exit to leave AI mode.")
        return True
    if text.startswith("/exit"):
        AI_MODE.discard(chat_id)
        tg_send(chat_id, "👋 Left AI mode. Use /ai to return anytime.")
        return True
    if text.startswith("/model"):
        rows = []
        for key, cfg in ai_bot.available():
            cur = " ✅" if MODEL_CHOICE.get(chat_id) == key else ""
            rows.append([{"text": f"🧠 {cfg['label']}{cur}",
                          "callback_data": f"model:{key}"}])
        rows.append([{"text": "☘️ Auto (first working)", "callback_data": "model:auto"}])
        tg_send(chat_id, "🧠 <b>Choose your AI model:</b>",
                {"inline_keyboard": rows})
        return True
    if text.startswith("/help"):
        tg_send(chat_id,
                "🛠 <b>Available commands</b>\n\n"
                "/start — open the platform & country menu\n"
                "/ping — check the bot is alive\n"
                "/status — see what is currently being sent\n"
                "/stop — stop all OTP sending\n"
                "/help — this list\n\n"
                "⚙️ <b>AI issues?</b> The bot automatically restarts and "
                "sends an alert here if it crashes.")
        return True
    return False


def handle_callback(cb):
    chat_id = (cb.get("message") or {}).get("chat", {}).get("id")
    msg_id = (cb.get("message") or {}).get("message_id")
    data = cb.get("data", "")
    tg("answerCallbackQuery", callback_query_id=cb["id"])
    if not chat_id or not msg_id:
        return
    if data == "stop":
        do_stop(chat_id, msg_id)
    elif data == "start":
        do_start(chat_id, msg_id)
    elif data == "back":
        tg_edit(chat_id, msg_id, "🌍 <b>Select a platform:</b>", platform_menu())
    elif data == "ctrl":
        tg_edit(chat_id, msg_id,
                "🛠 <b>OTP Control</b>\n"
                "Start or stop sending OTPs to the group:",
                control_menu())
    elif data == "search":
        SEARCH_MODE.add(chat_id)
        tg_edit(chat_id, msg_id, search_prompt(), platform_menu())
    elif data == "addgroup":
        ADDGROUP_MODE.add(chat_id)
        tg_edit(chat_id, msg_id, ADDGROUP_PROMPT, platform_menu())
    elif data == "listgroups":
        tg_edit(chat_id, msg_id, groups_text(), groups_keyboard())
    elif data.startswith("rmgroup:"):
        try:
            otp_bot.remove_target(int(data.split(":", 1)[1]))
        except ValueError:
            pass
        tg_edit(chat_id, msg_id, groups_text(), groups_keyboard())
    elif data == "insta":
        if CURRENT.get("cc") and CURRENT.get("short"):
            start_sender("instagram", CURRENT["cc"], CURRENT["flag"],
                         CURRENT["short"], CURRENT["name"], chat_id)
            tg_edit(chat_id, msg_id,
                    f"🚀 <b>Instagram OTPs started</b> for "
                    f"{CURRENT['flag']} {CURRENT['name']} "
                    f"({CURRENT['short']}) — 6-digit codes → OTP group.",
                    platform_menu())
        else:
            tg_edit(chat_id, msg_id,
                    "📸 <b>Pick a country for Instagram OTP</b> "
                    "(6-digit codes):",
                    country_menu("instagram", 0))
    elif data == "imo":
        if CURRENT.get("cc") and CURRENT.get("short"):
            start_sender("imo", CURRENT["cc"], CURRENT["flag"],
                         CURRENT["short"], CURRENT["name"], chat_id)
            tg_edit(chat_id, msg_id,
                    f"🚀 <b>IMO OTPs started</b> for "
                    f"{CURRENT['flag']} {CURRENT['name']} "
                    f"({CURRENT['short']}) — 4-digit codes → OTP group.",
                    platform_menu())
        else:
            tg_edit(chat_id, msg_id,
                    "💙 <b>Pick a country for IMO OTP</b> "
                    "(4-digit codes):",
                    country_menu("imo", 0))
    elif data == "chatgpt":
        if CURRENT.get("cc") and CURRENT.get("short"):
            start_sender("chatgpt", CURRENT["cc"], CURRENT["flag"],
                         CURRENT["short"], CURRENT["name"], chat_id)
            tg_edit(chat_id, msg_id,
                    f"🚀 <b>ChatGPT OTPs started</b> for "
                    f"{CURRENT['flag']} {CURRENT['name']} "
                    f"({CURRENT['short']}) — 6-digit codes → OTP group.",
                    platform_menu())
        else:
            tg_edit(chat_id, msg_id,
                    "🤖 <b>Pick a country for ChatGPT OTP</b> "
                    "(6-digit codes):",
                    country_menu("chatgpt", 0))
    elif data == "noop":
        pass
    elif data.startswith("pl:"):
        plat_key = data.split(":", 1)[1]
        plat = PLAT_BY_KEY[plat_key]["label"]
        tg_edit(chat_id, msg_id,
                f"🌍 <b>Select a country for {plat}:</b>", country_menu(plat_key, 0))
    elif data.startswith("pg:"):
        _, plat_key, page = data.split(":")
        plat = PLAT_BY_KEY[plat_key]["label"]
        tg_edit(chat_id, msg_id,
                f"🌍 <b>Select a country for {plat}:</b>",
                country_menu(plat_key, int(page)))
    elif data.startswith("sel:"):
        _, plat_key, cc = data.split(":")
        for p, flag, short, name in COUNTRIES:
            if p == cc:
                start_sender(plat_key, cc, flag, short, name, chat_id)
                tg_edit(chat_id, msg_id,
                        f"🚀 Started: {flag} {name} ({short}) … "
                        f"OTPs arriving in the OTP group.",
                        platform_menu())
                break
    elif data == "ai":
        if not ai_bot.available():
            tg_edit(chat_id, msg_id,
                    "⚠️ <b>No AI provider configured yet.</b>\n"
                    "Set one of GEMINI_API_KEY / OPENAI_API_KEY / GROQ_API_KEY "
                    "/ DEEPSEEK_API_KEY in Render → Environment variables.",
                    platform_menu())
            return
        AI_MODE.add(chat_id)
        ai_bot.reset_history(chat_id)
        tg_edit(chat_id, msg_id,
                "🤖 <b>Python AI assistant ready.</b>\n\n"
                "Ask me to solve anything — math, code, ideas, debugging.\n"
                "- Send normal text → I answer\n"
                "- Send a code snippet → I run it and show the result\n"
                "- /model → switch AI brain\n- /exit → back to the menu",
                {"inline_keyboard": [[{"text": "⬅️ Back to menu",
                                       "callback_data": "back"}]]})
    elif data.startswith("model:"):
        key = data.split(":", 1)[1]
        if key == "auto":
            MODEL_CHOICE.pop(chat_id, None)
            tg_edit(chat_id, msg_id, "☘️ <b>AI set to Auto</b> — picks the first "
                                     "working provider.")
        else:
            MODEL_CHOICE[chat_id] = key
            tg_edit(chat_id, msg_id, f"🧠 <b>AI set to:</b> "
                                     f"{ai_bot.MODELS[key]['label']}")


def is_owner(chat_id):
    return chat_id in owner_ids()


OWNER_FILE = "owner.json"


def owner_ids():
    ids = set(config.OWNER_IDS)
    try:
        with open(OWNER_FILE, "r", encoding="utf-8") as f:
            ids |= set(json.load(f))
    except Exception:
        pass
    return ids


def claim_owner(chat_id, username=""):
    """Lock the bot to the first chat that sends /start while no owner exists."""
    if owner_ids():
        return False
    with open(OWNER_FILE, "w", encoding="utf-8") as f:
        json.dump([chat_id], f)
    try:
        with open(DM_LOG, "a", encoding="utf-8") as f:
            f.write(f"OWNER LOCKED -> {chat_id} ({username})\n")
    except Exception:
        pass
    return True


def handle_ai_message(chat_id, text):
    """Handle a message sent while the user is in AI mode."""
    text = (text or "").strip()
    if not text:
        return
    # If the user pasted a code block or asks to run code, execute it.
    if text.startswith("```"):
        code = text.strip("` \n")
        if code.lower().startswith("python"):
            code = code[len("python"):].lstrip("\n")
        tg_send(chat_id, "🐍 Running your code…")
        result = ai_bot.run_python(code)
        out = f"<b>Result:</b>\n<pre>{result}</pre>"
        tg_send(chat_id, out)
        ai_bot.push_message(chat_id, "user",
                            f"[user ran this python code:\n{code}\noutput:\n{result}]")
        return
    if text.lower().startswith("run "):
        code = text[4:].strip()
        tg_send(chat_id, "🐍 Running your code…")
        result = ai_bot.run_python(code)
        out = f"<b>Result:</b>\n<pre>{result}</pre>"
        tg_send(chat_id, out)
        ai_bot.push_message(chat_id, "user",
                            f"[user ran this python code:\n{code}\noutput:\n{result}]")
        return

    # Normal chat: ask the AI, with a "typing" indicator.
    tg("sendChatAction", chat_id=chat_id, action="typing")
    if MODEL_CHOICE.get(chat_id):
        avail = dict((k, v) for k, v in ai_bot.available())
        key = MODEL_CHOICE[chat_id]
        if key in avail:
            reply = ai_bot.ask_with(chat_id, text, key)
        else:
            reply, key = ai_bot.ask(chat_id, text)
    else:
        reply, key = ai_bot.ask(chat_id, text)
    tg_send(chat_id, reply)


def handle_join_request(join):
    """Auto-approve anyone requesting to join the OTP group."""
    chat = join.get("chat", {})
    user = join.get("from", {})
    chat_id = chat.get("id")
    user_id = user.get("id")
    if chat_id != config.CHAT_ID or not user_id:
        return
    res = tg("approveChatJoinRequest", chat_id=chat_id, user_id=user_id)
    ok = bool(res and res.get("ok"))
    print(f"[{'OK' if ok else 'FAIL'}] join approved: "
          f"{user.get('first_name', '')} (@{user.get('username', '-')}, "
          f"{user_id})", flush=True)


def main():
    print("Country-menu OTP sender bot running...")
    print("The control panel opens in your private DM via /start.")
    print("OTP cards are streamed to the OTP group (config.CHAT_ID).")
    offset = 0
    while True:
        try:
            upd = tg("getUpdates", offset=offset, timeout=30,
                     allowed_updates=["message", "callback_query",
                                      "chat_join_request"])
            if not upd or not upd.get("ok"):
                time.sleep(1)
                continue
            for u in upd["result"]:
                offset = u["update_id"] + 1
                if "chat_join_request" in u:
                    handle_join_request(u["chat_join_request"])
                elif "callback_query" in u:
                    cb = u["callback_query"]
                    cb_chat = (cb.get("message") or {}).get("chat", {}).get("id")
                    if is_owner(cb_chat):
                        handle_callback(cb)
                    else:
                        # silent: just clear the loading spinner
                        tg("answerCallbackQuery", callback_query_id=cb["id"])
                elif u.get("message", {}).get("text") is not None:
                    chat_id = u["message"]["chat"]["id"]
                    msg_user = u["message"].get("from", {})
                    username = msg_user.get("username", "")
                    txt = u["message"]["text"]
                    log_chat(chat_id, username, msg_user.get("first_name", ""))
                    if not is_owner(chat_id):
                        # first /start claims ownership; anything else is ignored
                        if not owner_ids() and txt.strip().startswith("/start"):
                            claim_owner(chat_id, username)
                        else:
                            continue
                    if txt.startswith("/"):
                        handle_command(chat_id, txt)
                    elif txt in ("▶️ Start", "▶️ Start OTP", "Start"):
                        do_start(chat_id)
                    elif txt in ("🛑 Stop", "🛑 Stop OTP", "Stop"):
                        do_stop(chat_id)
                    elif chat_id in ADDGROUP_MODE:
                        handle_group_text(chat_id, txt)
                    elif chat_id in SEARCH_MODE:
                        handle_search_text(chat_id, txt)
                    elif chat_id in AI_MODE:
                        handle_ai_message(chat_id, txt)
        except KeyboardInterrupt:
            print("\nStopped.")
            stop_sender()
            break
        except Exception as e:
            print("loop error:", e)
            try:
                tg_send(config.CHAT_ID,
                        f"⚠️ <b>Bot loop error</b>\n<code>{e}</code>\n"
                        f"Restarting in 5s…")
            except Exception:
                pass
            time.sleep(5)


if __name__ == "__main__":
    main()