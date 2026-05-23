import os
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING


MONGODB_URI = os.environ.get("MONGODB_URI", "")

client: AsyncIOMotorClient = None
db = None
commands_col = None


async def init_db():
    global client, db, commands_col
    client = AsyncIOMotorClient(MONGODB_URI)
    db = client["notebot"]
    commands_col = db["commands"]
    await commands_col.create_index(
        [("creator_id", ASCENDING), ("command_name", ASCENDING)],
        unique=True
    )


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
