import os
import re
import logging
from telegram import (
    Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton,
    KeyboardButton, ReplyKeyboardRemove
)
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    MessageHandler, CallbackQueryHandler, filters
)
import database as db

logger = logging.getLogger(__name__)

OWNER_ID = int(os.environ.get("OWNER_ID", 0))

# Conversation states
(
    WAIT_CMD_NAME,
    WAIT_MESSAGES,
    WAIT_EDIT_MESSAGES,
    ADMIN_USER_LIST,
) = range(4)


def get_full_name(user):
    name = user.first_name or ""
    if user.last_name:
        name += f" {user.last_name}"
    return name.strip() or user.username or str(user.id)


async def build_main_menu(user_id: int):
    global_cmds = await db.get_all_global_commands()
    rows = []

    top_row = [KeyboardButton("Create Command")]
    if user_id == OWNER_ID:
        top_row.append(KeyboardButton("Admin Panel"))
    else:
        top_row.append(KeyboardButton("Config. Main Menu"))
    rows.append(top_row)

    btn_names = [f"/{c['command_name']}" for c in global_cmds]
    for i in range(0, len(btn_names), 3):
        rows.append(btn_names[i:i+3])

    if user_id != OWNER_ID:
        user_cmds = await db.get_user_commands(user_id)
        if user_cmds:
            rows.append(["Custom Commands"])

    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = None):
    user = update.effective_user
    markup = await build_main_menu(user.id)
    msg = text or (
        "You can create custom commands that your bot can reply to with predefined messages. "
        "Use the menu below to create new custom commands, change the look of the bot's menu or select a command to edit it."
    )
    try:
        await update.effective_message.reply_text(msg, reply_markup=markup)
    except Exception as e:
        logger.error(f"send_main_menu error: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_main_menu(update, context, "Welcome! Use the menu below to get started.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user = update.effective_user

    if text == "Create Command":
        return await create_command_start(update, context)
    elif text == "Admin Panel" and user.id == OWNER_ID:
        return await admin_panel(update, context)
    elif text == "Config. Main Menu":
        return await config_main_menu(update, context)
    elif text == "Custom Commands":
        return await show_user_commands(update, context)
    elif text.startswith("/"):
        cmd = text[1:].split("@")[0].lower()
        return await trigger_command(update, context, cmd)
    else:
        await send_main_menu(update, context)


async def trigger_command(update: Update, context: ContextTypes.DEFAULT_TYPE, cmd_name: str = None):
    user = update.effective_user
    if cmd_name is None:
        cmd_name = update.message.text[1:].split("@")[0].lower()

    doc = await db.get_global_command(cmd_name)
    if not doc:
        doc = await db.get_command(user.id, cmd_name)
    if not doc:
        return

    for msg in doc.get("messages", []):
        try:
            mtype = msg.get("type")
            caption = msg.get("caption", "")
            if mtype == "text":
                await update.message.reply_text(msg["content"])
            elif mtype == "photo":
                await update.message.reply_photo(msg["content"], caption=caption or None)
            elif mtype == "video":
                await update.message.reply_video(msg["content"], caption=caption or None)
            elif mtype == "document":
                await update.message.reply_document(msg["content"], caption=caption or None)
            elif mtype == "audio":
                await update.message.reply_audio(msg["content"], caption=caption or None)
            elif mtype == "voice":
                await update.message.reply_voice(msg["content"], caption=caption or None)
            elif mtype == "sticker":
                await update.message.reply_sticker(msg["content"])
            elif mtype == "animation":
                await update.message.reply_animation(msg["content"], caption=caption or None)
        except Exception as e:
            logger.error(f"trigger_command send error: {e}")


# ─── CREATE COMMAND FLOW ─────────────────────────────────────────────────────

async def create_command_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    cancel_kb = ReplyKeyboardMarkup([["Cancel"]], resize_keyboard=True)
    try:
        await update.effective_message.reply_text(
            "Enter the command name. Please use only latin letters, numbers and '_'.\n\n"
            "Some examples:\n/website\n/pricelist\n/contacts\n/best_music\n/best_photos",
            reply_markup=cancel_kb
        )
    except Exception as e:
        logger.error(f"create_command_start error: {e}")
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
            f"Bot can reply with one or more messages to a custom command. "
            f"You can use text, pictures, videos or any other file type.\n\n"
            f"Send everything that you want to add as a reply to this command and press 'Save'.",
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
            await send_main_menu(
                update, context,
                f"Custom command /{cmd_name} was successfully created.\n\n"
                "You can create custom commands that your bot can reply to with predefined messages. "
                "Use the menu below to create new custom commands, change the look of the bot's menu or select a command to edit it."
            )
        except Exception as e:
            logger.error(f"create_command db error: {e}")
            await send_main_menu(update, context, "Error saving command. Try again.")
        return ConversationHandler.END

    if text in ("Add Question", "Enable Random-message Mode"):
        try:
            await message.reply_text(f"Feature noted. Continue sending messages or press Save.")
        except Exception as e:
            logger.error(e)
        return WAIT_MESSAGES

    msg_data = _extract_message_data(message)
    if msg_data:
        context.user_data["messages"].append(msg_data)
        try:
            await message.reply_text(f"Message added ({len(context.user_data['messages'])} total). Send more or press 'Save'.")
        except Exception as e:
            logger.error(e)
    return WAIT_MESSAGES


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


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await send_main_menu(update, context, "Cancelled.")
    return ConversationHandler.END


# ─── USER CUSTOM COMMANDS ────────────────────────────────────────────────────

async def show_user_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    cmds = await db.get_user_commands(user.id)
    if not cmds:
        await send_main_menu(update, context, "You have no custom commands yet.")
        return

    buttons = [[InlineKeyboardButton(f"/{c['command_name']}", callback_data=f"mycmd_{c['command_name']}")] for c in cmds]
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
        context.user_data["viewing_cmd"] = cmd_name
        buttons = [
            [InlineKeyboardButton("View Command", callback_data=f"viewcmd_{cmd_name}")],
            [InlineKeyboardButton("Edit Messages", callback_data=f"editcmd_{cmd_name}")],
            [InlineKeyboardButton("Configure Menu", callback_data=f"cfgmenu_{cmd_name}")],
            [InlineKeyboardButton("Delete Command", callback_data=f"delcmd_{cmd_name}")],
        ]
        try:
            await query.edit_message_text(
                f"Custom command /{cmd_name}.\n\nHere you can look at the result of a command, delete it or add it to your bot's menu.",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        except Exception as e:
            logger.error(e)

    elif data.startswith("viewcmd_"):
        cmd_name = data[len("viewcmd_"):]
        doc = await db.get_command(user.id, cmd_name)
        if not doc:
            doc = await db.get_global_command(cmd_name)
        if doc:
            for msg in doc.get("messages", []):
                try:
                    mtype = msg.get("type")
                    if mtype == "text":
                        await context.bot.send_message(user.id, msg["content"])
                    elif mtype == "photo":
                        await context.bot.send_photo(user.id, msg["content"], caption=msg.get("caption") or None)
                    elif mtype == "video":
                        await context.bot.send_video(user.id, msg["content"], caption=msg.get("caption") or None)
                    elif mtype == "document":
                        await context.bot.send_document(user.id, msg["content"], caption=msg.get("caption") or None)
                except Exception as e:
                    logger.error(e)

    elif data.startswith("editcmd_"):
        cmd_name = data[len("editcmd_"):]
        doc = await db.get_command(user.id, cmd_name)
        if not doc and user.id == OWNER_ID:
            doc = await db.get_global_command(cmd_name)
        if not doc:
            await query.answer("Command not found.", show_alert=True)
            return

        context.user_data["editing_cmd"] = cmd_name
        context.user_data["editing_owner"] = (user.id == OWNER_ID and not await db.get_command(user.id, cmd_name))
        msgs = doc.get("messages", [])
        text_lines = []
        buttons = []
        for i, m in enumerate(msgs):
            preview = m.get("content", "")[:60] if m.get("type") == "text" else f"[{m.get('type')}]"
            text_lines.append(preview)
            buttons.append([InlineKeyboardButton(
                f"🗑 Press to delete this message: /{cmd_name}_delete{i}",
                callback_data=f"delmsg_{cmd_name}_{i}"
            )])
        buttons += [
            [InlineKeyboardButton("Add Messages to Command", callback_data=f"addmsg_{cmd_name}")],
            [InlineKeyboardButton("Delete All Messages", callback_data=f"delmsgall_{cmd_name}")],
            [InlineKeyboardButton("Go Back", callback_data=f"mycmd_{cmd_name}")],
        ]
        text = "\n".join(text_lines) if text_lines else "No messages."
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        except Exception as e:
            logger.error(e)

    elif data.startswith("delmsg_"):
        parts = data.split("_", 2)
        cmd_name = parts[1]
        idx = int(parts[2])
        doc = await db.get_command(user.id, cmd_name)
        if not doc and user.id == OWNER_ID:
            doc = await db.get_global_command(cmd_name)
        if doc:
            msgs = doc.get("messages", [])
            if 0 <= idx < len(msgs):
                msgs.pop(idx)
                target_id = user.id
                await db.update_command_messages(target_id, cmd_name, msgs)
                await query.answer("Message deleted.")
                await cmd_detail_callback(update, context)

    elif data.startswith("delmsgall_"):
        cmd_name = data[len("delmsgall_"):]
        await db.update_command_messages(user.id, cmd_name, [])
        await query.answer("All messages deleted.")
        context.user_data["viewing_cmd"] = cmd_name
        await cmd_detail_callback(update, context)

    elif data.startswith("addmsg_"):
        cmd_name = data[len("addmsg_"):]
        context.user_data["adding_to_cmd"] = cmd_name
        context.user_data["adding_to_owner"] = user.id
        save_kb = ReplyKeyboardMarkup(
            [["Save"], ["Cancel"]],
            resize_keyboard=True
        )
        try:
            await query.message.reply_text(
                "Send everything that you want to add as a reply to this command and press 'Save'.",
                reply_markup=save_kb
            )
        except Exception as e:
            logger.error(e)

    elif data.startswith("cfgmenu_"):
        cmd_name = data[len("cfgmenu_"):]
        buttons = [
            [InlineKeyboardButton("Add Menu Item +", callback_data=f"addmenuitem_{cmd_name}")],
            [InlineKeyboardButton("Go Back", callback_data=f"mycmd_{cmd_name}")],
        ]
        try:
            await query.edit_message_text(
                "You can customize the user menu layout. Select an element to move, rename or delete it.",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        except Exception as e:
            logger.error(e)

    elif data.startswith("addmenuitem_"):
        cmd_name = data[len("addmenuitem_"):]
        cmds = await db.get_user_commands(user.id)
        btns = [[InlineKeyboardButton(f"/{c['command_name']}", callback_data=f"menuadd_{cmd_name}_{c['command_name']}")] for c in cmds]
        btns.append([InlineKeyboardButton("Go Back", callback_data=f"cfgmenu_{cmd_name}")])
        try:
            await query.edit_message_text(
                "Choose any available command to add it to the menu.",
                reply_markup=InlineKeyboardMarkup(btns)
            )
        except Exception as e:
            logger.error(e)

    elif data.startswith("menuadd_"):
        await query.answer("Menu item added (feature: persistent menu config).")

    elif data.startswith("delcmd_"):
        cmd_name = data[len("delcmd_"):]
        buttons = [
            [InlineKeyboardButton("Yes, Delete", callback_data=f"confirmdelcmd_{cmd_name}")],
            [InlineKeyboardButton("No, Go Back", callback_data=f"mycmd_{cmd_name}")],
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
        await query.answer(f"/{cmd_name} deleted.")
        markup = await build_main_menu(user.id)
        try:
            await query.edit_message_text(f"Command /{cmd_name} has been deleted.")
        except Exception as e:
            logger.error(e)
        try:
            await context.bot.send_message(user.id, "Main menu:", reply_markup=markup)
        except Exception as e:
            logger.error(e)


# ─── ADD MESSAGES (edit flow) ─────────────────────────────────────────────────

async def adding_messages_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    text = message.text.strip() if message.text else None

    cmd_name = context.user_data.get("adding_to_cmd")
    owner_id = context.user_data.get("adding_to_owner")
    if not cmd_name or not owner_id:
        return

    if text == "Cancel":
        context.user_data.pop("adding_to_cmd", None)
        context.user_data.pop("adding_to_owner", None)
        await send_main_menu(update, context, "Cancelled.")
        return

    if text == "Save":
        new_msgs = context.user_data.pop("new_msgs_buffer", [])
        doc = await db.get_command(owner_id, cmd_name)
        existing = doc.get("messages", []) if doc else []
        await db.update_command_messages(owner_id, cmd_name, existing + new_msgs)
        context.user_data.pop("adding_to_cmd", None)
        context.user_data.pop("adding_to_owner", None)
        await send_main_menu(update, context, f"Custom command /{cmd_name} was successfully updated.")
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


# ─── CONFIG MAIN MENU ────────────────────────────────────────────────────────

async def config_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    cmds = await db.get_user_commands(user.id)
    buttons = [[InlineKeyboardButton(f"+ Add Menu Item +", callback_data="cfgmenu_root")]]
    for c in cmds:
        buttons.append([InlineKeyboardButton(f"/{c['command_name']}", callback_data=f"cfgitem_{c['command_name']}")])
    buttons.append([InlineKeyboardButton("Go Back", callback_data="back_main")])
    try:
        await update.message.reply_text(
            "You can customize the user menu layout. Select an element to move, rename or delete it.",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except Exception as e:
        logger.error(e)


# ─── ADMIN PANEL ─────────────────────────────────────────────────────────────

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    users = await db.get_all_users_with_commands()
    if not users:
        await send_main_menu(update, context, "No users have created commands yet.")
        return

    buttons = [
        [InlineKeyboardButton(f"{u['creator_name']} ({u['count']} cmds)", callback_data=f"adminuser_{u['_id']}")]
        for u in users
    ]
    buttons.append([InlineKeyboardButton("Go Back", callback_data="back_main")])
    try:
        await update.message.reply_text(
            "Admin Panel — Users with custom commands:",
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
            await query.answer("No commands found.", show_alert=True)
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
            [InlineKeyboardButton("Delete Command", callback_data=f"admindelcmd_{target_id}_{cmd_name}")],
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
            users = await db.get_all_users_with_commands()
            if not users:
                try:
                    await query.edit_message_text("No more user commands.")
                except Exception:
                    pass
                return
            buttons = [
                [InlineKeyboardButton(f"{u['creator_name']} ({u['count']} cmds)", callback_data=f"adminuser_{u['_id']}")]
                for u in users
            ]
            buttons.append([InlineKeyboardButton("Go Back", callback_data="back_main")])
            try:
                await query.edit_message_text("Admin Panel:", reply_markup=InlineKeyboardMarkup(buttons))
            except Exception as e:
                logger.error(e)
        else:
            buttons = [
                [InlineKeyboardButton(f"/{c['command_name']}", callback_data=f"admincmd_{target_id}_{c['command_name']}")]
                for c in cmds
            ]
            buttons.append([InlineKeyboardButton("Go Back", callback_data="admin_back")])
            try:
                await query.edit_message_text(
                    f"Remaining commands by user {target_id}:",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
            except Exception as e:
                logger.error(e)

    elif data == "admin_back":
        users = await db.get_all_users_with_commands()
        buttons = [
            [InlineKeyboardButton(f"{u['creator_name']} ({u['count']} cmds)", callback_data=f"adminuser_{u['_id']}")]
            for u in users
        ]
        buttons.append([InlineKeyboardButton("Go Back", callback_data="back_main")])
        try:
            await query.edit_message_text("Admin Panel:", reply_markup=InlineKeyboardMarkup(buttons))
        except Exception as e:
            logger.error(e)


# ─── COMBINED CALLBACK ROUTER ─────────────────────────────────────────────────

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user

    admin_prefixes = ("adminuser_", "admincmd_", "admindelcmd_", "admin_back")
    if any(data.startswith(p) for p in admin_prefixes):
        return await admin_callback(update, context)
    return await cmd_detail_callback(update, context)


# ─── HANDLERS REGISTRATION ────────────────────────────────────────────────────

def build_handlers():
    create_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^Create Command$"), create_command_start),
            CommandHandler("createcommand", create_command_start),
        ],
        states={
            WAIT_CMD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_cmd_name)],
            WAIT_MESSAGES: [MessageHandler(~filters.COMMAND, collect_messages)],
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
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & ~filters.Regex("^(Save|Cancel|Add Question|Enable Random-message Mode)$"),
            adding_messages_handler,
            block=False
        ),
        CallbackQueryHandler(callback_router),
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text),
        MessageHandler(filters.COMMAND, trigger_command),
    ]
