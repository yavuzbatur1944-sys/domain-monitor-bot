import logging
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    load_dotenv()
    BOT_TOKEN = os.getenv("BOT_TOKEN")
else:
    load_dotenv(override=False)

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", BASE_DIR / "domain_monitor.db"))
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "15"))

STATUS_UP = "UP"
STATUS_DOWN = "DOWN"
STATUS_UNKNOWN = "UNKNOWN"

DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))+$"
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)


@contextmanager
def db_connect() -> Iterator[sqlite3.Connection]:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def init_db() -> None:
    with db_connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS domains (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                domain TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'UNKNOWN',
                last_checked_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(chat_id, domain),
                FOREIGN KEY(chat_id) REFERENCES subscribers(chat_id) ON DELETE CASCADE
            )
            """
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_domains_chat_id ON domains(chat_id)")


def normalize_domain(raw_value: str) -> str:
    value = raw_value.strip().lower()
    if not value:
        raise ValueError("Domain boş olamaz.")

    if "://" in value:
        parsed = urlparse(value)
        value = parsed.netloc or parsed.path

    value = value.split("/")[0].split("?")[0].split("#")[0].strip(".")
    if value.startswith("www."):
        value = value[4:]

    if not DOMAIN_RE.match(value):
        raise ValueError("Geçerli bir domain girin. Örnek: example.com")

    return value


def ensure_subscriber(chat_id: int) -> None:
    with db_connect() as connection:
        connection.execute(
            """
            INSERT INTO subscribers(chat_id, created_at)
            VALUES(?, ?)
            ON CONFLICT(chat_id) DO NOTHING
            """,
            (chat_id, utc_now()),
        )


def add_domain(chat_id: int, domain: str) -> bool:
    ensure_subscriber(chat_id)
    with db_connect() as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO domains(chat_id, domain, created_at)
            VALUES(?, ?, ?)
            """,
            (chat_id, domain, utc_now()),
        )
        return cursor.rowcount > 0


def remove_domain(chat_id: int, domain: str) -> bool:
    with db_connect() as connection:
        cursor = connection.execute(
            "DELETE FROM domains WHERE chat_id = ? AND domain = ?",
            (chat_id, domain),
        )
        return cursor.rowcount > 0


def list_domains(chat_id: int) -> list[sqlite3.Row]:
    with db_connect() as connection:
        return connection.execute(
            """
            SELECT domain, status, last_checked_at, last_error
            FROM domains
            WHERE chat_id = ?
            ORDER BY domain
            """,
            (chat_id,),
        ).fetchall()


def get_domain(chat_id: int, domain: str) -> sqlite3.Row | None:
    with db_connect() as connection:
        return connection.execute(
            """
            SELECT id, chat_id, domain, status
            FROM domains
            WHERE chat_id = ? AND domain = ?
            """,
            (chat_id, domain),
        ).fetchone()


def get_all_domains() -> list[sqlite3.Row]:
    with db_connect() as connection:
        return connection.execute(
            """
            SELECT id, chat_id, domain, status
            FROM domains
            ORDER BY id
            """
        ).fetchall()


def update_domain_status(domain_id: int, status: str, error: str | None) -> None:
    with db_connect() as connection:
        connection.execute(
            """
            UPDATE domains
            SET status = ?, last_checked_at = ?, last_error = ?
            WHERE id = ?
            """,
            (status, utc_now(), error, domain_id),
        )


def status_icon(status: str) -> str:
    if status == STATUS_UP:
        return "OK"
    if status == STATUS_DOWN:
        return "ENGEL/ERISIM YOK"
    return "BILINMIYOR"


async def fetch_domain_once(url: str) -> tuple[bool, str | None]:
    timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS)
    headers = {"User-Agent": "DomainMonitorBot/1.0"}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
        response = await client.get(url)
        if response.status_code == 451:
            return False, "HTTP 451 - yasal erişim engeli olabilir"
        if 200 <= response.status_code < 500:
            return True, f"HTTP {response.status_code}"
        return False, f"HTTP {response.status_code}"


async def check_domain(domain: str) -> tuple[str, str | None]:
    errors: list[str] = []
    for scheme in ("https", "http"):
        url = f"{scheme}://{domain}"
        try:
            is_reachable, detail = await fetch_domain_once(url)
            if is_reachable:
                return STATUS_UP, detail
            errors.append(f"{scheme}: {detail}")
        except httpx.TimeoutException:
            errors.append(f"{scheme}: timeout")
        except httpx.ConnectError as error:
            errors.append(f"{scheme}: bağlantı hatası ({error.__class__.__name__})")
        except httpx.HTTPError as error:
            errors.append(f"{scheme}: HTTP hata ({error.__class__.__name__})")

    return STATUS_DOWN, "; ".join(errors)[:500]


def format_domain_result(domain: str, status: str, detail: str | None) -> str:
    text = f"{domain}: {status_icon(status)}"
    if detail:
        text += f"\nDetay: {detail}"
    return text


def get_command_arg(update: Update, command_name: str) -> str | None:
    if not update.message or not update.message.text:
        return None

    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2:
        return None
    return parts[1].strip()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return

    ensure_subscriber(update.effective_chat.id)
    await update.message.reply_text(
        "Domain Monitor Bot aktif.\n\n"
        "Domain eklemek için:\n"
        "/add example.com\n\n"
        "Kayıtlı domainler her 5 dakikada bir kontrol edilir. "
        "Durum değişirse bu sohbete bildirim gönderirim.\n\n"
        "/help ile komutları görebilirsin."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    await update.message.reply_text(
        "Komutlar:\n"
        "/start - Botu başlatır.\n"
        "/add domain.com - Domaini izleme listesine ekler.\n"
        "/remove domain.com - Domaini izleme listesinden çıkarır.\n"
        "/list - Kayıtlı domainleri ve son durumlarını listeler.\n"
        "/check domain.com - Domaini hemen kontrol eder.\n"
        "/help - Yardım mesajını gösterir."
    )


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return

    raw_domain = get_command_arg(update, "add")
    if not raw_domain:
        await update.message.reply_text("Kullanım: /add domain.com")
        return

    try:
        domain = normalize_domain(raw_domain)
    except ValueError as error:
        await update.message.reply_text(str(error))
        return

    created = add_domain(update.effective_chat.id, domain)
    if created:
        status, detail = await check_domain(domain)
        domain_row = get_domain(update.effective_chat.id, domain)
        if domain_row:
            update_domain_status(domain_row["id"], status, detail)
        await update.message.reply_text(f"{domain} eklendi.\n{format_domain_result(domain, status, detail)}")
    else:
        await update.message.reply_text(f"{domain} zaten izleme listesinde.")


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return

    raw_domain = get_command_arg(update, "remove")
    if not raw_domain:
        await update.message.reply_text("Kullanım: /remove domain.com")
        return

    try:
        domain = normalize_domain(raw_domain)
    except ValueError as error:
        await update.message.reply_text(str(error))
        return

    removed = remove_domain(update.effective_chat.id, domain)
    if removed:
        await update.message.reply_text(f"{domain} izleme listesinden çıkarıldı.")
    else:
        await update.message.reply_text(f"{domain} izleme listesinde bulunamadı.")


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return

    rows = list_domains(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("Henüz kayıtlı domain yok.\nEklemek için: /add example.com")
        return

    lines = ["Kayıtlı domainler:"]
    for row in rows:
        checked = row["last_checked_at"] or "henüz kontrol edilmedi"
        line = f"- {row['domain']}: {status_icon(row['status'])} ({checked})"
        if row["last_error"] and row["status"] == STATUS_DOWN:
            line += f"\n  Detay: {row['last_error']}"
        lines.append(line)

    await update.message.reply_text("\n".join(lines))


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    raw_domain = get_command_arg(update, "check")
    if not raw_domain:
        await update.message.reply_text("Kullanım: /check domain.com")
        return

    try:
        domain = normalize_domain(raw_domain)
    except ValueError as error:
        await update.message.reply_text(str(error))
        return

    status, detail = await check_domain(domain)
    await update.message.reply_text(format_domain_result(domain, status, detail))


async def check_all_domains(context: ContextTypes.DEFAULT_TYPE) -> None:
    domains = get_all_domains()
    if not domains:
        logger.info("No domains registered yet")
        return

    logger.info("Checking %s registered domains", len(domains))
    for row in domains:
        domain_id = row["id"]
        chat_id = row["chat_id"]
        domain = row["domain"]
        previous_status = row["status"]

        try:
            new_status, detail = await check_domain(domain)
        except Exception as error:
            logger.exception("Domain check failed for %s", domain)
            new_status = STATUS_DOWN
            detail = str(error)[:500]

        update_domain_status(domain_id, new_status, detail)

        if previous_status != STATUS_UNKNOWN and previous_status != new_status:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "Domain durum değişikliği!\n\n"
                        f"{domain}\n"
                        f"Eski durum: {status_icon(previous_status)}\n"
                        f"Yeni durum: {status_icon(new_status)}\n"
                        f"Detay: {detail or '-'}"
                    ),
                )
            except Exception:
                logger.exception("Could not send status change notification for %s", domain)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled bot error", exc_info=context.error)


def build_application() -> Application:
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_TELEGRAM_BOT_TOKEN_HERE":
        raise RuntimeError("BOT_TOKEN eksik. Railway environment variable veya .env dosyası ile BOT_TOKEN verin.")

    init_db()

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add", add_command))
    application.add_handler(CommandHandler("remove", remove_command))
    application.add_handler(CommandHandler("list", list_command))
    application.add_handler(CommandHandler("check", check_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_error_handler(error_handler)

    if application.job_queue is None:
        raise RuntimeError('JobQueue eksik. requirements.txt ile "python-telegram-bot[job-queue]" kurun.')

    application.job_queue.run_repeating(
        check_all_domains,
        interval=CHECK_INTERVAL_SECONDS,
        first=10,
        name="domain-monitor",
    )

    return application


def main() -> None:
    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
