"""
PriceWatch – Flask backend
==========================
Routes
------
  GET  /api/products            list all products
  POST /api/products            add a new product  ← bug was here (fixed)
  GET  /api/products/<id>       get single product + price history
  PUT  /api/products/<id>       update (name, threshold, active)
  DELETE /api/products/<id>     remove product + history
  POST /api/products/<id>/check force-check one product
  POST /api/check-all           force-check all active products
  GET  /api/settings            read settings
  POST /api/settings            save settings
"""

import logging
import logging.config
import os
import sqlite3
import time
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, g, jsonify, request
from flask_cors import CORS

from scrapers import scrape_url

# ─────────────────────────────────────────────────────────────────────────────
# Logging setup – structured, levelled, goes to stdout (Docker-friendly)
# ─────────────────────────────────────────────────────────────────────────────

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
            "datefmt": "%Y-%m-%dT%H:%M:%S",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "default",
            "stream": "ext://sys.stdout",
        }
    },
    "root": {
        "level": LOG_LEVEL,
        "handlers": ["console"],
    },
}

logging.config.dictConfig(LOGGING_CONFIG)
logger = logging.getLogger(__name__)
logger.info("PriceWatch backend starting (log level=%s)", LOG_LEVEL)

# ─────────────────────────────────────────────────────────────────────────────
# App + DB
# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
CORS(app)

DB_PATH = os.environ.get("DB_PATH", "/data/pricewatch.db")


def get_db() -> sqlite3.Connection:
    """Return a per-request SQLite connection stored on Flask's g object."""
    if "db" not in g:
        logger.debug("Opening DB connection to %s", DB_PATH)
        g.db = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
        # Enable WAL for better concurrency
        g.db.execute("PRAGMA journal_mode=WAL")
        # Enforce foreign keys
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        if exc:
            logger.warning("Closing DB after request error: %s", exc)
            db.rollback()
        db.close()


def init_db():
    """Create tables if they don't already exist."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    # Use a fresh connection here (called before first request)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS products (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                url         TEXT    NOT NULL UNIQUE,
                name        TEXT,
                threshold   REAL    NOT NULL DEFAULT 0,
                active      INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT    NOT NULL,
                last_checked TEXT
            );

            CREATE TABLE IF NOT EXISTS price_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                price       REAL    NOT NULL,
                checked_at  TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        conn.commit()
    logger.info("Database initialised at %s", DB_PATH)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_setting(key: str, default: str = "") -> str:
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def _save_setting(key: str, value: str):
    db = get_db()
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    # Note: caller must commit


def _check_product(product_id: int) -> dict:
    """
    Fetch current price for a product, persist it to price_history,
    send alerts if warranted, and return a status dict.
    """
    db = get_db()
    product = db.execute(
        "SELECT * FROM products WHERE id = ?", (product_id,)
    ).fetchone()

    if not product:
        logger.error("_check_product: product id=%s not found", product_id)
        return {"error": "Product not found"}

    url = product["url"]
    logger.info("Checking product id=%s url=%s", product_id, url)

    result = scrape_url(url)
    now = _now_iso()

    if not result.success:
        logger.warning(
            "Price check failed for product id=%s url=%s error=%r",
            product_id, url, result.error,
        )
        db.execute(
            "UPDATE products SET last_checked = ? WHERE id = ?", (now, product_id)
        )
        db.commit()
        return {"product_id": product_id, "error": result.error, "checked_at": now}

    price = result.price
    # Update product name if it was empty or scraper returned a better one
    if result.name and not product["name"]:
        db.execute(
            "UPDATE products SET name = ? WHERE id = ?", (result.name, product_id)
        )
        logger.info("Updated name for product id=%s → %r", product_id, result.name)

    db.execute(
        "INSERT INTO price_history (product_id, price, checked_at) VALUES (?, ?, ?)",
        (product_id, price, now),
    )
    db.execute(
        "UPDATE products SET last_checked = ? WHERE id = ?", (now, product_id)
    )
    db.commit()

    logger.info(
        "Price recorded: product id=%s price=%.2f url=%s", product_id, price, url
    )

    # Alert logic
    threshold = product["threshold"] or 0
    if threshold > 0:
        history = db.execute(
            "SELECT price FROM price_history WHERE product_id = ? ORDER BY id DESC LIMIT 2",
            (product_id,),
        ).fetchall()
        if len(history) >= 2:
            previous_price = history[1]["price"]
            if previous_price > 0:
                pct_drop = (previous_price - price) / previous_price * 100
                if pct_drop >= threshold:
                    logger.info(
                        "Alert triggered: product id=%s drop=%.1f%% threshold=%.1f%%",
                        product_id, pct_drop, threshold,
                    )
                    _send_alerts(product, price, previous_price, pct_drop)

    return {
        "product_id": product_id,
        "price": price,
        "name": result.name,
        "checked_at": now,
    }


def _send_alerts(product, current_price: float, previous_price: float, pct_drop: float):
    """Fire all configured notification channels."""
    import smtplib
    from email.mime.text import MIMEText

    name = product["name"] or product["url"]
    message = (
        f"🏷️ Price drop alert!\n\n"
        f"Product: {name}\n"
        f"URL: {product['url']}\n"
        f"Was: €{previous_price:.2f}  →  Now: €{current_price:.2f} ({pct_drop:.1f}% off)"
    )

    # Slack
    slack_url = _get_setting("slack_webhook")
    if slack_url:
        try:
            import requests as req_lib
            resp = req_lib.post(slack_url, json={"text": message}, timeout=10)
            resp.raise_for_status()
            logger.info("Slack alert sent for product id=%s", product["id"])
        except Exception as exc:
            logger.warning("Slack alert failed for product id=%s: %s", product["id"], exc)

    # Email
    smtp_host = _get_setting("smtp_host")
    smtp_port = _get_setting("smtp_port", "587")
    smtp_user = _get_setting("smtp_user")
    smtp_pass = _get_setting("smtp_pass")
    notify_email = _get_setting("notify_email")

    if smtp_host and smtp_user and notify_email:
        try:
            msg = MIMEText(message)
            msg["Subject"] = f"[PriceWatch] Price drop: {name}"
            msg["From"] = smtp_user
            msg["To"] = notify_email
            with smtplib.SMTP(smtp_host, int(smtp_port)) as server:
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.sendmail(smtp_user, notify_email, msg.as_string())
            logger.info("Email alert sent for product id=%s to %s", product["id"], notify_email)
        except Exception as exc:
            logger.warning("Email alert failed for product id=%s: %s", product["id"], exc)

    # Pushbullet
    pb_key = _get_setting("pushbullet_key")
    if pb_key:
        try:
            import requests as req_lib
            resp = req_lib.post(
                "https://api.pushbullet.com/v2/pushes",
                headers={"Access-Token": pb_key},
                json={"type": "note", "title": f"PriceWatch: {name}", "body": message},
                timeout=10,
            )
            resp.raise_for_status()
            logger.info("Pushbullet alert sent for product id=%s", product["id"])
        except Exception as exc:
            logger.warning("Pushbullet alert failed for product id=%s: %s", product["id"], exc)


# ─────────────────────────────────────────────────────────────────────────────
# Routes – Products
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/products", methods=["GET"])
def list_products():
    logger.debug("GET /api/products")
    db = get_db()
    rows = db.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
    products = []
    for row in rows:
        p = dict(row)
        # Attach latest price
        latest = db.execute(
            "SELECT price FROM price_history WHERE product_id = ? ORDER BY id DESC LIMIT 1",
            (p["id"],),
        ).fetchone()
        p["current_price"] = latest["price"] if latest else None
        products.append(p)
    logger.debug("Returning %d products", len(products))
    return jsonify(products)


@app.route("/api/products", methods=["POST"])
def add_product():
    """
    Add a new product and immediately check its price.

    Bug that was present:
      - The INSERT used to run inside a try/except that swallowed the error
        and returned 201 without actually committing, so the row was never
        persisted.  We now:
          1. Validate required fields before touching the DB.
          2. Explicitly call db.commit() after every write.
          3. Log the full exception so errors are visible in container logs.
    """
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    logger.info("POST /api/products url=%r", url)

    if not url:
        logger.warning("add_product: missing url in request body")
        return jsonify({"error": "url is required"}), 400

    threshold = float(data.get("threshold", 0) or 0)
    name = (data.get("name") or "").strip() or None
    now = _now_iso()

    db = get_db()

    # Check for duplicate
    existing = db.execute("SELECT id FROM products WHERE url = ?", (url,)).fetchone()
    if existing:
        logger.warning("add_product: duplicate url %r (existing id=%s)", url, existing["id"])
        return jsonify({"error": "Product with this URL already exists", "id": existing["id"]}), 409

    try:
        cursor = db.execute(
            "INSERT INTO products (url, name, threshold, active, created_at) "
            "VALUES (?, ?, ?, 1, ?)",
            (url, name, threshold, now),
        )
        product_id = cursor.lastrowid
        # *** Critical: commit before doing anything else ***
        db.commit()
        logger.info("Inserted product id=%s url=%r", product_id, url)
    except sqlite3.IntegrityError as exc:
        db.rollback()
        logger.error("IntegrityError inserting product url=%r: %s", url, exc)
        return jsonify({"error": "Database integrity error", "detail": str(exc)}), 409
    except sqlite3.Error as exc:
        db.rollback()
        logger.exception("DB error inserting product url=%r", url)
        return jsonify({"error": "Database error", "detail": str(exc)}), 500

    # Immediate first price check (best-effort – don't fail the response)
    try:
        check_result = _check_product(product_id)
        logger.info("Initial price check for product id=%s: %s", product_id, check_result)
    except Exception as exc:
        logger.exception("Initial price check failed for product id=%s", product_id)
        check_result = {"error": str(exc)}

    product = db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    response = dict(product)
    response["check_result"] = check_result
    return jsonify(response), 201


@app.route("/api/products/<int:product_id>", methods=["GET"])
def get_product(product_id):
    logger.debug("GET /api/products/%s", product_id)
    db = get_db()
    product = db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not product:
        logger.warning("get_product: id=%s not found", product_id)
        return jsonify({"error": "Not found"}), 404

    history = db.execute(
        "SELECT price, checked_at FROM price_history WHERE product_id = ? ORDER BY id ASC",
        (product_id,),
    ).fetchall()

    result = dict(product)
    result["price_history"] = [dict(h) for h in history]
    return jsonify(result)


@app.route("/api/products/<int:product_id>", methods=["PUT"])
def update_product(product_id):
    data = request.get_json(silent=True) or {}
    logger.info("PUT /api/products/%s data=%r", product_id, data)
    db = get_db()

    product = db.execute("SELECT id FROM products WHERE id = ?", (product_id,)).fetchone()
    if not product:
        return jsonify({"error": "Not found"}), 404

    fields, values = [], []
    for col in ("name", "threshold", "active"):
        if col in data:
            fields.append(f"{col} = ?")
            values.append(data[col])

    if not fields:
        return jsonify({"error": "No updatable fields provided"}), 400

    values.append(product_id)
    try:
        db.execute(f"UPDATE products SET {', '.join(fields)} WHERE id = ?", values)
        db.commit()
        logger.info("Updated product id=%s fields=%s", product_id, fields)
    except sqlite3.Error as exc:
        db.rollback()
        logger.exception("DB error updating product id=%s", product_id)
        return jsonify({"error": "Database error", "detail": str(exc)}), 500

    updated = db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    return jsonify(dict(updated))


@app.route("/api/products/<int:product_id>", methods=["DELETE"])
def delete_product(product_id):
    logger.info("DELETE /api/products/%s", product_id)
    db = get_db()
    product = db.execute("SELECT id FROM products WHERE id = ?", (product_id,)).fetchone()
    if not product:
        return jsonify({"error": "Not found"}), 404
    try:
        db.execute("DELETE FROM products WHERE id = ?", (product_id,))
        db.commit()
        logger.info("Deleted product id=%s", product_id)
    except sqlite3.Error as exc:
        db.rollback()
        logger.exception("DB error deleting product id=%s", product_id)
        return jsonify({"error": "Database error", "detail": str(exc)}), 500
    return jsonify({"deleted": product_id})


@app.route("/api/products/<int:product_id>/check", methods=["POST"])
def check_product(product_id):
    logger.info("POST /api/products/%s/check", product_id)
    result = _check_product(product_id)
    if "error" in result and result["error"] == "Product not found":
        return jsonify(result), 404
    return jsonify(result)


@app.route("/api/check-all", methods=["POST"])
def check_all():
    logger.info("POST /api/check-all – manual trigger")
    db = get_db()
    rows = db.execute("SELECT id FROM products WHERE active = 1").fetchall()
    ids = [r["id"] for r in rows]
    logger.info("Checking %d active products", len(ids))
    results = [_check_product(pid) for pid in ids]
    return jsonify({"checked": len(results), "results": results})


# ─────────────────────────────────────────────────────────────────────────────
# Routes – Settings
# ─────────────────────────────────────────────────────────────────────────────

SETTING_KEYS = [
    "slack_webhook",
    "smtp_host", "smtp_port", "smtp_user", "smtp_pass", "notify_email",
    "pushbullet_key",
    "check_interval",
]


@app.route("/api/settings", methods=["GET"])
def get_settings():
    logger.debug("GET /api/settings")
    # Use a fresh read so we don't need g.db
    with app.app_context():
        return jsonify({k: _get_setting(k) for k in SETTING_KEYS})


@app.route("/api/settings", methods=["POST"])
def save_settings():
    data = request.get_json(silent=True) or {}
    logger.info("POST /api/settings keys=%s", list(data.keys()))
    db = get_db()
    try:
        for key in SETTING_KEYS:
            if key in data:
                _save_setting(key, str(data[key]))
        db.commit()
    except sqlite3.Error as exc:
        db.rollback()
        logger.exception("DB error saving settings")
        return jsonify({"error": "Database error", "detail": str(exc)}), 500

    # Reschedule if interval changed
    if "check_interval" in data:
        try:
            interval = max(5, int(data["check_interval"]))
            _reschedule(interval)
            logger.info("Check interval updated to %d minutes", interval)
        except (ValueError, TypeError) as exc:
            logger.warning("Invalid check_interval value %r: %s", data.get("check_interval"), exc)

    return jsonify({"saved": True})


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler
# ─────────────────────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler(daemon=True)


def _scheduled_check_all():
    """Called by APScheduler on a timer."""
    logger.info("Scheduled price check triggered")
    with app.app_context():
        db = get_db()
        rows = db.execute("SELECT id FROM products WHERE active = 1").fetchall()
        ids = [r["id"] for r in rows]
        logger.info("Scheduled check: %d active products", len(ids))
        for pid in ids:
            try:
                _check_product(pid)
            except Exception as exc:
                logger.exception("Error in scheduled check for product id=%s", pid)
        close_db()


def _reschedule(interval_minutes: int):
    """Remove existing job and add a new one with the updated interval."""
    if scheduler.get_job("price_check"):
        scheduler.remove_job("price_check")
    scheduler.add_job(
        _scheduled_check_all,
        "interval",
        minutes=interval_minutes,
        id="price_check",
        replace_existing=True,
    )
    logger.info("Scheduler set to every %d minutes", interval_minutes)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()

    with app.app_context():
        interval = int(_get_setting("check_interval", "60") or 60)

    _reschedule(interval)
    scheduler.start()
    logger.info("APScheduler started with interval=%d minutes", interval)

    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    logger.info("Flask listening on 0.0.0.0:%d (debug=%s)", port, debug)
    app.run(host="0.0.0.0", port=port, debug=debug, use_reloader=False)
