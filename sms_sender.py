import logging
import os

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

log = logging.getLogger(__name__)

# Standard footer appended to the FIRST message sent to a new user.
# Required by A2P 10DLC: opt-out instruction + rates disclosure.
_OPT_IN_FOOTER = "\n\nReply STOP to unsubscribe. Msg & data rates may apply."

# HELP auto-response text (returned by /sms/inbound when user texts HELP).
HELP_RESPONSE = (
    "Coach Claude — cycling CdA analysis via Strava. "
    "Commands: 'last ride' | 'change weight'. "
    "Support: nikliolios@irlll.com. "
    "Reply STOP to unsubscribe."
)

# STOP acknowledgement (Twilio handles STOP natively at the carrier level, but
# we also intercept it in /sms/inbound so we can mark the user opted-out in DB).
STOP_RESPONSE = (
    "You have been unsubscribed from Coach Claude. "
    "No further messages will be sent. "
    "Text START to resubscribe."
)


def _client() -> Client:
    return Client(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))


def _send(to: str, body: str) -> None:
    """Send an SMS via the A2P Messaging Service if configured, else fall back to raw number."""
    kwargs: dict = {"body": body, "to": to}
    messaging_service_sid = os.getenv("TWILIO_MESSAGING_SERVICE_SID")
    if messaging_service_sid:
        kwargs["messaging_service_sid"] = messaging_service_sid
    else:
        kwargs["from_"] = os.getenv("TWILIO_FROM_NUMBER")
    public_url = os.getenv("PUBLIC_URL", "").rstrip("/")
    if public_url:
        kwargs["status_callback"] = f"{public_url}/sms/status"
    msg = _client().messages.create(**kwargs)
    log.info("SMS sent to %s: SID %s", to, msg.sid)


def send_weight_request(to: str) -> None:
    """First outbound message to a new user — must include A2P opt-in footer."""
    _send(
        to,
        "Hey, this is Coach Claude! I'll text you your CdA after every outdoor Strava ride. "
        "First, what's your combined rider + bike weight? "
        "Reply with a number in kg (e.g. 75) or lbs (e.g. 165 lbs)."
        + _OPT_IN_FOOTER,
    )


def send_cda_sms(to: str, cda: float, n_samples: int, activity_name: str, activity_id: int) -> None:
    _send(
        to,
        f"Coach Claude\n"
        f"Ride: \"{activity_name}\"\n"
        f"CdA: {cda:.4f} m²\n"
        f"({n_samples} samples)\n"
        f"strava.com/activities/{activity_id}",
    )


def send_weight_confirmed(to: str, weight_kg: float) -> None:
    _send(to, f"Got it — {weight_kg:.1f} kg stored. Coach Claude will use this for all your CdA calculations. "
              f"Reply 'change weight' any time to update it.")


def send_weight_parse_error(to: str) -> None:
    _send(to, "Coach Claude couldn't parse that. Please reply with just your weight, e.g. '75' or '165 lbs'.")
