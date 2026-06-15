"""Splitwise-style child app: groups, expenses, balances, settle-up.

Runs standalone (uvicorn spliti.app:split_app); was previously host-mounted in the fucku parent app.
Money crosses the API as decimal amounts but is stored/computed as integer paise.
"""

import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from spliti.config import get_settings
from spliti import ask, balances, db

STATIC_DIR = Path(__file__).parent / "static"

# The single group the UI locks onto for now (members/groups are managed out of band).
DEFAULT_GROUP = "Spiti"

split_app = FastAPI(
    title="split",
    description="A Splitwise-style expense splitter",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
security = HTTPBasic()

# Ensure the schema exists. The startup lifespan is unreliable for a host-mounted
# sub-app, so initialise at import time instead.
db.init_db()


def require_auth(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    password = get_settings().basic_auth_password
    if not password or not secrets.compare_digest(
        credentials.password.encode(), password.encode()
    ):
        raise HTTPException(
            status_code=401, detail="unauthorized", headers={"WWW-Authenticate": "Basic"}
        )


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


class SettlementCreate(BaseModel):
    from_member: int
    to_member: int
    amount: float = Field(gt=0)


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


def _group_detail(conn, gid: int) -> dict:
    group = _group_or_404(conn, gid)
    members = [dict(r) for r in conn.execute(
        "SELECT id, name FROM members WHERE group_id = ? ORDER BY id", (gid,)
    )]
    member_ids = [m["id"] for m in members]

    expenses = [
        {**dict(r), "deleted": bool(r["deleted_at"])}
        for r in conn.execute(
            """SELECT e.id, e.description, e.amount_paise, e.paid_by, e.created_at,
                      e.deleted_at, m.name AS paid_by_name
               FROM expenses e JOIN members m ON m.id = e.paid_by
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


# ---------------------------------------------------------------- routes


@split_app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@split_app.get("/", dependencies=[Depends(require_auth)])
def index() -> FileResponse:
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


@split_app.get("/api/current", dependencies=[Depends(require_auth)])
def current_group() -> dict:
    """Detail of the fixed group the UI uses (prefers DEFAULT_GROUP, else the oldest)."""
    conn = db.connect()
    try:
        row = (
            conn.execute(
                "SELECT id FROM groups WHERE name = ? ORDER BY id LIMIT 1", (DEFAULT_GROUP,)
            ).fetchone()
            or conn.execute("SELECT id FROM groups ORDER BY id LIMIT 1").fetchone()
        )
        if not row:
            raise HTTPException(status_code=404, detail="no group configured")
        return _group_detail(conn, row["id"])
    finally:
        conn.close()


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
        cur = conn.execute(
            "INSERT INTO members (group_id, name) VALUES (?, ?)", (gid, body.name.strip())
        )
        conn.commit()
        return {"id": cur.lastrowid, "name": body.name.strip(), "group_id": gid}
    finally:
        conn.close()


@split_app.post("/api/groups/{gid}/expenses", dependencies=[Depends(require_auth)])
def add_expense(gid: int, body: ExpenseCreate) -> dict:
    conn = db.connect()
    try:
        _group_or_404(conn, gid)
        valid = set(_member_ids(conn, gid))
        if not valid:
            raise HTTPException(status_code=422, detail="group has no members yet")
        if body.paid_by not in valid:
            raise HTTPException(status_code=422, detail="payer is not in this group")

        total = to_paise(body.amount)

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

        cur = conn.execute(
            "INSERT INTO expenses (group_id, description, amount_paise, paid_by) "
            "VALUES (?, ?, ?, ?)",
            (gid, body.description.strip(), total, body.paid_by),
        )
        eid = cur.lastrowid
        conn.executemany(
            "INSERT INTO expense_shares (expense_id, member_id, share_paise) VALUES (?, ?, ?)",
            [(eid, m, c) for m, c in share_map.items()],
        )
        conn.commit()
        return {"id": eid}
    finally:
        conn.close()


@split_app.delete(
    "/api/groups/{gid}/expenses/{eid}", dependencies=[Depends(require_auth)]
)
def delete_expense(gid: int, eid: int) -> dict:
    """Soft-delete: keep the row (so it stays in history and can be restored)."""
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT id FROM expenses WHERE id = ? AND group_id = ?", (eid, gid)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="expense not found")
        conn.execute(
            "UPDATE expenses SET deleted_at = datetime('now') "
            "WHERE id = ? AND deleted_at IS NULL",
            (eid,),
        )
        conn.commit()
        return {"deleted": eid}
    finally:
        conn.close()


@split_app.post(
    "/api/groups/{gid}/expenses/{eid}/restore", dependencies=[Depends(require_auth)]
)
def restore_expense(gid: int, eid: int) -> dict:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT id FROM expenses WHERE id = ? AND group_id = ?", (eid, gid)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="expense not found")
        conn.execute("UPDATE expenses SET deleted_at = NULL WHERE id = ?", (eid,))
        conn.commit()
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


@split_app.post("/api/groups/{gid}/settlements", dependencies=[Depends(require_auth)])
def add_settlement(gid: int, body: SettlementCreate) -> dict:
    conn = db.connect()
    try:
        _group_or_404(conn, gid)
        valid = set(_member_ids(conn, gid))
        if body.from_member not in valid or body.to_member not in valid:
            raise HTTPException(status_code=422, detail="member not in this group")
        if body.from_member == body.to_member:
            raise HTTPException(status_code=422, detail="cannot settle with yourself")
        cur = conn.execute(
            "INSERT INTO settlements (group_id, from_member, to_member, amount_paise) "
            "VALUES (?, ?, ?, ?)",
            (gid, body.from_member, body.to_member, to_paise(body.amount)),
        )
        conn.commit()
        return {"id": cur.lastrowid}
    finally:
        conn.close()
