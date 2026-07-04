#!/usr/bin/env python3
"""Behavioral tests for the consumer retro fan-out script."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "workflow_retro_fanout.sh"
WF_PATH = REPO_ROOT / ".github" / "workflows" / "workflow-log-analysis.yml"


def _write_exec(path: Path, body: str) -> None:
	path.write_text(body, encoding="utf-8")
	path.chmod(0o755)


def _install_mock_gh(bin_dir: Path) -> None:
	gh_script = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["MOCK_GH_STATE_FILE"])
state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
args = sys.argv[1:]


def save() -> None:
	state_path.write_text(json.dumps(state), encoding="utf-8")


def first_value(flag: str) -> str:
	for idx, arg in enumerate(args):
		if arg == flag and idx + 1 < len(args):
			return args[idx + 1]
	return ""


state.setdefault("calls", []).append(args)

if args[:2] == ["api", "graphql"] or (args[0] == "api" and "graphql" in args[1]):
	# workflow_retro.py merged-PR fetch: fail so the script fails open.
	save()
	sys.exit(1)

if args[0] == "api":
	method = first_value("-X") or "GET"
	path_arg = next((a for a in args[1:] if "/" in a and not a.startswith("-")), "")
	if "actions/variables/WORKFLOW_RETRO_ENABLED" in path_arg:
		var_map = state.get("consumer_var_responses", {})
		repo = path_arg.split("/actions/")[0][len("repos/"):]
		save()
		if repo in var_map:
			print(var_map[repo])
			sys.exit(0)
		sys.exit(1)
	if "/comments" in path_arg and method == "GET":
		save()
		print("[]")
		sys.exit(0)
	if "/comments" in path_arg and method in {"POST", "PATCH"}:
		input_file = first_value("--input")
		if input_file:
			state.setdefault("comment_payloads", []).append(Path(input_file).read_text(encoding="utf-8"))
		save()
		sys.exit(0)
	save()
	sys.exit(1)

if args[:2] == ["label", "create"]:
	state.setdefault("label_create_args", []).append(args)
	save()
	sys.exit(0)

if args[:2] == ["issue", "list"]:
	responses = state.setdefault("issue_list_responses", [])
	response = responses.pop(0) if responses else []
	save()
	print(json.dumps(response))
	sys.exit(0)

if args[:2] == ["issue", "create"]:
	state.setdefault("issue_create_args", []).append(args)
	repo = first_value("--repo") or "owner/repo"
	next_issue_number = int(state.get("next_issue_number", 9100))
	state["next_issue_number"] = next_issue_number + 1
	save()
	print(f"https://github.com/{repo}/issues/{next_issue_number}")
	sys.exit(0)

if args[:2] in (["issue", "edit"], ["issue", "reopen"]):
	save()
	sys.exit(0)

save()
print(f"unexpected gh args: {args}", file=sys.stderr)
sys.exit(1)
'''
	_write_exec(bin_dir / "gh", gh_script)


def _install_mock_codex(bin_dir: Path) -> None:
	codex_script = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["MOCK_GH_STATE_FILE"])
state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
state.setdefault("codex_calls", []).append(sys.argv[1:])
state_path.write_text(json.dumps(state), encoding="utf-8")
sys.stdout.write(os.environ.get("MOCK_CODEX_OUTPUT", ""))
sys.exit(0)
'''
	_write_exec(bin_dir / "codex", codex_script)


RETRO_BODY = "\n".join(
	[
		"## Weekly Retro",
		"",
		"### What Worked",
		"- pipeline stayed green",
		"",
		"### Failure Modes",
		"- none observed",
		"",
		"### Next Week Recommendation",
		"- keep monitoring",
		"",
		"### Metrics Snapshot",
		"- runs: 2",
		"",
	]
)


def _recent_iso(days_ago: int) -> str:
	return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_fanout(
	state: dict,
	*,
	report: dict,
	consumer_repos: list[str],
	extra_env: dict | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict]:
	with tempfile.TemporaryDirectory(prefix="retro-fanout-test-") as td:
		tmp_path = Path(td)
		bin_dir = tmp_path / "bin"
		bin_dir.mkdir(parents=True, exist_ok=True)
		state_file = tmp_path / "gh-state.json"
		state_file.write_text(json.dumps(state), encoding="utf-8")
		_install_mock_gh(bin_dir)
		_install_mock_codex(bin_dir)
		# workflow_retro.py needs the same interpreter version the tests run
		# on (CI pins 3.12); shim `python3` so the script uses it too.
		(bin_dir / "python3").symlink_to(sys.executable)

		report_file = tmp_path / "workflow_log_report.json"
		report_file.write_text(json.dumps(report), encoding="utf-8")
		roster_file = tmp_path / "consumer_repos.json"
		roster_file.write_text(json.dumps(consumer_repos), encoding="utf-8")

		env = os.environ.copy()
		env.update(
			{
				"GH_TOKEN": "test-token",
				"GITHUB_REPOSITORY": "owner/source",
				"GITHUB_WORKSPACE": str(REPO_ROOT),
				"OPENROUTER_API_KEY": "test-openrouter-key",
				"MOCK_CODEX_OUTPUT": RETRO_BODY,
				"MOCK_GH_STATE_FILE": str(state_file),
				"PATH": f"{bin_dir}:{env.get('PATH', '')}",
				"PYTHONDONTWRITEBYTECODE": "1",
				"WORKFLOW_LOG_REPORT_FILE": str(report_file),
				"CONSUMER_REPOS_FILE": str(roster_file),
				"CODEX_RETRY_BACKOFF_BASE_SECS": "1",
			}
		)
		env.update(extra_env or {})
		proc = subprocess.run(
			["bash", str(SCRIPT_PATH)],
			cwd=REPO_ROOT,
			env=env,
			capture_output=True,
			text=True,
			encoding="utf-8",
		)
		final_state = json.loads(state_file.read_text(encoding="utf-8"))
		return proc, final_state


def test_fanout_step_is_wired_default_on() -> None:
	wf = WF_PATH.read_text(encoding="utf-8")
	assert "bash scripts/workflow_retro_fanout.sh" in wf
	assert (
		"WORKFLOW_RETRO_CONSUMER_FANOUT_ENABLED: ${{ vars.WORKFLOW_RETRO_CONSUMER_FANOUT_ENABLED || 'true' }}"
	) in wf
	assert "!cancelled() && steps.retro_context.outcome == 'success'" in wf


def test_fanout_disabled_makes_no_calls() -> None:
	proc, state = _run_fanout(
		{},
		report={"runs": [], "summary": {}, "scope": {}, "errors": []},
		consumer_repos=["owner/active-repo"],
		extra_env={"WORKFLOW_RETRO_CONSUMER_FANOUT_ENABLED": "false"},
	)
	assert proc.returncode == 0, proc.stderr
	assert "WORKFLOW_RETRO_CONSUMER_FANOUT_ENABLED=false; skipping." in proc.stdout
	assert state.get("calls", []) == []
	assert state.get("codex_calls", []) == []


def test_fanout_posts_active_repo_skips_idle_and_disabled_and_source() -> None:
	report = {
		"runs": [
			{
				"repository": "owner/active-repo",
				"run_id": idx + 1,
				"workflow_name": f"Workflow {idx + 1}",
				"workflow_family": "review_autofix",
				"conclusion": "success",
				"duration_seconds": 60,
				"created_at": _recent_iso(days_ago=1),
			}
			for idx in range(2)
		],
		"summary": {},
		"scope": {},
		"errors": [],
	}
	state = {
		"consumer_var_responses": {"owner/disabled-repo": "false"},
		"issue_list_responses": [[]],
		"next_issue_number": 9100,
	}
	proc, final_state = _run_fanout(
		state,
		report=report,
		consumer_repos=[
			"owner/active-repo",
			"owner/disabled-repo",
			"owner/idle-repo",
			"owner/source",
		],
	)

	assert proc.returncode == 0, proc.stderr
	assert "WORKFLOW_RETRO_FANOUT_V1: repo=owner/disabled-repo status=skipped_disabled" in proc.stdout
	assert "repo=owner/idle-repo" in proc.stdout
	assert "status=skipped_no_activity" in proc.stdout
	assert "WORKFLOW_RETRO_FANOUT_V1: repo=owner/active-repo" in proc.stdout
	assert "status=posted tracker=#9100" in proc.stdout
	# The source repo's own retro is posted by the dedicated job steps.
	assert "repo=owner/source" not in proc.stdout
	assert len(final_state.get("codex_calls", [])) == 1
	create_args = final_state.get("issue_create_args", [])
	assert len(create_args) == 1
	assert "ai:retro" in create_args[0]
	assert "owner/active-repo" in create_args[0]
	payloads = "\n".join(final_state.get("comment_payloads", []))
	assert "## Weekly Retro" in payloads
	assert "<!-- ai:workflow-retro:" in payloads


def main() -> int:
	for name in sorted(globals()):
		if name.startswith("test_") and callable(globals()[name]):
			globals()[name]()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
