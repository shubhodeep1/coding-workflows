#!/usr/bin/env python3
"""Contract tests for scripts/propagate_consumer_secrets.sh and its workflow.

The script copies the library's consumer-facing Actions secrets into
consumer repositories through ``gh secret set``. These tests drive it with a
fake ``gh`` on PATH that records every call and the SHA-256 of the value it
received on stdin, so the assertions cover routing (which repo, which
secret), refusal of unregistered targets, empty-value skipping, per-repo
fail-open with a red exit, and that no secret value ever reaches stdout or
stderr. The workflow contract test pins the trigger, the secret wiring and
the CI registration.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "propagate_consumer_secrets.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "propagate-consumer-secrets.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

FAKE_GH = r"""#!/usr/bin/env bash
set -euo pipefail
# Records: set|<repo>|<name>|<sha256 of stdin>  and  list|<repo>
if [ "${1:-}" = "secret" ] && [ "${2:-}" = "set" ]; then
  name="$3"; repo="$5"
  digest="$(sha256sum | awk '{print $1}')"
  attempts="$(grep -c "^set|${repo}|${name}|" "${MOCK_GH_LOG}" 2>/dev/null || true)"
  printf 'set|%s|%s|%s\n' "${repo}" "${name}" "${digest}" >> "${MOCK_GH_LOG}"
  case " ${MOCK_GH_FAIL_REPOS:-} " in
    *" ${repo} "*) echo "gh: HTTP 403 Resource not accessible by personal access token" >&2; exit 1 ;;
  esac
  if [ "${MOCK_GH_FAIL_FIRST_ATTEMPT:-}" = "true" ] && [ "${attempts:-0}" -eq 0 ]; then
    echo "gh: upstream failure (HTTP 502)" >&2; exit 1
  fi
  exit 0
fi
if [ "${1:-}" = "secret" ] && [ "${2:-}" = "list" ]; then
  repo="$4"
  printf 'list|%s\n' "${repo}" >> "${MOCK_GH_LOG}"
  case " ${MOCK_GH_LIST_FAIL_REPOS:-} " in
    *" ${repo} "*) echo "gh: HTTP 500" >&2; exit 1 ;;
  esac
  case " ${MOCK_GH_LIST_EMPTY_REPOS:-} " in
    *" ${repo} "*) exit 0 ;;
  esac
  grep -E "^set\|${repo}\|" "${MOCK_GH_LOG}" | cut -d'|' -f3 | sort -u
  exit 0
fi
echo "unexpected gh call: $*" >&2
exit 2
"""

SECRET_NAMES = ("CHECK_TRIAGE_ISSUES_TOKEN", "GH_PAT", "OPENROUTER_API_KEY", "TG_BOT_SECRET")

# Fixture values: distinctive strings so the no-leak test can search for them.
FIXTURE_VALUES = {
	"CHECK_TRIAGE_ISSUES_TOKEN": "fixture-triage-7f3a",
	"GH_PAT": "fixture-pat-9c1e",
	"OPENROUTER_API_KEY": "fixture-router-2b8d",
	"TG_BOT_SECRET": "fixture-bot-5e4f",
}


def _run(
	*,
	registry: list[str],
	targets: str = "",
	values: dict[str, str] | None = None,
	names: str | None = None,
	fail_repos: str = "",
	list_empty_repos: str = "",
	list_fail_repos: str = "",
	fail_first_attempt: bool = False,
	retry_attempts: str = "1",
	token: str = "fixture-library-token",
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
	with tempfile.TemporaryDirectory(prefix="propagate-consumer-") as tmp_name:
		tmp = Path(tmp_name)
		bin_dir = tmp / "bin"
		bin_dir.mkdir()
		gh = bin_dir / "gh"
		gh.write_text(FAKE_GH, encoding="utf-8")
		gh.chmod(0o755)
		(bin_dir / "sleep").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
		(bin_dir / "sleep").chmod(0o755)
		registry_file = tmp / "consumer_repos.json"
		registry_file.write_text(json.dumps(registry), encoding="utf-8")
		log = tmp / "gh.log"
		env = {
			name: value for name, value in os.environ.items()
			if name not in ("GH_TOKEN", "BASH_ENV") and name not in SECRET_NAMES
		}
		env.update({
			"PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
			"CONSUMER_REPOS_FILE": str(registry_file),
			"PROPAGATE_TARGETS": targets,
			"MOCK_GH_LOG": str(log),
			"MOCK_GH_FAIL_REPOS": fail_repos,
			"MOCK_GH_LIST_EMPTY_REPOS": list_empty_repos,
			"MOCK_GH_LIST_FAIL_REPOS": list_fail_repos,
			"MOCK_GH_FAIL_FIRST_ATTEMPT": "true" if fail_first_attempt else "",
			"GH_RETRY_MAX_ATTEMPTS": retry_attempts,
		})
		if token:
			env["GH_TOKEN"] = token
		if names is not None:
			env["CONSUMER_SECRET_NAMES"] = names
		for name, value in (values or {}).items():
			env[name] = value
		proc = subprocess.run(
			["bash", "--noprofile", "--norc", str(SCRIPT)],
			cwd=REPO_ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
		)
		calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
		return proc, calls


def _sha(value: str) -> str:
	return hashlib.sha256(value.encode("utf-8")).hexdigest()


class PropagateConsumerSecretsScriptTests(unittest.TestCase):
	def test_sets_every_secret_on_every_target_and_verifies(self) -> None:
		proc, calls = _run(registry=["o/a", "o/b"], targets="o/a o/b", values=FIXTURE_VALUES)
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		expected_sets = {
			f"set|{repo}|{name}|{_sha(value)}"
			for repo in ("o/a", "o/b") for name, value in FIXTURE_VALUES.items()
		}
		self.assertEqual(expected_sets, {c for c in calls if c.startswith("set|")})
		self.assertEqual(["list|o/a", "list|o/b"], [c for c in calls if c.startswith("list|")])
		for repo in ("o/a", "o/b"):
			for name in FIXTURE_VALUES:
				self.assertIn(f"CONSUMER_SECRETS_PROPAGATE repo={repo} secret={name} status=set", proc.stdout)
		self.assertIn("CONSUMER_SECRETS_PROPAGATE summary targets=2 set=8 skipped=0 failed=0", proc.stdout)

	def test_empty_targets_means_every_registry_entry_in_order(self) -> None:
		proc, calls = _run(registry=["o/c", "o/a", "o/b"], targets="", values={"GH_PAT": "x"}, names="GH_PAT")
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertEqual(
			["set|o/c|GH_PAT|" + _sha("x"), "set|o/a|GH_PAT|" + _sha("x"), "set|o/b|GH_PAT|" + _sha("x")],
			[c for c in calls if c.startswith("set|")],
		)

	def test_unregistered_target_is_refused_without_any_write(self) -> None:
		proc, calls = _run(registry=["o/a"], targets="o/evil o/a", values={"GH_PAT": "x"}, names="GH_PAT")
		self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
		self.assertIn("CONSUMER_SECRETS_PROPAGATE repo=o/evil status=skipped_unregistered", proc.stdout)
		self.assertNotIn("set|o/evil|", "\n".join(calls))
		self.assertIn("set|o/a|GH_PAT|" + _sha("x"), calls)
		self.assertIn("summary targets=1 set=1 skipped=0 failed=1", proc.stdout)

	def test_invalid_slug_is_refused(self) -> None:
		proc, calls = _run(registry=["o/a", "../x"], targets="../x", values={"GH_PAT": "x"}, names="GH_PAT")
		self.assertEqual(proc.returncode, 1)
		self.assertIn("repo=../x status=skipped_invalid_slug", proc.stdout)
		self.assertEqual([], calls)

	def test_empty_value_is_skipped_with_a_warning(self) -> None:
		values = dict(FIXTURE_VALUES)
		values["TG_BOT_SECRET"] = ""
		proc, calls = _run(registry=["o/a"], values=values)
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertIn("::warning::propagate-consumer-secrets: TG_BOT_SECRET is empty", proc.stdout)
		self.assertIn("repo=o/a secret=TG_BOT_SECRET status=skipped_empty", proc.stdout)
		self.assertNotIn("|TG_BOT_SECRET|", "\n".join(calls))
		self.assertIn("summary targets=1 set=3 skipped=1 failed=0", proc.stdout)

	def test_failure_on_one_repo_continues_and_exits_red(self) -> None:
		proc, calls = _run(registry=["o/a", "o/b"], values={"GH_PAT": "x"}, names="GH_PAT", fail_repos="o/a")
		self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
		self.assertIn("repo=o/a secret=GH_PAT status=failed", proc.stdout)
		self.assertIn("repo=o/b secret=GH_PAT status=set", proc.stdout)
		self.assertIn("set|o/b|GH_PAT|" + _sha("x"), calls)
		self.assertIn("summary targets=2 set=1 skipped=0 failed=1", proc.stdout)
		self.assertIn("::error::propagate-consumer-secrets: 1 secret write(s) failed", proc.stdout)

	def test_verification_miss_is_reported(self) -> None:
		proc, _ = _run(registry=["o/a"], values={"GH_PAT": "x"}, names="GH_PAT", list_empty_repos="o/a")
		self.assertEqual(proc.returncode, 1)
		self.assertIn("repo=o/a secret=GH_PAT status=verify_missing", proc.stdout)

	def test_listing_failure_counts_as_a_failure(self) -> None:
		"""A write that cannot be verified must not leave the run green."""
		proc, calls = _run(registry=["o/a"], values={"GH_PAT": "x"}, names="GH_PAT", list_fail_repos="o/a")
		self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
		self.assertIn("repo=o/a secret=GH_PAT status=set", proc.stdout)
		self.assertIn("repo=o/a status=verify_list_failed", proc.stdout)
		self.assertIn("summary targets=1 set=1 skipped=0 failed=1", proc.stdout)
		self.assertIn("list|o/a", calls)

	def test_retry_pipes_the_value_again_on_every_attempt(self) -> None:
		"""gh_retry re-invokes the set helper, so attempt 2 must receive the
		full value on stdin, not an already-consumed pipe."""
		proc, calls = _run(
			registry=["o/a"], values={"GH_PAT": "retry-me"}, names="GH_PAT",
			fail_first_attempt=True, retry_attempts="2",
		)
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		sets = [c for c in calls if c.startswith("set|o/a|GH_PAT|")]
		self.assertEqual(sets, ["set|o/a|GH_PAT|" + _sha("retry-me")] * 2, calls)
		self.assertIn("repo=o/a secret=GH_PAT status=set", proc.stdout)

	def test_secret_values_never_reach_stdout_or_stderr(self) -> None:
		proc, _ = _run(registry=["o/a", "o/b"], values=FIXTURE_VALUES, fail_repos="o/b")
		output = proc.stdout + proc.stderr
		for value in FIXTURE_VALUES.values():
			self.assertNotIn(value, output)
		self.assertNotIn("fixture-library-token", output)

	def test_missing_token_fails_before_any_call(self) -> None:
		proc, calls = _run(registry=["o/a"], values=FIXTURE_VALUES, token="")
		self.assertEqual(proc.returncode, 1)
		self.assertIn("GH_TOKEN is empty", proc.stdout)
		self.assertEqual([], calls)

	def test_invalid_registry_fails_closed(self) -> None:
		with tempfile.TemporaryDirectory() as tmp_name:
			registry_file = Path(tmp_name) / "consumer_repos.json"
			registry_file.write_text('{"not": "an array"}', encoding="utf-8")
			env = {k: v for k, v in os.environ.items() if k != "BASH_ENV"}
			env.update({"GH_TOKEN": "t", "CONSUMER_REPOS_FILE": str(registry_file)})
			proc = subprocess.run(["bash", str(SCRIPT)], cwd=REPO_ROOT, env=env, capture_output=True, text=True)
		self.assertEqual(proc.returncode, 1)
		self.assertIn("is not a JSON array of owner/repo strings", proc.stdout)


class PropagateConsumerSecretsWorkflowContractTests(unittest.TestCase):
	def setUp(self) -> None:
		self.workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
		self.text = WORKFLOW.read_text(encoding="utf-8")

	def test_triggers_on_registry_change_and_dispatch(self) -> None:
		on = self.workflow.get("on") or self.workflow.get(True)
		self.assertEqual(on["push"]["branches"], ["main"])
		self.assertEqual(on["push"]["paths"], [".github/ai/consumer_repos.json"])
		self.assertIn("targets", on["workflow_dispatch"]["inputs"])

	def test_propagate_step_wires_every_secret_and_runs_the_script(self) -> None:
		job = self.workflow["jobs"]["propagate"]
		self.assertEqual(self.workflow["permissions"], {"contents": "read"})
		# No concurrency group: a replaced pending run would lose the
		# consumers its push added (review finding on PR #6709).
		self.assertNotIn("concurrency", self.workflow)
		step = next(s for s in job["steps"] if s.get("name") == "Propagate secrets")
		self.assertEqual(step["env"]["GH_TOKEN"], "${{ secrets.GH_PAT }}")
		for name in SECRET_NAMES:
			self.assertEqual(step["env"][name], "${{ secrets.%s }}" % name)
		self.assertEqual(step["env"]["PROPAGATE_TARGETS"], "${{ steps.targets.outputs.targets }}")
		self.assertIn("bash scripts/propagate_consumer_secrets.sh", step["run"])
		self.assertEqual(step["if"], "${{ steps.targets.outputs.skip != 'true' }}")

	def test_checkout_has_history_for_the_registry_diff(self) -> None:
		job = self.workflow["jobs"]["propagate"]
		checkout = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout"))
		self.assertEqual(checkout["with"]["fetch-depth"], 2)
		self.assertIs(checkout["with"]["persist-credentials"], False)
		resolve = next(s for s in job["steps"] if s.get("id") == "targets")
		# Only the push's own `before` tip is a valid diff base; HEAD~1 would
		# miss consumers added by an earlier commit of a multi-commit push.
		self.assertNotIn("HEAD~1", resolve["run"])
		self.assertIn("github.event.before", resolve["env"]["EVENT_BEFORE"])

	def test_no_secret_is_echoed_in_the_workflow(self) -> None:
		for line in self.text.splitlines():
			if "echo" in line:
				self.assertNotIn("secrets.", line)

	def test_ci_registers_this_test(self) -> None:
		self.assertIn("tests/test_propagate_consumer_secrets.py", CI_WORKFLOW.read_text(encoding="utf-8"))

	def test_script_contract_lines(self) -> None:
		script = SCRIPT.read_text(encoding="utf-8")
		self.assertIn('gh_retry set_consumer_secret "${secret_name}" "${target}"', script)
		self.assertIn('printf \'%s\' "${!1}" | gh secret set "$1" --repo "$2"', script)
		self.assertIn("status=skipped_unregistered", script)
		self.assertIn("status=verify_list_failed", script)
		self.assertNotIn("set -x", script)


if __name__ == "__main__":
	unittest.main()
