import asyncio
import html
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


APPROVE_PREFIX = "approve:"
REJECT_PREFIX = "reject:"
RETENTION_DAYS = 7
PHOTO_ADMIN_TEXT_LIMIT = 700


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_chat_id: int
    channel_id: int
    database_path: Path


class SubmissionStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    author_name TEXT NOT NULL,
                    text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    admin_message_id INTEGER,
                    moderator_id INTEGER,
                    content_type TEXT NOT NULL DEFAULT 'text',
                    file_id TEXT,
                    created_at TEXT NOT NULL,
                    decided_at TEXT
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(submissions)").fetchall()
            }
            if "content_type" not in columns:
                connection.execute(
                    "ALTER TABLE submissions ADD COLUMN content_type TEXT NOT NULL DEFAULT 'text'"
                )
            if "file_id" not in columns:
                connection.execute("ALTER TABLE submissions ADD COLUMN file_id TEXT")

    def create(
        self,
        user_id: int,
        username: str | None,
        author_name: str,
        text: str,
        content_type: str = "text",
        file_id: str | None = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO submissions (
                    user_id,
                    username,
                    author_name,
                    text,
                    content_type,
                    file_id,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, username, author_name, text, content_type, file_id, now),
            )
            return int(cursor.lastrowid)

    def set_admin_message(self, submission_id: int, admin_message_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE submissions SET admin_message_id = ? WHERE id = ?",
                (admin_message_id, submission_id),
            )

    def get(self, submission_id: int) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM submissions WHERE id = ?",
                (submission_id,),
            ).fetchone()

    def decide(self, submission_id: int, status: str, moderator_id: int) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE submissions
                SET status = ?, moderator_id = ?, decided_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (status, moderator_id, now, submission_id),
            )
            return cursor.rowcount == 1

    def set_status(self, submission_id: int, status: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE submissions SET status = ? WHERE id = ?",
                (status, submission_id),
            )

    def delete_decided_older_than(self, cutoff: datetime) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM submissions
                WHERE status IN ('approved', 'rejected')
                AND decided_at IS NOT NULL
                AND decided_at < ?
                """,
                (cutoff.isoformat(),),
            )
            return cursor.rowcount


def load_settings() -> Settings:
    load_dotenv()

    missing = [
        name
        for name in ("BOT_TOKEN", "ADMIN_CHAT_ID", "CHANNEL_ID")
        if not os.getenv(name)
    ]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

    return Settings(
        bot_token=os.environ["BOT_TOKEN"],
        admin_chat_id=int(os.environ["ADMIN_CHAT_ID"]),
        channel_id=int(os.environ["CHANNEL_ID"]),
        database_path=Path(os.getenv("DATABASE_PATH", "bot.sqlite3")),
    )


def user_label(update: Update) -> tuple[str | None, str]:
    user = update.effective_user
    if user is None:
        return None, "Неизвестный автор"

    username = user.username
    if username:
        return username, f"@{username}"

    safe_name = html.escape(user.full_name or "Пользователь")
    return None, f'<a href="tg://user?id={user.id}">{safe_name}</a>'


def moderation_keyboard(submission_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Одобрить", callback_data=f"{APPROVE_PREFIX}{submission_id}"),
                InlineKeyboardButton("Отклонить", callback_data=f"{REJECT_PREFIX}{submission_id}"),
            ]
        ]
    )


def build_moderation_text(submission_id: int, author_name: str, text: str) -> str:
    return (
        f"<b>Заявка #{submission_id}</b>\n"
        f"<b>Автор:</b> {author_name}\n\n"
        f"{html.escape(text)}"
    )


def clipped_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit - 3]}..."


def build_photo_moderation_caption(
    submission_id: int,
    author_name: str,
    text: str,
    footer: str = "",
) -> str:
    prefix = f"<b>Заявка #{submission_id}</b>\n<b>Автор:</b> {author_name}\n\n"
    caption = clipped_text(text, PHOTO_ADMIN_TEXT_LIMIT)

    return f"{prefix}{html.escape(caption)}{footer}"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    if update.effective_chat and update.effective_chat.type == ChatType.PRIVATE:
        await update.message.reply_text(
            "Отправьте текст или фото, а мы передадим его на модерацию анонимно."
        )


async def handle_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return

    text = update.message.text or ""
    if not text.strip():
        await update.message.reply_text("Пришлите текст сообщения.")
        return

    settings: Settings = context.application.bot_data["settings"]
    store: SubmissionStore = context.application.bot_data["store"]
    username, author_name = user_label(update)

    submission_id = await asyncio.to_thread(
        store.create,
        update.effective_user.id,
        username,
        author_name,
        text.strip(),
    )

    try:
        admin_message = await context.bot.send_message(
            chat_id=settings.admin_chat_id,
            text=build_moderation_text(submission_id, author_name, text.strip()),
            parse_mode=ParseMode.HTML,
            reply_markup=moderation_keyboard(submission_id),
            disable_web_page_preview=True,
        )
    except (Forbidden, BadRequest) as exc:
        logging.exception("Could not send submission to admin chat")
        await update.message.reply_text(
            "Не получилось отправить сообщение админам. Проверьте настройки бота."
        )
        raise exc

    await asyncio.to_thread(store.set_admin_message, submission_id, admin_message.message_id)
    await update.message.reply_text("Сообщение отправлено на модерацию.")


async def handle_private_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None or not update.message.photo:
        return

    caption = (update.message.caption or "").strip()
    photo = update.message.photo[-1]

    settings: Settings = context.application.bot_data["settings"]
    store: SubmissionStore = context.application.bot_data["store"]
    username, author_name = user_label(update)

    submission_id = await asyncio.to_thread(
        store.create,
        update.effective_user.id,
        username,
        author_name,
        caption,
        "photo",
        photo.file_id,
    )

    try:
        admin_message = await context.bot.send_photo(
            chat_id=settings.admin_chat_id,
            photo=photo.file_id,
            caption=build_photo_moderation_caption(submission_id, author_name, caption),
            parse_mode=ParseMode.HTML,
            reply_markup=moderation_keyboard(submission_id),
        )
    except (Forbidden, BadRequest) as exc:
        logging.exception("Could not send photo submission to admin chat")
        await update.message.reply_text(
            "Не получилось отправить фото админам. Проверьте настройки бота."
        )
        raise exc

    await asyncio.to_thread(store.set_admin_message, submission_id, admin_message.message_id)
    await update.message.reply_text("Фото отправлено на модерацию.")


async def handle_unsupported_private_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    await update.message.reply_text("Пока принимаются только текстовые сообщения и фотографии.")


async def is_admin_group_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    settings: Settings = context.application.bot_data["settings"]
    user = update.effective_user
    if user is None:
        return False

    try:
        member = await context.bot.get_chat_member(settings.admin_chat_id, user.id)
    except TelegramError:
        logging.exception("Could not check admin group membership")
        return False

    return member.status in {"creator", "administrator", "member"}


async def handle_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None or update.effective_user is None:
        return

    if not await is_admin_group_member(update, context):
        await query.answer("Недостаточно прав.", show_alert=True)
        return

    action, raw_submission_id = query.data.split(":", maxsplit=1)
    try:
        submission_id = int(raw_submission_id)
    except ValueError:
        await query.answer("Некорректная заявка.", show_alert=True)
        return

    settings: Settings = context.application.bot_data["settings"]
    store: SubmissionStore = context.application.bot_data["store"]
    submission = await asyncio.to_thread(store.get, submission_id)
    if submission is None:
        await query.answer("Заявка не найдена.", show_alert=True)
        return

    if submission["status"] != "pending":
        await query.answer("По этой заявке уже приняли решение.", show_alert=True)
        return

    status = "approved" if action == "approve" else "rejected"
    claimed_status = "publishing" if status == "approved" else status

    decided = await asyncio.to_thread(
        store.decide,
        submission_id,
        claimed_status,
        update.effective_user.id,
    )
    if not decided:
        await query.answer("По этой заявке уже приняли решение.", show_alert=True)
        return

    if status == "approved":
        try:
            if submission["content_type"] == "photo":
                await context.bot.send_photo(
                    chat_id=settings.channel_id,
                    photo=submission["file_id"],
                    caption=submission["text"] or None,
                )
            else:
                await context.bot.send_message(
                    chat_id=settings.channel_id,
                    text=submission["text"],
                    disable_web_page_preview=True,
                )
        except (Forbidden, BadRequest) as exc:
            logging.exception("Could not publish submission to channel")
            await asyncio.to_thread(store.set_status, submission_id, "pending")
            await query.answer("Не получилось отправить в канал.", show_alert=True)
            raise exc
        await asyncio.to_thread(store.set_status, submission_id, "approved")

    verdict = "Одобрено" if status == "approved" else "Отклонено"
    moderator = update.effective_user.mention_html()

    if query.message is not None:
        if submission["content_type"] == "photo":
            new_caption = build_photo_moderation_caption(
                submission_id,
                submission["author_name"],
                submission["text"],
                footer=f"\n\n<b>{verdict}</b>: {moderator}",
            )
            await query.message.edit_caption(
                caption=new_caption,
                parse_mode=ParseMode.HTML,
            )
        else:
            new_text = (
                f"{build_moderation_text(submission_id, submission['author_name'], submission['text'])}"
                f"\n\n<b>{verdict}</b>: {moderator}"
            )
            await query.message.edit_text(
                text=new_text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

    await query.answer(verdict)


async def cleanup_old_submissions(context: ContextTypes.DEFAULT_TYPE) -> None:
    store: SubmissionStore = context.application.bot_data["store"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    deleted_count = await asyncio.to_thread(store.delete_decided_older_than, cutoff)
    if deleted_count:
        logging.info("Deleted %s old moderated submissions", deleted_count)


def build_application(settings: Settings) -> Application:
    application = Application.builder().token(settings.bot_token).build()
    application.bot_data["settings"] = settings
    application.bot_data["store"] = SubmissionStore(settings.database_path)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_private_text)
    )
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.PHOTO & ~filters.COMMAND, handle_private_photo)
    )
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & ~filters.TEXT & ~filters.COMMAND, handle_unsupported_private_message)
    )
    application.add_handler(
        CallbackQueryHandler(handle_decision, pattern=f"^({APPROVE_PREFIX}|{REJECT_PREFIX})\\d+$")
    )
    application.job_queue.run_repeating(cleanup_old_submissions, interval=86400, first=0)

    return application


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
    settings = load_settings()
    application = build_application(settings)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
