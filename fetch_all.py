"""Headless script: fetch accounts + transactions from all automatable banks and upsert to DB."""
import os
import sys
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

load_dotenv()

os.environ.setdefault("DATABASE_URL", os.getenv("DATABASE_URL", "sqlite:///ais.db"))

from app import app
import commerzbank_client
import nordea_client
import db_utils


def fetch_commerzbank():
    print("\n=== Commerzbank ===")
    token = commerzbank_client.get_oauth_token()
    consent_id = commerzbank_client.SANDBOX_CONSENT
    status = commerzbank_client.get_consent_status(token, consent_id)
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
            booked = len(txn_data.get("booked", []))
            pending = len(txn_data.get("pending", []))
            print(f"{booked} booked, {pending} pending")
            db_utils.upsert_transactions("commerzbank", rid, txn_data)
    print("Commerzbank done.")


def fetch_nordea():
    print("\n=== Nordea ===")
    redirect_uri = os.getenv("NORDEA_REDIRECT_URI", "https://httpbin.org/get")
    location, _state = nordea_client.initiate_authorize(redirect_uri)
    parsed = urlparse(location)
    params = parse_qs(parsed.query)
    if "code" not in params:
        print(f"No code in sandbox redirect — location: {location}")
        print("Skipping — manual SCA required.")
        return

    token = nordea_client.exchange_code(params["code"][0], redirect_uri)
    accounts = nordea_client.get_accounts(token)
    print(f"Accounts: {len(accounts)}")
    with app.app_context():
        db_utils.upsert_accounts("nordea", accounts)
        for acc in accounts:
            rid = acc.get("resourceId", "")
            print(f"  Fetching transactions for {acc.get('iban', rid)} ...", end=" ")
            txn_data = nordea_client.get_transactions(token, rid)
            booked = len(txn_data.get("booked", []))
            pending = len(txn_data.get("pending", []))
            print(f"{booked} booked, {pending} pending")
            db_utils.upsert_transactions("nordea", rid, txn_data)
    print("Nordea done.")


if __name__ == "__main__":
    errors = []
    try:
        fetch_commerzbank()
    except Exception as e:
        print(f"Commerzbank error: {e}", file=sys.stderr)
        errors.append(("commerzbank", e))

    try:
        fetch_nordea()
    except Exception as e:
        print(f"Nordea error: {e}", file=sys.stderr)
        errors.append(("nordea", e))

    print("\n=== Summary ===")
    if errors:
        for bank, err in errors:
            print(f"  {bank}: FAILED — {err}")
        sys.exit(1)
    else:
        print("All banks fetched successfully.")
