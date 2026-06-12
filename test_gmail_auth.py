"""
Test script for Gmail API authentication and OTP email search.
Run this before the main scraper to confirm everything is wired up correctly.

  python test_gmail_auth.py            # auth + inbox check
  python test_gmail_auth.py --otp      # also search for a Flipkart OTP email
"""

import argparse
import os
import re
import sys

from dotenv import load_dotenv

# Reuse helpers from the main script
from scrape_flipkart_orders import (
    GMAIL_TOKEN_FILE,
    get_gmail_service,
    _decode_gmail_body,
)


def test_credentials_present() -> bool:
    print("\n[1/4] Checking .env for GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET…")
    client_id = os.getenv("GMAIL_CLIENT_ID", "").strip()
    client_secret = os.getenv("GMAIL_CLIENT_SECRET", "").strip()

    if not client_id:
        print("  FAIL — GMAIL_CLIENT_ID is missing or empty in .env")
        return False
    if not client_secret:
        print("  FAIL — GMAIL_CLIENT_SECRET is missing or empty in .env")
        return False

    print(f"  OK — Client ID  : {client_id[:20]}…")
    print(f"  OK — Client Secret: {client_secret[:10]}…")
    return True


def test_oauth_flow(email: str) -> object | None:
    print("\n[2/4] Authenticating with Gmail API…")
    if GMAIL_TOKEN_FILE.exists():
        print(f"  Found existing token: {GMAIL_TOKEN_FILE} — will refresh if needed.")
    else:
        print(
            f"  No token.json found.\n"
            f"  A browser will open for one-time OAuth consent.\n"
            f"  Sign in as: {email}"
        )

    try:
        service = get_gmail_service(login_hint=email)
        print("  OK — OAuth successful, Gmail API service created.")
        return service
    except SystemExit:
        print("  FAIL — get_gmail_service() exited (see error above).")
        return None
    except Exception as exc:
        print(f"  FAIL — Unexpected error: {exc}")
        return None


def test_inbox_access(service) -> bool:
    print("\n[3/4] Verifying inbox access (fetching 3 most recent emails)…")
    try:
        result = (
            service.users()
            .messages()
            .list(userId="me", maxResults=3, labelIds=["INBOX"])
            .execute()
        )
        messages = result.get("messages", [])
        if not messages:
            print("  WARN — Inbox appears empty (no messages returned).")
            return True  # Auth works even if inbox is empty

        for stub in messages:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=stub["id"], format="metadata",
                     metadataHeaders=["From", "Subject", "Date"])
                .execute()
            )
            headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
            print(
                f"  • From   : {headers.get('From', '—')}\n"
                f"    Subject: {headers.get('Subject', '—')}\n"
                f"    Date   : {headers.get('Date', '—')}"
            )

        print("  OK — Inbox readable.")
        return True

    except Exception as exc:
        print(f"  FAIL — Could not read inbox: {exc}")
        return False


def test_flipkart_otp_search(service) -> bool:
    print("\n[4/4] Searching for Flipkart OTP emails (last 5)…")
    try:
        result = (
            service.users()
            .messages()
            .list(userId="me", q="from:flipkart OTP", maxResults=5)
            .execute()
        )
        messages = result.get("messages", [])

        if not messages:
            print(
                "  WARN — No Flipkart OTP emails found.\n"
                "         That's OK if you haven't requested an OTP yet.\n"
                "         Trigger a login on Flipkart manually, then re-run with --otp to verify."
            )
            return True

        print(f"  Found {len(messages)} Flipkart OTP email(s). Checking the most recent…")
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=messages[0]["id"], format="full")
            .execute()
        )

        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        print(f"  From   : {headers.get('From', '—')}")
        print(f"  Subject: {headers.get('Subject', '—')}")
        print(f"  Date   : {headers.get('Date', '—')}")

        body = _decode_gmail_body(msg)
        otp_match = re.search(r"\b(\d{6})\b", body)
        if otp_match:
            print(f"  OK — 6-digit OTP found: {otp_match.group(1)}")
        else:
            print(
                "  WARN — Email found but no 6-digit OTP extracted from body.\n"
                "         The OTP may have already expired and been truncated, or the\n"
                "         email format is unexpected. Body preview (first 300 chars):\n"
                f"         {body[:300]!r}"
            )

        return True

    except Exception as exc:
        print(f"  FAIL — Search error: {exc}")
        return False


def main() -> None:
    load_dotenv()

    ap = argparse.ArgumentParser(description="Test Gmail API auth for the Flipkart scraper.")
    ap.add_argument(
        "--otp",
        action="store_true",
        help="Also search Gmail for Flipkart OTP emails (step 4).",
    )
    args = ap.parse_args()

    print("=" * 55)
    print("  Gmail API auth test for Flipkart OTP scraper")
    print("=" * 55)

    passed = 0
    total = 4 if args.otp else 3

    # Step 1
    if not test_credentials_present():
        print(f"\nResult: 0/{total} — fix .env and re-run.\n")
        sys.exit(1)
    passed += 1

    # Step 2
    flipkart_email = os.getenv("FLIPKART_USERNAME", "")
    service = test_oauth_flow(flipkart_email)
    if service is None:
        print(f"\nResult: {passed}/{total} — OAuth failed.\n")
        sys.exit(1)
    passed += 1

    # Step 3
    if not test_inbox_access(service):
        print(f"\nResult: {passed}/{total} — inbox access failed.\n")
        sys.exit(1)
    passed += 1

    # Step 4 (optional)
    if args.otp:
        if not test_flipkart_otp_search(service):
            print(f"\nResult: {passed}/{total} — OTP search failed.\n")
            sys.exit(1)
        passed += 1

    print(f"\n{'=' * 55}")
    print(f"  Result: {passed}/{total} tests passed — Gmail API is ready.")
    if not args.otp:
        print("  Tip: re-run with --otp after triggering a Flipkart login")
        print("       to verify end-to-end OTP extraction.")
    print("=" * 55 + "\n")


if __name__ == "__main__":
    main()
