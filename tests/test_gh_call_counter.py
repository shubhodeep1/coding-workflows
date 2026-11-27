"""Contract tests for the per-job gh call counter (issue #9887, GH_PAT budget part A).

Covers the canonical shim and summary under scripts/gh_call_counter/, their
embedded copies in scripts/gh_helpers.sh, gh_call_counter_install, and the
ordering invariant with the comment-signing shim (_ai_comment_sig_activate).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
SHIM = SCRIPTS / "gh_call_counter" / "gh"
SUMMARY = SCRIPTS / "gh_call_counter" / "gh-call-counter-summary"
HELPERS = SCRIPTS / "gh_helpers.sh"
UNKNOWN = "calls_in_job=unknown paginated_in_job=unknown calls_by_token=unknown"
LOG_LINE = re.compile(r"^v1 cmd=[a-z-]+ paginate=[01] token=(pat|github_token|other)$")


def _embedded(marker: str) -> str:
	helpers = HELPERS.read_text()
	start = helpers.index(f"<<'{marker}'\n") + len(f"<<'{marker}'\n")
	end = helpers.index(f"\n{marker}\n", start) + 1
	return helpers[start:end]


def _base_env(tmp_path: Path, bin_dir: Path, **extra: str) -> dict[str, str]:
	env = {
		"PATH": f"{bin_dir}:/usr/bin:/bin",
		"HOME": str(tmp_path),
		"RUNNER_TEMP": str(tmp_path / "runner"),
		"GH_CALL_COUNTER_LOG": str(tmp_path / "counter.log"),
	}
	(tmp_path / "runner").mkdir(exist_ok=True)
	env.update(extra)
	return env


def _fake_gh(bin_dir: Path, body: str = "") -> Path:
	bin_dir.mkdir(parents=True, exist_ok=True)
	gh = bin_dir / "gh"
	gh.write_text(
		"#!/usr/bin/env bash\n"
		'printf "%s\\n" "real:$*" >> "${REAL_CALLS:-/dev/null}"\n'
		+ (body or 'cat >/dev/null\necho "out:$1"\necho "err:$2" >&2\nexit 7\n')
	)
	gh.chmod(0o755)
	return gh


def _counter_dir(tmp_path: Path) -> Path:
	"""Stage the canonical shim like the installer does (a gh_call_counter.* dir)."""
	directory = tmp_path / "runner" / "gh_call_counter.test"
	directory.mkdir(parents=True, exist_ok=True)
	for source in (SHIM, SUMMARY):
		target = directory / source.name
		shutil.copyfile(source, target)
		target.chmod(0o755)
	return directory


def _run(cmd: list[str] | str, env: dict[str, str], stdin: str = "") -> subprocess.CompletedProcess[str]:
	if isinstance(cmd, str):
		cmd = ["bash", "-c", cmd]
	return subprocess.run(cmd, env=env, input=stdin, capture_output=True, text=True, timeout=60)


def _log_lines(tmp_path: Path) -> list[str]:
	log = tmp_path / "counter.log"
	return log.read_text().splitlines() if log.exists() else []


# ---------------------------------------------------------------------------
# Parity
# ---------------------------------------------------------------------------

def test_embedded_shim_and_summary_match_canonical_files() -> None:
	assert _embedded("GH_CALL_COUNTER_SHIM") == SHIM.read_text(), "re-embed scripts/gh_call_counter/gh"
	assert _embedded("GH_CALL_COUNTER_SUMMARY") == SUMMARY.read_text(), (
		"re-embed scripts/gh_call_counter/gh-call-counter-summary"
	)


def test_canonical_files_are_executable() -> None:
	assert os.access(SHIM, os.X_OK) and os.access(SUMMARY, os.X_OK)


def test_counter_functions_are_defined_before_activate_only_return() -> None:
	helpers = HELPERS.read_text()
	early_return = helpers.index('if [ "${AI_COMMENT_SIG_ACTIVATE_ONLY:-}" = "1" ]; then')
	for name in ("gh_call_counter_install()", "_gh_call_counter_refront()", "gh_call_counter_summary()"):
		assert helpers.index(f"\n{name}\n") < early_return, name


# ---------------------------------------------------------------------------
# Shim behaviour
# ---------------------------------------------------------------------------

def test_shim_counts_and_passes_everything_through(tmp_path: Path) -> None:
	bin_dir = tmp_path / "bin"
	_fake_gh(bin_dir)
	counter = _counter_dir(tmp_path)
	env = _base_env(tmp_path, bin_dir, GH_TOKEN="ghp_SECRETVALUE", REAL_CALLS=str(tmp_path / "real"))
	env["PATH"] = f"{counter}:{env['PATH']}"
	result = _run(["gh", "api", "repos/o/r", "--paginate"], env, stdin="payload")
	assert result.returncode == 7
	assert result.stdout == "out:api\n" and result.stderr == "err:repos/o/r\n"
	second = _run(["gh", "pr", "list"], env)
	assert second.returncode == 7
	assert _log_lines(tmp_path) == [
		"v1 cmd=api paginate=1 token=pat",
		"v1 cmd=pr paginate=0 token=pat",
	]
	assert (tmp_path / "real").read_text().splitlines() == ["real:api repos/o/r --paginate", "real:pr list"]


def test_shim_passes_stdin_through(tmp_path: Path) -> None:
	bin_dir = tmp_path / "bin"
	_fake_gh(bin_dir, body='cat\nexit 0\n')
	counter = _counter_dir(tmp_path)
	env = _base_env(tmp_path, bin_dir)
	env["PATH"] = f"{counter}:{env['PATH']}"
	result = _run(["gh", "issue", "comment", "--body-file", "-"], env, stdin="line1\nline2\n")
	assert result.returncode == 0 and result.stdout == "line1\nline2\n"


@pytest.mark.parametrize(
	("extra", "expected"),
	[
		({"GH_TOKEN": "x"}, "pat"),
		({"GITHUB_TOKEN": "y"}, "github_token"),
		({"GH_TOKEN": "", "GITHUB_TOKEN": "y"}, "github_token"),
		({}, "other"),
		({"GH_TOKEN": "x", "GH_CALL_COUNTER_TOKEN_KIND": "github_token"}, "github_token"),
		({"GITHUB_TOKEN": "y", "GH_CALL_COUNTER_TOKEN_KIND": "pat"}, "pat"),
		({"GH_TOKEN": "x", "GH_CALL_COUNTER_TOKEN_KIND": "bogus\nv1"}, "pat"),
	],
)
def test_shim_classifies_token_by_variable_name(tmp_path: Path, extra: dict[str, str], expected: str) -> None:
	bin_dir = tmp_path / "bin"
	_fake_gh(bin_dir)
	counter = _counter_dir(tmp_path)
	env = _base_env(tmp_path, bin_dir, **extra)
	env["PATH"] = f"{counter}:{env['PATH']}"
	_run(["gh", "run", "list"], env)
	assert _log_lines(tmp_path) == [f"v1 cmd=run paginate=0 token={expected}"]


def test_shim_never_logs_arguments_or_values(tmp_path: Path) -> None:
	bin_dir = tmp_path / "bin"
	_fake_gh(bin_dir)
	counter = _counter_dir(tmp_path)
	secret = "ghp_TOPSECRET123"
	env = _base_env(tmp_path, bin_dir, GH_TOKEN=secret, GITHUB_TOKEN="ghs_OTHERSECRET")
	env["PATH"] = f"{counter}:{env['PATH']}"
	_run(["gh", "api", "-H", f"Authorization: token {secret}", "repos/o/r"], env)
	_run(["gh", "pr", "comment", "5", "--body", f"leak {secret}"], env)
	_run(["gh", "evil\nv1 cmd=api paginate=1 token=pat", "x"], env)
	_run(["gh", "cmd=api"], env)
	text = (tmp_path / "counter.log").read_text()
	assert "SECRET" not in text and "Authorization" not in text and "leak" not in text
	lines = text.splitlines()
	assert lines == [
		"v1 cmd=api paginate=0 token=pat",
		"v1 cmd=pr paginate=0 token=pat",
		"v1 cmd=other paginate=0 token=pat",
		"v1 cmd=other paginate=0 token=pat",
	]
	assert all(LOG_LINE.match(line) for line in lines)


def test_kill_switch_execs_target_without_logging(tmp_path: Path) -> None:
	bin_dir = tmp_path / "bin"
	_fake_gh(bin_dir)
	counter = _counter_dir(tmp_path)
	env = _base_env(tmp_path, bin_dir, GH_CALL_COUNTER_ENABLED="false")
	env["PATH"] = f"{counter}:{env['PATH']}"
	result = _run(["gh", "api", "x"], env)
	assert result.returncode == 7 and result.stdout == "out:api\n"
	assert _log_lines(tmp_path) == []


def test_shim_exits_127_without_another_gh(tmp_path: Path) -> None:
	counter = _counter_dir(tmp_path)
	empty = tmp_path / "empty"
	empty.mkdir()
	env = _base_env(tmp_path, empty)
	env["PATH"] = f"{counter}:{empty}"
	result = subprocess.run(["/bin/bash", str(counter / "gh"), "api"], env=env, capture_output=True, text=True, timeout=30)
	assert result.returncode == 127
	assert "GH_CALL_COUNTER shim" in result.stderr


def test_shim_survives_unwritable_log(tmp_path: Path) -> None:
	bin_dir = tmp_path / "bin"
	_fake_gh(bin_dir)
	counter = _counter_dir(tmp_path)
	env = _base_env(tmp_path, bin_dir, GH_CALL_COUNTER_LOG=str(tmp_path / "missing-dir" / "log"))
	env["PATH"] = f"{counter}:{env['PATH']}"
	result = _run(["gh", "api", "x"], env)
	assert result.returncode == 7 and result.stdout == "out:api\n" and result.stderr == "err:x\n"


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def test_summary_counts_and_skips_malformed_lines(tmp_path: Path) -> None:
	log = tmp_path / "counter.log"
	log.write_text(
		"v1 cmd=api paginate=1 token=pat\n"
		"v1 cmd=pr paginate=0 token=github_token\n"
		"v1 cmd=issue paginate=0 token=other\n"
		"v1 cmd=api paginate=0 token=pat\n"
		"garbage line\n"
		"v1 cmd=api paginate=2 token=pat\n"
		"v1 cmd=api paginate=1 token=secret\n"
	)
	result = _run([str(SUMMARY)], {"PATH": "/usr/bin:/bin", "GH_CALL_COUNTER_LOG": str(log)})
	assert result.returncode == 0
	assert result.stdout == "calls_in_job=4 paginated_in_job=1 calls_by_token=pat:2,github_token:1,other:1\n"


def test_summary_reports_unknown_without_log(tmp_path: Path) -> None:
	result = _run([str(SUMMARY)], {"PATH": "/usr/bin:/bin", "GH_CALL_COUNTER_LOG": str(tmp_path / "none")})
	assert result.returncode == 0 and result.stdout == f"{UNKNOWN}\n"
	empty = tmp_path / "empty.log"
	empty.write_text("")
	result = _run([str(SUMMARY)], {"PATH": "/usr/bin:/bin", "GH_CALL_COUNTER_LOG": str(empty)})
	assert result.stdout == "calls_in_job=0 paginated_in_job=0 calls_by_token=pat:0,github_token:0,other:0\n"


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

def test_install_is_idempotent_and_persists_only_the_dir(tmp_path: Path) -> None:
	bin_dir = tmp_path / "bin"
	_fake_gh(bin_dir)
	github_path = tmp_path / "github_path"
	github_path.write_text("")
	env = _base_env(tmp_path, bin_dir, GITHUB_PATH=str(github_path), GH_TOKEN="x")
	(tmp_path / "counter.log").write_text("v1 cmd=api paginate=0 token=pat\n")
	script = f'''source "{HELPERS}"
gh_call_counter_install
gh_call_counter_install
printf '%s\\n' "${{PATH}}"
gh run list >/dev/null 2>&1 || true
gh_call_counter_summary
'''
	result = _run(script, env)
	assert result.returncode == 0, result.stderr
	assert result.stderr.count("GH_CALL_COUNTER outcome=active dir=") == 2
	persisted = github_path.read_text().splitlines()
	assert len(persisted) == 1 and "/gh_call_counter." in persisted[0]
	out = result.stdout.splitlines()
	assert out[0].split(":")[0] == persisted[0]
	assert out[0].count("/gh_call_counter.") == 1
	# The first install truncated the stale log; one call was counted after it.
	assert out[-1] == "calls_in_job=1 paginated_in_job=0 calls_by_token=pat:1,github_token:0,other:0"
	# A later step: PATH already carries the dir, so nothing new is written.
	later_env = {**env, "PATH": f"{persisted[0]}:{env['PATH']}"}
	later = _run(f'source "{HELPERS}"\ngh_call_counter_install\ngh_call_counter_summary\n', later_env)
	assert later.returncode == 0
	assert github_path.read_text().splitlines() == persisted
	assert later.stdout.strip().startswith("calls_in_job=1 ")


def test_install_disabled_is_a_noop(tmp_path: Path) -> None:
	bin_dir = tmp_path / "bin"
	_fake_gh(bin_dir)
	github_path = tmp_path / "github_path"
	github_path.write_text("")
	env = _base_env(tmp_path, bin_dir, GITHUB_PATH=str(github_path), GH_CALL_COUNTER_ENABLED="false")
	result = _run(f'source "{HELPERS}"\ngh_call_counter_install\ngh_call_counter_summary\n', env)
	assert result.returncode == 0
	assert "GH_CALL_COUNTER outcome=disabled" in result.stderr
	assert github_path.read_text() == ""
	assert result.stdout.strip() == UNKNOWN


def test_install_tolerates_mktemp_failure(tmp_path: Path) -> None:
	bin_dir = tmp_path / "bin"
	_fake_gh(bin_dir)
	env = _base_env(tmp_path, bin_dir)
	result = _run(f'source "{HELPERS}"\nmktemp() {{ return 1; }}\ngh_call_counter_install; echo rc=$?\n', env)
	assert result.returncode == 0 and "rc=0" in result.stdout
	assert "GH_CALL_COUNTER outcome=inactive reason=mktemp_failed" in result.stderr


# ---------------------------------------------------------------------------
# Chaining with the comment-signing shim
# ---------------------------------------------------------------------------

def _stub_signer(env: dict[str, str]) -> str:
	return (
		'stub="${AI_COMMENT_SIG_SHIM_DIR}/comment_signature.py"\n'
		"printf '%s\\n' 'import os, sys' "
		"'open(os.environ[\"SIGNED_LOG\"], \"a\").write(\"signed:\" + \" \".join(sys.argv[1:]) + \"\\n\")' > \"${stub}\"\n"
	)


@pytest.mark.skipif(shutil.which("jq") is None or shutil.which("python3") is None, reason="needs jq and python3")
@pytest.mark.parametrize("order", ["counter_first", "signing_first"])
def test_chain_with_comment_signing_shim_never_loops_or_bypasses(tmp_path: Path, order: str) -> None:
	bin_dir = tmp_path / "bin"
	_fake_gh(bin_dir, body="exit 0\n")
	py_dir = Path(shutil.which("python3")).parent
	jq_dir = Path(shutil.which("jq")).parent
	env = _base_env(
		tmp_path, bin_dir, GH_TOKEN="x", SIGNED_LOG=str(tmp_path / "signed"), REAL_CALLS=str(tmp_path / "real"),
	)
	env["PATH"] = f"{bin_dir}:{py_dir}:{jq_dir}:/usr/bin:/bin"
	if order == "counter_first":
		setup = (
			f'source "{HELPERS}"\ngh_call_counter_install\n'
			f'AI_COMMENT_SIGNING_SECRET=fake-secret _ai_comment_sig_activate\n'
		)
	else:
		setup = f'AI_COMMENT_SIGNING_SECRET=fake-secret source "{HELPERS}"\ngh_call_counter_install\n'
	script = (
		setup
		+ "export AI_COMMENT_SIGNING_SECRET=fake-secret\n"
		+ _stub_signer(env)
		+ 'printf "%s\\n" "${PATH%%:*}"\n'
		+ "gh issue comment 5 --body hi\n"
		+ "gh pr list\n"
		+ 'env -i PATH="${PATH}" GH_CALL_COUNTER_LOG="${GH_CALL_COUNTER_LOG}" AI_COMMENT_SIGNING_SECRET=fake-secret '
		+ 'SIGNED_LOG="${SIGNED_LOG}" REAL_CALLS="${REAL_CALLS}" gh issue comment 6 --body there\n'
	)
	result = _run(script, env)
	assert result.returncode == 0, result.stderr
	assert "/gh_call_counter." in result.stdout.splitlines()[0]
	signed = (tmp_path / "signed").read_text().splitlines()
	assert signed == ["signed:gh-shim -- issue comment 5 --body hi", "signed:gh-shim -- issue comment 6 --body there"]
	assert (tmp_path / "real").read_text().splitlines() == ["real:pr list"]
	assert _log_lines(tmp_path) == [
		"v1 cmd=issue paginate=0 token=pat",
		"v1 cmd=pr paginate=0 token=pat",
		"v1 cmd=issue paginate=0 token=other",
	]


def test_signing_shim_and_real_gh_resolution_skip_counter_dirs() -> None:
	helpers = HELPERS.read_text()
	assert "*/ai_comment_sig_shim.*|*/gh_call_counter.*) continue ;;" in helpers
	assert '"${_shim_self_dir}"|*/ai_comment_sig_shim.*|*/gh_call_counter.*) continue ;;' in helpers
	assert "declare -F _gh_call_counter_refront" in helpers
