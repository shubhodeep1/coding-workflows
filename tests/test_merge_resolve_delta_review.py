"""Resolution-only review of a conflict-resolver head (merge-lease plan Phase 3, D4).

Covers `scripts/review_head_gate.sh merge-resolve`, the gate block in
`scripts/review_autofix_step_evaluate_gate.sh`, the remerge branch of
`scripts/review_autofix_step_generate_diff_context.sh`,
`_pr_merge_resolve_marker_post` / `_pr_merge_resolve_final_ci_quick` in
`scripts/pr_checks_lib.sh`, the security-pass carry and the workflow wiring.
Every failed check must fall back to the full review (fail closed).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
HELPER = SCRIPTS / "review_head_gate.sh"
GATE_STEP = SCRIPTS / "review_autofix_step_evaluate_gate.sh"
DIFF_STEP = SCRIPTS / "review_autofix_step_generate_diff_context.sh"
PR_CHECKS_LIB = SCRIPTS / "pr_checks_lib.sh"
SECURITY_PASS = SCRIPTS / "review_single_issue_security_pass.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "review_autofix.yml"

FROM = "1" * 40
BASE = "2" * 40
HEAD = "3" * 40
LOGIN = "pipeline-bot"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as out:
    out.write(json.dumps(args) + "\n")
fail = os.environ.get("FAKE_FAIL", "")
path = next((a for a in args[1:] if a.startswith("repos/") or a == "user"), "")
jq = args[args.index("--jq") + 1] if "--jq" in args else None
def emit(obj):
    text = json.dumps(obj)
    if jq is None:
        print(text)
        sys.exit(0)
    res = subprocess.run(["jq", "-r", jq], input=text, capture_output=True, text=True)
    sys.stdout.write(res.stdout)
    sys.exit(res.returncode)
if args[:1] != ["api"]:
    sys.exit(1)
if path == "user":
    if fail == "user": sys.exit(1)
    emit({"login": os.environ.get("FAKE_LOGIN", "pipeline-bot")})
elif "/commits/" in path and "/statuses" in path:
    if fail == "statuses": sys.exit(1)
    emit(json.loads(os.environ.get("FAKE_STATUSES", "[]")))
elif "/commits/" in path:
    if fail == "commit": sys.exit(1)
    emit(json.loads(os.environ.get("FAKE_COMMIT", "{}")))
elif "/compare/" in path:
    if fail == "compare": sys.exit(1)
    emit({"status": os.environ.get("FAKE_COMPARE", "ahead")})
elif "/issues/" in path and "POST" in args:
    if fail == "post": sys.exit(1)
    emit({"id": 1})
else:
    sys.exit(1)
'''


def _marker(from_sha: str = FROM, resolve: str = HEAD, base: str = BASE) -> str:
	return f"<!-- ai:merge-resolve from={from_sha} resolve={resolve} base={base} -->"


def _commit(parents: list[str], committer: tuple[str, str] = ("claude-bot", "claude-bot@users.noreply.invalid"),
		author: tuple[str, str] = ("claude-bot", "claude-bot@users.noreply.invalid")) -> dict:
	return {
		"parents": [{"sha": sha} for sha in parents],
		"commit": {"committer": {"name": committer[0], "email": committer[1]}, "author": {"name": author[0], "email": author[1]}},
	}


def _statuses(state: str = "success", creator: str = LOGIN) -> list[dict]:
	return [
		{"context": "ai-review/head-gate", "state": state, "creator": {"login": creator}},
		{"context": "ai-review/head-gate", "state": "pending", "creator": {"login": creator}},
	]


def _fake_env(tmp_path: Path, **overrides: str) -> tuple[dict[str, str], Path]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	gh_path = bin_dir / "gh"
	gh_path.write_text(FAKE_GH, encoding="utf-8")
	gh_path.chmod(0o755)
	log_path = tmp_path / "gh.log"
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k not in ("BASH_ENV", "ENV")}
	env.update({
		"PATH": f"{bin_dir}:{os.environ['PATH']}",
		"FAKE_GH_LOG": str(log_path),
		"GH_RETRY_MAX_ATTEMPTS": "1",
		"GIT_CONFIG_GLOBAL": "/dev/null",
		"GIT_CONFIG_NOSYSTEM": "1",
		"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
		"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
		"FAKE_COMMIT": json.dumps(_commit([FROM, BASE])),
		"FAKE_STATUSES": json.dumps(_statuses()),
		"FAKE_COMPARE": "ahead",
	})
	env.update(overrides)
	return env, log_path


def _calls(log_path: Path) -> list[list[str]]:
	if not log_path.exists():
		return []
	return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


def _run_helper(tmp_path: Path, comments: list[dict], head: str = HEAD, base_ref: str = "main",
		**overrides: str) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
	env, log_path = _fake_env(tmp_path, **overrides)
	comments_file = tmp_path / "comments.json"
	comments_file.write_text(json.dumps(comments), encoding="utf-8")
	proc = subprocess.run(["bash", str(HELPER), "merge-resolve", "o/r", "42", head, base_ref, LOGIN, str(comments_file)],
		env=env, capture_output=True, text=True, check=False)
	return proc, _calls(log_path)


GOOD_COMMENTS = [{"author_login": "Pipeline-Bot", "body": f"Resolved.\n\n{_marker()}\n"}]


# --- review_head_gate.sh merge-resolve -------------------------------------


def test_merge_resolve_accepts_the_verified_resolution(tmp_path: Path) -> None:
	proc, calls = _run_helper(tmp_path, GOOD_COMMENTS)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert f"merge_resolve_from={FROM}" in proc.stdout.splitlines()
	assert f"REVIEW_HEAD_GATE mode=merge_resolve pr=42 head={HEAD} outcome=delta reason=merge_resolve" in proc.stdout
	paths = [next(a for a in call if a.startswith("repos/")) for call in calls]
	assert paths == [f"repos/o/r/commits/{HEAD}", f"repos/o/r/commits/{FROM}/statuses?per_page=100", f"repos/o/r/compare/{BASE}...main"]


def test_merge_resolve_accepts_pipeline_author_with_github_committer(tmp_path: Path) -> None:
	commit = _commit([FROM, BASE], committer=("GitHub", "noreply@github.com"))
	proc, _ = _run_helper(tmp_path, GOOD_COMMENTS, FAKE_COMMIT=json.dumps(commit))
	assert proc.returncode == 0, proc.stdout
	assert "outcome=delta" in proc.stdout


@pytest.mark.parametrize("comments,reason", [
	([{"author_login": "someone-else", "body": _marker()}], "no_resolve_marker"),
	([{"author_login": LOGIN, "body": _marker(resolve="4" * 40)}], "no_resolve_marker"),
	([{"author_login": LOGIN, "body": _marker() + "\nmore text after the marker"}], "no_resolve_marker"),
	([{"author_login": LOGIN, "body": _marker().replace("ai:merge-resolve", "ai:merge-resolved")}], "no_resolve_marker"),
	([], "no_resolve_marker"),
])
def test_merge_resolve_needs_a_trusted_final_line_marker(tmp_path: Path, comments: list[dict], reason: str) -> None:
	proc, calls = _run_helper(tmp_path, comments)
	assert proc.returncode == 1
	assert f"outcome=review reason={reason}" in proc.stdout
	assert "merge_resolve_from=" not in proc.stdout
	# No API read unless a trusted marker names the head.
	assert calls == []


@pytest.mark.parametrize("overrides,comments,reason", [
	({"FAKE_COMMIT": json.dumps(_commit([BASE, FROM]))}, None, "head_is_not_the_resolution"),
	({"FAKE_COMMIT": json.dumps(_commit([FROM, BASE, "6" * 40]))}, None, "head_is_not_the_resolution"),
	({"FAKE_COMMIT": json.dumps(_commit([FROM]))}, None, "head_is_not_the_resolution"),
	({}, [{"author_login": LOGIN, "body": _marker(base="5" * 40)}], "head_is_not_the_resolution"),
	({"FAKE_COMMIT": json.dumps(_commit([FROM, BASE], ("someone", "someone@example.com"), ("someone", "someone@example.com")))}, None, "identity_not_pipeline"),
	({"FAKE_COMMIT": json.dumps(_commit([FROM, BASE], ("claude-bot", "x@example.com"), ("codex-bot", "claude-bot@users.noreply.invalid")))}, None, "identity_not_pipeline"),
	({"FAKE_STATUSES": json.dumps(_statuses("failure"))}, None, "from_not_approved"),
	({"FAKE_STATUSES": json.dumps(_statuses(creator="intruder"))}, None, "from_not_approved"),
	({"FAKE_STATUSES": "[]"}, None, "from_not_approved"),
	({"FAKE_COMPARE": "behind"}, None, "base_parent_not_on_base"),
	({"FAKE_COMPARE": "diverged"}, None, "base_parent_not_on_base"),
	({"FAKE_FAIL": "commit"}, None, "commit_read_failed"),
	({"FAKE_FAIL": "statuses"}, None, "from_gate_read_failed"),
	({"FAKE_FAIL": "compare"}, None, "base_compare_failed"),
])
def test_merge_resolve_each_single_violation_gives_review(tmp_path: Path, overrides: dict, comments: list[dict] | None, reason: str) -> None:
	proc, _ = _run_helper(tmp_path, comments if comments is not None else GOOD_COMMENTS, **overrides)
	assert proc.returncode == 1, proc.stdout
	assert f"REVIEW_HEAD_GATE mode=merge_resolve pr=42 head={HEAD} outcome=review reason={reason}" in proc.stdout
	assert "merge_resolve_from=" not in proc.stdout


@pytest.mark.parametrize("head,base_ref", [("not-a-sha", "main"), (HEAD, "../main"), (HEAD, "")])
def test_merge_resolve_rejects_invalid_input(tmp_path: Path, head: str, base_ref: str) -> None:
	proc, calls = _run_helper(tmp_path, GOOD_COMMENTS, head=head, base_ref=base_ref)
	assert proc.returncode == 1
	assert "outcome=review reason=invalid_input" in proc.stdout
	assert calls == []


# --- gate block (scripts/review_autofix_step_evaluate_gate.sh) --------------


def _gate_block() -> str:
	text = GATE_STEP.read_text(encoding="utf-8")
	start = text.index("# ----- Resolution-only review (docs/plans/merge-lease-short-critical-section-plan.md Phase 3, D4) -----")
	end = text.index('echo "should_run=${SHOULD_RUN}" >> "${GITHUB_OUTPUT}"', start)
	output = 'if [ -n "${REVIEW_REMERGE_HEAD}" ]; then\n  echo "review_remerge_head=${REVIEW_REMERGE_HEAD}" >> "${GITHUB_OUTPUT}"\nfi\n'
	assert output in text
	return text[start:end] + output


def _run_gate(tmp_path: Path, head: str = HEAD, comments: list[dict] | None = None, **overrides: str) -> tuple[subprocess.CompletedProcess[str], str, list[list[str]], bool]:
	env, log_path = _fake_env(tmp_path, **overrides)
	work = tmp_path / "gate"
	(work / ".codex-head-gate-src").mkdir(parents=True)
	(work / ".codex-head-gate-src" / "scripts").symlink_to(SCRIPTS)
	comments_file = tmp_path / "gate_comments.json"
	comments_file.write_text(json.dumps(comments if comments is not None else GOOD_COMMENTS), encoding="utf-8")
	output = tmp_path / "github_output"
	output.write_text("", encoding="utf-8")
	fetched = tmp_path / "fetched"
	env.update({
		"GITHUB_OUTPUT": str(output), "REPOSITORY": "o/r", "PR_NUMBER": "42", "pr_head_sha_gate": head,
		"pr_base_ref_gate": "main", "HEAD_GATE_HELPER_VERIFIED": "true",
	})
	env.setdefault("SHOULD_RUN", "true")
	env.setdefault("FINAL_CI_REPAIR", "false")
	env.setdefault("REVIEW_DIFF_BASE_SHA", "")
	preamble = (
		"set -euo pipefail\n"
		'gate_marker_fetch_state="pending"\ngate_marker_author_login=""\n'
		f'gate_marker_comments_file="{comments_file}"\n'
		"gate_fetch_marker_comments()\n{\n"
		f'  : > "{fetched}"\n'
		'  gate_marker_fetch_state="ok"\n'
		f'  gate_marker_author_login="{LOGIN}"\n'
		"}\n"
	)
	proc = subprocess.run(["bash", "-c", preamble + _gate_block()], cwd=work, env=env, capture_output=True, text=True, check=False)
	return proc, output.read_text(encoding="utf-8"), _calls(log_path), fetched.exists()


def test_gate_outputs_review_remerge_head_for_a_verified_head(tmp_path: Path) -> None:
	proc, output, calls, _ = _run_gate(tmp_path)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert output == f"review_remerge_head={HEAD}\n"
	assert "REVIEW_HEAD_GATE mode=merge_resolve" in proc.stdout and "outcome=delta" in proc.stdout
	assert len(calls) == 3


@pytest.mark.parametrize("overrides", [
	{"MERGE_RESOLVE_DELTA_REVIEW_ENABLED": "false"},
	{"MERGE_RESOLVE_DELTA_REVIEW_ENABLED": "FALSE"},
	{"MERGE_RESOLVE_DELTA_REVIEW_ENABLED": "maybe"},
	{"SHOULD_RUN": "false"},
	{"FINAL_CI_REPAIR": "true"},
	{"REVIEW_DIFF_BASE_SHA": FROM},
])
def test_gate_switch_off_or_other_mode_keeps_todays_outputs(tmp_path: Path, overrides: dict) -> None:
	proc, output, calls, fetched = _run_gate(tmp_path, **overrides)
	assert proc.returncode == 0, proc.stderr
	assert output == ""
	assert calls == [] and not fetched
	assert "REVIEW_HEAD_GATE" not in proc.stdout


def test_gate_commit_on_top_of_the_resolution_gets_full_review(tmp_path: Path) -> None:
	# No marker names R', so the helper is never called and nothing is output.
	proc, output, calls, fetched = _run_gate(tmp_path, head="7" * 40)
	assert proc.returncode == 0, proc.stderr
	assert output == "" and calls == [] and fetched


def test_gate_unverified_head_gets_full_review(tmp_path: Path) -> None:
	proc, output, calls, _ = _run_gate(tmp_path, FAKE_STATUSES=json.dumps(_statuses("failure")))
	assert proc.returncode == 0, proc.stderr
	assert output == ""
	assert "outcome=review reason=from_not_approved" in proc.stdout


def test_gate_comment_filter_keeps_merge_resolve_bodies() -> None:
	text = GATE_STEP.read_text(encoding="utf-8")
	assert 'or ($b | contains("<!-- ai:merge-resolve ")) or' in text
	assert "review_diff_base_sha=${REVIEW_DIFF_BASE_SHA}" in text


# --- fixture repository with a real conflict --------------------------------


def _git(cwd: Path, *args: str) -> str:
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
	env.update({
		"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
		"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
		"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
	})
	return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=env).stdout.strip()


def _resolution_repo(tmp_path: Path, conflict: bool = True, ours: bool = False) -> tuple[Path, dict[str, str]]:
	"""A clone whose topic branch is an [ai-merge-resolve] merge of the
	approved head (`from`) with main (`base`). `ours` resolves the conflict
	by keeping `from`'s content and dropping the base change."""
	origin = tmp_path / "origin.git"
	work = tmp_path / "work"
	_git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
	_git(tmp_path, "clone", "-q", str(origin), str(work))
	(work / "conf.txt").write_text("line\n", encoding="utf-8")
	(work / "other.txt").write_text("other\n", encoding="utf-8")
	_git(work, "add", "-A")
	_git(work, "commit", "-q", "-m", "base")
	_git(work, "push", "-q", "origin", "HEAD:main")
	_git(work, "checkout", "-q", "-b", "topic")
	(work / "conf.txt").write_text("topic\n", encoding="utf-8")
	_git(work, "commit", "-q", "-am", "feature")
	from_sha = _git(work, "rev-parse", "HEAD")
	_git(work, "push", "-q", "origin", "topic")
	_git(work, "checkout", "-q", "main")
	if conflict:
		(work / "conf.txt").write_text("main\n", encoding="utf-8")
	else:
		(work / "base_only.txt").write_text("base only\n", encoding="utf-8")
		_git(work, "add", "-A")
	_git(work, "commit", "-q", "-am", "base moves")
	base_sha = _git(work, "rev-parse", "HEAD")
	_git(work, "push", "-q", "origin", "main")
	_git(work, "checkout", "-q", "topic")
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
	env.update({
		"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
		"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
		"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
	})
	merged = subprocess.run(["git", "-C", str(work), "merge", "--no-ff", "--no-commit", "main"], capture_output=True, text=True, env=env)
	assert (work / ".git" / "MERGE_HEAD").exists(), merged.stdout + merged.stderr
	if conflict:
		assert merged.returncode != 0 and "CONFLICT" in merged.stdout, "the fixture must conflict"
		if ours:
			(work / "conf.txt").write_text("topic\n", encoding="utf-8")
		else:
			(work / "conf.txt").write_text("resolved\n", encoding="utf-8")
			(work / "other.txt").write_text("other\nextra resolver edit\n", encoding="utf-8")
		_git(work, "add", "-A")
	_git(work, "commit", "-q", "-m", "[ai-merge-resolve] resolve merge conflicts with main")
	head = _git(work, "rev-parse", "HEAD")
	assert _git(work, "rev-parse", "HEAD^1", "HEAD^2").split() == [from_sha, base_sha]
	return work, {"from": from_sha, "base": base_sha, "head": head}


def _diff_context(tmp_path: Path, work: Path, remerge: str | None, tag: str, git_wrapper: bool = False) -> tuple[str, dict[str, str]]:
	support = tmp_path / "support"
	support.mkdir(exist_ok=True)
	(support / "git_ref_health_check.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
	out = tmp_path / f"out-{tag}"
	out.mkdir()
	(out / "pr.diff").write_text("FULL PR DIFF FROM gh pr diff\n", encoding="utf-8")
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k not in ("BASH_ENV", "ENV")}
	env.update({
		"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
		"SUPPORT_SCRIPTS_DIR": str(support), "BASE_BRANCH": "main", "TARGET_BRANCH": "topic", "GITHUB_ENV": str(out / "github_env"),
		"PR_DIFF_FILE": str(out / "pr.diff"), "ORIGINAL_PR_DIFF_FILE": str(out / "original.diff"),
		"PR_CHANGED_FILES_FILE": str(out / "changed.txt"), "LAST_COMMIT_STAT_FILE": str(out / "stat.txt"),
		"LAST_RUN_DIFF_FILE": str(out / "lr.diff"), "LAST_RUN_CHANGED_FILES_FILE": str(out / "lr.txt"),
		"LAST_RUN_DIFF_STAT_FILE": str(out / "lr.stat"), "PR_DIFF_SOURCE": "gh_pr_diff",
	})
	if git_wrapper:
		wrap = tmp_path / "oldgit"
		wrap.mkdir(exist_ok=True)
		real_git = shutil.which("git")
		(wrap / "git").write_text(f'#!/usr/bin/env bash\nif [ "${{1:-}}" = version ]; then echo "git version 2.35.1"; exit 0; fi\nexec "{real_git}" "$@"\n', encoding="utf-8")
		(wrap / "git").chmod(0o755)
		env["PATH"] = f"{wrap}:{env['PATH']}"
	if remerge is not None:
		env["REVIEW_REMERGE_HEAD"] = remerge
	proc = subprocess.run(["bash", "-c", f'source "{DIFF_STEP}"'], cwd=work, env=env, capture_output=True, text=True, check=False)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	names = {"pr": "pr.diff", "changed": "changed.txt", "original": "original.diff", "last": "lr.diff", "last_files": "lr.txt", "last_stat": "lr.stat"}
	files = {key: (out / name).read_text(encoding="utf-8") for key, name in names.items()}
	stdout = proc.stdout.replace(str(out), "<out>")
	return stdout, files


def test_diff_context_uses_the_remerge_diff_for_a_resolution_head(tmp_path: Path) -> None:
	work, shas = _resolution_repo(tmp_path)
	stdout, files = _diff_context(tmp_path, work, shas["head"], "remerge")
	assert f"REVIEW_DIFF_CONTEXT range=remerge base={shas['from']}" in stdout
	# The resolution hunk (against git's own conflicted merge) and the extra edit.
	assert "+resolved" in files["last"]
	assert "+extra resolver edit" in files["last"]
	assert sorted(files["last_files"].split()) == ["conf.txt", "other.txt"]
	assert "conf.txt" in files["last_stat"] and "other.txt" in files["last_stat"]
	# The whole-PR inputs are untouched (D4).
	assert files["pr"] == files["original"] == "FULL PR DIFF FROM gh pr diff\n"
	assert sorted(files["changed"].split()) == ["conf.txt", "other.txt"]


def test_diff_context_falls_back_on_an_empty_remerge_diff(tmp_path: Path) -> None:
	work, shas = _resolution_repo(tmp_path, conflict=False)
	baseline_out, baseline = _diff_context(tmp_path, work, None, "unset")
	stdout, files = _diff_context(tmp_path, work, shas["head"], "clean")
	assert "REVIEW_DIFF_CONTEXT range=merge_base reason=remerge_failed" in stdout
	assert files == baseline
	assert "No previous AI autofix run on PR head" in files["last"]
	assert "REVIEW_DIFF_CONTEXT range=merge_base\n" in baseline_out


@pytest.mark.parametrize("remerge,old_git", [("HEAD^1", False), ("not-a-sha", False), ("HEAD", True)])
def test_diff_context_falls_back_when_the_head_or_git_is_wrong(tmp_path: Path, remerge: str, old_git: bool) -> None:
	work, shas = _resolution_repo(tmp_path)
	_, baseline = _diff_context(tmp_path, work, None, "unset")
	value = {"HEAD^1": shas["from"], "HEAD": shas["head"]}.get(remerge, remerge)
	stdout, files = _diff_context(tmp_path, work, value, "case", git_wrapper=old_git)
	assert "REVIEW_DIFF_CONTEXT range=merge_base reason=remerge_failed" in stdout
	assert files == baseline


def test_diff_context_with_switch_off_equals_today(tmp_path: Path) -> None:
	# Switch off: the gate writes no review_remerge_head, so the step env is empty.
	work, _ = _resolution_repo(tmp_path)
	unset_out, unset_files = _diff_context(tmp_path, work, None, "unset")
	empty_out, empty_files = _diff_context(tmp_path, work, "", "empty")
	assert unset_out == empty_out
	assert unset_files == empty_files
	assert "range=remerge" not in empty_out and "remerge_failed" not in empty_out


# --- final CI decision (_pr_merge_resolve_final_ci_quick) -------------------


STUB_CI_SYNC_QUICK = '''#!/usr/bin/env bash
echo "$*" >> "${STUB_LOG}"
case "${1:-}" in
	delta-supported) [ "${STUB_PROBE:-ok}" = "ok" ] && echo delta_supported || exit 2 ;;
	full-ci-green) [ "${STUB_GREEN:-yes}" = "yes" ] ;;
	*) exit 2 ;;
esac
'''


def _lib_dir(tmp_path: Path) -> Path:
	lib = tmp_path / "lib"
	lib.mkdir()
	for name in ("pr_checks_lib.sh", "gh_helpers.sh"):
		shutil.copy(SCRIPTS / name, lib / name)
	(lib / "ci_sync_quick.sh").write_text(STUB_CI_SYNC_QUICK, encoding="utf-8")
	return lib


def _run_quick(tmp_path: Path, work: Path, head: str, **overrides: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
	lib = _lib_dir(tmp_path)
	env, _ = _fake_env(tmp_path, **overrides)
	stub_log = tmp_path / "stub.log"
	env.update({"STUB_LOG": str(stub_log), "REVIEW_FINAL_CI_WORKFLOW": "ci.yml"})
	for key, value in overrides.items():
		if value == "<unset>":
			env.pop(key, None)
	script = f'source "{lib}/gh_helpers.sh"; source "{lib}/pr_checks_lib.sh"; _pr_merge_resolve_final_ci_quick o/r 42 "{head}"'
	proc = subprocess.run(["bash", "-c", script], cwd=work, env=env, capture_output=True, text=True, check=False)
	stub_calls = stub_log.read_text(encoding="utf-8").splitlines() if stub_log.exists() else []
	return proc, stub_calls


def test_final_ci_quick_only_for_the_exact_verified_head(tmp_path: Path) -> None:
	work, shas = _resolution_repo(tmp_path)
	proc, stub_calls = _run_quick(tmp_path, work, shas["head"], REVIEW_REMERGE_HEAD=shas["head"])
	assert proc.returncode == 0, proc.stderr
	assert proc.stdout == "true\n"
	assert f"MERGE_RESOLVE_FINAL_CI pr=42 head={shas['head']} outcome=quick reason=merge_resolve" in proc.stderr
	assert stub_calls == ["delta-supported", f"full-ci-green o/r 42 {shas['from']} ci.yml"]


def test_final_ci_full_run_for_a_commit_on_top(tmp_path: Path) -> None:
	work, shas = _resolution_repo(tmp_path)
	(work / "later.txt").write_text("later\n", encoding="utf-8")
	_git(work, "add", "-A")
	_git(work, "commit", "-q", "-m", "[ai-autofix] later fix")
	later = _git(work, "rev-parse", "HEAD")
	proc, stub_calls = _run_quick(tmp_path, work, later, REVIEW_REMERGE_HEAD=shas["head"])
	assert proc.stdout == "false\n"
	assert "outcome=full reason=head_not_resolution" in proc.stderr
	assert stub_calls == []


@pytest.mark.parametrize("overrides,reason,expect_calls", [
	({"STUB_PROBE": "missing"}, "delta_unsupported", ["delta-supported"]),
	({"STUB_GREEN": "no"}, "approved_head_not_full_green", None),
	({"MERGE_BASE_SYNC_DELTA_CI_ENABLED": "false"}, "delta_ci_disabled", []),
])
def test_final_ci_full_run_when_a_condition_fails(tmp_path: Path, overrides: dict, reason: str, expect_calls: list[str] | None) -> None:
	work, shas = _resolution_repo(tmp_path)
	proc, stub_calls = _run_quick(tmp_path, work, shas["head"], REVIEW_REMERGE_HEAD=shas["head"], **overrides)
	assert proc.stdout == "false\n"
	assert f"outcome=full reason={reason}" in proc.stderr
	if expect_calls is not None:
		assert stub_calls == expect_calls


def test_final_ci_silent_without_a_verified_head(tmp_path: Path) -> None:
	work, shas = _resolution_repo(tmp_path)
	proc, stub_calls = _run_quick(tmp_path, work, shas["head"], REVIEW_REMERGE_HEAD="<unset>")
	assert proc.stdout == "false\n" and proc.stderr == "" and stub_calls == []


def test_final_ci_call_site_sets_sync_quick_from_the_helper() -> None:
	text = PR_CHECKS_LIB.read_text(encoding="utf-8")
	start = text.index("_pr_wait_for_required_checks()\n{")
	body = text[start:text.index("\n}\n", start)]
	assert "type _pr_merge_resolve_final_ci_quick >/dev/null 2>&1" in body
	call = body.index('PR_FINAL_CI_SYNC_QUICK="${final_ci_resolve_quick}"')
	assert body.index("_pr_ensure_final_ci", call) > call
	assert 'final_ci_resolve_quick="${PR_FINAL_CI_SYNC_QUICK:-false}"' in body


# --- marker post (_pr_merge_resolve_marker_post) ----------------------------


def _run_marker(tmp_path: Path, work: Path, expected: str, **overrides: str) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
	env, log_path = _fake_env(tmp_path, **overrides)
	env.setdefault("CONFLICT_RESOLVED", "true")
	env.update({"GITHUB_REPOSITORY": "o/r"})
	for key, value in overrides.items():
		if value == "<unset>":
			env.pop(key, None)
	script = f'source "{SCRIPTS}/gh_helpers.sh"; source "{SCRIPTS}/pr_checks_lib.sh"; _pr_merge_resolve_marker_post 42 "{expected}"'
	proc = subprocess.run(["bash", "-c", script], cwd=work, env=env, capture_output=True, text=True, check=False)
	return proc, _calls(log_path)


def _posts(calls: list[list[str]]) -> list[list[str]]:
	return [call for call in calls if "POST" in call]


def test_marker_post_binds_the_pushed_resolution(tmp_path: Path) -> None:
	work, shas = _resolution_repo(tmp_path)
	proc, calls = _run_marker(tmp_path, work, shas["from"])
	assert proc.returncode == 0, proc.stderr
	assert f"MERGE_RESOLVE_MARKER pr=42 head={shas['head']} outcome=posted" in proc.stdout
	assert [call[1] for call in calls if call[1] != "-X"][:2] == ["user", f"repos/o/r/commits/{shas['from']}/statuses?per_page=100"]
	posts = _posts(calls)
	assert len(posts) == 1
	assert "repos/o/r/issues/42/comments" in posts[0]
	body = next(arg for arg in posts[0] if arg.startswith("body="))[len("body="):]
	assert body.splitlines()[-1] == _marker(shas["from"], shas["head"], shas["base"])
	# The gate's verifier accepts the posted body (round trip).
	commit = _commit([shas["from"], shas["base"]])
	sub = tmp_path / "verify"
	sub.mkdir()
	helper, _ = _run_helper(sub, [{"author_login": LOGIN, "body": body}], head=shas["head"], FAKE_COMMIT=json.dumps(commit))
	assert f"merge_resolve_from={shas['from']}" in helper.stdout, helper.stdout


@pytest.mark.parametrize("overrides,expected_log", [
	({"MERGE_RESOLVE_DELTA_REVIEW_ENABLED": "false"}, "outcome=skipped reason=disabled"),
	({"MERGE_RESOLVE_DELTA_REVIEW_ENABLED": "bogus"}, "outcome=skipped reason=disabled"),
	({"CONFLICT_RESOLVED": "false"}, None),
	({"CONFLICT_RESOLVED": "<unset>"}, None),
])
def test_marker_post_off_paths_make_no_call(tmp_path: Path, overrides: dict, expected_log: str | None) -> None:
	work, shas = _resolution_repo(tmp_path)
	proc, calls = _run_marker(tmp_path, work, shas["from"], **overrides)
	assert proc.returncode == 0
	assert calls == []
	if expected_log is None:
		assert "MERGE_RESOLVE_MARKER" not in proc.stdout
	else:
		assert expected_log in proc.stdout


def test_marker_post_local_checks(tmp_path: Path) -> None:
	work, shas = _resolution_repo(tmp_path)
	proc, calls = _run_marker(tmp_path, work, "9" * 40)
	assert "outcome=skipped reason=parent_mismatch" in proc.stdout and calls == []
	(work / "later.txt").write_text("later\n", encoding="utf-8")
	_git(work, "add", "-A")
	_git(work, "commit", "-q", "-m", "[ai-autofix] later fix")
	sub = tmp_path / "later"
	sub.mkdir()
	proc, calls = _run_marker(sub, work, shas["from"])
	assert "outcome=skipped reason=not_resolution" in proc.stdout and calls == []


@pytest.mark.parametrize("overrides,reason", [
	({"FAKE_STATUSES": json.dumps(_statuses("failure"))}, "from_not_approved"),
	({"FAKE_STATUSES": json.dumps(_statuses(creator="intruder"))}, "from_not_approved"),
	({"FAKE_FAIL": "user"}, "read_failed"),
	({"FAKE_FAIL": "statuses"}, "read_failed"),
])
def test_marker_post_needs_an_approved_from(tmp_path: Path, overrides: dict, reason: str) -> None:
	work, shas = _resolution_repo(tmp_path)
	proc, calls = _run_marker(tmp_path, work, shas["from"], **overrides)
	assert f"outcome=skipped reason={reason}" in proc.stdout
	assert _posts(calls) == []


def test_marker_post_failure_is_logged(tmp_path: Path) -> None:
	work, shas = _resolution_repo(tmp_path)
	proc, calls = _run_marker(tmp_path, work, shas["from"], FAKE_FAIL="post")
	assert proc.returncode == 0
	assert "outcome=failed reason=post_failed" in proc.stdout
	assert len(_posts(calls)) == 1


# --- security pass and workflow wiring --------------------------------------


def test_security_pass_audits_a_resolution_head_as_a_delta() -> None:
	text = SECURITY_PASS.read_text(encoding="utf-8")
	assert 'review_head_gate_merge_resolve "${REPOSITORY}" "${PR_NUMBER}" "${head_sha}" "${base_ref}" "${SECURITY_PASS_AUTHOR_LOGIN}"' in text
	assert 'single_pass_carry_skip "${head_sha}" merge_resolve_audit' in text
	assert "freshness_unverified|parents_mismatch|changed_files_differ|patch_mismatch|merge_resolve_audit) ;;" in text
	assert 'if [[ "${single_pass_remerge_since:-}" =~ ^[0-9a-f]{40}$ ]]; then' in text
	# The override sits inside the delta-enabled, non-exhausted branch.
	delta = text.index('if [ "${SINGLE_ISSUE_SECURITY_PASS_DELTA_ENABLED:-true}" != "false" ] && [ "${exhausted_retry}" != "true" ]; then')
	assert delta < text.index('if [[ "${single_pass_remerge_since:-}" =~ ^[0-9a-f]{40}$ ]]; then') < text.index("if [ -n \"${delta_since}\" ]; then\n\t\tif single_pass_dispatch_audit")


def _step(text: str, name: str) -> str:
	start = text.index(f"      - name: {name}\n")
	return text[start:text.index("\n      - name: ", start + 10)]


def test_workflow_wiring() -> None:
	text = WORKFLOW.read_text(encoding="utf-8")
	switch = "MERGE_RESOLVE_DELTA_REVIEW_ENABLED: ${{ vars.MERGE_RESOLVE_DELTA_REVIEW_ENABLED || 'true' }}"
	assert "      review_remerge_head: ${{ steps.evaluate.outputs.review_remerge_head }}" in text
	assert switch in _step(text, "Evaluate review gate")
	assert "REVIEW_REMERGE_HEAD: ${{ needs.gate.outputs.review_remerge_head }}" in _step(text, "Generate diff context")
	assert switch in _step(text, "Single-issue security pass")
	# The deterministic-skip copy of the same gate honours the rollback too.
	assert switch in _step(text, "Single-issue security pass (deterministic skip)")
	assert "REVIEW_REMERGE_HEAD: ${{ needs.gate.outputs.review_remerge_head }}" in _step(text, "Enable auto-merge on PR")
	# The push step body lives in its step script (workflow size guard).
	push_step = _step(text, "Push all pending commits")
	assert '"${SUPPORT_SCRIPTS_DIR:-}/review_autofix_step_push_pending_commits.sh"' in push_step
	assert switch in push_step
	push = (SCRIPTS / "review_autofix_step_push_pending_commits.sh").read_text(encoding="utf-8")
	call = '( source "${SUPPORT_SCRIPTS_DIR:-}/gh_helpers.sh" && source "${SUPPORT_SCRIPTS_DIR:-}/pr_checks_lib.sh" && _pr_merge_resolve_marker_post "${PR_NUMBER:-}" "${LOCAL_HEAD_SHA:-}" ) || true'
	assert push.count(call) == 1
	# After the push loop, before the cache eviction.
	assert push.rindex("\ndone\n") < push.index(call) < push.index("gh_pr_cache_evict")
	assert len(text.encode("utf-8")) < 480_000


def test_registry_and_readme_list_the_switch() -> None:
	registry = json.loads((SCRIPTS / "stage_switches.json").read_text(encoding="utf-8"))
	entry = [item for item in registry["switches"] if item["name"] == "MERGE_RESOLVE_DELTA_REVIEW_ENABLED"]
	assert len(entry) == 1 and entry[0]["default"] == "true"
	readme = (ROOT / "README.md").read_text(encoding="utf-8")
	assert "| `MERGE_RESOLVE_DELTA_REVIEW_ENABLED` | No | `true` |" in readme


STUB_HEAD_GATE = '''
review_head_gate_freshness_sync()
{
	REVIEW_HEAD_GATE_FRESHNESS_FROM=""
	REVIEW_HEAD_GATE_FRESHNESS_BASE_PARENT=""
	echo "freshness_sync called" >> "${STUB_CALLS}"
	return 1
}
review_head_gate_merge_resolve()
{
	REVIEW_HEAD_GATE_MERGE_RESOLVE_FROM=""
	echo "merge_resolve called" >> "${STUB_CALLS}"
	[ "${STUB_MR_OK:-yes}" = "yes" ] || return 1
	REVIEW_HEAD_GATE_MERGE_RESOLVE_FROM="${STUB_MR_FROM}"
	return 0
}
'''


def _run_carry(tmp_path: Path, switch: str | None = None, mr_ok: str = "yes", from_status: str = "clean",
		work: Path | None = None, shas: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess[str], list[str]]:
	from_sha = shas["from"] if shas else FROM
	head_sha = shas["head"] if shas else HEAD
	support = tmp_path / "carry_support"
	support.mkdir()
	(support / "gh_helpers.sh").write_text("gh_retry() { \"$@\"; }\n", encoding="utf-8")
	(support / "review_head_gate.sh").write_text(STUB_HEAD_GATE, encoding="utf-8")
	comments = tmp_path / "carry_comments.json"
	comments.write_text(json.dumps([{"user": {"login": LOGIN}, "body": _marker(from_sha, head_sha)}]), encoding="utf-8")
	calls = tmp_path / "carry_calls.txt"
	env = {k: v for k, v in os.environ.items() if k not in ("BASH_ENV", "ENV")}
	env.pop("MERGE_RESOLVE_DELTA_REVIEW_ENABLED", None)
	env.update({
		"SUPPORT_SCRIPTS_DIR": str(support), "STUB_CALLS": str(calls), "STUB_MR_OK": mr_ok, "STUB_MR_FROM": from_sha,
		"REPOSITORY": "o/r", "PR_NUMBER": "42", "SECURITY_PASS_AUTHOR_LOGIN": LOGIN,
	})
	if switch is not None:
		env["MERGE_RESOLVE_DELTA_REVIEW_ENABLED"] = switch
	markers = f"x\\tx\\t{from_status}\\t{from_sha}"
	script = (
		f'source "{SECURITY_PASS}"\n'
		f'single_pass_base_sync_carry "{comments}" "{head_sha}" main topic "$(printf "{markers}")" || true\n'
		'echo "since=${single_pass_remerge_since:-} reason=${single_pass_carry_skip_reason:-}"\n'
	)
	for key in [k for k in env if k.startswith("GIT_")]:
		env.pop(key)
	env.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})
	proc = subprocess.run(["bash", "-c", script], cwd=work or tmp_path, env=env, capture_output=True, text=True, check=False)
	return proc, (calls.read_text(encoding="utf-8").splitlines() if calls.exists() else [])


def test_security_pass_carry_reaches_the_merge_resolve_branch(tmp_path: Path) -> None:
	# Regression: the empty freshness fields once collapsed under read's
	# default IFS, so a verified resolution head never reached this branch.
	work, shas = _resolution_repo(tmp_path)
	proc, calls = _run_carry(tmp_path, work=work, shas=shas)
	assert proc.returncode == 0, proc.stderr
	assert calls == ["freshness_sync called", "merge_resolve called"]
	assert f"outcome=merge_resolve_carry from={shas['from']}" in proc.stdout
	assert proc.stdout.splitlines()[-1] == f"since={shas['from']} reason=merge_resolve_audit"


def test_security_pass_carry_take_ours_resolution_gets_the_full_audit(tmp_path: Path) -> None:
	# The resolution keeps `from`'s conf.txt and drops the base change: the file
	# is in the remerge diff but not in from..head, so the delta audit would
	# never see it. The carry must fail closed to the full audit.
	work, shas = _resolution_repo(tmp_path, ours=True)
	proc, _ = _run_carry(tmp_path, work=work, shas=shas)
	assert proc.returncode == 0, proc.stderr
	assert f"outcome=merge_resolve_scope_uncovered from={shas['from']}" in proc.stdout
	assert "merge_resolve_carry" not in proc.stdout
	assert proc.stdout.splitlines()[-1] == "since= reason=freshness_unverified"


def test_security_pass_carry_unshallows_a_depth_one_checkout(tmp_path: Path) -> None:
	# The deterministic-skip job checks the head out with fetch-depth 1: the
	# head is present but its parents and their merge base are not. The carry
	# must unshallow instead of falling back to the full audit.
	work, shas = _resolution_repo(tmp_path)
	_git(work, "push", "-q", "origin", "HEAD:topic")
	shallow = tmp_path / "shallow"
	_git(tmp_path, "clone", "-q", "--depth", "1", "--branch", "topic", f"file://{tmp_path / 'origin.git'}", str(shallow))
	assert _git(shallow, "rev-parse", "--is-shallow-repository") == "true"
	assert _git(shallow, "rev-parse", "HEAD") == shas["head"]
	proc, _ = _run_carry(tmp_path, work=shallow, shas=shas)
	assert proc.returncode == 0, proc.stderr
	assert f"outcome=merge_resolve_carry from={shas['from']}" in proc.stdout
	assert proc.stdout.splitlines()[-1] == f"since={shas['from']} reason=merge_resolve_audit"


def test_security_pass_carry_without_git_objects_gets_the_full_audit(tmp_path: Path) -> None:
	proc, _ = _run_carry(tmp_path)
	assert proc.returncode == 0, proc.stderr
	assert "outcome=merge_resolve_scope_uncovered" in proc.stdout
	assert proc.stdout.splitlines()[-1] == "since= reason=freshness_unverified"


@pytest.mark.parametrize("switch", ["false", "FALSE", "off", "0", "maybe"])
def test_security_pass_carry_switch_off_skips_the_verifier(tmp_path: Path, switch: str) -> None:
	proc, calls = _run_carry(tmp_path, switch=switch)
	assert proc.returncode == 0, proc.stderr
	assert calls == ["freshness_sync called"]
	assert proc.stdout.splitlines()[-1] == "since= reason=freshness_unverified"


@pytest.mark.parametrize("mr_ok,from_status", [("no", "clean"), ("yes", "findings")])
def test_security_pass_carry_unverified_or_unclean_from_gets_no_delta(tmp_path: Path, mr_ok: str, from_status: str) -> None:
	proc, _ = _run_carry(tmp_path, mr_ok=mr_ok, from_status=from_status)
	assert proc.returncode == 0, proc.stderr
	assert "merge_resolve_carry" not in proc.stdout
	assert proc.stdout.splitlines()[-1] == "since= reason=freshness_unverified"
