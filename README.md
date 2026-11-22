# AquaPerk — Water Refill Loyalty Programme

A classy navy-and-gold loyalty system for a water refilling station.
**Python (Flask) + PostgreSQL**, with two staff roles: **Admin** and **Manager**.

## How the programme works

| Rule | Behaviour |
|---|---|
| Earning | Every refill scan adds **1 point** to the client |
| Reward | After the **10th** refill, the **next refill is free** |
| Restart | Redeeming the free refill resets the count to **0** |
| Cooldown | A client can't earn another point for **10 minutes** (configurable) |
| Walk-ins | Non-members can be recorded as a walk-in sale so daily sales stay complete |

## Roles

| Capability | Manager | Admin |
|---|:--:|:--:|
| Sign in, change own password | ✓ | ✓ |
| Register clients (name + phone) → QR card | ✓ | ✓ |
| Scan QR / look up by phone to add a point | ✓ | ✓ |
| Record walk-in sales | ✓ | ✓ |
| View daily & overall sales | ✓ | ✓ |
| **Delete client accounts** | | ✓ |
| Add / deactivate / delete managers, reset their passwords | | ✓ |
| Change price, currency, business name | | ✓ |
| Sales by staff member, CSV export | | ✓ |

## Quick start

```bash
# 1. PostgreSQL (or use your own server)
docker compose up -d

# 2. Python environment
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. Configuration
cp .env.example .env          # then set SECRET_KEY (see comment inside)

# 4. Create tables + first admin (prompts for a password)
python init_db.py             # add --demo to load sample clients and sales

# 5. Run
python app.py                 # http://localhost:5000
```

For production use `gunicorn -w 3 app:app` behind HTTPS (nginx, Caddy, etc.).

## Daily use

1. **New client** → enter name and phone → the account and QR card are created instantly.
2. On the client page, **Open card** (public link the client can keep on their phone), **Download QR**, or copy the link.
3. At each refill open **Scan & Refill**, tap *Start camera* and scan the client's QR. One point is added.
   - Forgot the QR? Type their phone number instead.
   - Scanned too soon? You'll see a live countdown until the next point is allowed.
   - On the 11th refill the screen shows **Free refill redeemed** and the count restarts.
4. **Sales** shows today, 7/14/30/90-day views with a chart, monthly totals and all-time totals.

## Project layout

```
aquaperk/
├── app.py              Flask app: routes, loyalty rules, reports
├── db.py               PostgreSQL connection pool + transaction helper
├── schema.sql          Tables (users, clients, refills, settings)
├── init_db.py          Creates tables, first admin, optional demo data
├── requirements.txt
├── docker-compose.yml  Local PostgreSQL
├── .env.example
├── templates/          Jinja pages (dashboard, scan, clients, sales, staff, ...)
└── static/
    ├── css/style.css   Navy & gold theme, responsive
    └── js/scan.js      Camera scanner + phone lookup + walk-ins
```

## Database

- **users** – staff logins (`role` is `admin` or `manager`, password hashes only)
- **clients** – name, unique phone, secret `qr_token`, `points` (0–10), totals, `last_refill_at`
- **refills** – one row per refill: client, staff, free or paid, amount, timestamp
- **settings** – business name, price per refill, currency

Deleting a client or staff member **never deletes sales**: their refill rows stay (links become empty), so daily and overall totals remain correct.

## Design notes & safety

- **Double-scan safe:** the client row is locked inside a transaction, so two simultaneous scans can't both pass the cooldown.
- **QR content:** the QR encodes the client's card link with a random, unguessable token (not their phone number). Deleting the client kills the QR.
- **Security:** hashed passwords, CSRF protection on every POST, HttpOnly/SameSite cookies, login throttling (8 failures / 10 min per IP), HTML-escaped output, role checks on every admin route.
- **Camera access** needs **HTTPS** (or `localhost`). Set `SESSION_COOKIE_SECURE=1` and `BASE_URL=https://your-domain` in production so QR links open for clients' phones.
- Sales dates use `TIMEZONE` (default `Africa/Nairobi`), so "today" matches your trading day.
- The scanner loads `html5-qrcode` and the fonts from public CDNs; copy them into `static/` if the station must work offline.

## Customising

- Change the cooldown: `COOLDOWN_MINUTES` in `.env`.
- Change the reward threshold: edit `REWARD_AT` in `app.py` **and** the `CHECK (points BETWEEN 0 AND 10)` in `schema.sql`.
- Brand colours live at the top of `static/css/style.css` (`--navy-*`, `--gold-*`).
- Phone numbers starting with `0` are stored with `DEFAULT_COUNTRY_CODE` (default `254`).
