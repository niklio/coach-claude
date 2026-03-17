"""TrainingPeaks integration using OAuth 2.0."""

import os
import logging
import urllib.parse

import requests

log = logging.getLogger(__name__)

_AUTH_URL = "https://oauth.trainingpeaks.com/OAuth/Authorize"
_TOKEN_URL = "https://oauth.trainingpeaks.com/OAuth/Token"
_WORKOUTS_URL = "https://tpapi.trainingpeaks.com/fitness/v6/workouts"


class IntegrationNotConfiguredError(Exception):
    """Raised when required environment variables are not set."""


def _get_credentials() -> tuple[str, str]:
    """Return (client_id, client_secret), raising if not configured."""
    client_id = os.getenv("TP_CLIENT_ID")
    client_secret = os.getenv("TP_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise IntegrationNotConfiguredError(
            "TrainingPeaks integration is not configured. "
            "Set TP_CLIENT_ID and TP_CLIENT_SECRET environment variables."
        )
    return client_id, client_secret


def get_auth_url(redirect_uri: str, state: str) -> str:
    """Return the TrainingPeaks OAuth 2.0 authorization URL."""
    client_id, _ = _get_credentials()

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "workouts:read",
        "state": state,
    }
    return f"{_AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(code: str, redirect_uri: str) -> dict:
    """
    Exchange an authorization code for access + refresh tokens.

    Returns a dict with keys: access_token, refresh_token, expires_in, token_type.
    """
    client_id, client_secret = _get_credentials()

    resp = requests.post(
        _TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=15,
    )
    resp.raise_for_status()
    log.info("TrainingPeaks token exchanged successfully")
    return resp.json()


def get_workouts(access_token: str, start_date: str, end_date: str) -> list:
    """
    Fetch workouts for a date range from TrainingPeaks.

    start_date / end_date: ISO 8601 date strings, e.g. "2024-01-01".
    Returns a list of workout dicts.
    """
    _get_credentials()  # raises if not configured

    # TODO: implement full TrainingPeaks workout fetch
    # The exact endpoint and pagination strategy should be confirmed against
    # the TrainingPeaks API docs once developer access is provisioned.
    resp = requests.get(
        _WORKOUTS_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params={"startDate": start_date, "endDate": end_date},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()
