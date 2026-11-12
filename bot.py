"""Telegram digest bot for LinkedIn job-alert emails (stdlib only)."""

from __future__ import annotations

import email
import email.header
import email.policy
import imaplib
import logging
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent


def load_dotenv() -> None:
    """Load simple KEY=VALUE settings without an external dependency."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()

DB_PATH = Path(os.getenv("DATABASE_PATH", str(ROOT / "jobs.sqlite3")))
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Budapest"))
DAILY_HOUR = int(os.getenv("DAILY_HOUR", "9"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "300"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
IMAP_HOST = os.getenv("IMAP_HOST", "")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER = os.getenv("IMAP_USER", "")
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD", "")
IMAP_FOLDER = os.getenv("IMAP_FOLDER", "INBOX")

DEFAULT_ROLES = "software engineer, software developer, cybersecurity, cyber security, security engineer, SOC analyst, penetration tester"
LINKEDIN_JOB = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)", re.I)
LOG = logging.getLogger("linkedin_telegram_bot")


def connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL,
            email_date TEXT NOT NULL, added_at TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS cursors (mailbox TEXT PRIMARY KEY, uid INTEGER NOT NULL);
        """
    )
    db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('roles',?)", (DEFAULT_ROLES,))
    db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('chat_id','')")
    db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('last_digest_date','')")
    db.commit()
    return db


def setting(db: sqlite3.Connection, key: str) -> str:
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""


def set_setting(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    db.commit()


def decode_header(value: str | None) -> str:
    if not value:
        return ""
    decoded = []
    for part, charset in email.header.decode_header(value):
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


class JobLinkParser(HTMLParser):
    """Extract LinkedIn job-card links and their visible anchor titles."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            self._href = dict(attrs).get("href") or ""
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            self.links.append((self._href, " ".join(" ".join(self._text).split())))
            self._href = ""
            self._text = []


def message_bodies(msg: Message) -> tuple[str, str]:
    plain, markup = [], []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.get_content_disposition() == "attachment":
            continue
        kind = part.get_content_type()
        if kind not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        body = payload.decode(charset, errors="replace")
        if kind == "text/html":
            markup.append(body)
        else:
            plain.append(body)
    return "\n".join(plain), "\n".join(markup)


def extract_jobs(msg: Message) -> list[tuple[str, str]]:
    plain, markup = message_bodies(msg)
    parser = JobLinkParser()
    parser.feed(markup)
    found: dict[str, str] = {}
    for url, title in parser.links:
        match = LINKEDIN_JOB.search(url)
        if match:
            job_id = match.group(1)
            found[job_id] = (title or "LinkedIn job listing").strip()
    # Plain-text alerts occasionally contain links without an HTML alternative.
    for match in LINKEDIN_JOB.finditer(plain):
        found.setdefault(match.group(1), "LinkedIn job listing")
    return [(job_id, title) for job_id, title in found.items()]


def is_linkedin_sender(msg: Message) -> bool:
    sender = email.utils.parseaddr(decode_header(msg.get("From")))[1].lower()
    return sender.endswith("@linkedin.com") or sender.endswith("@e.linkedin.com")


def role_matches(title: str, roles: str) -> bool:
    terms = [term.strip().casefold() for term in roles.split(",") if term.strip()]
    return not terms or any(term in title.casefold() for term in terms)


def collect_linkedin_alerts(db: sqlite3.Connection) -> int:
    required = (IMAP_HOST, IMAP_USER, IMAP_PASSWORD)
    if not all(required):
        LOG.warning("IMAP is not configured; LinkedIn alert emails cannot be collected yet.")
        return 0
    mailbox = f"{IMAP_HOST}:{IMAP_PORT}/{IMAP_FOLDER}"
    try:
        with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT) as client:
            client.login(IMAP_USER, IMAP_PASSWORD)
            status, _ = client.select(IMAP_FOLDER, readonly=True)
            if status != "OK":
                raise RuntimeError(f"Could not select IMAP folder {IMAP_FOLDER!r}")
            cursor = db.execute("SELECT uid FROM cursors WHERE mailbox=?", (mailbox,)).fetchone()
            if cursor is None:
                status, data = client.uid("search", None, "ALL")
                if status != "OK":
                    raise RuntimeError("Could not initialize IMAP mailbox cursor")
                existing_uids = [int(value) for value in data[0].split()] if data and data[0] else []
                last_uid = max(existing_uids, default=0)
                db.execute("INSERT INTO cursors(mailbox,uid) VALUES(?,?)", (mailbox, last_uid))
                db.commit()
                LOG.info("Initialized mailbox cursor at UID %d; waiting for new alert emails", last_uid)
                return 0
            last_uid = int(cursor["uid"])
            status, data = client.uid("search", None, "UID", f"{last_uid + 1}:*")
            if status != "OK":
                raise RuntimeError("IMAP UID search failed")
            uids = [int(value) for value in data[0].split()] if data and data[0] else []
            newest_uid = last_uid
            added = 0
            roles = setting(db, "roles")
            for uid in uids:
                newest_uid = max(newest_uid, uid)
                status, fetched = client.uid("fetch", str(uid), "(RFC822)")
                if status != "OK":
                    continue
                raw = next((item[1] for item in fetched if isinstance(item, tuple)), None)
                if raw is None:
                    continue
                msg = email.message_from_bytes(raw, policy=email.policy.default)
                if not is_linkedin_sender(msg):
                    continue
                email_date = decode_header(msg.get("Date"))
                parsed_date = email.utils.parsedate_to_datetime(email_date) if email_date else None
                if parsed_date and (datetime.now(parsed_date.tzinfo).date() - parsed_date.date()).days > 3:
                    continue
                for job_id, title in extract_jobs(msg):
                    if not role_matches(title, roles):
                        continue
                    url = f"https://www.linkedin.com/jobs/view/{job_id}/"
                    cur = db.execute(
                        "INSERT OR IGNORE INTO jobs(job_id,title,url,email_date,added_at) VALUES(?,?,?,?,?)",
                        (job_id, title, url, email_date, datetime.now(TZ).isoformat()),
                    )
                    added += cur.rowcount
            db.execute(
                "INSERT INTO cursors(mailbox,uid) VALUES(?,?) ON CONFLICT(mailbox) DO UPDATE SET uid=excluded.uid",
                (mailbox, newest_uid),
            )
            db.commit()
            return added
    except (imaplib.IMAP4.error, OSError, RuntimeError) as exc:
        LOG.exception("Could not read LinkedIn alert email: %s", exc)
        return 0


def telegram(method: str, payload: dict) -> dict:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in your .env file first.")
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    body = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=40) as response:
            result = __import__("json").loads(response.read().decode())
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Telegram API request failed: {exc}") from exc
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Telegram API error"))
    return result


def send_message(chat_id: str, text: str) -> None:
    telegram("sendMessage", {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"})


def send_digest(db: sqlite3.Connection, chat_id: str, *, force: bool = False) -> int:
    rows = db.execute("SELECT job_id,title,url FROM jobs WHERE sent=0 ORDER BY added_at DESC LIMIT 30").fetchall()
    if not rows:
        send_message(chat_id, "No new matching LinkedIn jobs yet. I’ll keep checking your alert emails.")
        return 0
    header = "New Budapest roles from your LinkedIn alerts:" if force else "Your daily LinkedIn job digest:"
    messages = [header]
    for row in rows:
        title = row["title"]
        url = row["url"]
        messages.append(f"\n• {title}\n{url}")
    # Telegram messages are limited to 4096 characters; send chunks safely.
    chunk = ""
    sent_ids: list[str] = []
    for item, row in zip(messages[1:], rows):
        if len(chunk) + len(item) > 3500:
            send_message(chat_id, f"{header}\n{chunk}")
            chunk = ""
        chunk += item
        sent_ids.append(row["job_id"])
    if chunk:
        send_message(chat_id, f"{header}\n{chunk}")
    if sent_ids:
        db.executemany("UPDATE jobs SET sent=1 WHERE job_id=?", [(job_id,) for job_id in sent_ids])
        db.commit()
    return len(sent_ids)


def command_response(db: sqlite3.Connection, chat_id: str, text: str) -> str | None:
    command, _, arg = text.strip().partition(" ")
    command = command.split("@", 1)[0].lower()
    if command in ("/start", "/subscribe"):
        existing_chat = setting(db, "chat_id")
        if existing_chat and existing_chat != chat_id:
            return "This bot is already subscribed in another Telegram chat."
        set_setting(db, "chat_id", chat_id)
        return (
            "Subscribed. I’ll send a daily digest at "
            f"{DAILY_HOUR:02d}:00 Budapest time.\n\n"
            "First, create daily email alerts on LinkedIn for Budapest jobs. "
            "Then connect the mailbox in .env and restart this bot.\n\n"
            "Default roles: " + setting(db, "roles") + "\n"
            "Use /roles to customize, /digest to send pending jobs now, /status for setup."
        )
    if command == "/help":
        return "/start — subscribe\n/roles — view role filters\n/roles term1, term2 — change filters\n/digest — send pending jobs now\n/status — check bot setup\n/stop — unsubscribe"
    if chat_id != setting(db, "chat_id"):
        return "This bot is private. Send /start in the subscribed chat to use it."
    if command == "/roles":
        if arg.strip():
            roles = ", ".join(part.strip() for part in arg.split(",") if part.strip())
            if not roles:
                return "Please provide comma-separated role keywords, for example /roles backend engineer, security analyst"
            set_setting(db, "roles", roles)
            return "Role filters updated: " + roles
        return "Current role filters: " + setting(db, "roles") + "\nChange them with /roles backend engineer, security analyst"
    if command == "/digest":
        send_digest(db, chat_id, force=True)
        return None
    if command == "/status":
        email_ready = all((IMAP_HOST, IMAP_USER, IMAP_PASSWORD))
        chat_ready = bool(setting(db, "chat_id"))
        pending = db.execute("SELECT COUNT(*) AS n FROM jobs WHERE sent=0").fetchone()["n"]
        return (
            f"Telegram subscribed: {'yes' if chat_ready else 'no'}\n"
            f"Email connection configured: {'yes' if email_ready else 'no'}\n"
            f"Pending matching jobs: {pending}\n"
            f"Daily send time: {DAILY_HOUR:02d}:00 Europe/Budapest\n"
            "Role filters: " + setting(db, "roles")
        )
    if command == "/stop":
        set_setting(db, "chat_id", "")
        return "Unsubscribed. Send /start any time to subscribe again."
    return "I don’t recognize that command. Send /help to see the available commands."


def poll_telegram(db: sqlite3.Connection, offset: int) -> int:
    result = telegram("getUpdates", {"offset": offset, "timeout": 25, "allowed_updates": '["message"]'})
    for update in result.get("result", []):
        offset = max(offset, int(update["update_id"]) + 1)
        message = update.get("message", {})
        text = message.get("text", "")
        chat_id = str(message.get("chat", {}).get("id", ""))
        if not text.startswith("/") or not chat_id:
            continue
        try:
            reply = command_response(db, chat_id, text)
            if reply:
                send_message(chat_id, reply)
        except Exception:
            LOG.exception("Failed to handle Telegram command")
            send_message(chat_id, "I hit an error handling that command. Check the bot log and configuration.")
    return offset


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not TELEGRAM_TOKEN:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN. Copy .env.example to .env and fill it in.")
    db = connect_db()
    offset = 0
    while True:
        try:
            added = collect_linkedin_alerts(db)
            if added:
                LOG.info("Queued %d new matching LinkedIn jobs", added)
            chat_id = setting(db, "chat_id")
            now = datetime.now(TZ)
            today = now.date().isoformat()
            if chat_id and now.hour >= DAILY_HOUR and setting(db, "last_digest_date") != today:
                send_digest(db, chat_id)
                set_setting(db, "last_digest_date", today)
            offset = poll_telegram(db, offset)
        except (RuntimeError, urllib.error.URLError) as exc:
            LOG.warning("Bot connection issue: %s", exc)
        except Exception:
            LOG.exception("Unexpected bot loop error")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()

