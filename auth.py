import os

from flask import current_app, session

import psd2_client


def initiate_consent_flow():
    base_url = current_app.config["SANDBOX_BASE_URL"]
    redirect_uri = current_app.config["REDIRECT_URI"]
    data = psd2_client.create_consent(base_url, redirect_uri)
    session["consent_id"] = data["consentId"]
    sca_url = data.get("_links", {}).get("scaRedirect", {}).get("href", "")
    if not sca_url:
        raise psd2_client.PSD2ApiError("No SCA redirect URL in consent response")
    return sca_url


def check_and_store_consent_status():
    base_url = current_app.config["SANDBOX_BASE_URL"]
    consent_id = session.get("consent_id")
    if not consent_id:
        raise psd2_client.PSD2ApiError("No consent ID in session")
    data = psd2_client.get_consent_status(base_url, consent_id)
    status = data.get("consentStatus", "unknown")
    session["consent_status"] = status
    return status
