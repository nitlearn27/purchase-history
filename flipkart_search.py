"""
Search Flipkart Minutes for a product name and return the top matching products
with their current catalog details (price, image, URL, availability).

Given a free-text product name, this module:
  1. Logs in to Flipkart (reusing the Gmail-OTP flow from scrape_flipkart_orders).
  2. Searches Flipkart Minutes, ranks the search-result titles by fuzzy relevance
     to the query, and keeps the top N.
  3. Opens each top result's product page and extracts current_price, image_url,
     product_url and availability.

It is READ-ONLY — it never adds to cart, checks out, or places an order. The
returned per-product shape matches GET /api/products minus the order-history-only
fields (no last_ordered_date / number_of_times_purchased / last_purchased_price /
category).

Run directly for local testing (headed):
    python flipkart_search.py "Amul Gold Milk" --limit=5
    python flipkart_search.py "Tata Salt" --headed=false
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from rapidfuzz import fuzz, utils

from scrape_flipkart_orders import (
    FLIPKART_HOME,
    extract_product_details,
    extract_weight,
    get_gmail_service,
    launch_logged_in_context,
)
from flipkart_minutes_cart import _resolve_location, _search_minutes

# Force UTF-8 stdout/stderr so unicode (₹, …) prints on Windows consoles.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_RESULTS_LIMIT = 5
MAX_RESULTS_LIMIT = 10


def default_results_limit() -> int:
    """Default number of search results to return. Reads SEARCH_RESULTS_LIMIT from
    the environment (.env); falls back to 5 when unset or invalid."""
    raw = (os.getenv("SEARCH_RESULTS_LIMIT") or "").strip()
    if not raw:
        return DEFAULT_RESULTS_LIMIT
    try:
        n = int(raw)
        return n if n > 0 else DEFAULT_RESULTS_LIMIT
    except ValueError:
        print(
            f"[config] SEARCH_RESULTS_LIMIT={raw!r} is not a valid integer; "
            f"using {DEFAULT_RESULTS_LIMIT}."
        )
        return DEFAULT_RESULTS_LIMIT


def clamp_limit(limit: int | None) -> int:
    """Coerce a requested result count into the supported 1..MAX_RESULTS_LIMIT range."""
    if limit is None:
        return default_results_limit()
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return default_results_limit()
    if n < 1:
        return 1
    return min(n, MAX_RESULTS_LIMIT)


# ---------------------------------------------------------------------------
# Candidate ranking (pure — unit-tested)
# ---------------------------------------------------------------------------

def rank_candidates(
    query: str, candidates: list[dict], limit: int
) -> list[dict]:
    """Return up to `limit` candidates sorted by descending fuzzy relevance to
    `query`. Each candidate is a dict with at least a 'title' key.

    Unlike flipkart_minutes_cart.best_match (which gates on a threshold and returns
    one winner), search returns the most relevant results even on a loose match —
    a search box should still surface rows. Uses the same token_set_ratio so word
    reordering and brand/size noise don't hurt ranking."""
    q = (query or "").strip()
    if not q or not candidates:
        return []

    scored = [
        (
            fuzz.token_set_ratio(
                q, (c.get("title") or "").strip(), processor=utils.default_process
            ),
            i,
            c,
        )
        for i, c in enumerate(candidates)
    ]
    # Sort by score desc, keeping original order for ties (stable on the index).
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [c for _, _, c in scored[: max(0, limit)]]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def search_products(query: str, limit: int, headless: bool) -> dict:
    """Log in, search Flipkart Minutes for `query`, then open each of the top
    `limit` results and capture its catalog details. Read-only — never carts or
    checks out. Returns {query, count, scraped_at, products: [...]}."""
    load_dotenv()
    flipkart_email = os.getenv("FLIPKART_USERNAME", "")
    if not flipkart_email:
        raise RuntimeError("FLIPKART_USERNAME must be set in .env")

    query = (query or "").strip()
    limit = clamp_limit(limit)
    scraped_at = datetime.now(tz=timezone.utc).isoformat()
    if not query:
        return {"query": query, "count": 0, "scraped_at": scraped_at, "products": []}

    print("[gmail] Authenticating with Gmail API…")
    gmail_service = get_gmail_service(login_hint=flipkart_email)
    print("[gmail] Gmail API ready.")

    products: list[dict] = []

    async with async_playwright() as pw:
        browser, context, page = await launch_logged_in_context(
            pw, headless, flipkart_email, gmail_service
        )

        # Land on Flipkart once and resolve the delivery location so Minutes
        # results render.
        await page.goto(FLIPKART_HOME, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except PlaywrightTimeoutError:
            pass
        await _resolve_location(page)

        print(f"[search] searching Minutes for {query!r}…")
        candidates = await _search_minutes(page, query)
        ranked = rank_candidates(query, candidates, limit)
        print(f"[search] {len(candidates)} result(s); visiting top {len(ranked)}.")

        for i, cand in enumerate(ranked, 1):
            href = cand.get("href")
            title = cand.get("title")
            print(f"  [{i}/{len(ranked)}] {title!r}")
            entry = {
                "product_name": title,
                "current_price": None,
                "product_url": href,
                "image_url": None,
                "availability": "Unavailable",
                "source": "Flipkart",
                "scraped_at": scraped_at,
                "weight": None,
            }
            try:
                await page.goto(href, wait_until="domcontentloaded")
                details = await extract_product_details(page)
                entry["current_price"] = details.get("current_price")
                entry["product_url"] = details.get("product_url") or href
                entry["image_url"] = details.get("image_url")
                entry["availability"] = details.get("availability", "Unavailable")

                # Extract weight from details or candidate title
                weight = None
                page_title = details.get("page_title")
                if page_title:
                    weight = extract_weight(page_title)
                if not weight or weight == "1 quantity":
                    title_weight = extract_weight(title)
                    if title_weight != "1 quantity" or not weight:
                        weight = title_weight
                entry["weight"] = weight
            except Exception as exc:
                print(f"      [details] failed: {exc}")
                entry["weight"] = extract_weight(title)
            products.append(entry)

        await context.close()

    print(f"\n[done] {len(products)} product(s) for {query!r}.")
    return {
        "query": query,
        "count": len(products),
        "scraped_at": scraped_at,
        "products": products,
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Search Flipkart Minutes and return top product matches with details."
    )
    ap.add_argument("query", help="Product name to search for.")
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help=f"Max results to return (1..{MAX_RESULTS_LIMIT}; default {DEFAULT_RESULTS_LIMIT}).",
    )
    ap.add_argument(
        "--headed",
        type=lambda v: v.lower() not in ("false", "0", "no"),
        default=True,
        help="Run in headed mode (default: true).",
    )
    args = ap.parse_args()
    result = asyncio.run(
        search_products(args.query, limit=args.limit, headless=not args.headed)
    )
    print("\nResults:")
    for p in result["products"]:
        price = f"₹{p['current_price']}" if p["current_price"] is not None else "—"
        weight = p.get("weight") or "—"
        print(f"  - {p['product_name']!r}  {price}  ({weight})  [{p['availability']}]")
        print(f"    {p['product_url']}")


if __name__ == "__main__":
    main()
