import pytest
from fastapi.testclient import TestClient

from spliti import ask, balances, db
from spliti.app import split_app
from tests.conftest import TEST_PASSWORD

# "Ada" is a member created by make_group(); expense creation now requires the
# signed-in name to be a group member, so the username can't be arbitrary.
AUTH = ("Ada", TEST_PASSWORD)


@pytest.fixture(autouse=True)
def split_db(tmp_path, monkeypatch):
    """Isolated SQLite per test for the split app."""
    path = tmp_path / "split.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db(path)
    yield


@pytest.fixture
def client():
    with TestClient(split_app) as c:
        yield c


def make_group(client, name="Trip", members=("Ada", "Bo", "Cy")):
    gid = client.post("/api/groups", json={"name": name}, auth=AUTH).json()["id"]
    ids = {}
    for m in members:
        ids[m] = client.post(
            f"/api/groups/{gid}/members", json={"name": m}, auth=AUTH
        ).json()["id"]
    return gid, ids


# ---------------------------------------------------------------- unit tests


def test_split_equally_distributes_remainder():
    shares = balances.split_equally(1000, [1, 2, 3])  # 10.00 / 3
    assert sum(shares.values()) == 1000
    assert sorted(shares.values()) == [333, 333, 334]


def test_net_balances_and_suggestions():
    # Ada paid 30 split equally among 3 -> each owes 10
    expenses = [{"paid_by": 1, "amount_paise": 3000}]
    shares = [
        {"member_id": 1, "share_paise": 1000},
        {"member_id": 2, "share_paise": 1000},
        {"member_id": 3, "share_paise": 1000},
    ]
    bal = balances.net_balances([1, 2, 3], expenses, shares, [])
    assert bal == {1: 2000, 2: -1000, 3: -1000}
    sug = balances.suggest_settlements(bal)
    assert sum(s["amount_paise"] for s in sug) == 2000
    assert all(s["to"] == 1 for s in sug)


# ---------------------------------------------------------------- API tests


def test_auth_required(client):
    assert client.get("/api/groups").status_code == 401


def test_401_omits_www_authenticate(client):
    """No WWW-Authenticate header — else browsers (iOS PWAs) pop their own
    native, non-persistent credential dialog instead of our in-app login."""
    for r in (client.get("/api/groups"), client.get("/api/current")):
        assert r.status_code == 401
        assert "www-authenticate" not in {k.lower() for k in r.headers}


def test_healthz_open(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_shell_is_public_but_api_is_gated(client):
    """The HTML shell loads without auth (no data in it); /api/* stays gated.

    Keeps the iOS PWA from prompting for Basic Auth on every launch — the page
    opens, then the UI authenticates /api/* with a header it manages.
    """
    shell = client.get("/")
    assert shell.status_code == 200
    assert "text/html" in shell.headers["content-type"]
    assert client.get("/api/current").status_code == 401


def test_pwa_assets_open_and_served(client):
    """Manifest, service worker and icons are public (browser fetches them for install)."""
    man = client.get("/manifest.webmanifest")
    assert man.status_code == 200
    assert man.headers["content-type"].startswith("application/manifest+json")
    assert man.json()["short_name"] == "Spliti"

    sw = client.get("/sw.js")
    assert sw.status_code == 200
    assert "javascript" in sw.headers["content-type"]
    assert sw.headers["service-worker-allowed"] == "/"

    icon = client.get("/icons/icon-512.png")
    assert icon.status_code == 200
    assert icon.headers["content-type"] == "image/png"


def test_icon_route_rejects_traversal_and_unknown(client):
    assert client.get("/icons/nope.png").status_code == 404
    # A traversal attempt must not escape the icons directory.
    assert client.get("/icons/..%2f..%2fapp.py").status_code == 404


def test_equal_expense_balances(client):
    gid, ids = make_group(client)
    r = client.post(
        f"/api/groups/{gid}/expenses",
        json={"description": "Dinner", "amount": 30, "paid_by": ids["Ada"]},
        auth=AUTH,
    )
    assert r.status_code == 200
    detail = client.get(f"/api/groups/{gid}", auth=AUTH).json()
    net = {b["name"]: b["net_paise"] for b in detail["balances"]}
    assert net == {"Ada": 2000, "Bo": -1000, "Cy": -1000}
    # everyone should owe Ada
    assert all(s["to_name"] == "Ada" for s in detail["suggestions"])


def test_equal_expense_subset(client):
    gid, ids = make_group(client)
    client.post(
        f"/api/groups/{gid}/expenses",
        json={
            "description": "Cab",
            "amount": 10,
            "paid_by": ids["Ada"],
            "members": [ids["Ada"], ids["Bo"]],
        },
        auth=AUTH,
    )
    detail = client.get(f"/api/groups/{gid}", auth=AUTH).json()
    net = {b["name"]: b["net_paise"] for b in detail["balances"]}
    assert net == {"Ada": 500, "Bo": -500, "Cy": 0}


def test_exact_split_must_sum(client):
    gid, ids = make_group(client)
    bad = client.post(
        f"/api/groups/{gid}/expenses",
        json={
            "description": "Hotel",
            "amount": 100,
            "paid_by": ids["Ada"],
            "split_type": "exact",
            "shares": {str(ids["Ada"]): 50, str(ids["Bo"]): 40},  # sums to 90, not 100
        },
        auth=AUTH,
    )
    assert bad.status_code == 422


def test_exact_split_ok(client):
    gid, ids = make_group(client)
    r = client.post(
        f"/api/groups/{gid}/expenses",
        json={
            "description": "Hotel",
            "amount": 100,
            "paid_by": ids["Ada"],
            "split_type": "exact",
            "shares": {
                str(ids["Ada"]): 50,
                str(ids["Bo"]): 30,
                str(ids["Cy"]): 20,
            },
        },
        auth=AUTH,
    )
    assert r.status_code == 200
    net = {
        b["name"]: b["net_paise"]
        for b in client.get(f"/api/groups/{gid}", auth=AUTH).json()["balances"]
    }
    assert net == {"Ada": 5000, "Bo": -3000, "Cy": -2000}


def test_expense_includes_shares(client):
    gid, ids = make_group(client, members=("Ada", "Bo", "Cy"))
    client.post(
        f"/api/groups/{gid}/expenses",
        json={
            "description": "Hotel", "amount": 100, "paid_by": ids["Ada"],
            "split_type": "exact",
            "shares": {str(ids["Ada"]): 50, str(ids["Bo"]): 30, str(ids["Cy"]): 20},
        },
        auth=AUTH,
    )
    exp = client.get(f"/api/groups/{gid}", auth=AUTH).json()["expenses"][0]
    shares = {s["name"]: s["share_paise"] for s in exp["shares"]}
    assert shares == {"Ada": 5000, "Bo": 3000, "Cy": 2000}


def test_settlement_zeroes_balance(client):
    gid, ids = make_group(client, members=("Ada", "Bo"))
    client.post(
        f"/api/groups/{gid}/expenses",
        json={"description": "Lunch", "amount": 20, "paid_by": ids["Ada"]},
        auth=AUTH,
    )  # Bo owes Ada 10
    client.post(
        f"/api/groups/{gid}/settlements",
        json={"from_member": ids["Bo"], "to_member": ids["Ada"], "amount": 10},
        auth=AUTH,
    )
    detail = client.get(f"/api/groups/{gid}", auth=AUTH).json()
    assert all(b["net_paise"] == 0 for b in detail["balances"])
    assert detail["suggestions"] == []


def test_delete_is_soft_and_restorable(client):
    gid, ids = make_group(client)
    eid = client.post(
        f"/api/groups/{gid}/expenses",
        json={"description": "X", "amount": 9, "paid_by": ids["Ada"]},
        auth=AUTH,
    ).json()["id"]

    # soft delete: stays in history (flagged), drops out of balances
    assert client.delete(f"/api/groups/{gid}/expenses/{eid}", auth=AUTH).status_code == 200
    detail = client.get(f"/api/groups/{gid}", auth=AUTH).json()
    assert len(detail["expenses"]) == 1
    assert detail["expenses"][0]["deleted"] is True
    assert all(b["net_paise"] == 0 for b in detail["balances"])

    # restore: back in balances
    r = client.post(f"/api/groups/{gid}/expenses/{eid}/restore", auth=AUTH)
    assert r.status_code == 200
    detail = client.get(f"/api/groups/{gid}", auth=AUTH).json()
    assert detail["expenses"][0]["deleted"] is False
    assert any(b["net_paise"] != 0 for b in detail["balances"])


def test_restore_unknown_expense_404(client):
    gid, _ = make_group(client)
    assert client.post(f"/api/groups/{gid}/expenses/9999/restore", auth=AUTH).status_code == 404


def test_payer_must_be_member(client):
    gid, _ = make_group(client)
    r = client.post(
        f"/api/groups/{gid}/expenses",
        json={"description": "X", "amount": 5, "paid_by": 99999},
        auth=AUTH,
    )
    assert r.status_code == 422


def test_me_identifies_member_and_rejects_strangers(client):
    make_group(client, name="Spiti", members=("Ada", "Bo"))
    ok = client.get("/api/me", auth=("Ada", TEST_PASSWORD))
    assert ok.status_code == 200 and ok.json()["name"] == "Ada"
    # name matching is case-insensitive
    assert client.get("/api/me", auth=("ada", TEST_PASSWORD)).status_code == 200
    # right password, but not a member -> 403 (rejected at login)
    assert client.get("/api/me", auth=("Mallory", TEST_PASSWORD)).status_code == 403
    # wrong password -> 401
    assert client.get("/api/me", auth=("Ada", "nope")).status_code == 401


def test_non_member_cannot_add_expense(client):
    gid, ids = make_group(client, members=("Ada", "Bo"))
    r = client.post(
        f"/api/groups/{gid}/expenses",
        json={"description": "Sneaky", "amount": 10, "paid_by": ids["Ada"]},
        auth=("Mallory", TEST_PASSWORD),
    )
    assert r.status_code == 403


def test_added_by_is_recorded_and_distinct_from_paid_by(client):
    gid, ids = make_group(client, members=("Ada", "Bo"))
    # Ada records an expense that Bo paid for.
    client.post(
        f"/api/groups/{gid}/expenses",
        json={"description": "Fuel", "amount": 40, "paid_by": ids["Bo"]},
        auth=("Ada", TEST_PASSWORD),
    )
    exp = client.get(f"/api/groups/{gid}", auth=AUTH).json()["expenses"][0]
    assert exp["paid_by_name"] == "Bo"
    assert exp["added_by_name"] == "Ada"
    assert exp["added_by"] == ids["Ada"]


def test_build_context_includes_data(client):
    gid, ids = make_group(client, members=("Ada", "Bo"))
    client.post(
        f"/api/groups/{gid}/expenses",
        json={"description": "Lunch", "amount": 20, "paid_by": ids["Ada"]},
        auth=AUTH,
    )
    detail = client.get(f"/api/groups/{gid}", auth=AUTH).json()
    ctx = ask.build_context(detail)
    assert "Lunch" in ctx and "₹20.00" in ctx
    assert "$" not in ctx
    assert "Ada" in ctx and "Bo" in ctx


def test_ask_streams_answer(client, monkeypatch):
    async def fake_stream(context, history, question):
        assert "Lunch" in context  # the route fed real group data in
        for chunk in ["Bo ", "owes ", "Ada $10.00."]:
            yield chunk

    monkeypatch.setattr(ask, "answer_stream", fake_stream)
    gid, ids = make_group(client, members=("Ada", "Bo"))
    client.post(
        f"/api/groups/{gid}/expenses",
        json={"description": "Lunch", "amount": 20, "paid_by": ids["Ada"]},
        auth=AUTH,
    )
    r = client.post(
        f"/api/groups/{gid}/ask", json={"question": "who owes whom?"}, auth=AUTH
    )
    assert r.status_code == 200
    assert r.text == "Bo owes Ada $10.00."


def test_ask_requires_auth(client):
    assert client.post("/api/groups/1/ask", json={"question": "hi"}).status_code == 401


def test_suggest_description(client, monkeypatch):
    async def fake(place=None):
        return "Dhaba lunch"
    monkeypatch.setattr(ask, "suggest_description", fake)
    r = client.get("/api/suggest-description", auth=AUTH)
    assert r.status_code == 200
    assert r.json()["suggestion"] == "Dhaba lunch"


def test_suggest_description_failsafe(client, monkeypatch):
    async def boom(place=None):
        raise RuntimeError("mistral down")
    monkeypatch.setattr(ask, "suggest_description", boom)
    assert client.get("/api/suggest-description", auth=AUTH).json()["suggestion"] == ""


def test_suggest_description_with_location(client, monkeypatch):
    seen = {}

    async def fake_geocode(lat, lon):
        seen["coords"] = (lat, lon)
        return "Kaza, Himachal Pradesh"

    async def fake(place=None):
        seen["place"] = place
        return "Kaza lunch"

    monkeypatch.setattr(ask, "reverse_geocode", fake_geocode)
    monkeypatch.setattr(ask, "suggest_description", fake)
    r = client.get("/api/suggest-description?lat=32.22&lon=78.07", auth=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["suggestion"] == "Kaza lunch"
    assert body["place"] == "Kaza, Himachal Pradesh"
    assert seen["coords"] == (32.22, 78.07)
    assert seen["place"] == "Kaza, Himachal Pradesh"


def test_suggest_description_appends_place(monkeypatch):
    import asyncio

    class _Resp:
        class _C:
            class _M:
                content = "Late Night Maggi"
            message = _M()
        choices = [_C()]

    class _Client:
        class chat:
            @staticmethod
            async def complete_async(**kw):
                return _Resp()

    monkeypatch.setattr(ask, "client", lambda: _Client())
    # With a place, the locality is appended after "@" (state is dropped).
    tagged = asyncio.run(ask.suggest_description(place="Bangalore, Karnataka"))
    assert tagged == "Late Night Maggi @ Bangalore"
    # Without a place, it stays a plain activity.
    plain = asyncio.run(ask.suggest_description(place=None))
    assert plain == "Late Night Maggi"


def test_ist_conversion():
    # 19:22 UTC + 5:30 = 00:52 IST the next day
    out = ask._ist("2026-06-14 19:22:14")
    assert out == "15 Jun 2026, 12:52 AM IST"
    assert ask._ist("not a date") == "not a date"


def test_current_prefers_spiti(client):
    client.post("/api/groups", json={"name": "Other"}, auth=AUTH)
    spiti = client.post("/api/groups", json={"name": "Spiti"}, auth=AUTH).json()["id"]
    client.post("/api/groups", json={"name": "Another"}, auth=AUTH)
    cur = client.get("/api/current", auth=AUTH).json()
    assert cur["group"]["id"] == spiti
    assert cur["group"]["name"] == "Spiti"


def test_current_falls_back_to_oldest(client):
    first = client.post("/api/groups", json={"name": "First"}, auth=AUTH).json()["id"]
    client.post("/api/groups", json={"name": "Second"}, auth=AUTH)
    assert client.get("/api/current", auth=AUTH).json()["group"]["id"] == first


def test_current_404_when_empty(client):
    assert client.get("/api/current", auth=AUTH).status_code == 404


# ---------------------------------------------------------------- ai / geocode


def test_ai_client_is_lazy_and_cached(monkeypatch):
    import spliti.ai as ai

    created = []

    class FakeMistral:
        def __init__(self, api_key):
            created.append(api_key)

    monkeypatch.setattr(ai, "Mistral", FakeMistral)
    monkeypatch.setattr(ai, "_client", None)
    first = ai.client()
    second = ai.client()
    assert first is second  # cached
    assert created == ["test-mistral-key"]  # built exactly once


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeHttp:
    """Stand-in for httpx.AsyncClient(...) as an async context manager."""

    def __init__(self, payload=None, exc=None):
        self._payload = payload
        self._exc = exc

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **k):
        if self._exc:
            raise self._exc
        return _FakeResp(self._payload)


@pytest.mark.asyncio
async def test_reverse_geocode_rejects_bad_coords():
    assert await ask.reverse_geocode(91, 0) is None
    assert await ask.reverse_geocode(0, 999) is None


@pytest.mark.asyncio
async def test_reverse_geocode_builds_local_and_broad(monkeypatch):
    ask._GEO_CACHE.clear()
    payload = {"address": {"suburb": "Carmelaram", "city": "Bengaluru"}}
    monkeypatch.setattr(ask.httpx, "AsyncClient", _FakeHttp(payload=payload))
    assert await ask.reverse_geocode(12.9, 77.7) == "Carmelaram, Bengaluru"
    # second call is served from the in-process cache (coords round to the same key)
    monkeypatch.setattr(ask.httpx, "AsyncClient", _FakeHttp(exc=RuntimeError("no net")))
    assert await ask.reverse_geocode(12.9, 77.7) == "Carmelaram, Bengaluru"


@pytest.mark.asyncio
async def test_reverse_geocode_swallows_errors(monkeypatch):
    ask._GEO_CACHE.clear()
    monkeypatch.setattr(
        ask.httpx, "AsyncClient", _FakeHttp(exc=ask.httpx.ConnectError("down"))
    )
    assert await ask.reverse_geocode(1.0, 2.0) is None


@pytest.mark.asyncio
async def test_answer_stream_yields_chunks(monkeypatch):
    class _Delta:
        content = "chunk"

    class _Choice:
        delta = _Delta()

    class _Event:
        class data:
            choices = [_Choice()]

    async def _events():
        yield _Event()

    class _Chat:
        async def stream_async(self, **kw):
            return _events()

    class _Client:
        chat = _Chat()

    monkeypatch.setattr(ask, "client", lambda: _Client())
    out = [c async for c in ask.answer_stream("ctx", [], "q")]
    assert out == ["chunk"]
