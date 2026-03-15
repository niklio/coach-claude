import os
import time
import logging

import requests

import token_store

log = logging.getLogger(__name__)

STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API = "https://www.strava.com/api/v3"
STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"


def get_auth_url(redirect_uri: str) -> str:
    params = {
        "client_id": os.getenv("STRAVA_CLIENT_ID"),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": "activity:read_all",
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{STRAVA_AUTH_URL}?{query}"


def exchange_code(code: str) -> dict:
    resp = requests.post(
        STRAVA_TOKEN_URL,
        data={
            "client_id": os.getenv("STRAVA_CLIENT_ID"),
            "client_secret": os.getenv("STRAVA_CLIENT_SECRET"),
            "code": code,
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    resp.raise_for_status()
    tokens = resp.json()
    token_store.save_tokens(tokens)
    return tokens


def _refresh_if_needed(tokens: dict) -> dict:
    if tokens.get("expires_at", 0) < time.time() + 60:
        log.info("Access token expired, refreshing...")
        resp = requests.post(
            STRAVA_TOKEN_URL,
            data={
                "client_id": os.getenv("STRAVA_CLIENT_ID"),
                "client_secret": os.getenv("STRAVA_CLIENT_SECRET"),
                "refresh_token": tokens["refresh_token"],
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        resp.raise_for_status()
        tokens = resp.json()
        token_store.save_tokens(tokens)
        log.info("Token refreshed successfully.")
    return tokens


def _auth_headers() -> dict:
    tokens = token_store.load_tokens()
    if tokens is None:
        raise RuntimeError("No Strava tokens found. Visit /auth to authorize.")
    tokens = _refresh_if_needed(tokens)
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def get_activity(activity_id: int) -> dict:
    resp = requests.get(
        f"{STRAVA_API}/activities/{activity_id}",
        headers=_auth_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_activity_streams(activity_id: int) -> dict:
    """Fetch time, velocity, power, and grade streams for an activity."""
    keys = "time,velocity_smooth,watts,altitude,grade_smooth"
    resp = requests.get(
        f"{STRAVA_API}/activities/{activity_id}/streams",
        headers=_auth_headers(),
        params={"keys": keys, "key_by_type": "true", "resolution": "high", "series_type": "time"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()
