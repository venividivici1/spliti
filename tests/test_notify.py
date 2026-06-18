"""Tests for Web Push notifications: subscriptions, preferences, and the
per-event dispatch rules. The push transport (pywebpush) is mocked, so these run
without network or real VAPID keys; the service-worker side needs a real device.
"""

import pytest
from fastapi.testclient import TestClient

from spliti import db, notifications
from spliti.app import split_app
from tests.conftest import TEST_PASSWORD

AUTH = ("Ada", TEST_PASSWORD)


@pytest.fixture(autouse=True)
def split_db(tmp_path, monkeypatch):
    path = tmp_path / "split.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db(path)
    yield


@pytest.fixture
def client():
    with TestClient(split_app) as c:
        yield c


@pytest.fixture
def sent(monkeypatch):
    """Turn the feature on with a mocked transport, capturing every push sent."""
    out = []
    monkeypatch.setattr(notifications, "is_configured", lambda: True)
    monkeypatch.setattr(notifications, "public_key", lambda: "test-public-key")
    monkeypatch.setattr(
        notifications, "_send_one",
        lambda endpoint, p256dh, auth, payload: out.append({"endpoint": endpoint, "payload": payload}) or True,
    )
    return out


def make_group(client, members=("Ada", "Bo", "Cy")):
    gid = client.post("/api/groups", json={"name": "Trip"}, auth=AUTH).json()["id"]
    ids = {}
    for m in members:
        ids[m] = client.post(
            f"/api/groups/{gid}/members", json={"name": m}, auth=AUTH
        ).json()["id"]
    return gid, ids


def subscribe(client, name, endpoint):
    return client.post(
        "/api/notify/subscribe",
        json={"endpoint": endpoint, "keys": {"p256dh": f"k-{name}", "auth": f"a-{name}"}},
        auth=(name, TEST_PASSWORD),
    )


def sub_count():
    conn = db.connect()
    try:
        return conn.execute("SELECT COUNT(*) c FROM push_subscriptions").fetchone()["c"]
    finally:
        conn.close()


def test_subscribe_requires_configuration(client):
    """With no VAPID keys, the feature is off and subscribe is rejected."""
    make_group(client)
    assert subscribe(client, "Bo", "https://push/bo").status_code == 503


def test_subscribe_and_default_prefs_all_on(client, sent):
    make_group(client)
    assert subscribe(client, "Bo", "https://push/bo").status_code == 200
    prefs = client.get("/api/notify/prefs", auth=("Bo", TEST_PASSWORD)).json()
    assert prefs == {"new_expense": True, "settlement": True, "balance": True, "delete_restore": True}


def test_update_prefs_round_trips(client, sent):
    make_group(client)
    subscribe(client, "Bo", "https://push/bo")
    client.put(
        "/api/notify/prefs",
        json={"new_expense": False, "settlement": True, "balance": False, "delete_restore": True},
        auth=("Bo", TEST_PASSWORD),
    )
    prefs = client.get("/api/notify/prefs", auth=("Bo", TEST_PASSWORD)).json()
    assert prefs["new_expense"] is False and prefs["balance"] is False
    assert prefs["settlement"] is True and prefs["delete_restore"] is True


def test_expense_notifies_others_not_the_actor(client, sent):
    gid, ids = make_group(client)
    for who in ("Ada", "Bo", "Cy"):
        subscribe(client, who, f"https://push/{who}")
    client.post(
        f"/api/groups/{gid}/expenses",
        json={"description": "Dinner", "amount": 30, "paid_by": ids["Ada"]},
        auth=AUTH,
    )
    assert {s["endpoint"] for s in sent} == {"https://push/Bo", "https://push/Cy"}


def test_balance_affected_message_is_personalised(client, sent):
    gid, ids = make_group(client, members=("Ada", "Bo"))
    subscribe(client, "Bo", "https://push/bo")
    client.post(
        f"/api/groups/{gid}/expenses",
        json={"description": "Lunch", "amount": 20, "paid_by": ids["Ada"]},
        auth=AUTH,
    )
    assert "You owe" in sent[0]["payload"]["body"]  # Bo now owes Ada 10


def test_prefs_off_suppresses_the_push(client, sent):
    gid, ids = make_group(client, members=("Ada", "Bo"))
    subscribe(client, "Bo", "https://push/bo")
    # Silence both categories that could fire for an expense Bo is part of.
    client.put(
        "/api/notify/prefs",
        json={"new_expense": False, "settlement": True, "balance": False, "delete_restore": True},
        auth=("Bo", TEST_PASSWORD),
    )
    client.post(
        f"/api/groups/{gid}/expenses",
        json={"description": "X", "amount": 10, "paid_by": ids["Ada"]},
        auth=AUTH,
    )
    assert sent == []


def test_generic_message_when_balance_pref_off_but_new_expense_on(client, sent):
    gid, ids = make_group(client, members=("Ada", "Bo"))
    subscribe(client, "Bo", "https://push/bo")
    client.put(
        "/api/notify/prefs",
        json={"new_expense": True, "settlement": True, "balance": False, "delete_restore": True},
        auth=("Bo", TEST_PASSWORD),
    )
    client.post(
        f"/api/groups/{gid}/expenses",
        json={"description": "Cab", "amount": 10, "paid_by": ids["Ada"]},
        auth=AUTH,
    )
    body = sent[0]["payload"]["body"]
    assert "Cab" in body and "You owe" not in body  # generic, not the balance line


def test_settlement_notifies_counterparty(client, sent):
    gid, ids = make_group(client, members=("Ada", "Bo"))
    subscribe(client, "Bo", "https://push/bo")
    client.post(
        f"/api/groups/{gid}/settlements",
        json={"from_member": ids["Bo"], "to_member": ids["Ada"], "amount": 5},
        auth=AUTH,  # Ada records it
    )
    assert any(s["endpoint"] == "https://push/bo" for s in sent)


def test_delete_notifies_participants(client, sent):
    gid, ids = make_group(client, members=("Ada", "Bo"))
    subscribe(client, "Bo", "https://push/bo")
    eid = client.post(
        f"/api/groups/{gid}/expenses",
        json={"description": "Cab", "amount": 10, "paid_by": ids["Ada"]},
        auth=AUTH,
    ).json()["id"]
    sent.clear()
    client.delete(f"/api/groups/{gid}/expenses/{eid}", auth=AUTH)
    assert sent and "deleted" in sent[0]["payload"]["body"]


def test_stale_subscription_is_pruned(client, sent, monkeypatch):
    gid, ids = make_group(client, members=("Ada", "Bo"))
    subscribe(client, "Bo", "https://push/bo")
    monkeypatch.setattr(notifications, "_send_one", lambda *a: False)  # 404/410 gone
    client.post(
        f"/api/groups/{gid}/expenses",
        json={"description": "X", "amount": 10, "paid_by": ids["Ada"]},
        auth=AUTH,
    )
    assert sub_count() == 0


def test_unsubscribe_removes_the_row(client, sent):
    make_group(client, members=("Ada", "Bo"))
    subscribe(client, "Bo", "https://push/bo")
    assert sub_count() == 1
    client.post("/api/notify/unsubscribe", json={"endpoint": "https://push/bo"}, auth=("Bo", TEST_PASSWORD))
    assert sub_count() == 0


def test_resubscribe_same_endpoint_upserts(client, sent):
    make_group(client, members=("Ada", "Bo"))
    subscribe(client, "Bo", "https://push/bo")
    subscribe(client, "Bo", "https://push/bo")  # same device again
    assert sub_count() == 1


def test_unsubscribe_is_scoped_to_the_owner(client, sent):
    """A member can't unsubscribe another member's device by passing its endpoint."""
    make_group(client, members=("Ada", "Bo"))
    subscribe(client, "Bo", "https://push/bo")
    # Ada tries to drop Bo's subscription — should be a no-op, the row survives.
    client.post("/api/notify/unsubscribe", json={"endpoint": "https://push/bo"}, auth=AUTH)
    assert sub_count() == 1
    # Bo (the owner) can drop it.
    client.post(
        "/api/notify/unsubscribe", json={"endpoint": "https://push/bo"},
        auth=("Bo", TEST_PASSWORD),
    )
    assert sub_count() == 0


def test_distinct_events_get_distinct_tags(client, sent):
    """A constant tag would collapse a burst into one banner; tags must differ."""
    gid, ids = make_group(client, members=("Ada", "Bo"))
    subscribe(client, "Bo", "https://push/bo")
    for desc in ("Dinner", "Cab"):
        client.post(
            f"/api/groups/{gid}/expenses",
            json={"description": desc, "amount": 10, "paid_by": ids["Ada"]},
            auth=AUTH,
        )
    tags = {s["payload"]["tag"] for s in sent}
    assert len(sent) == 2 and len(tags) == 2


def test_no_dispatch_when_group_has_no_subscribers(client, sent, monkeypatch):
    """With nobody subscribed, the write path skips the balance snapshot + dispatch."""
    gid, ids = make_group(client, members=("Ada", "Bo"))  # no subscribe()
    calls = []
    monkeypatch.setattr(notifications, "dispatch", lambda *a, **k: calls.append(a))
    client.post(
        f"/api/groups/{gid}/expenses",
        json={"description": "X", "amount": 10, "paid_by": ids["Ada"]},
        auth=AUTH,
    )
    assert calls == []
