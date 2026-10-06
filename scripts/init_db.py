"""Run locally once before enabling the Telegram webhook. Safe to rerun."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pymongo import MongoClient
from mongo_store import MongoStore


def initialize(db, config):
    hello = db.client.admin.command("hello")
    if not hello.get("setName") and hello.get("msg") != "isdbgrid":
        raise RuntimeError("Transactions require a replica set or sharded cluster; standalone MongoDB is unsupported.")
    for collection in ("bookings", "blocks", "sessions", "guards", "counters", "audit", "outbox", "updates"):
        if collection not in db.list_collection_names():
            db.create_collection(collection)
    MongoStore(db, config)  # Validate configuration before creating guard documents.
    db.bookings.create_index([("staff_id", 1), ("status", 1), ("start", 1), ("end", 1)])
    db.bookings.create_index([("customer_id", 1), ("start", -1)])
    db.bookings.create_index([("status", 1), ("start", 1)])
    db.blocks.create_index([("staff_id", 1), ("start", 1), ("end", 1)])
    db.outbox.create_index([("sent", 1), ("cancelled", 1), ("next_try", 1), ("lease_until", 1)])
    db.updates.create_index("at", expireAfterSeconds=30 * 86400)
    for kind in ("bookings", "blocks"):
        db.counters.update_one({"_id": kind}, {"$setOnInsert": {"value": 0}}, upsert=True)
    for staff in config["staff"]:
        db.guards.update_one({"_id": staff["id"]}, {"$setOnInsert": {"revision": 0}}, upsert=True)
    # Prove transactions work on this deployment before taking appointments.
    with db.client.start_session() as session:
        session.with_transaction(lambda s: db.guards.update_one(
            {"_id": config["staff"][0]["id"]}, {"$inc": {"revision": 1}}, session=s))


if __name__ == "__main__":
    config = json.loads(Path("config.json").read_text(encoding="utf-8"))
    client = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=5000)
    initialize(client[os.environ["MONGODB_DB"]], config)
    print("Database collections, indexes, calendar guards and transaction check ready.")
