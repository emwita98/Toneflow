"""Create the database tables and the first administrator.

Usage:
    python init_db.py                         # prompts for the admin password
    python init_db.py --admin-user owner --admin-name "Jane Doe"
    python init_db.py --demo                  # also adds a demo manager + sample clients/sales
"""
import argparse
import getpass
import os
import random
import secrets
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from werkzeug.security import generate_password_hash

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import db

REWARD_AT = 10


def apply_schema():
    sql = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    with db.transaction() as cur:
        cur.execute(sql)
    print("✓ Tables created / verified")


def ensure_user(username, full_name, password, role):
    with db.transaction() as cur:
        cur.execute("SELECT id FROM users WHERE username = %s", (username,))
        row = cur.fetchone()
        if row:
            print(f"• User '{username}' already exists - left unchanged")
            return row["id"]
        cur.execute(
            """INSERT INTO users (username, full_name, password_hash, role)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (username, full_name, generate_password_hash(password), role))
        print(f"✓ Created {role} '{username}'")
        return cur.fetchone()["id"]


def seed_demo(admin_id, manager_id):
    """Sample members with ~3 weeks of refill history that follows the real rules."""
    names = [("Amina Wanjiru", "0712000101"), ("Brian Otieno", "0722000102"),
             ("Cynthia Mutua", "0733000103"), ("David Kiplagat", "0700000104"),
             ("Esther Njeri", "0711000105"), ("Farid Hassan", "0745000106"),
             ("Grace Achieng", "0758000107"), ("Hassan Ali", "0768000108")]
    now = datetime.now(timezone.utc)
    with db.transaction() as cur:
        cur.execute("SELECT value FROM settings WHERE key = 'price'")
        price = Decimal(cur.fetchone()["value"])
        for name, raw in names:
            phone = "254" + raw[1:]
            cur.execute("SELECT 1 FROM clients WHERE phone = %s", (phone,))
            if cur.fetchone():
                continue
            times = sorted(now - timedelta(days=random.uniform(0.1, 21)) for _ in range(random.randint(4, 26)))
            points = total = redeemed = 0
            cur.execute(
                """INSERT INTO clients (name, phone, qr_token, created_by, created_at)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                (name, phone, secrets.token_urlsafe(16), manager_id, times[0]))
            cid = cur.fetchone()["id"]
            for t in times:
                if points >= REWARD_AT:
                    is_free, amount, points, redeemed = True, Decimal("0"), 0, redeemed + 1
                else:
                    is_free, amount, points = False, price, points + 1
                total += 1
                cur.execute(
                    """INSERT INTO refills (client_id, client_name, served_by, is_free, amount, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (cid, name, random.choice([admin_id, manager_id]), is_free, amount, t))
            cur.execute(
                """UPDATE clients SET points = %s, total_refills = %s,
                          free_refills_redeemed = %s, last_refill_at = %s WHERE id = %s""",
                (points, total, redeemed, times[-1], cid))
        for _ in range(25):  # some walk-in sales too
            cur.execute(
                """INSERT INTO refills (client_id, client_name, served_by, is_free, amount, created_at)
                   VALUES (NULL, 'Walk-in', %s, FALSE, %s, %s)""",
                (manager_id, price, now - timedelta(days=random.uniform(0, 14))))
    print("✓ Demo data added (8 members, ~3 weeks of sales)")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--admin-user", default=os.environ.get("ADMIN_USERNAME", "admin"))
    parser.add_argument("--admin-name", default=os.environ.get("ADMIN_NAME", "Administrator"))
    parser.add_argument("--admin-password", default=os.environ.get("ADMIN_PASSWORD"))
    parser.add_argument("--demo", action="store_true", help="add a demo manager and sample data")
    args = parser.parse_args()

    if "DATABASE_URL" not in os.environ:
        sys.exit("DATABASE_URL is not set. Copy .env.example to .env and edit it.")

    apply_schema()

    password = args.admin_password
    if not password:
        password = getpass.getpass(f"Choose a password for admin '{args.admin_user}' (min 8 chars): ")
    if len(password) < 8:
        sys.exit("Password must be at least 8 characters.")
    admin_id = ensure_user(args.admin_user.lower(), args.admin_name, password, "admin")

    if args.demo:
        manager_id = ensure_user("manager", "Demo Manager", "manager123", "manager")
        seed_demo(admin_id, manager_id)
        print("! Demo manager login: manager / manager123  (change or delete it before going live)")
    print("\nAll set. Start the app with:  python app.py")


if __name__ == "__main__":
    main()
