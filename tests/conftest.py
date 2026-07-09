"""Shared fixtures. Isolate SQLite files per test in a tmp_path."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point every test at ephemeral SQLite files and a stub upstream URL."""
    monkeypatch.setenv("TM_DB_PATH", str(tmp_path / "usage.sqlite"))
    monkeypatch.setenv("TM_SESSION_DB_PATH", str(tmp_path / "sessions.sqlite"))
    monkeypatch.setenv("TM_UPSTREAM_URL", "http://upstream.test")
    monkeypatch.delenv("TM_PAM_ENABLED", raising=False)
    yield
