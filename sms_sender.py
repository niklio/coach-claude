import logging
import os

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

log = logging.getLogger(__name__)


def _client() -> Client:
    return Client(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))


def _send(to: str, body: str) -> None:
    try:
        msg = _client().messages.create(body=body, from_=os.getenv("TWILIO_FROM_NUMBER"), to=to)
        log.info("SMS sent to %s: SID %s", to, msg.sid)
    except TwilioRestException as e:
        log.error("Failed to send SMS to %s: %s", to, e)


def send_weight_request(to: str) -> None:
    _send(
        to,
        "Hey, this is Coach Claude! I'll text you your CdA after every outdoor Strava ride. "
        "First, what's your combined rider + bike weight? "
        "Reply with a number in kg (e.g. 75) or lbs (e.g. 165 lbs).",
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
