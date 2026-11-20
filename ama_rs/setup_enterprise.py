"""Trusted local configuration entry point for initial enterprise setup."""

import argparse
import json

from sqlalchemy.exc import IntegrityError

from ama_rs.db import create_database_engine
from ama_rs.enterprises import get_enterprise, save_enterprise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("show")
    show.add_argument("enterprise")
    save = commands.add_parser("save")
    save.add_argument("settings", type=argparse.FileType("r", encoding="utf-8"))
    save.add_argument("--revision", type=int, required=True)
    save.add_argument("--operator", required=True)
    args = parser.parse_args()

    try:
        with create_database_engine().begin() as connection:
            if args.command == "show":
                entry = get_enterprise(connection, args.enterprise)
                if entry is None:
                    parser.error("Enterprise not found")
                print(json.dumps(dict(entry), ensure_ascii=False, indent=2))
                return
            with args.settings as document:
                settings = json.load(document)
            revision = save_enterprise(connection, settings, args.revision, args.operator)
        print(revision)
    except IntegrityError:
        parser.error("The enterprise ID or iiko ID is already registered")
    except (KeyError, ValueError, TypeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
