"""
One-time migration script.
Render Shell တွင် run ပါ:
    cd bot_project
    python fix_migration.py
"""
import asyncio
import os
from motor.motor_asyncio import AsyncIOMotorClient

MONGODB_URI = os.environ.get("MONGODB_URI", "")
NEW_OWNER_ID = int(os.environ.get("OWNER_ID", 0))


async def main():
    if not MONGODB_URI:
        print("ERROR: MONGODB_URI environment variable is not set.")
        return
    if NEW_OWNER_ID == 0:
        print("ERROR: OWNER_ID environment variable is not set.")
        return

    client = AsyncIOMotorClient(MONGODB_URI)
    db = client["notebot"]
    commands_col = db["commands"]
    settings_col = db["bot_settings"]

    print(f"\nCurrent OWNER_ID (new): {NEW_OWNER_ID}")
    print("=" * 50)

    pipeline = [
        {"$group": {"_id": "$creator_id", "count": {"$sum": 1}}}
    ]
    creators = await commands_col.aggregate(pipeline).to_list(None)

    if not creators:
        print("No commands found in database at all.")
        client.close()
        return

    print("\nAll creator_ids found in 'commands' collection:")
    for c in sorted(creators, key=lambda x: x["count"], reverse=True):
        marker = "  ← (current OWNER_ID — skip)" if c["_id"] == NEW_OWNER_ID else ""
        print(f"  creator_id = {c['_id']}  ({c['count']} commands){marker}")

    old_owners = [c for c in creators if c["_id"] != NEW_OWNER_ID]

    if not old_owners:
        print("\nAll commands already belong to the current OWNER_ID. Nothing to migrate.")
        await settings_col.update_one(
            {"key": "active_owner_id"},
            {"$set": {"key": "active_owner_id", "value": NEW_OWNER_ID}},
            upsert=True
        )
        client.close()
        return

    old_owners.sort(key=lambda x: x["count"], reverse=True)
    old_owner = old_owners[0]
    old_owner_id = old_owner["_id"]

    print(f"\nMost likely OLD owner: creator_id={old_owner_id} ({old_owner['count']} commands)")
    print(f"Will migrate: {old_owner_id} → {NEW_OWNER_ID}")
    print("=" * 50)

    result = await commands_col.update_many(
        {"creator_id": old_owner_id},
        {"$set": {"creator_id": NEW_OWNER_ID, "creator_name": "Bot Owner"}}
    )

    await settings_col.update_one(
        {"key": "active_owner_id"},
        {"$set": {"key": "active_owner_id", "value": NEW_OWNER_ID}},
        upsert=True
    )

    print(f"\n✅ Done! {result.modified_count} commands migrated to OWNER_ID={NEW_OWNER_ID}.")
    print("Restart your bot on Render to apply changes.")
    client.close()


asyncio.run(main())
