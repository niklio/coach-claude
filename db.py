import logging
from google.cloud import firestore

log = logging.getLogger(__name__)

_db = firestore.Client()
_USERS = "users"


def _ref(athlete_id: int):
    return _db.collection(_USERS).document(str(athlete_id))


def upsert_user(athlete_id: int, phone: str, access_token: str, refresh_token: str, expires_at: int) -> None:
    _ref(athlete_id).set(
        {
            "athlete_id": athlete_id,
            "phone_number": phone,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
        },
        merge=True,
    )


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


def user_count() -> int:
    return len(_db.collection(_USERS).list_documents())
