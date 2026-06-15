import os
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING


MONGODB_URI = os.environ.get("MONGODB_URI", "")

client: AsyncIOMotorClient = None
db = None
commands_col = None
user_menus_col = None
users_col = None
groups_col = None
settings_col = None


async def init_db():
    global client, db, commands_col, user_menus_col, users_col, groups_col, settings_col
    client = AsyncIOMotorClient(MONGODB_URI)
    db = client["notebot"]
    commands_col  = db["commands"]
    user_menus_col = db["user_menus"]
    users_col     = db["bot_users"]
    groups_col    = db["bot_groups"]
    settings_col  = db["bot_settings"]
    await commands_col.create_index(
        [("creator_id", ASCENDING), ("command_name", ASCENDING)],
        unique=True
    )
    await user_menus_col.create_index("user_id", unique=True)
    await users_col.create_index("user_id", unique=True)
    await groups_col.create_index("chat_id", unique=True)
    await settings_col.create_index("key", unique=True)


# ─── COMMANDS ─────────────────────────────────────────────────────────────────

async def get_command(creator_id: int, command_name: str):
    return await commands_col.find_one(
        {"creator_id": creator_id, "command_name": command_name}
    )


async def get_global_command(owner_id: int, command_name: str):
    return await commands_col.find_one(
        {"creator_id": owner_id, "command_name": command_name}
    )


async def get_all_global_commands(owner_id: int):
    cursor = commands_col.find({"creator_id": owner_id}, {"command_name": 1})
    return await cursor.to_list(length=None)


async def get_user_commands(creator_id: int):
    cursor = commands_col.find({"creator_id": creator_id}, {"command_name": 1})
    return await cursor.to_list(length=None)


async def create_command(creator_id: int, creator_name: str, command_name: str, messages: list):
    await commands_col.update_one(
        {"creator_id": creator_id, "command_name": command_name},
        {"$set": {
            "creator_id": creator_id,
            "creator_name": creator_name,
            "command_name": command_name,
            "messages": messages
        }},
        upsert=True
    )


async def update_command_messages(creator_id: int, command_name: str, messages: list):
    await commands_col.update_one(
        {"creator_id": creator_id, "command_name": command_name},
        {"$set": {"messages": messages}}
    )


async def delete_command(creator_id: int, command_name: str):
    await commands_col.delete_one(
        {"creator_id": creator_id, "command_name": command_name}
    )


async def get_total_command_count() -> int:
    return await commands_col.count_documents({})


async def get_all_users_with_commands(owner_id: int):
    pipeline = [
        {"$match": {"creator_id": {"$ne": owner_id}}},
        {"$group": {
            "_id": "$creator_id",
            "creator_name": {"$first": "$creator_name"},
            "count": {"$sum": 1}
        }}
    ]
    cursor = commands_col.aggregate(pipeline)
    return await cursor.to_list(length=None)


# ─── USER MENU CONFIG ──────────────────────────────────────────────────────────

async def get_user_menu_items(user_id: int) -> list:
    doc = await user_menus_col.find_one({"user_id": user_id})
    return doc.get("items", []) if doc else []


async def add_to_user_menu(user_id: int, command_name: str):
    await user_menus_col.update_one(
        {"user_id": user_id},
        {"$addToSet": {"items": command_name}},
        upsert=True
    )


async def remove_from_user_menu(user_id: int, command_name: str):
    await user_menus_col.update_one(
        {"user_id": user_id},
        {"$pull": {"items": command_name}}
    )


# ─── USER & GROUP TRACKING ────────────────────────────────────────────────────

async def upsert_user(user_id: int, name: str, username: str):
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"name": name, "username": username},
         "$setOnInsert": {"user_id": user_id}},
        upsert=True
    )


async def get_all_users() -> list:
    cursor = users_col.find({}, {"user_id": 1, "name": 1, "username": 1})
    return await cursor.to_list(length=None)


async def get_user_count() -> int:
    return await users_col.count_documents({})


async def upsert_group(chat_id: int, title: str, chat_type: str):
    await groups_col.update_one(
        {"chat_id": chat_id},
        {"$set": {"title": title, "type": chat_type},
         "$setOnInsert": {"chat_id": chat_id}},
        upsert=True
    )


async def get_all_groups() -> list:
    cursor = groups_col.find({}, {"chat_id": 1, "title": 1, "type": 1})
    return await cursor.to_list(length=None)


async def get_group_count() -> int:
    return await groups_col.count_documents({})


# ─── SETTINGS ────────────────────────────────────────────────────────────────

async def get_setting(key: str, default=None):
    doc = await settings_col.find_one({"key": key})
    return doc["value"] if doc else default


async def set_setting(key: str, value):
    await settings_col.update_one(
        {"key": key},
        {"$set": {"key": key, "value": value}},
        upsert=True
    )


# ─── OWNER ID MIGRATION ───────────────────────────────────────────────────────

async def get_stored_owner_id() -> int | None:
    doc = await settings_col.find_one({"key": "active_owner_id"})
    return doc["value"] if doc else None


async def migrate_owner_commands(old_owner_id: int, new_owner_id: int) -> int:
    """
    MongoDB တွင် old_owner_id ဖြင့် သိမ်းထားသော commands အားလုံးကို
    new_owner_id သို့ ပြောင်းပေးသည်။ ပြောင်းလဲသော commands အရေအတွက် ပြန်သည်။
    """
    result = await commands_col.update_many(
        {"creator_id": old_owner_id},
        {"$set": {"creator_id": new_owner_id, "creator_name": "Bot Owner"}}
    )
    old_menu = await user_menus_col.find_one({"user_id": old_owner_id})
    if old_menu:
        await user_menus_col.update_one(
            {"user_id": old_owner_id},
            {"$set": {"user_id": new_owner_id}},
        )
    await set_setting("active_owner_id", new_owner_id)
    return result.modified_count
