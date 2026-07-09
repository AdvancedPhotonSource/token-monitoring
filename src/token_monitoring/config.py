"""Env-var-driven config. Every knob is read at call time so a restart
picks up a change to `.env` without a code edit.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


_TRUTHY = {"1", "true", "yes", "on"}


def _truthy(name: str, default: str = "") -> bool:
    return os.environ.get(name, default).strip().lower() in _TRUTHY


def upstream_url() -> str:
    return os.environ.get("TM_UPSTREAM_URL", "https://apps.inside.anl.gov/argoapi").rstrip("/")


def upstream_timeout_s() -> float:
    try:
        return float(os.environ.get("TM_UPSTREAM_TIMEOUT_S", "120"))
    except ValueError:
        return 120.0


def db_path() -> Path:
    raw = os.environ.get("TM_DB_PATH", "").strip()
    if not raw:
        # Sensible default for local dev — deploy sets it explicitly.
        raw = str(Path.home() / ".token_monitoring" / "usage.sqlite")
    return Path(raw)


def session_db_path() -> Path:
    raw = os.environ.get("TM_SESSION_DB_PATH", "").strip()
    if not raw:
        raw = str(Path.home() / ".token_monitoring" / "sessions.sqlite")
    return Path(raw)


def host() -> str:
    return os.environ.get("TM_HOST", "0.0.0.0")


def port() -> int:
    try:
        return int(os.environ.get("TM_PORT", "9014"))
    except ValueError:
        return 9014


def log_level() -> str:
    return os.environ.get("TM_LOG_LEVEL", "info")


def ssl_keyfile() -> Optional[str]:
    v = os.environ.get("TM_SSL_KEYFILE", "").strip()
    return v or None


def ssl_certfile() -> Optional[str]:
    v = os.environ.get("TM_SSL_CERTFILE", "").strip()
    return v or None


def pam_enabled() -> bool:
    return _truthy("TM_PAM_ENABLED")
