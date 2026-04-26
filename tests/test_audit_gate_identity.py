#!/usr/bin/env python3
"""Behavior tests for canonical audit-gate identity matching."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CHECK_SCRIPT_SOURCE = REPO_ROOT / "workflow-templates" / "audit-gate" / "assets" / "scripts" / "security" / "check-npm-audit.js"


def _write_json(path: Path, payload: object) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _make_repo(*, allowlist: list[dict[str, object]], audit_payload: dict[str, object], audit_exit_code: int = 1) -> tuple[Path, dict[str, str]]:
	repo_root = Path(tempfile.mkdtemp(prefix="audit-gate-identity-"))

	_write_json(
		repo_root / "package.json",
		{
			"name": "fixture",
			"version": "1.0.0",
			"private": True,
			"scripts": {
				"audit:ci": "node scripts/security/check-npm-audit.js"
			},
		},
	)
	_write_json(repo_root / "security" / "dependency-audit-allowlist.json", allowlist)
	script_target = repo_root / "scripts" / "security" / "check-npm-audit.js"
	script_target.parent.mkdir(parents=True, exist_ok=True)
	shutil.copy2(CHECK_SCRIPT_SOURCE, script_target)

	real_npm = shutil.which("npm")
	assert real_npm is not None, "npm is required for audit gate tests"

	payload_path = repo_root / ".tmp" / "npm_audit_payload.json"
	_write_json(payload_path, audit_payload)

	wrapper_dir = repo_root / ".tmp" / "bin"
	wrapper_dir.mkdir(parents=True, exist_ok=True)
	wrapper_path = wrapper_dir / "npm"
	wrapper_path.write_text(
		"#!/usr/bin/env bash\n"
		"set -euo pipefail\n"
		"if [ \"${1:-}\" = \"audit\" ] && [ \"${2:-}\" = \"--json\" ]; then\n"
		"\tcat \"${AUDIT_GATE_FAKE_AUDIT_JSON}\"\n"
		"\texit \"${AUDIT_GATE_FAKE_AUDIT_EXIT_CODE:-1}\"\n"
		"fi\n"
		"exec \"${AUDIT_GATE_REAL_NPM}\" \"$@\"\n",
		encoding="utf-8",
	)
	wrapper_path.chmod(0o755)

	env = os.environ.copy()
	env["PATH"] = f"{wrapper_dir}:{env.get('PATH', '')}"
	env["AUDIT_GATE_REAL_NPM"] = real_npm
	env["AUDIT_GATE_FAKE_AUDIT_JSON"] = str(payload_path)
	env["AUDIT_GATE_FAKE_AUDIT_EXIT_CODE"] = str(audit_exit_code)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["_AUDIT_TEST_TEMP_DIR"] = str(repo_root)
	return repo_root, env


def _run_gate(*, repo_root: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
	result = subprocess.run(
		["npm", "run", "audit:ci"],
		cwd=str(repo_root),
		text=True,
		capture_output=True,
		check=False,
		env=env,
	)
	_temp_dir = env.get("_AUDIT_TEST_TEMP_DIR")
	if _temp_dir:
		shutil.rmtree(_temp_dir, ignore_errors=True)
	return result


def test_via_packages_churn_is_informational_only() -> None:
	allowlist = [
		{
			"package": "axios",
			"severity": "high",
			"advisoryId": "GHSA-aaaa-bbbb-cccc",
			"viaPackages": ["follow-redirects", "legacy-path"],
		}
	]
	audit_payload = {
		"auditReportVersion": 2,
		"vulnerabilities": {
			"axios": {
				"name": "axios",
				"severity": "high",
				"via": [
					{
						"source": 1097679,
						"name": "axios",
						"title": "SSRF issue in axios (GHSA-aaaa-bbbb-cccc)",
					},
				],
				"nodes": ["node_modules/foo/node_modules/axios"],
			},
		},
	}

	repo_root, env = _make_repo(allowlist=allowlist, audit_payload=audit_payload)
	result = _run_gate(repo_root=repo_root, env=env)

	assert result.returncode == 0, f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
	assert "matched allowlist entries" in result.stdout


def test_same_package_severity_different_advisories_do_not_collide() -> None:
	allowlist = [
		{
			"package": "lodash",
			"severity": "high",
			"advisoryId": "GHSA-1111-2222-3333",
		}
	]
	audit_payload = {
		"auditReportVersion": 2,
		"vulnerabilities": {
			"lodash": {
				"name": "lodash",
				"severity": "high",
				"via": [
					{
						"source": 1100001,
						"name": "lodash",
						"title": "Prototype pollution GHSA-1111-2222-3333",
					},
					{
						"source": 1100002,
						"name": "lodash",
						"title": "Another advisory GHSA-4444-5555-6666",
					},
				],
				"nodes": ["node_modules/lodash"],
			},
		},
	}

	repo_root, env = _make_repo(allowlist=allowlist, audit_payload=audit_payload)
	result = _run_gate(repo_root=repo_root, env=env)

	assert result.returncode == 1
	assert "Found 1 unallowlisted vulnerability finding(s)" in result.stderr
	assert "advisoryId=GHSA-4444-5555-6666" in result.stderr
	assert "advisoryId=GHSA-1111-2222-3333" not in result.stderr


def test_cve_fallback_is_used_when_ghsa_is_absent() -> None:
	allowlist = [
		{
			"package": "minimist",
			"severity": "moderate",
			"advisoryId": "CVE-2020-7598",
		}
	]
	audit_payload = {
		"auditReportVersion": 2,
		"vulnerabilities": {
			"minimist": {
				"name": "minimist",
				"severity": "moderate",
				"via": [
					{
						"source": 1096466,
						"name": "minimist",
						"title": "Prototype Pollution in minimist CVE-2020-7598",
						"url": "https://nvd.nist.gov/vuln/detail/CVE-2020-7598",
					},
				],
				"nodes": ["node_modules/minimist"],
			},
		},
	}

	repo_root, env = _make_repo(allowlist=allowlist, audit_payload=audit_payload)
	result = _run_gate(repo_root=repo_root, env=env)

	assert result.returncode == 0, f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
	assert "matched allowlist entries" in result.stdout


def test_cve_fallback_supports_object_shaped_cves_entries() -> None:
	allowlist = [
		{
			"package": "minimist",
			"severity": "moderate",
			"advisoryId": "CVE-2020-7598",
		}
	]
	audit_payload = {
		"auditReportVersion": 2,
		"vulnerabilities": {
			"minimist": {
				"name": "minimist",
				"severity": "moderate",
				"via": [
					{
						"source": 1096466,
						"name": "minimist",
						"title": "Prototype Pollution in minimist",
						"cves": [{"id": "CVE-2020-7598"}],
					},
				],
				"nodes": ["node_modules/minimist"],
			},
		},
	}

	repo_root, env = _make_repo(allowlist=allowlist, audit_payload=audit_payload)
	result = _run_gate(repo_root=repo_root, env=env)

	assert result.returncode == 0, f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
	assert "matched allowlist entries" in result.stdout


def test_failure_output_includes_via_packages_context() -> None:
	allowlist: list[dict[str, object]] = []
	audit_payload = {
		"auditReportVersion": 2,
		"vulnerabilities": {
			"serialize-javascript": {
				"name": "serialize-javascript",
				"severity": "high",
				"via": [
					{
						"source": 1099987,
						"name": "serialize-javascript",
						"title": "RCE advisory GHSA-hxcc-f52p-wc94",
					},
				],
				"nodes": [
					"node_modules/webpack/node_modules/terser-webpack-plugin/node_modules/serialize-javascript",
				],
			},
		},
	}

	repo_root, env = _make_repo(allowlist=allowlist, audit_payload=audit_payload)
	result = _run_gate(repo_root=repo_root, env=env)

	assert result.returncode == 1
	assert "advisoryId=GHSA-HXCC-F52P-WC94" in result.stderr
	assert "viaPackages=webpack > terser-webpack-plugin" in result.stderr
