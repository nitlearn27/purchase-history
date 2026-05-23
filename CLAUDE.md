# CLAUDE.md

This file provides guidance to Claude Code when working in this repository.

## Project Overview

A Playwright-based automation script that logs into Flipkart with user credentials,
scrapes the last 10 orders from the order history, expands each order into its
constituent items (via "See all items"), and produces a per-product report containing:

1. Purchase date- this may be same date for all product in a single order.
2. Number of times that product appears across the last 10 orders

## Tech Stack

**Language**: Python 3.10+ (preferred) or Node.js 18+
**Browser automation**: Playwright (playwright for Python, or @playwright/test for Node)
**Browser**: Chromium, headed mode by default (Flipkart login requires OTP interaction)
**Config**: Credentials read from a local .env file — never hardcoded, never committed

## Environment Setup
bash
# Python
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install playwright python-dotenv
playwright install chromium

Create a .env file in the project root (and add it to .gitignore):
FLIPKART_USERNAME=<email-or-phone>
FLIPKART_PASSWORD=<password>

## Running
bash
python scrape_flipkart_orders.py            # headed, default 10 orders
python scrape_flipkart_orders.py --headed=false   # headless (only if no OTP needed)
python scrape_flipkart_orders.py --orders=10      # override order count

Output is written to orders_report.json and printed as a table to stdout.

## High-Level Flow

The script must follow this exact sequence. Do not skip steps.

1. **Launch browser** (Chromium, headed, persistent context recommended so the session
   can be reused without re-logging in on subsequent runs).
2. **Navigate** to https://www.flipkart.com.
3. **Dismiss** the login modal if one appears on landing (Flipkart sometimes shows one
   immediately; sometimes you need to click the Login button manually).
4. **Login** using the credentials from .env.
   - Enter username/phone, click Continue, enter password, submit.
   - If Flipkart asks for an OTP, **pause and wait for the user** to enter it manually
     in the browser (do not attempt to bypass). Use page.wait_for_url on the post-login
     home page, or wait for a known logged-in element (e.g. the account dropdown).
5. **Navigate** to the orders page: `https://www.flipkart.com/account/orders`.
6. **Collect the last 10 orders**. Orders are listed newest-first; take the first 10
   visible order cards. If fewer than 10 exist, process whatever is there and note it
   in the output.
7. **For each order**: if the order card shows a "See all items" / "See N more items"
   link (multi-item orders), click it to expand and capture every product inside.
   Single-item orders need no expansion.
8. **For each product** extract:
   - Product title (used as the key to count repeats)
   - Purchase date (or "Order placed on" date) — normalize to ISO YYYY-MM-DD
9. **Aggregate** across all collected products: group by product title and compute the
   purchase count. Each row in the final report represents one product occurrence
   (date + total count of that product across the 10 orders).
10. **Write** results to orders_report.json and print a summary table.

## Selector Strategy

Flipkart's DOM uses hashed CSS class names (e.g. _1AtVbE) that change frequently.
**Never rely on those class names alone.** Prefer, in this order:

1. Role + accessible name: page.get_by_role("button", name="Login")
2. Visible text: page.get_by_text("See all items", exact=False)
3. Stable test attributes if present (data-testid, data-tkid)
4. Structural XPath anchored on visible labels as a last resort
5. Hashed class names — only with a comment explaining the fallback and a TODO to
   replace

When a selector fails, log the page URL and the surrounding HTML snippet, then exit
non-zero. Do not silently continue with empty data.

## Expected Output Shape

orders_report.json:
json
{
  "scraped_at": "2026-05-23T10:15:00+05:30",
  "orders_scanned": 10,
  "products": [
    {
      "title": "Boat Airdopes 141 Bluetooth Headset",
      "purchase_date": "2026-04-12",
      "purchase_count_in_last_10_orders": 2
    }
  ]
}

The purchase_count_in_last_10_orders value is the same for every row of the same
product title — it is a per-product aggregate repeated on each row, as the user
requested.

## Important Constraints & Pitfalls

**No credential logging.** Never print FLIPKART_PASSWORD or OTP values. Mask
  the username in logs (e.g. show only the last 4 chars).
**Respect Flipkart.** Use a single browser context, default Playwright user-agent,
  human-paced clicks (page.wait_for_load_state("networkidle") between major
  navigations, small asyncio.sleep/page.wait_for_timeout between scrolls). Do
  not parallelize requests.
**Captcha / OTP.** If a captcha or OTP screen appears, stop and surface a clear
  message to the user. Never attempt to solve a captcha.
**Lazy loading.** The orders page lazy-loads as you scroll. Scroll until at least
  10 order cards are present in the DOM, or until no new cards load after 2 attempts.
**"See all items" wording varies.** It may appear as "See all N items", "View all
  items", or similar. Match on the leading word "See" or "View" plus the word "item".
**Date parsing.** Flipkart shows dates like "Delivered on Mon, Apr 12th '26" or
  "Order placed on 12 Apr 2026". Use a tolerant parser (e.g. dateutil.parser) and
  fall back to extracting the first date-like substring.
**Session reuse.** Persist storage_state to auth_state.json after first login
  so reruns skip the login flow. Delete this file to force re-login.
**.gitignore must include**: .env, auth_state.json, __pycache__/,
  .venv/, orders_report.json (output is regenerated each run).

## File Layout
.
├── CLAUDE.md
├── README.md
├── .env.example          # template, no real values
├── .gitignore
├── requirements.txt
└── scrape_flipkart_orders.py

## When Editing This Project

Keep selectors centralized at the top of scrape_flipkart_orders.py in a
  SELECTORS dict so they can be updated in one place when Flipkart changes its
  markup.
Every Playwright action that depends on a network response must have an explicit
  wait (expect(...).to_be_visible() or page.wait_for_selector(...)) — never
  sleep blindly except for human-pacing delays.
Print progress to stdout in the form [order 3/10] expanding 'See all items'...
  so the user can follow along during the headed run.
If the script can't find the orders page (e.g. URL redirected to login), it
  should re-run the login flow once, then give up if that also fails.

## Out of Scope

No purchase, cancel, return, or any write action on the account.
No scraping beyond the 10 most recent orders unless --orders is explicitly
  raised by the user.