"""Wiring contract for model-provider outage handling (issue #5773)."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"


def _load(name: str, path: Path):
	spec = importlib.util.spec_from_file_location(name, path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def _workflow(name: str) -> dict:
	return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _step(workflow: dict, name: str) -> dict:
	for job in workflow["jobs"].values():
		for step in job.get("steps") or []:
			if step.get("name") == name:
				return step
	raise AssertionError(name)


def _flat(path: Path) -> str:
	return " ".join(path.read_text(encoding="utf-8").split())


REVIEW = _workflow("review_autofix.yml")
SWEEP = _workflow("review_autofix_sweep.yml")


def test_failure_evidence_steps_pass_the_reviewer_logs():
	for name in ("Assemble failure evidence", "Post editor summary comment"):
		run = _step(REVIEW, name)["run"]
		assert '--provider-log-dir "${PREVIOUS_REVIEWS_DIR:-}"' in run, name
		assert '--provider-log-dir "${RUNTIME_DIR:-}"' in run, name
	assemble = _step(REVIEW, "Assemble failure evidence")["run"]
	assert 'echo "AUTOFIX_PROVIDER_UNAVAILABLE=true"' in assemble
	assert 'echo "AUTOFIX_PROVIDER_OUTAGE=${provider_outage}"' in assemble


def test_outage_runs_label_nothing_and_send_no_per_pr_alert():
	for name in ("Mark linked issues review-blocked (workflow failure)",
		"Force orchestrate poll after workflow failure review-blocked label", "Telegram failure"):
		assert "env.AUTOFIX_PROVIDER_UNAVAILABLE != 'true'" in _step(REVIEW, name)["if"], name
	post = _step(REVIEW, "Post review-blocked comment on PR (workflow failure)")["run"]
	paused = post.index('if [ "${AUTOFIX_PROVIDER_UNAVAILABLE:-false}" = "true" ]; then')
	assert paused < post.index('elif [ "${EDITOR_SUMMARY_POSTED:-false}" = "true" ]; then')
	assert "**AI review/autofix paused: model provider unavailable**" in post


def test_review_workflow_stays_under_the_size_guard():
	assert (WORKFLOWS / "review_autofix.yml").stat().st_size < 480_000


def test_sweep_probe_job_wiring():
	probe = SWEEP["jobs"]["provider-outage-probe"]
	assert "github.event.schedule != '17 * * * *'" in probe["if"]
	assert "vars.PROVIDER_OUTAGE_PROBE_ENABLED != 'false'" in probe["if"]
	assert probe["outputs"]["skip_review_dispatch"] == "${{ steps.tick.outputs.skip_review_dispatch }}"
	step = _step(SWEEP, "Probe the model provider while an outage marker is open")
	assert step["env"]["OPENROUTER_API_KEY"] == "${{ secrets.OPENROUTER_API_KEY }}"
	assert step["env"]["GH_TOKEN"] == "${{ secrets.GH_PAT }}"
	assert step["env"]["PROVIDER_OUTAGE_RELEASE_RERUN_ENABLED"] == "${{ vars.PROVIDER_OUTAGE_RELEASE_RERUN_ENABLED || 'false' }}"
	assert "vars.PROVIDER_OUTAGE_PROBE_MODEL || vars.XPOLL_SUMMARISER_MODEL || 'openai/gpt-6-luna'" in step["env"]["PROVIDER_OUTAGE_PROBE_MODEL"]
	assert "python3 scripts/provider_outage.py tick" in step["run"]
	sweep = SWEEP["jobs"]["sweep"]
	assert sweep["needs"] == "provider-outage-probe"
	assert sweep["if"].startswith("!cancelled()") and "github.event.schedule != '17 * * * *'" in sweep["if"]
	dispatch = _step(SWEEP, "Enumerate open PRs and dispatch internal-review.yml")
	assert dispatch["env"]["PROVIDER_OUTAGE_SKIP_REVIEW_DISPATCH"] == "${{ needs.provider-outage-probe.outputs.skip_review_dispatch || 'false' }}"
	run = dispatch["run"]
	assert run.index("AUTOFIX_SWEEP_SKIP_ALL reason=provider_outage") < run.index("gh api --paginate")


def _stub_bin(tmp_path: Path) -> Path:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	(bin_dir / "gh").write_text("#!/bin/sh\necho gh-called >&2\nexit 1\n", encoding="utf-8")
	(bin_dir / "gh").chmod(0o755)
	return bin_dir


def test_sweep_spends_no_review_runs_while_the_probe_says_skip(tmp_path):
	run = _step(SWEEP, "Enumerate open PRs and dispatch internal-review.yml")["run"]
	env = {"PATH": f"{_stub_bin(tmp_path)}{os.pathsep}{os.environ['PATH']}", "REPOSITORY": "o/r", "HEAD_REF_FILTER": "",
		"DRY_RUN": "false", "ALLOW_WORKFLOW_EDITS": "false", "SWEEP_STALE_QUEUED_MINUTES": "120",
		"PROVIDER_OUTAGE_SKIP_REVIEW_DISPATCH": "true"}
	result = subprocess.run(["bash", "-c", run], env=env, capture_output=True, text=True, check=False)
	assert result.returncode == 0 and "AUTOFIX_SWEEP_SKIP_ALL reason=provider_outage repo=o/r" in result.stdout
	assert "gh-called" not in result.stderr


def _run_probe_step(tmp_path: Path, tick_script: str) -> dict[str, str]:
	work = tmp_path / "work"
	(work / "scripts").mkdir(parents=True)
	(work / "scripts" / "provider_outage.py").write_text(tick_script, encoding="utf-8")
	(work / "scripts" / "tg_helpers.sh").write_text(
		'tg_send_msg() { printf "%s|%s\\n" "$2" "$1" >> "$TG_LOG"; }\n', encoding="utf-8")
	output = tmp_path / "output"
	output.write_text("", encoding="utf-8")
	step = _step(SWEEP, "Probe the model provider while an outage marker is open")
	env = {"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(output), "REPOSITORY": "o/r", "GITHUB_RUN_ID": "1",
		"PROVIDER_OUTAGE_PROBE_MODEL": "m", "PROVIDER_OUTAGE_RELEASE_RERUN_ENABLED": "false", "ALLOW_WORKFLOW_EDITS": "false",
		"TG_LOG": str(tmp_path / "tg.log"), "PYTHONDONTWRITEBYTECODE": "1"}
	result = subprocess.run(["bash", "-c", step["run"]], cwd=work, env=env, capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
	values["tg"] = (tmp_path / "tg.log").read_text(encoding="utf-8") if (tmp_path / "tg.log").exists() else ""
	return values


def test_probe_step_outputs_and_alerts(tmp_path):
	recovered = _run_probe_step(tmp_path / "a", "import json\nprint(json.dumps({'outage_open': True, 'skip_review_dispatch': True, 'recovered': True, 'alert_text': 'Model provider recovered: openrouter'}))\n")
	assert recovered["skip_review_dispatch"] == "true" and recovered["tg"] == "WARNING|Model provider recovered: openrouter\n"
	quiet = _run_probe_step(tmp_path / "b", "import json\nprint(json.dumps({'outage_open': False, 'skip_review_dispatch': False}))\n")
	assert quiet["skip_review_dispatch"] == "false" and quiet["tg"] == ""
	broken = _run_probe_step(tmp_path / "c", "import sys\nprint('{\"error\": \"x\"}')\nsys.exit(2)\n")
	assert broken["skip_review_dispatch"] == "false"


def test_intake_handles_outages_before_any_diagnosis():
	intake = (ROOT / "scripts" / "workflow_failure_heal_intake.sh").read_text(encoding="utf-8")
	autofix = intake.index('if [ "${SOURCE_KIND}" = "autofix_failure" ] && [ "${FAILURE_REASON}" = "provider_unavailable" ]; then')
	assert autofix < intake.index("# --- Collect failed jobs + logs")
	release = intake.index('if [ "${SOURCE_KIND}" = "workflow_run" ]; then\n\tRELEASE_RUN_ID=')
	assert release < intake.index("# --- Fingerprint ---")
	assert 'provider-outage-detect --log-file "${RAW_LOG}"' in intake


def test_outage_label_is_registered_and_excluded_from_the_pipelines():
	outage = _load("provider_outage_contract", ROOT / "scripts" / "provider_outage.py")
	contract = json.loads((ROOT / ".github" / "ai" / "label_contract.v1.json").read_text(encoding="utf-8"))
	entry = contract["labels"]["ai:provider-outage"]
	assert (entry["color"], entry["description"]) == (outage.OUTAGE_LABEL_COLOR, outage.OUTAGE_LABEL_DESCRIPTION)
	helpers = (ROOT / "scripts" / "label_helpers.sh").read_text(encoding="utf-8")
	assert f'\t["ai:provider-outage"]="{outage.OUTAGE_LABEL_COLOR}"' in helpers
	assert f'\t["ai:provider-outage"]="{outage.OUTAGE_LABEL_DESCRIPTION}"' in helpers
	exclusion = "!contains(toJson(github.event.issue.labels.*.name), 'ai:provider-outage')"
	for path in (WORKFLOWS / "clarify.yml", WORKFLOWS / "internal-clarify.yml", ROOT / "workflow-templates" / "ai-clarify.yml"):
		assert exclusion in path.read_text(encoding="utf-8"), path
	route = _load("claude_issue_route_contract", ROOT / "scripts" / "claude_issue_route.py")
	routed = route.route_issue({"labels": [{"name": "ai:provider-outage"}], "title": "Model provider outage", "body": ""}, "claude")
	assert (routed["implementer"], routed["reason"]) == ("codex", "codex_only_issue_type")


def test_docs_name_the_outage_path():
	agents = _flat(ROOT / "agents.md")
	for text in ("**Model-provider outages (`provider_unavailable`, issue #5773).**", "PROVIDER_OUTAGE_PROBE_ENABLED=false",
		"PROVIDER_OUTAGE_RELEASE_RERUN_ENABLED=true", "`ai:provider-outage`"):
		assert text in agents, text
	readme = (ROOT / "README.md").read_text(encoding="utf-8")
	for variable in ("PROVIDER_OUTAGE_PROBE_ENABLED", "PROVIDER_OUTAGE_PROBE_MODEL", "PROVIDER_OUTAGE_RELEASE_RERUN_ENABLED"):
		assert re.search(rf"^\| `{variable}` \|", readme, re.MULTILINE), variable
	claude_md = _flat(ROOT / "CLAUDE.md")
	section = claude_md[claude_md.index("### H) Claude-fixer mode, claims, and the catch-all sweep"):claude_md.index("## §27.")]
	assert "**A model-provider outage is never a fix.**" in section and "`provider-unavailable` (`action: wait`)" in section
	assert (ROOT / "changelog.d" / "5773-provider-outage-resume.md").is_file()
	assert "### `scripts/provider_outage.py` + the `provider-outage-probe` job" in (ROOT / "docs" / "scripts-pending-removal.md").read_text(encoding="utf-8")
	twin = ROOT / "workflow-templates" / ".claude" / "commands"
	assert "`provider-unavailable` → the review failed because the model provider is down" in _flat(twin / "fix-claude-pr.md")
	assert "the script reports the not-done `provider-unavailable` and the checker waits" in _flat(twin / "implement-plan-claude.md")
