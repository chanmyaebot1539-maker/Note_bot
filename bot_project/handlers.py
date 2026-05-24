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

WAIT_CMD_NAME, WAIT_MESSAGES = range(2)

OWNER_BADGE = "👑 "


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def get_full_name(user):
    name = user.first_name or ""
    if user.last_name:
        name += f" {user.last_name}"
    return name.strip() or user.username or str(user.id)


async def build_main_menu(user_id: int):
    global_cmds = await db.get_all_global_commands(OWNER_ID)
    rows = []

    # Top row — owner sees Admin Panel, regular users see Config. Main Menu
    top_row = [KeyboardButton("Create Command")]
    if user_id == OWNER_ID:
        top_row.append(KeyboardButton("Admin Panel"))
    else:
        top_row.append(KeyboardButton("Config. Main Menu"))
    rows.append(top_row)

    # Global (owner) commands with 👑 badge
    global_btn_names = [f"{OWNER_BADGE}/{c['command_name']}" for c in global_cmds]
    for i in range(0, len(global_btn_names), 3):
        rows.append(global_btn_names[i:i + 3])

    # For regular users: also show their pinned personal commands
    if user_id != OWNER_ID:
        pinned = await db.get_user_menu_items(user_id)
        if pinned:
            pinned_btns = [f"/{p}" for p in pinned]
            for i in range(0, len(pinned_btns), 3):
                rows.append(pinned_btns[i:i + 3])

        user_cmds = await db.get_user_commands(user_id)
        if user_cmds:
            rows.append(["Custom Commands"])

    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = None):
    user = update.effective_user
    markup = await build_main_menu(user.id)
    msg = text or "Use the menu below to manage and trigger commands."
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
    markup = await build_main_menu(user.id)
    try:
        await update.message.reply_text(
            f"Welcome, {user.first_name or 'there'}!\n"
            f"Your Telegram ID: <code>{user.id}</code>\n\n"
            "Use the menu below to get started.",
            reply_markup=markup,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(e)


# ─── UNIFIED TEXT ROUTER ──────────────────────────────────────────────────────

async def route_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    text = message.text.strip() if message.text else None
    user = update.effective_user

    # ── "Add messages to existing command" mode ──
    adding_cmd = context.user_data.get("adding_to_cmd")
    adding_owner = context.user_data.get("adding_to_owner")
    if adding_cmd and adding_owner:
        await _handle_adding_messages(update, context, text, adding_cmd, adding_owner)
        return

    if not text:
        return

    # ── Menu navigation ──
    if text == "Create Command":
        return await create_command_start(update, context)

    if text == "Admin Panel":
        if user.id == OWNER_ID:
            return await admin_panel(update, context)
        return

    if text == "Config. Main Menu":
        if user.id != OWNER_ID:
            return await config_main_menu(update, context)
        return

    if text == "Custom Commands":
        return await show_user_commands(update, context)

    # ── Command trigger — keyboard buttons send "👑 /cmd" or "/cmd" ──
    # Strip the owner badge if present, then extract the command name
    clean = text.replace(OWNER_BADGE, "").strip()
    if clean.startswith("/"):
        raw = clean[1:].split("@")[0].strip().lower()
        if raw:
            return await trigger_command(update, context, raw)
        return

    await send_main_menu(update, context)


async def trigger_command(update: Update, context: ContextTypes.DEFAULT_TYPE, cmd_name: str = None):
    user = update.effective_user
    message = update.message

    if cmd_name is None:
        raw = (message.text or "").strip().replace(OWNER_BADGE, "")
        cmd_name = raw.lstrip("/").split("@")[0].lower()

    if not cmd_name:
        return

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
            "Bot can reply with one or more messages to this command.\n"
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
        try:
            await db.create_command(user.id, get_full_name(user), cmd_name, msgs)
        except Exception as e:
            logger.error(f"create_command db error: {e}")
            await send_main_menu(update, context, "Error saving command. Try again.")
            return ConversationHandler.END

        await send_main_menu(
            update, context,
            f"Custom command /{cmd_name} was successfully created.\n\n"
            "Use the menu below to create more or trigger existing ones."
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
            await message.reply_text("Continue sending messages or press 'Save'.")
        except Exception as e:
            logger.error(e)
        return

    msg_data = _extract_message_data(message) if message else None
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
            [InlineKeyboardButton("▶ View Command", callback_data=f"viewcmd_{cmd_name}")],
            [InlineKeyboardButton("✏️ Edit Messages", callback_data=f"editcmd_{cmd_name}")],
            [InlineKeyboardButton("📌 Configure Menu", callback_data=f"cfgmenu_{cmd_name}")],
            [InlineKeyboardButton("🗑 Delete Command", callback_data=f"delcmd_{cmd_name}")],
            [InlineKeyboardButton("« Back", callback_data="back_main")],
        ]
        try:
            await query.edit_message_text(
                f"Custom command /{cmd_name}.\n\nHere you can view, edit, or delete this command.",
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
            preview = m.get("content", "")[:50] if m.get("type") == "text" else f"[{m.get('type')}]"
            lines.append(f"{i + 1}. {preview}")
            buttons.append([InlineKeyboardButton(
                f"🗑 Delete message {i + 1}",
                callback_data=f"delmsg_{cmd_name}_{i}"
            )])
        buttons += [
            [InlineKeyboardButton("➕ Add Messages", callback_data=f"addmsg_{cmd_name}")],
            [InlineKeyboardButton("🗑 Delete All Messages", callback_data=f"delmsgall_{cmd_name}")],
            [InlineKeyboardButton("« Back", callback_data=f"mycmd_{cmd_name}")],
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
                query.data = f"editcmd_{cmd_name}"
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
                "Send everything you want to add as a reply to this command, then press 'Save'.",
                reply_markup=save_kb
            )
        except Exception as e:
            logger.error(e)

    elif data.startswith("cfgmenu_"):
        cmd_name = data[len("cfgmenu_"):]
        # This is reached from Custom Commands → command detail → Configure Menu
        # Show option to pin/unpin this command
        pinned = await db.get_user_menu_items(user.id)
        is_pinned = cmd_name in pinned
        toggle_label = "📌 Remove from Menu" if is_pinned else "📌 Add to Main Menu"
        toggle_cb = f"menunpin_{cmd_name}" if is_pinned else f"menupin_{cmd_name}"
        buttons = [
            [InlineKeyboardButton(toggle_label, callback_data=toggle_cb)],
            [InlineKeyboardButton("« Back", callback_data=f"mycmd_{cmd_name}")],
        ]
        status = "✅ This command is pinned to your main menu." if is_pinned else "This command is not in your main menu."
        try:
            await query.edit_message_text(
                f"/{cmd_name} — Menu Settings\n\n{status}",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        except Exception as e:
            logger.error(e)

    elif data.startswith("menupin_"):
        cmd_name = data[len("menupin_"):]
        await db.add_to_user_menu(user.id, cmd_name)
        await query.answer(f"/{cmd_name} added to your menu!", show_alert=False)
        query.data = f"cfgmenu_{cmd_name}"
        await cmd_detail_callback(update, context)

    elif data.startswith("menunpin_"):
        cmd_name = data[len("menunpin_"):]
        await db.remove_from_user_menu(user.id, cmd_name)
        await query.answer(f"/{cmd_name} removed from your menu.", show_alert=False)
        query.data = f"cfgmenu_{cmd_name}"
        await cmd_detail_callback(update, context)

    elif data.startswith("delcmd_"):
        cmd_name = data[len("delcmd_"):]
        buttons = [
            [InlineKeyboardButton("✅ Yes, Delete", callback_data=f"confirmdelcmd_{cmd_name}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"mycmd_{cmd_name}")],
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
        await db.remove_from_user_menu(user.id, cmd_name)
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
    """Only accessible by regular users (not owner). Shows a full menu configuration UI."""
    user = update.effective_user
    if user.id == OWNER_ID:
        return

    pinned = await db.get_user_menu_items(user.id)
    all_user_cmds = await db.get_user_commands(user.id)
    all_cmd_names = {c["command_name"] for c in all_user_cmds}

    buttons = []

    # Currently pinned items — show with remove button
    if pinned:
        for cmd_name in pinned:
            if cmd_name in all_cmd_names:
                buttons.append([
                    InlineKeyboardButton(f"/{cmd_name}", callback_data=f"cfgview_{cmd_name}"),
                    InlineKeyboardButton("❌ Remove", callback_data=f"cfgremove_{cmd_name}"),
                ])
            else:
                # Command was deleted, clean it up
                await db.remove_from_user_menu(user.id, cmd_name)

    # Add new item button
    unpinned = [c["command_name"] for c in all_user_cmds if c["command_name"] not in pinned]
    if unpinned:
        buttons.append([InlineKeyboardButton("➕ Add Menu Item", callback_data="cfgadd_list")])

    buttons.append([InlineKeyboardButton("Go Back", callback_data="back_main")])

    status = (
        "Your pinned commands appear as buttons in your main menu.\n"
        "Add or remove commands below."
        if all_user_cmds else
        "You have no custom commands yet.\nCreate one first using 'Create Command'."
    )

    try:
        await update.message.reply_text(
            f"⚙️ Configure Main Menu\n\n{status}",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except Exception as e:
        logger.error(e)


async def config_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    data = query.data

    if data == "cfgadd_list":
        pinned = await db.get_user_menu_items(user.id)
        all_cmds = await db.get_user_commands(user.id)
        unpinned = [c["command_name"] for c in all_cmds if c["command_name"] not in pinned]
        if not unpinned:
            await query.answer("All your commands are already in the menu.", show_alert=True)
            return
        buttons = [
            [InlineKeyboardButton(f"/{n}", callback_data=f"cfgpin_{n}")]
            for n in unpinned
        ]
        buttons.append([InlineKeyboardButton("« Back", callback_data="cfgback")])
        try:
            await query.edit_message_text(
                "Choose a command to add to your main menu:",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        except Exception as e:
            logger.error(e)

    elif data.startswith("cfgpin_"):
        cmd_name = data[len("cfgpin_"):]
        await db.add_to_user_menu(user.id, cmd_name)
        await query.answer(f"/{cmd_name} added to your menu!")
        await _refresh_config_menu(query, user)

    elif data.startswith("cfgremove_"):
        cmd_name = data[len("cfgremove_"):]
        await db.remove_from_user_menu(user.id, cmd_name)
        await query.answer(f"/{cmd_name} removed from your menu.")
        await _refresh_config_menu(query, user)

    elif data.startswith("cfgview_"):
        cmd_name = data[len("cfgview_"):]
        doc = await db.get_command(user.id, cmd_name)
        if doc:
            await _send_command_messages(query._bot if hasattr(query, '_bot') else context.bot, user.id, doc.get("messages", []))
        else:
            await query.answer("Command not found.", show_alert=True)

    elif data == "cfgback":
        await _refresh_config_menu(query, user)


async def _refresh_config_menu(query, user):
    pinned = await db.get_user_menu_items(user.id)
    all_user_cmds = await db.get_user_commands(user.id)
    all_cmd_names = {c["command_name"] for c in all_user_cmds}

    buttons = []
    valid_pinned = []
    for cmd_name in pinned:
        if cmd_name in all_cmd_names:
            valid_pinned.append(cmd_name)
            buttons.append([
                InlineKeyboardButton(f"/{cmd_name}", callback_data=f"cfgview_{cmd_name}"),
                InlineKeyboardButton("❌ Remove", callback_data=f"cfgremove_{cmd_name}"),
            ])

    unpinned = [c["command_name"] for c in all_user_cmds if c["command_name"] not in valid_pinned]
    if unpinned:
        buttons.append([InlineKeyboardButton("➕ Add Menu Item", callback_data="cfgadd_list")])

    buttons.append([InlineKeyboardButton("Go Back", callback_data="back_main")])

    status = (
        "Your pinned commands appear as buttons in your main menu.\nAdd or remove commands below."
        if all_user_cmds else
        "You have no custom commands yet. Create one first using 'Create Command'."
    )

    try:
        await query.edit_message_text(
            f"⚙️ Configure Main Menu\n\n{status}",
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
            "👑 Admin Panel\n\n"
            "No other users have created commands yet.\n\n"
            "When regular users create their own commands, they will appear here "
            "so you can review and delete inappropriate content."
        )
        return

    buttons = [
        [InlineKeyboardButton(
            f"{u['creator_name']} — {u['count']} cmd{'s' if u['count'] != 1 else ''}",
            callback_data=f"adminuser_{u['_id']}"
        )]
        for u in users
    ]
    buttons.append([InlineKeyboardButton("Go Back", callback_data="back_main")])
    try:
        await update.message.reply_text(
            "👑 Admin Panel\n\nUsers who have created commands — tap a name to manage:",
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
        buttons.append([InlineKeyboardButton("« Back", callback_data="admin_back")])
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
            [InlineKeyboardButton("« Back", callback_data=f"adminuser_{target_id}")],
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
        await db.remove_from_user_menu(target_id, cmd_name)
        await query.answer(f"/{cmd_name} deleted.", show_alert=True)
        cmds = await db.get_user_commands(target_id)
        if not cmds:
            await _admin_back_view(query)
        else:
            buttons = [
                [InlineKeyboardButton(f"/{c['command_name']}", callback_data=f"admincmd_{target_id}_{c['command_name']}")]
                for c in cmds
            ]
            buttons.append([InlineKeyboardButton("« Back", callback_data="admin_back")])
            try:
                await query.edit_message_text(
                    f"Commands by user {target_id}:",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
            except Exception as e:
                logger.error(e)

    elif data == "admin_back":
        await _admin_back_view(query)


async def _admin_back_view(query):
    users = await db.get_all_users_with_commands(OWNER_ID)
    if not users:
        try:
            await query.edit_message_text("👑 Admin Panel — No user commands found.")
        except Exception:
            pass
        return
    buttons = [
        [InlineKeyboardButton(
            f"{u['creator_name']} — {u['count']} cmd{'s' if u['count'] != 1 else ''}",
            callback_data=f"adminuser_{u['_id']}"
        )]
        for u in users
    ]
    buttons.append([InlineKeyboardButton("Go Back", callback_data="back_main")])
    try:
        await query.edit_message_text(
            "👑 Admin Panel — Users with custom commands:",
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

    cfg_prefixes = ("cfgadd_", "cfgpin_", "cfgremove_", "cfgview_", "cfgback")
    if any(data.startswith(p) for p in cfg_prefixes) or data == "cfgback":
        return await config_menu_callback(update, context)

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
        MessageHandler(filters.TEXT, route_message),
        MessageHandler(
            filters.PHOTO | filters.VIDEO | filters.Document.ALL |
            filters.AUDIO | filters.VOICE | filters.Sticker.ALL | filters.ANIMATION,
            route_message
        ),
    ]
