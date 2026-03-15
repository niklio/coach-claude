"""
One-off script to register (or inspect/delete) your Strava webhook subscription.

Run AFTER your app is deployed and publicly reachable:
    python register_webhook.py

Your app must be running at PUBLIC_URL before Strava will confirm the subscription.
"""

import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.getenv("STRAVA_CLIENT_ID")
CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
VERIFY_TOKEN = os.getenv("STRAVA_WEBHOOK_VERIFY_TOKEN")
SUBSCRIPTIONS_URL = "https://www.strava.com/api/v3/push_subscriptions"


def list_subscriptions():
    resp = requests.get(
        SUBSCRIPTIONS_URL,
        params={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        timeout=15,
    )
    print("Current subscriptions:", resp.status_code, resp.json())
    return resp.json()


def create_subscription():
    if not PUBLIC_URL:
        print("ERROR: PUBLIC_URL is not set in .env")
        sys.exit(1)

    callback_url = f"{PUBLIC_URL}/webhook"
    print(f"Registering webhook at: {callback_url}")
    resp = requests.post(
        SUBSCRIPTIONS_URL,
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "callback_url": callback_url,
            "verify_token": VERIFY_TOKEN,
        },
        timeout=15,
    )
    print("Response:", resp.status_code, resp.json())
    if resp.status_code == 201:
        print("\nSuccess! Webhook subscription created.")
    elif resp.status_code == 422:
        print("\nA subscription already exists. Delete it first with --delete <id>")
    else:
        print("\nSomething went wrong. Check the response above.")


def delete_subscription(sub_id: str):
    resp = requests.delete(
        f"{SUBSCRIPTIONS_URL}/{sub_id}",
        params={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        timeout=15,
    )
    print("Delete response:", resp.status_code)
    if resp.status_code == 204:
        print("Subscription deleted.")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--list" in args:
        list_subscriptions()
    elif "--delete" in args:
        idx = args.index("--delete")
        sub_id = args[idx + 1] if idx + 1 < len(args) else None
        if not sub_id:
            print("Usage: python register_webhook.py --delete <subscription_id>")
            sys.exit(1)
        delete_subscription(sub_id)
    else:
        create_subscription()
