# Deploy (Cloud Run + Firestore, australia-southeast1)

## Monorepo structure and ownership

A and B share this one repo; the submission is a single GitHub link.

| Directory | Owner | Notes |
|---|---|---|
| `services/property-api/` | A | calls rules-api (see section 11) |
| `services/rules-api/` | B | private Cloud Run service |
| `jobs/rates-refresh/` | B | Cloud Run job, daily |
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

```bash
gcloud run deploy rules-api \
  --image=$IMG/rules-api:$TAG --region=$REGION \
  --service-account=$(SA rules-api-sa) \
  --no-allow-unauthenticated \
  --set-env-vars=DATA_BACKEND=firestore

# only property-api-sa may call it
gcloud run services add-iam-policy-binding rules-api --region=$REGION \
  --member=serviceAccount:$(SA property-api-sa) --role=roles/run.invoker

export RULES_API_URL=$(gcloud run services describe rules-api --region=$REGION --format='value(status.url)')
```

## 9. Cloud Run jobs

```bash
# rates-refresh: Playwright/chromium needs memory
gcloud run jobs create rates-refresh \
  --image=$IMG/rates-refresh:$TAG --region=$REGION \
  --service-account=$(SA jobs-sa) \
  --memory=2Gi --cpu=1 --max-retries=1 --task-timeout=15m \
  --set-env-vars=DATA_BACKEND=firestore,GEMINI_MODEL=gemini-2.5-flash \
  --set-secrets=GEMINI_API_KEY=GEMINI_API_KEY:latest

# kb-refresh: same DATA_BACKEND=firestore as the rates job; it needs no Gemini secret (only DATA_BACKEND is read). Chromium fallback (blocked sites) needs 2Gi.
gcloud run jobs create kb-refresh \
  --image=$IMG/kb-refresh:$TAG --region=$REGION \
  --service-account=$(SA jobs-sa) \
  --memory=2Gi --max-retries=1 --task-timeout=15m \
  --set-env-vars=DATA_BACKEND=firestore

# smoke test once by hand, then check the Firestore job_runs collection
gcloud run jobs execute rates-refresh --region=$REGION --wait
```

Later image updates: `gcloud run jobs update rates-refresh --image=$IMG/rates-refresh:$NEW_TAG --region=$REGION` (same for kb-refresh); `gcloud run deploy rules-api --image=...` for the API.

## 10. Cloud Scheduler (Pacific/Auckland)

```bash
for JOB in rates-refresh kb-refresh; do
  gcloud run jobs add-iam-policy-binding $JOB --region=$REGION \
    --member=serviceAccount:$(SA scheduler-sa) --role=roles/run.invoker
done

# rates: every day 06:00
gcloud scheduler jobs create http rates-refresh-daily \
  --location=$REGION --schedule="0 6 * * *" --time-zone="Pacific/Auckland" \
  --http-method=POST \
  --uri="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/rates-refresh:run" \
  --oauth-service-account-email=$(SA scheduler-sa)

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
r = requests.get(f"{RULES}/health", headers={"Authorization": f"Bearer {tok}"})
```

Node 18+:

```js
const RULES = "https://rules-api-xxxx-ts.a.run.app";
const idUrl = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience=" + RULES;
const tok = await (await fetch(idUrl, { headers: { "Metadata-Flavor": "Google" } })).text();
const r = await fetch(`${RULES}/health`, { headers: { Authorization: `Bearer ${tok}` } });
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

- A stale bank shows up as a `stale_since: <date>` entry in that bank's `notes` in `rates_latest/current`, and in
  `job_runs/*` under `failed`. The job exits non-zero only when every bank fails; set a log-based alert on
  Cloud Run job failures if you want to hear about it.
- Rollback of a bad snapshot: copy `rates_snapshots/<good date>` over `rates_latest/current`.

## Rates coverage

`jobs/rates-refresh/config/banks.yaml` covers 11 lenders: Kiwibank, Westpac, ANZ, ASB, BNZ, TSB, Co-operative Bank,
SBS Bank, Unity, Nelson Building Society and NZHL (ASB-written, own rates page). Adding a lender means one entry in
that file plus a check with `python -m rates_refresh --fetch-only --bank <id>` from `jobs/rates-refresh/`.

Not covered (future work): non-bank lenders such as Pepper, Resimac, Liberty, Avanti and Basecorp. Their rates are
tiered by LVR *and* document type (full doc / alt doc / near-prime), so one lender has many rate rows rather than a
single `special`/`standard` ladder per term. The snapshot schema (`special`, `standard`, `low_equity.bands`) cannot
express that without a schema change that rules-api would also have to understand. Most of them also publish rates
only behind broker portals or as ranges, which the page-text extractor cannot read reliably.
