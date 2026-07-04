#!/usr/bin/env python3
"""Contract tests for the source-repo security-audit workflow."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "security-audit.yml"
INTERNAL_CLARIFY_PATH = REPO_ROOT / ".github" / "workflows" / "internal-clarify.yml"
SCRIPT_PATH = REPO_ROOT / "scripts" / "security_audit.sh"
_SANITIZED_GIT_ENV_KEYS = ("BASH_ENV", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX")


def _write_exec(path: Path, body: str) -> None:
	path.write_text(body, encoding="utf-8")
	path.chmod(0o755)


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
	for idx, arg in enumerate(args):
		if arg == flag and idx + 1 < len(args):
			return args[idx + 1]
	return ""


state.setdefault("calls", []).append(args)

if args[:2] == ["label", "create"]:
	state.setdefault("label_create_args", []).append(args)
	save()
	sys.exit(0)

if args[:2] == ["issue", "list"]:
	state.setdefault("issue_list_args", []).append(args)
	responses = state.setdefault("issue_list_responses", [])
	response = responses.pop(0) if responses else []
	save()
	print(json.dumps(response))
	sys.exit(0)

if args[:2] == ["issue", "create"]:
	state.setdefault("issue_create_args", []).append(args)
	body_file = first_value("--body-file")
	if body_file:
		state.setdefault("issue_create_bodies", []).append(Path(body_file).read_text(encoding="utf-8"))
	repo = first_value("--repo") or "owner/repo"
	next_issue_number = int(state.get("next_issue_number", 9100))
	state["next_issue_number"] = next_issue_number + 1
	save()
	print(f"https://github.com/{repo}/issues/{next_issue_number}")
	sys.exit(0)

if args[:2] == ["issue", "comment"]:
	state.setdefault("issue_comment_args", []).append(args)
	body_file = first_value("--body-file")
	if body_file:
		state.setdefault("issue_comment_bodies", []).append(Path(body_file).read_text(encoding="utf-8"))
	save()
	sys.exit(0)

if args[:2] == ["issue", "edit"]:
	state.setdefault("issue_edit_args", []).append(args)
	body_file = first_value("--body-file")
	if body_file:
		state.setdefault("issue_edit_bodies", []).append(Path(body_file).read_text(encoding="utf-8"))
	save()
	sys.exit(0)

if args[:2] == ["issue", "reopen"]:
	state.setdefault("issue_reopen_args", []).append(args)
	save()
	sys.exit(0)

save()
print(f"unexpected gh args: {args}", file=sys.stderr)
sys.exit(1)
	'''
	_write_exec(bin_dir / "gh", gh_script)


def _install_mock_codex(bin_dir: Path, state_file: Path) -> None:
	codex_script = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["MOCK_GH_STATE_FILE"])
state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
state.setdefault("codex_calls", []).append(sys.argv[1:])
state_path.write_text(json.dumps(state), encoding="utf-8")
sys.stdout.write(os.environ.get("MOCK_CODEX_OUTPUT", "[]"))
sys.exit(int(os.environ.get("MOCK_CODEX_EXIT_CODE", "0")))
'''
	_write_exec(bin_dir / "codex", codex_script)


def _run_security_audit(
	state: dict,
	*,
	enabled: bool = True,
	codex_output: str = "[]",
	extra_env: dict | None = None,
	cwd: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict]:
	with tempfile.TemporaryDirectory(prefix="security-audit-test-") as td:
		tmp_path = Path(td)
		bin_dir = tmp_path / "bin"
		bin_dir.mkdir(parents=True, exist_ok=True)
		state_file = tmp_path / "gh-state.json"
		_install_mock_gh(bin_dir, state_file)
		_install_mock_codex(bin_dir, state_file)
		state_file.write_text(json.dumps(state), encoding="utf-8")

		run_cwd = cwd or REPO_ROOT
		env = os.environ.copy()
		if Path(run_cwd) != REPO_ROOT:
			env = {key: value for key, value in env.items() if key not in _SANITIZED_GIT_ENV_KEYS}
		env.update(
			{
				"GH_TOKEN": "test-token",
				"GITHUB_REPOSITORY": "owner/repo",
				"OPENROUTER_API_KEY": "test-openrouter-key",
				"MOCK_CODEX_OUTPUT": codex_output,
				"MOCK_GH_STATE_FILE": str(state_file),
				"PATH": f"{bin_dir}:{env.get('PATH', '')}",
				"PYTHONDONTWRITEBYTECODE": "1",
				"SECURITY_AUDIT_ENABLED": "true" if enabled else "false",
			}
		)
		env.update(extra_env or {})
		proc = subprocess.run(
			["bash", "--noprofile", "--norc", str(SCRIPT_PATH)],
			cwd=run_cwd,
			env=env,
			capture_output=True,
			text=True,
			encoding="utf-8",
		)
		final_state = json.loads(state_file.read_text(encoding="utf-8"))
		return proc, final_state


def _git_fixture_repo(base_dir: Path) -> tuple[Path, str, str]:
	"""Create a two-commit fixture repo; returns (repo_dir, first_sha, head_sha)."""
	repo_dir = base_dir / "audited-repo"
	repo_dir.mkdir(parents=True, exist_ok=True)
	git_env = {key: value for key, value in os.environ.items() if key not in _SANITIZED_GIT_ENV_KEYS}
	git_env.update(
		{
			"GIT_AUTHOR_NAME": "t",
			"GIT_AUTHOR_EMAIL": "t@example.invalid",
			"GIT_COMMITTER_NAME": "t",
			"GIT_COMMITTER_EMAIL": "t@example.invalid",
		}
	)

	def _git(*args: str) -> str:
		return subprocess.run(
			["git", *args],
			cwd=repo_dir,
			env=git_env,
			check=True,
			capture_output=True,
			text=True,
			encoding="utf-8",
		).stdout.strip()

	_git("init", "-q")
	(repo_dir / "file_a.py").write_text("print('unchanged module')\nVALUE_A = 1\n", encoding="utf-8")
	_git("add", "file_a.py")
	_git("commit", "-q", "-m", "first commit")
	first_sha = _git("rev-parse", "HEAD")
	(repo_dir / "file_b.py").write_text("print('changed module')\nVALUE_B = 2\n", encoding="utf-8")
	_git("add", "file_b.py")
	_git("commit", "-q", "-m", "second commit")
	head_sha = _git("rev-parse", "HEAD")
	return repo_dir, first_sha, head_sha


def _iso_utc_for_current_week(*, day_offset: int) -> str:
	now_utc = datetime.now(timezone.utc)
	week_start = datetime.combine(
		(now_utc - timedelta(days=now_utc.weekday())).date(),
		datetime.min.time(),
		tzinfo=timezone.utc,
	)
	return (week_start + timedelta(days=day_offset, hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_security_audit_workflow_has_required_triggers_and_checkout_contract() -> None:
	content = WORKFLOW_PATH.read_text(encoding="utf-8")
	assert 'schedule:' in content
	assert 'workflow_dispatch: {}' in content
	assert 'workflow_call:' in content
	assert 'cron: "0 8 * * 0"' in content
	assert 'uses: actions/checkout@v5' in content
	assert 'ref: ${{ github.event.repository.default_branch }}' in content
	# Full history is required by the incremental scope resolver.
	assert 'fetch-depth: 0' in content


def test_security_audit_workflow_wires_codex_and_audit_env() -> None:
	content = WORKFLOW_PATH.read_text(encoding="utf-8")
	# Source-repo runs must keep using the local action so branch-local changes
	# to install-codex stay testable; consumer-called runs use the stable ref.
	assert "if: env.SECURITY_AUDIT_IS_SOURCE_REPO == 'true'" in content
	assert 'uses: ./.github/actions/install-codex' in content
	assert "if: env.SECURITY_AUDIT_IS_SOURCE_REPO != 'true'" in content
	assert 'uses: shubhodeep1/coding-workflows/.github/actions/install-codex@stable' in content
	assert 'scripts/write_codex_config.sh' in content
	assert 'OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}' in content
	assert "SECURITY_AUDIT_ENABLED: ${{ vars.SECURITY_AUDIT_ENABLED || 'true' }}" in content
	assert "SECURITY_AUDIT_CONFIDENCE_GATE: ${{ vars.SECURITY_AUDIT_CONFIDENCE_GATE || '8' }}" in content
	assert (
		"SECURITY_AUDIT_FP_EXCLUSIONS: ${{ vars.SECURITY_AUDIT_FP_EXCLUSIONS || 'scripts/security_audit_fp_exclusions.json' }}"
	) in content
	assert "SECURITY_AUDIT_SKIP_IF_UNCHANGED: ${{ vars.SECURITY_AUDIT_SKIP_IF_UNCHANGED || 'true' }}" in content
	assert "SECURITY_AUDIT_INCREMENTAL: ${{ vars.SECURITY_AUDIT_INCREMENTAL || 'true' }}" in content
	assert 'bash "${SECURITY_AUDIT_SUPPORT_DIR:-.}/scripts/security_audit.sh"' in content


def test_security_audit_consumer_template_calls_stable_reusable_workflow() -> None:
	template_path = REPO_ROOT / "workflow-templates" / "ai-security-audit.yml"
	content = template_path.read_text(encoding="utf-8")
	assert "cron: '0 8 * * 0'" in content
	assert 'workflow_dispatch: {}' in content
	assert 'uses: shubhodeep1/coding-workflows/.github/workflows/security-audit.yml@stable' in content
	assert 'secrets: inherit' in content
	manifest = (REPO_ROOT / "workflow-templates" / "profiles" / "full.txt").read_text(encoding="utf-8")
	assert "ai-security-audit.yml" in manifest.splitlines()


def test_security_audit_script_uses_read_only_codex_and_retry_wrappers() -> None:
	content = SCRIPT_PATH.read_text(encoding="utf-8")
	assert '--sandbox read-only' in content
	assert 'gh_retry gh issue list' in content
	assert 'gh_retry gh issue create' in content
	assert 'gh_retry gh issue comment' in content
	assert 'gh_retry gh issue edit' in content
	assert 'gh_retry gh issue reopen' in content
	assert 'CHANGED_FILE_COUNT="$(grep -c . "${CHANGED_FILES_FILE}" 2>/dev/null || true)"' in content
	assert 'if ! [[ "${CHANGED_FILE_COUNT}" =~ ^[0-9]+$ ]]; then' in content
	assert 'marker_regex = re.compile(re.escape(followup_marker_prefix) + r"([^>]+) -->")' in content


def test_internal_clarify_skips_source_repo_tracker_issues() -> None:
	content = INTERNAL_CLARIFY_PATH.read_text(encoding="utf-8")
	assert "if: ${{ !contains(github.event.issue.labels.*.name, 'ai:security-audit') && !contains(github.event.issue.labels.*.name, 'ai:retro') }}" in content


def test_security_audit_gate_disabled_skips_without_side_effects() -> None:
	proc, state = _run_security_audit({}, enabled=False)
	assert proc.returncode == 0, proc.stderr
	assert "SECURITY_AUDIT_ENABLED=false; skipping." in proc.stdout
	assert state.get("calls", []) == []
	assert state.get("codex_calls", []) == []


def test_security_audit_skips_when_head_unchanged_since_last_audit() -> None:
	head_sha = subprocess.run(
		["git", "rev-parse", "HEAD"],
		cwd=REPO_ROOT,
		check=True,
		capture_output=True,
		text=True,
		encoding="utf-8",
	).stdout.strip()
	state = {
		"issue_list_responses": [
			[
				{
					"number": 9000,
					"title": "AI Security Audit Tracker",
					"body": (
						"<!-- ai:security-audit-tracker:v1 -->\n# AI Security Audit Tracker\n\n"
						f"<!-- ai:security-audit-last-sha:{head_sha} -->\n"
					),
					"state": "OPEN",
					"url": "https://github.com/owner/repo/issues/9000",
				}
			],
		],
	}
	proc, final_state = _run_security_audit(state)
	assert proc.returncode == 0, proc.stderr
	assert f"skipping — HEAD {head_sha} unchanged since last audit (tracker=#9000)." in proc.stdout
	assert final_state.get("codex_calls", []) == []
	assert final_state.get("issue_comment_args", []) == []
	assert final_state.get("issue_create_args", []) == []


def test_security_audit_incremental_scope_drops_out_of_scope_findings() -> None:
	codex_output = json.dumps(
		[
			{
				"finding_id": "changed-file-finding",
				"owasp_or_stride_category": "A05:2021-Security Misconfiguration",
				"severity": "high",
				"confidence": 9,
				"file": "file_b.py",
				"line": 1,
				"exploit_scenario": "The newly added module prints without sanitising its constant.",
				"recommendation": "Validate the constant before use.",
			},
			{
				"finding_id": "unchanged-file-finding",
				"owasp_or_stride_category": "A05:2021-Security Misconfiguration",
				"severity": "high",
				"confidence": 9,
				"file": "file_a.py",
				"line": 1,
				"exploit_scenario": "This cites a file outside the incremental scope and must be dropped.",
				"recommendation": "Do not surface this issue.",
			},
		],
		ensure_ascii=True,
	)
	with tempfile.TemporaryDirectory(prefix="security-audit-fixture-") as fixture_td:
		repo_dir, first_sha, head_sha = _git_fixture_repo(Path(fixture_td))
		state = {
			"issue_list_responses": [
				[
					{
						"number": 9000,
						"title": "AI Security Audit Tracker",
						"body": (
							"<!-- ai:security-audit-tracker:v1 -->\n# AI Security Audit Tracker\n\n"
							f"<!-- ai:security-audit-last-sha:{first_sha} -->\n"
						),
						"state": "OPEN",
						"url": "https://github.com/owner/repo/issues/9000",
					}
				],
				[],
			],
		}
		proc, final_state = _run_security_audit(
			state,
			codex_output=codex_output,
			# Consumer-shaped run: support files resolve from the workflow
			# source tree while the audited checkout is the fixture repo.
			extra_env={"SECURITY_AUDIT_SUPPORT_DIR": str(REPO_ROOT)},
			cwd=repo_dir,
		)

	assert proc.returncode == 0, proc.stderr
	assert "scope=incremental" in proc.stdout
	assert "tracker=#9000 findings=1 followups_created=1" in proc.stdout
	comment_bodies = "\n".join(final_state.get("issue_comment_bodies", []))
	assert "Audit scope: incremental" in comment_bodies
	assert f"`{first_sha}`..`{head_sha}`" in comment_bodies
	assert "changed-file-finding" in comment_bodies
	assert "unchanged-file-finding" not in comment_bodies
	assert "Suppressed out-of-scope findings: 1" in comment_bodies
	edit_bodies = "\n".join(final_state.get("issue_edit_bodies", []))
	assert f"<!-- ai:security-audit-last-sha:{head_sha} -->" in edit_bodies


def test_security_audit_filters_findings_and_caps_followups() -> None:
	codex_output = json.dumps(
		[
			{
				"finding_id": "high-finding-one",
				"owasp_or_stride_category": "A01:2021-Broken Access Control",
				"severity": "high",
				"confidence": 9,
				"file": "scripts/label_helpers.sh",
				"line": 1,
				"exploit_scenario": "A caller could abuse label mutation sequencing to confuse downstream automation.",
				"recommendation": "Require a stricter precondition before mutating issue labels.",
			},
			{
				"finding_id": "low-confidence-finding",
				"owasp_or_stride_category": "A05:2021-Security Misconfiguration",
				"severity": "medium",
				"confidence": 7,
				"file": "scripts/security_audit.sh",
				"line": 1,
				"exploit_scenario": "This is intentionally below the confidence gate.",
				"recommendation": "Do not surface this issue.",
			},
			{
				"finding_id": "excluded-finding",
				"owasp_or_stride_category": "A03:2021-Injection",
				"severity": "high",
				"confidence": 10,
				"file": "scripts/gh_helpers.sh",
				"line": 1,
				"exploit_scenario": "A crafted gh api call here becomes rce through command injection.",
				"recommendation": "Avoid the gh api helper entirely.",
			},
			{
				"finding_id": "invalid-path",
				"owasp_or_stride_category": "A01:2021-Broken Access Control",
				"severity": "critical",
				"confidence": 10,
				"file": "missing/file.py",
				"line": 12,
				"exploit_scenario": "This should be rejected because the path does not exist.",
				"recommendation": "Do not surface this issue.",
			},
			{
				"finding_id": "high-finding-two",
				"owasp_or_stride_category": "A05:2021-Security Misconfiguration",
				"severity": "medium",
				"confidence": 8,
				"file": "scripts/security_audit.sh",
				"line": 1,
				"exploit_scenario": "The workflow could retain a broader token surface than intended.",
				"recommendation": "Narrow the token scope used for follow-up automation.",
			},
		],
		ensure_ascii=True,
	)
	state = {
		"issue_list_responses": [
			[],
			[
				{
					"number": 42,
					"title": "[security-audit] existing-finding: high scripts/example.py:10",
					"body": "<!-- ai:security-finding:existing-finding -->\nRefs #9100\n",
					"createdAt": _iso_utc_for_current_week(day_offset=0),
					"url": "https://github.com/owner/repo/issues/42",
				},
				{
					"number": 43,
					"title": "[security-audit] existing-finding-two: high scripts/example.py:20",
					"body": "<!-- ai:security-finding:existing-finding-two -->\nRefs #9100\n",
					"createdAt": _iso_utc_for_current_week(day_offset=1),
					"url": "https://github.com/owner/repo/issues/43",
				},
			],
		],
		"next_issue_number": 9100,
	}
	proc, final_state = _run_security_audit(state, codex_output=codex_output)

	assert proc.returncode == 0, proc.stderr
	assert "tracker=#9100 findings=2 followups_created=1" in proc.stdout
	codex_args = final_state.get("codex_calls", [[]])[0]
	assert "--sandbox" in codex_args
	assert codex_args[codex_args.index("--sandbox") + 1] == "read-only"
	assert len(final_state.get("label_create_args", [])) == 2
	assert len(final_state.get("issue_create_args", [])) == 2
	tracker_create_args = final_state["issue_create_args"][0]
	assert "ai:security-audit" in tracker_create_args
	followup_create_args = final_state["issue_create_args"][1]
	assert "ai:security" in followup_create_args
	assert "high-finding-one" in "\n".join(final_state.get("issue_comment_bodies", []))
	assert "high-finding-two" in "\n".join(final_state.get("issue_comment_bodies", []))
	assert "low-confidence-finding" not in "\n".join(final_state.get("issue_comment_bodies", []))
	assert "excluded-finding" not in "\n".join(final_state.get("issue_comment_bodies", []))
	assert "invalid-path" not in "\n".join(final_state.get("issue_comment_bodies", []))
	assert any("high-finding-one" in body for body in final_state.get("issue_create_bodies", []))
	assert not any("high-finding-two" in body for body in final_state.get("issue_create_bodies", [])[1:])


def main() -> int:
	for name in sorted(globals()):
		if name.startswith("test_") and callable(globals()[name]):
			globals()[name]()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
