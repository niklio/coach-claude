import base64
import json
import logging
import os
import re
import secrets
import threading
import traceback
import urllib.parse

import anthropic
import requests as _requests_lib
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, request, session
from twilio.twiml.messaging_response import MessagingResponse

import cda_calculator
import db
import sms_sender
import strava_client
import integrations.garmin as garmin_integration
import integrations.training_peaks as tp_integration
from integrations.garmin import IntegrationNotConfiguredError as GarminNotConfigured
from integrations.training_peaks import IntegrationNotConfiguredError as TPNotConfigured

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-me")

ADMIN_EMAIL = "nikliolios@irlll.com"
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")


# ---------------------------------------------------------------------------
# Shared message processing logic
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


def _lookup_last_cda_sync(user: dict) -> str:
    """Synchronously compute CdA for user's last outdoor ride. Returns a message string."""
    try:
        access, refresh, expires = strava_client.refresh_if_needed(
            user["access_token"], user["refresh_token"], user["expires_at"]
        )
        if access != user["access_token"]:
            db.update_tokens(user["athlete_id"], access, refresh, expires)

        activity = strava_client.get_last_outdoor_ride(access)
        if not activity:
            return "Couldn't find a recent outdoor ride on your Strava."

        streams = strava_client.get_activity_streams(activity["id"], access)
        crr = float(os.getenv("CRR", "0.004"))
        rho = float(os.getenv("RHO", "1.225"))
        cda, n_samples = cda_calculator.calculate_cda(streams, user["weight_kg"], crr, rho)
        return (
            f"Ride: \"{activity['name']}\"\n"
            f"CdA: {cda:.4f} m²\n"
            f"({n_samples} samples)\n"
            f"strava.com/activities/{activity['id']}"
        )

    except cda_calculator.NoPowerDataError:
        return "Your last ride has no power data — can't calculate CdA."
    except cda_calculator.InsufficientDataError as e:
        return f"Not enough data to calculate CdA: {e}"
    except Exception:
        log.error("Error in _lookup_last_cda_sync for athlete %d:\n%s", user["athlete_id"], traceback.format_exc())
        return "Something went wrong looking up your last ride. Try again."


def _process_message(user: dict, text: str) -> str:
    """Process an inbound message (SMS or chat) and return the reply text."""
    if _wants_last_cda(text):
        if user["weight_kg"] is None:
            return "I don't have your weight yet — send me your combined rider + bike weight in kg or lbs first."
        return _lookup_last_cda_sync(user)

    if _wants_to_change_weight(text):
        db.set_awaiting_weight(user["athlete_id"], True)
        return "Sure! What's your new combined rider + bike weight? Reply with a number in kg or lbs."

    if user["awaiting_weight"]:
        weight = _parse_weight(text)
        if weight is None:
            return "Coach Claude couldn't parse that. Please reply with just your weight, e.g. '75' or '165 lbs'."
        db.set_weight(user["athlete_id"], weight)
        return (
            f"Got it — {weight:.1f} kg stored! I'll use this for all your CdA calculations. "
            f"Reply 'change weight' any time to update it."
        )

    return "Commands:\n• 'last ride' — get CdA from your most recent ride\n• 'change weight' — update your stored weight"


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
# Inbound SMS
# ---------------------------------------------------------------------------

def _twiml(message: str):
    resp = MessagingResponse()
    resp.message(message)
    return str(resp), 200, {"Content-Type": "text/xml"}


def _lookup_last_cda(user: dict) -> None:
    reply = _lookup_last_cda_sync(user)
    sms_sender._send(user["phone_number"], reply)


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

    reply = _process_message(user, body)
    return _twiml(reply)


# ---------------------------------------------------------------------------
# OAuth — SMS flow: /auth?phone=+1XXXXXXXXXX
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
    state = base64.urlsafe_b64encode(json.dumps({"phone": phone, "source": "sms"}).encode()).decode()
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
        source = state_data.get("source", "sms")
        signup_name = state_data.get("name", "")
    except Exception:
        return "Invalid state parameter.", 400

    try:
        tokens = strava_client.exchange_code(code)
        athlete = tokens.get("athlete", {})
        athlete_id = athlete["id"]
        strava_name = f"{athlete.get('firstname', '')} {athlete.get('lastname', '')}".strip()
        # Prefer the name the user typed in the signup form; fall back to Strava name
        display_name = signup_name or strava_name
        db.upsert_user(
            athlete_id,
            phone,
            tokens["access_token"],
            tokens["refresh_token"],
            tokens["expires_at"],
            name=display_name,
        )
        log.info("Authorized athlete %d (%s) with phone %s via %s", athlete_id, display_name, phone, source)

        if source == "chat":
            session["athlete_id"] = athlete_id
            session["athlete_name"] = display_name
            public_url = os.getenv("PUBLIC_URL", request.host_url.rstrip("/"))
            return redirect(f"{public_url}/onboarding/integrations")

        return (
            f"<h2>You're connected to Coach Claude!</h2>"
            f"<p>Strava account: <strong>{display_name}</strong></p>"
            f"<p>Phone: <strong>{phone}</strong></p>"
            f"<p>Upload an outdoor ride and Coach Claude will text you your CdA. You can close this tab.</p>"
        ), 200
    except Exception as e:
        log.error("OAuth exchange failed: %s", e)
        return f"OAuth failed: {e}", 500


# ---------------------------------------------------------------------------
# Garmin OAuth
# ---------------------------------------------------------------------------

_GARMIN_NOT_CONFIGURED_HTML = (
    "<h2>Garmin integration coming soon</h2>"
    "<p>Garmin Connect support is not yet enabled on this server. "
    "Check back later!</p>"
)


@app.route("/garmin/auth")
def garmin_auth():
    athlete_id = session.get("athlete_id")
    if not athlete_id:
        return "Not authenticated — please connect your Strava account first.", 401

    try:
        public_url = os.getenv("PUBLIC_URL", request.host_url.rstrip("/"))
        callback_url = f"{public_url}/garmin/callback"
        authorize_url, token_secret = garmin_integration.get_auth_url(callback_url)
        session["garmin_token_secret"] = token_secret
        return redirect(authorize_url)
    except GarminNotConfigured:
        log.warning("Garmin integration not configured — returning coming-soon page")
        return _GARMIN_NOT_CONFIGURED_HTML, 503


@app.route("/garmin/callback")
def garmin_callback():
    athlete_id = session.get("athlete_id")
    if not athlete_id:
        return "Not authenticated.", 401

    oauth_token = request.args.get("oauth_token", "")
    oauth_verifier = request.args.get("oauth_verifier", "")
    token_secret = session.pop("garmin_token_secret", "")

    if not oauth_token or not oauth_verifier:
        return "Missing OAuth parameters from Garmin.", 400

    try:
        tokens = garmin_integration.exchange_token(oauth_token, oauth_verifier, token_secret)
        db.update_integration(athlete_id, "garmin", tokens)
        log.info("Garmin connected for athlete %d", athlete_id)
        public_url = os.getenv("PUBLIC_URL", request.host_url.rstrip("/"))
        return redirect(f"{public_url}/onboarding/integrations")
    except GarminNotConfigured:
        return _GARMIN_NOT_CONFIGURED_HTML, 503
    except Exception as e:
        log.error("Garmin token exchange failed for athlete %d: %s", athlete_id, e)
        return f"Garmin authorization failed: {e}", 500


# ---------------------------------------------------------------------------
# TrainingPeaks OAuth
# ---------------------------------------------------------------------------

_TP_NOT_CONFIGURED_HTML = (
    "<h2>TrainingPeaks integration coming soon</h2>"
    "<p>TrainingPeaks support is not yet enabled on this server. "
    "Check back later!</p>"
)


@app.route("/tp/auth")
def tp_auth():
    athlete_id = session.get("athlete_id")
    if not athlete_id:
        return "Not authenticated — please connect your Strava account first.", 401

    try:
        public_url = os.getenv("PUBLIC_URL", request.host_url.rstrip("/"))
        callback_url = f"{public_url}/tp/callback"
        state = base64.urlsafe_b64encode(
            json.dumps({"athlete_id": athlete_id}).encode()
        ).decode()
        authorize_url = tp_integration.get_auth_url(callback_url, state=state)
        return redirect(authorize_url)
    except TPNotConfigured:
        log.warning("TrainingPeaks integration not configured — returning coming-soon page")
        return _TP_NOT_CONFIGURED_HTML, 503


@app.route("/tp/callback")
def tp_callback():
    code = request.args.get("code", "")
    error = request.args.get("error", "")
    state = request.args.get("state", "")

    if error or not code:
        return f"TrainingPeaks authorization failed: {error or 'no code'}", 400

    # Recover athlete_id from state (set in /tp/auth) or fall back to session
    try:
        state_data = json.loads(base64.urlsafe_b64decode(state + "=="))
        athlete_id = state_data.get("athlete_id") or session.get("athlete_id")
    except Exception:
        athlete_id = session.get("athlete_id")

    if not athlete_id:
        return "Not authenticated.", 401

    try:
        public_url = os.getenv("PUBLIC_URL", request.host_url.rstrip("/"))
        callback_url = f"{public_url}/tp/callback"
        tokens = tp_integration.exchange_code(code, callback_url)
        db.update_integration(athlete_id, "trainingpeaks", tokens)
        log.info("TrainingPeaks connected for athlete %d", athlete_id)
        return redirect(f"{public_url}/onboarding/integrations")
    except TPNotConfigured:
        return _TP_NOT_CONFIGURED_HTML, 503
    except Exception as e:
        log.error("TrainingPeaks token exchange failed for athlete %d: %s", athlete_id, e)
        return f"TrainingPeaks authorization failed: {e}", 500


# ---------------------------------------------------------------------------
# Integration status
# ---------------------------------------------------------------------------

@app.route("/integrations/status")
def integrations_status():
    athlete_id = session.get("athlete_id")
    if not athlete_id:
        return jsonify({"error": "Not authenticated"}), 401

    integrations = db.get_user_integrations(athlete_id)
    return jsonify({
        "garmin": "garmin" in integrations,
        "trainingpeaks": "trainingpeaks" in integrations,
    })


# ---------------------------------------------------------------------------
# Chat interface — chat.irlll.com served from /chat*
# ---------------------------------------------------------------------------

CHAT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Coach Claude — Chat</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      background: #0a0a0a;
      color: #f0f0f0;
      height: 100dvh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }

    header {
      padding: 1rem 1.5rem;
      border-bottom: 1px solid #1e1e1e;
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }
    header .logo { font-weight: 800; font-size: 1.1rem; color: #fff; }
    header .dot {
      width: 8px; height: 8px; border-radius: 50%;
      background: #4ade80; flex-shrink: 0;
    }

    /* --- signup screens --- */
    .signup-screen {
      flex: 1;
      display: none;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 1.25rem;
      padding: 2rem;
      text-align: center;
    }
    .signup-screen.active { display: flex; }
    .signup-screen h2 { font-size: 1.4rem; font-weight: 700; }
    .signup-screen p { color: #888; max-width: 340px; line-height: 1.6; }

    .field-group {
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
      width: 100%;
      max-width: 300px;
    }
    .field-group label { font-size: 0.85rem; color: #888; text-align: left; }

    .text-input {
      background: #1e1e1e;
      border: 1px solid #2a2a2a;
      border-radius: 8px;
      color: #f0f0f0;
      font-size: 1rem;
      padding: 0.7rem 1rem;
      outline: none;
      width: 100%;
      font-family: inherit;
    }
    .text-input:focus { border-color: #4ade80; }
    .text-input::placeholder { color: #555; }

    .error-msg { font-size: 0.82rem; color: #f87171; min-height: 1em; }

    .primary-btn {
      background: #4ade80;
      color: #0a0a0a;
      border: none;
      border-radius: 8px;
      font-weight: 700;
      font-size: 0.95rem;
      padding: 0.75rem 2rem;
      cursor: pointer;
      transition: opacity 0.15s;
      width: 100%;
      max-width: 300px;
    }
    .primary-btn:hover { opacity: 0.85; }
    .primary-btn:disabled { opacity: 0.4; cursor: default; }

    .strava-btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 0.5rem;
      background: #fc4c02;
      color: #fff;
      font-weight: 600;
      font-size: 0.95rem;
      padding: 0.75rem 1.5rem;
      border-radius: 8px;
      text-decoration: none;
      transition: opacity 0.15s;
      width: 100%;
      max-width: 300px;
    }
    .strava-btn:hover { opacity: 0.85; }

    /* --- chat --- */
    #chat-screen {
      flex: 1;
      display: none;
      flex-direction: column;
      min-height: 0;
    }

    #messages {
      flex: 1;
      overflow-y: auto;
      min-height: 0;
      padding: 1rem 1.5rem;
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
    }

    /* pushes messages to bottom when there are only a few */
    #msg-spacer { flex: 1; }

    .msg {
      max-width: 75%;
      padding: 0.65rem 1rem;
      border-radius: 18px;
      font-size: 0.95rem;
      line-height: 1.5;
      white-space: pre-wrap;
      word-break: break-word;
      flex-shrink: 0;
    }
    .msg.user {
      align-self: flex-end;
      background: #2563eb;
      color: #fff;
      border-bottom-right-radius: 4px;
    }
    .msg.coach {
      align-self: flex-start;
      background: #1e1e1e;
      color: #f0f0f0;
      border-bottom-left-radius: 4px;
    }
    .msg.typing {
      align-self: flex-start;
      background: #1e1e1e;
      color: #666;
      font-style: italic;
      border-bottom-left-radius: 4px;
      flex-shrink: 0;
    }

    #input-row {
      display: flex;
      align-items: flex-end;
      gap: 0.5rem;
      padding: 0.75rem 1rem;
      border-top: 1px solid #1a1a1a;
      flex-shrink: 0;
      background: #0a0a0a;
    }
    #msg-input {
      flex: 1;
      background: #1a1a1a;
      border: 1px solid #2a2a2a;
      border-radius: 20px;
      color: #f0f0f0;
      font-size: 0.95rem;
      padding: 0.6rem 1rem;
      outline: none;
      resize: none;
      height: 40px;
      max-height: 120px;
      overflow-y: auto;
      font-family: inherit;
      line-height: 1.4;
    }
    #msg-input:focus { border-color: #333; }
    #msg-input::placeholder { color: #555; }
    #send-btn {
      background: #2563eb;
      color: #fff;
      border: none;
      border-radius: 50%;
      width: 36px;
      height: 36px;
      font-size: 1rem;
      cursor: pointer;
      transition: opacity 0.15s;
      flex-shrink: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 0;
    }
    #send-btn:hover { opacity: 0.85; }
    #send-btn:disabled { opacity: 0.35; cursor: default; }
  </style>
</head>
<body>
  <header>
    <div class="dot"></div>
    <div class="logo">Coach Claude</div>
  </header>

  <!-- Step 1: name + phone number -->
  <div id="phone-screen" class="signup-screen active">
    <h2>Get started</h2>
    <p>Coach Claude analyses your outdoor rides and texts you your aerodynamic CdA. Enter your details to create your account.</p>
    <div class="field-group">
      <label for="name-input">Your name</label>
      <input id="name-input" class="text-input" type="text" placeholder="Jane Smith" autocomplete="name" />
    </div>
    <div class="field-group">
      <label for="phone-input">Phone number</label>
      <input id="phone-input" class="text-input" type="tel" placeholder="+1 555 000 0000" autocomplete="tel" />
      <div id="phone-error" class="error-msg"></div>
    </div>
    <button id="phone-next-btn" class="primary-btn">Continue</button>
  </div>

  <!-- Step 2: connect Strava -->
  <div id="strava-screen" class="signup-screen">
    <h2>Connect Strava</h2>
    <p>Almost there. Connect your Strava account so Coach Claude can analyse your rides.</p>
    <a id="strava-link" href="#" class="strava-btn">Connect with Strava</a>
  </div>

  <!-- Chat -->
  <div id="chat-screen">
    <div id="messages">
      <div id="msg-spacer"></div>
    </div>
    <div id="input-row">
      <textarea id="msg-input" placeholder="Message Coach Claude…" rows="1"></textarea>
      <button id="send-btn" aria-label="Send">&#x2191;</button>
    </div>
  </div>

  <script>
    const phoneScreen  = document.getElementById('phone-screen');
    const stravaScreen = document.getElementById('strava-screen');
    const chatScreen   = document.getElementById('chat-screen');
    const messages     = document.getElementById('messages');
    const input        = document.getElementById('msg-input');
    const sendBtn      = document.getElementById('send-btn');

    // ---- phone + name step ----
    function normalizePhone(raw) {
      const digits = raw.replace(/\\D/g, '');
      if (digits.length === 10) return '+1' + digits;
      if (digits.length === 11 && digits[0] === '1') return '+' + digits;
      if (digits.length > 7) return '+' + digits;
      return null;
    }

    document.getElementById('phone-next-btn').addEventListener('click', () => {
      const raw = document.getElementById('phone-input').value.trim();
      const name = document.getElementById('name-input').value.trim();
      const phone = normalizePhone(raw);
      const err = document.getElementById('phone-error');
      if (!phone) {
        err.textContent = 'Please enter a valid phone number.';
        return;
      }
      err.textContent = '';
      const params = new URLSearchParams({ phone });
      if (name) params.set('name', name);
      document.getElementById('strava-link').href = '/chat/auth?' + params.toString();
      phoneScreen.classList.remove('active');
      stravaScreen.classList.add('active');
    });

    document.getElementById('name-input').addEventListener('keydown', e => {
      if (e.key === 'Enter') document.getElementById('phone-input').focus();
    });
    document.getElementById('phone-input').addEventListener('keydown', e => {
      if (e.key === 'Enter') document.getElementById('phone-next-btn').click();
    });

    // ---- chat ----
    function addMsg(text, role) {
      const div = document.createElement('div');
      div.className = 'msg ' + role;
      div.textContent = text;
      messages.appendChild(div);
      div.scrollIntoView({ block: 'end' });
      return div;
    }

    async function send() {
      const text = input.value.trim();
      if (!text) return;
      input.value = '';
      input.style.height = '42px';
      sendBtn.disabled = true;

      addMsg(text, 'user');
      const typing = addMsg('Thinking…', 'typing');

      try {
        const resp = await fetch('/chat/message', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text }),
        });
        const data = await resp.json();
        typing.remove();
        addMsg(data.reply, 'coach');
      } catch (e) {
        typing.remove();
        addMsg('Something went wrong. Try again.', 'coach');
      }

      sendBtn.disabled = false;
      input.focus();
    }

    sendBtn.addEventListener('click', send);
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    });
    input.addEventListener('input', () => {
      input.style.height = '42px';
      input.style.height = Math.min(input.scrollHeight, 120) + 'px';
    });

    // ---- check existing session, then fetch personalised greeting ----
    fetch('/chat/status')
      .then(r => r.json())
      .then(data => {
        if (!data.authenticated) return;
        phoneScreen.classList.remove('active');
        chatScreen.style.display = 'flex';
        sendBtn.disabled = true;
        input.disabled = true;
        const loadingMsg = addMsg('Coach Claude is reviewing your data\u2026', 'typing');
        fetch('/chat/init')
          .then(r => r.json())
          .then(initData => {
            loadingMsg.remove();
            addMsg(initData.greeting || 'Hey! I\\'m Coach Claude. How can I help?', 'coach');
            sendBtn.disabled = false;
            input.disabled = false;
            input.focus();
          })
          .catch(() => {
            loadingMsg.remove();
            addMsg('Hey! I\\'m Coach Claude. How can I help?', 'coach');
            sendBtn.disabled = false;
            input.disabled = false;
            input.focus();
          });
      });
  </script>
</body>
</html>
"""


@app.route("/chat")
def chat_ui():
    return CHAT_HTML, 200, {"Content-Type": "text/html"}


@app.route("/chat/auth")
def chat_auth():
    phone = request.args.get("phone", "").strip()
    name = request.args.get("name", "").strip()
    public_url = os.getenv("PUBLIC_URL", request.host_url.rstrip("/"))
    state_payload = {"phone": phone, "source": "chat"}
    if name:
        state_payload["name"] = name
    state = base64.urlsafe_b64encode(json.dumps(state_payload).encode()).decode()
    return redirect(strava_client.get_auth_url(f"{public_url}/callback", state=state))


_INTEGRATIONS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Coach Claude — Connect your data</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      background: #0a0a0a;
      color: #f0f0f0;
      min-height: 100dvh;
      display: flex;
      flex-direction: column;
    }

    header {
      padding: 1rem 1.5rem;
      border-bottom: 1px solid #1e1e1e;
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }
    header .logo { font-weight: 800; font-size: 1.1rem; color: #fff; }
    header .dot {
      width: 8px; height: 8px; border-radius: 50%;
      background: #4ade80; flex-shrink: 0;
    }

    main {
      flex: 1;
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 2.5rem 1.5rem 3rem;
      gap: 1.5rem;
      max-width: 480px;
      margin: 0 auto;
      width: 100%;
    }

    .heading { text-align: center; }
    .heading h2 { font-size: 1.5rem; font-weight: 700; margin-bottom: 0.5rem; }
    .heading p { color: #888; line-height: 1.6; font-size: 0.95rem; }

    .tiles {
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
      width: 100%;
    }

    .tile {
      background: #1a1a1a;
      border: 1px solid #2a2a2a;
      border-radius: 12px;
      padding: 1rem 1.25rem;
      display: flex;
      align-items: center;
      gap: 1rem;
    }

    .tile-icon {
      width: 44px;
      height: 44px;
      border-radius: 10px;
      flex-shrink: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 1.3rem;
      font-weight: 800;
    }
    .tile-icon.garmin  { background: #0066cc; color: #fff; }
    .tile-icon.tp      { background: #e87722; color: #fff; }
    .tile-icon.apple   { background: #2a2a2a; color: #aaa; }

    .tile-info { flex: 1; min-width: 0; }
    .tile-info .tile-name { font-weight: 600; font-size: 0.95rem; margin-bottom: 0.2rem; }
    .tile-info .tile-desc { font-size: 0.8rem; color: #777; line-height: 1.4; }

    .connect-btn {
      background: #4ade80;
      color: #0a0a0a;
      border: none;
      border-radius: 7px;
      font-weight: 700;
      font-size: 0.82rem;
      padding: 0.5rem 1rem;
      cursor: pointer;
      text-decoration: none;
      white-space: nowrap;
      transition: opacity 0.15s;
      flex-shrink: 0;
    }
    .connect-btn:hover { opacity: 0.85; }
    .connect-btn.disabled {
      background: #2a2a2a;
      color: #555;
      cursor: default;
      pointer-events: none;
    }

    .skip-link {
      color: #555;
      font-size: 0.88rem;
      text-decoration: none;
      margin-top: 0.5rem;
      transition: color 0.15s;
    }
    .skip-link:hover { color: #888; }
  </style>
</head>
<body>
  <header>
    <div class="dot"></div>
    <div class="logo">Coach Claude</div>
  </header>

  <main>
    <div class="heading">
      <h2>Connect your data sources</h2>
      <p>Strava is connected. Optionally link additional sources for richer analysis.</p>
    </div>

    <div class="tiles">

      <!-- Garmin -->
      <div class="tile">
        <div class="tile-icon garmin">G</div>
        <div class="tile-info">
          <div class="tile-name">Garmin Connect</div>
          <div class="tile-desc">Sync heart rate, power, and fitness metrics from your Garmin device.</div>
        </div>
        <a href="/garmin/auth" class="connect-btn">Connect</a>
      </div>

      <!-- TrainingPeaks -->
      <div class="tile">
        <div class="tile-icon tp">TP</div>
        <div class="tile-info">
          <div class="tile-name">TrainingPeaks</div>
          <div class="tile-desc">Import your structured training plans and TSS/ATL/CTL data.</div>
        </div>
        <a href="/tp/auth" class="connect-btn">Connect</a>
      </div>

      <!-- Apple Health -->
      <div class="tile">
        <div class="tile-icon apple">&#xf8ff;</div>
        <div class="tile-info">
          <div class="tile-name">Apple Health</div>
          <div class="tile-desc">Available on the iOS app — sync sleep, HRV, and activity data.</div>
        </div>
        <span class="connect-btn disabled">iOS only</span>
      </div>

    </div>

    <a href="/chat" class="skip-link">Skip for now &rarr;</a>
  </main>
</body>
</html>
"""


@app.route("/onboarding/integrations")
def onboarding_integrations():
    return _INTEGRATIONS_HTML, 200, {"Content-Type": "text/html"}


@app.route("/chat/status")
def chat_status():
    athlete_id = session.get("athlete_id")
    if not athlete_id:
        return jsonify({"authenticated": False})
    user = db.get_user_by_athlete(athlete_id)
    if not user:
        return jsonify({"authenticated": False})
    return jsonify({
        "authenticated": True,
        "name": session.get("athlete_name", ""),
        "needs_weight": user.get("weight_kg") is None,
    })


_CLAUDE_TOOLS = [
    {
        "name": "calculate_cda",
        "description": (
            "Calculate the CdA (aerodynamic drag coefficient) for a specific Strava ride or "
            "the user's most recent outdoor ride if no activity_id is given. Returns ride name, "
            "date, CdA in m², sample count, Strava URL, and a brief interpretation benchmarked "
            "against typical values. Requires the user to have a weight stored."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "activity_id": {
                    "type": "integer",
                    "description": (
                        "Strava activity ID to analyse. Omit to use the most recent outdoor ride."
                    ),
                }
            },
            "required": [],
        },
    },
    {
        "name": "get_recent_rides",
        "description": (
            "Fetch the user's recent outdoor rides from Strava. Returns name, date, distance "
            "(km), total elevation gain (m), and average power (watts, if available) for each "
            "ride. Use this to understand training load and to pick specific rides to analyse."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Number of rides to return. Default 5, max 20.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "get_athlete_profile",
        "description": (
            "Fetch the user's profile: full name, stored rider+bike weight, Strava FTP if set, "
            "location, and any connected integrations (Garmin, TrainingPeaks). Use this to "
            "personalise coaching responses and to check whether weight is set."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "set_weight",
        "description": (
            "Store the user's combined rider + bike weight in kg. "
            "Call this whenever the user provides or updates their weight."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "weight_kg": {
                    "type": "number",
                    "description": "Combined rider + bike weight in kilograms (30–250 kg).",
                }
            },
            "required": ["weight_kg"],
        },
    },
    {
        "name": "get_cda_history",
        "description": (
            "Fetch the last N outdoor rides and calculate CdA for each one. Returns a formatted "
            "table with date, CdA value, and sample count per ride, plus a trend summary. Rides "
            "without power data or insufficient samples are skipped and counted separately. Use "
            "this to identify aerodynamic trends and give coaching insights."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Number of rides to analyse. Default 5.",
                }
            },
            "required": [],
        },
    },
]

_SYSTEM_PROMPT = """\
You are Coach Claude, an expert AI cycling performance coach with deep knowledge of \
aerodynamics, training physiology, and power-based training. You help cyclists understand \
and improve their performance by analysing their Strava ride data.

## Your expertise

**Aerodynamics & CdA**
CdA (coefficient of drag area, m²) quantifies aerodynamic drag — it is the product of \
drag coefficient (Cd) and frontal area (A). Lower CdA = less drag = faster at the same power.

Typical CdA values by position:
• 0.20–0.25 m² — aggressive TT / triathlon position (full tuck, aero helmet, skinsuit)
• 0.25–0.28 m² — road race aero position (drops, tight kit, elbows in)
• 0.28–0.32 m² — road bike in the drops, relaxed
• 0.32–0.38 m² — road bike on the hoods
• 0.38–0.50 m² — upright / commuter position

A 0.01 m² CdA reduction saves ~6–8 watts at 40 km/h — meaningful in any race or time trial.

**Power-based training zones** (based on FTP)
• Zone 1 Recovery: <55% FTP
• Zone 2 Endurance: 56–75% FTP
• Zone 3 Tempo: 76–90% FTP
• Zone 4 Threshold: 91–105% FTP
• Zone 5 VO2max: 106–120% FTP
• Zone 6 Anaerobic: >121% FTP

**Key improvement levers**
1. Position changes — lower torso, narrower arms, aero helmet (highest impact)
2. Equipment — aero wheels, skinsuit, aero frame
3. Consistency — CdA varies ride to ride; track trends, not isolated measurements
4. Combined weight — lighter system reduces rolling resistance and gravity penalty

## How you operate

- When a user asks about performance, recent rides, CdA, or training — use your tools \
immediately. Do not ask unnecessary clarifying questions before fetching data.
- Give actionable, specific advice. Avoid generic platitudes like "ride more" or "train hard".
- Benchmark every CdA result against the typical ranges above. Tell the user what their \
number means in plain English.
- When you have multiple rides, spot trends. Is CdA improving? Are there outlier sessions?
- CdA from Strava streams has ~5% uncertainty, so differences <0.005 m² between rides are \
within noise. Focus on trends over 5+ rides.
- High CdA on a climb-heavy ride can reflect sitting up on steep grades — note this.
- If the user has no weight stored, ask politely and explain it is required for the physics \
model. Do not attempt CdA calculations without it.
- When the conversation starts, proactively fetch the athlete profile and recent rides to \
introduce yourself with personalised context.
- Be encouraging but honest. If CdA is high, say so plainly and explain what would help.\
"""

_MAX_HISTORY = 20  # max messages to keep in session


# ---------------------------------------------------------------------------
# Strava helper — paginated recent outdoor rides
# ---------------------------------------------------------------------------

def _get_recent_outdoor_rides(access_token: str, limit: int = 20) -> list:
    """Fetch up to `limit` recent outdoor ride activities from Strava."""
    import requests as _requests
    fetched = []
    page = 1
    while len(fetched) < limit:
        resp = _requests.get(
            f"{strava_client.STRAVA_API}/athlete/activities",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"per_page": min(30, limit * 2), "page": page},
            timeout=15,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        for activity in batch:
            if len(fetched) >= limit:
                break
            is_outdoor_ride = (
                activity.get("type") in strava_client.OUTDOOR_RIDE_TYPES or
                activity.get("sport_type") in strava_client.OUTDOOR_RIDE_TYPES
            )
            if is_outdoor_ride and not activity.get("trainer"):
                fetched.append(activity)
        page += 1
        if len(batch) < 30:
            break
    return fetched


def _interpret_cda(cda: float) -> str:
    """Return a plain-English benchmark for a given CdA value."""
    if cda < 0.25:
        return "Excellent — aggressive TT/triathlon position range (0.20–0.25 m²)"
    if cda < 0.28:
        return "Very good — aero road position range (0.25–0.28 m²)"
    if cda < 0.32:
        return "Good — road bike drops range (0.28–0.32 m²)"
    if cda < 0.38:
        return "Typical — road bike hoods range (0.32–0.38 m²)"
    return "High — upright position (>0.38 m²); meaningful gains possible with position work"


def _refresh_user_tokens(user: dict) -> dict:
    """Refresh Strava tokens if needed, persist if changed, return updated user dict."""
    access, refresh, expires = strava_client.refresh_if_needed(
        user["access_token"], user["refresh_token"], user["expires_at"]
    )
    if access != user["access_token"]:
        db.update_tokens(user["athlete_id"], access, refresh, expires)
        user = dict(user, access_token=access, refresh_token=refresh, expires_at=expires)
    return user


# ---------------------------------------------------------------------------
# Tool execution dispatcher
# ---------------------------------------------------------------------------

def _execute_claude_tool(name: str, tool_input: dict, user: dict) -> str:
    import datetime
    crr = float(os.getenv("CRR", "0.004"))
    rho = float(os.getenv("RHO", "1.225"))

    # ------------------------------------------------------------------ #
    #  calculate_cda                                                        #
    # ------------------------------------------------------------------ #
    if name == "calculate_cda":
        if user.get("weight_kg") is None:
            return (
                "Cannot calculate CdA: no weight stored. "
                "Ask the user for their combined rider + bike weight in kg first."
            )
        try:
            user = _refresh_user_tokens(user)
            access = user["access_token"]

            activity_id = tool_input.get("activity_id")
            if activity_id:
                activity = strava_client.get_activity(int(activity_id), access)
            else:
                activity = strava_client.get_last_outdoor_ride(access)
                if not activity:
                    return "No recent outdoor rides found on your Strava account."

            streams = strava_client.get_activity_streams(activity["id"], access)
            cda, n_samples = cda_calculator.calculate_cda(streams, user["weight_kg"], crr, rho)

            start_raw = activity.get("start_date_local", activity.get("start_date", ""))
            try:
                ride_date = datetime.datetime.fromisoformat(
                    start_raw.replace("Z", "+00:00")
                ).strftime("%d %b %Y")
            except Exception:
                ride_date = start_raw[:10] if start_raw else "unknown date"

            return (
                f"Ride: \"{activity['name']}\"\n"
                f"Date: {ride_date}\n"
                f"CdA: {cda:.4f} m²\n"
                f"Samples: {n_samples}\n"
                f"URL: https://www.strava.com/activities/{activity['id']}\n"
                f"Interpretation: {_interpret_cda(cda)}"
            )
        except cda_calculator.NoPowerDataError:
            return "This ride has no power meter data — CdA cannot be calculated without power."
        except cda_calculator.InsufficientDataError as e:
            return f"Insufficient data to calculate CdA: {e}"
        except Exception:
            log.error("calculate_cda tool error:\n%s", traceback.format_exc())
            return "Error calculating CdA. The ride may be missing required data streams."

    # ------------------------------------------------------------------ #
    #  get_recent_rides                                                     #
    # ------------------------------------------------------------------ #
    if name == "get_recent_rides":
        limit = min(int(tool_input.get("limit", 5)), 20)
        try:
            user = _refresh_user_tokens(user)
            rides = _get_recent_outdoor_rides(user["access_token"], limit=limit)
            if not rides:
                return "No recent outdoor rides found on your Strava account."

            lines = [f"Last {len(rides)} outdoor ride(s):\n"]
            for i, r in enumerate(rides, 1):
                start_raw = r.get("start_date_local", r.get("start_date", ""))
                try:
                    ride_date = datetime.datetime.fromisoformat(
                        start_raw.replace("Z", "+00:00")
                    ).strftime("%d %b %Y")
                except Exception:
                    ride_date = start_raw[:10] if start_raw else "?"

                dist_km = round(r.get("distance", 0) / 1000, 1)
                elev_m = round(r.get("total_elevation_gain", 0))
                avg_power = r.get("average_watts")
                power_str = f", {avg_power:.0f}W avg" if avg_power else ""
                lines.append(
                    f"{i}. {r['name']} — {ride_date}, {dist_km} km, "
                    f"+{elev_m}m{power_str} (ID: {r['id']})"
                )
            return "\n".join(lines)
        except Exception:
            log.error("get_recent_rides tool error:\n%s", traceback.format_exc())
            return "Error fetching recent rides from Strava."

    # ------------------------------------------------------------------ #
    #  get_athlete_profile                                                  #
    # ------------------------------------------------------------------ #
    if name == "get_athlete_profile":
        try:
            import requests as _requests
            user = _refresh_user_tokens(user)
            resp = _requests.get(
                f"{strava_client.STRAVA_API}/athlete",
                headers={"Authorization": f"Bearer {user['access_token']}"},
                timeout=15,
            )
            resp.raise_for_status()
            athlete = resp.json()

            full_name = (
                f"{athlete.get('firstname', '')} {athlete.get('lastname', '')}".strip()
                or user.get("name", "Unknown")
            )
            weight_str = (
                f"{user['weight_kg']:.1f} kg (combined rider + bike)"
                if user.get("weight_kg")
                else "Not set — needed for CdA calculations"
            )
            ftp = athlete.get("ftp")
            ftp_str = f"{ftp} W" if ftp else "Not set on Strava"
            city = athlete.get("city", "")
            country = athlete.get("country", "")
            location_str = ", ".join(filter(None, [city, country])) or "Not provided"

            integrations = user.get("integrations", {})
            int_str = ", ".join(k for k, v in integrations.items() if v) or "None"

            return (
                f"Name: {full_name}\n"
                f"Location: {location_str}\n"
                f"Stored weight: {weight_str}\n"
                f"FTP: {ftp_str}\n"
                f"Connected integrations: {int_str}"
            )
        except Exception:
            log.error("get_athlete_profile tool error:\n%s", traceback.format_exc())
            return "Error fetching athlete profile from Strava."

    # ------------------------------------------------------------------ #
    #  set_weight                                                           #
    # ------------------------------------------------------------------ #
    if name == "set_weight":
        weight_kg = tool_input.get("weight_kg")
        try:
            weight_kg = float(weight_kg)
        except (TypeError, ValueError):
            return f"Invalid weight value: {weight_kg!r}. Must be a number between 30 and 250 kg."
        if not (30 <= weight_kg <= 250):
            return f"Weight {weight_kg} kg is out of range. Must be between 30 and 250 kg."
        weight_kg = round(weight_kg, 1)
        db.set_weight(user["athlete_id"], weight_kg)
        user["weight_kg"] = weight_kg  # update in-place for subsequent tool calls this turn
        return f"Weight stored: {weight_kg} kg."

    # ------------------------------------------------------------------ #
    #  get_cda_history                                                      #
    # ------------------------------------------------------------------ #
    if name == "get_cda_history":
        if user.get("weight_kg") is None:
            return (
                "Cannot calculate CdA history: no weight stored. "
                "Ask the user for their combined rider + bike weight in kg first."
            )
        limit = min(int(tool_input.get("limit", 5)), 20)
        try:
            user = _refresh_user_tokens(user)
            rides = _get_recent_outdoor_rides(user["access_token"], limit=limit)
            if not rides:
                return "No recent outdoor rides found on your Strava account."

            results = []
            skipped = 0
            for r in rides:
                try:
                    streams = strava_client.get_activity_streams(r["id"], user["access_token"])
                    cda, n_samples = cda_calculator.calculate_cda(
                        streams, user["weight_kg"], crr, rho
                    )
                    start_raw = r.get("start_date_local", r.get("start_date", ""))
                    try:
                        ride_date = datetime.datetime.fromisoformat(
                            start_raw.replace("Z", "+00:00")
                        ).strftime("%d %b %Y")
                    except Exception:
                        ride_date = start_raw[:10] if start_raw else "?"
                    results.append({
                        "name": r["name"],
                        "date": ride_date,
                        "cda": cda,
                        "n_samples": n_samples,
                        "id": r["id"],
                    })
                except (cda_calculator.NoPowerDataError, cda_calculator.InsufficientDataError):
                    skipped += 1
                except Exception:
                    log.warning(
                        "CdA history: error on activity %d:\n%s",
                        r["id"], traceback.format_exc(),
                    )
                    skipped += 1

            if not results:
                return (
                    f"Could not calculate CdA for any of the last {len(rides)} ride(s) "
                    f"({skipped} skipped — likely missing power data or insufficient samples)."
                )

            lines = ["CdA history:\n"]
            lines.append(f"{'#':<3} {'Date':<14} {'CdA (m²)':<12} {'Samples':<9} Ride")
            lines.append("-" * 65)
            for i, r in enumerate(results, 1):
                lines.append(
                    f"{i:<3} {r['date']:<14} {r['cda']:.4f}      {r['n_samples']:<9} {r['name']}"
                )
            if skipped:
                lines.append(
                    f"\n({skipped} ride(s) skipped — no power data or insufficient samples)"
                )

            if len(results) >= 2:
                # results are newest-first; trend = newest minus oldest
                cdas = [r["cda"] for r in results]
                trend = cdas[0] - cdas[-1]
                if abs(trend) >= 0.005:
                    direction = "improving (decreasing)" if trend < 0 else "worsening (increasing)"
                    lines.append(
                        f"\nTrend: CdA has been {direction} by "
                        f"{abs(trend):.4f} m² over these {len(results)} rides."
                    )
                else:
                    lines.append(
                        "\nTrend: CdA is stable across these rides "
                        "(variation is within measurement noise)."
                    )

            return "\n".join(lines)
        except Exception:
            log.error("get_cda_history tool error:\n%s", traceback.format_exc())
            return "Error calculating CdA history."

    return f"Unknown tool: {name}"


# ---------------------------------------------------------------------------
# Core Claude agent loop
# ---------------------------------------------------------------------------

def _run_claude_agent(user: dict, messages: list, max_tokens: int = 1024) -> tuple:
    """
    Run the Claude agent loop with tool use until end_turn.
    `messages` is a list of Anthropic-format dicts (modified in-place).
    Returns (reply_text, messages).
    """
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    while True:
        with client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=max_tokens,
            system=_SYSTEM_PROMPT,
            tools=_CLAUDE_TOOLS,
            messages=messages,
        ) as stream:
            response = stream.get_final_message()

        if response.stop_reason == "end_turn":
            reply = next((b.text for b in response.content if b.type == "text"), "")
            messages.append({"role": "assistant", "content": reply})
            return reply, messages

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = _execute_claude_tool(block.name, block.input, user)
                    log.info("Tool %s → %r", block.name, result[:200])
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})
        else:
            break

    return "Something went wrong. Please try again.", messages


def _chat_with_claude(user: dict, text: str, history: list) -> tuple:
    """
    Run one user turn through the Claude agent.
    Returns (reply_text, updated_history).
    history contains simple {"role": ..., "content": str} dicts for session storage.
    """
    messages = list(history) + [{"role": "user", "content": text}]
    reply, _ = _run_claude_agent(user, messages)

    # Persist only simple text turns in session history (keeps session size manageable)
    new_history = (history + [
        {"role": "user", "content": text},
        {"role": "assistant", "content": reply},
    ])[-_MAX_HISTORY:]
    return reply, new_history


@app.route("/chat/init")
def chat_init():
    """
    Generate a personalised opening greeting using Claude.
    Called once per session by the frontend after /chat/status confirms auth.
    The greeting is cached in the session so it is only generated once.
    """
    athlete_id = session.get("athlete_id")
    if not athlete_id:
        return jsonify({"error": "not authenticated"}), 401

    user = db.get_user_by_athlete(athlete_id)
    if not user:
        return jsonify({"error": "user not found"}), 401

    # Return cached greeting if already generated this session
    cached = session.get("chat_greeting")
    if cached:
        return jsonify({"greeting": cached})

    log.info("Generating personalised greeting for athlete %d", athlete_id)
    try:
        # Build a minimal context message that asks Claude to introduce itself
        first_name = (session.get("athlete_name") or user.get("name") or "").split()[0] or "there"
        init_prompt = (
            f"The user {first_name} has just opened the Coach Claude chat. "
            "Fetch their athlete profile and recent rides using your tools, then write a "
            "short, warm, personalised opening message (2–4 sentences). "
            "Mention their name, note something specific from their recent rides if available "
            "(e.g. last ride name or how many rides this week), and offer a concrete next step "
            "like calculating CdA for a specific ride. Be direct and friendly — no generic "
            "platitudes. Do not use markdown headers or bullet lists in this greeting."
        )
        messages = [{"role": "user", "content": init_prompt}]
        greeting, _ = _run_claude_agent(user, messages, max_tokens=512)
        session["chat_greeting"] = greeting
        # Seed chat history with the greeting so context is preserved
        session["chat_history"] = [{"role": "assistant", "content": greeting}]
        return jsonify({"greeting": greeting})
    except Exception:
        log.error(
            "chat_init error for athlete %d:\n%s", athlete_id, traceback.format_exc()
        )
        # Fall back to a simple static greeting
        first_name = (session.get("athlete_name") or "").split()[0] or "there"
        fallback = (
            f"Hey {first_name}! I'm Coach Claude, your cycling performance coach. "
            "Ask me to calculate your CdA, review your recent rides, or analyse your aerodynamics."
        )
        return jsonify({"greeting": fallback})


@app.route("/chat/message", methods=["POST"])
def chat_message():
    athlete_id = session.get("athlete_id")
    if not athlete_id:
        return jsonify({"reply": "Session expired — please reconnect your Strava account."}), 401

    user = db.get_user_by_athlete(athlete_id)
    if not user:
        return jsonify({"reply": "User not found — please reconnect your Strava account."}), 401

    data = request.get_json(force=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"reply": ""}), 400

    log.info("Chat message from athlete %d: %r", athlete_id, text)

    history = session.get("chat_history", [])
    try:
        reply, updated_history = _chat_with_claude(user, text, history)
        session["chat_history"] = updated_history
    except Exception:
        log.error("Claude chat error for athlete %d:\n%s", athlete_id, traceback.format_exc())
        reply = "Something went wrong — please try again."

    return jsonify({"reply": reply})


# ---------------------------------------------------------------------------
# Admin dashboard — Google OAuth protected
# ---------------------------------------------------------------------------

@app.route("/admin")
def admin():
    if session.get("admin_email") != ADMIN_EMAIL:
        public_url = os.getenv("PUBLIC_URL", request.host_url.rstrip("/"))
        state = secrets.token_urlsafe(16)
        session["admin_oauth_state"] = state
        params = urllib.parse.urlencode({
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": f"{public_url}/admin/callback",
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
        })
        return redirect(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")

    users = db.get_all_users()
    users.sort(key=lambda u: u.get("name", "").lower())

    user_cards = ""
    for u in users:
        athlete_id = u.get("athlete_id", "")
        name = u.get("name") or "(no name)"
        phone = u.get("phone_number") or "(no phone)"
        weight = f"{u['weight_kg']} kg" if u.get("weight_kg") else "(not set)"
        awaiting = "yes" if u.get("awaiting_weight") else "no"
        integrations_data = u.get("integrations", {})
        int_list = ", ".join(k for k, v in integrations_data.items() if v) or "none" if integrations_data else "none"

        user_cards += f"""
        <div class="athlete-card">
          <div class="athlete-header">
            <span class="athlete-name">{name}</span>
            <span class="athlete-id">#{athlete_id}</span>
          </div>
          <div class="athlete-fields">
            <div class="field"><span class="label">Phone</span><span class="value">{phone}</span></div>
            <div class="field"><span class="label">Weight</span><span class="value">{weight}</span></div>
            <div class="field"><span class="label">Awaiting weight</span><span class="value">{awaiting}</span></div>
            <div class="field"><span class="label">Integrations</span><span class="value">{int_list}</span></div>
          </div>
        </div>
        """

    total = len(users)
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Coach Claude \u2014 Admin</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      background: #0a0a0a;
      color: #f0f0f0;
      min-height: 100vh;
      padding: 2rem;
    }}
    header {{
      display: flex;
      align-items: center;
      gap: 0.75rem;
      margin-bottom: 2rem;
      padding-bottom: 1rem;
      border-bottom: 1px solid #1e1e1e;
    }}
    header .dot {{ width: 8px; height: 8px; border-radius: 50%; background: #4ade80; flex-shrink: 0; }}
    header h1 {{ font-size: 1.2rem; font-weight: 800; color: #fff; }}
    header .subtitle {{ color: #888; font-size: 0.85rem; margin-left: auto; }}
    .stats {{ margin-bottom: 1.5rem; color: #4ade80; font-size: 0.9rem; font-weight: 600; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
      gap: 1rem;
    }}
    .athlete-card {{
      background: #111;
      border: 1px solid #1e1e1e;
      border-radius: 10px;
      padding: 1.25rem;
    }}
    .athlete-header {{
      display: flex;
      align-items: baseline;
      gap: 0.5rem;
      margin-bottom: 0.75rem;
    }}
    .athlete-name {{ font-weight: 700; font-size: 1rem; color: #fff; }}
    .athlete-id {{ color: #555; font-size: 0.8rem; }}
    .athlete-fields {{ display: flex; flex-direction: column; gap: 0.4rem; }}
    .field {{ display: flex; gap: 0.5rem; font-size: 0.85rem; }}
    .label {{ color: #666; min-width: 130px; flex-shrink: 0; }}
    .value {{ color: #ccc; }}
  </style>
</head>
<body>
  <header>
    <div class="dot"></div>
    <h1>Coach Claude \u2014 Admin</h1>
    <span class="subtitle">Signed in as {session.get("admin_email")}</span>
  </header>
  <div class="stats">{total} registered athlete{"" if total == 1 else "s"}</div>
  <div class="grid">
    {user_cards}
  </div>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html"}


@app.route("/admin/callback")
def admin_callback():
    code = request.args.get("code")
    state = request.args.get("state", "")
    error = request.args.get("error")

    if error or not code:
        return f"Google OAuth failed: {error or 'no code'}", 400

    if state != session.pop("admin_oauth_state", None):
        return "Invalid OAuth state.", 400

    public_url = os.getenv("PUBLIC_URL", request.host_url.rstrip("/"))
    redirect_uri = f"{public_url}/admin/callback"

    token_resp = _requests_lib.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=10,
    )
    if not token_resp.ok:
        log.error("Google token exchange failed: %s", token_resp.text)
        return "Google token exchange failed.", 500

    access_token = token_resp.json().get("access_token")
    if not access_token:
        return "No access token returned by Google.", 500

    userinfo_resp = _requests_lib.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    if not userinfo_resp.ok:
        return "Failed to fetch Google user info.", 500

    email = userinfo_resp.json().get("email", "")
    if email != ADMIN_EMAIL:
        log.warning("Admin login attempt from unauthorized email: %s", email)
        return "Access denied.", 403

    session["admin_email"] = email
    log.info("Admin login: %s", email)
    return redirect(f"{public_url}/admin")


# ---------------------------------------------------------------------------
# Homepage
# ---------------------------------------------------------------------------

@app.route("/")
def homepage():
    with open(os.path.join(os.path.dirname(__file__), "web/public/index.html")) as f:
        return f.read(), 200, {"Content-Type": "text/html"}


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    return jsonify({"status": "ok", "users": db.user_count()}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)
