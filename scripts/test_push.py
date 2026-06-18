"""Diagnose why Web Push banners aren't arriving.

Unlike the app (which swallows send errors as "transient"), this prints the real
result for every stored subscription, so a bad VAPID key/subject, a 401/403 from
the push service, a missing dependency, or simply "no subscriptions" all become
visible. Run it in the SAME environment as the server, against the live DB:

    python scripts/test_push.py --db /path/to/live/split.db
    python scripts/test_push.py --db /path/to/live/split.db --send   # actually deliver

Without --send it only reports config + subscriptions (no delivery). With --send it
pushes a real "Spliti test" notification to every subscription and prints status.
"""

import argparse
import json
import sys

from spliti import db, notifications
from spliti.config import get_settings


def main() -> None:
    ap = argparse.ArgumentParser(description="Diagnose Web Push delivery.")
    ap.add_argument("--db", help="path to the live SQLite file (default: app's split.db)")
    ap.add_argument("--send", action="store_true", help="actually send a test push")
    args = ap.parse_args()

    s = get_settings()
    print("== config ==")
    print(f"  DB path:           {db.DB_PATH if not args.db else args.db}")
    pk = s.vapid_private_key or ""
    fmt = ("missing" if not pk
           else "PEM (multi-line, ok)" if pk.lstrip().startswith("-----") and "\n" in pk
           else "PEM but FLATTENED — newlines lost (will be repaired)"
           if "-----BEGIN" in pk and "\n" not in pk and "\\n" not in pk
           else "PEM with literal \\n (will be repaired)" if "\\n" in pk
           else "raw / unknown")
    print(f"  VAPID_PRIVATE_KEY: {'set' if pk else 'MISSING'} [{fmt}]")
    print(f"  VAPID_PUBLIC_KEY:  {'set' if s.vapid_public_key else 'MISSING'}")
    print(f"  VAPID_SUBJECT:     {s.vapid_subject or 'MISSING'}")
    if not (s.vapid_private_key and s.vapid_public_key):
        sys.exit("\nVAPID keys are not set in THIS environment — the feature is dormant "
                 "here.\nMake sure the script runs with the same env/.env as the server.")
    if s.vapid_subject and not s.vapid_subject.startswith(("mailto:", "https://")):
        print("  WARNING: VAPID_SUBJECT should be a mailto: or https: URL — many push "
              "services reject anything else with a 400/403.")

    try:
        from pywebpush import WebPushException, webpush
    except Exception as e:
        sys.exit(f"\npywebpush import failed ({e!r}) — the dependency isn't installed in "
                 "this environment, so no push can ever be sent.")

    conn = db.connect(args.db)
    try:
        subs = conn.execute(
            "SELECT s.endpoint, s.p256dh, s.auth, m.name, m.group_id "
            "FROM push_subscriptions s JOIN members m ON m.id = s.member_id "
            "ORDER BY m.name"
        ).fetchall()
    finally:
        conn.close()

    print(f"\n== subscriptions ({len(subs)}) ==")
    if not subs:
        sys.exit("No subscriptions stored. Nobody has successfully enabled notifications "
                 "on this DB — the toggle either never granted permission, hit a 503 "
                 "(VAPID not set when they subscribed), or wrote to a different DB file.")
    for r in subs:
        host = r["endpoint"].split("/")[2] if "://" in r["endpoint"] else r["endpoint"]
        print(f"  {r['name']:<12} via {host}")

    if not args.send:
        print("\n(Re-run with --send to actually deliver a test notification.)")
        return

    payload = json.dumps({"title": "Spliti test", "body": "If you see this, push works.",
                          "url": "/", "tag": "spliti-test"})
    print("\n== sending ==")
    for r in subs:
        try:
            webpush(
                subscription_info={"endpoint": r["endpoint"],
                                   "keys": {"p256dh": r["p256dh"], "auth": r["auth"]}},
                data=payload,
                vapid_private_key=notifications.normalize_vapid_private_key(s.vapid_private_key),
                vapid_claims={"sub": s.vapid_subject},
                timeout=10,
            )
            print(f"  {r['name']:<12} OK")
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", "?")
            body = getattr(getattr(e, "response", None), "text", "")
            print(f"  {r['name']:<12} FAILED http={code} {str(e)[:160]}")
            if body:
                print(f"               body: {body[:200]}")
        except Exception as e:
            print(f"  {r['name']:<12} ERROR {e!r}")
    print("\nDelivery is the push service accepting the request; the banner itself still "
          "depends on the device being backgrounded and permission being granted.")


if __name__ == "__main__":
    main()
