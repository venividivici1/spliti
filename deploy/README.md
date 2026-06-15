# Deploy

Spliti runs as its own systemd service behind the shared `fucku` cloudflared
tunnel, independent of the fucku GitHub App.

## Service

`spliti-app.service` runs `uvicorn spliti.app:split_app` on `127.0.0.1:8001`.

```sh
sudo cp deploy/spliti-app.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now spliti-app.service
```

## Tunnel routing

The cloudflared tunnel (`~/.cloudflared/config.yml`, service
`cloudflared-fucku.service`) routes the Spliti hostnames to this service while
`bot.codexvault.org` stays on fucku (`:8000`):

```yaml
ingress:
  - hostname: bot.codexvault.org      # fucku
    service: http://127.0.0.1:8000
  - hostname: spliti.codexvault.org   # Spliti
    service: http://127.0.0.1:8001
  - service: http_status:404
```

After editing the config: `sudo systemctl restart cloudflared-fucku.service`.

> Note: fucku's `app/main.py` still has dead `app.host("spliti…")` mounts. They
> no longer receive traffic and can be removed from fucku independently.
