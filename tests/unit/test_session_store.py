from __future__ import annotations

import time

from webapp.session_store import SQLiteSessionStore


def test_basic_dict_like_crud(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    store["tok1"] = {"files": [], "last_accessed": time.time()}

    assert "tok1" in store
    assert store["tok1"]["files"] == []
    assert len(store) == 1

    del store["tok1"]
    assert "tok1" not in store
    assert len(store) == 0


def test_getitem_missing_token_raises_keyerror(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    try:
        store["does-not-exist"]
        assert False, "expected KeyError"
    except KeyError:
        pass
    assert store.get("does-not-exist") is None


def test_tuple_keyed_overrides_round_trip(tmp_path):
    """manual_overrides/qty_overrides are keyed by (BLItemNo, ElementId)
    tuples -- JSON has no tuple keys, so the store must convert on the way
    in and reconstruct real tuples on the way out."""
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    session = {
        "files": [],
        "manual_overrides": {("3005", "4211389"): 0.15},
        "qty_overrides": {("3023", "302326"): 10},
        "last_accessed": time.time(),
    }
    store["tok1"] = session

    reread = store["tok1"]
    assert reread["manual_overrides"] == {("3005", "4211389"): 0.15}
    assert reread["qty_overrides"] == {("3023", "302326"): 10}
    # Keys must be actual tuples (not lists), since main.py does
    # `key = (row["BLItemNo"], row["ElementId"]); key in overrides`.
    assert isinstance(next(iter(reread["manual_overrides"])), tuple)


def test_persists_across_reopening_the_store(tmp_path):
    """Simulates a process restart: a session written by one store instance
    must be readable by a brand new instance pointed at the same file."""
    db_path = tmp_path / "sessions.db"
    store1 = SQLiteSessionStore(db_path)
    store1["tok1"] = {"files": [{"name": "simple.csv"}], "last_accessed": time.time()}

    store2 = SQLiteSessionStore(db_path)  # fresh instance, same file
    assert "tok1" in store2
    assert store2["tok1"]["files"] == [{"name": "simple.csv"}]


def test_clear_removes_everything(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    store["tok1"] = {"files": [], "last_accessed": time.time()}
    store["tok2"] = {"files": [], "last_accessed": time.time()}
    store.clear()
    assert len(store) == 0


def test_evict_drops_expired_sessions_by_ttl(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    store["old"] = {"files": [], "last_accessed": time.time() - 100}
    store["fresh"] = {"files": [], "last_accessed": time.time()}

    store.evict(ttl_seconds=10, max_sessions=1000)

    assert "old" not in store
    assert "fresh" in store


def test_evict_drops_oldest_when_over_max_sessions(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    now = time.time()
    for i in range(5):
        store[f"tok{i}"] = {"files": [], "last_accessed": now - (5 - i)}  # tok0 oldest

    store.evict(ttl_seconds=10_000, max_sessions=3)

    assert len(store) == 3
    assert "tok0" not in store
    assert "tok1" not in store
    assert "tok4" in store  # most recently accessed survives
