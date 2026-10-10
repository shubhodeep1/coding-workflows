#!/usr/bin/env python3
"""Tests for ``gh_api_retry`` and the retry-rule fixes in scripts/gh_helpers.sh.

Issue #5873 (re-issued as #6634): when the ``GH_PAT`` budget ran out on
2026-10-01, hot-path calls failed on their first error and the retry helpers
waited on the wrong bucket, ignored ``retry-after``, slept after their last
attempt and retried POST creates. These tests drive the helpers through a
fake ``gh`` whose per-attempt stdout, stderr and exit code come from files,
with ``sleep`` replaced by a function that records its argument.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
GH_HELPERS = REPO_ROOT / "scripts" / "gh_helpers.sh"
RESOLVER = REPO_ROOT / "scripts" / "resolve_integration_ref.sh"

# Attempt N is served from $FAKE_GH_PLAN/N.{stdout,stderr,rc}; the highest
# planned attempt repeats when the plan runs out. Rate-limit probes do not
# consume an attempt.
FAKE_GH = r"""#!/usr/bin/env bash
if [ "$1" = "api" ] && [ "$2" = "rate_limit" ]; then
	printf '%s\n' "rate_limit $*" >> "${FAKE_GH_PLAN}/probes.log"
	printf '%s\n' "${FAKE_RL_RESET:-}"
	exit 0
fi
if [ "$1" = "api" ] && [ "$2" = "-i" ] && [ "$3" = "/rate_limit" ]; then
	printf '%s\n' "rate_limit $*" >> "${FAKE_GH_PLAN}/probes.log"
	printf 'HTTP/2.0 200 OK\r\nx-ratelimit-reset: 0\r\n\r\n%s' "${FAKE_RL_JSON:-{\}}"
	exit 0
fi
count_file="${FAKE_GH_PLAN}/count"
n=$(( $(cat "${count_file}" 2>/dev/null || echo 0) + 1 ))
echo "${n}" > "${count_file}"
printf '%s\n' "$*" >> "${FAKE_GH_PLAN}/calls.log"
step="${n}"
while [ "${step}" -gt 1 ] && [ ! -e "${FAKE_GH_PLAN}/${step}.rc" ]; do
	step=$(( step - 1 ))
done
cat "${FAKE_GH_PLAN}/${step}.stdout" 2>/dev/null || true
cat "${FAKE_GH_PLAN}/${step}.stderr" >&2 2>/dev/null || true
exit "$(cat "${FAKE_GH_PLAN}/${step}.rc")"
"""

OK_BODY = '{"number": 6634, "title": "real"}'
def _now() -> int:
	# Read the clock per test: a module-level value goes stale when earlier
	# test modules run for minutes, and short reset windows end in the past.
	return int(time.time())


def _headers(status: str, extra: dict[str, str] | None = None) -> str:
	lines = [f"HTTP/2.0 {status}"]
	for name, value in (extra or {}).items():
		lines.append(f"{name}: {value}")
	return "\r\n".join(lines) + "\r\n\r\n"


def _ok(body: str = OK_BODY, extra: dict[str, str] | None = None) -> tuple[str, str, int]:
	return (_headers("200 OK", extra) + body, "", 0)


def _primary(reset: int, resource: str = "core") -> tuple[str, str, int]:
	return (
		_headers("403 Forbidden", {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset), "x-ratelimit-resource": resource})
		+ '{"message":"API rate limit exceeded BODY-MARKER-RL"}',
		"gh: API rate limit exceeded for user ID 1. (HTTP 403)\n",
		1,
	)


def _bad_gateway() -> tuple[str, str, int]:
	return (_headers("502 Bad Gateway") + '{"message":"BODY-MARKER-502"}', "gh: HTTP 502: Bad Gateway\n", 1)


def _not_found() -> tuple[str, str, int]:
	return (_headers("404 Not Found") + '{"message":"BODY-MARKER-404"}', "gh: Not Found (HTTP 404)\n", 1)


def _run(tmp_path: Path, plan: list[tuple[str, str, int]], command: str, env_extra: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess, Path]:
	plan_dir = tmp_path / "plan"
	plan_dir.mkdir(exist_ok=True)
	for index, (out, err, rc) in enumerate(plan, start=1):
		(plan_dir / f"{index}.stdout").write_text(out, encoding="utf-8")
		(plan_dir / f"{index}.stderr").write_text(err, encoding="utf-8")
		(plan_dir / f"{index}.rc").write_text(str(rc), encoding="utf-8")
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	fake = bin_dir / "gh"
	fake.write_text(FAKE_GH, encoding="utf-8")
	fake.chmod(0o755)
	env = {k: v for k, v in os.environ.items() if not k.startswith(("TG_", "GH_RETRY", "GH_API_RETRY"))}
	env.update({
		"PATH": f"{bin_dir}:{env.get('PATH', '')}",
		"FAKE_GH_PLAN": str(plan_dir),
		"TMPDIR": str(tmp_path),
		"GH_RATE_LIMIT_BREAKER_FILE": str(tmp_path / "breaker"),
		"PYTHONDONTWRITEBYTECODE": "1",
	})
	env.update(env_extra or {})
	script = textwrap.dedent(f"""\
		set -uo pipefail
		source '{GH_HELPERS}'
		sleep() {{ printf '%s\\n' "$1" >> '{tmp_path}/sleeps'; }}
		""") + command
	result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, cwd=tmp_path)
	return result, plan_dir


def _calls(plan_dir: Path) -> list[str]:
	path = plan_dir / "calls.log"
	return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _sleeps(tmp_path: Path) -> list[int]:
	path = tmp_path / "sleeps"
	return [int(x) for x in path.read_text(encoding="utf-8").split()] if path.exists() else []


def test_failed_attempt_body_never_reaches_stdout(tmp_path: Path) -> None:
	result, plan = _run(tmp_path, [_bad_gateway(), _ok()], 'out="$(gh_api_retry repos/o/r/issues/1)"; rc=$?; printf "%s" "${out}"; exit "${rc}"')
	assert result.returncode == 0, result.stderr
	assert result.stdout == OK_BODY
	assert "BODY-MARKER" not in result.stdout
	assert "GH_API_RETRY outcome=retry kind=transient" in result.stderr
	assert _calls(plan)[0].startswith("api -i repos/o/r/issues/1")


def test_retry_to_file_keeps_only_successful_body_and_truncates_on_failure(tmp_path: Path) -> None:
	fail = (_not_found()[0], "gh: HTTP 502: Bad Gateway\n", 1)
	ok_out = "SUCCESS-BODY"
	result, _ = _run(
		tmp_path,
		[fail, (ok_out, "", 0)],
		f"gh_retry_to_file '{tmp_path}/out.json' gh api repos/o/r; echo rc=$?",
	)
	assert "rc=0" in result.stdout
	assert (tmp_path / "out.json").read_text() == ok_out
	(tmp_path / "plan" / "count").unlink()
	for name in ("1", "2"):
		for suffix in ("stdout", "stderr", "rc"):
			(tmp_path / "plan" / f"{name}.{suffix}").unlink()
	result, _ = _run(tmp_path, [fail], f"gh_retry_to_file '{tmp_path}/out.json' gh api repos/o/r; echo rc=$?", {"GH_RETRY_MAX_ATTEMPTS": "2"})
	assert "rc=1" in result.stdout
	assert (tmp_path / "out.json").read_text() == ""


def test_primary_limit_waits_for_matching_bucket(tmp_path: Path) -> None:
	reset = _now() + 30
	result, _ = _run(tmp_path, [_primary(reset, "graphql"), _ok()], "gh_api_retry graphql -f query='{ viewer { login } }' >/dev/null; echo rc=$?")
	assert "rc=0" in result.stdout, result.stderr
	sleeps = _sleeps(tmp_path)
	assert len(sleeps) == 1 and 25 <= sleeps[0] <= 32, sleeps
	assert "bucket=graphql" in result.stderr
	assert "graphql" in (tmp_path / "breaker").read_text()


def test_rate_limit_wait_reads_bucket_reset_from_rate_limit_body(tmp_path: Path) -> None:
	reset = _now() + 40
	json_body = f'{{"resources":{{"core":{{"reset":{_now() + 400}}},"graphql":{{"reset":{reset}}}}}}}'
	result, _ = _run(tmp_path, [], "_gh_rate_limit_wait graphql", {"FAKE_RL_JSON": json_body})
	if "jq" not in subprocess.run(["bash", "-c", "command -v jq || true"], capture_output=True, text=True).stdout:
		pytest.skip("jq not available")
	sleeps = _sleeps(tmp_path)
	assert len(sleeps) == 1 and 35 <= sleeps[0] <= 42, sleeps


def test_retry_after_is_honoured(tmp_path: Path) -> None:
	secondary = (_headers("403 Forbidden", {"retry-after": "7", "x-ratelimit-remaining": "4000"}) + "{}", "gh: secondary rate limit (HTTP 403)\n", 1)
	result, _ = _run(tmp_path, [secondary, _ok()], "gh_api_retry repos/o/r >/dev/null; echo rc=$?")
	assert "rc=0" in result.stdout
	assert _sleeps(tmp_path) == [7]


def test_secondary_without_retry_after_waits_sixty(tmp_path: Path) -> None:
	secondary = (_headers("403 Forbidden", {"x-ratelimit-remaining": "4000"}) + "{}", "gh: You have exceeded a secondary rate limit (HTTP 403)\n", 1)
	result, _ = _run(tmp_path, [secondary, _ok()], "gh_api_retry repos/o/r >/dev/null; echo rc=$?")
	assert "rc=0" in result.stdout
	assert _sleeps(tmp_path) == [60]


@pytest.mark.parametrize(
	"command",
	[
		"gh_api_retry -X POST repos/o/r/issues/1/comments -f body=x",
		"gh_api_retry repos/o/r/issues/1/comments -f body=x",
		"gh_api_retry --method=post repos/o/r/issues -F title=x",
		"gh_api_retry graphql -f 'query=mutation { x }'",
		"gh_retry gh api -X POST repos/o/r/issues/1/comments -f body=x",
		"gh_retry gh issue comment 1 --body x",
		"gh_retry gh workflow run review.yml",
		"gh_retry gh pr create --title x",
		"gh_api_json_to_file out.json gh api -X POST repos/o/r/dispatches",
	],
)
def test_post_create_without_idempotent_is_single_attempt(tmp_path: Path, command: str) -> None:
	result, plan = _run(tmp_path, [_bad_gateway(), _ok()], f"{command} >/dev/null; echo rc=$?")
	assert len(_calls(plan)) == 1, (command, _calls(plan), result.stderr)
	assert "rc=1" in result.stdout
	assert _sleeps(tmp_path) == []


@pytest.mark.parametrize(
	"command,env",
	[
		("gh_api_retry --idempotent -X POST repos/o/r/issues/1/labels -f labels[]=x", {}),
		("gh_api_retry -X POST repos/o/r/issues/1/labels -f labels[]=x", {"GH_RETRY_IDEMPOTENT": "true"}),
		("gh_retry --idempotent gh api -X POST repos/o/r/issues/1/labels -f labels[]=x", {}),
		("gh_retry gh api -X POST repos/o/r/issues/1/labels -f labels[]=x", {"GH_RETRY_IDEMPOTENT": "true"}),
		("gh_api_retry -X PATCH repos/o/r/issues/1 -f state=closed", {}),
		("gh_api_retry graphql -f 'query={ viewer { login } }'", {}),
		("gh_api_retry --method=GET repos/o/r/pulls -f state=open", {}),
		("gh_retry gh issue edit 1 --add-label x", {}),
	],
)
def test_idempotent_or_safe_calls_are_retried(tmp_path: Path, command: str, env: dict[str, str]) -> None:
	result, plan = _run(tmp_path, [_bad_gateway(), _ok()], f"{command} >/dev/null; echo rc=$?", env)
	assert "rc=0" in result.stdout, (command, result.stderr)
	assert len(_calls(plan)) == 2, command


def test_graphql_documents_in_files_are_classified(tmp_path: Path) -> None:
	"""A mutation in --input JSON is not retried; a query file read is."""
	(tmp_path / "mut.json").write_text('{"query": "mutation { addLabel }"}', encoding="utf-8")
	result, plan = _run(tmp_path, [_bad_gateway(), _ok()], "gh_api_retry graphql --input mut.json >/dev/null; echo rc=$?")
	assert "rc=1" in result.stdout and len(_calls(plan)) == 1
	(tmp_path / "plan" / "count").unlink()
	(tmp_path / "plan" / "calls.log").unlink()
	(tmp_path / "read.graphql").write_text("# comment\nquery { viewer { login } }\n", encoding="utf-8")
	result, plan = _run(tmp_path, [_bad_gateway(), _ok()], "gh_api_retry graphql -F query=@read.graphql >/dev/null; echo rc=$?")
	assert "rc=0" in result.stdout and len(_calls(plan)) == 2
	assert _gh_bash(tmp_path, "_gh_api_args_unsafe_post graphql -F query=@missing.graphql; echo rc=$?") == "rc=0"


@pytest.mark.parametrize(
	"doc,is_mutation",
	[
		("query { a }", False),
		("mutation { a }", True),
		("# leading comment\n  mutation X { a }", True),
		("query A { a } mutation B { b }", True),
		("query { mutationCount }", False),
		("# mutation in a comment\nquery { a }", False),
	],
)
def test_graphql_mutation_detection_matches_python_twin(tmp_path: Path, doc: str, is_mutation: bool) -> None:
	"""A later mutation that operationName can select is not retried (bash and Python agree)."""
	import sys
	sys.path.insert(0, str(REPO_ROOT / "scripts"))
	import gh_api_retry as gar
	(tmp_path / "doc.graphql").write_text(doc, encoding="utf-8")
	got = _gh_bash(tmp_path, '_gh_graphql_doc_is_mutation "$(cat doc.graphql)"; echo rc=$?')
	assert got == ("rc=0" if is_mutation else "rc=1"), doc
	assert gar.graphql_doc_is_mutation(doc) is is_mutation


def _gh_bash(tmp_path: Path, command: str) -> str:
	script = f"set -uo pipefail; source '{GH_HELPERS}'; cd '{tmp_path}'; {command}"
	return subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout.strip()


def test_legacy_rate_limit_feeds_optional_breaker(tmp_path: Path) -> None:
	"""A limit seen by gh_retry records a bucket line that --optional honours."""
	limited = ("", "gh: API rate limit exceeded for user ID 1. (HTTP 403)\n", 1)
	result, plan = _run(
		tmp_path,
		[limited, _ok()],
		"gh_retry gh api repos/o/r >/dev/null 2>&1; gh_api_retry --optional repos/o/r >/dev/null; echo rc=$?",
		{"GH_RETRY_MAX_ATTEMPTS": "1"},
	)
	assert "rc=75" in result.stdout, result.stderr
	assert len(_calls(plan)) == 1
	assert any(line.startswith("core ") for line in (tmp_path / "breaker").read_text().splitlines())


def test_no_sleep_after_last_attempt(tmp_path: Path) -> None:
	env = {"GH_RETRY_MAX_ATTEMPTS": "3"}
	result, plan = _run(tmp_path, [_bad_gateway()], "gh_api_retry repos/o/r >/dev/null; echo rc=$?", env)
	assert "rc=1" in result.stdout
	assert len(_calls(plan)) == 3
	assert len(_sleeps(tmp_path)) == 2
	assert all(s <= 120 for s in _sleeps(tmp_path))


def test_json_to_file_no_sleep_after_last_attempt(tmp_path: Path) -> None:
	result, plan = _run(tmp_path, [("", "gh: HTTP 502: Bad Gateway\n", 1)], f"gh_api_json_to_file '{tmp_path}/o.json' gh api repos/o/r; echo rc=$?", {"GH_RETRY_MAX_ATTEMPTS": "2"})
	assert "rc=1" in result.stdout
	assert len(_calls(plan)) == 2
	assert len(_sleeps(tmp_path)) == 1


def test_curl_no_sleep_after_last_attempt(tmp_path: Path) -> None:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	curl = bin_dir / "curl"
	curl.write_text("#!/usr/bin/env bash\necho x >> \"${FAKE_GH_PLAN}/curl_calls\"\nprintf 502\n", encoding="utf-8")
	curl.chmod(0o755)
	result, plan = _run(tmp_path, [], "curl_gh_api https://api.github.com/x >/dev/null; echo rc=$?", {"GH_RETRY_MAX_ATTEMPTS": "2"})
	assert "rc=1" in result.stdout
	assert len((plan / "curl_calls").read_text().split()) == 2
	assert len(_sleeps(tmp_path)) == 1


def test_reset_beyond_cap_gives_up_with_75_and_no_sleep(tmp_path: Path) -> None:
	result, _ = _run(tmp_path, [_primary(_now() + 3600)], "gh_api_retry repos/o/r >/dev/null; echo rc=$?")
	assert "rc=75" in result.stdout
	assert _sleeps(tmp_path) == []
	assert "outcome=gave_up kind=primary" in result.stderr
	lines = (tmp_path / "breaker").read_text().split("\n")
	assert any(line.startswith("core ") for line in lines)


def test_optional_call_skips_while_breaker_active(tmp_path: Path) -> None:
	(tmp_path / "breaker").write_text(f"core {_now() + 600}\ngarbage line\n", encoding="utf-8")
	result, plan = _run(tmp_path, [_ok()], "gh_api_retry --optional repos/o/r >/dev/null; echo rc=$?; gh_api_retry --optional graphql -f 'query={ a }' >/dev/null; echo rc2=$?")
	assert "rc=75" in result.stdout
	assert "rc2=0" in result.stdout
	assert len(_calls(plan)) == 1


def test_low_budget_records_breaker_line(tmp_path: Path) -> None:
	reset = _now() + 100
	result, _ = _run(tmp_path, [_ok(extra={"x-ratelimit-remaining": "5", "x-ratelimit-reset": str(reset)})], "gh_api_retry repos/o/r >/dev/null; echo rc=$?")
	assert "rc=0" in result.stdout
	assert f"core {reset} low" in (tmp_path / "breaker").read_text()


@pytest.mark.parametrize("plan_entry", [_not_found(), (_headers("422 Unprocessable Entity") + "{}", "gh: Validation Failed (HTTP 422)\n", 1), (_headers("403 Forbidden", {"x-ratelimit-remaining": "4000"}) + "{}", "gh: Resource not accessible by integration (HTTP 403)\n", 1)])
def test_permanent_failures_return_two_after_one_attempt(tmp_path: Path, plan_entry: tuple[str, str, int]) -> None:
	result, plan = _run(tmp_path, [plan_entry, _ok()], "gh_api_retry repos/o/r >/dev/null; echo rc=$?")
	assert "rc=2" in result.stdout
	assert len(_calls(plan)) == 1
	assert "BODY-MARKER" not in result.stdout


def test_last_status_is_visible_outside_subshell(tmp_path: Path) -> None:
	result, _ = _run(tmp_path, [_not_found()], 'gh_api_retry repos/o/r >/dev/null 2>&1; echo "rc=$? status=${GH_API_RETRY_LAST_STATUS} kind=${GH_API_RETRY_LAST_KIND}"')
	assert "rc=2 status=404 kind=permanent" in result.stdout


def test_paginate_runs_without_include_and_probes_bucket_reset(tmp_path: Path) -> None:
	limited = ("", "gh: API rate limit exceeded for user ID 1. (HTTP 403)\n", 1)
	result, plan = _run(tmp_path, [limited, ("[1]", "", 0)], "gh_api_retry --paginate search/issues -f q=x --method GET; echo; echo rc=$?", {"FAKE_RL_RESET": str(_now() + 20)})
	assert "rc=0" in result.stdout
	assert "[1]" in result.stdout
	assert all(" -i " not in f" {c} " for c in _calls(plan))
	assert ".resources.search.reset" in (plan / "probes.log").read_text()
	sleeps = _sleeps(tmp_path)
	assert len(sleeps) == 1 and 15 <= sleeps[0] <= 22


def test_resolver_exits_75_when_rate_limited(tmp_path: Path) -> None:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	fake = bin_dir / "gh"
	fake.write_text(FAKE_GH, encoding="utf-8")
	fake.chmod(0o755)
	plan_dir = tmp_path / "plan"
	plan_dir.mkdir()
	out, err, rc = _primary(_now() + 7200)
	(plan_dir / "1.stdout").write_text(out)
	(plan_dir / "1.stderr").write_text(err)
	(plan_dir / "1.rc").write_text(str(rc))
	env = {k: v for k, v in os.environ.items() if not k.startswith("TG_")}
	env.update({
		"PATH": f"{bin_dir}:{env.get('PATH', '')}",
		"FAKE_GH_PLAN": str(plan_dir),
		"TMPDIR": str(tmp_path),
		"GH_RATE_LIMIT_BREAKER_FILE": str(tmp_path / "breaker"),
		"REPO": "o/r",
		"ISSUE": "1",
		"GH_TOKEN": "x",
	})
	result = subprocess.run(["bash", str(RESOLVER)], env=env, capture_output=True, text=True)
	assert result.returncode == 75, (result.stdout, result.stderr)
	assert result.stdout == ""
