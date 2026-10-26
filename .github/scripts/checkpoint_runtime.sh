#!/usr/bin/env bash
set -euo pipefail

branch="${RUNTIME_CHECKPOINT_BRANCH:-master}"
max_attempts="${RUNTIME_CHECKPOINT_MAX_ATTEMPTS:-3}"
git_timeout_seconds="${RUNTIME_GIT_TIMEOUT_SECONDS:-90}"
private_log="${RUNTIME_PRIVATE_LOG:-/tmp/bot_output.txt}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export GIT_TERMINAL_PROMPT=0

retry_pause() {
  # Brief GitHub server outages can outlast consecutive one-second retries.
  # Bounded exponential pauses still fit the workflow's checkpoint timeout.
  local delay=30
  if ((attempt < 4)); then delay=$((5 * (2 ** (attempt - 1)))); fi
  sleep "$delay"
}

report_failure() {
  local message="$1"
  echo "::error title=Runtime checkpoint unavailable::$message"
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf '### Runtime checkpoint failed\n\n%s\n' "$message" >> "$GITHUB_STEP_SUMMARY"
  fi
  if [[ -f "$private_log" ]]; then
    printf '\n[CHECKPOINT FAILED] %s\n' "$message" >> "$private_log"
  fi
  exit 1
}

report_deferred() {
  # A checkpoint that lost the publish race is not a run failure: each
  # invocation reconciles live state from the newest branch tip at startup,
  # so the discarded snapshot is rebuilt on the next scheduled run. Surface
  # the deferral as a warning instead of a red run.
  local message="$1"
  echo "::warning title=Runtime checkpoint deferred::$message"
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf '### Runtime checkpoint deferred\n\n%s\n' "$message" >> "$GITHUB_STEP_SUMMARY"
  fi
  if [[ -f "$private_log" ]]; then
    printf '\n[CHECKPOINT DEFERRED] %s\n' "$message" >> "$private_log"
  fi
  exit 0
}

if [[ ! "$max_attempts" =~ ^[1-9][0-9]*$ ]]; then
  report_failure "RUNTIME_CHECKPOINT_MAX_ATTEMPTS must be a positive integer."
fi
if [[ ! "$git_timeout_seconds" =~ ^[1-9][0-9]*$ ]] || ((git_timeout_seconds > 600)); then
  report_failure "RUNTIME_GIT_TIMEOUT_SECONDS must be an integer from 1 through 600."
fi

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

# A monitor process imports configuration before it performs runtime tier
# moves. Refuse to checkpoint any malformed generated module, otherwise a
# successful current run can poison the next invocation.
if ! python "$script_dir/validate_runtime_config.py" >> "$private_log" 2>&1; then
  report_failure "Runtime checkpoint refused because generated configuration or persisted state is invalid. No malformed files were published."
fi

git config --local user.email "github-actions[bot]@users.noreply.github.com" \
  || report_failure "Could not configure the checkpoint Git identity."
git config --local user.name "github-actions[bot]" \
  || report_failure "Could not configure the checkpoint Git identity."

git add -A -- "${runtime_files[@]}" \
  || report_failure "Could not stage the allowlisted runtime files."

if git diff --staged --quiet; then
  echo "No runtime changes to save."
  exit 0
fi

# Commit first so the exact encrypted checkpoint remains addressable even if
# the remote branch advances while this job is running.
git commit -m "Auto-update: market prices and bid sync [skip ci]" \
  || report_failure "Could not create the local runtime checkpoint commit."
checkpoint_sha="$(git rev-parse HEAD)"
checkpoint_parent="$(git rev-parse HEAD^)"

published=false
failure_reason=""
# Set only when the branch tip was positively observed past this run's base:
# the distinguishing signature of a publish race (overlapping scheduled runs
# and live bots push [skip ci] checkpoints around the clock).
remote_moved=false

for ((attempt = 1; attempt <= max_attempts; attempt++)); do
  if ! timeout --signal=TERM --kill-after=5s "${git_timeout_seconds}s" git fetch --quiet origin "$branch"; then
    failure_reason="GitHub was unreachable during checkpoint fetch"
    echo "Runtime checkpoint fetch failed; retrying ($attempt/$max_attempts)."
    retry_pause
    continue
  fi
  remote_sha="$(git rev-parse "refs/remotes/origin/$branch")"

  # A lost push response may leave the remote at our exact checkpoint. Treat
  # that as success without attempting another write.
  if [[ "$remote_sha" == "$checkpoint_sha" ]]; then
    published=true
    break
  fi

  if [[ "$remote_sha" != "$checkpoint_parent" ]]; then
    remote_moved=true
    # origin/$branch advanced while the monitor ran. Replay this run's single
    # checkpoint commit onto the newer tip: unrelated updates (human code
    # pushes, disjoint state files) accept the replay cleanly. A conflicting
    # replay means a parallel runtime checkpoint already wrote newer state to
    # the same generated files — the newer remote tip wins and this run's
    # snapshot defers to the next invocation's reconciliation.
    if git rebase --autostash "origin/$branch"; then
      checkpoint_sha="$(git rev-parse HEAD)"
      checkpoint_parent="$(git rev-parse HEAD^)"
      echo "Runtime checkpoint rebased onto advanced origin/$branch."
    else
      git rebase --abort || true
      failure_reason="origin/$branch advanced while the monitor was running and the checkpoint could not be replayed onto the newer tip"
      break
    fi
  fi

  if timeout --signal=TERM --kill-after=5s "${git_timeout_seconds}s" git push origin "HEAD:$branch"; then
    published=true
    break
  fi

  # A rejection here is ambiguous: the branch may have moved again (race) or
  # the transport may have failed (auth, network). The next attempt's fetch —
  # or the final verification read — classifies it.
  failure_reason="the checkpoint push was rejected or interrupted"
  echo "Runtime checkpoint push failed; retrying ($attempt/$max_attempts)."
  retry_pause
done

# The last push may have reached GitHub even when its client response was lost.
# Perform one final bounded read before declaring the durable checkpoint absent;
# it also classifies an exhausted retry loop: a tip that keeps moving past this
# run's base is a publish race, while a tip still at the base means earlier
# rejections were real publish failures.
if [[ "$published" != "true" ]] \
  && timeout --signal=TERM --kill-after=5s "${git_timeout_seconds}s" git fetch --quiet origin "$branch"; then
  remote_sha="$(git rev-parse "refs/remotes/origin/$branch")"
  if [[ "$remote_sha" == "$checkpoint_sha" ]]; then
    published=true
  elif [[ "$remote_sha" != "$checkpoint_parent" ]]; then
    remote_moved=true
    # Keep a more specific replay-conflict diagnosis when one was recorded.
    if [[ "$failure_reason" != *"could not be replayed"* ]]; then
      failure_reason="origin/$branch advanced while the monitor was running"
    fi
  elif timeout --signal=TERM --kill-after=5s "${git_timeout_seconds}s" git push origin "HEAD:$branch"; then
    # The remote sat at this run's base, so one bounded last-chance push is a
    # fast-forward. Its failure below is attributable to transport or auth.
    published=true
  else
    remote_moved=false
    failure_reason="checkpoint push failed while origin/$branch still matched the run base"
  fi
fi

if [[ "$published" == "true" ]]; then
  echo "Runtime checkpoint published to $branch."
  exit 0
fi

if [[ "$remote_moved" == "true" ]]; then
  report_deferred "Runtime checkpoint was not published because ${failure_reason:-origin/$branch advanced while the monitor was running}. No recovery branch was created; the next run will reconcile from origin/$branch."
fi

report_failure "Runtime checkpoint was not published because ${failure_reason:-the push could not be confirmed}. No recovery branch was created; the next run will reconcile from origin/$branch."
