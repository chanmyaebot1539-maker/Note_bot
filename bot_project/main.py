import os
import asyncio
import logging
import threading
from quart import Quart
from telegram import Update
from telegram.ext import Application, ApplicationBuilder
import database as db
from handlers import build_handlers

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", 8080))

app = Quart(__name__)


@app.route("/")
async def health():
    return "OK", 200


async def run_web():
    await app.run_task(host="0.0.0.0", port=PORT)


async def error_handler(update: object, context):
    logger.error(f"Unhandled error: {context.error}", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "An internal error occurred. Please try again."
            )
        except Exception:
            pass


async def main():
    await db.init_db()

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_error_handler(error_handler)

    for handler in build_handlers():
        application.add_handler(handler)

    web_task = asyncio.create_task(run_web())

    await application.initialize()
    await application.start()
    await application.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )

    logger.info("Bot is running...")

    try:
        await web_task
    except asyncio.CancelledError:
        pass
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
