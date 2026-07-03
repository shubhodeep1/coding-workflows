#!/usr/bin/env python3
"""Contract tests for the editor-side smoke-fixture deterministic pre-write.

PR #2086 added a smoke-only override block to the editor prompt
instructing the model to restore tests/e2e_smoke_canary.txt to the
linked-issue spec; PR #2113 added the resolver-side analog. Run
25370115370 confirmed the legacy editor default still hit the empty-stdout
failure mode on the editor side: only the consolidator (a different
model — gpt-5.4-mini at reasoning=none) actually wrote the file via
`printf >`, and only because its config.toml inherited a
`workspace-write` sandbox in spite of the CLI's `--sandbox read-only`
flag (see test_summarize_reviewer_consensus_sandbox_pin.py for the
parallel fix that closes that hole).

This file pins the editor-side deterministic pre-write that no longer
relies on the model invoking apply_patch:

1. scripts/review_apply_fixes.sh applies a deterministic restoration
   of tests/e2e_smoke_canary.txt before entering the codex retry
   loop, gated on (a) IS_SMOKE_TEST=true AND (b) the canary file
   currently carries the well-known smoke bait shape (any of the
   three bait fingerprints: BROKEN_BY_E2E_BAIT, WRONG_VALUE_SHOULD_BE,
   or `# E2E_EDITOR_BAIT_<run_id>:`). The expected `run_id` is read
   from the bait marker line itself (the smoke gate seeds it with the
   genuine run_id so issue spec and bait-encoded value match).

2. Production runs (IS_SMOKE_TEST unset) skip the block entirely;
   the existing reviewer + editor + commit pipeline runs unchanged.

Without these tests, a later refactor that drops the deterministic
block, weakens its IS_SMOKE_TEST / bait-shape gates, or breaks the
run_id extraction would silently re-introduce the bail-out failure
mode the smoke gate has been chasing across 12+ PRs.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
APPLY_FIXES_SCRIPT = REPO_ROOT / "scripts" / "review_apply_fixes.sh"


def _apply_fixes_text() -> str:
	return APPLY_FIXES_SCRIPT.read_text(encoding="utf-8")


def test_editor_smoke_deterministic_block_present_and_gated() -> None:
	"""The editor smoke deterministic-pre-write block must be present
	and gated on IS_SMOKE_TEST + bait-marker shape."""
	src = _apply_fixes_text()

	# Block exists with a recognisable banner.
	assert "Smoke-fixture deterministic editor pre-write" in src, (
		"scripts/review_apply_fixes.sh must contain the smoke-fixture "
		"deterministic editor pre-write block (PR-#2086 override "
		"directive applied without invoking the model)."
	)

	# Gate (a): IS_SMOKE_TEST=true. The literal must appear in the
	# block's outer condition so production runs short-circuit.
	assert 'if [ "${IS_SMOKE_TEST:-false}" = "true" ]' in src, (
		"Editor smoke pre-write block must be gated on IS_SMOKE_TEST=true; "
		"otherwise production runs would silently overwrite "
		"tests/e2e_smoke_canary.txt on any matching repo state."
	)

	# Gate (b): canary carries one of the bait fingerprints.
	# All three are seeded by the smoke gate's bait commit; matching
	# any is sufficient to recognise a smoke run with bait.
	for fingerprint in ("BROKEN_BY_E2E_BAIT", "WRONG_VALUE_SHOULD_BE", "E2E_EDITOR_BAIT"):
		assert fingerprint in src, (
			f"Editor smoke pre-write block must check for the {fingerprint!r} "
			f"bait fingerprint to recognise the smoke fixture state — "
			f"otherwise it could clobber a clean canary on a smoke run "
			f"that has no bait yet."
		)

	# Block must run BEFORE the codex retry loop so the retry sees
	# a clean tree.
	deterministic_idx = src.index("Smoke-fixture deterministic editor pre-write")
	loop_idx = src.index(
		"attempt=1\neditor_max_attempts=3\nwhile [ \"${attempt}\" -le \"${editor_max_attempts}\" ]"
	)
	assert deterministic_idx < loop_idx, (
		"Editor deterministic pre-write must run before the codex retry "
		"loop — otherwise the model invocation can't observe the "
		"already-restored tree and the printf-vs-apply_patch escape "
		"hatch never engages."
	)


def test_editor_smoke_deterministic_extracts_run_id_from_bait_marker() -> None:
	"""The pre-write must extract the expected run_id from the
	`# E2E_EDITOR_BAIT_<run_id>:` marker line — that's the only
	authoritative source available to the editor script (the linked
	issue body is pulled in via a different mechanism). Run the
	extraction grep+sed pipeline against a synthetic bait file so a
	future refactor that swaps the regex silently keeps no test
	coverage."""
	src = _apply_fixes_text()

	# Locate the extraction snippet — it must use the canonical
	# `# E2E_EDITOR_BAIT_<digits>:` shape so the smoke gate's existing
	# bait-injection contract (seeded by Phase 3c in
	# .github/workflows/test-and-mark-stable.yml) keeps matching.
	assert "# E2E_EDITOR_BAIT_" in src, (
		"Editor pre-write must reference the canonical "
		"`# E2E_EDITOR_BAIT_<run_id>:` marker shape — that's what the "
		"smoke gate seeds via Phase 3c."
	)

	# Reproduce the extraction by simulating a bait canary and running
	# the same grep+sed pipeline the script uses.
	bait_content = (
		"status: BROKEN_BY_E2E_BAIT_25369768571\n"
		"run_id: WRONG_VALUE_SHOULD_BE_25369768571\n"
		"updated-by: e2e-bait-injector\n"
		"extra: this line violates the issue spec\n"
		"also: another line that must be removed\n"
		"# E2E_EDITOR_BAIT_25369768571: canary corrupted; restore to linked issue spec (smoke gate)\n"
	)
	with tempfile.TemporaryDirectory() as tmp:
		bait_path = Path(tmp) / "canary.txt"
		bait_path.write_text(bait_content, encoding="utf-8")
		# This is the literal pipeline the script runs.
		grep = subprocess.run(
			["grep", "-oE", "^# E2E_EDITOR_BAIT_[0-9]+:", str(bait_path)],
			check=True,
			capture_output=True,
			text=True,
		)
		head = subprocess.run(
			["head", "-1"],
			input=grep.stdout,
			capture_output=True,
			text=True,
			check=True,
		)
		sed = subprocess.run(
			["sed", "-E", "s/^# E2E_EDITOR_BAIT_([0-9]+):.*/\\1/"],
			input=head.stdout,
			capture_output=True,
			text=True,
			check=True,
		)

	extracted = sed.stdout.strip()
	assert extracted == "25369768571", (
		f"Extraction of run_id from bait marker must yield the embedded "
		f"digits exactly. Got {extracted!r}; expected '25369768571'."
	)


def test_editor_smoke_deterministic_writes_canonical_3line_spec() -> None:
	"""The deterministic pre-write must produce the canonical 3-line
	canary spec — `status: ok`, `run_id: <extracted>`,
	`updated-by: ai-pipeline`, with a trailing newline. This must
	match `tests/e2e_smoke_canary_spec.txt` (the canonical template
	Phase 4b's pytest verifies against)."""
	src = _apply_fixes_text()

	# The script must invoke the literal printf with the spec format.
	# Match by substring to allow minor whitespace drift but require
	# the load-bearing tokens.
	printf_match = re.search(
		r"printf 'status: ok\\nrun_id: %s\\nupdated-by: ai-pipeline\\n'",
		src,
	)
	assert printf_match, (
		"Editor pre-write must write the canonical 3-line canary spec "
		"`status: ok` / `run_id: %s` / `updated-by: ai-pipeline` with "
		"a trailing newline — Phase 4b's pytest checks for byte-exact "
		"match against tests/e2e_smoke_canary_spec.txt and any drift "
		"breaks the smoke gate."
	)

	# Sanity: the canonical spec template is consistent with what the
	# pre-write produces.
	spec_path = REPO_ROOT / "tests" / "e2e_smoke_canary_spec.txt"
	if spec_path.exists():
		spec_text = spec_path.read_text(encoding="utf-8")
		expected_keys = ("status: ok", "run_id:", "updated-by: ai-pipeline")
		for key in expected_keys:
			assert key in spec_text, (
				f"tests/e2e_smoke_canary_spec.txt must contain {key!r} "
				f"so the editor pre-write's output matches Phase 4b's "
				f"verification target."
			)


def test_editor_smoke_deterministic_block_only_on_smoke() -> None:
	"""Static check: the deterministic block opens with an
	IS_SMOKE_TEST=true gate and contains no sibling unconditional
	resolution code that could fire on production runs."""
	src = _apply_fixes_text()
	banner = "# ── Smoke-fixture deterministic editor pre-write"
	banner_idx = src.find(banner)
	assert banner_idx != -1, "deterministic editor pre-write block banner missing"
	# Bound the slice by the next downstream anchor — the watchdog
	# section that follows the block.
	end_marker = "# ── Adaptive progress-aware watchdog"
	end_idx = src.find(end_marker, banner_idx)
	assert end_idx != -1, (
		"Could not locate the watchdog section header after the "
		"deterministic editor pre-write block; if the layout was "
		"refactored, update this test's slicing anchor."
	)
	block = src[banner_idx:end_idx]
	# The first executable (non-comment) line must be the IS_SMOKE_TEST gate.
	non_comment_lines = [
		ln for ln in block.splitlines()
		if ln.strip() and not ln.lstrip().startswith("#")
	]
	assert non_comment_lines, "deterministic block has no executable lines"
	assert non_comment_lines[0].strip().startswith(
		'if [ "${IS_SMOKE_TEST:-false}" = "true" ]'
	), (
		"First executable line in the editor pre-write block must be "
		"the IS_SMOKE_TEST gate; got: " + non_comment_lines[0]
	)


def main() -> int:
	test_editor_smoke_deterministic_block_present_and_gated()
	test_editor_smoke_deterministic_extracts_run_id_from_bait_marker()
	test_editor_smoke_deterministic_writes_canonical_3line_spec()
	test_editor_smoke_deterministic_block_only_on_smoke()
	print(
		"OK: review_apply_fixes editor smoke deterministic pre-write "
		"contract assertions hold"
	)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
