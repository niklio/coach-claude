"""Garmin Connect integration using OAuth 1.0a."""

import os
import logging

import requests
from requests_oauthlib import OAuth1Session

log = logging.getLogger(__name__)

_REQUEST_TOKEN_URL = "https://connectapi.garmin.com/oauth-service/oauth/request_token"
_AUTHORIZE_URL = "https://connect.garmin.com/oauthConfirm"
_ACCESS_TOKEN_URL = "https://connectapi.garmin.com/oauth-service/oauth/access_token"
_ACTIVITIES_URL = "https://connectapi.garmin.com/activity-service/activity/search/activities"


class IntegrationNotConfiguredError(Exception):
    """Raised when required environment variables are not set."""


def _get_credentials() -> tuple[str, str]:
    """Return (consumer_key, consumer_secret), raising if not configured."""
    key = os.getenv("GARMIN_CONSUMER_KEY")
    secret = os.getenv("GARMIN_CONSUMER_SECRET")
    if not key or not secret:
        raise IntegrationNotConfiguredError(
            "Garmin integration is not configured. "
            "Set GARMIN_CONSUMER_KEY and GARMIN_CONSUMER_SECRET environment variables."
        )
    return key, secret


def get_auth_url(callback_url: str) -> tuple[str, str]:
    """
    Initiate Garmin OAuth 1.0a flow.

    Returns (authorize_url, oauth_token_secret) — the caller must store the
    token_secret in the session so it can be passed back to exchange_token().
    """
    consumer_key, consumer_secret = _get_credentials()

    oauth = OAuth1Session(
        client_key=consumer_key,
        client_secret=consumer_secret,
        callback_uri=callback_url,
    )
    resp = oauth.fetch_request_token(_REQUEST_TOKEN_URL, timeout=15)
    oauth_token = resp["oauth_token"]
    oauth_token_secret = resp["oauth_token_secret"]

    authorize_url = f"{_AUTHORIZE_URL}?oauth_token={oauth_token}"
    log.info("Garmin auth URL generated, oauth_token=%s", oauth_token)
    return authorize_url, oauth_token_secret


def exchange_token(
    oauth_token: str, oauth_verifier: str, token_secret: str
) -> dict:
    """
    Complete Garmin OAuth 1.0a flow.

    Returns a dict with keys: oauth_token, oauth_token_secret.
    """
    consumer_key, consumer_secret = _get_credentials()

    oauth = OAuth1Session(
        client_key=consumer_key,
        client_secret=consumer_secret,
        resource_owner_key=oauth_token,
        resource_owner_secret=token_secret,
        verifier=oauth_verifier,
    )
    resp = oauth.fetch_access_token(_ACCESS_TOKEN_URL, timeout=15)
    log.info("Garmin access token exchanged for oauth_token=%s", oauth_token)
    return {
        "oauth_token": resp["oauth_token"],
        "oauth_token_secret": resp["oauth_token_secret"],
    }


def get_recent_activities(
    access_token: str, access_secret: str, limit: int = 10
) -> list:
    """
    Fetch recent activities from Garmin Connect.

    Returns a list of activity dicts.
    """
    consumer_key, consumer_secret = _get_credentials()

    # TODO: implement full Garmin activity fetch
    # The Garmin Connect API uses a non-standard activity search endpoint.
    # Wire up the authenticated request once API access is confirmed.
    oauth = OAuth1Session(
        client_key=consumer_key,
        client_secret=consumer_secret,
        resource_owner_key=access_token,
        resource_owner_secret=access_secret,
    )
    resp = oauth.get(
        _ACTIVITIES_URL,
        params={"start": 0, "limit": limit},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()
