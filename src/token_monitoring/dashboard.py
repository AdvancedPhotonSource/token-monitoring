"""JSON endpoints for the single-page dashboard UI."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from token_monitoring import db
from token_monitoring.auth_pam import require_user


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api", tags=["dashboard"], dependencies=[Depends(require_user)])

    @router.get("/overview")
    def overview(days: int = Query(30, ge=1, le=365)) -> dict:
        store = _require_store()
        return {
            "days": days,
            "tokens_per_day": store.tokens_per_day(days=days),
            "top_users": store.top_users(n=10, days=days),
            "top_models": store.top_models(n=10, days=days),
            "current_month": store.current_month_totals(),
            "labels": store.all_labels(),
        }

    @router.get("/requests")
    def list_requests(
        user_hash: Optional[str] = None,
        model: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = Query(250, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> dict:
        store = _require_store()
        rows = store.list_requests(
            user_hash=user_hash, model=model,
            since=since, until=until,
            limit=limit, offset=offset,
        )
        total = store.count_requests(
            user_hash=user_hash, model=model, since=since, until=until,
        )
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "rows": rows,
            "labels": store.all_labels(),
        }

    @router.get("/users/{user_hash}")
    def user_detail(user_hash: str, days: int = Query(30, ge=1, le=365)) -> dict:
        store = _require_store()
        summary = store.user_summary(user_hash, days=days)
        summary["user_hash"] = user_hash
        summary["display_name"] = store.get_label(user_hash) or user_hash[:8]
        summary["recent"] = store.list_requests(user_hash=user_hash, limit=50)
        return summary

    return router


def _require_store():
    store = db.store()
    if store is None:
        raise HTTPException(status_code=503, detail="Store not initialised")
    return store
