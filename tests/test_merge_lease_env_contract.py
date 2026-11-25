#!/usr/bin/env python3
"""Merge-lease env contract for workflow steps (issue #10078).

On 2026-11-24 the repository variable MERGE_LEASE_TTL_MINUTES was 480, yet
PR #9970's lease was renewed at 22:38 UTC for 240 minutes: the poller and
sweep steps that can grant or renew a lease passed only some of the lease
settings, so scripts/pr_checks_lib.sh fell back to its built-in default.

Rules enforced here, for every workflow-level, job-level and step-level
`env:` block in .github/workflows/*.yml and workflow-templates/*.yml:

1. Full set. A block that passes any setting in REQUIRED_KEYS passes all of
   them. A step is checked on its effective env (workflow env, then job env,
   then step env), so a job-level full set covers its steps.
2. Same form and default. Each value is `${{ vars.<KEY> || '<default>' }}`
   and the default equals the script default in scripts/pr_checks_lib.sh
   (which also keeps every workflow consistent with every other one).
3. Literal exception. The only other value allowed is an entry of
   LITERAL_EXCEPTIONS, and the line directly above it in the workflow must
   be a `#` comment naming the reason.
4. Unwired settings. UNWIRED_KEYS are read by the script with built-in
   defaults and are not repository variables (Q1=A on issue #10078). Wiring
   one is a separate, deliberate change.
5. Unknown settings. Any other MERGE_LEASE_* env key must first be
   classified in one of the two lists above.
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

try:
	import yaml
except ModuleNotFoundError:  # PyYAML is absent in some credential-free test containers; CI installs it.
	yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_PATH = REPO_ROOT / "scripts" / "pr_checks_lib.sh"

REQUIRED_KEYS = (
	"MERGE_LEASE_ENABLED",
	"MERGE_LEASE_TTL_MINUTES",
	"MERGE_LEASE_WAIT_LABEL",
	"MERGE_LEASE_REVOKE_ENABLED",
	"MERGE_LEASE_IDLE_MINUTES",
	"MERGE_LEASE_PRIORITY_HEAD_PREFIXES",
	"MERGE_LEASE_SOFT_PATHS",
	"MERGE_LEASE_MAX_HOLDERS",
)

UNWIRED_KEYS = (
	"MERGE_LEASE_MAX_HOLD_MINUTES",
	"MERGE_LEASE_REF_PREFIX",
	"MERGE_LEASE_METRICS_ENABLED",
)

# (workflow file name, step name, key) -> the only literal value accepted.
LITERAL_EXCEPTIONS = {
	("review_autofix_sweep.yml", "Ensure the final CI run on reviewed PR heads", "MERGE_LEASE_SOFT_PATHS"): "none",
}

VALUE_RE = re.compile(r"^\$\{\{ vars\.([A-Z0-9_]+) \|\| '([^']*)' \}\}$")


def collect_env_blocks(name: str, text: str):
	"""Yield (workflow, job_id, step_name, env, effective_env) per env block.

	job_id and step_name are None for the workflow-level block, step_name is
	None for a job-level block. effective_env overlays workflow, job and step.
	"""
	doc = yaml.safe_load(text) or {}
	if not isinstance(doc, dict):
		return
	wf_env = doc.get("env") if isinstance(doc.get("env"), dict) else {}
	if wf_env:
		yield name, None, None, wf_env, dict(wf_env)
	jobs = doc.get("jobs") if isinstance(doc.get("jobs"), dict) else {}
	for job_id, job in jobs.items():
		if not isinstance(job, dict):
			continue
		job_env = job.get("env") if isinstance(job.get("env"), dict) else {}
		job_effective = {**wf_env, **job_env}
		if job_env:
			yield name, job_id, None, job_env, job_effective
		steps = job.get("steps") if isinstance(job.get("steps"), list) else []
		for index, step in enumerate(steps):
			if not isinstance(step, dict) or not isinstance(step.get("env"), dict):
				continue
			step_name = str(step.get("name") or step.get("id") or f"step[{index}]")
			step_env = step["env"]
			yield name, job_id, step_name, step_env, {**job_effective, **step_env}


def script_defaults(lib_text: str) -> dict[str, str]:
	"""Map each required key to its default in scripts/pr_checks_lib.sh.

	Accepts both `${KEY:-default}` and `${KEY-default}`; every occurrence of
	a key must carry the same default.
	"""
	defaults: dict[str, str] = {}
	problems: list[str] = []
	for key in REQUIRED_KEYS:
		found = set(re.findall(r"\$\{" + re.escape(key) + r":?-([^}]*)\}", lib_text))
		if len(found) != 1:
			problems.append(f"{key}: expected one default in pr_checks_lib.sh, found {sorted(found)}")
			continue
		defaults[key] = found.pop()
	if problems:
		raise AssertionError("; ".join(problems))
	return defaults


def _literal_has_comment(text: str, key: str, literal: str) -> bool:
	lines = text.splitlines()
	pattern = re.compile(r"^\s*" + re.escape(key) + r":\s*" + re.escape(literal) + r"\s*$")
	hits = [i for i, line in enumerate(lines) if pattern.match(line)]
	if not hits:
		return False
	return all(i > 0 and lines[i - 1].strip().startswith("#") for i in hits)


def violations(files: dict[str, str], defaults: dict[str, str]) -> list[str]:
	out: list[str] = []
	for name, text in sorted(files.items()):
		if "MERGE_LEASE_" not in text and "MERGE_BASE_FRESHNESS_ENABLED" not in text:
			continue
		for wf, job_id, step_name, env, effective in collect_env_blocks(name, text):
			where = f"{wf} job={job_id or '-'} step={step_name or '-'}"
			lease_keys = [k for k in env if str(k).startswith("MERGE_LEASE_")]
			for key in lease_keys:
				if key in UNWIRED_KEYS:
					out.append(
						f"{where}: {key} is not wired as a repository variable; wiring it is a "
						"separate, deliberate change that must also update its README row and "
						"UNWIRED_KEYS in tests/test_merge_lease_env_contract.py"
					)
				elif key not in REQUIRED_KEYS:
					out.append(
						f"{where}: unknown merge-lease setting {key}; classify it in REQUIRED_KEYS "
						"or UNWIRED_KEYS in tests/test_merge_lease_env_contract.py"
					)
			# A block that turns on the merge-base freshness gate runs
			# _pr_base_fresh_for_merge, which can renew a lease, so it needs the
			# full set even when it carries no MERGE_LEASE_* key itself.
			runs_freshness_gate = "MERGE_BASE_FRESHNESS_ENABLED" in env
			if not any(k in REQUIRED_KEYS for k in lease_keys) and not runs_freshness_gate:
				continue
			missing = [k for k in REQUIRED_KEYS if k not in effective]
			if missing:
				reason = "passes some merge-lease settings" if lease_keys else "runs the merge-base freshness gate"
				out.append(f"{where}: {reason} but is missing {', '.join(missing)}")
			for key in lease_keys:
				if key not in REQUIRED_KEYS:
					continue
				value = "" if env[key] is None else str(env[key])
				match = VALUE_RE.match(value)
				if match:
					if match.group(1) != key:
						out.append(f"{where}: {key} reads vars.{match.group(1)}")
					elif match.group(2) != defaults[key]:
						out.append(
							f"{where}: {key} default '{match.group(2)}' differs from the "
							f"scripts/pr_checks_lib.sh default '{defaults[key]}'"
						)
					continue
				literal = LITERAL_EXCEPTIONS.get((wf, step_name, key))
				if literal is None or value != literal:
					out.append(
						f"{where}: {key} must be ${{{{ vars.{key} || '{defaults[key]}' }}}} "
						"(or an allowlisted literal exception)"
					)
				elif not _literal_has_comment(text, key, literal):
					out.append(f"{where}: literal {key}: {literal} needs a # comment line directly above naming the reason")
	return out


def _repo_workflow_files() -> dict[str, str]:
	files: dict[str, str] = {}
	for pattern in (".github/workflows/*.yml", "workflow-templates/*.yml"):
		for path in sorted(REPO_ROOT.glob(pattern)):
			files[path.name if pattern.startswith(".github") else f"workflow-templates/{path.name}"] = path.read_text(encoding="utf-8")
	return files


def _fixture(env_lines: str, step_name: str = "Lease step", job_env: str = "") -> str:
	return (
		"name: fixture\n"
		"on: workflow_dispatch\n"
		"jobs:\n"
		"  j:\n"
		"    runs-on: ubuntu-24.04\n"
		+ job_env
		+ "    steps:\n"
		f"      - name: {step_name}\n"
		"        env:\n"
		+ env_lines
		+ "\n        run: echo ok\n"
	)


if yaml is None and os.environ.get("GITHUB_ACTIONS") == "true":
	# CI installs PyYAML; a silent skip there would drop the contract unnoticed.
	raise RuntimeError("PyYAML required for the merge-lease env contract")


@unittest.skipIf(yaml is None, "PyYAML is not installed")
class MergeLeaseEnvContractTest(unittest.TestCase):
	@classmethod
	def setUpClass(cls) -> None:
		cls.defaults = script_defaults(LIB_PATH.read_text(encoding="utf-8"))

	def _full(self, drop: tuple[str, ...] = (), override: dict[str, str] | None = None) -> str:
		override = override or {}
		lines = []
		for key in REQUIRED_KEYS:
			if key in drop:
				continue
			if key in override:
				lines.append(f"          {key}: {override[key]}")
			else:
				lines.append(f"          {key}: ${{{{ vars.{key} || '{self.defaults[key]}' }}}}")
		return "\n".join(lines)

	def test_script_defaults_found(self) -> None:
		self.assertEqual(set(self.defaults), set(REQUIRED_KEYS))
		self.assertEqual(self.defaults["MERGE_LEASE_ENABLED"], "true")
		self.assertEqual(self.defaults["MERGE_LEASE_WAIT_LABEL"], "ai:merge-lease-wait")
		self.assertRegex(self.defaults["MERGE_LEASE_TTL_MINUTES"], r"^[1-9][0-9]*$")

	def test_repo_workflows_satisfy_contract(self) -> None:
		found = violations(_repo_workflow_files(), self.defaults)
		self.assertEqual(found, [], "\n".join(found))

	def test_fixture_full_set_passes(self) -> None:
		self.assertEqual(violations({"f.yml": _fixture(self._full())}, self.defaults), [])

	def test_fixture_idle_without_ttl_fails(self) -> None:
		text = _fixture(self._full(drop=("MERGE_LEASE_TTL_MINUTES",)))
		found = violations({"f.yml": text}, self.defaults)
		self.assertEqual(len(found), 1, found)
		self.assertIn("missing MERGE_LEASE_TTL_MINUTES", found[0])

	def test_fixture_different_default_fails(self) -> None:
		text = _fixture(self._full(override={"MERGE_LEASE_MAX_HOLDERS": "${{ vars.MERGE_LEASE_MAX_HOLDERS || '2' }}"}))
		found = violations({"f.yml": text}, self.defaults)
		self.assertEqual(len(found), 1, found)
		self.assertIn("MERGE_LEASE_MAX_HOLDERS default '2' differs", found[0])

	def test_fixture_wrong_variable_fails(self) -> None:
		text = _fixture(self._full(override={"MERGE_LEASE_TTL_MINUTES": f"${{{{ vars.OTHER || '{self.defaults['MERGE_LEASE_TTL_MINUTES']}' }}}}"}))
		self.assertEqual(len(violations({"f.yml": text}, self.defaults)), 1)

	def _soft_paths_none(self, comment: bool) -> str:
		lines = self._full(drop=("MERGE_LEASE_SOFT_PATHS",))
		if comment:
			lines += "\n          # Soft path class off: the base sync treats clean and overlap alike."
		lines += "\n          MERGE_LEASE_SOFT_PATHS: none"
		return lines

	def test_fixture_soft_paths_none_with_comment_accepted(self) -> None:
		text = _fixture(self._soft_paths_none(comment=True), step_name="Ensure the final CI run on reviewed PR heads")
		self.assertEqual(violations({"review_autofix_sweep.yml": text}, self.defaults), [])

	def test_fixture_soft_paths_none_without_comment_fails(self) -> None:
		text = _fixture(self._soft_paths_none(comment=False), step_name="Ensure the final CI run on reviewed PR heads")
		found = violations({"review_autofix_sweep.yml": text}, self.defaults)
		self.assertEqual(len(found), 1, found)
		self.assertIn("needs a # comment", found[0])

	def test_fixture_soft_paths_none_outside_allowlist_fails(self) -> None:
		text = _fixture(self._soft_paths_none(comment=True), step_name="Some other step")
		found = violations({"review_autofix_sweep.yml": text}, self.defaults)
		self.assertEqual(len(found), 1, found)
		self.assertIn("allowlisted literal exception", found[0])

	def test_fixture_unwired_key_fails(self) -> None:
		text = _fixture(self._full() + "\n          MERGE_LEASE_REF_PREFIX: ${{ vars.MERGE_LEASE_REF_PREFIX || 'refs/merge-lease/' }}")
		found = violations({"f.yml": text}, self.defaults)
		self.assertEqual(len(found), 1, found)
		self.assertIn("not wired as a repository variable", found[0])

	def test_fixture_unknown_key_fails(self) -> None:
		text = _fixture(self._full() + "\n          MERGE_LEASE_SOMETHING_NEW: ${{ vars.MERGE_LEASE_SOMETHING_NEW || 'x' }}")
		found = violations({"f.yml": text}, self.defaults)
		self.assertEqual(len(found), 1, found)
		self.assertIn("unknown merge-lease setting", found[0])

	def test_fixture_job_env_full_set_covers_step(self) -> None:
		job_env = "    env:\n" + self._full().replace("          ", "      ") + "\n"
		step_env = f"          MERGE_LEASE_IDLE_MINUTES: ${{{{ vars.MERGE_LEASE_IDLE_MINUTES || '{self.defaults['MERGE_LEASE_IDLE_MINUTES']}' }}}}"
		text = _fixture(step_env, job_env=job_env)
		self.assertEqual(violations({"f.yml": text}, self.defaults), [])

	def test_fixture_freshness_gate_without_lease_keys_fails(self) -> None:
		text = _fixture("          MERGE_BASE_FRESHNESS_ENABLED: ${{ vars.MERGE_BASE_FRESHNESS_ENABLED || 'true' }}")
		found = violations({"f.yml": text}, self.defaults)
		self.assertEqual(len(found), 1, found)
		self.assertIn("runs the merge-base freshness gate but is missing MERGE_LEASE_ENABLED", found[0])

	def test_fixture_freshness_gate_with_full_set_passes(self) -> None:
		text = _fixture(self._full() + "\n          MERGE_BASE_FRESHNESS_ENABLED: ${{ vars.MERGE_BASE_FRESHNESS_ENABLED || 'true' }}")
		self.assertEqual(violations({"f.yml": text}, self.defaults), [])

	def test_fixture_partial_job_env_fails(self) -> None:
		job_env = "    env:\n" + self._full(drop=("MERGE_LEASE_ENABLED",)).replace("          ", "      ") + "\n"
		text = _fixture("          OTHER: x", job_env=job_env)
		found = violations({"f.yml": text}, self.defaults)
		self.assertEqual(len(found), 1, found)
		self.assertIn("missing MERGE_LEASE_ENABLED", found[0])


if __name__ == "__main__":
	unittest.main()
