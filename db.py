import logging
from google.cloud import firestore

log = logging.getLogger(__name__)

_db = firestore.Client()
_USERS = "users"


def _ref(athlete_id: int):
    return _db.collection(_USERS).document(str(athlete_id))


def upsert_user(
    athlete_id: int,
    phone: str,
    access_token: str,
    refresh_token: str,
    expires_at: int,
    name: str = "",
) -> None:
    doc = _ref(athlete_id)
    existing = doc.get().to_dict() or {}
    data = {
        "athlete_id": athlete_id,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
        # Only set defaults if not already present
        "weight_kg": existing.get("weight_kg", None),
        "awaiting_weight": existing.get("awaiting_weight", False),
    }
    if phone:
        data["phone_number"] = phone
    elif "phone_number" not in existing:
        data["phone_number"] = ""
    if name:
        data["name"] = name
    doc.set(data, merge=True)


def get_user_by_athlete(athlete_id: int) -> dict | None:
    doc = _ref(athlete_id).get()
    return doc.to_dict() if doc.exists else None


def get_user_by_phone(phone: str) -> dict | None:
    docs = _db.collection(_USERS).where("phone_number", "==", phone).limit(1).stream()
    for doc in docs:
        return doc.to_dict()
    return None


def update_tokens(athlete_id: int, access_token: str, refresh_token: str, expires_at: int) -> None:
    _ref(athlete_id).update(
        {"access_token": access_token, "refresh_token": refresh_token, "expires_at": expires_at}
    )


def set_weight(athlete_id: int, weight_kg: float) -> None:
    _ref(athlete_id).update({"weight_kg": weight_kg, "awaiting_weight": False})


def set_awaiting_weight(athlete_id: int, awaiting: bool) -> None:
    _ref(athlete_id).update({"awaiting_weight": awaiting})


def set_sms_opted_out(athlete_id: int, opted_out: bool) -> None:
    """Track whether a user has sent STOP (opted out) or START (resubscribed).

    When opted_out is True the app will not send further outbound SMS to this
    number.  Twilio also enforces a carrier-level block on opted-out numbers.
    """
    _ref(athlete_id).update({"sms_opted_out": opted_out})


def update_integrations(athlete_id: int, integrations: dict) -> None:
    """Merge integration connection state into the user document.

    integrations example: {"garmin": True, "training_peaks": True}
    """
    _ref(athlete_id).set({"integrations": integrations}, merge=True)


def get_user_integrations(athlete_id: int) -> dict:
    """Returns dict of connected integrations.

    e.g. {'garmin': {'oauth_token': '...', 'oauth_token_secret': '...'},
           'trainingpeaks': {'access_token': '...', 'refresh_token': '...'}}
    """
    doc = _ref(athlete_id).get()
    if not doc.exists:
        return {}
    data = doc.to_dict() or {}
    return data.get("integrations", {})


def update_integration(athlete_id: int, integration: str, tokens: dict) -> None:
    """Store tokens for a named integration under integrations.{name} in Firestore."""
    _ref(athlete_id).set({"integrations": {integration: tokens}}, merge=True)


def remove_integration(athlete_id: int, integration: str) -> None:
    """Remove a named integration from the user document."""
    _ref(athlete_id).update({f"integrations.{integration}": firestore.DELETE_FIELD})


def get_sms_history(athlete_id: int) -> list:
    """Return the stored SMS conversation history for Claude context."""
    doc = _ref(athlete_id).get()
    if not doc.exists:
        return []
    return (doc.to_dict() or {}).get("sms_history", [])


def set_sms_history(athlete_id: int, history: list) -> None:
    """Persist SMS conversation history to Firestore."""
    _ref(athlete_id).update({"sms_history": history})


def user_count() -> int:
    return sum(1 for _ in _db.collection(_USERS).list_documents())


def get_all_users() -> list:
    """Return all user documents from Firestore."""
    return [doc.to_dict() for doc in _db.collection(_USERS).stream() if doc.exists]


_PROFILES = "athlete_profiles"


def get_athlete_profile(athlete_id: int) -> dict | None:
    """Return the stored training profile for an athlete, or None if not built yet.

    The profile is written by the profile-building agent and lives in the
    'athlete_profiles' Firestore collection. Fields may include:
      name, primary_sport, consistency, weekly_hours_avg, weekly_rides_avg,
      ftp, sport_mix (dict), longest_ride_km, notes (list), sources (list),
      built_at (ISO timestamp string).
    """
    doc = _db.collection(_PROFILES).document(str(athlete_id)).get()
    return doc.to_dict() if doc.exists else None


def save_athlete_profile(athlete_id: int, profile: dict) -> None:
    """Upsert a training profile document for an athlete."""
    _db.collection(_PROFILES).document(str(athlete_id)).set(profile, merge=True)


def store_athlete_profile(athlete_id: int, profile: dict) -> None:
    """Store the athlete training profile.

    Writes to both the dedicated athlete_profiles collection and as the
    athlete_profile field on the user document so callers that access the
    user doc directly also have the latest profile.
    """
    save_athlete_profile(athlete_id, profile)
    _ref(athlete_id).set({"athlete_profile": profile}, merge=True)
