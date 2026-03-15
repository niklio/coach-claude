import logging
import os
import threading
import traceback

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, request

import cda_calculator
import sms_sender
import strava_client
import token_store

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-me")


# ---------------------------------------------------------------------------
# Strava webhook verification (GET) + event handler (POST)
# ---------------------------------------------------------------------------

@app.route("/webhook", methods=["GET"])
def webhook_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == os.getenv("STRAVA_WEBHOOK_VERIFY_TOKEN"):
        log.info("Webhook verified by Strava.")
        return jsonify({"hub.challenge": challenge}), 200
    log.warning("Webhook verification failed: mode=%s token=%s", mode, token)
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def webhook_event():
    data = request.get_json(force=True) or {}
    log.info("Webhook event received: %s", data)

    if data.get("object_type") == "activity" and data.get("aspect_type") == "create":
        activity_id = data.get("object_id")
        if activity_id:
            t = threading.Thread(target=_process_activity, args=(activity_id,), daemon=True)
            t.start()

    # Always return 200 immediately — Strava will retry if we don't
    return "OK", 200


def _process_activity(activity_id: int) -> None:
    try:
        log.info("Fetching activity %d ...", activity_id)
        activity = strava_client.get_activity(activity_id)

        activity_type = activity.get("type", "")
        sport_type = activity.get("sport_type", "")
        is_trainer = activity.get("trainer", False)

        # Only process outdoor (non-trainer) ride types
        outdoor_ride_types = {"Ride", "GravelRide", "MountainBikeRide"}
        if activity_type not in outdoor_ride_types and sport_type not in outdoor_ride_types:
            log.info(
                "Skipping activity %d — type=%s sport_type=%s",
                activity_id, activity_type, sport_type,
            )
            return

        if is_trainer:
            log.info("Skipping activity %d — trainer ride.", activity_id)
            return

        activity_name = activity.get("name", "Unnamed ride")
        log.info("Processing outdoor ride: '%s' (ID %d)", activity_name, activity_id)

        streams = strava_client.get_activity_streams(activity_id)

        mass_kg = float(os.getenv("RIDER_MASS_KG", "75.0"))
        crr = float(os.getenv("CRR", "0.004"))
        rho = float(os.getenv("RHO", "1.225"))

        cda, n_samples = cda_calculator.calculate_cda(streams, mass_kg, crr, rho)
        log.info("CdA for activity %d: %.4f m² (%d samples)", activity_id, cda, n_samples)

        sms_sender.send_cda_sms(cda, n_samples, activity_name, activity_id)

    except cda_calculator.NoPowerDataError:
        log.warning("Activity %d has no power data — skipping CdA calculation.", activity_id)
    except cda_calculator.InsufficientDataError as e:
        log.warning("Activity %d — insufficient data: %s", activity_id, e)
    except Exception:
        log.error("Unexpected error processing activity %d:\n%s", activity_id, traceback.format_exc())


# ---------------------------------------------------------------------------
# OAuth routes — visit /auth once in your browser to connect your Strava account
# ---------------------------------------------------------------------------

@app.route("/auth")
def auth():
    public_url = os.getenv("PUBLIC_URL", request.host_url.rstrip("/"))
    redirect_uri = f"{public_url}/callback"
    return redirect(strava_client.get_auth_url(redirect_uri))


@app.route("/callback")
def oauth_callback():
    code = request.args.get("code")
    error = request.args.get("error")
    if error or not code:
        return f"Authorization failed: {error or 'no code'}", 400
    try:
        tokens = strava_client.exchange_code(code)
        athlete = tokens.get("athlete", {})
        name = athlete.get("firstname", "")
        log.info("Authorized Strava account for %s %s", name, athlete.get("lastname", ""))
        return (
            f"<h2>Authorized!</h2>"
            f"<p>Connected as <strong>{name} {athlete.get('lastname','')}</strong>.</p>"
            f"<p>You can close this tab. Strava activities will now trigger CdA texts.</p>"
        ), 200
    except Exception as e:
        log.error("OAuth exchange failed: %s", e)
        return f"OAuth exchange failed: {e}", 500


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    tokens = token_store.load_tokens()
    authorized = tokens is not None
    return jsonify({"status": "ok", "strava_authorized": authorized}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)
