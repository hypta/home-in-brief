# Deploy (Cloud Run + Firestore, australia-southeast1)

## Monorepo structure and ownership

A and B share this one repo; the submission is a single GitHub link.

| Directory | Owner | Notes |
|---|---|---|
| `services/property-api/` | A | calls rules-api (see section 11) |
| `services/rules-api/` | B | private Cloud Run service |
| `jobs/rates-refresh/` | B | Cloud Run job; scheduler code kept but disabled, rates maintained by hand (`RATES_UPDATE.md`) |
| `jobs/kb-refresh/` | B | Cloud Run job, weekly |
| `shared/`, `kb/`, `data/` | shared | common code, knowledge base, seed data |

- Each service/job has its own `Dockerfile` and `requirements.txt`.
- The Docker build context is always the repo root (`docker build -f <dir>/Dockerfile .`), so Dockerfiles can `COPY shared/ ...`.
- All paths in `cloudbuild.yaml` and this file are relative to the repo root.
- Suggested: add `.github/CODEOWNERS` mapping each directory above to its owner (not created yet).
- A's image is a commented-out step in `cloudbuild.yaml`; enable it by uncommenting the step and its `images:` line.

Commands are listed in order and are **not executed by any script**. Run them yourself.

```bash
export PROJECT_ID=your-project-id
export REGION=australia-southeast1
export REPO=nz-buyer-report          # Artifact Registry repo name
export TAG=$(git rev-parse --short HEAD)
export IMG=${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}
gcloud config set project $PROJECT_ID
```

## 1. Enable APIs

```bash
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  firestore.googleapis.com secretmanager.googleapis.com cloudscheduler.googleapis.com \
  billingbudgets.googleapis.com
```

## 2. Artifact Registry repo

```bash
gcloud artifacts repositories create $REPO --repository-format=docker --location=$REGION
```

## 3. Firestore (Native mode)

```bash
gcloud firestore databases create --location=$REGION --type=firestore-native
```

## 3b. Load seed data into Firestore

A fresh database is empty. Without this step rules-api is healthy but `/v1/rates/latest` returns 404, `/v1/rules`
returns no repayment scenarios (warning `rates_unavailable`) and `/v1/sources` is `[]`. The rates job cannot fill the
gap: with no bank marked `tos_status: allowed` it fetches nothing (see section 9 and RATES_UPDATE.md).

Run the two scripts from the repo root against the **real project**, not the emulator. They use Application Default
Credentials, so the identity running them needs `roles/datastore.user` on the project (grant it to yourself, or to a
service account you impersonate, as in section 5).

```bash
make setup                                    # once: .venv with google-cloud-firestore
gcloud auth application-default login         # or: export GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member=user:$(gcloud config get-value account) --role=roles/datastore.user

unset FIRESTORE_EMULATOR_HOST                 # must be unset, otherwise the data goes to a local emulator
export GOOGLE_CLOUD_PROJECT=$PROJECT_ID
.venv/bin/python scripts/seed_firestore.py --dry-run   # optional: print what would be written
.venv/bin/python scripts/seed_firestore.py             # sources/* and kb_items/*
.venv/bin/python scripts/seed_rates.py                 # rates_latest/current and rates_snapshots/<NZ date>_<run_id>
```

Notes:
- Do not use `make seed` here: it hard-codes the emulator host and project `local-dev`.
- `seed_firestore.py` treats the git seed as the source of truth for content. It runs `kb/validate.py` first: if the seed
  is invalid, nothing is written. From Firestore it keeps `last_check` and a human verification only while that
  verification still matches the seed item's exact content hash (changed content becomes `needs_review`).
- A kb-refresh demotion (`needs_review`, with `demoted_at`) is kept on re-seed, even if the seed says `verified`, unless
  the seed's `verified_at` is strictly later than `demoted_at` and the content hash matches. Write `verified_at` as a
  full ISO 8601 timestamp with offset (for example `2026-10-04T09:30:00+13:00`), not just a date.
- Ids removed from the seed are marked `retired` (with `retired_at`). A retired id stays retired unless the seed item sets
  `"reinstate": true`, or carries a valid verification with `verified_at` later than `retired_at`. A seed item with
  `"status": "retired"` always retires.
  `seed_rates.py` overwrites `rates_latest/current` with `data/rates_seed.json`.
- All seeded KB items have `status: draft` until a person verifies them (see `kb/MANUAL_CHECK.md`). rules-api only
  returns `verified` items unless `INCLUDE_DRAFT=true` is set, so `kb_items` stays empty in production until items are verified.
- Check after seeding (needs an identity with `run.invoker`, after section 8):
  `curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" $RULES_API_URL/v1/rates/latest` returns 200.

## 4. Service accounts

```bash
for SA in rules-api-sa jobs-sa scheduler-sa property-api-sa; do
  gcloud iam service-accounts create $SA --display-name=$SA
done
# property-api-sa is the identity of the property-API service (the caller of rules-api).
```

## 5. IAM

```bash
SA() { echo "$1@${PROJECT_ID}.iam.gserviceaccount.com"; }

# jobs write Firestore; rules-api only reads it
gcloud projects add-iam-policy-binding $PROJECT_ID --member=serviceAccount:$(SA jobs-sa)      --role=roles/datastore.user
gcloud projects add-iam-policy-binding $PROJECT_ID --member=serviceAccount:$(SA rules-api-sa) --role=roles/datastore.viewer

# Cloud Build default SA needs to push images
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member=serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com --role=roles/artifactregistry.writer
```

The `run.invoker` bindings are in sections 8, 9 and 10 (they need the services and jobs to exist first).

## 6. Build images

```bash
gcloud builds submit --config cloudbuild.yaml \
  --substitutions=_REGION=$REGION,_REPO=$REPO,SHORT_SHA=$TAG .
```

## 7. Secret for the rates job

```bash
gcloud secrets create GEMINI_API_KEY --replication-policy=automatic
printf '%s' 'YOUR_GEMINI_API_KEY' | gcloud secrets versions add GEMINI_API_KEY --data-file=-
gcloud secrets add-iam-policy-binding GEMINI_API_KEY \
  --member=serviceAccount:$(SA jobs-sa) --role=roles/secretmanager.secretAccessor
```

## 8. Deploy rules-api (private)

`--min-instances=1` keeps one instance warm so judges do not hit a cold start. Use it for the judging period
(Oct 19 - Nov 6). It is billed for the idle instance (CPU and memory, roughly a few dollars to low tens of dollars a
month for a small instance; check the Cloud Run pricing page for your region and your budget alert from section 12).
Afterwards set it back to scale-to-zero:
`gcloud run services update rules-api --region=$REGION --min-instances=0`.

```bash
gcloud run deploy rules-api \
  --image=$IMG/rules-api:$TAG --region=$REGION \
  --service-account=$(SA rules-api-sa) \
  --no-allow-unauthenticated \
  --min-instances=1 \
  --set-env-vars=DATA_BACKEND=firestore

# only property-api-sa may call it
gcloud run services add-iam-policy-binding rules-api --region=$REGION \
  --member=serviceAccount:$(SA property-api-sa) --role=roles/run.invoker

export RULES_API_URL=$(gcloud run services describe rules-api --region=$REGION --format='value(status.url)')
```

## 9. Cloud Run jobs

```bash
# rates-refresh: created but NOT scheduled (see section 10). 2Gi is only needed if a bank is switched to `render: browser`.
gcloud run jobs create rates-refresh \
  --image=$IMG/rates-refresh:$TAG --region=$REGION \
  --service-account=$(SA jobs-sa) \
  --memory=2Gi --cpu=1 --max-retries=1 --task-timeout=15m \
  --set-env-vars=DATA_BACKEND=firestore,GEMINI_MODEL=gemini-2.5-flash \
  --set-secrets=GEMINI_API_KEY=GEMINI_API_KEY:latest

# kb-refresh: same DATA_BACKEND=firestore as the rates job; it needs no Gemini secret (only DATA_BACKEND is read) and no browser.
gcloud run jobs create kb-refresh \
  --image=$IMG/kb-refresh:$TAG --region=$REGION \
  --service-account=$(SA jobs-sa) \
  --memory=512Mi --max-retries=1 --task-timeout=15m \
  --set-env-vars=DATA_BACKEND=firestore

# smoke test once by hand, then check the Firestore job_runs collection
# (with no bank marked tos_status=allowed, rates-refresh fetches nothing, writes no snapshot and records skipped_by_policy in job_runs)
gcloud run jobs execute rates-refresh --region=$REGION --wait
```

Later image updates: `gcloud run jobs update rates-refresh --image=$IMG/rates-refresh:$NEW_TAG --region=$REGION` (same for kb-refresh); `gcloud run deploy rules-api --image=...` for the API.

## 10. Cloud Scheduler (Pacific/Auckland)

```bash
for JOB in kb-refresh; do   # add rates-refresh here when its schedule is enabled
  gcloud run jobs add-iam-policy-binding $JOB --region=$REGION \
    --member=serviceAccount:$(SA scheduler-sa) --role=roles/run.invoker
done

# rates: DISABLED. The banks' website terms do not clearly allow automated access, so rates are updated by hand
# (see RATES_UPDATE.md). Enable only when a bank in banks.yaml is marked tos_status: allowed.
# gcloud run jobs add-iam-policy-binding rates-refresh --region=$REGION \
#   --member=serviceAccount:$(SA scheduler-sa) --role=roles/run.invoker
# gcloud scheduler jobs create http rates-refresh-daily \
#   --location=$REGION --schedule="0 6 * * *" --time-zone="Pacific/Auckland" \
#   --http-method=POST \
#   --uri="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/rates-refresh:run" \
#   --oauth-service-account-email=$(SA scheduler-sa)

# kb: every Monday 06:00
gcloud scheduler jobs create http kb-refresh-weekly \
  --location=$REGION --schedule="0 6 * * 1" --time-zone="Pacific/Auckland" \
  --http-method=POST \
  --uri="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/kb-refresh:run" \
  --oauth-service-account-email=$(SA scheduler-sa)
```

## 11. Calling rules-api from the property API (service-to-service auth)

rules-api rejects unauthenticated requests. The caller runs as `property-api-sa` on Cloud Run, fetches an
ID token from the metadata server with `audience` = the rules-api URL, and sends it as a Bearer token.

Python (no extra deps beyond `requests`):

```python
import requests
RULES = "https://rules-api-xxxx-ts.a.run.app"
tok = requests.get("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity",
                   params={"audience": RULES}, headers={"Metadata-Flavor": "Google"}, timeout=5).text
r = requests.get(f"{RULES}/healthz", headers={"Authorization": f"Bearer {tok}"})
```

Node 18+:

```js
const RULES = "https://rules-api-xxxx-ts.a.run.app";
const idUrl = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience=" + RULES;
const tok = await (await fetch(idUrl, { headers: { "Metadata-Flavor": "Google" } })).text();
const r = await fetch(`${RULES}/healthz`, { headers: { Authorization: `Bearer ${tok}` } });
```

Cache the token for about 50 minutes (it lives 1 hour). Locally, use
`gcloud auth print-identity-token --impersonate-service-account=$(SA property-api-sa) --audiences=$RULES_API_URL`
(requires `roles/iam.serviceAccountTokenCreator` on that SA) or just
`curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" $RULES_API_URL/...` as a user who has run.invoker.

## 12. Budget alert

```bash
BILLING=$(gcloud billing projects describe $PROJECT_ID --format='value(billingAccountName)' | sed 's|.*/||')
gcloud billing budgets create --billing-account=$BILLING \
  --display-name="nz-buyer-report monthly" --budget-amount=50NZD \
  --filter-projects=projects/$PROJECT_ID \
  --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0
```

Budgets only alert, they do not stop spending. Adjust the amount (and currency to match the billing account).

## Operations notes

- Banks that are not `tos_status: allowed` are listed under `skipped_by_policy` in `job_runs/*` and keep their
  existing (hand-maintained) data. For allowed banks, a failed or blocked fetch is listed under `failed`; its old row
  is kept and marked with `stale_since: <date>`, but only if the run produces a snapshot, which requires at least one
  bank to update successfully. If every attempted bank fails (or none is allowed), no snapshot is written, so `rates_latest`
  is unchanged and carries no stale marker; only a `job_runs` record is written. The job exits non-zero only when it
  attempted banks and every one failed; with no allowed banks it exits 0.
- Rollback of a bad snapshot: copy the good snapshot document `rates_snapshots/{NZ date}_{run_id}` (for example `2026-10-04_20261003T191756Z-ab12`) over `rates_latest/current`. Job run ids (`job_runs/<run_id>`) have the form `YYYYMMDDTHHMMSSZ-xxxx`.

## Rates coverage and automation policy

`jobs/rates-refresh/config/banks.yaml` covers 11 lenders: Kiwibank, Westpac, ANZ, ASB, BNZ, TSB, Co-operative Bank,
SBS Bank, Unity, Nelson Building Society and NZHL (ASB-written, own rates page).

Each lender carries `tos_status` (`allowed`, `unclear` or `prohibited`), `tos_url`, `tos_checked_at` and `tos_note`
from a review of its website terms and robots.txt on 2026-10-03 (not legal advice). The job only fetches banks
marked `allowed`. As of that review none is: NZHL is `prohibited` (its terms name scraping and automation), the other
ten are `unclear` (no written permission, no explicit ban). Those rows are updated by hand, see `RATES_UPDATE.md`.

Fetch behaviour for allowed banks: honest `User-Agent` (`home-buyer-copilot/0.1 (+repo URL)`), robots.txt is read first
and a Disallow means no request, and a 401/403/429 or an Imperva/Incapsula/Cloudflare challenge page is final (the
bank is recorded as blocked and keeps its old data; there is no retry through a browser). Playwright is used only for a
bank that is `allowed` and sets `render: browser` because its rates need JavaScript.

Adding a lender means one entry in `banks.yaml` (start with `tos_status: unclear`) and a row in `data/rates_seed.json`.

Not covered (future work): non-bank lenders such as Pepper, Resimac, Liberty, Avanti and Basecorp. Their rates are
tiered by LVR *and* document type (full doc / alt doc / near-prime), so one lender has many rate rows rather than a
single `special`/`standard` ladder per term. The snapshot schema (`special`, `standard`, `low_equity.bands`) cannot
express that without a schema change that rules-api would also have to understand. Most of them also publish rates
only behind broker portals or as ranges, which the page-text extractor cannot read reliably.
