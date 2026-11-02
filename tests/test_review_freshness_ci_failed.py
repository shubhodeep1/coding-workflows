#!/usr/bin/env python3
"""A freshness-update head whose CI failed goes back to review/autofix (Q103).

PR #7153 was updated from its base twice (the cap), the updated head skipped
the reviewers (scripts/review_head_gate.sh freshness-sync), CI failed on the
combined tree, and the deterministic-skip-merge job refused the merge and
exited. Nothing ran review/autofix on that head again. The job now runs
scripts/review_freshness_ci_failed.sh, which marks the head and re-dispatches
the PR's review workflow; the marker makes the gate review that head
normally. These tests run the real script against a fake `gh`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "review_freshness_ci_failed.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "review_autofix.yml"
GATE_STEP = ROOT / "scripts" / "review_autofix_step_evaluate_gate.sh"
HEAD = "e" * 40
FAKE_GH = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as out:
    out.write(json.dumps(args) + "\\n")
if args[:1] == ["api"] and os.environ.get("FAKE_FAIL_COMMENT") == "1":
    sys.exit(1)
if args[:2] == ["workflow", "run"] and args[2] in os.environ.get("FAKE_FAIL_WORKFLOWS", "").split(","):
    sys.exit(1)
'''


def _run(**overrides: str) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		bin_dir = tmp / "bin"
		bin_dir.mkdir()
		gh = bin_dir / "gh"
		gh.write_text(FAKE_GH, encoding="utf-8")
		gh.chmod(0o755)
		log = tmp / "gh.log"
		env = {
			**os.environ,
			"PATH": f"{bin_dir}:{os.environ['PATH']}",
			"FAKE_GH_LOG": str(log),
			"REPOSITORY": "o/r",
			"PR_NUMBER": "7153",
			"PR_HEAD_SHA": HEAD,
			"REVIEW_AUTOFIX_CALLER_WORKFLOW_REF": "o/r/.github/workflows/internal-review.yml@refs/heads/main",
			"ALLOW_WORKFLOW_EDITS": "true",
			"FRESHNESS_CI_FAILED_REDISPATCH_ENABLED": "true",
			"FRESHNESS_CI_FAILED_RETRY_SLEEP_SECS": "0",
			**overrides,
		}
		result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False)
		calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
		return result, calls


def test_marks_the_head_then_dispatches_the_review_workflow() -> None:
	result, calls = _run()
	assert result.returncode == 0, result.stderr
	assert calls[0][:4] == ["api", "-X", "POST", "repos/o/r/issues/7153/comments"]
	body = calls[0][calls[0].index("-f") + 1]
	assert body.startswith("body=**CI failed on the updated head `eeeeeeeeeeee`.**")
	assert body.endswith(f"<!-- ai:freshness-ci-failed:v1 pr=7153 head={HEAD} -->")
	assert calls[1] == ["workflow", "run", "internal-review.yml", "--repo", "o/r", "-f", "pr_number=7153", "-f", "allow_workflow_edits=true"]
	assert len(calls) == 2
	assert f"FRESHNESS_CI_FAILED pr=7153 head={HEAD} outcome=redispatched workflow=internal-review.yml" in result.stdout


def test_dispatch_falls_through_the_review_workflow_names() -> None:
	result, calls = _run(
		REVIEW_AUTOFIX_CALLER_WORKFLOW_REF="o/r/.github/workflows/my-review.yml@refs/heads/main",
		FAKE_FAIL_WORKFLOWS="ai-review.yml,internal-review.yml",
		ALLOW_WORKFLOW_EDITS="",
	)
	assert result.returncode == 0, result.stderr
	assert [call[2] for call in calls[1:]] == ["ai-review.yml", "internal-review.yml", "my-review.yml"]
	assert calls[-1][-1] == "allow_workflow_edits=false"
	assert "outcome=redispatched workflow=my-review.yml" in result.stdout


def test_every_dispatch_failing_warns_and_succeeds() -> None:
	result, calls = _run(FAKE_FAIL_WORKFLOWS="ai-review.yml,internal-review.yml,review_autofix.yml")
	assert result.returncode == 0
	assert [call[2] for call in calls[1:]] == ["internal-review.yml", "ai-review.yml", "review_autofix.yml"]
	assert "outcome=dispatch_failed" in result.stdout


def test_a_failed_marker_post_does_not_dispatch() -> None:
	result, calls = _run(FAKE_FAIL_COMMENT="1")
	assert result.returncode == 0
	assert len(calls) == 3 and all(call[0] == "api" for call in calls)
	assert "outcome=marker_failed" in result.stdout


def test_retry_sleep_is_bounded() -> None:
	text = SCRIPT.read_text(encoding="utf-8")
	assert '[[ "${retry_sleep}" =~ ^[0-9]{1,2}$ ]] && [ "$((10#${retry_sleep}))" -le 60 ]; } || retry_sleep=5' in text


def test_switch_defaults_on_and_false_turns_it_off() -> None:
	for value in ("", "TRUE"):
		result, calls = _run(FRESHNESS_CI_FAILED_REDISPATCH_ENABLED=value)
		assert "outcome=redispatched" in result.stdout, value
		assert len(calls) == 2
	result, calls = _run(FRESHNESS_CI_FAILED_REDISPATCH_ENABLED="false")
	assert "outcome=disabled" in result.stdout and calls == []
	result, calls = _run(FRESHNESS_CI_FAILED_REDISPATCH_ENABLED="maybe")
	assert "::warning::FRESHNESS_CI_FAILED_REDISPATCH_ENABLED has an unrecognised value" in result.stdout
	assert calls == []


def test_invalid_inputs_do_nothing() -> None:
	for overrides in ({"PR_NUMBER": "0"}, {"PR_NUMBER": "7153;x"}, {"PR_HEAD_SHA": "abc"}, {"REPOSITORY": "o/../r"}, {"REPOSITORY": "o r"}):
		result, calls = _run(**overrides)
		assert result.returncode == 0
		assert "outcome=invalid_input" in result.stdout, overrides
		assert calls == [], overrides


def test_deterministic_skip_merge_job_runs_it_only_for_a_failed_freshness_head() -> None:
	text = WORKFLOW.read_text(encoding="utf-8")
	job = text[text.index("\n  deterministic-skip-merge:\n") + 1:]
	next_job = re.search(r"\n  [a-z][a-z0-9_-]*:\n", job[1:])
	job = job[: next_job.start() + 1] if next_job else job
	assert "            scripts/review_freshness_ci_failed.sh\n" in job
	assert "FRESHNESS_CI_FAILED_REDISPATCH_ENABLED: ${{ vars.FRESHNESS_CI_FAILED_REDISPATCH_ENABLED || 'true' }}" in job
	assert "REVIEW_AUTOFIX_CALLER_WORKFLOW_REF: ${{ github.workflow_ref }}" in job
	assert "FRESHNESS_CI_FAILED_RETRY_SLEEP_SECS: ${{ vars.FRESHNESS_CI_FAILED_RETRY_SLEEP_SECS || '5' }}" in job
	call = job.index('if [ "${DET_SKIP_REASON:-}" = "freshness_sync" ] && [ "${PR_CHECKS_WAIT_OUTCOME:-}" = "failed" ]; then')
	guarded = job[call: call + 600]
	assert 'if [ "${freshness_helper_verified}" = "true" ] && [ -f .codex-freshness-src/scripts/review_freshness_ci_failed.sh ]; then' in guarded
	assert "bash .codex-freshness-src/scripts/review_freshness_ci_failed.sh || true" in guarded
	# Runs before the label gate exits on a refused merge.
	assert call < job.index('if [ "${auto_merge_ready_labels_allowed}" != "true" ]; then')
	assert job.index("freshness_helper_verified=false") < job.index("freshness_helper_verified=true")


def test_gate_reads_the_marker_comments() -> None:
	assert 'or ($b | contains("<!-- ai:freshness-ci-failed:v1 "))) |' in GATE_STEP.read_text(encoding="utf-8")


def test_gate_marker_also_suppresses_docs_only_and_small_diff_skips() -> None:
	text = GATE_STEP.read_text(encoding="utf-8")
	block = text[text.index('FRESHNESS_CI_FAILED_SKIP_SUPPRESSED="false"'):]
	assert '[ "${gate_marker_fetch_state}" = "ok" ]' in block[:1200]
	assert "reason=freshness_ci_failed_unverifiable" in block[:1200]
	assert "reason=freshness_ci_failed pr=${PR_NUMBER} candidate=${candidate_skip_reason}" in block
	final = block[block.index('if [ "${candidate_skip}" = "true" ] && [ "${MATERIALITY_SKIP_SUPPRESSED}" != "true" ]'):]
	assert '[ "${FRESHNESS_CI_FAILED_SKIP_SUPPRESSED}" != "true" ]' in final[:600]


def test_dispatch_function_is_shared_and_returns_1_when_every_candidate_fails() -> None:
	# review_autofix_step_deterministic_skip_security_pass.sh sources this
	# file and calls the function directly (#8333); main stays guarded.
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		bin_dir = tmp / "bin"
		bin_dir.mkdir()
		gh = bin_dir / "gh"
		gh.write_text(FAKE_GH, encoding="utf-8")
		gh.chmod(0o755)
		log = tmp / "gh.log"
		env = {
			**os.environ,
			"PATH": f"{bin_dir}:{os.environ['PATH']}",
			"FAKE_GH_LOG": str(log),
			"REPOSITORY": "o/r",
			"PR_NUMBER": "8290",
			"REVIEW_AUTOFIX_CALLER_WORKFLOW_REF": "o/r/.github/workflows/ai-review.yml@refs/heads/main",
			"ALLOW_WORKFLOW_EDITS": "",
		}
		call = f'source "{SCRIPT}" && freshness_ci_failed_dispatch_review_workflow; echo "rc=$?"'
		result = subprocess.run(["bash", "-c", call], env=env, capture_output=True, text=True, check=False)
		assert result.stdout == "ai-review.yml\nrc=0\n", result.stdout
		log.unlink()
		env["FAKE_FAIL_WORKFLOWS"] = "ai-review.yml,internal-review.yml,review_autofix.yml"
		result = subprocess.run(["bash", "-c", call], env=env, capture_output=True, text=True, check=False)
		assert result.stdout == "rc=1\n", result.stdout
		calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
		assert [c[2] for c in calls] == ["ai-review.yml", "internal-review.yml", "review_autofix.yml"]
		assert all(c[-1] == "allow_workflow_edits=false" for c in calls)
		# Sourcing posts no marker comment and runs no main.
		assert not any(c[0] == "api" for c in calls)


if __name__ == "__main__":
	import sys

	failures = 0
	for name, func in sorted(globals().items()):
		if name.startswith("test_") and callable(func):
			try:
				func()
				print(f"PASS {name}")
			except Exception as exc:  # noqa: BLE001 - report every failing test
				failures += 1
				print(f"FAIL {name}: {exc!r}")
	sys.exit(1 if failures else 0)
