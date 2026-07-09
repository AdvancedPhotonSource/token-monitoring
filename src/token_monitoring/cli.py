"""CLI entry point.

Subcommands:
  serve      Run the FastAPI + static-file service via uvicorn.
  label-key  Insert/update the display name for a user_hash.
  init-db    Create the SQLite schema without starting the server.
  hash-key   Print the SHA-256 hash for a given API key (helper for
             label-key when you know the plaintext).
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys

from token_monitoring import config


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="token-monitoring",
        description="Argo LLM gateway proxy with per-user token accounting + dashboard.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    srv = sub.add_parser("serve", help="Run the proxy + dashboard (uvicorn).")
    srv.add_argument("--host", default=config.host())
    srv.add_argument("--port", type=int, default=config.port())
    srv.add_argument("--log-level", default=config.log_level())
    srv.add_argument("--reload", action="store_true")
    srv.add_argument("--ssl-keyfile", default=config.ssl_keyfile())
    srv.add_argument("--ssl-certfile", default=config.ssl_certfile())

    lk = sub.add_parser("label-key",
                        help="Set the display name for a user_hash (or plaintext key).")
    lk.add_argument("key", help="API key plaintext OR SHA-256 hash. Plaintext is hashed for you.")
    lk.add_argument("display_name", help="Human-readable label for the dashboard.")

    sub.add_parser("init-db", help="Create the SQLite schema and exit.")

    hk = sub.add_parser("hash-key", help="Print SHA-256 of an API key (for admin lookups).")
    hk.add_argument("key")

    return p


def _load_env() -> None:
    """Load env files if present. Silent if python-dotenv isn't installed.

    Explicit search order (no walk-up — a stray ~/.env with shell syntax
    would trigger dotenv parse warnings that don't help anyone):
      1. ~/.token_monitoring/env   (per-deploy secrets, matches serve_with_restart.sh)
      2. ./.env                    (dev convenience)
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    from pathlib import Path
    candidates = [
        Path.home() / ".token_monitoring" / "env",
        Path.cwd() / ".env",
    ]
    for p in candidates:
        if p.is_file():
            load_dotenv(dotenv_path=p, override=False)


def main(argv: list[str] | None = None) -> int:
    _load_env()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _build_parser().parse_args(argv)

    if args.cmd == "serve":
        return _cmd_serve(args)
    if args.cmd == "label-key":
        return _cmd_label_key(args)
    if args.cmd == "init-db":
        return _cmd_init_db()
    if args.cmd == "hash-key":
        return _cmd_hash_key(args)
    return 2


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    kwargs: dict = dict(
        host=args.host,
        port=int(args.port),
        log_level=args.log_level,
        reload=bool(args.reload),
    )
    if args.ssl_keyfile and args.ssl_certfile:
        kwargs["ssl_keyfile"] = args.ssl_keyfile
        kwargs["ssl_certfile"] = args.ssl_certfile

    uvicorn.run("token_monitoring.api:app", **kwargs)
    return 0


def _resolve_hash(key: str) -> str:
    # Treat as hash if it's exactly 64 hex chars; else hash it.
    k = key.strip()
    if len(k) == 64 and all(c in "0123456789abcdef" for c in k.lower()):
        return k.lower()
    return hashlib.sha256(k.encode("utf-8")).hexdigest()


def _cmd_label_key(args: argparse.Namespace) -> int:
    from token_monitoring import db as db_mod

    user_hash = _resolve_hash(args.key)
    store = db_mod.init_db(config.db_path())
    store.upsert_label(user_hash, args.display_name)
    print(f"labeled {user_hash} → {args.display_name}")
    db_mod.shutdown_db()
    return 0


def _cmd_init_db() -> int:
    from token_monitoring import db as db_mod

    db_mod.init_db(config.db_path())
    print(f"initialised {config.db_path()}")
    db_mod.shutdown_db()
    return 0


def _cmd_hash_key(args: argparse.Namespace) -> int:
    print(_resolve_hash(args.key))
    return 0


if __name__ == "__main__":
    sys.exit(main())
