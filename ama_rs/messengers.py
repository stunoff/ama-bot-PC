"""Telegram and MAX webhook normalization and outbound API clients."""

import json
from dataclasses import dataclass
from time import sleep
from uuid import uuid4
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ama_rs.enterprises import Actor


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post(url: str, body: dict, headers: dict | None = None, *, method: str = "POST") -> dict:
    request = Request(url, data=json.dumps(body, ensure_ascii=False).encode(), method=method,
                      headers={"Content-Type": "application/json"} | (headers or {}))
    return _read_response(request)


def _read_response(request: Request) -> dict:
    with build_opener(NoRedirect()).open(request, timeout=10) as response:
        result = json.load(response)
    if result.get("ok") is False:
        raise RuntimeError("Messenger did not confirm the request")
    return result


@dataclass(frozen=True)
class Document:
    filename: str
    data: bytes


def _document_upload(url: str, field: str, document: Document, fields: dict[str, str]) -> dict:
    boundary = uuid4().hex
    while boundary.encode() in document.data:
        boundary = uuid4().hex
    chunks = []
    for name, value in fields.items():
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    chunks += [
        (f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
         f'filename="{document.filename}"\r\nContent-Type: text/csv\r\n\r\n').encode(),
        document.data, f"\r\n--{boundary}--\r\n".encode(),
    ]
    return _read_response(Request(url,
        data=b"".join(chunks), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}))


def _max_destination(chat: dict) -> str:
    return urlencode({"user_id" if chat["kind"] == "personal" else "chat_id": chat["id"]})


def send_document(platform: str, token: str, chat: dict, document: Document) -> None:
    if platform == "telegram":
        fields = {"chat_id": str(chat["id"])}
        if chat.get("topic_id") not in (None, "1"):
            fields["message_thread_id"] = str(int(chat["topic_id"]))
        _document_upload(f"https://api.telegram.org/bot{token}/sendDocument", "document", document, fields)
        return
    if platform != "max":
        raise ValueError("Unsupported messenger")
    headers = {"Authorization": token}
    upload = _post("https://platform-api.max.ru/uploads?type=file", {}, headers)
    url = urlsplit(upload["url"])
    if url.scheme != "https" or not url.hostname or url.username is not None:
        raise RuntimeError("MAX returned an invalid upload URL")
    result = _document_upload(upload["url"], "data", document, {})
    attachment_token = result.get("token") or upload.get("token")
    if not attachment_token:
        raise RuntimeError("MAX did not confirm the file upload")
    body = {"attachments": [{"type": "file", "payload": {"token": attachment_token}}]}
    target = f"https://platform-api.max.ru/messages?{_max_destination(chat)}"
    for delay in (0.25, 0.5, 1, None):
        try:
            _post(target, body, headers)
            return
        except HTTPError as error:
            try:
                code = json.load(error).get("code")
            except (ValueError, AttributeError):
                code = None
            if code != "attachment.not.ready" or delay is None:
                raise
            sleep(delay)


@dataclass(frozen=True)
class Message:
    event_id: str
    actor: Actor
    text: str
    file_id: str | None = None
    callback_id: str | None = None


def normalize(platform: str, payload: dict) -> Message | None:
    if platform == "telegram":
        callback = payload.get("callback_query")
        if callback:
            message = callback.get("message")
            if not message or not isinstance(callback.get("data"), str):
                return None
            topic = message.get("message_thread_id")
            actor = Actor(platform, str(callback["from"]["id"]), str(message["chat"]["id"]),
                          str(topic) if topic is not None else None)
            return Message(str(payload["update_id"]), actor, callback["data"],
                           callback_id=str(callback["id"]))
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
    if platform == "max" and payload.get("update_type") == "message_callback":
        callback = payload["callback"]
        recipient = payload["message"]["recipient"]
        user_id = str(callback["user"]["user_id"])
        chat_id = user_id if recipient.get("chat_type") == "dialog" else str(recipient["chat_id"])
        callback_id = str(callback["callback_id"])
        return Message(callback_id, Actor(platform, user_id, chat_id), callback["payload"],
                       callback_id=callback_id)
    if platform == "max" and payload.get("update_type") == "message_created":
        message = payload["message"]
        if message.get("sender", {}).get("is_bot") or not isinstance(message["body"].get("text"), str):
            return None
        user_id = str(message["sender"]["user_id"])
        recipient = message["recipient"]
        chat_id = user_id if recipient.get("chat_type") == "dialog" else str(recipient["chat_id"])
        body = message["body"]
        images = [item["payload"]["token"] for item in body.get("attachments") or ()
                  if isinstance(item, dict) and item.get("type") == "image"
                  and isinstance(item.get("payload"), dict)
                  and isinstance(item["payload"].get("token"), str)
                  and 0 < len(item["payload"]["token"]) <= 512]
        return Message(str(body["mid"]), Actor(platform, user_id, chat_id), body["text"],
                       images[0] if images else None)
    return None


def send_message(platform: str, token: str, chat: dict, text: str, *, quiet: bool = False,
                 photo: str | None = None, buttons: tuple[tuple[str, str], ...] = (),
                 reply_to: str | None = None) -> str:
    if platform == "telegram":
        body = {"chat_id": chat["id"], "disable_notification": quiet}
        if chat.get("topic_id") not in (None, "1"):
            body["message_thread_id"] = int(chat["topic_id"])
        if reply_to:
            body["reply_parameters"] = {"message_id": int(reply_to), "allow_sending_without_reply": True}
        if buttons:
            body["reply_markup"] = {"inline_keyboard": [
                [{"text": label, "callback_data": key}] for label, key in buttons
            ]}
        method = "sendPhoto" if photo else "sendMessage"
        body.update({"photo": photo, "caption": text[:900]} if photo else {"text": text})
        return str(_post(f"https://api.telegram.org/bot{token}/{method}", body)["result"]["message_id"])
    if platform == "max":
        query = _max_destination(chat)
        body = {"text": text, "notify": not quiet}
        if reply_to:
            body["link"] = {"type": "reply", "mid": reply_to}
        attachments = []
        if photo:
            attachments.append({"type": "image", "payload": {"token": photo}})
        if buttons:
            attachments.append({"type": "inline_keyboard", "payload": {"buttons": [
                [{"type": "callback", "text": label, "payload": key}] for label, key in buttons
            ]}})
        if attachments:
            body["attachments"] = attachments
        return str(_post(f"https://platform-api.max.ru/messages?{query}",
                         body, {"Authorization": token})["message"]["body"]["mid"])
    raise ValueError("Unsupported messenger")


def report_edit_failure(platform: str, error: HTTPError) -> str | None:
    if error.code not in {400, 403, 404}:
        return None
    try:
        result = json.load(error)
    except (OSError, ValueError):
        return "missing" if platform == "max" and error.code == 404 else None
    if not isinstance(result, dict):
        return None
    if platform == "telegram":
        description = str(result.get("description", "")).lower().replace("’", "'")
        if "message is not modified" in description:
            return "unchanged"
        if "message to edit not found" in description:
            return "missing"
        if "message can't be edited" in description or "message can not be edited" in description:
            return "forbidden"
    if platform == "max":
        code = result.get("code")
        if error.code == 404 or code == "message.not.found":
            return "missing"
        if code in {"message.not.modified", "message.not.changed"}:
            return "unchanged"
        if code in {"message.edit.forbidden", "message.cannot.edit", "message.not.editable"}:
            return "forbidden"
    return None


def report_correction_reference(chat: dict, message_id: str) -> str:
    chat_id = str(chat["id"])
    if (chat["platform"] == "telegram" and chat_id.startswith("-100")
            and chat_id[4:].isascii() and chat_id[4:].isdigit() and message_id.isascii() and message_id.isdigit()):
        return f"https://t.me/c/{chat_id[4:]}/{message_id}"
    return f"сообщение {message_id}"


def definitely_rejected(error: HTTPError) -> bool:
    return error.code in {400, 401, 403, 404, 413}


def acknowledge_callback(platform: str, token: str, callback_id: str) -> None:
    if platform == "telegram":
        _post(f"https://api.telegram.org/bot{token}/answerCallbackQuery",
              {"callback_query_id": callback_id})
    elif platform == "max":
        query = urlencode({"callback_id": callback_id})
        _post(f"https://platform-api.max.ru/answers?{query}", {}, {"Authorization": token})


def edit_message(platform: str, token: str, chat: dict, external_id: str, text: str) -> str:
    if platform == "telegram":
        _post(f"https://api.telegram.org/bot{token}/editMessageText",
              {"chat_id": chat["id"], "message_id": int(external_id), "text": text})
    elif platform == "max":
        query = urlencode({"message_id": external_id})
        _post(f"https://platform-api.max.ru/messages?{query}", {"text": text},
              {"Authorization": token}, method="PUT")
    else:
        raise ValueError("Unsupported messenger")
    return external_id
