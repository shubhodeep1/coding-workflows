#!/usr/bin/env python3
"""Contract: reviewer attempt prompt copies must be namespaced per slot.

Background — the shared attempt-prompt race (all reviewers fail on empty stdin)
===============================================================================

``scripts/review_run_reviewers.sh`` fans the active reviewer model set out as
concurrent background workers (``run_reviewer ... &`` in ``run_reviewer_pass``).
Each attempt makes a private, mutable copy of the pass prompt so per-attempt
additions (nag block) never contaminate the shared base prompt::

    reviewer_attempt_prompt_file="${prompt_file}.<slot>.attempt_${attempt_number}"

When no model-family overlay exists, ``prepare_reviewer_prompt_for_model``
returns the SHARED pass prompt path for every slot. Before the slot suffix was
added, every concurrent worker then derived the SAME attempt path
(``<pass prompt>.attempt_1``) and raced on it: ``cp`` truncates it in place,
the nag block appends to it, ``sanitize_codex_prompt_file`` rewrites it via
iconv → tmp → ``mv`` (and mv's an *empty* tmp over the path whenever iconv
reads mid-truncation, after which every later sanitize preserves the
emptiness), while each worker's codex reads it as stdin. One bad interleaving
left the file empty and every reviewer failed non-retryably with::

    Reading prompt from stdin...
    No prompt provided via stdin.

→ "Pass 2 complete: 0 reviewers successful." → "All reviewers failed." →
review_autofix job failure. Observed in consumer run
tele-funtoken-msg-scoring/actions/runs/32222803753 (PR #3721, pass 2: 6/6
reviewers failed identically ~1.5s after launch while the assembled pass-2
prompt file was 571935 bytes).

This test pins the two halves of the fix:

1. the attempt prompt path embeds the per-slot ``safe_name`` so concurrent
   slots never share a mutable attempt file;
2. the pre-launch guard restores/flags an unexpectedly empty effective prompt
   instead of letting codex fail the slot silently.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "review_run_reviewers.sh"


def _script_text() -> str:
	return SCRIPT.read_text(encoding="utf-8")


def test_attempt_prompt_path_is_namespaced_per_slot() -> None:
	text = _script_text()
	assignments = re.findall(
		r'^\s*reviewer_attempt_prompt_file="([^"]+)"\s*$', text, flags=re.M
	)
	# The empty-string reset in the cp-failure fallback branch and in the
	# local declaration is fine; every non-empty assignment must carry both
	# the slot namespace and the attempt number.
	real = [a for a in assignments if a]
	assert real, "expected at least one reviewer_attempt_prompt_file assignment"
	for value in real:
		assert "${safe_name" in value, (
			"reviewer attempt prompt path must embed the per-slot safe_name; "
			"a shared '<prompt>.attempt_N' path is cp-truncated / sanitized / "
			f"read concurrently by every reviewer worker (got: {value!r})"
		)
		assert "attempt_${attempt_number}" in value, (
			f"attempt prompt path must stay per-attempt (got: {value!r})"
		)


def test_empty_effective_prompt_is_guarded_before_launch() -> None:
	text = _script_text()
	guard = re.search(
		r'if \[ ! -s "\$\{reviewer_effective_prompt_file\}" \]', text
	)
	assert guard, (
		"expected a pre-launch guard on an empty reviewer_effective_prompt_file "
		"(codex fails a slot non-retryably on empty stdin)"
	)
	launch = text.find('-- "${reviewer_codex_cmd[@]}" < "${reviewer_effective_prompt_file}"')
	assert launch != -1, "expected the codex launch redirect to be present"
	assert guard.start() < launch, (
		"the empty-prompt guard must run before the codex launch redirect"
	)
