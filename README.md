# Flipkart Purchase History Scraper

Logs into Flipkart, scrapes the last 10 orders, and produces a per-product report with
purchase dates and repeat-purchase counts.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
playwright install chromium
```

Copy `.env.example` to `.env` and fill in your credentials:

```
FLIPKART_USERNAME=your_email_or_phone
FLIPKART_PASSWORD=your_password
```

## Usage

```bash
# Default: headed browser, last 10 orders
python scrape_flipkart_orders.py

# Headless (only if no OTP is needed)
python scrape_flipkart_orders.py --headed=false

# Override order count
python scrape_flipkart_orders.py --orders=5
```

Output is written to `orders_report.json` and printed as a table to stdout.

## Notes

- On first run the browser opens so you can handle any OTP prompt manually.
- After a successful login `auth_state.json` is saved; subsequent runs reuse it.
- Delete `auth_state.json` to force a fresh login.
- Never commit `.env` or `auth_state.json`.
