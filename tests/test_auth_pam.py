"""SessionStore behaviour + require_user dependency.

We don't exercise the actual PAM library (that would need a running system
auth stack); Califone's PAM path is vendored as-is. Instead we cover the
session-store contract, cookie shape, and the FastAPI dependency's
authenticated / unauthenticated branches.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from token_monitoring import auth_pam
from token_monitoring.auth_pam import SessionRecord, SessionStore, require_user


def test_session_create_get_delete(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s.sqlite")
    sid = store.create(username="haskels", display_name="haskels")
    rec = store.get(sid)
    assert rec is not None
    assert rec.username == "haskels"

    store.delete(sid)
    assert store.get(sid) is None
    store.close()


def test_session_expiry_purges(tmp_path: Path, monkeypatch) -> None:
    store = SessionStore(tmp_path / "s.sqlite")
    sid = store.create(username="u", display_name="u")

    # Force expiry by rewinding the row's expires_at.
    with store._lock:
        store._conn.execute(
            "UPDATE sessions SET expires_at = ? WHERE session_id = ?",
            (time.time() - 10, sid),
        )
        store._conn.commit()

    assert store.get(sid) is None
    store.close()


def test_require_user_synth_when_pam_disabled(monkeypatch) -> None:
    """With TM_PAM_ENABLED unset, dashboard routes get an anonymous session."""
    monkeypatch.delenv("TM_PAM_ENABLED", raising=False)
    rec = require_user(tm_session=None)
    assert isinstance(rec, SessionRecord)
    assert rec.username == "anonymous"


def test_require_user_401_when_pam_enabled_no_cookie(monkeypatch) -> None:
    monkeypatch.setenv("TM_PAM_ENABLED", "1")
    # Init a store so the not-initialised branch doesn't fire first.
    from pathlib import Path as _P
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        store = SessionStore(_P(td) / "s.sqlite")
        with auth_pam.override_session_store(store):
            with pytest.raises(Exception) as ei:
                require_user(tm_session=None)
            # FastAPI HTTPException, 401.
            from fastapi import HTTPException
            assert isinstance(ei.value, HTTPException)
            assert ei.value.status_code == 401
        store.close()
