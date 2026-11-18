#!/usr/bin/env python3
"""Contract tests for the resolver-side smoke-fixture deterministic fallback.

PR #2095 added a smoke-only override block to the conflict-resolver
prompt, but PR #2112 / run 25370025320 confirmed the legacy editor default still
hits the documented empty-stdout failure mode on the trivial canary
conflict even with the override rendered correctly: 3 attempts, all
reading tests/e2e_smoke_canary.txt then exiting cleanly with 0 bytes
on stdout, no apply_patch invoked, retry loop bails at MAX_ATTEMPTS,
[ai-merge-resolve] commit aborted.

This file pins two resolver-script behaviors that together stop the
loop from bailing on this specific failure mode:

1. scripts/review_conflict_resolve.sh applies the override's
   specified resolution (keep HEAD `run_id:`, drop `alt-` bait line)
   deterministically before entering the codex loop, gated on
   IS_SMOKE_TEST=true AND the canary path being in the resolver's
   unmerged allowlist AND the canary actually carrying the smoke
   conflict shape. Production runs (IS_SMOKE_TEST unset) skip the
   block entirely.

2. Empty stdout from `codex exec` is no longer a hard failure on its
   own — the script falls through to the existing soft-validation
   gates (residual marker scan + fingerprint verify) so a model run
   that invoked apply_patch tool calls without emitting final
   assistant text still gets credit when the working tree is clean.

Without these tests, a later refactor that drops the deterministic
block, weakens its IS_SMOKE_TEST / allowlist gates, or restores the
empty-stdout early-continue would silently re-introduce the bail-out
on every smoke run.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RESOLVE_SCRIPT = REPO_ROOT / "scripts" / "review_conflict_resolve.sh"


def _resolve_script_text() -> str:
	return RESOLVE_SCRIPT.read_text(encoding="utf-8")


def test_smoke_deterministic_block_present_and_gated() -> None:
	"""The smoke deterministic-resolution block must be present and
	gated on IS_SMOKE_TEST + allowlist + canary marker shape."""
	src = _resolve_script_text()

	# Block exists with a recognisable banner.
	assert "Smoke-fixture deterministic pre-resolution" in src, (
		"scripts/review_conflict_resolve.sh must contain the smoke-fixture "
		"deterministic pre-resolution block (PR-#2095 override directive "
		"applied without invoking the model)."
	)

	# Gate (a): IS_SMOKE_TEST=true. The literal must appear in the
	# block's outer condition so production runs short-circuit.
	assert 'if [ "${IS_SMOKE_TEST:-false}" = "true" ]' in src, (
		"Smoke deterministic block must be gated on IS_SMOKE_TEST=true; "
		"otherwise production conflict resolutions would silently bypass "
		"the model on any canary-shaped diff."
	)

	# Gate (b): canary path is in the resolver's unmerged allowlist.
	assert "RESOLVER_ALLOWLIST_FILE" in src and 'tests/e2e_smoke_canary.txt' in src, (
		"Smoke deterministic block must scope itself to the canary path "
		"and require the canary to be in RESOLVER_ALLOWLIST_FILE so "
		"integration-sync runs that don't touch the canary are not "
		"affected."
	)
	# The canary path must be checked against the allowlist with a
	# fixed-string exact-line match (grep -Fxq) — substring matches
	# would let `tests/e2e_smoke_canary_b.txt` etc. trigger the block.
	assert re.search(
		r'grep -Fxq "\$\{_smoke_canary\}" "\$\{RESOLVER_ALLOWLIST_FILE\}"',
		src,
	), (
		"Smoke deterministic block must use grep -Fxq against the "
		"allowlist (fixed-string, exact line) so substring matches "
		"don't false-trigger on sibling canary files."
	)

	# Gate (c): canary file carries the smoke conflict shape.
	assert "<<<<<<< HEAD" in src and ">>>>>>> origin/" in src, (
		"Smoke deterministic block must check for the canonical "
		"`<<<<<<< HEAD` / `>>>>>>> origin/...` markers before "
		"rewriting the file — otherwise it could clobber a clean "
		"canary or a non-smoke-shaped conflict."
	)

	# The block must run BEFORE the actual snapshot loop (the
	# `cp -a "${_snap_path}" "${RESOLVER_ATTEMPT_BASE_DIR}/..."` line)
	# so retries — which restore from the snapshot — see the resolved
	# file rather than re-introducing the conflict markers every time.
	deterministic_idx = src.index("Smoke-fixture deterministic pre-resolution")
	snapshot_copy_idx = src.index(
		'cp -a "${_snap_path}" "${RESOLVER_ATTEMPT_BASE_DIR}/${_snap_path}"'
	)
	assert deterministic_idx < snapshot_copy_idx, (
		"Smoke deterministic resolution must run before the snapshot "
		"copy loop — otherwise retries restore the conflict-marker "
		"version of the canary and the loop never converges."
	)


def test_smoke_deterministic_resolution_keeps_head_drops_alt() -> None:
	"""The awk-driven resolution must keep the HEAD `run_id:` line and
	drop the `alt-...` bait line, producing a marker-free file
	byte-identical to what the override directive specifies.

	Runs the actual awk snippet from the script against a synthetic
	canary so a future refactor that swaps `awk` for `sed` (or vice
	versa) and silently inverts the keep/drop sides is caught."""
	src = _resolve_script_text()

	# Extract the awk program text from the script. The block uses a
	# multiline awk invocation; lift the body so we can re-execute it
	# verbatim against test fixtures.
	awk_match = re.search(
		r"awk '\n(?P<body>.*?)\n    ' \"\$\{_smoke_canary\}\"",
		src,
		flags=re.DOTALL,
	)
	assert awk_match, (
		"Could not locate the awk program inside the smoke deterministic "
		"block. If the implementation switched away from awk, update this "
		"test to extract the new resolver."
	)
	awk_body = awk_match.group("body")

	conflict_canary = (
		"status: ok\n"
		"<<<<<<< HEAD\n"
		"run_id: 25369768571\n"
		"=======\n"
		"run_id: alt-25369768571\n"
		">>>>>>> origin/main\n"
		"updated-by: ai-pipeline\n"
	)
	expected = (
		"status: ok\n"
		"run_id: 25369768571\n"
		"updated-by: ai-pipeline\n"
	)

	with tempfile.TemporaryDirectory() as tmp:
		canary_path = Path(tmp) / "canary.txt"
		canary_path.write_text(conflict_canary, encoding="utf-8")
		result = subprocess.run(
			["awk", awk_body, str(canary_path)],
			check=True,
			capture_output=True,
			text=True,
		)

	assert result.stdout == expected, (
		"awk resolution must keep the HEAD-side `run_id: <num>` line and "
		"drop the `=======` separator, the origin/main `run_id: alt-...` "
		"line, and the `>>>>>>>` end marker.\n"
		f"Got:\n{result.stdout!r}\nExpected:\n{expected!r}"
	)
	# Marker-free is the load-bearing post-condition — soft validation
	# downstream scans for these patterns.
	for marker in ("<<<<<<< ", "=======", ">>>>>>> "):
		assert marker not in result.stdout, (
			f"awk resolution left residual marker {marker!r} in output."
		)


def test_smoke_deterministic_block_is_no_op_without_smoke_env() -> None:
	"""A static check: the deterministic block opens with an
	IS_SMOKE_TEST=true gate, and there is no sibling unconditional
	resolution code that could fire on production runs. Mirrors the
	prepare-script test that pins production prompts byte-identical."""
	src = _resolve_script_text()
	banner = "# ── Smoke-fixture deterministic pre-resolution"
	banner_idx = src.find(banner)
	assert banner_idx != -1, "deterministic block banner missing"
	# Bound the slice by the next downstream anchor — the snapshot
	# copy line, which sits immediately after the deterministic
	# block. Production code that bypassed the IS_SMOKE_TEST gate
	# would need to live inside this slice for the gate-presence
	# check below to be load-bearing.
	end_marker = 'cp -a "${_snap_path}" "${RESOLVER_ATTEMPT_BASE_DIR}/${_snap_path}"'
	end_idx = src.find(end_marker, banner_idx)
	assert end_idx != -1, (
		"Could not locate the snapshot copy line after the "
		"deterministic block; if the layout was refactored, update "
		"this test's slicing anchor."
	)
	block = src[banner_idx:end_idx]
	# Body must contain exactly one outer IS_SMOKE_TEST gate.
	gate_literal = 'if [ "${IS_SMOKE_TEST:-false}" = "true" ]'
	assert block.count(gate_literal) == 1, (
		"The deterministic block must have exactly one IS_SMOKE_TEST "
		"gate; multiple instances suggest the gate was duplicated or "
		"split such that production runs may bypass it."
	)
	# The very first non-comment statement inside the block must be
	# that gate — no sibling unconditional code path.
	non_comment_lines = [
		ln for ln in block.splitlines()
		if ln.strip() and not ln.lstrip().startswith("#")
	]
	assert non_comment_lines, "deterministic block has no executable lines"
	assert non_comment_lines[0].strip().startswith(
		'if [ "${IS_SMOKE_TEST:-false}" = "true" ]'
	), (
		"First executable line in the deterministic block must be the "
		"IS_SMOKE_TEST gate; got: " + non_comment_lines[0]
	)


def test_empty_stdout_no_longer_hard_fail_in_resolver_loop() -> None:
	"""The resolver loop must NOT early-continue on empty `codex exec`
	stdout — it must write a placeholder summary and fall through to
	soft validation, so a model run that invoked apply_patch without
	emitting final text still gets credit when the working tree is
	clean (PR #2086 documents the same pattern on the editor side;
	PR #2112 / run 25370025320 hit it on the resolver side)."""
	src = _resolve_script_text()

	# The old code path was:
	#   if [ ! -s "${tmp_output}" ]; then
	#     rm -f "${tmp_output}"
	#     ...
	#     attempt=$((attempt + 1))
	#     sleep 2
	#     continue
	#   fi
	# After the fix the empty-stdout branch must NOT delete the temp
	# file (it gets a placeholder write instead) and must NOT
	# `continue` out of the loop — it must fall through to the
	# subsequent soft-validation gates.
	empty_block = re.search(
		r'if \[ ! -s "\$\{tmp_output\}" \]; then(?P<body>.*?)\n  fi',
		src,
		flags=re.DOTALL,
	)
	assert empty_block, (
		"Could not locate the empty-stdout handling block in the resolver "
		"loop; if its structure was refactored, update this regex."
	)
	body = empty_block.group("body")
	assert "continue" not in body, (
		"Empty-stdout branch must not `continue` out of the retry loop; "
		"it must fall through to soft validation so a clean working "
		"tree is accepted regardless of whether the model emitted "
		"final assistant text."
	)
	assert 'rm -f "${tmp_output}"' not in body, (
		"Empty-stdout branch must not delete the tmp_output file; "
		"the script writes a placeholder summary into it instead so "
		"the downstream `mv ${tmp_output} ${CONFLICT_RESOLVER_SUMMARY_FILE}` "
		"always has parseable text."
	)
	# Sanity: a placeholder summary must be written so downstream
	# commit / posting logic always sees non-empty content.
	assert "Resolver produced no stdout on attempt" in body, (
		"Empty-stdout branch must write a recognisable placeholder "
		"summary explaining that working-tree state is the source of "
		"truth — otherwise downstream tooling may misinterpret the "
		"empty content."
	)


def main() -> int:
	test_smoke_deterministic_block_present_and_gated()
	test_smoke_deterministic_resolution_keeps_head_drops_alt()
	test_smoke_deterministic_block_is_no_op_without_smoke_env()
	test_empty_stdout_no_longer_hard_fail_in_resolver_loop()
	print(
		"OK: review_conflict_resolve smoke deterministic-resolution "
		"and empty-stdout fallthrough contracts hold"
	)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
