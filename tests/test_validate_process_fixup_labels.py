#!/usr/bin/env python3
"""Regression test for validation fix-up label propagation."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATE_PROCESS = REPO_ROOT / "scripts" / "validate_process.sh"


def _validate_process_text() -> str:
	return VALIDATE_PROCESS.read_text(encoding="utf-8")


def _extract_needs_fixes_branch() -> str:
	text = _validate_process_text()
	match = re.search(
		r'  needs_fixes\)\n(?P<branch>.*?)\n    ;;\n\n  harness_error\)',
		text,
		re.DOTALL,
	)
	if not match:
		raise AssertionError("could not extract needs_fixes branch from validate_process.sh")
	return match.group("branch")


def _install_mock_gh(bin_dir: Path, state_file: Path) -> None:
	gh_script = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["MOCK_GH_STATE_FILE"])
if state_path.exists():
	state = json.loads(state_path.read_text(encoding="utf-8"))
else:
	state = {}
args = sys.argv[1:]


def save() -> None:
	state_path.write_text(json.dumps(state), encoding="utf-8")


def first_value(flag: str) -> str:
	for i, arg in enumerate(args):
		if arg == flag and i + 1 < len(args):
			return args[i + 1]
	return ""


state.setdefault("calls", []).append(args)

if args[:2] == ["issue", "create"]:
	repo = first_value("--repo") or "owner/repo"
	next_num = int(state.get("next_issue_number", 1201))
	state["next_issue_number"] = next_num + 1
	state.setdefault("issue_create_args", []).append(args)
	save()
	print(f"https://github.com/{repo}/issues/{next_num}")
	sys.exit(0)

save()
sys.exit(0)
'''
	mock_path = bin_dir / "gh"
	mock_path.write_text(gh_script, encoding="utf-8")
	mock_path.chmod(0o755)
	state_file.write_text("{}", encoding="utf-8")


def test_validate_needs_fixes_creates_orchestrator_labeled_issue() -> None:
	branch = _extract_needs_fixes_branch()

	with tempfile.TemporaryDirectory(prefix="test_validate_fixup_labels_") as td:
		tmp_path = Path(td)
		runtime_dir = tmp_path / "runtime"
		bin_dir = tmp_path / "bin"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		bin_dir.mkdir(parents=True, exist_ok=True)

		gh_state_file = runtime_dir / "gh_state.json"
		_install_mock_gh(bin_dir, gh_state_file)

		diagnose_file = runtime_dir / "validation_diagnosis.json"
		diagnose_file.write_text(
			json.dumps(
				{
					"status": "needs_fixes",
					"diagnosis": "runtime tests failed",
					"fix_issues": [
						{
							"id": "validation-fix-1",
							"title": "Fix flaky assertion",
							"body": "Apply deterministic wait strategy.",
							"priority": 2,
							"depends_on": [],
						}
					],
					"harness_fixes": "",
				}
			),
			encoding="utf-8",
		)

		labels_file = runtime_dir / "ensure_labels.txt"
		created_json_file = runtime_dir / "created_fix_issues.json"
		harness_script = f"""#!/usr/bin/env bash
set -euo pipefail

gh_retry() {{ "$@"; }}
ensure_label_exists() {{ printf '%s\\n' "$1" >> "${{ENSURE_LABELS_FILE}}"; }}
is_tracking_run() {{ return 0; }}
post_tracking_comment() {{ :; }}
set_tracking_phase_label() {{ :; }}
write_result_files() {{ :; }}
tg_notify() {{ :; }}

DIAG_STATUS=\"needs_fixes\"

case "${{DIAG_STATUS}}" in
  needs_fixes)
{branch}
    ;;
esac

printf '%s' "${{CREATED_FIX_ISSUES_JSON}}" > "${{CREATED_JSON_FILE}}"
"""
		script_path = runtime_dir / "needs_fixes_harness.sh"
		script_path.write_text(harness_script, encoding="utf-8")
		script_path.chmod(0o755)

		env = {
			"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
			"MOCK_GH_STATE_FILE": str(gh_state_file),
			"RUNTIME_DIR": str(runtime_dir),
			"DIAGNOSE_RESULT_FILE": str(diagnose_file),
			"VALIDATION_CYCLE": "2",
			"TRACKING_ISSUE_RAW": "1043",
			"INTEGRATION_BRANCH": "orchestrator/project-1020",
			"FAILED_TESTS": "3",
			"DIAG_TEXT": "runtime tests failed",
			"GITHUB_REPOSITORY": "owner/repo",
			"CREATED_FIX_ISSUES_JSON": "[]",
			"ENSURE_LABELS_FILE": str(labels_file),
			"CREATED_JSON_FILE": str(created_json_file),
		}
		run_env = os.environ.copy()
		run_env.update(env)

		proc = subprocess.run(
			["bash", str(script_path)],
			cwd=str(tmp_path),
			env=run_env,
			text=True,
			capture_output=True,
			timeout=60,
		)
		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"

		ensured = labels_file.read_text(encoding="utf-8").strip().splitlines()
		assert ensured == ["ai:clarification", "ai:orchestrator-managed"]

		state = json.loads(gh_state_file.read_text(encoding="utf-8"))
		issue_creates = state.get("issue_create_args", [])
		assert len(issue_creates) == 1
		create_args = issue_creates[0]
		assert create_args.count("--label") == 2
		assert "ai:clarification" in create_args
		assert "ai:orchestrator-managed" in create_args

		created_json = json.loads(created_json_file.read_text(encoding="utf-8"))
		assert created_json == [1201]


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	passed = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}")
			passed += 1
		except Exception as exc:
			print(f"  FAIL  {name}: {exc}")
			failed += 1
	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
