#!/usr/bin/env python3
"""Infra-failed review rounds do not spend the review budgets (issue #9917).

On PR #9853 the review editor failed three times because the Claude pool
broker refused the run (`broker_refused_workflow_sha_unverifiable`, then
`AI_ENGINE_FALLBACK_REFUSED role=REVIEW_EDITOR reason=no_credential`, run
44871258266). Those rounds reviewed nothing but still counted toward the
sweep's review-round cap of 10, so the PR was parked with ai:needs-human.

- review_round_infra_failure_reason (scripts/review_sweep_round_cap.sh)
  classifies such a round from the runner's own files;
- review_autofix.yml skips the commit for it (so the autofix iteration
  counter does not advance) and, for a dispatched run, uploads the
  `review-round-infra-failed` artifact;
- review_sweep_round_counts drops runs carrying that artifact;
- REVIEW_INFRA_FAILURE_EXCLUDED_ENABLED=false restores today's behaviour.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "scripts" / "review_sweep_round_cap.sh"
REVIEW_WF = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
SWEEP_WF = REPO_ROOT / ".github" / "workflows" / "review_autofix_sweep.yml"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")

_spec = importlib.util.spec_from_file_location("_round_cap_harness_9917", REPO_ROOT / "tests" / "test_review_sweep_round_cap.py")
assert _spec is not None and _spec.loader is not None
cap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cap)


# ---------------------------------------------------------------------------
# review_round_infra_failure_reason
# ---------------------------------------------------------------------------


def _classify(tmp_path: Path, *, records: str | None = None, editor_stderr: str | None = None, reviewers_stderr: str | None = None,
		summary: str | None = None, **env_extra: str) -> str:
	runtime = tmp_path / "runtime"
	runtime.mkdir(parents=True, exist_ok=True)
	env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_POOL_", "CLAUDE_CAPACITY_", "REVIEW_INFRA_", "AI_ENGINE_", "EDITOR_SUMMARY"))}
	env.update({
		"RUNNER_TEMP": str(tmp_path),
		"RUNTIME_DIR": str(runtime),
		"EDITOR_SUMMARY_FILE": str(runtime / "editor_summary.txt"),
	})
	if records is not None:
		(tmp_path / "ai-engine-fallback-records.tsv").write_text(records, encoding="utf-8")
	if editor_stderr is not None:
		(runtime / "editor_stage_stderr.txt").write_text(editor_stderr, encoding="utf-8")
	if reviewers_stderr is not None:
		(runtime / "reviewers_stage_stderr.txt").write_text(reviewers_stderr, encoding="utf-8")
	if summary is not None:
		(runtime / "editor_summary.txt").write_text(summary, encoding="utf-8")
	env.update(env_extra)
	proc = subprocess.run(["bash", "-c", f'set -euo pipefail; source "{HELPER}"; review_round_infra_failure_reason'],
		capture_output=True, text=True, env=env, check=False, timeout=60)
	assert proc.returncode == 0, proc.stderr
	return proc.stdout.strip()


def _record(role: str = "REVIEW_EDITOR", reason: str = "no_credential", action: str = "refused") -> str:
	return f"v1\t1795450964\t{role}\t{reason}\tnon_capacity\t{action}\tcapacity\n"


REFUSED_LINE = "::error::AI_ENGINE_FALLBACK_REFUSED role=REVIEW_EDITOR reason=no_credential\n"


def _apply_fixes_partial_finalize_summary(reason: str) -> str:
	"""The real fallback summary scripts/review_apply_fixes.sh writes for one partial-finalize reason."""
	text = (REPO_ROOT / "scripts" / "review_apply_fixes.sh").read_text(encoding="utf-8")
	anchor = {
		"soft_deadline": 'if [ "${editor_partial_finalize_reason}" = "soft_deadline" ]; then',
		"refusal": 'elif [ "${editor_partial_finalize_reason}" = "refusal" ]; then',
		"recoverable_failure": "  else\n    cat > \"${EDITOR_SUMMARY_FILE}\" <<'__EDITOR_SUMMARY__'",
	}[reason]
	start = text.index("<<'__EDITOR_SUMMARY__'\n", text.index(anchor)) + len("<<'__EDITOR_SUMMARY__'\n")
	body = text[start:text.index("\n__EDITOR_SUMMARY__\n", start)]
	assert "unavailable (partial finalize" in body, reason
	return body + "\n"


@pytest.mark.parametrize("kwargs, expected", [
	# The records file the host writes (scripts/ai_engine.sh).
	({"records": _record()}, "no_credential"),
	({"records": _record(reason="all_gated", action="wait")}, "all_gated"),
	({"records": _record(role="REVIEWER_PANEL", reason="all_gated")}, "all_gated"),
	# Not infra: another role, another reason, or a codex fallback that ran.
	({"records": _record(role="IMPLEMENT")}, ""),
	({"records": _record(reason="support_missing")}, ""),
	({"records": _record(action="codex")}, ""),
	# A sandboxed call whose record never reached the host file.
	({"editor_stderr": "noise\n" + REFUSED_LINE}, "no_credential"),
	({"reviewers_stderr": "::error::AI_ENGINE_FALLBACK_REFUSED role=REVIEW_REVIEWER reason=all_gated\n"}, "all_gated"),
	({"editor_stderr": "::error::AI_ENGINE_FALLBACK_REFUSED role=IMPLEMENT reason=no_credential\n"}, ""),
	({"editor_stderr": "::error::AI_ENGINE_FALLBACK_REFUSED role=REVIEW_EDITOR reason=no_credentialx\n"}, ""),
	# The pool step's broker refusal.
	({"CLAUDE_POOL_STEP_AVAILABLE": "false", "CLAUDE_POOL_STEP_REASON": "broker_refused_workflow_sha_unverifiable"}, "broker_refused_workflow_sha_unverifiable"),
	({"CLAUDE_POOL_STEP_AVAILABLE": "true", "CLAUDE_POOL_STEP_REASON": "broker_refused_workflow_sha_unverifiable"}, ""),
	({"CLAUDE_POOL_STEP_AVAILABLE": "false", "CLAUDE_POOL_STEP_REASON": "broker_not_configured"}, ""),
	({"CLAUDE_POOL_STEP_AVAILABLE": "false", "CLAUDE_POOL_STEP_REASON": "broker_refused_$(id)"}, ""),
	# The editor's fallback summary is not usable output.
	({"records": _record(), "summary": "Changes made:\n- unavailable (editor fallback)\n"}, "no_credential"),
	({"records": _record(), "summary": "The editor failed before producing a summary."}, "no_credential"),
	({"records": _record(), "summary": ""}, "no_credential"),
	# The partial-finalize summaries review_apply_fixes.sh writes after every
	# editor attempt failed are not usable output either.
	({"records": _record(), "summary": _apply_fixes_partial_finalize_summary("recoverable_failure")}, "no_credential"),
	({"editor_stderr": REFUSED_LINE, "summary": _apply_fixes_partial_finalize_summary("refusal")}, "no_credential"),
	({"records": _record(), "summary": _apply_fixes_partial_finalize_summary("soft_deadline")}, "no_credential"),
	# ...but a partial-finalize summary with no infra signal still counts.
	({"summary": _apply_fixes_partial_finalize_summary("recoverable_failure")}, ""),
	# The all_gated capacity defer fails before the reviewers and the editor run.
	({"CLAUDE_CAPACITY_WAIT_STEP_OUTCOME": "failure", "CLAUDE_POOL_STEP_REASON": "all_gated", "CLAUDE_POOL_STEP_AVAILABLE": "false"}, "all_gated"),
	({"CLAUDE_CAPACITY_WAIT_STEP_OUTCOME": "success", "CLAUDE_POOL_STEP_REASON": "all_gated", "CLAUDE_POOL_STEP_AVAILABLE": "false"}, ""),
	({"CLAUDE_CAPACITY_WAIT_STEP_OUTCOME": "failure", "CLAUDE_POOL_STEP_REASON": "no_accounts"}, ""),
	# Only the engine's own annotation line counts, not echoed text quoting it.
	({"reviewers_stderr": "model said: " + REFUSED_LINE}, ""),
	# A round that landed real editor output keeps counting.
	({"records": _record(), "summary": "Changes made:\n- fixed the guard in scripts/x.sh\n"}, ""),
	# No signal at all.
	({}, ""),
])
def test_classifier_truth_table(tmp_path: Path, kwargs: dict, expected: str) -> None:
	assert _classify(tmp_path, **kwargs) == expected


def test_classifier_kill_switch_prints_nothing(tmp_path: Path) -> None:
	for value in ("false", "FALSE", "0", "off"):
		assert _classify(tmp_path / value, records=_record(), editor_stderr=REFUSED_LINE, REVIEW_INFRA_FAILURE_EXCLUDED_ENABLED=value) == ""
	assert _classify(tmp_path / "on", records=_record(), REVIEW_INFRA_FAILURE_EXCLUDED_ENABLED="true") == "no_credential"


def test_classifier_ignores_symlinked_files(tmp_path: Path) -> None:
	target = tmp_path / "elsewhere.tsv"
	target.write_text(_record(), encoding="utf-8")
	link = tmp_path / "linked.tsv"
	link.symlink_to(target)
	assert _classify(tmp_path / "a", AI_ENGINE_FALLBACK_RECORDS_FILE=str(link)) == ""
	runtime = tmp_path / "b" / "runtime"
	runtime.mkdir(parents=True)
	stderr_target = tmp_path / "stderr.txt"
	stderr_target.write_text(REFUSED_LINE, encoding="utf-8")
	(runtime / "editor_stage_stderr.txt").symlink_to(stderr_target)
	assert _classify(tmp_path / "b") == ""


# ---------------------------------------------------------------------------
# review_sweep_round_counts
# ---------------------------------------------------------------------------


def _counts(tmp_path: Path, env_extra: dict[str, str] | None = None, **kwargs: object) -> subprocess.CompletedProcess:
	bin_dir = cap._setup(tmp_path, helper=False, **kwargs)
	script = f'source "{HELPER}"; review_sweep_round_counts o/r main 72'
	return subprocess.run(["bash", "-c", script], env=cap._env(tmp_path, bin_dir, **(env_extra or {})),
		capture_output=True, text=True, cwd=tmp_path, check=False)


def _marker(run_id: int, created_at: str = "2099-01-01T00:00:00Z") -> dict:
	return {"run": run_id, "created_at": created_at, "expired": False}


def test_a_no_credential_round_is_not_counted(tmp_path: Path) -> None:
	runs = [cap._round(5, id=101), cap._round(5, id=102), cap._round(5, id=103), cap._round(6, id=201)]
	raw_file = tmp_path / "raw.json"
	result = _counts(tmp_path, {"REVIEW_SWEEP_ROUND_RAW_COUNTS_FILE": str(raw_file)},
		rounds=[cap._page(*runs)], artifacts=[[_marker(102), _marker(103)], [_marker(999)]])
	assert result.returncode == 0, result.stderr
	assert json.loads(result.stdout) == {"5": 1, "6": 1}
	assert "REVIEW_ROUND_INFRA_EXCLUDED pr=5 run=102 reason=infra_failed" in result.stderr
	assert "REVIEW_ROUND_INFRA_EXCLUDED pr=5 run=103 reason=infra_failed" in result.stderr
	assert "outcome=counted window_hours=72 runs=2 prs=2 truncated=false" in result.stderr
	# The counts before the exclusion, for the sweep's dispatch backoff.
	assert json.loads(raw_file.read_text(encoding="utf-8")) == {"5": 3, "6": 1}
	calls = [json.loads(line) for line in cap._lines(tmp_path, "calls.log")]
	artifact_calls = [call for call in calls if "repos/o/r/actions/artifacts" in call]
	assert len(artifact_calls) == 1
	assert "--paginate" in artifact_calls[0] and "name=review-round-infra-failed" in artifact_calls[0] and "per_page=100" in artifact_calls[0]


def test_a_marker_older_than_the_window_is_ignored(tmp_path: Path) -> None:
	result = _counts(tmp_path, rounds=[cap._page(cap._round(5, id=101))], artifacts=[[_marker(101, created_at="2000-01-01T00:00:00Z")]])
	assert json.loads(result.stdout) == {"5": 1}
	assert "REVIEW_ROUND_INFRA_EXCLUDED" not in result.stderr


@pytest.mark.parametrize("flags, artifacts", [
	(("artifacts_fail",), None),
	((), [{"message": "Server Error"}]),
])
def test_a_failed_artifacts_listing_gives_todays_counts(tmp_path: Path, flags: tuple[str, ...], artifacts: list | None) -> None:
	result = _counts(tmp_path, rounds=[cap._page(cap._round(5, id=101), cap._round(5, id=102))], artifacts=artifacts, flags=flags)
	assert result.returncode == 0, result.stderr
	assert json.loads(result.stdout) == {"5": 2}
	assert "REVIEW_SWEEP_ROUND_CAP outcome=infra_marker_read_failed window_hours=72" in result.stderr
	assert "REVIEW_ROUND_INFRA_EXCLUDED" not in result.stderr


def test_kill_switch_makes_no_artifacts_call_and_counts_every_round(tmp_path: Path) -> None:
	result = _counts(tmp_path, {"REVIEW_INFRA_FAILURE_EXCLUDED_ENABLED": "false"},
		rounds=[cap._page(cap._round(5, id=101), cap._round(5, id=102))], artifacts=[[_marker(102)]])
	assert result.returncode == 0, result.stderr
	assert json.loads(result.stdout) == {"5": 2}
	assert not any("actions/artifacts" in line for line in cap._lines(tmp_path, "calls.log"))
	assert "REVIEW_ROUND_INFRA_EXCLUDED" not in result.stderr


# ---------------------------------------------------------------------------
# The sweep step
# ---------------------------------------------------------------------------


def _infra_rounds(pr: int, total: int, infra: int) -> tuple[list[dict], list[list[dict]]]:
	runs = [cap._round(pr, id=500 + i) for i in range(total)]
	return [cap._page(*runs)], [[_marker(500 + i) for i in range(infra)]]


def test_sweep_dispatches_a_pr_whose_rounds_were_infra_failed(tmp_path: Path) -> None:
	rounds, artifacts = _infra_rounds(5, 15, 10)
	result = cap._run_sweep(tmp_path, prs=[cap._pr(5)], rounds=rounds, artifacts=artifacts)
	assert result.returncode == 0, result.stderr
	assert cap._dispatched(tmp_path) == ["5"]
	assert "reason=review_round_cap" not in result.stdout
	assert not cap._lines(tmp_path, "labels.log") and not cap._lines(tmp_path, "comments.log")


def test_sweep_backs_off_at_twice_the_cap_without_a_label(tmp_path: Path) -> None:
	rounds, artifacts = _infra_rounds(5, 20, 15)
	result = cap._run_sweep(tmp_path, prs=[cap._pr(5), cap._pr(6)], rounds=rounds, artifacts=artifacts)
	assert result.returncode == 0, result.stderr
	assert cap._dispatched(tmp_path) == ["6"]
	assert "AUTOFIX_SWEEP_SKIP pr=#5 reason=review_round_infra_backoff rounds=5 raw_rounds=20 max=10 window_hours=48" in result.stdout
	assert "reason=review_round_cap " not in result.stdout
	assert not cap._lines(tmp_path, "labels.log") and not cap._lines(tmp_path, "comments.log")


def test_sweep_kill_switch_restores_todays_cap(tmp_path: Path) -> None:
	rounds, artifacts = _infra_rounds(5, 12, 12)
	result = cap._run_sweep(tmp_path, prs=[cap._pr(5)], rounds=rounds, artifacts=artifacts,
		env={"REVIEW_INFRA_FAILURE_EXCLUDED_ENABLED": "false"})
	assert result.returncode == 0, result.stderr
	assert cap._dispatched(tmp_path) == []
	assert "AUTOFIX_SWEEP_SKIP pr=#5 reason=review_round_cap rounds=12" in result.stdout
	assert cap._lines(tmp_path, "labels.log") == ["api repos/o/r/issues/5/labels -f labels[]=ai:needs-human"]


# ---------------------------------------------------------------------------
# Workflow contracts (text, so the module needs no PyYAML)
# ---------------------------------------------------------------------------


def _step_block(text: str, name: str) -> str:
	start = text.index(f"      - name: {name}\n")
	nxt = re.compile(r"^      (?:- name: |# )", re.M).search(text, start + 1)
	return text[start:nxt.start() if nxt else len(text)]


def test_review_run_classifies_the_round_and_skips_the_commit() -> None:
	text = REVIEW_WF.read_text(encoding="utf-8")
	classify = _step_block(text, "Classify infra-failed review round")
	assert "        id: infra_round\n" in classify
	# The all_gated capacity defer fails before the reviewers run (skipped),
	# so the classifier also runs when that defer failed.
	assert "        if: always() && env.PR_CLOSED != 'true' && steps.retrigger_guard.outputs.max_iterations_reached != 'true' && (steps.reviewers.outcome != 'skipped' || steps.claude_capacity_wait.outcome == 'failure')\n" in classify
	assert "CLAUDE_CAPACITY_WAIT_STEP_OUTCOME: ${{ steps.claude_capacity_wait.outcome }}" in classify
	assert "        continue-on-error: true\n" in classify
	assert "CLAUDE_POOL_STEP_AVAILABLE: ${{ steps.claude_pool.outputs.available }}" in classify
	assert "CLAUDE_POOL_STEP_REASON: ${{ steps.claude_pool.outputs.reason }}" in classify
	assert "REVIEW_INFRA_FAILURE_EXCLUDED_ENABLED: ${{ vars.REVIEW_INFRA_FAILURE_EXCLUDED_ENABLED || 'true' }}" in classify
	assert '"${SUPPORT_SCRIPTS_DIR:-}/review_sweep_round_cap.sh"' in classify
	assert ".codex-workflow-src/scripts/review_sweep_round_cap.sh" in classify
	assert "REVIEW_ROUND_INFRA_EXCLUDED pr=${PR_NUMBER:-} run=${GITHUB_RUN_ID:-} reason=" in classify
	# The autofix iteration counter only counts [ai-autofix] commits, so a
	# skipped commit is an iteration that does not advance it.
	commit = _step_block(text, "Commit changes")
	commit_if = next(line for line in commit.splitlines() if line.startswith("        if: "))
	assert commit_if.endswith(" && steps.infra_round.outputs.infra_failed != 'true'")
	assert text.index("      - name: Classify infra-failed review round\n") < text.index("      - name: Commit changes\n")
	assert text.index("      - name: Apply fixes with editor model\n") < text.index("      - name: Classify infra-failed review round\n")
	upload = _step_block(text, "Upload infra-failed review round marker")
	assert "        if: always() && steps.infra_round.outputs.infra_failed == 'true' && github.event_name == 'workflow_dispatch'\n" in upload
	assert "        uses: actions/upload-artifact@v6\n" in upload
	assert "          name: review-round-infra-failed\n" in upload
	assert "          retention-days: 3\n" in upload
	assert "          if-no-files-found: ignore\n" in upload
	# The helper rides the soft (fail-open) support list.
	soft = text.split("REVIEW_PREFLIGHT_SOFT_SUPPORT_SCRIPTS: >-\n", 1)[1].split("\n  #", 1)[0].split()
	assert "review_sweep_round_cap.sh" in soft


def test_review_run_step_marks_the_round_and_writes_the_marker(tmp_path: Path) -> None:
	text = REVIEW_WF.read_text(encoding="utf-8")
	body = _step_block(text, "Classify infra-failed review round").split("        run: |\n", 1)[1]
	run = "\n".join(line[10:] for line in body.splitlines())
	support = tmp_path / "support"
	support.mkdir()
	shutil.copy(HELPER, support / "review_sweep_round_cap.sh")
	runtime = tmp_path / "runtime"
	runtime.mkdir()
	(runtime / "editor_stage_stderr.txt").write_text(REFUSED_LINE, encoding="utf-8")
	out = tmp_path / "out.txt"
	base = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_POOL_", "CLAUDE_CAPACITY_", "REVIEW_INFRA_", "AI_ENGINE_"))}
	env = {**base, "SUPPORT_SCRIPTS_DIR": str(support), "GITHUB_WORKSPACE": str(tmp_path), "RUNNER_TEMP": str(tmp_path),
		"RUNTIME_DIR": str(runtime), "EDITOR_SUMMARY_FILE": str(runtime / "editor_summary.txt"), "GITHUB_OUTPUT": str(out),
		"PR_NUMBER": "9853", "GITHUB_RUN_ID": "44871258266", "AUTOFIX_FAILURE_HEAD_SHA": "a" * 40}
	proc = subprocess.run(["bash", "-e", "-c", run], capture_output=True, text=True, env=env, check=False, timeout=60)
	assert proc.returncode == 0, proc.stderr
	assert "REVIEW_ROUND_INFRA_EXCLUDED pr=9853 run=44871258266 reason=no_credential" in proc.stdout
	assert out.read_text(encoding="utf-8").splitlines() == ["infra_failed=true", "reason=no_credential"]
	assert (tmp_path / "review-round-infra-failed.txt").read_text(encoding="utf-8") == f"pr=9853 run=44871258266 head={'a' * 40} reason=no_credential\n"
	# A missing helper means not infra-failed (today's commit and count).
	shutil.rmtree(support)
	out.unlink()
	proc = subprocess.run(["bash", "-e", "-c", run], capture_output=True, text=True, env=env, check=False, timeout=60)
	assert proc.returncode == 0, proc.stderr
	assert out.read_text(encoding="utf-8").splitlines() == ["infra_failed=false", "reason="]


def test_sweep_passes_the_switch_with_its_default() -> None:
	text = SWEEP_WF.read_text(encoding="utf-8")
	step = text[text.index(cap.STEP_MARKER):]
	step = step[:step.index(cap.RUN_HEADER)]
	assert "          REVIEW_INFRA_FAILURE_EXCLUDED_ENABLED: ${{ vars.REVIEW_INFRA_FAILURE_EXCLUDED_ENABLED || 'true' }}\n" in step
	assert 'REVIEW_SWEEP_ROUND_RAW_COUNTS_FILE="${round_cap_raw_counts_file}" review_sweep_round_counts' in cap._extract_step_script()
