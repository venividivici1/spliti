"""Splitwise-style child app: groups, expenses, balances, settle-up.

Runs standalone (uvicorn spliti.app:split_app); was previously host-mounted in the fucku parent app.
Money crosses the API as decimal amounts but is stored/computed as integer paise.
"""

import secrets
import sqlite3
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from spliti.config import get_settings
from spliti import ask, balances, db, notifications

STATIC_DIR = Path(__file__).parent / "static"

# The single group the UI locks onto for now (members/groups are managed out of band).
DEFAULT_GROUP = "Spiti"

# ---- expense categories ----
CATEGORIES = {
    "chai":       {"emoji": "🍵", "label": "Chai & Snacks",
                   "keywords": ["chai", "tea", "coffee", "maggi", "snacks", "biscuit", "chips", "namkeen", "samosa", "pakora"]},
    "meals":      {"emoji": "🍽️", "label": "Meals",
                   "keywords": ["breakfast", "lunch", "dinner", "dhaba", "thali", "momo", "food", "biryani", "dal", "roti", "paratha"]},
    "fuel":       {"emoji": "⛽", "label": "Fuel",
                   "keywords": ["fuel", "petrol", "diesel", "gas", "filling"]},
    "stay":       {"emoji": "🏨", "label": "Stay",
                   "keywords": ["hotel", "homestay", "camp", "tent", "room", "lodge", "hostel", "airbnb", "resort", "night stay"]},
    "transport":  {"emoji": "🚗", "label": "Transport",
                   "keywords": ["toll", "parking", "cab", "taxi", "bus", "auto", "rickshaw", "bike", "rental", "ola", "uber"]},
    "activities": {"emoji": "🎒", "label": "Activities",
                   "keywords": ["trek", "rafting", "ticket", "entry", "permit", "paragliding", "camping", "safari", "museum", "temple"]},
    "shopping":   {"emoji": "🛒", "label": "Shopping",
                   "keywords": ["shopping", "souvenir", "clothes", "gift", "market", "handicraft"]},
    "essentials": {"emoji": "💊", "label": "Essentials",
                   "keywords": ["medicine", "pharmacy", "recharge", "sim", "atm", "laundry", "repair", "puncture", "mechanic"]},
    "drinks":     {"emoji": "🍺", "label": "Drinks",
                   "keywords": ["beer", "wine", "whisky", "rum", "alcohol", "bar", "pub", "old monk"]},
    "tips":       {"emoji": "💡", "label": "Tips & Misc",
                   "keywords": ["tip", "donation", "guide", "porter"]},
    "other":      {"emoji": "📦", "label": "Other", "keywords": []},
}

VALID_CATEGORIES = set(CATEGORIES.keys())


def detect_category(description: str) -> str:
    """Auto-detect a category from an expense description using keyword matching."""
    desc_lower = description.lower()
    for cat_id, cat in CATEGORIES.items():
        if cat_id == "other":
            continue
        if any(kw in desc_lower for kw in cat["keywords"]):
            return cat_id
    return "other"

split_app = FastAPI(
    title="split",
    description="A Splitwise-style expense splitter",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
# auto_error=False so a missing/invalid header doesn't auto-respond with
# `WWW-Authenticate: Basic`. That header makes browsers (notably iOS PWAs) pop
# their own native credential dialog on a 401 fetch — which iOS won't persist,
# so it reappears every launch. We omit it and let the in-app login handle auth.
security = HTTPBasic(auto_error=False)

# Ensure the schema exists. The startup lifespan is unreliable for a host-mounted
# sub-app, so initialise at import time instead.
db.init_db()


def authed_username(
    credentials: HTTPBasicCredentials | None = Depends(security),
) -> str:
    """Verify the shared password and return the signed-in name (the Basic-auth
    username). The name identifies which member is acting; it is validated
    against the group's members where identity matters (see `current_member`)."""
    password = get_settings().basic_auth_password
    if (
        credentials is None
        or not password
        or not secrets.compare_digest(credentials.password.encode(), password.encode())
    ):
        # No WWW-Authenticate header on purpose (see `security` above): the
        # frontend reads this 401 and shows its own login overlay.
        raise HTTPException(status_code=401, detail="unauthorized")
    return (credentials.username or "").strip()


def require_auth(_user: str = Depends(authed_username)) -> None:
    """Password gate for routes that don't need the caller's identity."""


def to_paise(amount: float) -> int:
    paise = round(amount * 100)
    if paise <= 0:
        raise HTTPException(status_code=422, detail="amount must be positive")
    return paise


# ---------------------------------------------------------------- models


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class MemberCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class ExpenseCreate(BaseModel):
    description: str = Field(min_length=1, max_length=200)
    amount: float = Field(gt=0)
    paid_by: int
    split_type: str = Field(default="equal", pattern="^(equal|exact)$")
    # equal: members to split among (default = everyone). exact: member_id -> amount
    members: list[int] | None = None
    shares: dict[int, float] | None = None
    # Optional client-generated id so a write created offline and replayed on
    # reconnect is applied exactly once (see db.expenses.client_id).
    client_id: str | None = Field(default=None, max_length=64)
    # Expense category (auto-detected from description if omitted).
    category: str | None = None


class SettlementCreate(BaseModel):
    from_member: int
    to_member: int
    amount: float = Field(gt=0)
    client_id: str | None = Field(default=None, max_length=64)


class PushKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=200)
    auth: str = Field(min_length=1, max_length=100)


class PushSubscriptionIn(BaseModel):
    # Shape of the browser's PushSubscription.toJSON(); expirationTime is ignored.
    endpoint: str = Field(min_length=1, max_length=1000)
    keys: PushKeys


class UnsubscribeIn(BaseModel):
    endpoint: str = Field(min_length=1, max_length=1000)


class NotifyPrefsIn(BaseModel):
    new_expense: bool = True
    settlement: bool = True
    balance: bool = True
    delete_restore: bool = True


class ChatTurn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=4000)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    history: list[ChatTurn] = Field(default_factory=list, max_length=20)


# ---------------------------------------------------------------- helpers


def _group_or_404(conn, gid: int) -> dict:
    row = conn.execute("SELECT * FROM groups WHERE id = ?", (gid,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="group not found")
    return dict(row)


def _member_ids(conn, gid: int) -> list[int]:
    return [r["id"] for r in conn.execute(
        "SELECT id FROM members WHERE group_id = ? ORDER BY id", (gid,)
    )]


def _existing_by_client_id(conn, table: str, gid: int, client_id: str | None) -> int | None:
    """Return the id of an already-stored row for this client_id, if any.

    Lets a write that was created offline and replayed on reconnect resolve to
    the same row instead of inserting a duplicate (idempotent sync).

    ``table`` is interpolated into the SQL, so callers must pass a trusted
    literal (only "expenses"/"settlements"), never user input."""
    if not client_id:
        return None
    row = conn.execute(
        f"SELECT id FROM {table} WHERE group_id = ? AND client_id = ?",
        (gid, client_id),
    ).fetchone()
    return row["id"] if row else None


def _member_by_name(conn, gid: int, name: str) -> dict | None:
    """Resolve a signed-in name to a member of the group (case-insensitive)."""
    row = conn.execute(
        "SELECT id, name FROM members WHERE group_id = ? AND name = ? COLLATE NOCASE",
        (gid, (name or "").strip()),
    ).fetchone()
    return dict(row) if row else None


def _default_group_id(conn) -> int:
    """The group the UI locks onto: DEFAULT_GROUP if present, else the oldest."""
    row = (
        conn.execute(
            "SELECT id FROM groups WHERE name = ? ORDER BY id LIMIT 1", (DEFAULT_GROUP,)
        ).fetchone()
        or conn.execute("SELECT id FROM groups ORDER BY id LIMIT 1").fetchone()
    )
    if not row:
        raise HTTPException(status_code=404, detail="no group configured")
    return row["id"]


def _group_detail(conn, gid: int) -> dict:
    group = _group_or_404(conn, gid)
    members = [dict(r) for r in conn.execute(
        "SELECT id, name FROM members WHERE group_id = ? ORDER BY id", (gid,)
    )]
    member_ids = [m["id"] for m in members]

    expenses = [
        {**dict(r), "deleted": bool(r["deleted_at"])}
        for r in conn.execute(
            """SELECT e.id, e.description, e.amount_paise, e.paid_by, e.added_by,
                      e.created_at, e.deleted_at, e.category,
                      m.name AS paid_by_name, a.name AS added_by_name
               FROM expenses e
               JOIN members m ON m.id = e.paid_by
               LEFT JOIN members a ON a.id = e.added_by
               WHERE e.group_id = ? ORDER BY e.id DESC""",
            (gid,),
        )
    ]
    # Only non-deleted expenses count toward balances.
    active_expenses = [e for e in expenses if not e["deleted"]]
    active_ids = {e["id"] for e in active_expenses}
    share_rows = conn.execute(
        """SELECT s.expense_id, s.member_id, s.share_paise, m.name AS member_name
           FROM expense_shares s
           JOIN expenses e ON e.id = s.expense_id
           JOIN members m ON m.id = s.member_id
           WHERE e.group_id = ?""",
        (gid,),
    ).fetchall()
    shares_by_expense: dict[int, list[dict]] = {}
    for r in share_rows:
        shares_by_expense.setdefault(r["expense_id"], []).append(
            {"member_id": r["member_id"], "name": r["member_name"], "share_paise": r["share_paise"]}
        )
    for e in expenses:
        e["shares"] = shares_by_expense.get(e["id"], [])
    # balances only count non-deleted expenses' shares
    shares = [
        {"member_id": r["member_id"], "share_paise": r["share_paise"]}
        for r in share_rows if r["expense_id"] in active_ids
    ]
    settlements = [dict(r) for r in conn.execute(
        """SELECT st.id, st.from_member, st.to_member, st.amount_paise, st.created_at,
                  f.name AS from_name, t.name AS to_name
           FROM settlements st
           JOIN members f ON f.id = st.from_member
           JOIN members t ON t.id = st.to_member
           WHERE st.group_id = ? ORDER BY st.id DESC""",
        (gid,),
    )]

    bal = balances.net_balances(member_ids, active_expenses, shares, settlements)
    suggestions = balances.suggest_settlements(bal)
    name_of = {m["id"]: m["name"] for m in members}
    return {
        "group": group,
        "members": members,
        "expenses": expenses,
        "settlements": settlements,
        "balances": [
            {"member_id": mid, "name": name_of[mid], "net_paise": bal[mid]}
            for mid in member_ids
        ],
        "suggestions": [
            {**s, "from_name": name_of[s["from"]], "to_name": name_of[s["to"]]}
            for s in suggestions
        ],
    }


def _net_by_member(detail: dict) -> dict[int, int]:
    """Map member_id -> net paise from a group detail, for push personalisation."""
    return {b["member_id"]: b["net_paise"] for b in detail["balances"]}


def _notify_expense_change(conn, background_tasks, gid, eid, exp_row, user, *, restored):
    """Schedule a delete/restore push to the expense's participants (not the actor)."""
    if not (notifications.is_configured() and notifications.group_has_subscribers(conn, gid)):
        return
    detail = _group_detail(conn, gid)
    participants = [
        r["member_id"]
        for r in conn.execute(
            "SELECT member_id FROM expense_shares WHERE expense_id = ?", (eid,)
        )
    ]
    affected = list(set(participants) | {exp_row["paid_by"]})
    actor = _member_by_name(conn, gid, user)
    summary = {
        "actor_name": actor["name"] if actor else "Someone",
        "description": exp_row["description"],
        "restored": restored,
        "event_id": eid,
    }
    background_tasks.add_task(
        notifications.dispatch, gid, "delete_restore",
        actor["id"] if actor else None, affected, _net_by_member(detail), summary,
    )


# ---------------------------------------------------------------- routes


@split_app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@split_app.get("/")
def index() -> FileResponse:
    # The shell is served without auth so it can open instantly (and, on iOS
    # PWAs, without a native Basic Auth prompt every launch). It carries no group
    # data — the UI fetches /api/* with an Authorization header it manages, and
    # those endpoints stay behind Basic Auth.
    return FileResponse(STATIC_DIR / "index.html")


# PWA assets — served without auth so the browser can fetch them for install
# (Android manifest, iOS apple-touch-icon) and register the service worker.
# They carry no group data; the app shell and /api/* stay behind auth.
@split_app.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json"
    )


@split_app.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    # Root-scoped (served from "/") so it can control the whole app.
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


@split_app.get("/icons/{name}", include_in_schema=False)
def icon(name: str) -> FileResponse:
    icons_dir = (STATIC_DIR / "icons").resolve()
    path = (icons_dir / name).resolve()
    if path.parent != icons_dir or not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path)


@split_app.get("/api/me")
def whoami(user: str = Depends(authed_username)) -> dict:
    """Identify the signed-in member, rejecting names that aren't in the group.

    The frontend calls this at login: 401 = wrong password, 403 = not a member,
    200 = a valid member (so it knows who is acting when recording expenses)."""
    conn = db.connect()
    try:
        gid = _default_group_id(conn)
        member = _member_by_name(conn, gid, user)
        if not member:
            names = [r["name"] for r in conn.execute(
                "SELECT name FROM members WHERE group_id = ? ORDER BY id", (gid,)
            )]
            raise HTTPException(
                status_code=403,
                detail=f"“{user}” isn’t in this group. Sign in as one of: {', '.join(names)}.",
            )
        return {"member_id": member["id"], "name": member["name"], "group_id": gid}
    finally:
        conn.close()


@split_app.get("/api/current", dependencies=[Depends(require_auth)])
def current_group() -> dict:
    """Detail of the fixed group the UI uses (prefers DEFAULT_GROUP, else the oldest)."""
    conn = db.connect()
    try:
        return _group_detail(conn, _default_group_id(conn))
    finally:
        conn.close()


def _current_member_or_403(conn, user: str) -> tuple[int, dict]:
    """Resolve the signed-in name to a member of the fixed group, or 403."""
    gid = _default_group_id(conn)
    member = _member_by_name(conn, gid, user)
    if not member:
        raise HTTPException(
            status_code=403, detail="you must sign in as a member of this group"
        )
    return gid, member


@split_app.get("/api/notify/config", dependencies=[Depends(require_auth)])
def notify_config() -> dict:
    """Whether push is available here, and the VAPID key the browser subscribes with."""
    return {"enabled": notifications.is_configured(), "public_key": notifications.public_key()}


@split_app.get("/api/notify/prefs")
def get_notify_prefs(user: str = Depends(authed_username)) -> dict:
    conn = db.connect()
    try:
        _, member = _current_member_or_403(conn, user)
        return notifications.get_prefs(conn, member["id"])
    finally:
        conn.close()


@split_app.put("/api/notify/prefs")
def put_notify_prefs(body: NotifyPrefsIn, user: str = Depends(authed_username)) -> dict:
    conn = db.connect()
    try:
        _, member = _current_member_or_403(conn, user)
        return notifications.set_prefs(conn, member["id"], body.model_dump())
    finally:
        conn.close()


@split_app.post("/api/notify/subscribe")
def notify_subscribe(body: PushSubscriptionIn, user: str = Depends(authed_username)) -> dict:
    if not notifications.is_configured():
        raise HTTPException(status_code=503, detail="notifications are not configured")
    conn = db.connect()
    try:
        _, member = _current_member_or_403(conn, user)
        notifications.save_subscription(
            conn, member["id"], body.endpoint, body.keys.p256dh, body.keys.auth
        )
        return {"ok": True}
    finally:
        conn.close()


@split_app.post("/api/notify/unsubscribe")
def notify_unsubscribe(body: UnsubscribeIn, user: str = Depends(authed_username)) -> dict:
    """Drop a subscription (member toggled push off, or the browser revoked it).
    Scoped to the signed-in member so no one can unsubscribe another member's device."""
    conn = db.connect()
    try:
        _, member = _current_member_or_403(conn, user)
        notifications.delete_subscription(conn, body.endpoint, member["id"])
        return {"ok": True}
    finally:
        conn.close()


@split_app.get("/api/categories")
def get_categories() -> dict:
    """Return available expense categories with emoji and labels."""
    return {"categories": {
        k: {"emoji": v["emoji"], "label": v["label"]}
        for k, v in CATEGORIES.items()
    }}


@split_app.get("/api/detect-category")
def api_detect_category(description: str = "") -> dict:
    """Auto-detect a category from an expense description."""
    return {"category": detect_category(description)}


@split_app.get("/api/suggest-description", dependencies=[Depends(require_auth)])
async def suggest_description(lat: float | None = None, lon: float | None = None) -> dict:
    """A time-of-day expense description suggestion (best-effort; empty if AI unavailable).

    Optional `lat`/`lon` let the suggestion be tailored to the group's current
    locality (reverse-geocoded server-side). Both location and AI are best-effort.
    """
    if not get_settings().mistral_api_key:
        return {"suggestion": ""}
    try:
        place = await ask.reverse_geocode(lat, lon) if lat is not None and lon is not None else None
        return {"suggestion": await ask.suggest_description(place=place), "place": place}
    except Exception:
        return {"suggestion": ""}


@split_app.get("/api/reverse-geocode", dependencies=[Depends(require_auth)])
async def reverse_geocode(lat: float, lon: float) -> dict:
    """Resolve a coordinate to a short locality (best-effort; null on any failure).

    Kept separate from /suggest-description so the (slow) location lookup never
    blocks the fast time-of-day suggestion on the client.
    """
    try:
        return {"place": await ask.reverse_geocode(lat, lon)}
    except Exception:
        return {"place": None}


@split_app.get("/api/groups", dependencies=[Depends(require_auth)])
def list_groups() -> dict:
    conn = db.connect()
    try:
        rows = conn.execute(
            """SELECT g.id, g.name, g.created_at,
                      (SELECT COUNT(*) FROM members m WHERE m.group_id = g.id) AS member_count
               FROM groups g ORDER BY g.id DESC"""
        ).fetchall()
        return {"groups": [dict(r) for r in rows]}
    finally:
        conn.close()


@split_app.post("/api/groups", dependencies=[Depends(require_auth)])
def create_group(body: GroupCreate) -> dict:
    conn = db.connect()
    try:
        cur = conn.execute("INSERT INTO groups (name) VALUES (?)", (body.name.strip(),))
        conn.commit()
        return {"id": cur.lastrowid, "name": body.name.strip()}
    finally:
        conn.close()


@split_app.get("/api/groups/{gid}", dependencies=[Depends(require_auth)])
def get_group(gid: int) -> dict:
    conn = db.connect()
    try:
        return _group_detail(conn, gid)
    finally:
        conn.close()


@split_app.delete("/api/groups/{gid}", dependencies=[Depends(require_auth)])
def delete_group(gid: int) -> dict:
    conn = db.connect()
    try:
        _group_or_404(conn, gid)
        conn.execute("DELETE FROM groups WHERE id = ?", (gid,))
        conn.commit()
        return {"deleted": gid}
    finally:
        conn.close()


@split_app.post("/api/groups/{gid}/members", dependencies=[Depends(require_auth)])
def add_member(gid: int, body: MemberCreate) -> dict:
    conn = db.connect()
    try:
        _group_or_404(conn, gid)
        name = body.name.strip()
        # Names are identities here (login is by name, and notifications resolve the
        # acting member by name), so they must be unique within a group.
        if _member_by_name(conn, gid, name):
            raise HTTPException(
                status_code=409, detail="a member with that name already exists"
            )
        cur = conn.execute(
            "INSERT INTO members (group_id, name) VALUES (?, ?)", (gid, name)
        )
        conn.commit()
        return {"id": cur.lastrowid, "name": name, "group_id": gid}
    finally:
        conn.close()


@split_app.post("/api/groups/{gid}/expenses")
def add_expense(
    gid: int,
    body: ExpenseCreate,
    background_tasks: BackgroundTasks,
    user: str = Depends(authed_username),
) -> dict:
    conn = db.connect()
    try:
        _group_or_404(conn, gid)
        # Idempotent replay: if this exact write already landed (same client_id),
        # return it instead of creating a duplicate.
        dup = _existing_by_client_id(conn, "expenses", gid, body.client_id)
        if dup is not None:
            return {"id": dup, "duplicate": True}
        # The person recording the expense must be a member of this group — so
        # we always know who added it, even when paid_by is someone else.
        adder = _member_by_name(conn, gid, user)
        if not adder:
            raise HTTPException(
                status_code=403, detail="you must sign in as a member of this group"
            )
        valid = set(_member_ids(conn, gid))
        if not valid:
            raise HTTPException(status_code=422, detail="group has no members yet")
        if body.paid_by not in valid:
            raise HTTPException(status_code=422, detail="payer is not in this group")

        total = to_paise(body.amount)
        category = body.category if body.category in VALID_CATEGORIES else detect_category(body.description)

        if body.split_type == "equal":
            participants = body.members or sorted(valid)
            if any(m not in valid for m in participants):
                raise HTTPException(status_code=422, detail="unknown member in split")
            if not participants:
                raise HTTPException(status_code=422, detail="need someone to split among")
            share_map = balances.split_equally(total, participants)
        else:  # exact
            if not body.shares:
                raise HTTPException(status_code=422, detail="exact split needs shares")
            share_map = {int(m): to_paise(a) for m, a in body.shares.items()}
            if any(m not in valid for m in share_map):
                raise HTTPException(status_code=422, detail="unknown member in shares")
            if sum(share_map.values()) != total:
                raise HTTPException(
                    status_code=422, detail="shares must sum to the total amount"
                )

        try:
            cur = conn.execute(
                "INSERT INTO expenses "
                "(group_id, description, amount_paise, paid_by, added_by, client_id, category) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (gid, body.description.strip(), total, body.paid_by, adder["id"], body.client_id, category),
            )
            eid = cur.lastrowid
            conn.executemany(
                "INSERT INTO expense_shares (expense_id, member_id, share_paise) VALUES (?, ?, ?)",
                [(eid, m, c) for m, c in share_map.items()],
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # A concurrent replay of the same client_id won the race and inserted
            # first (the check above and this insert aren't atomic). Resolve to the
            # row that landed instead of failing — keeps replay exactly-once.
            conn.rollback()
            dup = _existing_by_client_id(conn, "expenses", gid, body.client_id)
            if dup is not None:
                return {"id": dup, "duplicate": True}
            raise

        if notifications.is_configured() and notifications.group_has_subscribers(conn, gid):
            detail = _group_detail(conn, gid)
            summary = {
                "actor_name": adder["name"],
                "description": body.description.strip(),
                "amount_paise": total,
                "event_id": eid,
            }
            background_tasks.add_task(
                notifications.dispatch, gid, "new_expense", adder["id"],
                list(set(share_map) | {body.paid_by}), _net_by_member(detail), summary,
            )
        return {"id": eid}
    finally:
        conn.close()


@split_app.delete("/api/groups/{gid}/expenses/{eid}")
def delete_expense(
    gid: int, eid: int, background_tasks: BackgroundTasks,
    user: str = Depends(authed_username),
) -> dict:
    """Soft-delete: keep the row (so it stays in history and can be restored)."""
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT id, description, paid_by FROM expenses WHERE id = ? AND group_id = ?",
            (eid, gid),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="expense not found")
        cur = conn.execute(
            "UPDATE expenses SET deleted_at = datetime('now') "
            "WHERE id = ? AND deleted_at IS NULL",
            (eid,),
        )
        conn.commit()
        if cur.rowcount:  # only notify on a real state change (not a replayed delete)
            _notify_expense_change(conn, background_tasks, gid, eid, row, user, restored=False)
        return {"deleted": eid}
    finally:
        conn.close()


@split_app.post("/api/groups/{gid}/expenses/{eid}/restore")
def restore_expense(
    gid: int, eid: int, background_tasks: BackgroundTasks,
    user: str = Depends(authed_username),
) -> dict:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT id, description, paid_by FROM expenses WHERE id = ? AND group_id = ?",
            (eid, gid),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="expense not found")
        cur = conn.execute(
            "UPDATE expenses SET deleted_at = NULL WHERE id = ? AND deleted_at IS NOT NULL",
            (eid,),
        )
        conn.commit()
        if cur.rowcount:
            _notify_expense_change(conn, background_tasks, gid, eid, row, user, restored=True)
        return {"restored": eid}
    finally:
        conn.close()


@split_app.post("/api/groups/{gid}/ask", dependencies=[Depends(require_auth)])
async def ask_group(gid: int, body: AskRequest) -> StreamingResponse:
    if not get_settings().mistral_api_key:
        raise HTTPException(status_code=503, detail="chat is not configured (no Mistral key)")
    conn = db.connect()
    try:
        context = ask.build_context(_group_detail(conn, gid))
    finally:
        conn.close()
    history = [t.model_dump() for t in body.history]
    return StreamingResponse(
        ask.answer_stream(context, history, body.question),
        media_type="text/plain; charset=utf-8",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@split_app.post("/api/groups/{gid}/settlements")
def add_settlement(
    gid: int, body: SettlementCreate, background_tasks: BackgroundTasks,
    user: str = Depends(authed_username),
) -> dict:
    conn = db.connect()
    try:
        _group_or_404(conn, gid)
        dup = _existing_by_client_id(conn, "settlements", gid, body.client_id)
        if dup is not None:
            return {"id": dup, "duplicate": True}
        valid = set(_member_ids(conn, gid))
        if body.from_member not in valid or body.to_member not in valid:
            raise HTTPException(status_code=422, detail="member not in this group")
        if body.from_member == body.to_member:
            raise HTTPException(status_code=422, detail="cannot settle with yourself")
        amount_paise = to_paise(body.amount)
        try:
            cur = conn.execute(
                "INSERT INTO settlements "
                "(group_id, from_member, to_member, amount_paise, client_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (gid, body.from_member, body.to_member, amount_paise, body.client_id),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # Concurrent replay of the same client_id won the race — resolve to the
            # row that landed rather than 500 (see add_expense).
            conn.rollback()
            dup = _existing_by_client_id(conn, "settlements", gid, body.client_id)
            if dup is not None:
                return {"id": dup, "duplicate": True}
            raise

        if notifications.is_configured() and notifications.group_has_subscribers(conn, gid):
            detail = _group_detail(conn, gid)
            name_of = {m["id"]: m["name"] for m in detail["members"]}
            actor = _member_by_name(conn, gid, user)
            summary = {
                "from_name": name_of.get(body.from_member, "Someone"),
                "to_name": name_of.get(body.to_member, "someone"),
                "amount_paise": amount_paise,
                "event_id": cur.lastrowid,
            }
            background_tasks.add_task(
                notifications.dispatch, gid, "settlement",
                actor["id"] if actor else None,
                [body.from_member, body.to_member], _net_by_member(detail), summary,
            )
        return {"id": cur.lastrowid}
    finally:
        conn.close()
