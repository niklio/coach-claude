# Twilio A2P 10DLC — Registration & Compliance Guide

## What is A2P 10DLC?

A2P 10DLC (Application-to-Person 10-digit Long Code) is the US carrier standard that requires any application sending SMS/MMS to US consumers through a standard local phone number to register both the sending business (Brand) and the message programme (Campaign) with The Campaign Registry (TCR). Unregistered traffic is filtered or blocked by carriers such as AT&T, T-Mobile, and Verizon.

---

## Use Case Classification

| Field | Value |
|---|---|
| **Use case category** | Mixed / App Notifications + Conversational |
| **More specific label** | "App Notifications" or "Conversational Messaging" |
| **Who sends** | Coach Claude app (automated webhook, triggered by new Strava activity) |
| **Who receives** | Individual cyclists who explicitly signed up via the app's OAuth flow |
| **What is sent** | CdA aerodynamic coefficient results after each outdoor ride; weight-setup prompts; user-initiated queries (last ride CdA, weight update) |
| **Volume** | Very low — at most one message per user per outdoor ride. Typical user uploads 3–5 rides per week. Total expected volume is well below 2,000 message segments/day on any single carrier. |
| **Recommended registration tier** | **Low-Volume Standard** (cheaper, appropriate for < 2,000 segments/day on T-Mobile) |

---

## Registration Checklist

### Step 0 — Prerequisites

- [ ] Upgrade Twilio account to paid (trial accounts cannot register A2P).
- [ ] Confirm the `TWILIO_FROM_NUMBER` is a US 10DLC number (not a toll-free or short code).
- [ ] Have a publicly accessible Privacy Policy URL (see below).
- [ ] Have a business website or landing page (can be the app's public URL: `https://api.irlll.com`).

---

### Step 1 — Create a Customer Profile (Business Identity)

In the Twilio Console: **Messaging → Regulatory Compliance → Customer Profiles → Create**

Required information:

| Field | Value to supply |
|---|---|
| Entity type | Sole Proprietor (if no EIN) or Private/For-Profit Company (if registered LLC/Corp) |
| Legal business name | Exact name matching your IRS / state registration |
| EIN / Tax ID | Required if you have one. If sole proprietor with no EIN, choose Sole Proprietor path. |
| Business address | Physical mailing address (no PO boxes) |
| Authorized representative | Your name, title, direct phone, and email |
| Website URL | `https://api.irlll.com` (or wherever the app lives publicly) |
| Industry | Software / Technology |

> **Note:** If you have an EIN you cannot register as Sole Proprietor. New EINs can take 30–90 days to propagate through TCR's validation databases.

---

### Step 2 — Register Your Brand

In the Twilio Console: **Messaging → Senders → A2P Brands → Register a Brand**

The Brand is your business identity in TCR. For Low-Volume Standard:

- Registration fee: **$4 one-time** (as of 2025; campaign fees are currently waived).
- TCR typically approves brands within **minutes to a few hours**.
- You will receive a Trust Score (higher = better throughput). Accuracy of legal name and EIN is the primary driver.

Sole Proprietor limits:
- Only **1 campaign** and **1 phone number** allowed per sole-proprietor brand.
- Each phone number can validate at most 3 brands; each email at most 10.

---

### Step 3 — Register Your Campaign

In the Twilio Console: **Messaging → Senders → A2P Campaigns → Register a Campaign**

#### Campaign Description (40–4096 chars)

Suggested text:

```
Coach Claude is a cycling performance app that automatically calculates each user's
aerodynamic drag coefficient (CdA) from outdoor Strava ride data and delivers the
result via SMS. Users explicitly opt in by connecting their Strava account through
the app's OAuth flow and providing their phone number. Messages include ride CdA
results, weight setup prompts, and responses to user-initiated queries. No marketing
or promotional content is sent.
```

#### Sample Messages

Provide **at least 2** samples. Use brackets for variable content.

**Sample 1 — First outbound message (weight request):**
```
Hey, this is Coach Claude! I'll text you your CdA after every outdoor Strava ride.
First, what's your combined rider + bike weight? Reply with a number in kg (e.g. 75)
or lbs (e.g. 165 lbs).

Reply STOP to unsubscribe. Msg & data rates may apply.
```

**Sample 2 — CdA result notification:**
```
Coach Claude
Ride: "[Ride Name]"
CdA: [0.2345] m²
([N] samples)
strava.com/activities/[activity_id]
```

**Sample 3 — Weight confirmation:**
```
Got it — [75.0] kg stored. Coach Claude will use this for all your CdA calculations.
Reply 'change weight' any time to update it.
```

#### Opt-In Method

Select **"Opt-In via website"** (or "Opt-in via mobile QR code" if applicable).

Screenshot / description to provide to Twilio:

> Users visit `https://api.irlll.com/chat` (or receive a link via the app), enter
> their name and phone number, then click "Connect with Strava". Completing the
> Strava OAuth flow constitutes explicit opt-in consent to receive SMS from Coach
> Claude. The sign-up page states: "Coach Claude analyses your outdoor rides and
> texts you your aerodynamic CdA."

#### Opt-In Keywords

```
START, UNSTOP, YES
```

#### Opt-In Message (auto-reply when user texts START)

```
You have been resubscribed to Coach Claude. Reply STOP at any time to unsubscribe.
```

#### Opt-Out Keywords

```
STOP, STOPALL, UNSUBSCRIBE, CANCEL, END, QUIT
```

#### Opt-Out Message

```
You have been unsubscribed from Coach Claude. No further messages will be sent. Text START to resubscribe.
```

#### Help Keywords

```
HELP, INFO
```

#### Help Message

```
Coach Claude — cycling CdA analysis via Strava. Commands: 'last ride' | 'change weight'. Support: nikliolios@irlll.com. Reply STOP to unsubscribe.
```

#### Other campaign fields

| Field | Value |
|---|---|
| Embedded links | Yes (strava.com activity links in CdA results) |
| Embedded phone numbers | No |
| Affiliate marketing | No |
| Age-gated content | No |

---

### Step 4 — Link Your Phone Number to the Campaign

1. In the Twilio Console, create or select a **Messaging Service**.
2. Add your `TWILIO_FROM_NUMBER` as a sender in that Messaging Service.
3. Associate the Messaging Service with the campaign you registered.
4. Update your `.env` / Cloud Run secrets: replace `TWILIO_FROM_NUMBER` with `TWILIO_MESSAGING_SERVICE_SID` if you prefer to use a Messaging Service SID instead of a raw number. The current code in `sms_sender.py` uses `from_=` directly; both approaches are valid.

---

### Step 5 — Set the Inbound URL and Status Callback in Twilio Console

In **Phone Numbers → Manage → Active Numbers → [your number]**:

| Field | Value |
|---|---|
| Messaging — A message comes in | `https://api.irlll.com/sms/inbound` (HTTP POST) |
| Messaging — Status callbacks | `https://api.irlll.com/sms/status` (HTTP POST) |

The status callback URL is also set programmatically in `sms_sender._send()` via the `status_callback` kwarg (uses `PUBLIC_URL` env var). Both approaches are fine; the programmatic value overrides the console default per message.

---

### Step 6 — Privacy Policy

You **must** have a public Privacy Policy that includes this language (or equivalent):

> "We will not share or sell your mobile phone number or SMS opt-in data with third
> parties for marketing or promotional purposes."

Suggested location: `https://api.irlll.com/privacy` (a static HTML or Markdown page is sufficient).

Until the Privacy Policy page exists, you can add a minimal route to `app.py`:

```python
@app.route("/privacy")
def privacy():
    return """<h1>Privacy Policy</h1>
<p>Coach Claude collects your phone number solely to deliver cycling CdA analysis via SMS.
We do not share or sell your mobile number or SMS opt-in data with any third party
for marketing or promotional purposes. Msg &amp; data rates may apply.
Reply STOP to unsubscribe at any time.</p>""", 200
```

---

## Code Changes Implemented

The following changes have already been made in this branch:

### `sms_sender.py`

1. **Opt-in footer** (`_OPT_IN_FOOTER`) appended to `send_weight_request()` — the first outbound SMS a new user ever receives. Contains "Reply STOP to unsubscribe. Msg & data rates may apply." as required.
2. **`HELP_RESPONSE`** constant — brand name, support email, opt-out instruction.
3. **`STOP_RESPONSE`** constant — opt-out acknowledgement with brand name and resubscribe instruction.
4. **`status_callback`** kwarg added to `_send()` — posts delivery status events to `/sms/status` automatically whenever `PUBLIC_URL` is set.

### `app.py`

5. **STOP/START/HELP keyword handlers** added at the top of `sms_inbound()` — processed before any user lookup so they always work, even for unknown numbers.
6. **`sms_opted_out` guard** in `sms_inbound()` — if a known user has previously sent STOP, inbound messages are silently ignored (HTTP 204) rather than triggering a reply.
7. **`sms_opted_out` guard** in `_process_activity()` — prevents the app from attempting to send outbound CdA/weight-request SMS to opted-out users when a new Strava activity arrives.
8. **`/sms/status` webhook route** added — logs delivery success/failure for A2P compliance monitoring.

### `db.py`

9. **`set_sms_opted_out(athlete_id, opted_out)`** added — persists opt-out/re-subscribe state to Firestore under the `sms_opted_out` field.

---

## Remaining Manual Actions

| # | Action | Owner |
|---|---|---|
| 1 | Create a Privacy Policy page at `/privacy` (or external URL) | Developer |
| 2 | Register Brand in Twilio Console (Step 2 above) | Developer |
| 3 | Register Campaign in Twilio Console (Step 3 above) with the description and samples above | Developer |
| 4 | Link phone number to campaign in a Messaging Service (Step 4) | Developer |
| 5 | Set inbound + status callback URLs in Twilio Console (Step 5) | Developer |
| 6 | Verify `PUBLIC_URL` env var is set in Cloud Run so status callbacks fire correctly | Developer |
| 7 | Screenshot the `/chat` sign-up page to upload as opt-in evidence in Twilio campaign form | Developer |

---

## Estimated Timeline

| Phase | Duration |
|---|---|
| Brand registration (TCR review) | Minutes to a few hours |
| Campaign registration (TCR + carrier review) | **10–15 business days** (current queue as of 2025–2026) |
| Number linking and go-live | Same day after campaign approved |
| **Total from submission to production-ready** | **~2–3 weeks** |

During the review period, outbound messages can still be sent but may be filtered by some carriers. Plan accordingly if you have a launch date.

---

## Sources

- [Programmable Messaging and A2P 10DLC — Twilio Docs](https://www.twilio.com/docs/messaging/compliance/a2p-10dlc)
- [A2P 10DLC Registration Quickstart — Twilio Docs](https://www.twilio.com/docs/messaging/compliance/a2p-10dlc/quickstart)
- [Gather Required Business Information — Twilio Docs](https://www.twilio.com/docs/messaging/compliance/a2p-10dlc/collect-business-info)
- [Improving Your Chances of A2P 10DLC Approval — Twilio Blog](https://www.twilio.com/en-us/blog/insights/best-practices/improving-your-chances-of-a2p10dlc-registration-approval)
- [New Requirements for A2P 10DLC Registrations — Twilio Blog](https://www.twilio.com/en-us/blog/new-requirements-for-a2p-10dlc-registrations)
- [Direct Sole Proprietor Registration Overview — Twilio Docs](https://www.twilio.com/docs/messaging/compliance/a2p-10dlc/direct-sole-proprietor-registration-overview)
