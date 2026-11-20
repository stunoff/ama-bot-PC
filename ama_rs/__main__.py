import json
import os
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sqlalchemy.exc import SQLAlchemyError

from ama_rs.db import create_database_engine
from ama_rs.integrations import ingest_iiko, run_bot_command, valid_signature
from ama_rs.messengers import normalize, send_message
from ama_rs.processes import ProcessConflict

WEBHOOKS = {
    "/webhooks/telegram": ("telegram", "X-Telegram-Bot-Api-Secret-Token"),
    "/webhooks/max": ("max", "X-Max-Bot-Api-Secret"),
    "/webhooks/iiko": ("iiko", "X-AMA-Signature"),
}


def main() -> None:
    engine = create_database_engine()

    class Handler(BaseHTTPRequestHandler):
        def reply(self, code: int, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path != "/healthz":
                self.reply(404, b"Not found\n")
                return

            try:
                with engine.connect() as connection:
                    connection.exec_driver_sql("SELECT 1")
            except SQLAlchemyError:
                self.reply(503, b"Database unavailable\n")
                return

            self.reply(200, b"ok\n")

        def do_POST(self) -> None:
            webhook = WEBHOOKS.get(self.path)
            if webhook is None:
                self.reply(404, b"Not found\n")
                return
            source, header = webhook
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024 * 1024:
                    raise ValueError
            except ValueError:
                self.reply(400, b"Invalid request length\n")
                return
            raw = self.rfile.read(length)
            if not valid_signature(source, raw, self.headers.get(header),
                                   os.environ.get(f"{source.upper()}_WEBHOOK_SECRET")):
                self.reply(403, b"Unauthorized\n")
                return
            now = datetime.now(UTC)
            try:
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError
                if source == "iiko":
                    with engine.begin() as connection:
                        ingest_iiko(connection, payload, now)
                elif (message := normalize(source, payload)) is not None:
                    with engine.begin() as connection:
                        response = run_bot_command(connection, message, now)
                    token = os.environ.get(f"{source.upper()}_BOT_TOKEN")
                    if response and token:
                        actor = message.actor
                        send_message(source, token, {
                            "id": actor.chat_id, "topic_id": actor.topic_id,
                            "kind": "personal" if actor.chat_id == actor.user_id else "group",
                        }, response)
            except ProcessConflict:
                self.reply(409, b"Event requires configuration or prior state\n")
                return
            except (ValueError, KeyError, TypeError, AttributeError):
                self.reply(422, b"Invalid event\n")
                return
            except (SQLAlchemyError, OSError, RuntimeError):
                self.reply(503, b"Service temporarily unavailable\n")
                return
            self.reply(200, b"ok\n")

        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            # Avoid writing request data, including untrusted URL paths, to logs.
            return

    with ThreadingHTTPServer(("0.0.0.0", 8080), Handler) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
