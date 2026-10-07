# Crispigamers Merch Store

Flask storefront with:

- Crispigamers neon branding
- Product detail pages
- Color and size variants
- Session shopping cart
- SQLite order database
- Stripe Checkout
- Stripe webhook verification
- Shipping address collection
- Promo-code support
- Responsive storefront

## 1. Install Python

Use Python 3.10+.

## 2. Install dependencies

```bash
python -m pip install -r requirements.txt
```

## 3. Configure Stripe

Copy `.env.example` to `.env` and add your Stripe test secret key.

```env
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
BASE_URL=http://127.0.0.1:5000
```

Never commit `.env` or real Stripe secret keys to GitHub.

## 4. Run the store

```bash
python app.py
```

Open:

http://127.0.0.1:5000

## 5. Local Stripe webhook testing

Install the Stripe CLI, log in, start the Flask app, then run:

```bash
stripe listen --forward-to 127.0.0.1:5000/stripe/webhook
```

Stripe CLI prints a webhook signing secret beginning with `whsec_`. Put that value in `.env` as `STRIPE_WEBHOOK_SECRET`.

## 6. Test payment

Use Stripe test mode and one of Stripe's official test card numbers, such as:

4242 4242 4242 4242

Use any future expiration date, any three-digit CVC, and a valid test billing/shipping address.

## Production checklist

- Use a strong permanent `FLASK_SECRET_KEY`.
- Set `FLASK_DEBUG=0`.
- Use HTTPS.
- Set `SESSION_COOKIE_SECURE=1`.
- Use live Stripe keys only in a protected server environment.
- Configure a Stripe webhook endpoint for `/stripe/webhook`.
- Back up `orders.db` or move orders to a production database.
- Add real inventory tracking before accepting large volumes of orders.
