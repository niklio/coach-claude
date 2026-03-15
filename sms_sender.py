import logging
import os

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

log = logging.getLogger(__name__)


def send_cda_sms(cda: float, n_samples: int, activity_name: str, activity_id: int) -> None:
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_FROM_NUMBER")
    to_number = os.getenv("TWILIO_TO_NUMBER")

    if not all([account_sid, auth_token, from_number, to_number]):
        raise RuntimeError("Twilio credentials not fully configured in .env")

    body = (
        f"New Strava ride: \"{activity_name}\"\n"
        f"Estimated CdA: {cda:.4f} m²\n"
        f"(median of {n_samples} valid samples)\n"
        f"strava.com/activities/{activity_id}"
    )

    try:
        client = Client(account_sid, auth_token)
        message = client.messages.create(body=body, from_=from_number, to=to_number)
        log.info("SMS sent: SID %s", message.sid)
    except TwilioRestException as e:
        log.error("Failed to send SMS: %s", e)
