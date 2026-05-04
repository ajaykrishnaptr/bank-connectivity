"""
Tiny Flask glue around the UniCredit consent flow.

The actual HTTP work is done by `psd2_client`. This module just owns
the session-state side: it stashes the consentId on the way out and
re-reads it when the user comes back from the bank's SCA page.
"""
from flask import current_app, session

import psd2_client


def initiate_consent_flow() -> str:
    """Create a fresh AIS consent at UniCredit, stash its ID in the
    session, and return the SCA redirect URL the user should be sent
    to. Raises PSD2ApiError if the bank's response is missing the
    expected `_links.scaRedirect.href`.
    """
    base_url     = current_app.config["SANDBOX_BASE_URL"]
    redirect_uri = current_app.config["REDIRECT_URI"]
    data = psd2_client.create_consent(base_url, redirect_uri)
    session["consent_id"] = data["consentId"]

    sca_url = data.get("_links", {}).get("scaRedirect", {}).get("href", "")
    if not sca_url:
        raise psd2_client.PSD2ApiError("No SCA redirect URL in consent response")
    return sca_url


def check_and_store_consent_status() -> str:
    """Re-fetch the consent we created in `initiate_consent_flow` and
    return its current status string ("valid", "received", "rejected",
    ...). The caller decides whether the status is good enough to
    proceed. Raises PSD2ApiError if there's no consent_id in the
    session — that means the user hit /callback without going through
    /unicredit/connect first.
    """
    base_url   = current_app.config["SANDBOX_BASE_URL"]
    consent_id = session.get("consent_id")
    if not consent_id:
        raise psd2_client.PSD2ApiError("No consent ID in session")
    data   = psd2_client.get_consent_status(base_url, consent_id)
    status = data.get("consentStatus", "unknown")
    session["consent_status"] = status
    return status
