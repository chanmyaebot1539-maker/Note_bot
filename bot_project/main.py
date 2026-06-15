import os
import asyncio
import logging
from aiohttp import web
from telegram import Update
from telegram.ext import ApplicationBuilder
import database as db
from handlers import build_handlers

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT      = int(os.environ.get("PORT", 8080))
OWNER_ID  = int(os.environ.get("OWNER_ID", 0))


async def health(request):
    return web.Response(text="OK")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Health-check server running on port {PORT}")
    return runner


async def error_handler(update: object, context):
    logger.error(f"Unhandled error: {context.error}", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "An internal error occurred. Please try again."
            )
        except Exception:
            pass


async def run_owner_migration():
    """
    OWNER_ID ပြောင်းလဲသွားပါက MongoDB တွင် ဟောင်းသော owner ၏
    commands အားလုံးကို အသစ်သော OWNER_ID သို့ auto-migrate လုပ်သည်။
    """
    if OWNER_ID == 0:
        logger.warning("OWNER_ID is not set (defaulting to 0). Owner commands will not work correctly.")
        return

    stored_owner_id = await db.get_stored_owner_id()

    if stored_owner_id is None:
        logger.info(f"First run — storing OWNER_ID={OWNER_ID} in database.")
        await db.set_setting("active_owner_id", OWNER_ID)
        return

    if stored_owner_id == OWNER_ID:
        logger.info(f"OWNER_ID={OWNER_ID} unchanged. No migration needed.")
        return

    logger.warning(
        f"OWNER_ID changed: {stored_owner_id} → {OWNER_ID}. "
        "Migrating owner commands in MongoDB..."
    )
    count = await db.migrate_owner_commands(stored_owner_id, OWNER_ID)
    logger.info(
        f"Migration complete. {count} command(s) reassigned from "
        f"owner {stored_owner_id} to {OWNER_ID}."
    )


async def main():
    await db.init_db()
    await run_owner_migration()

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_error_handler(error_handler)

    for handler in build_handlers():
        application.add_handler(handler)

    web_runner = await start_web_server()

    await application.initialize()
    await application.start()
    await application.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )

    logger.info("Bot is running...")

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        await web_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
