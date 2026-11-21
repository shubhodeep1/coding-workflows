"""Contract tests: the review-blocked judge skips an open draft PR (issue #7653).

``scripts/review_rb_judge.sh`` reads the PR once (``_pr_meta``) for its
closed-PR short-circuit. Directly after that check it skips an open draft:
``RB_JUDGE_DRAFT_SKIP`` log line, ``judge_handled=true`` /
``judge_action=skip`` / ``judge_skip_reason=draft_pr``, no ``PR_CLOSED``, and
nothing after it runs (no linked-issue lookup, no model call). Only a JSON
boolean ``true`` counts as a draft. The "Telegram review-blocked judge
decision" step in ``review_autofix.yml`` sends nothing for that skip.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "review_rb_judge.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"

START_ANCHOR = '_pr_meta="$(gh_retry _safe_gh_jq'
END_ANCHOR = 'ensure_label_exists "ai:ready-to-merge"'

needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required by the extracted block")


def _extract_block() -> str:
	text = SCRIPT.read_text(encoding="utf-8")
	start = text.find(START_ANCHOR)
	assert start != -1, f"anchor not found in {SCRIPT}: {START_ANCHOR}"
	end = text.find(END_ANCHOR, start)
	assert end != -1, f"anchor not found in {SCRIPT}: {END_ANCHOR}"
	return text[start:end]


def _run_block(pr_payload: object | None, *, fetch_fails: bool = False) -> tuple[subprocess.CompletedProcess[str], str, str, list]:
	block = _extract_block()
	with tempfile.TemporaryDirectory(prefix="test_rb_judge_draft_skip_") as td:
		tmp = Path(td)
		bin_dir = tmp / "bin"
		bin_dir.mkdir()
		gh_log = tmp / "gh_calls.jsonl"
		gh_shim = bin_dir / "gh"
		gh_shim.write_text(
			"#!/usr/bin/env python3\n"
			"import json, os, sys\n"
			"with open(os.environ['GH_CALL_LOG'], 'a', encoding='utf-8') as fh:\n"
			"\tfh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
			"sys.exit(0)\n",
			encoding="utf-8",
		)
		gh_shim.chmod(0o755)
		fixture = tmp / "pr.json"
		if isinstance(pr_payload, str):
			fixture.write_text(pr_payload, encoding="utf-8")
		else:
			fixture.write_text(json.dumps(pr_payload if pr_payload is not None else {}), encoding="utf-8")
		github_output = tmp / "github_output.txt"
		github_output.write_text("", encoding="utf-8")
		github_env = tmp / "github_env.txt"
		github_env.write_text("", encoding="utf-8")
		harness = tmp / "harness.sh"
		harness.write_text(
			"set -euo pipefail\n"
			'gh_retry() { "$@"; }\n'
			'_safe_gh_jq() { if [ "${FIXTURE_FAIL:-0}" = "1" ]; then return 1; fi; cat "${PR_FIXTURE}"; }\n'
			+ block
			+ "\necho PROCEEDED\n",
			encoding="utf-8",
		)
		env = {
			"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
			"GH_CALL_LOG": str(gh_log),
			"PR_FIXTURE": str(fixture),
			"FIXTURE_FAIL": "1" if fetch_fails else "0",
			"GITHUB_OUTPUT": str(github_output),
			"GITHUB_ENV": str(github_env),
			"PR_NUMBER": "42",
			"REPOSITORY": "owner/repo",
		}
		proc = subprocess.run(["bash", str(harness)], env=env, text=True, capture_output=True, timeout=60)
		calls = []
		if gh_log.exists():
			calls = [json.loads(line) for line in gh_log.read_text(encoding="utf-8").splitlines() if line.strip()]
		return proc, github_output.read_text(encoding="utf-8"), github_env.read_text(encoding="utf-8"), calls


def _pr(state: str = "open", **extra: object) -> dict:
	payload: dict = {"number": 42, "state": state, "merged": False, "merged_at": None}
	payload.update(extra)
	return payload


@needs_jq
def test_open_draft_skips() -> None:
	proc, output, env_out, calls = _run_block(_pr(draft=True))
	assert proc.returncode == 0, proc.stderr
	assert "PROCEEDED" not in proc.stdout
	lines = output.splitlines()
	assert "judge_handled=true" in lines
	assert "judge_action=skip" in lines
	assert "judge_skip_reason=draft_pr" in lines
	assert "RB_JUDGE_DRAFT_SKIP pr=42 outcome=skip reason=draft_pr" in proc.stdout.splitlines()
	assert "PR_CLOSED" not in env_out
	assert calls == []


@needs_jq
@pytest.mark.parametrize(
	"payload",
	[
		_pr(draft=False),
		_pr(),
		_pr(draft="true"),
		_pr(draft=1),
	],
	ids=["false", "missing", "string_true", "one"],
)
def test_non_draft_values_proceed(payload: dict) -> None:
	proc, output, env_out, _ = _run_block(payload)
	assert proc.returncode == 0, proc.stderr
	assert "PROCEEDED" in proc.stdout, proc.stdout + proc.stderr
	assert "judge_skip_reason" not in output
	assert "RB_JUDGE_DRAFT_SKIP" not in proc.stdout
	assert "PR_CLOSED" not in env_out


@needs_jq
@pytest.mark.parametrize("mode", ["fetch_fails", "empty_object"])
def test_unreadable_pr_proceeds(mode: str) -> None:
	if mode == "fetch_fails":
		proc, output, _, _ = _run_block(None, fetch_fails=True)
	else:
		proc, output, _, _ = _run_block({})
	assert proc.returncode == 0, proc.stderr
	assert "PROCEEDED" in proc.stdout, proc.stdout + proc.stderr
	assert "judge_skip_reason" not in output


@needs_jq
def test_closed_draft_keeps_pr_not_open() -> None:
	proc, output, env_out, _ = _run_block(_pr(state="closed", draft=True))
	assert proc.returncode == 0, proc.stderr
	assert "PROCEEDED" not in proc.stdout
	assert "judge_skip_reason=pr_not_open" in output.splitlines()
	assert "draft_pr" not in output
	assert "PR_CLOSED=true" in env_out.splitlines()
	assert "RB_JUDGE_DRAFT_SKIP" not in proc.stdout


@needs_jq
@pytest.mark.parametrize("draft", [True, False])
def test_merged_pr_proceeds(draft: bool) -> None:
	payload = _pr(state="closed", draft=draft, merged=True, merged_at="2026-10-18T00:00:00Z")
	proc, output, _, _ = _run_block(payload)
	assert proc.returncode == 0, proc.stderr
	assert "PROCEEDED" in proc.stdout, proc.stdout + proc.stderr
	assert "judge_skip_reason" not in output


def test_skip_precedes_linked_issue_lookup_once() -> None:
	text = SCRIPT.read_text(encoding="utf-8")
	assert text.count("RB_JUDGE_DRAFT_SKIP") == 1
	skip = text.index("RB_JUDGE_DRAFT_SKIP")
	assert skip < text.index("RB_LINKED_ISSUES_GRAPHQL_JSON=")
	assert text.index("judge_skip_reason=pr_not_open") < skip


def _telegram_step() -> str:
	workflow = WORKFLOW.read_text(encoding="utf-8")
	start = workflow.index("      - name: Telegram review-blocked judge decision")
	end = workflow.index("      - name: Check PR state before failure alerts", start)
	return workflow[start:end]


def test_telegram_step_suppresses_draft_skip_before_case() -> None:
	step = _telegram_step()
	assert "draft_pr" in step
	assert step.index("draft_pr") < step.index('case "${JUDGE_ACTION}"')


def _telegram_sends(env: dict[str, str]) -> list[str]:
	step = _telegram_step()
	body = step[step.index("        run: |\n") + len("        run: |\n"):]
	body = textwrap.dedent(body)
	body = re.sub(r"\$\{\{[^}]*\}\}", "x", body)
	with tempfile.TemporaryDirectory() as tmp:
		log = Path(tmp) / "tg.log"
		(Path(tmp) / "tg_helpers.sh").write_text(
			'tg_send_tracked() { printf \'%s\\n\' "$3" >> "${TG_LOG}"; }\n', encoding="utf-8"
		)
		run_env = {
			"PATH": "/usr/bin:/bin",
			"SUPPORT_SCRIPTS_DIR": tmp,
			"TG_LOG": str(log),
			"PR_NUMBER": "7",
			"MAX_AUTOFIX_ITERATIONS": "5",
		}
		run_env.update(env)
		result = subprocess.run(["bash", "-c", body], env=run_env, capture_output=True, text=True, check=False)
		assert result.returncode == 0, result.stderr
		return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


@needs_jq
def test_telegram_step_sends_nothing_for_draft_skip() -> None:
	sends = _telegram_sends({"JUDGE_HANDLED": "true", "JUDGE_ACTION": "skip", "JUDGE_SKIP_REASON": "draft_pr"})
	assert sends == []


@needs_jq
def test_telegram_step_still_sends_for_other_skip_reason() -> None:
	sends = _telegram_sends({"JUDGE_HANDLED": "true", "JUDGE_ACTION": "skip", "JUDGE_SKIP_REASON": "stale_head"})
	assert sends, "a skip with another reason must still alert"
