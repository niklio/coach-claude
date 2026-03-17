"""
athlete_profile.py — Build and store a comprehensive athlete training profile.

Main entry point:
    build_and_store_profile(user: dict) -> dict
"""

import logging
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import requests

import db
import strava_client
import integrations.garmin as garmin_integration
import integrations.training_peaks as tp_integration

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Profile builder
# ---------------------------------------------------------------------------

def build_and_store_profile(user: dict) -> dict:
    """
    Build a comprehensive athlete profile from all connected data sources and
    persist it to Firestore under the user's document.

    Returns the profile dict that was stored.
    """
    athlete_id = user["athlete_id"]
    log.info("Building athlete profile for athlete %d", athlete_id)

    now = datetime.now(timezone.utc)
    window_start = int(now.timestamp()) - 90 * 86400  # 90 days ago (unix)
    window_start_iso = (now - timedelta(days=90)).strftime("%Y-%m-%d")
    window_end_iso = now.strftime("%Y-%m-%d")

    sources: list[str] = []
    notes: list[str] = []

    # ------------------------------------------------------------------ #
    # 1. Strava                                                            #
    # ------------------------------------------------------------------ #
    strava_activities: list[dict] = []
    try:
        access, refresh, expires = strava_client.refresh_if_needed(
            user["access_token"], user["refresh_token"], user["expires_at"]
        )
        if access != user["access_token"]:
            db.update_tokens(athlete_id, access, refresh, expires)
            user = dict(user, access_token=access, refresh_token=refresh, expires_at=expires)

        strava_activities = strava_client.get_all_activities(access, after=window_start)
        log.info(
            "Fetched %d Strava activities (last 90 days) for athlete %d",
            len(strava_activities),
            athlete_id,
        )
        sources.append("strava")
    except Exception:
        log.exception("Failed to fetch Strava activities for athlete %d", athlete_id)

    # ------------------------------------------------------------------ #
    # 2. Garmin                                                            #
    # ------------------------------------------------------------------ #
    garmin_activities: list[dict] = []
    try:
        integrations = db.get_user_integrations(athlete_id)
        garmin_tokens = integrations.get("garmin")
        if garmin_tokens and garmin_tokens.get("oauth_token"):
            garmin_activities = garmin_integration.get_recent_activities(
                garmin_tokens["oauth_token"],
                garmin_tokens["oauth_token_secret"],
                limit=200,
            )
            log.info(
                "Fetched %d Garmin activities for athlete %d",
                len(garmin_activities),
                athlete_id,
            )
            sources.append("garmin")
            notes.append(f"Garmin Connect linked with {len(garmin_activities)} recent activities.")
    except Exception:
        log.warning("Could not fetch Garmin activities for athlete %d", athlete_id, exc_info=True)

    # ------------------------------------------------------------------ #
    # 3. TrainingPeaks                                                     #
    # ------------------------------------------------------------------ #
    tp_workouts: list[dict] = []
    try:
        integrations = db.get_user_integrations(athlete_id)
        tp_tokens = integrations.get("trainingpeaks")
        if tp_tokens and tp_tokens.get("access_token"):
            tp_workouts = tp_integration.get_workouts(
                tp_tokens["access_token"],
                start_date=window_start_iso,
                end_date=window_end_iso,
            )
            log.info(
                "Fetched %d TrainingPeaks workouts for athlete %d",
                len(tp_workouts),
                athlete_id,
            )
            sources.append("trainingpeaks")
            notes.append(
                f"TrainingPeaks connected with {len(tp_workouts)} workouts logged in the last 90 days."
            )
    except Exception:
        log.warning(
            "Could not fetch TrainingPeaks workouts for athlete %d", athlete_id, exc_info=True
        )

    # ------------------------------------------------------------------ #
    # 4. Analyse Strava activities                                         #
    # ------------------------------------------------------------------ #
    sport_mix: dict[str, int] = defaultdict(int)
    total_moving_seconds = 0.0
    total_elevation = 0
    longest_ride_km = 0.0
    power_watts_sum = 0.0
    power_count = 0
    ride_distances_km: list[float] = []
    activity_dates: list[datetime] = []

    for act in strava_activities:
        sport = act.get("sport_type") or act.get("type") or "Unknown"
        sport_mix[sport] += 1
        total_moving_seconds += act.get("moving_time", 0)
        total_elevation += act.get("total_elevation_gain", 0) or 0

        dist_m = act.get("distance", 0) or 0
        dist_km = dist_m / 1000.0

        is_ride = sport in strava_client.OUTDOOR_RIDE_TYPES or sport in {
            "VirtualRide",
            "EBikeRide",
        }
        if is_ride:
            ride_distances_km.append(dist_km)
            if dist_km > longest_ride_km:
                longest_ride_km = dist_km

        avg_w = act.get("average_watts")
        if avg_w and avg_w > 0:
            power_watts_sum += avg_w
            power_count += 1

        start_raw = act.get("start_date_local") or act.get("start_date") or ""
        if start_raw:
            try:
                dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                activity_dates.append(dt)
            except Exception:
                pass

    # Weekly averages
    weeks = 90 / 7  # ~12.86 weeks
    weekly_hours_avg = round((total_moving_seconds / 3600) / weeks, 2)
    weekly_rides_avg = round(len(ride_distances_km) / weeks, 2)
    total_elevation_90d = int(total_elevation)
    avg_power_watts = round(power_watts_sum / power_count, 1) if power_count else None

    # Training consistency
    days_with_activity = len({d.date() for d in activity_dates})
    avg_days_per_week = days_with_activity / weeks
    if avg_days_per_week >= 4:
        training_consistency = "high"
    elif avg_days_per_week >= 2:
        training_consistency = "medium"
    else:
        training_consistency = "low"

    # Primary sport
    primary_sport = max(sport_mix, key=sport_mix.__getitem__) if sport_mix else "Unknown"

    # FTP estimate — from Strava athlete endpoint
    ftp_estimate = None
    if "strava" in sources:
        try:
            access_token = user["access_token"]
            resp = requests.get(
                f"{strava_client.STRAVA_API}/athlete",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=15,
            )
            resp.raise_for_status()
            ftp_estimate = resp.json().get("ftp") or None
        except Exception:
            log.warning("Could not fetch Strava athlete FTP for athlete %d", athlete_id, exc_info=True)

    # ------------------------------------------------------------------ #
    # 5. Generate notes                                                    #
    # ------------------------------------------------------------------ #
    # Weekend long rides
    weekend_rides = [
        act for act in strava_activities
        if (act.get("sport_type") or act.get("type")) in strava_client.OUTDOOR_RIDE_TYPES
        and not act.get("trainer")
        and _is_weekend(act.get("start_date_local") or act.get("start_date") or "")
    ]
    if weekend_rides:
        avg_weekend_km = sum(a.get("distance", 0) for a in weekend_rides) / len(weekend_rides) / 1000
        notes.append(f"Does long rides on weekends (avg {avg_weekend_km:.0f} km).")

    # Power data coverage
    rides_with_power = sum(
        1 for act in strava_activities
        if (act.get("sport_type") or act.get("type")) in strava_client.OUTDOOR_RIDE_TYPES
        and (act.get("average_watts") or 0) > 0
    )
    total_ride_acts = sum(
        1 for act in strava_activities
        if (act.get("sport_type") or act.get("type")) in strava_client.OUTDOOR_RIDE_TYPES
    )
    if total_ride_acts > 0:
        pct_power = int(rides_with_power / total_ride_acts * 100)
        notes.append(f"Power data available on {pct_power}% of rides.")

    if ftp_estimate:
        notes.append(f"Strava FTP set at {ftp_estimate} W.")

    if training_consistency == "high":
        notes.append(f"High training consistency: {avg_days_per_week:.1f} active days/week on average.")
    elif training_consistency == "low":
        notes.append(f"Low training frequency: {avg_days_per_week:.1f} active days/week on average.")

    # Cap notes at 5
    notes = notes[:5]

    # ------------------------------------------------------------------ #
    # 6. Assemble profile                                                  #
    # ------------------------------------------------------------------ #
    profile = {
        "sport_mix": dict(sport_mix),
        "weekly_hours_avg": weekly_hours_avg,
        "weekly_rides_avg": weekly_rides_avg,
        "longest_ride_km": round(longest_ride_km, 1),
        "total_elevation_90d": total_elevation_90d,
        "avg_power_watts": avg_power_watts,
        "ftp_estimate": ftp_estimate,
        "training_consistency": training_consistency,
        "primary_sport": primary_sport,
        "notes": notes,
        "sources": sources,
        "built_at": now.isoformat(),
    }

    db.store_athlete_profile(athlete_id, profile)
    log.info("Athlete profile stored for athlete %d: %s", athlete_id, profile)
    return profile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_weekend(date_str: str) -> bool:
    """Return True if the ISO date string falls on Saturday or Sunday."""
    if not date_str:
        return False
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.weekday() >= 5  # 5 = Saturday, 6 = Sunday
    except Exception:
        return False
