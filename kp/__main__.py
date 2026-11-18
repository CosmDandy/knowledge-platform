"""CLI: python -m kp sync|search|related|status --db DB [--vault DIR]"""
import argparse
import json

from . import index, query


def main() -> None:
    p = argparse.ArgumentParser(prog="kp")
    p.add_argument("cmd", choices=["sync", "rebuild", "search", "related", "read", "status"])
    p.add_argument("arg", nargs="?")
    p.add_argument("--db", required=True)
    p.add_argument("--vault")
    a = p.parse_args()
    if a.cmd in ("sync", "rebuild"):
        out = index.sync(a.vault, a.db, full=a.cmd == "rebuild")
    else:
        con = query.open_ro(a.db)
        out = {"search": lambda: query.search(con, a.arg or ""),
               "related": lambda: query.related(con, a.arg),
               "read": lambda: query.read_note(con, a.arg),
               "status": lambda: query.status(con)}[a.cmd]()
    print(json.dumps(out, ensure_ascii=False, indent=2))


main()
