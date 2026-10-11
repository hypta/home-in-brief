#!/usr/bin/env bash
# Build, push and deploy the targets in .github/deploy/targets.json to Cloud Run.
# Run by .github/workflows/deploy.yml after `make test` passed and gcloud is authenticated.
#
# Env: PROJECT_ID, REGION, AR_REPO, SHA (full git sha of the commit to deploy), DEPLOY_ALL (true|false).
#      DRY_RUN=1 prints the build/push/deploy/seed/IAM commands instead of running them (gcloud describe still runs).
# Each deployed service and job carries the label git_sha=<sha>; the next run deploys a target only when one of
# its paths changed since that sha, so a failed deploy is retried on the next push.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
CONFIG=.github/deploy/targets.json
IMG="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}"
SUMMARY=${GITHUB_STEP_SUMMARY:-/dev/stdout}
DEPLOY_ALL=${DEPLOY_ALL:-false}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

run() { if [ "${DRY_RUN:-}" = 1 ]; then echo "+ $*"; else "$@"; fi; }
cfg() { jq -r --arg n "$1" ".targets[] | select(.name == \$n) | $2" "$CONFIG"; }
G=(--region="$REGION" --project="$PROJECT_ID")

# JSON description of a service or job, or nothing if it does not exist.
describe() {
  if [ "$1" = service ]; then gcloud run services describe "$2" "${G[@]}" --format=json 2>/dev/null || true
  else gcloud run jobs describe "$2" "${G[@]}" --format=json 2>/dev/null || true; fi
}

# True if any of the given paths changed between commit $1 and $SHA (or $1 is empty or not in history).
changed_since() {
  local base=$1; shift
  [ -n "$base" ] || return 0
  git cat-file -e "${base}^{commit}" 2>/dev/null || return 0
  ! git diff --quiet "$base" "$SHA" -- "$@"
}

# ---- plan ------------------------------------------------------------------------------------------------------
NAMES=$(jq -r '.targets[].name' "$CONFIG")
: > "$WORK/plan"
for name in $NAMES; do
  kind=$(cfg "$name" .kind)
  describe "$kind" "$name" > "$WORK/$name.json"
  base=""
  [ -s "$WORK/$name.json" ] && base=$(jq -r '.metadata.labels.git_sha // empty' "$WORK/$name.json")
  echo "$base" > "$WORK/$name.base"
  # shellcheck disable=SC2046
  if [ "$DEPLOY_ALL" = true ]; then reason="deploy_all"
  elif [ ! -s "$WORK/$name.json" ]; then reason="not deployed yet"
  elif [ -z "$base" ]; then reason="no git_sha label"
  elif changed_since "$base" $(cfg "$name" '.paths[]'); then reason="changed since ${base:0:7}"
  else reason=""; fi
  printf '%s\t%s\t%s\n' "$name" "$kind" "$reason" >> "$WORK/plan"
done

SEED_BASE_NAME=$(jq -r '.seeds.baseline' "$CONFIG")
SEED_BASE=$(cat "$WORK/$SEED_BASE_NAME.base")
SEEDS=""
for i in $(jq -r '.seeds.scripts | keys[]' "$CONFIG"); do
  script=$(jq -r ".seeds.scripts[$i].script" "$CONFIG")
  # shellcheck disable=SC2046
  if [ "$DEPLOY_ALL" = true ] || [ -z "$SEED_BASE" ] || changed_since "$SEED_BASE" $(jq -r ".seeds.scripts[$i].paths[]" "$CONFIG"); then
    SEEDS="$SEEDS $script"
  fi
done

{
  echo "### Cloud Run deploy of \`${SHA:0:7}\` to \`$PROJECT_ID\` ($REGION)"
  echo
  echo "| Target | Kind | Action |"
  echo "|---|---|---|"
  while IFS=$'\t' read -r name kind reason; do
    echo "| $name | $kind | ${reason:-unchanged, skipped} |"
  done < "$WORK/plan"
  echo
  echo "Firestore seed scripts: ${SEEDS:- none}"
} >> "$SUMMARY"

TO_DEPLOY=$(awk -F'\t' '$3 != "" {print $1}' "$WORK/plan")
if [ -z "$TO_DEPLOY" ] && [ -z "$SEEDS" ]; then
  echo "Nothing to deploy." >> "$SUMMARY"
  exit 0
fi

# ---- seed Firestore (before the new rules-api revision goes live) -----------------------------------------------
if [ -n "$SEEDS" ]; then
  [ -x .venv/bin/python ] || run make setup
  for script in $SEEDS; do
    run env -u FIRESTORE_EMULATOR_HOST GOOGLE_CLOUD_PROJECT="$PROJECT_ID" .venv/bin/python "$script"
  done
fi

# ---- build, push, deploy ----------------------------------------------------------------------------------------
service_url() { gcloud run services describe "$1" "${G[@]}" --format='value(status.url)' 2>/dev/null || true; }
kv() { jq -r --arg n "$1" ".targets[] | select(.name == \$n) | (.$2 // {}) | to_entries | map(\"\(.key)=\(.value)\") | join(\",\")" "$CONFIG"; }

for name in $TO_DEPLOY; do
  kind=$(cfg "$name" .kind)
  image="$IMG/$name:$SHA"
  run docker build -f "$(cfg "$name" .dockerfile)" -t "$image" .
  run docker push "$image"

  args=()
  env_vars=$(kv "$name" env)
  secrets=$(kv "$name" secrets)
  for flag in $(cfg "$name" '(.flags // [])[]'); do args+=("$flag"); done

  if [ "$kind" = service ]; then
    for var in $(cfg "$name" '(.url_env // {}) | keys[]'); do
      target=$(cfg "$name" ".url_env[\"$var\"]")
      url=$(service_url "$target")
      [ -n "$url" ] || { echo "::error::$name needs $var but service $target does not exist"; exit 1; }
      env_vars="${env_vars:+$env_vars,}$var=$url"
    done
    [ -n "$env_vars" ] && args+=(--update-env-vars="$env_vars")
    [ -n "$secrets" ] && args+=(--update-secrets="$secrets")
    if [ "$(cfg "$name" .public)" = true ]; then args+=(--allow-unauthenticated); else args+=(--no-allow-unauthenticated); fi
    run gcloud run deploy "$name" --image="$image" "${G[@]}" --update-labels=git_sha="$SHA" --quiet ${args[@]+"${args[@]}"}
  else
    [ -n "$env_vars" ] && args+=(--set-env-vars="$env_vars")
    [ -n "$secrets" ] && args+=(--set-secrets="$secrets")
    run gcloud run jobs deploy "$name" --image="$image" "${G[@]}" --labels=git_sha="$SHA" --quiet ${args[@]+"${args[@]}"}
  fi
done

# ---- run.invoker for the runtime identity on every private service --------------------------------------------
# The runtime identity is the Cloud Run default service account (no --service-account is passed). Read it from the
# web service; fall back to <project number>-compute@developer.gserviceaccount.com (metadata.namespace is the
# project number in the Cloud Run v1 resource).
PUBLIC=$(jq -r '.targets[] | select(.kind == "service" and .public == true) | .name' "$CONFIG" | head -n1)
PRIVATE=$(jq -r '.targets[] | select(.kind == "service" and .public != true) | .name' "$CONFIG")
desc=$(describe service "$PUBLIC")
for n in $PRIVATE; do [ -n "$desc" ] && break; desc=$(describe service "$n"); done
RUNTIME_SA=""
if [ -n "$desc" ]; then
  RUNTIME_SA=$(jq -r '.spec.template.spec.serviceAccountName // empty' <<<"$desc")
  [ -n "$RUNTIME_SA" ] || RUNTIME_SA="$(jq -r '.metadata.namespace // empty' <<<"$desc")-compute@developer.gserviceaccount.com"
fi
if [ -n "$RUNTIME_SA" ] && [ "$RUNTIME_SA" != "-compute@developer.gserviceaccount.com" ]; then
  for n in $PRIVATE; do
    [ -n "$(describe service "$n")" ] || continue
    run gcloud run services add-iam-policy-binding "$n" "${G[@]}" --quiet \
      --member="serviceAccount:$RUNTIME_SA" --role=roles/run.invoker --format=none
  done
  echo "run.invoker on private services: \`$RUNTIME_SA\`" >> "$SUMMARY"
else
  echo "::warning::could not determine the runtime service account; run.invoker bindings not checked"
fi

# ---- smoke test -------------------------------------------------------------------------------------------------
[ "${DRY_RUN:-}" = 1 ] && { echo "Dry run: smoke test skipped." >> "$SUMMARY"; exit 0; }
fail=0
for name in $TO_DEPLOY; do
  kind=$(cfg "$name" .kind)
  desc=$(describe "$kind" "$name")
  ready=$(jq -r '[.status.conditions[]? | select(.type == "Ready") | .status][0] // "missing"' <<<"$desc")
  if [ "$ready" = True ]; then echo "- $name: Ready" >> "$SUMMARY"
  else echo "- $name: **Ready=$ready**" >> "$SUMMARY"; echo "::error::$name is not Ready ($ready)"; fail=1; fi
done

if [ -n "$PUBLIC" ] && [ -n "$(describe service "$PUBLIC")" ]; then
  url=$(service_url "$PUBLIC")
  code() { curl -s -o "$WORK/body" -w '%{http_code}' --max-time 30 --retry 3 --retry-all-errors "$url$1" || true; }
  # ISO 8601 time (e.g. 2026-10-10T20:20:00+13:00) -> epoch seconds (GNU date on the runner; BSD date locally)
  rates_epoch() { date -d "$1" +%s 2>/dev/null || date -j -f '%Y-%m-%dT%H:%M:%S%z' "${1%:*}${1##*:}" +%s; }
  # POST JSON once (no retry: each call spends Gemini quota); sets c (status), secs (latency), mode (X-Report-Mode)
  smoke_post() {
    local out
    out=$(curl -s -o "$WORK/body" -D "$WORK/headers" -w '%{http_code} %{time_total}' --max-time 60 \
      -H 'content-type: application/json' --data "$2" "$url$1" || true)
    out=${out:-000 0}; c=${out% *}; secs=${out#* }
    mode=$(tr -d '\r' < "$WORK/headers" 2>/dev/null | awk -F': ' 'tolower($1) == "x-report-mode" {print $2}' | tail -n1)
  }
  for path in / /config.json; do
    c=$(code "$path")
    echo "- $PUBLIC GET $path: $c" >> "$SUMMARY"
    [ "$c" = 200 ] || { echo "::error::$PUBLIC GET $path returned $c"; fail=1; }
  done
  # Through the relay to rules-api: 200 with banks, the oldest captured_at at most 30 days old (rules-api's
  # STALE_DAYS). A 502 upstream_auth means a missing invoker binding.
  c=$(code /api/rates/latest)
  oldest=$(jq -r '[.banks[]?.captured_at // empty] | min // empty' "$WORK/body" 2>/dev/null || true)
  age=$(( $(date +%s) - $(rates_epoch "$oldest" 2>/dev/null || echo 0) ))
  echo "- $PUBLIC GET /api/rates/latest (relay to rules-api): $c, oldest captured_at ${oldest:-none}" >> "$SUMMARY"
  if [ "$c" != 200 ] || [ -z "$oldest" ]; then
    echo "::error::rates returned $c without a snapshot: $(head -c 300 "$WORK/body")"; fail=1
  elif [ "$age" -gt $(( 30 * 86400 )) ]; then
    echo "::error::rates snapshot is older than 30 days ($oldest)"; fail=1
  fi

  # Report and follow-up through web with a public address (740 Sandringham Road Extension), real Gemini. Only when
  # a target on that path was deployed, so other deploys cost no Gemini calls. web's relay does not pass report-api's
  # X-Report-Mode header on (2026-10-11): when it is missing, 200 and at least one summary sentence are required.
  if grep -qxE 'report-api|rules-api|web' <<<"$TO_DEPLOY"; then
    smoke_post /api/report '{"address_id": 2100210, "user_inputs": {"deposit": 34000, "household_income": 130000}}'
    n=$(jq -r '.summary | length' "$WORK/body" 2>/dev/null || echo 0)
    echo "- $PUBLIC POST /api/report: $c, ${secs}s, $n sentences, model $(jq -r '.model // "?"' "$WORK/body" 2>/dev/null), mode ${mode:-header not passed}" >> "$SUMMARY"
    # drafts with the warning generation_degraded: report-api refused the Gemini call on purpose (daily or event
    # allowance used up, protection/control.disabled, REPORT_DEGRADED; Codex F03). A warning, not a failed deploy:
    # the smoke test itself may be the call that finds the day's allowance used up. Any other drafts fail.
    degraded=$(jq -r '(.warnings // []) | index("generation_degraded") != null' "$WORK/body" 2>/dev/null || echo false)
    if [ "$c" != 200 ] || [ "${n:-0}" -lt 1 ]; then echo "::error::report returned $c with ${n:-0} sentences"; fail=1
    elif [ "$mode" = drafts ] && [ "$degraded" = true ]; then
      echo "::warning::report answered from drafts (generation_degraded: Gemini allowance used up or switched off); Gemini path not checked"
      echo "  - report-api refused the Gemini call (generation_degraded): Gemini path **not checked** by this deploy" >> "$SUMMARY"
    elif [ -n "$mode" ] && [ "$mode" != gemini ]; then echo "::error::report mode is $mode, not gemini"; fail=1; fi
    smoke_post /api/followup '{"address_id": 2100210, "user_inputs": {"deposit": 34000, "household_income": 130000}, "question": "What is the flood risk for this address?", "history": []}'
    echo "- $PUBLIC POST /api/followup: $c, ${secs}s, mode ${mode:-header not passed}" >> "$SUMMARY"
    [ "$c" = 200 ] || { echo "::error::followup returned $c: $(head -c 300 "$WORK/body")"; fail=1; }
  fi
fi
exit $fail
