# PriceWatch 🛒

A self-hosted price tracker with a web portal, automatic price checking, and alerts via Slack, Email, and Pushbullet.

## Features

- **Web portal** – add/remove products, view price history charts, pause tracking
- **Smart scraping** – JSON-LD structured data + CSS fallbacks for etos.nl, holland & barrett, and generic shops
- **Alerts** – Slack webhook, Email (SMTP), Pushbullet
- **Scheduled checks** – configurable interval (default: every 60 minutes)
- **SQLite** – zero external database needed; data persists in a Docker volume
- **Price history** – sparkline chart per product

---

## Quick Start (NAS / any Docker host)

```bash
# 1. Clone / copy this folder to your NAS
scp -r price-tracker/ nas:/opt/price-tracker

# 2. Build & start
cd /opt/price-tracker
docker compose up -d --build

# 3. Open the portal
# http://YOUR-NAS-IP:8080
```

---

## Directory Structure

```
price-tracker/
├── backend/
│   ├── app.py            # Flask API + scraper + scheduler
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/
│   ├── index.html        # Single-page web portal
│   ├── nginx.conf        # Reverse proxy: /api → backend
│   └── Dockerfile
└── docker-compose.yml
```

---

## Configuration (via web portal → Settings)

| Setting | Description |
|---------|-------------|
| Slack Webhook URL | `https://hooks.slack.com/services/XXX/YYY/ZZZ` |
| SMTP Host/Port | e.g. `smtp.gmail.com` / `587` |
| SMTP User/Pass | Your email + app password |
| Notify Email | Where to send alerts |
| Pushbullet API Key | From pushbullet.com → Account |
| Check Interval | Minutes between automatic checks (min: 5) |

### Gmail App Password
1. Enable 2FA on your Google account
2. Go to myaccount.google.com → Security → App Passwords
3. Generate a password for "Mail" and use it as SMTP password

---

## Adding Products

1. Open `http://NAS-IP:8080`
2. Click **+ Add Product**
3. Paste any product URL (etos.nl, hollandandbarrett.nl, or any shop with structured data)
4. Set the discount threshold (e.g. 10 = alert only when ≥10% off)
5. Click **Add Product** – price is fetched immediately

---

## Supported Shops

| Shop | Method |
|------|--------|
| etos.nl | JSON-LD + CSS fallback |
| hollandandbarrett.nl | JSON-LD + CSS fallback |
| Any shop with JSON-LD | Automatic |
| Any shop with Open Graph price meta | Automatic |
| Generic fallback | CSS class heuristics |

---

## Synology NAS Notes

- Use **Container Manager** (DSM 7.2+) or SSH + `docker compose`
- Map port `8080` in the port forwarding settings
- Data persists in the `pricewatch-data` Docker volume

---

## Updating

```bash
cd /opt/price-tracker
docker compose pull
docker compose up -d --build
```

---

## Troubleshooting

**Price shows as "Pending"** – The scraper couldn't extract a price. Some shops use JavaScript rendering; open an issue with the URL.

**No alerts received** – Check Settings → save → then use "Check All" to trigger a manual check with a known sale item.

**Rebuild after code change** – `docker compose up -d --build`
