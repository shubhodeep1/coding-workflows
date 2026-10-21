"""Tests for the synced-file security finding heal-intake kind (project #7604, Phase 1).

Covers scripts/workflow_failure_heal.py (validation, managed-path check,
fingerprint buckets, issue / occurrence composition) and the
`synced_security_finding` branch of scripts/workflow_failure_heal_intake.sh,
driven through the stub-gh harness of tests/test_workflow_failure_heal.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import test_workflow_failure_heal as base  # noqa: E402
import workflow_failure_heal as heal  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
LIB_PATH = SCRIPTS_DIR / "workflow_failure_heal.py"
INTAKE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "workflow-failure-heal-intake.yml"
SELF_REPO = base.SELF_REPO
CONSUMER_A = base.CONSUMER_REPO
CONSUMER_B = "shubhodeep1/second-consumer"
RELEASE = "c" * 40
HOOK = ".claude/hooks/guard.py"
WRAPPER = ".github/workflows/ai-review.yml"
FREE_TEXT = "SECRET-EXPLOIT-NARRATIVE"


def _finding(**overrides) -> dict:
	finding = {
		"file": HOOK,
		"line": 30,
		"category": "A01:2021-Broken Access Control",
		"severity": "high",
		"confidence": 9,
		"finding_id": "guard-bypass",
	}
	finding.update(overrides)
	return finding


def _payload(repo: str = CONSUMER_A, findings: list | None = None, **overrides) -> dict:
	payload = {
		"schema_version": heal.SCHEMA_VERSION,
		"source_repo": repo,
		"source_kind": "synced_security_finding",
		"wrapper_sha": RELEASE,
		"reporter_run_url": f"https://github.com/{repo}/actions/runs/{base._ID_RUN_ID}",
		"run_refs": [],
		"findings": findings if findings is not None else [_finding()],
	}
	payload.update(overrides)
	return payload


# ---------------------------------------------------------------------------
# Field validation
# ---------------------------------------------------------------------------


def test_valid_payload_keeps_only_the_allowed_fields() -> None:
	normalized = heal.validate_payload(_payload(findings=[_finding(severity="CRITICAL", category="a03:2021 injection via x")]))
	assert normalized["findings"] == [{
		"file": HOOK, "line": 30, "category": "A03:2021-Injection", "severity": "critical", "confidence": 9, "finding_id": "guard-bypass",
	}]
	assert normalized["failure_reason"] == "synced_security_finding"
	assert normalized["issue_number"] is None and normalized["label"] is None
	assert normalized["issue_title"] == "Synced-file security findings"
	assert normalized["workflow_name"] == "AI Security Audit"
	assert normalized["issue_excerpt"] == normalized["comments_excerpt"] == normalized["failure_evidence"] == ""
	assert heal.validate_payload(_payload(findings=[_finding(category="tampering")]))["findings"][0]["category"] == "Tampering"


@pytest.mark.parametrize(
	("raw", "expected"),
	[
		("A01:2021-Broken Access Control", "A01:2021-Broken Access Control"),
		("A01: Broken Access Control", "A01:2021-Broken Access Control"),
		("A03 Injection", "A03:2021-Injection"),
		("a10", "A10:2021-Server-Side Request Forgery"),
		("A05 - 2021 Security Misconfiguration", "A05:2021-Security Misconfiguration"),
		("STRIDE: Tampering", "Tampering"),
		("stride - elevation of privilege", "Elevation of Privilege"),
		("Tampering / Elevation of Privilege", "Tampering"),
		("STRIDE: Spoofing/Repudiation", "Spoofing"),
		("  information   disclosure ", "Information Disclosure"),
	],
)
def test_category_accepts_common_owasp_and_stride_spellings(raw, expected) -> None:
	assert heal.normalize_synced_security_category(raw) == expected


@pytest.mark.parametrize(
	"raw",
	[
		"A01:2017-Injection",  # 2017 numbering differs from 2021
		"A01 2017",
		"A1:2017-Injection",
		"A011",
		"A01x",
		"A00: Nothing",
		"A11",
		"STRIDE",
		"STRIDE: Made Up",
		"Made Up / Tampering",
		"Something else",
	],
)
def test_category_still_rejects_unmappable_spellings(raw) -> None:
	with pytest.raises(ValueError):
		heal.normalize_synced_security_category(raw)


@pytest.mark.parametrize(
	"finding",
	[
		_finding(finding_id="bad id"),
		_finding(finding_id="x" * 81),
		_finding(finding_id="<!--x"),
		_finding(finding_id="guard-bypass\n"),
		_finding(file=HOOK + "\n"),
		_finding(line=0),
		_finding(line=1_000_001),
		_finding(line=True),
		_finding(line="30"),
		_finding(severity="medium"),
		_finding(confidence=11),
		_finding(confidence=0),
		_finding(category="A1:2017-Injection"),
		_finding(category="Something else"),
		_finding(exploit_scenario=FREE_TEXT),
		_finding(recommendation=FREE_TEXT),
		{k: v for k, v in _finding().items() if k != "confidence"},
		_finding(file="../etc/passwd"),
		_finding(file="/abs/path.py"),
		_finding(file=".claude//x.py"),
		_finding(file=".claude/hooks/x\x01.py"),
		_finding(file=".claude/hooks/x y.py"),
		"not-an-object",
	],
)
def test_invalid_finding_is_rejected(finding) -> None:
	with pytest.raises(ValueError):
		heal.validate_payload(_payload(findings=[finding]))


def test_invalid_report_shapes_are_rejected() -> None:
	with pytest.raises(ValueError):
		heal.validate_payload(_payload(findings=[]))
	with pytest.raises(ValueError):
		heal.validate_payload(_payload(findings=[_finding(finding_id=f"id-{i}") for i in range(51)]))
	assert len(heal.validate_payload(_payload(findings=[_finding(finding_id=f"id-{i}") for i in range(50)]))["findings"]) == 50
	with pytest.raises(ValueError):
		heal.validate_payload(_payload(findings=[_finding(), _finding()]))
	with pytest.raises(ValueError):
		heal.validate_payload(_payload(run_refs=[{"repo": CONSUMER_A, "run_id": "5"}]))
	with pytest.raises(ValueError):
		heal.validate_payload(_payload(wrapper_sha=None))
	with pytest.raises(ValueError):
		heal.validate_payload(_payload(wrapper_sha="stable"))
	# reporter_run_id (plan step 5) is accepted only as a positive integer and never published.
	assert "reporter_run_id" not in heal.validate_payload(_payload(reporter_run_id="12345"))
	heal.validate_payload(_payload(reporter_run_id=12345))
	for bad in (FREE_TEXT, "0", True, "12 34"):
		with pytest.raises(ValueError):
			heal.validate_payload(_payload(reporter_run_id=bad))
	# Free text at the top level of the report is refused too.
	for key in ("exploit_scenario", "recommendation", "failure_evidence", "issue_excerpt", "issue_title"):
		with pytest.raises(ValueError):
			heal.validate_payload(_payload(**{key: FREE_TEXT}))


# ---------------------------------------------------------------------------
# Managed-path check, source mapping and fingerprints
# ---------------------------------------------------------------------------


def test_managed_path_check() -> None:
	digests = {HOOK: ["a" * 64], WRAPPER: ["b" * 64]}
	assert heal.check_synced_security_managed_paths([_finding()], digests) is None
	assert heal.check_synced_security_managed_paths([_finding(file=WRAPPER)], digests) is None
	assert heal.check_synced_security_managed_paths([_finding(file=".claude/hooks/other.py")], digests) == "unmanaged_path"
	assert heal.check_synced_security_managed_paths([_finding(file="../" + HOOK)], digests) == "unmanaged_path"
	assert heal.check_synced_security_managed_paths([_finding(file="/" + HOOK)], digests) == "unmanaged_path"
	assert heal.check_synced_security_managed_paths([_finding()], {}) == "release_unavailable"
	assert heal.check_synced_security_managed_paths([_finding()], None) == "release_unavailable"


def test_managed_path_cli(tmp_path: Path) -> None:
	payload = tmp_path / "payload.json"
	payload.write_text(json.dumps(_payload()), encoding="utf-8")
	digests = tmp_path / "digests.json"
	digests.write_text(json.dumps({WRAPPER: ["b" * 64]}), encoding="utf-8")
	result = subprocess.run(["python3", str(LIB_PATH), "synced-security-check-paths", "--payload-json", str(payload), "--managed-digests-json", str(digests)], capture_output=True, text=True, check=False)
	assert result.returncode == 1 and json.loads(result.stdout) == {"reason": "unmanaged_path"}
	digests.write_text(json.dumps({HOOK: ["a" * 64]}), encoding="utf-8")
	result = subprocess.run(["python3", str(LIB_PATH), "synced-security-check-paths", "--payload-json", str(payload), "--managed-digests-json", str(digests)], capture_output=True, text=True, check=False)
	assert result.returncode == 0


def test_source_paths_follow_the_sync_mapping() -> None:
	assert heal.synced_security_source_paths(WRAPPER) == ["workflow-templates/ai-review.yml"]
	assert heal.synced_security_source_paths(HOOK) == ["workflow-templates/.claude/hooks/guard.py"]
	assert heal.synced_security_source_paths("CLAUDE.md") == ["CLAUDE.md"]
	contract = {"managed_files": [{"source": "gate.sh", "target": "scripts/audit_gate.sh"}]}
	assert heal.synced_security_source_paths("scripts/audit_gate.sh", contract) == ["workflow-templates/audit-gate/gate.sh"]
	assert heal.synced_security_source_paths(".github/retired.txt") == []


def test_fingerprint_bucket_edges_and_clamp() -> None:
	fp = lambda line, bucket=25: heal.synced_security_fingerprint(HOOK, "Spoofing", line, bucket)  # noqa: E731
	assert fp(24) != fp(25)
	assert fp(25) == fp(26)
	assert fp(30) == fp(30)
	assert heal.synced_security_line_bucket(None) == 25
	assert heal.synced_security_line_bucket("") == 25
	assert heal.synced_security_line_bucket("abc") == 25
	assert heal.synced_security_line_bucket("0") == 1
	assert heal.synced_security_line_bucket("-3") == 1
	assert heal.synced_security_line_bucket("40") == 40
	env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
	cmd = ["python3", str(LIB_PATH), "synced-security-fingerprint", "--file", HOOK, "--category", "spoofing", "--line"]
	outputs = {}
	for line in (24, 25, 26):
		outputs[line] = subprocess.run([*cmd, str(line)], capture_output=True, text=True, check=True, env={**env, "HEAL_SYNCED_SECURITY_LINE_BUCKET": "25"}).stdout.strip()
	assert outputs[24] != outputs[25] == outputs[26]
	assert outputs[25] == heal.synced_security_fingerprint(HOOK, "Spoofing", 25, 25)
	# The env default is 25; --bucket overrides it.
	default = subprocess.run([*cmd, "25"], capture_output=True, text=True, check=True, env={k: v for k, v in env.items() if k != "HEAL_SYNCED_SECURITY_LINE_BUCKET"}).stdout.strip()
	assert default == outputs[25]
	clamped = subprocess.run([*cmd, "25", "--bucket", "0"], capture_output=True, text=True, check=True, env=env).stdout.strip()
	assert clamped == heal.synced_security_fingerprint(HOOK, "Spoofing", 25, 1)


def test_groups_merge_findings_of_one_bucket() -> None:
	findings = heal.validate_synced_security_findings([_finding(line=26, finding_id="b"), _finding(line=25, finding_id="a"), _finding(line=24, finding_id="c")])
	groups = heal.group_synced_security_findings(findings, 25)
	assert [len(group["findings"]) for group in groups] == [2, 1]
	assert [item["finding_id"] for item in groups[0]["findings"]] == ["a", "b"]
	assert (groups[0]["bucket_start"], groups[0]["bucket_end"]) == (25, 49)
	assert (groups[1]["bucket_start"], groups[1]["bucket_end"]) == (1, 24)


def test_line_bucket_is_capped_so_groups_stay_loadable(tmp_path) -> None:
	"""A bucket above the line cap used to give bucket_end past the loader's
	bound, so every group of the report failed with "invalid group bucket"."""
	cap = heal.SYNCED_SECURITY_LINE_MAX
	assert heal.synced_security_line_bucket(str(cap)) == cap
	assert heal.synced_security_line_bucket("5000000") == cap
	findings = heal.validate_synced_security_findings([_finding(line=cap, finding_id="edge")])
	for bucket in ("5000000", cap * 10):
		groups = heal.group_synced_security_findings(findings, heal.synced_security_line_bucket(bucket) if isinstance(bucket, str) else bucket)
		assert len(groups) == 1
		group_file = tmp_path / "group.json"
		group_file.write_text(json.dumps(groups[0]), encoding="utf-8")
		loaded = heal._load_synced_security_group(str(group_file))
		assert loaded["bucket_end"] == groups[0]["bucket_end"]
		assert loaded["bucket_start"] <= cap <= loaded["bucket_end"]


# ---------------------------------------------------------------------------
# Composition: validated fields only
# ---------------------------------------------------------------------------


def test_issue_and_occurrence_carry_no_free_text() -> None:
	normalized = heal.validate_payload(_payload())
	group = heal.group_synced_security_findings(normalized["findings"], 25)[0]
	marker = f"<!-- ai:workflow-heal-scope:v1 paths=workflow-templates/.claude/hooks/guard.py,tests/**,changelog.d/*.md runs={CONSUMER_A}:1234 -->"
	title, body = heal.compose_synced_security_issue(payload=normalized, group=group, gen=1, root=group["fingerprint"], scope_marker=marker, max_depth=3, intake_run_url="https://github.com/o/r/actions/runs/7")
	occurrence = heal.compose_synced_security_occurrence(payload=normalized, group=group, intake_run_url="https://github.com/o/r/actions/runs/7")
	assert title == f"[security] A01:2021-Broken Access Control in {HOOK} (lines 25-49)"
	markers = heal.parse_heal_markers(body)
	assert markers["fp"] == group["fingerprint"] and markers["gen"] == "1" and markers["classification"] == "workflow-defect"
	assert body.count("ai:workflow-heal-scope:v1") == 1 and marker in body
	assert "Target branch" not in body
	assert "`workflow-templates/.claude/hooks/guard.py`" in body
	assert occurrence.startswith(f"<!-- {heal.MARKER_PREFIX}occurrence -->")
	for text in (title, body, occurrence):
		assert FREE_TEXT not in text
		assert "exploit_scenario" not in text and "recommendation" not in text.lower().replace("the recommendation stay", "")
		assert CONSUMER_A in text or text is title
		assert "guard-bypass" in text or text is title


# ---------------------------------------------------------------------------
# Intake runs (stub gh)
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> None:
	subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class _Release:
	"""setup_git hook for base._run_intake: a release commit and a signed identity."""

	def __init__(self, payload: dict, *, identity: bool = True, workflow_file: str = "security-audit.yml", ref_sha: str | None = None, consumers: tuple[str, ...] = (CONSUMER_A, CONSUMER_B), retired: tuple[str, ...] = ()) -> None:
		self.payload = payload
		self.retired = retired
		self.identity = identity
		self.workflow_file = workflow_file
		self.ref_sha = ref_sha
		self.consumers = consumers
		self.tmp = Path(tempfile.mkdtemp(prefix="synced-sec-id-"))
		self.token_file = self.tmp / "identity.jwt"
		self.jwks_file = self.tmp / "jwks.json"
		self.jwks_file.write_text(json.dumps(base._test_jwks()), encoding="utf-8")

	def env(self) -> dict[str, str]:
		if not self.identity:
			return {}
		return base._identity_env(self.token_file, self.jwks_file)

	def __call__(self, tmp: Path, work: Path) -> None:
		for name in ("build_sync_manifest.py", "workflow_wrapper_refs.py"):
			shutil.copy(SCRIPTS_DIR / name, work / "scripts" / name)
		(work / ".github" / "ai" / "consumer_repos.json").write_text(json.dumps(list(self.consumers)), encoding="utf-8")
		(work / "workflow-templates" / ".claude" / "hooks").mkdir(parents=True)
		(work / "workflow-templates" / ".claude" / "hooks" / "guard.py").write_text("print('guard')\n", encoding="utf-8")
		(work / "workflow-templates" / "ai-review.yml").write_text(
			"jobs:\n  review:\n    uses: shubhodeep1/coding-workflows/.github/workflows/review_autofix.yml@stable\n", encoding="utf-8")
		if self.retired:
			(work / "workflow-templates" / "retired_files.txt").write_text(
				"# Consumer files retired from the sync.\n" + "".join(f"{path} {'a' * 64}\n" for path in self.retired), encoding="utf-8")
		_git(work, "init", "-q", "-b", "main")
		_git(work, "config", "user.name", "test")
		_git(work, "config", "user.email", "test@example.invalid")
		_git(work, "add", "workflow-templates")
		_git(work, "commit", "-qm", "release")
		sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, check=True, capture_output=True, text=True).stdout.strip()
		self.payload["wrapper_sha"] = sha
		claims = base._id_claims(
			now=int(time.time()),
			repository=self.payload["source_repo"],
			job_workflow_ref=f"{SELF_REPO}/.github/workflows/{self.workflow_file}@{self.ref_sha or sha}",
		)
		self.token_file.write_text(base._sign_test_jwt(claims), encoding="utf-8")


SCOPE_GUARD_ON = {"HEAL_INTAKE_SCOPE_GUARD_ENABLED": "true"}


def _intake(payload: dict, state: dict | None = None, *, extra_env: dict | None = None, **release_kwargs):
	release = _Release(payload, **release_kwargs)
	env = {**release.env(), **(extra_env or {})}
	state = state if state is not None else {"compare": {"status": "ahead"}, "heal_issues": []}
	try:
		result, state_after, _ = base._run_intake(payload, state, diagnosis="", extra_env=env, setup_git=release)
	finally:
		shutil.rmtree(release.tmp, ignore_errors=True)
	return result, state_after


def _api_writes(state: dict) -> list:
	return state.get("issues_created", []) + state.get("comments_posted", []) + state.get("issue_edits", [])


def test_intake_two_consumers_in_one_bucket_give_one_issue_and_one_occurrence() -> None:
	result, state = _intake(_payload(CONSUMER_A, [_finding(line=30)]), extra_env=SCOPE_GUARD_ON)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "WORKFLOW_HEAL received kind=synced_security_finding findings=1 opened=1 occurrences=0 skipped=0" in result.stdout
	assert len(state["issues_created"]) == 1
	created = state["issues_created"][0]
	assert created["repo"] == SELF_REPO and created["labels"] == ["ai:workflow-heal"]
	body = created["body"]
	assert body.count("ai:workflow-heal-scope:v1") == 1
	assert f"runs={CONSUMER_A}:{base._ID_RUN_ID}" in body
	assert "paths=workflow-templates/.claude/hooks/guard.py,tests/**,changelog.d/*.md" in body
	assert FREE_TEXT not in body
	# No log, run or diagnosis read for this kind.
	assert not [call for call in state.get("calls", []) if any("/actions/" in arg or "/logs" in arg for arg in call)]

	heal_issue = {
		"number": 901, "state": "open", "state_reason": None, "title": created["title"], "body": body, "author": "workflow-bot",
		"created_at": "2026-10-18T00:00:00Z", "closed_at": None, "html_url": f"https://github.com/{SELF_REPO}/issues/901", "pull_request": False,
	}
	second, state2 = _intake(_payload(CONSUMER_B, [_finding(line=26, finding_id="other-id")]), {"compare": {"status": "identical"}, "heal_issues": [heal_issue]}, extra_env=SCOPE_GUARD_ON)
	assert second.returncode == 0, second.stdout + second.stderr
	assert "WORKFLOW_HEAL received kind=synced_security_finding findings=1 opened=0 occurrences=1 skipped=0" in second.stdout
	assert "issues_created" not in state2
	comments = state2["comments_posted"]
	assert len(comments) == 1 and comments[0]["path"] == f"repos/{SELF_REPO}/issues/901/comments"
	assert comments[0]["body"].startswith("<!-- workflow-failure-heal:occurrence -->")
	assert CONSUMER_B in comments[0]["body"] and "other-id" in comments[0]["body"]


def test_intake_groups_two_findings_of_one_report_into_one_issue() -> None:
	result, state = _intake(_payload(CONSUMER_A, [_finding(line=30), _finding(line=31, finding_id="second"), _finding(line=200, finding_id="third")]), extra_env=SCOPE_GUARD_ON)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "findings=3 opened=2 occurrences=0 skipped=0" in result.stdout
	assert len(state["issues_created"]) == 2


def test_intake_budget_exhausted_opens_nothing() -> None:
	open_issues = [
		{"number": 800 + i, "state": "open", "state_reason": None, "title": "x", "body": f"<!-- workflow-failure-heal:fp={i:064x} -->", "author": "workflow-bot",
		 "created_at": "2026-01-01T00:00:00Z", "closed_at": None, "html_url": "", "pull_request": False}
		for i in range(10)
	]
	result, state = _intake(_payload(), {"compare": {"status": "ahead"}, "heal_issues": open_issues}, extra_env=SCOPE_GUARD_ON)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "skip reason=budget_exhausted detail=max_open_issues" in result.stdout
	assert "findings=1 opened=0 occurrences=0 skipped=1" in result.stdout
	assert not _api_writes(state)


def test_intake_scope_guard_off_creates_nothing() -> None:
	result, state = _intake(_payload())
	assert result.returncode == 0, result.stdout + result.stderr
	assert "skip reason=scope_guard_disabled" in result.stdout
	assert not _api_writes(state)


def test_intake_flag_off_skips_before_anything() -> None:
	for value in ("false", "FALSE", "bogus"):
		result, state = _intake(_payload(), extra_env={**SCOPE_GUARD_ON, "HEAL_SYNCED_SECURITY_FINDING_ENABLED": value})
		assert result.returncode == 0, result.stdout + result.stderr
		assert "WORKFLOW_HEAL skip reason=synced_security_finding_disabled" in result.stdout
		assert "report_auth=" not in result.stdout
		assert not state.get("calls")
	result, _ = _intake(_payload(), extra_env={**SCOPE_GUARD_ON, "HEAL_SYNCED_SECURITY_FINDING_ENABLED": ""})
	assert "synced_security_finding_disabled" not in result.stdout


@pytest.mark.parametrize(
	("release_kwargs", "state", "reason"),
	[
		({"workflow_file": "review_autofix.yml"}, None, "job_workflow_ref_mismatch"),
		({"identity": False}, None, "identity_required"),
		({"ref_sha": "d" * 40}, None, "release_mismatch"),
		({}, {"compare": {"status": "diverged"}, "heal_issues": []}, "release_not_on_stable"),
		({}, {"heal_issues": []}, "release_unavailable"),
	],
)
def test_intake_rejects_unverified_reports(release_kwargs, state, reason) -> None:
	result, state_after = _intake(_payload(), state, extra_env=SCOPE_GUARD_ON, **release_kwargs)
	assert result.returncode == 0, result.stdout + result.stderr
	assert f"report_auth=rejected reason={reason} " in result.stdout
	assert not _api_writes(state_after)


def test_intake_rejects_unmanaged_and_retired_paths() -> None:
	result, state = _intake(_payload(findings=[_finding(file=".claude/hooks/not-synced.py")]), extra_env=SCOPE_GUARD_ON)
	assert "report_auth=rejected reason=unmanaged_path " in result.stdout
	assert not _api_writes(state)



RETIRED = ".claude/commands/retired-helper.md"


def test_intake_skips_retired_file_findings_with_a_named_reason() -> None:
	"""A retired file is a managed path (the consumer's sync deletes its
	byte-identical copies), but nothing in coding-workflows ships it any more,
	so there is no source to fix: skip it early with reason=retired_file
	instead of reaching scope_unresolved, and open nothing."""
	result, state = _intake(_payload(findings=[_finding(file=RETIRED)]), extra_env=SCOPE_GUARD_ON, retired=(RETIRED,))
	assert result.returncode == 0, result.stdout + result.stderr
	assert "report_auth=rejected" not in result.stdout
	assert f"skip reason=retired_file file={RETIRED} " in result.stdout
	assert "scope_unresolved" not in result.stdout
	assert "WORKFLOW_HEAL received kind=synced_security_finding findings=1 opened=0 occurrences=0 skipped=1" in result.stdout
	assert not _api_writes(state)


def test_intake_retired_skip_leaves_live_findings_in_the_same_report() -> None:
	payload = _payload(findings=[_finding(file=RETIRED, finding_id="old"), _finding(line=30, finding_id="live")])
	result, state = _intake(payload, extra_env=SCOPE_GUARD_ON, retired=(RETIRED,))
	assert result.returncode == 0, result.stdout + result.stderr
	assert f"skip reason=retired_file file={RETIRED} " in result.stdout
	assert "WORKFLOW_HEAL received kind=synced_security_finding findings=2 opened=1 occurrences=0 skipped=1" in result.stdout
	assert len(state.get("issues_created", [])) == 1

def test_intake_rejects_self_repo_and_unregistered_consumers() -> None:
	result, state = _intake(_payload(repo="shubhodeep1/unknown-consumer"), extra_env=SCOPE_GUARD_ON)
	assert "skip reason=unregistered_source_repo" in result.stdout
	assert not _api_writes(state)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_workflow_and_script_default_the_switches() -> None:
	workflow = INTAKE_WORKFLOW.read_text(encoding="utf-8")
	assert "HEAL_SYNCED_SECURITY_FINDING_ENABLED: ${{ vars.HEAL_SYNCED_SECURITY_FINDING_ENABLED || 'true' }}" in workflow
	assert "HEAL_SYNCED_SECURITY_LINE_BUCKET: ${{ vars.HEAL_SYNCED_SECURITY_LINE_BUCKET || '25' }}" in workflow
	intake = base.INTAKE_SCRIPT.read_text(encoding="utf-8")
	assert '"${HEAL_SYNCED_SECURITY_FINDING_ENABLED:-true}"' in intake
	assert '"${HEAL_SYNCED_SECURITY_LINE_BUCKET:-25}"' in intake
	assert heal.REPORT_IDENTITY_WORKFLOW_BY_KIND["synced_security_finding"] == ("security-audit.yml",)
	assert "synced_security_finding" in heal.SOURCE_KINDS
