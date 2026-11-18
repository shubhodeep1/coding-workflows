#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-check}"

if [[ "${MODE}" != "check" && "${MODE}" != "repair" ]]; then
  echo "[git-ref-health] Error: Invalid mode '${MODE}'. Must be 'check' or 'repair'." >&2
  exit 1
fi


if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "[git-ref-health] Not inside a git repository; skipping."
  exit 0
fi

echo "[git-ref-health] Running in mode: ${MODE}"

if [ "${MODE}" = "repair" ]; then
  if ! git remote prune origin; then
    echo "[git-ref-health] Warning: git remote prune origin failed; continuing with manual ref checks."
  fi
fi

refs_output=""
if ! refs_output="$(git for-each-ref refs/remotes/origin --format='%(refname)')"; then
  echo "[git-ref-health] Error: failed to enumerate refs/remotes/origin." >&2
  exit 1
fi

declare -a refs=()
if [ -n "${refs_output}" ]; then
  mapfile -t refs < <(printf "%s" "${refs_output}")
fi

if [ "${#refs[@]}" -eq 0 ]; then
  echo "[git-ref-health] No refs/remotes/origin refs found."
  exit 0
fi

declare -a suspicious_refs=()
declare -a broken_refs=()

for ref in "${refs[@]}"; do
  if [[ "${ref}" =~ (^|/)__pycache__(/|$)|\.pyc$ ]]; then
    suspicious_refs+=("${ref}")
  fi
  if ! git rev-parse --verify "${ref}^{object}" >/dev/null 2>&1; then
    broken_refs+=("${ref}")
  fi
done

if [ "${#suspicious_refs[@]}" -gt 0 ]; then
  echo "[git-ref-health] Suspicious remote-tracking refs detected:"
  printf '  - %s\n' "${suspicious_refs[@]}"

  if [ "${MODE}" = "repair" ]; then
    echo "[git-ref-health] Repair mode: deleting suspicious remote-tracking refs."
    for ref in "${suspicious_refs[@]}"; do
      if [[ "${ref}" != refs/remotes/origin/* ]]; then
        echo "[git-ref-health] Skipping unexpected suspicious ref: ${ref}"
        continue
      fi
      if ! git update-ref -d "${ref}"; then
        echo "[git-ref-health] Warning: failed to delete suspicious ref: ${ref}" >&2
      fi
    done
  fi
fi

if [ "${MODE}" = "repair" ] && [ "${#suspicious_refs[@]}" -gt 0 ]; then
  for ref in "${suspicious_refs[@]}"; do
    if git show-ref --verify --quiet "${ref}"; then
      echo "[git-ref-health] Unresolved suspicious ref remains after repair: ${ref}" >&2
      exit 1
    fi
  done
fi

if [ "${#broken_refs[@]}" -eq 0 ]; then
  echo "[git-ref-health] No broken remote-tracking refs detected."
  exit 0
fi

echo "[git-ref-health] Broken remote-tracking refs detected:"
printf '  - %s\n' "${broken_refs[@]}"

if [ "${MODE}" = "repair" ]; then
  for ref in "${broken_refs[@]}"; do
    if [[ "${ref}" != refs/remotes/origin/* ]]; then
      echo "[git-ref-health] Skipping unexpected broken ref: ${ref}"
      continue
    fi
    if ! git update-ref -d "${ref}"; then
      echo "[git-ref-health] Warning: failed to delete broken ref: ${ref}" >&2
    fi
  done
fi

declare -a remaining_broken=()
for ref in "${broken_refs[@]}"; do
  if git show-ref --verify --quiet "${ref}"; then
    if ! git rev-parse --verify "${ref}^{object}" >/dev/null 2>&1; then
      remaining_broken+=("${ref}")
    fi
  fi
done

if [ "${#remaining_broken[@]}" -gt 0 ]; then
  echo "[git-ref-health] Unresolved broken refs remain after ${MODE}."
  printf '  - %s\n' "${remaining_broken[@]}"
  echo "[git-ref-health] Remediation: delete/rename offending remote branches, then rerun with a clean fetch."
  exit 1
fi

echo "[git-ref-health] Broken refs were repaired successfully."
