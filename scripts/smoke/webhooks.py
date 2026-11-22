import hashlib
import hmac
import json
import os
from datetime import UTC, datetime
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from sqlalchemy import delete, select
from ama_rs.db import create_database_engine
from ama_rs.enterprises import enterprises, history, save_enterprise
from ama_rs.integrations import receipts, run_bot_command
from ama_rs.messengers import Document, normalize, send_document, send_message
from ama_rs.personnel import people

settings = {
    "id": "webhook-test", "iiko_id": "webhook-test", "name": "Demo", "timezone": "Europe/Moscow",
    "format": "delivery", "server_address": "server.invalid", "lm_address": "lm.invalid",
    "contact_mode": "independent", "final_route": "headquarters", "brands": ["Demo"],
    "chats": {"enterprise": {"kind": "enterprise", "id": "1", "platform": "telegram", "calendar": "always"}},
    "grants": [{"chat": "enterprise", "role": "service_account", "user": "2"}],
}
example_max = normalize("max", {"update_type": "message_created", "message": {
    "sender": {"user_id": 2}, "recipient": {"chat_id": 1}, "body": {
        "mid": "max-photo", "text": "/photo card step slot", "attachments": [
            {"type": "file", "payload": {"token": "not-an-image"}},
            {"type": "image", "payload": {"token": "photo-token"}},
        ],
    },
}})
assert example_max.actor.chat_id == "1" and example_max.file_id == "photo-token"
with patch("ama_rs.messengers._post", return_value={"message": {"body": {"mid": "sent-photo"}}}) as post:
    assert send_message("max", "test", {"id": "1", "kind": "group"}, "Чек-лист",
                        photo=example_max.file_id, buttons=(("Открыть", "card"),)) == "sent-photo"
    body = post.call_args.args[1]
    assert body["attachments"][0] == {"type": "image", "payload": {"token": "photo-token"}}
    assert body["attachments"][1]["type"] == "inline_keyboard"
not_ready = HTTPError("https://platform-api.max.ru/messages", 400, "Pending", {},
                      BytesIO(b'{"code":"attachment.not.ready"}'))
with (patch("ama_rs.messengers._post", side_effect=[
          {"url": "https://upload.max.ru/test"}, not_ready, {"message": {"body": {"mid": "csv"}}}
      ]) as post, patch("ama_rs.messengers._read_response", return_value={"token": "csv-token"}) as upload,
      patch("ama_rs.messengers.sleep") as wait):
    send_document("max", "test", {"id": "2", "kind": "personal"}, Document("refunds.csv", b"one,two"))
    upload_request = upload.call_args.args[0]
    assert upload_request.full_url == "https://upload.max.ru/test"
    assert b'name="data"; filename="refunds.csv"' in upload_request.data
    assert b"one,two" in upload_request.data and not upload_request.has_header("Authorization")
    assert post.call_args.args == (
        "https://platform-api.max.ru/messages?user_id=2",
        {"attachments": [{"type": "file", "payload": {"token": "csv-token"}}]},
        {"Authorization": "test"},
    )
    wait.assert_called_once_with(0.25)
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
