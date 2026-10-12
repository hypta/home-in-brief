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
  # POST JSON once: no retry of any kind, also not after 503 busy (each call spends Gemini quota). Sets c (status),
  # secs (latency), mode (X-Report-Mode) and reason (X-Report-Degraded-Reason; only on a drafts fallback).
  smoke_post() {
    local out
    out=$(curl -s -o "$WORK/body" -D "$WORK/headers" -w '%{http_code} %{time_total}' --max-time 60 \
      -H 'content-type: application/json' --data "$2" "$url$1" || true)
    out=${out:-000 0}; c=${out% *}; secs=${out#* }
    hdr() { tr -d '\r' < "$WORK/headers" 2>/dev/null | awk -F': ' -v h="$1" 'tolower($1) == h {print $2}' | tail -n1; }
    mode=$(hdr x-report-mode); reason=$(hdr x-report-degraded-reason)
  }
  # Follow-up shape (shared/b_common/report_schemas.py, FollowupResponse): language is a non-empty string and either
  # answer has at least one sentence ({"text": non-empty string, ...}; a bare non-empty string is accepted too) or
  # web.text (a search answer) is a non-empty string.
  FOLLOWUP_SHAPE_JQ='def nonempty: type == "string" and (gsub("\\s"; "") | length) > 0;
    if (.language | nonempty)
       and (((.answer // []) | type) == "array" and ([.answer[]? | if type == "object" then .text else . end | select(nonempty)] | length) > 0
            or ((.web // {}) | type) == "object" and ((.web // {}).text | nonempty))
    then "ok" else "bad" end'
  # Gemini path check from the two headers, which web passes on. Sets verdict: ok | unverified | nohdr | warn | fail.
  #   no X-Report-Mode (the header did not arrive): nohdr, an anomaly because web forwards it (PASSED_ON in
  #   services/web/app/main.py); only status and sentences are checked;
  #   gemini: ok; drafts with reason allowance_daily / allowance_event / disabled: warn (refused on purpose);
  #   drafts with any other reason (backend_error, model_error, prompt_too_large, deadline): fail;
  #   drafts without a reason header: unverified if the body says generation_degraded (the reason is unknown, so
  #   neither a pass nor an allowance warn), else fail (report) or ok (follow-up: a fixed refusal or program
  #   sentences are drafts without any failure); mock or anything else: fail.
  gemini_verdict() {
    local what=$1
    if [ -z "$mode" ]; then verdict=nohdr
    elif [ "$mode" = gemini ]; then verdict=ok
    elif [ "$mode" = drafts ]; then
      case "$reason" in
        allowance_daily|allowance_event|disabled) verdict=warn ;;
        "") if [ "$(jq -r '(.warnings // []) | index("generation_degraded") != null' "$WORK/body" 2>/dev/null)" = true ]; then
              verdict=unverified
            elif [ "$what" = followup ]; then verdict=ok
            else verdict=fail; fi ;;
        *) verdict=fail ;;
      esac
    else verdict=fail; fi
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
  # a target on that path was deployed, so other deploys cost no Gemini calls. Each POST is sent once (no retry, not
  # even after 503 busy). web passes X-Report-Mode / X-Report-Degraded-Reason on; if X-Report-Mode is missing (it should not
  # be: warning, web is not forwarding it), the Gemini path is reported as not verified and only 200 and the response shape are required.
  if grep -qxE 'report-api|rules-api|web' <<<"$TO_DEPLOY"; then
    calls=0; calls_note=""
    smoke_post /api/report '{"address_id": 2100210, "user_inputs": {"deposit": 34000, "household_income": 130000}}'
    n=$(jq -r '.summary | length' "$WORK/body" 2>/dev/null || echo 0)
    echo "- $PUBLIC POST /api/report: $c, ${secs}s, $n sentences, model $(jq -r '.model // "?"' "$WORK/body" 2>/dev/null), mode ${mode:-missing}${reason:+, degraded reason $reason}" >> "$SUMMARY"
    gemini_verdict report
    if [ "$c" != 200 ] || [ "${n:-0}" -lt 1 ]; then echo "::error::report returned $c with ${n:-0} sentences"; fail=1
    else
      case "$verdict" in
        nohdr) echo "::warning::report response has no X-Report-Mode header, although web forwards it; Gemini path not checked"
               echo "  - report: **X-Report-Mode header missing** (web should forward it): Gemini path not checked" >> "$SUMMARY" ;;
        unverified) echo "  - report: Gemini path not verified (mode $mode, no degraded reason header)" >> "$SUMMARY" ;;
        warn) echo "::warning::report answered from drafts (${reason:-generation_degraded}: Gemini allowance used up or switched off); Gemini path not checked"
              echo "  - report-api refused the Gemini call on purpose (${reason:-generation_degraded}): Gemini path **not checked** by this deploy" >> "$SUMMARY" ;;
        fail) echo "::error::report mode is ${mode}${reason:+ (degraded reason $reason)}, not gemini"; fail=1 ;;
      esac
    fi
    # Gemini calls this POST reserved (estimate: report-api does not return the count): a cache miss in mode
    # gemini is 1 (2 when the first answer was unusable), a cache hit 0; drafts after a refusal 0; unknown mode 0-2.
    case "$mode:$reason" in
      gemini:*) calls=$((calls + 1)); calls_note="report 1 (0 if cached, 2 after a retry)" ;;
      drafts:allowance_*|drafts:disabled|drafts:prompt_too_large) calls_note="report 0" ;;
      drafts:*) calls=$((calls + 1)); calls_note="report 0-2 (failed call)" ;;
      *) calls=$((calls + 1)); calls_note="report 0-2 (mode header missing)" ;;
    esac
    smoke_post /api/followup '{"address_id": 2100210, "user_inputs": {"deposit": 34000, "household_income": 130000}, "question": "What is the flood risk for this address?", "history": []}'
    echo "- $PUBLIC POST /api/followup: $c, ${secs}s, mode ${mode:-missing}${reason:+, degraded reason $reason}" >> "$SUMMARY"
    # shape: language is a non-empty string; answer has a non-empty sentence or web.text is a non-empty string
    shape=$(jq -r "$FOLLOWUP_SHAPE_JQ" "$WORK/body" 2>/dev/null || echo bad)
    gemini_verdict followup
    if [ "$c" != 200 ]; then echo "::error::followup returned $c: $(head -c 300 "$WORK/body")"; fail=1
    elif [ "$shape" != ok ]; then echo "::error::followup response has no non-empty answer sentence or web text, or no language: $(head -c 300 "$WORK/body")"; fail=1
    else
      case "$verdict" in
        nohdr) echo "::warning::follow-up response has no X-Report-Mode header, although web forwards it; Gemini path not checked"
               echo "  - follow-up: **X-Report-Mode header missing** (web should forward it): Gemini path not checked" >> "$SUMMARY" ;;
        unverified) echo "  - follow-up: Gemini path not verified (mode $mode, no degraded reason header)" >> "$SUMMARY" ;;
        warn) echo "::warning::follow-up answered from fixed sentences (${reason:-generation_degraded}); Gemini path not checked"
              echo "  - follow-up: Gemini refused on purpose (${reason:-generation_degraded}): Gemini path **not checked**" >> "$SUMMARY" ;;
        fail) echo "::error::follow-up mode is ${mode}${reason:+ (degraded reason $reason)}"; fail=1 ;;
      esac
    fi
    # an English flood question: classify + answer (a retry or a refused call changes it by one)
    case "$mode:$reason" in
      gemini:*) calls=$((calls + 2)); calls_note="$calls_note, follow-up 2 (3 after a retry)" ;;
      drafts:allowance_*|drafts:disabled|drafts:prompt_too_large) calls_note="$calls_note, follow-up 0-1" ;;
      *) calls=$((calls + 2)); calls_note="$calls_note, follow-up 1-3 (estimate)" ;;
    esac
    echo "- Gemini allowance used by this smoke test (estimated, from the response headers): about $calls call(s): $calls_note; no Search call" >> "$SUMMARY"
  fi
fi
exit $fail
