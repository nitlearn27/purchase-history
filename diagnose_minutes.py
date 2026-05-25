"""
Diagnostic walkthrough for the Flipkart Minutes flow.

Runs the same login + navigation the scraper does, but:
  - Forces headless=False so you can watch
  - slow_mo=1200 ms between actions
  - Logs every URL transition (each time the main frame navigates)
  - Saves a screenshot at every checkpoint into ./minutes_diag/
  - ACTUALLY CLICKS the product tile (not goto), then waits and logs
    everything that happens after
  - Tries the common Flipkart price selectors on the resulting page and
    reports what each finds
  - Pauses at the end so the browser stays open for inspection

Run:
    .venv\\Scripts\\python.exe diagnose_minutes.py

Output:
    minutes_diag/<timestamp>_<step>.png screenshots
    minutes_diag/url_log.txt          full URL transition log
    Console: step-by-step narration
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

load_dotenv()

from scrape_flipkart_orders import (
    AUTH_STATE_FILE,
    FLIPKART_HOME,
    FLIPKART_ORDERS,
    SELECTORS,
    _clean_minutes_product_title,
    get_gmail_service,
    login,
)

DEBUG_DIR = Path("minutes_diag")
DEBUG_DIR.mkdir(exist_ok=True)
URL_LOG = DEBUG_DIR / "url_log.txt"


def _stamp() -> str:
    return datetime.now().strftime("%H%M%S")


def _shot_path(label: str) -> str:
    return str(DEBUG_DIR / f"{_stamp()}_{label}.png")


def _log(line: str) -> None:
    """Print to console AND append to url_log.txt."""
    print(line)
    with URL_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


async def _snap(page, label: str) -> None:
    path = _shot_path(label)
    try:
        await page.screenshot(path=path, full_page=True)
        _log(f"  [shot]    {path}")
    except Exception as exc:
        _log(f"  [shot-fail] {label}: {exc}")


async def _dump_prices(page) -> None:
    """Run every selector we care about and report what each finds."""
    _log("\n  ---- Price-selector probe on current page ----")
    selectors = [
        ("div.Nx9bqj.CxhGGd",            "current selling price (most specific)"),
        ("div.Nx9bqj",                   "Nx9bqj (broader)"),
        ("div._30jeq3",                  "older price class"),
        ("div[class*='price']",          "any class containing 'price'"),
        ("span:has-text('₹')",           "any span containing ₹"),
    ]
    for sel, desc in selectors:
        try:
            loc = page.locator(sel).first
            cnt = await loc.count()
            if cnt == 0:
                _log(f"    {sel:<30} → no match ({desc})")
                continue
            text = (await loc.inner_text(timeout=2_000)).strip()[:80]
            _log(f"    {sel:<30} → {text!r}")
        except Exception as exc:
            _log(f"    {sel:<30} → ERROR: {exc}")

    # Body-text fallback: list every ₹ value on the page
    try:
        body = await page.locator("body").inner_text()
        prices = re.findall(r"₹\s*([\d,]+(?:\.\d+)?)", body)
        _log(f"    body-text ₹ scan          → first 10 values: {prices[:10]}")
    except Exception as exc:
        _log(f"    body-text ₹ scan          → ERROR: {exc}")


async def main() -> None:
    # Clear the URL log at start of run.
    URL_LOG.write_text(f"# Minutes diagnostic — {datetime.now().isoformat()}\n", encoding="utf-8")

    flipkart_email = os.getenv("FLIPKART_USERNAME", "")
    if not flipkart_email:
        print("[error] FLIPKART_USERNAME must be set in .env")
        sys.exit(1)

    _log("[gmail] Authenticating with Gmail API…")
    gmail_service = get_gmail_service(login_hint=flipkart_email)
    _log("[gmail] Ready.\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            slow_mo=1200,                  # human-watchable pace
            args=["--start-maximized"],
        )

        try:
            lat = float(os.getenv("FLIPKART_LAT") or 12.9716)
            lng = float(os.getenv("FLIPKART_LNG") or 77.5946)
        except ValueError:
            lat, lng = 12.9716, 77.5946
        _log(f"[ctx] geolocation set to lat={lat}, lng={lng}")

        storage_state = str(AUTH_STATE_FILE) if AUTH_STATE_FILE.exists() else None
        if storage_state:
            _log(f"[ctx] restoring auth from {AUTH_STATE_FILE}")

        context = await browser.new_context(
            storage_state=storage_state,
            viewport={"width": 1400, "height": 900},
            geolocation={"latitude": lat, "longitude": lng},
            permissions=["geolocation"],
        )
        page = await context.new_page()

        # Subscribe to every main-frame navigation so we see the redirect chain.
        page.on(
            "framenavigated",
            lambda f: _log(f"  [url]     {f.url}") if f is page.main_frame else None,
        )

        _log("\n=== STEP 1: log in / restore session ===")
        await login(page, flipkart_email, gmail_service)
        _log(f"  page.url after login: {page.url}")
        await _snap(page, "01_after_login")

        _log("\n=== STEP 2: navigate to /account/orders ===")
        await page.goto(FLIPKART_ORDERS, wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1_500)
        await _snap(page, "02_orders_page")

        _log("\n=== STEP 3: find the first Flipkart Minutes basket card ===")
        cards = await page.query_selector_all(SELECTORS["order_card"])
        _log(f"  found {len(cards)} order card(s) total")
        minutes_card = None
        for i, c in enumerate(cards):
            txt = (await c.inner_text()) or ""
            if re.search(r"minutes\s*basket", txt, re.I):
                _log(f"  Minutes basket = card index {i}")
                minutes_card = c
                break
        if not minutes_card:
            _log("  [stop] no Minutes basket card on this account — try a different order range.")
            await page.wait_for_timeout(15_000)
            await context.close()
            return
        await _snap(page, "03_minutes_card_visible")

        _log("\n=== STEP 4: click into the Minutes basket card ===")
        await minutes_card.scroll_into_view_if_needed()
        await minutes_card.click()
        try:
            await page.wait_for_url(re.compile(r"order_details.*grocery=true"), timeout=15_000)
            _log(f"  [ok] reached grocery detail: {page.url}")
        except PlaywrightTimeoutError:
            _log(f"  [stop] did not reach grocery detail. Landed: {page.url}")
            await context.close()
            return
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(800)
        await _snap(page, "04_basket_detail")

        _log("\n=== STEP 5: click 'See all items' (if present) ===")
        try:
            see_all = page.get_by_text(re.compile(r"(see|view)\s+all\s+items", re.I)).first
            await see_all.wait_for(state="visible", timeout=8_000)
            await see_all.click()
            await page.wait_for_timeout(1_500)
            await page.wait_for_load_state("networkidle")
            _log("  [ok] 'See all items' clicked")
        except PlaywrightTimeoutError:
            _log("  [note] 'See all items' not visible — using visible items.")
        await _snap(page, "05_all_items_expanded")

        basket_url = page.url
        _log(f"\n  basket_detail_url = {basket_url}")

        _log("\n=== STEP 6: enumerate item tiles (JS evaluate) ===")
        items = await page.evaluate(r"""
            () => {
              const imgs = Array.from(document.querySelectorAll(
                'img[src*="rukmini"]:not([src*="promos"]):not([src*="logos"]):not([src*="banner"])'
              ));
              const out = [];
              for (const img of imgs) {
                let cur = img;
                let href = null;
                for (let i = 0; i < 10 && cur; i++) {
                  if (!href && cur.tagName === 'A' && cur.href) href = cur.href;
                  const t = (cur.innerText || '').replace(/\s+/g,' ').trim();
                  if (t.length > 5 && t.length < 400 && t.includes('₹')) {
                    out.push({ text: t.slice(0, 200), src: img.src, href: href });
                    break;
                  }
                  cur = cur.parentElement;
                }
              }
              return out;
            }
        """)
        _log(f"  found {len(items)} item(s) in the basket")
        for i, it in enumerate(items[:5]):
            title = _clean_minutes_product_title(it["text"])
            _log(f"\n  Item {i+1}: {title!r}")
            _log(f"    raw text  : {it['text'][:120]!r}")
            _log(f"    image src : {it['src'][:90]}")
            _log(f"    anchor    : {it['href']!r}")
        if not items:
            _log("  [stop] no item tiles found.")
            await context.close()
            return

        first = items[0]
        first_title = _clean_minutes_product_title(first["text"])

        _log("\n" + "=" * 70)
        _log(f"=== STEP 7: ACTUALLY CLICK on the first product tile ===")
        _log(f"     product = {first_title}")
        _log("=" * 70)
        url_before = page.url
        _log(f"  url BEFORE click: {url_before}")

        # Try to find the tile as the anchor wrapping the exact image. Falls
        # back to text-based locator if attribute selector misses.
        tile = None
        if first.get("src"):
            try:
                cand = page.locator(f"a:has(img[src=\"{first['src']}\"])").first
                if await cand.count() > 0:
                    tile = cand
                    _log("  tile resolution: a:has(img[src=...])")
            except Exception:
                pass
        if tile is None:
            tile = page.get_by_text(first_title, exact=False).first
            _log("  tile resolution: get_by_text fallback")

        try:
            await tile.scroll_into_view_if_needed()
            await _snap(page, "06_before_click")

            await tile.click()
            _log("  [click] dispatched")

            # Don't impose a wait_for_url — just observe every navigation for 12 s
            # and let the URL log show the chain. Take a screenshot mid-way and
            # at the end.
            await page.wait_for_timeout(4_000)
            _log(f"  url 4s after click : {page.url}")
            await _snap(page, "07_4s_after_click")

            await page.wait_for_timeout(4_000)
            _log(f"  url 8s after click : {page.url}")
            await _snap(page, "08_8s_after_click")

            await page.wait_for_timeout(4_000)
            _log(f"  url 12s after click: {page.url}")
            await _snap(page, "09_12s_after_click")

            await _dump_prices(page)

        except Exception as exc:
            _log(f"  [click FAILED] {exc}")
            await _snap(page, "07_click_failed")

        _log("\n" + "=" * 70)
        _log("=== STEP 8: browser stays open 60s for you to inspect ===")
        _log("=" * 70)
        _log("  Look at: the URL bar, where the click landed, whether a ₹ price is visible.")
        _log(f"  All screenshots saved under: {DEBUG_DIR.resolve()}")
        _log(f"  Full URL log:                {URL_LOG.resolve()}")
        await page.wait_for_timeout(60_000)
        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
