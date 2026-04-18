"""
PriceWatch – Flask backend
==========================
Routes
------
  GET    /api/products                list all products
  POST   /api/products                add a new product
  GET    /api/products/<id>           get single product + price history
  PUT    /api/products/<id>           update name / threshold / active / manual price
  DELETE /api/products/<id>           remove product + history
  POST   /api/products/<id>/check     force-check one product
  POST   /api/check-all               force-check all active products
  GET    /api/settings                read settings
  POST   /api/settings                save settings
  GET    /api/logs                    recent scrape log entries (frontend log viewer)
"""

import logging
import logging.config
import os
import sqlite3
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, g, jsonify, request
from flask_cors import CORS

from scrapers import scrape_url

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.config.dictConfig({
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
    "root": {"level": LOG_LEVEL, "handlers": ["console"]},
})

logger = logging.getLogger(__name__)
logger.info("PriceWatch backend starting (log level=%s)", LOG_LEVEL)

# ─────────────────────────────────────────────────────────────────────────────
# App + DB
# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
CORS(app)

DB_PATH = os.environ.get("DB_PATH", "/data/pricewatch.db")


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        if exc:
            db.rollback()
        db.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS products (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                url          TEXT    NOT NULL UNIQUE,
                name         TEXT,
                threshold    REAL    NOT NULL DEFAULT 0,
                active       INTEGER NOT NULL DEFAULT 1,
                created_at   TEXT    NOT NULL,
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

            CREATE TABLE IF NOT EXISTS scrape_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id  INTEGER REFERENCES products(id) ON DELETE SET NULL,
                product_url TEXT,
                scraper     TEXT,
                level       TEXT    NOT NULL DEFAULT 'INFO',
                message     TEXT    NOT NULL,
                logged_at   TEXT    NOT NULL
            );
        """)
        conn.commit()
    logger.info("Database initialised at %s", DB_PATH)


# ─────────────────────────────────────────────────────────────────────────────
# DB log writer — persists scrape events so the frontend can display them
# ─────────────────────────────────────────────────────────────────────────────

def _log(level: str, message: str, product_id=None, product_url=None, scraper=None):
    """Write a log entry to the scrape_logs table AND to Python logging."""
    getattr(logger, level.lower(), logger.info)(message)
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO scrape_logs (product_id, product_url, scraper, level, message, logged_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (product_id, product_url, scraper, level.upper(), message, _now_iso()),
            )
            conn.commit()
        # Keep last 500 entries
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "DELETE FROM scrape_logs WHERE id NOT IN "
                "(SELECT id FROM scrape_logs ORDER BY id DESC LIMIT 500)"
            )
            conn.commit()
    except Exception as exc:
        logger.warning("Failed to write to scrape_logs: %s", exc)


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


def _check_product(product_id: int) -> dict:
    db = get_db()
    product = db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()

    if not product:
        return {"error": "Product not found"}

    url = product["url"]
    _log("INFO", f"Starting price check for: {url}", product_id=product_id, product_url=url)

    result = scrape_url(url)
    now = _now_iso()

    if not result.success:
        _log("ERROR",
             f"Price check failed — {result.error}",
             product_id=product_id, product_url=url,
             scraper=result.error)
        db.execute("UPDATE products SET last_checked = ? WHERE id = ?", (now, product_id))
        db.commit()
        return {"product_id": product_id, "error": result.error, "checked_at": now}

    price = result.price
    scraper_used = getattr(result, "scraper_name", "unknown")

    if result.name and not product["name"]:
        db.execute("UPDATE products SET name = ? WHERE id = ?", (result.name, product_id))
        _log("INFO", f"Name auto-detected: {result.name!r}",
             product_id=product_id, product_url=url)

    db.execute(
        "INSERT INTO price_history (product_id, price, checked_at) VALUES (?, ?, ?)",
        (product_id, price, now),
    )
    db.execute("UPDATE products SET last_checked = ? WHERE id = ?", (now, product_id))
    db.commit()

    _log("INFO", f"Price recorded: €{price:.2f}", product_id=product_id, product_url=url)

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
                    _log("INFO",
                         f"Alert triggered! Drop {pct_drop:.1f}% >= threshold {threshold:.1f}%",
                         product_id=product_id, product_url=url)
                    _send_alerts(product, price, previous_price, pct_drop)

    return {"product_id": product_id, "price": price, "name": result.name, "checked_at": now}


def _send_alerts(product, current_price, previous_price, pct_drop):
    import smtplib
    from email.mime.text import MIMEText

    name = product["name"] or product["url"]
    message = (
        f"🏷️ Price drop alert!\n\n"
        f"Product: {name}\n"
        f"URL: {product['url']}\n"
        f"Was: €{previous_price:.2f}  →  Now: €{current_price:.2f} ({pct_drop:.1f}% off)"
    )

    slack_url = _get_setting("slack_webhook")
    if slack_url:
        try:
            import requests as req_lib
            req_lib.post(slack_url, json={"text": message}, timeout=10).raise_for_status()
            _log("INFO", "Slack alert sent", product_id=product["id"])
        except Exception as exc:
            _log("ERROR", f"Slack alert failed: {exc}", product_id=product["id"])

    smtp_host = _get_setting("smtp_host")
    smtp_user = _get_setting("smtp_user")
    notify_email = _get_setting("notify_email")
    if smtp_host and smtp_user and notify_email:
        try:
            msg = MIMEText(message)
            msg["Subject"] = f"[PriceWatch] Price drop: {name}"
            msg["From"] = smtp_user
            msg["To"] = notify_email
            with smtplib.SMTP(smtp_host, int(_get_setting("smtp_port", "587"))) as s:
                s.starttls()
                s.login(smtp_user, _get_setting("smtp_pass"))
                s.sendmail(smtp_user, notify_email, msg.as_string())
            _log("INFO", f"Email alert sent to {notify_email}", product_id=product["id"])
        except Exception as exc:
            _log("ERROR", f"Email alert failed: {exc}", product_id=product["id"])

    pb_key = _get_setting("pushbullet_key")
    if pb_key:
        try:
            import requests as req_lib
            req_lib.post(
                "https://api.pushbullet.com/v2/pushes",
                headers={"Access-Token": pb_key},
                json={"type": "note", "title": f"PriceWatch: {name}", "body": message},
                timeout=10,
            ).raise_for_status()
            _log("INFO", "Pushbullet alert sent", product_id=product["id"])
        except Exception as exc:
            _log("ERROR", f"Pushbullet alert failed: {exc}", product_id=product["id"])


# ─────────────────────────────────────────────────────────────────────────────
# Routes – Products
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/products", methods=["GET"])
def list_products():
    db = get_db()
    rows = db.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
    products = []
    for row in rows:
        p = dict(row)
        latest = db.execute(
            "SELECT price, checked_at FROM price_history WHERE product_id = ? ORDER BY id DESC LIMIT 1",
            (p["id"],),
        ).fetchone()
        p["current_price"] = round(latest["price"], 2) if latest else None
        p["last_price_at"] = latest["checked_at"] if latest else None

        prev = db.execute(
            "SELECT price FROM price_history WHERE product_id = ? ORDER BY id DESC LIMIT 1 OFFSET 1",
            (p["id"],),
        ).fetchone()
        p["previous_price"] = round(prev["price"], 2) if prev else None

        # Latest scrape error for this product (if last check failed)
        last_err = db.execute(
            "SELECT message, logged_at FROM scrape_logs "
            "WHERE product_id = ? AND level = 'ERROR' ORDER BY id DESC LIMIT 1",
            (p["id"],),
        ).fetchone()
        p["last_error"] = last_err["message"] if last_err else None

        if not p.get("name"):
            p["name"] = p["url"]
        products.append(p)
    return jsonify(products)


@app.route("/api/products", methods=["POST"])
def add_product():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url is required"}), 400

    threshold = float(data.get("threshold", 0) or 0)
    name = (data.get("name") or "").strip() or None
    now = _now_iso()
    db = get_db()

    existing = db.execute("SELECT id FROM products WHERE url = ?", (url,)).fetchone()
    if existing:
        return jsonify({"error": "Product with this URL already exists", "id": existing["id"]}), 409

    try:
        cursor = db.execute(
            "INSERT INTO products (url, name, threshold, active, created_at) VALUES (?, ?, ?, 1, ?)",
            (url, name, threshold, now),
        )
        product_id = cursor.lastrowid
        db.commit()
        _log("INFO", f"Product added: {url}", product_id=product_id, product_url=url)
    except sqlite3.IntegrityError as exc:
        db.rollback()
        return jsonify({"error": "Database integrity error", "detail": str(exc)}), 409
    except sqlite3.Error as exc:
        db.rollback()
        logger.exception("DB error inserting product")
        return jsonify({"error": "Database error", "detail": str(exc)}), 500

    try:
        check_result = _check_product(product_id)
    except Exception as exc:
        logger.exception("Initial price check failed for product id=%s", product_id)
        check_result = {"error": str(exc)}

    product = db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    response = dict(product)
    response["check_result"] = check_result
    return jsonify(response), 201


@app.route("/api/products/<int:product_id>", methods=["GET"])
def get_product(product_id):
    db = get_db()
    product = db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not product:
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
    db = get_db()

    if not db.execute("SELECT id FROM products WHERE id = ?", (product_id,)).fetchone():
        return jsonify({"error": "Not found"}), 404

    # Handle manual price override — inserts into price_history
    manual_price = data.pop("manual_price", None)
    if manual_price is not None:
        try:
            price_val = float(manual_price)
            now = _now_iso()
            db.execute(
                "INSERT INTO price_history (product_id, price, checked_at) VALUES (?, ?, ?)",
                (product_id, price_val, now),
            )
            db.execute("UPDATE products SET last_checked = ? WHERE id = ?", (now, product_id))
            _log("INFO", f"Manual price set: €{price_val:.2f}", product_id=product_id)
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid manual_price value"}), 400

    fields, values = [], []
    for col in ("name", "threshold", "active"):
        if col in data:
            fields.append(f"{col} = ?")
            values.append(data[col])

    if fields:
        values.append(product_id)
        try:
            db.execute(f"UPDATE products SET {', '.join(fields)} WHERE id = ?", values)
        except sqlite3.Error as exc:
            db.rollback()
            return jsonify({"error": "Database error", "detail": str(exc)}), 500

    try:
        db.commit()
        logger.info("Updated product id=%s", product_id)
    except sqlite3.Error as exc:
        db.rollback()
        return jsonify({"error": "Database error", "detail": str(exc)}), 500

    updated = db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    return jsonify(dict(updated))


@app.route("/api/products/<int:product_id>", methods=["DELETE"])
def delete_product(product_id):
    db = get_db()
    if not db.execute("SELECT id FROM products WHERE id = ?", (product_id,)).fetchone():
        return jsonify({"error": "Not found"}), 404
    try:
        db.execute("DELETE FROM products WHERE id = ?", (product_id,))
        db.commit()
    except sqlite3.Error as exc:
        db.rollback()
        return jsonify({"error": "Database error", "detail": str(exc)}), 500
    return jsonify({"deleted": product_id})


@app.route("/api/products/<int:product_id>/check", methods=["POST"])
def check_product(product_id):
    result = _check_product(product_id)
    if "error" in result and result["error"] == "Product not found":
        return jsonify(result), 404
    return jsonify(result)


@app.route("/api/check-all", methods=["POST"])
def check_all():
    _log("INFO", "Manual check-all triggered")
    db = get_db()
    ids = [r["id"] for r in db.execute("SELECT id FROM products WHERE active = 1").fetchall()]
    results = [_check_product(pid) for pid in ids]
    return jsonify({"checked": len(results), "results": results})


# ─────────────────────────────────────────────────────────────────────────────
# Routes – Logs
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/logs", methods=["GET"])
def get_logs():
    """Return recent scrape log entries for display in the frontend."""
    limit = min(int(request.args.get("limit", 100)), 500)
    product_id = request.args.get("product_id")
    db = get_db()

    if product_id:
        rows = db.execute(
            "SELECT l.*, p.name as product_name FROM scrape_logs l "
            "LEFT JOIN products p ON p.id = l.product_id "
            "WHERE l.product_id = ? ORDER BY l.id DESC LIMIT ?",
            (product_id, limit),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT l.*, p.name as product_name FROM scrape_logs l "
            "LEFT JOIN products p ON p.id = l.product_id "
            "ORDER BY l.id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    return jsonify([dict(r) for r in rows])


@app.route("/api/logs", methods=["DELETE"])
def clear_logs():
    db = get_db()
    db.execute("DELETE FROM scrape_logs")
    db.commit()
    return jsonify({"cleared": True})


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
    with app.app_context():
        return jsonify({k: _get_setting(k) for k in SETTING_KEYS})


@app.route("/api/settings", methods=["POST"])
def save_settings():
    data = request.get_json(silent=True) or {}
    db = get_db()
    try:
        for key in SETTING_KEYS:
            if key in data:
                _save_setting(key, str(data[key]))
        db.commit()
    except sqlite3.Error as exc:
        db.rollback()
        return jsonify({"error": "Database error", "detail": str(exc)}), 500

    if "check_interval" in data:
        try:
            _reschedule(max(5, int(data["check_interval"])))
        except (ValueError, TypeError):
            pass

    return jsonify({"saved": True})


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler
# ─────────────────────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler(daemon=True)


def _scheduled_check_all():
    _log("INFO", "Scheduled price check started")
    with app.app_context():
        db = get_db()
        ids = [r["id"] for r in db.execute("SELECT id FROM products WHERE active = 1").fetchall()]
        for pid in ids:
            try:
                _check_product(pid)
            except Exception as exc:
                logger.exception("Error in scheduled check for product id=%s", pid)
        close_db()


def _reschedule(interval_minutes: int):
    if scheduler.get_job("price_check"):
        scheduler.remove_job("price_check")
    scheduler.add_job(_scheduled_check_all, "interval", minutes=interval_minutes,
                      id="price_check", replace_existing=True)
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
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    logger.info("Flask listening on 0.0.0.0:%d", port)
    app.run(host="0.0.0.0", port=port, debug=debug, use_reloader=False)
