import hashlib
import hmac
import json
import os
from datetime import UTC, datetime
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from sqlalchemy import delete, select
from ama_rs.db import create_database_engine
from ama_rs.enterprises import enterprises, history, save_enterprise
from ama_rs.integrations import receipts, run_bot_command
from ama_rs.messengers import normalize
from ama_rs.personnel import people

settings = {
    "id": "webhook-test", "iiko_id": "webhook-test", "name": "Demo", "timezone": "Europe/Moscow",
    "format": "delivery", "server_address": "server.invalid", "lm_address": "lm.invalid",
    "contact_mode": "independent", "final_route": "headquarters", "brands": ["Demo"],
    "chats": {"enterprise": {"kind": "enterprise", "id": "1", "platform": "telegram", "calendar": "always"}},
    "grants": [{"chat": "enterprise", "role": "service_account", "user": "2"}],
}
engine = create_database_engine()
with engine.begin() as conn:
    save_enterprise(conn, settings, 0, "test")
    example = normalize("telegram", {"update_id": 88, "message": {"text": "/menu", "from": {"id": 2}, "chat": {"id": 1}}})
    assert run_bot_command(conn, example, datetime.now(UTC)).buttons
    assert run_bot_command(conn, example, datetime.now(UTC)) is None
try:
    payload = json.dumps({"id": "webhook-event", "enterprise_id": settings["iiko_id"], "type": "staff.updated",
                          "data": {"employee_id": "iiko-1", "name": "Test", "profile": "kitchen"}}).encode()
    signed = hmac.new(os.environ["IIKO_WEBHOOK_SECRET"].encode(), payload, hashlib.sha256).hexdigest()
    for signature in ("incorrect", signed, signed):
        req = Request("http://127.0.0.1:8080/webhooks/iiko", data=payload, method="POST",
                      headers={"X-AMA-Signature": signature})
        try:
            with urlopen(req, timeout=5) as response:
                assert signature == signed and response.status == 200
        except HTTPError as exc:
            assert signature == "incorrect" and exc.code == 403
    with engine.connect() as conn:
        assert conn.execute(select(people.c.iiko_id).where(
            people.c.enterprise_id == settings["id"])).scalars().all() == ["iiko-1"]
finally:
    with engine.begin() as conn:
        conn.execute(delete(people).where(people.c.enterprise_id == settings["id"]))
        conn.execute(delete(history).where(history.c.enterprise_id == settings["id"]))
        conn.execute(delete(enterprises).where(enterprises.c.id == settings["id"]))
        key = hashlib.sha256(json.dumps([settings["id"], "webhook-event"]).encode()).hexdigest()
        conn.execute(delete(receipts).where(receipts.c.event_id.in_([key, "88"])))
print("HTTP signature validation, iiko event deduplication, and Telegram command deduplication verified")
