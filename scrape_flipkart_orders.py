"""
Flipkart order history scraper.
Login: email → Request OTP on Flipkart → fetch OTP via Gmail API → complete login.
"""

import argparse
import asyncio
import base64
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from dateutil import parser as dateutil_parser
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ---------------------------------------------------------------------------
# Selectors — update here when Flipkart changes its markup
# ---------------------------------------------------------------------------
SELECTORS = {
    # Flipkart: landing page modal
    "modal_close": "button[class*='close'], [class*='_2KpZ6l'], [class*='close-button']",
    # Flipkart: login form
    "username_input": (
        "input[type='text'][placeholder*='mail'], "
        "input[type='tel'], "
        "input[placeholder*='hone'], "
        "input[placeholder*='ser']"
    ),
    "request_otp_button": "button:has-text('Request OTP'), button:has-text('Continue')",
    # OTP entry — single field variant
    "otp_input_single": (
        "input[placeholder*='OTP'], input[placeholder*='otp'], "
        "input[placeholder*='one time'], input[class*='otp'], "
        "input[type='number'][maxlength='6']"
    ),
    # OTP entry — 6 individual digit-box variant
    "otp_digit_inputs": "input[maxlength='1']",
    "otp_verify_button": (
        "button:has-text('Verify'), button:has-text('Submit'), "
        "button:has-text('Confirm'), button[type='submit']"
    ),
    "logged_in_indicator": (
        "a:has-text('My Account'), span:has-text('Account'), "
        "[class*='account'][href*='wishlist'], [class*='profileIcon']"
    ),
    # Flipkart: orders page
    "order_card": (
        "div[class*='_1YokD2'], div[class*='orderCard'], "
        "div[class*='_2LjOP2'], div[class*='order-card']"
    ),
}

# ---------------------------------------------------------------------------
# Gmail API config
# ---------------------------------------------------------------------------
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

load_dotenv()
GMAIL_TOKEN_FILE = Path(os.getenv("GMAIL_TOKEN_FILE", "token.json"))

AUTH_STATE_FILE = Path("auth_state.json")
ORDERS_REPORT_FILE = Path("orders_report.json")
FLIPKART_HOME = "https://www.flipkart.com"
FLIPKART_ORDERS = "https://www.flipkart.com/account/orders"


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def mask(value: str) -> str:
    return f"***{value[-4:]}" if value and len(value) > 4 else "****"


def parse_date(raw: str) -> str:
    if not raw:
        return "unknown"
    cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", raw)
    cleaned = re.sub(r"'(\d{2})\b", lambda m: f"20{m.group(1)}", cleaned)
    try:
        return dateutil_parser.parse(cleaned, fuzzy=True).date().isoformat()
    except Exception:
        m = re.search(r"\d{4}-\d{2}-\d{2}", raw)
        if m:
            return m.group(0)
        m = re.search(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})", raw)
        if m:
            d, mo, y = m.groups()
            y = f"20{y}" if len(y) == 2 else y
            return f"{y}-{int(mo):02d}-{int(d):02d}"
        return "unknown"


# ---------------------------------------------------------------------------
# Gmail API — authentication and OTP retrieval
# ---------------------------------------------------------------------------

def get_gmail_service(login_hint: str = ""):
    """
    Return an authenticated Gmail API service object.

    Credentials are read from GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in .env.
    On first run a browser opens at http://localhost:3000 for OAuth consent and
    token.json is saved.  Subsequent runs refresh the token silently.

    login_hint: pre-selects the Google account in the consent screen (avoids
                the "wrong account" problem when multiple accounts are signed in).
    """
    creds = None

    if GMAIL_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_FILE), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("[gmail] Refreshing Gmail OAuth token…")
            creds.refresh(Request())
        else:
            client_id = os.getenv("GMAIL_CLIENT_ID", "").strip()
            client_secret = os.getenv("GMAIL_CLIENT_SECRET", "").strip()

            if not client_id or not client_secret:
                print(
                    "\n[error] GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET must be set in .env\n\n"
                    "  How to get them:\n"
                    "  1. https://console.cloud.google.com → select your project\n"
                    "  2. APIs & Services → Credentials\n"
                    "  3. Find your OAuth 2.0 Client ID → click the pencil/edit icon\n"
                    "  4. Copy 'Client ID' and 'Client secret' into .env\n"
                )
                sys.exit(1)

            # Desktop app client — Google automatically allows any http://localhost:{port}
            # so no redirect URI needs to be registered in Cloud Console.
            client_config = {
                "installed": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uris": ["http://localhost"],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }

            print(
                f"[gmail] Opening browser for Gmail OAuth consent (one-time setup)…\n"
                f"        Account: {login_hint or '(not specified)'}"
            )
            flow = InstalledAppFlow.from_client_config(client_config, GMAIL_SCOPES)
            # port=0 → OS picks any free port; no registration required for Desktop apps
            creds = flow.run_local_server(
                port=0,
                login_hint=login_hint or None,
            )

        GMAIL_TOKEN_FILE.write_text(creds.to_json())
        print("[gmail] OAuth token saved to token.json.")

    return build("gmail", "v1", credentials=creds)


def _decode_gmail_body(message: dict) -> str:
    """Extract plain-text (or de-tagged HTML) body from a Gmail API message."""
    payload = message.get("payload", {})

    def b64decode(data: str) -> str:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore") if data else ""

    def walk_parts(parts) -> tuple[str, str]:
        plain = html = ""
        for part in parts:
            mime = part.get("mimeType", "")
            data = part.get("body", {}).get("data", "")
            if mime == "text/plain" and data:
                plain = plain or b64decode(data)
            elif mime == "text/html" and data:
                html = html or re.sub(r"<[^>]+>", " ", b64decode(data))
            if part.get("parts"):
                p2, h2 = walk_parts(part["parts"])
                plain = plain or p2
                html = html or h2
        return plain, html

    parts = payload.get("parts", [])
    if parts:
        plain, html = walk_parts(parts)
        return plain or html

    # Non-multipart message
    return b64decode(payload.get("body", {}).get("data", ""))


def fetch_otp_via_gmail_api(
    service, after_timestamp: int, max_wait_secs: int = 90
) -> str | None:
    """
    Synchronous function (runs in a thread).
    Polls the Gmail API every 6 s for a Flipkart email that arrived after
    `after_timestamp` (Unix seconds), extracts and returns a 6-digit OTP.
    """
    interval = 6
    attempts = max_wait_secs // interval
    # Gmail API 'after:' accepts Unix timestamps
    query = f"from:flipkart after:{after_timestamp}"

    print(f"[gmail] Polling Gmail API for Flipkart OTP (up to {max_wait_secs}s)…")

    for attempt in range(attempts):
        try:
            result = (
                service.users()
                .messages()
                .list(userId="me", q=query, maxResults=5)
                .execute()
            )
            messages = result.get("messages", [])

            for stub in messages:
                msg = (
                    service.users()
                    .messages()
                    .get(userId="me", id=stub["id"], format="full")
                    .execute()
                )
                body = _decode_gmail_body(msg)
                match = re.search(r"\b(\d{6})\b", body)
                if match:
                    print("[gmail] 6-digit OTP found in Flipkart email.")
                    return match.group(1)

            remaining = max_wait_secs - (attempt + 1) * interval
            if messages:
                print(f"[gmail] Flipkart email found but no OTP yet ({remaining}s remaining)…")
            else:
                print(f"[gmail] No Flipkart OTP email yet ({remaining}s remaining)…")

        except HttpError as exc:
            print(f"[gmail] API error on attempt {attempt + 1}: {exc}")

        time.sleep(interval)

    return None


# ---------------------------------------------------------------------------
# Flipkart: auth helpers
# ---------------------------------------------------------------------------

async def dismiss_modal(page) -> None:
    try:
        close = page.locator(SELECTORS["modal_close"]).first
        await close.wait_for(state="visible", timeout=4_000)
        await close.click()
        print("[nav] Dismissed login modal.")
    except PlaywrightTimeoutError:
        pass


async def is_logged_in(page) -> bool:
    try:
        await page.wait_for_selector(SELECTORS["logged_in_indicator"], timeout=4_000)
        return True
    except PlaywrightTimeoutError:
        return False


async def enter_otp_on_flipkart(page, otp: str) -> None:
    """Fill OTP into Flipkart's input — handles single field and 6-digit-box layouts."""
    # Single input
    single = page.locator(SELECTORS["otp_input_single"]).first
    try:
        await single.wait_for(state="visible", timeout=5_000)
        await single.fill(otp)
        print("[auth] OTP entered (single input).")
        return
    except PlaywrightTimeoutError:
        pass

    # 6 separate digit inputs
    digit_inputs = page.locator(SELECTORS["otp_digit_inputs"])
    if await digit_inputs.count() >= 6:
        for i, digit in enumerate(otp):
            await digit_inputs.nth(i).fill(digit)
        print("[auth] OTP entered (digit-box inputs).")
        return

    print("[error] Could not locate OTP input on Flipkart page.")
    print(f"  URL: {page.url}")
    sys.exit(1)


async def save_auth(context) -> None:
    storage = await context.storage_state()
    AUTH_STATE_FILE.write_text(json.dumps(storage, indent=2))
    print("[auth] Flipkart session saved to auth_state.json.")


async def login(page, flipkart_email: str, gmail_service) -> None:
    """
    Flipkart OTP login:
      1. Enter email → click Request OTP
      2. Fetch OTP from Gmail API (non-blocking via thread)
      3. Enter OTP → verify
    """
    print(f"[auth] Logging in to Flipkart as …{mask(flipkart_email)}")

    await page.goto(FLIPKART_HOME, wait_until="domcontentloaded")
    await page.wait_for_load_state("networkidle")

    await dismiss_modal(page)

    if await is_logged_in(page):
        print("[auth] Already logged in via saved session.")
        return

    # Open login form
    try:
        btn = page.get_by_role("link", name=re.compile(r"^Login$", re.I)).or_(
            page.get_by_role("button", name=re.compile(r"^Login$", re.I))
        )
        await btn.first.click(timeout=5_000)
        await page.wait_for_timeout(1_500)
    except PlaywrightTimeoutError:
        pass

    # Enter email
    username_input = page.locator(SELECTORS["username_input"]).first
    await username_input.wait_for(state="visible", timeout=12_000)
    await username_input.fill(flipkart_email)

    # Record time just before requesting OTP so we only pick up the fresh email
    otp_request_at = int(datetime.now(tz=timezone.utc).timestamp()) - 30

    # Click "Request OTP"
    otp_btn = page.locator(SELECTORS["request_otp_button"]).first
    await otp_btn.wait_for(state="visible", timeout=8_000)
    await otp_btn.click()
    await page.wait_for_timeout(2_000)
    print("[auth] OTP requested from Flipkart.")

    # Fetch OTP via Gmail API in a background thread (keeps Playwright event loop alive)
    otp = await asyncio.to_thread(
        fetch_otp_via_gmail_api, gmail_service, otp_request_at
    )

    if not otp:
        print(
            "\n[error] Could not retrieve OTP from Gmail.\n"
            "  • Ensure the Gmail API is enabled and credentials.json is correct.\n"
            "  • Check that the Flipkart OTP email landed in your inbox (not spam).\n"
            "  • Delete token.json and re-run to redo the OAuth consent if needed.\n"
        )
        sys.exit(1)

    # Enter OTP on Flipkart
    await enter_otp_on_flipkart(page, otp)
    await page.wait_for_timeout(500)

    # Submit
    verify_btn = page.locator(SELECTORS["otp_verify_button"]).first
    await verify_btn.click()

    try:
        await page.wait_for_url(re.compile(r"flipkart\.com(?!/login)"), timeout=30_000)
    except PlaywrightTimeoutError:
        print("[error] Login did not complete after OTP entry. OTP may have expired; try again.")
        sys.exit(1)

    await page.wait_for_load_state("networkidle")
    print("[auth] Flipkart login successful.")


# ---------------------------------------------------------------------------
# Order scraping
# ---------------------------------------------------------------------------

async def scroll_until_n_orders(page, n: int) -> list:
    prev_count = 0
    stall_attempts = 0
    while True:
        cards = await page.query_selector_all(SELECTORS["order_card"])
        count = len(cards)
        print(f"[orders] Found {count} order card(s) so far…")
        if count >= n:
            break
        if count == prev_count:
            stall_attempts += 1
            if stall_attempts >= 2:
                print(f"[orders] No new cards after 2 scroll attempts; stopping with {count}.")
                break
        else:
            stall_attempts = 0
        prev_count = count
        await page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
        await page.wait_for_timeout(1_800)
    return await page.query_selector_all(SELECTORS["order_card"])


async def extract_date_from_card(card) -> str:
    try:
        date_el = await card.query_selector(
            "span:has-text('Delivered'), span:has-text('Order'), "
            "div:has-text('Ordered on'), div:has-text('Order placed'), "
            "[class*='_3XGuOJ'], [class*='_2JC35k']"
        )
        if date_el:
            return parse_date(await date_el.inner_text())
        full_text = await card.inner_text()
        m = re.search(
            r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}[,\s']+\d{2,4}|"
            r"\d{1,2}\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4}",
            full_text, re.I,
        )
        if m:
            return parse_date(m.group(0))
    except Exception:
        pass
    return "unknown"


async def expand_and_get_products(page, card, order_idx: int, total: int) -> list[dict]:
    see_all = await card.query_selector(
        "a:has-text('See'), a:has-text('View'), span:has-text('See'), span:has-text('View')"
    )
    if see_all:
        text = (await see_all.inner_text()).strip()
        if re.search(r"(see|view).+item", text, re.I):
            print(f"[order {order_idx}/{total}] Expanding '{text}'…")
            await see_all.click()
            await page.wait_for_load_state("networkidle")
            await page.wait_for_timeout(800)

    date = await extract_date_from_card(card)

    title_els = await card.query_selector_all(
        "a[class*='s1Q9rs'], a[class*='_2Kcoaa'], a[class*='IRpwTa'], "
        "[class*='_3njFb'], [class*='_2cLu-l'], a[title], "
        "div[class*='_3NwUo0'] a, p[class*='_2-ut94']"
    )

    products: list[dict] = []
    seen: set[str] = set()
    for el in title_els:
        raw = (await el.inner_text()).strip() or (await el.get_attribute("title") or "").strip()
        title = re.sub(r"\s+", " ", raw).strip()
        if title and title not in seen:
            seen.add(title)
            products.append({"title": title, "date": date})

    if not products:
        for link in await card.query_selector_all("a"):
            t = (await link.inner_text()).strip()
            if len(t) > 10 and t not in seen:
                seen.add(t)
                products.append({"title": t, "date": date})
                if len(products) >= 10:
                    break

    if not products:
        print(f"[order {order_idx}/{total}] WARNING: no product titles found.")
        html = await card.inner_html()
        print(f"  Card HTML snippet (first 600 chars):\n  {html[:600]}")

    print(f"[order {order_idx}/{total}] {len(products)} product(s), date={date}")
    return products


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(num_orders: int, headless: bool) -> None:
    load_dotenv()
    flipkart_email = os.getenv("FLIPKART_USERNAME", "")

    if not flipkart_email:
        print("[error] FLIPKART_USERNAME must be set in .env")
        sys.exit(1)

    # Authenticate with Gmail API before opening the browser
    print("[gmail] Authenticating with Gmail API…")
    gmail_service = get_gmail_service(login_hint=flipkart_email)
    print("[gmail] Gmail API ready.")

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            "browser_profile",
            headless=headless,
            slow_mo=150,
            viewport={"width": 1280, "height": 800},
        )
        page = context.pages[0] if context.pages else await context.new_page()

        # ---- Login ----
        await login(page, flipkart_email, gmail_service)
        await save_auth(context)

        # ---- Navigate to orders ----
        print("[nav] Navigating to orders page…")
        await page.goto(FLIPKART_ORDERS, wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle")

        if "login" in page.url.lower():
            print("[auth] Redirected to login; retrying…")
            await login(page, flipkart_email, gmail_service)
            await page.goto(FLIPKART_ORDERS, wait_until="domcontentloaded")
            await page.wait_for_load_state("networkidle")
            if "login" in page.url.lower():
                print("[error] Still on login page after retry. Exiting.")
                await context.close()
                sys.exit(1)

        # ---- Collect order cards ----
        cards = await scroll_until_n_orders(page, num_orders)
        cards = cards[:num_orders]
        actual_count = len(cards)
        print(f"[orders] Processing {actual_count} order(s).")

        if actual_count == 0:
            print("[error] No order cards found.")
            print(f"  URL: {page.url}")
            print(f"  Page snippet:\n{(await page.content())[:800]}")
            await context.close()
            sys.exit(1)

        # ---- Extract products ----
        all_products: list[dict] = []
        for idx, card in enumerate(cards, start=1):
            try:
                products = await expand_and_get_products(page, card, idx, actual_count)
                all_products.extend(products)
            except Exception as exc:
                print(f"[order {idx}/{actual_count}] Error: {exc}")

        await context.close()

    # ---- Aggregate ----
    title_counts = Counter(p["title"] for p in all_products)
    report_products = [
        {
            "title": p["title"],
            "purchase_date": p["date"],
            "purchase_count_in_last_10_orders": title_counts[p["title"]],
        }
        for p in all_products
    ]
    report = {
        "scraped_at": datetime.now(tz=timezone.utc).astimezone().isoformat(),
        "orders_scanned": actual_count,
        "products": report_products,
    }
    ORDERS_REPORT_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\n[done] Report written to {ORDERS_REPORT_FILE}")

    print(f"\n{'#':<4}  {'Product Title':<60}  {'Date':<12}  {'Count'}")
    print("-" * 90)
    for i, p in enumerate(report_products, 1):
        title = p["title"][:58] + ".." if len(p["title"]) > 60 else p["title"]
        print(f"{i:<4}  {title:<60}  {p['purchase_date']:<12}  {p['purchase_count_in_last_10_orders']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Scrape Flipkart order history.")
    ap.add_argument("--orders", type=int, default=10, help="Number of orders to scrape (default: 10)")
    ap.add_argument(
        "--headed",
        type=lambda v: v.lower() not in ("false", "0", "no"),
        default=True,
        help="Run in headed mode (default: true)",
    )
    args = ap.parse_args()
    asyncio.run(run(num_orders=args.orders, headless=not args.headed))


if __name__ == "__main__":
    main()
