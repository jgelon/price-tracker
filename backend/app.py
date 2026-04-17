from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler
import requests
from bs4 import BeautifulSoup
import re
import smtplib
import json
import os
import logging
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///prices.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# ── Models ──────────────────────────────────────────────────────────────────

class Product(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(200), nullable=False)
    url         = db.Column(db.String(2000), nullable=False)
    current_price = db.Column(db.Float, nullable=True)
    original_price = db.Column(db.Float, nullable=True)
    currency    = db.Column(db.String(10), default='€')
    last_checked = db.Column(db.DateTime, nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    active      = db.Column(db.Boolean, default=True)
    threshold_pct = db.Column(db.Float, default=5.0)   # alert when discount >= X%
    image_url   = db.Column(db.String(2000), nullable=True)
    history     = db.relationship('PriceHistory', backref='product', lazy=True, cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'url': self.url,
            'current_price': self.current_price,
            'original_price': self.original_price,
            'currency': self.currency,
            'last_checked': self.last_checked.isoformat() if self.last_checked else None,
            'created_at': self.created_at.isoformat(),
            'active': self.active,
            'threshold_pct': self.threshold_pct,
            'image_url': self.image_url,
            'discount_pct': self._discount_pct(),
        }

    def _discount_pct(self):
        if self.original_price and self.current_price and self.original_price > 0:
            return round((1 - self.current_price / self.original_price) * 100, 1)
        return 0.0


class PriceHistory(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    price      = db.Column(db.Float, nullable=False)
    timestamp  = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {'price': self.price, 'timestamp': self.timestamp.isoformat()}


class Settings(db.Model):
    id    = db.Column(db.Integer, primary_key=True)
    key   = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text, nullable=True)

    @staticmethod
    def get(key, default=None):
        s = Settings.query.filter_by(key=key).first()
        return s.value if s else default

    @staticmethod
    def set(key, value):
        s = Settings.query.filter_by(key=key).first()
        if s:
            s.value = value
        else:
            s = Settings(key=key, value=value)
            db.session.add(s)
        db.session.commit()


# ── Scraper ──────────────────────────────────────────────────────────────────

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'nl-NL,nl;q=0.9,en;q=0.8',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
}

def parse_price(text):
    """Extract a float price from a string like '€ 12,99' or '12.99'."""
    if not text:
        return None
    text = text.replace('\xa0', ' ').strip()
    # Replace comma-decimal European format
    match = re.search(r'[\d]+[.,][\d]{2}', text)
    if match:
        p = match.group().replace(',', '.')
        try:
            return float(p)
        except ValueError:
            pass
    # Integer fallback
    match = re.search(r'[\d]+', text)
    if match:
        try:
            return float(match.group())
        except ValueError:
            pass
    return None


def scrape_etos(soup, url):
    result = {}
    # Product name
    name_el = soup.find('h1')
    if name_el:
        result['name'] = name_el.get_text(strip=True)

    # Price – Etos uses data attributes and specific class names
    # Try sale price first
    sale = soup.find(class_=re.compile(r'sales|sale-price|promo', re.I))
    regular = soup.find(class_=re.compile(r'(original|regular|was|strike)', re.I))

    # Generic price selectors
    price_el = soup.find('span', class_=re.compile(r'price', re.I))

    # Try JSON-LD structured data
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            data = json.loads(script.string or '{}')
            if isinstance(data, list):
                data = data[0]
            if data.get('@type') in ('Product', 'product'):
                offers = data.get('offers', {})
                if isinstance(offers, list):
                    offers = offers[0]
                p = offers.get('price') or offers.get('lowPrice')
                if p:
                    result['current_price'] = float(p)
                result['currency'] = offers.get('priceCurrency', '€')
                if not result.get('name'):
                    result['name'] = data.get('name', '')
                img = data.get('image')
                if img:
                    result['image_url'] = img[0] if isinstance(img, list) else img
        except Exception:
            pass

    if sale:
        result['current_price'] = parse_price(sale.get_text())
    if regular:
        result['original_price'] = parse_price(regular.get_text())
    if not result.get('current_price') and price_el:
        result['current_price'] = parse_price(price_el.get_text())

    return result


def scrape_holland_barrett(soup, url):
    result = {}
    name_el = soup.find('h1')
    if name_el:
        result['name'] = name_el.get_text(strip=True)

    for script in soup.find_all('script', type='application/ld+json'):
        try:
            data = json.loads(script.string or '{}')
            if isinstance(data, list):
                data = data[0]
            if data.get('@type') in ('Product',):
                offers = data.get('offers', {})
                if isinstance(offers, list):
                    offers = offers[0]
                p = offers.get('price') or offers.get('lowPrice')
                if p:
                    result['current_price'] = float(p)
                result['currency'] = offers.get('priceCurrency', '€')
                if not result.get('name'):
                    result['name'] = data.get('name', '')
                img = data.get('image')
                if img:
                    result['image_url'] = img[0] if isinstance(img, list) else img
        except Exception:
            pass

    # Fallback selectors
    if not result.get('current_price'):
        for sel in ['[data-test="product-price"]', '.productPrice', '[class*="price"]']:
            el = soup.select_one(sel)
            if el:
                result['current_price'] = parse_price(el.get_text())
                break

    return result


def scrape_generic(soup, url):
    result = {}
    name_el = soup.find('h1')
    if name_el:
        result['name'] = name_el.get_text(strip=True)

    # JSON-LD first
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            data = json.loads(script.string or '{}')
            if isinstance(data, list):
                data = data[0]
            if data.get('@type') in ('Product',):
                offers = data.get('offers', {})
                if isinstance(offers, list):
                    offers = offers[0]
                p = offers.get('price') or offers.get('lowPrice')
                if p:
                    result['current_price'] = float(p)
                result['currency'] = offers.get('priceCurrency', '€')
                if not result.get('name'):
                    result['name'] = data.get('name', '')
                img = data.get('image')
                if img:
                    result['image_url'] = img[0] if isinstance(img, list) else img
        except Exception:
            pass

    # Meta og:price
    meta_price = soup.find('meta', property='product:price:amount')
    if meta_price:
        result['current_price'] = parse_price(meta_price.get('content'))
    meta_currency = soup.find('meta', property='product:price:currency')
    if meta_currency:
        result['currency'] = meta_currency.get('content', '€')

    if not result.get('current_price'):
        for sel in ['[class*="sale"]', '[class*="price"]', '[itemprop="price"]']:
            el = soup.select_one(sel)
            if el:
                p = parse_price(el.get('content') or el.get_text())
                if p:
                    result['current_price'] = p
                    break

    return result


def fetch_product_data(url):
    """Fetch and parse product data from a URL."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')

        if 'etos.nl' in url:
            data = scrape_etos(soup, url)
        elif 'hollandandbarrett' in url:
            data = scrape_holland_barrett(soup, url)
        else:
            data = scrape_generic(soup, url)

        if not data.get('name'):
            title = soup.find('title')
            data['name'] = title.get_text(strip=True) if title else url

        return data
    except Exception as e:
        logger.error(f"Error fetching {url}: {e}")
        return None


# ── Notifications ─────────────────────────────────────────────────────────────

def send_slack(webhook_url, message):
    try:
        requests.post(webhook_url, json={'text': message}, timeout=10)
    except Exception as e:
        logger.error(f"Slack error: {e}")


def send_email(product, old_price, new_price):
    smtp_host   = Settings.get('smtp_host')
    smtp_port   = int(Settings.get('smtp_port', 587))
    smtp_user   = Settings.get('smtp_user')
    smtp_pass   = Settings.get('smtp_pass')
    notify_email = Settings.get('notify_email')

    if not all([smtp_host, smtp_user, smtp_pass, notify_email]):
        return

    discount = round((1 - new_price / old_price) * 100, 1) if old_price else 0
    subject = f"💰 Price drop! {product.name} now {product.currency}{new_price:.2f} ({discount}% off)"
    body = f"""Price alert for {product.name}

Old price: {product.currency}{old_price:.2f}
New price: {product.currency}{new_price:.2f}
Discount:  {discount}%

View product: {product.url}
"""
    try:
        msg = MIMEMultipart()
        msg['From'] = smtp_user
        msg['To'] = notify_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))
        with smtplib.SMTP(smtp_host, smtp_port) as srv:
            srv.starttls()
            srv.login(smtp_user, smtp_pass)
            srv.sendmail(smtp_user, notify_email, msg.as_string())
        logger.info(f"Email sent for {product.name}")
    except Exception as e:
        logger.error(f"Email error: {e}")


def send_pushbullet(product, old_price, new_price):
    api_key = Settings.get('pushbullet_key')
    if not api_key:
        return
    discount = round((1 - new_price / old_price) * 100, 1) if old_price else 0
    try:
        requests.post(
            'https://api.pushbullet.com/v2/pushes',
            headers={'Access-Token': api_key},
            json={
                'type': 'link',
                'title': f"💰 {product.name} – {discount}% off!",
                'body': f"{product.currency}{old_price:.2f} → {product.currency}{new_price:.2f}",
                'url': product.url,
            },
            timeout=10,
        )
    except Exception as e:
        logger.error(f"Pushbullet error: {e}")


def notify(product, old_price, new_price):
    discount = round((1 - new_price / old_price) * 100, 1) if old_price else 0
    msg = (f"💰 Price drop on *{product.name}*!\n"
           f"{product.currency}{old_price:.2f} → *{product.currency}{new_price:.2f}* ({discount}% off)\n"
           f"{product.url}")

    slack_webhook = Settings.get('slack_webhook')
    if slack_webhook:
        send_slack(slack_webhook, msg)

    send_email(product, old_price, new_price)
    send_pushbullet(product, old_price, new_price)


# ── Check logic ───────────────────────────────────────────────────────────────

def check_product(product):
    logger.info(f"Checking: {product.name} ({product.url})")
    data = fetch_product_data(product.url)
    if not data or not data.get('current_price'):
        logger.warning(f"Could not fetch price for {product.name}")
        return

    new_price = data['current_price']
    old_price = product.current_price

    # Update product
    if data.get('name') and product.name.startswith('http'):
        product.name = data['name']
    if data.get('image_url'):
        product.image_url = data['image_url']
    if data.get('original_price'):
        product.original_price = data['original_price']
    product.current_price = new_price
    product.last_checked = datetime.utcnow()
    if data.get('currency'):
        product.currency = data['currency']

    # Record history
    db.session.add(PriceHistory(product_id=product.id, price=new_price))
    db.session.commit()

    # Alert if price dropped enough
    if old_price and new_price < old_price:
        drop_pct = (1 - new_price / old_price) * 100
        if drop_pct >= product.threshold_pct:
            notify(product, old_price, new_price)


def check_all_products():
    with app.app_context():
        products = Product.query.filter_by(active=True).all()
        logger.info(f"Scheduled check: {len(products)} products")
        for p in products:
            check_product(p)


# ── API Routes ────────────────────────────────────────────────────────────────

@app.route('/api/products', methods=['GET'])
def get_products():
    products = Product.query.order_by(Product.created_at.desc()).all()
    return jsonify([p.to_dict() for p in products])


@app.route('/api/products', methods=['POST'])
def add_product():
    data = request.json
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'URL is required'}), 400

    # Try to fetch product info immediately
    fetched = fetch_product_data(url)
    name = data.get('name') or (fetched.get('name') if fetched else None) or url

    product = Product(
        name=name,
        url=url,
        current_price=fetched.get('current_price') if fetched else None,
        original_price=fetched.get('original_price') if fetched else None,
        currency=fetched.get('currency', '€') if fetched else '€',
        image_url=fetched.get('image_url') if fetched else None,
        threshold_pct=float(data.get('threshold_pct', 5.0)),
        last_checked=datetime.utcnow() if fetched else None,
    )
    db.session.add(product)
    db.session.commit()

    if product.current_price:
        db.session.add(PriceHistory(product_id=product.id, price=product.current_price))
        db.session.commit()

    return jsonify(product.to_dict()), 201


@app.route('/api/products/<int:product_id>', methods=['PUT'])
def update_product(product_id):
    product = Product.query.get_or_404(product_id)
    data = request.json
    if 'name' in data:
        product.name = data['name']
    if 'threshold_pct' in data:
        product.threshold_pct = float(data['threshold_pct'])
    if 'active' in data:
        product.active = bool(data['active'])
    db.session.commit()
    return jsonify(product.to_dict())


@app.route('/api/products/<int:product_id>', methods=['DELETE'])
def delete_product(product_id):
    product = Product.query.get_or_404(product_id)
    db.session.delete(product)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/products/<int:product_id>/check', methods=['POST'])
def check_product_now(product_id):
    product = Product.query.get_or_404(product_id)
    check_product(product)
    return jsonify(product.to_dict())


@app.route('/api/products/<int:product_id>/history', methods=['GET'])
def get_history(product_id):
    history = PriceHistory.query.filter_by(product_id=product_id)\
        .order_by(PriceHistory.timestamp.asc()).all()
    return jsonify([h.to_dict() for h in history])


@app.route('/api/settings', methods=['GET'])
def get_settings():
    keys = ['slack_webhook', 'notify_email', 'smtp_host', 'smtp_port',
            'smtp_user', 'pushbullet_key', 'check_interval_minutes']
    return jsonify({k: Settings.get(k, '') for k in keys})


@app.route('/api/settings', methods=['POST'])
def save_settings():
    data = request.json
    for k, v in data.items():
        Settings.set(k, v)
    return jsonify({'ok': True})


@app.route('/api/check-all', methods=['POST'])
def trigger_check_all():
    check_all_products()
    return jsonify({'ok': True})


@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'time': datetime.utcnow().isoformat()})


# ── Startup ───────────────────────────────────────────────────────────────────

def start_scheduler():
    scheduler = BackgroundScheduler()
    interval  = int(Settings.get('check_interval_minutes', 60))
    scheduler.add_job(check_all_products, 'interval', minutes=interval, id='price_check')
    scheduler.start()
    logger.info(f"Scheduler started, checking every {interval} minutes")


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    start_scheduler()
    app.run(host='0.0.0.0', port=5000, debug=False)
