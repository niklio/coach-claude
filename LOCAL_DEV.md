# Local Development Guide

This guide covers everything you need to run the Coach Claude Flask app locally with hot reloading.

---

## Prerequisites

- **Python 3.12** — matches the production Docker image. Use [pyenv](https://github.com/pyenv/pyenv) to manage versions: `pyenv install 3.12` then `pyenv local 3.12`
- **pip** — comes with Python; upgrade it: `pip install --upgrade pip`
- **Google Cloud SDK** — required for Firestore access via Application Default Credentials: [install gcloud](https://cloud.google.com/sdk/docs/install)
- **ngrok** (optional) — only needed if you want to test the Strava webhook locally

---

## Setup

### 1. Clone and create a virtual environment

```bash
git clone <repo-url>
cd strava-cda
python3.12 -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment variables

```bash
cp .env.example .env
```

Open `.env` and fill in the values. See [Environment Variables](#environment-variables) below for what is required vs optional.

### 4. Authenticate with Google Cloud (for Firestore)

The app uses Google Cloud Firestore via Application Default Credentials (ADC). On Cloud Run this is automatic; locally you must authenticate manually.

```bash
gcloud auth application-default login
```

This opens a browser and saves credentials at `~/.config/gcloud/application_default_credentials.json`. The `google-cloud-firestore` client picks them up automatically — no extra env var needed.

If you'd rather use a service account key file instead, set:
```
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account-key.json
```
(This line is already commented out in `.env.example`.)

---

## Running the app

```bash
flask --app app run --reload --debug --port 8000
```

Or use the Makefile shortcut:

```bash
make dev
```

The app starts at `http://localhost:8000`. Flask's `--reload` flag watches source files and restarts automatically on changes. The `--debug` flag enables the interactive debugger and verbose logging.

---

## Environment Variables

### Required for core functionality

| Variable | Description |
|---|---|
| `SECRET_KEY` | Flask session secret. Any random string works locally. |
| `STRAVA_CLIENT_ID` | From [strava.com/settings/api](https://www.strava.com/settings/api) |
| `STRAVA_CLIENT_SECRET` | From the same Strava API settings page |
| `STRAVA_WEBHOOK_VERIFY_TOKEN` | Any string you choose; must match what you register with Strava |

### Required for Firestore (all user data)

Firestore is used for all user storage. Without it, the app will crash on startup because `db.py` calls `firestore.Client()` at import time. You need ADC set up (see above) or `GOOGLE_APPLICATION_CREDENTIALS` pointing to a service account key.

### Required for SMS features

| Variable | Description |
|---|---|
| `TWILIO_ACCOUNT_SID` | From [console.twilio.com](https://console.twilio.com) |
| `TWILIO_AUTH_TOKEN` | From Twilio console |
| `TWILIO_FROM_NUMBER` | The Twilio phone number (E.164 format, e.g. `+15551234567`) |

Without these, the `/sms/inbound` route and outbound SMS after activity processing will fail at send time, but the app will start.

### Required for the AI chat (`/chat` routes)

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | From [console.anthropic.com](https://console.anthropic.com) |

Without this, the `/chat` endpoint will raise an error when a message is sent, but the app starts fine.

### Required for the `/admin` dashboard

| Variable | Description |
|---|---|
| `GOOGLE_CLIENT_ID` | OAuth 2.0 client ID from Google Cloud Console |
| `GOOGLE_CLIENT_SECRET` | OAuth 2.0 client secret from Google Cloud Console |

Without these, the `/admin` login will fail (Google OAuth redirects will be broken), but all other routes are unaffected.

### Required for Garmin integration

| Variable | Description |
|---|---|
| `GARMIN_CONSUMER_KEY` | From Garmin Connect developer portal |
| `GARMIN_CONSUMER_SECRET` | From Garmin Connect developer portal |

The app uses `IntegrationNotConfiguredError` to gracefully handle missing Garmin credentials — the integration routes will return an error response rather than crashing.

### Required for TrainingPeaks integration

| Variable | Description |
|---|---|
| `TP_CLIENT_ID` | From TrainingPeaks developer portal |
| `TP_CLIENT_SECRET` | From TrainingPeaks developer portal |

Same graceful-degradation behavior as Garmin.

### Optional / has defaults

| Variable | Default | Description |
|---|---|---|
| `PUBLIC_URL` | Request host | Base URL used in OAuth callbacks and SMS links. Set to `http://localhost:8000` for local dev. |
| `PORT` | `8080` | Only used by gunicorn in Docker; Flask dev server ignores this. |
| `CRR` | `0.004` | Rolling resistance coefficient (physics default) |
| `RHO` | `1.225` | Air density kg/m³ (physics default) |

---

## What works without all credentials

| Feature | Works without credentials? |
|---|---|
| App starts | Only if Firestore ADC is configured — `db.py` initializes at import time |
| Strava OAuth flow (`/auth`, `/callback`) | Requires `STRAVA_CLIENT_ID` + `STRAVA_CLIENT_SECRET` |
| Strava webhook (`/webhook`) | Starts, but event processing needs Strava tokens and Firestore |
| Inbound SMS (`/sms/inbound`) | Needs Firestore + Twilio to fully work |
| Outbound SMS after activity | Needs Firestore + Twilio |
| AI chat (`/chat`) | Needs `ANTHROPIC_API_KEY` to respond; app starts without it |
| `/admin` dashboard | Needs `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET` for login |
| Garmin integration | Fails gracefully with `IntegrationNotConfiguredError` |
| TrainingPeaks integration | Fails gracefully with `IntegrationNotConfiguredError` |
| Physics constants (`CRR`, `RHO`) | Have defaults; always work |

**Minimum viable local setup** (just to start the app and explore routes):
1. Firestore ADC (`gcloud auth application-default login`)
2. `SECRET_KEY=any-random-string`
3. `STRAVA_CLIENT_ID` + `STRAVA_CLIENT_SECRET` (for OAuth flows)

---

## Strava webhook (local testing)

Strava's webhook requires a publicly reachable HTTPS URL. For local development, use [ngrok](https://ngrok.com):

```bash
# In a separate terminal
ngrok http 8000
```

ngrok will give you a URL like `https://abc123.ngrok-free.app`. Use that as your `PUBLIC_URL` in `.env` and register the webhook with Strava pointing to `https://abc123.ngrok-free.app/webhook`.

Note: Each ngrok session gives a new URL (unless you have a paid plan with a fixed subdomain), so you'll need to re-register the webhook each time.

---

## Frontend (Firebase Hosting + web/)

The `web/` directory contains a static frontend deployed via Firebase Hosting. The `firebase.json` config rewrites `/chat**`, `/auth**`, and `/callback**` to the Cloud Run backend. For local Flask development, you don't need to run the Firebase frontend — hit the Flask routes directly at `http://localhost:8000`.

---

## Makefile targets

```bash
make setup    # Create .venv, install dependencies, copy .env.example → .env
make install  # pip install -r requirements.txt (into active venv)
make dev      # Run Flask with hot reload and debug mode
```
