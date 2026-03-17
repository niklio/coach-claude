# Hosting Architecture — irlll.com

_Last updated: 2026-03-16_

---

## Architecture Overview

| Domain | Target | Status |
|---|---|---|
| `irlll.com` | Cloud Run (`coach-claude`, us-east1) | DNS resolves, HTTPS cert pending (broken) |
| `api.irlll.com` | Cloud Run (`coach-claude`, us-east1) | Fully working |
| `chat.irlll.com` | Cloud Run (`coach-claude`, us-east1) | Fully working |
| Firebase Hosting | `coach-8413d` project | Deployed but no custom domain pointed at it |

### Cloud Run service

- **Service**: `coach-claude`
- **Project**: `coach-8413d`
- **Region**: `us-east1`
- **URL**: `https://coach-claude-259958861423.us-east1.run.app`
- **Traffic**: 100% to latest revision (`coach-claude-00017-cdc`)
- **PUBLIC_URL env var**: `https://irlll.com`

### Firebase Hosting

- **Project**: `coach-8413d`
- **Config**: `web/firebase.json`
- **Static files**: `web/public/` (currently only `index.html`)
- **Rewrites**: `/chat**`, `/auth**`, `/callback**` proxy to Cloud Run; all else → `index.html`
- **Custom domain**: None configured in Firebase — `irlll.com` DNS is NOT pointed at Firebase

---

## DNS Records (current state)

```
irlll.com      A       216.239.32.21   (Cloud Run IPs)
irlll.com      A       216.239.34.21
irlll.com      A       216.239.36.21
irlll.com      A       216.239.38.21
api.irlll.com  CNAME   ghs.googlehosted.com.
chat.irlll.com CNAME   ghs.googlehosted.com.
```

---

## What is Broken

### 1. `https://irlll.com` — TLS certificate not provisioned

**Symptom**: `curl -sv https://irlll.com` fails with `SSL_ERROR_SYSCALL`. HTTP (`http://irlll.com`) redirects to HTTPS, but HTTPS then fails.

**Root cause**: The Cloud Run domain mapping for `irlll.com` is stuck in `CertificatePending` state. Google Cloud Run provisions TLS certs via an ACME HTTP-01 challenge. The challenge validation is failing because the DNS records (Google-hosted A records) are resolving but the certificate issuance challenge data is not visible publicly — the mapping status message says:

> "Certificate issuance pending. The challenge data was not visible through the public internet. This may indicate that DNS is not properly configured or has not fully propagated."

The `DomainRoutable` condition is `True`, meaning the mapping itself is valid. The cert provisioning is just stuck.

**Why `api.irlll.com` works but `irlll.com` doesn't**: The apex domain (`irlll.com`) requires A/AAAA records, while subdomains use CNAME to `ghs.googlehosted.com`. The apex domain cert challenge may be failing because Cloud Run's certificate provisioning for apex domains is less reliable than for subdomain CNAMEs. The `api.irlll.com` and `chat.irlll.com` mappings are both `Ready` with certs provisioned.

### 2. Architecture confusion: Cloud Run vs Firebase Hosting for `irlll.com`

The deploy workflow and configs suggest `irlll.com` should be served by Firebase Hosting (with rewrites to Cloud Run for dynamic routes). However, the DNS A records for `irlll.com` currently point directly to Cloud Run IPs — not to Firebase Hosting.

Additionally, `app.py` has a `homepage()` route at `/` that reads and serves `web/public/index.html` directly from Flask/Cloud Run. This means the homepage is served from Cloud Run even without Firebase Hosting.

The current setup effectively has two competing configurations:
- Cloud Run domain mapping for `irlll.com` (DNS is pointed here, cert is broken)
- Firebase Hosting for `irlll.com` (no DNS pointed here, may or may not have a custom domain configured)

---

## What is Working

- **`https://api.irlll.com`** — fully working, serves the Flask API (verified: `/health` returns `{"status":"ok","users":1}`)
- **`https://chat.irlll.com`** — fully working, serves the `/chat` UI (verified: returns full chat HTML page)
- **`http://irlll.com`** — redirects to HTTPS (Cloud Run does this automatically)
- **`https://coach-claude-259958861423.us-east1.run.app`** — raw Cloud Run URL, always works
- **Deploy workflow** — triggers on push to main, deploys API to Cloud Run and static files to Firebase Hosting
- **All Flask routes** — `/auth`, `/callback`, `/chat`, `/chat/auth`, `/chat/message`, `/chat/status`, `/chat/init`, `/admin`, `/webhook`, `/sms/inbound`, `/health` etc. all function via `api.irlll.com` or `chat.irlll.com`

---

## Recommended Fix Options

There are two valid approaches. Pick one and stick with it.

### Option A: Use Cloud Run exclusively for `irlll.com` (simpler)

Keep the current DNS A records (pointing `irlll.com` at Cloud Run IPs). Fix the broken TLS cert by deleting and recreating the domain mapping.

**Manual steps:**

1. Delete the existing stuck domain mapping:
   ```
   gcloud beta run domain-mappings delete irlll.com --region us-east1 --project coach-8413d
   ```
2. Recreate it:
   ```
   gcloud beta run domain-mappings create --service coach-claude --domain irlll.com --region us-east1 --project coach-8413d
   ```
3. Verify the new mapping gives the same A/AAAA records and update DNS if they differ.
4. Wait 10–30 minutes for cert provisioning to complete.
5. Verify with: `curl -sv https://irlll.com`

**Code change needed**: Remove the `deploy-web` job from `.github/workflows/deploy.yml` (or keep it for staging/backup), since the homepage is already served by the `/` Flask route.

**Pros**: Simple. One system. `irlll.com/chat`, `/auth`, `/callback` all work natively via Flask.
**Cons**: Firebase Hosting CDN is not used for the static homepage.

### Option B: Use Firebase Hosting for `irlll.com` (as originally designed)

Point `irlll.com` DNS at Firebase Hosting instead of Cloud Run. Firebase Hosting rewrites `/chat**`, `/auth**`, `/callback**` to Cloud Run.

**Manual steps:**

1. Re-authenticate Firebase CLI locally: `firebase login --reauth`
2. Add `irlll.com` as a custom domain in Firebase Hosting:
   - Go to [Firebase Console](https://console.firebase.google.com/project/coach-8413d/hosting) → Hosting → Add custom domain → `irlll.com`
   - Firebase will give you DNS records to set (likely A records pointing to Firebase IPs, different from the current Cloud Run IPs)
3. Delete the Cloud Run domain mapping for `irlll.com` (to avoid conflict):
   ```
   gcloud beta run domain-mappings delete irlll.com --region us-east1 --project coach-8413d
   ```
4. Update DNS: replace the current Cloud Run A records (`216.239.32/34/36/38.21`) with the Firebase Hosting A records.
5. Wait for DNS propagation and Firebase TLS cert to provision.

**Code changes needed**:
- Remove or simplify the `homepage()` route in `app.py` (Flask's `/` route currently serves the homepage — with Firebase Hosting in front, this is redundant but not harmful).
- The `web/firebase.json` rewrites look correct already — `/chat**`, `/auth**`, `/callback**` proxy to `coach-claude` Cloud Run. The `** → /index.html` catch-all handles the homepage.
- Consider adding `/admin**` and `/webhook**` to the Firebase rewrites if those need to be reachable on `irlll.com`.

**Pros**: Static homepage served from Firebase CDN (fast global delivery). Clean separation between static and dynamic.
**Cons**: More moving parts. Firebase Hosting CLI auth must work in CI.

---

## Deploy Workflow Notes

The current `.github/workflows/deploy.yml`:
- Uses Workload Identity Federation (no long-lived service account keys) — correct.
- Deploys API via `gcloud run deploy --source .` (builds from source using Cloud Build) — correct.
- Sets `PUBLIC_URL=https://irlll.com` as an env var update — correct.
- Deploys Firebase Hosting via `firebase deploy --only hosting --project coach-8413d` — this works in CI as long as the service account `github-actions@coach-8413d.iam.gserviceaccount.com` has the `Firebase Hosting Admin` role.

**To verify the service account has the right Firebase role:**
```
gcloud projects get-iam-policy coach-8413d \
  --flatten="bindings[].members" \
  --filter="bindings.members:serviceAccount:github-actions@coach-8413d.iam.gserviceaccount.com" \
  --format="table(bindings.role)"
```
It needs `roles/firebasehosting.admin` (or `roles/firebase.admin`).

---

## Environment Variables (on Cloud Run)

All secrets are set on the Cloud Run service. The deploy workflow uses `--update-env-vars` which only updates the vars listed; all others persist. Key vars:

| Var | Purpose |
|---|---|
| `PUBLIC_URL` | Base URL for OAuth redirects — must be `https://irlll.com` |
| `SECRET_KEY` | Flask session signing key |
| `ANTHROPIC_API_KEY` | Claude API |
| `STRAVA_CLIENT_ID` / `STRAVA_CLIENT_SECRET` | Strava OAuth |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Google OAuth (admin login) |
| `STRAVA_WEBHOOK_VERIFY_TOKEN` | Strava webhook verification |

---

## Quick Status Check Commands

```bash
# Cloud Run service
gcloud run services describe coach-claude --region us-east1 --project coach-8413d

# Domain mappings
gcloud beta run domain-mappings list --region us-east1 --project coach-8413d

# Certificate status for apex domain
gcloud beta run domain-mappings describe --domain irlll.com --region us-east1 --project coach-8413d

# Live health check
curl https://api.irlll.com/health

# Test HTTPS on apex
curl -sv https://irlll.com
```
