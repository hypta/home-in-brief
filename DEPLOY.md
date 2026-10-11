# Deploy (Cloud Run + Firestore, australia-southeast1)

## Monorepo structure and ownership

A and B share this one repo; the submission is a single GitHub link.

| Directory | Owner | Notes |
|---|---|---|
| `services/property-api/` | A | private Cloud Run service (section 8b) |
| `services/web/` | A | the only public Cloud Run service: report page and relay to the private services (section 8c) |
| `services/rules-api/` | B | private Cloud Run service (section 8) |
| `jobs/rates-refresh/` | B | Cloud Run job; scheduler code kept but disabled, rates maintained by hand (`RATES_UPDATE.md`) |
| `jobs/kb-refresh/` | B | Cloud Run job, weekly |
| `shared/`, `kb/`, `data/` | shared | common code, knowledge base, seed data |
| `.github/` | shared | deploy workflow (`workflows/deploy.yml`) and its target list (`deploy/targets.json`) |

- Each service/job has its own `Dockerfile` and `requirements.txt`.
- The Docker build context is always the repo root (`docker build -f <dir>/Dockerfile .`), so Dockerfiles can `COPY shared/ ...`.
- All paths in this file are relative to the repo root.
- Suggested: add `.github/CODEOWNERS` mapping each directory above to its owner (not created yet).
- `cloudbuild.yaml` builds all five images with Cloud Build. **The GitHub Actions workflow does not use it**; it is kept
  for building by hand (`gcloud builds submit --config cloudbuild.yaml ...`, see the comment at its top).

## 1. How deployment works

Deployment is done by GitHub Actions (`.github/workflows/deploy.yml`), not by hand:

1. Every push to `main` (and the manual **Run workflow** button) runs `make setup` and `make test`.
2. If the tests pass, the deploy job logs in to Google Cloud with **Workload Identity Federation** (no key files),
   builds the changed images on the runner, pushes them to Artifact Registry tagged with the git sha, and deploys them
   with `gcloud run deploy` / `gcloud run jobs deploy`. Details in section 6.
3. Until the repository variable `GCP_PROJECT_ID` is set, the deploy job writes one line to the job summary and stops
   without failing. Tests still run.

Everything runs as the **Cloud Run default service account** (the Compute Engine default service account,
`PROJECT_NUMBER-compute@developer.gserviceaccount.com`). There are no per-service service accounts. The only extra
account is the deployer that GitHub Actions impersonates (section 3).

Why a deployer service account and not direct WIF: the `google-github-actions/auth` README says "not all Google Cloud
resources support `principalSet` identities", and Google's list of products and limitations says "Cloud Run doesn't
support Workload Identity Federation direct resource access. To allow access, use service account impersonation."
Sources: https://github.com/google-github-actions/auth#direct-wif,
https://docs.cloud.google.com/iam/docs/federated-identity-supported-services.

## One-time setup (checklist)

Done once by a person with Owner on the project, in this order. The commands are **not executed by any script**.

| Step | What | Section |
|---|---|---|
| 1 | Create the project, link billing, enable APIs | 2 |
| 2 | Artifact Registry repository | 2 |
| 3 | Firestore database (Native mode) | 2 |
| 4 | Roles for the default runtime service account | 3 |
| 5 | Deployer service account and its roles | 3 |
| 6 | Workload Identity pool and provider (with attribute condition) | 3 |
| 7 | Secrets: `LINZ_API_KEY` and `GEMINI_API_KEY` | 4 |
| 8 | Budget alert | 12 |
| 9 | GitHub repository variables | 5 |
| 10 | First deploy: run the workflow with `deploy_all` | 7 |
| 11 | Cloud Scheduler for kb-refresh (needs the job from step 10) | 10 |

Optional after step 10: the web basemap key (section 8c) and `--min-instances=1` for the judging period (section 8).

Shell variables used by every command below:

```bash
export PROJECT_ID=your-project-id
export REGION=australia-southeast1
export REPO=nz-buyer-report                 # Artifact Registry repo name (GCP_AR_REPO)
export GITHUB_REPO=hypta/home-buyer-copilot
gcloud config set project $PROJECT_ID
# after the project exists:
export PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')
export RUNTIME_SA=${PROJECT_NUMBER}-compute@developer.gserviceaccount.com
export DEPLOY_SA=github-deployer@${PROJECT_ID}.iam.gserviceaccount.com
```

## 2. Project, APIs, Artifact Registry, Firestore

```bash
gcloud projects create $PROJECT_ID
gcloud billing projects link $PROJECT_ID --billing-account=BILLING_ACCOUNT_ID   # gcloud billing accounts list

gcloud services enable run.googleapis.com artifactregistry.googleapis.com firestore.googleapis.com \
  secretmanager.googleapis.com cloudscheduler.googleapis.com billingbudgets.googleapis.com \
  iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com compute.googleapis.com
# cloudbuild.googleapis.com is only needed for building by hand with cloudbuild.yaml.
# compute.googleapis.com creates the Compute Engine default service account that Cloud Run runs as.

gcloud artifacts repositories create $REPO --repository-format=docker --location=$REGION

gcloud firestore databases create --location=$REGION --type=firestore-native
```

## 3. Identities and IAM

### Default runtime service account (what the services and jobs run as)

The workflow passes no `--service-account`, so every service and job runs as `$RUNTIME_SA`. Depending on the
organisation policy `iam.automaticIamGrantsForDefaultServiceAccounts`, a new project may or may not give this account
the broad Editor role. Grant what it needs explicitly either way. On our deployment the account did get Editor
automatically; A removed it and kept only the grants listed in this section, and the services work normally, so do the
same (find it with `gcloud projects get-iam-policy $PROJECT_ID --flatten=bindings --filter="bindings.members:$RUNTIME_SA"
--format='table(bindings.role)'`, then `gcloud projects remove-iam-policy-binding $PROJECT_ID
--member=serviceAccount:$RUNTIME_SA --role=roles/editor`; verify the flags):

```bash
# Firestore: the jobs write it, rules-api reads it, report-api reads and writes protection/* (Gemini allowance,
# section 8d). One identity for all, so this is datastore.user, not viewer.
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member=serviceAccount:$RUNTIME_SA --role=roles/datastore.user
```

Also for `$RUNTIME_SA`, granted elsewhere:
- `roles/secretmanager.secretAccessor` on the two secrets (section 4).
- `roles/run.invoker` on rules-api and property-api, so web can call them with an ID token. **The workflow adds these
  bindings** after each deploy (`gcloud run services add-iam-policy-binding`), because the services must exist first.
- `roles/run.invoker` on the kb-refresh job, for Cloud Scheduler (section 10).

Trade-off of a single identity: every service and job gets the union of these grants, so rules-api and web can also
write Firestore and read both secrets. The team accepted this to avoid per-service accounts; the grants above are the
whole list.

### Deployer service account (what GitHub Actions impersonates)

```bash
gcloud iam service-accounts create github-deployer --display-name="GitHub Actions deployer"

# create and update services and jobs, and set their IAM policy (--allow-unauthenticated, run.invoker bindings);
# roles/run.developer cannot set IAM policy
gcloud projects add-iam-policy-binding $PROJECT_ID --member=serviceAccount:$DEPLOY_SA --role=roles/run.admin

# deploy services and jobs that run as the default service account (iam.serviceAccounts.actAs)
gcloud iam service-accounts add-iam-policy-binding $RUNTIME_SA \
  --member=serviceAccount:$DEPLOY_SA --role=roles/iam.serviceAccountUser

# push images (includes read)
gcloud artifacts repositories add-iam-policy-binding $REPO --location=$REGION \
  --member=serviceAccount:$DEPLOY_SA --role=roles/artifactregistry.writer

# run scripts/seed_firestore.py and scripts/seed_rates.py (section 3b)
gcloud projects add-iam-policy-binding $PROJECT_ID --member=serviceAccount:$DEPLOY_SA --role=roles/datastore.user
```

The role list follows the Cloud Run deployer permissions (Cloud Run Developer or Admin, Service Account User on the
service identity, Artifact Registry access): https://docs.cloud.google.com/run/docs/reference/iam/roles.

### Workload Identity Federation (GitHub -> deployer)

The attribute condition admits only tokens from this repository on `refs/heads/main`. A push to `main` and a
**Run workflow** started from `main` both carry that ref; runs from any other branch are rejected at the token
exchange (the workflow also skips the deploy job off `main`).

```bash
gcloud iam workload-identity-pools create github \
  --location=global --display-name="GitHub Actions"

gcloud iam workload-identity-pools providers create-oidc home-buyer-copilot \
  --location=global --workload-identity-pool=github \
  --display-name="hypta/home-buyer-copilot" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
  --attribute-condition="assertion.repository == '${GITHUB_REPO}' && assertion.ref == 'refs/heads/main'"

# let identities from this repository impersonate the deployer
gcloud iam service-accounts add-iam-policy-binding $DEPLOY_SA \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/attribute.repository/${GITHUB_REPO}"

# the value for GCP_WIF_PROVIDER (projects/NUMBER/locations/global/workloadIdentityPools/github/providers/home-buyer-copilot)
gcloud iam workload-identity-pools providers describe home-buyer-copilot \
  --location=global --workload-identity-pool=github --format='value(name)'
```

Commands follow the `google-github-actions/auth` README ("Workload Identity Federation through a Service Account").

## 3b. Seed data in Firestore

A fresh database is empty. Without seed data rules-api is healthy but `/v1/rates/latest` returns 404, `/v1/rules`
returns no repayment scenarios (warning `rates_unavailable`) and `/v1/sources` is `[]`. The rates job cannot fill the
gap: with no bank marked `tos_status: allowed` it fetches nothing (see section 9 and RATES_UPDATE.md).

**The workflow seeds Firestore** as the deployer, before deploying the new rules-api revision:
- `scripts/seed_firestore.py` (`sources/*`, `kb_items/*`) when `kb/seed/` changed since the commit rules-api was last
  deployed from;
- `scripts/seed_rates.py` (`rates_latest/current`, `rates_snapshots/<NZ date>_<run_id>`) when `data/` changed;
- both on the first deploy (rules-api not deployed yet) and on a `deploy_all` run.

The scripts read `GOOGLE_CLOUD_PROJECT` and Application Default Credentials (the workflow's WIF credentials) and need
`roles/datastore.user` (granted to the deployer in section 3). `FIRESTORE_EMULATOR_HOST` must be unset.

To run them by hand from the repo root against the **real project**, not the emulator:

```bash
make setup                                    # once: .venv with google-cloud-firestore
gcloud auth application-default login
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
  is invalid, nothing is written (and the workflow run fails). From Firestore it keeps `last_check` and a human
  verification only while that verification still matches the seed item's exact content hash (changed content becomes
  `needs_review`).
- A kb-refresh demotion (`needs_review`, with `demoted_at`) is kept on re-seed, even if the seed says `verified`, unless
  the seed's `verified_at` is strictly later than `demoted_at` and the content hash matches. Write `verified_at` as a
  full ISO 8601 timestamp with offset (for example `2026-10-04T09:30:00+13:00`), not just a date.
- Ids removed from the seed are marked `retired` (with `retired_at`). A retired id stays retired unless the seed item sets
  `"reinstate": true`, or carries a valid verification with `verified_at` later than `retired_at`. A seed item with
  `"status": "retired"` always retires.
- `seed_rates.py` overwrites `rates_latest/current` with `data/rates_seed.json`. That is the intended path while rates
  are maintained by hand; if the rates job is ever scheduled, a later `data/` change would overwrite its snapshot.
- Seeded KB items have `status: draft` until a person verifies them (see `kb/MANUAL_CHECK.md`). rules-api only
  returns `verified` items unless `INCLUDE_DRAFT=true` is set.
- Check after seeding (needs an identity with `run.invoker` on rules-api):
  `curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" $RULES_API_URL/v1/rates/latest` returns 200.

## 4. Secrets (Secret Manager)

Secret values never appear in the workflow; the services and jobs mount them by name
(`--update-secrets` / `--set-secrets` in `.github/deploy/targets.json`).

```bash
# LINZ Data Service key for property-api: read from the unquoted LINZ_API_KEY=... line in .env, never typed or printed
gcloud secrets create LINZ_API_KEY --replication-policy=automatic
grep '^LINZ_API_KEY=' .env | cut -d= -f2- | tr -d '\n' | gcloud secrets versions add LINZ_API_KEY --data-file=-

# Gemini key for rates-refresh, same way
gcloud secrets create GEMINI_API_KEY --replication-policy=automatic
grep '^GEMINI_API_KEY=' .env | cut -d= -f2- | tr -d '\n' | gcloud secrets versions add GEMINI_API_KEY --data-file=-

for S in LINZ_API_KEY GEMINI_API_KEY; do
  gcloud secrets add-iam-policy-binding $S \
    --member=serviceAccount:$RUNTIME_SA --role=roles/secretmanager.secretAccessor
done
```

To rotate a key, add a new version; the services read `:latest` when a new instance starts (redeploy, or
`gcloud run services update property-api --region=$REGION --update-secrets=LINZ_API_KEY=LINZ_API_KEY:latest`).

## 5. GitHub repository variables

Repository **variables** (not secrets; none of these values is secret). In GitHub: Settings -> Secrets and variables
-> Actions -> Variables -> New repository variable. Or with the GitHub CLI:

```bash
gh variable set GCP_PROJECT_ID   --repo $GITHUB_REPO --body "$PROJECT_ID"
gh variable set GCP_REGION       --repo $GITHUB_REPO --body "$REGION"          # optional, default australia-southeast1
gh variable set GCP_AR_REPO      --repo $GITHUB_REPO --body "$REPO"            # optional, default nz-buyer-report
gh variable set GCP_DEPLOY_SA    --repo $GITHUB_REPO --body "$DEPLOY_SA"
gh variable set GCP_WIF_PROVIDER --repo $GITHUB_REPO --body "$(gcloud iam workload-identity-pools providers describe \
  home-buyer-copilot --location=global --workload-identity-pool=github --format='value(name)')"
```

Setting `GCP_PROJECT_ID` is what switches deployment on. If it is set but `GCP_WIF_PROVIDER` or `GCP_DEPLOY_SA` is
missing, the deploy job fails with a message saying so.

## 6. What the workflow does

`.github/workflows/deploy.yml`, jobs:

- **test**: Python 3.12, `make setup` (uv, with the uv package cache), `make test`. Runs on every trigger.
- **deploy** (needs test, only on `refs/heads/main`): one run at a time (`concurrency: cloud-run-deploy`, a newer
  queued run replaces an older queued one; a running deploy is never cancelled). Permissions `contents: read`,
  `id-token: write`. Steps: check variables, checkout with full history, `google-github-actions/auth` (WIF, impersonating
  `GCP_DEPLOY_SA`), `setup-gcloud`, `gcloud auth configure-docker`, then `.github/deploy/deploy.sh`.

Triggers: push to `main`; **Run workflow** (`workflow_dispatch`) with the option `deploy_all`.

**What gets deployed.** Each service and job carries the label `git_sha` of the commit it was deployed from. A target is
deployed when it does not exist yet, has no label, or one of its paths changed between that commit and the commit
being deployed (`git diff`). So a deploy that failed is retried on the next push, and `deploy_all` deploys everything.

| Target | Kind | Access | Paths that trigger it | Env / secrets |
|---|---|---|---|---|
| rules-api | service | private (`--no-allow-unauthenticated`) | `services/rules-api/`, `shared/`, `kb/`, `data/` | `DATA_BACKEND=firestore` |
| property-api | service | private | `services/property-api/`, `shared/` | secret `LINZ_API_KEY` |
| report-api | service | private, `--max-instances=2` | `services/report-api/`, `shared/` | `RULES_API_URL`, secret `GEMINI_API_KEY`, allowance env (section 8d) |
| web | service | **public** (`--allow-unauthenticated`) | `services/web/`, `shared/` | `RULES_API_URL`, `PROPERTY_API_URL`, `REPORT_API_URL` |
| kb-refresh | job | - | `jobs/kb-refresh/`, `kb/`, `shared/` | `DATA_BACKEND=firestore` |
| rates-refresh | job | - | `jobs/rates-refresh/`, `shared/` | `DATA_BACKEND=firestore`, `GEMINI_MODEL`, secret `GEMINI_API_KEY` |

The list lives in `.github/deploy/targets.json` and is deployed in that order (web after the private services). The
paths follow what each Dockerfile copies; `shared/` triggers every Python target. To add a service: add an entry
there, and if web calls it, a `url_env` line on web (plus a relay route in web's code).

Per deploy run:
1. Seed scripts when their inputs changed (section 3b).
2. For each target: `docker build -f <Dockerfile> .` on the runner, push `REGION-docker.pkg.dev/PROJECT/REPO/<name>:<git sha>`,
   then `gcloud run deploy` (services) or `gcloud run jobs deploy` (jobs). No `--service-account`: the default runtime
   service account is used. Service env vars and secrets use `--update-env-vars` / `--update-secrets`, so values set
   by hand (`LINZ_BASEMAPS_KEY`, `INCLUDE_DRAFT`, `--min-instances`) survive a deploy. Each `url_env` entry (web:
   `RULES_API_URL`, `PROPERTY_API_URL`, `REPORT_API_URL`; report-api: `RULES_API_URL`) is read from
   `gcloud run services describe ... --format='value(status.url)'`.
3. `roles/run.invoker` for the runtime service account on every private service (idempotent).
4. Smoke test: every deployed service and job must report `Ready=True` in `gcloud run ... describe`. For web, without a
   token: `GET /` and `GET /config.json` must return 200, and `GET /api/rates/latest` (through the relay to rules-api)
   must return 200 with banks whose oldest `captured_at` is at most 30 days old (a 502 `upstream_auth` means a missing
   `run.invoker` binding). When report-api, rules-api or web was deployed, `POST /api/report` (address 2100210, fixed
   inputs, real Gemini) must return 200 with at least one summary sentence and `POST /api/followup` (one English
   question) 200 with an `answer` array and a `language`. Each POST is sent once (no retry, not even after 503 busy).
   report-api's `X-Report-Mode` (`gemini`, `drafts`, `mock`) and `X-Report-Degraded-Reason` decide the Gemini check:
   `gemini` passes; `drafts` with `allowance_daily`, `allowance_event` or `disabled` is a warning; `drafts` with any
   other reason (`backend_error`, `model_error`, `prompt_too_large`, `deadline`) fails the deploy. While web's relay
   does not pass the headers on, only status and shape are checked and the summary says "Gemini path not verified
   (header not forwarded)". Latency, mode, reason and the estimated Gemini calls the smoke test used go to the job
   summary. Private services are not called;
   if you check them by hand use `/health`, not `/healthz` (Cloud Run reserves some paths ending in `z`,
   [known issues](https://docs.cloud.google.com/run/docs/known-issues)).
5. A table of what was deployed and why goes to the job summary.

Jobs are deployed, not executed; nothing in the workflow runs rates-refresh or kb-refresh.

## 7. First deploy, manual deploy and rollback

**First deploy** (step 10): GitHub -> Actions -> test-and-deploy -> Run workflow, branch `main`, tick `deploy_all`.
It seeds Firestore, creates all services and jobs, and adds the invoker bindings. A push to `main` would do the same
(nothing exists yet), but `deploy_all` makes it explicit. From the CLI:
`gh workflow run deploy.yml --repo $GITHUB_REPO --ref main -f deploy_all=true`.

**Expect temporary 403s on a brand-new project.** On the first deploy of a new project, testing right after the
`run.invoker` bindings were added gave web 403 several times in a row; it worked after about 3 minutes (IAM change
propagation, our reading). This only happened on the first deploy of a fresh project. If web or a private service
returns 403 right after the first deploy, wait a few minutes and retry before changing any IAM.

**Redeploy without a code change** (for example after rotating a secret): Run workflow with `deploy_all`.

**Rollback, preferred**: `git revert` the bad commit and push to `main`; the workflow redeploys the affected targets.

**Rollback, fast** (a service, without waiting for CI): send all traffic to the previous revision.

```bash
gcloud run revisions list --service=web --region=$REGION            # newest first
gcloud run services update-traffic web --region=$REGION --to-revisions=web-00012-abc=100
```

While traffic is pinned to a revision, a new deploy may not receive traffic (verify); after the fix is deployed run
`gcloud run services update-traffic web --region=$REGION --to-latest`.

A job has no revisions to route; point it back at an earlier image (tags are git shas):
`gcloud run jobs update kb-refresh --region=$REGION --image=$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/kb-refresh:<old sha>`.
The job keeps its `git_sha` label, so the workflow will only redeploy it when its paths change again (or on `deploy_all`).

## 8. rules-api (private)

Deployed by the workflow: private, `DATA_BACKEND=firestore`, runtime identity = default service account, which reads
Firestore (section 3). Only identities with `run.invoker` can call it; the workflow grants that to the runtime service
account so web can relay to it.

`--min-instances=1` keeps one instance warm so judges do not hit a cold start. Use it for the judging period
(Oct 19 - Nov 6). It is billed for the idle instance (CPU and memory, roughly a few dollars to low tens of dollars a
month for a small instance; check the Cloud Run pricing page for your region and your budget alert from section 12).
The workflow does not pass `--min-instances`, so set it by hand and it stays across deploys:

```bash
gcloud run services update rules-api --region=$REGION --min-instances=1    # judging period
gcloud run services update rules-api --region=$REGION --min-instances=0    # afterwards: scale to zero
```

Check as a user who has `run.invoker` on the service:

```bash
export RULES_API_URL=$(gcloud run services describe rules-api --region=$REGION --format='value(status.url)')
curl -s -H "Authorization: Bearer $(gcloud auth print-identity-token)" $RULES_API_URL/health
```

## 8b. property-api (private)

property-api uses no Firestore. Its only secret is the LINZ Data Service key (`LINZ_API_KEY` in Secret Manager,
section 4), mounted with `--update-secrets=LINZ_API_KEY=LINZ_API_KEY:latest`. Only the public web service may call it,
with an ID token as in section 11 (`audience` = the property-api URL); browsers never call it directly. web runs as the
default runtime service account, which the workflow grants `run.invoker` on property-api.

For the judging period add `--min-instances=1` by hand, as for rules-api in section 8.

Check as a user who has `run.invoker` on the service. The check uses `/v1/addresses` rather than `/healthz`
because Cloud Run reserves some paths ending in `z`
([known issues](https://docs.cloud.google.com/run/docs/known-issues)). The two address ids are the public
facilities used in the tests:

```bash
export PROPERTY_API_URL=$(gcloud run services describe property-api --region=$REGION --format='value(status.url)')
TOKEN=$(gcloud auth print-identity-token)
mkdir -p out
curl -s -o /dev/null -w '%{http_code}\n' "$PROPERTY_API_URL/v1/addresses?q=740%20sandringham"   # no token: 403
curl -s -H "Authorization: Bearer $TOKEN" "$PROPERTY_API_URL/v1/addresses?q=740%20sandringham"
curl -s -H "Authorization: Bearer $TOKEN" "$PROPERTY_API_URL/v1/property-context?address_id=2100210" -o out/ctx_2100210.json
curl -s -H "Authorization: Bearer $TOKEN" "$PROPERTY_API_URL/v1/property-context?address_id=1132249" -o out/ctx_1132249.json
```

Compare the two files with the same requests against a local run (`services/property-api/README.md`). Only
`generated_at` should differ; both must carry a New Zealand offset (`+13:00` or `+12:00`).

## 8c. web (public)

The only service that allows unauthenticated access. It serves the report page and relays the page's API calls
to property-api and rules-api as the default runtime service account, which therefore needs `run.invoker` on both
(added by the workflow). The workflow deploys it after the private services and sets `PROPERTY_API_URL` and
`RULES_API_URL` from their `status.url`.

`LINZ_BASEMAPS_KEY` is a plain environment variable, not a secret: the page hands it to browsers, which fetch the
map tiles themselves. Without it, or when LINZ refuses it, the map uses OpenStreetMap. The workflow does not set it;
set it once after the first deploy, and replace it the same way without a rebuild (deploys keep it, because the
workflow only adds env vars with `--update-env-vars`). The current key is a 90-day key issued on 2026-10-04:

```bash
gcloud run services update web --region=$REGION \
  --update-env-vars LINZ_BASEMAPS_KEY=$(grep '^LINZ_BASEMAPS_KEY=' .env | cut -d= -f2-)
```

For the judging period add `--min-instances=1` by hand, as for rules-api in section 8.

Check without a token. The relay answers only the paths listed in `services/web/README.md`:

```bash
export WEB_URL=$(gcloud run services describe web --region=$REGION --format='value(status.url)')
curl -s -o /dev/null -w '%{http_code}\n' $WEB_URL/                                   # 200, the page
curl -s "$WEB_URL/api/addresses?q=740%20sandringham"                                 # property-api through the relay
curl -s -o /dev/null -w '%{http_code}\n' $WEB_URL/api/rates/latest                   # 200, rules-api through the relay
curl -s -o /dev/null -w '%{http_code}\n' $WEB_URL/api/anything-else                  # 404
```

A 502 with `"code": "upstream_auth"` means a `run.invoker` binding for the runtime service account is missing on
rules-api or property-api; rerun the workflow with `deploy_all`, or add it with
`gcloud run services add-iam-policy-binding SERVICE --region=$REGION --member=serviceAccount:$RUNTIME_SA --role=roles/run.invoker`.

## 8d. report-api: Gemini allowance and generation slots (Codex F03)

report-api counts every outbound Gemini call (report rewrite, classify, answer, translation, value extraction, Google
Search) in Firestore **before** sending it, and refuses calls over the allowance: the report and follow-up then answer
from the program's checked drafts (200, warning `generation_degraded`, `X-Report-Mode: drafts`). Failed and timed-out
calls count; nothing is refunded. Code: `services/report-api/app/protection.py`.

Firestore documents (Native mode, default database; created on first use):

| Document | Fields |
|---|---|
| `protection/control` | `disabled` (bool, set by hand), `event_calls` (int, whole judging period), `event_searches` (int), `updated_at` |
| `protection/daily-YYYY-MM-DD` | `date`, `calls` (int; Search calls included), `searches` (int), `updated_at`. One per **UTC** day |

Env (set by the deploy from `targets.json`; defaults in code are the same):

| Env | Default | Meaning |
|---|---|---|
| `PROTECTION_BACKEND` | `firestore` on Cloud Run, else `memory` | `firestore`, `memory` (one process: local runs and tests) or `off` |
| `GEMINI_DAILY_CALLS` | `100` | Gemini calls per UTC day, all instances together (Search included) |
| `SEARCH_DAILY_CALLS` | `30` | Google Search calls per UTC day (inside the 100). Used up: only the search is skipped |
| `GEMINI_EVENT_CALLS` | `3000` | Gemini calls in total (`protection/control.event_calls`); daily resets do not renew it |
| `GENERATION_SLOTS` | `2` | /v1/report and /v1/followup requests at once per instance; more: 503 `busy`, `Retry-After: 5` |
| `GEMINI_MAX_OUTPUT_TOKENS` | `2048` | `max_output_tokens` on every call (thinking tokens included) |
| `GEMINI_INPUT_MAX_BYTES` | `32000` | Larger prompts (UTF-8 bytes) are not sent (drafts) and not counted |
| `REPORT_DEGRADED` | `false` | `true`: never call Gemini, drafts only (no Firestore read) |
| `PROTECTION_TIMEOUT_S` | `1` | Time for the allowance transaction; slower, unavailable or conflicting: the call is refused |
| `REPORT_CACHE_SIZE`, `REPORT_CACHE_TTL_S` | `100`, `600` | /v1/report result cache per instance (not in `targets.json`; 0 turns it off). Only Gemini reports without web-search inputs |

Firestore unavailable, missing permission or a slow transaction: **every call is refused** (drafts), never allowed.
`$RUNTIME_SA` needs `roles/datastore.user` (section 3), which it already has for rules-api and the jobs.

Operations:
- Stop all Gemini calls at once, surviving deploys: set `disabled: true` on `protection/control` in the Firestore
  console (back to `false` by hand). Preferred over `REPORT_DEGRADED`, because the deploy sets `REPORT_DEGRADED=false`
  from `targets.json` (`--update-env-vars`) and would undo a value set by hand; change it in `targets.json` too.
- Remaining today: `GEMINI_DAILY_CALLS` minus `calls` on today's UTC daily document. Raise a limit with
  `gcloud run services update report-api --region=$REGION --update-env-vars=GEMINI_DAILY_CALLS=150` and in `targets.json`.
- Logs: the completion record has `allowance` (`ok`, `daily_exhausted`, `event_exhausted`, `search_exhausted`,
  `disabled`, `backend_error`, `degraded`, `prompt_too_large`) and `busy`; refused calls log `allowance_refused`,
  busy refusals `busy_rejected`, Firestore failures `allowance_backend_error`.
- The deploy smoke test treats a drafts answer with `X-Report-Degraded-Reason` `allowance_daily`, `allowance_event` or
  `disabled` as a warning (the allowance may be used up for the day), any other reason as a failure (section 4 above).

## 9. Cloud Run jobs

Deployed (created or updated) by the workflow, never executed by it:

- **rates-refresh**: `--memory=2Gi --cpu=1 --max-retries=1 --task-timeout=15m`, `DATA_BACKEND=firestore`,
  `GEMINI_MODEL=gemini-3.5-flash-lite`, secret `GEMINI_API_KEY`. Created but **not scheduled** (section 10). 2Gi is only
  needed if a bank is switched to `render: browser`.
- **kb-refresh**: `--memory=512Mi --max-retries=1 --task-timeout=15m`, `DATA_BACKEND=firestore`. It needs no Gemini
  secret (only `DATA_BACKEND` is read) and no browser. Scheduled weekly (section 10).

Smoke test once by hand after the first deploy, then check the Firestore `job_runs` collection (with no bank marked
`tos_status=allowed`, rates-refresh fetches nothing, writes no snapshot and records `skipped_by_policy` in `job_runs`):

```bash
gcloud run jobs execute rates-refresh --region=$REGION --wait
```

## 10. Cloud Scheduler (Pacific/Auckland)

One-time, after the first deploy created the jobs. Cloud Scheduler calls the Cloud Run Admin API with an OAuth token
for a service account. Google's guide uses the Compute Engine default service account for this
(`--oauth-service-account-email PROJECT_NUMBER-compute@developer.gserviceaccount.com`,
https://docs.cloud.google.com/run/docs/execute/jobs-on-schedule), so this repo uses the same default runtime account
instead of a separate scheduler account. It needs `roles/run.invoker` on the job (that role includes `run.jobs.run`),
and the person creating the scheduler job needs `iam.serviceAccounts.actAs` on it (Owner has it).

```bash
for JOB in kb-refresh; do   # add rates-refresh here when its schedule is enabled
  gcloud run jobs add-iam-policy-binding $JOB --region=$REGION \
    --member=serviceAccount:$RUNTIME_SA --role=roles/run.invoker
done

# rates: DISABLED. The banks' website terms do not clearly allow automated access, so rates are updated by hand
# (see RATES_UPDATE.md). Enable only when a bank in banks.yaml is marked tos_status: allowed.
# gcloud run jobs add-iam-policy-binding rates-refresh --region=$REGION \
#   --member=serviceAccount:$RUNTIME_SA --role=roles/run.invoker
# gcloud scheduler jobs create http rates-refresh-daily \
#   --location=$REGION --schedule="0 6 * * *" --time-zone="Pacific/Auckland" \
#   --http-method=POST \
#   --uri="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/rates-refresh:run" \
#   --oauth-service-account-email=$RUNTIME_SA

# kb: every Monday 06:00
gcloud scheduler jobs create http kb-refresh-weekly \
  --location=$REGION --schedule="0 6 * * 1" --time-zone="Pacific/Auckland" \
  --http-method=POST \
  --uri="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/kb-refresh:run" \
  --oauth-service-account-email=$RUNTIME_SA
```

## 11. Calling private services (service-to-service auth)

rules-api and property-api reject unauthenticated requests. The caller (web) runs on Cloud Run as the default runtime
service account, fetches an ID token from the metadata server with `audience` = the private service's URL, and sends
it as a Bearer token. The calling identity needs `run.invoker` on the target (the workflow grants it).

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

Cache the token for about 50 minutes (it lives 1 hour). Locally, just
`curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" $RULES_API_URL/...` as a user who has
`run.invoker` (Owner has it).

## 12. Budget alert

```bash
BILLING=$(gcloud billing projects describe $PROJECT_ID --format='value(billingAccountName)' | sed 's|.*/||')
gcloud billing budgets create --billing-account=$BILLING \
  --display-name="nz-buyer-report monthly" --budget-amount=50NZD \
  --filter-projects=projects/$PROJECT_ID \
  --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0 \
  --credit-types-treatment=exclude-all-credits
```

`--credit-types-treatment=exclude-all-credits` is required on a trial account: by default the budget counts spend net of
credits, so the free-trial credit offsets the cost to 0 and the alert never fires (verify the flag with
`gcloud billing budgets create --help`).

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
