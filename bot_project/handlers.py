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
BROADCAST_TARGET, BROADCAST_MSG = range(10, 12)

OWNER_BADGE = "👑 "

# Non-clickable section header labels
HEADER_OWNER = "═══ Bot Owner Commands ═══"
HEADER_USER  = "═══ Your Commands ═══"


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def get_full_name(user):
    name = user.first_name or ""
    if user.last_name:
        name += f" {user.last_name}"
    return name.strip() or user.username or str(user.id)


async def build_main_menu(user_id: int):
    global_cmds = await db.get_all_global_commands(OWNER_ID)
    rows = []

    if user_id == OWNER_ID:
        # ── Owner layout ──────────────────────────────────────────────────────
        rows.append([KeyboardButton("Create Command"), KeyboardButton("Admin Panel")])
        rows.append([KeyboardButton("My Commands")])
        rows.append([KeyboardButton("/userlist"), KeyboardButton("/grouplist"), KeyboardButton("/broadcast")])
        # Owner commands shown as a collapsible section header too
        if global_cmds:
            rows.append([KeyboardButton(HEADER_OWNER)])
    else:
        # ── Regular user layout ───────────────────────────────────────────────
        rows.append([KeyboardButton("Create Command"), KeyboardButton("Config. Main Menu")])

        # Collapsed section: Bot Owner Commands (tap to expand via inline keyboard)
        if global_cmds:
            rows.append([KeyboardButton(HEADER_OWNER)])

        # Collapsed section: Your Commands (tap to expand via inline keyboard)
        user_cmds = await db.get_user_commands(user_id)
        if user_cmds:
            rows.append([KeyboardButton(HEADER_USER)])

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
            logger.error(f"_send_command_messages ({mtype}): {e}")


# ─── START ────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    # Track user
    await db.upsert_user(user.id, get_full_name(user), user.username or "")

    # Track group if applicable
    if chat.type in ("group", "supergroup", "channel"):
        await db.upsert_group(chat.id, chat.title or "", chat.type)

    # Handle referral link: /start ref_12345
    args = context.args or []
    referrer_id = None
    if args and args[0].startswith("ref_"):
        try:
            referrer_id = int(args[0][4:])
        except ValueError:
            pass

    bot_username = (await context.bot.get_me()).username
    markup = await build_main_menu(user.id)

    welcome_text = (
        f"👋 Hello, {user.first_name or 'there'}!\n\n"
        "🤖 <b>What this bot can do:</b>\n"
        "• Create custom commands that reply with text, photos, videos or any file\n"
        "• Owner's commands are available to <b>everyone</b>\n"
        "• Create your <b>own private commands</b> only you can use\n"
        "• Pin your favourite commands to your main menu\n"
        "• Owner can manage and moderate all users' commands\n\n"
        "Use the menu below to get started!"
    )

    inline_buttons = [
        [
            InlineKeyboardButton("➕ Add me to your chat!", url=f"https://t.me/{bot_username}?startgroup=true"),
        ],
        [
            InlineKeyboardButton("🎵 Music bot", url="https://t.me/music100200bot?start=tg"),
            InlineKeyboardButton("🔗 Share bot", callback_data=f"sharebot_{user.id}"),
        ],
    ]

    try:
        await update.message.reply_text(
            welcome_text,
            reply_markup=InlineKeyboardMarkup(inline_buttons),
            parse_mode="HTML"
        )
        await update.message.reply_text(
            "Choose an option from the menu below:",
            reply_markup=markup
        )
    except Exception as e:
        logger.error(e)

    if referrer_id and referrer_id != user.id:
        try:
            await context.bot.send_message(
                referrer_id,
                f"🎉 Someone joined using your referral link!\nUser: {get_full_name(user)}"
            )
        except Exception:
            pass


# ─── UNIFIED TEXT ROUTER ──────────────────────────────────────────────────────

async def route_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    text = message.text.strip() if message.text else None
    user = update.effective_user

    # Track user on every interaction
    await db.upsert_user(user.id, get_full_name(user), user.username or "")

    # ── "Add messages to existing command" mode ──
    adding_cmd = context.user_data.get("adding_to_cmd")
    adding_owner_id = context.user_data.get("adding_to_owner")
    if adding_cmd and adding_owner_id:
        await _handle_adding_messages(update, context, text, adding_cmd, adding_owner_id)
        return

    if not text:
        return

    # ── Section header taps → expand into inline keyboard ──
    if text == HEADER_OWNER:
        return await show_owner_commands_panel(update, context)

    if text == HEADER_USER:
        return await show_my_commands_panel(update, context)

    # ── Menu navigation ──
    if text == "Create Command":
        return await create_command_start(update, context)

    if text == "Admin Panel" and user.id == OWNER_ID:
        return await admin_panel(update, context)

    if text == "My Commands" and user.id == OWNER_ID:
        return await show_user_commands(update, context)

    if text == "Config. Main Menu" and user.id != OWNER_ID:
        return await config_main_menu(update, context)

    if text == "Custom Commands":
        return await show_user_commands(update, context)

    # ── Owner slash-command shortcuts in keyboard ──
    if text in ("/userlist", "/grouplist", "/broadcast") and user.id == OWNER_ID:
        if text == "/userlist":
            return await cmd_userlist(update, context)
        elif text == "/grouplist":
            return await cmd_grouplist(update, context)
        elif text == "/broadcast":
            return await broadcast_start(update, context)

    # ── Command trigger (keyboard buttons or typed /cmd) ──
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
        logger.info(f"Command not found: '{cmd_name}' for user {user.id}")
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
            "Send everything you want as a reply, then press 'Save'.",
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
            f"✅ Custom command /{cmd_name} was successfully created!"
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
        await send_main_menu(update, context, f"✅ Command /{cmd_name} updated successfully.")
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
        await send_main_menu(update, context, "You have no custom commands yet. Use 'Create Command' to add one.")
        return

    buttons = [
        [InlineKeyboardButton(f"/{c['command_name']}", callback_data=f"mycmd_{c['command_name']}")]
        for c in cmds
    ]
    buttons.append([InlineKeyboardButton("« Go Back", callback_data="back_main")])
    label = "👑 Your Global Commands:" if user.id == OWNER_ID else "Your Custom Commands:"
    try:
        await update.message.reply_text(label, reply_markup=InlineKeyboardMarkup(buttons))
    except Exception as e:
        logger.error(e)


async def show_owner_commands_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shown when any user taps the ═══ Bot Owner Commands ═══ header."""
    global_cmds = await db.get_all_global_commands(OWNER_ID)
    if not global_cmds:
        try:
            await update.message.reply_text("No global commands have been created yet.")
        except Exception as e:
            logger.error(e)
        return

    buttons = [
        [InlineKeyboardButton(f"👑 /{c['command_name']}", callback_data=f"runcmd_{c['command_name']}")]
        for c in global_cmds
    ]
    buttons.append([InlineKeyboardButton("✖ Close", callback_data="close_panel")])
    try:
        await update.message.reply_text(
            "👑 <b>Bot Owner Commands</b>\nTap a command to run it:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(e)


async def show_my_commands_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shown when a user taps the ═══ Your Commands ═══ header."""
    user = update.effective_user
    cmds = await db.get_user_commands(user.id)
    if not cmds:
        await send_main_menu(update, context, "You have no custom commands yet. Use 'Create Command' to add one.")
        return

    buttons = [
        [InlineKeyboardButton(f"/{c['command_name']}", callback_data=f"runcmd_own_{user.id}_{c['command_name']}")]
        for c in cmds
    ]
    buttons.append([InlineKeyboardButton("✖ Close", callback_data="close_panel")])
    try:
        await update.message.reply_text(
            "🗂 <b>Your Commands</b>\nTap a command to run it:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(e)


# ─── OWNER COMMANDS ───────────────────────────────────────────────────────────

async def cmd_userlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    users = await db.get_all_users()
    count = len(users)
    if not users:
        await update.effective_message.reply_text("No users have started the bot yet.")
        return

    lines = [f"👥 <b>User List ({count} total)</b>\n"]
    for u in users:
        uname = f"@{u['username']}" if u.get("username") else "—"
        lines.append(f"• <b>{u['name']}</b> | {uname} | ID: <code>{u['user_id']}</code>")

    # Split into chunks of 4096 chars (Telegram limit)
    text = "\n".join(lines)
    for chunk in _split_text(text):
        try:
            await update.effective_message.reply_text(chunk, parse_mode="HTML")
        except Exception as e:
            logger.error(e)


async def cmd_grouplist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    groups = await db.get_all_groups()
    count = len(groups)
    if not groups:
        await update.effective_message.reply_text("The bot has not been added to any groups yet.")
        return

    lines = [f"💬 <b>Group List ({count} total)</b>\n"]
    for g in groups:
        lines.append(f"• <b>{g.get('title', '—')}</b> | {g.get('type', '—')} | ID: <code>{g['chat_id']}</code>")

    text = "\n".join(lines)
    for chunk in _split_text(text):
        try:
            await update.effective_message.reply_text(chunk, parse_mode="HTML")
        except Exception as e:
            logger.error(e)


def _split_text(text: str, limit: int = 4000):
    lines = text.split("\n")
    chunks = []
    current = []
    length = 0
    for line in lines:
        if length + len(line) + 1 > limit:
            chunks.append("\n".join(current))
            current = [line]
            length = len(line)
        else:
            current.append(line)
            length += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


# ─── BROADCAST ────────────────────────────────────────────────────────────────

async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return ConversationHandler.END

    users = await db.get_all_users()
    user_count = len(users)
    buttons = [
        [InlineKeyboardButton(f"📢 All Users ({user_count})", callback_data="bc_all")],
        [InlineKeyboardButton("👤 Specific User", callback_data="bc_choose")],
        [InlineKeyboardButton("❌ Cancel", callback_data="bc_cancel")],
    ]
    try:
        await update.effective_message.reply_text(
            "📢 <b>Broadcast</b>\n\nSend a message to all users, or pick a specific user:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(e)
    return BROADCAST_TARGET


async def broadcast_target_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    if user.id != OWNER_ID:
        return ConversationHandler.END

    data = query.data

    if data == "bc_cancel":
        try:
            await query.edit_message_text("Broadcast cancelled.")
        except Exception:
            pass
        return ConversationHandler.END

    if data == "bc_all":
        context.user_data["bc_target"] = "all"
        try:
            await query.edit_message_text(
                "📢 <b>Broadcast to All Users</b>\n\nNow send the message you want to broadcast.\n"
                "You can send text, photo, video, or any file.\n\nSend /cancel to abort.",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(e)
        return BROADCAST_MSG

    if data == "bc_choose":
        users = await db.get_all_users()
        if not users:
            await query.answer("No users found.", show_alert=True)
            return ConversationHandler.END
        buttons = [
            [InlineKeyboardButton(
                f"{u['name']} ({u['user_id']})",
                callback_data=f"bc_user_{u['user_id']}"
            )]
            for u in users[:50]  # limit to 50 buttons
        ]
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="bc_cancel")])
        try:
            await query.edit_message_text(
                "Choose a user to send the message to:",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        except Exception as e:
            logger.error(e)
        return BROADCAST_TARGET

    if data.startswith("bc_user_"):
        target_id = int(data[len("bc_user_"):])
        context.user_data["bc_target"] = target_id
        try:
            await query.edit_message_text(
                f"📩 <b>Send message to user {target_id}</b>\n\nNow send the message.\n"
                "You can send text, photo, video, or any file.\n\nSend /cancel to abort.",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(e)
        return BROADCAST_MSG

    return BROADCAST_TARGET


async def broadcast_send_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != OWNER_ID:
        return ConversationHandler.END

    message = update.message
    if message.text and message.text.strip() == "/cancel":
        await send_main_menu(update, context, "Broadcast cancelled.")
        return ConversationHandler.END

    target = context.user_data.get("bc_target")
    if not target:
        await send_main_menu(update, context, "Broadcast target lost. Please try again.")
        return ConversationHandler.END

    if target == "all":
        users = await db.get_all_users()
        targets = [u["user_id"] for u in users]
    else:
        targets = [int(target)]

    sent = 0
    failed = 0
    for uid in targets:
        try:
            await _forward_or_copy(context.bot, message, uid)
            sent += 1
        except Exception as e:
            logger.warning(f"Broadcast failed for {uid}: {e}")
            failed += 1

    await send_main_menu(
        update, context,
        f"📢 Broadcast complete!\n✅ Sent: {sent}\n❌ Failed: {failed}"
    )
    return ConversationHandler.END


async def _forward_or_copy(bot, message, chat_id: int):
    """Copy message to target chat preserving media."""
    if message.text:
        await bot.send_message(chat_id, message.text, parse_mode=None)
    elif message.photo:
        await bot.send_photo(chat_id, message.photo[-1].file_id, caption=message.caption)
    elif message.video:
        await bot.send_video(chat_id, message.video.file_id, caption=message.caption)
    elif message.document:
        await bot.send_document(chat_id, message.document.file_id, caption=message.caption)
    elif message.audio:
        await bot.send_audio(chat_id, message.audio.file_id, caption=message.caption)
    elif message.voice:
        await bot.send_voice(chat_id, message.voice.file_id)
    elif message.sticker:
        await bot.send_sticker(chat_id, message.sticker.file_id)
    elif message.animation:
        await bot.send_animation(chat_id, message.animation.file_id, caption=message.caption)
    else:
        await bot.forward_message(chat_id, message.chat_id, message.message_id)


# ─── CONFIG MAIN MENU ──────────────────────────────────────────────────────────

async def config_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id == OWNER_ID:
        return

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
                InlineKeyboardButton("❌", callback_data=f"cfgremove_{cmd_name}"),
            ])

    unpinned = [c["command_name"] for c in all_user_cmds if c["command_name"] not in valid_pinned]
    if unpinned:
        buttons.append([InlineKeyboardButton("➕ Add Menu Item", callback_data="cfgadd_list")])

    buttons.append([InlineKeyboardButton("« Go Back", callback_data="back_main")])

    status = (
        "Commands you pin here appear as buttons in your main menu.\nTap ❌ to remove, or ➕ to add."
        if all_user_cmds else
        "You have no custom commands yet. Use 'Create Command' to add one."
    )

    try:
        await update.message.reply_text(
            f"⚙️ <b>Configure Main Menu</b>\n\n{status}",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="HTML"
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
        await query.answer(f"/{cmd_name} added!")
        await _refresh_config_menu(query, user)

    elif data.startswith("cfgremove_"):
        cmd_name = data[len("cfgremove_"):]
        await db.remove_from_user_menu(user.id, cmd_name)
        await query.answer(f"/{cmd_name} removed.")
        await _refresh_config_menu(query, user)

    elif data.startswith("cfgview_"):
        cmd_name = data[len("cfgview_"):]
        doc = await db.get_command(user.id, cmd_name)
        if doc:
            await _send_command_messages(context.bot, user.id, doc.get("messages", []))
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
                InlineKeyboardButton("❌", callback_data=f"cfgremove_{cmd_name}"),
            ])

    unpinned = [c["command_name"] for c in all_user_cmds if c["command_name"] not in valid_pinned]
    if unpinned:
        buttons.append([InlineKeyboardButton("➕ Add Menu Item", callback_data="cfgadd_list")])
    buttons.append([InlineKeyboardButton("« Go Back", callback_data="back_main")])

    status = (
        "Commands you pin here appear as buttons in your main menu."
        if all_user_cmds else
        "You have no custom commands yet."
    )
    try:
        await query.edit_message_text(
            f"⚙️ <b>Configure Main Menu</b>\n\n{status}",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(e)


# ─── CMD DETAIL CALLBACKS ─────────────────────────────────────────────────────

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

    if data == "close_panel":
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    # ── Run owner global command from panel ──
    if data.startswith("runcmd_own_"):
        # format: runcmd_own_{user_id}_{cmd_name}
        rest = data[len("runcmd_own_"):]
        parts = rest.split("_", 1)
        try:
            owner_id = int(parts[0])
            cmd_name = parts[1]
        except (IndexError, ValueError):
            await query.answer("Invalid command.", show_alert=True)
            return
        doc = await db.get_command(owner_id, cmd_name)
        if doc:
            await _send_command_messages(context.bot, query.message.chat_id, doc.get("messages", []))
        else:
            await query.answer("Command not found.", show_alert=True)
        return

    if data.startswith("runcmd_"):
        cmd_name = data[len("runcmd_"):]
        doc = await db.get_global_command(OWNER_ID, cmd_name)
        if doc:
            await _send_command_messages(context.bot, query.message.chat_id, doc.get("messages", []))
        else:
            await query.answer("Command not found.", show_alert=True)
        return

    if data.startswith("sharebot_"):
        uid = int(data[len("sharebot_"):])
        bot_username = (await context.bot.get_me()).username
        ref_link = f"https://t.me/{bot_username}?start=ref_{uid}"
        try:
            await query.answer(f"Your referral link:\n{ref_link}", show_alert=True)
        except Exception as e:
            logger.error(e)
        try:
            await context.bot.send_message(
                uid,
                f"🔗 <b>Your Share Link:</b>\n<code>{ref_link}</code>\n\n"
                "Share this link! When someone starts the bot through it, you'll be notified.",
                parse_mode="HTML"
            )
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
                f"Custom command /{cmd_name}.\n\nView, edit, or delete this command.",
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
            [InlineKeyboardButton("🗑 Delete All", callback_data=f"delmsgall_{cmd_name}")],
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
                "Send the messages you want to add, then press 'Save'.",
                reply_markup=save_kb
            )
        except Exception as e:
            logger.error(e)

    elif data.startswith("cfgmenu_"):
        cmd_name = data[len("cfgmenu_"):]
        pinned = await db.get_user_menu_items(user.id)
        is_pinned = cmd_name in pinned
        toggle_label = "📌 Remove from Menu" if is_pinned else "📌 Add to Main Menu"
        toggle_cb = f"menunpin_{cmd_name}" if is_pinned else f"menupin_{cmd_name}"
        status = "✅ Pinned to your main menu." if is_pinned else "Not in your main menu."
        buttons = [
            [InlineKeyboardButton(toggle_label, callback_data=toggle_cb)],
            [InlineKeyboardButton("« Back", callback_data=f"mycmd_{cmd_name}")],
        ]
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
        await query.answer(f"/{cmd_name} added to your menu!")
        query.data = f"cfgmenu_{cmd_name}"
        await cmd_detail_callback(update, context)

    elif data.startswith("menunpin_"):
        cmd_name = data[len("menunpin_"):]
        await db.remove_from_user_menu(user.id, cmd_name)
        await query.answer(f"/{cmd_name} removed from menu.")
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
            await query.edit_message_text(f"✅ Command /{cmd_name} has been deleted.")
        except Exception as e:
            logger.error(e)
        try:
            await context.bot.send_message(user.id, "Main menu:", reply_markup=markup)
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
            "👑 <b>Admin Panel</b>\n\n"
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
    buttons.append([InlineKeyboardButton("« Go Back", callback_data="back_main")])
    try:
        await update.message.reply_text(
            "👑 <b>Admin Panel</b>\n\nUsers who have created commands:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="HTML"
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
    buttons.append([InlineKeyboardButton("« Go Back", callback_data="back_main")])
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

    if any(data.startswith(p) for p in ("adminuser_", "admincmd_", "admindelcmd_", "admin_back")):
        return await admin_callback(update, context)

    if any(data.startswith(p) for p in ("cfgadd_", "cfgpin_", "cfgremove_", "cfgview_")) or data == "cfgback":
        return await config_menu_callback(update, context)

    if any(data.startswith(p) for p in ("bc_all", "bc_choose", "bc_cancel", "bc_user_")):
        return await broadcast_target_callback(update, context)

    return await cmd_detail_callback(update, context)


# ─── HANDLER REGISTRATION ─────────────────────────────────────────────────────

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

    broadcast_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^/broadcast$"), broadcast_start),
            CommandHandler("broadcast", broadcast_start),
        ],
        states={
            BROADCAST_TARGET: [CallbackQueryHandler(broadcast_target_callback)],
            BROADCAST_MSG: [MessageHandler(~filters.COMMAND, broadcast_send_message)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conv),
        ],
        allow_reentry=True,
    )

    return [
        CommandHandler("start", start),
        CommandHandler("userlist", cmd_userlist),
        CommandHandler("grouplist", cmd_grouplist),
        create_conv,
        broadcast_conv,
        CallbackQueryHandler(callback_router),
        MessageHandler(filters.TEXT, route_message),
        MessageHandler(
            filters.PHOTO | filters.VIDEO | filters.Document.ALL |
            filters.AUDIO | filters.VOICE | filters.Sticker.ALL | filters.ANIMATION,
            route_message
        ),
    ]
