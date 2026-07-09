"""Simple ANL-domain-credential sign-in for the token-monitoring dashboard.

Vendored from the Califone dashboard's auth_pam.py — same PAM stack, same
session model, just renamed env vars (CF_* → TM_*) and cookie (cf_session
→ tm_session). Original rationale copied below because it applies here
unchanged; any future update to the ANL auth flow should update Califone
first and re-vendor.

We call PAM directly. ``python-pam`` opens the same libpam.so, uses the
same ``password-auth`` service definition, and gets the same
success/failure signal that SSH's password login does. No secrets in the
app, no protocol code we have to maintain, and any future change to the
ANL auth stack (Kerberos rotation, MFA push, whatever) is picked up by
libpam without touching us.

Design contract
---------------
* One login mode. The user POSTs a username + password to
  ``/auth/login``, we call ``pam.pam().authenticate(...)`` and on a
  True result mint a server-side session cookie. Any failure is a
  generic 401.
* Server-side sessions in SQLite (session id in cookie, everything else
  server-side). Sliding 12 h expiry.
* Dormant unless ``TM_PAM_ENABLED`` is truthy — tests import the module
  cleanly without ``python-pam`` installed because the client is a
  deferred import inside :func:`authenticate`.

Rate limiting: a ``time.sleep`` on failure is defence-in-depth. libpam
itself already imposes a ``pam_faildelay`` (2 s on arecibo), so brute
forcing is naturally slow before our sleep runs. Per-IP counters /
CAPTCHAs would be overkill for an APS-network-only service.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from fastapi import APIRouter, Cookie, HTTPException, Response
from pydantic import BaseModel

_log = logging.getLogger(__name__)


# =========================================================================
# Config — every knob is an env var so flipping values never requires a
# code change. TM_PAM_ENABLED is the master switch.
# =========================================================================


_TRUTHY = {"1", "true", "yes", "on"}


def _env_truthy(name: str, default: str = "") -> bool:
    return os.environ.get(name, default).strip().lower() in _TRUTHY


def pam_enabled() -> bool:
    """Master switch — read at request time so an env flip + restart
    turns sign-in on/off without a code change."""
    return _env_truthy("TM_PAM_ENABLED")


def _pam_service() -> str:
    """PAM service name to authenticate against.

    ``password-auth`` is the standard include target on RHEL/Rocky that
    covers SSHd's password path (and thus arecibo's SSSD → KRB5 stack).
    Override via ``TM_PAM_SERVICE`` if a deploy wants to use a custom
    /etc/pam.d/ file (e.g. one that emits an audit tag).
    """
    return os.environ.get("TM_PAM_SERVICE", "password-auth").strip() or "password-auth"


def _failure_delay_s() -> float:
    try:
        return float(os.environ.get("TM_PAM_FAILURE_DELAY_S", "1.0"))
    except ValueError:
        return 1.0


COOKIE_NAME = "tm_session"
_SESSION_TTL_SECONDS = 12 * 3600  # 12 h sliding window
_SESSION_ID_BYTES = 32             # 256-bit cookie value

# POSIX-safe username characters. libpam itself validates further, but
# refusing anything unusual up-front lets us reject obvious garbage
# without ever handing it to PAM (and keeps the audit log clean).
_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,62}$")


# =========================================================================
# SessionStore — SQLite-backed so server restarts don't log everyone out
# =========================================================================


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    username: str
    display_name: str
    created_at: float
    expires_at: float
    last_seen: float


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    username      TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    created_at    REAL NOT NULL,
    expires_at    REAL NOT NULL,
    last_seen     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_expires  ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_sessions_username ON sessions(username);
"""


class SessionStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, timeout=10.0,
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def create(self, *, username: str, display_name: str) -> str:
        now = time.time()
        sid = secrets.token_urlsafe(_SESSION_ID_BYTES)
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions "
                "  (session_id, username, display_name, "
                "   created_at, expires_at, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (sid, username, display_name,
                 now, now + _SESSION_TTL_SECONDS, now),
            )
            self._conn.commit()
        return sid

    def get(self, session_id: str) -> Optional[SessionRecord]:
        """Return the session iff present and not expired; slide the
        expiry window forward on hit; drop the row on expiry."""
        if not session_id:
            return None
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,),
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] < now:
                self._conn.execute(
                    "DELETE FROM sessions WHERE session_id = ?", (session_id,),
                )
                self._conn.commit()
                return None
            new_expires = now + _SESSION_TTL_SECONDS
            self._conn.execute(
                "UPDATE sessions SET last_seen = ?, expires_at = ? "
                "WHERE session_id = ?",
                (now, new_expires, session_id),
            )
            self._conn.commit()
            return SessionRecord(
                session_id=row["session_id"],
                username=row["username"],
                display_name=row["display_name"],
                created_at=row["created_at"],
                expires_at=new_expires,
                last_seen=now,
            )

    def delete(self, session_id: str) -> None:
        if not session_id:
            return
        with self._lock:
            self._conn.execute(
                "DELETE FROM sessions WHERE session_id = ?", (session_id,),
            )
            self._conn.commit()

    def purge_expired(self) -> int:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM sessions WHERE expires_at < ?", (now,),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# =========================================================================
# Module-level session store wired up by ``init_pam()``
# =========================================================================


_session_store: Optional[SessionStore] = None


def session_store() -> Optional[SessionStore]:
    return _session_store


def init_pam(*, session_db_path: Path) -> APIRouter:
    """Prepare the SessionStore and return the FastAPI router.

    Idempotent — the last caller wins. Callers gate on
    :func:`pam_enabled` before invoking this.
    """
    global _session_store
    _session_store = SessionStore(session_db_path)
    return _build_router()


def shutdown_pam() -> None:
    global _session_store
    if _session_store is not None:
        _session_store.close()
    _session_store = None


# =========================================================================
# PAM auth — the only actual credential check
# =========================================================================


def _valid_username(username: str) -> bool:
    return bool(username) and bool(_USERNAME_RE.match(username))


def authenticate(username: str, password: str) -> bool:
    """Return True iff PAM accepts (*username*, *password*).

    Runs ``pam.pam().authenticate(username, password, service=...)``
    against the configured service (default ``password-auth``). On
    arecibo that routes through SSSD's KRB5 domain to the ANL
    Kerberos KDC; on other hosts it uses whatever the local PAM
    stack is wired to.

    Contained — every exception becomes False so a broken libpam
    binding can't leak internals via a 500 response.
    """
    if not _valid_username(username) or not password:
        return False

    # Deferred import so the module stays importable in test contexts
    # that don't install python-pam. In production it's a required
    # dep; a missing install here surfaces as a loud 500 on first
    # login rather than a silent import error at boot.
    try:
        import pam as pam_mod  # noqa: WPS433
    except ImportError as exc:  # pragma: no cover — install error path
        _log.error("python-pam not installed; cannot authenticate: %s", exc)
        raise RuntimeError("python-pam is required for PAM authentication") from exc

    service = _pam_service()
    try:
        p = pam_mod.pam()
        ok = bool(p.authenticate(username, password, service=service))
        if ok:
            _log.info("PAM auth ok for user=%r via service=%r", username, service)
        else:
            code = getattr(p, "code", None)
            reason = getattr(p, "reason", None)
            _log.info(
                "PAM auth rejected user=%r service=%r code=%r reason=%r",
                username, service, code, reason,
            )
        return ok
    except Exception as exc:  # noqa: BLE001
        _log.warning("PAM auth crashed for user=%r: %s", username, exc)
        return False


# =========================================================================
# Routes: /auth/login, /auth/logout, /auth/whoami
# =========================================================================


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    ok: bool
    username: str
    display_name: str


class WhoamiResponse(BaseModel):
    authenticated: bool
    username: Optional[str] = None
    display_name: Optional[str] = None
    expires_at: Optional[float] = None


class LogoutResponse(BaseModel):
    ok: bool


def _build_router() -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["auth"])

    @router.post("/login", response_model=LoginResponse, include_in_schema=False)
    def login(payload: LoginRequest, response: Response) -> LoginResponse:
        if _session_store is None:
            raise HTTPException(status_code=503, detail="Auth not initialised")

        username = (payload.username or "").strip().lower()
        password = payload.password or ""

        # Shape validation before any I/O — a bad shape is 401 (same wire
        # response as a bad password so we don't leak which names exist).
        if not _valid_username(username):
            time.sleep(_failure_delay_s())
            raise HTTPException(status_code=401, detail="Invalid credentials")

        if not authenticate(username, password):
            time.sleep(_failure_delay_s())
            raise HTTPException(status_code=401, detail="Invalid credentials")

        display_name = username

        sid = _session_store.create(username=username, display_name=display_name)
        _log.info("issued session sid=%s... for user=%r", sid[:8], username)

        response.set_cookie(
            key=COOKIE_NAME,
            value=sid,
            max_age=_SESSION_TTL_SECONDS,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        return LoginResponse(ok=True, username=username, display_name=display_name)

    @router.post("/logout", response_model=LogoutResponse, include_in_schema=False)
    def logout(
        response: Response,
        tm_session: Optional[str] = Cookie(default=None),
    ) -> LogoutResponse:
        if _session_store is not None and tm_session:
            _session_store.delete(tm_session)
        response.delete_cookie(key=COOKIE_NAME, path="/")
        return LogoutResponse(ok=True)

    @router.get("/whoami", response_model=WhoamiResponse, include_in_schema=False)
    def whoami(
        tm_session: Optional[str] = Cookie(default=None),
    ) -> WhoamiResponse:
        if _session_store is None or not tm_session:
            return WhoamiResponse(authenticated=False)
        rec = _session_store.get(tm_session)
        if rec is None:
            return WhoamiResponse(authenticated=False)
        return WhoamiResponse(
            authenticated=True,
            username=rec.username,
            display_name=rec.display_name,
            expires_at=rec.expires_at,
        )

    return router


# =========================================================================
# FastAPI dependency: require an authenticated session
# =========================================================================


def require_user(tm_session: Optional[str] = Cookie(default=None)) -> SessionRecord:
    """FastAPI dependency for dashboard routes. 401 if not signed in."""
    if not pam_enabled():
        # Auth disabled — return a synthetic anonymous session for tests
        # and local dev. Production sets TM_PAM_ENABLED=1.
        return SessionRecord(
            session_id="anon",
            username="anonymous",
            display_name="anonymous",
            created_at=0.0,
            expires_at=float("inf"),
            last_seen=0.0,
        )
    if _session_store is None:
        raise HTTPException(status_code=503, detail="Auth not initialised")
    if not tm_session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    rec = _session_store.get(tm_session)
    if rec is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return rec


# =========================================================================
# Test helpers
# =========================================================================


@contextmanager
def override_session_store(store: SessionStore) -> Iterator[None]:
    """Swap the module-level SessionStore for a test instance."""
    global _session_store
    prev = _session_store
    _session_store = store
    try:
        yield
    finally:
        _session_store = prev
