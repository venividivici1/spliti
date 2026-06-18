"""Generate a VAPID key pair for Web Push.

Run once and put the output in your environment (or .env):

    python scripts/gen_vapid.py

    VAPID_PRIVATE_KEY  -> a secret; keep it out of version control
    VAPID_PUBLIC_KEY   -> the application server key handed to the browser
    VAPID_SUBJECT      -> a contact mailto:/https: URL (defaults are fine)

Requires the `pywebpush` dependency (it pulls in `py-vapid` + `cryptography`).
"""

import base64

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid01


def main() -> None:
    vapid = Vapid01()
    vapid.generate_keys()

    private_pem = vapid.private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    raw_public = vapid.public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    application_server_key = base64.urlsafe_b64encode(raw_public).rstrip(b"=").decode()

    print("# --- add these to your environment / .env ---")
    print(f'VAPID_PRIVATE_KEY="{private_pem.strip()}"')
    print(f"VAPID_PUBLIC_KEY={application_server_key}")
    print("VAPID_SUBJECT=mailto:you@example.com")


if __name__ == "__main__":
    main()
