import base64
import json
import logging
import os
import re
import threading
import traceback
import urllib.parse

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, request
from twilio.twiml.messaging_response import MessagingResponse

import cda_calculator
import db
import sms_sender
import strava_client

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-me")



# ---------------------------------------------------------------------------
# Strava webhook
# ---------------------------------------------------------------------------

@app.route("/webhook", methods=["GET"])
def webhook_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == os.getenv("STRAVA_WEBHOOK_VERIFY_TOKEN"):
        return jsonify({"hub.challenge": challenge}), 200
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def webhook_event():
    data = request.get_json(force=True) or {}
    if data.get("object_type") == "activity" and data.get("aspect_type") == "create":
        activity_id = data.get("object_id")
        athlete_id = data.get("owner_id")
        if activity_id and athlete_id:
            t = threading.Thread(
                target=_process_activity, args=(activity_id, athlete_id), daemon=True
            )
            t.start()
    return "OK", 200


def _process_activity(activity_id: int, athlete_id: int) -> None:
    try:
        user = db.get_user_by_athlete(athlete_id)
        if not user:
            log.warning("No user found for athlete_id %d — skipping.", athlete_id)
            return

        # Refresh token if needed and persist
        access, refresh, expires = strava_client.refresh_if_needed(
            user["access_token"], user["refresh_token"], user["expires_at"]
        )
        if access != user["access_token"]:
            db.update_tokens(athlete_id, access, refresh, expires)

        activity = strava_client.get_activity(activity_id, access)

        activity_type = activity.get("type", "")
        sport_type = activity.get("sport_type", "")
        outdoor_types = {"Ride", "GravelRide", "MountainBikeRide"}
        if activity_type not in outdoor_types and sport_type not in outdoor_types:
            log.info("Skipping activity %d — type=%s", activity_id, activity_type)
            return
        if activity.get("trainer"):
            log.info("Skipping activity %d — trainer ride.", activity_id)
            return

        activity_name = activity.get("name", "Unnamed ride")

        # If we don't have this user's weight, ask for it (once)
        if user["weight_kg"] is None:
            if not user["awaiting_weight"]:
                sms_sender.send_weight_request(user["phone_number"])
                db.set_awaiting_weight(athlete_id, True)
                log.info("Asked %s for their weight.", user["phone_number"])
            else:
                log.info("Still waiting for weight from %s, skipping activity.", user["phone_number"])
            return

        streams = strava_client.get_activity_streams(activity_id, access)

        crr = float(os.getenv("CRR", "0.004"))
        rho = float(os.getenv("RHO", "1.225"))

        cda, n_samples = cda_calculator.calculate_cda(streams, user["weight_kg"], crr, rho)
        log.info("CdA for activity %d: %.4f m² (%d samples)", activity_id, cda, n_samples)

        sms_sender.send_cda_sms(user["phone_number"], cda, n_samples, activity_name, activity_id)

    except cda_calculator.NoPowerDataError:
        log.warning("Activity %d has no power data — skipping.", activity_id)
    except cda_calculator.InsufficientDataError as e:
        log.warning("Activity %d — insufficient data: %s", activity_id, e)
    except Exception:
        log.error("Unexpected error processing activity %d:\n%s", activity_id, traceback.format_exc())


# ---------------------------------------------------------------------------
# Inbound SMS — users reply with their weight or "change weight"
# ---------------------------------------------------------------------------

def _parse_weight(text: str) -> float | None:
    """Parse a weight from SMS text. Accepts kg or lbs."""
    match = re.search(r"(\d+\.?\d*)\s*(lbs?|kg)?", text.lower())
    if not match:
        return None
    val = float(match.group(1))
    unit = match.group(2) or "kg"
    if "lb" in unit:
        val = val * 0.453592
    if val < 30 or val > 250:
        return None
    return round(val, 1)


def _wants_to_change_weight(text: str) -> bool:
    text = text.lower()
    return any(phrase in text for phrase in ["change weight", "update weight", "new weight", "reset weight", "change my weight"])


def _wants_last_cda(text: str) -> bool:
    text = text.lower()
    return any(phrase in text for phrase in ["last ride", "last cda", "my cda", "recent ride", "latest ride", "what was my", "what's my cda"])


def _twiml(message: str):
    resp = MessagingResponse()
    resp.message(message)
    return str(resp), 200, {"Content-Type": "text/xml"}


def _lookup_last_cda(user: dict) -> None:
    try:
        access, refresh, expires = strava_client.refresh_if_needed(
            user["access_token"], user["refresh_token"], user["expires_at"]
        )
        if access != user["access_token"]:
            db.update_tokens(user["athlete_id"], access, refresh, expires)

        activity = strava_client.get_last_outdoor_ride(access)
        if not activity:
            sms_sender._send(user["phone_number"], "Couldn't find a recent outdoor ride on your Strava.")
            return

        streams = strava_client.get_activity_streams(activity["id"], access)
        crr = float(os.getenv("CRR", "0.004"))
        rho = float(os.getenv("RHO", "1.225"))
        cda, n_samples = cda_calculator.calculate_cda(streams, user["weight_kg"], crr, rho)
        sms_sender.send_cda_sms(user["phone_number"], cda, n_samples, activity["name"], activity["id"])

    except cda_calculator.NoPowerDataError:
        sms_sender._send(user["phone_number"], "Your last ride has no power data — can't calculate CdA.")
    except cda_calculator.InsufficientDataError as e:
        sms_sender._send(user["phone_number"], f"Not enough data to calculate CdA: {e}")
    except Exception:
        log.error("Error in _lookup_last_cda for athlete %d:\n%s", user["athlete_id"], traceback.format_exc())
        sms_sender._send(user["phone_number"], "Something went wrong looking up your last ride. Try again.")


@app.route("/sms/inbound", methods=["POST"])
def sms_inbound():
    from_number = request.form.get("From", "").strip()
    body = request.form.get("Body", "").strip()
    log.info("Inbound SMS from %s: %r", from_number, body)

    user = db.get_user_by_phone(from_number)
    if not user:
        public_url = os.getenv("PUBLIC_URL", "").rstrip("/")
        encoded_phone = urllib.parse.quote(from_number)
        auth_url = f"{public_url}/auth?phone={encoded_phone}"
        return _twiml(
            f"Hey! This is Coach Claude — I text you your aerodynamic CdA after every outdoor ride.\n\n"
            f"To get started, connect your Strava account:\n{auth_url}"
        )

    if _wants_last_cda(body):
        if user["weight_kg"] is None:
            return _twiml("I don't have your weight yet — reply with your combined rider + bike weight in kg or lbs first.")
        t = threading.Thread(target=_lookup_last_cda, args=(user,), daemon=True)
        t.start()
        return _twiml("Looking up your last ride...")

    if _wants_to_change_weight(body):
        db.set_awaiting_weight(user["athlete_id"], True)
        return _twiml("Sure! What's your new combined rider + bike weight? Reply with a number in kg or lbs.")

    if user["awaiting_weight"]:
        weight = _parse_weight(body)
        if weight is None:
            sms_sender.send_weight_parse_error(from_number)
            return _twiml("")  # already sent the error via send, return empty TwiML
        db.set_weight(user["athlete_id"], weight)
        return _twiml(f"Got it — {weight:.1f} kg stored! I'll use this for all your CdA calculations. "
                      f"Reply 'change weight' any time to update it.")

    return _twiml("Commands:\n• 'last ride' — get CdA from your most recent ride\n• 'change weight' — update your stored weight")


# ---------------------------------------------------------------------------
# OAuth — users visit /auth?phone=+1XXXXXXXXXX to connect their Strava account
# ---------------------------------------------------------------------------

@app.route("/auth")
def auth():
    phone = request.args.get("phone", "").strip()
    if not phone:
        return (
            "<h2>Coach Claude — Connect your Strava account</h2>"
            "<p>Add your phone number to the URL: <code>/auth?phone=+1XXXXXXXXXX</code></p>"
        ), 400
    public_url = os.getenv("PUBLIC_URL", request.host_url.rstrip("/"))
    state = base64.urlsafe_b64encode(json.dumps({"phone": phone}).encode()).decode()
    return redirect(strava_client.get_auth_url(f"{public_url}/callback", state=state))


@app.route("/callback")
def oauth_callback():
    code = request.args.get("code")
    state = request.args.get("state", "")
    error = request.args.get("error")

    if error or not code:
        return f"Authorization failed: {error or 'no code'}", 400

    try:
        state_data = json.loads(base64.urlsafe_b64decode(state + "=="))
        phone = state_data.get("phone", "")
    except Exception:
        return "Invalid state parameter.", 400

    if not phone:
        return "Phone number missing from state.", 400

    try:
        tokens = strava_client.exchange_code(code)
        athlete = tokens.get("athlete", {})
        athlete_id = athlete["id"]
        db.upsert_user(
            athlete_id,
            phone,
            tokens["access_token"],
            tokens["refresh_token"],
            tokens["expires_at"],
        )
        name = f"{athlete.get('firstname', '')} {athlete.get('lastname', '')}".strip()
        log.info("Authorized athlete %d (%s) with phone %s", athlete_id, name, phone)
        return (
            f"<h2>You're connected to Coach Claude!</h2>"
            f"<p>Strava account: <strong>{name}</strong></p>"
            f"<p>Phone: <strong>{phone}</strong></p>"
            f"<p>Upload an outdoor ride and Coach Claude will text you your CdA. You can close this tab.</p>"
        ), 200
    except Exception as e:
        log.error("OAuth exchange failed: %s", e)
        return f"OAuth failed: {e}", 500


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    return jsonify({"status": "ok", "users": db.user_count()}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)
