# Dealer Platform — Production Deployment (Cloudflare Tunnel)

This puts the four apps on your real domain over HTTPS, with **no port-forwarding**
and **no exposed IP**, while the compute stays on this LAN box so it can keep
reaching SQL Server `10.1.1.10` (VIN / inventory / auction data).

## Architecture

```
                          Cloudflare (TLS, DDoS)
  customer ──https──▶  trade.YOURDOMAIN.com  ─┐
                       leads.YOURDOMAIN.com   │   cloudflared (this Mac)
                       credit.YOURDOMAIN.com  ├─▶ localhost:5001 / 5002 / 5003 / 5050
                       admin.YOURDOMAIN.com  ─┘        (waitress, on the LAN)
                                                         │
                                                         └─▶ SQL Server 10.1.1.10
```

| Subdomain | App | Local port |
|---|---|---|
| `trade.YOURDOMAIN.com` | Trade-In widget | 5001 |
| `leads.YOURDOMAIN.com` | Dealer Lead Form | 5002 |
| `credit.YOURDOMAIN.com` | Credit Estimator | 5003 |
| `admin.YOURDOMAIN.com` | Platform Admin | 5050 |

The dealer is in the URL path (`/t/DEMO`), so one set of subdomains serves every
dealer. Subdomains (not path prefixes) are used so each app's `/api/...` and
`/offer` routes never collide.

## What's already done (app side)
- All four apps run under **waitress** (production WSGI, debug off) via each app's
  `serve_prod.py` + `run_server.sh`, supervised by their existing LaunchAgents.
- **ProxyFix** is enabled on every app, so `url_for(_external=True)` emits
  `https://<subdomain>` behind the tunnel — the offer recall links, QR codes,
  and emailed/texted links all become your real domain automatically.
- Strong per-app `FLASK_SECRET_KEY` and an admin `SETUP_PASSWORD` are set in each
  app's `.env` (see "Secrets" below).

So once the tunnel is up and DNS resolves, the links are correct with no code change.

---

## Prerequisites
1. A domain whose DNS is managed by **Cloudflare** (add the site to Cloudflare,
   update the registrar's nameservers — this is the part you're working on).
2. A Cloudflare account with access to that zone.

## Step 1 — Install cloudflared
```bash
brew install cloudflared
cloudflared --version
```

## Step 2 — Authenticate
```bash
cloudflared tunnel login
```
Pick your domain in the browser window that opens. This writes
`~/.cloudflared/cert.pem`.

## Step 3 — Create the tunnel
```bash
cloudflared tunnel create dealer-platform
```
Note the **Tunnel ID** it prints and the credentials file it writes
(`~/.cloudflared/<TUNNEL_ID>.json`).

## Step 4 — Configure ingress
```bash
cp /Users/markrankin/claude/deploy/cloudflared-config.example.yml ~/.cloudflared/config.yml
```
Edit `~/.cloudflared/config.yml`:
- replace `<TUNNEL_ID>` in `credentials-file` with your Tunnel ID,
- replace every `YOURDOMAIN.com` with your domain.

## Step 5 — Route DNS (one per subdomain)
```bash
cloudflared tunnel route dns dealer-platform trade.YOURDOMAIN.com
cloudflared tunnel route dns dealer-platform leads.YOURDOMAIN.com
cloudflared tunnel route dns dealer-platform credit.YOURDOMAIN.com
cloudflared tunnel route dns dealer-platform admin.YOURDOMAIN.com
```
(Each creates a proxied CNAME to the tunnel in Cloudflare DNS.)

## Step 6 — Test it
```bash
cloudflared tunnel --config ~/.cloudflared/config.yml run dealer-platform
```
Then visit `https://trade.YOURDOMAIN.com/t/DEMO`. When it works, Ctrl-C.

## Step 7 — Run the tunnel persistently
Easiest (system daemon, auto-starts on boot):
```bash
sudo cloudflared service install
```
…or, to match the other apps as a **user LaunchAgent**:
```bash
cp /Users/markrankin/claude/deploy/com.dealerplatform.tunnel.plist ~/Library/LaunchAgents/
# adjust the cloudflared path inside if you're on Intel (/usr/local/bin/cloudflared)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dealerplatform.tunnel.plist
launchctl list | grep dealerplatform.tunnel
```

---

## Hardening (do before going live)

- **Lock down the admin subdomain.** `admin.YOURDOMAIN.com` is the keys to every
  dealer. It already requires `SETUP_PASSWORD`, but also put it behind
  **Cloudflare Access** (Zero Trust → Access → Application → self-hosted →
  `admin.YOURDOMAIN.com`, policy = your email / Google login). That way the admin
  never even reaches the app without authentication.
- **Secrets** live only in each app's `.env` (git-ignored): `FLASK_SECRET_KEY`
  (set, unique per app), `SETUP_PASSWORD` (set for the admin — value is in
  `platform/.env`), SQL creds, and the SendGrid keys. Never commit `.env`.
- **Optional: bind apps to localhost only.** Right now they listen on `0.0.0.0`
  (so the LAN can hit them directly too). Once everything goes through the tunnel,
  you can change `host="0.0.0.0"` → `"127.0.0.1"` in each `serve_prod.py` so the
  apps are reachable *only* via cloudflared.
- **Email/SendGrid** already sends from the authenticated `noreply@panafax.ai`,
  so delivery keeps working; the links inside emails become your new domain
  automatically (ProxyFix). If you want the *from* address on the new domain,
  authenticate that domain in SendGrid too.

## Operations

- **Restart one app** (never `pkill -f serve_prod.py` — that hits all four and
  triggers a launchd restart-throttle outage):
  ```bash
  launchctl kickstart -k gui/$(id -u)/com.tradein.web      # or com.dealerleads.web / com.creditestimator.web / com.platformadmin.web
  ```
- **Tunnel logs:** `deploy/tunnel.out.log` / `tunnel.err.log` (LaunchAgent) or
  `cloudflared tunnel info dealer-platform`.
- **App logs:** each app dir's `web.out.log` / `web.err.log`.
- **Valuation caches** refresh automatically: `com.tradein.refresh` (nightly,
  inventory counts) and `com.tradein.refresh.vin8` (weekly, prefix map).

## Quick verification checklist
- [ ] `https://trade.YOURDOMAIN.com/t/DEMO` loads with the banner + HTTPS lock
- [ ] Complete a trade-in → thank-you shows the certificate + barcode
- [ ] "Email me my offer" arrives, and the **offer link inside it is your domain**
- [ ] `https://admin.YOURDOMAIN.com` prompts for the password (or Cloudflare Access)
