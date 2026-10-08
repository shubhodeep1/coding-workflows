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
import shlex
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
_WORKER_ACCOUNT_ID = "a" * 32
_OTHER_WORKER_ACCOUNT_ID = "b" * 32
_WORKER_URL = f"https://api.cloudflare.com/client/v4/accounts/{_WORKER_ACCOUNT_ID}/workers/scripts/name"


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
		'if true; then echo "git push origin"; fi',
		"man git commit",
		"grep -rn 'git push' scripts/",
		"ls -la",
		"",
	],
)
def test_unguarded_commands_are_ignored(command: str) -> None:
	assert not (guard.git_subcommands(command) & guard.GUARDED_SUBCOMMANDS)


@pytest.mark.parametrize("command", [
	">/tmp/guard-out git commit -m x",
	"2>/dev/null git push origin HEAD:feature/x",
	"2>&1 git push origin HEAD:feature/x",
	"VAR=1 2>&1 git push origin HEAD:feature/x",
	"FOO=bar 2>/dev/null git commit -m x",
])
def test_leading_redirection_preserves_guarded_command(command: str) -> None:
	assert guard.git_subcommands(command) & guard.GUARDED_SUBCOMMANDS


def test_unbalanced_quotes_do_not_raise() -> None:
	assert guard.git_subcommands("git commit -m 'unterminated") == set()


@pytest.mark.parametrize("command", [
	"if true; then git push origin; fi",
	"if git push origin; then :; fi",
	"if ! git push origin; then :; fi",
	"if ! ! git push origin; then :; fi",
	"! git push origin",
	"if true; then if git push origin; then :; fi; fi",
	"for item in a b; do git push origin; done",
	"case x in x) git push origin;; esac",
	"case x in\nx) git push origin;; esac",
	"case y in x) :;; y) git push origin;; esac",
	"case x in x) if ! git push origin; then :; fi;; esac",
])
def test_shell_control_words_do_not_hide_push(command: str) -> None:
	assert "push" in guard.git_subcommands(command)


@pytest.mark.parametrize(("command", "expected"), [
	("git push origin 123 > /dev/null", ["git", "push", "origin", "123"]),
	("git push origin 123 >/dev/null", ["git", "push", "origin", "123"]),
	("git push origin 123 2>&1", ["git", "push", "origin", "123"]),
	("git push origin 7 &>/dev/null", ["git", "push", "origin", "7"]),
	('git push origin "12">x', ["git", "push", "origin", "12"]),
	('git push origin "12">x; echo 12>out', ["git", "push", "origin", "12"]),
	('git push origin "12">x 12>out', ["git", "push", "origin", "12"]),
	("git push origin 12 >x; echo 12>out", ["git", "push", "origin", "12"]),
	("git push origin 2>/dev/null", ["git", "push", "origin"]),
	("git push origin 123>x", ["git", "push", "origin"]),
	("git push origin ² > x", ["git", "push", "origin", "²"]),
	("git push origin 2 2>&2>/dev/null", ["git", "push", "origin", "2"]),
	("git push origin 3 2>&3>/dev/null", ["git", "push", "origin", "3"]),
	("git push origin 2 >&2>f", ["git", "push", "origin", "2"]),
	("git push origin 12 2>12>x", ["git", "push", "origin", "12"]),
	("git push origin 2 2<&2<in", ["git", "push", "origin", "2"]),
	("git push origin 2 2>&2 >/dev/null", ["git", "push", "origin", "2"]),
])
def test_numeric_push_target_before_redirect(command: str, expected: list[str]) -> None:
	assert guard._shell_segments_with_operators(command)[0] == ("", expected)


@pytest.mark.parametrize(
	"command,expected_branch",
	[
		("git push origin 2 > /tmp/push.log", "2"),
		('git push origin "2" > /tmp/push.log', "2"),
		('git push origin "2"> /tmp/push.log', "2"),
		("git push origin 2> /tmp/push.log", ""),
		("git push origin \\2> /tmp/push.log", "2"),
		("git push origin 2 2>err >out", "2"),
		("git push origin 2 2>err 2>out", "2"),
		("git push origin 2 >out 2>err", "2"),
		("git push origin 2>err >out", ""),
	],
)
def test_redirection_keeps_numeric_push_refspecs(command: str, expected_branch: str) -> None:
	invocations = guard._guarded_git_invocations(command, str(REPO_ROOT))
	assert len(invocations) == 1
	assert guard._push_targets(invocations[0], str(REPO_ROOT))[0].branch == expected_branch


@pytest.mark.parametrize(
	"command",
	[
		f'curl -q -sS -X POST {_WORKER_URL} --header="Authorization: Bearer ${{CF_TOKEN}}" --data-binary=@worker.js',
		f"curl -q -sS -X PUT {_WORKER_URL}/content -H 'Content-Type: text/javascript' -d @w.js",
		f"curl -q -sS -X PATCH {_WORKER_URL}/settings -d 'prices $(USD) use `literal` markers'",
		f"curl -q -sS -X POST {_WORKER_URL.replace(_WORKER_ACCOUNT_ID, _WORKER_ACCOUNT_ID.upper())} -d @worker.js",
	],
)
def test_canonical_api_writes_do_not_request_extra_confirmation(command: str, monkeypatch) -> None:
	monkeypatch.setenv("FT_GAMES_CF", f"{_WORKER_ACCOUNT_ID}:tok")
	monkeypatch.delenv("FUNTOKEN_IO_CF", raising=False)
	assert not guard._api_write_requires_confirmation(command)


@pytest.mark.parametrize("command", [
	'curl -q -sS -X PUT https://api.digitalocean.com/v2/apps/id -H "Authorization: Bearer ${DIGITALOCEAN_ACCESS_TOKEN}" -d @spec.json',
	'curl -q -sS -X POST https://api.digitalocean.com/v2/apps/id -d @spec.json',
	'curl -q -sS -X PATCH https://api.digitalocean.com/v2/apps/id -d \'{"method":"-X DELETE"}\'',
	"curl -q -sS -X POST https://api.cloudflare.com/client/v4/workers -d 'prices $(USD) use `literal` markers'",
	"curl -q -sS -X POST https://api.cloudflare.com/client/v4/~health -H 'Authorization: Bearer token'",
	"curl -q -sS -X POST https://api.cloudflare.com/client/v4/accounts/id/workers/scripts/name -d @worker.js",
	"curl -q -sS -X POST https://api.cloudflare.com/client/v4/zones/zone/dns_records -d @zone.json",
	"curl -q -sS -X PATCH https://api.cloudflare.com/client/v4/zones/zone/settings -d @zone.json",
	"curl -q -sS -X POST https://api.cloudflare.com/client/v4/zones/zone/workers/routes -d @routes.json",
	f"curl -q -sS -X POST {_WORKER_URL.replace(_WORKER_ACCOUNT_ID, _OTHER_WORKER_ACCOUNT_ID)} -d @worker.js",
	f"curl -q -sS -X PUT {_WORKER_URL}/secrets -d @secret.json",
	f"curl -q -sS -X PUT {_WORKER_URL}/secrets/NAME -d @secret.json",
	f"curl -q -sS -X PUT {_WORKER_URL}/../../../zones/zone/dns_records -d @zone.json",
	f"curl -q -sS -X PUT {_WORKER_URL}/%2e%2e -d @worker.js",
	f"curl -q -sS -X PUT {_WORKER_URL}?x=1 -d @worker.js",
	f"curl -q -sS -X PUT {_WORKER_URL}/ -d @worker.js",
	f"curl -q -sS -X PUT {_WORKER_URL.replace('/workers/scripts/name', '/workers/domains')} -d @worker.js",
	f"curl -q -sS -X PUT {_WORKER_URL.replace('/workers/scripts/name', '/members')} -d @worker.js",
])
def test_api_write_destination_requires_confirmation(command: str, monkeypatch) -> None:
	monkeypatch.setenv("FT_GAMES_CF", f"{_WORKER_ACCOUNT_ID}:tok")
	monkeypatch.delenv("FUNTOKEN_IO_CF", raising=False)
	assert guard._api_write_requires_confirmation(command)


@pytest.mark.parametrize("credential", [None, _WORKER_ACCOUNT_ID, "a" * 31 + ":tok", _WORKER_ACCOUNT_ID + ":"])
def test_worker_write_requires_well_formed_credential(credential: str | None, monkeypatch) -> None:
	monkeypatch.delenv("FT_GAMES_CF", raising=False)
	monkeypatch.delenv("FUNTOKEN_IO_CF", raising=False)
	if credential is not None:
		monkeypatch.setenv("FT_GAMES_CF", credential)
	assert guard._api_write_requires_confirmation(f"curl -q -sS -X POST {_WORKER_URL} -d @worker.js")


def test_worker_write_accepts_either_matching_account(monkeypatch) -> None:
	monkeypatch.setenv("FT_GAMES_CF", "malformed")
	monkeypatch.setenv("FUNTOKEN_IO_CF", f"{_WORKER_ACCOUNT_ID}:tok")
	assert not guard._api_write_requires_confirmation(f"curl -q -sS -X POST {_WORKER_URL} -d @worker.js")


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
		f"curl -q -sS -X POST {_WORKER_URL} -X DELETE",
		f"curl -q -sS -X POST {_WORKER_URL} --url https://example.com/",
		f"curl -q -sS -X POST {_WORKER_URL} -d @worker.js; echo done",
	],
)
def test_noncanonical_api_writes_request_confirmation(command: str, monkeypatch) -> None:
	monkeypatch.setenv("FT_GAMES_CF", f"{_WORKER_ACCOUNT_ID}:tok")
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


@pytest.mark.parametrize(("url", "expected_reason"), [
	("https://api.digitalocean.com/v2/apps/id", "CLAUDE.md §22.B"),
	(_WORKER_URL.replace(_WORKER_ACCOUNT_ID, _OTHER_WORKER_ACCOUNT_ID), "FUNTOKEN_IO_CF or FT_GAMES_CF"),
	(f"{_WORKER_URL}/secrets/NAME", "Worker secret writes need approval"),
])
def test_api_write_reason_emits_ask_decision(url: str, expected_reason: str, monkeypatch, capsys) -> None:
	monkeypatch.setenv("FT_GAMES_CF", f"{_WORKER_ACCOUNT_ID}:tok")
	monkeypatch.delenv("FUNTOKEN_IO_CF", raising=False)
	command = f"curl -q -sS -X PUT {url} -d @body.json"
	assert guard.evaluate({"tool_name": "Bash", "tool_input": {"command": command}}) == (0, "")
	output = json.loads(capsys.readouterr().out)
	assert output["hookSpecificOutput"]["permissionDecision"] == "ask"
	assert expected_reason in output["hookSpecificOutput"]["permissionDecisionReason"]


def test_api_write_reason_does_not_leak_credential(monkeypatch, capsys) -> None:
	monkeypatch.setenv("FT_GAMES_CF", f"{_WORKER_ACCOUNT_ID}:SENTINELTOKEN")
	command = f"curl -q -sS -X PUT {_WORKER_URL}/secrets -d @body.json"
	guard.evaluate({"tool_name": "Bash", "tool_input": {"command": command}})
	assert "SENTINELTOKEN" not in capsys.readouterr().out


def test_api_write_confirmation_ignores_merge_guard_kill_switch(monkeypatch, capsys) -> None:
	monkeypatch.setenv("CLAUDE_PR_MERGE_GUARD", "off")
	command = "curl -q -sS -X POST https://api.cloudflare.com/client/v4/zones/zone/dns_records -d @body.json"
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


def test_numeric_branch_before_spaced_redirect_remains_a_refspec(merged_branch_repo, monkeypatch) -> None:
	repo, _ = merged_branch_repo
	merged_sha = _git(repo, "rev-parse", "HEAD")
	_git(repo, "branch", "123", "feature/x")
	_git(repo, "checkout", "main")
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)
	monkeypatch.setattr(guard, "query_pull_requests", lambda slug, branch, cwd: [dict(MERGED_PR, headRefOid=merged_sha)])
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": "git push origin 123 > /dev/null"}})
	assert code == 2 and "Branch `123`" in message


@pytest.mark.parametrize("command,checkout_branch", [
	("git push origin 123&>/dev/null", "main"),
	("git push origin 123&>>/dev/null", "main"),
	("git push origin HEAD:123&>/dev/null", "feature/x"),
])
def test_numeric_branch_before_ampersand_redirect_is_checked(
	merged_branch_repo, monkeypatch, command: str, checkout_branch: str
) -> None:
	repo, _ = merged_branch_repo
	merged_sha = _git(repo, "rev-parse", "HEAD")
	_git(repo, "branch", "123", "feature/x")
	_git(repo, "checkout", checkout_branch)
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)
	monkeypatch.setattr(guard, "query_pull_requests", lambda slug, branch, cwd: [dict(MERGED_PR, headRefOid=merged_sha)])
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": command}})
	assert code == 2 and "Branch `123`" in message


@pytest.mark.parametrize("command,arguments", [
	("git push origin 123&>f", ["git", "push", "origin", "123"]),
	("git push origin 123&>>f", ["git", "push", "origin", "123"]),
	("git push origin 2>&1", ["git", "push", "origin"]),
	("git push origin 123>&2", ["git", "push", "origin"]),
	("git push origin 123>>f", ["git", "push", "origin"]),
	("git push origin 123>|f", ["git", "push", "origin"]),
	("git push origin 123 >|f", ["git", "push", "origin", "123"]),
	("git push origin >|f", ["git", "push", "origin"]),
	("git push origin 123<>f", ["git", "push", "origin"]),
])
def test_fd_prefix_only_removed_for_supported_redirects(command: str, arguments: list[str]) -> None:
	assert guard._shell_segments_with_operators(command) == [("", arguments)]


@pytest.mark.parametrize("command,expected", [
	("cd /tmp >/missing", True),
	("cd /tmp 2>/dev/null", False),
	("cd /tmp &>/dev/null", False),
	("exit 2>&1", True),
	("exit <<<x", True),
	("cd /tmp >", True),
	("cd /tmp >; git push origin", True),
	("cd /tmp", False),
])
def test_redirect_uncertainty_on_shell_segments(command: str, expected: bool) -> None:
	segments = guard._shell_segments_with_redirects(command)
	assert segments[0][2] is expected
	if len(segments) > 1:
		assert segments[1][2] is False


def test_clobber_redirect_checks_current_branch(merged_branch_repo, monkeypatch) -> None:
	repo, _ = merged_branch_repo
	merged_sha = _git(repo, "rev-parse", "HEAD")
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)
	monkeypatch.setattr(guard, "query_pull_requests", lambda slug, branch, cwd: [dict(MERGED_PR, headRefOid=merged_sha)] if branch == "feature/x" else [])
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": "git push origin 123>|/dev/null"}})
	assert code == 2 and "Branch `feature/x`" in message


@pytest.mark.parametrize("command", [
	'git push origin 123&>/dev/null\n"unterminated',
	'g\'\'it push origin 123&>/dev/null\n"unterminated',
	'g""it push origin 123&>/dev/null\n"unterminated',
	'g\\i\\t pu\'\'sh origin 123&>/dev/null\n"unterminated',
])
def test_malformed_later_line_requests_confirmation_for_redirected_push(command: str, capsys) -> None:
	assert guard.git_subcommands(command) == set()
	assert guard.evaluate({"tool_name": "Bash", "tool_input": {"command": command}}) == (0, "")
	decision = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
	assert decision["permissionDecision"] == "ask"


def test_unparsable_non_git_command_requests_confirmation(capsys) -> None:
	assert guard.evaluate({"tool_name": "Bash", "tool_input": {"command": 'pwd\n"unterminated'}}) == (0, "")
	decision = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
	assert decision["permissionDecision"] == "ask"


def _worktree_pr_stub(stub_bin: Path, merged_sha: str) -> None:
	"""Answer one open branch and one merged branch from the same REST listing."""
	merged = _merged_pr_payload(merged_sha)
	open_pr = json.dumps([{
		"number": 42, "state": "open", "html_url": "https://github.com/o/r/pull/42",
		"title": "open worktree", "merged_at": None, "head": {"sha": merged_sha},
	}])
	stub = stub_bin / "gh"
	stub.write_text(
		f"#!/bin/sh\ncase \"$*\" in\n  *feature/open*) printf '%s\\n' '{open_pr}' ;;\n"
		f"  *) printf '%s\\n' '{merged}' ;;\nesac\n", encoding="utf-8"
	)
	stub.chmod(0o755)


def test_worktree_push_to_open_branch_from_merged_checkout(merged_branch_repo) -> None:
	repo, stub_bin = merged_branch_repo
	merged_sha = _git(repo, "rev-parse", "HEAD")
	worktree = repo.parent / "detached"
	_git(repo, "worktree", "add", "--detach", str(worktree), "feature/x")
	_worktree_pr_stub(stub_bin, merged_sha)
	proc = _run_hook(repo, stub_bin, f"cd {worktree} && git push origin HEAD:feature/open")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "skipped" not in proc.stdout


@pytest.mark.parametrize("prefix", ["cd {worktree} && git", "git -C {worktree}",
	"GIT_DIR={worktree}/.git GIT_WORK_TREE={worktree} git"])
def test_worktree_push_to_merged_branch_from_other_checkout(merged_branch_repo, prefix: str) -> None:
	repo, stub_bin = merged_branch_repo
	merged_sha = _git(repo, "rev-parse", "HEAD")
	worktree = repo.parent / "other"
	_git(repo, "worktree", "add", "-b", "feature/open", str(worktree), "feature/x")
	_git(repo, "checkout", "main")
	_worktree_pr_stub(stub_bin, merged_sha)
	command = f"{prefix.format(worktree=worktree)} push origin HEAD:feature/x"
	proc = _run_hook(repo, stub_bin, command)
	assert proc.returncode == 2, proc.stdout + proc.stderr
	assert "Branch `feature/x`" in proc.stderr


def test_git_dash_c_allows_detached_worktree_open_destination(merged_branch_repo) -> None:
	repo, stub_bin = merged_branch_repo
	merged_sha = _git(repo, "rev-parse", "HEAD")
	worktree = repo.parent / "detached"
	_git(repo, "worktree", "add", "--detach", str(worktree), "feature/x")
	_worktree_pr_stub(stub_bin, merged_sha)
	proc = _run_hook(repo, stub_bin, f"git -C {worktree} push origin HEAD:feature/open")
	assert proc.returncode == 0, proc.stdout + proc.stderr


def test_git_dir_allows_detached_worktree_open_destination(merged_branch_repo) -> None:
	repo, stub_bin = merged_branch_repo
	merged_sha = _git(repo, "rev-parse", "HEAD")
	worktree = repo.parent / "detached"
	_git(repo, "worktree", "add", "--detach", str(worktree), "feature/x")
	_worktree_pr_stub(stub_bin, merged_sha)
	proc = _run_hook(repo, stub_bin,
		f"GIT_DIR={worktree}/.git GIT_WORK_TREE={worktree} git push origin HEAD:feature/open")
	assert proc.returncode == 0, proc.stdout + proc.stderr


def test_commit_in_merged_worktree_is_not_judged_from_main_checkout(merged_branch_repo) -> None:
	repo, stub_bin = merged_branch_repo
	_git(repo, "checkout", "main")
	worktree = repo.parent / "merged"
	_git(repo, "worktree", "add", str(worktree), "feature/x")
	proc = _run_hook(repo, stub_bin, f"git -C {worktree} commit -m next")
	assert proc.returncode == 2, proc.stdout + proc.stderr
	assert "Branch `feature/x`" in proc.stderr


def test_unresolvable_worktree_falls_back_to_checkout_with_warning(merged_branch_repo) -> None:
	"""Legacy name: unresolved worktrees now require confirmation, not checkout fallback."""
	repo, stub_bin = merged_branch_repo
	proc = _run_hook(repo, stub_bin, "cd $WT && git push origin HEAD:feature/open")
	assert proc.returncode == 2, proc.stdout + proc.stderr
	assert "Branch `feature/x`" in proc.stderr
	response = json.loads(proc.stdout)
	assert "could not resolve git command directory" in response["systemMessage"]
	assert "hookSpecificOutput" not in response


def test_unresolvable_worktree_push_asks_when_checkout_is_main(merged_branch_repo) -> None:
	repo, stub_bin = merged_branch_repo
	_git(repo, "checkout", "main")
	proc = _run_hook(repo, stub_bin, "cd $WT && git push origin HEAD")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	response = json.loads(proc.stdout)
	assert response["hookSpecificOutput"]["permissionDecision"] == "ask"
	assert "could not resolve git command directory" in response["systemMessage"]
	assert "could not resolve git push repository" in response["systemMessage"]


@pytest.mark.parametrize("override", ["GIT_DIR", "GIT_WORK_TREE"])
def test_appended_git_override_falls_back_without_using_rhs(merged_branch_repo, monkeypatch, override: str) -> None:
	# Finding #6305 (re-issue #6638): the appended value selects a repository
	# the hook cannot see, so the push asks and the session checkout's PR
	# history is never consulted, even though that checkout is merged.
	repo, _ = merged_branch_repo
	worktree = repo.parent / "open"
	_git(repo, "worktree", "add", "-b", "feature/open", str(worktree), "main")
	value = worktree / ".git" if override == "GIT_DIR" else worktree
	command = f"{override}+={value} git push origin HEAD"
	invocations = guard._guarded_git_invocations(command, str(repo))
	assert len(invocations) == 1
	assert invocations[0].environment == {}
	assert invocations[0].explicit_directory_unresolved
	assert invocations[0].warning == "could not resolve explicit git push directory; cannot check checkout PR history"
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: pytest.fail("must not check the session checkout"))
	monkeypatch.setattr(guard, "query_pull_requests", lambda *args: pytest.fail("must not check the session checkout"))
	ask: list[str] = []
	monkeypatch.setattr(guard, "_request_confirmation", lambda reason, prompt_reason=None: ask.append(reason))
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": command}})
	assert (code, message) == (0, "")
	assert ask and "could not resolve explicit git push directory" in ask[0]
	assert "checking the session checkout instead" not in ask[0]


@pytest.mark.parametrize("override", ["GIT_DIR", "GIT_WORK_TREE"])
def test_appended_git_override_exploit_asks_from_default_branch_checkout(merged_branch_repo, monkeypatch, override: str) -> None:
	"""The exact #6305 command: a default-branch session checkout, an appended
	override naming the merged repository. Before the fix the session checkout
	(clean, on main) was checked in place of the override and the push passed."""
	repo, _ = merged_branch_repo
	_git(repo, "checkout", "main")
	value = repo / ".git" if override == "GIT_DIR" else repo
	monkeypatch.setattr(guard, "query_pull_requests", lambda *args: pytest.fail("must not check the session checkout"))
	ask: list[str] = []
	monkeypatch.setattr(guard, "_request_confirmation", lambda reason, prompt_reason=None: ask.append(reason))
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": f"{override}+={value} git push origin HEAD"}})
	assert (code, message) == (0, "")
	assert ask, "the push must ask, never pass silently"
	assert "could not resolve explicit git push directory" in ask[0]


@pytest.mark.parametrize("command", [
	"if true; then git -C relative-dir push origin HEAD; fi",
	"env -C relative-dir git push origin HEAD",
	"GIT_DIR=relative.git git push origin HEAD",
])
def test_unresolved_explicit_push_override_asks_without_checking_checkout(merged_branch_repo, monkeypatch, command: str) -> None:
	repo, _ = merged_branch_repo
	_git(repo, "checkout", "main")
	monkeypatch.setattr(guard, "query_pull_requests", lambda *args: pytest.fail("must not check the session checkout"))
	ask: list[str] = []
	monkeypatch.setattr(guard, "_request_confirmation", lambda reason, prompt_reason=None: ask.append(reason))
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": command}})
	assert (code, message) == (0, "")
	assert ask, command
	assert "could not resolve explicit git push directory" in ask[0], ask[0]


@pytest.mark.parametrize("override", ["GIT_DIR", "GIT_WORK_TREE"])
def test_appended_git_override_asks_when_checkout_is_not_merged(merged_branch_repo, override: str) -> None:
	repo, stub_bin = merged_branch_repo
	worktree = repo.parent / "merged"
	_git(repo, "checkout", "main")
	_git(repo, "worktree", "add", str(worktree), "feature/x")
	value = worktree / ".git" if override == "GIT_DIR" else worktree
	proc = _run_hook(repo, stub_bin, f"{override}+={value} git push origin HEAD")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	response = json.loads(proc.stdout)
	assert response["hookSpecificOutput"]["permissionDecision"] == "ask"
	assert "could not resolve explicit git push directory" in response["systemMessage"]
	assert "checking the session checkout instead" not in response["systemMessage"]


def test_explicit_source_tip_and_multiple_destinations(merged_branch_repo) -> None:
	repo, stub_bin = merged_branch_repo
	merged_sha = _git(repo, "rev-parse", "HEAD")
	_git(repo, "checkout", "main")
	_worktree_pr_stub(stub_bin, merged_sha)
	# Neither HEAD nor the current branch carries the merged work. The source
	# feature/x does, so it must determine ancestry for both destinations.
	proc = _run_hook(repo, stub_bin,
		"git push origin feature/x:feature/open feature/x:feature/x")
	assert proc.returncode == 2, proc.stdout + proc.stderr
	assert "Branch `feature/x`" in proc.stderr


def test_repeated_destination_uses_one_pr_listing(merged_branch_repo, monkeypatch) -> None:
	repo, _ = merged_branch_repo
	merged_sha = _git(repo, "rev-parse", "HEAD")
	calls: list[tuple[str, str]] = []
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)
	def listing(slug, branch, cwd):
		calls.append((slug, branch))
		return [dict(MERGED_PR, headRefOid=merged_sha)]
	monkeypatch.setattr(guard, "query_pull_requests", listing)
	code, _ = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": "git push origin HEAD:feature/x HEAD:feature/x"}})
	assert code == 2
	assert calls == [("o/r", "feature/x")]


def test_same_destination_different_sources_checks_each_tip(merged_branch_repo, monkeypatch) -> None:
	repo, _ = merged_branch_repo
	merged_sha = _git(repo, "rev-parse", "HEAD")
	main_sha = _git(repo, "rev-parse", "main")
	calls: list[str] = []
	checked_tips: list[str] = []
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)
	original_blocking_pull_request = guard.blocking_pull_request
	def check_tip(pull_requests, cwd, base, tip):
		checked_tips.append(tip)
		return original_blocking_pull_request(pull_requests, cwd, base, tip)
	monkeypatch.setattr(guard, "blocking_pull_request", check_tip)
	def listing(slug, branch, cwd):
		calls.append(branch)
		return [dict(MERGED_PR, headRefOid=merged_sha)]
	monkeypatch.setattr(guard, "query_pull_requests", listing)
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": "git push origin main:feature/x feature/x:feature/x"}})
	assert code == 2
	assert "Branch `feature/x`" in message
	assert calls == ["feature/x"]
	assert checked_tips == [main_sha, merged_sha]


def test_cached_merged_pr_is_rechecked_once_for_repeated_targets(merged_branch_repo, monkeypatch) -> None:
	repo, _ = merged_branch_repo
	merged_sha = _git(repo, "rev-parse", "HEAD")
	calls: list[str] = []
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: [dict(MERGED_PR, headRefOid=merged_sha)])
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)
	def listing(slug, branch, cwd):
		calls.append(branch)
		return [dict(MERGED_PR, headRefOid=merged_sha), OPEN_PR]
	monkeypatch.setattr(guard, "query_pull_requests", listing)
	code, _ = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": "git push origin HEAD:feature/x HEAD:feature/x"}})
	assert code == 0
	assert calls == ["feature/x"]


@pytest.mark.parametrize("options", ["--delete --no-delete", "--tags --no-tags", "-uo ci.skip"])
def test_negated_options_and_value_options_do_not_hide_a_push(merged_branch_repo, options: str) -> None:
	repo, stub_bin = merged_branch_repo
	proc = _run_hook(repo, stub_bin, f"git push {options} origin HEAD:feature/x")
	assert proc.returncode == 2, proc.stdout + proc.stderr


def test_conditional_cd_outside_its_list_warns_and_uses_checkout(merged_branch_repo) -> None:
	repo, stub_bin = merged_branch_repo
	worktree = repo.parent / "other"
	_git(repo, "worktree", "add", "-b", "feature/open", str(worktree), "main")
	proc = _run_hook(repo, stub_bin,
		f"false && cd {worktree}; git push origin HEAD:feature/open")
	assert proc.returncode == 2, proc.stdout + proc.stderr
	assert "could not resolve git command directory" in proc.stdout


@pytest.mark.parametrize("command", [
	"git push origin HEAD",
	"COUNT+=1 git push origin HEAD:feature/x",
	"git push --repo=origin",
	"git push --repo origin origin HEAD:feature/x",
	"git push --repo=origin origin HEAD:feature/x",
	"git push --repo=upstream origin",
	"git push origin --repo=upstream",
	"git push origin HEAD:feature/x --repo=upstream",
	"git push --repo=upstream origin HEAD:feature/x",
	"git push origin HEAD~0:feature/x",
	"git push origin HEAD:feature/x 2>&1",
])
def test_push_parser_guards_real_destination(merged_branch_repo, monkeypatch, command: str) -> None:
	repo, _ = merged_branch_repo
	merged_sha = _git(repo, "rev-parse", "HEAD")
	lookups: list[str] = []
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)
	def listing(slug, branch, cwd):
		lookups.append(branch)
		return [dict(MERGED_PR, headRefOid=merged_sha)] if branch == "feature/x" else []
	monkeypatch.setattr(guard, "query_pull_requests", listing)
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": command}})
	assert code == 2, message
	assert lookups == ["feature/x"]


@pytest.mark.parametrize("refspec,command", [
	("2", "git push origin 2 > /tmp/out"),
	("2", "git push origin 2 3>/tmp/out"),
	("2", "git push origin 2&>/tmp/out"),
	("12", "git push origin 12&>>/tmp/out"),
	("2", "git push origin ''2>/dev/null"),
	("2", 'git push origin ""2>/dev/null'),
	("12", "git push origin ''12>/tmp/out"),
	("2", "git push origin ''2&>/tmp/out"),
	("123", 'git push origin ""123>>/dev/null'),
	("2", "git push origin '2'>/tmp/out"),
	("12", "git push origin ''12>/dev/null"),
	("2", "git push origin ''2>&1"),
	("2", "git push origin \\2>/tmp/out"),
	("12", "git push origin \\12>/tmp/out"),
	("12", "git push origin '1'2>/tmp/out"),
	("123", "git push origin 123 > /dev/null"),
	("123", 'git push origin "123">/dev/null'),
])
def test_numeric_push_refspec_before_redirect_is_checked(merged_branch_repo, monkeypatch,
	refspec: str, command: str) -> None:
	repo, _ = merged_branch_repo
	_git(repo, "branch", refspec)
	_git(repo, "checkout", "main")
	merged_sha = _git(repo, "rev-parse", refspec)
	lookups: list[str] = []
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)
	def listing(slug, branch, cwd):
		lookups.append(branch)
		return [dict(MERGED_PR, headRefOid=merged_sha)] if branch == refspec else []
	monkeypatch.setattr(guard, "query_pull_requests", listing)
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": command}})
	assert code == 2, message
	assert lookups == [refspec]


def test_empty_quoted_numeric_refspec_is_not_an_fd() -> None:
	quoted = guard._guarded_git_invocations("git push origin ''2>/dev/null", "/repo")
	assert len(quoted) == 1
	assert quoted[0].arguments == ["origin", "2"]
	bare = guard._guarded_git_invocations("git push origin 2>/dev/null", "/repo")
	assert len(bare) == 1
	assert bare[0].arguments == ["origin"]
	assert guard._shell_segments_with_operators("git status;2>/dev/null") == [
		("", ["git", "status"]),
	]


@pytest.mark.parametrize("command", [
	"git push origin ''2>/dev/null",
	'git push origin ""2>/dev/null',
])
def test_empty_quoted_numeric_word_is_kept_as_refspec(command: str) -> None:
	invocations = guard._guarded_git_invocations(command, "/repo")
	assert len(invocations) == 1
	assert invocations[0].arguments == ["origin", "2"]


@pytest.mark.parametrize("command", [
	"git push origin HEAD:feature/open 2>/dev/null",
	"git push origin HEAD:feature/open 2>&1",
	"git push origin HEAD:feature/open 12>/dev/null",
	"git push origin HEAD:feature/open 2>out 2>&1",
	"git push origin HEAD:feature/open\t2>/dev/null",
])
def test_adjacent_fd_redirect_does_not_create_numeric_refspec(command: str) -> None:
	invocations = guard._guarded_git_invocations(command, "/repo")
	assert len(invocations) == 1
	assert invocations[0].arguments == ["origin", "HEAD:feature/open"]


@pytest.mark.parametrize("command", [
	">/tmp/guard-out git commit -m x",
	">/tmp/guard-out git push origin HEAD:feature/x",
	"2>&1 git push origin HEAD:feature/x",
	"VAR=1 2>&1 git push origin HEAD:feature/x",
	"FOO=bar 2>/dev/null git commit -m x",
])
def test_leading_redirection_still_blocks_merged_branch(merged_branch_repo, monkeypatch, command: str) -> None:
	repo, _ = merged_branch_repo
	merged_sha = _git(repo, "rev-parse", "HEAD")
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)
	monkeypatch.setattr(guard, "query_pull_requests", lambda *args: [dict(MERGED_PR, headRefOid=merged_sha)])
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": command}})
	assert code == 2 and "Branch `feature/x`" in message


def test_numeric_refspec_before_redirection_still_checks_merged_branch(merged_branch_repo, monkeypatch) -> None:
	repo, _ = merged_branch_repo
	_git(repo, "branch", "2", "HEAD")
	merged_sha = _git(repo, "rev-parse", "HEAD")
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)
	monkeypatch.setattr(guard, "query_pull_requests", lambda slug, branch, cwd:
		[dict(MERGED_PR, headRefOid=merged_sha)] if branch == "2" else [])
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": "git push origin 2 > /dev/null"}})
	assert code == 2 and "Branch `2`" in message


@pytest.mark.parametrize("redirect", ["2 > /dev/null", "'2'>/dev/null", "\\2>/dev/null"])
def test_ambiguous_numeric_redirect_asks_if_branch_is_safe(merged_branch_repo, monkeypatch, capsys, redirect: str) -> None:
	repo, _ = merged_branch_repo
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	monkeypatch.setattr(guard, "query_pull_requests", lambda slug, branch, cwd: [])
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": f"git push origin {redirect}"}})
	assert code == 0 and message == ""
	decision = json.loads(capsys.readouterr().out.splitlines()[-1])
	assert decision["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize("command", [
	"git push origin 2>/dev/null",
	"git push -u origin feature/x 2>&1 | tail -1",
	"git push origin HEAD:feature/y 2>&1",
])
def test_glued_fd_redirect_after_push_needs_no_confirmation(merged_branch_repo, monkeypatch, capsys, command: str) -> None:
	# Bash reads digits glued to `<`/`>` as an IO number, never as a refspec.
	repo, _ = merged_branch_repo
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)
	monkeypatch.setattr(guard, "query_pull_requests", lambda slug, branch, cwd: [])
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": command}})
	assert code == 0 and message == ""
	assert "permissionDecision" not in capsys.readouterr().out


def test_glued_fd_redirect_still_blocks_merged_branch(merged_branch_repo, monkeypatch) -> None:
	repo, _ = merged_branch_repo
	merged_sha = _git(repo, "rev-parse", "HEAD")
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)
	monkeypatch.setattr(guard, "query_pull_requests", lambda slug, branch, cwd:
		[dict(MERGED_PR, headRefOid=merged_sha)] if branch == "feature/x" else [])
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": "git push -u origin feature/x 2>&1 | tail -1"}})
	assert code == 2 and "Branch `feature/x`" in message


def test_unresolved_push_source_asks_without_checkout_lookup(merged_branch_repo, monkeypatch, capsys) -> None:
	repo, _ = merged_branch_repo
	monkeypatch.setattr(guard, "query_pull_requests", lambda *args: pytest.fail("unresolved source must not check checkout"))
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": "git push origin $SOURCE:feature/x"}})
	assert code == 0 and message == ""
	decision = json.loads(capsys.readouterr().out.splitlines()[-1])
	assert decision["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize("prefix", ["GIT_DIR+=other", "GIT_WORK_TREE+=other"])
def test_appended_git_directory_push_asks_when_checkout_is_safe(merged_branch_repo, monkeypatch, capsys, prefix: str) -> None:
	# Git applies the appended value on top of a shell state the hook cannot
	# read, so the push asks without checking the session checkout (finding
	# #6305, see test_appended_git_override_falls_back_without_using_rhs).
	repo, _ = merged_branch_repo
	monkeypatch.setattr(guard, "query_pull_requests", lambda *args: pytest.fail("must not check the session checkout"))
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": f"{prefix} git push origin HEAD:feature/x"}})
	assert code == 0 and message == ""
	decision = json.loads(capsys.readouterr().out.splitlines()[-1])
	assert decision["hookSpecificOutput"]["permissionDecision"] == "ask"
	assert "could not resolve explicit git push directory" in decision["systemMessage"]


@pytest.mark.parametrize("command", [
	"git push --repo=https://github.com/other/repo HEAD:feature/x",
	"git push --rep https://github.com/other/repo HEAD:feature/x",
	"git push https://github.com/other/repo HEAD:feature/x",
	"git push https://github.com/o/r.git HEAD:feature/x",
	"git push --repo=origin https://github.com/o/r.git HEAD:feature/x",
	"git push --repo=https://github.com/o/r.git",
	"git push git@github.com:o/r.git HEAD:feature/x",
	"git push --repo=upstream",
	"git push --repo upstream",
	"git push --rep upstream",
	"git push --repo=upstream HEAD:feature/x",
	"git push --repo=origin HEAD:feature/x",
	"git push --repo origin HEAD:feature/x",
	"git push --repo=https://github.com/other/repo --signed=if-asked HEAD:feature/x",
	"git push --repo=https://x-access-token:private@github.com/other/repo HEAD:feature/x",
])
def test_push_to_unverified_repository_requires_confirmation(merged_branch_repo, monkeypatch, capsys, command: str) -> None:
	repo, _ = merged_branch_repo
	monkeypatch.setattr(guard, "query_pull_requests", lambda *args: pytest.fail("must not query origin for another repository"))
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": command}})
	assert code == 0 and message == ""
	output = capsys.readouterr().out
	decision = json.loads(output.splitlines()[-1])
	assert decision["hookSpecificOutput"]["permissionDecision"] == "ask"
	assert "private" not in output
	if "github.com/o/r.git" in command:
		assert "explicit push URL may be rewritten" in output


@pytest.mark.parametrize("command", [
	"git -c remote.origin.url=https://github.com/other/repo push origin --delete feature/x",
	"git -c remote.origin.pushurl=https://github.com/other/repo push origin :feature/x",
	"git -c url.https://github.com/other/repo.insteadOf=https://github.com/o/r push origin HEAD:feature/x",
	"git -cremote.origin.url=https://github.com/other/repo push origin --tags",
	"git --config-env=remote.origin.url=REMOTE_URL push origin HEAD:feature/x",
	"git --config-env remote.origin.url=REMOTE_URL push origin HEAD:feature/x",
	"git -c user.name=bot push origin HEAD:feature/x",
])
def test_per_command_config_push_asks_without_using_origin_prs(merged_branch_repo, monkeypatch, capsys, command: str) -> None:
	repo, _ = merged_branch_repo
	monkeypatch.setattr(guard, "query_pull_requests", lambda *args: pytest.fail("overridden push must not query origin"))
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": command}})
	assert code == 0 and message == ""
	output = capsys.readouterr().out
	decision = json.loads(output.splitlines()[-1])
	assert decision["hookSpecificOutput"]["permissionDecision"] == "ask"
	assert "other/repo" not in output  # URLs may carry credentials; never print config values.


@pytest.mark.parametrize("command", [
	"GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.url GIT_CONFIG_VALUE_0=https://github.com/other/repo git push origin HEAD:feature/x",
	"GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.pushurl GIT_CONFIG_VALUE_0=https://github.com/other/repo git push origin --tags",
	"GIT_CONFIG_PARAMETERS='remote.origin.url=https://github.com/other/repo' git push origin HEAD:feature/x",
	"GIT_CONFIG_GLOBAL=/tmp/other-config git push origin :feature/x",
	"GIT_CONFIG_COUNT+=1 git push origin HEAD:feature/x",
	"GIT_CONFIG=/tmp/other-config git push origin HEAD:feature/x",
	"env GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.url GIT_CONFIG_VALUE_0=https://github.com/other/repo git push origin HEAD:feature/x",
	"/usr/bin/env -i GIT_CONFIG_GLOBAL=/tmp/other-config git push origin HEAD:feature/x",
	"env -u GIT_CONFIG_GLOBAL git push origin HEAD:feature/x",
	"env - git push origin HEAD:feature/x",
	"env FOO+=bar git push origin HEAD:feature/x",
	"env -S 'git push origin HEAD:feature/x'",
	"env --split-string='git push origin HEAD:feature/x'",
	"env -S 'GIT_CONFIG_COUNT=1 git push origin HEAD:feature/x'",
	"env -v git push origin HEAD:feature/x",
	"env -S 'git push origin ${TARGET}'",
])
def test_environment_config_push_asks_without_using_origin_prs(merged_branch_repo, monkeypatch, capsys, command: str) -> None:
	repo, _ = merged_branch_repo
	monkeypatch.setattr(guard, "query_pull_requests", lambda *args: pytest.fail("overridden push must not query origin"))
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": command}})
	assert code == 0 and message == ""
	decision = json.loads(capsys.readouterr().out.splitlines()[-1])
	assert decision["hookSpecificOutput"]["permissionDecision"] == "ask"
	assert "other/repo" not in json.dumps(decision)


@pytest.mark.parametrize("command", [
	"env GIT_CONFIG_COUNT=0 git commit -m x",
	"env - git commit -m x",
	"env FOO+=bar git commit -m x",
	"env -S 'git commit -m x'",
	"env --split-string='git commit -m x'",
])
def test_env_wrapped_commit_keeps_the_merged_pr_check(merged_branch_repo, monkeypatch, command: str) -> None:
	repo, _ = merged_branch_repo
	merged_sha = _git(repo, "rev-parse", "HEAD")
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)
	monkeypatch.setattr(guard, "query_pull_requests", lambda *args: [dict(MERGED_PR, headRefOid=merged_sha)])
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": command}})
	assert code == 2 and "Branch `feature/x`" in message


@pytest.mark.parametrize("wrapper", [
	"env -C {repo} git commit -m x",
	"env --chdir={repo} git commit -m x",
	"env GIT_DIR={repo}/.git GIT_WORK_TREE={repo} git commit -m x",
	"env -S 'GIT_DIR={repo}/.git git commit -m x'",
])
def test_env_wrapped_commit_checks_selected_repo(merged_branch_repo, monkeypatch, wrapper: str) -> None:
	repo, _ = merged_branch_repo
	other = repo.parent / "open-worktree"
	_git(repo, "worktree", "add", "-b", "feature/open", str(other), "main")
	merged_sha = _git(repo, "rev-parse", "feature/x")
	seen: list[tuple[str, str]] = []
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)
	def listing(slug, branch, cwd):
		seen.append((branch, cwd))
		return [dict(MERGED_PR, headRefOid=merged_sha)] if branch == "feature/x" else [OPEN_PR]
	monkeypatch.setattr(guard, "query_pull_requests", listing)
	command = wrapper.format(repo=repo)
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(other), "tool_input": {"command": command}})
	assert code == 2 and "Branch `feature/x`" in message
	# GIT_DIR selects the repository without changing process cwd; env -C
	# changes cwd but leaves the Git environment alone.
	assert seen == [("feature/x", str(other) if "GIT_DIR=" in command else str(repo))]


@pytest.mark.parametrize("command", [
	"env -C /does-not-exist git commit -m x",
	"env GIT_DIR=$REPO git commit -m x",
	"env -C . git -C $REPO commit -m x",
	"env -C/does-not-exist git commit -m x",
	"env --chdir=/does-not-exist git commit -m x",
	"env GIT_DIR=/does-not-exist git commit -m x",
	"git -C /does-not-exist commit -m x",
	'env -C "$OTHER_REPO" git commit -m x',
	"GIT_DIR=/does-not-exist git commit -m x",
	"GIT_DIR+=/does-not-exist git commit -m x",
	"GIT_WORK_TREE+=/does-not-exist git commit -m x",
	'cd "$WT" && git commit -m x',
	"cd $WT && git commit -m x",
	'if true; then cd "$WT" && git commit -m x; fi',
	"env -S 'git commit -m ${MESSAGE}'",
	"env -v git commit -m x",
])
def test_env_unresolved_commit_directory_asks_instead_of_checking_checkout(merged_branch_repo, monkeypatch, capsys, command: str) -> None:
	repo, _ = merged_branch_repo
	monkeypatch.setattr(guard, "query_pull_requests", lambda *args: pytest.fail("must not check the wrong checkout"))
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": command}})
	assert code == 0 and message == ""
	decision = json.loads(capsys.readouterr().out.splitlines()[-1])
	assert decision["hookSpecificOutput"]["permissionDecision"] == "ask"
	if "GIT_DIR=" in command:
		assert "could not resolve git commit directory" in decision["hookSpecificOutput"]["permissionDecisionReason"]
	assert "checking the session checkout instead" not in json.dumps(decision)
	if command.startswith("env -C") and " git -C " not in command:
		assert "no checkout was checked" in decision["hookSpecificOutput"]["permissionDecisionReason"]
		assert "merged-PR guard needs confirmation" in decision["systemMessage"]
	if command.startswith("env GIT_DIR="):
		assert "could not resolve git commit directory" in decision["hookSpecificOutput"]["permissionDecisionReason"]


def test_env_chdir_commit_still_asks_when_warning_text_changes(merged_branch_repo, monkeypatch, capsys) -> None:
	repo, _ = merged_branch_repo
	original = guard._guarded_git_invocations
	monkeypatch.setattr(guard, "_guarded_git_invocations", lambda command, checkout: [
		invocation._replace(warning="different wording") for invocation in original(command, checkout)
	])
	monkeypatch.setattr(guard, "query_pull_requests", lambda *args: pytest.fail("must not check the wrong checkout"))
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": "env -C /does-not-exist git commit -m x"}})
	assert code == 0 and message == ""
	assert json.loads(capsys.readouterr().out.splitlines()[-1])["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize("command", [
	"if true; then env FOO=bar git commit -m x; fi",
	"if true; then git -c user.name=bot commit -m x; fi",
	"if true; then GIT_CONFIG_COUNT=0 git commit -m x; fi",
])
def test_unrelated_override_does_not_prompt_for_shell_control_commit(merged_branch_repo, command: str) -> None:
	"""Per-command config on an unresolved commit asks (author decision Q30 = A).

	The name predates that decision and is kept per CLAUDE.md §6. Per-command
	configuration can select another checkout, so the guard asks instead of
	checking the session checkout's PR history.
	"""
	repo, stub_bin = merged_branch_repo
	_git(repo, "checkout", "main")
	proc = _run_hook(repo, stub_bin, command)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "could not resolve git commit directory" in proc.stdout
	assert _ask_decision(proc) is not None


def test_unresolved_env_directory_inside_shell_control_still_asks(merged_branch_repo) -> None:
	repo, stub_bin = merged_branch_repo
	_git(repo, "checkout", "main")
	proc = _run_hook(repo, stub_bin, "if true; then env -C /does-not-exist git commit -m x; fi")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert _ask_decision(proc) is not None


@pytest.mark.parametrize("command", [
	"if true; then env GIT_DIR=/does-not-exist git commit -m x; fi",
	"if true; then git -C /does-not-exist commit -m x; fi",
	"if true; then git --config-env=core.worktree:UNSET_WORKTREE commit -m x; fi",
])
def test_unresolved_directory_selector_inside_shell_control_asks(merged_branch_repo, command: str) -> None:
	repo, stub_bin = merged_branch_repo
	_git(repo, "checkout", "main")
	proc = _run_hook(repo, stub_bin, command)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert _ask_decision(proc) is not None


def test_absolute_core_worktree_cannot_identify_repo_inside_shell_control(merged_branch_repo, monkeypatch, capsys) -> None:
	repo, _ = merged_branch_repo
	other = repo.parent / "open-worktree"
	_git(repo, "worktree", "add", "-b", "feature/open", str(other), "main")
	# The session checkout (an open branch) is still checked, but cannot
	# authorize the commit: the configured worktree may be another checkout.
	monkeypatch.setattr(guard, "_read_cache", lambda *args: None)
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)
	monkeypatch.setattr(guard, "query_pull_requests", lambda *args: [])
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(other),
		"tool_input": {"command": f"if true; then git -c core.worktree={repo} commit -m x; fi"}})
	assert code == 0 and message == ""
	assert json.loads(capsys.readouterr().out.splitlines()[-1])["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_absolute_core_worktree_with_known_directory_checks_repo(merged_branch_repo, monkeypatch) -> None:
	repo, _ = merged_branch_repo
	merged_sha = _git(repo, "rev-parse", "HEAD")
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)
	monkeypatch.setattr(guard, "query_pull_requests", lambda slug, branch, cwd: [dict(MERGED_PR, headRefOid=merged_sha)])
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": f"git -c core.worktree={repo} commit -m x"}})
	assert code == 2 and "Branch `feature/x`" in message


@pytest.mark.parametrize("selector,path", [
	("GIT_DIR", ".git"),
	("GIT_WORK_TREE", ""),
])
def test_appended_git_directory_selector_still_asks_after_resolved_chdir(
	merged_branch_repo, monkeypatch, capsys, selector: str, path: str,
) -> None:
	repo, _ = merged_branch_repo
	other = repo.parent / "open-worktree"
	_git(repo, "worktree", "add", "-b", "feature/open", str(other), "main")
	monkeypatch.setattr(guard, "query_pull_requests", lambda *args: pytest.fail("must not check the wrong checkout"))
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(other),
		"tool_input": {"command": f"{selector}+={repo / path} git -C {other} commit -m x"}})
	assert code == 0 and message == ""
	decision = json.loads(capsys.readouterr().out.splitlines()[-1])
	assert decision["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize("command", [
	"if true; then env -C {repo} git commit -m x; fi",
	"if true; then git -C {repo} commit -m x; fi",
])
def test_absolute_chdir_inside_shell_control_checks_selected_repo(merged_branch_repo, monkeypatch, command: str) -> None:
	repo, _ = merged_branch_repo
	other = repo.parent / "open-worktree"
	_git(repo, "worktree", "add", "-b", "feature/open", str(other), "main")
	merged_sha = _git(repo, "rev-parse", "feature/x")
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)
	monkeypatch.setattr(guard, "query_pull_requests", lambda slug, branch, cwd: [dict(MERGED_PR, headRefOid=merged_sha)])
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(other),
		"tool_input": {"command": command.format(repo=repo)}})
	assert code == 2 and "Branch `feature/x`" in message


def test_non_env_uncertain_configured_commit_keeps_warning_only_behavior(merged_branch_repo, monkeypatch, capsys) -> None:
	# A `git -c` commit whose directory is unknown only because of shell control
	# flow is never blocked on the session checkout alone (code 0, no message),
	# but author decision Q30 = A makes it ask: per-command configuration may
	# select another checkout (core.worktree), so the hook cannot vouch for it.
	repo, _ = merged_branch_repo
	monkeypatch.setattr(guard, "query_pull_requests", lambda *args: [])
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": "if true; then git -c user.name=bot commit -m x; fi"}})
	assert code == 0 and message == ""
	decision = json.loads(capsys.readouterr().out.splitlines()[-1])
	assert decision["hookSpecificOutput"]["permissionDecision"] == "ask"
	assert "per-command Git configuration may select another checkout" in decision["hookSpecificOutput"]["permissionDecisionReason"]


def test_env_chdir_does_not_change_subsequent_command_directory(merged_branch_repo, monkeypatch) -> None:
	repo, _ = merged_branch_repo
	other = repo.parent / "open-worktree"
	_git(repo, "worktree", "add", "-b", "feature/open", str(other), "main")
	merged_sha = _git(repo, "rev-parse", "feature/x")
	seen: list[tuple[str, str]] = []
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)
	def listing(slug, branch, cwd):
		seen.append((branch, cwd))
		return [dict(MERGED_PR, headRefOid=merged_sha)] if branch == "feature/x" else [OPEN_PR]
	monkeypatch.setattr(guard, "query_pull_requests", listing)
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(other),
		"tool_input": {"command": f"env -C {repo} git commit -m x; git commit -m y"}})
	assert code == 2 and "Branch `feature/x`" in message
	assert seen == [("feature/x", str(repo)), ("feature/open", str(other))]


def test_per_command_config_commit_keeps_the_merged_pr_check(merged_branch_repo, monkeypatch) -> None:
	repo, _ = merged_branch_repo
	merged_sha = _git(repo, "rev-parse", "HEAD")
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)

	def listing(slug, branch, cwd):
		return [dict(MERGED_PR, headRefOid=merged_sha)]

	monkeypatch.setattr(guard, "query_pull_requests", listing)
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": "git -c user.name=bot commit -m x"}})
	assert code == 2 and "Branch `feature/x`" in message


@pytest.mark.parametrize("command", [
	"if true; then git -c user.name=bot commit -m x; fi",
	"if true; then git --config-env=user.name=BOT_NAME commit -m x; fi",
	"if true; then GIT_CONFIG_COUNT=0 git commit -m x; fi",
])
def test_ambiguous_non_env_config_commit_warns_without_asking(merged_branch_repo, monkeypatch, capsys, command: str) -> None:
	"""Per-command config on an unresolved commit asks (author decision Q30 = A).

	The name predates that decision and is kept per CLAUDE.md §6. The warning
	that the session checkout was checked is still emitted alongside the ask.
	"""
	repo, _ = merged_branch_repo
	_git(repo, "checkout", "main")
	monkeypatch.setattr(guard, "_read_cache", lambda *args: None)
	monkeypatch.setattr(guard, "query_pull_requests", lambda *args: [])
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": command}})
	assert code == 0 and message == ""
	response = json.loads(capsys.readouterr().out.splitlines()[-1])
	assert "could not resolve git command directory" in response["systemMessage"]
	assert "could not resolve git commit directory" in response["systemMessage"]
	assert response["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_ambiguous_config_commit_still_checks_merged_pr(merged_branch_repo, monkeypatch) -> None:
	repo, _ = merged_branch_repo
	merged_sha = _git(repo, "rev-parse", "HEAD")
	monkeypatch.setattr(guard, "_read_cache", lambda *args: None)
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)
	monkeypatch.setattr(guard, "query_pull_requests", lambda *args: [dict(MERGED_PR, headRefOid=merged_sha)])
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": "if true; then git -c user.name=bot commit -m x; fi"}})
	assert code == 2 and "Branch `feature/x`" in message


def test_matching_refspec_on_unverified_remote_does_not_check_origin(merged_branch_repo, monkeypatch, capsys) -> None:
	repo, _ = merged_branch_repo
	monkeypatch.setattr(guard, "query_pull_requests", lambda *args: pytest.fail("must not query origin for another repository"))
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": "git push https://github.com/other/repo :"}})
	assert code == 0 and message == ""
	decision = json.loads(capsys.readouterr().out.splitlines()[-1])
	assert decision["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize("command", [
	"git push https://github.com/other/repo :feature/x",
	"git push --repo=https://github.com/other/repo :feature/x",
	"git push --repo=https://github.com/other/repo -d feature/x",
	"git push https://github.com/other/repo --delete feature/x",
	"git push https://github.com/other/repo --tags",
])
def test_deletion_and_tag_only_pushes_to_other_repository_ask(merged_branch_repo, monkeypatch, capsys, command: str) -> None:
	repo, _ = merged_branch_repo
	monkeypatch.setattr(guard, "query_pull_requests", lambda *args: pytest.fail("must not query origin for another repository"))
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": command}})
	assert code == 0 and message == ""
	decision = json.loads(capsys.readouterr().out.splitlines()[-1])
	assert decision["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize("remote", ["origin"])
def test_deletion_on_origin_does_not_check_merged_pr(merged_branch_repo, monkeypatch, capsys, remote: str) -> None:
	repo, _ = merged_branch_repo
	monkeypatch.setattr(guard, "query_pull_requests", lambda *args: pytest.fail("deletions must not check PR history"))
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": f"git push {remote} :feature/x"}})
	assert code == 0 and message == ""
	assert "permissionDecision" not in capsys.readouterr().out


@pytest.mark.parametrize("args", [":feature/x", "--delete feature/x", "--tags"])
def test_same_slug_explicit_url_asks_for_deletion_and_tags(merged_branch_repo, monkeypatch, capsys, args: str) -> None:
	repo, _ = merged_branch_repo
	monkeypatch.setattr(guard, "query_pull_requests", lambda *unused: pytest.fail("must not query origin"))
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": f"git push https://github.com/o/r.git {args}"}})
	assert code == 0 and message == ""
	assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_explicit_origin_url_still_checks_origin_pr(merged_branch_repo, monkeypatch, capsys) -> None:
	"""Legacy name: even a matching explicit URL now asks without an origin lookup."""
	repo, _ = merged_branch_repo
	merged_sha = _git(repo, "rev-parse", "HEAD")
	lookups: list[tuple[str, str]] = []
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)
	def listing(slug, branch, cwd):
		lookups.append((slug, branch))
		return [dict(MERGED_PR, headRefOid=merged_sha)]
	monkeypatch.setattr(guard, "query_pull_requests", listing)
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": "git push --repo=https://github.com/other/repo https://github.com/o/r.git HEAD:feature/x"}})
	assert code == 0 and message == ""
	assert lookups == []  # URL rewriting means origin's PR history cannot authorize this push.
	assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize("command", [
	"git push origin 123 > /dev/null",
	"git push origin 123 2>&1",
	'git push origin "123">/dev/null',
])
def test_numeric_push_refspec_guards_merged_branch(merged_branch_repo, monkeypatch, command: str) -> None:
	repo, _ = merged_branch_repo
	merged_sha = _git(repo, "rev-parse", "HEAD")
	_git(repo, "branch", "123", merged_sha)
	lookups: list[str] = []
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)
	def listing(slug, branch, cwd):
		lookups.append(branch)
		return [dict(MERGED_PR, headRefOid=merged_sha)] if branch == "123" else []
	monkeypatch.setattr(guard, "query_pull_requests", listing)
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": command}})
	assert code == 2, message
	assert lookups == ["123"]


def test_quoted_numeric_push_refspec_cannot_match_later_redirect(merged_branch_repo, monkeypatch) -> None:
	repo, _ = merged_branch_repo
	merged_sha = _git(repo, "rev-parse", "HEAD")
	_git(repo, "branch", "12", merged_sha)
	lookups: list[str] = []
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)
	def listing(slug, branch, cwd):
		lookups.append(branch)
		return [dict(MERGED_PR, headRefOid=merged_sha)] if branch == "12" else []
	monkeypatch.setattr(guard, "query_pull_requests", listing)
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": 'git push origin "12">/dev/null; echo 12>x'}})
	assert code == 2, message
	assert lookups == ["12"]


@pytest.mark.parametrize("command", [
	"git push origin HEAD:$DEST",
	"git push --repo=origin HEAD:$DEST",
	"git push --repo origin HEAD:feature/x",
	"git push --repo=origin HEAD:feature/x",
	"git push --repo=upstream mirror HEAD:feature/x",
	"git push --repo=upstream ../mirror HEAD:feature/x",
	"git push --repo=upstream https://github.com/o/r.git HEAD:feature/x",
	"git push --unknown origin 12",
])
def test_unknown_explicit_push_target_requires_confirmation(merged_branch_repo, capsys, command: str) -> None:
	repo, _ = merged_branch_repo
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": command}})
	assert (code, message) == (0, "")
	output = json.loads(capsys.readouterr().out)
	assert output["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_positional_remote_probe_failure_requires_confirmation(merged_branch_repo, monkeypatch, capsys) -> None:
	repo, _ = merged_branch_repo
	_git(repo, "branch", "origin", "main")
	actual_run = guard._run
	def timed_out_config(argv, cwd, timeout):
		if argv == ["git", "config", "--get", "remote.origin.url"]:
			return 124, "", "timed out"
		return actual_run(argv, cwd, timeout)
	monkeypatch.setattr(guard, "_run", timed_out_config)
	monkeypatch.setattr(guard, "query_pull_requests", lambda *args: pytest.fail("destination unknown"))
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": "git push --repo=upstream origin"}})
	assert (code, message) == (0, "")
	assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_unconfigured_positional_repository_does_not_become_a_branch(merged_branch_repo, monkeypatch, capsys) -> None:
	repo, _ = merged_branch_repo
	_git(repo, "branch", "mirror", "main")
	monkeypatch.setattr(guard, "query_pull_requests", lambda *args: pytest.fail("destination unknown"))
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": "git push --repo=upstream mirror"}})
	assert (code, message) == (0, "")
	output = json.loads(capsys.readouterr().out)
	assert output["hookSpecificOutput"]["permissionDecision"] == "ask"
	assert "could not resolve git push positional repository" in output["systemMessage"]


@pytest.mark.parametrize("command", [
	"git push origin HEAD:$DEST1 HEAD:$DEST2",
	"git push origin HEAD:$DEST1 :",
])
def test_multiple_unknown_push_targets_emit_one_confirmation(merged_branch_repo, monkeypatch, capsys, command: str) -> None:
	repo, _ = merged_branch_repo
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)
	monkeypatch.setattr(guard, "query_pull_requests", lambda slug, branch, cwd: [])
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": command}})
	assert (code, message) == (0, "")
	output = json.loads(capsys.readouterr().out)
	assert output["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_api_failure_and_unresolved_destination_emit_one_confirmation(merged_branch_repo, monkeypatch, capsys) -> None:
	repo, _ = merged_branch_repo
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	def unavailable_pr_listing(slug, branch, cwd):
		raise guard.LookupUnavailable("API unavailable")

	monkeypatch.setattr(guard, "query_pull_requests", unavailable_pr_listing)
	assert guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": "git push origin HEAD:feature/a HEAD:$DEST"}}) == (0, "")
	response = json.loads(capsys.readouterr().out)
	assert response["hookSpecificOutput"]["permissionDecision"] == "ask"
	assert "could not reach GitHub" in response["systemMessage"]
	assert "could not resolve git push destination" in response["systemMessage"]
	assert "could not reach GitHub" in response["hookSpecificOutput"]["permissionDecisionReason"]
	assert "could not resolve git push destination" in response["hookSpecificOutput"]["permissionDecisionReason"]


def test_internal_error_preserves_buffered_confirmation(monkeypatch, capsys) -> None:
	def failing_evaluation(payload):
		guard._request_confirmation("x")
		raise RuntimeError("broken")

	monkeypatch.setattr(guard, "_evaluate_bash", failing_evaluation)
	assert guard.evaluate({"tool_name": "Bash"}) == (0, "")
	response = json.loads(capsys.readouterr().out)
	assert response["hookSpecificOutput"]["permissionDecision"] == "ask"
	assert "internal error" in response["systemMessage"]
	assert "merged-PR guard needs confirmation: x" in response["systemMessage"]
	assert guard.evaluate({"tool_name": "Other"}) == (0, "")
	assert capsys.readouterr().out == ""


def test_unknown_push_target_does_not_prompt_before_merged_branch_block(merged_branch_repo, monkeypatch, capsys) -> None:
	repo, _ = merged_branch_repo
	merged_sha = _git(repo, "rev-parse", "HEAD")
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)
	monkeypatch.setattr(guard, "query_pull_requests", lambda slug, branch, cwd:
		[dict(MERGED_PR, headRefOid=merged_sha)] if branch == "feature/x" else [])
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": "git push origin HEAD:$DEST feature/x"}})
	assert code == 2, message
	assert "feature/x" in message
	assert capsys.readouterr().out == ""


@pytest.mark.parametrize("command", [
	"git push origin 2 > /dev/null",
	"git push origin 2  >/dev/null",
	'git push origin "2">/dev/null',
	"git push origin feature/open 2 >&1",
	"git push origin 2 2>&2>/dev/null",
	"git push origin 2 >&2>f",
])
def test_numeric_refspec_before_redirect_is_guarded(merged_branch_repo, monkeypatch, command: str) -> None:
	repo, _ = merged_branch_repo
	merged_sha = _git(repo, "rev-parse", "HEAD")
	_git(repo, "branch", "2")
	_git(repo, "checkout", "main")
	lookups: list[str] = []
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)
	def listing(slug, branch, cwd):
		lookups.append(branch)
		return [dict(MERGED_PR, headRefOid=merged_sha)] if branch == "2" else [OPEN_PR]
	monkeypatch.setattr(guard, "query_pull_requests", listing)
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": command}})
	assert code == 2, message
	assert "2" in lookups


@pytest.mark.parametrize(("command", "expected"), [
	("git push origin 2>/dev/null", ["git", "push", "origin"]),
	("git push origin 2 > /dev/null", ["git", "push", "origin", "2"]),
	("x 12<in", ["x"]),
	("x '2'>f", ["x", "2"]),
	("x 2>a 3>b", ["x"]),
	("x 2>&2 >f", ["x"]),
])
def test_numeric_word_before_redirect_tokenization(command: str, expected: list[str]) -> None:
	assert guard._shell_segments_with_operators(command) == [("", expected)]


def test_unresolved_push_source_requests_confirmation(monkeypatch, merged_branch_repo, capsys) -> None:
	repo, _ = merged_branch_repo
	_git(repo, "checkout", "main")
	monkeypatch.setattr(guard, "query_pull_requests", lambda *args: pytest.fail("cannot validate unknown tip"))
	assert guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": 'git push origin "$SOURCE:feature/x"'}}) == (0, "")
	response = json.loads(capsys.readouterr().out)
	assert response["hookSpecificOutput"]["permissionDecision"] == "ask"
	assert "feature/x" in response["systemMessage"]


@pytest.mark.parametrize("command", [
	'git push origin HEAD:$DEST',
	'git push origin HEAD:"${DEST}"',
])
def test_unresolved_push_destination_requests_confirmation(monkeypatch, merged_branch_repo, capsys, command: str) -> None:
	repo, _ = merged_branch_repo
	_git(repo, "checkout", "main")
	monkeypatch.setattr(guard, "query_pull_requests", lambda *args: pytest.fail("cannot validate unknown destination"))
	assert guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": command}}) == (0, "")
	response = json.loads(capsys.readouterr().out)
	assert response["hookSpecificOutput"]["permissionDecision"] == "ask"
	assert "could not resolve git push destination" in response["systemMessage"]


def test_unresolved_destination_does_not_hide_literal_merged_branch(monkeypatch, merged_branch_repo) -> None:
	repo, _ = merged_branch_repo
	merged_sha = _git(repo, "rev-parse", "HEAD")
	_git(repo, "checkout", "main")
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)
	def listing(slug, branch, cwd):
		assert branch == "feature/x"
		return [dict(MERGED_PR, headRefOid=merged_sha)]
	monkeypatch.setattr(guard, "query_pull_requests", listing)
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": "git push origin HEAD:$DEST feature/x:feature/x"}})
	assert code == 2
	assert "Branch `feature/x`" in message


@pytest.mark.parametrize("command", [
	'git push --all origin; git push origin HEAD:$DEST',
	'git push --all origin; git push origin "$SOURCE:feature/x"',
])
def test_bulk_and_unresolved_push_emit_one_confirmation(merged_branch_repo, capsys, command: str) -> None:
	repo, _ = merged_branch_repo
	_git(repo, "checkout", "main")
	assert guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": command}}) == (0, "")
	response = json.loads(capsys.readouterr().out)
	assert response["hookSpecificOutput"]["permissionDecision"] == "ask"
	assert "Bulk git push" in response["systemMessage"]
	assert "could not resolve git push" in response["systemMessage"]


def test_cd_or_exit_preserves_worktree_for_push(merged_branch_repo, monkeypatch) -> None:
	repo, _ = merged_branch_repo
	worktree = repo.parent / "other"
	_git(repo, "worktree", "add", "-b", "feature/open", str(worktree), "main")
	lookups: list[str] = []
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)
	def listing(slug, branch, cwd):
		lookups.append(branch)
		return [dict(MERGED_PR, headRefOid=_git(repo, "rev-parse", "feature/x"))] if branch == "feature/x" else [OPEN_PR]
	monkeypatch.setattr(guard, "query_pull_requests", listing)
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": f"cd {worktree} || exit 1; git push origin HEAD:feature/open"}})
	assert code == 0, message
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": f"cd {worktree} || exit 1 && git push origin HEAD:feature/open"}})
	assert code == 0, message
	assert lookups == ["feature/open", "feature/open"]


def test_failed_cd_and_exit_redirects_check_session_checkout(merged_branch_repo) -> None:
	repo, stub_bin = merged_branch_repo
	other_directory = repo.parent / "not-a-repo"
	other_directory.mkdir()
	missing = repo.parent / "missing" / "output"
	proc = _run_hook(repo, stub_bin,
		f"cd {other_directory} >{missing} || exit >{missing}; git push origin")
	assert proc.returncode == 2, proc.stdout + proc.stderr
	assert "Branch `feature/x`" in proc.stderr
	assert "could not resolve git command directory" in proc.stdout


@pytest.mark.parametrize("command", [
	"cd {worktree} >{missing} || exit 1; git push origin HEAD:feature/open",
	"cd {worktree} || exit 1 >{missing}; git push origin HEAD:feature/open",
	"cd {worktree} 2>&1 || exit 1; git push origin HEAD:feature/open",
])
def test_redirected_cd_or_exit_cannot_authorize_worktree_push(merged_branch_repo, command: str) -> None:
	repo, stub_bin = merged_branch_repo
	merged_sha = _git(repo, "rev-parse", "HEAD")
	worktree = repo.parent / "other"
	_git(repo, "worktree", "add", "-b", "feature/open", str(worktree), "main")
	_worktree_pr_stub(stub_bin, merged_sha)
	missing = repo.parent / "missing" / "output"
	proc = _run_hook(repo, stub_bin, command.format(worktree=worktree, missing=missing))
	assert proc.returncode == 2, proc.stdout + proc.stderr
	assert "Branch `feature/x`" in proc.stderr
	assert "could not resolve git command directory" in proc.stdout


def test_dev_null_redirect_keeps_cd_or_exit_worktree(merged_branch_repo, monkeypatch, capsys) -> None:
	repo, _ = merged_branch_repo
	worktree = repo.parent / "other"
	_git(repo, "worktree", "add", "-b", "feature/open", str(worktree), "main")
	lookups: list[str] = []
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)
	def listing(slug, branch, cwd):
		lookups.append(branch)
		return [OPEN_PR]
	monkeypatch.setattr(guard, "query_pull_requests", listing)
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo),
		"tool_input": {"command": f"cd {worktree} 2>/dev/null || exit 1; git push origin HEAD:feature/open"}})
	assert code == 0, message
	assert lookups == ["feature/open"]
	assert "permissionDecision" not in capsys.readouterr().out


@pytest.mark.parametrize("subcommand,asks", [
	("git push origin HEAD:feature/open", True),
	("git commit -m x", True),
])
@pytest.mark.parametrize("directory_change", ["cd $WT &&", "cd {other_dir} >{missing};"])
def test_unknown_directory_push_asks_but_commit_warns(
	merged_branch_repo, subcommand: str, asks: bool, directory_change: str
) -> None:
	repo, stub_bin = merged_branch_repo
	merged_sha = _git(repo, "rev-parse", "HEAD")
	_git(repo, "checkout", "-b", "feature/open", "main")
	_worktree_pr_stub(stub_bin, merged_sha)
	prefix = directory_change.format(other_dir=repo.parent, missing=repo.parent / "missing" / "output")
	proc = _run_hook(repo, stub_bin, f"{prefix} {subcommand}")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "could not resolve git" in proc.stdout
	assert (_ask_decision(proc) is not None) is asks


@pytest.mark.parametrize("command", [
	"if true; then git push origin; fi",
	"if git push origin; then :; fi",
	"if ! git push origin; then :; fi",
	"if ! ! git push origin; then :; fi",
	"! git push origin",
	"if true; then if git push origin; then :; fi; fi",
	"for item in a b; do git push origin; done",
	"case x in x) git push origin;; esac",
	"case x in\nx) git push origin;; esac",
	"case y in x) :;; y) git push origin;; esac",
	"case x in x) if ! git push origin; then :; fi;; esac",
])
def test_shell_control_push_checks_merged_checkout(merged_branch_repo, command: str) -> None:
	repo, stub_bin = merged_branch_repo
	proc = _run_hook(repo, stub_bin, command)
	assert proc.returncode == 2, proc.stdout + proc.stderr
	assert "Branch `feature/x`" in proc.stderr


@pytest.mark.parametrize("command", [
	"if true; then git push origin; fi",
	"if ! git push origin; then :; fi",
	"if true; then if git push origin; then :; fi; fi",
	"case x in x) if ! git push origin; then :; fi;; esac",
])
def test_shell_control_push_on_open_checkout_asks(merged_branch_repo, command: str) -> None:
	repo, stub_bin = merged_branch_repo
	_git(repo, "checkout", "main")
	proc = _run_hook(repo, stub_bin, command)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert _ask_decision(proc) is not None


def test_shell_control_commit_on_open_checkout_only_warns(merged_branch_repo) -> None:
	repo, stub_bin = merged_branch_repo
	_git(repo, "checkout", "main")
	proc = _run_hook(repo, stub_bin, "if true; then git commit -m next; fi")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "could not resolve git command directory" in proc.stdout
	assert _ask_decision(proc) is None


def test_conditional_cd_or_exit_keeps_worktree_after_semicolon(merged_branch_repo, monkeypatch) -> None:
	repo, _ = merged_branch_repo
	merged_sha = _git(repo, "rev-parse", "HEAD")
	_git(repo, "checkout", "main")
	worktree = repo.parent / "merged"
	_git(repo, "worktree", "add", str(worktree), "feature/x")
	monkeypatch.setattr(guard, "_read_cache", lambda slug, branch: None)
	monkeypatch.setattr(guard, "_write_cache", lambda *args: None)
	monkeypatch.setattr(guard, "query_pull_requests", lambda slug, branch, cwd: [dict(MERGED_PR, headRefOid=merged_sha)])
	command = f"true && cd {worktree} || exit 1; git push origin HEAD:feature/x"
	invocations = guard._guarded_git_invocations(command, str(repo))
	assert len(invocations) == 1
	assert invocations[0].cwd == str(worktree)
	code, message = guard.evaluate({"tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": command}})
	assert code == 2, message
	assert "Branch `feature/x`" in message


# ──────────────────────────────────────────────────────────────────
# Wiring — the parts that only exist as config / instruction text
# ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", [SETTINGS_PATH, TEMPLATE_SETTINGS_PATH])
def test_api_write_allowlist_disables_implicit_curl_config(path: Path) -> None:
	settings = json.loads(path.read_text(encoding="utf-8"))
	allow = settings["permissions"]["allow"]
	assert allow[:3] == [
		"Bash(curl -q -sS -X PUT https://api.cloudflare.com/client/v4/accounts/*)",
		"Bash(curl -q -sS -X POST https://api.cloudflare.com/client/v4/accounts/*)",
		"Bash(curl -q -sS -X PATCH https://api.cloudflare.com/client/v4/accounts/*)",
	]
	# Every allowed curl rule keeps `-q` so an implicit ~/.curlrc cannot alter the call.
	curl_rules = [rule for rule in allow if rule.startswith("Bash(curl")]
	assert curl_rules == allow[:3]
	assert not any("api.digitalocean.com" in rule for rule in allow)


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


def test_review_editor_can_transfer_guard_without_opening_other_claude_hooks() -> None:
	# Historical test name kept for discovery; safety hook transfer is now denied.
	spec = importlib.util.spec_from_file_location(
		"review_untrusted_workspace", REPO_ROOT / "scripts" / "review_untrusted_workspace.py"
	)
	assert spec is not None and spec.loader is not None
	workspace_guard = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(workspace_guard)
	assert not workspace_guard.allowed(".claude/hooks/pr_merge_status_guard.py")
	assert not workspace_guard.allowed(".claude/hooks/unrelated.py")


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


# Pipes and data heredocs must not make a command unreadable or its directory
# unknown (false prompts reported after #6133 / #6135).


def _open_feature_checkout(repo: Path, stub_bin: Path) -> None:
	"""Check out `feature/open`, which the stub answers with an open PR."""
	_worktree_pr_stub(stub_bin, _git(repo, "rev-parse", "HEAD"))
	_git(repo, "checkout", "-b", "feature/open")


@pytest.mark.parametrize("command", [
	"git log --oneline -3 | head -3; git push origin feature/open",
	(
		"git fetch -q origin feature/open && git log --oneline HEAD..origin/feature/open | head -3; "
		"git push origin feature/open 2>&1 | tail -1"
	),
	"git status | head -1 && git push origin feature/open",
	"cd /tmp | true; git push origin feature/open",
	"git fetch -q origin || true; git push origin feature/open",
	"true |& cd /tmp; git push origin feature/open",
])
def test_pipe_or_unrelated_or_list_keeps_push_directory_known(merged_branch_repo, command: str) -> None:
	repo, stub_bin = merged_branch_repo
	_open_feature_checkout(repo, stub_bin)
	proc = _run_hook(repo, stub_bin, command)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert _ask_decision(proc) is None, proc.stdout
	assert "could not resolve git" not in proc.stdout


def test_pipe_before_push_still_blocks_merged_branch(merged_branch_repo) -> None:
	repo, stub_bin = merged_branch_repo
	proc = _run_hook(repo, stub_bin, "git log --oneline -3 | head -3; git push origin feature/x")
	assert proc.returncode == 2, proc.stdout + proc.stderr
	assert "Branch `feature/x`" in proc.stderr
	assert "could not resolve git" not in proc.stdout


@pytest.mark.parametrize("command", [
	"true || cd /tmp; git push origin feature/open",
	"cd /tmp && true || git push origin feature/open",
	"sleep 1 & git push origin feature/open",
])
def test_conditional_directory_change_still_asks_for_push(merged_branch_repo, command: str) -> None:
	repo, stub_bin = merged_branch_repo
	_open_feature_checkout(repo, stub_bin)
	proc = _run_hook(repo, stub_bin, command)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert _ask_decision(proc) is not None, proc.stdout


@pytest.mark.parametrize("command", [
	"cat <<'EOF'\nit's fine\nEOF",
	(
		"python3 - <<'EOF'\nold = '''def f():\n\t\"\"\"the checkout's PR\"\"\"\n'''\nEOF\n"
		"python3 -m pytest -q tests 2>&1 | tail -5"
	),
	(
		"python3 - <<'EOF'\nnew = '''# the security audit's export\n\t\tisolation_args+=(--include \"${p}\")\n'''\nEOF\n"
		"bash -n scripts/ai_engine.sh && echo ok"
	),
	(
		"git commit -q -F - <<'EOF'\nFollow main's rule\nEOF\n"
		"git log --oneline HEAD..origin/feature/open | head -3; git push origin feature/open 2>&1 | tail -1"
	),
	"git commit -m \"$(cat <<'EOF'\nsay \"hi\", it's done\nEOF\n)\"",
	"cat <<-EOF\n\tit's indented\n\tEOF",
	"env cat <<'EOF'\nit's data\nEOF",
	"timeout 5 cat <<'EOF' | tail -1\nit's data\nEOF",
])
def test_data_heredoc_with_unbalanced_quotes_does_not_prompt(merged_branch_repo, command: str) -> None:
	repo, stub_bin = merged_branch_repo
	_open_feature_checkout(repo, stub_bin)
	proc = _run_hook(repo, stub_bin, command)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert _ask_decision(proc) is None, proc.stdout


def test_data_heredoc_commit_on_merged_branch_is_still_blocked(merged_branch_repo) -> None:
	repo, stub_bin = merged_branch_repo
	proc = _run_hook(repo, stub_bin, "git commit -q -F - <<'EOF'\nFollow main's rule\nEOF")
	assert proc.returncode == 2, proc.stdout + proc.stderr
	assert "Branch `feature/x`" in proc.stderr


@pytest.mark.parametrize("command", [
	"bash <<'EOF'\ngit push origin feature/x\nit's\nEOF",
	"sudo sh <<'EOF'\ngit push origin feature/x\nit's\nEOF",
	"cat <<EOF\n$(git push origin feature/x)\nit's\nEOF",
	"cat <<'EOF'\nfine\nEOF\ngit status\n\"unterminated",
	"cat <<'EOF' | bash\ngit push origin feature/x\nit's\nEOF",
	"env bash <<'EOF'\ngit push origin feature/x\nit's\nEOF",
])
def test_heredoc_run_as_shell_is_still_parsed(merged_branch_repo, command: str) -> None:
	repo, stub_bin = merged_branch_repo
	_open_feature_checkout(repo, stub_bin)
	proc = _run_hook(repo, stub_bin, command)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert _ask_decision(proc) is not None, proc.stdout


@pytest.mark.parametrize("command", [
	# The delimiter is `EOF-TEXT`, not `EOF`: the push runs after the heredoc.
	"cat <<EOF-TEXT\npayload\nEOF-TEXT\ngit push origin feature/x",
	"cat <<'EOF' | bash\ngit push origin feature/x\nEOF",
	"cat <<'EOF' |\ngit push origin feature/x\nEOF\nbash",
])
def test_heredoc_that_hides_an_executed_push_still_blocks(merged_branch_repo, command: str) -> None:
	repo, stub_bin = merged_branch_repo
	proc = _run_hook(repo, stub_bin, command)
	assert proc.returncode == 2, proc.stdout + proc.stderr
	assert "Branch `feature/x`" in proc.stderr


def test_strip_data_heredoc_bodies_keeps_operator_and_shell_bodies() -> None:
	assert guard._strip_data_heredoc_bodies("cat <<'EOF'\nit's\nEOF\ngit status") == "cat <<'EOF'\ngit status"
	assert guard._strip_data_heredoc_bodies('echo "<<EOF"\nit\'s\nEOF') == 'echo "<<EOF"\nit\'s\nEOF'
	shell_body = "bash <<'EOF'\ngit push\nEOF"
	assert guard._strip_data_heredoc_bodies(shell_body) == shell_body
	substitution = "cat <<EOF\n$(git push)\nEOF"
	assert guard._strip_data_heredoc_bodies(substitution) == substitution
	assert guard._strip_data_heredoc_bodies("cat <<\\EOF\n$(x)\nEOF") == "cat <<\\EOF\n$(x)\nEOF"
	partial_delimiter = "cat <<EOF-1\nx\nEOF-1\ngit push"
	assert guard._strip_data_heredoc_bodies(partial_delimiter) == partial_delimiter
	piped = "cat <<'EOF' | bash\ngit push\nEOF"
	assert guard._strip_data_heredoc_bodies(piped) == piped
	assert guard._strip_data_heredoc_bodies("env cat <<'EOF'\nit's\nEOF") == "env cat <<'EOF'"


# ──────────────────────────────────────────────────────────────────
# Shell wrappers: `bash -c`, `eval`, `$(...)`, backticks, `<(...)`
# (security finding merged-pr-guard-misses-wrapped-git). Every case runs
# against both copies; the live copy must match the template byte for byte.
# ──────────────────────────────────────────────────────────────────


def _load_template_guard():
	spec = importlib.util.spec_from_file_location("pr_merge_status_guard_template", TEMPLATE_GUARD_PATH)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


template_guard = _load_template_guard()
WRAPPER_GUARD_COPIES = pytest.mark.parametrize(
	("guard_module", "hook_path"),
	[(guard, GUARD_PATH), (template_guard, TEMPLATE_GUARD_PATH)],
	ids=["live", "template"],
)


def _nested_shell(command: str, levels: int) -> str:
	for _ in range(levels):
		command = "bash -c " + shlex.quote(command)
	return command


def _run_hook_at(hook_path: Path, repo: Path, stub_bin: Path, command: str) -> subprocess.CompletedProcess:
	env = _git_env()
	env["PATH"] = f"{stub_bin}{os.pathsep}{env.get('PATH', '')}"
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env.pop("CLAUDE_PR_MERGE_GUARD", None)
	env["TMPDIR"] = str(repo.parent / "cache")
	(repo.parent / "cache").mkdir(exist_ok=True)
	return subprocess.run(
		[sys.executable, str(hook_path)],
		input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)}),
		capture_output=True,
		text=True,
		env=env,
		timeout=60,
		check=False,
	)


@WRAPPER_GUARD_COPIES
@pytest.mark.parametrize("command", [
	"bash -c 'git push origin HEAD:x'",
	'/bin/sh -c "git commit -m x"',
	"sh -c 'git commit -m x'",
	"zsh -lc 'git push'",
	"bash -e -c 'git push'",
	"bash -o pipefail -c 'git push'",
	'eval "git push origin x"',
	"echo $(git push origin x)",
	"echo `git push`",
	"cat <(git push origin x)",
	"env bash -c 'git push'",
	"env -S 'bash -c \"git push\"'",
	"X=$(git commit -m y)",
	"bash -c \"bash -c 'git push origin x'\"",
	# A nested shell that reads its script from a heredoc (#6777 review).
	"bash -c \"bash <<'EOF'\ngit push origin x\nEOF\"",
	"echo $(bash <<'EOF'\ngit push origin x\nEOF\n)",
	"bash -c \"cat <<'EOF'\nit's data\nEOF\ngit push origin x\"",
])
def test_shell_wrapped_git_writes_are_detected(guard_module, hook_path: Path, command: str) -> None:
	assert guard_module.git_subcommands(command) & guard_module.GUARDED_SUBCOMMANDS


@WRAPPER_GUARD_COPIES
@pytest.mark.parametrize("command", [
	"bash -c 'echo hi'",
	"bash script.sh",
	"echo '$(git push)'",
	"echo $(git rev-parse HEAD)",
	"echo $(( 1 + 2 ))",
	# A data heredoc inside a wrapper is never run, even when it mentions a push.
	"bash -c \"python3 <<'EOF'\nprint('git push origin x')\nEOF\"",
])
def test_shell_wrappers_without_git_writes_are_ignored(guard_module, hook_path: Path, command: str) -> None:
	assert not (guard_module.git_subcommands(command) & guard_module.GUARDED_SUBCOMMANDS)


HEREDOC_COMMIT = "git commit -m \"$(cat <<'EOF'\nDon't git push from here; it's a commit message.\nEOF\n)\""
# Covers the `git commit -F -` (message on stdin) form only.
NESTED_HEREDOC_COMMIT = "bash -c \"git commit -q -F - <<'EOF'\nDon't git push; it's a message.\nEOF\""


@WRAPPER_GUARD_COPIES
def test_heredoc_commit_message_is_not_a_wrapped_write(guard_module, hook_path: Path) -> None:
	assert guard_module.git_subcommands(HEREDOC_COMMIT) == {"commit"}
	assert [invocation.warning for invocation in guard_module._guarded_git_invocations(HEREDOC_COMMIT, "/")] == [""]


@WRAPPER_GUARD_COPIES
def test_nested_data_heredoc_commit_is_one_commit(guard_module, hook_path: Path) -> None:
	invocations = guard_module._guarded_git_invocations(NESTED_HEREDOC_COMMIT, "/")
	assert [(invocation.subcommand, invocation.warning) for invocation in invocations] == [("commit", "")]


@WRAPPER_GUARD_COPIES
@pytest.mark.parametrize("command", [
	"bash -c 'git push origin HEAD:feature/x'",
	"/bin/sh -c 'git push origin HEAD:feature/x'",
	"/bin/sh -c 'git commit -m x'",
	'eval "git push origin HEAD:feature/x"',
	"echo $(git push origin HEAD:feature/x)",
	"echo `git commit -m x`",
	_nested_shell("git push origin HEAD:feature/x", 3),
])
def test_e2e_blocks_wrapped_write_on_merged_branch(merged_branch_repo, guard_module, hook_path: Path, command: str) -> None:
	repo, stub_bin = merged_branch_repo
	proc = _run_hook_at(hook_path, repo, stub_bin, command)
	assert proc.returncode == 2, proc.stdout + proc.stderr
	assert "pull/41" in proc.stderr


@WRAPPER_GUARD_COPIES
@pytest.mark.parametrize("command", [
	"bash -c 'git push origin HEAD:feature/open'",
	"sh -c 'git push origin HEAD:feature/open'",
])
def test_e2e_allows_wrapped_push_to_open_branch(merged_branch_repo, guard_module, hook_path: Path, command: str) -> None:
	repo, stub_bin = merged_branch_repo
	_worktree_pr_stub(stub_bin, _git(repo, "rev-parse", "HEAD"))
	proc = _run_hook_at(hook_path, repo, stub_bin, command)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "permissionDecision" not in proc.stdout


@WRAPPER_GUARD_COPIES
def test_e2e_heredoc_commit_on_rebuilt_branch_does_not_ask(merged_branch_repo, guard_module, hook_path: Path) -> None:
	repo, stub_bin = merged_branch_repo
	_git(repo, "checkout", "-B", "feature/x", "main")
	proc = _run_hook_at(hook_path, repo, stub_bin, HEREDOC_COMMIT)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "permissionDecision" not in proc.stdout


@WRAPPER_GUARD_COPIES
@pytest.mark.parametrize("command", [
	'eval "$x"',
	'bash -c "$CMD"',
	"bash -c 'P=git; $P push origin x'",
	"echo $(git push",
	"MSG='git push' bash -c",
	'echo "$(git push origin "$B")"',
	_nested_shell("git push origin HEAD:feature/x", 4),
])
def test_unreadable_wrapped_write_asks_without_querying_prs(
	merged_branch_repo, monkeypatch, capsys, guard_module, hook_path: Path, command: str,
) -> None:
	repo, _ = merged_branch_repo
	monkeypatch.setattr(
		guard_module, "query_pull_requests", lambda *args: pytest.fail("unreadable wrapper must not query origin")
	)
	code, message = guard_module.evaluate({"tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": command}})
	assert (code, message) == (0, "")
	decision = json.loads(capsys.readouterr().out.splitlines()[-1])
	assert decision["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_shell_wrapper_scanner_reports_unterminated_text() -> None:
	assert template_guard._scan_shell_text("echo $(git push").unreadable
	assert template_guard._scan_shell_text("echo `git push").unreadable
	assert template_guard._scan_shell_text("cat <<EOF\nno terminator").unreadable
	scan = template_guard._scan_shell_text("a $(b) `c` <(d) \"$(e)\" '$(f)'")
	assert scan.bodies == ["b", "c", "d", "e"] and not scan.unreadable
