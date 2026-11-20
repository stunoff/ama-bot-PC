from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sqlalchemy.exc import SQLAlchemyError

from ama_rs.db import create_database_engine


def main() -> None:
    engine = create_database_engine()

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/healthz":
                self.send_error(404)
                return

            try:
                with engine.connect() as connection:
                    connection.exec_driver_sql("SELECT 1")
            except SQLAlchemyError:
                self.send_error(503, "Database unavailable")
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", "3")
            self.end_headers()
            self.wfile.write(b"ok\n")

        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            if code != 200:
                super().log_request(code, size)

    with ThreadingHTTPServer(("0.0.0.0", 8080), HealthHandler) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
