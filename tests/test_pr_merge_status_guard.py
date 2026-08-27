#!/usr/bin/env python3
"""Behaviour and wiring contract for the merged-PR commit guard (CLAUDE.md §21).

Covers the three pieces that can silently detach the mechanism:
  1. Command parsing — which Bash calls are guarded at all.
  2. The three-condition detection rule, including its self-clearing property.
  3. The fail-open contract, plus the settings.json / template-parity wiring.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
GUARD_PATH = REPO_ROOT / ".claude" / "hooks" / "pr_merge_status_guard.py"
TEMPLATE_GUARD_PATH = REPO_ROOT / "workflow-templates" / ".claude" / "hooks" / "pr_merge_status_guard.py"
SETTINGS_PATH = REPO_ROOT / ".claude" / "settings.json"
TEMPLATE_SETTINGS_PATH = REPO_ROOT / "workflow-templates" / ".claude" / "settings.json"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
TEMPLATE_CLAUDE_MD = REPO_ROOT / "workflow-templates" / "CLAUDE.md"


def _load_guard():
	spec = importlib.util.spec_from_file_location("pr_merge_status_guard", GUARD_PATH)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


guard = _load_guard()


def _git_env() -> dict[str, str]:
	env = dict(os.environ)
	for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
		env.pop(key, None)
	return env


# ──────────────────────────────────────────────────────────────────
# Command parsing
# ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
	"command",
	[
		"git commit -m 'wip'",
		'git commit -m "message with the word push in it"',
		'git commit -m "message with ; && | operators"',
		"git push -u origin feature",
		"cd /repo && git commit --amend --no-edit",
		"git add -A; git commit -m x",
		"git -C /some/repo commit -m x",
		"git -c user.name=bot commit -m x",
		"GIT_AUTHOR_NAME=bot git commit -m x",
		"/usr/bin/git push origin HEAD",
	],
)
def test_guarded_commands_are_detected(command: str) -> None:
	assert guard.git_subcommands(command) & guard.GUARDED_SUBCOMMANDS


@pytest.mark.parametrize(
	"command",
	[
		"git status",
		"git log --oneline -5",
		"git fetch origin main",
		"echo 'git commit -m x'",
		"man git commit",
		"grep -rn 'git push' scripts/",
		"ls -la",
		"",
	],
)
def test_unguarded_commands_are_ignored(command: str) -> None:
	assert not (guard.git_subcommands(command) & guard.GUARDED_SUBCOMMANDS)


def test_unbalanced_quotes_do_not_raise() -> None:
	assert guard.git_subcommands("git commit -m 'unterminated") == set()


@pytest.mark.parametrize(
	"command",
	[
		'curl -q -sS -X PUT https://api.digitalocean.com/v2/apps/id -H "Authorization: Bearer ${DIGITALOCEAN_ACCESS_TOKEN}" -d @spec.json',
		'curl -q -sS -X POST https://api.cloudflare.com/client/v4/accounts/id/workers/scripts/name --header="Authorization: Bearer ${CF_TOKEN}" --data-binary=@worker.js',
		'curl -q -sS -X PATCH https://api.digitalocean.com/v2/apps/id -d \'{"method":"-X DELETE"}\'',
		"curl -q -sS -X POST https://api.cloudflare.com/client/v4/workers -d 'prices $(USD) use `literal` markers'",
		"curl -q -sS -X POST https://api.cloudflare.com/client/v4/~health -H 'Authorization: Bearer token'",
	],
)
def test_canonical_api_writes_do_not_request_extra_confirmation(command: str) -> None:
	assert not guard._api_write_requires_confirmation(command)


@pytest.mark.parametrize(
	"command",
	[
		"curl -q -sS -X PUT https://api.digitalocean.com/v2/droplets/id -X DELETE",
		"curl -q -sS -X PUT https://api.digitalocean.com/v2/droplets/id -XDELETE",
		"curl -q -sS -X POST https://api.cloudflare.com/client/v4/workers --request DELETE",
		"curl -q -sS -X POST https://api.cloudflare.com/client/v4/workers --request=DELETE",
		"curl -q -sS -X PATCH https://api.digitalocean.com/v2/apps/id --url https://example.com/",
		"curl -q -sS -X PATCH https://api.digitalocean.com/v2/apps/id --url=https://example.com/",
		"curl -q -sS -X PUT https://api.digitalocean.com/v2/apps/id https://example.com/",
		"curl -q -sS -X PUT https://api.digitalocean.com/v2/{apps,droplets}/id",
		"curl -q -sS -X PUT https://api.digitalocean.com/v2/apps/[1-2] -d @spec.json",
		"curl -q -sS -X PUT https://api.digitalocean.com/v2/apps?id=1 -d @spec.json",
		"curl -q -sS -X PATCH https://api.digitalocean.com/v2/apps/id?fields[]=spec -d @spec.json",
		"curl -q -sS -X PUT https://api.cloudflare.com/client/v4/zones/example/* -d @zone.json",
		"curl -q -sS -X PUT https://api.digitalocean.com/v2/apps/id#frag -X DELETE",
		"curl -q -sS -X PATCH https://api.digitalocean.com/v2/apps/id#frag --url https://example.com/",
		"curl -q -sS -X POST https://api.cloudflare.com/client/v4/workers; curl -X DELETE https://api.cloudflare.com/client/v4/workers/id",
		"curl -q -sS -X PATCH https://api.digitalocean.com/v2/apps/id -d \"$(cat /tmp/body)\"",
		'curl -q -sS -X PATCH https://api.digitalocean.com/v2/apps/id -d "it\'s $(cat /tmp/body)"',
		"curl -q -sS -X PUT https://api.digitalocean.com/v2/apps/id -d $BODY",
		"curl -q -sS -X PUT https://api.digitalocean.com/v2/apps/id -d ${BODY}",
		"curl -q -sS -X PUT https://api.digitalocean.com/v2/apps/id -d {x,-X,DELETE}",
		"curl -q -sS -X PUT https://api.digitalocean.com/v2/apps/id -d /tmp/*",
		"curl -q -sS -X PUT https://api.digitalocean.com/v2/apps/id -d ~/spec.json",
		"curl -q -sS -X POST https://api.cloudflare.com/client/v4/workers -H $AUTHORIZATION",
		'curl -q -sS -X PUT https://api.digitalocean.com/v2/apps/id -d "$@"',
		'curl -q -sS -X PUT https://api.digitalocean.com/v2/apps/id -d "${BODY_PARTS[@]}"',
		'curl -q -sS -X PUT https://api.digitalocean.com/v2/apps/id -d "${BODY_PARTS[@]:1}"',
		'curl -q -sS -X PUT https://api.digitalocean.com/v2/apps/id -d "${@:1}"',
		'curl -q -sS -X PUT https://api.digitalocean.com/v2/apps/id -d "${!BODY_REF}"',
		'curl -q -sS -X PUT https://api.digitalocean.com/v2/apps/id -d "${!CURL_ARG_@}"',
		'curl -q -sS -X PUT https://api.digitalocean.com/v2/apps/id -d "${BODY@P}"',
		"curl -q -sS -X PUT https://api.digitalocean.com/v2/apps/id -d 'unterminated",
		"  curl -q -sS -X POST https://api.cloudflare.com/client/v4/workers -H 'unterminated",
		"curl -q -sS -X PUT https://api.digitalocean.com/v2/apps/id>/tmp/response",
	],
)
def test_noncanonical_api_writes_request_confirmation(command: str) -> None:
	assert guard._api_write_requires_confirmation(command)


@pytest.mark.parametrize(
	"command",
	[
		"curl -q -sS -X PUT https://api.digitalocean.com/v2/droplets/id -X DELETE",
		"curl -q -sS -X PUT https://api.digitalocean.com/v2/apps?id=1 -d @spec.json",
		"curl -q -sS -X PUT https://api.digitalocean.com/v2/apps/[1-2] -d @spec.json",
		"curl -q -sS -X PUT https://api.digitalocean.com/v2/apps/id#frag -X DELETE",
		"curl -q -sS -X PATCH https://api.digitalocean.com/v2/apps/id#frag --url https://example.com/",
		"curl -q -sS -X PUT https://api.digitalocean.com/v2/apps/id -d ${BODY}",
		"curl -q -sS -X PUT https://api.digitalocean.com/v2/apps/id -d 'unterminated",
	],
)
def test_noncanonical_api_write_emits_ask_decision(command: str, capsys) -> None:
	assert guard.evaluate({"tool_name": "Bash", "tool_input": {"command": command}}) == (0, "")
	output = json.loads(capsys.readouterr().out)
	assert output["hookSpecificOutput"]["permissionDecision"] == "ask"


# ──────────────────────────────────────────────────────────────────
# Repo slug extraction — mirrors session-start.sh's host whitelist
# ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
	"url,expected",
	[
		("https://github.com/owner/repo.git", "owner/repo"),
		("https://github.com/owner/repo", "owner/repo"),
		("git@github.com:owner/repo.git", "owner/repo"),
		("ssh://git@github.com/owner/repo.git", "owner/repo"),
		("https://x-access-token:tok@github.com/owner/repo.git", "owner/repo"),
		("http://user:pw@127.0.0.1:8080/git/owner/repo", "owner/repo"),
		("http://localhost:9000/git/owner/repo.git", "owner/repo"),
		("https://github.com/owner/repo/", "owner/repo"),
		("git@github.com:owner/repo", "owner/repo"),
	],
)
def test_slug_extracted_from_supported_remotes(url: str, expected: str) -> None:
	assert guard.extract_repo_slug(url) == expected


@pytest.mark.parametrize(
	"url",
	[
		"https://evilgithub.com/owner/repo.git",
		"https://github.com.evil.com/owner/repo",
		"https://gitlab.com/owner/repo.git",
		"http://localhost:9000/notgit/owner/repo",
		"https://github.com/owner/repo/tree/main",
		"https://github.com/owner",
		"https://github.com//repo",
		# Owner names are GitHub-username-shaped: no dots. Only the repo half
		# may carry one.
		"https://github.com/ow.ner/repo",
		"not a url",
		"",
	],
)
def test_lookalike_and_malformed_remotes_yield_no_slug(url: str) -> None:
	"""A bogus slug would be handed straight to `gh -R` and query the wrong repo."""
	assert guard.extract_repo_slug(url) == ""


def test_slug_extraction_matches_the_bash_implementation_it_mirrors() -> None:
	"""Parity with `extract_repo_slug` in session-start.sh, verified by running it.

	The two implementations encode the same host whitelist. If they drift, the
	guard could accept a remote the session-start probe rejects (or vice versa),
	and the whitelist is what stops a lookalike host from aiming `gh -R` at an
	unrelated github.com repo.
	"""
	session_start = REPO_ROOT / ".claude" / "hooks" / "session-start.sh"
	urls = [
		"https://github.com/owner/repo.git",
		"git@github.com:owner/repo.git",
		"ssh://git@github.com/owner/repo.git",
		"http://user:pw@127.0.0.1:8080/git/owner/repo",
		"http://localhost:9000/git/owner/repo.git",
		"https://evilgithub.com/owner/repo.git",
		"https://github.com.evil.com/owner/repo",
		"https://gitlab.com/owner/repo.git",
		"http://localhost:9000/notgit/owner/repo",
		"https://github.com/owner/repo/tree/main",
		"https://github.com/owner",
		"https://github.com//repo",
		"https://github.com/ow.ner/repo",
		"not a url",
	]
	script = f'source "{session_start}"\n' + "\n".join(
		f'extract_repo_slug "{url}"; echo ""' for url in urls
	)
	proc = subprocess.run(
		["bash", "-c", script], capture_output=True, text=True, timeout=60, check=True
	)
	# Each URL emits its slug (or nothing) followed by a delimiter newline.
	bash_results = proc.stdout.split("\n\n")[: len(urls)]
	for url, bash_slug in zip(urls, bash_results):
		assert guard.extract_repo_slug(url) == bash_slug.strip(), f"drift on {url}"


# ──────────────────────────────────────────────────────────────────
# Detection rule
# ──────────────────────────────────────────────────────────────────


MERGED_PR = {
	"number": 41,
	"state": "MERGED",
	"url": "https://github.com/o/r/pull/41",
	"title": "the merged one",
	"mergedAt": "2026-07-01T10:00:00Z",
	"headRefOid": "deadbeef",
}
OPEN_PR = {
	"number": 42,
	"state": "OPEN",
	"url": "https://github.com/o/r/pull/42",
	"title": "the live one",
	"mergedAt": None,
	"headRefOid": "cafebabe",
}


def _with_ancestry(monkeypatch, ancestors: set[str]) -> None:
	monkeypatch.setattr(guard, "is_ancestor", lambda sha, cwd: sha in ancestors)


def test_blocks_when_merged_pr_is_ancestor_and_no_open_pr(monkeypatch) -> None:
	_with_ancestry(monkeypatch, {"deadbeef"})
	assert guard.blocking_pull_request([MERGED_PR], "/repo") == MERGED_PR


def test_allows_when_an_open_pr_exists(monkeypatch) -> None:
	"""Condition 2 — commits still land somewhere live."""
	_with_ancestry(monkeypatch, {"deadbeef"})
	assert guard.blocking_pull_request([MERGED_PR, OPEN_PR], "/repo") is None


def test_allows_when_branch_was_reset_off_merged_history(monkeypatch) -> None:
	"""Condition 3 — the self-clearing property.

	After `git checkout -B <branch> origin/<default>` the merged PR still
	matches `--head <branch>` and no new PR exists yet, so conditions 1 and 2
	both hold. Only ancestry distinguishes this from the stranded case, and it
	must allow — otherwise the remediation in §21.A could never be committed.
	"""
	_with_ancestry(monkeypatch, set())
	assert guard.blocking_pull_request([MERGED_PR], "/repo") is None


def test_allows_when_no_pr_ever_existed(monkeypatch) -> None:
	_with_ancestry(monkeypatch, {"deadbeef"})
	assert guard.blocking_pull_request([], "/repo") is None


def test_allows_when_only_a_closed_unmerged_pr_exists(monkeypatch) -> None:
	closed = dict(OPEN_PR, state="CLOSED", mergedAt=None, headRefOid="deadbeef")
	_with_ancestry(monkeypatch, {"deadbeef"})
	assert guard.blocking_pull_request([closed], "/repo") is None


# ──────────────────────────────────────────────────────────────────
# Transport — REST first, GraphQL fallback
# ──────────────────────────────────────────────────────────────────


REST_PULL = {
	"number": 41,
	"state": "closed",
	"html_url": "https://github.com/o/r/pull/41",
	"title": "the merged one",
	"merged_at": "2026-07-01T10:00:00Z",
	"head": {"sha": "deadbeef"},
}


def test_rest_response_is_normalized_to_gh_pr_list_field_names() -> None:
	"""`state` is passed through verbatim and intentionally diverges: REST calls a
	merged PR `closed`, GraphQL calls it `MERGED`. The detection rule never reads
	that value for mergedness — `mergedAt` decides — and only tests it for `OPEN`,
	which both transports spell the same way modulo case.
	"""
	assert guard._normalize_rest_pull(REST_PULL) == dict(MERGED_PR, state="closed")


def test_normalized_rest_state_survives_the_open_check(monkeypatch) -> None:
	"""REST returns lowercase `open`; the detection rule must still see it."""
	rest_open = guard._normalize_rest_pull(
		dict(REST_PULL, number=42, state="open", merged_at=None, head={"sha": "cafebabe"})
	)
	_with_ancestry(monkeypatch, {"deadbeef"})
	merged = guard._normalize_rest_pull(REST_PULL)
	assert guard.blocking_pull_request([merged, rest_open], "/repo") is None


def test_rest_is_tried_first_and_graphql_is_not_called(monkeypatch) -> None:
	monkeypatch.setattr(guard, "_query_via_rest", lambda slug, branch, cwd: [MERGED_PR])
	monkeypatch.setattr(
		guard,
		"_query_via_pr_list",
		lambda slug, branch, cwd: pytest.fail("GraphQL must not run when REST succeeds"),
	)
	assert guard.query_pull_requests("o/r", "feature/x", "/repo") == [MERGED_PR]


def test_graphql_fallback_runs_when_rest_is_gated(monkeypatch) -> None:
	def _gated(slug, branch, cwd):
		raise guard.LookupUnavailable("HTTP 403")

	monkeypatch.setattr(guard, "_query_via_rest", _gated)
	monkeypatch.setattr(guard, "_query_via_pr_list", lambda slug, branch, cwd: [OPEN_PR])
	assert guard.query_pull_requests("o/r", "feature/x", "/repo") == [OPEN_PR]


def test_both_transports_failing_raises_with_both_reasons(monkeypatch) -> None:
	def _rest(slug, branch, cwd):
		raise guard.LookupUnavailable("rest boom")

	def _graphql(slug, branch, cwd):
		raise guard.LookupUnavailable("graphql boom")

	monkeypatch.setattr(guard, "_query_via_rest", _rest)
	monkeypatch.setattr(guard, "_query_via_pr_list", _graphql)
	with pytest.raises(guard.LookupUnavailable) as excinfo:
		guard.query_pull_requests("o/r", "feature/x", "/repo")
	assert "rest boom" in str(excinfo.value)
	assert "graphql boom" in str(excinfo.value)


def test_rest_call_targets_the_pulls_endpoint_with_one_request(monkeypatch) -> None:
	"""§15 — one call, `state=all`, not separate merged/open queries."""
	seen: list[list[str]] = []

	def _fake_run(argv, cwd, timeout):
		seen.append(argv)
		return 0, "[]", ""

	monkeypatch.setattr(guard, "_run", _fake_run)
	guard._query_via_rest("o/r", "feature/x", "/repo")
	assert len(seen) == 1
	argv = seen[0]
	assert argv[:2] == ["gh", "api"]
	assert "repos/o/r/pulls" in argv
	assert "state=all" in argv
	assert "head=o:feature/x" in argv


def test_default_branch_uses_remote_head_when_local_refs_are_missing(monkeypatch) -> None:
	def _fake_run(argv, cwd, timeout):
		lookup = {
			("git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"): (1, "", ""),
			("git", "rev-parse", "--verify", "refs/remotes/origin/main"): (1, "", ""),
			("git", "rev-parse", "--verify", "refs/remotes/origin/master"): (1, "", ""),
			(
				"git",
				"ls-remote",
				"--symref",
				"origin",
				"HEAD",
			): (0, "ref: refs/heads/trunk\tHEAD\n0123456789abcdef\tHEAD\n", ""),
		}
		result = lookup.get(tuple(argv))
		assert result is not None, f"unexpected argv: {argv}"
		return result

	monkeypatch.setattr(guard, "_run", _fake_run)
	assert guard.default_branch("/repo") == "trunk"


def test_default_branch_does_not_guess_local_main_over_remote_head(monkeypatch) -> None:
	def _fake_run(argv, cwd, timeout):
		lookup = {
			("git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"): (1, "", ""),
			("git", "ls-remote", "--symref", "origin", "HEAD"): (
				0,
				"ref: refs/heads/trunk\tHEAD\n0123456789abcdef\tHEAD\n",
				"",
			),
		}
		result = lookup.get(tuple(argv))
		assert result is not None, f"unexpected argv: {argv}"
		return result

	monkeypatch.setattr(guard, "_run", _fake_run)
	assert guard.default_branch("/repo") == "trunk"


def test_default_branch_returns_empty_when_only_local_main_exists(monkeypatch) -> None:
	def _fake_run(argv, cwd, timeout):
		lookup = {
			("git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"): (1, "", ""),
			("git", "ls-remote", "--symref", "origin", "HEAD"): (1, "", ""),
		}
		result = lookup.get(tuple(argv))
		assert result is not None, f"unexpected argv: {argv}"
		return result

	monkeypatch.setattr(guard, "_run", _fake_run)
	assert guard.default_branch("/repo") == ""


# ──────────────────────────────────────────────────────────────────
# Fail-open contract
# ──────────────────────────────────────────────────────────────────


def _evaluate(monkeypatch, tmp_path: Path, **overrides) -> tuple[int, str]:
	defaults = {
		"current_branch": lambda cwd: "feature/x",
		"default_branch": lambda cwd: "main",
		"repo_slug": lambda cwd: "o/r",
		"query_pull_requests": lambda slug, branch, cwd: [MERGED_PR],
		"is_ancestor": lambda sha, cwd: True,
		"_read_cache": lambda slug, branch: None,
		"_write_cache": lambda slug, branch, prs: None,
	}
	defaults.update(overrides)
	for name, value in defaults.items():
		monkeypatch.setattr(guard, name, value)
	monkeypatch.delenv("CLAUDE_PR_MERGE_GUARD", raising=False)
	payload = {
		"tool_name": "Bash",
		"tool_input": {"command": "git commit -m x"},
		"cwd": str(tmp_path),
	}
	return guard.evaluate(payload)


def test_blocks_end_to_end(monkeypatch, tmp_path: Path) -> None:
	code, message = _evaluate(monkeypatch, tmp_path)
	assert code == 2
	assert "§21" in message
	assert "git checkout -B feature/x origin/main" in message
	assert "https://github.com/o/r/pull/41" in message


def test_block_message_uses_a_placeholder_when_default_branch_is_unknown(monkeypatch, tmp_path: Path) -> None:
	code, message = _evaluate(monkeypatch, tmp_path, default_branch=lambda cwd: "")
	assert code == 2
	assert "git fetch origin <default-branch>" in message
	assert "could not determine the default branch automatically" in message


def test_fails_open_when_gh_is_unavailable(monkeypatch, tmp_path: Path, capsys) -> None:
	def _unavailable(slug, branch, cwd):
		raise guard.LookupUnavailable("gh: not found")

	code, message = _evaluate(monkeypatch, tmp_path, query_pull_requests=_unavailable)
	assert code == 0
	assert message == ""
	assert "feature/x" in json.loads(capsys.readouterr().out)["systemMessage"]


def test_fails_open_when_slug_is_underivable(monkeypatch, tmp_path: Path, capsys) -> None:
	code, _ = _evaluate(monkeypatch, tmp_path, repo_slug=lambda cwd: "")
	assert code == 0
	assert "systemMessage" in json.loads(capsys.readouterr().out)


def test_skipped_on_detached_head(monkeypatch, tmp_path: Path) -> None:
	assert _evaluate(monkeypatch, tmp_path, current_branch=lambda cwd: "")[0] == 0


def test_skipped_on_default_branch(monkeypatch, tmp_path: Path) -> None:
	assert _evaluate(
		monkeypatch,
		tmp_path,
		current_branch=lambda cwd: "main",
		repo_slug=lambda cwd: pytest.fail("default branch should skip before repo lookup"),
	)[0] == 0


def test_escape_hatch_disables_the_guard(monkeypatch, tmp_path: Path) -> None:
	monkeypatch.setenv("CLAUDE_PR_MERGE_GUARD", "off")
	monkeypatch.setattr(guard, "current_branch", lambda cwd: pytest.fail("must not run"))
	assert guard.evaluate(
		{"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}}
	) == (0, "")


def test_non_bash_tools_are_ignored() -> None:
	assert guard.evaluate({"tool_name": "Edit", "tool_input": {"command": "git commit"}}) == (0, "")


def test_block_is_reverified_live_before_firing(monkeypatch, tmp_path: Path) -> None:
	"""A cached block must not outlive the PR that justified it (§21.D)."""
	calls: list[str] = []

	def _fresh(slug, branch, cwd):
		calls.append(branch)
		return [MERGED_PR, OPEN_PR]

	code, _ = _evaluate(
		monkeypatch,
		tmp_path,
		_read_cache=lambda slug, branch: [MERGED_PR],
		query_pull_requests=_fresh,
	)
	assert calls == ["feature/x"], "a cache-derived block must trigger one live re-check"
	assert code == 0, "the freshly-opened PR must clear the guard immediately"


def test_cached_allow_issues_no_api_call(monkeypatch, tmp_path: Path) -> None:
	def _must_not_run(slug, branch, cwd):
		pytest.fail("cached allow must not hit the GitHub API (§15)")

	code, _ = _evaluate(
		monkeypatch,
		tmp_path,
		_read_cache=lambda slug, branch: [MERGED_PR, OPEN_PR],
		query_pull_requests=_must_not_run,
	)
	assert code == 0


# ──────────────────────────────────────────────────────────────────
# Process-level contract
# ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("payload", ["", "not json", "[]", "null"])
def test_malformed_stdin_exits_zero(payload: str) -> None:
	proc = subprocess.run(
		[sys.executable, str(GUARD_PATH)],
		input=payload,
		capture_output=True,
		text=True,
		timeout=30,
	)
	assert proc.returncode == 0


def test_unguarded_command_exits_zero_without_touching_git() -> None:
	proc = subprocess.run(
		[sys.executable, str(GUARD_PATH)],
		input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls -la"}}),
		capture_output=True,
		text=True,
		timeout=30,
	)
	assert proc.returncode == 0
	assert proc.stdout.strip() == ""


# ──────────────────────────────────────────────────────────────────
# End-to-end against a real git repository
# ──────────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
	proc = subprocess.run(
		["git", *args],
		cwd=repo,
		capture_output=True,
		text=True,
		check=True,
		env=_git_env(),
		timeout=30,
	)
	return proc.stdout.strip()


@pytest.fixture()
def merged_branch_repo(tmp_path: Path):
	"""A repo whose `feature/x` branch has a merged PR and no open one.

	Returns (repo_path, stub_bin_path). The stub `gh` answers the REST pulls
	query with a merged PR whose head sha is the branch tip, reproducing the
	exact stranded-work state §21 exists to catch.
	"""
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-b", "main")
	_git(repo, "config", "user.email", "test@example.com")
	_git(repo, "config", "user.name", "Test")
	_git(repo, "remote", "add", "origin", "https://github.com/o/r.git")
	(repo / "seed.txt").write_text("seed\n", encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-m", "seed")
	seed_sha = _git(repo, "rev-parse", "HEAD")
	_git(repo, "update-ref", "refs/remotes/origin/main", seed_sha)
	_git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")

	_git(repo, "checkout", "-b", "feature/x")
	(repo / "work.txt").write_text("work\n", encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-m", "merged work")
	merged_sha = _git(repo, "rev-parse", "HEAD")

	payload = json.dumps(
		[
			{
				"number": 41,
				"state": "closed",
				"html_url": "https://github.com/o/r/pull/41",
				"title": "the merged one",
				"merged_at": "2026-07-01T10:00:00Z",
				"head": {"sha": merged_sha},
			}
		]
	)
	stub_bin = tmp_path / "bin"
	stub_bin.mkdir()
	stub = stub_bin / "gh"
	stub.write_text(f"#!/bin/sh\ncat <<'EOF'\n{payload}\nEOF\n", encoding="utf-8")
	stub.chmod(0o755)
	return repo, stub_bin


def _run_hook(repo: Path, stub_bin: Path, command: str) -> subprocess.CompletedProcess:
	env = _git_env()
	env["PATH"] = f"{stub_bin}{os.pathsep}{env.get('PATH', '')}"
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env.pop("CLAUDE_PR_MERGE_GUARD", None)
	# A cold cache per run: the guard keys its TTL cache on slug+branch, which
	# every case in this fixture shares.
	env["TMPDIR"] = str(repo.parent / "cache")
	(repo.parent / "cache").mkdir(exist_ok=True)
	return subprocess.run(
		[sys.executable, str(GUARD_PATH)],
		input=json.dumps(
			{"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)}
		),
		capture_output=True,
		text=True,
		env=env,
		timeout=60,
	)


def test_e2e_blocks_commit_stacked_on_merged_history(merged_branch_repo) -> None:
	repo, stub_bin = merged_branch_repo
	(repo / "more.txt").write_text("more\n", encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-m", "follow-up that would be stranded")

	proc = _run_hook(repo, stub_bin, "git commit -m 'next'")
	assert proc.returncode == 2, proc.stdout + proc.stderr
	assert "git checkout -B feature/x origin/main" in proc.stderr
	assert "pull/41" in proc.stderr


def test_e2e_allows_after_branch_is_reset_off_merged_history(merged_branch_repo) -> None:
	"""The §21.A remediation must actually clear the guard, with the same branch
	name and before any new PR exists."""
	repo, stub_bin = merged_branch_repo
	_git(repo, "checkout", "-B", "feature/x", "main")
	(repo / "fresh.txt").write_text("fresh\n", encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-m", "fresh work on a rebuilt branch")

	proc = _run_hook(repo, stub_bin, "git commit -m 'next'")
	assert proc.returncode == 0, proc.stdout + proc.stderr


def test_e2e_allows_unguarded_commands_on_a_stranded_branch(merged_branch_repo) -> None:
	repo, stub_bin = merged_branch_repo
	assert _run_hook(repo, stub_bin, "git status").returncode == 0


def test_e2e_guards_push_as_well_as_commit(merged_branch_repo) -> None:
	repo, stub_bin = merged_branch_repo
	proc = _run_hook(repo, stub_bin, "git push -u origin feature/x")
	assert proc.returncode == 2, proc.stdout + proc.stderr


# ──────────────────────────────────────────────────────────────────
# Wiring — the parts that only exist as config / instruction text
# ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", [SETTINGS_PATH, TEMPLATE_SETTINGS_PATH])
def test_api_write_allowlist_disables_implicit_curl_config(path: Path) -> None:
	settings = json.loads(path.read_text(encoding="utf-8"))
	assert settings["permissions"]["allow"] == [
		"Bash(curl -q -sS -X PUT https://api.digitalocean.com/*)",
		"Bash(curl -q -sS -X POST https://api.digitalocean.com/*)",
		"Bash(curl -q -sS -X PATCH https://api.digitalocean.com/*)",
		"Bash(curl -q -sS -X PUT https://api.cloudflare.com/*)",
		"Bash(curl -q -sS -X POST https://api.cloudflare.com/*)",
		"Bash(curl -q -sS -X PATCH https://api.cloudflare.com/*)",
	]


@pytest.mark.parametrize("path", [SETTINGS_PATH, TEMPLATE_SETTINGS_PATH])
def test_guard_is_wired_as_a_pretooluse_bash_hook(path: Path) -> None:
	settings = json.loads(path.read_text(encoding="utf-8"))
	entries = settings["hooks"]["PreToolUse"]
	matched = [entry for entry in entries if entry.get("matcher") == "Bash"]
	assert matched, f"{path} has no PreToolUse hook matching Bash"
	commands = [hook["command"] for entry in matched for hook in entry["hooks"]]
	assert any("pr_merge_status_guard.py" in command for command in commands)
	# Invoked through `python3` rather than relying on the executable bit:
	# the consumer sync copies with plain `cp`, which leaves an existing
	# destination's mode untouched.
	assert any(
		command.startswith("python3 ")
		for command in commands
		if "pr_merge_status_guard.py" in command
	)


@pytest.mark.parametrize("path", [SETTINGS_PATH, TEMPLATE_SETTINGS_PATH])
def test_session_start_hook_is_preserved(path: Path) -> None:
	"""§6 — adding PreToolUse must not displace the existing SessionStart hook."""
	settings = json.loads(path.read_text(encoding="utf-8"))
	commands = [
		hook["command"]
		for entry in settings["hooks"]["SessionStart"]
		for hook in entry["hooks"]
	]
	assert any("session-start.sh" in command for command in commands)


def test_template_copies_are_identical() -> None:
	"""Consumer repos receive the guard via the workflow-templates/.claude mirror."""
	assert TEMPLATE_GUARD_PATH.read_text(encoding="utf-8") == GUARD_PATH.read_text(encoding="utf-8")
	assert TEMPLATE_SETTINGS_PATH.read_text(encoding="utf-8") == SETTINGS_PATH.read_text(
		encoding="utf-8"
	)
	assert TEMPLATE_CLAUDE_MD.read_text(encoding="utf-8") == CLAUDE_MD.read_text(encoding="utf-8")


def test_claude_md_documents_the_guard() -> None:
	text = CLAUDE_MD.read_text(encoding="utf-8")
	assert "## §21. Merged-PR Commit Guard (MANDATORY)" in text
	assert ".claude/hooks/pr_merge_status_guard.py" in text
	assert "CLAUDE_PR_MERGE_GUARD=off" in text
	assert "The guard is skipped entirely" not in text
	assert "The API-write\nconfirmation safeguard remains active in both cases." in text
