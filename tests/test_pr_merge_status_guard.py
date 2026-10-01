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
				"env",
				"GIT_TERMINAL_PROMPT=0",
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
			("env", "GIT_TERMINAL_PROMPT=0", "git", "ls-remote", "--symref", "origin", "HEAD"): (
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
			("env", "GIT_TERMINAL_PROMPT=0", "git", "ls-remote", "--symref", "origin", "HEAD"): (1, "", ""),
		}
		result = lookup.get(tuple(argv))
		assert result is not None, f"unexpected argv: {argv}"
		return result

	monkeypatch.setattr(guard, "_run", _fake_run)
	assert guard.default_branch("/repo") == ""


def test_remote_history_operations_disable_terminal_prompts(monkeypatch) -> None:
	observed_calls: list[tuple[list[str], int]] = []

	def _fake_run(argv, cwd, timeout):
		observed_calls.append((argv, timeout))
		return 0, "", ""

	monkeypatch.setattr(guard, "_run", _fake_run)
	assert guard.default_branch("/repo") == ""
	assert guard.remote_branch_tip("feature/x", "/repo") == ""
	assert guard.fetch_from_origin(["main"], "/repo") is True
	remote_calls = [call for call in observed_calls if call[0][:3] == ["env", "GIT_TERMINAL_PROMPT=0", "git"]]
	assert len(remote_calls) == 3
	assert all(timeout == guard._GIT_REMOTE_TIMEOUT_SECONDS for _, timeout in remote_calls)


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
	allow = settings["permissions"]["allow"]
	assert allow[:6] == [
		"Bash(curl -q -sS -X PUT https://api.digitalocean.com/*)",
		"Bash(curl -q -sS -X POST https://api.digitalocean.com/*)",
		"Bash(curl -q -sS -X PATCH https://api.digitalocean.com/*)",
		"Bash(curl -q -sS -X PUT https://api.cloudflare.com/*)",
		"Bash(curl -q -sS -X POST https://api.cloudflare.com/*)",
		"Bash(curl -q -sS -X PATCH https://api.cloudflare.com/*)",
	]
	# Every allowed curl rule keeps `-q` so an implicit ~/.curlrc cannot alter the call.
	curl_rules = [rule for rule in allow if rule.startswith("Bash(curl")]
	assert curl_rules == allow[:6]


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


# ──────────────────────────────────────────────────────────────────
# Merge-commit refinement, git-history fallback, MCP push tools
# ──────────────────────────────────────────────────────────────────


def _stub_gh(stub_bin: Path, payload: str | None) -> None:
	"""Write a `gh` stub: answers with `payload`, or fails like a gated proxy."""
	stub = stub_bin / "gh"
	if payload is None:
		stub.write_text(
			"#!/bin/sh\necho 'gh: GitHub access is not enabled for this session (HTTP 403)' >&2\nexit 1\n",
			encoding="utf-8",
		)
	else:
		stub.write_text(f"#!/bin/sh\ncat <<'EOF'\n{payload}\nEOF\n", encoding="utf-8")
	stub.chmod(0o755)


def _merged_pr_payload(head_sha: str) -> str:
	return json.dumps(
		[
			{
				"number": 41,
				"state": "closed",
				"html_url": "https://github.com/o/r/pull/41",
				"title": "the merged one",
				"merged_at": "2026-07-01T10:00:00Z",
				"head": {"sha": head_sha},
			}
		]
	)


def _run_hook_payload(repo: Path, stub_bin: Path, payload: dict) -> subprocess.CompletedProcess:
	env = _git_env()
	env["PATH"] = f"{stub_bin}{os.pathsep}{env.get('PATH', '')}"
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env.pop("CLAUDE_PR_MERGE_GUARD", None)
	env["TMPDIR"] = str(repo.parent / "cache")
	(repo.parent / "cache").mkdir(exist_ok=True)
	payload = {**payload, "cwd": str(repo)}
	return subprocess.run(
		[sys.executable, str(GUARD_PATH)],
		input=json.dumps(payload),
		capture_output=True,
		text=True,
		env=env,
		timeout=120,
	)


def _bash_payload(command: str) -> dict:
	return {"tool_name": "Bash", "tool_input": {"command": command}}


def _mcp_payload(branch: str = "feature/x", owner: str = "o", repo: str = "r") -> dict:
	return {
		"tool_name": "mcp__github__push_files",
		"tool_input": {
			"owner": owner,
			"repo": repo,
			"branch": branch,
			"files": [{"path": "f.txt", "content": "x"}],
			"message": "m",
		},
	}


def _ask_decision(proc: subprocess.CompletedProcess) -> dict | None:
	for line in proc.stdout.splitlines():
		try:
			parsed = json.loads(line)
		except ValueError:
			continue
		if parsed.get("hookSpecificOutput", {}).get("permissionDecision") == "ask":
			return parsed
	return None


@pytest.fixture()
def merge_commit_repo(tmp_path: Path):
	"""A repo with a real (local, offline) origin whose `feature/x` was merged
	into main by a merge commit, checked out at the merged head.

	`remote.origin.url` stays a github.com URL so slug derivation works;
	`url.<bare>.insteadOf` routes fetch/ls-remote to the bare repo. Returns
	(repo_path, stub_bin_path, merged_sha, merge_commit_sha).
	"""
	bare = tmp_path / "origin.git"
	subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True, env=_git_env())
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q", "-b", "main")
	_git(repo, "config", "user.email", "test@example.com")
	_git(repo, "config", "user.name", "Test")
	_git(repo, "remote", "add", "origin", "https://github.com/o/r.git")
	_git(repo, "config", f"url.{bare}.insteadOf", "https://github.com/o/r.git")
	(repo / "seed.txt").write_text("seed\n", encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "seed")
	_git(repo, "push", "-q", "origin", "main")
	_git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")

	_git(repo, "checkout", "-q", "-b", "feature/x")
	(repo / "work.txt").write_text("work\n", encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "merged work")
	merged_sha = _git(repo, "rev-parse", "HEAD")
	_git(repo, "push", "-q", "origin", "feature/x")

	_git(repo, "checkout", "-q", "main")
	(repo / "main.txt").write_text("main moved on\n", encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "main work")
	_git(repo, "merge", "-q", "--no-ff", "feature/x", "-m", "Merge pull request #41")
	merge_sha = _git(repo, "rev-parse", "HEAD")
	_git(repo, "push", "-q", "origin", "main")
	_git(repo, "checkout", "-q", "feature/x")

	stub_bin = tmp_path / "bin"
	stub_bin.mkdir()
	_stub_gh(stub_bin, _merged_pr_payload(merged_sha))
	return repo, stub_bin, merged_sha, merge_sha


def test_first_parent_chain_separates_side_history_from_branch_states(merge_commit_repo) -> None:
	repo, _, merged_sha, merge_sha = merge_commit_repo
	cwd = str(repo)
	assert guard.on_first_parent_chain(merge_sha, "refs/remotes/origin/main", cwd) is True
	assert guard.on_first_parent_chain(merged_sha, "refs/remotes/origin/main", cwd) is False
	seed = _git(repo, "rev-list", "--max-parents=0", "HEAD")
	assert guard.on_first_parent_chain(seed, "refs/remotes/origin/main", cwd) is True
	assert guard.on_first_parent_chain("", "refs/remotes/origin/main", cwd) is None


def test_merge_commit_stranded_branch_still_blocks(merge_commit_repo) -> None:
	repo, stub_bin, _, _ = merge_commit_repo
	proc = _run_hook_payload(repo, stub_bin, _bash_payload("git commit -m next"))
	assert proc.returncode == 2, proc.stdout + proc.stderr
	assert "pull/41" in proc.stderr


def test_merge_commit_reset_branch_is_allowed(merge_commit_repo) -> None:
	"""Regression: with merge-commit merges the merged head is an ancestor of
	the default branch, so plain ancestry blocked the §21.A remediation
	itself. The fork point on main's first-parent chain must clear it."""
	repo, stub_bin, _, _ = merge_commit_repo
	_git(repo, "checkout", "-q", "-B", "feature/x", "origin/main")
	(repo / "fresh.txt").write_text("fresh\n", encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "fresh work on a rebuilt branch")
	proc = _run_hook_payload(repo, stub_bin, _bash_payload("git commit -m next"))
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert _ask_decision(proc) is None


def test_history_fallback_asks_when_origin_dropped_the_merged_branch(merge_commit_repo) -> None:
	"""An absent remote ref could be a deleted branch or one never pushed."""
	repo, stub_bin, _, _ = merge_commit_repo
	_stub_gh(stub_bin, None)
	_git(repo, "push", "-q", "origin", "--delete", "feature/x")
	commit_proc = _run_hook_payload(repo, stub_bin, _bash_payload("git commit -m next"))
	assert commit_proc.returncode == 0, commit_proc.stdout + commit_proc.stderr
	assert "never-pushed branch" in commit_proc.stdout
	assert _ask_decision(commit_proc) is None
	push_proc = _run_hook_payload(repo, stub_bin, _bash_payload("git push -u origin feature/x"))
	assert push_proc.returncode == 0, push_proc.stdout + push_proc.stderr
	assert _ask_decision(push_proc) is not None


def test_history_fallback_never_pushed_stacked_branch_is_inconclusive(merge_commit_repo) -> None:
	"""A new branch based on merged side history must not be destructively reset."""
	repo, stub_bin, _, _ = merge_commit_repo
	_stub_gh(stub_bin, None)
	_git(repo, "checkout", "-q", "-b", "feature/never-pushed")
	commit_proc = _run_hook_payload(repo, stub_bin, _bash_payload("git commit -m next"))
	assert commit_proc.returncode == 0, commit_proc.stdout + commit_proc.stderr
	assert "never-pushed branch" in commit_proc.stdout
	(repo / "stacked.txt").write_text("stacked\n", encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "stacked work")
	push_proc = _run_hook_payload(
		repo, stub_bin, _bash_payload("git push -u origin feature/never-pushed")
	)
	assert push_proc.returncode == 0, push_proc.stdout + push_proc.stderr
	assert _ask_decision(push_proc) is not None


def test_history_fallback_blocks_when_remote_branch_is_contained_in_main(merge_commit_repo) -> None:
	repo, stub_bin, _, _ = merge_commit_repo
	_stub_gh(stub_bin, None)
	proc = _run_hook_payload(repo, stub_bin, _bash_payload("git push origin feature/x"))
	assert proc.returncode == 2, proc.stdout + proc.stderr
	assert "fully contained in origin/main" in proc.stderr


def test_history_fallback_push_asks_when_inconclusive(merge_commit_repo) -> None:
	"""A rebuilt branch and a squash-merged one look identical to git; the
	push must go through the harness prompt, not silently proceed."""
	repo, stub_bin, _, _ = merge_commit_repo
	_stub_gh(stub_bin, None)
	_git(repo, "checkout", "-q", "-B", "feature/x", "origin/main")
	(repo / "fresh.txt").write_text("fresh\n", encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "fresh")
	proc = _run_hook_payload(repo, stub_bin, _bash_payload("git push -u origin feature/x"))
	assert proc.returncode == 0, proc.stdout + proc.stderr
	decision = _ask_decision(proc)
	assert decision is not None, proc.stdout
	assert "inconclusive" in decision["systemMessage"]
	assert "still open" in decision["hookSpecificOutput"]["permissionDecisionReason"]


def test_history_fallback_commit_warns_when_inconclusive(merge_commit_repo) -> None:
	"""A local commit strands nothing by itself; it is allowed with a warning."""
	repo, stub_bin, _, _ = merge_commit_repo
	_stub_gh(stub_bin, None)
	_git(repo, "checkout", "-q", "-B", "feature/x", "origin/main")
	proc = _run_hook_payload(repo, stub_bin, _bash_payload("git commit -m next"))
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert _ask_decision(proc) is None
	assert "merged-PR guard skipped" in json.loads(proc.stdout)["systemMessage"]


def test_history_fallback_treats_a_stacked_branch_as_inconclusive(merge_commit_repo) -> None:
	"""`feature/y` forks off `feature/x` (merged side history) but carries its
	own unmerged commits on origin — a stacked branch, not a corpse."""
	repo, stub_bin, _, _ = merge_commit_repo
	_stub_gh(stub_bin, None)
	_git(repo, "checkout", "-q", "-b", "feature/y")
	(repo / "stacked.txt").write_text("stacked\n", encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "stacked work")
	_git(repo, "push", "-q", "origin", "feature/y")
	proc = _run_hook_payload(repo, stub_bin, _bash_payload("git push origin feature/y"))
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert _ask_decision(proc) is not None
	assert "carries commits not yet in origin/main" in proc.stdout


def test_history_verdict_is_unavailable_without_a_default_branch(tmp_path: Path) -> None:
	verdict, detail = guard.git_history_verdict("HEAD", "feature/x", "", str(tmp_path))
	assert verdict == guard.VERDICT_UNAVAILABLE
	assert "default branch" in detail


def test_mcp_push_onto_merged_remote_branch_is_blocked(merge_commit_repo) -> None:
	repo, stub_bin, _, _ = merge_commit_repo
	proc = _run_hook_payload(repo, stub_bin, _mcp_payload())
	assert proc.returncode == 2, proc.stdout + proc.stderr
	assert "origin/feature/x" in proc.stderr
	assert "pull/41" in proc.stderr


def test_mcp_push_onto_rebuilt_remote_branch_is_allowed(merge_commit_repo) -> None:
	repo, stub_bin, _, _ = merge_commit_repo
	_git(repo, "checkout", "-q", "-B", "feature/x", "origin/main")
	_git(repo, "push", "-q", "--force", "origin", "feature/x")
	proc = _run_hook_payload(repo, stub_bin, _mcp_payload())
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert _ask_decision(proc) is None


def test_mcp_push_to_a_branch_origin_does_not_have_is_allowed(merge_commit_repo) -> None:
	repo, stub_bin, _, _ = merge_commit_repo
	_stub_gh(stub_bin, None)
	proc = _run_hook_payload(repo, stub_bin, _mcp_payload(branch="feature/never-pushed"))
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert proc.stdout.strip() == ""


def test_mcp_push_uses_history_fallback_when_api_is_gated(merge_commit_repo) -> None:
	repo, stub_bin, _, _ = merge_commit_repo
	_stub_gh(stub_bin, None)
	proc = _run_hook_payload(repo, stub_bin, _mcp_payload())
	assert proc.returncode == 2, proc.stdout + proc.stderr
	assert "fully contained in origin/main" in proc.stderr


def test_mcp_push_asks_when_remote_tip_fetch_fails(merge_commit_repo, monkeypatch, capsys) -> None:
	repo, _, merged_sha, _ = merge_commit_repo
	monkeypatch.setattr(guard, "remote_branch_tip", lambda branch, cwd: merged_sha)
	monkeypatch.setattr(guard, "fetch_from_origin", lambda refs, cwd: False)
	monkeypatch.setattr(
		guard, "query_pull_requests", lambda *args: pytest.fail("API lookup must not run")
	)
	payload = {**_mcp_payload(), "cwd": str(repo)}
	assert guard.evaluate(payload) == (0, "")
	decision = _ask_decision(
		subprocess.CompletedProcess([], 0, stdout=capsys.readouterr().out, stderr="")
	)
	assert decision is not None
	assert "could not fetch origin/feature/x" in decision["systemMessage"]


def test_mcp_push_asks_when_remote_tip_lookup_fails(merge_commit_repo, monkeypatch, capsys) -> None:
	repo, _, _, _ = merge_commit_repo
	monkeypatch.setattr(guard, "remote_branch_tip", lambda branch, cwd: None)
	monkeypatch.setattr(
		guard, "query_pull_requests", lambda *args: pytest.fail("API lookup must not run")
	)
	payload = {**_mcp_payload(), "cwd": str(repo)}
	assert guard.evaluate(payload) == (0, "")
	decision = _ask_decision(
		subprocess.CompletedProcess([], 0, stdout=capsys.readouterr().out, stderr="")
	)
	assert decision is not None
	assert "could not list origin's branches" in decision["systemMessage"]
	assert "no local checkout" not in decision["systemMessage"]


def test_remote_only_mcp_push_caches_fresh_pr_snapshot(monkeypatch, tmp_path: Path) -> None:
	pull_requests = [MERGED_PR]
	cache_writes: list[tuple[str, str, list[dict]]] = []
	monkeypatch.setattr(guard, "repo_slug", lambda cwd: "o/local")
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	monkeypatch.setattr(guard, "query_pull_requests", lambda slug, branch, cwd: pull_requests)
	monkeypatch.setattr(
		guard,
		"_write_cache",
		lambda slug, branch, entries: cache_writes.append((slug, branch, entries)),
	)
	payload = {**_mcp_payload(), "cwd": str(tmp_path)}
	assert guard.evaluate(payload) == (0, "")
	assert cache_writes == [("o/r", "feature/x", pull_requests)]


@pytest.mark.parametrize("gated", [False, True])
def test_mcp_push_to_another_repository_asks(merge_commit_repo, gated: bool) -> None:
	"""No local checkout of the target → ancestry cannot be verified → ask."""
	repo, stub_bin, _, _ = merge_commit_repo
	if gated:
		_stub_gh(stub_bin, None)
	proc = _run_hook_payload(repo, stub_bin, _mcp_payload(owner="someone-else"))
	assert proc.returncode == 0, proc.stdout + proc.stderr
	decision = _ask_decision(proc)
	assert decision is not None, proc.stdout
	assert "someone-else/r" in decision["systemMessage"]


def test_mcp_push_to_default_branch_is_skipped(merge_commit_repo) -> None:
	repo, stub_bin, _, _ = merge_commit_repo
	_stub_gh(stub_bin, None)
	proc = _run_hook_payload(repo, stub_bin, _mcp_payload(branch="main"))
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert proc.stdout.strip() == ""


def test_mcp_create_or_update_file_is_guarded_too(merge_commit_repo) -> None:
	repo, stub_bin, _, _ = merge_commit_repo
	payload = {
		"tool_name": "mcp__github__create_or_update_file",
		"tool_input": {"owner": "o", "repo": "r", "branch": "feature/x", "path": "f", "content": "c", "message": "m"},
	}
	proc = _run_hook_payload(repo, stub_bin, payload)
	assert proc.returncode == 2, proc.stdout + proc.stderr


def test_mcp_push_with_malformed_input_is_ignored() -> None:
	assert guard.evaluate({"tool_name": "mcp__github__push_files", "tool_input": {"owner": "o"}}) == (0, "")
	assert guard.evaluate({"tool_name": "mcp__github__push_files", "tool_input": "nope"}) == (0, "")


def test_mcp_push_escape_hatch(monkeypatch) -> None:
	monkeypatch.setenv("CLAUDE_PR_MERGE_GUARD", "off")
	monkeypatch.setattr(guard, "repo_slug", lambda cwd: pytest.fail("must not run"))
	assert guard.evaluate(_mcp_payload()) == (0, "")


@pytest.mark.parametrize("path", [SETTINGS_PATH, TEMPLATE_SETTINGS_PATH])
def test_guard_is_wired_for_the_mcp_push_tools(path: Path) -> None:
	settings = json.loads(path.read_text(encoding="utf-8"))
	entries = settings["hooks"]["PreToolUse"]
	matched = [
		entry
		for entry in entries
		if set(str(entry.get("matcher", "")).split("|")) == set(guard.MCP_PUSH_TOOLS)
	]
	assert matched, f"{path} has no PreToolUse hook matching {sorted(guard.MCP_PUSH_TOOLS)}"
	commands = [hook["command"] for entry in matched for hook in entry["hooks"]]
	assert any(
		command.startswith("python3 ") and "pr_merge_status_guard.py" in command
		for command in commands
	)


def test_claude_md_documents_the_history_fallback() -> None:
	text = CLAUDE_MD.read_text(encoding="utf-8")
	assert "mcp__github__push_files" in text
	assert "first-parent" in text
	assert "git history" in text
	assert "permissionDecision" in text or "asks" in text


# ──────────────────────────────────────────────────────────────────
# Effective repository and push target ref (issue #5144)
#
# These tests read the workflow-templates twin: the hook is a protected path,
# so the change lands there first and reaches .claude/hooks/ through a
# `[claude-twin-sync]` commit, after which both copies are identical.
# ──────────────────────────────────────────────────────────────────


def _load_guard_module(path: Path):
	spec = importlib.util.spec_from_file_location("pr_merge_status_guard_twin", path)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


twin_guard = _load_guard_module(TEMPLATE_GUARD_PATH)


def _targets(command: str, cwd: Path) -> list[tuple]:
	return [
		(t.subcommand, t.cwd, t.branch, t.tip, t.reaches_remote, bool(t.fallback_reason))
		for t in twin_guard.guard_targets(command, str(cwd))
	]


def test_cd_into_a_worktree_judges_the_worktree_and_the_target_ref(tmp_path: Path) -> None:
	worktree = tmp_path / "wt"
	worktree.mkdir()
	assert _targets(f"cd {worktree} && git push origin HEAD:feature/open", tmp_path / "..") == [
		("push", str(worktree), "feature/open", "HEAD", True, False)
	]


def test_relative_cd_chain_and_cd_options_are_followed(tmp_path: Path) -> None:
	(tmp_path / "a" / "b").mkdir(parents=True)
	assert _targets("cd a; cd -P -- b && git commit -m x", tmp_path) == [
		("commit", str(tmp_path / "a" / "b"), "", "HEAD", False, False)
	]
	assert _targets("cd a/b && cd .. && git commit -m x", tmp_path) == [
		("commit", str(tmp_path / "a"), "", "HEAD", False, False)
	]


def test_git_dash_c_is_followed_and_composes(tmp_path: Path) -> None:
	(tmp_path / "a" / "b").mkdir(parents=True)
	assert _targets("git -C a -C b push origin HEAD:x", tmp_path) == [
		("push", str(tmp_path / "a" / "b"), "x", "HEAD", True, False)
	]


def test_git_dir_prefix_and_option_select_the_repository(tmp_path: Path) -> None:
	git_dir = tmp_path / "other" / ".git"
	git_dir.mkdir(parents=True)
	assert _targets(f"GIT_DIR={git_dir} git push origin src:dst", tmp_path) == [
		("push", str(git_dir), "dst", "src", True, False)
	]
	assert _targets("git --git-dir=other/.git commit -m x", tmp_path) == [
		("commit", str(git_dir), "", "HEAD", False, False)
	]
	assert _targets("git -C other --git-dir .git commit -m x", tmp_path) == [
		("commit", str(git_dir), "", "HEAD", False, False)
	]


@pytest.mark.parametrize(
	"command",
	[
		'cd "~"; git commit -m x',
		"cd '~'; git commit -m x",
		"cd \\~; git commit -m x",
		'cd ~"/wt"; git commit -m x',
		'git -C "~" commit -m x',
		"git --git-dir '~' commit -m x",
		'GIT_DIR="~" git commit -m x',
		"git --git-dir=~ commit -m x",
	],
)
def test_quoted_tilde_is_a_literal_directory_not_home(tmp_path: Path, monkeypatch, command: str) -> None:
	# Bash expands none of these, so the path names a directory called `~`
	# under the current one. It does not exist here, so the guard falls back
	# to the session checkout instead of judging $HOME.
	home = tmp_path / "home"
	(home / "wt").mkdir(parents=True)
	session = tmp_path / "session"
	session.mkdir()
	monkeypatch.setenv("HOME", str(home))
	targets = twin_guard.guard_targets(command, str(session))
	assert len(targets) == 1, command
	assert targets[0].cwd == str(session)
	assert "~" in targets[0].fallback_reason


def test_quoted_tilde_resolves_to_a_literal_tilde_directory(tmp_path: Path, monkeypatch) -> None:
	home = tmp_path / "home"
	home.mkdir()
	(tmp_path / "~" / "wt").mkdir(parents=True)
	monkeypatch.setenv("HOME", str(home))
	assert _targets('cd "~/wt" && git commit -m x', tmp_path) == [
		("commit", str(tmp_path / "~" / "wt"), "", "HEAD", False, False)
	]
	assert _targets("git --git-dir=~/wt commit -m x", tmp_path) == [
		("commit", str(tmp_path / "~" / "wt"), "", "HEAD", False, False)
	]


def test_unquoted_tilde_still_expands_to_home(tmp_path: Path, monkeypatch) -> None:
	home = tmp_path / "home"
	(home / "wt").mkdir(parents=True)
	monkeypatch.setenv("HOME", str(home))
	# A quoted `~` elsewhere in the command does not affect this path word.
	assert _targets('cd ~/wt && git commit -m "~ fix"', tmp_path) == [
		("commit", str(home / "wt"), "", "HEAD", False, False)
	]
	assert _targets("GIT_DIR=~/wt git commit -m x", tmp_path) == [
		("commit", str(home / "wt"), "", "HEAD", False, False)
	]


def test_literal_tilde_words_follow_bash_quoting() -> None:
	assert twin_guard._literal_tilde_words('cd "~" ~/a \'~/b\' \\~c ~"/d" x"~" GIT_DIR="~/e" GIT_DIR=~/f') == {
		"~",
		"~/b",
		"~c",
		"~/d",
		"GIT_DIR=~/e",
	}


def test_cd_dash_p_resolves_symlinks_physically(tmp_path: Path) -> None:
	(tmp_path / "real" / "sub").mkdir(parents=True)
	(tmp_path / "repo").mkdir()
	(tmp_path / "repo" / "link").symlink_to(tmp_path / "real" / "sub")
	repo = tmp_path / "repo"
	assert _targets("cd -P link/.. && git commit -m x", repo) == [
		("commit", str(tmp_path / "real"), "", "HEAD", False, False)
	]
	# The last of -L / -P wins, grouped or not.
	assert _targets("cd -LP link/.. && git commit -m x", repo) == [
		("commit", str(tmp_path / "real"), "", "HEAD", False, False)
	]
	assert _targets("cd -P -L link/.. && git commit -m x", repo) == [
		("commit", str(repo), "", "HEAD", False, False)
	]
	# A plain cd is logical: `link/..` is the directory it started in.
	assert _targets("cd link/.. && git commit -m x", repo) == [
		("commit", str(repo), "", "HEAD", False, False)
	]


def test_git_dash_c_and_git_dir_resolve_symlinks_physically(tmp_path: Path) -> None:
	(tmp_path / "real" / "sub").mkdir(parents=True)
	(tmp_path / "real" / ".git").mkdir()
	(tmp_path / "repo").mkdir()
	(tmp_path / "repo" / "link").symlink_to(tmp_path / "real" / "sub")
	repo = tmp_path / "repo"
	assert _targets("git -C link/.. push origin HEAD:x", repo) == [
		("push", str(tmp_path / "real"), "x", "HEAD", True, False)
	]
	assert _targets("git --git-dir=link/../.git commit -m x", repo) == [
		("commit", str(tmp_path / "real" / ".git"), "", "HEAD", False, False)
	]


@pytest.mark.parametrize(
	"command",
	[
		"cd wt -P && git push origin HEAD:x",
		"cd -X wt; git push origin HEAD:x",
		"cd -@ wt; git push origin HEAD:x",
	],
)
def test_cd_that_bash_rejects_falls_back_to_the_session_checkout(tmp_path: Path, command: str) -> None:
	# `cd wt -P` is "too many arguments" and an unknown option is rejected,
	# so the shell stays where it was.
	(tmp_path / "wt").mkdir()
	targets = twin_guard.guard_targets(command, str(tmp_path))
	assert len(targets) == 1, command
	assert targets[0].cwd == str(tmp_path)
	assert targets[0].fallback_reason


def test_cd_dash_e_is_accepted(tmp_path: Path) -> None:
	(tmp_path / "wt").mkdir()
	assert _targets("cd -Pe wt && git commit -m x", tmp_path) == [
		("commit", str(tmp_path / "wt"), "", "HEAD", False, False)
	]


def test_git_work_tree_does_not_change_the_judged_repository(tmp_path: Path) -> None:
	(tmp_path / "tree").mkdir()
	assert _targets("GIT_WORK_TREE=tree git commit -m x", tmp_path) == [
		("commit", str(tmp_path), "", "HEAD", False, False)
	]


@pytest.mark.parametrize(
	"command",
	[
		"cd $WORKTREE && git push origin HEAD:x",
		'cd "$(git rev-parse --show-toplevel)/wt" && git push origin HEAD:x',
		"cd missing-dir && git push origin HEAD:x",
		"cd - && git push origin HEAD:x",
		"cd ~someone && git push origin HEAD:x",
		"cd wt w2 && git push origin HEAD:x",
		"pushd wt && git push origin HEAD:x",
		"(cd wt; git push origin HEAD:x)",
		"if cd wt; then true; fi; git push origin HEAD:x",
		"export GIT_DIR=wt/.git; git push origin HEAD:x",
		"GIT_DIR=wt/.git; git push origin HEAD:x",
		"git -C $WORKTREE push origin HEAD:x",
		"GIT_DIR=$DIR git push origin HEAD:x",
	],
)
def test_unresolvable_directory_falls_back_to_the_session_checkout(tmp_path: Path, command: str) -> None:
	(tmp_path / "wt").mkdir()
	(tmp_path / "w2").mkdir()
	targets = twin_guard.guard_targets(command, str(tmp_path))
	assert targets, command
	for target in targets:
		assert target.cwd == str(tmp_path)
		assert target.branch == ""
		assert target.tip == "HEAD"
		assert target.fallback_reason


def test_a_subshell_only_affects_later_commands(tmp_path: Path) -> None:
	"""A subshell's own `git push)` is not a guarded call (as in git_subcommands),
	but the directory after it is unknown, so later calls fall back."""
	(tmp_path / "wt").mkdir()
	targets = twin_guard.guard_targets("(cd wt && git commit -m x) ; git push origin HEAD:y", str(tmp_path))
	assert [(t.subcommand, t.branch, bool(t.fallback_reason)) for t in targets] == [
		("commit", "", True),
		("push", "", True),
	]


@pytest.mark.parametrize(
	"command",
	[
		"git push origin --delete feature/x",
		"git push -d origin feature/x",
		"git push origin :feature/x",
		"git push origin refs/tags/v1.0",
		"git push origin v1.0:refs/tags/v1.0",
		"git push origin 'refs/tags/*'",
		"git push origin 'refs/tags/*:refs/tags/*'",
		"git push origin '+refs/tags/v*:refs/tags/v*'",
		"git push origin 'refs/tags/v1.?'",
		"git push --tags origin",
		"git push origin --tags",
		"git push --tag origin",
		"git push --ta origin",
		"git status",
		"git log -1",
	],
)
def test_deletions_tags_and_unguarded_calls_yield_no_target(tmp_path: Path, command: str) -> None:
	assert twin_guard.guard_targets(command, str(tmp_path)) == []


@pytest.mark.parametrize(
	("command", "branch", "tip"),
	[
		("git push", "", "HEAD"),
		("git push -u origin", "", "HEAD"),
		("git push --tags origin HEAD:feature/y", "feature/y", "HEAD"),
		("git push --follow-tags origin", "", "HEAD"),
		("git push origin HEAD", "", "HEAD"),
		("git push origin feature/y", "feature/y", "feature/y"),
		("git push origin +HEAD:refs/heads/feature/y", "feature/y", "HEAD"),
		("git push -o ci.skip --repo origin origin HEAD:feature/y", "feature/y", "HEAD"),
		("git push --force-with-lease=feature/y origin @:feature/y", "feature/y", "HEAD"),
		("git push origin -- HEAD:feature/y", "feature/y", "HEAD"),
	],
)
def test_push_refspecs_name_the_judged_branch_and_tip(tmp_path: Path, command: str, branch: str, tip: str) -> None:
	assert _targets(command, tmp_path) == [("push", str(tmp_path), branch, tip, True, False)]


@pytest.mark.parametrize(
	"command",
	[
		"git push --all origin",
		"git push origin --all",
		"git push --branches origin",
		"git push --mirror origin",
		"git push --al origin",
		"git push --mir origin",
		"git push -u --all origin",
		"git push origin :",
		"git push origin +:",
	],
)
def test_bulk_pushes_judge_the_checked_out_branch_and_carry_a_bulk_reason(tmp_path: Path, command: str) -> None:
	"""PR #5173 review: `--all`, `--branches`, `--mirror` (and the prefixes git
	expands to them) and the `:` matching refspec write every branch, so the
	target also carries a `bulk_reason` that makes the hook ask."""
	targets = twin_guard.guard_targets(command, str(tmp_path))
	assert len(targets) == 1, command
	target = targets[0]
	assert (target.cwd, target.branch, target.tip, target.reaches_remote) == (str(tmp_path), "", "HEAD", True)
	assert not target.fallback_reason, command
	assert target.bulk_reason, command


def test_bulk_push_in_an_unresolvable_directory_keeps_the_fallback_and_the_bulk_reason(tmp_path: Path) -> None:
	targets = twin_guard.guard_targets("cd $WORKTREE && git push --all origin", str(tmp_path))
	assert len(targets) == 1
	assert (targets[0].cwd, targets[0].branch, targets[0].tip) == (str(tmp_path), "", "HEAD")
	assert targets[0].fallback_reason and targets[0].bulk_reason


@pytest.mark.parametrize(
	("command", "bulk"),
	[
		("git push origin 'refs/heads/*:refs/heads/*'", True),
		("git push origin '+refs/heads/merged*:refs/heads/merged*'", True),
		("git push origin 'feature/*'", True),
		("git push origin HEAD:$B", False),
		("git push origin HEAD:feature/[x]", False),
	],
)
def test_pattern_refspecs_keep_the_fallback_and_ask(tmp_path: Path, command: str, bulk: bool) -> None:
	"""A `*` pattern keeps the AD-9 fallback (session checkout, warning) and,
	when it can write branches, also carries a `bulk_reason`."""
	targets = twin_guard.guard_targets(command, str(tmp_path))
	assert len(targets) == 1, command
	assert targets[0].fallback_reason, command
	assert bool(targets[0].bulk_reason) is bulk, command


def test_push_options_that_are_not_bulk_flags_are_not_mistaken_for_them() -> None:
	for token in ("--atomic", "--a", "--m", "--no-verify", "--force", "-a", "--tags", "--allow"):
		assert not twin_guard._is_push_bulk_flag(token), token
	for token in ("--all", "--al", "--branches", "--br", "--mirror", "--mirr"):
		assert twin_guard._is_push_bulk_flag(token), token


def test_every_guarded_call_in_a_command_is_a_target(tmp_path: Path) -> None:
	(tmp_path / "wt").mkdir()
	assert _targets("git commit -m x && cd wt && git push origin HEAD:a HEAD:b", tmp_path) == [
		("commit", str(tmp_path), "", "HEAD", False, False),
		("push", str(tmp_path / "wt"), "a", "HEAD", True, False),
		("push", str(tmp_path / "wt"), "b", "HEAD", True, False),
	]


def test_guard_targets_agree_with_git_subcommands_on_unreadable_input(tmp_path: Path) -> None:
	assert twin_guard.guard_targets("git commit -m 'unterminated", str(tmp_path)) == []
	assert twin_guard.guard_targets("echo 'cd /tmp && git push'", str(tmp_path)) == []


_OPEN_PR_REST = {
	"number": 42,
	"state": "open",
	"html_url": "https://github.com/o/r/pull/42",
	"title": "the live one",
	"merged_at": None,
	"head": {"sha": "cafebabe"},
}


@pytest.fixture()
def worktree_repo(tmp_path: Path):
	"""A main checkout stranded on `feature/x` (PR #41 merged, no open PR) and a
	detached scratch worktree with fresh work on top of main.

	The `gh` stub answers `feature/x` with the merged PR, `feature/open` with an
	open PR, anything else with no PR, and logs every call to `gh-calls.log`.
	"""
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q", "-b", "main")
	_git(repo, "config", "user.email", "test@example.com")
	_git(repo, "config", "user.name", "Test")
	_git(repo, "remote", "add", "origin", "https://github.com/o/r.git")
	(repo / "seed.txt").write_text("seed\n", encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "seed")
	seed_sha = _git(repo, "rev-parse", "HEAD")
	_git(repo, "update-ref", "refs/remotes/origin/main", seed_sha)
	_git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")

	_git(repo, "checkout", "-q", "-b", "feature/x")
	(repo / "work.txt").write_text("work\n", encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "merged work")
	merged_sha = _git(repo, "rev-parse", "HEAD")

	worktree = tmp_path / "wt"
	_git(repo, "worktree", "add", "-q", "--detach", str(worktree), seed_sha)
	(worktree / "fresh.txt").write_text("fresh\n", encoding="utf-8")
	_git(worktree, "add", "-A")
	_git(worktree, "commit", "-q", "-m", "fresh work in a scratch worktree")

	stub_bin = tmp_path / "bin"
	stub_bin.mkdir()
	calls_log = tmp_path / "gh-calls.log"
	merged_json = _merged_pr_payload(merged_sha)
	open_json = json.dumps([_OPEN_PR_REST])
	(stub_bin / "gh").write_text(
		"#!/bin/sh\n"
		f'echo "$*" >> "{calls_log}"\n'
		'for arg in "$@"; do\n'
		'  case "$arg" in\n'
		f"    head=o:feature/x) cat <<'EOF'\n{merged_json}\nEOF\n      exit 0;;\n"
		f"    head=o:feature/open) cat <<'EOF'\n{open_json}\nEOF\n      exit 0;;\n"
		"  esac\n"
		"done\n"
		"echo '[]'\n",
		encoding="utf-8",
	)
	(stub_bin / "gh").chmod(0o755)
	return repo, worktree, stub_bin, merged_sha, calls_log


def _run_twin_hook(cwd: Path, stub_bin: Path, command: str) -> subprocess.CompletedProcess:
	env = _git_env()
	env["PATH"] = f"{stub_bin}{os.pathsep}{env.get('PATH', '')}"
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env.pop("CLAUDE_PR_MERGE_GUARD", None)
	cache_dir = stub_bin.parent / "cache"
	cache_dir.mkdir(exist_ok=True)
	env["TMPDIR"] = str(cache_dir)
	return subprocess.run(
		[sys.executable, str(TEMPLATE_GUARD_PATH)],
		input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}),
		capture_output=True,
		text=True,
		env=env,
		timeout=120,
	)


def test_e2e_worktree_push_to_an_open_pr_branch_is_allowed_from_a_stranded_checkout(worktree_repo) -> None:
	"""The observed false block: the main checkout sits on a merged branch, the
	push runs from a detached worktree onto an open PR's branch."""
	repo, worktree, stub_bin, _, _ = worktree_repo
	proc = _run_twin_hook(repo, stub_bin, f"cd {worktree} && git push origin HEAD:feature/open")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert _ask_decision(proc) is None


def test_e2e_worktree_commit_is_not_judged_by_the_stranded_checkout(worktree_repo) -> None:
	repo, worktree, stub_bin, _, _ = worktree_repo
	proc = _run_twin_hook(repo, stub_bin, f"cd {worktree} && git commit -m fresh")
	assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("main_checkout", ["stranded", "rebuilt", "detached"])
def test_e2e_push_of_merged_history_to_a_merged_branch_is_blocked(worktree_repo, main_checkout: str) -> None:
	"""The missed block: whatever the main checkout is on, a worktree pushing
	merged history to a merged branch with no open PR is blocked."""
	repo, worktree, stub_bin, merged_sha, _ = worktree_repo
	if main_checkout == "rebuilt":
		_git(repo, "checkout", "-q", "-B", "feature/x", "main")
	elif main_checkout == "detached":
		_git(repo, "checkout", "-q", "--detach", "main")
	_git(worktree, "checkout", "-q", "--detach", merged_sha)
	proc = _run_twin_hook(repo, stub_bin, f"cd {worktree} && git push origin HEAD:feature/x")
	assert proc.returncode == 2, proc.stdout + proc.stderr
	assert "pull/41" in proc.stderr
	assert f"Judged in: {worktree}" in proc.stderr


def test_e2e_fresh_worktree_push_to_a_merged_branch_is_allowed(worktree_repo) -> None:
	"""Self-clearing still holds for the target ref: fresh work rebuilt from
	main does not stack on the merged head, so the push is allowed."""
	repo, worktree, stub_bin, _, _ = worktree_repo
	proc = _run_twin_hook(repo, stub_bin, f"cd {worktree} && git push origin HEAD:feature/x")
	assert proc.returncode == 0, proc.stdout + proc.stderr


def test_e2e_git_dash_c_behaves_like_cd(worktree_repo) -> None:
	repo, worktree, stub_bin, merged_sha, _ = worktree_repo
	allowed = _run_twin_hook(repo, stub_bin, f"git -C {worktree} push origin HEAD:feature/open")
	assert allowed.returncode == 0, allowed.stdout + allowed.stderr
	_git(worktree, "checkout", "-q", "--detach", merged_sha)
	_git(repo, "checkout", "-q", "--detach", "main")
	blocked = _run_twin_hook(repo, stub_bin, f"git -C {worktree} push origin HEAD:feature/x")
	assert blocked.returncode == 2, blocked.stdout + blocked.stderr


def test_e2e_git_dir_prefix_is_judged_even_from_outside_any_repository(worktree_repo, tmp_path: Path) -> None:
	repo, _, stub_bin, _, _ = worktree_repo
	elsewhere = tmp_path / "elsewhere"
	elsewhere.mkdir()
	proc = _run_twin_hook(elsewhere, stub_bin, f"GIT_DIR={repo / '.git'} git push origin HEAD:feature/x")
	assert proc.returncode == 2, proc.stdout + proc.stderr
	assert "pull/41" in proc.stderr


def test_e2e_linked_worktree_git_dir_is_judged_by_its_own_head(worktree_repo) -> None:
	"""A linked worktree's git dir sits under the main checkout's `.git/worktrees/`.
	Git run from inside it reads that worktree's HEAD, never the enclosing
	checkout's, so the guard judges the commit the push actually sends."""
	repo, worktree, stub_bin, merged_sha, _ = worktree_repo
	linked_git_dir = Path(_git(worktree, "rev-parse", "--absolute-git-dir"))
	assert linked_git_dir.parent.name == "worktrees"
	command = f"GIT_DIR={linked_git_dir} git push origin HEAD:feature/x"
	# The main checkout is stranded on the merged branch; the worktree carries
	# fresh work rebuilt from main, so the push is allowed.
	allowed = _run_twin_hook(repo, stub_bin, command)
	assert allowed.returncode == 0, allowed.stdout + allowed.stderr
	# The main checkout is rebuilt from main; the worktree sits on the merged
	# head, so the push stacks on merged history and is blocked.
	_git(repo, "checkout", "-q", "--detach", "main")
	_git(worktree, "checkout", "-q", "--detach", merged_sha)
	blocked = _run_twin_hook(repo, stub_bin, command)
	assert blocked.returncode == 2, blocked.stdout + blocked.stderr
	assert "pull/41" in blocked.stderr
	assert f"Judged in: {linked_git_dir}" in blocked.stderr


def test_e2e_unresolvable_directory_keeps_todays_behaviour_and_warns(worktree_repo) -> None:
	repo, worktree, stub_bin, _, _ = worktree_repo
	# The session checkout is stranded, so today's behaviour blocks.
	blocked = _run_twin_hook(repo, stub_bin, 'cd "$WORKTREE" && git push origin HEAD:feature/open')
	assert blocked.returncode == 2, blocked.stdout + blocked.stderr
	assert "could not resolve where `git push` runs" in blocked.stderr
	# After a rebuild, today's behaviour allows, and the warning still shows.
	_git(repo, "checkout", "-q", "-B", "feature/x", "main")
	allowed = _run_twin_hook(repo, stub_bin, 'cd "$WORKTREE" && git push origin HEAD:feature/open')
	assert allowed.returncode == 0, allowed.stdout + allowed.stderr
	message = json.loads(allowed.stdout)["systemMessage"]
	assert "could not resolve where `git push` runs" in message
	assert "needs shell expansion" in message


def test_e2e_one_api_call_per_slug_and_branch_pair(worktree_repo) -> None:
	"""§21.D: two targets on the same (slug, branch) share one REST call."""
	repo, worktree, stub_bin, _, calls_log = worktree_repo
	proc = _run_twin_hook(
		repo,
		stub_bin,
		f"git -C {worktree} push origin HEAD:feature/open && git -C {worktree} push origin main:feature/open",
	)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	calls = calls_log.read_text(encoding="utf-8").splitlines()
	assert len(calls) == 1, calls
	assert "head=o:feature/open" in calls[0]


def test_e2e_several_asks_merge_into_one_hook_result(worktree_repo) -> None:
	"""With GitHub gated, two inconclusive pushes produce one JSON object."""
	repo, worktree, stub_bin, _, _ = worktree_repo
	_git(repo, "checkout", "-q", "-B", "feature/x", "main")
	_stub_gh(stub_bin, None)
	proc = _run_twin_hook(
		repo, stub_bin, f"git -C {worktree} push origin HEAD:feature/a HEAD:feature/b"
	)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	lines = [line for line in proc.stdout.splitlines() if line.strip()]
	assert len(lines) == 1, proc.stdout
	decision = _ask_decision(proc)
	assert decision is not None
	assert "feature/a" in decision["systemMessage"] and "feature/b" in decision["systemMessage"]


@pytest.mark.parametrize("command", ["git push --all origin", "git push origin :", "git push --mirror origin"])
def test_e2e_bulk_push_from_the_default_branch_asks(worktree_repo, command: str) -> None:
	"""PR #5173 review: the checked-out branch is the default branch, so it is
	skipped, but the push writes the other branches too: ask, with no API call."""
	repo, _, stub_bin, _, calls_log = worktree_repo
	_git(repo, "checkout", "-q", "main")
	proc = _run_twin_hook(repo, stub_bin, command)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	decision = _ask_decision(proc)
	assert decision is not None, proc.stdout
	assert "judges only the checked-out branch" in decision["hookSpecificOutput"]["permissionDecisionReason"]
	assert not calls_log.exists() or not calls_log.read_text(encoding="utf-8").strip()


def test_e2e_bulk_push_from_a_stranded_checkout_still_blocks(worktree_repo) -> None:
	repo, _, stub_bin, _, _ = worktree_repo
	proc = _run_twin_hook(repo, stub_bin, "git push --all origin")
	assert proc.returncode == 2, proc.stdout + proc.stderr
	assert "pull/41" in proc.stderr
	assert _ask_decision(proc) is None


def test_e2e_bulk_push_from_an_open_pr_branch_asks(worktree_repo) -> None:
	repo, _, stub_bin, _, _ = worktree_repo
	_git(repo, "checkout", "-q", "-b", "feature/open", "main")
	proc = _run_twin_hook(repo, stub_bin, "git push --all origin")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert _ask_decision(proc) is not None


def test_e2e_bulk_push_is_not_judged_when_the_guard_is_off(worktree_repo, monkeypatch) -> None:
	repo, _, stub_bin, _, _ = worktree_repo
	_git(repo, "checkout", "-q", "main")
	env = _git_env()
	env["PATH"] = f"{stub_bin}{os.pathsep}{env.get('PATH', '')}"
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["CLAUDE_PR_MERGE_GUARD"] = "off"
	proc = subprocess.run(
		[sys.executable, str(TEMPLATE_GUARD_PATH)],
		input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push --all origin"}, "cwd": str(repo)}),
		capture_output=True,
		text=True,
		env=env,
		timeout=120,
	)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert proc.stdout.strip() == ""


@pytest.mark.parametrize("command", ["git push --tags origin", "git push origin 'refs/tags/*:refs/tags/*'"])
def test_e2e_tag_only_push_from_a_stranded_checkout_is_allowed(worktree_repo, command: str) -> None:
	"""PR #5173 review: `git push --tags origin` and a tag pattern refspec
	write no branch, so the stranded checkout's branch is not judged."""
	repo, _, stub_bin, _, calls_log = worktree_repo
	proc = _run_twin_hook(repo, stub_bin, command)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert _ask_decision(proc) is None
	assert not calls_log.exists() or not calls_log.read_text(encoding="utf-8").strip()


def test_e2e_bulk_ask_merges_with_other_notices_into_one_result(worktree_repo) -> None:
	repo, _, stub_bin, _, _ = worktree_repo
	_git(repo, "checkout", "-q", "main")
	proc = _run_twin_hook(repo, stub_bin, "git push origin 'refs/heads/*:refs/heads/*'")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	lines = [line for line in proc.stdout.splitlines() if line.strip()]
	assert len(lines) == 1, proc.stdout
	decision = _ask_decision(proc)
	assert decision is not None
	assert "could not resolve where `git push` runs" in decision["systemMessage"]


def test_single_directory_commands_keep_their_output_shape(monkeypatch, tmp_path: Path, capsys) -> None:
	"""A plain `git commit` in the session checkout prints exactly what it did
	before: one `merged-PR guard skipped:` warning when GitHub is unreachable."""
	def _unavailable(slug, branch, cwd):
		raise twin_guard.LookupUnavailable("gh: not found")

	for name, value in {
		"current_branch": lambda cwd: "feature/x",
		"default_branch": lambda cwd: "main",
		"repo_slug": lambda cwd: "o/r",
		"query_pull_requests": _unavailable,
		"_read_cache": lambda slug, branch: None,
		"_write_cache": lambda slug, branch, prs: None,
	}.items():
		monkeypatch.setattr(twin_guard, name, value)
	monkeypatch.delenv("CLAUDE_PR_MERGE_GUARD", raising=False)
	code, message = twin_guard.evaluate(
		{"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}, "cwd": str(tmp_path)}
	)
	assert (code, message) == (0, "")
	system_message = json.loads(capsys.readouterr().out)["systemMessage"]
	assert system_message.startswith("merged-PR guard skipped: could not reach GitHub")
	assert "\n" not in system_message


@pytest.mark.parametrize(
	"command",
	[
		"git push origin HEAD:heads/feature/x",
		"git push origin HEAD:tags/v1",
		"git push origin HEAD:remotes/origin/x",
		"git push origin HEAD:$B",
		'git push origin "$B"',
		"B=feature/x; git push origin $B",
		"git push origin 'refs/heads/*:refs/heads/*'",
		"git push origin 'refs/heads/merged*:refs/heads/merged*'",
		"git push origin HEAD:feature/[x]",
		"git push origin HEAD:{a,b}",
		"git push origin HEAD~1:feature/x",
		"git push origin -- -x:feature/x",
		"git push origin -- HEAD:-x",
	],
)
def test_unresolvable_refspecs_fall_back_to_the_session_checkout(tmp_path: Path, command: str) -> None:
	"""PR #5173 review: refspec words the guard cannot turn into one branch
	keep the old behaviour (session checkout, HEAD) with a warning reason."""
	targets = twin_guard.guard_targets(command, str(tmp_path))
	assert targets, command
	for target in targets:
		assert (target.cwd, target.branch, target.tip) == (str(tmp_path), "", "HEAD"), command
		assert target.fallback_reason, command


def test_plain_slashed_branch_names_are_still_judged_on_the_target(tmp_path: Path) -> None:
	"""Only the `heads/`, `tags/`, `remotes/` shorthands fall back; ordinary
	branch names with slashes (every `claude/…` branch) stay precise."""
	assert _targets("git push origin HEAD:claude/implement-plan-x-phase-1", tmp_path) == [
		("push", str(tmp_path), "claude/implement-plan-x-phase-1", "HEAD", True, False)
	]


@pytest.mark.parametrize(
	"command",
	[
		"cd wt || git push origin HEAD:feature/open",
		"cd wt & git push origin HEAD:feature/open",
		"cd wt | cat; git push origin HEAD:feature/open",
		"echo x | cd wt; git push origin HEAD:feature/open",
		"false || cd wt; git push origin HEAD:feature/open",
		"cd wt 2>/dev/null && git push origin HEAD:feature/open",
	],
)
def test_cd_not_run_sequentially_makes_the_directory_unknown(tmp_path: Path, command: str) -> None:
	"""PR #5173 review: a `cd` joined by `||`, `&`, `|` (or a redirection) may
	not run or runs in a subshell, so git's directory is unknown."""
	(tmp_path / "wt").mkdir()
	targets = twin_guard.guard_targets(command, str(tmp_path))
	assert targets, command
	for target in targets:
		assert (target.cwd, target.branch, target.tip) == (str(tmp_path), "", "HEAD"), command
		assert target.fallback_reason, command


def test_cd_joined_sequentially_is_still_followed(tmp_path: Path) -> None:
	(tmp_path / "wt").mkdir()
	for command in (
		"cd wt && git push origin HEAD:x",
		"cd wt; git push origin HEAD:x",
		"cd wt\ngit push origin HEAD:x",
		"true || false; cd wt && git push origin HEAD:x",
	):
		assert _targets(command, tmp_path) == [("push", str(tmp_path / "wt"), "x", "HEAD", True, False)], command


@pytest.mark.parametrize(
	"command",
	[
		"git fetch origin && cd wt; git push origin HEAD:feature/open",
		"git fetch origin && cd wt\ngit push origin HEAD:feature/open",
		"git fetch origin && cd wt && git status || git push origin HEAD:feature/open",
		"cd wt && git status & git push origin HEAD:feature/open",
		"cd wt && git status & wait; git push origin HEAD:feature/open",
	],
)
def test_cd_that_may_be_skipped_makes_later_lists_unknown(tmp_path: Path, command: str) -> None:
	"""PR #5173 review round 1 (head 420ccd0): a `cd` after `&&` behind a
	command that may fail is skipped with it, and the push after `;`, a newline
	or `||` then runs in the original directory; a `cd` in a list sent to the
	background with `&` never reaches the commands after it."""
	(tmp_path / "wt").mkdir()
	targets = twin_guard.guard_targets(command, str(tmp_path))
	assert targets, command
	push = targets[-1]
	assert (push.cwd, push.branch, push.tip) == (str(tmp_path), "", "HEAD"), command
	assert push.fallback_reason, command


def test_cd_behind_a_command_that_may_fail_is_followed_inside_its_chain(tmp_path: Path) -> None:
	(tmp_path / "wt" / "sub").mkdir(parents=True)
	for command, directory in (
		("git fetch origin && cd wt && git push origin HEAD:x", tmp_path / "wt"),
		("git fetch origin && cd wt && git status && git push origin HEAD:x", tmp_path / "wt"),
		("cd wt && cd sub; git push origin HEAD:x", tmp_path / "wt" / "sub"),
		("git fetch origin || true; cd wt && git push origin HEAD:x", tmp_path / "wt"),
	):
		assert _targets(command, tmp_path) == [("push", str(directory), "x", "HEAD", True, False)], command


def test_separator_segments_match_the_plain_segmenter() -> None:
	for command in (
		"cd a && git push || echo no; git commit -m 'x; y' | tee log & wait",
		"git commit -m x\n\ngit push",
		"",
	):
		assert [tokens for tokens, _before, _after in twin_guard._shell_segments_with_separators(command)] == (
			twin_guard._shell_segments(command)
		)


@pytest.mark.parametrize(
	"command",
	[
		"git push origin HEAD:heads/feature/open",
		"git push origin HEAD:$B",
		'git push origin "$B"',
		"git push origin 'refs/heads/*:refs/heads/*'",
		"cd {worktree} || git push origin HEAD:feature/open",
		"cd {worktree} & git push origin HEAD:feature/open",
		"git push origin -- -x:feature/open",
	],
)
def test_e2e_review_bypasses_block_on_a_stranded_checkout(worktree_repo, command: str) -> None:
	"""Each shape from the PR #5173 review blocks when the session checkout sits
	on merged history (the old behaviour), with the fallback warning."""
	repo, worktree, stub_bin, _, _ = worktree_repo
	proc = _run_twin_hook(repo, stub_bin, command.format(worktree=worktree))
	assert proc.returncode == 2, proc.stdout + proc.stderr
	assert "pull/41" in proc.stderr
	assert "merged-PR guard: could not resolve" in proc.stderr


def test_e2e_review_bypass_warns_when_the_fallback_allows(worktree_repo) -> None:
	repo, _, stub_bin, _, _ = worktree_repo
	_git(repo, "checkout", "-q", "-B", "feature/x", "main")
	proc = _run_twin_hook(repo, stub_bin, "git push origin HEAD:$B")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "needs shell expansion or is a pattern" in json.loads(proc.stdout)["systemMessage"]


def test_claude_md_documents_the_effective_repository_rule() -> None:
	text = CLAUDE_MD.read_text(encoding="utf-8")
	assert "effective repository" in text
	assert "`git push <remote> <src>:<dst>`" in text
	assert "one API call per `(slug, branch)` pair" in text


def test_push_repo_option_does_not_shift_the_repository_positional(tmp_path: Path) -> None:
	"""PR #5173 review round 1 (rejected finding): git reads the first
	positional as the repository even when `--repo` is given, so
	`git push --repo origin HEAD:feature/x` names no refspec (git treats
	`HEAD:feature/x` as the repository) and the checked-out branch is judged."""
	for command in (
		"git push --repo origin HEAD:feature/x",
		"git push --repo=origin HEAD:feature/x",
	):
		assert _targets(command, tmp_path) == [("push", str(tmp_path), "", "HEAD", True, False)], command
	assert _targets("git push --repo origin origin HEAD:feature/x", tmp_path) == [
		("push", str(tmp_path), "feature/x", "HEAD", True, False)
	]


def test_block_keeps_skip_warnings_for_other_targets(monkeypatch, tmp_path: Path) -> None:
	"""PR #5173 review round 1: when one target blocks, the skip warning of
	another target in the same command stays in the block message."""
	def _judge(target, session_cwd, memo):
		if target.branch == "feature/merged":
			return 2, "blocked: feature/merged sits on merged PR history", "", "", ""
		return 0, "", "warn", "could not reach GitHub for feature/other", ""

	monkeypatch.setattr(twin_guard, "_judge_guard_target", _judge)
	monkeypatch.delenv("CLAUDE_PR_MERGE_GUARD", raising=False)
	code, message = twin_guard.evaluate(
		{
			"tool_name": "Bash",
			"tool_input": {"command": "git push origin HEAD:feature/merged HEAD:feature/other"},
			"cwd": str(tmp_path),
		}
	)
	assert code == 2
	assert message.startswith("blocked: feature/merged")
	assert "merged-PR guard skipped: could not reach GitHub for feature/other" in message


@pytest.mark.parametrize(
	"command",
	[
		"cd $WORKTREE && git push --tags origin",
		"cd $WORKTREE && git push origin 'refs/tags/*:refs/tags/*'",
		"cd $WORKTREE && git push origin refs/tags/v1.0",
		"cd $WORKTREE && git push --delete origin feature/x",
		"cd $WORKTREE && git push origin :feature/x",
		"git -C $WORKTREE push origin --tags",
	],
)
def test_unresolvable_directory_skips_pushes_that_write_no_branch(tmp_path: Path, command: str) -> None:
	"""PR #5173 review: a tag-only push or a deletion lands no commits on a
	branch whatever the directory, so an unknown directory adds no fallback
	target on the session checkout."""
	assert twin_guard.guard_targets(command, str(tmp_path)) == []


def test_unresolvable_directory_still_falls_back_for_a_branch_push(tmp_path: Path) -> None:
	targets = twin_guard.guard_targets("cd $WORKTREE && git push --tags origin HEAD:feature/y", str(tmp_path))
	assert len(targets) == 1
	assert (targets[0].cwd, targets[0].branch, targets[0].tip) == (str(tmp_path), "", "HEAD")
	assert targets[0].fallback_reason


@pytest.mark.parametrize("command", ["git push origin feature/merged:", "git push origin +feature/merged:"])
def test_empty_destination_refspec_judges_the_checked_out_branch(tmp_path: Path, command: str) -> None:
	"""PR #5173 review: git rejects `<src>:` for a push (`fatal: invalid
	refspec`), so it writes nothing; the guard judges the checked-out branch
	with HEAD, as for a push without a refspec, rather than skipping the call."""
	assert _targets(command, tmp_path) == [("push", str(tmp_path), "", "HEAD", True, False)]


def test_ambiguous_tags_prefix_still_judges_the_checked_out_branch(tmp_path: Path) -> None:
	"""PR #5173 review: only `--ta`, `--tag` and `--tags` mean tags-only; git
	refuses `--t` as ambiguous with `--thin`, so it is judged as before."""
	assert _targets("git push --t origin", tmp_path) == [("push", str(tmp_path), "", "HEAD", True, False)]


def test_cd_into_a_directory_without_search_permission_falls_back(monkeypatch, tmp_path: Path) -> None:
	"""PR #5173 review: `cd` fails on a directory the process cannot enter, so
	`cd locked; git push` pushes from the session checkout; the guard judges
	the session checkout with a reason instead of probing the locked
	directory (where git cannot run, which would allow silently)."""
	locked = tmp_path / "locked"
	locked.mkdir()
	real_access = twin_guard.os.access

	def fake_access(path, mode, *args, **kwargs):
		if os.path.normpath(str(path)) == str(locked) and mode & os.X_OK:
			return False
		return real_access(path, mode, *args, **kwargs)

	monkeypatch.setattr(twin_guard.os, "access", fake_access)
	targets = twin_guard.guard_targets("cd locked; git push origin HEAD:feature/y", str(tmp_path))
	assert len(targets) == 1
	assert (targets[0].cwd, targets[0].branch, targets[0].tip) == (str(tmp_path), "", "HEAD")
	assert "cannot be entered" in targets[0].fallback_reason


def test_failed_reverification_is_not_repeated_for_the_same_pair(monkeypatch, tmp_path: Path, capsys) -> None:
	"""PR #5173 review, §21.D: a cache-derived block whose live re-check fails
	is remembered, so a second target on the same (slug, branch) pair does not
	call GitHub again."""
	live_calls: list[tuple[str, str]] = []

	def _unavailable(slug, branch, cwd):
		live_calls.append((slug, branch))
		raise twin_guard.LookupUnavailable("HTTP 403")

	for name, value in {
		"current_branch": lambda cwd: "feature/x",
		"default_branch": lambda cwd: "main",
		"repo_slug": lambda cwd: "o/r",
		"_read_cache": lambda slug, branch: [{"number": 41}],
		"_write_cache": lambda slug, branch, prs: None,
		"blocking_pull_request": lambda prs, cwd, base, tip="HEAD": {"number": 41},
		"query_pull_requests": _unavailable,
		"git_history_verdict": lambda tip, branch, base, cwd: (twin_guard.VERDICT_INCONCLUSIVE, "no remote ref"),
	}.items():
		monkeypatch.setattr(twin_guard, name, value)
	monkeypatch.delenv("CLAUDE_PR_MERGE_GUARD", raising=False)
	code, message = twin_guard.evaluate(
		{"tool_name": "Bash", "tool_input": {"command": "git commit -m x && git push"}, "cwd": str(tmp_path)}
	)
	assert (code, message) == (0, "")
	assert live_calls == [("o/r", "feature/x")]
	hook_output = json.loads(capsys.readouterr().out)
	assert hook_output["hookSpecificOutput"]["permissionDecision"] == "ask"
	assert "could not re-verify: HTTP 403" in hook_output["systemMessage"]


def test_bulk_push_with_an_inconclusive_judge_asks_once(monkeypatch, tmp_path: Path, capsys) -> None:
	"""PR #5173 review: when the judge already asks for a bulk push, the bulk
	reason joins that ask instead of adding a second confirmation entry."""
	monkeypatch.setattr(
		twin_guard,
		"_judge_guard_target",
		lambda target, session_cwd, memo: (0, "", "ask", "history is inconclusive", "short: inconclusive"),
	)
	monkeypatch.delenv("CLAUDE_PR_MERGE_GUARD", raising=False)
	code, message = twin_guard.evaluate(
		{"tool_name": "Bash", "tool_input": {"command": "git push --all origin"}, "cwd": str(tmp_path)}
	)
	assert (code, message) == (0, "")
	hook_output = json.loads(capsys.readouterr().out)
	assert hook_output["systemMessage"].count("merged-PR guard needs confirmation:") == 1
	assert "history is inconclusive" in hook_output["systemMessage"]
	assert "judges only the checked-out branch" in hook_output["systemMessage"]
	decision_reason = hook_output["hookSpecificOutput"]["permissionDecisionReason"]
	assert "short: inconclusive" in decision_reason
	assert "judges only the checked-out branch" in decision_reason


def test_a_quoted_tilde_word_does_not_make_the_same_unquoted_word_literal(tmp_path: Path, monkeypatch) -> None:
	"""PR #5173 review (round 9): literal-tilde state is per occurrence, not per
	text. A quoted `"~/wt"` elsewhere in the command leaves an unquoted `~/wt`
	to the home directory, as Bash expands it."""
	home = tmp_path / "home"
	(home / "wt").mkdir(parents=True)
	(tmp_path / "~" / "wt").mkdir(parents=True)
	monkeypatch.setenv("HOME", str(home))
	assert _targets('echo "~/wt"; cd ~/wt && git commit -m x', tmp_path) == [
		("commit", str(home / "wt"), "", "HEAD", False, False)
	]
	assert _targets('git commit -m "~/wt" && git -C ~/wt push origin HEAD:feature/x', tmp_path) == [
		("commit", str(tmp_path), "", "HEAD", False, False),
		("push", str(home / "wt"), "feature/x", "HEAD", True, False),
	]
	# The quoted occurrence still names a directory called `~`.
	(home / "wt" / "~" / "wt").mkdir(parents=True)
	assert _targets('cd ~/wt; cd "~/wt" && git commit -m x', tmp_path) == [
		("commit", str(home / "wt" / "~" / "wt"), "", "HEAD", False, False)
	]


def test_tilde_flags_are_read_per_word(tmp_path: Path, monkeypatch) -> None:
	home = tmp_path / "home"
	(home / "wt").mkdir(parents=True)
	(tmp_path / "~" / "wt").mkdir(parents=True)
	monkeypatch.setenv("HOME", str(home))
	# The unquoted occurrence after the quoted one expands; the quoted one does not.
	assert twin_guard._tilde_word_flags('cd "~/wt"; cd ~/wt') == [
		("cd", False),
		("~/wt", True),
		("cd", False),
		("~/wt", False),
	]
	# A word made only of quotes is kept, so the flags line up with the tokens.
	assert twin_guard._tilde_word_flags('git commit -m "" ~') == [
		("git", False),
		("commit", False),
		("-m", False),
		("", False),
		("~", False),
	]
	assert _targets('cd "~/wt" && cd .. && cd ~/wt && git commit -m x', tmp_path) == [
		("commit", str(home / "wt"), "", "HEAD", False, False)
	]


def test_the_same_tilde_path_quoted_and_unquoted_in_one_git_call_falls_back(tmp_path: Path, monkeypatch) -> None:
	"""Within one segment the guard cannot tell which occurrence is the path
	word, so that call is judged on the session checkout with a warning; later
	calls keep the directory."""
	home = tmp_path / "home"
	(home / "wt").mkdir(parents=True)
	monkeypatch.setenv("HOME", str(home))
	targets = twin_guard.guard_targets('git -C ~/wt commit -m "~/wt"; git -C ~/wt push origin HEAD:feature/x', str(tmp_path))
	assert [(t.subcommand, t.cwd, bool(t.fallback_reason)) for t in targets] == [
		("commit", str(tmp_path), True),
		("push", str(home / "wt"), False),
	]
	assert "quoted in one place and unquoted in another" in targets[0].fallback_reason


@pytest.mark.parametrize(
	"command",
	[
		"if true; then git push origin HEAD:x; fi",
		"if false; then true; else git commit -m x; fi",
		"while true; do git push origin HEAD:x; done",
		"until false; do git commit -m x; done",
		"! git commit -m x",
		"time git push origin HEAD:x",
		"if true; then GIT_DIR=.git git commit -m x; fi",
		"if git commit -m x; then true; fi",
	],
)
def test_git_after_a_reserved_word_is_guarded(command: str) -> None:
	"""PR #5173 review (round 9): `then git push`, `do git commit`, `! git …`
	run git, so they are guarded like a bare `git …`."""
	assert twin_guard.git_subcommands(command) & twin_guard.GUARDED_SUBCOMMANDS
	assert twin_guard.guard_targets(command, "/")


def test_git_after_a_reserved_word_is_judged_in_the_tracked_directory(tmp_path: Path) -> None:
	(tmp_path / "wt").mkdir()
	assert _targets("cd wt && if true; then git push origin HEAD:feature/x; fi", tmp_path) == [
		("push", str(tmp_path / "wt"), "feature/x", "HEAD", True, False)
	]


@pytest.mark.parametrize(
	"command",
	[
		"if true; then true; cd wt; fi; git push origin HEAD:x",
		"if false; then true; else true; cd wt; fi; git push origin HEAD:x",
		"for d in a; do true; cd wt; done; git push origin HEAD:x",
		"while false; do true; cd wt; done; git push origin HEAD:x",
		"case a in a) cd wt;; esac; git push origin HEAD:x",
		"if true; then if true; then true; fi; cd wt; fi; git push origin HEAD:x",
	],
)
def test_a_cd_inside_an_if_or_loop_body_makes_the_directory_unknown(tmp_path: Path, command: str) -> None:
	(tmp_path / "wt").mkdir()
	targets = twin_guard.guard_targets(command, str(tmp_path))
	assert [(t.subcommand, t.cwd, bool(t.fallback_reason)) for t in targets] == [("push", str(tmp_path), True)]


def test_a_cd_after_a_closed_compound_is_followed(tmp_path: Path) -> None:
	(tmp_path / "wt").mkdir()
	for command in (
		"if true; then true; fi; cd wt && git commit -m x",
		"for d in a b; do true; done; cd wt && git commit -m x",
		"case a in a) true;; esac; cd wt && git commit -m x",
	):
		assert _targets(command, tmp_path) == [("commit", str(tmp_path / "wt"), "", "HEAD", False, False)], command


def test_e2e_push_after_a_reserved_word_is_blocked_on_a_stranded_checkout(worktree_repo) -> None:
	"""The bypass from the PR #5173 review: a push inside `if …; then …; fi`
	was never judged. It is now blocked like a bare push."""
	repo, _, stub_bin, _, _ = worktree_repo
	proc = _run_twin_hook(repo, stub_bin, "if true; then git push origin HEAD:feature/x; fi")
	assert proc.returncode == 2, proc.stdout + proc.stderr
	assert "pull/41" in proc.stderr


def test_structurally_equal_targets_are_judged_once(monkeypatch, tmp_path: Path) -> None:
	"""PR #5173 review (round 9, rejected finding): two equal targets are the
	same question about the same repository state (the hook runs before the
	command), so the second judgement could not differ; it is skipped."""
	judged: list[tuple] = []

	def fake_judge(target, session_cwd, memo):
		judged.append(target)
		return 0, "", "", "", ""

	monkeypatch.setattr(twin_guard, "_judge_guard_target", fake_judge)
	monkeypatch.delenv("CLAUDE_PR_MERGE_GUARD", raising=False)
	code, _message = twin_guard.evaluate(
		{"tool_name": "Bash", "tool_input": {"command": "git commit -m a && git commit -m b"}, "cwd": str(tmp_path)}
	)
	assert code == 0
	assert len(judged) == 1


def test_git_dir_option_with_equals_keeps_its_tilde_literal_as_bash_does(tmp_path: Path) -> None:
	"""PR #5173 review (round 9, rejected finding): Bash expands a `~` after
	`=` only in an assignment word (`NAME=~/x`); `--git-dir=~/wt` is not one,
	so git receives the `~` as written."""
	proc = subprocess.run(
		["bash", "-c", 'printf "%s" --git-dir=~/wt'],
		capture_output=True,
		text=True,
		env={**os.environ, "HOME": str(tmp_path)},
		check=True,
	)
	assert proc.stdout == "--git-dir=~/wt"
