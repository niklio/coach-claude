import base64
import json
import logging
import os
import re
import threading
import traceback
import urllib.parse

import anthropic
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, request, session
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
    except Exception:
        return "Invalid state parameter.", 400

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
        log.info("Authorized athlete %d (%s) with phone %s via %s", athlete_id, name, phone, source)

        if source == "chat":
            session["athlete_id"] = athlete_id
            session["athlete_name"] = name
            public_url = os.getenv("PUBLIC_URL", request.host_url.rstrip("/"))
            return redirect(f"{public_url}/chat")

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

  <!-- Step 1: phone number -->
  <div id="phone-screen" class="signup-screen active">
    <h2>Get started</h2>
    <p>Coach Claude analyses your outdoor rides and texts you your aerodynamic CdA. Enter your phone number to create your account.</p>
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

    // ---- phone step ----
    function normalizePhone(raw) {
      const digits = raw.replace(/\\D/g, '');
      if (digits.length === 10) return '+1' + digits;
      if (digits.length === 11 && digits[0] === '1') return '+' + digits;
      if (digits.length > 7) return '+' + digits;
      return null;
    }

    document.getElementById('phone-next-btn').addEventListener('click', () => {
      const raw = document.getElementById('phone-input').value.trim();
      const phone = normalizePhone(raw);
      const err = document.getElementById('phone-error');
      if (!phone) {
        err.textContent = 'Please enter a valid phone number.';
        return;
      }
      err.textContent = '';
      document.getElementById('strava-link').href = '/chat/auth?phone=' + encodeURIComponent(phone);
      phoneScreen.classList.remove('active');
      stravaScreen.classList.add('active');
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

    // ---- check existing session ----
    fetch('/chat/status')
      .then(r => r.json())
      .then(data => {
        if (data.authenticated) {
          phoneScreen.classList.remove('active');
          chatScreen.style.display = 'flex';
          const firstName = (data.name || '').split(' ')[0] || 'there';
          if (data.needs_weight) {
            addMsg(
              'Hey ' + firstName + '! I\\'m Coach Claude.\\n\\nWhat\\'s your combined rider + bike weight? Reply with a number in kg or lbs (e.g. 75 or 165 lbs).',
              'coach'
            );
          } else {
            addMsg(
              'Hey ' + firstName + '! I\\'m Coach Claude.\\n\\n• "last ride" — get CdA from your most recent ride\\n• "change weight" — update your stored weight',
              'coach'
            );
          }
          input.focus();
        }
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
    public_url = os.getenv("PUBLIC_URL", request.host_url.rstrip("/"))
    state = base64.urlsafe_b64encode(json.dumps({"phone": phone, "source": "chat"}).encode()).decode()
    return redirect(strava_client.get_auth_url(f"{public_url}/callback", state=state))


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
        "name": "get_last_ride_cda",
        "description": (
            "Calculate the CdA (aerodynamic drag coefficient) for the user's most recent "
            "outdoor Strava ride. Returns ride name, CdA in m², sample count, and a Strava link. "
            "Requires the user to have a weight stored."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "set_weight",
        "description": (
            "Store the user's combined rider + bike weight in kg. "
            "Use this whenever the user provides their weight."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "weight_kg": {
                    "type": "number",
                    "description": "Combined rider + bike weight in kilograms.",
                }
            },
            "required": ["weight_kg"],
        },
    },
    {
        "name": "get_weight",
        "description": "Retrieve the user's currently stored weight.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]

_SYSTEM_PROMPT = """\
You are Coach Claude, an AI cycling performance coach. You help cyclists understand and \
improve their aerodynamics by calculating their CdA (coefficient of drag area) from Strava \
ride data.

CdA measures how aerodynamic a rider is — lower is better. Typical values:
• 0.20–0.25 m² — aggressive TT/triathlon position
• 0.28–0.35 m² — road bike, hoods or drops
• 0.35–0.45 m² — upright position

You have tools to look up rides and manage the user's weight profile. Be concise, \
encouraging, and data-driven. When a user asks about their performance or recent ride, \
use tools to fetch real data rather than asking clarifying questions first.

If the user hasn't provided their weight yet, ask for it before attempting a CdA \
calculation — it's required for the physics model.\
"""

_MAX_HISTORY = 20  # max messages to keep in session


def _execute_claude_tool(name: str, tool_input: dict, user: dict) -> str:
    if name == "get_last_ride_cda":
        if user.get("weight_kg") is None:
            return "Cannot calculate CdA: no weight stored. Ask the user for their combined rider + bike weight first."
        return _lookup_last_cda_sync(user)

    if name == "set_weight":
        weight_kg = tool_input.get("weight_kg")
        if not weight_kg or not (30 <= weight_kg <= 250):
            return f"Invalid weight value: {weight_kg}. Must be between 30 and 250 kg."
        weight_kg = round(weight_kg, 1)
        db.set_weight(user["athlete_id"], weight_kg)
        user["weight_kg"] = weight_kg  # update in-place for subsequent tool calls
        return f"Weight stored: {weight_kg} kg."

    if name == "get_weight":
        w = user.get("weight_kg")
        return f"Stored weight: {w} kg." if w else "No weight stored yet."

    return f"Unknown tool: {name}"


def _chat_with_claude(user: dict, text: str, history: list) -> tuple[str, list]:
    """
    Run one user turn through Claude with tool use.
    Returns (reply_text, updated_history).
    history is a list of {"role": "user"|"assistant", "content": str}.
    """
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    # Build message list for this request
    messages = list(history) + [{"role": "user", "content": text}]

    while True:
        with client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=1024,
            system=_SYSTEM_PROMPT,
            tools=_CLAUDE_TOOLS,
            messages=messages,
        ) as stream:
            response = stream.get_final_message()

        if response.stop_reason == "end_turn":
            reply = next((b.text for b in response.content if b.type == "text"), "")
            # Persist only text turns in history
            new_history = (history + [
                {"role": "user", "content": text},
                {"role": "assistant", "content": reply},
            ])[-_MAX_HISTORY:]
            return reply, new_history

        if response.stop_reason == "tool_use":
            # Append assistant turn (with tool_use blocks) to working messages
            messages.append({"role": "assistant", "content": response.content})

            # Execute all tool calls
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = _execute_claude_tool(block.name, block.input, user)
                    log.info("Tool %s → %r", block.name, result[:120])
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

            messages.append({"role": "user", "content": tool_results})
        else:
            # Unexpected stop reason
            break

    return "Something went wrong. Please try again.", history


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
# Health check
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    return jsonify({"status": "ok", "users": db.user_count()}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)
