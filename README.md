# Spliti

A Splitwise-style expense splitter, built for a friends' road trip to **Spiti**
(a Himalayan region in India). One FastAPI app with a single-file web UI.

Money crosses the API as decimal rupee amounts but is stored and computed as
integer **paise** throughout, so balances always reconcile exactly.

## Features

- **Groups, members, expenses, settlements** — the usual shared-ledger model.
- **Equal or exact splits** — split evenly (leftover paise distributed
  deterministically) or specify exact per-member shares that must sum to the total.
- **Balances + settle-up** — net balances per member and a greedy min-cash-flow
  suggestion for who should pay whom.
- **Member sign-in + attribution** — you sign in with your name (must be one of
  the group's members; `GET /api/me` validates it). Each expense records both
  who **paid** and who **added** it (`added_by`), shown side by side in the
  expense detail; an arbitrary name can't post expenses.
- **Soft delete** — deleted expenses stay in history and can be restored.
- **Installable (PWA)** — "Add to Home Screen" on Android and iOS installs Spliti
  as a standalone app (web manifest + service worker + icons). The PWA assets
  (`/manifest.webmanifest`, `/sw.js`, `/icons/*`) are served without auth; the
  app shell and `/api/*` stay behind Basic Auth.
- **AI extras (optional, needs `MISTRAL_API_KEY`)**
  - `/api/groups/{id}/ask` — chat Q&A over the group's expenses.
  - `/api/suggest-description` — a time-of-day expense suggestion, optionally
    location-tagged via OpenStreetMap reverse-geocoding.

The UI locks onto a single group (`DEFAULT_GROUP = "Spiti"`); group/member
management happens out of band against the API.

## Setup

Configure the environment, then create a virtualenv and install. This box uses
[`uv`](https://docs.astral.sh/uv/) (plain `python -m venv` + `pip` works too if
`python3-venv` is installed):

```sh
cp .env.example .env          # set BASIC_AUTH_PASSWORD (and MISTRAL_API_KEY for AI)

# with uv
uv venv .venv
source .venv/bin/activate
uv pip install -e ".[dev]"

# …or with stdlib venv + pip
# python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
```

## Run

### Local / development

```sh
source .venv/bin/activate
uvicorn spliti.app:split_app --reload --port 8000
```

Then open <http://localhost:8000> and log in with **any username** and the
`BASIC_AUTH_PASSWORD` you set. The SQLite database is created at
`spliti/split.db` on first run (the schema self-initialises at import time, so
there is no separate migration step).

Without a venv activated, call the binary directly:

```sh
.venv/bin/uvicorn spliti.app:split_app --reload --port 8000
```

### Production (systemd + cloudflared)

In the live deployment Spliti runs as a systemd service on `127.0.0.1:8001`
behind a Cloudflare tunnel at <https://spliti.codexvault.org>. See
[`deploy/README.md`](deploy/README.md) for the unit file and tunnel ingress.

```sh
sudo systemctl restart spliti-app.service     # after backend changes
sudo journalctl -u spliti-app.service -f      # tail logs
```

The frontend is a single static `spliti/static/index.html` served per request,
so UI edits are live on the next page load without a restart.

## Tests

```sh
source .venv/bin/activate
pytest -q          # 36 tests; enforces ≥85% coverage (see pyproject.toml)
```

## Layout

```
spliti/
├── app.py        # FastAPI app: routes, auth, request models
├── config.py     # settings from env / .env
├── ai.py         # shared Mistral client
├── ask.py        # AI Q&A + description suggestions + reverse-geocoding
├── balances.py   # net-balance + settle-up math (pure, integer paise)
├── db.py         # SQLite schema, connection, idempotent migrations
├── split.db      # local SQLite data (git-ignored)
└── static/
    ├── index.html          # the entire single-file web UI
    ├── manifest.webmanifest # PWA manifest (installable app metadata)
    ├── sw.js               # service worker (standalone/offline shell)
    └── icons/              # app icons (Android maskable + iOS apple-touch)
```

> Extracted from the `fucku` GitHub-App monorepo, where Spliti previously ran as
> a host-mounted child app at `spliti.codexvault.org`.
