import os
import sys
import re
import time
import html
import asyncio
import sqlite3
import logging
import contextvars
import tempfile
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Callable, Awaitable, List, Tuple

import aiohttp
import psutil
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile, BufferedInputFile
)
from aiogram.exceptions import TelegramBadRequest

# ==============================================================================
# ⚙️ CONFIGURATION
# ==============================================================================
MAIN_BOT_TOKEN = os.getenv("HOST_BOT_TOKEN", "9004889446:AAFGWsWlxJBdYHQWgINIbVf44hkXTZx65rA")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8680144387"))

DB_PATH = os.path.abspath("hosting.db")
HOST_DIR = os.path.abspath("hosted_files")

BRAND = "PRIME HOSTING"
VERSION = "9.0"
FREE_BOT_LIMIT = 2
PREMIUM_BOT_LIMIT = 10
FREE_LOG_LINES = 30
PREMIUM_LOG_LINES = 100
MAX_CRASH_RESTARTS = 5
MAX_UPLOAD_MB = 20
BANNED_TEXT = "🚫 <b>Aap is service se banned hain.</b>"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("prime_hosting")

bot = Bot(token=MAIN_BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


# ==============================================================================
# 🗄️ DATABASE
# ==============================================================================
def db_query(query: str, params: tuple = (), fetchone: bool = False, fetchall: bool = False) -> Any:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        c = conn.cursor()
        c.execute(query, params)
        res = c.fetchone() if fetchone else (c.fetchall() if fetchall else None)
        conn.commit()
        return res
    except Exception as e:
        logger.error("DB Error: %s", e)
        return None
    finally:
        conn.close()


def db_insert(query: str, params: tuple = ()) -> Optional[int]:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        c = conn.cursor()
        c.execute(query, params)
        conn.commit()
        return c.lastrowid
    except Exception as e:
        logger.error("DB Insert Error: %s", e)
        return None
    finally:
        conn.close()


def _columns(table: str) -> set:
    rows = db_query(f"PRAGMA table_info({table})", fetchall=True) or []
    return {r[1] for r in rows}


_SETTINGS: Optional[Dict[str, str]] = None   # in-memory copy, refreshed after every write


def _load_settings() -> None:
    global _SETTINGS
    rows = db_query("SELECT key, value FROM settings", fetchall=True)
    _SETTINGS = {k: v for k, v in rows} if rows else {}


def get_setting(key: str, default: str = "") -> str:
    if _SETTINGS is None:
        _load_settings()
    v = (_SETTINGS or {}).get(key)
    return v if v not in (None, "") else default


def set_setting(key: str, value: Any) -> None:
    global _SETTINGS
    db_query("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    _SETTINGS = None


def del_setting(key: str) -> None:
    global _SETTINGS
    db_query("DELETE FROM settings WHERE key=?", (key,))
    _SETTINGS = None


def del_settings_like(pattern: str) -> None:
    global _SETTINGS
    db_query("DELETE FROM settings WHERE key LIKE ?", (pattern,))
    _SETTINGS = None


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, is_premium INTEGER DEFAULT 0, is_banned INTEGER DEFAULT 0)")
    c.execute("""CREATE TABLE IF NOT EXISTS hosted_bots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    bot_name TEXT,
                    file_path TEXT,
                    status TEXT DEFAULT 'Stopped',
                    auto_restart INTEGER DEFAULT 0)""")
    conn.commit()
    conn.close()

    # Purane database ke liye migration (naye columns add karna)
    user_cols = _columns("users")
    migrations = {
        "first_name": "TEXT DEFAULT ''",
        "username": "TEXT DEFAULT ''",
        "premium_until": "TEXT",
        "joined_at": "TEXT DEFAULT ''",
    }
    for col, ddl in migrations.items():
        if col not in user_cols:
            db_query(f"ALTER TABLE users ADD COLUMN {col} {ddl}")

    if "created_at" not in _columns("hosted_bots"):
        db_query("ALTER TABLE hosted_bots ADD COLUMN created_at TEXT DEFAULT ''")

    # Admin settings (maintenance, limits, force-join, emojis, UI texts, YouTube links ...)
    db_query("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    global _SETTINGS
    _SETTINGS = None


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


USER_KEYS = ("user_id", "is_premium", "premium_until", "is_banned", "first_name", "username", "joined_at")
BOT_KEYS = ("id", "user_id", "bot_name", "file_path", "status", "auto_restart", "created_at")


def ensure_user(uid: int, first_name: str = "", username: str = ""):
    db_query("INSERT OR IGNORE INTO users (user_id, first_name, username, joined_at) VALUES (?,?,?,?)",
             (uid, first_name, username or "", now_str()))
    db_query("UPDATE users SET first_name=?, username=? WHERE user_id=?", (first_name, username or "", uid))


def is_premium_active(u: dict) -> bool:
    if not u or not u.get("is_premium"):
        return False
    until = u.get("premium_until")
    if not until:  # NULL = lifetime
        return True
    try:
        return datetime.fromisoformat(until) > datetime.now()
    except ValueError:
        return False


def get_user(uid: int) -> Optional[dict]:
    row = db_query(f"SELECT {', '.join(USER_KEYS)} FROM users WHERE user_id=?", (uid,), fetchone=True)
    if not row:
        return None
    u = dict(zip(USER_KEYS, row))
    u["premium"] = is_premium_active(u)
    u["banned"] = bool(u["is_banned"])
    return u


def set_premium(uid: int, days: int):
    until = None if days <= 0 else (datetime.now() + timedelta(days=days)).isoformat(timespec="seconds")
    db_query("UPDATE users SET is_premium=1, premium_until=? WHERE user_id=?", (until, uid))


def remove_premium(uid: int):
    db_query("UPDATE users SET is_premium=0, premium_until=NULL WHERE user_id=?", (uid,))


def set_ban(uid: int, banned: bool):
    db_query("UPDATE users SET is_banned=? WHERE user_id=?", (1 if banned else 0, uid))


def bot_limit(u: dict) -> int:
    key, default = ("premium_limit", PREMIUM_BOT_LIMIT) if u["premium"] else ("free_limit", FREE_BOT_LIMIT)
    try:
        return max(1, int(get_setting(key, str(default))))
    except ValueError:
        return default


def user_bot_count(uid: int) -> int:
    row = db_query("SELECT COUNT(*) FROM hosted_bots WHERE user_id=?", (uid,), fetchone=True)
    return row[0] if row else 0


def get_bot(bot_id: int) -> Optional[dict]:
    row = db_query(f"SELECT {', '.join(BOT_KEYS)} FROM hosted_bots WHERE id=?", (bot_id,), fetchone=True)
    return dict(zip(BOT_KEYS, row)) if row else None


def set_status(bot_id: int, status: str):
    db_query("UPDATE hosted_bots SET status=? WHERE id=?", (status, bot_id))


def can_access(uid: int, hb: Optional[dict]) -> bool:
    return bool(hb) and (hb["user_id"] == uid or uid == ADMIN_ID)



# ==============================================================================
# 🎨 PREMIUM / NORMAL EMOJI ENGINE + EDITABLE UI TEXTS
#   * Har emoji ka ek "slot" hota hai. Slot me premium ID ho to premium emoji dikhta hai,
#     ID na ho to normal emoji dikhta hai (kabhi blank nahi).
#   * Har message / caption / button ke plain emoji automatically premium ban jate hain.
#   * Admin -> Edit Emojis se koi bhi slot ka premium ID badal sakta hai.
#   * Admin -> Edit UI Texts se har screen ka text, YouTube link aur emoji alag-alag badal sakta hai.
# ==============================================================================
# (slot, normal emoji, premium emoji ID or "" = abhi premium ID nahi, normal emoji dikhega)
EMOJI_SLOTS = [
    ("brand", "🎬", "5994502837327892086"), ("profile", "👤", "5258011929993026890"),
    ("user_id", "🆔", "5474625972751837256"), ("name", "📛", "5215399540814781035"),
    ("gem", "💎", "5886505193180239900"), ("crown", "👑", "5886505193180239900"),
    ("free", "🆓", ""), ("slots", "📦", "6160968017304888311"),
    ("shield", "🛡", "6086672466132865380"), ("settings", "⚙️", ""),
    ("check", "✅", "6161241250239356403"), ("cross", "❌", "6037421444789440735"),
    ("warn", "⚠️", "6037421444789440735"), ("stats", "📊", "6161437856662298090"),
    ("server", "🖥", ""), ("ram", "💾", ""), ("disk", "💽", ""),
    ("bots", "🤖", "5312361253610475399"), ("back", "🔙", "5877536313623711363"),
    ("help", "❓", "5967280668885913944"), ("chat", "💬", "5967280668885913944"),
    ("logs", "📋", "6005986106703613755"), ("file", "📄", ""),
    ("download", "📥", ""), ("upload", "📤", ""), ("phone", "📱", ""),
    ("play", "▶️", ""), ("stop", "⏹", ""), ("restart", "🔁", ""),
    ("clean", "🧹", ""), ("delete", "🗑", ""), ("pip", "📦", ""),
    ("bolt", "⚡", ""), ("broadcast", "📢", "6005986106703613755"),
    ("users", "👥", "5904630315946611415"), ("search", "🔍", ""),
    ("point_down", "👇", "6161302621027049305"), ("calendar", "📅", "5433614043006903194"),
    ("clock", "⏳", ""), ("link", "🔗", "5875465628285931233"),
    ("video", "▶️", ""), ("channel", "📌", ""), ("backup", "🗂", ""),
    ("maint", "🔧", ""), ("ban", "⛔", ""), ("limit", "📏", ""),
    ("emoji", "🎨", ""), ("edit", "✏️", ""),
]
DEFAULT_EMOJIS: Dict[str, str] = {s: i for s, _f, i in EMOJI_SLOTS}
EMOJI_FALLBACK: Dict[str, str] = {s: f for s, f, _i in EMOJI_SLOTS}

# plain emoji (jo code / text me likhe hain) -> slot
_EMOJI_SLOT_MAP = {
    "🎬": "brand", "✨": "brand", "👤": "profile", "🆔": "user_id", "📛": "name",
    "💎": "gem", "👑": "crown", "🆓": "free", "📦": "slots", "🛡": "shield", "🔒": "shield",
    "⚙": "settings", "✅": "check", "🟢": "check", "🚫": "cross", "❌": "cross", "🔴": "cross",
    "🛑": "cross", "✖": "cross", "⚠": "warn", "😅": "warn", "📊": "stats", "📈": "stats",
    "🖥": "server", "💾": "ram", "💽": "disk", "🤖": "bots", "📱": "phone", "🔙": "back",
    "❓": "help", "💬": "chat", "📋": "logs", "📄": "file", "📥": "download", "📤": "upload",
    "▶": "play", "⏹": "stop", "🔁": "restart", "🔄": "restart", "🧹": "clean", "🗑": "delete",
    "⚡": "bolt", "📢": "broadcast", "👥": "users", "🔍": "search", "🔎": "search",
    "👇": "point_down", "📅": "calendar", "🗓": "calendar", "⏳": "clock", "⌛": "clock",
    "🔗": "link", "📌": "channel", "🗂": "backup", "🔧": "maint", "⛔": "ban", "📏": "limit",
    "🎨": "emoji", "✏": "edit",
}
_EMOJI_RE = re.compile(
    "(" + "|".join(sorted((re.escape(k) for k in _EMOJI_SLOT_MAP), key=len, reverse=True)) + ")\ufe0f?"
)
_TRAIL_EMOJI_RE = re.compile(
    "(" + "|".join(sorted((re.escape(k) for k in _EMOJI_SLOT_MAP), key=len, reverse=True)) + ")\ufe0f?\\s*$"
)
# <pre>/<code>/existing <tg-emoji>/tags ko haath nahi lagate (logs safe rehte hain)
_SPLIT_RE = re.compile(r"(<pre.*?</pre>|<code.*?</code>|<tg-emoji.*?</tg-emoji>|<[^>]+>)", re.S)

_CUR_UI = contextvars.ContextVar("cur_ui", default=None)   # (ui_key, text prefix) of the screen being sent
YT_RE = re.compile(r"^https?://(www\.|m\.)?(youtube\.com|youtu\.be)/\S+$", re.I)


def get_emoji_icon(slot: str, ui_key: Optional[str] = None) -> str:
    """Premium emoji ID: per-screen override -> admin global -> default. '' = normal emoji dikhao."""
    if get_setting("premium_emoji", "ON") == "OFF":
        return ""
    if ui_key:
        ov = get_setting(f"uiemoji_{ui_key}_{slot}", "")
        if ov.isdigit():
            return ov
    stored = get_setting(f"emoji_{slot}", "")
    if stored.isdigit():
        return stored
    return DEFAULT_EMOJIS.get(slot, "")


def get_emoji(slot: str, ui_key: Optional[str] = None) -> str:
    fb = EMOJI_FALLBACK.get(slot, "✨")
    icon = get_emoji_icon(slot, ui_key)
    return f'<tg-emoji emoji-id="{icon}">{fb}</tg-emoji>' if icon else fb


def premiumize_text(text: str) -> str:
    """Plain emoji -> premium <tg-emoji> (jis emoji ka premium ID nahi, wo normal hi rehta hai)."""
    if not text or not isinstance(text, str):
        return text
    parts = _SPLIT_RE.split(text)

    def _sub(m):
        icon = get_emoji_icon(_EMOJI_SLOT_MAP.get(m.group(1), ""))
        return f'<tg-emoji emoji-id="{icon}">{m.group(0)}</tg-emoji>' if icon else m.group(0)

    for i in range(0, len(parts), 2):
        parts[i] = _EMOJI_RE.sub(_sub, parts[i])
    return "".join(parts)


def premiumize_markup(markup):
    """Button ke shuru/aakhir ka emoji -> premium icon (ID na ho to button jaisa hai waisa rahega)."""
    if not isinstance(markup, InlineKeyboardMarkup):
        return markup
    changed = False
    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for b in row:
            try:
                if b.text and not getattr(b, "icon_custom_emoji_id", None):
                    t = b.text.strip()
                    m = _EMOJI_RE.match(t)
                    if m:
                        rest = t[m.end():].strip()
                    else:
                        m = _TRAIL_EMOJI_RE.search(t)
                        rest = t[:m.start()].strip() if m else ""
                    if m:
                        icon = get_emoji_icon(_EMOJI_SLOT_MAP.get(m.group(1), ""))
                        if icon:
                            b = b.model_copy(update={"text": rest or b.text, "icon_custom_emoji_id": icon})
                            changed = True
            except Exception:
                pass
            new_row.append(b)
        rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=rows) if changed else markup


def ui_video_button(key: str) -> Optional[InlineKeyboardButton]:
    link = get_setting(f"ui_yt_{key}", "")
    if not link:
        return None
    return InlineKeyboardButton(text="▶️ Watch Video", url=link)


def _attach_ui_video(method) -> None:
    """Jis screen ka YouTube link set hai, us message me 'Watch Video' button jod deta hai."""
    try:
        cur = _CUR_UI.get()
        if not cur:
            return
        key, prefix = cur
        if "reply_markup" not in getattr(type(method), "model_fields", {}):
            return
        text = getattr(method, "text", None) or getattr(method, "caption", None)
        if not isinstance(text, str) or prefix not in text:
            return
        _CUR_UI.set(None)
        btn_ = ui_video_button(key)
        if not btn_:
            return
        rm = getattr(method, "reply_markup", None)
        if rm is None:
            rows = []
        elif isinstance(rm, InlineKeyboardMarkup):
            rows = [list(r) for r in rm.inline_keyboard]
        else:
            return
        if any(getattr(b, "url", None) == btn_.url for r in rows for b in r):
            return
        rows.insert(0, [btn_])
        setattr(method, "reply_markup", InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception as e:
        logger.warning("UI video attach skipped: %s", e)


async def premium_emoji_request_middleware(make_request, bot_, method):
    """Bot ki har Telegram API call par chalta hai. Premium emoji reject ho to normal emoji se retry."""
    orig: Dict[str, Any] = {}
    try:
        _attach_ui_video(method)
        if get_setting("premium_emoji", "ON") != "OFF":
            fields = getattr(type(method), "model_fields", {})
            pm = getattr(method, "parse_mode", None)
            if "parse_mode" in fields and (pm is None or not isinstance(pm, str) or pm.upper() == "HTML"):
                for fld in ("text", "caption"):
                    val = getattr(method, fld, None)
                    if isinstance(val, str) and val:
                        new = premiumize_text(val)
                        if new != val:
                            orig[fld] = val
                            setattr(method, fld, new)
            rm = getattr(method, "reply_markup", None)
            if rm is not None:
                new_rm = premiumize_markup(rm)
                if new_rm is not rm:
                    orig["reply_markup"] = rm
                    setattr(method, "reply_markup", new_rm)
    except Exception as e:
        logger.warning("Premium emoji middleware skipped: %s", e)
    try:
        return await make_request(bot_, method)
    except TelegramBadRequest as e:
        if orig and "not modified" not in str(e).lower():
            for k, v in orig.items():
                setattr(method, k, v)
            logger.warning("Premium emoji rejected (%s) -> normal emoji se dobara bhej raha hu", e)
            return await make_request(bot_, method)
        raise


try:
    bot.session.middleware(premium_emoji_request_middleware)
except Exception as _e:
    logger.warning("Auto premium emoji enable nahi hua: %s", _e)

# ---------------------------------------------------------------- editable UI texts
DEFAULT_UI: Dict[str, str] = {
    "home": (
        "{brand} <b>{v_brand} SERVER v{v_version}</b> {brand}\n"
        "{check} Welcome, <b>{v_name}</b>!\n\n"
        "{profile} <b>Profile Card</b>\n"
        "┣ {user_id} User ID: <code>{v_id}</code>\n"
        "┣ {gem} Plan: {v_plan}\n"
        "┣ {slots} Slots: {v_slots}\n"
        "┗ {shield} Crash Guard: {check} Active\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "{point_down} <i>Ek option select karo:</i>"
    ),
    "profile": (
        "{profile} <b>My Profile</b>\n━━━━━━━━━━━━━━━━━━\n"
        "{name} Name: {v_name}\n"
        "{user_id} ID: <code>{v_id}</code>\n"
        "{gem} Plan: {v_plan}\n"
        "{calendar} Premium Till: {v_till}\n"
        "{slots} Slots: {v_slots}\n"
        "{calendar} Joined: {v_joined}\n"
        "━━━━━━━━━━━━━━━━━━"
    ),
    "status": (
        "{stats} <b>Server Status</b>\n━━━━━━━━━━━━━━━━━━\n"
        "{server} CPU: <b>{v_cpu}%</b>\n"
        "{ram} RAM: <b>{v_ram}%</b>\n"
        "{disk} Disk: <b>{v_disk}%</b>\n"
        "{bots} Active Bots: <b>{v_active}</b>\n"
        "━━━━━━━━━━━━━━━━━━"
    ),
    "help": (
        "{help} <b>Help Guide</b>\n━━━━━━━━━━━━━━━━━━\n"
        "{upload} 1. Apni <b>.py</b> file upload karo\n"
        "{play} 2. <b>Start</b> button dabao\n"
        "{logs} 3. Crash ho to <b>Live Logs</b> dekho\n"
        "{pip} 4. Missing module ke liye <b>Pip Install</b>\n"
        "{bolt} 5. <b>Auto-Restart</b> {gem} premium feature hai\n\n"
        "{chat} Commands: /start /help /cancel"
    ),
    "upload_prompt": (
        "{upload} <b>Apni Python (.py) file bhejo</b>\n\n"
        "{slots} Slots: {v_slots}\n"
        "{limit} Max size: {v_max} MB"
    ),
    "maintenance": (
        "{maint} <b>Maintenance Mode</b>\n\n"
        "Service abhi update ho rahi hai. Thodi der baad dobara try karo."
    ),
    "join_required": (
        "{channel} <b>Pehle hamara channel join karo</b>\n\n"
        "Bot use karne ke liye channel join karna zaroori hai.\n\n"
        "{point_down} <b>Join Channel</b> dabao, phir <b>I Joined - Verify</b>."
    ),
}

# key -> (label, placeholders help)
UI_EDITABLE: Dict[str, Tuple[str, str]] = {
    "home": ("Home / Start", "{v_brand} {v_version} {v_name} {v_id} {v_plan} {v_slots} + emoji slots"),
    "profile": ("My Profile", "{v_name} {v_id} {v_plan} {v_till} {v_slots} {v_joined} + emoji slots"),
    "status": ("Server Status", "{v_cpu} {v_ram} {v_disk} {v_active} + emoji slots"),
    "help": ("Help Guide", "emoji slots like {upload} {play} {logs}"),
    "upload_prompt": ("Upload Prompt", "{v_slots} {v_max} + emoji slots"),
    "maintenance": ("Maintenance Message", "emoji slots like {maint}"),
    "join_required": ("Join Channel Message", "{channel} {point_down} + emoji slots"),
}


def ui(key: str, **kwargs) -> str:
    """Admin ka edit kiya text (ui_<key>) -> warna default. {slot} = premium/normal emoji, {v_*} = live values."""
    template = get_setting(f"ui_{key}", "") or DEFAULT_UI.get(key, "")
    for slot in DEFAULT_EMOJIS:
        tag = "{" + slot + "}"
        if tag in template:
            template = template.replace(tag, get_emoji(slot, ui_key=key))
    for k, v in kwargs.items():
        template = template.replace("{" + k + "}", str(v))
    try:
        _CUR_UI.set((key, template[:40]))
    except Exception:
        pass
    return template


# ==============================================================================
# 🧠 STATES
# ==============================================================================
class HostingStates(StatesGroup):
    wait_file = State()
    wait_pip_package = State()


class AdminStates(StatesGroup):
    wait_input = State()
    wait_ui_text = State()
    wait_ui_video = State()
    wait_ui_emoji = State()
    wait_emoji_value = State()


# ==============================================================================
# 🤖 BOT MANAGER
# ==============================================================================
class BotInstance:
    def __init__(self, bot_id: int, user_id: int, file_path: str, auto_restart: bool):
        self.bot_id = bot_id
        self.user_id = user_id
        self.file_path = file_path
        self.auto_restart = auto_restart
        self.process: Optional[asyncio.subprocess.Process] = None
        self.logs: deque = deque(maxlen=300)
        self.is_running = False
        self.should_run = False
        self.crashes = 0
        self.started_at = 0.0

    def log(self, text: str):
        self.logs.append(text)

    async def clear_webhook(self):
        try:
            with open(self.file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            match = re.search(r"[0-9]{8,10}:[a-zA-Z0-9_-]{35}", content)
            if match:
                token = match.group(0)
                async with aiohttp.ClientSession() as session:
                    await session.get(f"https://api.telegram.org/bot{token}/deleteWebhook?drop_pending_updates=true")
                self.log("✅ Webhook cleared.")
        except Exception:
            pass

    async def start(self) -> bool:
        if self.is_running:
            return True
        self.should_run = True
        self.logs.clear()
        self.log("⏳ Booting process...")

        if not os.path.exists(self.file_path):
            self.log("❌ Error: File not found.")
            return False

        await self.clear_webhook()

        try:
            self.process = await asyncio.create_subprocess_exec(
                sys.executable, "-u", self.file_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=os.path.dirname(self.file_path),
            )
        except Exception as e:
            self.log(f"❌ System Error: {e}")
            self.is_running = False
            return False

        self.is_running = True
        self.started_at = time.time()
        set_status(self.bot_id, "Running")
        self.log("🚀 Bot is live.")
        asyncio.create_task(self._watch())
        return True

    async def _watch(self):
        proc = self.process
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="ignore").rstrip()
            if text:
                self.log(text)

        code = await proc.wait()
        self.is_running = False
        self.log(f"⚠️ Bot stopped (Code: {code})")
        set_status(self.bot_id, "Stopped")

        if not self.should_run or not self.auto_restart:
            return

        # Lamba chala aur crash hua to counter reset
        if time.time() - self.started_at > 300:
            self.crashes = 0
        self.crashes += 1

        if self.crashes <= MAX_CRASH_RESTARTS:
            self.log(f"🔄 Crash Guard: restart {self.crashes}/{MAX_CRASH_RESTARTS} in 5s...")
            await asyncio.sleep(5)
            if self.should_run:
                await self.start()
        else:
            self.should_run = False
            self.log("🛑 Crash Guard: bahut zyada crashes, bot band kar diya gaya.")

    async def stop(self):
        self.should_run = False
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=10)
            except asyncio.TimeoutError:
                self.process.kill()
        self.is_running = False
        set_status(self.bot_id, "Stopped")

    def kill_silently(self):
        """Shutdown ke time use hota hai: DB status change nahi karta taaki restart pe bot wapas chale."""
        self.should_run = False
        if self.process and self.process.returncode is None:
            self.process.terminate()


ACTIVE_BOTS: Dict[int, BotInstance] = {}


def get_instance(bot_id: int) -> Optional[BotInstance]:
    if bot_id in ACTIVE_BOTS:
        return ACTIVE_BOTS[bot_id]
    hb = get_bot(bot_id)
    if not hb:
        return None
    inst = BotInstance(bot_id, hb["user_id"], hb["file_path"], bool(hb["auto_restart"]))
    ACTIVE_BOTS[bot_id] = inst
    return inst


# ==============================================================================
# 🎨 UI KEYBOARDS & TEXT
# ==============================================================================
def btn(text: str, cb: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb)


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


def main_menu(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [btn("📤 Upload Bot (.py)", "upload")],
        [btn("📱 My Hosted Bots", "my_bots")],
        [btn("👤 My Profile", "profile"), btn("📊 Server Status", "status")],
        [btn("❓ Help", "help")],
    ]
    if uid == ADMIN_ID:
        rows.append([btn("👑 ADMIN PANEL", "admin_main")])
    return kb(*rows)


def bot_panel_kb(bot_id: int, auto_on: bool, viewer_premium: bool) -> InlineKeyboardMarkup:
    gem = "" if viewer_premium else " 💎"
    auto_label = f"⚡ Auto-Restart: {'🟢 ON' if auto_on else '🔴 OFF'}{gem}"
    dl_label = "📥 Download Logs 💎" if not viewer_premium else "📥 Download Logs"
    return kb(
        [btn("▶️ Start", f"start:{bot_id}"), btn("⏹ Stop", f"stop:{bot_id}"), btn("🔁 Restart", f"restart:{bot_id}")],
        [btn("📋 Live Logs", f"logs:{bot_id}"), btn("🧹 Clear Logs", f"clearlog:{bot_id}")],
        [btn("📦 Pip Install", f"pip:{bot_id}"), btn(auto_label, f"toggle:{bot_id}")],
        [btn(dl_label, f"dllog:{bot_id}"), btn("📄 Download File", f"dlfile:{bot_id}")],
        [btn("🗑 Delete Bot", f"del:{bot_id}")],
        [btn("🔙 Back", "my_bots")],
    )


def admin_main_menu() -> InlineKeyboardMarkup:
    maint = get_setting("maintenance", "OFF")
    pe = get_setting("premium_emoji", "ON")
    return kb(
        [btn("📢 Broadcast", "adm_in:broadcast"), btn("📢 Broadcast Premium", "adm_in:broadcast_prem")],
        [btn("📈 Stats", "adm_stats"), btn("👥 Users", "adm_users")],
        [btn("🤖 All Bots", "adm_bots"), btn("🔎 Open Bot by ID", "adm_in:open_bot")],
        [btn("💎 Give Premium", "adm_in:give_prem"), btn("🚫 Remove Premium", "adm_in:rm_prem")],
        [btn("💎 Premium List", "adm_prem_list"), btn("⛔ Banned List", "adm_ban_list")],
        [btn("⛔ Ban User", "adm_in:ban"), btn("✅ Unban User", "adm_in:unban")],
        [btn("🔍 User Info", "adm_in:info"), btn("🛑 Stop User Bots", "adm_in:stop_user")],
        [btn("✏️ Edit UI Texts", "adm_ui"), btn("🎨 Edit Emojis", "adm_emoji")],
        [btn("📏 Free Limit", "adm_in:limit_free"), btn("📏 Premium Limit", "adm_in:limit_prem")],
        [btn("📌 Force Join Channel", "adm_in:set_join"), btn("🗂 Backup DB", "adm_backup")],
        [btn(f"🔧 Maintenance: {maint}", "adm_maint")],
        [btn(f"✨ Premium Emoji: {pe}", "adm_toggle_pe")],
        [btn("🛑 Stop All Bots", "adm_stopall")],
        [btn("🔙 Back to Main", "back_main")],
    )


ADMIN_PROMPTS = {
    "broadcast": "📢 <b>Broadcast Message</b>\nJo bhi bhejna hai (text / photo / file) wo abhi send karo.",
    "give_prem": "👑 <b>Give Premium</b>\nFormat: <code>USER_ID DAYS</code>\n(DAYS = 0 matlab Lifetime ♾)",
    "rm_prem": "🚫 <b>Remove Premium</b>\nUser ID bhejo.",
    "ban": "⛔ <b>Ban User</b>\nUser ID bhejo.",
    "unban": "✅ <b>Unban User</b>\nUser ID bhejo.",
    "info": "🔍 <b>User Info</b>\nUser ID bhejo.",
}


ADMIN_PROMPTS.update({
    "broadcast_prem": "📢 <b>Broadcast (sirf Premium users)</b>\nJo bhejna hai (text / photo / file) wo abhi send karo.",
    "open_bot": "🔎 <b>Open Bot by ID</b>\nHosted Bot ka ID bhejo (kisi ka bhi bot control kar sakte ho).",
    "stop_user": "🛑 <b>Stop User Bots</b>\nUser ID bhejo — uske saare running bots band ho jayenge.",
    "limit_free": "📏 <b>Free Bot Limit</b>\nFree users ke liye naya max bots number bhejo (1-1000).",
    "limit_prem": "📏 <b>Premium Bot Limit</b>\nPremium users ke liye naya max bots number bhejo (1-1000).",
    "set_join": "📌 <b>Force Join Channel</b>\nChannel ka <code>@username</code> ya link bhejo.\n<code>off</code> bhejo to band ho jayega.\n⚠️ Bot ko us channel me <b>admin</b> banana zaroori hai.",
})


def admin_prompt_text(action: str) -> str:
    text = ADMIN_PROMPTS[action]
    if action == "set_join":
        text += f"\n\nCurrent: <b>{esc(get_setting('force_join', '') or 'OFF')}</b>"
    elif action == "limit_free":
        text += f"\nCurrent: <b>{bot_limit({'premium': False})}</b>"
    elif action == "limit_prem":
        text += f"\nCurrent: <b>{bot_limit({'premium': True})}</b>"
    return text


def premium_expiry_text(u: dict) -> str:
    if not u["is_premium"]:
        return "—"
    if not u["premium_until"]:
        return "♾ Lifetime"
    try:
        until = datetime.fromisoformat(u["premium_until"])
    except ValueError:
        return "—"
    if until > datetime.now():
        return f"⏳ {until.strftime('%d %b %Y, %H:%M')}"
    return "⌛ Expired"


def home_text(u: dict, name: str) -> str:
    plan = f"{get_emoji('crown')} PREMIUM" if u["premium"] else f"{get_emoji('free')} FREE"
    return ui("home", v_brand=BRAND, v_version=VERSION, v_name=esc(name), v_id=u["user_id"], v_plan=plan,
              v_slots=f"{user_bot_count(u['user_id'])}/{bot_limit(u)}")


def profile_text(u: dict, name: str) -> str:
    plan = f"{get_emoji('crown')} PREMIUM" if u["premium"] else f"{get_emoji('free')} FREE"
    return ui("profile", v_name=esc(name), v_id=u["user_id"], v_plan=plan, v_till=premium_expiry_text(u),
              v_slots=f"{user_bot_count(u['user_id'])}/{bot_limit(u)}", v_joined=u["joined_at"] or "—")


def server_stats_text() -> str:
    cpu = psutil.cpu_percent(interval=0.3)
    ram = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent
    active = sum(1 for b in ACTIVE_BOTS.values() if b.is_running)
    return ui("status", v_cpu=cpu, v_ram=ram, v_disk=disk, v_active=active)


def my_bots_view(uid: int):
    rows = db_query("SELECT id, bot_name FROM hosted_bots WHERE user_id=? ORDER BY id DESC", (uid,), fetchall=True) or []
    if not rows:
        return (
            "📭 <b>Abhi koi bot hosted nahi hai.</b>\n\n📤 Pehla bot upload karo!",
            kb([btn("📤 Upload Bot (.py)", "upload")], [btn("🔙 Back", "back_main")]),
        )
    buttons = []
    for bid, name in rows:
        inst = ACTIVE_BOTS.get(bid)
        icon = "🟢" if inst and inst.is_running else "🔴"
        buttons.append([btn(f"{icon} {name}", f"view:{bid}")])
    buttons.append([btn("🔙 Back", "back_main")])
    text = f"📱 <b>My Hosted Bots</b> ({len(rows)})\n━━━━━━━━━━━━━━━━━━\n🟢 Running  |  🔴 Stopped"
    return text, kb(*buttons)


def stats_text() -> str:
    users = db_query("SELECT user_id, is_premium, premium_until, is_banned FROM users", fetchall=True) or []
    total = len(users)
    premium = sum(1 for r in users if is_premium_active({"is_premium": r[1], "premium_until": r[2]}))
    banned = sum(1 for r in users if r[3] == 1)
    bots_total = (db_query("SELECT COUNT(*) FROM hosted_bots", fetchone=True) or (0,))[0]
    running = sum(1 for b in ACTIVE_BOTS.values() if b.is_running)
    return (
        f"📈 <b>Platform Stats</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"👥 Total Users: <b>{total}</b>\n"
        f"💎 Premium Users: <b>{premium}</b>\n"
        f"🚫 Banned Users: <b>{banned}</b>\n"
        f"🤖 Total Bots: <b>{bots_total}</b>\n"
        f"🟢 Running Bots: <b>{running}</b>\n"
        f"━━━━━━━━━━━━━━━━━━"
    )


async def safe_edit(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup):
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" in str(e).lower():
            return
        try:
            await call.message.delete()
        except Exception:
            pass
        await call.message.answer(text, reply_markup=markup)


# ==============================================================================
# 🛡️ MIDDLEWARE (Ban check on every message & button)
# ==============================================================================
class BanMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable[..., Awaitable[Any]], event: Any, data: Dict[str, Any]) -> Any:
        user = getattr(event, "from_user", None)
        if user and user.id != ADMIN_ID:
            row = get_user(user.id)
            if row and row["banned"]:
                if isinstance(event, CallbackQuery):
                    await event.answer("🚫 Aap banned hain.", show_alert=True)
                else:
                    await event.answer(BANNED_TEXT)
                return None
        return await handler(event, data)


dp.message.outer_middleware(BanMiddleware())
dp.callback_query.outer_middleware(BanMiddleware())


# ---------------------------- Maintenance mode + Force-join channel ----------------------------
_join_cache: Dict[int, float] = {}
_join_warned = False


def normalize_channel(text: str) -> Optional[str]:
    """'' = off, None = galat input, warna '@username'."""
    t = text.strip()
    if t.lower() in ("off", "none", "0", "remove"):
        return ""
    m = re.search(r"(?:t\.me/|@)?([A-Za-z0-9_]{5,32})/?$", t)
    return "@" + m.group(1) if m else None


async def is_channel_member(user_id: int, channel: str) -> bool:
    global _join_warned
    now = time.time()
    if _join_cache.get(user_id, 0) > now:
        return True
    try:
        cm = await bot.get_chat_member(channel, user_id)
        status = str(getattr(cm.status, "value", cm.status))
        ok = status in ("member", "administrator", "creator") or (status == "restricted" and bool(getattr(cm, "is_member", False)))
    except Exception as e:
        # Aksar: bot channel ka admin nahi hai. Sabko lock karne ke bajay allow karo + admin ko batao.
        if not _join_warned:
            _join_warned = True
            logger.error("Force-join check failed (%s). Bot ko %s me admin banao!", e, channel)
            try:
                await bot.send_message(ADMIN_ID, f"⚠️ Force-join check fail: <code>{esc(e)}</code>\nBot ko {esc(channel)} me <b>admin</b> banao.")
            except Exception:
                pass
        return True
    if ok:
        _join_cache[user_id] = now + 120
    return ok


def join_kb() -> InlineKeyboardMarkup:
    channel = get_setting("force_join", "")
    return kb(
        [InlineKeyboardButton(text="📌 Join Channel", url=f"https://t.me/{channel.lstrip('@')}")],
        [btn("✅ I Joined - Verify", "check_join")],
    )


class GateMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable[..., Awaitable[Any]], event: Any, data: Dict[str, Any]) -> Any:
        user = getattr(event, "from_user", None)
        if not user or user.is_bot or user.id == ADMIN_ID:
            return await handler(event, data)

        if get_setting("maintenance", "OFF") == "ON":
            if isinstance(event, CallbackQuery):
                await event.answer("🔧 Maintenance chal raha hai. Thodi der baad try karo.", show_alert=True)
            else:
                await event.answer(ui("maintenance"))
            return None

        channel = get_setting("force_join", "")
        if channel and not (isinstance(event, CallbackQuery) and event.data == "check_join"):
            if not await is_channel_member(user.id, channel):
                if isinstance(event, CallbackQuery):
                    try:
                        await event.answer("❌ Pehle channel join karo!", show_alert=True)
                    except Exception:
                        pass
                    try:
                        await event.message.answer(ui("join_required"), reply_markup=join_kb())
                    except Exception:
                        pass
                else:
                    await event.answer(ui("join_required"), reply_markup=join_kb())
                return None
        return await handler(event, data)


dp.message.outer_middleware(GateMiddleware())
dp.callback_query.outer_middleware(GateMiddleware())


# ==============================================================================
# 🏠 GENERAL HANDLERS
# ==============================================================================
@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    ensure_user(msg.from_user.id, msg.from_user.first_name or "", msg.from_user.username or "")
    u = get_user(msg.from_user.id)
    await msg.answer(home_text(u, msg.from_user.first_name or "User"), reply_markup=main_menu(msg.from_user.id))


@dp.message(Command("cancel"))
async def cmd_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("✖️ Cancelled.", reply_markup=main_menu(msg.from_user.id))


@dp.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer(help_text())


@dp.message(Command("admin"))
async def cmd_admin(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.answer("⚙️ <b>👑 SUPER ADMIN PANEL</b>", reply_markup=admin_main_menu())


@dp.callback_query(F.data == "back_main")
async def cb_back_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    ensure_user(call.from_user.id, call.from_user.first_name or "", call.from_user.username or "")
    u = get_user(call.from_user.id)
    await safe_edit(call, home_text(u, call.from_user.first_name or "User"), main_menu(call.from_user.id))


@dp.callback_query(F.data == "profile")
async def cb_profile(call: CallbackQuery):
    u = get_user(call.from_user.id)
    await safe_edit(call, profile_text(u, call.from_user.first_name or "User"),
                    kb([btn("🔙 Back", "back_main")]))


@dp.callback_query(F.data == "status")
async def cb_status(call: CallbackQuery):
    await safe_edit(call, server_stats_text(), kb([btn("🔙 Back", "back_main")]))


def help_text() -> str:
    return ui("help")


@dp.callback_query(F.data == "help")
async def cb_help(call: CallbackQuery):
    await safe_edit(call, help_text(), kb([btn("🔙 Back", "back_main")]))


# ==============================================================================
# 📤 UPLOAD & HOSTED BOT MANAGEMENT
# ==============================================================================
@dp.callback_query(F.data == "upload")
async def cb_upload(call: CallbackQuery, state: FSMContext):
    u = get_user(call.from_user.id)
    count = user_bot_count(call.from_user.id)
    limit = bot_limit(u)
    if count >= limit:
        return await call.answer(f"🚫 Limit reached ({count}/{limit}). Premium lo 💎", show_alert=True)
    await safe_edit(
        call,
        ui("upload_prompt", v_slots=f"{count}/{limit}", v_max=MAX_UPLOAD_MB),
        kb([btn("🔙 Cancel", "back_main")]),
    )
    await state.set_state(HostingStates.wait_file)


@dp.message(HostingStates.wait_file, F.document)
async def handle_upload(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    u = get_user(uid)
    doc = msg.document

    if not doc.file_name or not doc.file_name.lower().endswith(".py"):
        return await msg.answer("❌ Sirf <b>.py</b> files allowed hain.")
    if doc.file_size and doc.file_size > MAX_UPLOAD_MB * 1024 * 1024:
        return await msg.answer(f"❌ File {MAX_UPLOAD_MB} MB se badi hai.")

    count = user_bot_count(uid)
    if count >= bot_limit(u):
        await state.clear()
        return await msg.answer(f"🚫 Slot limit poori ho gayi ({count}/{bot_limit(u)}).")

    user_dir = os.path.join(HOST_DIR, str(uid))
    os.makedirs(user_dir, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", doc.file_name)
    file_path = os.path.join(user_dir, f"{int(time.time())}_{safe_name}")

    status = await msg.answer("⏳ File download ho rahi hai...")
    await bot.download(doc, destination=file_path)

    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
    if MAIN_BOT_TOKEN in content:
        os.remove(file_path)
        await state.clear()
        return await status.edit_text(
            "❌ <b>CRITICAL ERROR:</b>\nAapne Hosting Bot ka hi token file me daal diya hai!\n\n"
            "@BotFather se <b>NAYA BOT</b> banao aur uska token use karo."
        )

    auto = 1 if u["premium"] else 0
    bot_id = db_insert(
        "INSERT INTO hosted_bots (user_id, bot_name, file_path, auto_restart, created_at) VALUES (?,?,?,?,?)",
        (uid, doc.file_name, file_path, auto, now_str()),
    )
    await state.clear()
    if not bot_id:
        return await status.edit_text("❌ Database error. Dobara try karo.")

    hb = get_bot(bot_id)
    await status.edit_text(
        f"✅ <b>File Uploaded Successfully!</b>\n\n📄 File: <code>{esc(doc.file_name)}</code>\n"
        f"🆔 Bot ID: <code>{bot_id}</code>",
        reply_markup=bot_panel_kb(bot_id, bool(hb["auto_restart"]), u["premium"]),
    )


@dp.callback_query(F.data == "my_bots")
async def cb_my_bots(call: CallbackQuery):
    text, markup = my_bots_view(call.from_user.id)
    await safe_edit(call, text, markup)


async def fetch_owned(call: CallbackQuery, bot_id: int) -> Optional[dict]:
    hb = get_bot(bot_id)
    if not can_access(call.from_user.id, hb):
        await call.answer("Bot nahi mila ya access nahi hai.", show_alert=True)
        return None
    return hb


def build_panel(viewer_id: int, bot_id: int):
    hb = get_bot(bot_id)
    inst = get_instance(bot_id)
    viewer = get_user(viewer_id) or {"premium": False}
    status = "🟢 Running" if inst and inst.is_running else "🔴 Stopped"
    owner_note = ""
    if viewer_id == ADMIN_ID and hb["user_id"] != ADMIN_ID:
        owner_note = f"👤 Owner: <code>{hb['user_id']}</code>\n"
    text = (
        f"🤖 <b>Bot Control Panel</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"📄 Name: <code>{esc(hb['bot_name'])}</code>\n"
        f"🆔 Bot ID: <code>{bot_id}</code>\n"
        f"{owner_note}"
        f"📊 Status: {status}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    return text, bot_panel_kb(bot_id, inst.auto_restart if inst else bool(hb["auto_restart"]), viewer["premium"])


async def render_panel(call: CallbackQuery, bot_id: int):
    text, markup = build_panel(call.from_user.id, bot_id)
    await safe_edit(call, text, markup)


@dp.callback_query(F.data.startswith("view:"))
async def cb_view(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    if not await fetch_owned(call, bot_id):
        return
    await render_panel(call, bot_id)


@dp.callback_query(F.data.startswith("start:"))
async def cb_start(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    if not await fetch_owned(call, bot_id):
        return
    inst = get_instance(bot_id)
    if inst.is_running:
        return await call.answer("Bot pehle se running hai ✅", show_alert=True)

    await call.answer("🚀 Starting...")
    await inst.start()
    await asyncio.sleep(3)

    if not inst.is_running:
        logs = "\n".join(list(inst.logs)[-20:])[-2500:]
        await safe_edit(
            call,
            f"❌ <b>Bot Crash Ho Gaya!</b>\n\n<b>Error:</b>\n<pre>{esc(logs)}</pre>",
            kb([btn("📦 Pip Install", f"pip:{bot_id}")], [btn("🔙 Back", f"view:{bot_id}")]),
        )
    else:
        await render_panel(call, bot_id)


@dp.callback_query(F.data.startswith("stop:"))
async def cb_stop(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    if not await fetch_owned(call, bot_id):
        return
    inst = get_instance(bot_id)
    if inst.is_running:
        await inst.stop()
        await call.answer("⏹ Bot stopped.")
    else:
        await call.answer("Bot already stopped hai.")
    await render_panel(call, bot_id)


@dp.callback_query(F.data.startswith("restart:"))
async def cb_restart(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    if not await fetch_owned(call, bot_id):
        return
    inst = get_instance(bot_id)
    await call.answer("🔁 Restarting...")
    await inst.stop()
    await inst.start()
    await asyncio.sleep(2)
    await render_panel(call, bot_id)


@dp.callback_query(F.data.startswith("toggle:"))
async def cb_toggle(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    hb = await fetch_owned(call, bot_id)
    if not hb:
        return
    viewer = get_user(call.from_user.id)
    if not viewer["premium"]:
        return await call.answer("💎 Auto-Restart sirf Premium users ke liye hai!", show_alert=True)

    inst = get_instance(bot_id)
    inst.auto_restart = not inst.auto_restart
    db_query("UPDATE hosted_bots SET auto_restart=? WHERE id=?", (1 if inst.auto_restart else 0, bot_id))
    await call.answer(f"Auto-Restart {'ON ⚡' if inst.auto_restart else 'OFF'}")
    await render_panel(call, bot_id)


@dp.callback_query(F.data.startswith("logs:"))
async def cb_logs(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    if not await fetch_owned(call, bot_id):
        return
    viewer = get_user(call.from_user.id)
    inst = get_instance(bot_id)
    limit = PREMIUM_LOG_LINES if viewer["premium"] else FREE_LOG_LINES
    lines = list(inst.logs)[-limit:]
    log_text = "\n".join(lines) if lines else "Abhi koi log nahi hai."
    log_text = log_text[-3500:]
    await safe_edit(
        call,
        f"📋 <b>Live Logs</b> (last {limit})\n<pre>{esc(log_text)}</pre>",
        kb([btn("🔄 Refresh", f"logs:{bot_id}")], [btn("🔙 Back", f"view:{bot_id}")]),
    )


@dp.callback_query(F.data.startswith("clearlog:"))
async def cb_clearlog(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    if not await fetch_owned(call, bot_id):
        return
    inst = get_instance(bot_id)
    inst.logs.clear()
    inst.log("🧹 Logs cleared!")
    await call.answer("🧹 Logs Cleared!")
    await cb_logs(call)


@dp.callback_query(F.data.startswith("dllog:"))
async def cb_dllog(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    hb = await fetch_owned(call, bot_id)
    if not hb:
        return
    viewer = get_user(call.from_user.id)
    if not viewer["premium"]:
        return await call.answer("💎 Full log download sirf Premium users ke liye hai!", show_alert=True)
    inst = get_instance(bot_id)
    data = "\n".join(inst.logs).encode("utf-8") or b"No logs."
    await call.message.answer_document(
        BufferedInputFile(data, filename=f"{hb['bot_name']}_logs.txt"),
        caption=f"📥 Logs of <code>{esc(hb['bot_name'])}</code>",
    )
    await call.answer()


@dp.callback_query(F.data.startswith("dlfile:"))
async def cb_dlfile(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    hb = await fetch_owned(call, bot_id)
    if not hb:
        return
    if not os.path.exists(hb["file_path"]):
        return await call.answer("File server par nahi mili.", show_alert=True)
    await call.message.answer_document(
        FSInputFile(hb["file_path"], filename=hb["bot_name"]),
        caption=f"📄 <code>{esc(hb['bot_name'])}</code>",
    )
    await call.answer()


@dp.callback_query(F.data.startswith("pip:"))
async def cb_pip(call: CallbackQuery, state: FSMContext):
    bot_id = int(call.data.split(":")[1])
    if not await fetch_owned(call, bot_id):
        return
    await state.update_data(pip_bot_id=bot_id)
    await safe_edit(
        call,
        "📦 <b>Pip package ka naam bhejo:</b>\n(e.g. <code>pyrogram</code>, <code>requests==2.31.0</code>)",
        kb([btn("🔙 Cancel", f"view:{bot_id}")]),
    )
    await state.set_state(HostingStates.wait_pip_package)


PIP_RE = re.compile(r"^[A-Za-z0-9_.\-\[\]<>=!~, ]{1,120}$")


@dp.message(HostingStates.wait_pip_package)
async def execute_pip(msg: Message, state: FSMContext):
    data = await state.get_data()
    bot_id = data.get("pip_bot_id")
    pkg = (msg.text or "").strip()
    if not PIP_RE.match(pkg):
        return await msg.answer("❌ Galat package name. Sirf letters, numbers aur <code>=&lt;&gt;!~[]</code> allowed hain.")

    await state.clear()
    status = await msg.answer(f"⏳ Installing <code>{esc(pkg)}</code>...")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install", *pkg.split(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        proc.kill()
        return await status.edit_text("⌛ Timeout (5 min). Dobara try karo.")

    back = kb([btn("🔙 Back to Bot", f"view:{bot_id}")]) if bot_id else kb([btn("🔙 Back", "back_main")])
    if proc.returncode == 0:
        await status.edit_text(f"✅ <b>Package Installed!</b>\n<code>{esc(pkg)}</code> ready hai.", reply_markup=back)
    else:
        err = stderr.decode(errors="ignore")[-1000:]
        await status.edit_text(f"❌ <b>Install Failed:</b>\n<pre>{esc(err)}</pre>", reply_markup=back)


@dp.callback_query(F.data.startswith("del:"))
async def cb_delete(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    hb = await fetch_owned(call, bot_id)
    if not hb:
        return
    inst = ACTIVE_BOTS.pop(bot_id, None)
    if inst:
        await inst.stop()
    if os.path.exists(hb["file_path"]):
        os.remove(hb["file_path"])
    db_query("DELETE FROM hosted_bots WHERE id=?", (bot_id,))
    await call.answer("🗑 Bot Deleted!", show_alert=True)
    text, markup = my_bots_view(call.from_user.id)
    await safe_edit(call, text, markup)


# ==============================================================================
# 👑 ADMIN PANEL
# ==============================================================================
@dp.callback_query(F.data == "admin_main")
async def cb_admin_main(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("⛔ Access denied", show_alert=True)
    await state.clear()
    await safe_edit(call, "⚙️ <b>👑 SUPER ADMIN PANEL</b>", admin_main_menu())


@dp.callback_query(F.data == "adm_stats")
async def cb_adm_stats(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    await safe_edit(call, stats_text(), kb([btn("🔙 Back", "admin_main")]))


@dp.callback_query(F.data == "adm_users")
async def cb_adm_users(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    rows = db_query(
        "SELECT user_id, first_name, username, is_premium, premium_until, is_banned FROM users ORDER BY user_id DESC LIMIT 15",
        fetchall=True,
    ) or []
    lines = []
    for uid, name, uname, prem, until, banned in rows:
        p = "💎" if is_premium_active({"is_premium": prem, "premium_until": until}) else "·"
        b = "🚫" if banned else ""
        handle = f"@{uname}" if uname else (name or "—")
        lines.append(f"{p}{b} <code>{uid}</code> {esc(handle)}")
    body = "\n".join(lines) if lines else "Koi user nahi."
    await safe_edit(call, f"👥 <b>Latest Users</b>\n━━━━━━━━━━━━━━━━━━\n{body}\n\n💎 Premium  🚫 Banned",
                    kb([btn("🔙 Back", "admin_main")]))


@dp.callback_query(F.data == "adm_bots")
async def cb_adm_bots(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    rows = db_query("SELECT id, user_id, bot_name FROM hosted_bots ORDER BY id DESC LIMIT 20", fetchall=True) or []
    lines = []
    for bid, uid, name in rows:
        inst = ACTIVE_BOTS.get(bid)
        icon = "🟢" if inst and inst.is_running else "🔴"
        lines.append(f"{icon} <code>{bid}</code> {esc(name)} · <code>{uid}</code>")
    body = "\n".join(lines) if lines else "Koi bot nahi."
    await safe_edit(call, f"🤖 <b>All Hosted Bots (latest 20)</b>\n━━━━━━━━━━━━━━━━━━\n{body}",
                    kb([btn("🔙 Back", "admin_main")]))


@dp.callback_query(F.data == "adm_stopall")
async def cb_adm_stopall(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    count = 0
    for inst in list(ACTIVE_BOTS.values()):
        if inst.is_running:
            await inst.stop()
            count += 1
    await call.answer(f"🛑 {count} bots stopped.", show_alert=True)
    await safe_edit(call, "⚙️ <b>👑 SUPER ADMIN PANEL</b>", admin_main_menu())


@dp.callback_query(F.data.startswith("adm_in:"))
async def cb_adm_input(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    action = call.data.split(":", 1)[1]
    if action not in ADMIN_PROMPTS:
        return await call.answer("Unknown action")
    await state.set_state(AdminStates.wait_input)
    await state.update_data(action=action)
    await safe_edit(call, admin_prompt_text(action), kb([btn("🔙 Cancel", "admin_main")]))


def parse_int(s: str) -> Optional[int]:
    s = s.strip()
    return int(s) if re.fullmatch(r"-?\d+", s) else None


async def do_broadcast(msg: Message, only_premium: bool = False):
    rows = db_query("SELECT user_id, is_premium, premium_until FROM users WHERE is_banned=0", fetchall=True) or []
    if only_premium:
        rows = [r for r in rows if is_premium_active({"is_premium": r[1], "premium_until": r[2]})]
    users = [r[0] for r in rows]
    status = await msg.answer(f"⏳ Broadcasting to {len(users)} users...")
    sent = failed = 0
    for uid in users:
        try:
            await msg.copy_to(uid)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await status.edit_text(f"✅ <b>Broadcast Complete</b>\n📨 Sent: {sent}\n❌ Failed: {failed}")


@dp.message(AdminStates.wait_input)
async def admin_input_handler(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    action = (await state.get_data()).get("action")
    text = (msg.text or "").strip()

    if action == "broadcast":
        await state.clear()
        return await do_broadcast(msg)

    back = kb([btn("🔙 Admin Panel", "admin_main")])

    if action == "broadcast_prem":
        await state.clear()
        return await do_broadcast(msg, only_premium=True)

    if action == "open_bot":
        bid = parse_int(text)
        if bid is None or not get_bot(bid):
            return await msg.answer("❌ Is ID ka koi bot nahi mila. Sahi Bot ID bhejo.")
        await state.clear()
        panel_text, panel_markup = build_panel(ADMIN_ID, bid)
        return await msg.answer(panel_text, reply_markup=panel_markup)

    if action in ("limit_free", "limit_prem"):
        n = parse_int(text)
        if n is None or not 1 <= n <= 1000:
            return await msg.answer("❌ 1 se 1000 ke beech ka number bhejo.")
        set_setting("free_limit" if action == "limit_free" else "premium_limit", n)
        await state.clear()
        return await msg.answer(f"✅ {'Free' if action == 'limit_free' else 'Premium'} limit ab <b>{n}</b> bots hai.", reply_markup=back)

    if action == "stop_user":
        target = parse_int(text)
        if target is None:
            return await msg.answer("❌ Sahi numeric User ID bhejo.")
        await state.clear()
        n = await stop_user_bots(target)
        return await msg.answer(f"🛑 User <code>{target}</code> ke <b>{n}</b> bots band kiye.", reply_markup=back)

    if action == "set_join":
        channel = normalize_channel(text)
        if channel is None:
            return await msg.answer("❌ Sahi channel <code>@username</code> ya link bhejo (ya <code>off</code>).")
        await state.clear()
        if not channel:
            del_setting("force_join")
            return await msg.answer("✅ Force Join <b>OFF</b> kar diya.", reply_markup=back)
        note = ""
        try:
            me = await bot.me()
            cm = await bot.get_chat_member(channel, me.id)
            if str(getattr(cm.status, "value", cm.status)) not in ("administrator", "creator"):
                note = "\n⚠️ Bot channel me admin nahi hai — admin banao, warna verify kaam nahi karega."
        except Exception as e:
            note = f"\n⚠️ Channel check nahi hua ({esc(e)}). Bot ko channel me <b>admin</b> banao."
        set_setting("force_join", channel)
        return await msg.answer(f"✅ Force Join ON: <b>{esc(channel)}</b>{note}", reply_markup=back)

    if action == "give_prem":
        parts = text.split()
        if len(parts) != 2 or parse_int(parts[0]) is None or parse_int(parts[1]) is None:
            return await msg.answer("❌ Format: <code>USER_ID DAYS</code> (e.g. <code>123456 30</code>)")
        uid, days = int(parts[0]), int(parts[1])
        ensure_user(uid)
        set_premium(uid, days)
        await state.clear()
        period = "Lifetime ♾" if days <= 0 else f"{days} days"
        return await msg.answer(f"💎 User <code>{uid}</code> ab Premium hai ({period})!", reply_markup=back)

    uid = parse_int(text)
    if uid is None:
        return await msg.answer("❌ Sahi numeric User ID bhejo.")
    await state.clear()

    if action == "rm_prem":
        remove_premium(uid)
        await msg.answer(f"🚫 Premium removed for <code>{uid}</code>.", reply_markup=back)
    elif action == "ban":
        if uid == ADMIN_ID:
            return await msg.answer("😅 Admin ko ban nahi kar sakte.", reply_markup=back)
        ensure_user(uid)
        set_ban(uid, True)
        stopped = await stop_user_bots(uid)
        await msg.answer(f"⛔ User <code>{uid}</code> banned. ({stopped} running bots band kiye)", reply_markup=back)
    elif action == "unban":
        set_ban(uid, False)
        await msg.answer(f"✅ User <code>{uid}</code> unbanned.", reply_markup=back)
    elif action == "info":
        u = get_user(uid)
        if not u:
            return await msg.answer("❌ User database me nahi mila.", reply_markup=back)
        await msg.answer(
            f"🔍 <b>User Info</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"🆔 ID: <code>{uid}</code>\n"
            f"📛 Name: {esc(u['first_name'])}\n"
            f"🔗 Username: @{esc(u['username']) if u['username'] else '—'}\n"
            f"💎 Plan: {'PREMIUM' if u['premium'] else 'FREE'}\n"
            f"📅 Premium Till: {premium_expiry_text(u)}\n"
            f"🚫 Banned: {'Yes' if u['banned'] else 'No'}\n"
            f"📦 Bots: {user_bot_count(uid)}/{bot_limit(u)}\n"
            f"🗓 Joined: {u['joined_at'] or '—'}",
            reply_markup=back,
        )


@dp.message(Command("stats"))
async def cmd_stats(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.answer(stats_text())


# ==============================================================================
# 👑 ADMIN PANEL — EXTRA FEATURES
# ==============================================================================
async def stop_user_bots(uid: int) -> int:
    n = 0
    for inst in list(ACTIVE_BOTS.values()):
        if inst.user_id == uid and inst.is_running:
            await inst.stop()
            n += 1
    return n


@dp.callback_query(F.data == "check_join")
async def cb_check_join(call: CallbackQuery, state: FSMContext):
    channel = get_setting("force_join", "")
    _join_cache.pop(call.from_user.id, None)
    if channel and not await is_channel_member(call.from_user.id, channel):
        return await call.answer("❌ Abhi tak channel join nahi kiya! Join karke Verify dabao.", show_alert=True)
    await call.answer("✅ Verified! Welcome.")
    try:
        await call.message.delete()
    except Exception:
        pass
    await state.clear()
    ensure_user(call.from_user.id, call.from_user.first_name or "", call.from_user.username or "")
    u = get_user(call.from_user.id)
    await call.message.answer(home_text(u, call.from_user.first_name or "User"), reply_markup=main_menu(call.from_user.id))


@dp.callback_query(F.data == "adm_maint")
async def cb_adm_maint(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    new = "OFF" if get_setting("maintenance", "OFF") == "ON" else "ON"
    set_setting("maintenance", new)
    await call.answer(f"🔧 Maintenance {new}")
    await safe_edit(call, "⚙️ <b>👑 SUPER ADMIN PANEL</b>", admin_main_menu())


@dp.callback_query(F.data == "adm_toggle_pe")
async def cb_adm_toggle_pe(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    new = "OFF" if get_setting("premium_emoji", "ON") == "ON" else "ON"
    set_setting("premium_emoji", new)
    await call.answer(f"✨ Premium Emoji {new}")
    await safe_edit(call, "⚙️ <b>👑 SUPER ADMIN PANEL</b>", admin_main_menu())


def _user_line(uid, name, uname, extra="") -> str:
    handle = f"@{uname}" if uname else (name or "—")
    return f"<code>{uid}</code> {esc(handle)}{extra}"


@dp.callback_query(F.data == "adm_prem_list")
async def cb_adm_prem_list(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    rows = db_query("SELECT user_id, first_name, username, is_premium, premium_until FROM users WHERE is_premium=1 ORDER BY user_id DESC LIMIT 60", fetchall=True) or []
    lines = []
    for uid, name, uname, prem, until in rows:
        if not is_premium_active({"is_premium": prem, "premium_until": until}):
            continue
        lines.append("💎 " + _user_line(uid, name, uname, f" · {'♾' if not until else until[:10]}"))
    body = "\n".join(lines[:40]) if lines else "Koi active premium user nahi."
    await safe_edit(call, f"💎 <b>Premium Users</b> ({len(lines)})\n━━━━━━━━━━━━━━━━━━\n{body}", kb([btn("🔙 Back", "admin_main")]))


@dp.callback_query(F.data == "adm_ban_list")
async def cb_adm_ban_list(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    rows = db_query("SELECT user_id, first_name, username FROM users WHERE is_banned=1 ORDER BY user_id DESC LIMIT 40", fetchall=True) or []
    body = "\n".join("⛔ " + _user_line(*r) for r in rows) if rows else "Koi banned user nahi."
    await safe_edit(call, f"⛔ <b>Banned Users</b> ({len(rows)})\n━━━━━━━━━━━━━━━━━━\n{body}", kb([btn("🔙 Back", "admin_main")]))


@dp.callback_query(F.data == "adm_backup")
async def cb_adm_backup(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer("🗂 Backup ban raha hai...")
    name = f"hosting_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    tmp = os.path.join(tempfile.gettempdir(), name)
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    try:
        await call.message.answer_document(FSInputFile(tmp, filename=name), caption="🗂 Database backup")
    finally:
        os.remove(tmp)


# ---------------------------- Edit UI texts: har screen ka apna admin panel ----------------------------
def _ui_slots_in(key: str) -> List[str]:
    tpl = get_setting(f"ui_{key}", "") or DEFAULT_UI.get(key, "")
    out: List[str] = []
    for sl in re.findall(r"\{([a-z_]+)\}", tpl):
        if sl in DEFAULT_EMOJIS and sl not in out:
            out.append(sl)
    return out


def _ui_key(call: CallbackQuery) -> Optional[str]:
    key = call.data.split(":", 1)[1].split(":", 1)[0]
    return key if key in UI_EDITABLE else None


@dp.callback_query(F.data == "adm_ui")
async def cb_adm_ui(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.clear()
    rows, pair = [], []
    for key, (label, _h) in UI_EDITABLE.items():
        tags = []
        if get_setting(f"ui_{key}", ""):
            tags.append("edited")
        if get_setting(f"ui_yt_{key}", ""):
            tags.append("video")
        pair.append(btn(label + (f" ({', '.join(tags)})" if tags else ""), f"uip:{key}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([btn("🔙 Back", "admin_main")])
    await safe_edit(call, "✏️ <b>Edit UI Texts</b>\n\nHar screen ka alag admin panel hai: text, YouTube link aur emojis.\nEk screen chuno:", kb(*rows))


async def _show_ui_panel(call: CallbackQuery, key: str):
    yt = get_setting(f"ui_yt_{key}", "")
    edited = bool(get_setting(f"ui_{key}", ""))
    n_emoji = len([1 for sl in _ui_slots_in(key) if get_setting(f"uiemoji_{key}_{sl}", "")])
    rows = [
        [btn("✏️ Edit Text", f"uied:{key}"), btn("👁 Preview", f"uiprev:{key}")],
        [btn("▶️ " + ("Change YouTube Link" if yt else "Set YouTube Link"), f"uiyt:{key}")],
    ]
    if yt:
        rows.append([btn("🗑 Remove YouTube Link", f"uiytd:{key}")])
    rows.append([btn("🎨 Set Emojis of this screen", f"uiem:{key}")])
    rows.append([btn("🔄 Reset text to default", f"uirs:{key}")])
    rows.append([btn("🔙 Back to list", "adm_ui")])
    await safe_edit(
        call,
        f"✏️ <b>{UI_EDITABLE[key][0]}</b> — admin panel\n\n"
        f"▶️ YouTube: {esc(yt) if yt else '<i>not set</i>'}\n"
        f"✅ Custom text: <b>{'yes' if edited else 'no (default)'}</b>\n"
        f"🎨 Custom emojis: <b>{n_emoji}</b>",
        kb(*rows),
    )


@dp.callback_query(F.data.startswith("uip:"))
async def cb_ui_panel(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    key = _ui_key(call)
    if not key:
        return await call.answer("Unknown screen", show_alert=True)
    await state.clear()
    await _show_ui_panel(call, key)


@dp.callback_query(F.data.startswith("uiprev:"))
async def cb_ui_preview(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    key = _ui_key(call)
    if not key:
        return await call.answer("Unknown screen", show_alert=True)
    await call.answer()
    text = re.sub(r"\{(v_\w+)\}", "—", ui(key))
    btn_ = ui_video_button(key)
    try:
        await call.message.answer(text, reply_markup=kb([btn_]) if btn_ else None)
    except Exception as e:
        await call.message.answer(f"❌ Is text me galat HTML / emoji hai:\n<code>{esc(str(e))[:300]}</code>")


@dp.callback_query(F.data.startswith("uied:"))
async def cb_ui_edit(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    key = _ui_key(call)
    if not key:
        return await call.answer("Unknown screen", show_alert=True)
    await state.update_data(ui_key=key)
    await state.set_state(AdminStates.wait_ui_text)
    current = get_setting(f"ui_{key}", "") or DEFAULT_UI[key]
    await safe_edit(
        call,
        f"✏️ <b>{UI_EDITABLE[key][0]}</b>\n\nNaya text bhejo (HTML / formatting / premium emoji chalega).\n"
        f"Placeholders: <code>{esc(UI_EDITABLE[key][1])}</code>\n\nCurrent template:\n<pre>{esc(current)}</pre>",
        kb([btn("🔙 Back", f"uip:{key}")]),
    )


@dp.message(AdminStates.wait_ui_text)
async def admin_save_ui_text(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    key = (await state.get_data()).get("ui_key")
    new_text = (msg.html_text or "").strip()
    if key not in UI_EDITABLE or not new_text:
        await state.clear()
        return await msg.answer("❌ Kuch save nahi hua.", reply_markup=admin_main_menu())
    if len(new_text) > 3500:
        return await msg.answer("❌ Bahut lamba (max 3500 characters).")
    set_setting(f"ui_{key}", new_text)
    await state.clear()
    await msg.answer(f"✅ <b>{UI_EDITABLE[key][0]}</b> update ho gaya!", reply_markup=kb([btn("🔙 Back to this screen", f"uip:{key}")]))


@dp.callback_query(F.data.startswith("uirs:"))
async def cb_ui_reset(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    key = _ui_key(call)
    if not key:
        return await call.answer("Unknown screen", show_alert=True)
    del_setting(f"ui_{key}")
    await state.clear()
    await call.answer("Default par reset")
    await _show_ui_panel(call, key)


@dp.callback_query(F.data.startswith("uiyt:"))
async def cb_ui_yt(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    key = _ui_key(call)
    if not key:
        return await call.answer("Unknown screen", show_alert=True)
    await state.update_data(ui_key=key)
    await state.set_state(AdminStates.wait_ui_video)
    cur = get_setting(f"ui_yt_{key}", "")
    await safe_edit(
        call,
        f"▶️ <b>YouTube link — {UI_EDITABLE[key][0]}</b>\n\nYouTube video ka link bhejo. Is screen par users ko <b>Watch Video</b> button dikhega.\n"
        f"Example: <code>https://youtu.be/xxxxxxxxxxx</code>\nCurrent: {esc(cur) if cur else '<i>none</i>'}",
        kb([btn("🔙 Back", f"uip:{key}")]),
    )


@dp.message(AdminStates.wait_ui_video)
async def admin_save_ui_video(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    key = (await state.get_data()).get("ui_key")
    link = (msg.text or "").strip()
    if key not in UI_EDITABLE:
        await state.clear()
        return await msg.answer("❌ Kuch save nahi hua.", reply_markup=admin_main_menu())
    if not YT_RE.match(link):
        return await msg.answer("❌ Sahi YouTube link bhejo (youtube.com ya youtu.be).")
    set_setting(f"ui_yt_{key}", link)
    await state.clear()
    await msg.answer(f"✅ <b>{UI_EDITABLE[key][0]}</b> ke liye YouTube link save ho gaya.", reply_markup=kb([btn("🔙 Back to this screen", f"uip:{key}")]))


@dp.callback_query(F.data.startswith("uiytd:"))
async def cb_ui_yt_delete(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    key = _ui_key(call)
    if not key:
        return await call.answer("Unknown screen", show_alert=True)
    del_setting(f"ui_yt_{key}")
    await call.answer("YouTube link hata diya")
    await _show_ui_panel(call, key)


@dp.callback_query(F.data.startswith("uiem:"))
async def cb_ui_emoji_list(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    key = _ui_key(call)
    if not key:
        return await call.answer("Unknown screen", show_alert=True)
    await state.clear()
    rows, pair = [], []
    for sl in _ui_slots_in(key):
        mark = " *" if get_setting(f"uiemoji_{key}_{sl}", "") else ""
        pair.append(btn(f"{EMOJI_FALLBACK[sl]} {sl}{mark}", f"uies:{key}:{sl}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([btn("🔄 Reset all emojis of this screen", f"uiemr:{key}")])
    rows.append([btn("🔙 Back", f"uip:{key}")])
    await safe_edit(
        call,
        f"🎨 <b>Emojis — {UI_EDITABLE[key][0]}</b>\n\nIs screen ka emoji slot chuno, phir <b>premium emoji</b> (ya uska ID) bhejo.\n"
        "<code>*</code> = is screen ke liye custom emoji set hai.\nJis slot me premium ID nahi, wahan normal emoji dikhta hai.",
        kb(*rows),
    )


@dp.callback_query(F.data.startswith("uies:"))
async def cb_ui_emoji_pick(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    try:
        _, key, slot = call.data.split(":", 2)
    except ValueError:
        return await call.answer("Unknown slot", show_alert=True)
    if key not in UI_EDITABLE or slot not in DEFAULT_EMOJIS:
        return await call.answer("Unknown slot", show_alert=True)
    await state.update_data(ui_key=key, slot=slot)
    await state.set_state(AdminStates.wait_ui_emoji)
    await safe_edit(
        call,
        f"🎨 <b>{slot}</b> — {UI_EDITABLE[key][0]}\n\nEk <b>premium emoji</b> (ya numeric ID) bhejo.\n"
        "<code>default</code> bhejo to global emoji wapas aa jayega.",
        kb([btn("🔙 Back", f"uiem:{key}")]),
    )


def _read_emoji_input(msg: Message) -> Tuple[Optional[str], bool]:
    """(emoji_id, is_default_reset)"""
    txt = (msg.text or "").strip()
    ids = [e.custom_emoji_id for e in (msg.entities or []) if e.type == "custom_emoji" and e.custom_emoji_id]
    if txt.lower() in ("default", "reset"):
        return None, True
    if ids:
        return str(ids[0]), False
    if txt.isdigit():
        return txt, False
    return None, False


@dp.message(AdminStates.wait_ui_emoji)
async def admin_save_ui_emoji(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    key, slot = data.get("ui_key"), data.get("slot")
    if key not in UI_EDITABLE or slot not in DEFAULT_EMOJIS:
        await state.clear()
        return await msg.answer("❌ Kuch save nahi hua.", reply_markup=admin_main_menu())
    eid, reset = _read_emoji_input(msg)
    if reset:
        del_setting(f"uiemoji_{key}_{slot}")
        done = "global emoji par wapas"
    elif eid:
        set_setting(f"uiemoji_{key}_{slot}", eid)
        done = f"set: <code>{eid}</code>"
    else:
        return await msg.answer("❌ Premium emoji, numeric ID ya <code>default</code> bhejo.")
    await state.clear()
    await msg.answer(f"✅ <b>{slot}</b> {done}.", reply_markup=kb([btn("🔙 Back to emojis", f"uiem:{key}")]))


@dp.callback_query(F.data.startswith("uiemr:"))
async def cb_ui_emoji_reset(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    key = _ui_key(call)
    if not key:
        return await call.answer("Unknown screen", show_alert=True)
    del_settings_like(f"uiemoji_{key}_%")
    await call.answer("Emojis reset")
    await cb_ui_emoji_list(call, state)


# ---------------------------- Edit global emojis (har slot ka premium ID) ----------------------------
EMOJI_PAGE = 12


@dp.callback_query(F.data.startswith("adm_emoji"))
async def cb_adm_emoji(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.clear()
    page = int(call.data.split(":")[1]) if ":" in call.data and call.data.split(":")[1].isdigit() else 0
    slots = list(DEFAULT_EMOJIS)
    pages = (len(slots) + EMOJI_PAGE - 1) // EMOJI_PAGE
    page = max(0, min(page, pages - 1))
    rows, pair = [], []
    for sl in slots[page * EMOJI_PAGE:(page + 1) * EMOJI_PAGE]:
        has_premium = bool(get_setting(f"emoji_{sl}", "") or DEFAULT_EMOJIS.get(sl))
        pair.append(btn(f"{EMOJI_FALLBACK[sl]} {sl}{' *' if has_premium else ''}", f"emo:{sl}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    nav = []
    if page > 0:
        nav.append(btn("⬅️ Prev", f"adm_emoji:{page - 1}"))
    if page < pages - 1:
        nav.append(btn("Next ➡️", f"adm_emoji:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([btn("🔙 Back", "admin_main")])
    await safe_edit(
        call,
        f"🎨 <b>Edit Emojis</b> (page {page + 1}/{pages})\n\nSlot chuno, phir premium emoji (ya ID) bhejo.\n"
        "<code>*</code> = premium ID set hai, baaki me normal emoji dikhta hai.\n"
        "💡 Koi premium emoji bot ko bhejoge to wo uska ID bata dega.",
        kb(*rows),
    )


@dp.callback_query(F.data.startswith("emo:"))
async def cb_emoji_pick(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    slot = call.data.split(":", 1)[1]
    if slot not in DEFAULT_EMOJIS:
        return await call.answer("Unknown slot", show_alert=True)
    await state.update_data(slot=slot)
    await state.set_state(AdminStates.wait_emoji_value)
    cur = get_setting(f"emoji_{slot}", "") or DEFAULT_EMOJIS.get(slot) or "—"
    await safe_edit(
        call,
        f"🎨 <b>{slot}</b> {EMOJI_FALLBACK[slot]}\nCurrent ID: <code>{cur}</code>\n\n"
        "Ek <b>premium emoji</b> (ya numeric ID) bhejo.\n<code>default</code> = code wala default.\n<code>none</code> = sirf normal emoji.",
        kb([btn("🔙 Back", "adm_emoji")]),
    )


@dp.message(AdminStates.wait_emoji_value)
async def admin_save_emoji(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    slot = (await state.get_data()).get("slot")
    if slot not in DEFAULT_EMOJIS:
        await state.clear()
        return await msg.answer("❌ Kuch save nahi hua.", reply_markup=admin_main_menu())
    txt = (msg.text or "").strip().lower()
    eid, reset = _read_emoji_input(msg)
    if txt == "none":
        set_setting(f"emoji_{slot}", "x")          # non-numeric => normal emoji hi dikhega
        done = "ab normal emoji dikhayega"
    elif reset:
        del_setting(f"emoji_{slot}")
        done = "default par wapas"
    elif eid:
        set_setting(f"emoji_{slot}", eid)
        done = f"set: <code>{eid}</code>"
    else:
        return await msg.answer("❌ Premium emoji, numeric ID, <code>default</code> ya <code>none</code> bhejo.")
    await state.clear()
    await msg.answer(f"✅ <b>{slot}</b> {done}.", reply_markup=kb([btn("🔙 Back to emojis", "adm_emoji")]))


# ---------------------------- Premium emoji ID finder (admin kisi bhi premium emoji bheje) ----------------------------
@dp.message(F.from_user.id == ADMIN_ID, F.entities)
async def admin_emoji_id_finder(msg: Message, state: FSMContext):
    if await state.get_state() is not None:
        return
    ids = [e.custom_emoji_id for e in (msg.entities or []) if e.type == "custom_emoji" and e.custom_emoji_id]
    if not ids:
        return
    lines = "\n".join(f"<code>{i}</code>" for i in dict.fromkeys(ids))
    await msg.answer(f"🎨 <b>Premium emoji ID(s):</b>\n{lines}\n\nAdmin → Edit Emojis me jakar kisi slot par set kar do.")


# ==============================================================================
# 🚀 STARTUP / SHUTDOWN
# ==============================================================================
async def restore_bots():
    rows = db_query("SELECT id FROM hosted_bots WHERE status='Running'", fetchall=True) or []
    for (bid,) in rows:
        inst = get_instance(bid)
        if inst:
            await inst.start()
            logger.info("♻️ Restored bot %s", bid)


async def on_startup():
    init_db()
    os.makedirs(HOST_DIR, exist_ok=True)
    logger.info("🚀 %s v%s started!", BRAND, VERSION)
    asyncio.create_task(restore_bots())


async def on_shutdown():
    # Status DB me change nahi karte, taaki restart ke baad bots wapas chal jayein
    for inst in ACTIVE_BOTS.values():
        inst.kill_silently()
    logger.info("🛑 Hosting bot shutting down.")


async def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
