"""Web Push (VAPID) delivery and per-member preferences.

The feature is optional: with no VAPID keys configured everything here turns into
a no-op and the API reports it as unavailable, the same way chat degrades without
a Mistral key.

Delivery decision per recipient per event (the "most specific wins" rule): if the
event moved that member's balance and their `balance` preference is on, they get a
personalised ping; otherwise they get the generic event ping if its category is
on; otherwise nothing. The member who performed the action is never notified.
"""

import json

from spliti import db
from spliti.config import get_settings

# Notification categories == columns in notify_prefs (all default on).
PREF_COLUMNS = ("new_expense", "settlement", "balance", "delete_restore")


def is_configured() -> bool:
    s = get_settings()
    return bool(s.vapid_private_key and s.vapid_public_key)


def public_key() -> str:
    return get_settings().vapid_public_key


# ---------------------------------------------------------------- preferences


def get_prefs(conn, member_id: int) -> dict:
    """Current member's prefs; a missing row means everything is on."""
    row = conn.execute(
        "SELECT new_expense, settlement, balance, delete_restore "
        "FROM notify_prefs WHERE member_id = ?",
        (member_id,),
    ).fetchone()
    if not row:
        return {c: True for c in PREF_COLUMNS}
    return {c: bool(row[c]) for c in PREF_COLUMNS}


def set_prefs(conn, member_id: int, prefs: dict) -> dict:
    vals = [1 if prefs.get(c, True) else 0 for c in PREF_COLUMNS]
    conn.execute(
        "INSERT INTO notify_prefs (member_id, new_expense, settlement, balance, delete_restore) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(member_id) DO UPDATE SET "
        "new_expense = excluded.new_expense, settlement = excluded.settlement, "
        "balance = excluded.balance, delete_restore = excluded.delete_restore",
        (member_id, *vals),
    )
    conn.commit()
    return get_prefs(conn, member_id)


# ---------------------------------------------------------------- subscriptions


def save_subscription(conn, member_id: int, endpoint: str, p256dh: str, auth: str) -> None:
    """Upsert on endpoint, so re-subscribing the same device just refreshes it
    (and re-points it at the current member)."""
    conn.execute(
        "INSERT INTO push_subscriptions (member_id, endpoint, p256dh, auth) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(endpoint) DO UPDATE SET "
        "member_id = excluded.member_id, p256dh = excluded.p256dh, auth = excluded.auth",
        (member_id, endpoint, p256dh, auth),
    )
    conn.commit()


def delete_subscription(conn, endpoint: str, member_id: int | None = None) -> None:
    """Remove a subscription by endpoint. Pass member_id to scope the delete to the
    member who owns it (so one member can't unsubscribe another's device); leave it
    None for internal pruning, where we already matched the endpoint to a member."""
    if member_id is None:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
    else:
        conn.execute(
            "DELETE FROM push_subscriptions WHERE endpoint = ? AND member_id = ?",
            (endpoint, member_id),
        )
    conn.commit()


def _subscriptions_for(conn, member_id: int):
    return conn.execute(
        "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE member_id = ?",
        (member_id,),
    ).fetchall()


def group_has_subscribers(conn, gid: int) -> bool:
    """Cheap gate so the write path skips the balance snapshot and the dispatch
    task entirely when nobody in the group has a push subscription (the feature can
    be configured server-side with zero subscribers)."""
    row = conn.execute(
        "SELECT 1 FROM push_subscriptions s JOIN members m ON m.id = s.member_id "
        "WHERE m.group_id = ? LIMIT 1",
        (gid,),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------- messages


def _rupees(paise: int) -> str:
    r = paise / 100
    return f"₹{r:,.0f}" if paise % 100 == 0 else f"₹{r:,.2f}"


def _balance_line(net_paise: int) -> str:
    if net_paise > 0:
        return f"You're owed {_rupees(net_paise)}"
    if net_paise < 0:
        return f"You owe {_rupees(-net_paise)}"
    return "You're all settled up"


def _build_payload(event_type: str, category: str, summary: dict, net_paise: int) -> dict:
    """Notification body for one recipient. `category` is either the event type
    (generic message) or 'balance' (event context + the recipient's net)."""
    if event_type == "new_expense":
        ctx = f"{summary['actor_name']} added “{summary['description']}” · {_rupees(summary['amount_paise'])}"
    elif event_type == "settlement":
        ctx = f"{summary['from_name']} paid {summary['to_name']} {_rupees(summary['amount_paise'])}"
    else:  # delete_restore
        verb = "restored" if summary.get("restored") else "deleted"
        ctx = f"{summary['actor_name']} {verb} “{summary['description']}”"

    body = f"{ctx}. {_balance_line(net_paise)}." if category == "balance" else ctx
    # A tag unique to the event so distinct events stack instead of overwriting each
    # other in the tray (a constant tag collapses a burst into a single banner).
    tag = f"spliti-{event_type}-{summary.get('event_id', '')}"
    return {"title": "Spliti", "body": body, "url": "/", "tag": tag}


def _choose_category(prefs: dict, event_type: str, affected: bool) -> str | None:
    """Most-specific-wins: a balance-affecting event prefers the personalised ping."""
    if affected and prefs.get("balance"):
        return "balance"
    if prefs.get(event_type):
        return event_type
    return None


# ---------------------------------------------------------------- send


def _send_one(endpoint: str, p256dh: str, auth: str, payload: dict) -> bool:
    """Send a single push. Returns False only when the subscription is gone
    (404/410) and should be pruned; any other error is swallowed as transient."""
    try:
        from pywebpush import WebPushException, webpush
    except Exception:
        # Dependency missing/broken — can't send, but keep the subscription.
        return True

    s = get_settings()
    try:
        webpush(
            subscription_info={"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}},
            data=json.dumps(payload),
            vapid_private_key=s.vapid_private_key,
            vapid_claims={"sub": s.vapid_subject},
            timeout=10,
        )
        return True
    except WebPushException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        return status not in (404, 410)
    except Exception:
        return True


def dispatch(
    gid: int,
    event_type: str,
    actor_member_id: int | None,
    affected_ids: list[int],
    net_by_member: dict[int, int],
    summary: dict,
) -> None:
    """Background entry point: notify every eligible member of the group.

    Runs after the response via BackgroundTasks, so it opens its own connection
    (the request's is already closed) and must never raise into the caller.
    """
    if not is_configured():
        return
    conn = db.connect()
    try:
        members = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM members WHERE group_id = ? ORDER BY id", (gid,)
            )
        ]
        affected = set(affected_ids or [])
        for m in members:
            if m == actor_member_id:
                continue
            # Isolate each recipient: a locked write or a bad row for one member
            # must not abort the rest (and dispatch must never raise — see below).
            try:
                category = _choose_category(get_prefs(conn, m), event_type, m in affected)
                if not category:
                    continue
                payload = _build_payload(event_type, category, summary, net_by_member.get(m, 0))
                for sub in _subscriptions_for(conn, m):
                    if not _send_one(sub["endpoint"], sub["p256dh"], sub["auth"], payload):
                        delete_subscription(conn, sub["endpoint"])
            except Exception:
                continue
    except Exception:
        # Runs after the response in a BackgroundTask: never raise into the worker.
        pass
    finally:
        conn.close()
