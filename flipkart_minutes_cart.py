"""
Add products to the Flipkart Minutes cart by fuzzy-matching free-text names.

Given an array of product names, this module:
  1. Logs in to Flipkart (reusing the Gmail-OTP flow from scrape_flipkart_orders).
  2. For each name, searches Flipkart Minutes, fuzzy-matches the name against the
     search-result titles, then opens the best match's product page and clicks
     its "Add to Cart" button. (Adding happens on the detail page because the
     in-grid Add buttons live inside horizontal carousels whose RN-web scroll
     views swallow taps.)

It NEVER proceeds to checkout, payment, or places an order — matched items are
left in the cart for the user to review and buy manually.

Run directly for local testing (headed):
    python flipkart_minutes_cart.py "Amul Gold Milk" "Aashirvaad Atta 5kg"
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from rapidfuzz import fuzz, utils

from scrape_flipkart_orders import (
    FLIPKART_HOME,
    SELECTORS,
    _UNAVAILABLE_TERMS,
    _clean_minutes_product_title,
    _set_delivery_location_if_prompted,
    get_gmail_service,
    launch_logged_in_context,
)

# Flipkart Minutes search results live behind the global search with the
# HYPERLOCAL marketplace param. Hitting this URL directly is far more reliable
# than driving the search box through the SPA.
MINUTES_SEARCH_URL = (
    "https://www.flipkart.com/search?q={q}&marketplace=HYPERLOCAL"
)

# Force UTF-8 stdout/stderr so unicode (₹, …) prints on Windows consoles.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_MATCH_THRESHOLD = 60.0


def match_threshold() -> float:
    """Minimum fuzzy score (0–100) for a search result to be accepted as a match.
    Reads CART_MATCH_THRESHOLD from the environment; defaults to 60."""
    raw = (os.getenv("CART_MATCH_THRESHOLD") or "").strip()
    if not raw:
        return DEFAULT_MATCH_THRESHOLD
    try:
        return float(raw)
    except ValueError:
        print(f"[config] CART_MATCH_THRESHOLD={raw!r} is not a number; using {DEFAULT_MATCH_THRESHOLD}.")
        return DEFAULT_MATCH_THRESHOLD


# ---------------------------------------------------------------------------
# Fuzzy matching (pure — unit-tested)
# ---------------------------------------------------------------------------

def best_match(
    query: str, candidates: list[str], threshold: float | None = None
) -> tuple[int, float] | None:
    """Return (index, score) of the candidate that best matches `query`, or None
    when no candidate scores at/above `threshold`.

    Uses rapidfuzz's token_set_ratio, which is robust to word reordering and to
    extra brand/size noise common in grocery titles (e.g. matching
    "Amul Milk" against "Amul Gold Full Cream Milk 500 ml")."""
    if threshold is None:
        threshold = match_threshold()

    q = (query or "").strip()
    if not q or not candidates:
        return None

    best_idx = -1
    best_score = -1.0
    for i, cand in enumerate(candidates):
        # default_process lowercases + strips punctuation so matching is
        # case-insensitive and robust to brand/size punctuation noise.
        score = fuzz.token_set_ratio(
            q, (cand or "").strip(), processor=utils.default_process
        )
        if score > best_score:
            best_score = score
            best_idx = i

    if best_idx < 0 or best_score < threshold:
        return None
    return best_idx, best_score


# ---------------------------------------------------------------------------
# Flipkart Minutes navigation + search
# ---------------------------------------------------------------------------

async def _resolve_location(page) -> bool:
    """Best-effort resolution of any Minutes delivery-location gate so search
    results and Add buttons render. Reuses the scraper's saved-address/pincode
    helper. Returns True iff a delivery-location prompt was actually handled
    (callers reload the results grid afterwards, since selecting an address
    re-renders or navigates away from it)."""
    try:
        return await _set_delivery_location_if_prompted(page)
    except Exception as exc:
        print(f"  [location] resolve failed (continuing): {exc}")
        return False


async def _collect_candidates(page) -> list[dict]:
    """Return search-result candidates [{title, href}] for fuzzy matching.

    Each Minutes result is a product anchor (`a[href*="/p/itm"]`) whose visible
    text is the title and whose href is the product page. We add from the detail
    page later, so we keep the href and don't need to locate in-grid Add buttons
    (which live inside un-clickable carousels)."""
    raw = await page.evaluate(
        r"""
        (anchorSel) => {
          const anchors = Array.from(document.querySelectorAll(anchorSel));
          const out = [];
          const seen = new Set();
          for (const a of anchors) {
            const title = (a.innerText || '').replace(/\s+/g, ' ').trim();
            if (!title || seen.has(title.toLowerCase())) continue;
            seen.add(title.toLowerCase());
            out.push({ text: title.slice(0, 200), href: a.href });
          }
          return out;
        }
        """,
        SELECTORS["minutes_product_anchor"],
    )
    candidates: list[dict] = []
    for item in raw:
        title = _clean_minutes_product_title(item["text"]) or item["text"]
        if not title or not item.get("href"):
            continue
        candidates.append({"title": title, "href": item["href"]})
    return candidates


async def _search_minutes(page, query: str) -> list[dict]:
    """Navigate to the Flipkart Minutes results for `query` and return candidate
    products [{title, href}]. Returns [] when no results render."""
    url = MINUTES_SEARCH_URL.format(q=quote_plus(query))
    anchor_sel = SELECTORS["minutes_product_anchor"]

    async def _goto_results() -> bool:
        try:
            await page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            print(f"  [search] navigation failed: {exc}")
            return False
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except PlaywrightTimeoutError:
            pass
        await page.wait_for_timeout(1_500)
        return True

    if not await _goto_results():
        return []

    # Results only render once a delivery location is resolved. Selecting a
    # saved address re-renders (and clicking "Deliver here" reloads) the page,
    # which can strand us off the results grid — so whenever we actually handle
    # a location prompt, reload the search URL to land back on the grid. Repeat
    # once in case the reload re-prompts; a resolved location persists in the
    # context, so this settles quickly.
    for _ in range(2):
        if not await _resolve_location(page):
            break
        if not await _goto_results():
            return []

    # Headless servers (e.g. Railway) render the Minutes grid noticeably slower
    # than a local headed run (which also injects slow_mo between actions), so a
    # fixed short wait races the lazy-loaded results. Wait for the first product
    # anchor to actually attach before collecting; tolerate a timeout, since some
    # queries legitimately have no results.
    try:
        await page.wait_for_selector(anchor_sel, state="attached", timeout=15_000)
    except PlaywrightTimeoutError:
        pass

    return await _collect_candidates(page)


async def _add_candidate_to_cart(page, href: str) -> bool:
    """Open the product's detail page and click its "Add to Cart" button.
    Returns True once the button flips to "Go to cart"/"View cart".

    The detail-page button sits outside Flipkart's horizontal result carousels
    (whose RN-web scroll views swallow taps), so this works for every result —
    not just items that happen to land in the main vertical grid."""
    try:
        await page.goto(href, wait_until="domcontentloaded")
    except Exception as exc:
        print(f"  [add] could not open product page: {exc}")
        return False
    try:
        await page.wait_for_load_state("networkidle", timeout=8_000)
    except PlaywrightTimeoutError:
        pass
    await page.wait_for_timeout(1_000)
    await _resolve_location(page)
    await page.wait_for_timeout(600)

    add_re = SELECTORS["detail_add_to_cart_text"]

    # Tag the visible "Add to Cart" control (a <div>, matched on its own text).
    found = await page.evaluate(
        r"""
        (addRe) => {
          const re = new RegExp(addRe, 'i');
          const els = Array.from(document.querySelectorAll('div,button,span,a')).filter(el => {
            const own = Array.from(el.childNodes).filter(n => n.nodeType === 3)
              .map(n => n.textContent.trim()).join('').trim();
            const r = el.getBoundingClientRect();
            return re.test(own) && r.width > 0 && r.height > 0;
          });
          if (!els.length) return false;
          els[0].setAttribute('data-buy', '1');
          return true;
        }
        """,
        add_re,
    )
    if not found:
        # No "Add to Cart" button. It's either already in the cart (the action
        # bar shows "Go to cart" or a quantity stepper, which varies by product)
        # or genuinely unavailable. Distinguish by out-of-stock markers rather
        # than guessing the in-cart UI.
        low = ((await page.locator("body").inner_text()) or "").lower()
        if any(term in low for term in _UNAVAILABLE_TERMS):
            print("  [add] product unavailable (out of stock)")
            return False
        print("  [add] already in cart")
        return True

    try:
        target = page.locator("[data-buy='1']").first
        await target.scroll_into_view_if_needed()
        await target.click(timeout=5_000)
    except Exception as exc:
        print(f"  [add] click failed: {exc}")
        return False

    await page.wait_for_timeout(2_000)

    # Confirm: a successful add replaces "Add to Cart" with "Go to cart".
    body = (await page.locator("body").inner_text()) or ""
    if re.search(r"go to cart|view cart|added to cart", body, re.I):
        return True
    # Fallback: the tagged "Add to Cart" control is gone or relabelled.
    still = await page.evaluate(
        r"""(addRe) => {
          const re = new RegExp(addRe, 'i');
          const e = document.querySelector("[data-buy='1']");
          if (!e) return false;
          const own = Array.from(e.childNodes).filter(n => n.nodeType === 3)
            .map(n => n.textContent.trim()).join('').trim();
          return re.test(own);
        }""",
        add_re,
    )
    if not still:
        return True
    print("  [add] clicked but no cart confirmation (unconfirmed)")
    return False


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def add_products_to_cart(product_names: list[str], headless: bool) -> dict:
    """Log in, then for each name search Minutes, fuzzy-match, and Add the best
    match to the cart. Returns a results dict; never checks out."""
    load_dotenv()
    flipkart_email = os.getenv("FLIPKART_USERNAME", "")
    if not flipkart_email:
        raise RuntimeError("FLIPKART_USERNAME must be set in .env")

    names = [n.strip() for n in (product_names or []) if isinstance(n, str) and n.strip()]
    if not names:
        return {"requested": 0, "added": 0, "results": []}

    print("[gmail] Authenticating with Gmail API…")
    gmail_service = get_gmail_service(login_hint=flipkart_email)
    print("[gmail] Gmail API ready.")

    threshold = match_threshold()
    results: list[dict] = []

    async with async_playwright() as pw:
        browser, context, page = await launch_logged_in_context(
            pw, headless, flipkart_email, gmail_service
        )

        # Land on Flipkart Minutes once; subsequent searches reuse the page.
        await page.goto(FLIPKART_HOME, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except PlaywrightTimeoutError:
            pass
        await _resolve_location(page)

        for i, name in enumerate(names, 1):
            print(f"\n[cart {i}/{len(names)}] searching Minutes for {name!r}…")
            entry = {
                "input": name,
                "matched_title": None,
                "score": None,
                "status": "no_match",
            }
            try:
                candidates = await _search_minutes(page, name)
                if not candidates:
                    print("  [result] no search results / search unavailable")
                    entry["status"] = "no_match"
                    results.append(entry)
                    continue

                titles = [c["title"] for c in candidates]
                match = best_match(name, titles, threshold)
                if match is None:
                    best_guess = max(
                        (
                            (fuzz.token_set_ratio(name, t, processor=utils.default_process), t)
                            for t in titles
                        ),
                        default=(0, None),
                    )
                    print(
                        f"  [result] best candidate {best_guess[1]!r} scored "
                        f"{best_guess[0]} (< {threshold}); skipping"
                    )
                    entry["status"] = "no_match"
                    results.append(entry)
                    continue

                idx, score = match
                chosen = candidates[idx]
                entry["matched_title"] = chosen["title"]
                entry["score"] = round(score, 1)
                print(f"  [match] {chosen['title']!r} (score {score:.1f})")

                if await _add_candidate_to_cart(page, chosen["href"]):
                    entry["status"] = "added"
                    print("  [result] added to cart")
                else:
                    entry["status"] = "error"
                    print("  [result] match found but Add failed")
            except Exception as exc:
                entry["status"] = "error"
                entry["error"] = str(exc)
                print(f"  [result] error: {exc}")
                try:
                    await page.screenshot(path=str(Path(f"cart_debug_{i}.png")))
                except Exception:
                    pass

            results.append(entry)

        await context.close()

    added = sum(1 for r in results if r["status"] == "added")
    print(f"\n[done] {added}/{len(names)} product(s) added to the Minutes cart.")
    return {"requested": len(names), "added": added, "results": results}


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Fuzzy-match product names and add them to the Flipkart Minutes cart."
    )
    ap.add_argument("products", nargs="+", help="One or more product names to add.")
    ap.add_argument(
        "--headed",
        type=lambda v: v.lower() not in ("false", "0", "no"),
        default=True,
        help="Run in headed mode (default: true).",
    )
    args = ap.parse_args()
    summary = asyncio.run(
        add_products_to_cart(args.products, headless=not args.headed)
    )
    print("\nSummary:")
    for r in summary["results"]:
        print(
            f"  - {r['input']!r}: {r['status']}"
            + (f" → {r['matched_title']!r} ({r['score']})" if r.get("matched_title") else "")
        )


if __name__ == "__main__":
    main()
