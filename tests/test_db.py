"""UsageStore: schema, inserts, aggregate queries, labels."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from token_monitoring.db import UsageRow, UsageStore


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _row(**overrides) -> UsageRow:
    defaults = dict(
        ts_utc=_iso(datetime.now(timezone.utc)),
        user_hash="u1",
        model="claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        latency_ms=200,
        status_code=200,
        endpoint="/v1/messages",
        streamed=True,
    )
    defaults.update(overrides)
    return UsageRow(**defaults)


def test_schema_creates_tables(tmp_path: Path) -> None:
    store = UsageStore(tmp_path / "u.sqlite")
    # New store == empty aggregate results.
    assert store.tokens_per_day() == []
    assert store.top_users() == []
    assert store.top_models() == []
    assert store.current_month_totals()["input_tokens"] == 0
    store.close()


def test_insert_and_aggregate(tmp_path: Path) -> None:
    store = UsageStore(tmp_path / "u.sqlite")
    store.insert_request(_row(user_hash="u1", input_tokens=100, output_tokens=40))
    store.insert_request(_row(user_hash="u1", input_tokens=50, output_tokens=20))
    store.insert_request(_row(user_hash="u2", input_tokens=200, output_tokens=80,
                              model="claude-haiku-4-5"))

    top_users = store.top_users(days=1)
    assert len(top_users) == 2
    # u2 is bigger (200+80 > 150+60), should be first.
    assert top_users[0]["user_hash"] == "u2"
    assert top_users[0]["input_tokens"] == 200

    top_models = store.top_models(days=1)
    assert {m["model"] for m in top_models} == {"claude-sonnet-4-6", "claude-haiku-4-5"}

    per_day = store.tokens_per_day(days=1)
    assert len(per_day) == 1
    assert per_day[0]["input_tokens"] == 350
    assert per_day[0]["output_tokens"] == 140
    assert per_day[0]["n"] == 3

    month = store.current_month_totals()
    assert month["input_tokens"] == 350
    assert month["output_tokens"] == 140
    assert month["total_tokens"] == 490

    store.close()


def test_list_and_count_with_filters(tmp_path: Path) -> None:
    store = UsageStore(tmp_path / "u.sqlite")
    for i in range(5):
        store.insert_request(_row(user_hash="u1" if i < 3 else "u2",
                                  model="A" if i % 2 == 0 else "B"))

    all_rows = store.list_requests()
    assert len(all_rows) == 5
    assert store.count_requests() == 5

    u1_rows = store.list_requests(user_hash="u1")
    assert len(u1_rows) == 3
    assert store.count_requests(user_hash="u1") == 3

    a_rows = store.list_requests(model="A")
    assert all(r["model"] == "A" for r in a_rows)
    store.close()


def test_labels_upsert_and_get(tmp_path: Path) -> None:
    store = UsageStore(tmp_path / "u.sqlite")
    store.upsert_label("hash-abc", "haskels")
    assert store.get_label("hash-abc") == "haskels"

    store.upsert_label("hash-abc", "haskels (updated)")
    assert store.get_label("hash-abc") == "haskels (updated)"

    assert store.get_label("does-not-exist") is None
    assert store.all_labels() == {"hash-abc": "haskels (updated)"}
    store.close()


def test_user_summary(tmp_path: Path) -> None:
    store = UsageStore(tmp_path / "u.sqlite")
    store.insert_request(_row(user_hash="u1", model="A", input_tokens=10, output_tokens=5))
    store.insert_request(_row(user_hash="u1", model="A", input_tokens=20, output_tokens=8))
    store.insert_request(_row(user_hash="u1", model="B", input_tokens=100, output_tokens=40))
    store.insert_request(_row(user_hash="u2", model="A", input_tokens=999, output_tokens=999))

    summary = store.user_summary("u1")
    assert summary["totals"]["input_tokens"] == 130
    assert summary["totals"]["output_tokens"] == 53
    assert summary["totals"]["n"] == 3
    # Per-model list ordered by total tokens desc: B (140) > A (43).
    assert summary["per_model"][0]["model"] == "B"
    store.close()
