"""FastAPI app assembly.

Two route groups on one port:
  * Proxy: /v1/* — Anthropic Messages API forwarded to Argo. API-key auth
    (the caller's Argo key), no session cookie.
  * Dashboard: /, /static/*, /api/*, /auth/* — PAM session cookie.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from token_monitoring import auth_pam, config, dashboard, db, proxy

_log = logging.getLogger(__name__)


def _web_dir() -> Path:
    """Locate the bundled `web/` directory.

    Installed layout: {site-packages}/token_monitoring/web/
    Dev layout:       {repo_root}/web/
    """
    pkg_web = Path(__file__).parent / "web"
    if pkg_web.exists():
        return pkg_web
    repo_web = Path(__file__).resolve().parents[2] / "web"
    return repo_web


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    db.init_db(config.db_path())
    _log.info("usage store at %s", config.db_path())
    try:
        yield
    finally:
        await proxy.shutdown_client()
        db.shutdown_db()
        auth_pam.shutdown_pam()


def build_app() -> FastAPI:
    app = FastAPI(
        title="token-monitoring",
        version="0.0.1",
        docs_url=None, redoc_url=None,
        lifespan=_lifespan,
    )

    # Proxy router first — matches /v1/* before the catch-all root.
    app.include_router(proxy.build_router())

    # PAM auth routes (dormant unless TM_PAM_ENABLED=1).
    if auth_pam.pam_enabled():
        app.include_router(auth_pam.init_pam(session_db_path=config.session_db_path()))
        _log.info("PAM auth enabled (service=%s)", "password-auth")
    else:
        _log.info("PAM auth disabled — dashboard is open")

    # Dashboard JSON API.
    app.include_router(dashboard.build_router())

    # Static + index.
    web = _web_dir()
    if web.exists():
        app.mount("/static", StaticFiles(directory=str(web)), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(str(web / "index.html"))
    else:
        _log.warning("web/ directory not found — dashboard UI will 404")

    @app.get("/health", include_in_schema=False)
    def health() -> dict:
        return {"status": "ok", "service": "token-monitoring"}

    return app


# Module-level app for `uvicorn token_monitoring.api:app` style.
app = build_app()
