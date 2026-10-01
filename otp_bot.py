import json
import logging
import os
import re
import time

import requests

import config
from countries import COUNTRIES  # full ISO-3166 list: (dial, short, name)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("otp-bot")

# Show every HTTP request/response in real time on the terminal.
logging.getLogger("urllib3.connectionpool").setLevel(logging.DEBUG)
logging.getLogger("urllib3.connectionpool").propagate = True

session = requests.Session()

# ---------- state (dedup) ----------
STATE_FILE = config.STATE_FILE
_seen = None


def load_state():
    global _seen
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            _seen = set(json.load(f))
    else:
        _seen = set()
    log.info("Loaded %d already-forwarded records", len(_seen))


def save_state():
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(list(_seen), f)


def doget(url, params, timeout=30):
    try:
        r = session.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        log.warning("Request failed: %s", e)
        return None
    except ValueError:
        log.warning("Non-JSON response")
        return None


# ---------- CR API ----------
def fetch_otps():
    params = {
        "token": config.API_TOKEN,
        "records": config.RECORDS,
    }
    resp = doget(config.API_URL, params)
    if not resp:
        return None
    if resp.get("status") != "success":
        log.info("API status: %s - %s", resp.get("status"), resp.get("msg"))
        return []
    return resp.get("data") or []


def record_key(rec):
    # No unique ID in the API, so dedup with the fields that make a record unique.
    return (str(rec.get("dt")), str(rec.get("num")), str(rec.get("message")))


def new_records(records):
    fresh = []
    for rec in records:
        key = record_key(rec)
        if key not in _seen:
            fresh.append(rec)
            _seen.add(key)
    # API returns newest-first; forward oldest-first so order in the group makes sense.
    fresh.reverse()
    return fresh


# ---------- Telegram ----------
def tg_url(method):
    return f"https://api.telegram.org/bot{config.BOT_TOKEN}/{method}"


TARGET_FILE = "target.json"


def get_target_chat_id():
    """Where OTP cards go: owner-chosen group if set, else config.CHAT_ID."""
    try:
        with open(TARGET_FILE, "r", encoding="utf-8") as f:
            tid = int(json.load(f).get("chat_id", 0))
            if tid:
                return tid
    except Exception:
        pass
    return config.CHAT_ID


def get_target_title():
    try:
        with open(TARGET_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("title", "")
    except Exception:
        return ""


def set_target_chat_id(chat_id, title=""):
    with open(TARGET_FILE, "w", encoding="utf-8") as f:
        json.dump({"chat_id": chat_id, "title": title}, f)


def tg_send(text, copy_otp=None):
    payload = {
        "chat_id": get_target_chat_id(),
        "text": text,
        "parse_mode": "HTML",
    }
    if copy_otp:
        payload["reply_markup"] = {
            "inline_keyboard": [
                [{"text": copy_otp, "copy_text": {"text": copy_otp}}],
                [{"text": config.CHANNEL_NAME, "url": config.CHANNEL_URL}],
                [{"text": config.NUMBER_BOT_NAME, "url": config.NUMBER_BOT_URL}],
            ]
        }
    for attempt in range(5):
        try:
            r = session.post(
                tg_url("sendMessage"),
                json=payload,
                timeout=30,
            )
            data = r.json()
            if data.get("ok"):
                return True
            if data.get("error_code") == 429:
                retry = data.get("parameters", {}).get("retry_after", 5)
                log.warning("Telegram rate-limited, retrying in %ss", retry)
                time.sleep(retry + 1)
                continue
            log.warning("Telegram send failed: %s", data)
            return False
        except requests.RequestException as e:
            log.warning("Telegram send error: %s", e)
            return False
    log.warning("Telegram send failed after retries")
    return False


def tg_me():
    try:
        r = session.get(tg_url("getMe"), timeout=30)
        return r.json()
    except requests.RequestException:
        return None


def flag_emoji(short):
    """Build a flag emoji from an ISO2 code, e.g. 'IN' -> '🇮🇳'."""
    if len(short) != 2:
        return ""
    return "".join(chr(127397 + ord(c)) for c in short.upper())


# Lookup tables (dial prefix -> flag/name/short) built from the full country list.
PREFIX_FLAGS = [(dial, flag_emoji(short)) for dial, short, _name in COUNTRIES]
COUNTRY_NAMES = {}
COUNTRY_SHORT = {}
for _dial, short, name in COUNTRIES:
    COUNTRY_NAMES.setdefault(_dial, name)
    COUNTRY_SHORT.setdefault(_dial, short)
# sort longer prefixes first so e.g. "1" doesn't eat "1 23"
PREFIX_FLAGS.sort(key=lambda p: len(p[0]), reverse=True)


def country_flag_name(number):
    prefix = _country_prefix(number)
    if not prefix:
        return ""
    flag = ""
    for p, f in PREFIX_FLAGS:
        if p == prefix:
            flag = f
            break
    name = COUNTRY_NAMES.get(prefix, "")
    return f"{flag} {name}".strip()


def number_prefix(number):
    digits = "".join(ch for ch in str(number) if ch.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("+"):
        digits = digits[1:]
    return digits[:7]


def _country_prefix(number):
    digits = "".join(ch for ch in str(number) if ch.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("+"):
        digits = digits[1:]
    for prefix, _flag in PREFIX_FLAGS:
        if digits.startswith(prefix):
            return prefix
    return ""


def flag_txt(number):
    prefix = _country_prefix(number)
    if not prefix:
        return ""
    for p, f in PREFIX_FLAGS:
        if p == prefix:
            return f
    return ""


def detect_lang(msg):
    """Best-effort language tag for the SMS text (EN by default)."""
    msg = (msg or "").lower()
    if not msg:
        return "EN"
    if re.search(r"[\u0600-\u06ff]", msg):
        return "AR"
    if re.search(r"[\u0900-\u097f]", msg):
        return "HI"
    if re.search(r"[\u0400-\u04ff]", msg):
        return "RU"
    if re.search(r"[\u4e00-\u9fff]", msg):
        return "ZH"
    if re.search(r"[\u3040-\u30ff]", msg):
        return "JA"
    if re.search(r"[\uac00-\ud7af]", msg):
        return "KO"
    words = dict(
        EN=["your", "code", "otp", "verification", "verify", "password", "login", "security"],
        ES=["codigo", "código", "verificac"],
        FR=["votre", "vérification", "code de", "confidentialite"],
        IT=["codice", "verifica", "password"],
        DE=["bestätigung", "einmal", "passwort"],
        PT=["codigo", "código", "verifica"],
        TR=["doğrulama", "kod", "sifre", "hesap"],
        ID=["kode", "verifikasi", "sandi"],
        VI=["ma xac nhan", "xác nhận", "otp"],
        NL=["bevestiging", "code", "wachtwoord"],
    )
    scores = {k: sum(msg.count(w) for w in ws) for k, ws in words.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] else "EN"


def find_otp(msg):
    # Common markers then a number, or any 4-8 digit sequence.
    m = re.search(
        r"(?i)(?:code|otp|verif(?:ication)?|using|pin|password|auth|token)\D{0,12}?(\d{4,8})",
        msg,
    )
    if m:
        return m.group(1)
    m = re.search(r"(?<!\d)(\d{5,8})(?!\d)", msg)
    if m:
        return m.group(1)
    return ""


def mask_number(number):
    digits = "".join(ch for ch in str(number) if ch.isdigit())
    if len(digits) <= 7:
        return digits
    return digits[:3] + "****" + digits[-2:]


CLI_SHORT = {
    "facebook": "FB",
    "fb": "FB",
    "whatsapp": "WS",
    "whats app": "WS",
    "wa": "WS",
    "telegram": "TG",
    "tg": "TG",
    "instagram": "IG",
    "ig": "IG",
    "imo": "IMO",
    "im": "IMO",
    "chatgpt": "GPT",
    "gpt": "GPT",
    "twitter": "TW",
    "x": "X",
    "tiktok": "TT",
    "snapchat": "SC",
    "gmail": "GM",
    "google": "GO",
    "outlook": "OL",
    "microsoft": "MS",
    "ms": "MS",
    "paypal": "PP",
    "amazon": "AMZ",
    "ebay": "EBAY",
    "netflix": "NF",
    "uber": "UBER",
    "ola": "OLA",
    "linkedin": "LIN",
    "discord": "DC",
    "apple": "AP",
    "binance": "BNB",
    "binanace": "BNB",
    "coinbase": "CB",
    "crypto": "CRYPTO",
    "paytm": "PM",
    "phonepe": "PP",
    "gpay": "GP",
    "googlepay": "GP",
    "viber": "VB",
    "line": "LN",
    "skype": "SK",
    "zalando": "ZA",
    "deliveroo": "DL",
    "swiggy": "SW",
    "zomato": "ZM",
    "irctc": "IRCTC",
    "airtel": "AT",
    "jio": "JIO",
    "vi": "VI",
    "vodafone": "VI",
    "sbi": "SBI",
    "hdfc": "HDFC",
    "icici": "ICICI",
    "axis": "AXIS",
    "kraken": "KR",
    "bybit": "BY",
    "okx": "OKX",
    "kucoin": "KU",
    "blizzard": "BZ",
    "steam": "ST",
    "epic": "EP",
    "rockstar": "RS",
}


def shorten_cli(name):
    name = (name or "").strip()
    key = name.lower()
    if key in CLI_SHORT:
        return CLI_SHORT[key]
    # match by full-name substring, e.g. "facebook (via)" -> FB
    for full, short in CLI_SHORT.items():
        if full in key:
            return short
    return name


def make_reference():
    import random as _r
    digits4 = "".join(_r.choice("0123456789") for _ in range(4))
    digits3 = "".join(_r.choice("0123456789") for _ in range(3))
    return f"{digits4}SYRx{digits3}"


def country_flag(number):
    prefix = _country_prefix(number)
    for p, f in PREFIX_FLAGS:
        if p == prefix:
            return f
    return ""


def country_name(number):
    prefix = _country_prefix(number)
    return COUNTRY_NAMES.get(prefix, "")


def country_short(number):
    prefix = _country_prefix(number)
    return COUNTRY_SHORT.get(prefix, "")


def country_dial(number):
    prefix = _country_prefix(number)
    return f"+{prefix}" if prefix else ""


# app/sender name (lowercased) or its short code -> icon emoji
APP_ICONS = {
    "facebook": "📘", "fb": "📘",
    "whatsapp": "💬", "whats app": "💬", "wa": "💬",
    "telegram": "✈️", "tg": "✈️",
    "instagram": "📸", "ig": "📸",
    "imo": "💙", "im": "💙",
    "chatgpt": "🤖", "gpt": "🤖",
    "twitter": "🐦", "x": "🐦",
    "tiktok": "🎵", "tt": "🎵",
    "snapchat": "👻", "sc": "👻",
    "gmail": "📧", "gm": "📧",
    "google": "🔎", "go": "🔎",
    "outlook": "📧", "ol": "📧",
    "microsoft": "🪟", "ms": "🪟",
    "paypal": "💰", "pp": "💰",
    "amazon": "📦", "amz": "📦",
    "ebay": "🛒", "ebay": "🛒",
    "netflix": "🎬", "nf": "🎬",
    "uber": "🚗", "uber": "🚗",
    "ola": "🛵", "ola": "🛵",
    "linkedin": "💼", "lin": "💼",
    "discord": "🎮", "dc": "🎮",
    "apple": "", "ap": "",
    "binance": "🪙", "bnb": "🪙", "binanace": "🪙",
    "coinbase": "🪙", "cb": "🪙",
    "crypto": "🪙", "crypto": "🪙",
    "paytm": "💳", "pm": "💳",
    "phonepe": "💳", "pp": "💳",
    "gpay": "💳", "gp": "💳",
    "googlepay": "💳",
    "viber": "📞", "vb": "📞",
    "line": "💬", "ln": "💬",
    "skype": "📞", "sk": "📞",
    "zalando": "🛍️", "za": "🛍️",
    "deliveroo": "🛵", "dl": "🛵",
    "swiggy": "🍔", "sw": "🍔",
    "zomato": "🍔", "zm": "🍔",
    "airtel": "📶", "at": "📶",
    "jio": "📶", "jio": "📶",
    "vi": "📶", "vi": "📶",
    "vodafone": "📶",
    "bank": "🏦", "sbi": "🏦", "hdfc": "🏦", "icici": "🏦", "axis": "🏦",
    "steam": "🎮", "st": "🎮",
    "epic": "🎮", "ep": "🎮",
    "blizzard": "🎮", "bz": "🎮",
    "rockstar": "🎮", "rs": "🎮",
    "kraken": "🪙", "kr": "🪙",
    "bybit": "🪙", "by": "🪙",
    "okx": "🪙", "okx": "🪙",
    "kucoin": "🪙", "ku": "🪙",
    "hitv": "📺",
}


def app_icon(name):
    key = (name or "").strip().lower()
    if key in APP_ICONS:
        return APP_ICONS[key]
    for full, icon in APP_ICONS.items():
        if full in key and icon:
            return icon
    return ""


def number_ref(number):
    import random as _r
    prefix = _country_prefix(number) or "0"
    digits3 = "".join(_r.choice("0123456789") for _ in range(3))
    return f"+{prefix}SYRx{digits3}"


def format_record(rec, flag=None, short=None):
    msg = (rec.get("message") or "").strip()
    app_name = rec.get("cli") or ""
    number = rec.get("num") or ""
    otp = find_otp(msg) or "\u2014"
    if flag is None:
        flag = country_flag(number)
    if short is None:
        short = country_short(number) or "\u2014"
    lang = detect_lang(msg)
    cli = shorten_cli(app_name)
    app_ic = app_icon(app_name)
    hidden = mask_number(number)
    nref = number_ref(number)
    service = f"{app_ic} {cli}" if app_ic else cli
    return (
        "┌──[ <b>𝙎𝙔𝙍𝙭_𝙊𝙏𝙋</b> ]──┐\n"
        "│\n"
        f"│ &gt; COUNTRY : {flag} <b>{short}</b>\n"
        f"│ &gt; LANG    : <code>{lang}</code>\n"
        f"│ &gt; SERVICE : <b>{service}</b>\n"
        f"│ &gt; NUMBER  : <code>{nref}</code>\n"
        f"│ &gt; OTP     : <code>{otp}</code>\n"
        f"│ &gt; PREFIX  : <code>{number_prefix(number)}</code>\n"
        "│\n"
        "└──[ <b>@yaufee</b> ]──┘"
    )


# ---------- main loop ----------
def main():
    load_state()
    me = tg_me()
    if not me or not me.get("ok"):
        log.error("Could not verify bot token. Check config.BOT_TOKEN")
        return
    log.info("Bot @%s started. Forwarding OTPs to chat %s", me["result"]["username"], config.CHAT_ID)

    consecutive_errors = 0
    while True:
        try:
            records = fetch_otps()
            if records is None:
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    log.error("API unreachable; continuing to retry")
            else:
                consecutive_errors = 0
                if records:
                    log.info("Polled API: got %d record(s)", len(records))
                fresh = new_records(records)
                for rec in fresh:
                    log.info(
                        "Forwarding OTP -> num=%s cli=%s dt=%s",
                        rec.get("num"), rec.get("cli"), rec.get("dt"),
                    )
                    otp_code = find_otp(rec.get("message") or "")
                    ok = tg_send(format_record(rec), otp_code or None)
                    if not ok:
                        # Re-queue: forget the key so we retry next poll.
                        log.warning("Forward failed for %s - will retry", rec.get("num"))
                        _seen.discard(record_key(rec))
                    else:
                        time.sleep(0.3)  # respect Telegram rate limits
                if fresh:
                    save_state()
        except KeyboardInterrupt:
            log.info("Stopped by user")
            save_state()
            break
        except Exception as e:
            log.exception("Unexpected error: %s", e)
        time.sleep(config.POLL_INTERVAL)


if __name__ == "__main__":
    main()
