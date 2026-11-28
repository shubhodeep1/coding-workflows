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
   then step env), so a job-level full set covers its steps. A block that
   sets MERGE_BASE_FRESHNESS_ENABLED itself also needs the full set, because
   _pr_base_fresh_for_merge can renew a lease. A lease-writing step is found
   only by these env markers (a REQUIRED_KEYS key, the freshness flag, or a
   READ_ONLY_LEASE_STEPS entry), not by its `run:` text: several steps run
   scripts/orchestrate_poll_process.sh in modes that exit before any lease
   path, so matching the script name would report steps that never write one.
2. Same form and default. Each value is `${{ vars.<KEY> || '<default>' }}`
   and the default equals the script default in scripts/pr_checks_lib.sh
   (which also keeps every workflow consistent with every other one).
3. Literal exception. The only other value allowed is an entry of
   LITERAL_EXCEPTIONS, and the line directly above it in the workflow must
   be a `#` comment naming the reason.
4. Unwired settings. UNWIRED_KEYS are read by the script with built-in
   defaults and are not repository variables (Q1=A on issue #10078). Wiring
   one is a separate, deliberate change.
5. Optional settings. OPTIONAL_KEYS are read only by the steps that need
   them (MERGE_LEASE_HOLD_LABELS: the merge-train release steps; the
   ready-first grant, conflict-release and inline-sync switches: the tick and
   inline-merge steps), so they are not part of the full set. Where one is passed it uses the rule 2 form,
   and its default equals the scripts/pr_checks_lib.sh default when the
   script carries exactly one; otherwise every workflow site agrees.
6. Unknown settings. Any other MERGE_LEASE_* env key must first be
   classified in one of the three lists above.
7. Read-only lease steps. A step listed in READ_ONLY_LEASE_STEPS only reads
   the lease and never grants or renews one, so it passes exactly the listed
   subset of REQUIRED_KEYS instead of the full set (rule 2 still applies to
   each value, and dropping a listed key is reported as missing). Passing any
   other required key, or setting MERGE_BASE_FRESHNESS_ENABLED, puts the step
   back under rule 1.
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

# Per-step settings outside the full set (rule 5). MERGE_LEASE_HOLD_LABELS is
# passed by the merge-train release steps of cancel_on_pr_close.yml and
# orchestrate_poll.yml on main (CI run 45608850132, shard 31).
# MERGE_LEASE_READY_GRANT_ENABLED, MERGE_LEASE_CONFLICT_RELEASE_ENABLED and
# MERGE_LEASE_CONFLICT_RELEASED_LABEL (issue #10098) are passed by the steps
# that run the merge-lease tick (cancel_on_pr_close.yml, orchestrate_poll.yml,
# review_autofix.yml); MERGE_LEASE_INLINE_SYNC_ENABLED (issue #10198) by the
# review_autofix.yml gate and inline update-and-merge sites. Each defaults in
# scripts/pr_checks_lib.sh, so rule 5 compares the workflow default with it.
OPTIONAL_KEYS = (
	"MERGE_LEASE_HOLD_LABELS",
	"MERGE_LEASE_READY_GRANT_ENABLED",
	"MERGE_LEASE_CONFLICT_RELEASE_ENABLED",
	"MERGE_LEASE_CONFLICT_RELEASED_LABEL",
	"MERGE_LEASE_INLINE_SYNC_ENABLED",
)

# (workflow file name, step name, key) -> the only literal value accepted.
LITERAL_EXCEPTIONS = {
	("review_autofix_sweep.yml", "Ensure the final CI run on reviewed PR heads", "MERGE_LEASE_SOFT_PATHS"): "none",
}

# (workflow file name, step name) -> the REQUIRED_KEYS subset a read-only
# lease step may pass instead of the full set (rule 7). The codex-agent
# "Single-issue security pass" step runs scripts/review_single_issue_security_pass.sh,
# whose lease check only calls _merge_lease_read / _merge_lease_holder_row /
# _merge_lease_max_holders to let a lease holder past the audit limit; it
# never writes the lease, so TTL, idle and revocation settings have no reader
# there. It reached this branch from main through the base sync. Its twin
# "Single-issue security pass (deterministic skip)" runs the same script and is
# listed too, so its classification does not depend on its job env.
READ_ONLY_LEASE_STEPS = {
	("review_autofix.yml", "Single-issue security pass"): frozenset({"MERGE_LEASE_ENABLED", "MERGE_LEASE_MAX_HOLDERS"}),
	("review_autofix.yml", "Single-issue security pass (deterministic skip)"): frozenset({"MERGE_LEASE_ENABLED", "MERGE_LEASE_MAX_HOLDERS"}),
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


def optional_script_defaults(lib_text: str) -> dict[str, str]:
	"""Map each OPTIONAL_KEYS key to its scripts/pr_checks_lib.sh default.

	A key is left out when the script has no default, several different ones,
	or one built from another expansion; rule 5 then compares the workflow
	sites with each other instead.
	"""
	out: dict[str, str] = {}
	for key in OPTIONAL_KEYS:
		found = set(re.findall(r"\$\{" + re.escape(key) + r":?-([^}]*)\}", lib_text))
		if len(found) == 1:
			value = found.pop()
			if "$" not in value:
				out[key] = value
	return out


def _literal_has_comment(text: str, key: str, literal: str) -> bool:
	lines = text.splitlines()
	pattern = re.compile(r"^\s*" + re.escape(key) + r":\s*" + re.escape(literal) + r"\s*$")
	hits = [i for i, line in enumerate(lines) if pattern.match(line)]
	if not hits:
		return False
	# The comment must carry text: a bare `#` names no reason. A line-based
	# check cannot judge the reason itself, so non-empty text is the bar.
	return all(i > 0 and lines[i - 1].strip().startswith("#") and lines[i - 1].strip().lstrip("#").strip() for i in hits)


def violations(files: dict[str, str], defaults: dict[str, str], optional_defaults: dict[str, str] | None = None) -> list[str]:
	out: list[str] = []
	optional_defaults = optional_defaults or {}
	# key -> [(where, default)] for optional keys without a script default.
	optional_seen: dict[str, list[tuple[str, str]]] = {}
	for name, text in sorted(files.items()):
		if (
			"MERGE_LEASE_" not in text
			and "MERGE_BASE_FRESHNESS_ENABLED" not in text
			and not any(reader_wf == name for reader_wf, _ in READ_ONLY_LEASE_STEPS)
		):
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
				elif key in OPTIONAL_KEYS:
					value = "" if env[key] is None else str(env[key])
					match = VALUE_RE.match(value)
					if not match or match.group(1) != key:
						out.append(f"{where}: {key} must be ${{{{ vars.{key} || '<default>' }}}}")
					elif key in optional_defaults:
						if match.group(2) != optional_defaults[key]:
							out.append(
								f"{where}: {key} default '{match.group(2)}' differs from the "
								f"scripts/pr_checks_lib.sh default '{optional_defaults[key]}'"
							)
					else:
						optional_seen.setdefault(key, []).append((where, match.group(2)))
				elif key not in REQUIRED_KEYS:
					out.append(
						f"{where}: unknown merge-lease setting {key}; classify it in REQUIRED_KEYS, "
						"OPTIONAL_KEYS or UNWIRED_KEYS in tests/test_merge_lease_env_contract.py"
					)
			# A block that turns on the merge-base freshness gate runs
			# _pr_base_fresh_for_merge, which can renew a lease, so it needs the
			# full set even when it carries no MERGE_LEASE_* key itself.
			runs_freshness_gate = "MERGE_BASE_FRESHNESS_ENABLED" in env
			# A listed reader is looked up before the skip, so one that drops
			# both of its listed keys is still reported as missing them.
			read_only_keys = READ_ONLY_LEASE_STEPS.get((wf, step_name))
			if not any(k in REQUIRED_KEYS for k in lease_keys) and not runs_freshness_gate and read_only_keys is None:
				continue
			missing = [k for k in REQUIRED_KEYS if k not in effective]
			if (
				read_only_keys is not None
				and not runs_freshness_gate
				and {k for k in lease_keys if k in REQUIRED_KEYS} <= read_only_keys
			):
				# A reader is held to its listed keys: dropping one (for example
				# MERGE_LEASE_MAX_HOLDERS, which falls back to the script default
				# of 2) is still reported as missing.
				missing = [k for k in REQUIRED_KEYS if k in read_only_keys and k not in effective]
			if missing:
				if lease_keys:
					reason = "passes some merge-lease settings"
				elif runs_freshness_gate:
					reason = "runs the merge-base freshness gate"
				else:
					reason = "is a listed read-only lease step"
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
	for key, sites in sorted(optional_seen.items()):
		if len({default for _, default in sites}) > 1:
			out.append(f"{key}: workflow defaults disagree: " + "; ".join(f"{where} '{default}'" for where, default in sites))
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
		lib_text = LIB_PATH.read_text(encoding="utf-8")
		cls.defaults = script_defaults(lib_text)
		cls.optional_defaults = optional_script_defaults(lib_text)

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
		found = violations(_repo_workflow_files(), self.defaults, self.optional_defaults)
		self.assertEqual(found, [], "\n".join(found))

	def test_fixture_full_set_passes(self) -> None:
		self.assertEqual(violations({"f.yml": _fixture(self._full())}, self.defaults), [])

	def test_fixture_idle_without_ttl_fails(self) -> None:
		text = _fixture(self._full(drop=("MERGE_LEASE_TTL_MINUTES",)))
		found = violations({"f.yml": text}, self.defaults)
		self.assertEqual(len(found), 1, found)
		self.assertIn("missing MERGE_LEASE_TTL_MINUTES", found[0])

	def test_fixture_different_default_fails(self) -> None:
		text = _fixture(self._full(override={"MERGE_LEASE_MAX_HOLDERS": "${{ vars.MERGE_LEASE_MAX_HOLDERS || '3' }}"}))
		found = violations({"f.yml": text}, self.defaults)
		self.assertEqual(len(found), 1, found)
		self.assertIn("MERGE_LEASE_MAX_HOLDERS default '3' differs", found[0])

	def test_fixture_wrong_variable_fails(self) -> None:
		text = _fixture(self._full(override={"MERGE_LEASE_TTL_MINUTES": f"${{{{ vars.OTHER || '{self.defaults['MERGE_LEASE_TTL_MINUTES']}' }}}}"}))
		self.assertEqual(len(violations({"f.yml": text}, self.defaults)), 1)

	def _soft_paths_none(self, comment: bool, comment_text: str = "Soft path class off: the base sync treats clean and overlap alike.") -> str:
		lines = self._full(drop=("MERGE_LEASE_SOFT_PATHS",))
		if comment:
			lines += ("\n          # " + comment_text).rstrip()
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

	def test_fixture_soft_paths_none_with_bare_comment_fails(self) -> None:
		text = _fixture(self._soft_paths_none(comment=True, comment_text=""), step_name="Ensure the final CI run on reviewed PR heads")
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

	def test_fixture_optional_key_without_full_set_passes(self) -> None:
		text = _fixture("          MERGE_LEASE_HOLD_LABELS: ${{ vars.MERGE_LEASE_HOLD_LABELS || 'a,b' }}")
		self.assertEqual(violations({"f.yml": text}, self.defaults, {}), [])

	def test_fixture_optional_key_literal_fails(self) -> None:
		text = _fixture("          MERGE_LEASE_HOLD_LABELS: a,b")
		found = violations({"f.yml": text}, self.defaults, {})
		self.assertEqual(len(found), 1, found)
		self.assertIn("MERGE_LEASE_HOLD_LABELS must be", found[0])

	def test_fixture_optional_key_script_default_mismatch_fails(self) -> None:
		text = _fixture("          MERGE_LEASE_HOLD_LABELS: ${{ vars.MERGE_LEASE_HOLD_LABELS || 'a' }}")
		found = violations({"f.yml": text}, self.defaults, {"MERGE_LEASE_HOLD_LABELS": "b"})
		self.assertEqual(len(found), 1, found)
		self.assertIn("default 'a' differs", found[0])

	def test_fixture_optional_key_sites_disagree_fails(self) -> None:
		first = _fixture("          MERGE_LEASE_HOLD_LABELS: ${{ vars.MERGE_LEASE_HOLD_LABELS || 'a' }}")
		second = _fixture("          MERGE_LEASE_HOLD_LABELS: ${{ vars.MERGE_LEASE_HOLD_LABELS || 'b' }}")
		found = violations({"f.yml": first, "g.yml": second}, self.defaults, {})
		self.assertEqual(len(found), 1, found)
		self.assertIn("workflow defaults disagree", found[0])

	def test_optional_script_defaults_skip_nested_expansion(self) -> None:
		self.assertEqual(optional_script_defaults("${MERGE_LEASE_HOLD_LABELS-x,y}"), {"MERGE_LEASE_HOLD_LABELS": "x,y"})
		self.assertEqual(optional_script_defaults("${MERGE_LEASE_HOLD_LABELS:-${OTHER}}"), {})
		self.assertEqual(optional_script_defaults(""), {})

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

	def _read_only_subset(self, extra: tuple[str, ...] = ()) -> str:
		keys = ("MERGE_LEASE_ENABLED", "MERGE_LEASE_MAX_HOLDERS") + extra
		return "\n".join(f"          {k}: ${{{{ vars.{k} || '{self.defaults[k]}' }}}}" for k in keys)

	def test_fixture_read_only_step_subset_passes(self) -> None:
		text = _fixture(self._read_only_subset(), step_name="Single-issue security pass")
		self.assertEqual(violations({"review_autofix.yml": text}, self.defaults), [])

	def test_fixture_read_only_subset_outside_allowlist_fails(self) -> None:
		text = _fixture(self._read_only_subset(), step_name="Some other step")
		found = violations({"review_autofix.yml": text}, self.defaults)
		self.assertEqual(len(found), 1, found)
		self.assertIn("missing MERGE_LEASE_TTL_MINUTES", found[0])

	def test_fixture_read_only_step_with_extra_key_fails(self) -> None:
		text = _fixture(self._read_only_subset(extra=("MERGE_LEASE_IDLE_MINUTES",)), step_name="Single-issue security pass")
		found = violations({"review_autofix.yml": text}, self.defaults)
		self.assertEqual(len(found), 1, found)
		self.assertIn("missing MERGE_LEASE_TTL_MINUTES", found[0])

	def test_fixture_read_only_step_missing_listed_key_fails(self) -> None:
		text = _fixture(
			"          MERGE_LEASE_ENABLED: ${{ vars.MERGE_LEASE_ENABLED || 'true' }}",
			step_name="Single-issue security pass",
		)
		found = violations({"review_autofix.yml": text}, self.defaults)
		self.assertEqual(len(found), 1, found)
		self.assertIn("passes some merge-lease settings but is missing MERGE_LEASE_MAX_HOLDERS", found[0])
		self.assertNotIn("MERGE_LEASE_TTL_MINUTES", found[0])

	def test_fixture_read_only_step_missing_both_listed_keys_fails(self) -> None:
		text = _fixture("          OTHER: x", step_name="Single-issue security pass")
		found = violations({"review_autofix.yml": text}, self.defaults)
		self.assertEqual(len(found), 1, found)
		self.assertIn("is a listed read-only lease step but is missing MERGE_LEASE_ENABLED, MERGE_LEASE_MAX_HOLDERS", found[0])

	def test_read_only_steps_exist_with_env(self) -> None:
		# A renamed reader step, or one whose env block is gone, would no
		# longer be checked at all; keep each READ_ONLY_LEASE_STEPS entry live.
		files = _repo_workflow_files()
		seen = set()
		for name, text in files.items():
			if "MERGE_LEASE_" not in text:
				continue
			for wf, _job, step_name, _env, _effective in collect_env_blocks(name, text):
				seen.add((wf, step_name))
		self.assertEqual(sorted(set(READ_ONLY_LEASE_STEPS) - seen), [])

	def test_fixture_read_only_step_wrong_default_fails(self) -> None:
		text = _fixture(
			"          MERGE_LEASE_ENABLED: ${{ vars.MERGE_LEASE_ENABLED || 'true' }}\n"
			"          MERGE_LEASE_MAX_HOLDERS: ${{ vars.MERGE_LEASE_MAX_HOLDERS || '3' }}",
			step_name="Single-issue security pass",
		)
		found = violations({"review_autofix.yml": text}, self.defaults)
		self.assertEqual(len(found), 1, found)
		self.assertIn("MERGE_LEASE_MAX_HOLDERS default '3' differs", found[0])

	def test_fixture_partial_job_env_fails(self) -> None:
		job_env = "    env:\n" + self._full(drop=("MERGE_LEASE_ENABLED",)).replace("          ", "      ") + "\n"
		text = _fixture("          OTHER: x", job_env=job_env)
		found = violations({"f.yml": text}, self.defaults)
		self.assertEqual(len(found), 1, found)
		self.assertIn("missing MERGE_LEASE_ENABLED", found[0])


if __name__ == "__main__":
	unittest.main()
