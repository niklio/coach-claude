import os
import time
import logging

import requests

log = logging.getLogger(__name__)

STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API = "https://www.strava.com/api/v3"
STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"


def get_auth_url(redirect_uri: str, state: str = "") -> str:
    params = {
        "client_id": os.getenv("STRAVA_CLIENT_ID"),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": "activity:read_all",
        "state": state,
    }
    query = "&".join(f"{k}={v}" for k, v in params.items() if v)
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
    return resp.json()


def refresh_if_needed(access_token: str, refresh_token: str, expires_at: int) -> tuple[str, str, int]:
    """Returns (access_token, refresh_token, expires_at), refreshing if needed."""
    if expires_at < time.time() + 60:
        log.info("Refreshing expired Strava token...")
        resp = requests.post(
            STRAVA_TOKEN_URL,
            data={
                "client_id": os.getenv("STRAVA_CLIENT_ID"),
                "client_secret": os.getenv("STRAVA_CLIENT_SECRET"),
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["access_token"], data["refresh_token"], data["expires_at"]
    return access_token, refresh_token, expires_at


def get_activity(activity_id: int, access_token: str) -> dict:
    resp = requests.get(
        f"{STRAVA_API}/activities/{activity_id}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


OUTDOOR_RIDE_TYPES = {"Ride", "GravelRide", "MountainBikeRide"}


def get_last_outdoor_ride(access_token: str) -> dict | None:
    """Return the most recent outdoor ride activity, or None if not found."""
    resp = requests.get(
        f"{STRAVA_API}/athlete/activities",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"per_page": 10, "page": 1},
        timeout=15,
    )
    resp.raise_for_status()
    for activity in resp.json():
        if (activity.get("type") in OUTDOOR_RIDE_TYPES or
                activity.get("sport_type") in OUTDOOR_RIDE_TYPES):
            if not activity.get("trainer"):
                return activity
    return None


def get_all_activities(access_token: str, after: int, per_page: int = 200) -> list:
    """Fetch all activities after a unix timestamp, paginating until exhausted.

    Parameters
    ----------
    access_token:
        Valid Strava Bearer token.
    after:
        Unix timestamp.  Only activities that started after this time are returned.
    per_page:
        Page size (Strava max is 200).

    Returns a flat list of activity summary dicts.
    """
    activities = []
    page = 1
    while True:
        resp = requests.get(
            f"{STRAVA_API}/athlete/activities",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"after": after, "per_page": per_page, "page": page},
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        activities.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return activities


def get_activity_streams(activity_id: int, access_token: str) -> dict:
    keys = "time,velocity_smooth,watts,altitude,grade_smooth"
    resp = requests.get(
        f"{STRAVA_API}/activities/{activity_id}/streams",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"keys": keys, "key_by_type": "true", "resolution": "high", "series_type": "time"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()
