"""
Headless sync script — pulls accounts + transactions from every bank
that can complete its OAuth/consent flow without manual user action,
and upserts the results into the local DB.

Currently runs:
  * Commerzbank — uses the canonical sandbox consent ID.
  * Nordea     — uses the mock-authorizer header so the sandbox auto-
                 approves.

Banks whose flows require a real human (UniCredit SCA, Deutsche Bank
SCA, ING redirect-paste) are NOT touched here — those still need the
web UI.

Why this exists: the web UI fetches per-user, but for cron-style or
ad-hoc syncing during development it's faster to call this from a
shell. Both functions log per-bank summaries; failures are surfaced
in the final summary block and exit 1.

Usage:
    python3 fetch_all.py
"""
import os
import sys
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

load_dotenv()

# Default DB path matches the Flask app — run this script and the web
# UI against the same SQLite file unless you override DATABASE_URL.
os.environ.setdefault("DATABASE_URL", os.getenv("DATABASE_URL", "sqlite:///ais.db"))

# Imports live below env setup so the app picks up DATABASE_URL.
from app import app  # noqa: E402
import commerzbank_client  # noqa: E402
import db_utils  # noqa: E402
import nordea_client  # noqa: E402


def fetch_commerzbank() -> None:
    """Sync Commerzbank using the sandbox's pre-authorised consent.

    Skips silently if the consent isn't valid (typical first-run
    failure: bank credentials misconfigured)."""
    print("\n=== Commerzbank ===")
    token      = commerzbank_client.get_oauth_token()
    consent_id = commerzbank_client.SANDBOX_CONSENT
    status     = commerzbank_client.get_consent_status(token, consent_id)
    print(f"Consent status: {status}")
    if status != "valid":
        print("Skipping — consent not valid.")
        return

    accounts = commerzbank_client.get_accounts(token, consent_id)
    print(f"Accounts: {len(accounts)}")

    with app.app_context():
        db_utils.upsert_accounts("commerzbank", accounts)
        for acc in accounts:
            rid = acc.get("resourceId", "")
            print(f"  Fetching transactions for {acc.get('iban', rid)} ...", end=" ")
            txn_data = commerzbank_client.get_transactions(token, consent_id, rid)
            booked   = len(txn_data.get("booked", []))
            pending  = len(txn_data.get("pending", []))
            print(f"{booked} booked, {pending} pending")
            db_utils.upsert_transactions("commerzbank", rid, txn_data)
    print("Commerzbank done.")


def fetch_nordea() -> None:
    """Sync Nordea using the sandbox mock-authorizer auto-approval.

    The "auto-approval" works because `initiate_authorize` returns a
    302 whose Location already contains `?code=…` — see
    nordea_client. If sandbox config drifts and the code isn't there,
    we bail rather than block on real SCA from a script.
    """
    print("\n=== Nordea ===")
    redirect_uri = os.getenv("NORDEA_REDIRECT_URI", "https://httpbin.org/get")
    location, _state = nordea_client.initiate_authorize(redirect_uri)
    params = parse_qs(urlparse(location).query)
    if "code" not in params:
        print(f"No code in sandbox redirect — location: {location}")
        print("Skipping — manual SCA required.")
        return

    token    = nordea_client.exchange_code(params["code"][0], redirect_uri)
    accounts = nordea_client.get_accounts(token)
    print(f"Accounts: {len(accounts)}")

    with app.app_context():
        db_utils.upsert_accounts("nordea", accounts)
        for acc in accounts:
            rid = acc.get("resourceId", "")
            print(f"  Fetching transactions for {acc.get('iban', rid)} ...", end=" ")
            txn_data = nordea_client.get_transactions(token, rid)
            booked   = len(txn_data.get("booked", []))
            pending  = len(txn_data.get("pending", []))
            print(f"{booked} booked, {pending} pending")
            db_utils.upsert_transactions("nordea", rid, txn_data)
    print("Nordea done.")


if __name__ == "__main__":
    # Each bank runs in its own try/except so a Commerzbank failure
    # doesn't prevent the Nordea sync (and vice versa). Final summary
    # decides the exit code.
    errors: list[tuple[str, Exception]] = []

    try:
        fetch_commerzbank()
    except Exception as e:  # noqa: BLE001 — keep going to the next bank
        print(f"Commerzbank error: {e}", file=sys.stderr)
        errors.append(("commerzbank", e))

    try:
        fetch_nordea()
    except Exception as e:  # noqa: BLE001
        print(f"Nordea error: {e}", file=sys.stderr)
        errors.append(("nordea", e))

    print("\n=== Summary ===")
    if errors:
        for bank, err in errors:
            print(f"  {bank}: FAILED — {err}")
        sys.exit(1)
    else:
        print("All banks fetched successfully.")
