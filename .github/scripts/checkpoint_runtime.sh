#!/usr/bin/env bash
set -euo pipefail

branch="${RUNTIME_CHECKPOINT_BRANCH:-master}"
max_attempts="${RUNTIME_CHECKPOINT_MAX_ATTEMPTS:-3}"

if [[ -n "${RUNTIME_CHECKPOINT_FILES:-}" ]]; then
  read -r -a runtime_files <<< "${RUNTIME_CHECKPOINT_FILES}"
else
  runtime_files=(
    data/state.json
    data/outbid_stats.json
    data/active_bid_prices.json
    data/active_bid_prices.csv
    data/temp_dead_listings.json
    data/branch_split_candidates.json
    config/listings.py
    config/listings_hot.py
    config/listings_mid.py
    config/listings_sos.py
    config/temporary_bids.py
    config/managed_overlays.py
  )
fi

git config --local user.email "github-actions[bot]@users.noreply.github.com"
git config --local user.name "github-actions[bot]"

mkdir -p data
touch data/state.json
git add -A -- "${runtime_files[@]}"

if git diff --staged --quiet; then
  echo "No runtime changes to save."
  exit 0
fi

# Commit first so the exact encrypted checkpoint remains addressable even if
# the remote branch advances while this job is running.
git commit -m "Auto-update: market prices and bid sync [skip ci]"
checkpoint_sha="$(git rev-parse HEAD)"

checkpoint_files="$(mktemp)"
upstream_files="$(mktemp)"
trap 'rm -f "$checkpoint_files" "$upstream_files"' EXIT
git diff-tree --no-commit-id --name-only -r "$checkpoint_sha" \
  | sort -u > "$checkpoint_files"

published=false
recovery_reason=""

for ((attempt = 1; attempt <= max_attempts; attempt++)); do
  if ! git fetch --quiet origin "$branch"; then
    recovery_reason="GitHub was unreachable during checkpoint fetch"
    echo "Runtime checkpoint fetch failed; retrying ($attempt/$max_attempts)."
    sleep "$attempt"
    continue
  fi
  remote_sha="$(git rev-parse "refs/remotes/origin/$branch")"
  checkpoint_parent="$(git rev-parse HEAD^)"

  if [[ "$checkpoint_parent" != "$remote_sha" ]]; then
    if ! git merge-base --is-ancestor "$checkpoint_parent" "$remote_sha"; then
      recovery_reason="remote history was rewritten while the monitor was running"
      break
    fi

    git diff --name-only "$checkpoint_parent" "$remote_sha" \
      | sort -u > "$upstream_files"
    overlap="$(comm -12 "$checkpoint_files" "$upstream_files" | tr '\n' ' ')"
    if [[ -n "$overlap" ]]; then
      recovery_reason="remote changed the same runtime file(s): $overlap"
      break
    fi

    if ! git rebase "$remote_sha"; then
      git rebase --abort || true
      recovery_reason="a disjoint runtime checkpoint rebase failed unexpectedly"
      break
    fi
    checkpoint_sha="$(git rev-parse HEAD)"
  fi

  if git push origin "HEAD:$branch"; then
    published=true
    break
  fi

  echo "Runtime checkpoint push raced with another update; retrying ($attempt/$max_attempts)."
  sleep "$attempt"
done

if [[ "$published" == "true" ]]; then
  echo "Runtime checkpoint published to $branch."
  exit 0
fi

# Never ask Git to merge overlapping encrypted blobs. Preserve the exact
# checkpoint on an encrypted recovery branch and let the next scheduled run
# reconcile live orders from a fresh complete API snapshot.
recovery_ref="runtime-recovery/${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}"
recovery_published=false
for ((attempt = 1; attempt <= max_attempts; attempt++)); do
  if git push origin "$checkpoint_sha:refs/heads/$recovery_ref"; then
    recovery_published=true
    break
  fi
  echo "Recovery-branch push failed; retrying ($attempt/$max_attempts)."
  sleep "$attempt"
done

if [[ "$recovery_published" != "true" ]]; then
  echo "::error title=Runtime checkpoint unavailable::The runtime checkpoint could not be pushed to master or its recovery branch."
  exit 1
fi

message="Runtime checkpoint was preserved on $recovery_ref because ${recovery_reason:-the branch kept advancing}. The next scheduled run will reconcile live orders."
echo "::warning title=Runtime checkpoint deferred::$message"
if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  printf '### Runtime checkpoint deferred\n\n%s\n' "$message" >> "$GITHUB_STEP_SUMMARY"
fi
if [[ -f /tmp/bot_output.txt ]]; then
  printf '\n[CHECKPOINT] %s\n' "$message" >> /tmp/bot_output.txt
fi
