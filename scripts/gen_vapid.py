"""Generate a VAPID key pair for Web Push.

    python scripts/gen_vapid.py

Writes the private key to a file (default ``vapid_private.pem``) and prints the
three env vars to add to your environment / .env. The private key is referenced
by **path**, not pasted inline — a multi-line PEM stuffed into a shell `export`,
a systemd `Environment=`, or a hand-edited `.env` is easily mangled (lost line
breaks / stray quotes), which makes pywebpush fail with "Could not deserialize
key data" and silently breaks delivery. A path sidesteps all of that.

    VAPID_PRIVATE_KEY  -> path to the key file (secret; keep it out of git)
    VAPID_PUBLIC_KEY   -> the application server key handed to the browser
    VAPID_SUBJECT      -> a contact mailto:/https: URL

Requires the `pywebpush` dependency (it pulls in `py-vapid` + `cryptography`).
"""

import argparse
import base64
import os
import stat
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid01


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a VAPID key pair for Web Push.")
    ap.add_argument(
        "--out", default="vapid_private.pem",
        help="file to write the private key to (default: vapid_private.pem)",
    )
    ap.add_argument(
        "--force", action="store_true",
        help="overwrite the key file if it already exists",
    )
    args = ap.parse_args()

    out = Path(args.out)
    if out.exists() and not args.force:
        raise SystemExit(
            f"{out} already exists — refusing to overwrite (pass --force). "
            "Overwriting a live key invalidates every existing subscription."
        )

    vapid = Vapid01()
    vapid.generate_keys()

    private_pem = vapid.private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    out.write_text(private_pem)
    os.chmod(out, stat.S_IRUSR | stat.S_IWUSR)  # 0600 — readable only by the owner

    raw_public = vapid.public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    application_server_key = base64.urlsafe_b64encode(raw_public).rstrip(b"=").decode()

    print(f"# wrote private key to {out.resolve()} (mode 600)")
    print("# --- add these to your environment / .env ---")
    print(f"VAPID_PRIVATE_KEY={out.resolve()}")
    print(f"VAPID_PUBLIC_KEY={application_server_key}")
    print("VAPID_SUBJECT=mailto:you@example.com")
    print(f"# and keep the key file out of git:  echo {out.name} >> .gitignore")


if __name__ == "__main__":
    main()
