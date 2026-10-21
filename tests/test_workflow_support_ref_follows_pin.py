"""Q64: consumer wrappers pin each reusable workflow to a commit
(`<file>@<sha> # stable`). The "Resolve workflow support ref" steps of the
workflows below stage their support scripts from that same commit instead of
a newer `stable`; validate.yml does the same in its own step (#7123). Every
such step is run here for a pin, a tag, look-alike identities and this
repository.

tele-funtoken-msg-scoring#4566: a pinned run must never fall back to `main`
or `stable` support code. The fallback and main-snapshot checkouts run only
for an unpinned `stable` ref, the checkout HEAD is verified, the
integration-ref resolver follows the pin and fails closed, and shared actions
are referenced by commit."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = (
	"plan.yml",
	"clarify.yml",
	"implement.yml",
	"orchestrate_clarify_respond.yml",
	"orchestrate.yml",
	"orchestrate_poll.yml",
	"check_failure_triage.yml",
	"cancel_on_pr_close.yml",
	"sync_ai_labels.yml",
	"workflow_failure_heal.yml",
)
RESOLVER_WORKFLOWS = ("plan.yml", "clarify.yml", "implement.yml", "orchestrate_clarify_respond.yml", "validate.yml")
SUPPORT_REPO = "shubhodeep1/coding-workflows"
PIN = "de7fef2fce27d8e45ff8451eeba7fb980f13ee66"


def _resolve_steps(workflow: str) -> list[dict]:
	data = yaml.safe_load((ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8"))
	return [
		step
		for job in data["jobs"].values()
		for step in job.get("steps", [])
		if str(step.get("name", "")).startswith("Resolve workflow support ref")
	]


def _run(step: dict, repository: str, workflow_ref: str, tmp_path: Path) -> dict[str, str]:
	env_file = tmp_path / "github_env"
	env_file.write_text("", encoding="utf-8")
	script = (
		step["run"]
		.replace("${{ github.repository }}", repository)
		.replace("${{ github.sha }}", "1" * 40)
	)
	assert "${{" not in script
	env = {
		**os.environ,
		"GITHUB_ENV": str(env_file),
		"SUPPORT_REF_WORKFLOW_JOB_JSON": json.dumps({"workflow_ref": workflow_ref}),
	}
	subprocess.run(["bash", "-c", script], env=env, check=True, capture_output=True, text=True)
	return dict(line.split("=", 1) for line in env_file.read_text(encoding="utf-8").splitlines() if "=" in line)


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_support_ref_follows_the_consumer_pin(workflow: str, tmp_path: Path) -> None:
	steps = _resolve_steps(workflow)
	assert steps, workflow
	reusable = f"shubhodeep1/coding-workflows/.github/workflows/{workflow}"
	other = "validate.yml" if workflow != "validate.yml" else "plan.yml"
	for step in steps:
		assert step["env"]["SUPPORT_REF_WORKFLOW_JOB_JSON"] == "${{ toJSON(job) }}"
		cases = (
			("shubhodeep1/binance-blessings", f"{reusable}@{PIN}", PIN),
			("shubhodeep1/binance-blessings", f"{reusable}@refs/tags/stable", "stable"),
			("shubhodeep1/binance-blessings", "", "stable"),
			("shubhodeep1/binance-blessings", f"evil/coding-workflows/.github/workflows/{workflow}@{PIN}", "stable"),
			("shubhodeep1/binance-blessings", f"shubhodeep1/coding-workflows/.github/workflows/{other}@{PIN}", "stable"),
			("shubhodeep1/binance-blessings", f"{reusable}@{PIN[:-1]}", "stable"),
			("shubhodeep1/coding-workflows", f"{reusable}@{PIN}", "1" * 40),
		)
		for repository, workflow_ref, expected in cases:
			values = _run(step, repository, workflow_ref, tmp_path)
			assert len(values) == 1, (step["name"], values)
			assert list(values.values()) == [expected], (workflow, step["name"], repository, workflow_ref, values)


def _all_steps(workflow: str) -> list[dict]:
	data = yaml.safe_load((ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8"))
	return [step for job in data["jobs"].values() for step in job.get("steps", [])]


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_pinned_runs_never_check_out_support_from_main(workflow: str) -> None:
	main_checkouts = 0
	for step in _all_steps(workflow):
		with_ = step.get("with") or {}
		if not str(step.get("uses", "")).startswith("actions/checkout@"):
			continue
		if with_.get("repository") != SUPPORT_REPO or with_.get("ref") != "main":
			continue
		main_checkouts += 1
		condition = str(step.get("if", ""))
		assert re.search(r"env\.(DUPLICATE_NOTICE_)?SCRIPT_REF == 'stable'", condition), (workflow, step["name"], condition)
	assert main_checkouts, f"{workflow} has no main support checkout left to guard; drop it from WORKFLOWS"


def _git(cwd: Path, *args: str) -> str:
	env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
	return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_pinned_support_checkout_head_is_verified(workflow: str, tmp_path: Path) -> None:
	steps = [
		step
		for step in _all_steps(workflow)
		if step.get("name") in ("Ensure workflow support source checkout", "Verify pinned workflow support checkout")
	]
	assert steps, workflow
	support = tmp_path / ".codex-workflow-src"
	support.mkdir()
	_git(support, "init", "-q")
	(support / "f").write_text("x", encoding="utf-8")
	_git(support, "add", "f")
	_git(support, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "c")
	head = _git(support, "rev-parse", "HEAD")
	for step in steps:
		script = step["run"]
		assert "${{" not in script, (workflow, step["name"])
		for script_ref, ok in ((head, True), ("2" * 40, False), ("stable", True)):
			result = subprocess.run(
				["bash", "-c", script],
				cwd=tmp_path,
				env={**os.environ, "SCRIPT_REF": script_ref, "GIT_CONFIG_GLOBAL": os.devnull},
				capture_output=True,
				text=True,
			)
			assert (result.returncode == 0) == ok, (workflow, step["name"], script_ref, result.stdout, result.stderr)
			if not ok:
				assert "does not match the pinned commit" in result.stdout + result.stderr


def _resolver_step(workflow: str) -> dict:
	steps = [step for step in _all_steps(workflow) if "resolver_ref_pinned=false" in str(step.get("run", ""))]
	assert len(steps) == 1, workflow
	return steps[0]


def _run_resolver(step: dict, workflow_ref: str, routing: str, tmp_path: Path) -> subprocess.CompletedProcess:
	# A git that always fails: staging the resolver can never succeed.
	fake_bin = tmp_path / "bin"
	fake_bin.mkdir(exist_ok=True)
	fake_git = fake_bin / "git"
	fake_git.write_text("#!/bin/sh\necho 'fatal: simulated clone failure' >&2\nexit 128\n", encoding="utf-8")
	fake_git.chmod(0o755)
	output = tmp_path / "github_output"
	output.write_text("", encoding="utf-8")
	script = step["run"].replace("${{ github.repository }}", "shubhodeep1/binance-blessings").replace("${{ github.sha }}", "1" * 40)
	assert "${{" not in script
	env = {
		**os.environ,
		"PATH": f"{fake_bin}:{os.environ['PATH']}",
		"SUPPORT_REF_WORKFLOW_JOB_JSON": json.dumps({"workflow_ref": workflow_ref}),
		"CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED": routing,
		"GH_TOKEN": "test-token",
		"ISSUE_NUMBER": "5",
		"TRACKING_ISSUE": "5",
		"GITHUB_OUTPUT": str(output),
		"RUNNER_TEMP": str(tmp_path),
		"GITHUB_RUN_ID": "1",
		"GITHUB_RUN_ATTEMPT": "1",
	}
	return subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True)


@pytest.mark.parametrize("workflow", RESOLVER_WORKFLOWS)
def test_integration_resolver_follows_the_pin_and_fails_closed(workflow: str, tmp_path: Path) -> None:
	step = _resolver_step(workflow)
	assert step["env"]["SUPPORT_REF_WORKFLOW_JOB_JSON"] == "${{ toJSON(job) }}"
	reusable = f"{SUPPORT_REPO}/.github/workflows/{workflow}"
	(tmp_path / "pinned").mkdir()
	(tmp_path / "unpinned").mkdir()
	pinned = _run_resolver(step, f"{reusable}@{PIN}", "false", tmp_path / "pinned")
	assert pinned.returncode != 0, (pinned.stdout, pinned.stderr)
	assert f"pinned_ref={PIN}" in pinned.stdout + pinned.stderr
	unpinned = _run_resolver(step, f"{reusable}@refs/tags/stable", "false", tmp_path / "unpinned")
	assert unpinned.returncode == 0, (unpinned.stdout, unpinned.stderr)
	assert "pinned_ref=" not in unpinned.stdout + unpinned.stderr
	assert "ref=" in (tmp_path / "unpinned" / "github_output").read_text(encoding="utf-8")


def test_shared_actions_are_referenced_by_commit() -> None:
	# validate.yml's setup-runtime moves to the commit carrying the pinned
	# install-codex reference in a follow-up; nothing else may use a moving ref.
	allowed = {("validate.yml", "setup-runtime@stable")}
	paths = sorted((ROOT / ".github" / "workflows").glob("*.yml")) + sorted((ROOT / ".github" / "actions").glob("*/action.yml"))
	offenders = []
	for path in paths:
		for match in re.finditer(r"uses:\s*shubhodeep1/coding-workflows/\.github/actions/([\w.-]+@[\w./-]+)", path.read_text(encoding="utf-8")):
			ref = match.group(1)
			if re.fullmatch(r"[\w.-]+@[0-9a-f]{40}", ref):
				continue
			if (path.name, ref) in allowed:
				continue
			offenders.append(f"{path.relative_to(ROOT)}: {ref}")
	assert not offenders, offenders


def test_integration_resolver_names_the_pin_on_the_default_routing_path(tmp_path: Path) -> None:
	# CI-triage routing defaults on; the pinned-run log line still carries the pin.
	for workflow in RESOLVER_WORKFLOWS:
		step = _resolver_step(workflow)
		run_dir = tmp_path / workflow
		run_dir.mkdir()
		reusable = f"{SUPPORT_REPO}/.github/workflows/{workflow}"
		result = _run_resolver(step, f"{reusable}@{PIN}", "true", run_dir)
		assert result.returncode != 0, (workflow, result.stdout, result.stderr)
		output = result.stdout + result.stderr
		assert "ci_triage_routing=on" in output and f"pinned_ref={PIN}" in output, (workflow, output)


_SUPPORT_CHECKOUT_REFS = {
	"${{ env.SCRIPT_REF }}": "SCRIPT_REF",
	"${{ env.DUPLICATE_NOTICE_SCRIPT_REF }}": "DUPLICATE_NOTICE_SCRIPT_REF",
}


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_every_support_checkout_is_verified_before_use(workflow: str) -> None:
	# The first step after a support checkout that reads .codex-workflow-src must
	# compare its HEAD with a pinned ref, so no job runs unverified support code.
	data = yaml.safe_load((ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8"))
	checked = 0
	for job_name, job in data["jobs"].items():
		steps = job.get("steps", [])
		for index, step in enumerate(steps):
			with_ = step.get("with") or {}
			if not str(step.get("uses", "")).startswith("actions/checkout@") or with_.get("repository") != SUPPORT_REPO:
				continue
			variable = _SUPPORT_CHECKOUT_REFS.get(str(with_.get("ref", "")))
			if variable is None or with_.get("path") != ".codex-workflow-src":
				continue
			user = next(
				(later for later in steps[index + 1 :] if not str(later.get("uses", "")).startswith("actions/checkout@") and ".codex-workflow-src" in str(later.get("run", ""))),
				None,
			)
			assert user is not None, (workflow, job_name, step.get("name"))
			script = str(user["run"])
			assert '=~ ^[0-9a-f]{40}$ ]]' in script and "git -C .codex-workflow-src rev-parse HEAD" in script and f'"${{{variable}' in script, (workflow, job_name, step.get("name"), user.get("name"))
			checked += 1
	assert checked, workflow


def test_gh_api_retry_bootstrap_fetches_the_pinned_commit() -> None:
	# The pre-support gh_api_retry helper follows the pin as well; a pinned run
	# fetches the commit itself and checks FETCH_HEAD before reading the helper.
	for workflow in ("implement.yml", "validate.yml", "orchestrate_clarify_respond.yml", "security-audit.yml", "review_autofix.yml"):
		text = (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
		start = text.index("      - name: Bootstrap GitHub API retry helper\n")
		step = text[start : text.index("\n      - name: ", start + 10)]
		assert "SUPPORT_REF_WORKFLOW_JOB_JSON: ${{ toJSON(job) }}" in step, workflow
		assert 'fetch -q --no-tags --depth 1 --filter=blob:none origin "${ghr_pin}"' in step, workflow
		assert '[ "$(git -C "${ghr_dir}/src" rev-parse FETCH_HEAD 2>/dev/null)" = "${ghr_pin}" ]' in step, workflow


# Phase 1 of docs/plans/pin-promote-cycle-to-candidate-plan.md: in this
# repository, a run whose job workflow_ref is exactly
# `<this file>@refs/heads/promote-candidate` takes support code from that
# branch's tip, but only when the branches API reports it protected. Every
# other ref (look-alikes, other files, @main, consumer pins) keeps its source.
PROMOTE_CANDIDATE_WORKFLOWS = tuple(workflow for workflow in WORKFLOWS if workflow != "sync_ai_labels.yml")
ALL_PROMOTE_CANDIDATE_WORKFLOWS = PROMOTE_CANDIDATE_WORKFLOWS + (
	"issue_pr_status.yml",
	"memory_maintenance.yml",
	"review_autofix.yml",
	"validate.yml",
)
PC_TIP = "a" * 40
MAIN_TIP = "b" * 40
GITHUB_SHA = "1" * 40
PC_PROTECTED = json.dumps({"protected": True, "commit": {"sha": PC_TIP}})
PC_UNPROTECTED = json.dumps({"protected": False, "commit": {"sha": PC_TIP}})
PC_BAD_SHA = json.dumps({"protected": True, "commit": {"sha": "not-a-sha"}})
MAIN_PROTECTED = json.dumps({"protected": True, "commit": {"sha": MAIN_TIP}})


def _fake_gh(tmp_path: Path) -> Path:
	bin_dir = tmp_path / "fake-gh-bin"
	bin_dir.mkdir(exist_ok=True)
	fake = bin_dir / "gh"
	fake.write_text(
		"#!/bin/sh\n"
		'printf \'%s\\n\' "$*" >> "${FAKE_GH_LOG}"\n'
		'case "$*" in\n'
		'  *repos/shubhodeep1/coding-workflows/branches/promote-candidate*)\n'
		'    [ "${MOCK_PC_RC:-0}" = 0 ] || exit "${MOCK_PC_RC}"; printf \'%s\\n\' "${MOCK_PC_JSON:-}" ;;\n'
		'  *repos/shubhodeep1/coding-workflows/branches/main*) printf \'%s\\n\' "${MOCK_MAIN_JSON:-}" ;;\n'
		"  *) exit 1 ;;\n"
		"esac\n",
		encoding="utf-8",
	)
	fake.chmod(0o755)
	return bin_dir


def _run_script(script: str, tmp_path: Path, env_extra: dict[str, str], pc_json: str = PC_PROTECTED, pc_rc: int = 0):
	env_file = tmp_path / "github_env"
	out_file = tmp_path / "github_output"
	gh_log = tmp_path / "gh_calls.log"
	for path in (env_file, out_file, gh_log):
		path.write_text("", encoding="utf-8")
	env = {k: v for k, v in os.environ.items() if k != "GH_API_RETRY_HELPERS"}
	env.update(
		{
			"PATH": f"{_fake_gh(tmp_path)}:{os.environ['PATH']}",
			"GITHUB_ENV": str(env_file),
			"GITHUB_OUTPUT": str(out_file),
			"FAKE_GH_LOG": str(gh_log),
			"MOCK_PC_JSON": pc_json,
			"MOCK_PC_RC": str(pc_rc),
			"MOCK_MAIN_JSON": MAIN_PROTECTED,
			"GH_TOKEN": "test-token",
		}
	)
	env.update(env_extra)
	result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True)
	env_values = dict(line.split("=", 1) for line in env_file.read_text(encoding="utf-8").splitlines() if "=" in line)
	outputs = dict(line.split("=", 1) for line in out_file.read_text(encoding="utf-8").splitlines() if "=" in line)
	calls = [line for line in gh_log.read_text(encoding="utf-8").splitlines() if line]
	return result, env_values, outputs, calls


def _substitute(script: str, repository: str) -> str:
	script = script.replace("${{ github.repository }}", repository).replace("${{ github.sha }}", GITHUB_SHA)
	assert "${{" not in script
	return script


def _step(workflow: str, name: str) -> dict:
	steps = [step for step in _all_steps(workflow) if step.get("name") == name]
	assert len(steps) == 1, (workflow, name, len(steps))
	return steps[0]


def _ref(workflow: str, ref: str) -> str:
	return f"{SUPPORT_REPO}/.github/workflows/{workflow}@{ref}"


def _look_alikes(workflow: str) -> tuple[str, ...]:
	other = "plan.yml" if workflow != "plan.yml" else "clarify.yml"
	return (
		_ref(workflow, "refs/heads/promote-candidate-x"),
		_ref(workflow, "refs/heads/promote-candidate/x"),
		_ref(other, "refs/heads/promote-candidate"),
		f"evil/coding-workflows/.github/workflows/{workflow}@refs/heads/promote-candidate",
	)


@pytest.mark.parametrize("workflow", PROMOTE_CANDIDATE_WORKFLOWS)
def test_support_ref_follows_protected_promote_candidate(workflow: str, tmp_path: Path) -> None:
	steps = _resolve_steps(workflow)
	assert steps, workflow
	exact = _ref(workflow, "refs/heads/promote-candidate")
	for step in steps:
		assert step["env"]["GH_TOKEN"] == "${{ secrets.GH_PAT || github.token }}", (workflow, step["name"])
		self_script = _substitute(step["run"], SUPPORT_REPO)
		consumer_script = _substitute(step["run"], "shubhodeep1/binance-blessings")

		def run(script: str, workflow_ref: str, **kwargs):
			result, values, _outputs, calls = _run_script(
				script, tmp_path, {"SUPPORT_REF_WORKFLOW_JOB_JSON": json.dumps({"workflow_ref": workflow_ref})}, **kwargs
			)
			assert result.returncode == 0, (workflow, step["name"], result.stdout, result.stderr)
			assert len(values) == 1, (workflow, step["name"], values)
			return list(values.values())[0], result.stdout, calls

		value, stdout, calls = run(self_script, exact)
		assert value == PC_TIP, (workflow, step["name"])
		assert f"SUPPORT_REF source=promote_candidate sha={PC_TIP}" in stdout
		assert len(calls) == 1 and "branches/promote-candidate" in calls[0]
		for kwargs in ({"pc_json": PC_UNPROTECTED}, {"pc_json": PC_BAD_SHA}, {"pc_json": "", "pc_rc": 1}):
			value, stdout, _calls = run(self_script, exact, **kwargs)
			assert value == GITHUB_SHA, (workflow, step["name"], kwargs)
			assert "SUPPORT_REF source=promote_candidate_unprotected" in stdout
		for workflow_ref in _look_alikes(workflow) + (_ref(workflow, "refs/heads/main"), _ref(workflow, PIN)):
			value, stdout, calls = run(self_script, workflow_ref)
			assert value == GITHUB_SHA, (workflow, step["name"], workflow_ref)
			assert calls == [] and "source=promote_candidate" not in stdout, (workflow, workflow_ref)
		value, _stdout, calls = run(consumer_script, _ref(workflow, PIN))
		assert value == PIN and calls == []
		value, _stdout, calls = run(consumer_script, exact)
		assert value == "stable" and calls == []


def _review_gate_job_json(workflow_ref: str) -> str:
	return json.dumps({"workflow_repository": SUPPORT_REPO, "workflow_ref": workflow_ref, "workflow_sha": "c" * 40})


def test_review_gate_takes_protected_promote_candidate_tip(tmp_path: Path) -> None:
	script = _step("review_autofix.yml", "Resolve trusted review support commit")["run"]
	assert "${{" not in script
	exact = _ref("review_autofix.yml", "refs/heads/promote-candidate")

	def run(workflow_ref: str, caller: str = SUPPORT_REPO, **kwargs):
		result, _values, outputs, calls = _run_script(
			script, tmp_path, {"CALLER_REPOSITORY": caller, "WORKFLOW_JOB_JSON": _review_gate_job_json(workflow_ref)}, **kwargs
		)
		assert result.returncode == 0, (workflow_ref, result.stdout, result.stderr)
		return outputs, result.stdout, calls

	outputs, stdout, _calls = run(exact)
	assert outputs["review_support_sha"] == PC_TIP
	assert outputs["review_support_ref"] == "refs/heads/promote-candidate"
	assert f"SUPPORT_REF source=promote_candidate sha={PC_TIP}" in stdout
	outputs, stdout, _calls = run(exact, pc_json=PC_UNPROTECTED)
	assert (outputs["review_support_sha"], outputs["review_support_ref"]) == (MAIN_TIP, "refs/heads/main")
	assert "SUPPORT_REF source=promote_candidate_unprotected" in stdout
	for workflow_ref in (_ref("review_autofix.yml", "refs/heads/promote-candidate-x"), _ref("review_autofix.yml", "refs/heads/main")):
		outputs, stdout, calls = run(workflow_ref)
		assert (outputs["review_support_sha"], outputs["review_support_ref"]) == (MAIN_TIP, "refs/heads/main")
		assert not any("promote-candidate" in call for call in calls)
	outputs, _stdout, calls = run(_ref("review_autofix.yml", PIN), caller="shubhodeep1/binance-blessings")
	assert (outputs["review_support_sha"], outputs["review_support_ref"]) == (PIN, PIN)
	assert calls == []


def test_review_downstream_allowlists_accept_promote_candidate(tmp_path: Path) -> None:
	text = (ROOT / ".github" / "workflows" / "review_autofix.yml").read_text(encoding="utf-8")
	assert "^(refs/heads/main|refs/tags/stable|[0-9a-f]{40})$" not in text
	assert text.count("^(refs/heads/main|refs/heads/promote-candidate|refs/tags/stable|[0-9a-f]{40})$") == 7
	data = yaml.safe_load(text)
	step = next(step for step in data["jobs"]["codex-agent"]["steps"] if step.get("name") == "Resolve workflow support ref")
	for ref, ok in (("refs/heads/promote-candidate", True), ("refs/heads/promote-candidate-x", False)):
		result, values, _outputs, _calls = _run_script(
			step["run"], tmp_path, {"WORKFLOW_REPOSITORY": SUPPORT_REPO, "WORKFLOW_REF": ref, "WORKFLOW_SHA": PC_TIP}
		)
		assert (result.returncode == 0) == ok, (ref, result.stdout, result.stderr)
		if ok:
			assert values == {"SCRIPT_REF": PC_TIP}


def test_activation_support_takes_protected_promote_candidate_tip(tmp_path: Path) -> None:
	script = _step("issue_pr_status.yml", "Resolve trusted activation support commit")["run"]
	assert "${{" not in script
	exact = _ref("issue_pr_status.yml", "refs/heads/promote-candidate")

	def run(workflow_ref: str, caller: str = SUPPORT_REPO, **kwargs):
		job = json.dumps({"workflow_repository": SUPPORT_REPO, "workflow_ref": workflow_ref})
		return _run_script(script, tmp_path, {"CALLER_REPOSITORY": caller, "WORKFLOW_JOB_JSON": job}, **kwargs)

	result, _values, outputs, _calls = run(exact)
	assert result.returncode == 0 and outputs == {"support_sha": PC_TIP}, result.stderr
	assert f"SUPPORT_REF source=promote_candidate sha={PC_TIP}" in result.stdout
	result, _values, outputs, _calls = run(exact, pc_json=PC_UNPROTECTED)
	assert result.returncode == 0 and outputs == {"support_sha": MAIN_TIP}, result.stderr
	assert "SUPPORT_REF source=promote_candidate_unprotected" in result.stdout
	for workflow_ref in (_ref("issue_pr_status.yml", "refs/heads/promote-candidate/x"), _ref("issue_pr_status.yml", "refs/heads/main")):
		result, _values, outputs, calls = run(workflow_ref)
		assert result.returncode == 0 and outputs == {"support_sha": MAIN_TIP}, result.stderr
		assert not any("promote-candidate" in call for call in calls)
	result, _values, outputs, calls = run(_ref("issue_pr_status.yml", PIN), caller="shubhodeep1/binance-blessings")
	assert result.returncode == 0 and outputs == {"support_sha": PIN} and calls == []


def _script_prefix(script: str, stop_marker: str, tail: str) -> str:
	index = script.index(stop_marker)
	return script[:index] + tail


def test_validate_support_sites_take_protected_promote_candidate_tip(tmp_path: Path) -> None:
	# The gh-retry bootstrap stays byte-identical across workflows
	# (tests/test_workflow_gh_retry_fallback_contract.py), so only the resolver
	# and the support-files fetch follow promote-candidate.
	exact = _ref("validate.yml", "refs/heads/promote-candidate")
	resolver = _substitute(_step("validate.yml", "Resolve integration ref")["run"], SUPPORT_REPO)
	resolver = _script_prefix(resolver, "resolver_stage_root=", 'echo "RESOLVER=${resolver_ref}|${resolver_ref_pinned}" >> "$GITHUB_ENV"\n')
	support = _step("validate.yml", "Fetch workflow support files")["run"]
	support = _script_prefix(support, "helper_stage_root=", "")
	cases = (
		(exact, PC_PROTECTED, f"{PC_TIP}|true", PC_TIP),
		(exact, PC_UNPROTECTED, f"{GITHUB_SHA}|false", "main"),
		(_ref("validate.yml", "refs/heads/promote-candidate-x"), PC_PROTECTED, f"{GITHUB_SHA}|false", "main"),
		(_ref("plan.yml", "refs/heads/promote-candidate"), PC_PROTECTED, f"{GITHUB_SHA}|false", "main"),
		(_ref("validate.yml", "refs/heads/main"), PC_PROTECTED, f"{GITHUB_SHA}|false", "main"),
	)
	for workflow_ref, pc_json, resolver_expected, helper_ref in cases:
		job = json.dumps({"workflow_ref": workflow_ref})
		env = {"GITHUB_REPOSITORY": SUPPORT_REPO, "SUPPORT_REF_WORKFLOW_JOB_JSON": job, "VALIDATE_WORKFLOW_JOB_JSON": job, "TRACKING_ISSUE": "5"}
		result, values, outputs, _calls = _run_script(resolver, tmp_path, env, pc_json=pc_json)
		assert result.returncode == 0 and values == {"RESOLVER": resolver_expected}, (workflow_ref, result.stderr)
		expected_outputs = {"promote_candidate_checked": "true"} if workflow_ref == exact else {}
		if helper_ref == PC_TIP:
			expected_outputs["promote_candidate_sha"] = PC_TIP
		assert outputs == expected_outputs, (workflow_ref, outputs)
		result, _values, _outputs, _calls = _run_script(support, tmp_path, env, pc_json=pc_json)
		assert result.returncode == 0, (workflow_ref, result.stderr)
		assert f"VALIDATE_SUPPORT_REF ref={helper_ref} " in result.stdout, (workflow_ref, result.stdout)
	# Both checkouts use one candidate commit: the support fetch reuses the
	# resolver's accepted SHA instead of reading the (possibly moved) branch.
	moved = "c" * 40
	job = json.dumps({"workflow_ref": exact})
	env = {"GITHUB_REPOSITORY": SUPPORT_REPO, "VALIDATE_WORKFLOW_JOB_JSON": job, "VALIDATE_PROMOTE_CANDIDATE_SHA": moved}
	result, _values, _outputs, calls = _run_script(support, tmp_path, env)
	assert result.returncode == 0 and calls == [], result.stderr
	assert f"VALIDATE_SUPPORT_REF ref={moved} " in result.stdout, result.stdout
	# The reused output only applies on the exact promote-candidate ref.
	job = json.dumps({"workflow_ref": _ref("validate.yml", "refs/heads/main")})
	env["VALIDATE_WORKFLOW_JOB_JSON"] = job
	result, _values, _outputs, calls = _run_script(support, tmp_path, env)
	assert result.returncode == 0 and calls == [], result.stderr
	assert "VALIDATE_SUPPORT_REF ref=main " in result.stdout, result.stdout
	# When the resolver read the branch and rejected it, the support fetch keeps
	# main instead of reading again (a second read could pick a different source).
	env = {
		"GITHUB_REPOSITORY": SUPPORT_REPO,
		"VALIDATE_WORKFLOW_JOB_JSON": json.dumps({"workflow_ref": exact}),
		"VALIDATE_PROMOTE_CANDIDATE_SHA": "",
		"VALIDATE_PROMOTE_CANDIDATE_CHECKED": "true",
	}
	result, _values, _outputs, calls = _run_script(support, tmp_path, env, pc_json=PC_PROTECTED)
	assert result.returncode == 0 and calls == [], result.stderr
	assert "VALIDATE_SUPPORT_REF ref=main " in result.stdout, result.stdout
	assert "SUPPORT_REF source=promote_candidate_unprotected" in result.stdout, result.stdout


def test_memory_maintenance_checks_out_protected_promote_candidate_tip(tmp_path: Path) -> None:
	step = _step("memory_maintenance.yml", "Resolve workflow support ref")
	assert step["id"] == "promote_candidate_ref"
	checkout = _step("memory_maintenance.yml", "Checkout repository")
	assert checkout["with"]["ref"] == "${{ steps.promote_candidate_ref.outputs.ref }}"
	exact = _ref("memory_maintenance.yml", "refs/heads/promote-candidate")
	cases = (
		(SUPPORT_REPO, exact, PC_PROTECTED, {"ref": PC_TIP}),
		(SUPPORT_REPO, exact, PC_UNPROTECTED, {}),
		(SUPPORT_REPO, _ref("memory_maintenance.yml", "refs/heads/promote-candidate-x"), PC_PROTECTED, {}),
		(SUPPORT_REPO, _ref("memory_maintenance.yml", "refs/heads/main"), PC_PROTECTED, {}),
		("shubhodeep1/binance-blessings", exact, PC_PROTECTED, {}),
	)
	for caller, workflow_ref, pc_json, expected in cases:
		env = {"CALLER_REPOSITORY": caller, "SUPPORT_REF_WORKFLOW_JOB_JSON": json.dumps({"workflow_ref": workflow_ref})}
		result, _values, outputs, _calls = _run_script(step["run"], tmp_path, env, pc_json=pc_json)
		assert result.returncode == 0 and outputs == expected, (caller, workflow_ref, result.stderr)


@pytest.mark.parametrize("workflow", ALL_PROMOTE_CANDIDATE_WORKFLOWS)
def test_promote_candidate_markers_present(workflow: str) -> None:
	text = (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
	assert "SUPPORT_REF source=promote_candidate sha=" in text, workflow
	assert "SUPPORT_REF source=promote_candidate_unprotected" in text, workflow
	assert f"{workflow.replace('.yml', '')}\\.yml@refs/heads/promote-candidate$" in text, workflow
