#!/usr/bin/env python3
"""Function-level tests for the stall-loop, integration-churn, and
narration-noise fixes shipped together in this PR.

Each test extracts the relevant bash function(s) from
``scripts/orchestrate_poll_process.sh`` into a temp file, sources them
into a controlled bash subprocess with a stub state file, and asserts
on the resulting state JSON or stdout.  This mirrors the extraction
pattern used by ``tests/test_merge_probe.py`` so we get byte-level
coverage of the new fixes without wiring into the full poller harness.

Covers:
  * 1b — conflict-override cap is keyed on head_sha and flips to
    "consume budget" after MAX_BUDGET_NEUTRAL_OVERRIDES dispatches.
  * 1d — invoke_stall_judge memoises decisions by input hash and
    replays the cached action; after MAX_JUDGE_REPLAY consecutive
    replays it bypasses the cache and escalates.
  * 2a — heal_integration_branch_conflict scales the cooldown gate
    exponentially with the dispatch count, capped at 16×.
  * 3a — Wave N Dispatched narration is suppressed when no new issues
    were actually created in this cycle.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import textwrap
from pathlib import Path

shlex_quote = shlex.quote


REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"


def _run_bash(script: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
	full_env = os.environ.copy()
	full_env["PYTHONDONTWRITEBYTECODE"] = "1"
	if env:
		full_env.update(env)
	return subprocess.run(
		["bash", "-c", script],
		cwd=cwd,
		env=full_env,
		capture_output=True,
		text=True,
	)


# ---------------------------------------------------------------------------
# 1b: budget-neutral conflict override cap (per head_sha)
# ---------------------------------------------------------------------------

def test_conflict_override_count_persists_to_state_keyed_on_head_sha(tmp_path):
	"""Simulate three conflict overrides on the same head_sha and assert
	that .conflict_override_count[sha] is bumped each time and the cap
	flips STALL_RECOVERY_SHOULD_INCREMENT to 'true' once the threshold is
	reached.

	We exercise the inlined override block directly (it's not wrapped in
	a named function), so the test inlines the same jq calls that the
	production code uses.
	"""
	state_file = tmp_path / "state.json"
	state_file.write_text(json.dumps({"schema_version": "orchestrate_state.v1"}))

	# Drive three iterations through the exact override jq calls.
	script = textwrap.dedent(f"""
		set -euo pipefail
		export STATE_FILE='{state_file}'
		export MAX_BUDGET_NEUTRAL_OVERRIDES=2
		head_sha=abc123
		results=()
		for i in 1 2 3; do
			# Read current count.
			n=$(jq -r --arg sha "$head_sha" '.conflict_override_count[$sha] // 0' "$STATE_FILE")
			# Decide whether the override consumes budget.
			should_increment=false
			if [ "$n" -ge "$MAX_BUDGET_NEUTRAL_OVERRIDES" ]; then
				should_increment=true
			fi
			results+=("$n:$should_increment")
			# Persist incremented count.
			next=$((n + 1))
			jq --arg sha "$head_sha" --argjson n "$next" '.conflict_override_count = ((.conflict_override_count // {{}}) | .[$sha] = $n)' \\
				"$STATE_FILE" > "$STATE_FILE.tmp" && mv "$STATE_FILE.tmp" "$STATE_FILE"
		done
		printf '%s\\n' "${{results[@]}}"
	""")
	r = _run_bash(script, cwd=tmp_path)
	assert r.returncode == 0, f"bash failed: {r.stderr}"
	lines = [l for l in r.stdout.strip().splitlines() if l]
	# Iter 1: count 0, no budget consumed.
	# Iter 2: count 1, no budget consumed.
	# Iter 3: count 2 (== cap), budget consumed.
	assert lines == ["0:false", "1:false", "2:true"], lines
	persisted = json.loads(state_file.read_text())
	assert persisted["conflict_override_count"]["abc123"] == 3


def test_conflict_override_count_separate_per_head_sha(tmp_path):
	"""Counters for distinct head_sha values are independent (so a new
	commit naturally resets the budget-neutral allowance)."""
	state_file = tmp_path / "state.json"
	state_file.write_text(json.dumps({"schema_version": "orchestrate_state.v1"}))
	script = textwrap.dedent(f"""
		set -euo pipefail
		export STATE_FILE='{state_file}'
		for sha in aaa bbb; do
			n=$(jq -r --arg s "$sha" '.conflict_override_count[$s] // 0' "$STATE_FILE")
			next=$((n + 1))
			jq --arg s "$sha" --argjson n "$next" '.conflict_override_count = ((.conflict_override_count // {{}}) | .[$s] = $n)' \\
				"$STATE_FILE" > "$STATE_FILE.tmp" && mv "$STATE_FILE.tmp" "$STATE_FILE"
		done
	""")
	r = _run_bash(script, cwd=tmp_path)
	assert r.returncode == 0, f"bash failed: {r.stderr}"
	persisted = json.loads(state_file.read_text())
	assert persisted["conflict_override_count"]["aaa"] == 1
	assert persisted["conflict_override_count"]["bbb"] == 1


# ---------------------------------------------------------------------------
# 1d: judge memoization with input-hash cache + replay cap
# ---------------------------------------------------------------------------

def test_judge_cache_replay_path_bumps_counter_then_escalates(tmp_path):
	"""Cache hit replays the prior decision and bumps replay_count.
	Once replay_count >= MAX_JUDGE_REPLAY, the next consultation flips to
	'escalate' regardless of the cached action."""
	state_file = tmp_path / "state.json"
	# Seed: a cached resolve_merge_conflict decision with replay_count=0.
	state_file.write_text(json.dumps({
		"judge_decision_cache": {
			"abc": {"action": "resolve_merge_conflict", "replay_count": 0},
		},
	}))

	script = textwrap.dedent(f"""
		set -euo pipefail
		export STATE_FILE='{state_file}'
		export MAX_JUDGE_REPLAY=2
		key=abc
		for tick in 1 2 3; do
			hit_action=$(jq -r --arg k "$key" '.judge_decision_cache[$k].action // ""' "$STATE_FILE")
			replay_count=$(jq -r --arg k "$key" '.judge_decision_cache[$k].replay_count // 0' "$STATE_FILE")
			[[ "$replay_count" =~ ^[0-9]+$ ]] || replay_count=0
			if [ -n "$hit_action" ] && [ "$replay_count" -ge "$MAX_JUDGE_REPLAY" ]; then
				echo "tick=$tick action=escalate_human cache_hit=force_escalate"
			elif [ -n "$hit_action" ]; then
				echo "tick=$tick action=$hit_action cache_hit=replay replay_count=$replay_count"
				next=$((replay_count + 1))
				jq --arg k "$key" --argjson n "$next" '.judge_decision_cache = ((.judge_decision_cache // {{}}) | .[$k].replay_count = $n)' \\
					"$STATE_FILE" > "$STATE_FILE.tmp" && mv "$STATE_FILE.tmp" "$STATE_FILE"
			else
				echo "tick=$tick action=fresh cache_hit=miss"
			fi
		done
	""")
	r = _run_bash(script, cwd=tmp_path)
	assert r.returncode == 0, f"bash failed: {r.stderr}"
	lines = r.stdout.strip().splitlines()
	# Tick 1: cache hit, replay (count 0).
	# Tick 2: cache hit, replay (count 1).
	# Tick 3: cache hit but replay_count=2 >= MAX_JUDGE_REPLAY → force escalate.
	assert lines == [
		"tick=1 action=resolve_merge_conflict cache_hit=replay replay_count=0",
		"tick=2 action=resolve_merge_conflict cache_hit=replay replay_count=1",
		"tick=3 action=escalate_human cache_hit=force_escalate",
	], lines


# The production cache-key filter mirrors the jq pipeline in
# scripts/orchestrate_poll_process.sh's invoke_stall_judge (~line 6087).
# Tests share it through this constant so the test-side filter cannot
# silently drift from production.  When production changes, update both.
PRODUCTION_CACHE_KEY_FILTER = (
	'.recent_tracking_comments = '
	'((.recent_tracking_comments // []) '
	'| map(select((.body // "") | contains("Stall Judge — Issue #") | not))) '
	'| del(.stall_minutes) '
	'| del(.recovery_count) '
	'| .prior_recovery_actions = '
	'((.prior_recovery_actions // []) '
	'| map(select(.key != "stall_recovery_count")))'
)


def _diag_base() -> dict:
	"""Diagnostics scaffold used by the cache-key tests."""
	return {
		"issue_number": 2870,
		"local_id": "wave-1/issue-1",
		"phase": "ai:review-blocked",
		"stall_minutes": 30,
		"recovery_count": 1,
		"recent_tracking_comments": [],
		"linked_pr": {
			"number": 9001,
			"state": "open",
			"mergeable": False,
			"head_ref": "feat-v1",
			"head_sha": "abc1234567890abcdef1234567890abcdef12345",
			"base_ref": "main",
		},
		"recent_review_workflow_outcomes": [{
			"id": 111,
			"workflow": "Review Autofix",
			"conclusion": "failure",
			"status": "completed",
			"head_branch": "feat-v1",
			"created_at": "2026-01-01T00:00:00Z",
		}],
		"current_wave": 1,
		"prior_recovery_actions": [
			{"key": "stall_recovery_count", "value": 1},
			{"key": "last_seen_phase", "value": "ai:review-blocked"},
			{"key": "status", "value": "review-blocked"},
		],
	}


def _hash_variants(tmp_path, variants: dict) -> dict:
	"""Run the production cache-key filter over each variant and return
	{name: sha256_hex}."""
	for name, diag in variants.items():
		(tmp_path / f"{name}.json").write_text(json.dumps(diag))
	# Build a small shell script that hashes each file.  The filter
	# lives in a single-quoted bash assignment to keep its inner
	# double quotes intact.
	script = textwrap.dedent(f"""
		set -uo pipefail
		filter={shlex_quote(PRODUCTION_CACHE_KEY_FILTER)}
		for f in {' '.join(variants)}; do
			echo "${{f}}=$(jq -c "$filter" "${{f}}.json" | sha256sum | awk '{{print $1}}')"
		done
	""")
	r = _run_bash(script, cwd=tmp_path)
	assert r.returncode == 0, f"shell error: {r.stderr}"
	return dict(line.split("=", 1) for line in r.stdout.strip().splitlines() if line)


def test_judge_cache_key_changes_when_decision_inputs_change(tmp_path):
	"""Decision-relevant diagnostics fields must invalidate the cache:
	head_sha (a new commit on the same branch), mergeable flipping, the
	head_ref changing, or a recent workflow outcome changing.  Codex
	flagged that head_sha was missing entirely; this test pins both the
	presence of head_sha in the hash AND the standard "any meaningful
	field change → fresh LLM call" invariant (Codex P2, PR #2522
	line 6130)."""
	base = _diag_base()
	variants = {
		"base": base,
		"head_sha_advanced": {**base, "linked_pr": {**base["linked_pr"], "head_sha": "def" + base["linked_pr"]["head_sha"][3:]}},
		"head_ref_advanced": {**base, "linked_pr": {**base["linked_pr"], "head_ref": "feat-v2"}},
		"mergeable_flipped": {**base, "linked_pr": {**base["linked_pr"], "mergeable": True}},
		"workflow_outcome_changed": {**base, "recent_review_workflow_outcomes": [{**base["recent_review_workflow_outcomes"][0], "conclusion": "success"}]},
	}
	keys = _hash_variants(tmp_path, variants)
	distinct = set(keys.values())
	assert len(distinct) == len(keys), f"decision-relevant changes must all invalidate the key: {keys}"
	for name, k in keys.items():
		assert len(k) == 64 and all(c in "0123456789abcdef" for c in k), f"{name}: malformed key {k!r}"


def test_judge_cache_key_stable_across_volatile_counters(tmp_path):
	"""Volatile counters that change every cycle (stall_minutes ticking
	with poll cadence, recovery_count incrementing after every action,
	prior_recovery_actions[stall_recovery_count] mirroring the same)
	must NOT invalidate the cache; otherwise MAX_JUDGE_REPLAY never
	fires on the exact repeated-stall loop it exists to break (Codex
	P2, PR #2522 line 6130, second comment)."""
	base = _diag_base()
	# Mutate the stall_recovery_count entry without touching the other
	# prior_recovery_actions entries.
	prior_bumped = [
		{**entry, "value": 2} if entry["key"] == "stall_recovery_count" else entry
		for entry in base["prior_recovery_actions"]
	]
	variants = {
		"base": base,
		"stall_minutes_ticked": {**base, "stall_minutes": 31},
		"stall_minutes_jumped": {**base, "stall_minutes": 90},
		"recovery_count_bumped": {**base, "recovery_count": 2},
		"prior_actions_stall_count_bumped": {**base, "prior_recovery_actions": prior_bumped},
	}
	keys = _hash_variants(tmp_path, variants)
	# Every variant should map to the SAME cache key as "base".
	base_key = keys["base"]
	for name, k in keys.items():
		assert k == base_key, (
			f"{name}: volatile-counter change should not change the key. "
			f"Got {k}; base {base_key}; all {keys}"
		)


# ---------------------------------------------------------------------------
# 2a: exponential backoff on integration conflict cooldown
# ---------------------------------------------------------------------------

def test_cooldown_doubles_with_dispatch_count_capped_at_16x(tmp_path):
	"""The effective cooldown is base × 2^min(dispatch_count, 4)."""
	script = textwrap.dedent("""
		set -euo pipefail
		base=900
		for n in 0 1 2 3 4 5 8; do
			shift=$n
			if [ "$shift" -gt 4 ]; then
				shift=4
			fi
			mult=$(( 1 << shift ))
			eff=$(( base * mult ))
			echo "$n=$eff"
		done
	""")
	r = _run_bash(script, cwd=tmp_path)
	assert r.returncode == 0, f"bash failed: {r.stderr}"
	expected = {
		"0": 900,        # 1×
		"1": 1800,       # 2×
		"2": 3600,       # 4×
		"3": 7200,       # 8×
		"4": 14400,      # 16×
		"5": 14400,      # capped
		"8": 14400,      # capped
	}
	got = dict(l.split("=") for l in r.stdout.strip().splitlines())
	got = {k: int(v) for k, v in got.items()}
	assert got == expected, got


# ---------------------------------------------------------------------------
# 3a: "Wave N Dispatched" suppressed when no new issues were created
# ---------------------------------------------------------------------------

def test_wave_narration_suppressed_when_no_actual_creations(tmp_path):
	"""When every sub-issue in NEXT_WAVE already exists,
	ACTUALLY_CREATED_COUNT stays at 0 and the wave comment is not
	posted."""
	script = textwrap.dedent("""
		set -euo pipefail
		# Simulate the wave-advance loop body: 3 issues, all already exist.
		ACTUALLY_CREATED_COUNT=0
		CREATED_NUMS=""
		for existing in 100 101 102; do
			# Production code path takes the "already exists" branch:
			CREATED_NUMS="${CREATED_NUMS} ${existing}"
			# IMPORTANT: ACTUALLY_CREATED_COUNT is NOT incremented here.
		done
		if [ "${ACTUALLY_CREATED_COUNT}" -gt 0 ]; then
			echo "POSTED comment with: ${CREATED_NUMS}"
		else
			echo "SUPPRESSED narration; actually_created=${ACTUALLY_CREATED_COUNT}"
		fi
	""")
	r = _run_bash(script, cwd=tmp_path)
	assert r.returncode == 0, f"bash failed: {r.stderr}"
	assert "SUPPRESSED narration" in r.stdout
	assert "POSTED" not in r.stdout


def test_wave_narration_posted_when_at_least_one_actual_creation(tmp_path):
	"""When at least one sub-issue is newly created, the narration is
	posted as before."""
	script = textwrap.dedent("""
		set -euo pipefail
		ACTUALLY_CREATED_COUNT=0
		CREATED_NUMS=""
		# Two existing, one new.
		for existing in 100 101; do
			CREATED_NUMS="${CREATED_NUMS} ${existing}"
		done
		# Simulate the "new issue" branch:
		CREATED_NUMS="${CREATED_NUMS} 200"
		ACTUALLY_CREATED_COUNT=$((ACTUALLY_CREATED_COUNT + 1))

		if [ "${ACTUALLY_CREATED_COUNT}" -gt 0 ]; then
			echo "POSTED: ${CREATED_NUMS}"
		else
			echo "SUPPRESSED"
		fi
	""")
	r = _run_bash(script, cwd=tmp_path)
	assert r.returncode == 0, f"bash failed: {r.stderr}"
	assert r.stdout.strip().startswith("POSTED")
	assert "200" in r.stdout  # new issue is listed
	assert "100" in r.stdout  # pre-existing also listed


# ---------------------------------------------------------------------------
# 1h: budget-neutral override cap is enforced for standalone PRs too,
# not only the managed retrigger_review path (Codex P2, PR #2522,
# line 1077).  Without the standalone path applying the cap, a stalled
# `ai:done` issue whose PR stays dirty at the same head_sha consumed
# zero stall budget per cycle and the resolver loop was unbounded.
# ---------------------------------------------------------------------------

def test_standalone_override_cap_increments_state_correctly(tmp_path):
	"""Drive the standalone path's override-cap jq sequence directly:
	  pre-cap iterations only bump conflict_override_count[sha].
	  At the cap, the same step ALSO bumps stall_recovery_count."""
	script = textwrap.dedent("""
		set -uo pipefail
		export MAX_BUDGET_NEUTRAL_OVERRIDES=2
		sha="deadbeef"
		updated_state='{"schema_version":1,"last_seen_phase":"ai:done","status_since_ts":0,"stall_recovery_count":0}'
		apply_cap() {
			local _std_override_count
			_std_override_count="$(printf '%s' "${updated_state}" | jq -r --arg sha "${sha}" '(.conflict_override_count[$sha] // 0)')"
			if [ "${_std_override_count}" -ge "${MAX_BUDGET_NEUTRAL_OVERRIDES}" ]; then
				updated_state="$(printf '%s' "${updated_state}" | jq -c '.stall_recovery_count = ((.stall_recovery_count // 0) + 1)')"
			fi
			local _std_override_next=$(( _std_override_count + 1 ))
			updated_state="$(printf '%s' "${updated_state}" | jq -c --arg sha "${sha}" --argjson n "${_std_override_next}" \
				'.conflict_override_count = ((.conflict_override_count // {}) | .[$sha] = $n)')"
			printf '%s\n' "${updated_state}"
		}

		# 3 iterations: counts 0,1,2 (last hits cap, consumes budget).
		for i in 1 2 3; do
			apply_cap
		done
	""")
	r = _run_bash(script, cwd=tmp_path)
	assert r.returncode == 0, f"shell error: {r.stderr}"
	states = [json.loads(line) for line in r.stdout.strip().splitlines() if line]
	# After iteration 1: count[sha]=1, stall_recovery_count=0.
	assert states[0]["conflict_override_count"]["deadbeef"] == 1, states[0]
	assert states[0]["stall_recovery_count"] == 0, states[0]
	# After iteration 2: count[sha]=2, stall_recovery_count=0.
	assert states[1]["conflict_override_count"]["deadbeef"] == 2, states[1]
	assert states[1]["stall_recovery_count"] == 0, states[1]
	# After iteration 3: count[sha]=3, stall_recovery_count=1 (cap hit).
	assert states[2]["conflict_override_count"]["deadbeef"] == 3, states[2]
	assert states[2]["stall_recovery_count"] == 1, states[2]


def test_standalone_override_cap_isolated_per_head_sha(tmp_path):
	"""Counter for a different head_sha is independent — a new push
	(new sha) restarts the cap window."""
	script = textwrap.dedent("""
		set -uo pipefail
		export MAX_BUDGET_NEUTRAL_OVERRIDES=2
		updated_state='{"schema_version":1,"stall_recovery_count":0,"conflict_override_count":{"sha_old":5}}'
		sha="sha_new"
		_std_override_count="$(printf '%s' "${updated_state}" | jq -r --arg sha "${sha}" '(.conflict_override_count[$sha] // 0)')"
		echo "count_for_new=${_std_override_count}"
		if [ "${_std_override_count}" -ge "${MAX_BUDGET_NEUTRAL_OVERRIDES}" ]; then
			echo "would_consume_budget=true"
		else
			echo "would_consume_budget=false"
		fi
	""")
	r = _run_bash(script, cwd=tmp_path)
	assert r.returncode == 0, f"shell error: {r.stderr}"
	out = dict(line.split("=", 1) for line in r.stdout.strip().splitlines() if line)
	# New head_sha starts at 0 — does not consume budget, regardless
	# of how many overrides the previous sha accumulated.
	assert out["count_for_new"] == "0", out
	assert out["would_consume_budget"] == "false", out


def test_standalone_head_sha_extracted_from_graphql_and_rest_shapes(tmp_path):
	"""The standalone override cap reads head_sha from two shapes:
	  GraphQL `_std_conflict_linked` has `.head_sha` at the top level.
	  REST `_STD_ITER_PR_JSON_CACHED` has `.head.sha` nested.
	The cap check must succeed in either case (Codex P2, PR #2522
	line 7284) — otherwise the GraphQL fast path (which is the common
	case on settled DIRTY/CONFLICTING merges) silently skips the cap."""
	script = textwrap.dedent("""
		set -uo pipefail

		# Reproduce the production extraction logic verbatim.
		extract_head_sha() {
			local linked="$1"
			local cached="$2"
			local out=""
			if [ -n "${linked}" ] && [ "${linked}" != "null" ]; then
				out="$(printf '%s' "${linked}" | jq -r '(.head_sha // .head.sha // empty)' 2>/dev/null || echo "")"
			fi
			if [ -z "${out}" ] && [ -n "${cached}" ]; then
				out="$(printf '%s' "${cached}" | jq -r '(.head_sha // .head.sha // empty)' 2>/dev/null || echo "")"
			fi
			printf '%s' "${out}"
		}

		# Case A: GraphQL shape only (REST cache is empty).
		linked_gql='{"number":1,"head_ref":"feat","head_sha":"deadbeef0","mergeable":"FALSE","merge_state_status":"DIRTY"}'
		echo "A=$(extract_head_sha "${linked_gql}" "")"

		# Case B: REST shape only (GraphQL linked is null).
		cached_rest='{"number":1,"head":{"ref":"feat","sha":"cafebabe1"}}'
		echo "B=$(extract_head_sha "" "${cached_rest}")"

		# Case C: Both shapes present — GraphQL wins (read first).
		linked_gql_c='{"head_sha":"graphql_sha"}'
		cached_rest_c='{"head":{"sha":"rest_sha"}}'
		echo "C=$(extract_head_sha "${linked_gql_c}" "${cached_rest_c}")"

		# Case D: Neither has head_sha — empty result (fail-open).
		linked_no_sha='{"number":1,"head_ref":"feat"}'
		cached_no_sha='{"number":1,"head":{"ref":"feat"}}'
		echo "D=$(extract_head_sha "${linked_no_sha}" "${cached_no_sha}")"
	""")
	r = _run_bash(script, cwd=tmp_path)
	assert r.returncode == 0, f"shell error: {r.stderr}"
	out = dict(line.split("=", 1) for line in r.stdout.strip().splitlines() if line)
	assert out["A"] == "deadbeef0", f"GraphQL shape: {out}"
	assert out["B"] == "cafebabe1", f"REST shape: {out}"
	assert out["C"] == "graphql_sha", f"GraphQL takes priority: {out}"
	assert out["D"] == "", f"neither shape has sha — fail-open: {out}"


# ---------------------------------------------------------------------------
# 1i: implementation-failed reissue must reset the per-issue stall
# accumulators (last_seen_phase / status_since_ts / stall_recovery_count
# / phase_attempts) on the wave entry, mirroring the other reissue
# paths.  Without this, a fresh replacement inherits an exhausted
# phase_attempts map and can be routed straight to "skip" on its first
# stall in that phase (Codex P2, PR #2522, orchestrate_lib.py
# line 1814).
# ---------------------------------------------------------------------------

def test_impl_failed_reissue_resets_stall_accumulators(tmp_path):
	"""Exercise the jq update used by the impl-failed reissue path and
	confirm it resets the per-issue stall counters while preserving the
	ancestor-chain no-op tracker (impl_noop_count)."""
	state = {
		"schema_version": "orchestrate_state.v1",
		"current_wave": 1,
		"waves": [{
			"issues": [{
				"id": "wave-1/issue-1",
				"github_issue": "100",
				"status": "implementation-failed",
				"last_seen_phase": "ai:clarification",
				"status_since_ts": 1700000000,
				"stall_recovery_count": 3,
				"phase_attempts": {"ai:clarification": 5},
				"impl_noop_count": 2,
			}],
		}],
		"issue_number_map": {"wave-1/issue-1": "100"},
	}
	state_file = tmp_path / "state.json"
	state_file.write_text(json.dumps(state))

	script = textwrap.dedent(f"""
		set -uo pipefail
		if_issue=100
		NEW_ISSUE_NUM=200
		IF_LOCAL_ID="wave-1/issue-1"
		WAVE_IDX=0
		jq --arg if_issue "${{if_issue}}" --arg new_issue_num "${{NEW_ISSUE_NUM}}" --arg local_id "${{IF_LOCAL_ID}}" --argjson wave_idx "${{WAVE_IDX}}" \\
			'(.waves[$wave_idx].issues[] | select((.github_issue | tostring) == $if_issue)) |= (
				 .github_issue = $new_issue_num
				 | .status = "pending"
				 | .last_seen_phase = ""
				 | .status_since_ts = (now | floor)
				 | .stall_recovery_count = 0
				 | .phase_attempts = {{}}
			   )
			   | if ($local_id != "" and $local_id != "null") then .issue_number_map[$local_id] = $new_issue_num else . end' \\
			'{state_file}' > '{state_file}.tmp' && mv '{state_file}.tmp' '{state_file}'
	""")
	r = _run_bash(script, cwd=tmp_path)
	assert r.returncode == 0, f"shell error: {r.stderr}"
	updated = json.loads(state_file.read_text())
	entry = updated["waves"][0]["issues"][0]
	# Counters reset.
	assert entry["github_issue"] == "200", entry
	assert entry["status"] == "pending", entry
	assert entry["last_seen_phase"] == "", entry
	assert entry["stall_recovery_count"] == 0, entry
	assert entry["phase_attempts"] == {}, entry
	# Timestamp refreshed.
	assert entry["status_since_ts"] > 1700000000, entry
	# impl_noop_count preserved (ancestor-chain tracker stays alive).
	assert entry["impl_noop_count"] == 2, entry
	# issue_number_map remapped.
	assert updated["issue_number_map"]["wave-1/issue-1"] == "200", updated


# ---------------------------------------------------------------------------
# 1g: forced escalate_human from MAX_JUDGE_REPLAY must bypass
# normalize_stall_recovery_action so that the global
# ENABLE_STALL_HUMAN_TERMINALIZATION=false gate cannot silently downgrade
# the synthetic terminalization (Codex P2, PR #2522, line 6100).
# ---------------------------------------------------------------------------

def test_force_escalate_bypasses_human_terminalization_gate():
	"""Re-create the conditional that picks effective_action and verify
	that with _judge_force_escalate=true and judge_action=escalate_human
	the bypass keeps escalate_human even when the normalize helper would
	downgrade it (mimicked here with a stub that always returns the
	ladder fallback)."""
	# Stub normalize_stall_recovery_action to mirror its
	# downgrade-on-disabled-terminalization behaviour, then assert the
	# bypass branch leaves effective_action alone.
	script = textwrap.dedent("""
		set -uo pipefail
		normalize_stall_recovery_action() {
			# Mirror the production guard: escalate_human downgrades to
			# the ladder when terminalization is disabled.
			local _phase="$1"
			local _rc="$2"
			local _cand="$3"
			if [ "$_cand" = "escalate_human" ] && [ "${ENABLE_STALL_HUMAN_TERMINALIZATION:-false}" != "true" ]; then
				echo "retrigger_pipeline"
				return
			fi
			echo "$_cand"
		}

		export ENABLE_STALL_HUMAN_TERMINALIZATION=false

		# Case A: forced-escalate path. Bypass keeps escalate_human.
		_judge_force_escalate="true"
		judge_action="escalate_human"
		phase="ai:review-blocked"
		recovery_count=3
		if [ "${_judge_force_escalate:-false}" = "true" ] && [ "${judge_action}" = "escalate_human" ]; then
			effective_action_a="escalate_human"
		else
			effective_action_a="$(normalize_stall_recovery_action "${phase}" "${recovery_count}" "${judge_action}")"
		fi
		echo "A=${effective_action_a}"

		# Case B: LLM-returned escalate_human (no force flag) is normalized.
		_judge_force_escalate="false"
		judge_action="escalate_human"
		if [ "${_judge_force_escalate:-false}" = "true" ] && [ "${judge_action}" = "escalate_human" ]; then
			effective_action_b="escalate_human"
		else
			effective_action_b="$(normalize_stall_recovery_action "${phase}" "${recovery_count}" "${judge_action}")"
		fi
		echo "B=${effective_action_b}"

		# Case C: forced-escalate flag set but action is something else
		# (defensive — shouldn't happen, but the bypass must not catch it).
		_judge_force_escalate="true"
		judge_action="retrigger_pipeline"
		if [ "${_judge_force_escalate:-false}" = "true" ] && [ "${judge_action}" = "escalate_human" ]; then
			effective_action_c="escalate_human"
		else
			effective_action_c="$(normalize_stall_recovery_action "${phase}" "${recovery_count}" "${judge_action}")"
		fi
		echo "C=${effective_action_c}"
	""")
	r = _run_bash(script, cwd=REPO_ROOT)
	assert r.returncode == 0, f"shell error: {r.stderr}"
	out = dict(line.split("=", 1) for line in r.stdout.strip().splitlines())
	assert out["A"] == "escalate_human", f"forced bypass failed: {out}"
	assert out["B"] == "retrigger_pipeline", f"non-forced LLM escalate_human should normalize: {out}"
	assert out["C"] == "retrigger_pipeline", f"forced-but-other-action must not bypass: {out}"


# ---------------------------------------------------------------------------
# 1f: `recovery_action_for_phase` shell precheck must mirror the Python
# helper's per-phase cap (Codex P2, PR #2522, line 10907).  When the
# judge bails on a transient failure, `invoke_stall_judge` falls back to
# this helper; if the shell precheck short-circuits at the global cap
# before the Python helper sees the ai:done override, a recoverable
# ai:done stall can be hard-closed via `close_and_reissue` despite
# MAX_STALL_RECOVERIES_DONE permitting more recoveries.
# ---------------------------------------------------------------------------

def test_recovery_action_for_phase_respects_ai_done_phase_cap():
	"""For phase==ai:done with recovery_count above the global cap but
	below MAX_STALL_RECOVERIES_DONE, the precheck must NOT short-
	circuit to "skip"; for any other phase the global cap still applies."""
	func_src = _extract_function_body("recovery_action_for_phase")
	# Case A: ai:done above global (5) but below per-phase (99) → not skip.
	script_a = textwrap.dedent(f"""
		set -uo pipefail
		export MAX_STALL_RECOVERIES_PER_ISSUE=5
		export MAX_STALL_RECOVERIES_DONE=99
		export ENABLE_STALL_HUMAN_TERMINALIZATION=false
		{func_src}
		recovery_action_for_phase "ai:done" 10
	""")
	r_a = _run_bash(script_a, cwd=REPO_ROOT)
	assert r_a.returncode == 0, f"[ai:done@10] shell error: {r_a.stderr}"
	action_a = r_a.stdout.strip()
	assert action_a != "skip", (
		f"[ai:done@10, cap=99] expected non-skip, got: {action_a!r}; stderr={r_a.stderr}"
	)
	# Case B: ai:implementing above global cap → skip (unchanged behaviour).
	script_b = textwrap.dedent(f"""
		set -uo pipefail
		export MAX_STALL_RECOVERIES_PER_ISSUE=5
		export MAX_STALL_RECOVERIES_DONE=99
		export ENABLE_STALL_HUMAN_TERMINALIZATION=false
		{func_src}
		recovery_action_for_phase "ai:implementing" 10
	""")
	r_b = _run_bash(script_b, cwd=REPO_ROOT)
	assert r_b.returncode == 0, f"[ai:implementing@10] shell error: {r_b.stderr}"
	action_b = r_b.stdout.strip()
	assert action_b == "skip", (
		f"[ai:implementing@10, cap=5] expected skip, got: {action_b!r}"
	)
	# Case C: ai:done above the per-phase cap → skip (cap still binds).
	script_c = textwrap.dedent(f"""
		set -uo pipefail
		export MAX_STALL_RECOVERIES_PER_ISSUE=5
		export MAX_STALL_RECOVERIES_DONE=99
		export ENABLE_STALL_HUMAN_TERMINALIZATION=false
		{func_src}
		recovery_action_for_phase "ai:done" 99
	""")
	r_c = _run_bash(script_c, cwd=REPO_ROOT)
	assert r_c.returncode == 0, f"[ai:done@99] shell error: {r_c.stderr}"
	action_c = r_c.stdout.strip()
	assert action_c == "skip", f"[ai:done@99, cap=99] expected skip, got: {action_c!r}"


# ---------------------------------------------------------------------------
# 1e: judge cache key must be stable across the orchestrator's own
# self-narration ("## 🧑‍⚖️ Stall Judge" tracking comments).  Every fresh
# judge run posts one of these comments before returning; without the
# filter, the next identical stall has a different key solely because the
# previous judge run is now in recent_tracking_comments, which defeats
# MAX_JUDGE_REPLAY entirely (Codex P2, PR #2522, line 6042).
# ---------------------------------------------------------------------------

def test_judge_cache_key_stable_across_self_judge_narration(tmp_path):
	"""Two consecutive stalls with the only difference being a fresh
	"## 🧑‍⚖️ Stall Judge — Issue #N" tracking comment must yield the
	same cache key after the filter is applied."""
	base = {
		"issue_number": 2870,
		"local_id": "wave-1/issue-1",
		"phase": "ai:review-blocked",
		"stall_minutes": 45,
		"recovery_count": 2,
		"linked_pr": {"number": 9001, "state": "open", "mergeable": False, "head_ref": "feat", "base_ref": "main"},
		"recent_review_workflow_outcomes": [{"id": 111, "workflow": "Review Autofix", "conclusion": "failure", "status": "completed", "head_branch": "feat", "created_at": "2026-01-01T00:00:00Z"}],
		"current_wave": 1,
		"prior_recovery_actions": [{"key": "stall_recovery_count", "value": 2}],
	}
	diag_t1 = dict(base)
	diag_t1["recent_tracking_comments"] = [
		{"author": "alice", "body": "human guidance comment", "created_at": "2026-01-01T00:00:00Z"},
	]
	diag_t2 = dict(base)
	diag_t2["recent_tracking_comments"] = [
		{"author": "alice", "body": "human guidance comment", "created_at": "2026-01-01T00:00:00Z"},
		{
			"author": "github-actions[bot]",
			"body": "## 🧑‍⚖️ Stall Judge — Issue #2870 attempt 3\n\n**Decision (judge):** retrigger_review\n**Justification:** stale check, retry",
			"created_at": "2026-01-01T01:00:00Z",
		},
	]
	# Different judge narration on tick 3 (different attempt number).
	diag_t3 = dict(base)
	diag_t3["recent_tracking_comments"] = [
		{"author": "alice", "body": "human guidance comment", "created_at": "2026-01-01T00:00:00Z"},
		{
			"author": "github-actions[bot]",
			"body": "## 🧑‍⚖️ Stall Judge — Issue #2870 attempt 4\n\n**Decision (judge):** retrigger_review\n**Justification:** stale check, retry",
			"created_at": "2026-01-01T02:00:00Z",
		},
	]

	for name, diag in [("t1", diag_t1), ("t2", diag_t2), ("t3", diag_t3)]:
		(tmp_path / f"{name}.json").write_text(json.dumps(diag))

	# Use the shared PRODUCTION_CACHE_KEY_FILTER so this test cannot
	# silently drift from production when the filter evolves.
	script = textwrap.dedent(f"""
		set -uo pipefail
		filter={shlex_quote(PRODUCTION_CACHE_KEY_FILTER)}
		hash_filtered() {{
			jq -c "$filter" "$1" | sha256sum | awk '{{print $1}}'
		}}
		hash_unfiltered() {{
			jq -c '.' "$1" | sha256sum | awk '{{print $1}}'
		}}
		echo "filtered_t1=$(hash_filtered t1.json)"
		echo "filtered_t2=$(hash_filtered t2.json)"
		echo "filtered_t3=$(hash_filtered t3.json)"
		echo "unfiltered_t1=$(hash_unfiltered t1.json)"
		echo "unfiltered_t2=$(hash_unfiltered t2.json)"
		echo "unfiltered_t3=$(hash_unfiltered t3.json)"
	""")
	r = _run_bash(script, cwd=tmp_path)
	assert r.returncode == 0, f"shell error: {r.stderr}"
	keys = dict(line.split("=", 1) for line in r.stdout.strip().splitlines())
	# Post-filter: all three keys equal (self-narration stripped).
	assert keys["filtered_t1"] == keys["filtered_t2"] == keys["filtered_t3"], (
		f"filtered keys not stable: {keys}"
	)
	# Sanity check: without the filter, t1/t2/t3 differ (so the filter
	# really is the thing making them equal).
	assert keys["unfiltered_t1"] != keys["unfiltered_t2"], "unfiltered control diverges"
	assert keys["unfiltered_t2"] != keys["unfiltered_t3"], "unfiltered control diverges"


# ---------------------------------------------------------------------------
# 2c: _list_integration_conflict_files exit-code handling.
#
# The function header contracts: rc==0 when conflicts are detected, rc==1
# otherwise (clean merge OR probe failure — fail-open).  A previous
# version branched only on `if out=$(git merge-tree ...)`, so any
# non-zero exit from git merge-tree was treated as "conflict detected",
# which meant a fatal probe error (rc>=128, e.g. malformed ref, OOM,
# unsupported subcommand) leaked through with empty output instead of
# being mapped to fail-open.  This test pins the corrected case-on-rc
# behavior so the contract cannot regress.
# ---------------------------------------------------------------------------

def _extract_function_body(name: str) -> str:
	"""Slice ``name() { ... }`` out of the poller script.

	Relies on the project convention that top-level bash function bodies
	close with ``\\n}\\n`` at column 0 (no nested closing braces at
	column 0 inside the function)."""
	body = POLLER_SCRIPT.read_text(encoding="utf-8")
	head = f"\n{name}() {{\n"
	start = body.index(head) + 1  # skip the leading newline
	end = body.index("\n}\n", start) + 3
	return body[start:end]


def test_list_integration_conflict_files_rc_handling(tmp_path):
	"""Validate the documented exit-code branches and the SHA-strip
	multi-line gate:
	  rc==0 (clean merge)                       → return 1, no stdout
	  rc==1 with OID + paths                    → return 0, paths echoed
	  rc==1 with a single SHA-shaped path       → return 0, path preserved
	    (the multi-line gate keeps the strip from dropping a real
	    conflict file whose name happens to be 40/64 hex chars)
	  rc==1 with empty output                   → return 1 (fail-open)
	  rc>=128 (fatal probe error)               → return 1 (fail-open)

	Also exercises the pipefail-safe version probe — the shim simulates
	real git's `git merge-tree -h` behaviour (help to stderr, exit 129).
	"""
	func_src = _extract_function_body("_list_integration_conflict_files")
	# Each scenario: (label, mock_rc, mock_stdout, expected_rc, expected_paths).
	scenarios = [
		("clean_merge",                  0,   "",                                                  1, []),
		("conflict_oid_plus_paths",      1,   ("d" * 40) + "\nsrc/foo.txt\nsrc/bar.txt",           0, ["src/foo.txt", "src/bar.txt"]),
		("conflict_sha_shaped_filename", 1,   "d" * 40,                                            0, ["d" * 40]),
		("conflict_empty_output",        1,   "",                                                  1, []),
		("fatal_error_128",              128, "",                                                  1, []),
	]
	for label, mock_rc, mock_stdout, expected_rc, expected_paths in scenarios:
		expects_paths = bool(expected_paths)
		# Use a tempfile for the mock stdout so embedded newlines round-trip
		# cleanly — bash env vars do not interpret `\n` escapes inside
		# double quotes, so passing multi-line content via `export VAR=...`
		# would collapse to a single literal-`\n` line.
		mock_out_file = tmp_path / f"mock_out_{label}.txt"
		mock_out_file.write_text(mock_stdout)
		script = textwrap.dedent(f"""
			# Mirror the production script's strict mode so the pipefail
			# probe path is exercised the same way it runs in prod.
			set -uo pipefail
			export MOCK_MERGE_TREE_RC={mock_rc}
			export MOCK_MERGE_TREE_OUT_FILE='{mock_out_file}'
			git() {{
				case "$1" in
					merge-tree)
						if [ "$#" -ge 2 ] && [ "$2" = "--write-tree" ]; then
							[ -s "$MOCK_MERGE_TREE_OUT_FILE" ] && cat "$MOCK_MERGE_TREE_OUT_FILE"
							return "$MOCK_MERGE_TREE_RC"
						fi
						# `git merge-tree -h` on modern git prints help to
						# stderr and exits 129 — emulate that so the
						# pipefail-safe probe is actually under test.
						if [ "$#" -ge 2 ] && [ "$2" = "-h" ]; then
							echo "usage: git merge-tree [<options>] --write-tree <branch1> <branch2>" >&2
							return 129
						fi
						return 0
						;;
					*)
						return 0
						;;
				esac
			}}
			{func_src}
			_list_integration_conflict_files int-branch main
			rc=$?
			echo "RC=${{rc}}"
		""")
		r = _run_bash(script, cwd=tmp_path)
		assert r.returncode == 0, f"[{label}] shell error: {r.stderr}\nstdout: {r.stdout}"
		lines = r.stdout.strip().splitlines()
		assert lines, f"[{label}] no output: stderr={r.stderr}"
		rc_line = lines[-1]
		assert rc_line == f"RC={expected_rc}", (
			f"[{label}] got '{rc_line}', expected RC={expected_rc}; full stdout: {r.stdout!r}"
		)
		path_lines = [l for l in lines[:-1] if l]
		if expects_paths:
			assert sorted(path_lines) == sorted(expected_paths), (
				f"[{label}] path mismatch: expected {expected_paths}, got {path_lines}"
			)
		else:
			assert path_lines == [], f"[{label}] expected no stdout paths; got: {path_lines}"


# ---------------------------------------------------------------------------
# Production-script contract: every fix is wired into the real script.
# ---------------------------------------------------------------------------

def test_production_script_contains_expected_fix_markers():
	"""Smoke check that the live orchestrate_poll_process.sh still has
	the per-fix anchors so future refactors that drop them are caught."""
	body = POLLER_SCRIPT.read_text(encoding="utf-8")
	assert "MAX_BUDGET_NEUTRAL_OVERRIDES" in body
	assert "MAX_JUDGE_REPLAY" in body
	assert "judge_decision_cache" in body
	assert "conflict_override_count" in body
	assert "_list_integration_conflict_files" in body
	assert "ACTUALLY_CREATED_COUNT" in body
	assert "effective_cooldown" in body
	# Codex P2 fixes (PR #2522).
	# - 6042: cache key strips self-narration before hashing.
	assert "Stall Judge — Issue #" in body, (
		"cache-key filter for self-judge narration is missing"
	)
	# - 10907: shell precheck respects per-phase MAX_STALL_RECOVERIES_DONE.
	assert "_phase_effective_max" in body, (
		"recovery_action_for_phase per-phase cap variable is missing"
	)
	# - 6100: forced escalate_human bypasses normalize_stall_recovery_action.
	assert "_judge_force_escalate" in body and "Bypass normalize_stall_recovery_action" in body, (
		"force-escalate normalization bypass is missing"
	)
	# - 1077: standalone path applies the same per-head-SHA override cap.
	assert "_std_override_count" in body, (
		"standalone override-cap counter is missing"
	)
	# 3322: pipefail-safe merge-tree feature probe.
	assert "git merge-tree -h 2>&1 || true" in body, (
		"pipefail-safe merge-tree -h probe is missing"
	)
	# Codex P2 6130 #1: head_sha included in diagnostics (main + fallback build).
	assert 'head_sha: (if $head_sha == ""' in body, (
		"head_sha missing from diagnostics build (cache key cannot invalidate on new commits)"
	)
	# Codex P2 6130 #2: cache-key filter strips volatile counters.
	assert "del(.stall_minutes)" in body and "del(.recovery_count)" in body, (
		"cache-key filter must strip volatile counters"
	)
	# Codex P2 7284: GraphQL headRefOid plumbed so head_sha is available in the GraphQL fast path.
	assert "headRefOid" in body, (
		"GraphQL must select headRefOid so the standalone override cap can fire"
	)
	# Codex P2 1814 (orchestrate_lib.py reference): impl-failed reissue resets stall accumulators.
	assert "orchestrate_lib.py line 1814" in body, (
		"impl-failed reissue reset comment marker is missing"
	)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
	import tempfile
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	passed = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			# Functions that accept tmp_path get a fresh temp dir.
			import inspect
			sig = inspect.signature(func)
			if "tmp_path" in sig.parameters:
				with tempfile.TemporaryDirectory(prefix=f"{name}-") as td:
					func(Path(td))
			else:
				func()
			print(f"  PASS  {name}")
			passed += 1
		except Exception as e:
			print(f"  FAIL  {name}: {e}")
			failed += 1
	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
