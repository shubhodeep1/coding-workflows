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
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from repo_root import repo_root  # noqa: E402

REPO_ROOT = repo_root()
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


def _fixture(tmp_path: Path) -> dict[str, str]:
	# The fixture must live somewhere the unprivileged identity can traverse
	# up to; pytest's tmp_path is under the invoking user's private tmp
	# root, so open every ancestor below /tmp that this test created.
	support_root = tmp_path / "support"
	scripts_dir = support_root / "scripts"
	runtime_dir = tmp_path / "runtime"
	workspace = tmp_path / "workspace"
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
	setup_end = editor_text.index("\ncleanup_editor_isolation() {", setup_start)
	setup_block = editor_text[setup_start:setup_end]
	assert "editor_isolation_preflight_probe" in setup_block
	assert setup_block.index("editor_isolation_preflight_probe") < setup_block.index('EDITOR_ISOLATION_ACTIVE="true"')
	assert 'CLAUDE_BRANCH_REVIEW_MODE:-false}" != "true"' in reviewers_text
	assert "failing before reviewer spend" in reviewers_text


def test_helper_has_no_top_level_side_effects_and_parses() -> None:
	subprocess.run(["bash", "-n", str(HELPER)], check=True)
	result = subprocess.run(
		["bash", "-c", f"set -euo pipefail; source {HELPER}; declare -F editor_isolation_preflight_probe editor_isolation_prepare_shared_paths editor_isolation_preflight_enabled"],
		check=True,
		capture_output=True,
		text=True,
		env={"PATH": "/usr/bin:/bin"},
	)
	assert result.stdout.strip().splitlines() == [
		"editor_isolation_preflight_probe",
		"editor_isolation_prepare_shared_paths",
		"editor_isolation_preflight_enabled",
	]


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
def test_probe_flags_missing_opencode_on_editor_path(tmp_path: Path) -> None:
	env = _fixture(tmp_path)
	env["EDITOR_CODEX_PATH"] = "/usr/bin:/bin"
	if os.path.exists("/usr/bin/opencode") or os.path.exists("/bin/opencode"):
		pytest.skip("opencode is installed system-wide")
	result = _run_probe(env, prepare=True)
	assert result.returncode == 1
	assert "reason=opencode_not_on_editor_path" in result.stderr
