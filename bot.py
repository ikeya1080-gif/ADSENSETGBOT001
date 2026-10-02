# bot.py — V4 ADS BOT — Railway edition
# Config: sirf BOT_TOKEN aur ADMIN_ID (environment variables)

import asyncio
import json
import os
import pathlib
import re
import secrets
import sqlite3
import string
import time
from datetime import datetime, timedelta

from telethon import TelegramClient, events, Button
from telethon.errors import FloodWaitError, SessionPasswordNeededError
import telethon.errors as _tgerr
from telethon.tl.functions.updates import GetStateRequest
from telethon.sessions import StringSession
import telethon
from telethon.tl import types as _tltypes   # FIX: entity classes resolved from the installed Telethon

# ─────────────────────────── CONFIG ──────────────────────────
# Railway me sirf 2 variables set karne hain:  BOT_TOKEN  aur  ADMIN_ID
def _require_env(name):
    val = os.environ.get(name, "").strip()
    if not val:
        raise SystemExit(f"❌ Environment variable {name} set nahi hai. Railway > Variables me add karo.")
    return val

BOT_TOKEN = "8997744670:AAGoEfN3w7sl9x77w9SCe5OfXj24ZEoGbcA"
try:
    ADMIN_ID = int(_require_env("ADMIN_ID"))
except ValueError:
    raise SystemExit("❌ ADMIN_ID sirf number hona chahiye (apna Telegram user ID).")

# Telethon ko API_ID/API_HASH internally chahiye (user account login ke liye) —
# yeh ab env me dene ki zarurat nahi. Chaho to API_ID/API_HASH env se override kar sakte ho.
API_ID   = int(os.environ.get("API_ID", "24244418"))
API_HASH = os.environ.get("API_HASH", "b2673deba5561827f53e82b6161fe6f4")

# Data folder: Railway Volume ho to wahan (restart pe data safe), warna local folder
DATA_DIR = (os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
            or os.environ.get("DATA_DIR")
            or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.environ.get("DB_FILE", os.path.join(DATA_DIR, "bot_data.db"))
WELCOME_PHOTO = str(pathlib.Path(__file__).parent / "welcome.jpg")
MAX_ACCOUNTS    = 999  # No limit
MAX_FAILS       = 5

print(f"Telethon {telethon.__version__} | DB: {DB_FILE}")

# ─────────────────────────── DATABASE ────────────────────────
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
c    = conn.cursor()
# Performance optimizations
c.execute("PRAGMA journal_mode=WAL")    # Faster concurrent writes
c.execute("PRAGMA synchronous=NORMAL")  # Balance speed vs safety
c.execute("PRAGMA cache_size=10000")    # 10MB cache
c.execute("PRAGMA temp_store=MEMORY")   # Temp tables in RAM
conn.commit()
c.execute("""CREATE TABLE IF NOT EXISTS users(
    user_id       INTEGER PRIMARY KEY,
    username      TEXT    DEFAULT '',
    trial_granted INTEGER DEFAULT 0,
    trial_expires TEXT,
    is_banned     INTEGER DEFAULT 0,
    joined_at     TEXT    DEFAULT CURRENT_TIMESTAMP)""")
c.execute("""CREATE TABLE IF NOT EXISTS user_accounts(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER,
    phone       TEXT UNIQUE,
    session_str TEXT,
    added_at    TEXT DEFAULT CURRENT_TIMESTAMP)""")
c.execute("""CREATE TABLE IF NOT EXISTS access_codes(
    code       TEXT PRIMARY KEY,
    days_valid INTEGER,
    created_at TEXT,
    claimed_by INTEGER,
    claimed_at TEXT,
    expires_at TEXT,
    is_active  INTEGER DEFAULT 1)""")
c.execute("""CREATE TABLE IF NOT EXISTS scheduled_tasks(
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER,
    phone            TEXT,
    messages_json    TEXT    DEFAULT '[]',
    interval_seconds INTEGER,
    next_run         TEXT,
    current_msg_idx  INTEGER DEFAULT 0,
    fail_count       INTEGER DEFAULT 0,
    is_active        INTEGER DEFAULT 1,
    created_at       TEXT    DEFAULT CURRENT_TIMESTAMP)""")
c.execute("""CREATE TABLE IF NOT EXISTS admins(
    user_id  INTEGER PRIMARY KEY,
    username TEXT DEFAULT '',
    added_by INTEGER,
    added_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
c.execute("""CREATE TABLE IF NOT EXISTS code_requests(
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    requested_by INTEGER,
    days         INTEGER,
    status       TEXT DEFAULT 'pending',
    code         TEXT DEFAULT '',
    requested_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
c.execute("""CREATE TABLE IF NOT EXISTS logs(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT,
    admin_id   INTEGER,
    admin_name TEXT,
    code       TEXT    DEFAULT '',
    details    TEXT    DEFAULT '',
    created_at TEXT    DEFAULT CURRENT_TIMESTAMP)""")
# Add columns if not exists
try:
    c.execute("ALTER TABLE users ADD COLUMN is_protected INTEGER DEFAULT 0")
except Exception: pass
try:
    c.execute("ALTER TABLE access_codes ADD COLUMN created_by INTEGER DEFAULT NULL")
except Exception: pass
try:
    c.execute("ALTER TABLE scheduled_tasks ADD COLUMN msg_ids_json TEXT DEFAULT '[]'")
except Exception: pass
try:
    c.execute("ALTER TABLE scheduled_tasks ADD COLUMN source_chat_id INTEGER DEFAULT NULL")
except Exception: pass
try:
    # custom_targets: JSON list of @usernames or invite links to add
    c.execute("ALTER TABLE scheduled_tasks ADD COLUMN custom_targets TEXT DEFAULT '[]'")
except Exception: pass
try:
    # send_to: "all" | "groups" | "channels"
    c.execute("ALTER TABLE scheduled_tasks ADD COLUMN send_to TEXT DEFAULT 'all'")
except Exception: pass
for _ix in (
    "CREATE INDEX IF NOT EXISTS ix_accounts_user   ON user_accounts(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_tasks_user      ON scheduled_tasks(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_tasks_active    ON scheduled_tasks(is_active)",
    "CREATE INDEX IF NOT EXISTS ix_codes_claimed   ON access_codes(claimed_by, is_active)",
    "CREATE INDEX IF NOT EXISTS ix_codes_creator   ON access_codes(created_by)",
    "CREATE INDEX IF NOT EXISTS ix_requests_status ON code_requests(status)",
    "CREATE INDEX IF NOT EXISTS ix_requests_by     ON code_requests(requested_by)",
):
    c.execute(_ix)
conn.commit()

# ─────────────────────────── GLOBALS ─────────────────────────
import os as _os
_default_sess = _os.path.join(DATA_DIR, "bot_session")
_sess_path    = _os.environ.get("SESSION_PATH", _default_sess)
bot = TelegramClient(_sess_path, API_ID, API_HASH, connection_retries=5)
pending: dict    = {}
scheduler_tasks: dict = {}
db_lock          = None

# ─────────────────────────── UTILS ───────────────────────────
def is_super_admin(uid):
    return uid == ADMIN_ID

def is_admin(uid):
    if uid == ADMIN_ID: return True
    return c.execute("SELECT user_id FROM admins WHERE user_id=?", (uid,)).fetchone() is not None

async def db_write(sql, params=()):
    async with db_lock:
        c.execute(sql, params)
        conn.commit()
        return c.lastrowid

async def log_event(event_type, admin_id, admin_name, code="", details=""):
    """Log important events: code_created, code_approved, code_claimed"""
    await db_write(
        "INSERT INTO logs(event_type,admin_id,admin_name,code,details) VALUES(?,?,?,?,?)",
        (event_type, admin_id, admin_name, code, details)
    )

def now_utc():    return datetime.utcnow()
def now_iso():    return now_utc().isoformat()
def parse_iso(s): return datetime.fromisoformat(s)

def fmt_mins(secs):
    m = secs // 60
    if m < 60:    return f"{m} min"
    if m == 60:   return "1 hour"
    if m == 1440: return "Daily"
    h, r = divmod(m, 60)
    return f"{h}h {r}m" if r else f"{h}h"

def msgs_list(j):
    try:
        v = json.loads(j or "[]")
        return v if isinstance(v, list) else [str(v)]
    except Exception:
        return [j] if j else []

# ───────────────── ENTITY HELPERS (Premium Custom Emoji safe) ─────────────────
# FIX: ONE shared implementation for capture / edit / send-now / scheduler.
# Entities are stored as JSON dicts inside the EXISTING scheduled_tasks.msg_ids_json
# field (no schema change).  Offsets/lengths are stored exactly as Telegram supplied
# them (UTF-16 code units) and are never recalculated, except for the exact UTF-16
# length of a stripped prefix when leading whitespace is removed from the text.
_ENTITY_NAMES = (
    "MessageEntityBold", "MessageEntityItalic", "MessageEntityCode", "MessageEntityPre",
    "MessageEntityUnderline", "MessageEntityStrike", "MessageEntityBlockquote",
    "MessageEntitySpoiler", "MessageEntityTextUrl", "MessageEntityCustomEmoji",
)
ENTITY_MAP = {n: getattr(_tltypes, n) for n in _ENTITY_NAMES if hasattr(_tltypes, n)}

def _u16len(t):
    """Length of a str in UTF-16 code units (Telegram's offset unit)."""
    return len((t or "").encode("utf-16-le")) // 2

def entity_to_dict(e):
    d = {"type": type(e).__name__, "offset": e.offset, "length": e.length,
         "data": getattr(e, "url", None) or getattr(e, "language", None)}
    # FIX: Preserve Telegram Custom Emoji document_id (stored as str -> JSON-safe for 64-bit ids)
    if type(e).__name__ == "MessageEntityCustomEmoji":
        d["document_id"] = str(e.document_id)
    if getattr(e, "collapsed", None):          # newer Telethon: collapsible blockquote
        d["collapsed"] = True
    return d

def entities_to_json(raw_text, entities, cp_start=0, cp_end=None):
    """Serialise supported entities of `raw_text` (optionally only for raw_text[cp_start:cp_end]).
    Returns a JSON string ("[]" when nothing to keep)."""
    if not entities: return "[]"
    raw_text = raw_text or ""
    if cp_end is None: cp_end = len(raw_text)
    lo, hi = _u16len(raw_text[:cp_start]), _u16len(raw_text[:cp_end])
    out = []
    for e in entities:
        try:
            if type(e).__name__ not in ENTITY_MAP: continue
            d = entity_to_dict(e)
            st, en = max(e.offset, lo), min(e.offset + e.length, hi)
            if en <= st: continue
            d["offset"], d["length"] = st - lo, en - st
            out.append(d)
        except Exception as ex:
            print(f"⚠️ entity serialise skipped ({type(ex).__name__}: {ex})")
    return json.dumps(out)

def capture_text_and_entities(message, fallback_text, cp_start=None, cp_end=None):
    """Return (text, ents_json) for a received bot message.
    No supported entities -> (fallback_text, "[]")  == exactly the old behaviour.
    With entities -> raw Telegram text (message.message) + entities, so custom emoji survive."""
    try:
        raw = message.message or ""
        if cp_start is None: cp_start, cp_end = 0, len(raw)
        seg = raw[cp_start:cp_end]
        if not seg.strip(): return fallback_text, "[]"
        s0 = cp_start + (len(seg) - len(seg.lstrip()))
        e0 = cp_start + len(seg.rstrip())
        ej = entities_to_json(raw, message.entities, s0, e0)
        if ej == "[]": return fallback_text, "[]"
        return raw[s0:e0], ej
    except Exception as ex:
        print(f"⚠️ entity capture failed, using plain text ({type(ex).__name__}: {ex})")
        return fallback_text, "[]"

def message_has_media(message):
    m = getattr(message, "media", None)
    return bool(m) and type(m).__name__ != "MessageMediaWebPage"

def rebuild_entities(ej, text=None, ctx=""):
    """JSON (new OR old format) -> list of Telethon entities, or None.
    A malformed entity is logged and skipped; it never drops the others."""
    if not ej: return None
    try:
        elist = json.loads(ej) if isinstance(ej, (str, bytes)) else ej
    except Exception as ex:
        print(f"⚠️ [{ctx}] entity JSON unreadable ({type(ex).__name__})"); return None
    if isinstance(elist, dict): elist = [elist]
    if not isinstance(elist, list): return None
    limit  = _u16len(text) if text is not None else None
    result = []
    for ed in elist:
        try:
            if not isinstance(ed, dict): continue
            name = ed.get("type"); cls = ENTITY_MAP.get(name)
            if not cls: continue
            off, ln = int(ed["offset"]), int(ed["length"])
            if off < 0 or ln <= 0: continue
            if limit is not None and off + ln > limit:
                print(f"⚠️ [{ctx}] {name} out of bounds ({off}+{ln}>{limit}) — skipped"); continue
            d = ed.get("data")
            if name == "MessageEntityCustomEmoji":
                did = ed.get("document_id")
                if did in (None, ""):      # old record: id was never saved -> cannot rebuild
                    print(f"⚠️ [{ctx}] custom emoji without document_id (old task) — skipped"); continue
                # FIX: Rebuild MessageEntityCustomEmoji with offset, length AND document_id
                result.append(cls(offset=off, length=ln, document_id=int(did)))
            elif name == "MessageEntityTextUrl":
                if not d: continue
                result.append(cls(offset=off, length=ln, url=d))
            elif name == "MessageEntityPre":
                result.append(cls(offset=off, length=ln, language=d or ""))   # language is required
            elif name == "MessageEntityBlockquote" and ed.get("collapsed"):
                try: result.append(cls(offset=off, length=ln, collapsed=True))
                except TypeError: result.append(cls(offset=off, length=ln))
            else:
                result.append(cls(offset=off, length=ln))
        except Exception as ex:
            print(f"⚠️ [{ctx}] bad entity skipped ({type(ex).__name__}: {ex})")
    return result or None

def parse_msg_ids_json(raw_json):
    """Tolerant reader for scheduled_tasks.msg_ids_json -> (pairs, ents_all).
    Handles: new dict {"pairs","ents"}, old dict, old plain list, '[]', NULL, garbage."""
    try:
        raw = json.loads(raw_json or "{}")
    except Exception:
        return [], []
    if isinstance(raw, dict):
        pairs, ents = raw.get("pairs", []), raw.get("ents", [])
    elif isinstance(raw, list):
        pairs, ents = [], raw
    else:
        return [], []
    if not isinstance(pairs, list): pairs = []
    if not isinstance(ents, list):  ents = []
    if ents and all(isinstance(x, dict) for x in ents):   # flat entity list of ONE message
        ents = [ents]
    return pairs, ents

def pad_capture(st):
    """FIX: keep msg_ids / peers / entities_list / media_flags index-aligned with messages
    (text-typed messages used to append only to `messages`, shifting every later index)."""
    n = len(st.get("messages", []))
    for k, fill in (("msg_ids", None), ("peers", None), ("entities_list", "[]"), ("media_flags", False)):
        lst = st.setdefault(k, [])
        while len(lst) < n: lst.append(fill)

def gen_code(n=10):
    return "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(n))

def upsert_user(uid, uname=""):
    row = c.execute("SELECT user_id FROM users WHERE user_id=?", (uid,)).fetchone()
    if row: c.execute("UPDATE users SET username=? WHERE user_id=?", (uname or "", uid))
    else:   c.execute("INSERT INTO users(user_id,username) VALUES(?,?)", (uid, uname or ""))
    conn.commit()

async def check_access(uid):
    if is_admin(uid): return True, "ADMIN"
    row = c.execute("SELECT is_banned FROM users WHERE user_id=?", (uid,)).fetchone()
    if row and row[0]: return False, "BANNED"
    code = c.execute(
        "SELECT code,expires_at FROM access_codes WHERE claimed_by=? AND is_active=1", (uid,)
    ).fetchone()
    if code and now_utc() <= parse_iso(code[1]): return True, code[0]
    trial = c.execute(
        "SELECT trial_expires FROM users WHERE user_id=? AND trial_granted=1", (uid,)
    ).fetchone()
    if trial and now_utc() <= parse_iso(trial[0]): return True, "TRIAL"
    return False, None

# ─────────────────── ACCOUNT / SESSION HELPERS ───────────────────
def _errs(*names):
    """Telethon error classes (jo is version me exist karte hain) ka tuple."""
    return tuple(getattr(_tgerr, n) for n in names if hasattr(_tgerr, n))

# Telegram ne session terminate / revoke / ban kiya
DEAD_ERRORS  = _errs("AuthKeyUnregisteredError", "SessionRevokedError", "SessionExpiredError",
                     "UserDeactivatedError", "UserDeactivatedBanError", "AuthKeyDuplicatedError",
                     "AuthKeyInvalidError", "AuthKeyPermEmptyError")
CODE_EXPIRED = _errs("PhoneCodeExpiredError")
CODE_INVALID = _errs("PhoneCodeInvalidError", "PhoneCodeEmptyError")
PW_INVALID   = _errs("PasswordHashInvalidError")

_last_ok       = {}      # phone -> last time session verified OK
_last_code_req = {}      # user_id -> last OTP request time
_dead_busy     = set()   # phones jinka cleanup chal raha hai

def norm_phone(raw):
    """'+91 98765-43210' -> '+919876543210' (galat ho to None)."""
    d = re.sub(r"\D", "", raw or "")
    return "+" + d if 8 <= len(d) <= 15 else None

async def close(cl):
    try: await cl.disconnect()
    except Exception: pass

async def probe_client(phone, sess_str):
    """(client, state). state: 'ok' | 'dead' (Telegram se session terminate) | 'error' (network etc.)"""
    cl = None
    try:
        cl = TelegramClient(StringSession(sess_str), API_ID, API_HASH)
        await cl.connect()
        await cl(GetStateRequest())          # asli authorization check
        _last_ok[phone] = time.time()
        return cl, "ok"
    except DEAD_ERRORS:
        state = "dead"
    except Exception:
        state = "error"
    if cl: await close(cl)
    return None, state

async def open_client(phone, sess_str):
    cl, state = await probe_client(phone, sess_str)
    if state == "dead":
        asyncio.create_task(handle_dead(phone, sess_str))
    return cl

def stop_tasks_for(phone, keep=None):
    """Is phone ke saare running tasks cancel + DB me inactive (rows rehte hain)."""
    for (tid,) in c.execute("SELECT id FROM scheduled_tasks WHERE phone=?", (phone,)).fetchall():
        t = scheduler_tasks.pop(tid, None)
        if t and t is not keep: t.cancel()
    c.execute("UPDATE scheduled_tasks SET is_active=0 WHERE phone=?", (phone,))
    conn.commit()

async def handle_dead(phone, sess_str=None):
    """Session Telegram se terminate/ban ho gaya -> account bot se hatao, tasks roko, owner ko batao."""
    if phone in _dead_busy: return
    _dead_busy.add(phone)
    try:
        async with db_lock:
            row = c.execute("SELECT user_id,session_str FROM user_accounts WHERE phone=?", (phone,)).fetchone()
            if not row: return
            if sess_str is not None and row[1] != sess_str: return   # account dobara add ho chuka — naya session
            owner = row[0]
            stop_tasks_for(phone, keep=asyncio.current_task())
            c.execute("DELETE FROM user_accounts WHERE phone=?", (phone,))
            conn.commit()
        _last_ok.pop(phone, None)
        try:
            await bot.send_message(owner,
                f"⚠️ `{phone}` ka session Telegram se terminate ho gaya.\n\n"
                "Isliye account bot se hata diya aur uske tasks band kar diye.\n"
                "Dobara use karna ho to /addaccount se add karo.")
        except Exception: pass
    finally:
        _dead_busy.discard(phone)

async def _logout_session(sess_str):
    """Bot ka session Telegram (Active Sessions) se bhi hata do. Best-effort."""
    cl = None
    try:
        cl = TelegramClient(StringSession(sess_str), API_ID, API_HASH)
        await asyncio.wait_for(cl.connect(), 15)
        await asyncio.wait_for(cl.log_out(), 15)
    except Exception: pass
    finally:
        if cl: await close(cl)

async def remove_account(phone, logout=True):
    """Account hatao: tasks band, DB se delete, Telegram session logout. True = account tha."""
    async with db_lock:
        row = c.execute("SELECT session_str FROM user_accounts WHERE phone=?", (phone,)).fetchone()
        if not row: return False
        stop_tasks_for(phone)
        c.execute("DELETE FROM user_accounts WHERE phone=?", (phone,))
        conn.commit()
    _last_ok.pop(phone, None)
    if logout: asyncio.create_task(_logout_session(row[0]))
    return True

async def verify_accounts(uid=None, ttl=600):
    """Accounts check karo (parallel, 5 ek saath). Dead ho to auto-remove. 'ttl' sec me verify ho chuke skip."""
    if uid is None: rows = c.execute("SELECT phone,session_str FROM user_accounts").fetchall()
    else:           rows = c.execute("SELECT phone,session_str FROM user_accounts WHERE user_id=?", (uid,)).fetchall()
    now   = time.time()
    stale = [(p, ss) for p, ss in rows if now - _last_ok.get(p, 0) > ttl]
    if not stale: return
    sem = asyncio.Semaphore(5)
    async def one(p, ss):
        async with sem:
            cl, st = await probe_client(p, ss)
        if cl: await close(cl)
        if st == "dead": await handle_dead(p, ss)
    await asyncio.gather(*[one(p, ss) for p, ss in stale])

async def drop_pending(uid):
    """Pending state hatao aur uska login client band karo (leak nahi)."""
    p = pending.pop(uid, None)
    if p and p.get("client"): await close(p["client"])

async def _phone_available(uid, phone):
    """Number add karne se pehle: kisi ke paas already active to nahi? (ok, error_msg)"""
    row = c.execute("SELECT user_id,session_str FROM user_accounts WHERE phone=?", (phone,)).fetchone()
    if not row: return True, ""
    owner, sess = row
    cl, st = await probe_client(phone, sess)
    if cl: await close(cl)
    if st == "dead":                      # purana session mar chuka — saaf karke aage badho
        await handle_dead(phone, sess); return True, ""
    if st != "ok":
        return False, "⚠️ Abhi number verify nahi ho paya (network). Thodi der baad try karo."
    if owner == uid:
        return False, (f"ℹ️ `{phone}` already tumhare account me active hai.\n"
                       f"Dobara add karna ho to pehle `/removeaccount {phone}` karo.")
    return False, (f"❌ `{phone}` kisi aur user ke account me active hai.\n"
                   f"Owner ya admin se `/removeaccount {phone}` karwao, phir add karo.")

async def _save_account(uid, phone, sess):
    """Naya login save karo. Purane owner ke tasks hata deta hai taaki wo is account se na chalen."""
    old_owner = None
    async with db_lock:
        row = c.execute("SELECT user_id FROM user_accounts WHERE phone=?", (phone,)).fetchone()
        if row and row[0] != uid: old_owner = row[0]
        for (tid, towner) in c.execute("SELECT id,user_id FROM scheduled_tasks WHERE phone=?", (phone,)).fetchall():
            if towner != uid:
                t = scheduler_tasks.pop(tid, None)
                if t: t.cancel()
                c.execute("DELETE FROM scheduled_tasks WHERE id=?", (tid,))
        c.execute("DELETE FROM user_accounts WHERE phone=?", (phone,))
        c.execute("INSERT INTO user_accounts(user_id,phone,session_str) VALUES(?,?,?)", (uid, phone, sess))
        conn.commit()
    _last_ok[phone] = time.time()
    if old_owner:
        try: await bot.send_message(old_owner, f"ℹ️ `{phone}` ab kisi aur user ne add kar liya hai — tumhare account se hata diya gaya.")
        except Exception: pass

def _otp_text(phone, resends=0):
    return (
        f"📩 **OTP bheja gaya!**{' (resend ' + str(resends) + '/3)' if resends else ''}\n\n"
        f"📱 Number: `{phone}`\n\n"
        "⚠️ **Code seedha paste MAT karo** — Telegram use turant expire kar deta hai.\n"
        "Digits ke beech **space ya dash** do:\n"
        "`1 2 3 4 5`   ya   `1-2-3-4-5`\n\n"
        "➡️ Ab code bhejo:\n_(/cancel se wapas)_"
    )

async def _start_login(uid, phone, resends=0):
    """Naye client se OTP bhejo. Returns (ok, message)."""
    cl = None
    try:
        cl = TelegramClient(StringSession(), API_ID, API_HASH)
        await cl.connect()
        sent = await cl.send_code_request(phone)
    except FloodWaitError as fw:
        if cl: await close(cl)
        return False, f"⛔ Telegram ne rok diya — {fw.seconds // 60 + 1} min baad try karo."
    except Exception as e:
        if cl: await close(cl)
        n = type(e).__name__
        if   "PhoneNumberInvalid" in n: txt = "❌ Number galat hai. Country code ke saath sahi number bhejo."
        elif "PhoneNumberBanned"  in n: txt = "🚫 Yeh number Telegram pe banned hai."
        elif "PhoneNumberFlood"   in n: txt = "⛔ Is number pe bahut zyada code requests ho gayi — kuch ghante baad try karo."
        elif "ApiId"              in n: txt = "❌ API ID/HASH invalid hai — owner ko batao."
        else:                           txt = f"❌ OTP bhejne me error:\n`{e}`"
        return False, txt
    now = time.time()
    pending[uid] = {"action": "add_otp", "phone": phone, "client": cl,
                    "phone_code_hash": sent.phone_code_hash,
                    "ts": now, "sent_at": now, "resends": resends, "tries": 0}
    _last_code_req[uid] = now
    return True, _otp_text(phone, resends)

# ─────────────────────────── KEYBOARDS ───────────────────────
def main_kb():
    return [
        [Button.text("➕ Add Account"),   Button.text("📊 My Groups")],
        [Button.text("⏰ Schedule Msg"),  Button.text("🚀 Send Now")],
        [Button.text("📋 My Schedules"), Button.text("🛑 Stop All")],
        [Button.text("⚙️ Settings"),     Button.text("🔑 Redeem Code")],
        [Button.text("💬 Buy Access")],
    ]

def admin_kb(uid=None):
    # Sub Admin keyboard — Pending aur Logs nahi dikhte
    sub_kb = [
        [Button.text("👥 Users"),        Button.text("📱 All Numbers")],
        [Button.text("🔑 Codes"),        Button.text("⏰ All Tasks")],
        [Button.text("➕ Gen Code"),     Button.text("📊 Stats")],
        [Button.text("📢 Broadcast"),    Button.text("👑 Admins")],
        [Button.text("📋 My Requests"),  Button.text("🔙 User Menu")],
    ]
    # Owner keyboard — Pending + Logs with count
    pending_cnt = c.execute("SELECT COUNT(*) FROM code_requests WHERE status='pending'").fetchone()[0]
    pending_btn = "📋 Pending (" + str(pending_cnt) + ")" if pending_cnt > 0 else "📋 Pending"
    owner_kb = [
        [Button.text("👥 Users"),        Button.text("📱 All Numbers")],
        [Button.text("🔑 Codes"),        Button.text("⏰ All Tasks")],
        [Button.text("➕ Gen Code"),     Button.text("📊 Stats")],
        [Button.text("📢 Broadcast"),    Button.text("👑 Admins")],
        [Button.text(pending_btn),       Button.text("📜 Logs")],
        [Button.text("🔙 User Menu")],
    ]
    # uid pass hua hai to check karo, warna default owner kb return karo
    if uid is not None:
        return owner_kb if is_super_admin(uid) else sub_kb
    return owner_kb

def action_btns():
    return [
        [Button.inline("🚀 Send Now",     b"do_send_now")],
        [Button.inline("⏰ Schedule",     b"do_schedule")],
        [Button.inline("📋 My Schedules", b"view_tasks")],
        [Button.inline("❌ Cancel",       b"cx")],
    ]

# ─────────────────────────── WELCOME ─────────────────────────
async def send_welcome(event, caption, buttons):
    try:
        if pathlib.Path(WELCOME_PHOTO).exists():
            await bot.send_file(
                event.chat_id, WELCOME_PHOTO,
                caption=caption, buttons=buttons, parse_mode="markdown"
            )
        else:
            await event.reply(caption, buttons=buttons)
    except Exception:
        await event.reply(caption, buttons=buttons)

# ─────────────────────────── SCHEDULER ───────────────────────
async def run_task(task_id, uid, phone, sess, interval, initial_delay=0):
    if initial_delay > 0:
        await asyncio.sleep(initial_delay)
    while True:
        row = c.execute(
            "SELECT is_active,messages_json,current_msg_idx,fail_count "
            "FROM scheduled_tasks WHERE id=?", (task_id,)
        ).fetchone()
        if not row or not row[0]: break

        active, mj, idx, fails = row
        msgs     = msgs_list(mj)
        if not msgs: await asyncio.sleep(interval); continue

        msg      = msgs[idx % len(msgs)]
        next_idx = (idx + 1) % len(msgs)
        acc = c.execute("SELECT session_str FROM user_accounts WHERE phone=?", (phone,)).fetchone()
        if not acc:   # account bot se hat chuka hai -> task roko
            await db_write("UPDATE scheduled_tasks SET is_active=0 WHERE id=?", (task_id,))
            scheduler_tasks.pop(task_id, None)
            break
        sess = acc[0]
        cl, st = await probe_client(phone, sess)
        if st == "dead":   # Telegram se session terminate hua
            await handle_dead(phone, sess)
            break
        next_run = (now_utc() + timedelta(seconds=interval)).isoformat()

        if cl:
            try:
                # Fresh fetch — ALL groups/channels user is member of
                all_dialogs = []
                try:
                    async for d in cl.iter_dialogs():
                        all_dialogs.append(d)
                except Exception:
                    all_dialogs = await cl.get_dialogs(limit=None)
                groups = [d for d in all_dialogs if d.is_group or d.is_channel]
                sent   = 0
                print(f"Task #{task_id} | {phone} | Found {len(all_dialogs)} dialogs | {len(groups)} groups/channels")
                try:
                    await bot.send_message(uid,
                        f"📊 Task #{task_id} starting\n"
                        f"📱 `{phone}`\n"
                        f"👥 {len(groups)} groups/channels found\n"
                        f"🔄 Sending..."
                    )
                except Exception: pass

                # Get forward pairs + entities
                task_row2 = c.execute(
                    "SELECT msg_ids_json FROM scheduled_tasks WHERE id=?", (task_id,)
                ).fetchone()
                fwd_pairs2, ents_all = parse_msg_ids_json(task_row2[0] if task_row2 else None)
                msg_i = idx % len(msgs)
                # FIX: only trust per-message lists that are aligned with messages_json
                # (old tasks could be misaligned -> wrong entities / wrong forwarded message)
                ents_json2 = ents_all[msg_i] if len(ents_all) == len(msgs) else None
                fwd_pair   = fwd_pairs2[msg_i] if len(fwd_pairs2) == len(msgs) else None
                if not isinstance(fwd_pair, (list, tuple)): fwd_pair = None
                orig_mid   = fwd_pair[0] if fwd_pair and len(fwd_pair) > 0 else None
                orig_peer2 = fwd_pair[1] if fwd_pair and len(fwd_pair) > 1 else None
                fwd_media  = bool(fwd_pair[2]) if fwd_pair and len(fwd_pair) > 2 else False
                legacy_pair = bool(fwd_pair) and len(fwd_pair) <= 2     # saved before this fix

                # FIX: Rebuild MessageEntityCustomEmoji (+ all other entities) from JSON
                entities_to_use = rebuild_entities(ents_json2, text=msg, ctx=f"task #{task_id} msg {msg_i+1}")
                # FIX: log only counts (never secrets) so Railway logs show if custom emoji are really being sent
                if entities_to_use:
                    _n_ce = sum(1 for _e in entities_to_use if type(_e).__name__ == "MessageEntityCustomEmoji")
                    print(f"Task #{task_id} msg {msg_i+1}: {len(entities_to_use)} entities ({_n_ce} custom emoji) will be sent natively")
                elif ents_json2 and ents_json2 != "[]":
                    print(f"⚠️ Task #{task_id} msg {msg_i+1}: stored entities unusable — sending without formatting")

                # FIX: Prefer native entity sending when formatting is available.
                # Forward only when: media is involved, OR there are no entities to preserve,
                # OR the task is an old (pre-fix) record — old behaviour kept for those.
                prefer_forward = bool(orig_mid and orig_peer2) and (
                    legacy_pair or fwd_media or not entities_to_use)

                async def _send_one(target):
                    if prefer_forward:
                        try:
                            await cl.forward_messages(target, orig_mid, orig_peer2)
                            return
                        except FloodWaitError:
                            raise
                        except Exception:
                            if fwd_media or not msg: raise      # media can't be degraded to text
                    if entities_to_use:
                        await cl.send_message(target, msg, formatting_entities=entities_to_use)
                    else:
                        await cl.send_message(target, msg)

                all_targets = [g.entity for g in groups]
                sent = 0
                first_err = None

                for target in all_targets:
                    try:
                        await _send_one(target)
                        sent += 1
                        await asyncio.sleep(1)
                    except FloodWaitError as fw:
                        await asyncio.sleep(fw.seconds + 10)
                        try:
                            await _send_one(target)   # FIX: retry the SAME way (entities kept), not plain text
                            sent += 1
                        except Exception: pass
                    except Exception as ex:
                        if first_err is None: first_err = f"{type(ex).__name__}: {ex}"
                if first_err and sent == 0 and entities_to_use:
                    print(f"⚠️ Task #{task_id}: nothing sent with entities — {first_err}")

                await close(cl)
                await db_write(
                    "UPDATE scheduled_tasks SET fail_count=0,current_msg_idx=?,next_run=? WHERE id=?",
                    (next_idx, next_run, task_id)
                )
                label = f"msg {idx+1}/{len(msgs)}" if len(msgs) > 1 else "msg"
                print(f"Task #{task_id} done | {sent}/{len(groups)} sent")
                try:
                    await bot.send_message(uid,
                        f"✅ Task #{task_id} ({label})\n"
                        f"📤 Sent: **{sent}/{len(groups)}** groups\n"
                        f"📝 `{msg[:60]}{'...' if len(msg)>60 else ''}`")
                except Exception: pass
            except Exception as e:
                await close(cl)
                fails += 1
                await db_write(
                    "UPDATE scheduled_tasks SET fail_count=?,next_run=? WHERE id=?",
                    (fails, next_run, task_id))
                if fails >= MAX_FAILS:
                    await db_write("UPDATE scheduled_tasks SET is_active=0 WHERE id=?", (task_id,))
                    try:
                        await bot.send_message(uid,
                            f"🚫 Task #{task_id} auto-disabled ({MAX_FAILS} errors)\n`{e}`")
                    except Exception: pass
                    break
        else:
            fails += 1
            await db_write("UPDATE scheduled_tasks SET fail_count=?,next_run=? WHERE id=?",
                (fails, next_run, task_id))
            if fails >= MAX_FAILS:
                await db_write("UPDATE scheduled_tasks SET is_active=0 WHERE id=?", (task_id,))
                try:
                    await bot.send_message(uid,
                        f"🚫 Task #{task_id} auto-disabled — `{phone}` connect nahi hua {MAX_FAILS}x")
                except Exception: pass
                break

        await asyncio.sleep(interval)

def start_task(tid, uid, phone, sess, interval, initial_delay=0):
    t = asyncio.create_task(run_task(tid, uid, phone, sess, interval, initial_delay))
    scheduler_tasks[tid] = t

# ─────────────────────────── /start ──────────────────────────
@bot.on(events.NewMessage(pattern=r"^/start"))
async def cmd_start(event):
    uid   = event.sender_id
    uname = getattr(event.sender, "username", "") or ""
    upsert_user(uid, uname)

    if is_super_admin(uid):
        await event.reply(
            "👑 **Welcome Owner!**\n\n"
            "🔑 Tumhare paas full control hai.\n"
            "/help — saari commands",
            buttons=admin_kb(uid)
        ); return

    if is_admin(uid):
        await event.reply(
            "🔰 **Welcome Admin!**\n\n"
            "✅ Tum Sub Admin ho.\n"
            "/help — saari commands",
            buttons=admin_kb(uid)
        ); return

    ok, tag = await check_access(uid)

    if tag == "BANNED":
        await event.reply(
            "🚫 **Access Denied**\n\nTumhara account ban ho gaya hai.\n"
            "Admin se contact karo: @V4_XTRD"
        ); return

    welcome_text = (
        "🍂 **ALEXADS** 🍂\n"
        "**TG Ads Bot**\n\n"
        "🤖 @V4_XTRD_bot\n"
        "👑 Owner: @V4_XTRD\n\n"
        "📣 **Our Channel:** [Alex Store](https://t.me/alexstore037)\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "⚡ _Powerful Telegram Ads Bot_\n"
        "_Send ads to all groups automatically!_\n"
        "━━━━━━━━━━━━━━━━"
    )

    if ok:
        caption = welcome_text + "\n\n✅ **Welcome back! Access active hai.**\nMenu se kaam shuru karo 👇"
        await send_welcome(event, caption, main_kb()); return

    trial = c.execute("SELECT trial_granted FROM users WHERE user_id=?", (uid,)).fetchone()
    if not trial or not trial[0]:
        exp = (now_utc() + timedelta(days=28)).isoformat()
        c.execute("UPDATE users SET trial_granted=1,trial_expires=? WHERE user_id=?", (exp, uid))
        conn.commit()
        caption = (
            welcome_text +
            f"\n\n🎁 **Welcome! Tumhe 10 din ka FREE Trial mila!**\n"
            f"⏳ Valid till: **{exp.split('T')[0]}** (28 din)\n\n👇 Start karo!"
        )
        await send_welcome(event, caption, main_kb())
    else:
        caption = (
            welcome_text +
            "\n\n⏳ **Trial expire ho gaya.**\n\n"
            "🔑 Access ke liye:\n"
            "  /redeem CODE — Code lagao\n"
            "  📩 Contact: @V4_XTRD"
        )
        await send_welcome(event, caption, main_kb())

# ─────────────────────────── /help ───────────────────────────
@bot.on(events.NewMessage(pattern=r"^/help$"))
async def cmd_help(event):
    uid = event.sender_id

    # ── OWNER HELP ──
    if is_super_admin(uid):
        msg = (
            "👑 **OWNER COMMANDS**\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "**👥 User Management:**\n"
            "/users — All users list\n"
            "/userinfo ID — User details\n"
            "/ban ID — Ban user\n"
            "/unban ID — Unban user\n"
            "/removeuser ID — Delete user\n"
            "/endtrial ID — Trial khatam karo\n\n"
            "**👑 Admin Management:**\n"
            "/addadmin ID — New admin add karo\n"
            "/removeadmin ID — Admin hatao\n"
            "/admins — Admin list\n"
            "/adminstats — Admin wise coupon stats\n\n"
            "**🔑 Coupon / Code:**\n"
            "/gencode DAYS — Direct code generate\n"
            "/extend ID DAYS — Access extend karo\n"
            "/revoke CODE — Code revoke karo\n"
            "/codes — All codes (by admin)\n"
            "/pending — Pending approval requests\n"
            "/logs — Activity logs\n\n"
            "**📊 Stats & Tasks:**\n"
            "/stats — Bot statistics\n"
            "/tasks — All tasks\n"
            "/adminstoptask ID — Task stop karo\n"
            "/adminstarttask ID — Task start karo\n"
            "/admindeltask ID — Task delete karo\n"
            "/usergroups ID — User ke groups dekho\n\n"
            "**📱 Numbers:**\n"
            "/numbers — All numbers\n"
            "/removenum +phone — Number remove karo\n\n"
            "**🔒 Protection:**\n"
            "/protect — Sab users protect/unprotect\n"
            "/pruser @username — Specific user protect\n"
            "/protectedlist — Protected users list\n\n"
            "**📢 Messaging:**\n"
            "/sendmsg ID text — User ko message bhejo\n"
            "/broadcast text — Sab ko broadcast karo\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "**🔧 General:**\n"
            "/admin — Admin panel\n"
            "/start /help /cancel /myid /status\n"
            "/buy — Admin list show karo"
        )
        await event.reply(msg, buttons=admin_kb(event.sender_id)); return

    # ── SUB ADMIN HELP ──
    if is_admin(uid):
        msg = (
            "🔰 **SUB ADMIN COMMANDS**\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "**🔑 Coupon:**\n"
            "/gencode DAYS — Code request karo (Owner approve karega)\n"
            "/approval — Apni requests aur status dekho\n"
            "/codes — Apne approved codes dekho\n\n"
            "**👥 User Management:**\n"
            "/users — Users list dekho\n"
            "/userinfo ID — User details\n"
            "/ban ID — Ban user\n"
            "/unban ID — Unban user\n"
            "/endtrial ID — Trial khatam karo\n"
            "/numbers — Phone numbers\n\n"
            "**📊 Tasks & Stats:**\n"
            "/stats — Bot stats\n"
            "/tasks — All tasks\n"
            "/adminstoptask ID — Task stop karo\n"
            "/adminstarttask ID — Task start karo\n"
            "/usergroups ID — User ke groups\n\n"
            "**📢 Messaging:**\n"
            "/sendmsg ID text — User ko message\n"
            "/broadcast text — Broadcast karo\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "**🔧 General:**\n"
            "/admin — Admin panel\n"
            "/start /help /cancel /myid /status\n"
            "/buy — Admin list show karo"
        )
        await event.reply(msg, buttons=admin_kb(event.sender_id)); return

    # ── NORMAL USER HELP ──
    msg = (
        "📖 **USER COMMANDS**\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "**🔐 Access:**\n"
        "/start — Bot start karo\n"
        "/status — Apna access status dekho\n"
        "/redeem CODE — Access code lagao\n"
        "/buy — Coupon/Code ke liye admin se contact karo\n\n"
        "**📱 Account:**\n"
        "/addaccount — Phone se account add karo\n"
        "/removeaccount +phone — Account hatao\n"
        "/mygroups — Apne groups dekho\n\n"
        "**📤 Messaging:**\n"
        "/sendnow message — Abhi sab groups mein bhejo\n"
        "/schedule — Auto schedule banao\n"
        "/myschedules — Apne schedules dekho\n\n"
        "**⏰ Tasks:**\n"
        "/starttask ID — Task start karo\n"
        "/stoptask ID — Task stop karo\n"
        "/deltask ID — Task delete karo\n"
        "/stopall — Sab tasks stop karo\n\n"
        "**⚙️ Other:**\n"
        "/settings — Settings dekho\n"
        "/protect — Apna account protect karo\n"
        "/myid — Apna Telegram ID dekho\n"
        "/cancel — Cancel karo\n"
        "/help — Yeh menu\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    await event.reply(msg, buttons=main_kb())

# ─────────────────────────── /cancel ─────────────────────────
@bot.on(events.NewMessage(pattern=r"^/cancel$"))
async def cmd_cancel(event):
    uid = event.sender_id
    if uid in pending:
        await drop_pending(uid)
        await event.reply("✅ Cancel ho gaya.", buttons=main_kb(), parse_mode='md')
    else:
        await event.reply("Kuch cancel nahi tha.", buttons=main_kb(), parse_mode='md')

# ─────────────────────────── /myid ───────────────────────────
@bot.on(events.NewMessage(pattern=r"^/myid$"))
async def cmd_myid(event):
    uid   = event.sender_id
    uname = getattr(event.sender, "username", None) or "none"
    await event.reply(f"🆔 **Tumhara Telegram ID:** `{uid}`\n👤 Username: @{uname}", parse_mode='md')

# ─────────────────────────── /status ─────────────────────────
@bot.on(events.NewMessage(pattern=r"^/status$"))
async def cmd_status(event):
    uid     = event.sender_id
    ok, tag = await check_access(uid)
    if tag == "ADMIN":  await event.reply("👑 **Status: Admin**", parse_mode='md'); return
    if tag == "BANNED": await event.reply("🚫 **Status: Banned**", parse_mode='md'); return
    if ok and tag == "TRIAL":
        row = c.execute("SELECT trial_expires FROM users WHERE user_id=?", (uid,)).fetchone()
        await event.reply(f"🎁 **Status: Trial**\nExpiry: {(row[0] or '?').split('T')[0]}")
    elif ok:
        row = c.execute("SELECT expires_at FROM access_codes WHERE code=?", (tag,)).fetchone()
        await event.reply(f"✅ **Status: Active**\nCode: `{tag}`\nExpiry: {(row[0] or '?').split('T')[0]}")
    else:
        await event.reply("❌ **Status: No Access**\n/redeem CODE karo.", parse_mode='md')

# ─────────────────────────── /redeem ─────────────────────────
@bot.on(events.NewMessage(pattern=r"^/redeem\s+(\S+)$"))
async def cmd_redeem(event):
    await _do_redeem(event, event.sender_id, event.pattern_match.group(1).strip().upper())

# ─────────────────────────── /addaccount ─────────────────────
@bot.on(events.NewMessage(pattern=r"^/addaccount$"))
async def cmd_addaccount(event):
    uid     = event.sender_id
    ok, tag = await check_access(uid)
    if not ok:
        await event.reply("❌ Access nahi hai. /redeem CODE karo."); return
    await drop_pending(uid)        # purana login client band
    cnt = c.execute("SELECT COUNT(*) FROM user_accounts WHERE user_id=?", (uid,)).fetchone()[0]
    pending[uid] = {"action": "add_phone", "ts": time.time()}
    await event.reply(
        f"📱 Phone bhejo country code ke saath (e.g. `+919876543210`)\n({cnt}/{MAX_ACCOUNTS})\n\n"
        "/cancel se wapas."
    )

# ─────────────────────────── /removeaccount ──────────────────
@bot.on(events.NewMessage(pattern=r"^/removeaccount\s+(\+[\d\s\-()]+)$"))
async def cmd_removeaccount(event):
    uid   = event.sender_id
    phone = norm_phone(event.pattern_match.group(1))
    row   = phone and c.execute("SELECT user_id FROM user_accounts WHERE phone=?", (phone,)).fetchone()
    if not row:
        await event.reply("❌ Yeh phone linked nahi."); return
    if row[0] != uid and not is_admin(uid):
        await event.reply("❌ Yeh account tumhara nahi."); return
    await remove_account(phone)
    await event.reply(f"🗑 `{phone}` removed.\nTasks band ho gaye aur Telegram session logout ho gaya.", buttons=main_kb())

# ─────────────────────────── /mygroups ───────────────────────
@bot.on(events.NewMessage(pattern=r"^/mygroups$"))
async def cmd_mygroups(event):
    uid     = event.sender_id
    ok, _   = await check_access(uid)
    if not ok: await event.reply("❌ Access nahi hai."); return
    accounts = c.execute("SELECT phone,session_str FROM user_accounts WHERE user_id=?", (uid,)).fetchall()
    if not accounts: await event.reply("Koi account nahi. /addaccount karo."); return
    msg = await event.reply("🔍 Fetching groups...")
    lines = ["📊 **Tumhare Groups:**\n"]
    for phone, sess in accounts:
        cl = await open_client(phone, sess)
        if not cl: lines.append(f"\n📵 `{phone}`: connect fail"); continue
        try:
            dlgs   = await cl.get_dialogs(limit=None)
            groups = [d for d in dlgs if d.is_group or d.is_channel]
            lines.append(f"\n📱 `{phone}` — **{len(groups)} groups:**")
            for g in groups:
                icon  = "📣" if g.is_channel else "👥"
                uname = f"@{g.entity.username}" if getattr(g.entity, 'username', None) else "🔒 private"
                lines.append(f"  {icon} {g.name}  |  {uname}")
        except Exception as e: lines.append(f"\n⚠️ `{phone}`: {e}")
        finally: await close(cl)
    full = "\n".join(lines)
    await msg.edit(full[:4000])
    if len(full) > 4000: await event.reply(full[4000:8000])

# ─────────────────────────── /sendnow ────────────────────────
@bot.on(events.NewMessage(pattern=r"^/sendnow\s+(.+)$"))
async def cmd_sendnow(event):
    uid     = event.sender_id
    ok, _   = await check_access(uid)
    if not ok: await event.reply("❌ Access nahi."); return
    text     = event.pattern_match.group(1).strip()
    # FIX: keep entities (offsets shifted by the exact "/sendnow " prefix length)
    text, ents_js = capture_text_and_entities(event.message, text,
                        event.pattern_match.start(1), event.pattern_match.end(1))
    accounts = c.execute("SELECT phone,session_str FROM user_accounts WHERE user_id=?", (uid,)).fetchall()
    if not accounts: await event.reply("❌ Koi account nahi."); return
    msg = await event.reply("📤 Sending...")
    await _send_now_core(msg, uid, text, accounts, ents_js)

# ─────────────────────────── /schedule ───────────────────────
@bot.on(events.NewMessage(pattern=r"^/schedule$"))
async def cmd_schedule(event):
    uid     = event.sender_id
    ok, _   = await check_access(uid)
    if not ok: await event.reply("❌ Access nahi."); return
    has_acct = c.execute("SELECT COUNT(*) FROM user_accounts WHERE user_id=?", (uid,)).fetchone()[0]
    if not has_acct and not is_admin(uid):
        await event.reply("❌ Koi account nahi. /addaccount karo."); return
    if not has_acct and is_admin(uid):
        has_acct = c.execute("SELECT COUNT(*) FROM user_accounts").fetchone()[0]
        if not has_acct:
            await event.reply("❌ Koi account nahi."); return
    pending[uid] = {"action": "await_msg", "mode": "schedule", "messages": []}
    await event.reply("📝 **Message #1 type karo** (ya forward karo):\n\nMultiple messages add kar sakte ho.\n/cancel se wapas.", parse_mode='md')

# ─────────────────────────── /myschedules ────────────────────
@bot.on(events.NewMessage(pattern=r"^/myschedules$"))
async def cmd_myschedules(event):
    await _show_schedules(event, event.sender_id, edit=False)

# ─────────────────────────── /stoptask ───────────────────────
@bot.on(events.NewMessage(pattern=r"^/starttask\s+(\d+)$"))
async def cmd_starttask(event):
    uid = event.sender_id
    tid = int(event.pattern_match.group(1))
    row = c.execute("SELECT user_id,phone,interval_seconds,is_active FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
    if not row: await event.reply("❌ Task nahi mila."); return
    if row[0] != uid and not is_admin(uid): await event.reply("❌ Tumhara nahi."); return
    if row[3]: await event.reply("⚠️ Task already chal raha hai."); return
    phone, iv = row[1], row[2]
    sess_row  = c.execute("SELECT session_str FROM user_accounts WHERE user_id=? AND phone=?", (uid, phone)).fetchone()
    if not sess_row:
        sess_row = c.execute("SELECT session_str FROM user_accounts WHERE phone=?", (phone,)).fetchone()
    if not sess_row: await event.reply("❌ Account nahi mila. /addaccount karo."); return
    await db_write("UPDATE scheduled_tasks SET is_active=1,fail_count=0 WHERE id=?", (tid,))
    if tid not in scheduler_tasks:
        start_task(tid, uid, phone, sess_row[0], iv)
    await event.reply("▶️ **Task #" + str(tid) + " Start Ho Gaya!**", buttons=main_kb(), parse_mode='md')

@bot.on(events.NewMessage(pattern=r"^/stoptask\s+(\d+)$"))
async def cmd_stoptask(event):
    uid = event.sender_id
    tid = int(event.pattern_match.group(1))
    row = c.execute("SELECT user_id FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
    if not row: await event.reply(f"❌ Task #{tid} nahi mila."); return
    if row[0] != uid and not is_admin(uid): await event.reply("❌ Tumhara nahi."); return
    await db_write("UPDATE scheduled_tasks SET is_active=0 WHERE id=?", (tid,))
    if tid in scheduler_tasks: scheduler_tasks[tid].cancel(); del scheduler_tasks[tid]
    await event.reply(f"⏹ Task #{tid} stop ho gaya.")

# ─────────────────────────── /deltask ────────────────────────
@bot.on(events.NewMessage(pattern=r"^/deltask\s+(\d+)$"))
async def cmd_deltask(event):
    uid = event.sender_id
    tid = int(event.pattern_match.group(1))
    row = c.execute("SELECT user_id FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
    if not row: await event.reply(f"❌ Task #{tid} nahi mila."); return
    if row[0] != uid and not is_admin(uid): await event.reply("❌ Tumhara nahi."); return
    await db_write("UPDATE scheduled_tasks SET is_active=0 WHERE id=?", (tid,))
    if tid in scheduler_tasks: scheduler_tasks[tid].cancel(); del scheduler_tasks[tid]
    await db_write("DELETE FROM scheduled_tasks WHERE id=?", (tid,))
    await event.reply(f"🗑 Task #{tid} deleted.")

# ─────────────────────────── /stopall ────────────────────────
@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() in ["/stopall", "🛑 Stop All"]))
async def cmd_stopall(event):
    uid = event.sender_id
    await db_write("UPDATE scheduled_tasks SET is_active=0 WHERE user_id=?", (uid,))
    stopped = 0
    for tid in list(scheduler_tasks):
        row = c.execute("SELECT user_id FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
        if row and row[0] == uid:
            scheduler_tasks[tid].cancel(); del scheduler_tasks[tid]; stopped += 1
    await event.reply(f"🛑 {stopped} task(s) stop.", buttons=main_kb())

# ─────────────────────────── /settings ───────────────────────
@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() in ["/settings", "⚙️ Settings"]))
async def cmd_settings(event):
    uid      = event.sender_id
    ok, tag  = await check_access(uid)
    await verify_accounts(uid)          # terminated sessions auto-remove
    accounts = c.execute("SELECT phone,added_at FROM user_accounts WHERE user_id=?", (uid,)).fetchall()
    tasks    = c.execute("SELECT id,interval_seconds,is_active,messages_json FROM scheduled_tasks WHERE user_id=?", (uid,)).fetchall()
    lines = [
        "⚙️ **Settings**\n",
        f"🔐 Access: {'✅ ' + str(tag) if ok else '❌ No access'}",
        f"\n📱 Accounts ({len(accounts)}/{MAX_ACCOUNTS}):",
    ]
    for ph, added in accounts:
        lines.append(f"  • `{ph}` — {(added or '').split('T')[0]}\n    /removeaccount {ph}")
    if not accounts: lines.append("  Koi nahi — /addaccount")
    lines.append(f"\n⏰ Tasks ({len(tasks)}):")
    for tid, iv, act2, mj in tasks:
        nm = len(msgs_list(mj))
        lines.append(
            f"  {'▶️' if act2 else '⏹'} #{tid} | {fmt_mins(iv)} | {nm} msg(s)\n"
            f"    /stoptask {tid}  /deltask {tid}"
        )
    if not tasks: lines.append("  Koi nahi — /schedule")
    await event.reply("\n".join(lines), buttons=main_kb(), parse_mode='md')

# ─────────────────────────── ADMIN COMMANDS ──────────────────
@bot.on(events.NewMessage(pattern=r"^/addadmin\s+(\d+)$"))
async def cmd_addadmin(event):
    if not is_super_admin(event.sender_id):
        await event.reply("❌ Sirf Super Admin yeh kar sakta hai."); return
    uid = int(event.pattern_match.group(1))
    if uid == ADMIN_ID:
        await event.reply("⚠️ Yeh already Super Admin hai."); return
    # Try to get username from DB first, then from Telegram
    urow = c.execute("SELECT username FROM users WHERE user_id=?", (uid,)).fetchone()
    uname = urow[0] if urow and urow[0] else ""
    if not uname:
        try:
            tg_user = await bot.get_entity(uid)
            uname = tg_user.username or ""
        except Exception: pass
    # Save/update user in users table too
    if not c.execute("SELECT user_id FROM users WHERE user_id=?", (uid,)).fetchone():
        c.execute("INSERT OR IGNORE INTO users(user_id,username) VALUES(?,?)", (uid, uname))
        conn.commit()
    else:
        if uname:
            c.execute("UPDATE users SET username=? WHERE user_id=?", (uname, uid))
            conn.commit()
    await db_write("INSERT OR REPLACE INTO admins(user_id,username,added_by,added_at) VALUES(?,?,?,?)",
        (uid, uname, event.sender_id, now_iso()))
    name = "@" + uname if uname else "`" + str(uid) + "`"
    msg = (
        "✅ **" + name + " Admin ban gaya!**\n\n"
        "🆔 ID: `" + str(uid) + "`\n"
        "👤 Username: " + ("@" + uname if uname else "—") + "\n"
        "⚙️ Permissions:\n"
        "  ✅ Admin panel use kar sakta hai\n"
        "  ✅ Coupon request kar sakta hai (Owner approve karega)\n"
        "  ✅ Apne codes dekh sakta hai\n"
        "  ❌ Naye admin nahi bana sakta\n\n"
        "/removeadmin " + str(uid) + " — hatane ke liye"
    )
    await event.reply(msg, buttons=admin_kb(event.sender_id))
    # Notify the new admin
    try:
        await bot.send_message(uid,
            "🎉 **Tumhe Admin Banaya Gaya!**\n\n"
            "👑 Bot: @V4_XTRD_bot\n"
            "🔰 Role: Sub Admin\n\n"
            "Ab tum /admin se admin panel access kar sakte ho.\n"
            "/help se saari commands dekho."
        )
    except Exception: pass

@bot.on(events.NewMessage(pattern=r"^/buy$"))
async def cmd_buy(event):
    # Build dynamic admin list from DB
    rows = c.execute("SELECT user_id, username FROM admins").fetchall()
    # Add owner info
    owner_row = c.execute("SELECT username FROM users WHERE user_id=?", (ADMIN_ID,)).fetchone()
    owner_name = owner_row[0] if owner_row and owner_row[0] else None

    lines = [
        "💬 **DM Any Admin For Coupon / Best Price**\n",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "👑 **Owner:**",
        "  • " + ("@" + owner_name if owner_name else "`" + str(ADMIN_ID) + "`"),
    ]
    if rows:
        lines.append("\n🔰 **Admins:**")
        for uid2, uname in rows:
            if uname:
                lines.append("  • @" + uname)
            else:
                lines.append("  • `" + str(uid2) + "`")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("💡 _Admin ko DM karo aur best deal pao!_")
    await event.reply("\n".join(lines), parse_mode='md')

@bot.on(events.NewMessage(pattern=r"^/removeadmin\s+(\d+)$"))
async def cmd_removeadmin(event):
    if not is_super_admin(event.sender_id):
        await event.reply("❌ Sirf Super Admin yeh kar sakta hai."); return
    uid = int(event.pattern_match.group(1))
    row = c.execute("SELECT user_id FROM admins WHERE user_id=?", (uid,)).fetchone()
    if not row:
        await event.reply(f"❌ `{uid}` admin nahi hai."); return
    await db_write("DELETE FROM admins WHERE user_id=?", (uid,))
    await event.reply(f"🗑 `{uid}` admin se remove ho gaya.", buttons=admin_kb(event.sender_id))

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() in ["/admins", "👑 Admins"]))
async def cmd_admins_list(event):
    if not is_admin(event.sender_id): return
    rows = c.execute("SELECT user_id,username,added_at FROM admins").fetchall()
    out = "👑 **Super Admin (Sirf Tum):**\n"
    out += "  • `" + str(ADMIN_ID) + "` — Full Powers\n\n"
    out += "🔰 **Sub Admins** (" + str(len(rows)) + "):\n"
    if rows:
        for uid2, uname, added in rows:
            name = "@" + uname if uname else "`" + str(uid2) + "`"
            out += "  • " + name + " | `" + str(uid2) + "`\n"
            if is_super_admin(event.sender_id):
                out += "    /removeadmin " + str(uid2) + "\n"
    else:
        out += "  Koi sub admin nahi\n"
    if is_super_admin(event.sender_id):
        out += "\n➕ Add karo: /addadmin USER_ID"
    await event.reply(out, buttons=admin_kb(event.sender_id))




@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() in ["/admin", "🔧 Admin Panel"]))
async def cmd_admin(event):
    if not is_admin(event.sender_id): return
    await event.reply("👑 **Admin Panel**", buttons=admin_kb(event.sender_id), parse_mode='md')

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() in ["👤 User Menu", "🔙 User Menu"]))
async def cmd_usermenu(event):
    await event.reply("👤 **User Menu**", buttons=main_kb(), parse_mode='md')

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() in ["/stats", "📊 Stats"]))
async def cmd_stats(event):
    if not is_admin(event.sender_id): return
    total  = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    banned = c.execute("SELECT COUNT(*) FROM users WHERE is_banned=1").fetchone()[0]
    trials = c.execute("SELECT COUNT(*) FROM users WHERE trial_granted=1 AND trial_expires>?", (now_iso(),)).fetchone()[0]
    phones = c.execute("SELECT COUNT(*) FROM user_accounts").fetchone()[0]
    codes  = c.execute("SELECT COUNT(*) FROM access_codes").fetchone()[0]
    actc   = c.execute("SELECT COUNT(*) FROM access_codes WHERE is_active=1 AND expires_at>?", (now_iso(),)).fetchone()[0]
    claimed= c.execute("SELECT COUNT(*) FROM access_codes WHERE claimed_by IS NOT NULL").fetchone()[0]
    tasks  = c.execute("SELECT COUNT(*) FROM scheduled_tasks WHERE is_active=1").fetchone()[0]
    pending_req = c.execute("SELECT COUNT(*) FROM code_requests WHERE status='pending'").fetchone()[0]
    admins_cnt  = c.execute("SELECT COUNT(*) FROM admins").fetchone()[0]

    out = (
        "📊 **Bot Statistics**\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "👥 Total Users: `" + str(total) + "` | 🚫 Banned: `" + str(banned) + "`\n"
        "🎁 Active Trials: `" + str(trials) + "`\n"
        "📱 Numbers: `" + str(phones) + "`\n"
        "⏰ Running Tasks: `" + str(tasks) + "`\n"
        "👑 Sub Admins: `" + str(admins_cnt) + "`\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔑 **Coupon Stats:**\n"
        "  Total: `" + str(codes) + "` | Active: `" + str(actc) + "`\n"
        "  ✅ Claimed: `" + str(claimed) + "` | 🟢 Unclaimed: `" + str(actc - claimed) + "`\n"
        "  ⏳ Pending Approval: `" + str(pending_req) + "`\n"
    )

    # Admin coupon breakdown (owner only)
    if is_super_admin(event.sender_id):
        out += "━━━━━━━━━━━━━━━━━━━━━━\n"
        out += "📋 **Coupons By Admin:**\n"
        # Owner codes
        owner_cnt = c.execute("SELECT COUNT(*) FROM access_codes WHERE created_by=?", (ADMIN_ID,)).fetchone()[0]
        owner_row = c.execute("SELECT username FROM users WHERE user_id=?", (ADMIN_ID,)).fetchone()
        owner_uname = owner_row[0] if owner_row and owner_row[0] else ""
        out += "  👑 " + ("@" + owner_uname if owner_uname else "Owner") + " | ID: `" + str(ADMIN_ID) + "`\n"
        out += "     Total Coupons Created: **" + str(owner_cnt) + "**\n\n"
        # Sub admin codes
        admin_rows = c.execute("SELECT user_id,username FROM admins").fetchall()
        for aid, auname in admin_rows:
            cnt  = c.execute("SELECT COUNT(*) FROM access_codes WHERE created_by=?", (aid,)).fetchone()[0]
            clm  = c.execute("SELECT COUNT(*) FROM access_codes WHERE created_by=? AND claimed_by IS NOT NULL", (aid,)).fetchone()[0]
            pend = c.execute("SELECT COUNT(*) FROM code_requests WHERE requested_by=? AND status='pending'", (aid,)).fetchone()[0]
            name = "@" + auname if auname else "ID:" + str(aid)
            out += "  🔰 " + name + " | ID: `" + str(aid) + "`\n"
            out += "     Total Created: **" + str(cnt) + "** | Claimed: **" + str(clm) + "** | Pending: **" + str(pend) + "**\n\n"

    await event.reply(out[:4000], buttons=admin_kb(event.sender_id))

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() in ["/logs", "📜 Logs"]))
async def cmd_logs(event):
    if not is_super_admin(event.sender_id): return
    rows = c.execute(
        "SELECT event_type,admin_name,admin_id,code,details,created_at FROM logs ORDER BY id DESC LIMIT 30"
    ).fetchall()
    if not rows:
        await event.reply("📜 **Logs**\n\nKoi log nahi abhi tak.", buttons=admin_kb(event.sender_id)); return
    icons = {"code_created": "🆕", "code_approved": "✅", "code_claimed": "🔑", "code_rejected": "❌"}
    lines = ["📜 **Recent Logs** (last 30)\n"]
    for etype, aname, aid, code, details, created in rows:
        icon = icons.get(etype, "📌")
        date = (created or "").replace("T", " ").split(".")[0]
        lines.append(
            icon + " **" + etype.replace("_", " ").title() + "**\n"
            "   👤 " + (aname or "—") + " | `" + str(aid) + "`\n"
            "   🔑 `" + (code or "—") + "`\n"
            "   📝 " + (details or "—") + "\n"
            "   🕐 " + date
        )
    await event.reply("\n\n".join(lines)[:4000], buttons=admin_kb(event.sender_id), parse_mode='md')

@bot.on(events.NewMessage(pattern=r"^/adminstats$"))
async def cmd_adminstats(event):
    if not is_super_admin(event.sender_id): return
    out = "📋 **Admin Coupon Statistics**\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    # Owner stats
    o_total   = c.execute("SELECT COUNT(*) FROM access_codes WHERE created_by=?", (ADMIN_ID,)).fetchone()[0]
    o_claimed = c.execute("SELECT COUNT(*) FROM access_codes WHERE created_by=? AND claimed_by IS NOT NULL", (ADMIN_ID,)).fetchone()[0]
    o_active  = c.execute("SELECT COUNT(*) FROM access_codes WHERE created_by=? AND is_active=1 AND expires_at>?", (ADMIN_ID, now_iso())).fetchone()[0]
    orow = c.execute("SELECT username FROM users WHERE user_id=?", (ADMIN_ID,)).fetchone()
    oname = "@" + orow[0] if orow and orow[0] else "Owner"
    out += (
        "👑 **Admin:** " + oname + "\n"
        "   🆔 Admin ID: `" + str(ADMIN_ID) + "`\n"
        "   🔑 Total Coupons Created: **" + str(o_total) + "**\n"
        "   ✅ Claimed: **" + str(o_claimed) + "** | 🟢 Unclaimed: **" + str(o_active - o_claimed) + "**\n\n"
    )
    # Sub admin stats
    admin_rows = c.execute("SELECT user_id,username FROM admins").fetchall()
    if admin_rows:
        for aid, auname in admin_rows:
            total_req  = c.execute("SELECT COUNT(*) FROM code_requests WHERE requested_by=?", (aid,)).fetchone()[0]
            approved   = c.execute("SELECT COUNT(*) FROM code_requests WHERE requested_by=? AND status='approved'", (aid,)).fetchone()[0]
            pending    = c.execute("SELECT COUNT(*) FROM code_requests WHERE requested_by=? AND status='pending'", (aid,)).fetchone()[0]
            rejected   = c.execute("SELECT COUNT(*) FROM code_requests WHERE requested_by=? AND status='rejected'", (aid,)).fetchone()[0]
            ac_claimed = c.execute("SELECT COUNT(*) FROM access_codes WHERE created_by=? AND claimed_by IS NOT NULL", (aid,)).fetchone()[0]
            ac_active  = c.execute("SELECT COUNT(*) FROM access_codes WHERE created_by=? AND is_active=1 AND expires_at>?", (aid, now_iso())).fetchone()[0]
            name = "@" + auname if auname else "ID:" + str(aid)
            out += (
                "🔰 **Admin:** " + name + "\n"
                "   🆔 Admin ID: `" + str(aid) + "`\n"
                "   📨 Total Requests: **" + str(total_req) + "**\n"
                "   ✅ Approved: **" + str(approved) + "** | ❌ Rejected: **" + str(rejected) + "** | ⏳ Pending: **" + str(pending) + "**\n"
                "   🔑 Active Codes: **" + str(ac_active) + "** | Claimed: **" + str(ac_claimed) + "**\n\n"
            )
    else:
        out += "🔰 Koi sub admin nahi.\n"
    out += "━━━━━━━━━━━━━━━━━━━━━━"
    await event.reply(out[:4000], buttons=admin_kb(event.sender_id))

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() in ["/users", "👥 Users"]))
async def cmd_users(event):
    if not is_admin(event.sender_id): return
    rows = c.execute(
        "SELECT user_id,username,trial_granted,trial_expires,is_banned FROM users ORDER BY rowid DESC LIMIT 20"
    ).fetchall()
    if not rows: await event.reply("Koi user nahi.", buttons=admin_kb(event.sender_id)); return
    lines = ["👥 **Users** (last 20)\n"]
    buttons = []
    for uid2, uname, trial, texp, banned in rows:
        prot = c.execute("SELECT is_protected FROM users WHERE user_id=?", (uid2,)).fetchone()
        is_prot = prot[0] if prot else 0
        if is_prot and not is_super_admin(event.sender_id):
            lines.append("🔒 Protected User | /userinfo " + str(uid2))
            buttons.append([Button.inline("🔒 Protected", b"noop")])
            continue
        ph  = c.execute("SELECT COUNT(*) FROM user_accounts WHERE user_id=?", (uid2,)).fetchone()[0]
        cod = c.execute("SELECT code FROM access_codes WHERE claimed_by=? AND is_active=1", (uid2,)).fetchone()
        st  = "🚫" if banned else ("✅" if cod else "🎁" if (trial and texp and now_utc() <= parse_iso(texp)) else "❌")
        name = f"@{uname}" if uname else f"ID:{uid2}"
        lines.append(f"• {name} `{uid2}` 📱{ph} {st}\n  /userinfo {uid2}")
        buttons.append([
            Button.inline(f"ℹ️ {name[:12]}", f"uinfo_{uid2}".encode()),
            Button.inline("🚫 Ban",           f"uban_{uid2}".encode()),
            Button.inline("🗑 Del",           f"udelc_{uid2}".encode()),
        ])
    await event.reply("\n".join(lines), buttons=buttons, parse_mode='md')

@bot.on(events.NewMessage(pattern=r"^/userinfo\s+(\d+)$"))
async def cmd_userinfo(event):
    if not is_admin(event.sender_id): return
    await _show_userinfo(event, int(event.pattern_match.group(1)), event.sender_id)

async def _show_userinfo(ctx, uid, requester_id=None):
    row = c.execute("SELECT user_id,username,trial_granted,trial_expires,is_banned,joined_at,is_protected FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row: await ctx.reply("❌ User `" + str(uid) + "` nahi mila."); return
    # Protection check — sub admin cannot see protected user
    is_prot = row[6] if len(row) > 6 else 0
    if is_prot and requester_id and not is_super_admin(requester_id):
        await ctx.reply(
            "🔒 **Protected User**\n\n"
            "Is user ne apna data protect kiya hua hai.\n"
            "Sirf Owner details dekh sakta hai."
        ); return
    _, uname, trial, texp, banned, joined, is_prot2 = row
    phones = c.execute("SELECT phone,added_at FROM user_accounts WHERE user_id=?", (uid,)).fetchall()
    code   = c.execute("SELECT code,expires_at FROM access_codes WHERE claimed_by=? AND is_active=1", (uid,)).fetchone()
    tasks  = c.execute("SELECT id,interval_seconds,is_active FROM scheduled_tasks WHERE user_id=?", (uid,)).fetchall()
    name      = f"@{uname}" if uname else f"ID:{uid}"
    joined_dt = (joined or "").split("T")[0] or "?"
    prot_icon = "🔒" if is_prot2 else "🔓"

    if banned:
        status = "🚫 BANNED"
    elif code:
        status = "✅ Active | " + code[0] + " | exp " + code[1].split("T")[0]
    elif trial and texp and now_utc() <= parse_iso(texp):
        status = "🎁 Trial | exp " + texp.split("T")[0]
    else:
        status = "❌ No Access"

    out = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "👤 **" + name + "** | `" + str(uid) + "`\n"
        "📅 Joined: " + joined_dt + "\n"
        "🔐 " + status + "\n"
        "" + prot_icon + " Protection: " + ("ON" if is_prot2 else "OFF") + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📱 Accounts (" + str(len(phones)) + "):\n"
    )
    for ph, ad in phones:
        out += "  • `" + ph + "`\n"
    if not phones:
        out += "  Koi nahi\n"

    out += "\n⏰ Tasks (" + str(len(tasks)) + "):\n"
    for tid, iv, act2 in tasks:
        icon = "▶️" if act2 else "⏹"
        out += "  " + icon + " #" + str(tid) + " " + fmt_mins(iv) + "  /adminstoptask " + str(tid) + "\n"
    if not tasks:
        out += "  Koi task nahi\n"

    out += "━━━━━━━━━━━━━━━━━━━━━━\n"
    out += "/ban " + str(uid) + "  /unban " + str(uid) + "  /removeuser " + str(uid)

    btns = [
        [Button.inline("📊 Groups",  ("ugrp_" + str(uid)).encode()),
         Button.inline("➕ Extend",  ("uext_" + str(uid)).encode())],
        [Button.inline("🚫 Ban",     ("uban_" + str(uid)).encode()),
         Button.inline("✅ Unban",   ("uunb_" + str(uid)).encode())],
        [Button.inline("🔒 Protect" if not is_prot2 else "🔓 Unprotect",
                       ("upr_" + str(uid)).encode()),
         Button.inline("⏹ End Trial", ("uet_" + str(uid)).encode())],
        [Button.inline("🗑 Delete",  ("udelc_" + str(uid)).encode())],
    ]
    try:
        await ctx.edit(out, buttons=btns, parse_mode='md')
    except Exception:
        try:
            await ctx.respond(out, buttons=btns, parse_mode='md')
        except Exception:
            try:
                await bot.send_message(
                    ctx.sender_id if hasattr(ctx, 'sender_id') else ctx.chat_id,
                    out, buttons=btns, parse_mode='md'
                )
            except Exception as e:
                print(f"userinfo error: {e}")

@bot.on(events.NewMessage(pattern=r"^/ban\s+(\d+)$"))
async def cmd_ban(event):
    if not is_admin(event.sender_id): return
    uid = int(event.pattern_match.group(1))
    await db_write("UPDATE users SET is_banned=1 WHERE user_id=?", (uid,))
    await event.reply(f"🚫 `{uid}` banned.", buttons=admin_kb(event.sender_id))

@bot.on(events.NewMessage(pattern=r"^/unban\s+(\d+)$"))
async def cmd_unban(event):
    if not is_admin(event.sender_id): return
    uid = int(event.pattern_match.group(1))
    await db_write("UPDATE users SET is_banned=0 WHERE user_id=?", (uid,))
    await event.reply(f"✅ `{uid}` unbanned.", buttons=admin_kb(event.sender_id))

@bot.on(events.NewMessage(pattern=r"^/removeuser\s+(\d+)$"))
async def cmd_removeuser(event):
    if not is_admin(event.sender_id): return
    uid = int(event.pattern_match.group(1))
    await _del_user(uid)
    await event.reply(f"🗑 User `{uid}` deleted.", buttons=admin_kb(event.sender_id))

async def _del_user(uid):
    for (ph,) in c.execute("SELECT phone FROM user_accounts WHERE user_id=?", (uid,)).fetchall():
        await remove_account(ph)                       # tasks stop + Telegram logout + delete
    for tid, in c.execute("SELECT id FROM scheduled_tasks WHERE user_id=?", (uid,)).fetchall():
        t = scheduler_tasks.pop(tid, None)
        if t: t.cancel()
    c.execute("DELETE FROM scheduled_tasks WHERE user_id=?", (uid,))
    c.execute("UPDATE access_codes SET claimed_by=NULL,claimed_at=NULL WHERE claimed_by=?", (uid,))
    c.execute("DELETE FROM users WHERE user_id=?", (uid,))
    conn.commit()

@bot.on(events.NewMessage(func=lambda e: e.text and (e.text.strip() == "➕ Gen Code" or e.text.strip().startswith("/gencode"))))
async def cmd_gencode(event):
    if not is_admin(event.sender_id): return
    import re
    text = event.text.strip()
    if text == "➕ Gen Code":
        pending[event.sender_id] = {"action": "admin_gencode"}
        await event.reply("🔑 Kitne din ka code? (e.g. `30`)", buttons=[[Button.text("❌ Cancel")]]); return
    m = re.match(r"^/gencode\s+(\d+)$", text)
    if not m: await event.reply("Usage: /gencode 30"); return
    await _do_gencode(event, int(m.group(1)), event.sender_id)

async def _do_gencode(ctx, days, requester_id=None):
    # requester_id se uid decide karo
    uid = requester_id or ADMIN_ID
    # Super admin — direct generate
    if is_super_admin(uid):
        code    = gen_code()
        expires = (now_utc() + timedelta(days=days)).isoformat()
        await db_write("INSERT INTO access_codes(code,days_valid,created_at,expires_at,created_by) VALUES(?,?,?,?,?)",
            (code, days, now_iso(), expires, uid))
        _urow  = c.execute("SELECT username FROM users WHERE user_id=?", (uid,)).fetchone()
        _uname = _urow[0] if _urow and _urow[0] else ""
        await log_event("code_created", uid, "@" + _uname if _uname else str(uid), code, str(days) + " days")
        await ctx.reply(
            "✅ **Code Generate Hua!**\n\n🔑 `" + code + "`\n📅 " + str(days) + " din\n⏳ " + expires.split("T")[0] + "\n\n/redeem " + code,
            buttons=admin_kb(uid)
        )
    else:
        # Sub admin — send approval request to owner
        req_id = await db_write(
            "INSERT INTO code_requests(requested_by,days) VALUES(?,?)",
            (uid, days)
        )
        urow  = c.execute("SELECT username FROM users WHERE user_id=?", (uid,)).fetchone()
        uname = urow[0] if urow and urow[0] else ""
        name  = "@" + uname if uname else "`" + str(uid) + "`"
        await log_event("code_created", uid, name, "", str(days) + " days (pending approval)")
        await ctx.reply(
            "⏳ **Request bheji gayi!**\n\nOwner verify karega tab code milega.",
            buttons=admin_kb(uid)
        )
        # Notify super admin
        await bot.send_message(
            ADMIN_ID,
            "🔔 **Code Request Aayi!**\n\n"
            "👤 Admin: " + name + "\n"
            "📅 Days: **" + str(days) + "**\n\n"
            "Approve karo toh code generate hoga.",
            buttons=[
                [Button.inline("✅ Approve", ("creq_ok_" + str(req_id)).encode())],
                [Button.inline("❌ Reject",  ("creq_no_" + str(req_id)).encode())],
            ]
        )

# ── /pending — show all pending requests ──────────────────────
@bot.on(events.NewMessage(func=lambda e: e.text and (
    e.text.strip().startswith("📋 Pending") or e.text.strip() == "/pending"
)))
async def cmd_pending(event):
    if not is_super_admin(event.sender_id): return
    rows = c.execute(
        "SELECT cr.id, cr.requested_by, u.username, cr.days, cr.requested_at "
        "FROM code_requests cr LEFT JOIN users u ON cr.requested_by=u.user_id "
        "WHERE cr.status='pending' ORDER BY cr.id ASC"
    ).fetchall()
    if not rows:
        await event.reply("📋 **Pending Requests**\n\nKoi pending request nahi hai! ✅", buttons=admin_kb(event.sender_id)); return
    lines2  = ["📋 **Pending Code Requests** (" + str(len(rows)) + ")\n"]
    buttons = []
    for req_id, req_by, uname, days, req_at in rows:
        name = "@" + uname if uname else "ID:" + str(req_by)
        date = (req_at or "").split("T")[0]
        lines2.append(
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔔 **Request #" + str(req_id) + "**\n"
            "👤 Admin: " + name + " | `" + str(req_by) + "`\n"
            "📅 Days: **" + str(days) + "**\n"
            "🕐 Date: " + date
        )
        buttons.append([
            Button.inline("✅ Approve #" + str(req_id), ("creq_ok_" + str(req_id)).encode()),
            Button.inline("❌ Reject #" + str(req_id),  ("creq_no_" + str(req_id)).encode()),
        ])
    buttons.append([
        Button.inline("✅ Approve ALL", b"creq_all_ok"),
        Button.inline("❌ Reject ALL",  b"creq_all_no"),
    ])
    await event.reply("\n\n".join(lines2), buttons=buttons, parse_mode='md')

# Approve ALL
@bot.on(events.CallbackQuery(data=b"creq_all_ok"))
async def cb_creq_all_ok(event):
    if not is_super_admin(event.sender_id): return
    rows = c.execute("SELECT id,requested_by,days FROM code_requests WHERE status='pending'").fetchall()
    if not rows: await event.answer("Koi pending nahi.", alert=True); return
    done = 0
    for req_id, requester, days in rows:
        code    = gen_code()
        expires = (now_utc() + timedelta(days=days)).isoformat()
        await db_write("INSERT INTO access_codes(code,days_valid,created_at,expires_at,created_by) VALUES(?,?,?,?,?)",
            (code, days, now_iso(), expires, requester))
        await db_write("UPDATE code_requests SET status=?,code=? WHERE id=?", ("approved", code, req_id))
        try:
            await bot.send_message(requester,
                "✅ **Code Approved By Owner!**\n\n"
                "🔑 `" + code + "`\n"
                "📅 " + str(days) + " din\n"
                "⏳ " + expires.split("T")[0] + "\n\n"
                "/redeem " + code
            )
        except Exception: pass
        done += 1
    await event.edit("✅ **" + str(done) + " requests approve ho gayi!**", buttons=admin_kb(event.sender_id), parse_mode='md')

# Reject ALL
@bot.on(events.CallbackQuery(data=b"creq_all_no"))
async def cb_creq_all_no(event):
    if not is_super_admin(event.sender_id): return
    rows = c.execute("SELECT id,requested_by FROM code_requests WHERE status='pending'").fetchall()
    if not rows: await event.answer("Koi pending nahi.", alert=True); return
    for req_id, requester in rows:
        await db_write("UPDATE code_requests SET status='rejected' WHERE id=?", (req_id,))
        try:
            await bot.send_message(requester, "❌ **Code Request Reject Ho Gayi.**\nOwner ne approve nahi kiya.")
        except Exception: pass
    await event.edit("❌ **" + str(len(rows)) + " requests reject ho gayi.**", buttons=admin_kb(event.sender_id), parse_mode='md')

# Approve callback
@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"creq_ok_")))
async def cb_creq_ok(event):
    if not is_super_admin(event.sender_id): return
    req_id = int(event.data.decode().replace("creq_ok_", ""))
    row = c.execute("SELECT requested_by,days,status FROM code_requests WHERE id=?", (req_id,)).fetchone()
    if not row: await event.edit("❌ Request nahi mili."); return
    requester, days, status = row
    if status != "pending": await event.edit("⚠️ Already processed."); return
    code    = gen_code()
    expires = (now_utc() + timedelta(days=days)).isoformat()
    await db_write("INSERT INTO access_codes(code,days_valid,created_at,expires_at,created_by) VALUES(?,?,?,?,?)",
        (code, days, now_iso(), expires, requester))
    await db_write("UPDATE code_requests SET status=?,code=? WHERE id=?", ("approved", code, req_id))
    # Log approval
    _urow2 = c.execute("SELECT username FROM users WHERE user_id=?", (requester,)).fetchone()
    _un2   = _urow2[0] if _urow2 and _urow2[0] else ""
    await log_event("code_approved", ADMIN_ID, "Owner", code, "Approved for " + ("@" + _un2 if _un2 else str(requester)) + " | " + str(days) + " days")
    remaining = c.execute("SELECT COUNT(*) FROM code_requests WHERE status='pending'").fetchone()[0]
    await event.edit(
        "✅ **Approved!**\n\n"
        "🔑 `" + code + "`\n"
        "📅 " + str(days) + " din\n"
        "⏳ " + expires.split("T")[0] + "\n\n"
        "📋 Remaining pending: " + str(remaining)
    )
    try:
        await bot.send_message(requester,
            "✅ **Code Approved By Owner!**\n\n"
            "🔑 `" + code + "`\n"
            "📅 " + str(days) + " din\n"
            "⏳ " + expires.split("T")[0] + "\n\n"
            "/redeem " + code
        )
    except Exception: pass

# Reject callback
@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"creq_no_")))
async def cb_creq_no(event):
    if not is_super_admin(event.sender_id): return
    req_id = int(event.data.decode().replace("creq_no_", ""))
    row = c.execute("SELECT requested_by,days,status FROM code_requests WHERE id=?", (req_id,)).fetchone()
    if not row: await event.edit("❌ Request nahi mili."); return
    requester, days, status = row
    if status != "pending": await event.edit("⚠️ Already processed."); return
    await db_write("UPDATE code_requests SET status=? WHERE id=?", ("rejected", req_id))
    _urow3 = c.execute("SELECT username FROM users WHERE user_id=?", (requester,)).fetchone()
    _un3   = _urow3[0] if _urow3 and _urow3[0] else ""
    await log_event("code_rejected", ADMIN_ID, "Owner", "", "Rejected request from " + ("@" + _un3 if _un3 else str(requester)) + " | " + str(days) + " days")
    remaining = c.execute("SELECT COUNT(*) FROM code_requests WHERE status='pending'").fetchone()[0]
    await event.edit("❌ **Rejected.**\n\n📋 Remaining pending: " + str(remaining), parse_mode='md')
    try:
        await bot.send_message(requester, "❌ **Code Request Reject Ho Gayi.**\nOwner ne approve nahi kiya.")
    except Exception: pass

# ─────────────────────────── ADMIN COMMANDS PART 2 ──────────
@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() in ["/codes", "🔑 Codes"]))
async def cmd_codes(event):
    if not is_admin(event.sender_id): return
    uid = event.sender_id
    if not is_super_admin(uid):
        # Sub admin: only own approved codes
        rows = c.execute(
            "SELECT code,days_valid,claimed_by,expires_at,is_active FROM access_codes WHERE created_by=? ORDER BY rowid DESC", (uid,)
        ).fetchall()
        if not rows:
            await event.reply("🔑 **Tumhare Codes**\n\nAbhi tak koi code approve nahi hua.", buttons=admin_kb(event.sender_id)); return
        lines2 = ["🔑 **Tumhare Approved Codes** (" + str(len(rows)) + ")\n"]
        for code, days, cb, exp, active in rows:
            expired = now_utc() > parse_iso(exp)
            if not active:    st = "🚫 Revoked"
            elif expired:     st = "⌛ Expired"
            elif cb:          st = "✅ Claimed"
            else:             st = "🟢 Unclaimed"
            urow = c.execute("SELECT username FROM users WHERE user_id=?", (cb,)).fetchone() if cb else None
            claimant = ("@" + urow[0] if urow and urow[0] else "`" + str(cb) + "`") if cb else "—"
            lines2.append("━━━━━━━━━━━━\n" + st + " `" + code + "`\n📅 " + str(days) + "d | ⏳ " + exp.split("T")[0] + "\n👤 " + claimant)
        await event.reply("\n\n".join(lines2), buttons=admin_kb(event.sender_id)); return
    # Owner: codes grouped by admin
    all_sections = [(ADMIN_ID, "👑 Owner (You)")]
    for aid, auname in c.execute("SELECT user_id,username FROM admins").fetchall():
        all_sections.append((aid, "🔰 @" + auname if auname else "🔰 ID:" + str(aid)))
    full_text = "🔑 **All Codes By Admin**\n\n"
    buttons   = []
    for admin_id, admin_name in all_sections:
        rows = c.execute(
            "SELECT code,days_valid,claimed_by,expires_at,is_active FROM access_codes WHERE created_by=? ORDER BY rowid DESC LIMIT 15", (admin_id,)
        ).fetchall()
        if not rows: continue
        full_text += "━━━━━━━━━━━━━━━━━━━━━━\n" + admin_name + "  (" + str(len(rows)) + " codes)\n\n"
        for code, days, cb, exp, active in rows:
            expired = now_utc() > parse_iso(exp)
            if not active:  st = "🚫"
            elif expired:   st = "⌛"
            elif cb:        st = "✅ Claimed"
            else:           st = "🟢 Unclaimed"
            full_text += st + " `" + code + "` | " + str(days) + "d | " + exp.split("T")[0] + "\n"
            if active and not expired:
                buttons.append([Button.inline("🚫 Revoke " + code, ("rev_" + code).encode())])
        full_text += "\n"
    if full_text == "🔑 **All Codes By Admin**\n\n":
        await event.reply("Koi code nahi.", buttons=admin_kb(event.sender_id)); return
    await event.reply(full_text[:4000], buttons=buttons or None)

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() == "📋 My Requests"))
async def btn_my_requests(event):
    await cmd_approval(event)

@bot.on(events.NewMessage(pattern=r"^/approval$"))
async def cmd_approval(event):
    uid = event.sender_id
    if not is_admin(uid): return
    if is_super_admin(uid):
        await cmd_pending(event); return

    # ── SUB ADMIN: Full dashboard ──
    urow  = c.execute("SELECT username FROM users WHERE user_id=?", (uid,)).fetchone()
    uname = urow[0] if urow and urow[0] else str(uid)

    # Code requests
    reqs = c.execute(
        "SELECT id,days,status,code,requested_at FROM code_requests WHERE requested_by=? ORDER BY id DESC", (uid,)
    ).fetchall()

    # Own codes from access_codes
    codes = c.execute(
        "SELECT code,days_valid,claimed_by,expires_at,is_active FROM access_codes WHERE created_by=? ORDER BY rowid DESC", (uid,)
    ).fetchall()

    # Own logs
    logs = c.execute(
        "SELECT event_type,code,details,created_at FROM logs WHERE admin_id=? ORDER BY id DESC LIMIT 10", (uid,)
    ).fetchall()

    p  = sum(1 for r in reqs if r[2]=="pending")
    a  = sum(1 for r in reqs if r[2]=="approved")
    rj = sum(1 for r in reqs if r[2]=="rejected")

    out = (
        "📋 **My Panel — @" + uname + "**\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    # ── SECTION 1: Pending/Approval Requests ──
    out += "⏳ **Code Requests** — Pending: **" + str(p) + "** | ✅ " + str(a) + " | ❌ " + str(rj) + "\n\n"
    if reqs:
        for req_id, days, status, code, req_at in reqs:
            date = (req_at or "").split("T")[0]
            if status == "pending":
                st = "⏳ PENDING"
            elif status == "approved":
                st = "✅ APPROVED"
            else:
                st = "❌ REJECTED"
            out += "━━━━━━━━━━━━\n"
            out += st + "  #" + str(req_id) + "  |  📅 " + str(days) + " din  |  🕐 " + date + "\n"
            if status == "pending":
                out += "   ⏳ Owner approval ka wait kar raha hai...\n"
            elif status == "approved" and code:
                claimed_row = c.execute("SELECT claimed_by FROM access_codes WHERE code=?", (code,)).fetchone()
                clm = "✅ Claimed" if (claimed_row and claimed_row[0]) else "🟢 Unclaimed"
                out += "   🔑 Code: `" + code + "`\n"
                out += "   " + clm + "\n"
            elif status == "rejected":
                out += "   ❌ Owner ne reject kar diya.\n"
    else:
        out += "   Koi request nahi abhi tak. /gencode se bhejo.\n"

    # ── SECTION 2: My Codes ──
    out += "\n━━━━━━━━━━━━━━━━━━━━━━\n"
    out += "🔑 **My Codes** (" + str(len(codes)) + ")\n\n"
    if codes:
        for code, days, cb, exp, active in codes:
            expired = now_utc() > parse_iso(exp)
            if not active:   st = "🚫 Revoked"
            elif expired:    st = "⌛ Expired"
            elif cb:         st = "✅ Claimed"
            else:            st = "🟢 Unclaimed"
            urow2 = c.execute("SELECT username FROM users WHERE user_id=?", (cb,)).fetchone() if cb else None
            claimant = ("@" + urow2[0] if urow2 and urow2[0] else str(cb)) if cb else "—"
            out += st + "  `" + code + "`  |  " + str(days) + "d  |  ⏳" + exp.split("T")[0] + "\n"
            if cb:
                out += "   👤 Claimed by: " + claimant + "\n"
    else:
        out += "   Koi code nahi. Pehle gencode request karo.\n"

    # ── SECTION 3: Recent Activity Logs ──
    out += "\n━━━━━━━━━━━━━━━━━━━━━━\n"
    out += "📜 **My Recent Activity** (last 10)\n\n"
    icons = {"code_created": "🆕", "code_approved": "✅", "code_claimed": "🔑", "code_rejected": "❌"}
    if logs:
        for etype, lcode, details, created in logs:
            icon = icons.get(etype, "📌")
            date2 = (created or "").replace("T", " ").split(".")[0]
            out += icon + " " + etype.replace("_", " ").title() + "\n"
            if lcode: out += "   🔑 `" + lcode + "`\n"
            if details: out += "   📝 " + details + "\n"
            out += "   🕐 " + date2 + "\n\n"
    else:
        out += "   Koi activity nahi abhi tak.\n"

    out += "━━━━━━━━━━━━━━━━━━━━━━"
    await event.reply(out[:4096], buttons=admin_kb(event.sender_id))

@bot.on(events.NewMessage(pattern=r"^/extend\s+(\d+)\s+(\d+)$"))
async def cmd_extend(event):
    if not is_admin(event.sender_id): return
    await _do_extend(event, int(event.pattern_match.group(1)), int(event.pattern_match.group(2)), event.sender_id)

async def _do_extend(ctx, target_uid, days, admin_uid=None):
    admin_uid = admin_uid or ADMIN_ID
    row = c.execute("SELECT code,expires_at FROM access_codes WHERE claimed_by=? AND is_active=1", (target_uid,)).fetchone()
    if row:
        new_exp = (parse_iso(row[1]) + timedelta(days=days)).isoformat()
        await db_write("UPDATE access_codes SET expires_at=?,days_valid=days_valid+? WHERE code=?", (new_exp, days, row[0]))
        await ctx.reply("✅ `" + str(target_uid) + "` +" + str(days) + " din. Expiry: " + new_exp.split("T")[0], buttons=admin_kb(admin_uid), parse_mode='md')
    else:
        code    = gen_code()
        expires = (now_utc() + timedelta(days=days)).isoformat()
        await db_write("INSERT INTO access_codes(code,days_valid,created_at,claimed_by,claimed_at,expires_at,created_by) VALUES(?,?,?,?,?,?,?)",
            (code, days, now_iso(), target_uid, now_iso(), expires, admin_uid))
        await ctx.reply("✅ Code `" + code + "` given to `" + str(target_uid) + "`. Expiry: " + expires.split("T")[0], buttons=admin_kb(admin_uid), parse_mode='md')

@bot.on(events.NewMessage(pattern=r"^/revoke\s+(\S+)$"))
async def cmd_revoke(event):
    if not is_admin(event.sender_id): return
    code = event.pattern_match.group(1).upper()
    await db_write("UPDATE access_codes SET is_active=0 WHERE code=?", (code,))
    await event.reply("🚫 `" + code + "` revoked.", buttons=admin_kb(event.sender_id), parse_mode='md')

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() in ["/numbers", "📱 All Numbers"]))
async def cmd_numbers(event):
    if not is_admin(event.sender_id): return
    rows = c.execute("SELECT ua.phone,ua.user_id,u.username,ua.added_at FROM user_accounts ua LEFT JOIN users u ON ua.user_id=u.user_id ORDER BY ua.added_at DESC").fetchall()
    if not rows: await event.reply("Koi number nahi.", buttons=admin_kb(event.sender_id)); return
    lines2 = ["📱 **All Numbers** (" + str(len(rows)) + ")\n"]
    buttons = []
    for phone, uid2, uname, added in rows:
        name = "@" + uname if uname else "ID:" + str(uid2)
        lines2.append("• `" + phone + "` — " + name + "  /removenum " + phone)
        buttons.append([Button.inline("🗑 " + phone, ("rmnum_" + phone).encode())])
    await event.reply("\n".join(lines2), buttons=buttons, parse_mode='md')

@bot.on(events.NewMessage(pattern=r"^/removenum\s+(\+[\d\s\-()]+)$"))
async def cmd_removenum(event):
    if not is_admin(event.sender_id): return
    phone = norm_phone(event.pattern_match.group(1))
    ok = bool(phone) and await remove_account(phone)
    await event.reply(("🗑 `" + phone + "` removed (tasks band + session logout).") if ok else "❌ Yeh number linked nahi.",
                      buttons=admin_kb(event.sender_id), parse_mode='md')

@bot.on(events.NewMessage(pattern=r"^/endtrial\s+(\d+)$"))
async def cmd_endtrial(event):
    if not is_admin(event.sender_id): return
    uid = int(event.pattern_match.group(1))
    row = c.execute("SELECT user_id,username,trial_granted FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row: await event.reply("❌ User nahi mila."); return
    if not row[2]: await event.reply("⚠️ Is user ka trial tha hi nahi."); return
    await db_write("UPDATE users SET trial_expires=?,trial_granted=0 WHERE user_id=?", (now_iso(), uid))
    name = "@" + row[1] if row[1] else "`" + str(uid) + "`"
    await event.reply("✅ **" + name + " ka Trial Khatam!**", buttons=admin_kb(event.sender_id), parse_mode='md')
    try:
        await bot.send_message(uid, "⚠️ **Tumhara Trial Khatam Ho Gaya**\n\nAdmin ne trial end kar diya.\nAccess ke liye /redeem CODE karo.")
    except Exception: pass

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() in ["/tasks", "⏰ All Tasks"]))
async def cmd_tasks(event):
    if not is_admin(event.sender_id): return
    rows = c.execute("SELECT st.id,st.user_id,u.username,st.phone,st.interval_seconds,st.is_active,st.fail_count,st.messages_json FROM scheduled_tasks st LEFT JOIN users u ON st.user_id=u.user_id ORDER BY st.id DESC").fetchall()
    if not rows: await event.reply("Koi task nahi.", buttons=admin_kb(event.sender_id)); return
    lines2  = ["⏰ **All Tasks** (" + str(len(rows)) + ")\n"]
    buttons = []
    for tid, uid2, uname, phone, iv, act2, fails, mj in rows:
        name  = "@" + uname if uname else "ID:" + str(uid2)
        msgs  = msgs_list(mj)
        nm    = len(msgs)
        # Show first message preview
        preview = (msgs[0][:60] + "...") if msgs and len(msgs[0]) > 60 else (msgs[0] if msgs else "—")
        lines2.append(
            ("▶️" if act2 else "⏹") + " **#" + str(tid) + "** " + name + "\n"
            "   📱 `" + phone + "` | ⏱ " + fmt_mins(iv) + " | " + str(nm) + " msg\n"
            "   📝 `" + preview + "`"
        )
        row_btns = []
        if act2: row_btns.append(Button.inline("🛑 Stop #" + str(tid),  ("ast_"    + str(tid)).encode()))
        else:    row_btns.append(Button.inline("▶️ Start #" + str(tid), ("astart_" + str(tid)).encode()))
        row_btns.append(Button.inline("👁 Msgs #" + str(tid), ("atms_" + str(tid)).encode()))
        buttons.append(row_btns)
    await event.reply("\n\n".join(lines2), buttons=buttons or None, parse_mode='md')

# Admin view full messages of any task
@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"atms_")))
async def cb_atms(event):
    if not is_admin(event.sender_id): return
    tid = int(event.data.decode().replace("atms_", ""))
    row = c.execute(
        "SELECT st.messages_json, u.username, st.user_id, st.phone, st.interval_seconds "
        "FROM scheduled_tasks st LEFT JOIN users u ON st.user_id=u.user_id WHERE st.id=?", (tid,)
    ).fetchone()
    if not row: await event.answer("Task nahi mila.", alert=True); return
    mj, uname, uid2, phone, iv = row
    msgs  = msgs_list(mj)
    name  = "@" + uname if uname else "ID:" + str(uid2)
    lines = [
        "📝 **Task #" + str(tid) + " — Messages**\n"
        "👤 " + name + " | 📱 `" + phone + "` | ⏱ " + fmt_mins(iv) + "\n"
        "Total: **" + str(len(msgs)) + "** messages\n"
    ]
    for i, m in enumerate(msgs, 1):
        lines.append("**" + str(i) + ".** `" + m[:300] + "`")
    await event.edit("\n\n".join(lines)[:4000], parse_mode='md')

@bot.on(events.NewMessage(pattern=r"^/adminstarttask\s+(\d+)$"))
async def cmd_adminstarttask(event):
    if not is_admin(event.sender_id): return
    tid = int(event.pattern_match.group(1))
    row = c.execute("SELECT user_id,phone,interval_seconds,is_active FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
    if not row: await event.reply("❌ Task nahi mila."); return
    uid2, phone, iv, active = row
    if active: await event.reply("⚠️ Task already chal raha hai."); return
    sess_row = c.execute("SELECT session_str FROM user_accounts WHERE user_id=? AND phone=?", (uid2, phone)).fetchone()
    if not sess_row: await event.reply("❌ Session nahi mila."); return
    await db_write("UPDATE scheduled_tasks SET is_active=1,fail_count=0 WHERE id=?", (tid,))
    if tid not in scheduler_tasks:
        start_task(tid, uid2, phone, sess_row[0], iv)
    await event.reply("▶️ **Task #" + str(tid) + " Started!**", buttons=admin_kb(event.sender_id), parse_mode='md')

@bot.on(events.NewMessage(pattern=r"^/adminstoptask\s+(\d+)$"))
async def cmd_adminstoptask(event):
    if not is_admin(event.sender_id): return
    tid = int(event.pattern_match.group(1))
    await db_write("UPDATE scheduled_tasks SET is_active=0 WHERE id=?", (tid,))
    if tid in scheduler_tasks: scheduler_tasks[tid].cancel(); del scheduler_tasks[tid]
    await event.reply("🛑 Task #" + str(tid) + " stopped.", buttons=admin_kb(event.sender_id), parse_mode='md')

@bot.on(events.NewMessage(pattern=r"^/admindeltask\s+(\d+)$"))
async def cmd_admindeltask(event):
    if not is_admin(event.sender_id): return
    tid = int(event.pattern_match.group(1))
    await db_write("UPDATE scheduled_tasks SET is_active=0 WHERE id=?", (tid,))
    if tid in scheduler_tasks: scheduler_tasks[tid].cancel(); del scheduler_tasks[tid]
    await db_write("DELETE FROM scheduled_tasks WHERE id=?", (tid,))
    await event.reply("🗑 Task #" + str(tid) + " deleted.", buttons=admin_kb(event.sender_id), parse_mode='md')

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"astart_")))
async def cb_astart(event):
    if not is_admin(event.sender_id): return
    tid = int(event.data.decode().replace("astart_", ""))
    row = c.execute("SELECT user_id,phone,interval_seconds FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
    if not row: await event.answer("Task nahi mila.", alert=True); return
    uid2, phone, iv = row
    sess_row = c.execute("SELECT session_str FROM user_accounts WHERE user_id=? AND phone=?", (uid2, phone)).fetchone()
    if not sess_row:
        sess_row = c.execute("SELECT session_str FROM user_accounts WHERE phone=?", (phone,)).fetchone()
    if not sess_row: await event.answer("Session nahi mila.", alert=True); return
    await db_write("UPDATE scheduled_tasks SET is_active=1,fail_count=0 WHERE id=?", (tid,))
    if tid not in scheduler_tasks:
        start_task(tid, uid2, phone, sess_row[0], iv)
    await event.answer("▶️ Started!")
    await event.edit("▶️ **Task #" + str(tid) + " started!**", parse_mode='md')

@bot.on(events.NewMessage(pattern=r"^/usergroups\s+(\d+)$"))
async def cmd_usergroups(event):
    if not is_admin(event.sender_id): return
    uid = int(event.pattern_match.group(1))
    msg = await event.reply("🔍 Fetching...")
    accounts = c.execute("SELECT phone,session_str FROM user_accounts WHERE user_id=?", (uid,)).fetchall()
    urow = c.execute("SELECT username FROM users WHERE user_id=?", (uid,)).fetchone()
    name = "@" + urow[0] if urow and urow[0] else "ID:" + str(uid)
    if not accounts: await msg.edit("📊 " + name + " ke koi accounts nahi."); return
    lines2 = ["📊 **" + name + " ke Groups & Channels**\n"]
    for phone, sess in accounts:
        cl = await open_client(phone, sess)
        if not cl: lines2.append("\n📵 `" + phone + "`: fail"); continue
        try:
            dlgs     = await cl.get_dialogs(limit=None)
            channels = [d for d in dlgs if d.is_channel]
            groups   = [d for d in dlgs if d.is_group and not d.is_channel]
            lines2.append(
                "\n📱 `" + phone + "`\n"
                "📣 Channels: **" + str(len(channels)) + "** | "
                "👥 Groups: **" + str(len(groups)) + "**\n"
            )
            if channels:
                lines2.append("━━ 📣 **CHANNELS** ━━")
                for g in channels:
                    un = "@" + g.entity.username if getattr(g.entity, "username", None) else "🔒 private"
                    lines2.append("  📣 " + g.name + "  " + un)
            if groups:
                lines2.append("\n━━ 👥 **GROUPS** ━━")
                for g in groups:
                    un = "@" + g.entity.username if getattr(g.entity, "username", None) else "🔒 private"
                    lines2.append("  👥 " + g.name + "  " + un)
        except Exception as e: lines2.append("\n⚠️ `" + phone + "`: " + str(e))
        finally: await close(cl)
    await msg.edit("\n".join(lines2)[:4000], parse_mode='md')

@bot.on(events.NewMessage(pattern=r"^/sendmsg\s+(\d+)\s+(.+)$"))
async def cmd_sendmsg(event):
    if not is_admin(event.sender_id): return
    target = int(event.pattern_match.group(1))
    text   = event.pattern_match.group(2).strip()
    try:
        await bot.send_message(target, "📨 **Admin message:**\n\n" + text)
        await event.reply("✅ Sent to `" + str(target) + "`.", buttons=admin_kb(event.sender_id), parse_mode='md')
    except Exception as e:
        await event.reply("❌ Failed: " + str(e), buttons=admin_kb(event.sender_id), parse_mode='md')

@bot.on(events.NewMessage(func=lambda e: e.text and (e.text.strip() == "📢 Broadcast" or e.text.strip().startswith("/broadcast"))))
async def cmd_broadcast(event):
    if not is_admin(event.sender_id): return
    import re
    text = event.text.strip()
    if text == "📢 Broadcast":
        pending[event.sender_id] = {"action": "admin_broadcast"}
        await event.reply("📢 Message type karo:", buttons=[[Button.text("❌ Cancel")]]); return
    m = re.match(r"^/broadcast\s+(.+)$", text, re.DOTALL)
    if not m: await event.reply("Usage: /broadcast text"); return
    await _do_broadcast(event, m.group(1).strip())

async def _do_broadcast(ctx, text):
    users = [r[0] for r in c.execute("SELECT user_id FROM users WHERE is_banned=0").fetchall()]
    prog  = await ctx.reply("📢 Sending to " + str(len(users)) + " users...")
    body  = "📢 **Admin message:**\n\n" + text
    sem   = asyncio.Semaphore(5)   # 5 parallel — Telegram bot limit (~30 msg/sec) ke andar

    async def _one(u):
        async with sem:
            try:
                await bot.send_message(u, body); return True
            except FloodWaitError as fw:
                await asyncio.sleep(fw.seconds + 1)
                try:
                    await bot.send_message(u, body); return True
                except Exception: return False
            except Exception:
                return False

    results = await asyncio.gather(*[_one(u) for u in users])
    sent = sum(1 for r in results if r); failed = len(results) - sent
    bcast_uid = getattr(ctx, 'sender_id', ADMIN_ID) or ADMIN_ID
    await prog.edit("📢 **Done!** ✅ " + str(sent) + " | ❌ " + str(failed), buttons=admin_kb(bcast_uid), parse_mode='md')

# ─────────────────────────── BUTTON HANDLERS ─────────────────
@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() == "➕ Add Account"))
async def btn_add(event): await cmd_addaccount(event)

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() == "📊 My Groups"))
async def btn_groups(event): await cmd_mygroups(event)

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() == "⏰ Schedule Msg"))
async def btn_sched(event): await cmd_schedule(event)

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() == "🚀 Send Now"))
async def btn_sendnow(event):
    uid   = event.sender_id
    ok, _ = await check_access(uid)
    if not ok: await event.reply("❌ Access nahi."); return
    has_acct = c.execute("SELECT COUNT(*) FROM user_accounts WHERE user_id=?", (uid,)).fetchone()[0]
    if not has_acct and not is_admin(uid):
        await event.reply("❌ Account nahi. /addaccount karo."); return
    if not has_acct and is_admin(uid):
        has_acct = c.execute("SELECT COUNT(*) FROM user_accounts").fetchone()[0]
        if not has_acct:
            await event.reply("❌ Koi account nahi system mein."); return
    pending[uid] = {"action": "await_msg", "mode": "send_now"}
    await event.reply("✏️ Message type karo (ya forward karo):\n/cancel se wapas.", parse_mode='md')

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() == "📋 My Schedules"))
async def btn_scheds(event): await _show_schedules(event, event.sender_id, edit=False)

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() == "🔑 Redeem Code"))
async def btn_redeem(event):
    uid = event.sender_id
    pending[uid] = {"action": "await_redeem_code"}
    await event.reply("🔑 Code type karo:\n/cancel se wapas.", buttons=[[Button.text("❌ Cancel")]], parse_mode='md')

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() == "💬 Buy Access"))
async def btn_buy(event):
    await cmd_buy(event)

@bot.on(events.NewMessage(func=lambda e: e.text and e.text.strip() == "❌ Cancel"))
async def btn_cancel(event):
    uid = event.sender_id
    if uid in pending:
        cl = pending[uid].get("client")
        if cl: await close(cl)
        pending.pop(uid, None)
    await event.reply("✅ Cancel ho gaya.", buttons=main_kb(), parse_mode='md')

# ─────────────────────────── FORWARD ─────────────────────────
@bot.on(events.NewMessage(func=lambda e: e.message and e.message.fwd_from is not None))
async def on_forward(event):
    uid   = event.sender_id
    ok, _ = await check_access(uid)
    if not ok: await event.reply("❌ Access nahi.", parse_mode='md'); return

    text = event.message.message or ""

    # ── Get ORIGINAL source from fwd_from ──
    fwd      = event.message.fwd_from
    orig_id  = None
    orig_peer= None

    if fwd:
        # Channel post forward
        if getattr(fwd, "channel_post", None) and getattr(fwd, "from_id", None):
            orig_id   = fwd.channel_post
            from_id   = fwd.from_id
            if hasattr(from_id, "channel_id"):
                orig_peer = from_id.channel_id
            elif hasattr(from_id, "chat_id"):
                orig_peer = from_id.chat_id
            elif hasattr(from_id, "user_id"):
                orig_peer = from_id.user_id
        # User message forward
        elif getattr(fwd, "saved_from_msg_id", None):
            orig_id   = fwd.saved_from_msg_id
            orig_peer = getattr(getattr(fwd, "saved_from_peer", None), "channel_id", None)

    # Entities from current message (FIX: shared helper keeps CustomEmoji document_id)
    ents_json = entities_to_json(text, event.message.entities)
    has_media = message_has_media(event.message)

    st = pending.get(uid, {})
    if st.get("action") == "await_msg" and st.get("mode") == "schedule":
        pad_capture(st)                            # FIX: keep lists aligned with messages
        msgs      = st.setdefault("messages", [])
        msg_ids   = st.setdefault("msg_ids", [])   # original msg IDs
        peers     = st.setdefault("peers", [])      # original chat/channel IDs
        ents_list = st.setdefault("entities_list", [])
        msgs.append(text)
        msg_ids.append(orig_id)
        peers.append(orig_peer)
        ents_list.append(ents_json)
        st.setdefault("media_flags", []).append(has_media)
        has_src = "✅ Original source mila!" if orig_id and orig_peer else "⚠️ Source nahi mila, entities use hongi"
        await event.reply(
            f"📩 **Message #{len(msgs)} added!**\n{has_src}\n`{text[:80]}`",
            buttons=[
                [Button.inline(f"➕ Add #{len(msgs)+1}", b"add_msg")],
                [Button.inline("▶️ Continue",           b"msgs_done")],
                [Button.inline("❌ Cancel",             b"cx")],
            ], parse_mode='md'
        )
    elif st.get("action") == "tedit_msg":
        tid = st["tid"]
        pending.pop(uid, None)
        await db_write(
            "UPDATE scheduled_tasks SET messages_json=?, msg_ids_json=?, source_chat_id=? WHERE id=?",
            # FIX: new-format JSON (pairs + entities) instead of a bare [orig_id] list that was misread as entities
            (json.dumps([text]),
             json.dumps({"pairs": [[orig_id, orig_peer, has_media]], "ents": [ents_json]}),
             orig_peer, tid)
        )
        if tid in scheduler_tasks:
            scheduler_tasks[tid].cancel(); del scheduler_tasks[tid]
        row_e = c.execute("SELECT phone, interval_seconds, is_active FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
        if row_e and row_e[2]:
            sess_e = c.execute("SELECT session_str FROM user_accounts WHERE user_id=? AND phone=?", (uid, row_e[0])).fetchone()
            if not sess_e:
                sess_e = c.execute("SELECT session_str FROM user_accounts WHERE phone=?", (row_e[0],)).fetchone()
            if sess_e: start_task(tid, uid, row_e[0], sess_e[0], row_e[1])
        await event.reply(
            f"✅ **Task #{tid} update ho gaya!**\n`{text[:80]}`",
            buttons=main_kb(), parse_mode='md'
        )
    else:
        pending[uid] = {
            "action":  "msg_ready", "text": text,
            "orig_id": orig_id,     "orig_peer": orig_peer,
            "ents_json": ents_json, "has_media": has_media
        }
        await event.reply(
            f"📩 **Forward detect hua!**\n`{text[:100]}`\n\nKya karna hai?",
            buttons=action_btns(), parse_mode='md'
        )

# ─────────────────────────── CALLBACKS ───────────────────────
@bot.on(events.CallbackQuery(data=b"cx"))
async def cb_cx(event):
    await drop_pending(event.sender_id)
    await event.edit("❌ Cancel ho gaya.", parse_mode='md')

@bot.on(events.CallbackQuery(data=b"resend_otp"))
async def cb_resend_otp(event):
    uid = event.sender_id
    p   = pending.get(uid)
    if not p or p.get("action") != "add_otp":
        await event.answer("⚠️ Koi active login nahi — /addaccount se shuru karo.", alert=True); return
    wait = 30 - (time.time() - p.get("sent_at", 0))
    if wait > 0:
        await event.answer(f"⏳ {int(wait) + 1} sec baad resend karo.", alert=True); return
    resends = p.get("resends", 0) + 1
    if resends > 3:
        await drop_pending(uid)
        await event.edit("⛔ Resend limit (3) ho gayi.\nThodi der baad /addaccount se dobara try karo."); return
    phone = p["phone"]
    await drop_pending(uid)                       # purana client band
    ok, msg = await _start_login(uid, phone, resends)
    if ok:
        await event.edit(msg, buttons=[Button.inline("🔄 OTP Resend", b"resend_otp")], parse_mode='md')
        await event.answer("✅ Naya OTP bheja")
    else:
        await event.edit(msg, parse_mode='md')

@bot.on(events.CallbackQuery(data=b"noop"))
async def cb_noop(event): await event.answer()

@bot.on(events.CallbackQuery(data=b"do_send_now"))
async def cb_do_send_now(event):
    uid = event.sender_id
    if uid not in pending or pending[uid].get("action") != "msg_ready":
        await event.answer("Koi message nahi.", alert=True); return
    _p       = pending.pop(uid)
    text     = _p["text"]
    ents_js  = _p.get("ents_json")            # FIX: carry entities (custom emoji) into Send Now
    accounts = c.execute("SELECT phone,session_str FROM user_accounts WHERE user_id=?", (uid,)).fetchall()
    if not accounts and is_admin(uid):
        accounts = c.execute("SELECT phone,session_str FROM user_accounts").fetchall()
    if not accounts: await event.edit("❌ Koi account nahi."); return
    await event.edit("📤 Sending...", parse_mode='md')
    await _send_now_core(event, uid, text, accounts, ents_js)

@bot.on(events.CallbackQuery(data=b"do_schedule"))
async def cb_do_schedule(event):
    uid = event.sender_id
    if uid not in pending or pending[uid].get("action") != "msg_ready":
        await event.answer("Koi message nahi.", alert=True); return
    _p   = pending[uid]
    text = _p.pop("text")
    # FIX: seed capture lists so the first message keeps its entities (custom emoji).
    # Pair stays None: this message was always sent as text (not forwarded) — behaviour unchanged.
    _p.update({"action": "schedule_pick_account", "messages": [text],
               "msg_ids": [None], "peers": [None],
               "entities_list": [_p.get("ents_json") or "[]"],
               "media_flags": [False]})
    await _show_acct_picker(event, uid)

@bot.on(events.CallbackQuery(data=b"view_tasks"))
async def cb_view_tasks(event):
    pending.pop(event.sender_id, None)
    await _show_schedules(event, event.sender_id, edit=True)

@bot.on(events.CallbackQuery(data=b"add_msg"))
async def cb_add_msg(event):
    uid  = event.sender_id
    msgs = pending.get(uid, {}).get("messages", [])
    pending[uid]["action"] = "await_msg"
    pending[uid]["mode"]   = "schedule"
    await event.edit(f"✅ {len(msgs)} message(s) ready!\n📝 **Message #{len(msgs)+1} type karo:**\n/cancel se wapas.")

@bot.on(events.CallbackQuery(data=b"msgs_done"))
async def cb_msgs_done(event):
    uid  = event.sender_id
    msgs = pending.get(uid, {}).get("messages", [])
    if not msgs: await event.answer("Koi message nahi!", alert=True); return
    pending[uid]["action"] = "schedule_pick_account"
    await _show_acct_picker(event, uid)

async def _show_acct_picker(event, uid):
    await verify_accounts(uid)
    accounts = c.execute("SELECT phone FROM user_accounts WHERE user_id=?", (uid,)).fetchall()
    if not accounts and is_admin(uid):
        accounts = c.execute(
            "SELECT ua.phone, u.username FROM user_accounts ua LEFT JOIN users u ON ua.user_id=u.user_id"
        ).fetchall()
        if not accounts:
            await event.edit("❌ Koi account nahi."); pending.pop(uid, None); return
        msgs    = pending[uid].get("messages", [])
        preview = "\n".join(f"  {i+1}. `{m[:50]}`" for i, m in enumerate(msgs))
        btns    = [[Button.inline(f"📱 {row[0]} (@{row[1] or '?'})", f"acct_{row[0]}".encode())] for row in accounts]
        btns.append([Button.inline("❌ Cancel", b"cx")])
        await event.edit(f"📝 **{len(msgs)} msg(s):**\n{preview}\n\n📱 **Kaunsa account use karein?**", buttons=btns)
        return
    if not accounts: await event.edit("❌ Koi account nahi. /addaccount karo."); pending.pop(uid, None); return
    msgs    = pending[uid].get("messages", [])
    preview = "\n".join(f"  {i+1}. `{m[:50]}`" for i, m in enumerate(msgs))
    btns    = [[Button.inline(f"📱 {ph[0]}", f"acct_{ph[0]}".encode())] for ph in accounts]
    btns.append([Button.inline("❌ Cancel", b"cx")])
    await event.edit(f"📝 **{len(msgs)} msg(s):**\n{preview}\n\n📱 **Kaunsa account?**", buttons=btns)

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"acct_")))
async def cb_acct(event):
    uid   = event.sender_id
    phone = event.data.decode().replace("acct_", "")
    if uid not in pending or pending[uid].get("action") != "schedule_pick_account":
        await event.answer("Session expire.", alert=True); return
    sess_row = c.execute("SELECT session_str FROM user_accounts WHERE user_id=? AND phone=?", (uid, phone)).fetchone()
    if not sess_row and is_admin(uid):
        sess_row = c.execute("SELECT session_str FROM user_accounts WHERE phone=?", (phone,)).fetchone()
    if not sess_row: await event.edit("❌ Account nahi mila."); pending.pop(uid, None); return
    pending[uid]["selected_phone"] = phone
    pending[uid]["selected_sess"]  = sess_row[0]
    pending[uid]["action"]         = "schedule_interval"
    await event.edit(
        f"✅ Account: `{phone}`\n\n⏰ **Interval choose karo:**",
        buttons=[
            [Button.inline("⏱ 5 min",   b"iv5"),   Button.inline("⏱ 10 min",  b"iv10")],
            [Button.inline("⏱ 15 min",  b"iv15"),  Button.inline("⏱ 30 min",  b"iv30")],
            [Button.inline("⏱ 45 min",  b"iv45"),  Button.inline("⏱ 1 hour",  b"iv60")],
            [Button.inline("⏱ 2 hours", b"iv120"), Button.inline("⏱ 6 hours", b"iv360")],
            [Button.inline("📅 12h",    b"iv720"), Button.inline("📅 Daily",  b"iv1440")],
            [Button.inline("✏️ Custom minutes", b"iv_custom")],
            [Button.inline("❌ Cancel", b"cx")],
        ]
    )

IV_MAP = {
    b"iv5":300, b"iv10":600, b"iv15":900, b"iv30":1800,
    b"iv45":2700, b"iv60":3600, b"iv120":7200, b"iv180":10800,
    b"iv360":21600, b"iv720":43200, b"iv1440":86400,
}

@bot.on(events.CallbackQuery(data=lambda d: d in IV_MAP))
async def cb_interval(event):
    uid = event.sender_id
    if uid not in pending or pending[uid].get("action") != "schedule_interval":
        await event.answer("Session expire.", alert=True); return
    await _create_task_cb(event, uid, IV_MAP[event.data])

@bot.on(events.CallbackQuery(data=b"iv_custom"))
async def cb_iv_custom(event):
    uid = event.sender_id
    if uid not in pending: await event.answer("Session expire.", alert=True); return
    pending[uid]["action"] = "schedule_custom_iv"
    await event.edit("✏️ **Kitne minutes?** Type karo:\nExamples: `5` `42` `200` (minimum 1)", parse_mode='md')

async def _create_task_cb(event, uid, iv_sec):
    if uid in pending:
        pending[uid]["iv_sec"] = iv_sec
    await _finalize_task(event, uid, "all")

async def _finalize_task(event, uid, send_to):
    data  = pending.pop(uid, {})
    msgs  = data.get("messages", [])
    phone = data.get("selected_phone")
    sess  = data.get("selected_sess")
    iv_sec= data.get("iv_sec", 1800)
    pad_capture(data)                          # FIX: guarantee index alignment with msgs
    ents_list  = data.get("entities_list", [])
    msg_ids    = data.get("msg_ids", [])
    peers      = data.get("peers", [])
    media_flags= data.get("media_flags", [])
    source_cid = data.get("source_chat_id") or (peers[0] if peers else None)
    custom_tgts= data.get("custom_targets", [])
    # Store peers as msg_ids_json combined: [[msg_id, peer], ...]
    fwd_pairs  = [[mid, peer, bool(mf)] for mid, peer, mf in zip(msg_ids, peers, media_flags)]
    if not phone:
        row = c.execute("SELECT phone,session_str FROM user_accounts WHERE user_id=?", (uid,)).fetchone()
        if not row and is_admin(uid):
            row = c.execute("SELECT phone,session_str FROM user_accounts").fetchone()
        if not row: await event.edit("❌ Koi account nahi."); return
        phone, sess = row
    tid = await db_write(
        "INSERT INTO scheduled_tasks(user_id,phone,messages_json,interval_seconds,next_run,msg_ids_json,source_chat_id,send_to,custom_targets) VALUES(?,?,?,?,?,?,?,?,?)",
        (uid, phone, json.dumps(msgs), iv_sec,
         (now_utc() + timedelta(seconds=iv_sec)).isoformat(),
         json.dumps({"pairs": fwd_pairs, "ents": ents_list}),
         source_cid, send_to, json.dumps(custom_tgts))
    )
    start_task(tid, uid, phone, sess, iv_sec)
    send_label = "👥 Groups" if send_to=="groups" else ("📣 Channels" if send_to=="channels" else "🌐 Sab")
    preview = "\n".join(f"  {i+1}. `{m[:60]}`" for i, m in enumerate(msgs))
    await event.edit(
        f"✅ **Task #{tid} Schedule Ho Gaya!**\n\n📱 `{phone}`\n⏱ Har **{iv_sec//60} min**\n"
        f"💬 **{len(msgs)} msg(s):**\n{preview}\n\n/myschedules  /stoptask {tid}  /deltask {tid}"
    )

# Admin inline callbacks
@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"uinfo_")))
async def cb_uinfo(event):
    if not is_admin(event.sender_id): return
    await event.answer()  # instant response — stops loading spinner
    uid = int(event.data.decode().replace("uinfo_", ""))
    await _show_userinfo(event, uid, event.sender_id)

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"ugrp_")))
async def cb_ugrp(event):
    if not is_admin(event.sender_id): return
    uid  = int(event.data.decode().replace("ugrp_", ""))
    await event.edit("🔍 Fetching groups...", parse_mode='md')
    accounts = c.execute("SELECT phone,session_str FROM user_accounts WHERE user_id=?", (uid,)).fetchall()
    urow = c.execute("SELECT username FROM users WHERE user_id=?", (uid,)).fetchone()
    name = f"@{urow[0]}" if urow and urow[0] else f"ID:{uid}"
    if not accounts: await event.edit(f"📊 {name} ke koi accounts nahi."); return
    lines = [f"📊 **{name} ke Groups**\n"]
    for phone, sess in accounts:
        cl = await open_client(phone, sess)
        if not cl: lines.append(f"\n📵 `{phone}`: fail"); continue
        try:
            dlgs = await cl.get_dialogs(limit=None)
            grps = [d for d in dlgs if d.is_group or d.is_channel]
            lines.append(f"\n📱 `{phone}` — {len(grps)}:")
            for g in grps:
                icon  = "📣" if g.is_channel else "👥"
                uname = f"@{g.entity.username}" if getattr(g.entity, 'username', None) else "🔒 private"
                lines.append(f"  {icon} {g.name}  |  {uname}")
        except Exception as e: lines.append(f"\n⚠️ `{phone}`: {e}")
        finally: await close(cl)
    await event.edit("\n".join(lines)[:4000], parse_mode='md')

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"uext_")))
async def cb_uext(event):
    if not is_admin(event.sender_id): return
    uid = int(event.data.decode().replace("uext_", ""))
    pending[event.sender_id] = {"action": "admin_extend", "target_uid": uid}
    await event.edit(f"➕ User `{uid}` — kitne extra days?", buttons=[[Button.inline("❌ Cancel", b"cx")]])

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"uban_")))
async def cb_uban(event):
    if not is_admin(event.sender_id): return
    uid = int(event.data.decode().replace("uban_", ""))
    await db_write("UPDATE users SET is_banned=1 WHERE user_id=?", (uid,))
    await event.answer("🚫 Banned!")
    await _show_userinfo(event, uid, event.sender_id)

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"uunb_")))
async def cb_uunb(event):
    if not is_admin(event.sender_id): return
    uid = int(event.data.decode().replace("uunb_", ""))
    await db_write("UPDATE users SET is_banned=0 WHERE user_id=?", (uid,))
    await event.answer("✅ Unbanned!")
    await _show_userinfo(event, uid, event.sender_id)

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"upr_")))
async def cb_upr(event):
    if not is_super_admin(event.sender_id):
        await event.answer("❌ Sirf Owner!", alert=True); return
    uid = int(event.data.decode().replace("upr_", ""))
    row = c.execute("SELECT is_protected FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row: await event.answer("User nahi mila.", alert=True); return
    new_val = 0 if row[0] else 1
    await db_write("UPDATE users SET is_protected=? WHERE user_id=?", (new_val, uid))
    await event.answer("🔒 Protected!" if new_val else "🔓 Unprotected!")
    await _show_userinfo(event, uid, event.sender_id)

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"uet_")))
async def cb_uet(event):
    if not is_admin(event.sender_id):
        await event.answer("❌ Admin only!", alert=True); return
    uid = int(event.data.decode().replace("uet_", ""))
    row = c.execute("SELECT trial_granted,username FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row: await event.answer("User nahi mila.", alert=True); return
    if not row[0]:
        await event.answer("⚠️ Is user ka trial tha hi nahi.", alert=True); return
    await db_write("UPDATE users SET trial_expires=?,trial_granted=0 WHERE user_id=?", (now_iso(), uid))
    name = "@" + row[1] if row[1] else str(uid)
    await event.answer("✅ Trial ended!")
    try:
        await bot.send_message(uid,
            "⚠️ **Tumhara Trial Khatam Ho Gaya**\n\n"
            "Admin ne tumhara trial end kar diya.\n"
            "Access ke liye /redeem CODE karo."
        )
    except Exception: pass
    await _show_userinfo(event, uid, event.sender_id)

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"udelc_")))
async def cb_udelc(event):
    if not is_admin(event.sender_id): return
    uid = int(event.data.decode().replace("udelc_", ""))
    row = c.execute("SELECT username FROM users WHERE user_id=?", (uid,)).fetchone()
    name = f"@{row[0]}" if row and row[0] else f"ID:{uid}"
    await event.edit(
        f"⚠️ **Confirm delete `{name}`?**\nSaara data delete hoga.",
        buttons=[[Button.inline("✅ Delete", f"udely_{uid}".encode()),
                  Button.inline("❌ Cancel", b"cx")]]
    )

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"udely_")))
async def cb_udely(event):
    if not is_admin(event.sender_id): return
    uid = int(event.data.decode().replace("udely_", ""))
    await _del_user(uid)
    await event.edit(f"🗑 User `{uid}` deleted.")

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"rmnum_")))
async def cb_rmnum(event):
    if not is_admin(event.sender_id): return
    phone = event.data.decode().replace("rmnum_", "")
    ok = await remove_account(phone)
    await event.edit(f"🗑 `{phone}` removed." if ok else f"❌ `{phone}` linked nahi.")

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"rev_")))
async def cb_rev(event):
    if not is_admin(event.sender_id): return
    code = event.data.decode().replace("rev_", "")
    await db_write("UPDATE access_codes SET is_active=0 WHERE code=?", (code,))
    await event.edit(f"🚫 `{code}` revoked.")

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"ast_")))
async def cb_ast(event):
    if not is_admin(event.sender_id): return
    tid = int(event.data.decode().replace("ast_", ""))
    await db_write("UPDATE scheduled_tasks SET is_active=0 WHERE id=?", (tid,))
    if tid in scheduler_tasks: scheduler_tasks[tid].cancel(); del scheduler_tasks[tid]
    await event.edit(f"🛑 Task #{tid} stopped.")

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"tsp_") and d != b"tsp_all"))
async def cb_tsp(event):
    uid = event.sender_id
    tid = int(event.data.decode().replace("tsp_", ""))
    row = c.execute("SELECT user_id FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
    if not row or row[0] != uid: await event.answer("❌ Tumhara nahi.", alert=True); return
    await db_write("UPDATE scheduled_tasks SET is_active=0 WHERE id=?", (tid,))
    if tid in scheduler_tasks: scheduler_tasks[tid].cancel(); del scheduler_tasks[tid]
    await event.answer("⏹ Stopped!")
    await _show_schedules(event, uid, edit=True)

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"tst_") and d != b"tst_all"))
async def cb_tst(event):
    uid = event.sender_id
    tid = int(event.data.decode().replace("tst_", ""))
    row = c.execute("SELECT user_id,phone,interval_seconds FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
    if not row or row[0] != uid: await event.answer("❌ Tumhara nahi.", alert=True); return
    _, phone, iv = row
    sess_row = c.execute("SELECT session_str FROM user_accounts WHERE user_id=? AND phone=?", (uid, phone)).fetchone()
    if not sess_row:
        sess_row = c.execute("SELECT session_str FROM user_accounts WHERE phone=?", (phone,)).fetchone()
    if not sess_row:
        await event.answer("❌ Account nahi mila. /addaccount karo.", alert=True); return
    await db_write("UPDATE scheduled_tasks SET is_active=1,fail_count=0 WHERE id=?", (tid,))
    if tid not in scheduler_tasks:
        start_task(tid, uid, phone, sess_row[0], iv)
    await event.answer("▶️ Started!")
    await _show_schedules(event, uid, edit=True)

@bot.on(events.CallbackQuery(data=b"tst_all"))
async def cb_tst_all(event):
    uid  = event.sender_id
    rows = c.execute(
        "SELECT id,phone,interval_seconds FROM scheduled_tasks WHERE user_id=? AND is_active=0", (uid,)
    ).fetchall()
    started = 0
    for tid, phone, iv in rows:
        sess_row = c.execute("SELECT session_str FROM user_accounts WHERE user_id=? AND phone=?", (uid, phone)).fetchone()
        if not sess_row:
            sess_row = c.execute("SELECT session_str FROM user_accounts WHERE phone=?", (phone,)).fetchone()
        if not sess_row: continue
        await db_write("UPDATE scheduled_tasks SET is_active=1,fail_count=0 WHERE id=?", (tid,))
        if tid not in scheduler_tasks:
            start_task(tid, uid, phone, sess_row[0], iv)
        started += 1
    await event.answer("▶️ " + str(started) + " tasks started!")
    await _show_schedules(event, uid, edit=True)

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"tdl_") and d != b"tdl_all"))
async def cb_tdl(event):
    uid = event.sender_id
    tid = int(event.data.decode().replace("tdl_", ""))
    row = c.execute("SELECT user_id FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
    if not row or row[0] != uid: await event.answer("❌ Tumhara nahi.", alert=True); return
    await db_write("UPDATE scheduled_tasks SET is_active=0 WHERE id=?", (tid,))
    if tid in scheduler_tasks: scheduler_tasks[tid].cancel(); del scheduler_tasks[tid]
    await db_write("DELETE FROM scheduled_tasks WHERE id=?", (tid,))
    await event.edit(f"🗑 Task #{tid} deleted.")

@bot.on(events.CallbackQuery(data=b"tsp_all"))
async def cb_tsp_all(event):
    uid = event.sender_id
    await db_write("UPDATE scheduled_tasks SET is_active=0 WHERE user_id=?", (uid,))
    for tid in list(scheduler_tasks):
        row = c.execute("SELECT user_id FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
        if row and row[0] == uid: scheduler_tasks[tid].cancel(); del scheduler_tasks[tid]
    await event.answer("⏹ Sab stopped!")
    await _show_schedules(event, uid, edit=True)

@bot.on(events.CallbackQuery(data=b"tdl_all"))
async def cb_tdl_all(event):
    uid = event.sender_id
    for tid, in c.execute("SELECT id FROM scheduled_tasks WHERE user_id=?", (uid,)).fetchall():
        if tid in scheduler_tasks: scheduler_tasks[tid].cancel(); del scheduler_tasks[tid]
    await db_write("DELETE FROM scheduled_tasks WHERE user_id=?", (uid,))
    await event.edit("🗑 Saare tasks delete ho gaye!", parse_mode='md')

@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"tms_")))
async def cb_tms(event):
    uid = event.sender_id
    tid = int(event.data.decode().replace("tms_", ""))
    row = c.execute("SELECT user_id,messages_json FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
    if not row or row[0] != uid: await event.answer("❌ Tumhara nahi.", alert=True); return
    msgs  = msgs_list(row[1])
    lines = [f"📝 **Task #{tid} — {len(msgs)} Messages:**\n"]
    for i, m in enumerate(msgs, 1): lines.append(f"**{i}.** `{m[:200]}`\n")
    await event.edit("\n".join(lines)[:4000], parse_mode='md')

# ── Edit message text ──────────────────────────────────────
@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"tedit_msg_")))
async def cb_tedit_msg(event):
    uid = event.sender_id
    tid = int(event.data.decode().replace("tedit_msg_", ""))
    row = c.execute("SELECT user_id FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
    if not row or row[0] != uid: await event.answer("❌ Tumhara nahi.", alert=True); return
    pending[uid] = {"action": "tedit_msg", "tid": tid}
    await event.edit(
        f"✏️ **Task #{tid} — Naya message bhejo ya forward karo:**\n\n"
        f"⚠️ Purana message replace ho jayega.\n/cancel se wapas.",
        buttons=[[Button.inline("❌ Cancel", b"cx")]], parse_mode='md'
    )

# ── Edit interval time ───────────────────────────────────
@bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"tedit_iv_")))
async def cb_tedit_iv(event):
    uid = event.sender_id
    tid = int(event.data.decode().replace("tedit_iv_", ""))
    row = c.execute("SELECT user_id, interval_seconds FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
    if not row or row[0] != uid: await event.answer("❌ Tumhara nahi.", alert=True); return
    curr_mins = row[1] // 60
    pending[uid] = {"action": "tedit_iv", "tid": tid}
    await event.edit(
        f"⏱ **Task #{tid} — Naya interval set karo:**\n\n"
        f"Current: **{curr_mins} minutes**\n\n"
        f"Naya interval minutes mein type karo (e.g. `30`):\n/cancel se wapas.",
        buttons=[[Button.inline("❌ Cancel", b"cx")]], parse_mode='md'
    )

# ─────────────────────────── HELPERS ─────────────────────────
async def _do_redeem(ctx, uid, code):
    row = c.execute("SELECT * FROM access_codes WHERE code=?", (code,)).fetchone()
    if not row: await ctx.reply("❌ Code exist nahi karta."); return
    code_val = row[0]
    days     = row[1]
    claimed_by = row[3]
    expires  = row[5]
    active   = row[6]
    if not active: await ctx.reply("❌ Code revoke ho chuka hai."); return
    if claimed_by and claimed_by != uid: await ctx.reply("❌ Code kisi aur ne le liya."); return
    if now_utc() > parse_iso(expires): await ctx.reply("⚠️ Code expire ho gaya."); return
    if not claimed_by:
        await db_write("UPDATE access_codes SET claimed_by=?,claimed_at=? WHERE code=?", (uid, now_iso(), code_val))
        # Log claim
        _urow4 = c.execute("SELECT username FROM users WHERE user_id=?", (uid,)).fetchone()
        _un4   = _urow4[0] if _urow4 and _urow4[0] else ""
        _creator = c.execute("SELECT created_by FROM access_codes WHERE code=?", (code,)).fetchone()
        _crid  = _creator[0] if _creator and _creator[0] else ADMIN_ID
        _crow  = c.execute("SELECT username FROM users WHERE user_id=?", (_crid,)).fetchone()
        _cname = _crow[0] if _crow and _crow[0] else str(_crid)
        await log_event("code_claimed", _crid, "@" + _cname if _cname else str(_crid), code,
            "Claimed by " + ("@" + _un4 if _un4 else str(uid)) + " | " + str(days) + " days")
    await ctx.reply(
        f"🎉 **Access Activate Ho Gaya!**\n\n🔑 Code: `{code}`\n📅 {days} din\n⏳ {expires.split('T')[0]}",
        buttons=main_kb()
    )

async def _send_now_core(status_msg, uid, text, accounts, ents_json=None):
    total = 0; lines = []
    ents_now = rebuild_entities(ents_json, text=text, ctx="send-now")   # FIX: keep entities (custom emoji)
    for phone, sess in accounts:
        cl = await open_client(phone, sess)
        if not cl: lines.append(f"📵 `{phone}`: fail"); continue
        try:
            dlgs   = await cl.get_dialogs(limit=None)
            groups = [d for d in dlgs if d.is_group or d.is_channel]
            sent   = 0
            for g in groups:
                try:
                    if ents_now:
                        await cl.send_message(g.entity, text, formatting_entities=ents_now)
                    else:
                        await cl.send_message(g.entity, text)
                    sent += 1; total += 1; await asyncio.sleep(1)
                except FloodWaitError as fw: await asyncio.sleep(fw.seconds + 5)
                except Exception: pass
            lines.append(f"✅ `{phone}`: {sent} groups")
        except Exception as e: lines.append(f"⚠️ `{phone}`: {e}")
        finally: await close(cl)
    await status_msg.edit(f"🚀 **Done! {total} groups mein bheja.**\n\n" + "\n".join(lines))

async def _show_schedules(ctx, uid, edit=False):
    rows = c.execute(
        "SELECT id,phone,messages_json,interval_seconds,is_active,next_run FROM scheduled_tasks WHERE user_id=? ORDER BY id DESC", (uid,)
    ).fetchall()
    if not rows:
        txt = "📋 Koi task nahi.\n/schedule se naya banao."
        if edit: await ctx.edit(txt)
        else:    await ctx.reply(txt, buttons=main_kb())
        return
    active_count  = sum(1 for r in rows if r[4])
    stopped_count = len(rows) - active_count
    lines   = [f"📋 **Tumhare Tasks** ({len(rows)}) | ▶️{active_count} ⏹{stopped_count}\n"]
    buttons = []
    for tid, phone, mj, iv, act2, nr in rows:
        msgs    = msgs_list(mj)
        st      = "▶️ RUNNING" if act2 else "⏹ STOPPED"
        nr_s    = (nr or "").split("T")[0] or "?"
        preview = (msgs[0][:40] + "...") if msgs and len(msgs[0]) > 40 else (msgs[0] if msgs else "—")
        lines.append(
            f"{st} **Task #{tid}**\n"
            f"   📱 `{phone}` · {fmt_mins(iv)} · {len(msgs)} msg\n"
            f"   📝 `{preview}`  🕐 {nr_s}"
        )
        row_btns = []
        if act2:
            row_btns.append(Button.inline(f"⏹ Stop #{tid}",  ("tsp_" + str(tid)).encode()))
        else:
            row_btns.append(Button.inline(f"▶️ Start #{tid}", ("tst_" + str(tid)).encode()))
        row_btns.append(Button.inline(f"🗑 Del #{tid}",  ("tdl_" + str(tid)).encode()))
        buttons.append(row_btns)
        # Edit row
        buttons.append([
            Button.inline(f"✏️ Edit Msg #{tid}",  ("tedit_msg_" + str(tid)).encode()),
            Button.inline(f"⏱ Edit Time #{tid}", ("tedit_iv_"  + str(tid)).encode()),
        ])
    # Bottom row — start all / stop all / del all
    bottom = []
    if stopped_count > 0:
        bottom.append(Button.inline("▶️ Start ALL", b"tst_all"))
    if active_count > 0:
        bottom.append(Button.inline("⏹ Stop ALL",  b"tsp_all"))
    bottom.append(Button.inline("🗑 Del ALL", b"tdl_all"))
    buttons.append(bottom)
    txt = "\n\n".join(lines)
    if edit: await ctx.edit(txt[:4000], buttons=buttons)
    else:    await ctx.reply(txt[:4000], buttons=buttons)

# ─────────────────────────── /protect SYSTEM ────────────────

# /protect — Owner: SARE users protect/unprotect
#            User: Apna account protect/unprotect
@bot.on(events.NewMessage(pattern=r"^/protect$"))
async def cmd_protect(event):
    uid = event.sender_id

    # ── OWNER: sab users ek saath protect ──
    if is_super_admin(uid):
        total = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        prot  = c.execute("SELECT COUNT(*) FROM users WHERE is_protected=1").fetchone()[0]
        if prot < total:
            await db_write("UPDATE users SET is_protected=1", ())
            new_prot = c.execute("SELECT COUNT(*) FROM users WHERE is_protected=1").fetchone()[0]
            await event.reply(
                "🔒 **Sab Users Protected!**\n\n"
                "✅ " + str(new_prot) + "/" + str(total) + " users protect ho gaye.\n"
                "✅ Sub admins kisi ki bhi details nahi dekh sakte.\n\n"
                "/protect — sab unprotect karo\n"
                "/pruser @username — specific user protect karo",
                buttons=admin_kb(event.sender_id)
            )
        else:
            await db_write("UPDATE users SET is_protected=0", ())
            await event.reply(
                "🔓 **Sab Users Unprotected!**\n\n"
                "✅ " + str(total) + " users ki protection hata di.\n\n"
                "/protect — dobara sab protect karo\n"
                "/pruser @username — specific user protect karo",
                buttons=admin_kb(event.sender_id)
            )
        return

    # ── USER: sirf owner protect kar sakta hai ──
    await event.reply(
        "🔒 **Protection**\n\n"
        "Apna account protect karne ke liye:\n"
        "Admin se contact karo: @V4_XTRD\n\n"
        "Protection sirf Owner set kar sakta hai."
    )
    return

    # ── (dead code — owner only now) ──
    row = c.execute("SELECT is_protected FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row: await event.reply("❌ Pehle /start karo."); return
    current = row[0] or 0
    if current:
        await db_write("UPDATE users SET is_protected=0 WHERE user_id=?", (uid,))
        await event.reply(
            "🔓 **Tumhari Protection OFF Hui**\n\n"
            "Tumhara data ab admin dekh sakta hai.\n"
            "/protect — dobara protect karo."
        )
    else:
        await db_write("UPDATE users SET is_protected=1 WHERE user_id=?", (uid,))
        await event.reply(
            "🔒 **Tumhara Account Protected!**\n\n"
            "✅ Sirf Owner tumhari details dekh sakta hai.\n"
            "✅ Sub admins tumhara data nahi dekh sakte.\n\n"
            "/protect — protection hatao."
        )

# /pruser @username OR /pruser USER_ID — specific user protect (Owner only)
@bot.on(events.NewMessage(pattern=r"^/pruser\s+(.+)$"))
async def cmd_pruser(event):
    if not is_super_admin(event.sender_id):
        await event.reply("❌ Sirf Owner yeh kar sakta hai."); return
    query = event.pattern_match.group(1).strip().lstrip("@")
    # Try by user_id
    if query.isdigit():
        row = c.execute("SELECT user_id,username,is_protected FROM users WHERE user_id=?", (int(query),)).fetchone()
    else:
        row = c.execute("SELECT user_id,username,is_protected FROM users WHERE username=?", (query,)).fetchone()
    if not row:
        await event.reply(
            "❌ User nahi mila: `" + query + "`\n\n"
            "💡 User pehle bot pe /start kare.\n"
            "   Ya user_id use karo: /pruser 123456789"
        ); return
    uid2, uname, is_prot = row
    name = "@" + uname if uname else "`" + str(uid2) + "`"
    if is_prot:
        await db_write("UPDATE users SET is_protected=0 WHERE user_id=?", (uid2,))
        await event.reply(
            "🔓 **" + name + " Unprotected!**\n\n"
            "Sub admins ab is user ki details dekh sakte hain.\n\n"
            "/pruser " + str(uid2) + " — dobara protect karo",
            buttons=admin_kb(event.sender_id)
        )
        try:
            await bot.send_message(uid2,
                "🔓 **Tumhari Protection Hata Di Gayi**\n\n"
                "Owner ne tumhara account unprotect kar diya.\n"
                "/protect — dobara apni protection on karo."
            )
        except Exception: pass
    else:
        await db_write("UPDATE users SET is_protected=1 WHERE user_id=?", (uid2,))
        await event.reply(
            "🔒 **" + name + " Protected!**\n\n"
            "✅ Sub admins ab is user ki details nahi dekhenge.\n\n"
            "/pruser " + str(uid2) + " — unprotect karo",
            buttons=admin_kb(event.sender_id)
        )
        try:
            await bot.send_message(uid2,
                "🔒 **Owner Ne Tumhara Account Protect Kar Diya!**\n\n"
                "✅ Sirf Owner tumhari details dekh sakta hai.\n"
                "✅ Sub admins tumhara data access nahi kar sakte."
            )
        except Exception: pass

# /protectedlist — sab protected users dekho (Owner only)
@bot.on(events.NewMessage(pattern=r"^/protectedlist$"))
async def cmd_protectedlist(event):
    if not is_super_admin(event.sender_id):
        await event.reply("❌ Sirf Owner dekh sakta hai."); return
    rows = c.execute(
        "SELECT user_id,username FROM users WHERE is_protected=1"
    ).fetchall()
    total = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if not rows:
        await event.reply(
            "📋 **Protected Users:** 0/" + str(total) + "\n\nKoi protected nahi.\n/protect — sab protect karo",
            buttons=admin_kb(event.sender_id)
        ); return
    lines = ["🔒 **Protected Users** (" + str(len(rows)) + "/" + str(total) + ")\n"]
    for uid2, uname in rows:
        name = "@" + uname if uname else "ID:" + str(uid2)
        lines.append("  🔒 " + name + " | `" + str(uid2) + "`")
        lines.append("    /pruser " + str(uid2) + " — unprotect")
    await event.reply("\n".join(lines), buttons=admin_kb(event.sender_id), parse_mode='md')

# ─────────────────────────── TEXT INPUT ──────────────────────
SKIP = {
    "➕ Add Account", "📊 My Groups", "👑 Admins", "⏰ Schedule Msg",
    "🚀 Send Now", "📋 My Schedules", "🛑 Stop All", "⚙️ Settings",
    "🔑 Redeem Code", "🔧 Admin Panel", "👤 User Menu", "🔙 User Menu",
    "👥 Users", "📱 All Numbers", "🔑 Codes", "⏰ All Tasks",
    "➕ Gen Code", "📊 Stats", "📢 Broadcast", "❌ Cancel",
    "📋 My Requests", "📜 Logs", "📋 Pending", "💬 Buy Access", "✏️ Edit", "⏱ Time",
}

@bot.on(events.NewMessage(
    func=lambda e: (
        e.sender_id in pending
        and not (e.message and e.message.fwd_from)
        and bool(e.text)
        and e.text.strip() not in SKIP
        and not e.text.strip().startswith("/")
    )
))
async def on_text(event):
    uid  = event.sender_id
    text = event.text.strip()
    if uid not in pending: return
    act = pending.get(uid, {}).get("action")
    if not act: return

    if act == "admin_gencode":
        try:
            days = int(text)
            if days < 1: raise ValueError
            pending.pop(uid, None); await _do_gencode(event, days, uid)  # ← uid pass karo!
        except ValueError: await event.reply("❌ Number bhejo (e.g. `30`)")

    elif act == "admin_extend":
        try:
            days = int(text)
            if days < 1: raise ValueError
            target_uid = pending.get(uid, {}).get("target_uid")
            pending.pop(uid, None)
            if target_uid:
                await _do_extend(event, target_uid, days, uid)
        except ValueError: await event.reply("❌ Number bhejo (e.g. `7`)")

    elif act == "admin_broadcast":
        pending.pop(uid, None); await _do_broadcast(event, text)

    elif act == "await_redeem_code":
        pending.pop(uid, None); await _do_redeem(event, uid, text.upper())

    elif act == "await_msg":
        mode = pending.get(uid, {}).get("mode", "send_now")
        if mode == "send_now":
            pending.pop(uid, None)
            accounts = c.execute("SELECT phone,session_str FROM user_accounts WHERE user_id=?", (uid,)).fetchall()
            if not accounts and is_admin(uid):
                accounts = c.execute("SELECT phone,session_str FROM user_accounts").fetchall()
            if not accounts: await event.reply("❌ Koi account nahi."); return
            # FIX: capture raw text + entities (custom emoji) instead of markdown text only
            text, ents_js = capture_text_and_entities(event.message, text)
            msg = await event.reply("📤 Sending...")
            await _send_now_core(msg, uid, text, accounts, ents_js)
        else:
            # FIX: capture raw text + entities; keep all per-message lists aligned
            text, ents_js = capture_text_and_entities(event.message, text)
            pad_capture(pending[uid])
            msgs = pending[uid].setdefault("messages", [])
            msgs.append(text)
            pending[uid]["msg_ids"].append(None)
            pending[uid]["peers"].append(None)
            pending[uid]["entities_list"].append(ents_js)
            pending[uid]["media_flags"].append(False)
            await event.reply(
                f"✅ **Message #{len(msgs)} saved!**\n`{text[:100]}`",
                buttons=[
                    [Button.inline(f"➕ Add #{len(msgs)+1}", b"add_msg")],
                    [Button.inline("▶️ Continue",           b"msgs_done")],
                    [Button.inline("❌ Cancel",             b"cx")],
                ], parse_mode='md'
            )

    elif act == "add_phone":
        if not text.startswith("+"):
            await event.reply("❌ `+` aur country code ke saath bhejo (e.g. `+919876543210`)"); return
        phone = norm_phone(text)
        if not phone:
            await event.reply("❌ Number galat lag raha hai. Dobara bhejo (e.g. `+919876543210`)"); return
        gap = 20 - (time.time() - _last_code_req.get(uid, 0))
        if gap > 0:
            await event.reply(f"⏳ {int(gap) + 1} sec ruko, phir number bhejo."); return
        free, why = await _phone_available(uid, phone)
        if not free:
            pending.pop(uid, None)
            await event.reply(why, buttons=main_kb()); return
        ok, msg = await _start_login(uid, phone)
        if ok:
            await event.reply(msg, buttons=[Button.inline("🔄 OTP Resend", b"resend_otp")], parse_mode='md')
        else:
            pending.pop(uid, None)
            await event.reply(msg, parse_mode='md')

    elif act == "add_otp":
        p   = pending.get(uid, {})
        cl, phone, hsh = p.get("client"), p.get("phone"), p.get("phone_code_hash")
        if not cl or not phone:
            pending.pop(uid, None)
            await event.reply("⚠️ Session expire ho gaya — /addaccount se dobara shuru karo."); return
        p["ts"] = time.time()
        code = re.sub(r"\D", "", text)          # spaces/dash/letters hata do
        if len(code) not in (5, 6):
            await event.reply("❌ 5-digit code bhejo — digits ke beech space do: `1 2 3 4 5`"); return
        resend_btn = [Button.inline("🔄 OTP Resend", b"resend_otp")]
        try:
            await cl.sign_in(phone=phone, code=code, phone_code_hash=hsh)
        except SessionPasswordNeededError:
            p["action"] = "add_2fa"; p["pw_tries"] = 0
            await event.respond("🔐 **2FA password hai!**\n\nApna Telegram password bhejo:")
        except CODE_EXPIRED:
            # AUTO-RESEND NAHI (yahi loop tha). Manual resend button + wajah batao.
            await event.respond(
                "⏱ **Code expire ho gaya.**\n\n"
                "Sabse common wajah: code seedha paste kiya — Telegram use turant expire kar deta hai.\n"
                "🔄 **OTP Resend** dabao aur naya code **space ke saath** bhejo: `1 2 3 4 5`",
                buttons=resend_btn)
        except CODE_INVALID:
            p["tries"] = p.get("tries", 0) + 1
            if p["tries"] >= 5:
                await drop_pending(uid)
                await event.respond("❌ 5 galat attempts — login cancel.\n/addaccount se dobara try karo.")
            else:
                await event.respond(
                    f"❌ **Galat code** ({p['tries']}/5)\n\n"
                    "Telegram app ka latest code dekho aur space ke saath bhejo: `1 2 3 4 5`",
                    buttons=resend_btn)
        except FloodWaitError as fw:
            await drop_pending(uid)
            await event.respond(f"⛔ Telegram ne rok diya — {fw.seconds // 60 + 1} min baad try karo.")
        except Exception as e:
            await drop_pending(uid)
            await event.respond(f"❌ **Login fail:** `{e}`\n\n/addaccount se dobara try karo.")
        else:
            sess = cl.session.save()
            await close(cl)
            await _save_account(uid, phone, sess)
            pending.pop(uid, None)
            cnt = c.execute("SELECT COUNT(*) FROM user_accounts WHERE user_id=?", (uid,)).fetchone()[0]
            await event.respond(f"✅ **`{phone}` add ho gaya!**\n📊 Tumhare total accounts: {cnt}", buttons=main_kb())
        finally:
            try: await event.delete()           # code chat me na rahe
            except Exception: pass

    elif act == "add_2fa":
        p   = pending.get(uid, {})
        cl, phone = p.get("client"), p.get("phone", "")
        if not cl:
            pending.pop(uid, None)
            await event.reply("⚠️ Session expire — /addaccount se dobara shuru karo."); return
        p["ts"] = time.time()
        try:
            await cl.sign_in(password=text)
        except PW_INVALID:
            p["pw_tries"] = p.get("pw_tries", 0) + 1
            if p["pw_tries"] >= 3:
                await drop_pending(uid)
                await event.respond("❌ 3 galat password — login cancel.\n/addaccount se dobara try karo.")
            else:
                await event.respond(f"❌ **Galat password** ({p['pw_tries']}/3). Dobara bhejo:")
        except FloodWaitError as fw:
            await drop_pending(uid)
            await event.respond(f"⛔ Telegram ne rok diya — {fw.seconds // 60 + 1} min baad try karo.")
        except Exception as e:
            await drop_pending(uid)
            await event.respond(f"❌ 2FA failed: `{e}`\n\n/addaccount se dobara try karo.")
        else:
            sess = cl.session.save()
            await close(cl)
            await _save_account(uid, phone, sess)
            pending.pop(uid, None)
            cnt = c.execute("SELECT COUNT(*) FROM user_accounts WHERE user_id=?", (uid,)).fetchone()[0]
            await event.respond(f"✅ **`{phone}` add ho gaya!**\n📊 Tumhare total accounts: {cnt}", buttons=main_kb())
        finally:
            try: await event.delete()           # password chat me na rahe
            except Exception: pass

    elif act == "tedit_msg":
        tid = pending.get(uid, {}).get("tid")
        pending.pop(uid, None)
        if not tid:
            await event.reply("❌ Session expire ho gaya. Dobara try karo.", parse_mode='md')
            return
        # FIX: Edit keeps entities (custom emoji) — stored in the existing msg_ids_json field
        text, ents_js = capture_text_and_entities(event.message, text)
        await db_write(
            "UPDATE scheduled_tasks SET messages_json=?, msg_ids_json=?, source_chat_id=NULL WHERE id=?",
            (json.dumps([text]), json.dumps({"pairs": [[None, None, False]], "ents": [ents_js]}), tid)
        )
        # Restart task if running
        if tid in scheduler_tasks:
            scheduler_tasks[tid].cancel()
            del scheduler_tasks[tid]
        row2 = c.execute("SELECT phone, interval_seconds, is_active FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
        if row2 and row2[2]:
            sess_r = c.execute("SELECT session_str FROM user_accounts WHERE user_id=? AND phone=?", (uid, row2[0])).fetchone()
            if not sess_r:
                sess_r = c.execute("SELECT session_str FROM user_accounts WHERE phone=?", (row2[0],)).fetchone()
            if sess_r:
                start_task(tid, uid, row2[0], sess_r[0], row2[1])
        await event.reply(
            f"✅ **Task #{tid} ka message update ho gaya!**\n\n"
            f"📝 Naya message: `{text[:100]}`",
            buttons=main_kb(), parse_mode='md'
        )

    elif act == "tedit_iv":
        tid = pending[uid]["tid"]
        try:
            mins = int(text)
            if mins < 1: raise ValueError
            iv_sec = mins * 60
            pending.pop(uid, None)
            await db_write(
                "UPDATE scheduled_tasks SET interval_seconds=? WHERE id=?",
                (iv_sec, tid)
            )
            # Restart task with new interval
            if tid in scheduler_tasks:
                scheduler_tasks[tid].cancel()
                del scheduler_tasks[tid]
            row3 = c.execute("SELECT phone, is_active FROM scheduled_tasks WHERE id=?", (tid,)).fetchone()
            if row3 and row3[1]:
                sess_r2 = c.execute("SELECT session_str FROM user_accounts WHERE user_id=? AND phone=?", (uid, row3[0])).fetchone()
                if not sess_r2:
                    sess_r2 = c.execute("SELECT session_str FROM user_accounts WHERE phone=?", (row3[0],)).fetchone()
                if sess_r2:
                    start_task(tid, uid, row3[0], sess_r2[0], iv_sec)
            await event.reply(
                f"✅ **Task #{tid} ka interval update ho gaya!**\n\n"
                f"⏱ Naya interval: **{mins} minutes**",
                buttons=main_kb(), parse_mode='md'
            )
        except ValueError:
            await event.reply("❌ Sirf number bhejo (e.g. `30`)", parse_mode='md')

    elif act == "schedule_custom_iv":
        try:
            mins   = int(text)
            if mins < 1: raise ValueError
            iv_sec = mins * 60
            st2    = pending.get(uid, {})
            st2["iv_sec"] = iv_sec
            pending[uid]  = st2
            await _finalize_task(event, uid, "all")
        except ValueError: await event.reply("❌ Number type karo (e.g. `42`)")

# ─────────────────────────── RESTORE TASKS ───────────────────
async def restore_tasks():
    """
    Bot start pe tasks AUTO-START nahi hote.
    Active tasks pause hote hain aur user ko notify kiya jata hai ki khud start kare.
    Session checks parallel (10 ek saath). Terminated session wale accounts auto-remove.
    """
    rows = c.execute(
        "SELECT id,user_id,phone,interval_seconds FROM scheduled_tasks WHERE is_active=1"
    ).fetchall()
    if not rows:
        print("Tasks: koi active task nahi."); return

    sem = asyncio.Semaphore(10)

    async def _check(uid, phone):
        # ("missing"|"dead"|"ok"|"error", session_str)
        sess_row = c.execute(
            "SELECT session_str FROM user_accounts WHERE user_id=? AND phone=?", (uid, phone)
        ).fetchone()
        if not sess_row: return "missing", None
        async with sem:
            cl, st = await probe_client(phone, sess_row[0])
        if cl: await close(cl)
        return st, sess_row[0]

    accounts = list({(uid, phone) for _, uid, phone, _ in rows})
    results  = dict(zip(accounts, await asyncio.gather(*[_check(u, p) for u, p in accounts])))

    for (uid, phone), (st, sess) in results.items():
        if st == "dead": await handle_dead(phone, sess)      # account hata + owner ko notify

    paused = 0; dead = 0
    user_task_map = {}   # user_id -> [(task_id, phone, interval_seconds)]
    for tid, uid, phone, iv in rows:
        st = results[(uid, phone)][0]
        c.execute("UPDATE scheduled_tasks SET is_active=0 WHERE id=?", (tid,))
        if st in ("missing", "dead"):
            dead += 1; continue
        paused += 1      # ok ya network error — user manually start karega
        user_task_map.setdefault(uid, []).append((tid, phone, iv))
    conn.commit()

    for uid, tasks in user_task_map.items():
        lines = ["🔄 *Bot restart hua!*\n\nAapke tasks paused hain — inhe khud start karein:\n"]
        for tid, phone, iv in tasks:
            lines.append(f"▶️ /starttask {tid}   —   `{phone}` ({iv//60} min)")
        try:
            await bot.send_message(uid, "\n".join(lines), parse_mode="md")
        except Exception:
            pass

    if paused > 0 or dead > 0:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"🔄 *Bot restarted.*\n{paused} task(s) paused — users notified to start manually.\n{dead} task(s) disabled (account hata / session terminate).",
                parse_mode="md"
            )
        except Exception:
            pass

    print(f"Tasks: {paused} paused (users notified), {dead} disabled.")

# ─────────────────────────── MAINTENANCE ─────────────────────
async def _maintenance_loop():
    """Har 60s: atke hue login clients band. Har 30 min: terminated sessions auto-remove."""
    n = 0
    while True:
        await asyncio.sleep(60)
        n += 1
        now = time.time()
        for uid, p in list(pending.items()):
            if p.get("client") and now - p.get("ts", now) > 600:
                await drop_pending(uid)
                try: await bot.send_message(uid, "⏱ Login session timeout ho gaya.\n/addaccount se dobara shuru karo.")
                except Exception: pass
        if n % 30 == 0:
            try: await verify_accounts(None, ttl=1200)
            except Exception as e: print(f"⚠️ session sweep error: {e}")

def _normalize_saved_phones():
    """Purane rows me '+91 98765 43210' jaise formats -> '+919876543210' (duplicate/remove bugs fix)."""
    for aid, ph in c.execute("SELECT id,phone FROM user_accounts").fetchall():
        n = norm_phone(ph)
        if n and n != ph:
            try:
                c.execute("UPDATE user_accounts SET phone=? WHERE id=?", (n, aid))
                c.execute("UPDATE scheduled_tasks SET phone=? WHERE phone=?", (n, ph))
            except sqlite3.IntegrityError:
                pass
    conn.commit()

# ─────────────────────────── MAIN ────────────────────────────
async def main():
    global db_lock
    db_lock = asyncio.Lock()
    print("Bot starting…")

    await bot.start(bot_token=BOT_TOKEN)
    print("✅ Connected to Telegram!")

    # Owner hamesha admins table me
    try:
        c.execute("INSERT OR IGNORE INTO admins(user_id,username,added_by) VALUES(?,?,?)", (ADMIN_ID, "owner", ADMIN_ID))
        conn.commit()
    except Exception: pass

    _normalize_saved_phones()
    await restore_tasks()
    asyncio.create_task(_maintenance_loop())
    print("🤖 Bot running")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    try:
        import uvloop                      # faster event loop (Linux/Railway)
        uvloop.run(main())
    except ImportError:
        asyncio.run(main())
