"""Optional Cloud Firestore mirror of the SQLite store.

SQLite stays the source of truth. After each successful write the app schedules
a background task that re-mirrors the affected group to Firestore, so a cloud
copy converges to local state (handy for backup, dashboards, or a second
consumer). The feature is optional: with no project configured everything here
is a no-op — the same way push degrades without VAPID keys and chat without a
Mistral key — so the app runs unchanged on SQLite alone.

Design notes:
  * Every change re-writes a *full snapshot* of the group, keyed by the SQLite
    row ids (group/member/expense/settlement). That makes the mirror idempotent
    and self-healing: a replayed, duplicated, or out-of-order task simply
    rewrites the same documents to the same values.
  * It runs in a BackgroundTask after the response, so it opens its own DB
    connection (the request's is already closed) and must never raise into the
    worker — any failure is swallowed, leaving SQLite (the source of truth)
    untouched.

Firestore layout (collection / document):
    groups/{gid}
      ├─ members/{member_id}
      ├─ expenses/{expense_id}      # includes soft-delete flag + embedded shares
      └─ settlements/{settlement_id}
"""

from functools import lru_cache

from spliti import db
from spliti.config import get_settings

# Top-level collection holding one document per group.
COLLECTION = "groups"

# Firestore caps a single batched write at 500 operations; stay comfortably
# under it and commit in chunks so a large group still mirrors in one pass.
_BATCH_LIMIT = 450


def is_configured() -> bool:
    """Whether a Firestore mirror target is set. Empty project id == disabled."""
    return bool(get_settings().firestore_project_id)


@lru_cache(maxsize=1)
def _get_client():
    """Lazily build (and cache) the Firestore client.

    The import lives here, not at module top, so `google-cloud-firestore` is an
    *optional* dependency — the app imports and runs fine without it when the
    mirror is disabled. Tests patch this function with a fake client.
    """
    from google.cloud import firestore

    s = get_settings()
    project = s.firestore_project_id or None
    if s.firestore_credentials:
        # An explicit service-account JSON path; otherwise the client falls back
        # to Application Default Credentials (GOOGLE_APPLICATION_CREDENTIALS, the
        # gcloud SDK login, or GCE/Cloud Run metadata).
        return firestore.Client.from_service_account_json(
            s.firestore_credentials, project=project
        )
    return firestore.Client(project=project)


# ---------------------------------------------------------------- read snapshot


def _read_group(conn, gid: int):
    """Read a full group snapshot from SQLite, or None if the group is gone.

    Self-contained SQL (not app._group_detail) so the mirror stays decoupled
    from the request layer and reflects exactly the committed rows."""
    group = conn.execute(
        "SELECT id, name, created_at FROM groups WHERE id = ?", (gid,)
    ).fetchone()
    if not group:
        return None
    members = conn.execute(
        "SELECT id, name FROM members WHERE group_id = ? ORDER BY id", (gid,)
    ).fetchall()
    expenses = conn.execute(
        "SELECT id, description, amount_paise, paid_by, added_by, client_id, "
        "category, created_at, deleted_at FROM expenses WHERE group_id = ? ORDER BY id",
        (gid,),
    ).fetchall()
    share_rows = conn.execute(
        "SELECT s.expense_id, s.member_id, s.share_paise FROM expense_shares s "
        "JOIN expenses e ON e.id = s.expense_id WHERE e.group_id = ?",
        (gid,),
    ).fetchall()
    settlements = conn.execute(
        "SELECT id, from_member, to_member, amount_paise, client_id, created_at "
        "FROM settlements WHERE group_id = ? ORDER BY id",
        (gid,),
    ).fetchall()
    return group, members, expenses, share_rows, settlements


def _documents(group_ref, snapshot):
    """Flatten a group snapshot into (doc_ref, data) pairs to write."""
    group, members, expenses, share_rows, settlements = snapshot

    shares_by_expense: dict[int, list[dict]] = {}
    for r in share_rows:
        shares_by_expense.setdefault(r["expense_id"], []).append(
            {"member_id": r["member_id"], "share_paise": r["share_paise"]}
        )

    docs = [(group_ref, {"id": group["id"], "name": group["name"],
                         "created_at": group["created_at"]})]
    for m in members:
        docs.append((
            group_ref.collection("members").document(str(m["id"])),
            {"id": m["id"], "name": m["name"]},
        ))
    for e in expenses:
        docs.append((
            group_ref.collection("expenses").document(str(e["id"])),
            {
                "id": e["id"],
                "description": e["description"],
                "amount_paise": e["amount_paise"],
                "paid_by": e["paid_by"],
                "added_by": e["added_by"],
                "category": e["category"],
                "client_id": e["client_id"],
                "created_at": e["created_at"],
                "deleted_at": e["deleted_at"],
                "deleted": e["deleted_at"] is not None,
                "shares": shares_by_expense.get(e["id"], []),
            },
        ))
    for st in settlements:
        docs.append((
            group_ref.collection("settlements").document(str(st["id"])),
            {
                "id": st["id"],
                "from_member": st["from_member"],
                "to_member": st["to_member"],
                "amount_paise": st["amount_paise"],
                "client_id": st["client_id"],
                "created_at": st["created_at"],
            },
        ))
    return docs


# ---------------------------------------------------------------- write mirror


def mirror_group(gid: int) -> None:
    """Background entry point: re-write the whole group to Firestore.

    No-op when the mirror is unconfigured. Best-effort: any error (client build,
    network, missing dependency) is swallowed so the after-response worker never
    raises and SQLite stays the unaffected source of truth.
    """
    if not is_configured():
        return
    try:
        client = _get_client()
        conn = db.connect()
        try:
            snapshot = _read_group(conn, gid)
        finally:
            conn.close()
        if snapshot is None:
            return  # group was deleted between the write and this task — nothing to mirror
        group_ref = client.collection(COLLECTION).document(str(gid))
        docs = _documents(group_ref, snapshot)
        for i in range(0, len(docs), _BATCH_LIMIT):
            batch = client.batch()
            for ref, data in docs[i:i + _BATCH_LIMIT]:
                batch.set(ref, data)
            batch.commit()
    except Exception:
        # Runs after the response in a BackgroundTask: never raise into the worker.
        pass


def delete_group(gid: int) -> None:
    """Background entry point: remove a group (and its subcollections) from the
    mirror after it was deleted in SQLite. Best-effort and no-op when disabled."""
    if not is_configured():
        return
    try:
        client = _get_client()
        group_ref = client.collection(COLLECTION).document(str(gid))
        # recursive_delete clears the document and every nested subcollection.
        client.recursive_delete(group_ref)
    except Exception:
        pass
