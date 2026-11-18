"""CLI: python -m knowledge sync|rebuild|search|related|read|status --db DB [--vault DIR] [--include DIR]..."""
import argparse
import json
import os

from . import index, query


def main() -> None:
    p = argparse.ArgumentParser(prog="knowledge")
    p.add_argument("cmd", choices=["sync", "rebuild", "search", "related", "read", "status"])
    p.add_argument("arg", nargs="?")
    p.add_argument("--db", required=True)
    p.add_argument("--vault")
    p.add_argument("--include", action="append", help="vault-relative folder to index; repeatable")
    a = p.parse_args()
    if a.cmd in ("sync", "rebuild"):
        from . import embeddings
        embedder = None if os.environ.get("KNOWLEDGE_SEMANTIC", "on") == "off" else embeddings.Embedder(
            os.environ.get("KNOWLEDGE_MODEL_DIR"))
        out = index.sync(a.vault, a.db, full=a.cmd == "rebuild", include=a.include, embedder=embedder)
    else:
        con = query.open_ro(a.db)
        out = {"search": lambda: query.search(con, a.arg or ""),
               "related": lambda: query.related(con, a.arg),
               "read": lambda: query.read_note(con, a.arg),
               "status": lambda: query.status(con)}[a.cmd]()
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
