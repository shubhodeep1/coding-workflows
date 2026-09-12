"""Editor isolation preflight (scripts/editor_isolation_preflight.sh).

The review editor launches as an unprivileged identity via
``sudo -n -u <user> env -i``. Runs 34304993091 / 34320556598 on PR #4057
burned three editor attempts each on ``opencode_helpers.sh: Permission
denied`` after 1.5-2.5 hours of reviewer fan-out, with no log line naming
the directory that closed the path. The preflight probes every launch path
as that identity, before reviewer spend, and reports the first denied path
component.
"""

from __future__ import annotations

import os
import pwd
import re
import signal
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

try:
	import pytest
except ModuleNotFoundError:
	class _StandalonePytestMark:
		@staticmethod
		def skipif(_condition: bool, *, reason: str):
			return lambda function: function

	class _StandalonePytest:
		mark = _StandalonePytestMark()

		@staticmethod
		def skip(reason: str) -> None:
			raise RuntimeError(reason)

	pytest = _StandalonePytest()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from repo_root import repo_root  # noqa: E402

try:
	REPO_ROOT = repo_root()
except RuntimeError:
	REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "scripts" / "editor_isolation_preflight.sh"
STAGE_HELPER = REPO_ROOT / "scripts" / "stage_workflow_support.sh"
EDITOR_SCRIPT = REPO_ROOT / "scripts" / "review_apply_fixes.sh"
REVIEWERS_SCRIPT = REPO_ROOT / "scripts" / "review_run_reviewers.sh"
ISOLATION_USER = "nobody"


def _sudo_to_nobody_available() -> bool:
	try:
		result = subprocess.run(
			["sudo", "-n", "-u", ISOLATION_USER, "--", "true"],
			check=False,
			capture_output=True,
			text=True,
			timeout=30,
		)
	except (OSError, subprocess.TimeoutExpired):
		return False
	return result.returncode == 0


def _run_probe(env: dict[str, str], *, prepare: bool) -> subprocess.CompletedProcess[str]:
	prepare_call = "editor_isolation_prepare_shared_paths; " if prepare else ""
	command = (
		f"source {HELPER}; {prepare_call}editor_isolation_preflight_probe {ISOLATION_USER}"
	)
	return subprocess.run(
		["bash", "-c", command],
		check=False,
		capture_output=True,
		text=True,
		env={"PATH": "/usr/bin:/bin", **env},
		cwd=REPO_ROOT,
		timeout=120,
	)


def _run_sequence(env: dict[str, str], *commands: str) -> subprocess.CompletedProcess[str]:
	"""Run helper calls in one shell so the in-shell restore record survives."""
	script = f"source {HELPER}; " + "; ".join(f'{c}; echo "RC[{c.split()[0]}]=$?"' for c in commands)
	return subprocess.run(
		["bash", "-c", script],
		check=False,
		capture_output=True,
		text=True,
		env={"PATH": "/usr/bin:/bin", **env},
		cwd=REPO_ROOT,
		timeout=120,
	)


def _fixture(tmp_path: Path, root: Path | None = None) -> dict[str, str]:
	# The fixture must live somewhere the unprivileged identity can traverse
	# up to; pytest's tmp_path is under the invoking user's private tmp
	# root, so open every ancestor below /tmp that this test created.
	# ``root`` places the support bundle, RUNTIME_DIR and workspace beneath
	# an extra directory so a test can close that ancestor the way the
	# GitHub-hosted runner home (/home/runner, 0750) closes RUNNER_TEMP.
	base = root if root is not None else tmp_path
	support_root = base / "support"
	scripts_dir = support_root / "scripts"
	runtime_dir = base / "runtime"
	workspace = base / "workspace"
	bin_dir = tmp_path / "bin"
	for directory in (scripts_dir, runtime_dir / "editor-sandbox", workspace, bin_dir):
		directory.mkdir(parents=True, exist_ok=True)
	(scripts_dir / "opencode_helpers.sh").write_text("#!/bin/bash\n", encoding="utf-8")
	opencode = bin_dir / "opencode"
	opencode.write_text("#!/bin/sh\necho 1.18.23\n", encoding="utf-8")
	opencode.chmod(0o755)
	current = tmp_path
	while current != current.parent and str(current).startswith("/tmp"):
		try:
			current.chmod(current.stat().st_mode | stat.S_IXOTH | stat.S_IROTH)
		except OSError:
			break
		current = current.parent
	return {
		"SUPPORT_ROOT_DIR": str(support_root),
		"SUPPORT_SCRIPTS_DIR": str(scripts_dir),
		"OPENCODE_HELPERS_PATH": str(scripts_dir / "opencode_helpers.sh"),
		"RUNTIME_DIR": str(runtime_dir),
		"WORKSPACE_PATH": str(workspace),
		"EDITOR_CODEX_PATH": f"{bin_dir}:/usr/bin:/bin",
	}


def _write_process_group_metadata(
	path: Path,
	*,
	guard_pid: str,
	child_pid: str,
	process_group_id: str,
	isolated_user: str,
) -> None:
	guard_stat_path = Path(f"/proc/{guard_pid}/stat")
	guard_stat_fields = (
		guard_stat_path.read_text(encoding="ascii", errors="replace").rpartition(")")[2].split()
		if guard_pid.isdigit() and guard_stat_path.exists()
		else []
	)
	guard_start_time_ticks = guard_stat_fields[19] if len(guard_stat_fields) > 19 else "1"
	guard_uid = Path(f"/proc/{guard_pid}").stat().st_uid if guard_pid.isdigit() and Path(f"/proc/{guard_pid}").exists() else os.getuid()
	child_stat_path = Path(f"/proc/{child_pid}/stat")
	child_stat_fields = (
		child_stat_path.read_text(encoding="ascii", errors="replace").rpartition(")")[2].split()
		if child_pid.isdigit() and child_stat_path.exists()
		else []
	)
	child_start_time_ticks = child_stat_fields[19] if len(child_stat_fields) > 19 else "1"
	child_uid = Path(f"/proc/{child_pid}").stat().st_uid if child_pid.isdigit() and Path(f"/proc/{child_pid}").exists() else os.getuid()
	try:
		isolated_uid = pwd.getpwnam(isolated_user).pw_uid
	except KeyError:
		isolated_uid = os.getuid()
	path.write_text(
		f"guard_pid={guard_pid}\n"
		f"guard_uid={guard_uid}\n"
		f"guard_start_time_ticks={guard_start_time_ticks}\n"
		f"child_pid={child_pid}\n"
		f"process_group_id={process_group_id}\n"
		f"isolated_user={isolated_user}\n"
		f"isolated_uid={isolated_uid}\n"
		f"child_uid={child_uid}\n"
		f"child_start_time_ticks={child_start_time_ticks}\n"
		"signal_mode=privileged\n",
		encoding="utf-8",
	)
	path.chmod(0o600)


def _run_process_group_helper(
	tmp_path: Path,
	metadata_path: Path,
	expected_user: str,
	requested_signal: str,
	expected_guard_pid: str,
) -> subprocess.CompletedProcess[str]:
	bin_dir = tmp_path / "signal-bin"
	bin_dir.mkdir(exist_ok=True)
	fake_sudo = bin_dir / "sudo"
	fake_sudo.write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")
	fake_sudo.chmod(0o755)
	command = (
		f"source {HELPER}; editor_isolation_signal_process_group "
		f"{metadata_path} {expected_user} {requested_signal} {expected_guard_pid}"
	)
	return subprocess.run(
		["bash", "-c", command],
		check=False,
		capture_output=True,
		text=True,
		env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
		cwd=REPO_ROOT,
		timeout=30,
	)


def test_helper_is_staged_and_wired_into_both_review_scripts() -> None:
	assert HELPER.is_file()
	stage_text = STAGE_HELPER.read_text(encoding="utf-8")
	required_line = next(
		line for line in stage_text.splitlines() if line.startswith("REQUIRED_BOOTSTRAP_SCRIPTS=")
	)
	assert "editor_isolation_preflight.sh" in required_line.split('"')[1].split()

	editor_text = EDITOR_SCRIPT.read_text(encoding="utf-8")
	reviewers_text = REVIEWERS_SCRIPT.read_text(encoding="utf-8")
	for text in (editor_text, reviewers_text):
		assert "editor_isolation_preflight.sh" in text
		assert "editor_isolation_prepare_shared_paths" in text
		assert "editor_isolation_preflight_probe" in text

	# The editor probes inside setup_editor_isolation, before the identity
	# switch is armed; the reviewers script probes before any model call and
	# skips reviewer-only mode, where no editor runs.
	setup_start = editor_text.index("setup_editor_isolation() {")
	setup_end = editor_text.index("\n_editor_isolation_setup_rollback() {", setup_start)
	setup_block = editor_text[setup_start:setup_end]
	assert "editor_isolation_preflight_probe" in setup_block
	assert setup_block.index("editor_isolation_preflight_probe") < setup_block.index('EDITOR_ISOLATION_ACTIVE="true"')
	assert 'CLAUDE_BRANCH_REVIEW_MODE:-false}" != "true"' in reviewers_text
	assert "failing before reviewer spend" in reviewers_text

	# Ancestor traverse (runs 34335652907 / 34337926193: first_denied=
	# /home/runner): the editor closes .git and the runner command files
	# before opening any ancestor, opens before the probe, and restores in
	# cleanup and in the setup rollback; the reviewers script opens for the
	# probe and restores immediately, whatever the probe's outcome.
	git_close = setup_block.index('chmod 0700 "${resolved_source}/.git" "${EDITOR_ISOLATION_COMMAND_DIR}"')
	open_call = setup_block.index("editor_isolation_open_ancestor_traverse")
	assert git_close < open_call < setup_block.index("editor_isolation_preflight_probe")
	assert setup_block.count("\n    _editor_isolation_setup_rollback\n") == 2
	rollback_start = setup_end
	rollback_end = editor_text.index("\ncleanup_editor_isolation() {", rollback_start)
	assert "editor_isolation_restore_ancestor_traverse" in editor_text[rollback_start:rollback_end]
	cleanup_start = rollback_end
	cleanup_end = editor_text.index("\neditor_isolation_exit_trap() {", cleanup_start)
	cleanup_block = editor_text[cleanup_start:cleanup_end]
	assert cleanup_block.index("editor_isolation_verify_process_group_stopped") < cleanup_block.index("sudo -n chown -R")
	assert "editor_isolation_restore_ancestor_traverse" in cleanup_block
	assert cleanup_block.index('chmod "${EDITOR_ISOLATION_GIT_MODE}"') < cleanup_block.index("editor_isolation_restore_ancestor_traverse")

	reviewers_open = reviewers_text.index("editor_isolation_open_ancestor_traverse")
	reviewers_probe = reviewers_text.index("editor_isolation_preflight_probe", reviewers_open)
	reviewers_restore = reviewers_text.index("editor_isolation_restore_ancestor_traverse", reviewers_probe)
	assert reviewers_open < reviewers_probe < reviewers_restore
	assert reviewers_restore < reviewers_text.index("failing before reviewer spend", reviewers_restore)


def test_helper_has_no_top_level_side_effects_and_parses() -> None:
	subprocess.run(["bash", "-n", str(HELPER)], check=True)
	result = subprocess.run(
		["bash", "-c", f"set -euo pipefail; source {HELPER}; declare -F editor_isolation_preflight_probe editor_isolation_prepare_shared_paths editor_isolation_preflight_enabled editor_isolation_open_ancestor_traverse editor_isolation_restore_ancestor_traverse editor_isolation_signal_process_group editor_isolation_process_group_has_members editor_isolation_verify_process_group_stopped"],
		check=True,
		capture_output=True,
		text=True,
		env={"PATH": "/usr/bin:/bin"},
	)
	assert result.stdout.strip().splitlines() == [
		"editor_isolation_preflight_probe",
		"editor_isolation_prepare_shared_paths",
		"editor_isolation_preflight_enabled",
		"editor_isolation_open_ancestor_traverse",
		"editor_isolation_restore_ancestor_traverse",
		"editor_isolation_signal_process_group",
		"editor_isolation_process_group_has_members",
		"editor_isolation_verify_process_group_stopped",
	]
	# restore with nothing recorded is a no-op that succeeds, also under set -u.
	subprocess.run(
		["bash", "-c", f"set -euo pipefail; source {HELPER}; editor_isolation_restore_ancestor_traverse"],
		check=True,
		env={"PATH": "/usr/bin:/bin"},
	)


def test_kill_switch_disables_preflight() -> None:
	for value, expected_rc in (("false", 1), ("0", 1), ("off", 1), ("true", 0), ("", 0)):
		result = subprocess.run(
			["bash", "-c", f"source {HELPER}; editor_isolation_preflight_enabled"],
			check=False,
			capture_output=True,
			text=True,
			env={"PATH": "/usr/bin:/bin", "EDITOR_ISOLATION_PREFLIGHT_ENABLED": value},
		)
		assert result.returncode == expected_rc, value


def test_process_group_signal_rejects_invalid_metadata_signal_and_user(tmp_path: Path) -> None:
	metadata_path = tmp_path / "process-group.env"
	current_user = pwd.getpwuid(os.getuid()).pw_name
	_write_process_group_metadata(
		metadata_path,
		guard_pid=str(os.getpid()),
		child_pid="-7",
		process_group_id="-7",
		isolated_user=current_user,
	)
	malformed = _run_process_group_helper(
		tmp_path, metadata_path, current_user, "TERM", str(os.getpid())
	)
	assert malformed.returncode == 1
	assert "EDITOR_ISOLATION_PROCESS_GROUP_METADATA_INVALID" in malformed.stderr

	unsupported = _run_process_group_helper(
		tmp_path, metadata_path, current_user, "USR1", str(os.getpid())
	)
	assert unsupported.returncode == 1
	assert "EDITOR_ISOLATION_PROCESS_GROUP_SIGNAL_INVALID" in unsupported.stderr

	_write_process_group_metadata(
		metadata_path,
		guard_pid=str(os.getpid()),
		child_pid="999999",
		process_group_id="999999",
		isolated_user=current_user,
	)
	mismatched = _run_process_group_helper(
		tmp_path, metadata_path, f"{current_user}-mismatch", "TERM", str(os.getpid())
	)
	assert mismatched.returncode == 1
	assert "reason=field_mismatch" in mismatched.stderr


def test_process_group_signal_reports_failed_privileged_kill(tmp_path: Path) -> None:
	child = subprocess.Popen(["sleep", "1000"], start_new_session=True)
	metadata_path = tmp_path / "process-group-live.env"
	current_user = pwd.getpwuid(os.getuid()).pw_name
	try:
		_write_process_group_metadata(
			metadata_path,
			guard_pid=str(os.getpid()),
			child_pid=str(child.pid),
			process_group_id=str(child.pid),
			isolated_user=current_user,
		)
		result = _run_process_group_helper(
			tmp_path, metadata_path, current_user, "TERM", str(os.getpid())
		)
		assert result.returncode == 1
		assert "EDITOR_ISOLATION_PROCESS_GROUP_SIGNAL_FAILED" in result.stderr
	finally:
		try:
			os.killpg(child.pid, signal.SIGKILL)
		except ProcessLookupError:
			pass
		child.wait(timeout=10)


def test_process_group_metadata_rejects_reused_process_identity(tmp_path: Path) -> None:
	child = subprocess.Popen(["sleep", "1000"], start_new_session=True)
	metadata_path = tmp_path / "process-group-reused.env"
	current_user = pwd.getpwuid(os.getuid()).pw_name
	try:
		_write_process_group_metadata(
			metadata_path,
			guard_pid=str(os.getpid()),
			child_pid=str(child.pid),
			process_group_id=str(child.pid),
			isolated_user=current_user,
		)
		metadata_path.write_text(
			metadata_path.read_text(encoding="utf-8").replace("child_start_time_ticks=", "child_start_time_ticks=9", 1),
			encoding="utf-8",
		)
		result = _run_process_group_helper(
			tmp_path, metadata_path, current_user, "TERM", str(os.getpid())
		)
		assert result.returncode == 1
		assert "reason=process_identity_mismatch" in result.stderr

		_write_process_group_metadata(
			metadata_path,
			guard_pid=str(os.getpid()),
			child_pid=str(child.pid),
			process_group_id=str(child.pid),
			isolated_user=current_user,
		)
		metadata_path.write_text(
			metadata_path.read_text(encoding="utf-8").replace("guard_start_time_ticks=", "guard_start_time_ticks=9", 1),
			encoding="utf-8",
		)
		guard_result = _run_process_group_helper(
			tmp_path, metadata_path, current_user, "TERM", str(os.getpid())
		)
		assert guard_result.returncode == 1
		assert "reason=guard_identity_mismatch" in guard_result.stderr
	finally:
		os.killpg(child.pid, signal.SIGKILL)
		child.wait(timeout=10)


def test_process_group_signal_accepts_only_exited_guard_convergence(tmp_path: Path) -> None:
	metadata_path = tmp_path / "process-group-exited-guard.env"
	current_user = pwd.getpwuid(os.getuid()).pw_name
	missing_pid = "99999999"
	_write_process_group_metadata(
		metadata_path,
		guard_pid=missing_pid,
		child_pid=missing_pid,
		process_group_id=missing_pid,
		isolated_user=current_user,
	)
	converged = _run_process_group_helper(
		tmp_path, metadata_path, current_user, "TERM", missing_pid
	)
	assert converged.returncode == 0, converged.stderr
	assert "guard_identity_unavailable" not in converged.stderr

	child = subprocess.Popen(["sleep", "1000"], start_new_session=True)
	try:
		_write_process_group_metadata(
			metadata_path,
			guard_pid=missing_pid,
			child_pid=str(child.pid),
			process_group_id=str(child.pid),
			isolated_user=current_user,
		)
		live_group = _run_process_group_helper(
			tmp_path, metadata_path, current_user, "TERM", missing_pid
		)
		assert live_group.returncode == 1
		assert "reason=guard_identity_unavailable" in live_group.stderr
		assert child.poll() is None
	finally:
		os.killpg(child.pid, signal.SIGKILL)
		child.wait(timeout=10)


def test_process_group_member_probe_rejects_malformed_pgrep_output(tmp_path: Path) -> None:
	child = subprocess.Popen(["sleep", "1000"], start_new_session=True)
	metadata_path = tmp_path / "process-group-malformed-probe.env"
	bin_dir = tmp_path / "malformed-probe-bin"
	bin_dir.mkdir()
	current_user = pwd.getpwuid(os.getuid()).pw_name
	try:
		_write_process_group_metadata(
			metadata_path,
			guard_pid=str(os.getpid()),
			child_pid=str(child.pid),
			process_group_id=str(child.pid),
			isolated_user=current_user,
		)
		fake_pgrep = bin_dir / "pgrep"
		fake_pgrep.write_text("#!/bin/sh\nprintf 'not-a-pid\\n'\n", encoding="utf-8")
		fake_pgrep.chmod(0o755)
		result = subprocess.run(
			[
				"bash",
				"-c",
				f"source {HELPER}; editor_isolation_process_group_has_members {metadata_path} {current_user}",
			],
			check=False,
			capture_output=True,
			text=True,
			env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
			cwd=REPO_ROOT,
			timeout=30,
		)
		assert result.returncode == 2
		assert "reason=invalid_member_pid" in result.stderr
	finally:
		os.killpg(child.pid, signal.SIGKILL)
		child.wait(timeout=10)


@pytest.mark.skipif(not _sudo_to_nobody_available(), reason="passwordless sudo to nobody unavailable")
def test_probe_reports_first_denied_component_then_passes_after_prepare(tmp_path: Path) -> None:
	env = _fixture(tmp_path)
	runtime_dir = Path(env["RUNTIME_DIR"])
	runtime_dir.chmod(0o700)

	denied = _run_probe(env, prepare=False)
	assert denied.returncode == 1
	assert "EDITOR_ISOLATION_PREFLIGHT_FAILED" in denied.stderr
	denied_lines = [line for line in denied.stderr.splitlines() if "EDITOR_ISOLATION_PREFLIGHT_DENIED" in line]
	assert denied_lines, denied.stderr
	runtime_line = next(line for line in denied_lines if f"path={runtime_dir}" in line)
	assert f"first_denied={runtime_dir}" in runtime_line
	assert re.search(r"mode=drwx------ owner=\S+", runtime_line), runtime_line

	passed = _run_probe(env, prepare=True)
	assert passed.returncode == 0, passed.stderr
	assert "EDITOR_ISOLATION_PREFLIGHT_OK" in passed.stdout
	# prepare grants traverse-only on RUNTIME_DIR and read on the bundle.
	assert stat.S_IMODE(runtime_dir.stat().st_mode) == 0o711
	helper_mode = stat.S_IMODE(Path(env["OPENCODE_HELPERS_PATH"]).stat().st_mode)
	assert helper_mode & stat.S_IROTH


@pytest.mark.skipif(not _sudo_to_nobody_available(), reason="passwordless sudo to nobody unavailable")
def test_probe_names_closed_ancestor_not_the_leaf(tmp_path: Path) -> None:
	env = _fixture(tmp_path)
	closed_parent = tmp_path / "closed"
	nested_workspace = closed_parent / "ws"
	nested_workspace.mkdir(parents=True)
	closed_parent.chmod(0o700)
	env["WORKSPACE_PATH"] = str(nested_workspace)

	result = _run_probe(env, prepare=True)
	assert result.returncode == 1
	workspace_line = next(
		line for line in result.stderr.splitlines() if f"path={nested_workspace}" in line
	)
	assert f"first_denied={closed_parent} " in workspace_line


@pytest.mark.skipif(not _sudo_to_nobody_available(), reason="passwordless sudo to nobody unavailable")
def test_open_ancestor_traverse_mirrors_runner_home_then_restores(tmp_path: Path) -> None:
	# /home/runner on a GitHub-hosted runner: 0750, owned by the job user,
	# above RUNNER_TEMP (support bundle + detached workspace). RUNTIME_DIR
	# lives under /tmp in production and passed the probe; here it sits
	# under the same closed ancestor, which only widens the check.
	home = tmp_path / "home"
	env = _fixture(tmp_path, root=home)
	home.chmod(0o750)
	scripts_dir = Path(env["SUPPORT_SCRIPTS_DIR"])
	workspace = Path(env["WORKSPACE_PATH"])

	denied = _run_probe(env, prepare=True)
	assert denied.returncode == 1
	for path in (env["OPENCODE_HELPERS_PATH"], str(scripts_dir), str(workspace)):
		line = next(line for line in denied.stderr.splitlines() if f"path={path} " in line)
		assert f"first_denied={home} mode=drwxr-x--- owner=" in line, line
	# prepare must not open ancestors on its own: that is the explicit
	# open/restore pair's job, held only as long as the caller needs it.
	assert stat.S_IMODE(home.stat().st_mode) == 0o750

	result = _run_sequence(
		env,
		"editor_isolation_prepare_shared_paths",
		f"editor_isolation_open_ancestor_traverse {ISOLATION_USER}",
		f"editor_isolation_open_ancestor_traverse {ISOLATION_USER}",
		f"editor_isolation_preflight_probe {ISOLATION_USER}",
		"editor_isolation_restore_ancestor_traverse",
	)
	assert result.returncode == 0, result.stderr
	assert "RC[editor_isolation_open_ancestor_traverse]=0" in result.stdout
	assert "RC[editor_isolation_preflight_probe]=0" in result.stdout
	assert "RC[editor_isolation_restore_ancestor_traverse]=0" in result.stdout
	assert "EDITOR_ISOLATION_PREFLIGHT_OK" in result.stdout
	granted = [line for line in result.stdout.splitlines() if "EDITOR_ISOLATION_ANCESTOR_TRAVERSE_GRANTED" in line]
	# One grant for the closed ancestor, recorded once even though three
	# probe paths share it and open ran twice; traverse only, no read bit.
	assert granted == [
		f"EDITOR_ISOLATION_ANCESTOR_TRAVERSE_GRANTED user={ISOLATION_USER} path={home} mode_before=750 mode_after=751"
	]
	assert f"EDITOR_ISOLATION_ANCESTOR_TRAVERSE_RESTORED path={home} mode=750" in result.stdout
	assert stat.S_IMODE(home.stat().st_mode) == 0o750

	# Restoration closed the ancestor again: a fresh probe is denied at it.
	closed_again = _run_probe(env, prepare=True)
	assert closed_again.returncode == 1
	assert f"first_denied={home} mode=drwxr-x---" in closed_again.stderr


@pytest.mark.skipif(not _sudo_to_nobody_available(), reason="passwordless sudo to nobody unavailable")
def test_open_ancestor_traverse_records_nested_ancestors_and_restores_in_order(tmp_path: Path) -> None:
	outer = tmp_path / "outer"
	inner = outer / "inner"
	env = _fixture(tmp_path, root=inner)
	outer.chmod(0o700)
	inner.chmod(0o750)

	result = _run_sequence(
		env,
		f"editor_isolation_open_ancestor_traverse {ISOLATION_USER}",
		f"editor_isolation_preflight_probe {ISOLATION_USER}",
		"editor_isolation_restore_ancestor_traverse",
	)
	assert result.returncode == 0, result.stderr
	assert "RC[editor_isolation_preflight_probe]=0" in result.stdout
	granted = [line for line in result.stdout.splitlines() if "EDITOR_ISOLATION_ANCESTOR_TRAVERSE_GRANTED" in line]
	assert [line.split(" path=")[1].split(" ")[0] for line in granted] == [str(outer), str(inner)]
	restored = [line for line in result.stdout.splitlines() if "EDITOR_ISOLATION_ANCESTOR_TRAVERSE_RESTORED" in line]
	assert restored == [
		f"EDITOR_ISOLATION_ANCESTOR_TRAVERSE_RESTORED path={inner} mode=750",
		f"EDITOR_ISOLATION_ANCESTOR_TRAVERSE_RESTORED path={outer} mode=700",
	]
	assert stat.S_IMODE(outer.stat().st_mode) == 0o700
	assert stat.S_IMODE(inner.stat().st_mode) == 0o750


@pytest.mark.skipif(not _sudo_to_nobody_available(), reason="passwordless sudo to nobody unavailable")
def test_open_ancestor_traverse_never_touches_the_leaf(tmp_path: Path) -> None:
	# A closed workspace itself is the editor stage's chown to handle, not
	# an ancestor grant: open leaves it alone and the probe still names it.
	env = _fixture(tmp_path)
	workspace = Path(env["WORKSPACE_PATH"])
	workspace.chmod(0o700)
	result = _run_sequence(
		env,
		f"editor_isolation_open_ancestor_traverse {ISOLATION_USER}",
		f"editor_isolation_preflight_probe {ISOLATION_USER}",
		"editor_isolation_restore_ancestor_traverse",
	)
	assert "RC[editor_isolation_open_ancestor_traverse]=0" in result.stdout
	assert "RC[editor_isolation_preflight_probe]=1" in result.stdout
	assert "EDITOR_ISOLATION_ANCESTOR_TRAVERSE_GRANTED" not in result.stdout
	assert f"path={workspace} first_denied={workspace} mode=drwx------" in result.stderr
	assert stat.S_IMODE(workspace.stat().st_mode) == 0o700


@pytest.mark.skipif(not _sudo_to_nobody_available(), reason="passwordless sudo to nobody unavailable")
def test_probe_flags_missing_opencode_on_editor_path(tmp_path: Path) -> None:
	env = _fixture(tmp_path)
	env["EDITOR_CODEX_PATH"] = "/usr/bin:/bin"
	if os.path.exists("/usr/bin/opencode") or os.path.exists("/bin/opencode"):
		pytest.skip("opencode is installed system-wide")
	result = _run_probe(env, prepare=True)
	assert result.returncode == 1
	assert "reason=opencode_not_on_editor_path" in result.stderr


def main() -> int:
	test_helper_is_staged_and_wired_into_both_review_scripts()
	test_helper_has_no_top_level_side_effects_and_parses()
	test_kill_switch_disables_preflight()
	with tempfile.TemporaryDirectory(prefix="editor-isolation-direct-") as td:
		tmp_path = Path(td)
		test_process_group_signal_rejects_invalid_metadata_signal_and_user(tmp_path)
		test_process_group_signal_reports_failed_privileged_kill(tmp_path)
		test_process_group_metadata_rejects_reused_process_identity(tmp_path)
		test_process_group_signal_accepts_only_exited_guard_convergence(tmp_path)
		test_process_group_member_probe_rejects_malformed_pgrep_output(tmp_path)
	print("OK: editor isolation preflight process-group contract holds")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
