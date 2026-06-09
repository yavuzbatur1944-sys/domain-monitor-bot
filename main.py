import asyncio
import logging
import os
import re
import sqlite3
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import Message, Update
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
BTK_QUERY_TIMEOUT_SECONDS = int(os.getenv("BTK_QUERY_TIMEOUT_SECONDS", "300"))
MAX_CONCURRENT_CHECKS = int(os.getenv("MAX_CONCURRENT_CHECKS", "3"))

STATUS_BLOCKED = "BLOCKED"
STATUS_CLEAR = "CLEAR"
STATUS_UNKNOWN = "UNKNOWN"

DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))+$"
)

CommandCallback = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


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


def status_label(status: str) -> str:
    if status == STATUS_BLOCKED:
        return "ENGEL VAR"
    if status == STATUS_CLEAR:
        return "ENGEL YOK"
    return "BILINMIYOR"


def parse_btk_result(result_text: str) -> tuple[str, str]:
    normalized = " ".join(result_text.casefold().split())

    no_block_phrases = (
        "uygulanan bir karar bulunamadı",
        "uygulanan bir karar bulunamadi",
        "uygulanmış bir karar bulunamadı",
        "uygulanmis bir karar bulunamadi",
        "herhangi bir karar bulunamadı",
        "herhangi bir karar bulunamadi",
    )
    if any(phrase in normalized for phrase in no_block_phrases):
        return STATUS_CLEAR, result_text.strip()

    blocked_phrases = (
        "erişime engellenmiştir",
        "erisime engellenmistir",
        "erişimin engellenmesi",
        "erisimin engellenmesi",
        "idari tedbir",
        "koruma tedbiri",
        "sulh ceza hakimliği",
        "sulh ceza hakimligi",
        "mahkeme kararı",
        "mahkeme karari",
    )
    if any(phrase in normalized for phrase in blocked_phrases):
        return STATUS_BLOCKED, result_text.strip()

    return STATUS_UNKNOWN, result_text.strip() or "BTK sonucu yorumlanamadı."


def query_btk_sync(domain: str) -> str:
    try:
        from BTKSorgu import BTKSorgu
    except ImportError as error:
        raise RuntimeError('BTK sorgusu için "BTKSorgu" paketi kurulmalı. requirements.txt ile kurun.') from error

    result = BTKSorgu(domain)
    return str(result)


async def check_domain(domain: str) -> tuple[str, str | None]:
    try:
        result_text = await asyncio.wait_for(
            asyncio.to_thread(query_btk_sync, domain),
            timeout=BTK_QUERY_TIMEOUT_SECONDS,
        )
        status, detail = parse_btk_result(result_text)
        return status, detail[:1000]
    except asyncio.TimeoutError:
        logger.warning("BTK query timeout for %s", domain)
        return STATUS_UNKNOWN, "BTK sorgusu zaman aşımına uğradı, bir sonraki 5 dakikalık kontrolde tekrar denenecek."
    except Exception as error:
        logger.exception("BTK query failed for %s", domain)
        return STATUS_UNKNOWN, str(error)[:500]


def format_domain_result(domain: str, status: str, detail: str | None) -> str:
    text = f"{domain}: {status_label(status)}"
    if detail:
        text += f"\nDetay: {detail}"
    return text


def get_command_arg(update: Update) -> str | None:
    if not update.message or not update.message.text:
        return None

    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2:
        return None
    return parts[1].strip()


def log_command_received(command_name: str, update: Update) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    user_id = update.effective_user.id if update.effective_user else None
    logger.info("command received: /%s chat_id=%s user_id=%s", command_name, chat_id, user_id)


async def reply_short_error(update: Update) -> None:
    message = update.effective_message
    if message:
        await message.reply_text("Kısa bir hata oluştu. Lütfen tekrar deneyin.")


def command_wrapper(command_name: str, callback: CommandCallback) -> CommandCallback:
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        log_command_received(command_name, update)
        try:
            await callback(update, context)
        except ValueError as error:
            if update.effective_message:
                await update.effective_message.reply_text(str(error))
        except Exception:
            logger.exception("Command failed: /%s", command_name)
            await reply_short_error(update)

    return wrapped


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return

    ensure_subscriber(update.effective_chat.id)
    await update.message.reply_text(
        "BTK/ESB Domain Monitor Bot aktif.\n\n"
        "Domain eklemek için:\n"
        "/add example.com\n\n"
        "Kayıtlı domainler her 5 dakikada bir BTK/ESB sorgusu ile kontrol edilir. "
        "Engel gelirse veya kalkarsa bu sohbete bildirim gönderirim.\n\n"
        "/help ile komutları görebilirsin."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    await update.message.reply_text(
        "Komutlar:\n"
        "/start - Botu başlatır.\n"
        "/add domain.com - Domaini BTK/ESB takip listesine ekler.\n"
        "/remove domain.com - Domaini takip listesinden çıkarır.\n"
        "/list - Kayıtlı domainleri ve son BTK/ESB durumlarını listeler.\n"
        "/check domain.com - Domain için hemen BTK/ESB sorgusu yapar.\n"
        "/help - Yardım mesajını gösterir."
    )


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return

    raw_domain = get_command_arg(update)
    if not raw_domain:
        await update.message.reply_text("Kullanım: /add domain.com")
        return

    domain = normalize_domain(raw_domain)
    created = add_domain(update.effective_chat.id, domain)
    if not created:
        await update.message.reply_text(f"{domain} zaten takip listesinde.")
        return

    await update.message.reply_text(
        f"{domain} takip listesine eklendi.\n"
        "İlk BTK/ESB sorgusu arka planda başlatıldı. Sonuç gelince ayrıca yazacağım."
    )
    schedule_background_check(context, update.effective_chat.id, domain, notify=True)


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return

    raw_domain = get_command_arg(update)
    if not raw_domain:
        await update.message.reply_text("Kullanım: /remove domain.com")
        return

    domain = normalize_domain(raw_domain)
    removed = remove_domain(update.effective_chat.id, domain)
    if removed:
        await update.message.reply_text(f"{domain} takip listesinden çıkarıldı.")
    else:
        await update.message.reply_text(f"{domain} takip listesinde bulunamadı.")


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
        line = f"- {row['domain']}: {status_label(row['status'])} ({checked})"
        if row["last_error"] and row["status"] != STATUS_CLEAR:
            line += f"\n  Detay: {row['last_error']}"
        lines.append(line)

    await update.message.reply_text("\n".join(lines))


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    raw_domain = get_command_arg(update)
    if not raw_domain:
        await update.message.reply_text("Kullanım: /check domain.com")
        return

    domain = normalize_domain(raw_domain)
    await update.message.reply_text(f"{domain} için BTK/ESB sorgusu başlatıldı.")
    status, detail = await check_domain(domain)
    await update.message.reply_text(format_domain_result(domain, status, detail))


def schedule_background_check(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    domain: str,
    *,
    notify: bool,
) -> None:
    task = run_domain_check_and_store(context, chat_id, domain, notify=notify)
    if context.application:
        context.application.create_task(task)
    else:
        asyncio.create_task(task)


async def run_domain_check_and_store(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    domain: str,
    *,
    notify: bool,
) -> None:
    try:
        status, detail = await check_domain(domain)
        domain_row = get_domain(chat_id, domain)
        if not domain_row:
            return

        update_domain_status(domain_row["id"], status, detail)
        if notify:
            await context.bot.send_message(
                chat_id=chat_id,
                text="İlk BTK/ESB sorgusu tamamlandı.\n" + format_domain_result(domain, status, detail),
            )
    except Exception:
        logger.exception("Background domain check failed for %s", domain)
        if notify:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{domain} için ilk BTK/ESB sorgusu tamamlanamadı. Sonraki periyodik kontrolde tekrar denenecek.",
            )


async def check_all_domains(context: ContextTypes.DEFAULT_TYPE) -> None:
    domains = get_all_domains()
    if not domains:
        logger.info("No domains registered yet")
        return

    logger.info("Checking %s registered domains", len(domains))
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)

    async def check_row(row: sqlite3.Row) -> None:
        async with semaphore:
            domain_id = row["id"]
            chat_id = row["chat_id"]
            domain = row["domain"]
            previous_status = row["status"]

            new_status, detail = await check_domain(domain)
            update_domain_status(domain_id, new_status, detail)

            if previous_status == STATUS_UNKNOWN or new_status == STATUS_UNKNOWN or previous_status == new_status:
                return

            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "BTK/ESB erişim engeli durum değişikliği!\n\n"
                        f"{domain}\n"
                        f"Eski durum: {status_label(previous_status)}\n"
                        f"Yeni durum: {status_label(new_status)}\n"
                        f"Detay: {detail or '-'}"
                    ),
                )
            except Exception:
                logger.exception("Could not send status change notification for %s", domain)

    await asyncio.gather(*(check_row(row) for row in domains))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled bot error", exc_info=context.error)
    if isinstance(update, Update):
        await reply_short_error(update)


def build_application() -> Application:
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_TELEGRAM_BOT_TOKEN_HERE":
        raise RuntimeError("BOT_TOKEN eksik. Railway environment variable veya .env dosyası ile BOT_TOKEN verin.")

    init_db()

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", command_wrapper("start", start), block=False))
    application.add_handler(CommandHandler("help", command_wrapper("help", help_command), block=False))
    application.add_handler(CommandHandler("add", command_wrapper("add", add_command), block=False))
    application.add_handler(CommandHandler("remove", command_wrapper("remove", remove_command), block=False))
    application.add_handler(CommandHandler("list", command_wrapper("list", list_command), block=False))
    application.add_handler(CommandHandler("check", command_wrapper("check", check_command), block=False))
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
