"""Almacén de sesiones en DynamoDB con TTL (reemplazo del dict en memoria)."""
import json
import time
import os
from datetime import date

import boto3
from boto3.dynamodb.conditions import Key

TABLE_NAME = os.getenv("DYNAMODB_TABLE", "chatbot-agendamiento")
SESSION_TTL_SECONDS = 600  # 10 minutos

_table = None


def _get_table():
    global _table
    if _table is None:
        dynamodb = boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1"))
        _table = dynamodb.Table(TABLE_NAME)
    return _table


def _serialize(data: dict) -> str:
    """Serializa data de sesión (convierte date a string)."""
    def default(obj):
        if isinstance(obj, date):
            return {"__date__": obj.isoformat()}
        raise TypeError(f"Not serializable: {type(obj)}")
    return json.dumps(data, default=default)


def _deserialize(raw: str) -> dict:
    """Deserializa data de sesión."""
    def hook(obj):
        if "__date__" in obj:
            from datetime import date as d
            return d.fromisoformat(obj["__date__"])
        return obj
    return json.loads(raw, object_hook=hook)


def get_session(user_id: str) -> dict:
    table = _get_table()
    resp = table.get_item(Key={"PK": "SESSION", "SK": f"USER#{user_id}"})
    if "Item" in resp:
        item = resp["Item"]
        return {"state": item["state"], "data": _deserialize(item.get("data_json", "{}"))}
    return {"state": "IDLE", "data": {}}


def save_session(user_id: str, session: dict):
    table = _get_table()
    ttl = int(time.time()) + SESSION_TTL_SECONDS
    table.put_item(Item={
        "PK": "SESSION",
        "SK": f"USER#{user_id}",
        "state": session["state"],
        "data_json": _serialize(session["data"]),
        "ttl": ttl,
    })


def clear_session(user_id: str):
    table = _get_table()
    table.delete_item(Key={"PK": "SESSION", "SK": f"USER#{user_id}"})
