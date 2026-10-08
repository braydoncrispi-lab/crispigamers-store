import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

try:
    import stripe
except ImportError as exc:
    raise RuntimeError(
        "The 'stripe' package is required. Run: pip install -r requirements.txt"
    ) from exc

from flask import Flask, jsonify, redirect, render_template, request, session, url_for


# ============================================================
# PATHS / ENVIRONMENT
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("ORDERS_DB", BASE_DIR / "orders.db"))


def load_dotenv_file(path: Path) -> None:
    """Small .env loader so python-dotenv is not required."""
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv_file(BASE_DIR / ".env")


# ============================================================
# APP / STRIPE CONFIGURATION
# ============================================================

app = Flask(__name__)

app.config.update(
    SECRET_KEY=os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "0") == "1",
)

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:5000").rstrip("/")


# ============================================================
# PRODUCT CATALOG
# ============================================================

PRODUCTS = [
    {
        "id": 1,
        "name": "Neon Skull Tee",
        "price": 24.99,
        "tag": "DROP 01",
        "theme": "cyan",
        "description": "The signature Crispigamers tee featuring the neon skull mark. Built for streams, sessions, and everyday wear.",
        "long_description": "A clean gaming tee built around the Crispigamers identity. Comfortable enough for marathon sessions and sharp enough for everyday wear.",
        "colors": ["Black", "White", "Purple", "Royal Blue"],
        "sizes": ["S", "M", "L", "XL", "2XL", "3XL"],
        "rating": 4.9,
        "reviews": 27,
        "stock": "In stock",
    },
    {
        "id": 2,
        "name": "Crispi Cyber Hoodie",
        "price": 49.99,
        "tag": "LIMITED",
        "theme": "pink",
        "description": "Premium hoodie with a bold Crispigamers mark and relaxed gaming fit.",
        "long_description": "Your cold-night loadout. Designed around the Crispigamers neon aesthetic with a roomy fit for gaming, streaming, and daily wear.",
        "colors": ["Black", "Charcoal", "Purple"],
        "sizes": ["S", "M", "L", "XL", "2XL", "3XL"],
        "rating": 5.0,
        "reviews": 14,
        "stock": "Low stock",
    },
    {
        "id": 3,
        "name": "Neon Glow Snapback",
        "price": 29.99,
        "tag": "BEST SELLER",
        "theme": "blue",
        "description": "Structured snapback with a Crispigamers neon mark.",
        "long_description": "A clean adjustable gaming cap built around the neon skull identity. Easy to wear on stream or away from the setup.",
        "colors": ["Black", "Black/Blue", "Black/Purple"],
        "sizes": ["OS"],
        "rating": 4.8,
        "reviews": 19,
        "stock": "In stock",
    },
    {
        "id": 4,
        "name": "Overclocked Fuel Mug",
        "price": 22.99,
        "tag": "NEW DROP",
        "theme": "orange",
        "description": "Crispigamers coffee mug for caffeine-powered gaming sessions.",
        "long_description": "Your official fuel tank. A bold desk piece that brings the Crispigamers neon look into your setup.",
        "colors": ["Black", "White"],
        "sizes": ["12 oz", "15 oz"],
        "rating": 4.7,
        "reviews": 9,
        "stock": "In stock",
    },
]


# ============================================================
# DATABASE
# ============================================================


def db_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with db_connection() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                stripe_session_id TEXT UNIQUE NOT NULL,
                email TEXT,
                amount_total INTEGER NOT NULL,
                currency TEXT NOT NULL,
                payment_status TEXT NOT NULL,
                items_json TEXT NOT NULL,
                shipping_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS stripe_events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )


init_db()


# ============================================================
# PRODUCT / CART HELPERS
# ============================================================


def get_product(product_id):
    return next((p for p in PRODUCTS if p["id"] == product_id), None)


def default_variant(product):
    color = product["colors"][0]
    size = product["sizes"][1] if len(product["sizes"]) > 1 else product["sizes"][0]
    return color, size


def normalize_cart():
    raw = session.get("cart", {})
    normalized = {}

    if not isinstance(raw, dict):
        return normalized

    for key, quantity in raw.items():
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            continue

        if quantity <= 0:
            continue

        # New format: product_id|color|size
        if isinstance(key, str) and key.count("|") >= 2:
            product_id_text, color, size = key.split("|", 2)
            try:
                product = get_product(int(product_id_text))
            except ValueError:
                product = None

            if not product:
                continue

            if color not in product["colors"] or size not in product["sizes"]:
                continue

            normalized[f"{product['id']}|{color}|{size}"] = min(quantity, 99)
            continue

        # Backwards compatibility with old product-only carts.
        try:
            product = get_product(int(key))
        except (TypeError, ValueError):
            product = None

        if product:
            color, size = default_variant(product)
            normalized[f"{product['id']}|{color}|{size}"] = min(quantity, 99)

    return normalized


def get_cart():
    cart = normalize_cart()
    items = []
    total_cents = 0
    count = 0

    for key, quantity in cart.items():
        try:
            product_id_text, color, size = key.split("|", 2)
            product = get_product(int(product_id_text))
        except (ValueError, TypeError):
            product = None

        if not product:
            continue

        unit_cents = int(round(product["price"] * 100))
        subtotal_cents = unit_cents * quantity

        items.append(
            {
                **product,
                "quantity": quantity,
                "color": color,
                "size": size,
                "unit_cents": unit_cents,
                "subtotal_cents": subtotal_cents,
                "price_display": f"${unit_cents / 100:.2f}",
                "subtotal_display": f"${subtotal_cents / 100:.2f}",
                "cart_key": key,
            }
        )

        total_cents += subtotal_cents
        count += quantity

    session["cart"] = cart
    session.modified = True

    return items, total_cents, count


def stripe_ready():
    return bool(stripe.api_key)


# ============================================================
# ORDER HELPERS
# ============================================================


def safe_stripe_product_name(line_item):
    """Return a useful product/variant name from a Stripe line item."""
    price = getattr(line_item, "price", None)
    if not price:
        return getattr(line_item, "description", None) or "Crispigamers item"

    product_ref = getattr(price, "product", None)

    if hasattr(product_ref, "name"):
        return product_ref.name

    if isinstance(product_ref, str):
        try:
            product = stripe.Product.retrieve(product_ref)
            return getattr(product, "name", None) or "Crispigamers item"
        except Exception:
            pass

    return getattr(line_item, "description", None) or "Crispigamers item"


def create_order_from_session(checkout_session):
    """Save a paid Stripe Checkout Session exactly once."""
    session_id = checkout_session.id

    with db_connection() as db:
        existing = db.execute(
            "SELECT id FROM orders WHERE stripe_session_id = ?",
            (session_id,),
        ).fetchone()

        if existing:
            return existing["id"]

    customer_details = getattr(checkout_session, "customer_details", None)
    email = getattr(customer_details, "email", None) if customer_details else None

    shipping = getattr(checkout_session, "shipping_details", None)
    shipping_dict = None

    if shipping:
        if hasattr(shipping, "to_dict_recursive"):
            shipping_dict = shipping.to_dict_recursive()
        else:
            try:
                shipping_dict = dict(shipping)
            except Exception:
                shipping_dict = {"raw": str(shipping)}

    line_items_response = stripe.checkout.Session.list_line_items(
        session_id,
        limit=100,
        expand=["data.price.product"],
    )

    order_items = []

    for line in line_items_response.data:
        order_items.append(
            {
                "description": safe_stripe_product_name(line),
                "quantity": int(line.quantity or 0),
                "amount_total": int(line.amount_total or 0),
                "currency": getattr(line, "currency", None) or "usd",
            }
        )

    order_id = str(uuid4())

    with db_connection() as db:
        db.execute(
            """
            INSERT OR IGNORE INTO orders (
                id,
                stripe_session_id,
                email,
                amount_total,
                currency,
                payment_status,
                items_json,
                shipping_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                session_id,
                email,
                int(checkout_session.amount_total or 0),
                checkout_session.currency or "usd",
                checkout_session.payment_status or "unknown",
                json.dumps(order_items),
                json.dumps(shipping_dict) if shipping_dict else None,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    return order_id


# ============================================================
# TEMPLATE GLOBALS
# ============================================================


@app.context_processor
def inject_globals():
    _, total_cents, count = get_cart()
    return {
        "cart_total": total_cents / 100,
        "cart_count": count,
        "stripe_enabled": stripe_ready(),
    }


# ============================================================
# STORE ROUTES
# ============================================================


@app.get("/")
def home():
    return render_template("index.html", products=PRODUCTS)


@app.get("/product/<int:product_id>")
def product_page(product_id):
    product = get_product(product_id)
    if not product:
        return redirect(url_for("home"))
    return render_template("product.html", product=product)


@app.post("/cart/add")
def add_to_cart():
    product_id = request.form.get("product_id", type=int)
    quantity = request.form.get("quantity", 1, type=int) or 1
    quantity = max(1, min(quantity, 99))

    product = get_product(product_id)
    if not product:
        return redirect(url_for("home"))

    color = request.form.get("color") or product["colors"][0]
    size = request.form.get("size") or default_variant(product)[1]

    if color not in product["colors"]:
        color = product["colors"][0]
    if size not in product["sizes"]:
        size = product["sizes"][0]

    cart = normalize_cart()
    cart_key = f"{product_id}|{color}|{size}"
    cart[cart_key] = min(cart.get(cart_key, 0) + quantity, 99)
    session["cart"] = cart
    session.modified = True

    destination = request.form.get("next") or url_for("cart")
    if not destination.startswith("/") or destination.startswith("//"):
        destination = url_for("cart")

    return redirect(destination)


@app.post("/cart/update")
def update_cart():
    cart_key = request.form.get("cart_key", "")
    quantity = request.form.get("quantity", 1, type=int) or 0
    cart = normalize_cart()

    if quantity <= 0:
        cart.pop(cart_key, None)
    elif cart_key in cart:
        cart[cart_key] = min(quantity, 99)

    session["cart"] = cart
    session.modified = True
    return redirect(url_for("cart"))


@app.post("/cart/remove")
def remove_from_cart():
    cart_key = request.form.get("cart_key", "")
    cart = normalize_cart()
    cart.pop(cart_key, None)
    session["cart"] = cart
    session.modified = True
    return redirect(url_for("cart"))


@app.post("/cart/clear")
def clear_cart():
    session["cart"] = {}
    session.modified = True
    return redirect(url_for("cart"))


@app.get("/cart")
def cart():
    items, total_cents, count = get_cart()
    return render_template(
        "cart.html",
        items=items,
        total=total_cents / 100,
        count=count,
    )


# ============================================================
# CHECKOUT
# ============================================================


@app.get("/checkout")
def checkout():
    items, total_cents, count = get_cart()

    if not items:
        return redirect(url_for("cart"))

    return render_template(
        "checkout.html",
        items=items,
        total=total_cents / 100,
        count=count,
    )


@app.post("/checkout/create-session")
def create_checkout_session():
    items, total_cents, count = get_cart()

    if not items:
        return redirect(url_for("cart"))

    if not stripe_ready():
        return render_template(
            "checkout.html",
            items=items,
            total=total_cents / 100,
            count=count,
            checkout_error="Stripe is not configured. Add STRIPE_SECRET_KEY to your .env file.",
        )

    line_items = []

    for item in items:
        line_items.append(
            {
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": item["name"],
                        "description": f"{item['color']} / {item['size']} · Official Crispigamers merch",
                    },
                    "unit_amount": item["unit_cents"],
                },
                "quantity": item["quantity"],
            }
        )

    try:
        checkout_session = stripe.checkout.Session.create(
            mode="payment",
            line_items=line_items,
            success_url=(
                f"{BASE_URL}{url_for('payment_success')}"
                "?session_id={CHECKOUT_SESSION_ID}"
            ),
            cancel_url=f"{BASE_URL}{url_for('checkout')}",
            billing_address_collection="auto",
            shipping_address_collection={"allowed_countries": ["US"]},
            phone_number_collection={"enabled": True},
            allow_promotion_codes=True,
            customer_creation="always",
            metadata={
                "store": "crispigamers",
                "cart_count": str(count),
            },
        )
    except Exception:
        app.logger.exception("Stripe Checkout creation failed")
        return render_template(
            "checkout.html",
            items=items,
            total=total_cents / 100,
            count=count,
            checkout_error="Stripe could not start checkout. Check your Stripe key and try again.",
        ), 502

    return redirect(checkout_session.url, code=303)


@app.get("/payment/success")
def payment_success():
    session_id = request.args.get("session_id", "").strip()

    if not session_id or not stripe_ready():
        return redirect(url_for("home"))

    try:
        checkout_session = stripe.checkout.Session.retrieve(session_id)

        if checkout_session.payment_status != "paid":
            return render_template(
                "success.html",
                paid=False,
                total=0,
                order_id="",
                verification_error="The payment has not been marked as paid yet. Please wait a moment and check your Stripe dashboard.",
            ), 402

        order_id = create_order_from_session(checkout_session)
        session["cart"] = {}
        session.modified = True

        return render_template(
            "success.html",
            paid=True,
            total=(checkout_session.amount_total or 0) / 100,
            order_id=order_id[-8:].upper(),
        )

    except Exception:
        app.logger.exception("Unable to verify Stripe payment")
        return render_template(
            "success.html",
            paid=False,
            total=0,
            order_id="",
            verification_error="We could not verify the payment yet. If you were charged, do not pay again; check your Stripe dashboard or contact support.",
        ), 502


# ============================================================
# STRIPE WEBHOOK
# ============================================================


@app.post("/stripe/webhook")
def stripe_webhook():
    if not STRIPE_WEBHOOK_SECRET:
        return jsonify(error="STRIPE_WEBHOOK_SECRET is not configured"), 500

    payload = request.get_data()
    signature = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload,
            signature,
            STRIPE_WEBHOOK_SECRET,
        )
    except ValueError:
        return jsonify(error="Invalid payload"), 400
    except stripe.error.SignatureVerificationError:
        return jsonify(error="Invalid signature"), 400

    # Idempotency: Stripe may retry webhook events.
    with db_connection() as db:
        already_seen = db.execute(
            "SELECT 1 FROM stripe_events WHERE event_id = ?",
            (event.id,),
        ).fetchone()

        if already_seen:
            return jsonify(received=True)

        db.execute(
            "INSERT INTO stripe_events (event_id, event_type, created_at) VALUES (?, ?, ?)",
            (
                event.id,
                event.type,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    if event.type in {
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
    }:
        checkout_session = event.data.object

        if checkout_session.payment_status in {"paid", "no_payment_required"}:
            try:
                create_order_from_session(checkout_session)
            except Exception:
                app.logger.exception("Order creation from webhook failed")
                return jsonify(error="Order processing failed"), 500

    return jsonify(received=True)


# ============================================================
# JSON CART API
# ============================================================


@app.get("/api/cart")
def api_cart():
    items, total_cents, count = get_cart()

    safe_items = []
    for item in items:
        safe_items.append(
            {
                "id": item["id"],
                "name": item["name"],
                "color": item["color"],
                "size": item["size"],
                "quantity": item["quantity"],
                "unit_price": item["unit_cents"] / 100,
                "subtotal": item["subtotal_cents"] / 100,
            }
        )

    return jsonify(
        {
            "items": safe_items,
            "total": total_cents / 100,
            "count": count,
        }
    )


# ============================================================
# ERROR HANDLERS
# ============================================================


@app.errorhandler(404)
def not_found(_error):
    return render_template("404.html"), 404


@app.errorhandler(500)
def server_error(_error):
    return render_template("500.html"), 500

@app.route("/robots.txt")
def robots_txt():
    return (
        "User-agent: *\n"
        "Allow: /\n"
        f"Sitemap: {BASE_URL.rstrip('/')}/sitemap.xml\n"
    ), 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/sitemap.xml")
def sitemap_xml():
    urls = [
        url_for("home", _external=True),
        url_for("cart", _external=True),
    ]

    for product in PRODUCTS:
        urls.append(url_for("product_page", product_id=product["id"], _external=True))

    xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'

    for page_url in urls:
        xml += f"  <url><loc>{page_url}</loc></url>\n"

    xml += "</urlset>\n"

    return xml, 200, {"Content-Type": "application/xml; charset=utf-8"}

# ============================================================
# LOCAL DEVELOPMENT
# ============================================================

if __name__ == "__main__":
    app.run(
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG", "1") == "1",
    )
