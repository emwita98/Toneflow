"""AquaPerk · Water Refill Loyalty Programme (Flask + PostgreSQL)."""
import csv
import io
import math
import os
import re
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.errors
import qrcode
import qrcode.constants
from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, send_file, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

try:  # optional: load variables from a local .env file
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import db

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
REWARD_AT = 10  # after the 10th paid refill, the next one is free
COOLDOWN_MINUTES = int(os.environ.get("COOLDOWN_MINUTES", "10"))
COOLDOWN = timedelta(minutes=COOLDOWN_MINUTES)
TZ_NAME = os.environ.get("TIMEZONE", "Africa/Nairobi")
TZ = ZoneInfo(TZ_NAME)
COUNTRY_CODE = os.environ.get("DEFAULT_COUNTRY_CODE", "254")

DEFAULT_SETTINGS = {"business_name": "AquaPerk", "price": "20.00", "currency": "KES"}

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or "dev-only-change-me",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)
if not os.environ.get("SECRET_KEY"):
    app.logger.warning("SECRET_KEY is not set - using an insecure development key.")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def card_url(token):
    base = os.environ.get("BASE_URL", "").rstrip("/")
    if base:
        return f"{base}{url_for('card', token=token)}"
    return url_for("card", token=token, _external=True)


app.jinja_env.globals.update(
    csrf_token=csrf_token,
    card_url=card_url,
    REWARD_AT=REWARD_AT,
    COOLDOWN_MINUTES=COOLDOWN_MINUTES,
)


def get_settings():
    if "settings" not in g:
        values = dict(DEFAULT_SETTINGS)
        try:
            with db.transaction() as cur:
                cur.execute("SELECT key, value FROM settings")
                values.update({r["key"]: r["value"] for r in cur.fetchall()})
        except Exception:  # keep error pages renderable if the DB is down
            pass
        g.settings = values
    return g.settings


def current_price(cur):
    cur.execute("SELECT value FROM settings WHERE key = 'price'")
    row = cur.fetchone()
    try:
        return Decimal(row["value"]) if row else Decimal(DEFAULT_SETTINGS["price"])
    except InvalidOperation:
        return Decimal(DEFAULT_SETTINGS["price"])


def normalize_phone(raw):
    """Return digits-only phone with country code, or None if it looks invalid."""
    raw = (raw or "").strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if raw.startswith("+"):
        pass
    elif digits.startswith("0"):
        digits = COUNTRY_CODE + digits[1:]
    elif len(digits) == 9:
        digits = COUNTRY_CODE + digits
    return digits if 9 <= len(digits) <= 15 else None


def extract_token(code):
    """Accept a bare token or a full card URL (what the QR code contains)."""
    code = (code or "").strip()
    if not code:
        return None
    token = code.split("#")[0].split("?")[0].rstrip("/").split("/")[-1]
    return token if re.fullmatch(r"[A-Za-z0-9_-]{10,64}", token) else None


def fmt_wait(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


@app.template_filter("money")
def money_filter(value):
    return f"{get_settings()['currency']} {Decimal(value or 0):,.2f}"


@app.template_filter("localdt")
def localdt_filter(value, fmt="%d %b %Y, %H:%M"):
    if not value:
        return "-"
    return value.astimezone(TZ).strftime(fmt)


@app.template_filter("localday")
def localday_filter(value):
    return value.strftime("%a %d %b %Y")


@app.template_filter("phone")
def phone_filter(value):
    return f"+{value}" if value else ""


@app.template_filter("maskphone")
def maskphone_filter(value):
    value = value or ""
    return f"+{value[:3]} ••• ••{value[-3:]}" if len(value) > 6 else "••••"


@app.context_processor
def inject_globals():
    s = get_settings()
    return {"brand": s["business_name"], "currency": s["currency"],
            "current_user": g.get("user")}


# --------------------------------------------------------------------------
# Request lifecycle: session user + CSRF protection
# --------------------------------------------------------------------------
@app.before_request
def load_user_and_check_csrf():
    g.user = None
    if request.endpoint == "static":
        return
    uid = session.get("uid")
    if uid:
        with db.transaction() as cur:
            cur.execute("SELECT id, username, full_name, role, is_active FROM users WHERE id = %s", (uid,))
            user = cur.fetchone()
        if user and user["is_active"]:
            g.user = user
        else:
            session.clear()
    if request.method == "POST":
        sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
        expected = session.get("csrf", "")
        if not expected or not secrets.compare_digest(sent.encode(), expected.encode()):
            abort(400, "Your session expired. Please refresh the page and try again.")


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not g.user:
            if request.path.startswith("/api/"):
                return jsonify(status="error", title="Signed out",
                               message="Please sign in again."), 401
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if g.user["role"] != "admin":
            abort(403, "This area is for administrators only.")
        return view(*args, **kwargs)
    return wrapper


@app.errorhandler(psycopg2.Error)
def database_error(e):
    app.logger.exception("Database error")
    if request.path.startswith("/api/"):
        return jsonify(status="error", title="Database problem",
                       message="Something went wrong saving that. Please try again."), 500
    return render_template("error.html", code=500, title="Database problem",
                           message="We couldn't reach the database. Please try again shortly."), 500


def http_error(e):
    code = getattr(e, "code", 500)
    message = getattr(e, "description", "Something went wrong.")
    if request.path.startswith("/api/"):
        return jsonify(status="error", title="Error", message=message), code
    titles = {400: "Request problem", 403: "Not allowed", 404: "Page not found",
              405: "Not allowed", 500: "Server error"}
    return render_template("error.html", code=code, title=titles.get(code, "Error"),
                           message=message), code


for _code in (400, 403, 404, 405, 500):
    app.register_error_handler(_code, http_error)


# --------------------------------------------------------------------------
# Core loyalty logic
# --------------------------------------------------------------------------
def record_refill(cur, client_id, staff_id):
    """Add one refill for a client, honouring cooldown and the free-refill rule.

    The client row is locked (SELECT ... FOR UPDATE) so two simultaneous scans
    of the same QR code can never both pass the cooldown check.
    """
    cur.execute("SELECT * FROM clients WHERE id = %s FOR UPDATE", (client_id,))
    client = cur.fetchone()
    if not client:
        return {"status": "not_found"}

    cur.execute("SELECT clock_timestamp() AS now")
    now = cur.fetchone()["now"]

    last = client["last_refill_at"]
    if last and now - last < COOLDOWN:
        wait = (COOLDOWN - (now - last)).total_seconds()
        return {"status": "cooldown", "client": client, "wait_seconds": math.ceil(wait)}

    price = current_price(cur)
    redeemed = client["free_refills_redeemed"]
    if client["points"] >= REWARD_AT:          # reward refill: free, count restarts
        is_free, amount, points = True, Decimal("0"), 0
        redeemed += 1
    else:                                      # normal refill: +1 point
        is_free, amount, points = False, price, client["points"] + 1

    cur.execute(
        """UPDATE clients
              SET points = %s, total_refills = total_refills + 1,
                  free_refills_redeemed = %s, last_refill_at = %s
            WHERE id = %s""",
        (points, redeemed, now, client_id),
    )
    cur.execute(
        """INSERT INTO refills (client_id, client_name, served_by, is_free, amount, created_at)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (client_id, client["name"], staff_id, is_free, amount, now),
    )
    return {"status": "free" if is_free else "ok", "client": client,
            "points": points, "amount": amount}


def refill_payload(result):
    """Turn a record_refill() result into the JSON the scanner UI displays."""
    status = result["status"]
    if status == "not_found":
        return {"status": "not_found", "title": "Client not found",
                "message": "No client matches that code or phone number."}
    c = result["client"]
    info = {"name": c["name"], "phone": f"+{c['phone']}"}
    if status == "cooldown":
        return {"status": "cooldown", "title": "Too soon",
                "message": f"{c['name']} was served a moment ago. "
                           f"Next point in {fmt_wait(result['wait_seconds'])}.",
                "wait_seconds": result["wait_seconds"], "client": info, "points": c["points"]}
    if status == "free":
        return {"status": "free", "title": "Free refill redeemed",
                "message": "Loyalty count restarts from zero.",
                "client": info, "points": 0}
    points = result["points"]
    message = ("Reward unlocked - the next refill is free!" if points >= REWARD_AT
               else f"{points} of {REWARD_AT} refills collected.")
    return {"status": "ok", "title": "Point added", "message": message,
            "client": info, "points": points, "reward_ready": points >= REWARD_AT}


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
_failed_logins = defaultdict(list)
_DUMMY_HASH = generate_password_hash("not-a-real-password")


def _login_blocked(ip):
    cutoff = time.time() - 600
    _failed_logins[ip] = [t for t in _failed_logins[ip] if t > cutoff]
    return len(_failed_logins[ip]) >= 8


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        ip = request.remote_addr or "?"
        if _login_blocked(ip):
            flash("Too many attempts. Please wait a few minutes and try again.", "error")
            return render_template("login.html"), 429
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        with db.transaction() as cur:
            cur.execute("SELECT * FROM users WHERE username = %s", (username,))
            user = cur.fetchone()
        valid = check_password_hash(user["password_hash"] if user else _DUMMY_HASH, password)
        if user and valid and user["is_active"]:
            _failed_logins.pop(ip, None)
            session.clear()
            session["uid"] = user["id"]
            session.permanent = True
            nxt = request.args.get("next", "")
            if nxt.startswith("/") and not nxt.startswith("//"):
                return redirect(nxt)
            return redirect(url_for("dashboard"))
        _failed_logins[ip].append(time.time())
        flash("Incorrect username or password.", "error")
    return render_template("login.html")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        current = request.form.get("current", "")
        new = request.form.get("new", "")
        confirm = request.form.get("confirm", "")
        with db.transaction() as cur:
            cur.execute("SELECT password_hash FROM users WHERE id = %s", (g.user["id"],))
            row = cur.fetchone()
            if not check_password_hash(row["password_hash"], current):
                flash("Your current password is incorrect.", "error")
            elif len(new) < 8:
                flash("New password must be at least 8 characters.", "error")
            elif new != confirm:
                flash("The new passwords don't match.", "error")
            else:
                cur.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                            (generate_password_hash(new), g.user["id"]))
                flash("Password updated.", "success")
                return redirect(url_for("account"))
    return render_template("account.html")


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
@app.route("/")
@login_required
def dashboard():
    with db.transaction() as cur:
        cur.execute(
            """SELECT COUNT(*) AS refills,
                      COUNT(*) FILTER (WHERE is_free) AS free_refills,
                      COALESCE(SUM(amount), 0) AS revenue
                 FROM refills
                WHERE (created_at AT TIME ZONE %s)::date = (now() AT TIME ZONE %s)::date""",
            (TZ_NAME, TZ_NAME))
        today = cur.fetchone()
        cur.execute("SELECT COUNT(*) AS n FROM clients")
        total_clients = cur.fetchone()["n"]
        cur.execute(
            """SELECT COUNT(*) AS n FROM clients
                WHERE (created_at AT TIME ZONE %s)::date = (now() AT TIME ZONE %s)::date""",
            (TZ_NAME, TZ_NAME))
        new_today = cur.fetchone()["n"]
        cur.execute(
            """SELECT r.*, u.full_name AS staff FROM refills r
                 LEFT JOIN users u ON u.id = r.served_by
                ORDER BY r.created_at DESC LIMIT 8""")
        recent = cur.fetchall()
        cur.execute(
            """SELECT id, name, phone, points FROM clients
                WHERE points >= %s ORDER BY points DESC, name LIMIT 6""",
            (REWARD_AT - 2,))
        almost = cur.fetchall()
    return render_template("dashboard.html", today=today, total_clients=total_clients,
                           new_today=new_today, recent=recent, almost=almost)


# --------------------------------------------------------------------------
# Scan & refill
# --------------------------------------------------------------------------
@app.route("/scan")
@login_required
def scan():
    return render_template("scan.html")


@app.post("/api/refill")
@login_required
def api_refill():
    data = request.get_json(silent=True) or {}
    with db.transaction() as cur:
        if data.get("code") is not None:
            token = extract_token(data["code"])
            if not token:
                return jsonify(status="not_found", title="Unrecognised code",
                               message="That doesn't look like a client QR code.")
            cur.execute("SELECT id FROM clients WHERE qr_token = %s", (token,))
        elif data.get("phone") is not None:
            phone = normalize_phone(data["phone"])
            if not phone:
                return jsonify(status="error", title="Invalid number",
                               message="Enter a valid phone number.")
            cur.execute("SELECT id FROM clients WHERE phone = %s", (phone,))
        else:
            return jsonify(status="error", title="Nothing to scan",
                           message="Send a QR code or a phone number."), 400
        row = cur.fetchone()
        if not row:
            return jsonify(refill_payload({"status": "not_found"}))
        result = record_refill(cur, row["id"], g.user["id"])
    return jsonify(refill_payload(result))


@app.post("/api/walkin")
@login_required
def api_walkin():
    """Record a refill for a customer who is not in the loyalty programme."""
    with db.transaction() as cur:
        price = current_price(cur)
        cur.execute(
            """INSERT INTO refills (client_id, client_name, served_by, is_free, amount)
               VALUES (NULL, 'Walk-in', %s, FALSE, %s)""",
            (g.user["id"], price))
    return jsonify(status="walkin", title="Walk-in sale recorded",
                   message=f"{money_filter(price)} added to today's sales.")


# --------------------------------------------------------------------------
# Clients
# --------------------------------------------------------------------------
@app.route("/clients")
@login_required
def clients():
    q = request.args.get("q", "").strip()
    digits = re.sub(r"\D", "", q)
    with db.transaction() as cur:
        cur.execute(
            """SELECT * FROM clients
                WHERE (%(q)s = '' OR name ILIKE %(like)s
                       OR (%(d)s::text IS NOT NULL AND phone LIKE %(dlike)s))
                ORDER BY lower(name) LIMIT 300""",
            {"q": q, "like": f"%{q}%", "d": digits or None, "dlike": f"%{digits}%"})
        rows = cur.fetchall()
    return render_template("clients.html", clients=rows, q=q)


@app.route("/clients/new", methods=["GET", "POST"])
@login_required
def client_new():
    form = {"name": "", "phone": ""}
    if request.method == "POST":
        name = " ".join(request.form.get("name", "").split())
        raw_phone = request.form.get("phone", "")
        form = {"name": name, "phone": raw_phone}
        phone = normalize_phone(raw_phone)
        if not 2 <= len(name) <= 100:
            flash("Please enter the client's name.", "error")
        elif not phone:
            flash("Please enter a valid phone number.", "error")
        else:
            try:
                with db.transaction() as cur:
                    cur.execute(
                        """INSERT INTO clients (name, phone, qr_token, created_by)
                           VALUES (%s, %s, %s, %s) RETURNING id""",
                        (name, phone, secrets.token_urlsafe(16), g.user["id"]))
                    new_id = cur.fetchone()["id"]
            except psycopg2.errors.UniqueViolation:
                flash("That phone number is already registered.", "error")
            else:
                flash(f"{name} is now a member. Their QR card is ready below.", "success")
                return redirect(url_for("client_detail", client_id=new_id))
    return render_template("client_new.html", form=form)


@app.route("/clients/<int:client_id>")
@login_required
def client_detail(client_id):
    with db.transaction() as cur:
        cur.execute("SELECT * FROM clients WHERE id = %s", (client_id,))
        client = cur.fetchone()
        if not client:
            abort(404, "That client doesn't exist (it may have been deleted).")
        cur.execute(
            """SELECT r.*, u.full_name AS staff FROM refills r
                 LEFT JOIN users u ON u.id = r.served_by
                WHERE r.client_id = %s ORDER BY r.created_at DESC LIMIT 30""",
            (client_id,))
        history = cur.fetchall()
    cooldown_left = 0
    if client["last_refill_at"]:
        elapsed = datetime.now(timezone.utc) - client["last_refill_at"]
        cooldown_left = max(0, int((COOLDOWN - elapsed).total_seconds()))
    return render_template("client_detail.html", client=client, history=history,
                           cooldown_left=cooldown_left, fmt_wait=fmt_wait)


@app.post("/clients/<int:client_id>/refill")
@login_required
def client_refill(client_id):
    with db.transaction() as cur:
        result = record_refill(cur, client_id, g.user["id"])
    payload = refill_payload(result)
    category = {"ok": "success", "free": "success"}.get(payload["status"], "error")
    flash(f"{payload['title']}: {payload['message']}", category)
    return redirect(url_for("client_detail", client_id=client_id))


@app.post("/clients/<int:client_id>/delete")
@admin_required
def client_delete(client_id):
    with db.transaction() as cur:
        cur.execute("DELETE FROM clients WHERE id = %s RETURNING name", (client_id,))
        row = cur.fetchone()
    if row:
        flash(f"{row['name']}'s account was deleted. Past sales were kept.", "success")
    else:
        flash("That client no longer exists.", "error")
    return redirect(url_for("clients"))


# --------------------------------------------------------------------------
# Public membership card + QR code
# --------------------------------------------------------------------------
@app.route("/card/<token>")
def card(token):
    if not extract_token(token):
        abort(404, "This membership card link is not valid.")
    with db.transaction() as cur:
        cur.execute("SELECT name, phone, points, total_refills FROM clients WHERE qr_token = %s",
                    (token,))
        client = cur.fetchone()
    if not client:
        abort(404, "This membership card is no longer active.")
    return render_template("card.html", client=client, token=token)


@app.route("/card/<token>/qr.png")
def card_qr(token):
    if not extract_token(token):
        abort(404)
    with db.transaction() as cur:
        cur.execute("SELECT name FROM clients WHERE qr_token = %s", (token,))
        client = cur.fetchone()
    if not client:
        abort(404)
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(card_url(token))
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0a1b2e", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    download = request.args.get("download") == "1"
    slug = re.sub(r"[^a-z0-9]+", "-", client["name"].lower()).strip("-") or "client"
    return send_file(buf, mimetype="image/png", max_age=3600,
                     as_attachment=download, download_name=f"{slug}-qr.png")


# --------------------------------------------------------------------------
# Sales
# --------------------------------------------------------------------------
DAILY_SQL = """
    SELECT d::date AS day,
           COALESCE(r.refills, 0) AS refills,
           COALESCE(r.paid, 0)    AS paid,
           COALESCE(r.free, 0)    AS free,
           COALESCE(r.revenue, 0) AS revenue
      FROM generate_series((now() AT TIME ZONE %(tz)s)::date - (%(days)s - 1),
                           (now() AT TIME ZONE %(tz)s)::date, interval '1 day') AS d
      LEFT JOIN (
            SELECT (created_at AT TIME ZONE %(tz)s)::date AS day,
                   COUNT(*) AS refills,
                   COUNT(*) FILTER (WHERE NOT is_free) AS paid,
                   COUNT(*) FILTER (WHERE is_free) AS free,
                   SUM(amount) AS revenue
              FROM refills GROUP BY 1) r ON r.day = d::date
     ORDER BY d DESC"""


@app.route("/sales")
@login_required
def sales():
    days = request.args.get("days", 14, type=int)
    days = days if days in (7, 14, 30, 90) else 14
    with db.transaction() as cur:
        cur.execute(DAILY_SQL, {"tz": TZ_NAME, "days": days})
        daily = cur.fetchall()
        cur.execute(
            """SELECT COUNT(*) AS refills,
                      COUNT(*) FILTER (WHERE is_free) AS free_refills,
                      COALESCE(SUM(amount), 0) AS revenue,
                      MIN(created_at) AS since
                 FROM refills""")
        overall = cur.fetchone()
        cur.execute(
            """SELECT to_char(m, 'FMMon YYYY') AS label, refills, free_refills, revenue
                 FROM (SELECT date_trunc('month', created_at AT TIME ZONE %s) AS m,
                              COUNT(*) AS refills,
                              COUNT(*) FILTER (WHERE is_free) AS free_refills,
                              COALESCE(SUM(amount), 0) AS revenue
                         FROM refills GROUP BY 1) t
                ORDER BY m DESC LIMIT 12""", (TZ_NAME,))
        monthly = cur.fetchall()
        by_staff = []
        if g.user["role"] == "admin":
            cur.execute(
                """SELECT COALESCE(u.full_name, 'Removed staff') AS staff,
                          COUNT(*) AS refills,
                          COUNT(*) FILTER (WHERE r.is_free) AS free_refills,
                          COALESCE(SUM(r.amount), 0) AS revenue
                     FROM refills r LEFT JOIN users u ON u.id = r.served_by
                    GROUP BY 1 ORDER BY revenue DESC""")
            by_staff = cur.fetchall()
    range_totals = {
        "refills": sum(d["refills"] for d in daily),
        "free": sum(d["free"] for d in daily),
        "revenue": sum((d["revenue"] for d in daily), Decimal("0")),
    }
    max_revenue = max((d["revenue"] for d in daily), default=Decimal("0")) or Decimal("1")
    return render_template("sales.html", daily=daily, days=days, overall=overall,
                           monthly=monthly, by_staff=by_staff, today=daily[0],
                           range_totals=range_totals, max_revenue=max_revenue)


@app.route("/sales/export.csv")
@admin_required
def sales_export():
    with db.transaction() as cur:
        cur.execute(
            """SELECT (created_at AT TIME ZONE %s)::date AS day,
                      COUNT(*) AS refills,
                      COUNT(*) FILTER (WHERE NOT is_free) AS paid,
                      COUNT(*) FILTER (WHERE is_free) AS free,
                      COALESCE(SUM(amount), 0) AS revenue
                 FROM refills GROUP BY 1 ORDER BY 1 DESC""", (TZ_NAME,))
        rows = cur.fetchall()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["date", "refills", "paid_refills", "free_refills", "revenue"])
    for r in rows:
        writer.writerow([r["day"].isoformat(), r["refills"], r["paid"], r["free"], f"{r['revenue']:.2f}"])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=daily-sales.csv"})


# --------------------------------------------------------------------------
# Admin: staff & settings
# --------------------------------------------------------------------------
@app.route("/staff")
@admin_required
def staff():
    with db.transaction() as cur:
        cur.execute(
            """SELECT u.*, (SELECT COUNT(*) FROM refills r WHERE r.served_by = u.id) AS served
                 FROM users u ORDER BY (u.role = 'admin') DESC, lower(u.full_name)""")
        users = cur.fetchall()
    return render_template("staff.html", users=users)


@app.post("/staff/new")
@admin_required
def staff_new():
    username = request.form.get("username", "").strip().lower()
    full_name = " ".join(request.form.get("full_name", "").split())
    password = request.form.get("password", "")
    if not re.fullmatch(r"[a-z0-9._-]{3,30}", username):
        flash("Username must be 3-30 characters: letters, numbers, dot, dash or underscore.", "error")
    elif not 2 <= len(full_name) <= 100:
        flash("Please enter the manager's full name.", "error")
    elif len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
    else:
        try:
            with db.transaction() as cur:
                cur.execute(
                    """INSERT INTO users (username, full_name, password_hash, role)
                       VALUES (%s, %s, %s, 'manager')""",
                    (username, full_name, generate_password_hash(password)))
        except psycopg2.errors.UniqueViolation:
            flash("That username is already taken.", "error")
        else:
            flash(f"Manager account created for {full_name}.", "success")
    return redirect(url_for("staff"))


@app.post("/staff/<int:user_id>/toggle")
@admin_required
def staff_toggle(user_id):
    if user_id == g.user["id"]:
        flash("You can't deactivate your own account.", "error")
        return redirect(url_for("staff"))
    with db.transaction() as cur:
        cur.execute("UPDATE users SET is_active = NOT is_active WHERE id = %s "
                    "RETURNING full_name, is_active", (user_id,))
        row = cur.fetchone()
    if row:
        flash(f"{row['full_name']} is now {'active' if row['is_active'] else 'deactivated'}.", "success")
    return redirect(url_for("staff"))


@app.post("/staff/<int:user_id>/password")
@admin_required
def staff_password(user_id):
    password = request.form.get("password", "")
    if len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
    else:
        with db.transaction() as cur:
            cur.execute("UPDATE users SET password_hash = %s WHERE id = %s RETURNING full_name",
                        (generate_password_hash(password), user_id))
            row = cur.fetchone()
        if row:
            flash(f"Password reset for {row['full_name']}.", "success")
    return redirect(url_for("staff"))


@app.post("/staff/<int:user_id>/delete")
@admin_required
def staff_delete(user_id):
    if user_id == g.user["id"]:
        flash("You can't delete your own account.", "error")
        return redirect(url_for("staff"))
    with db.transaction() as cur:
        cur.execute("DELETE FROM users WHERE id = %s RETURNING full_name", (user_id,))
        row = cur.fetchone()
    if row:
        flash(f"{row['full_name']}'s account was deleted. Their sales records were kept.", "success")
    return redirect(url_for("staff"))


@app.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    if request.method == "POST":
        business = " ".join(request.form.get("business_name", "").split())
        currency = request.form.get("currency", "").strip().upper()
        try:
            price = Decimal(request.form.get("price", "").strip())
        except InvalidOperation:
            price = None
        if not 1 <= len(business) <= 60:
            flash("Business name must be 1-60 characters.", "error")
        elif not re.fullmatch(r"[A-Z$€£]{1,5}", currency):
            flash("Currency should be a short code such as KES or USD.", "error")
        elif price is None or not Decimal("0") < price <= Decimal("100000"):
            flash("Enter a valid price per refill.", "error")
        else:
            with db.transaction() as cur:
                for key, value in (("business_name", business), ("currency", currency),
                                   ("price", f"{price:.2f}")):
                    cur.execute(
                        """INSERT INTO settings (key, value) VALUES (%s, %s)
                           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                        (key, value))
            flash("Settings saved. The new price applies to refills from now on.", "success")
            return redirect(url_for("settings"))
    s = get_settings()
    return render_template("settings.html", s=s)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")),
            debug=os.environ.get("FLASK_DEBUG") == "1")
