"""Telegram and MAX webhook normalization and outbound API clients."""

import json
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ama_rs.enterprises import Actor


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post(url: str, body: dict, headers: dict | None = None) -> dict:
    request = Request(url, data=json.dumps(body, ensure_ascii=False).encode(), method="POST",
                      headers={"Content-Type": "application/json"} | (headers or {}))
    with build_opener(NoRedirect()).open(request, timeout=10) as response:
        result = json.load(response)
    if result.get("ok") is False:
        raise RuntimeError("Messenger did not confirm the request")
    return result


@dataclass(frozen=True)
class Message:
    event_id: str
    actor: Actor
    text: str
    file_id: str | None = None


def normalize(platform: str, payload: dict) -> Message | None:
    if platform == "telegram":
        message = payload.get("message")
        if not message or message.get("from", {}).get("is_bot"):
            return None
        text = message.get("text") or message.get("caption")
        if not isinstance(text, str):
            return None
        photo = message.get("photo")
        return Message(str(payload["update_id"]), Actor(
            platform, str(message["from"]["id"]), str(message["chat"]["id"]),
            str(message["message_thread_id"]) if message.get("message_thread_id") is not None else None,
        ), text, photo[-1]["file_id"] if photo else None)
    if platform == "max" and payload.get("update_type") == "message_created":
        message = payload["message"]
        if message.get("sender", {}).get("is_bot") or not isinstance(message["body"].get("text"), str):
            return None
        user_id = str(message["sender"]["user_id"])
        recipient = message["recipient"]
        chat_id = user_id if recipient.get("chat_type") == "dialog" else str(recipient["chat_id"])
        return Message(str(message["body"]["mid"]), Actor(platform, user_id, chat_id), message["body"]["text"])
    return None


def send_message(platform: str, token: str, chat: dict, text: str, *, quiet: bool = False,
                 photo: str | None = None) -> str:
    if platform == "telegram":
        body = {"chat_id": chat["id"], "disable_notification": quiet}
        if chat.get("topic_id") not in (None, "1"):
            body["message_thread_id"] = int(chat["topic_id"])
        method = "sendPhoto" if photo else "sendMessage"
        body.update({"photo": photo, "caption": text[:900]} if photo else {"text": text})
        return str(_post(f"https://api.telegram.org/bot{token}/{method}", body)["result"]["message_id"])
    if platform == "max":
        query = urlencode({"user_id" if chat["kind"] == "personal" else "chat_id": chat["id"]})
        return str(_post(f"https://platform-api.max.ru/messages?{query}",
                         {"text": text, "notify": not quiet}, {"Authorization": token})["message"]["body"]["mid"])
    raise ValueError("Unsupported messenger")


def definitely_rejected(error: HTTPError) -> bool:
    return error.code in {400, 401, 403, 404, 413}
