"""Tests for the optional Cloud Firestore mirror.

The real `google-cloud-firestore` client is replaced with an in-memory fake, so
these run with no GCP credentials and no network. They assert that writes land
as the right documents, that the mirror is a no-op when unconfigured, that group
deletes propagate, and that the after-response task never raises on errors.
"""

import pytest
from fastapi.testclient import TestClient

from spliti import db, firestore_sync
from spliti.app import split_app
from spliti.config import get_settings
from tests.conftest import TEST_PASSWORD

AUTH = ("Ada", TEST_PASSWORD)


# ---------------------------------------------------------------- fake client


class _FakeDoc:
    """A document reference backed by a shared flat {path: data} store."""

    def __init__(self, store, path):
        self.store = store
        self.path = path

    def set(self, data, merge=False):
        if merge and self.path in self.store.docs:
            self.store.docs[self.path].update(data)
        else:
            self.store.docs[self.path] = dict(data)

    def delete(self):
        self.store.docs.pop(self.path, None)

    def collection(self, name):
        return _FakeCollection(self.store, f"{self.path}/{name}")


class _FakeCollection:
    def __init__(self, store, path):
        self.store = store
        self.path = path

    def document(self, doc_id):
        return _FakeDoc(self.store, f"{self.path}/{doc_id}")


class _FakeBatch:
    def __init__(self, store):
        self.store = store
        self.ops = []

    def set(self, ref, data, merge=False):
        self.ops.append(("set", ref, data, merge))

    def commit(self):
        for _op, ref, data, merge in self.ops:
            ref.set(data, merge=merge)
        self.ops = []


class FakeFirestore:
    """Minimal in-memory stand-in for google.cloud.firestore.Client."""

    def __init__(self):
        self.docs = {}

    def collection(self, name):
        return _FakeCollection(self, name)

    def batch(self):
        return _FakeBatch(self)

    def recursive_delete(self, ref):
        prefix = ref.path + "/"
        for path in [p for p in self.docs if p == ref.path or p.startswith(prefix)]:
            del self.docs[path]


# ---------------------------------------------------------------- fixtures


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
def fs(monkeypatch):
    """Enable the mirror with the in-memory fake client."""
    store = FakeFirestore()
    monkeypatch.setattr(firestore_sync, "is_configured", lambda: True)
    monkeypatch.setattr(firestore_sync, "_get_client", lambda: store)
    return store


def make_group(client, name="Trip", members=("Ada", "Bo", "Cy")):
    gid = client.post("/api/groups", json={"name": name}, auth=AUTH).json()["id"]
    ids = {}
    for m in members:
        ids[m] = client.post(
            f"/api/groups/{gid}/members", json={"name": m}, auth=AUTH
        ).json()["id"]
    return gid, ids


# ---------------------------------------------------------------- tests


def test_group_and_members_are_mirrored(client, fs):
    gid, ids = make_group(client)
    assert fs.docs[f"groups/{gid}"]["name"] == "Trip"
    for name, mid in ids.items():
        assert fs.docs[f"groups/{gid}/members/{mid}"]["name"] == name


def test_expense_mirrored_with_embedded_shares(client, fs):
    gid, ids = make_group(client, members=("Ada", "Bo"))
    eid = client.post(
        f"/api/groups/{gid}/expenses",
        json={"description": "Dinner", "amount": 30, "paid_by": ids["Ada"]},
        auth=AUTH,
    ).json()["id"]
    doc = fs.docs[f"groups/{gid}/expenses/{eid}"]
    assert doc["description"] == "Dinner"
    assert doc["amount_paise"] == 3000
    assert doc["paid_by"] == ids["Ada"]
    assert doc["deleted"] is False
    # equal split of 3000 between two members → two shares of 1500
    assert sorted(s["share_paise"] for s in doc["shares"]) == [1500, 1500]


def test_soft_delete_then_restore_round_trips_the_flag(client, fs):
    gid, ids = make_group(client, members=("Ada", "Bo"))
    eid = client.post(
        f"/api/groups/{gid}/expenses",
        json={"description": "Cab", "amount": 10, "paid_by": ids["Ada"]},
        auth=AUTH,
    ).json()["id"]
    client.delete(f"/api/groups/{gid}/expenses/{eid}", auth=AUTH)
    assert fs.docs[f"groups/{gid}/expenses/{eid}"]["deleted"] is True
    client.post(f"/api/groups/{gid}/expenses/{eid}/restore", auth=AUTH)
    assert fs.docs[f"groups/{gid}/expenses/{eid}"]["deleted"] is False


def test_settlement_is_mirrored(client, fs):
    gid, ids = make_group(client, members=("Ada", "Bo"))
    sid = client.post(
        f"/api/groups/{gid}/settlements",
        json={"from_member": ids["Bo"], "to_member": ids["Ada"], "amount": 5},
        auth=AUTH,
    ).json()["id"]
    doc = fs.docs[f"groups/{gid}/settlements/{sid}"]
    assert doc["from_member"] == ids["Bo"] and doc["amount_paise"] == 500


def test_delete_group_clears_the_subtree(client, fs):
    gid, ids = make_group(client, members=("Ada", "Bo"))
    client.post(
        f"/api/groups/{gid}/expenses",
        json={"description": "X", "amount": 10, "paid_by": ids["Ada"]},
        auth=AUTH,
    )
    assert any(p.startswith(f"groups/{gid}") for p in fs.docs)
    client.delete(f"/api/groups/{gid}", auth=AUTH)
    assert not any(p.startswith(f"groups/{gid}") for p in fs.docs)


def test_mirror_is_idempotent(client, fs):
    """Re-mirroring the same group rewrites the same docs, not duplicates."""
    gid, _ = make_group(client, members=("Ada",))
    before = dict(fs.docs)
    firestore_sync.mirror_group(gid)
    assert fs.docs == before


def test_disabled_mirror_writes_nothing(client, monkeypatch):
    """With no project configured, the mirror is a no-op and never builds a client."""
    monkeypatch.setattr(firestore_sync, "is_configured", lambda: False)
    monkeypatch.setattr(
        firestore_sync, "_get_client",
        lambda: pytest.fail("client must not be built when the mirror is disabled"),
    )
    make_group(client)  # would raise via the patched _get_client if it tried to mirror


def test_mirror_swallows_client_errors(client, monkeypatch):
    """A failing Firestore client must not raise out of the background task."""
    monkeypatch.setattr(firestore_sync, "is_configured", lambda: True)

    def boom():
        raise RuntimeError("firestore down")

    monkeypatch.setattr(firestore_sync, "_get_client", boom)
    # The expense still gets created in SQLite; the mirror failure is swallowed.
    gid, ids = make_group(client, members=("Ada",))
    r = client.post(
        f"/api/groups/{gid}/expenses",
        json={"description": "X", "amount": 10, "paid_by": ids["Ada"]},
        auth=AUTH,
    )
    assert r.status_code == 200


def test_mirror_missing_group_is_noop(fs):
    """Mirroring a group id that no longer exists writes nothing and doesn't raise."""
    firestore_sync.mirror_group(999_999)
    assert fs.docs == {}


def test_is_configured_follows_project_id(monkeypatch):
    monkeypatch.setenv("FIRESTORE_PROJECT_ID", "my-project")
    get_settings.cache_clear()
    assert firestore_sync.is_configured() is True
    monkeypatch.delenv("FIRESTORE_PROJECT_ID", raising=False)
    get_settings.cache_clear()
    assert firestore_sync.is_configured() is False
