import os
import re
import logging
from telegram import (
    Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton,
    KeyboardButton
)
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    MessageHandler, CallbackQueryHandler, filters
)
import database as db

logger = logging.getLogger(__name__)

OWNER_ID = int(os.environ.get("OWNER_ID", 0))

# Conversation states
WAIT_CMD_NAME, WAIT_MESSAGES = range(2)


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def get_full_name(user):
    name = user.first_name or ""
    if user.last_name:
        name += f" {user.last_name}"
    return name.strip() or user.username or str(user.id)


async def build_main_menu(user_id: int):
    global_cmds = await db.get_all_global_commands(OWNER_ID)
    rows = []

    top_row = [KeyboardButton("Create Command")]
    if user_id == OWNER_ID:
        top_row.append(KeyboardButton("Admin Panel"))
    else:
        top_row.append(KeyboardButton("Config. Main Menu"))
    rows.append(top_row)

    btn_names = [f"/{c['command_name']}" for c in global_cmds]
    for i in range(0, len(btn_names), 3):
        rows.append(btn_names[i:i + 3])

    if user_id != OWNER_ID:
        user_cmds = await db.get_user_commands(user_id)
        if user_cmds:
            rows.append(["Custom Commands"])

    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = None):
    user = update.effective_user
    markup = await build_main_menu(user.id)
    msg = text or (
        "Use the menu below to create commands, view your custom commands, "
        "or trigger any of the global commands shown."
    )
    try:
        await update.effective_message.reply_text(msg, reply_markup=markup)
    except Exception as e:
        logger.error(f"send_main_menu error: {e}")


# ─── SEND COMMAND REPLIES ──────────────────────────────────────────────────────

async def _send_command_messages(bot, chat_id: int, messages: list):
    for msg in messages:
        try:
            mtype = msg.get("type")
            caption = msg.get("caption") or None
            if mtype == "text":
                await bot.send_message(chat_id, msg["content"])
            elif mtype == "photo":
                await bot.send_photo(chat_id, msg["content"], caption=caption)
            elif mtype == "video":
                await bot.send_video(chat_id, msg["content"], caption=caption)
            elif mtype == "document":
                await bot.send_document(chat_id, msg["content"], caption=caption)
            elif mtype == "audio":
                await bot.send_audio(chat_id, msg["content"], caption=caption)
            elif mtype == "voice":
                await bot.send_voice(chat_id, msg["content"])
            elif mtype == "sticker":
                await bot.send_sticker(chat_id, msg["content"])
            elif mtype == "animation":
                await bot.send_animation(chat_id, msg["content"], caption=caption)
        except Exception as e:
            logger.error(f"_send_command_messages error ({mtype}): {e}")


# ─── START ────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await send_main_menu(
        update, context,
        f"Welcome{', ' + user.first_name if user.first_name else ''}! "
        "Use the menu below to get started.\n\n"
        f"Your Telegram ID: <code>{user.id}</code>",
    )
    # Edit: send_main_menu doesn't support parse_mode, send directly
    markup = await build_main_menu(user.id)
    try:
        await update.message.reply_text(
            f"Welcome! Your Telegram ID is <code>{user.id}</code>.\n"
            "Use the menu below to get started.",
            reply_markup=markup,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(e)


# ─── UNIFIED TEXT ROUTER ──────────────────────────────────────────────────────

async def route_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Single entry point for ALL text messages (both plain text and /commands)."""
    message = update.message
    if not message or not message.text:
        return

    text = message.text.strip()
    user = update.effective_user

    # ── Check if we're in "add messages to existing command" mode ──
    adding_cmd = context.user_data.get("adding_to_cmd")
    adding_owner = context.user_data.get("adding_to_owner")
    if adding_cmd and adding_owner:
        await _handle_adding_messages(update, context, text, adding_cmd, adding_owner)
        return

    # ── Menu navigation ──
    if text == "Create Command":
        return await create_command_start(update, context)

    if text == "Admin Panel":
        if user.id == OWNER_ID:
            return await admin_panel(update, context)
        return

    if text == "Config. Main Menu":
        return await config_main_menu(update, context)

    if text == "Custom Commands":
        return await show_user_commands(update, context)

    # ── Command trigger (keyboard buttons like /love OR typed /love) ──
    if text.startswith("/"):
        raw = text[1:].split("@")[0].strip().lower()
        if raw:
            return await trigger_command(update, context, raw)
        return

    # ── Fallback: show main menu ──
    await send_main_menu(update, context)


async def trigger_command(update: Update, context: ContextTypes.DEFAULT_TYPE, cmd_name: str = None):
    user = update.effective_user
    message = update.message

    if cmd_name is None:
        raw = message.text.strip() if message.text else ""
        cmd_name = raw.lstrip("/").split("@")[0].lower()

    if not cmd_name:
        return

    # Look up global (owner) command first, then user's private command
    doc = await db.get_global_command(OWNER_ID, cmd_name)
    if not doc:
        doc = await db.get_command(user.id, cmd_name)

    if not doc:
        logger.info(f"No command found: '{cmd_name}' for user {user.id} / owner {OWNER_ID}")
        return

    await _send_command_messages(context.bot, message.chat_id, doc.get("messages", []))


# ─── CREATE COMMAND FLOW ───────────────────────────────────────────────────────

async def create_command_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    cancel_kb = ReplyKeyboardMarkup([["Cancel"]], resize_keyboard=True)
    try:
        await update.effective_message.reply_text(
            "Enter the command name. Please use only Latin letters, numbers and '_'.\n\n"
            "Some examples:\n/website\n/pricelist\n/contacts\n/best_music\n/best_photos",
            reply_markup=cancel_kb
        )
    except Exception as e:
        logger.error(e)
    return WAIT_CMD_NAME


async def received_cmd_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == "Cancel":
        await send_main_menu(update, context, "Cancelled.")
        return ConversationHandler.END

    if not re.match(r'^[a-zA-Z0-9_]+$', text):
        try:
            await update.message.reply_text(
                "Invalid name. Use only Latin letters, numbers, and '_'. Try again:"
            )
        except Exception as e:
            logger.error(e)
        return WAIT_CMD_NAME

    context.user_data["cmd_name"] = text.lower()
    context.user_data["messages"] = []

    save_kb = ReplyKeyboardMarkup(
        [["Add Question"], ["Enable Random-message Mode"], ["Save"], ["Cancel"]],
        resize_keyboard=True
    )
    try:
        await update.message.reply_text(
            "Bot can reply with one or more messages to a custom command.\n"
            "You can use text, pictures, videos or any other file type.\n\n"
            "Send everything you want as a reply to this command, then press 'Save'.",
            reply_markup=save_kb
        )
    except Exception as e:
        logger.error(e)
    return WAIT_MESSAGES


async def collect_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    text = message.text.strip() if message.text else None

    if text == "Cancel":
        await send_main_menu(update, context, "Cancelled.")
        return ConversationHandler.END

    if text == "Save":
        msgs = context.user_data.get("messages", [])
        if not msgs:
            try:
                await message.reply_text("Please send at least one message before saving.")
            except Exception as e:
                logger.error(e)
            return WAIT_MESSAGES

        user = update.effective_user
        cmd_name = context.user_data["cmd_name"]
        creator_name = get_full_name(user)

        try:
            await db.create_command(user.id, creator_name, cmd_name, msgs)
        except Exception as e:
            logger.error(f"create_command db error: {e}")
            await send_main_menu(update, context, "Error saving command. Try again.")
            return ConversationHandler.END

        await send_main_menu(
            update, context,
            f"Custom command /{cmd_name} was successfully created.\n\n"
            "Use the menu below to create more commands or trigger existing ones."
        )
        return ConversationHandler.END

    if text in ("Add Question", "Enable Random-message Mode"):
        try:
            await message.reply_text("Feature noted. Continue sending messages or press 'Save'.")
        except Exception as e:
            logger.error(e)
        return WAIT_MESSAGES

    msg_data = _extract_message_data(message)
    if msg_data:
        context.user_data["messages"].append(msg_data)
        count = len(context.user_data["messages"])
        try:
            await message.reply_text(f"Message added ({count} total). Send more or press 'Save'.")
        except Exception as e:
            logger.error(e)
    return WAIT_MESSAGES


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await send_main_menu(update, context, "Cancelled.")
    return ConversationHandler.END


def _extract_message_data(message):
    if message.text:
        return {"type": "text", "content": message.text}
    elif message.photo:
        return {"type": "photo", "content": message.photo[-1].file_id, "caption": message.caption or ""}
    elif message.video:
        return {"type": "video", "content": message.video.file_id, "caption": message.caption or ""}
    elif message.document:
        return {"type": "document", "content": message.document.file_id, "caption": message.caption or ""}
    elif message.audio:
        return {"type": "audio", "content": message.audio.file_id, "caption": message.caption or ""}
    elif message.voice:
        return {"type": "voice", "content": message.voice.file_id}
    elif message.sticker:
        return {"type": "sticker", "content": message.sticker.file_id}
    elif message.animation:
        return {"type": "animation", "content": message.animation.file_id, "caption": message.caption or ""}
    return None


# ─── ADD MESSAGES TO EXISTING COMMAND ─────────────────────────────────────────

async def _handle_adding_messages(update, context, text, cmd_name, owner_id):
    message = update.message

    if text == "Cancel":
        context.user_data.pop("adding_to_cmd", None)
        context.user_data.pop("adding_to_owner", None)
        context.user_data.pop("new_msgs_buffer", None)
        await send_main_menu(update, context, "Cancelled.")
        return

    if text == "Save":
        new_msgs = context.user_data.pop("new_msgs_buffer", [])
        doc = await db.get_command(owner_id, cmd_name)
        existing = doc.get("messages", []) if doc else []
        await db.update_command_messages(owner_id, cmd_name, existing + new_msgs)
        context.user_data.pop("adding_to_cmd", None)
        context.user_data.pop("adding_to_owner", None)
        await send_main_menu(update, context, f"Command /{cmd_name} was successfully updated.")
        return

    if text in ("Add Question", "Enable Random-message Mode"):
        try:
            await message.reply_text("Feature noted. Continue sending messages or press 'Save'.")
        except Exception as e:
            logger.error(e)
        return

    msg_data = _extract_message_data(message)
    if msg_data:
        buf = context.user_data.get("new_msgs_buffer", [])
        buf.append(msg_data)
        context.user_data["new_msgs_buffer"] = buf
        try:
            await message.reply_text(f"Message added ({len(buf)} new). Send more or press 'Save'.")
        except Exception as e:
            logger.error(e)


# ─── USER CUSTOM COMMANDS ──────────────────────────────────────────────────────

async def show_user_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    cmds = await db.get_user_commands(user.id)
    if not cmds:
        await send_main_menu(update, context, "You have no custom commands yet.")
        return

    buttons = [
        [InlineKeyboardButton(f"/{c['command_name']}", callback_data=f"mycmd_{c['command_name']}")]
        for c in cmds
    ]
    buttons.append([InlineKeyboardButton("Go Back", callback_data="back_main")])
    try:
        await update.message.reply_text(
            "Your custom commands:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except Exception as e:
        logger.error(e)


async def cmd_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    data = query.data

    if data == "back_main":
        markup = await build_main_menu(user.id)
        try:
            await query.message.delete()
        except Exception:
            pass
        try:
            await context.bot.send_message(user.id, "Main menu:", reply_markup=markup)
        except Exception as e:
            logger.error(e)
        return

    if data.startswith("mycmd_"):
        cmd_name = data[len("mycmd_"):]
        buttons = [
            [InlineKeyboardButton("View Command", callback_data=f"viewcmd_{cmd_name}")],
            [InlineKeyboardButton("Edit Messages", callback_data=f"editcmd_{cmd_name}")],
            [InlineKeyboardButton("Configure Menu", callback_data=f"cfgmenu_{cmd_name}")],
            [InlineKeyboardButton("Delete Command", callback_data=f"delcmd_{cmd_name}")],
            [InlineKeyboardButton("Back", callback_data="back_main")],
        ]
        try:
            await query.edit_message_text(
                f"Custom command /{cmd_name}.\n\n"
                "Here you can view, edit, or delete this command.",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        except Exception as e:
            logger.error(e)

    elif data.startswith("viewcmd_"):
        cmd_name = data[len("viewcmd_"):]
        doc = await db.get_command(user.id, cmd_name)
        if not doc:
            doc = await db.get_global_command(OWNER_ID, cmd_name)
        if doc:
            await _send_command_messages(context.bot, user.id, doc.get("messages", []))
        else:
            await query.answer("Command not found.", show_alert=True)

    elif data.startswith("editcmd_"):
        cmd_name = data[len("editcmd_"):]
        doc = await db.get_command(user.id, cmd_name)
        if not doc and user.id == OWNER_ID:
            doc = await db.get_global_command(OWNER_ID, cmd_name)
        if not doc:
            await query.answer("Command not found.", show_alert=True)
            return

        msgs = doc.get("messages", [])
        lines = []
        buttons = []
        for i, m in enumerate(msgs):
            preview = m.get("content", "")[:60] if m.get("type") == "text" else f"[{m.get('type')}]"
            lines.append(f"{i+1}. {preview}")
            buttons.append([InlineKeyboardButton(
                f"🗑 Delete message {i+1}",
                callback_data=f"delmsg_{cmd_name}_{i}"
            )])
        buttons += [
            [InlineKeyboardButton("Add Messages to Command", callback_data=f"addmsg_{cmd_name}")],
            [InlineKeyboardButton("Delete All Messages", callback_data=f"delmsgall_{cmd_name}")],
            [InlineKeyboardButton("Go Back", callback_data=f"mycmd_{cmd_name}")],
        ]
        body = "\n".join(lines) if lines else "No messages yet."
        try:
            await query.edit_message_text(body, reply_markup=InlineKeyboardMarkup(buttons))
        except Exception as e:
            logger.error(e)

    elif data.startswith("delmsg_"):
        parts = data.split("_", 2)
        cmd_name = parts[1]
        idx = int(parts[2])
        doc = await db.get_command(user.id, cmd_name)
        if doc:
            msgs = doc.get("messages", [])
            if 0 <= idx < len(msgs):
                msgs.pop(idx)
                await db.update_command_messages(user.id, cmd_name, msgs)
                await query.answer("Message deleted.")
                # Refresh edit view
                fake_data = f"editcmd_{cmd_name}"
                query.data = fake_data
                await cmd_detail_callback(update, context)

    elif data.startswith("delmsgall_"):
        cmd_name = data[len("delmsgall_"):]
        await db.update_command_messages(user.id, cmd_name, [])
        await query.answer("All messages deleted.")
        query.data = f"editcmd_{cmd_name}"
        await cmd_detail_callback(update, context)

    elif data.startswith("addmsg_"):
        cmd_name = data[len("addmsg_"):]
        context.user_data["adding_to_cmd"] = cmd_name
        context.user_data["adding_to_owner"] = user.id
        save_kb = ReplyKeyboardMarkup([["Save"], ["Cancel"]], resize_keyboard=True)
        try:
            await query.message.reply_text(
                "Send everything that you want to add as a reply to this command, then press 'Save'.",
                reply_markup=save_kb
            )
        except Exception as e:
            logger.error(e)

    elif data.startswith("cfgmenu_"):
        cmd_name = data[len("cfgmenu_"):]
        buttons = [
            [InlineKeyboardButton("+ Add Menu Item +", callback_data=f"addmenuitem_{cmd_name}")],
            [InlineKeyboardButton("Go Back", callback_data=f"mycmd_{cmd_name}")],
        ]
        try:
            await query.edit_message_text(
                "Customize your menu layout. Add items from your commands.",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        except Exception as e:
            logger.error(e)

    elif data.startswith("addmenuitem_"):
        cmd_name = data[len("addmenuitem_"):]
        cmds = await db.get_user_commands(user.id)
        btns = [
            [InlineKeyboardButton(f"/{c['command_name']}", callback_data=f"menuadd_{c['command_name']}")]
            for c in cmds
        ]
        btns.append([InlineKeyboardButton("Go Back", callback_data=f"cfgmenu_{cmd_name}")])
        try:
            await query.edit_message_text(
                "Choose a command to add to the menu:",
                reply_markup=InlineKeyboardMarkup(btns)
            )
        except Exception as e:
            logger.error(e)

    elif data.startswith("menuadd_"):
        await query.answer("Menu item noted.", show_alert=False)

    elif data.startswith("delcmd_"):
        cmd_name = data[len("delcmd_"):]
        buttons = [
            [InlineKeyboardButton("Yes, Delete", callback_data=f"confirmdelcmd_{cmd_name}")],
            [InlineKeyboardButton("Cancel", callback_data=f"mycmd_{cmd_name}")],
        ]
        try:
            await query.edit_message_text(
                f"Are you sure you want to delete /{cmd_name}?",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        except Exception as e:
            logger.error(e)

    elif data.startswith("confirmdelcmd_"):
        cmd_name = data[len("confirmdelcmd_"):]
        await db.delete_command(user.id, cmd_name)
        markup = await build_main_menu(user.id)
        try:
            await query.edit_message_text(f"Command /{cmd_name} has been deleted.")
        except Exception as e:
            logger.error(e)
        try:
            await context.bot.send_message(user.id, "Main menu:", reply_markup=markup)
        except Exception as e:
            logger.error(e)


# ─── CONFIG MAIN MENU ──────────────────────────────────────────────────────────

async def config_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    cmds = await db.get_user_commands(user.id)
    buttons = [[InlineKeyboardButton("+ Add Menu Item +", callback_data="cfgmenu_root")]]
    for c in cmds:
        buttons.append([InlineKeyboardButton(f"/{c['command_name']}", callback_data=f"cfgitem_{c['command_name']}")])
    buttons.append([InlineKeyboardButton("Go Back", callback_data="back_main")])
    try:
        await update.message.reply_text(
            "Customize your menu layout. Select a command to configure it.",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except Exception as e:
        logger.error(e)


# ─── ADMIN PANEL ──────────────────────────────────────────────────────────────

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    users = await db.get_all_users_with_commands(OWNER_ID)
    if not users:
        await send_main_menu(
            update, context,
            "Admin Panel\n\nNo other users have created commands yet.\n\n"
            "When regular users create commands, they will appear here so you can review or delete them."
        )
        return

    buttons = [
        [InlineKeyboardButton(
            f"{u['creator_name']} ({u['count']} cmd{'s' if u['count'] != 1 else ''})",
            callback_data=f"adminuser_{u['_id']}"
        )]
        for u in users
    ]
    buttons.append([InlineKeyboardButton("Go Back", callback_data="back_main")])
    try:
        await update.message.reply_text(
            "Admin Panel — Users with custom commands:\n"
            "Tap a user to see and manage their commands.",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except Exception as e:
        logger.error(e)


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    if user.id != OWNER_ID:
        return

    data = query.data

    if data.startswith("adminuser_"):
        target_id = int(data[len("adminuser_"):])
        cmds = await db.get_user_commands(target_id)
        if not cmds:
            await query.answer("No commands found for this user.", show_alert=True)
            return
        buttons = [
            [InlineKeyboardButton(f"/{c['command_name']}", callback_data=f"admincmd_{target_id}_{c['command_name']}")]
            for c in cmds
        ]
        buttons.append([InlineKeyboardButton("Go Back", callback_data="admin_back")])
        try:
            await query.edit_message_text(
                f"Commands by user {target_id}:",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        except Exception as e:
            logger.error(e)

    elif data.startswith("admincmd_"):
        parts = data.split("_", 2)
        target_id = int(parts[1])
        cmd_name = parts[2]
        buttons = [
            [InlineKeyboardButton("🗑 Delete This Command", callback_data=f"admindelcmd_{target_id}_{cmd_name}")],
            [InlineKeyboardButton("Go Back", callback_data=f"adminuser_{target_id}")],
        ]
        try:
            await query.edit_message_text(
                f"Command /{cmd_name} by user {target_id}:",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        except Exception as e:
            logger.error(e)

    elif data.startswith("admindelcmd_"):
        parts = data.split("_", 2)
        target_id = int(parts[1])
        cmd_name = parts[2]
        await db.delete_command(target_id, cmd_name)
        await query.answer(f"/{cmd_name} deleted.", show_alert=True)
        cmds = await db.get_user_commands(target_id)
        if not cmds:
            await admin_back_view(query)
        else:
            buttons = [
                [InlineKeyboardButton(f"/{c['command_name']}", callback_data=f"admincmd_{target_id}_{c['command_name']}")]
                for c in cmds
            ]
            buttons.append([InlineKeyboardButton("Go Back", callback_data="admin_back")])
            try:
                await query.edit_message_text(
                    f"Commands by user {target_id}:",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
            except Exception as e:
                logger.error(e)

    elif data == "admin_back":
        await admin_back_view(query)


async def admin_back_view(query):
    users = await db.get_all_users_with_commands(OWNER_ID)
    if not users:
        try:
            await query.edit_message_text("Admin Panel — No user commands found.")
        except Exception:
            pass
        return
    buttons = [
        [InlineKeyboardButton(
            f"{u['creator_name']} ({u['count']} cmd{'s' if u['count'] != 1 else ''})",
            callback_data=f"adminuser_{u['_id']}"
        )]
        for u in users
    ]
    buttons.append([InlineKeyboardButton("Go Back", callback_data="back_main")])
    try:
        await query.edit_message_text(
            "Admin Panel — Users with custom commands:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except Exception as e:
        logger.error(e)


# ─── COMBINED CALLBACK ROUTER ──────────────────────────────────────────────────

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    admin_prefixes = ("adminuser_", "admincmd_", "admindelcmd_", "admin_back")
    if any(data.startswith(p) for p in admin_prefixes):
        return await admin_callback(update, context)
    return await cmd_detail_callback(update, context)


# ─── HANDLER REGISTRATION ─────────────────────────────────────────────────────

def build_handlers():
    create_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^Create Command$"), create_command_start),
            CommandHandler("createcommand", create_command_start),
        ],
        states={
            WAIT_CMD_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_cmd_name)
            ],
            WAIT_MESSAGES: [
                MessageHandler(~filters.COMMAND, collect_messages)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conv),
            MessageHandler(filters.Regex("^Cancel$"), cancel_conv),
        ],
        allow_reentry=True,
    )

    return [
        CommandHandler("start", start),
        create_conv,
        CallbackQueryHandler(callback_router),
        # Single unified router handles ALL text including /commands from keyboard
        MessageHandler(filters.TEXT, route_message),
        # Also handle non-text media (photos/videos/docs) for the "add messages" flow
        MessageHandler(
            filters.PHOTO | filters.VIDEO | filters.Document.ALL |
            filters.AUDIO | filters.VOICE | filters.Sticker.ALL | filters.ANIMATION,
            route_message
        ),
    ]
