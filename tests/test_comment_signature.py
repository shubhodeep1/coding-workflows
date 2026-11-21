#!/usr/bin/env python3
"""Signed job-token comments (run-volume plan item 15, decision D16).

Drives scripts/comment_signature.py through the real `gh` shim that
scripts/gh_helpers.sh installs, against a fake `gh` that records every call
(arguments, token, posted payload) and answers from canned responses.
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
SCRIPTS = REPO_ROOT / "scripts"
SECRET = "test-secret"
REPO = "acme/widgets"
PAT = "pat-token"
JOB = "job-token"

_SPEC = importlib.util.spec_from_file_location("comment_signature", SCRIPTS / "comment_signature.py")
cs = importlib.util.module_from_spec(_SPEC)
sys.modules["comment_signature"] = cs
_SPEC.loader.exec_module(cs)

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
state = os.environ["FAKE_GH_STATE"]
args = sys.argv[1:]
record = {"args": args, "token": os.environ.get("GH_TOKEN", "")}
if "--input" in args:
	path = args[args.index("--input") + 1]
	record["payload"] = json.load(sys.stdin if path == "-" else open(path))
with open(os.path.join(state, "calls.jsonl"), "a") as handle:
	handle.write(json.dumps(record) + "\n")
responses = json.load(open(os.path.join(state, "responses.json")))
key = " ".join(a for a in args if not a.startswith("/tmp") and not a.startswith(state))
for rule in responses:
	if rule["match"] in key and rule.get("token", record["token"]) == record["token"]:
		sys.stdout.write(rule.get("stdout", ""))
		sys.stderr.write(rule.get("stderr", ""))
		sys.exit(rule.get("rc", 0))
sys.stdout.write('{"id": 1, "html_url": "https://github.com/acme/widgets/issues/5#issuecomment-1"}')
'''


def issue_url(num: int, repo: str = REPO) -> str:
	return f"https://api.github.com/repos/{repo}/issues/{num}"


def bot_comment(body: str, num: int = 5, cid: int = 10) -> dict:
	return {"id": cid, "issue_url": issue_url(num), "body": body, "author_association": "NONE",
		"user": {"login": "github-actions[bot]", "type": "Bot", "id": 41898282}}


@pytest.fixture()
def env(tmp_path):
	bindir = tmp_path / "bin"
	bindir.mkdir()
	fake = bindir / "gh"
	fake.write_text(FAKE_GH)
	fake.chmod(0o755)
	state = tmp_path / "state"
	state.mkdir()
	(state / "responses.json").write_text(json.dumps([
		{"match": "api user", "stdout": '{"login":"owner-login","type":"User","id":4242}'},
	]))
	runner_temp = tmp_path / "runner"
	runner_temp.mkdir()
	base = {
		"PATH": f"{bindir}:{os.environ['PATH']}",
		"HOME": str(tmp_path),
		"FAKE_GH_STATE": str(state),
		"RUNNER_TEMP": str(runner_temp),
		"GITHUB_REPOSITORY": REPO,
		"GH_TOKEN": PAT,
		"AI_COMMENT_SIGNING_SECRET": SECRET,
		"AI_COMMENT_JOB_TOKEN": JOB,
	}
	return {"env": base, "state": state, "tmp": tmp_path}


def set_responses(env, rules):
	rules = rules + [{"match": "api user", "stdout": '{"login":"owner-login","type":"User","id":4242}'}]
	(env["state"] / "responses.json").write_text(json.dumps(rules))


def run(env, script: str, extra: dict | None = None, stdin: str = "") -> subprocess.CompletedProcess:
	full = dict(env["env"], **(extra or {}))
	wrapped = f'source "{SCRIPTS}/gh_helpers.sh"\n{script}'
	return subprocess.run(["bash", "-c", wrapped], capture_output=True, text=True, env=full, input=stdin, check=False)


def calls(env) -> list[dict]:
	path = env["state"] / "calls.jsonl"
	if not path.exists():
		return []
	return [json.loads(line) for line in path.read_text().splitlines()]


def comment_calls(env) -> list[dict]:
	return [c for c in calls(env) if c["args"][:2] != ["api", "user"]]


# ---------------------------------------------------------------------------
# Signing primitives
# ---------------------------------------------------------------------------

def test_sign_then_normalize_restores_pat_comment():
	signed = cs.sign_body(SECRET, REPO, 5, "owner-login", "User", "AI autofix editor summary\n\nok\n")
	assert signed.startswith("AI autofix editor summary\n\nok\n\n<!-- ai-comment-sig:v1 repo=acme/widgets issue=5 as=owner-login type=User sig=")
	out = cs.normalize_comment(bot_comment(signed), SECRET)
	assert out["body"] == "AI autofix editor summary\n\nok\n"
	assert out["user"]["login"] == "owner-login"
	assert out["user"]["type"] == "User"
	assert out["author_association"] == "COLLABORATOR"


def test_owner_login_gets_owner_association():
	signed = cs.sign_body(SECRET, REPO, 5, "acme", "User", "x")
	assert cs.normalize_comment(bot_comment(signed), SECRET)["author_association"] == "OWNER"


@pytest.mark.parametrize("mutate", [
	lambda c: c.update(body=c["body"].replace("ok", "forged")),
	lambda c: c.update(issue_url=issue_url(6)),
	lambda c: c.update(issue_url=issue_url(5, "acme/other")),
	lambda c: c.update(body=c["body"].replace("as=owner-login", "as=someone")),
	lambda c: c.update(body=c["body"].replace("type=User", "type=Bot")),
	lambda c: c.update(body=c["body"].replace("issue=5", "issue=6")),
	lambda c: c.update(body=c["body"].replace("repo=acme/widgets", "repo=acme/other")),
	lambda c: (c.pop("issue_url"), c.update(html_url="https://github.com/acme/widgets/issues/6#issuecomment-10")),
])
def test_tampered_or_moved_comment_stays_untrusted(mutate):
	comment = bot_comment(cs.sign_body(SECRET, REPO, 5, "owner-login", "User", "ok"))
	mutate(comment)
	assert cs.normalize_comment(comment, SECRET) == comment


def test_comment_without_location_verifies_against_the_signed_location():
	comment = bot_comment(cs.sign_body(SECRET, REPO, 5, "owner-login", "User", "ok"))
	comment.pop("issue_url")
	assert cs.normalize_comment(comment, SECRET)["user"]["login"] == "owner-login"


def test_graphql_comment_nodes_are_normalized():
	signed = cs.sign_body(SECRET, REPO, 5, "owner-login", "User", "journal")
	forged = cs.sign_body("wrong", REPO, 5, "owner-login", "User", "forged")
	response = {"data": {"repository": {"issue": {"comments": {"nodes": [
		{"author": {"login": "github-actions", "__typename": "Bot"}, "authorAssociation": "NONE", "body": signed,
			"url": "https://github.com/acme/widgets/issues/5#issuecomment-1"},
		{"author": {"login": "github-actions"}, "body": forged},
		{"author": {"login": "owner-login"}, "body": "pat"},
	]}}}}}
	nodes = cs.normalize_value(response, SECRET)["data"]["repository"]["issue"]["comments"]["nodes"]
	assert nodes[0]["author"] == {"login": "owner-login", "__typename": "User"}
	assert nodes[0]["body"] == "journal" and nodes[0]["authorAssociation"] == "COLLABORATOR"
	assert nodes[1]["author"]["login"] == "github-actions" and nodes[1]["body"] == forged
	assert nodes[2] == {"author": {"login": "owner-login"}, "body": "pat"}


def test_wrong_secret_and_non_bot_author_are_untouched():
	signed = cs.sign_body(SECRET, REPO, 5, "owner-login", "User", "ok")
	assert cs.normalize_comment(bot_comment(signed), "other")["user"]["login"] == "github-actions[bot]"
	human = dict(bot_comment(signed), user={"login": "mallory", "type": "User"})
	assert cs.normalize_comment(human, SECRET) == human


def test_resigning_replaces_the_old_line():
	once = cs.sign_body(SECRET, REPO, 5, "owner-login", "User", "ok")
	assert cs.sign_body(SECRET, REPO, 5, "owner-login", "User", once) == once


@pytest.mark.parametrize("body,keep", [
	("/approved [auto-approved-by-plan]", True),
	("/answer [auto-answered-by-clarify]\nAuto", True),
	("intro\n/reclarify", True),
	("## Clarification required\nQ1", True),
	("Implementation Plan\n\n1. do", True),
	("## Clarification Required", True),
	("/Approved", True),
	("## Implementation plan", False),  # the hardened readers' regex does not match it either
	("AI autofix editor summary", False),
	("<!-- ORCHESTRATOR_STATE_V2 part=1/1 manifest=x -->", False),
])
def test_keep_caller_author(body, keep):
	assert cs.keep_caller_author(body) is keep


# ---------------------------------------------------------------------------
# Shim: activation and pass-through
# ---------------------------------------------------------------------------

def test_without_secret_no_shim_is_installed(env):
	result = run(env, "command -v gh", extra={"AI_COMMENT_SIGNING_SECRET": ""})
	assert result.stdout.strip() == str(env["tmp"] / "bin" / "gh")


def test_shim_installed_once_and_nested_sourcing_does_not_loop(env):
	script = f'command -v gh; bash -c \'_GH_HELPERS_LOADED=; source "{SCRIPTS}/gh_helpers.sh"; gh api repos/{REPO}/pulls/1\''
	result = run(env, script)
	assert result.returncode == 0, result.stderr
	assert "ai_comment_sig_shim." in result.stdout.splitlines()[0]
	assert calls(env)[-1]["args"] == ["api", f"repos/{REPO}/pulls/1"]


def test_stripped_environment_still_finds_real_gh(env):
	result = run(env, 'env -i PATH="$PATH" HOME="$HOME" FAKE_GH_STATE="$FAKE_GH_STATE" gh api rate_limit')
	assert result.returncode == 0, result.stderr
	assert calls(env)[-1]["args"] == ["api", "rate_limit"]


def test_unrelated_calls_pass_through_byte_for_byte(env):
	result = run(env, f'gh api repos/{REPO}/pulls/3 --jq .title; gh issue edit 3 --add-label x')
	assert result.returncode == 0, result.stderr
	assert [c["args"] for c in calls(env)] == [
		["api", f"repos/{REPO}/pulls/3", "--jq", ".title"],
		["issue", "edit", "3", "--add-label", "x"],
	]
	assert all(c["token"] == PAT for c in calls(env))


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

def test_informational_comment_is_signed_and_posted_with_job_token(env):
	result = run(env, f'gh_retry gh api "repos/{REPO}/issues/5/comments" -f body="AI autofix editor summary" --jq .id')
	assert result.returncode == 0, result.stderr
	post = comment_calls(env)
	assert len(post) == 1
	assert post[0]["token"] == JOB
	assert post[0]["args"][:4] == ["api", f"repos/{REPO}/issues/5/comments", "--method", "POST"]
	# The shim applies --jq itself, after normalizing the create response.
	assert "--jq" not in post[0]["args"]
	assert result.stdout.strip() == "1"
	body = post[0]["payload"]["body"]
	assert cs.normalize_comment(bot_comment(body), SECRET)["body"] == "AI autofix editor summary"


def test_identity_lookup_is_cached_per_job(env):
	run(env, f'gh api repos/{REPO}/issues/5/comments -f body=a; gh api repos/{REPO}/issues/5/comments -f body=b')
	assert sum(1 for c in calls(env) if c["args"][:2] == ["api", "user"]) == 1


@pytest.mark.parametrize("body", ["/answer [auto-answered-by-clarify]", "Clarification required\nQ1", "Implementation Plan\n1."])
def test_command_comments_keep_the_pat(env, body):
	result = run(env, f'gh api repos/{REPO}/issues/5/comments -f body="$B"', extra={"B": body})
	assert result.returncode == 0, result.stderr
	post = comment_calls(env)
	assert post == [{"args": ["api", f"repos/{REPO}/issues/5/comments", "-f", f"body={body}"], "token": PAT}]


def test_post_switch_off_keeps_the_pat_but_reads_still_verify(env):
	signed = cs.sign_body(SECRET, REPO, 5, "owner-login", "User", "old state")
	set_responses(env, [
		{"match": "-f body=new", "stdout": ""},
		{"match": "issues/5/comments", "stdout": _page(bot_comment(signed))},
	])
	script = f'gh api repos/{REPO}/issues/5/comments -f body=new; gh api repos/{REPO}/issues/5/comments --jq ".[0].user.login"'
	result = run(env, script, extra={"AI_COMMENT_SIGNING_POST": "false"})
	posts = comment_calls(env)
	assert posts[0] == {"args": ["api", f"repos/{REPO}/issues/5/comments", "-f", "body=new"], "token": PAT}
	assert result.stdout.strip() == "owner-login"


def test_other_repository_keeps_the_pat(env):
	run(env, 'gh api repos/acme/elsewhere/issues/5/comments -f body=x')
	assert comment_calls(env)[0]["token"] == PAT
	assert comment_calls(env)[0]["args"][-1] == "body=x"


def test_body_file_and_stdin_forms(env):
	(env["tmp"] / "b.md").write_text("from file")
	run(env, f'gh api repos/{REPO}/issues/5/comments -F body=@{env["tmp"]}/b.md; printf "from stdin" | gh api repos/{REPO}/issues/5/comments -F body=@-')
	bodies = [cs.strip_signature(c["payload"]["body"]) for c in comment_calls(env)]
	assert bodies == ["from file", "from stdin"]


def test_input_json_form_keeps_other_fields(env):
	(env["tmp"] / "p.json").write_text(json.dumps({"body": "hello"}))
	run(env, f'gh api repos/{REPO}/issues/5/comments --input {env["tmp"]}/p.json')
	assert cs.strip_signature(comment_calls(env)[0]["payload"]["body"]) == "hello"


def test_gh_issue_comment_form(env):
	result = run(env, f'gh issue comment 7 --repo {REPO} --body "phase marker"')
	assert result.returncode == 0, result.stderr
	post = comment_calls(env)[0]
	assert post["token"] == JOB
	assert post["args"][1] == f"repos/{REPO}/issues/7/comments"
	# `gh issue comment` prints the comment URL; the shim extracts it.
	assert result.stdout.strip() == "https://github.com/acme/widgets/issues/5#issuecomment-1"
	assert "AI_COMMENT_SIG outcome=posted" in result.stderr


def test_refused_job_token_falls_back_once_and_is_cached(env):
	set_responses(env, [{"match": "--method POST", "token": JOB, "rc": 1, "stderr": "HTTP 403: Resource not accessible by integration"}])
	result = run(env, f'gh api repos/{REPO}/issues/5/comments -f body=one; gh api repos/{REPO}/issues/5/comments -f body=two')
	assert result.returncode == 0, result.stderr
	posts = comment_calls(env)
	assert [p["token"] for p in posts] == [JOB, PAT, PAT]
	assert posts[1]["args"][-1] == "body=one"
	assert "outcome=fallback" in result.stderr


def test_refused_job_token_with_stdin_body_resends_rebuilt_payload(env):
	set_responses(env, [{"match": "--method POST", "token": JOB, "rc": 1, "stderr": "HTTP 403"}])
	run(env, f'printf "piped" | gh api repos/{REPO}/issues/5/comments -F body=@-')
	posts = comment_calls(env)
	assert [p["token"] for p in posts] == [JOB, PAT]
	assert posts[1]["payload"] == {"body": "piped"}


def test_rate_limited_job_token_falls_back_without_caching(env):
	set_responses(env, [{"match": "--method POST", "token": JOB, "rc": 1, "stderr": "HTTP 403: You have exceeded a secondary rate limit"}])
	result = run(env, f'gh api repos/{REPO}/issues/5/comments -f body=one; gh api repos/{REPO}/issues/5/comments -f body=two')
	assert result.returncode == 0, result.stderr
	# The second comment tries the job token again: the refusal was not cached.
	assert [p["token"] for p in comment_calls(env)] == [JOB, PAT, JOB, PAT]
	assert "reason=job_token_rate_limited" in result.stderr


def test_posted_comment_carries_the_pat_user_id(env):
	run(env, f'gh api repos/{REPO}/issues/5/comments -f body="escalation marker"')
	body = comment_calls(env)[0]["payload"]["body"]
	assert " uid=4242 sig=" in body
	normalized = cs.normalize_comment(bot_comment(body), SECRET)
	assert normalized["user"]["id"] == 4242 and normalized["user"]["login"] == "owner-login"
	# The id is signed: changing it breaks the signature.
	assert cs.normalize_comment(bot_comment(body.replace("uid=4242", "uid=4243")), SECRET)["user"]["login"] == cs.BOT_LOGIN


def test_ambiguous_failure_is_never_resent(env):
	set_responses(env, [{"match": "--method POST", "rc": 1, "stderr": "HTTP 502: Bad Gateway"}])
	result = run(env, f'gh api repos/{REPO}/issues/5/comments -f body=x')
	assert result.returncode == 1
	assert len(comment_calls(env)) == 1


def test_oversized_signed_body_keeps_the_pat(env):
	result = run(env, f'gh api repos/{REPO}/issues/5/comments -F body=@{env["tmp"]}/big.md', extra={})
	# file missing: parse error before stdin use -> passthrough to real gh
	assert comment_calls(env)[0]["token"] == PAT
	(env["tmp"] / "big.md").write_text("x" * 65_500)
	run(env, f'gh api repos/{REPO}/issues/5/comments -F body=@{env["tmp"]}/big.md')
	assert comment_calls(env)[-1]["token"] == PAT
	assert result is not None


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def _page(*comments):
	return json.dumps(list(comments))


def test_paginated_read_with_jq_sees_signed_comment_as_pat(env):
	signed = cs.sign_body(SECRET, REPO, 5, "owner-login", "User", "state")
	forged = cs.sign_body("wrong", REPO, 5, "owner-login", "User", "forged")
	set_responses(env, [{"match": "issues/5/comments", "stdout": _page(bot_comment(signed)) + _page(bot_comment(forged, cid=11))}])
	result = run(env, f'gh_retry gh api "repos/{REPO}/issues/5/comments?per_page=100" --paginate --jq \'.[] | "\\(.user.login) \\(.user.type) \\(.body | split("\\n")[0])"\'')
	assert result.returncode == 0, result.stderr
	assert result.stdout.splitlines() == ["owner-login User state", "github-actions[bot] Bot forged"]
	assert "--jq" not in comment_calls(env)[0]["args"]


def test_read_without_jq_returns_compact_pages(env):
	signed = cs.sign_body(SECRET, REPO, 5, "owner-login", "User", "state")
	set_responses(env, [{"match": "issues/5/comments", "stdout": _page(bot_comment(signed))}])
	result = run(env, f'gh api repos/{REPO}/issues/5/comments')
	page = json.loads(result.stdout)
	assert page[0]["body"] == "state" and page[0]["user"]["login"] == "owner-login"


def test_single_comment_read_and_include_headers(env):
	signed = cs.sign_body(SECRET, REPO, 5, "owner-login", "User", "one")
	set_responses(env, [{"match": "issues/comments/10", "stdout": "HTTP/2.0 200 OK\nLink: x\n\n" + json.dumps(bot_comment(signed))}])
	result = run(env, f'gh api -i repos/{REPO}/issues/comments/10')
	head, _, body = result.stdout.partition("\n\n")
	assert head.startswith("HTTP/2.0 200 OK")
	assert json.loads(body)["body"] == "one"


def test_graphql_read_through_the_shim(env):
	signed = cs.sign_body(SECRET, REPO, 5, "owner-login", "User", "marker")
	response = {"data": {"repository": {"issue": {"comments": {"nodes": [{"author": {"login": "github-actions"}, "body": signed}]}}}}}
	set_responses(env, [{"match": "api graphql", "stdout": json.dumps(response)}])
	(env["tmp"] / "q.graphql").write_text("query { x }")
	query_file = env["tmp"] / "q.graphql"
	result = run(env, f'gh api graphql -F query=@{query_file} --jq ".data.repository.issue.comments.nodes[0].author.login"')
	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == "owner-login"


def test_non_json_read_still_applies_jq_and_jq_errors_exit_1(env):
	set_responses(env, [{"match": "issues/5/comments", "stdout": "not json"}])
	result = run(env, f'gh api repos/{REPO}/issues/5/comments --jq length')
	assert result.returncode == 1
	assert "not json" not in result.stdout


def test_failed_read_is_returned_unchanged(env):
	set_responses(env, [{"match": "issues/5/comments", "rc": 1, "stdout": "partial", "stderr": "HTTP 500"}])
	result = run(env, f'gh api repos/{REPO}/issues/5/comments --jq length')
	assert result.returncode == 1
	assert result.stdout == "partial"


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------

def test_edit_of_signed_bot_comment_is_resigned(env):
	signed = cs.sign_body(SECRET, REPO, 5, "owner-login", "User", "old")
	set_responses(env, [{"match": "issues/comments/10 --method PATCH", "stdout": "{}"}, {"match": "issues/comments/10", "stdout": json.dumps(bot_comment(signed))}])
	run(env, f'gh api -X PATCH repos/{REPO}/issues/comments/10 -f body=new')
	patch = [c for c in comment_calls(env) if "PATCH" in c["args"]][-1]
	assert patch["token"] == PAT
	out = cs.normalize_comment(bot_comment(patch["payload"]["body"]), SECRET)
	assert out["body"] == "new" and out["user"]["login"] == "owner-login"


@pytest.mark.parametrize("author_comment", [
	{"id": 10, "issue_url": issue_url(5), "body": "pat", "user": {"login": "owner-login", "type": "User"}},
	bot_comment("unsigned bot"),
	bot_comment(cs.sign_body("wrong", REPO, 5, "owner-login", "User", "forged")),
])
def test_edit_of_unsigned_or_pat_comment_is_not_signed(env, author_comment):
	set_responses(env, [{"match": "issues/comments/10 -f", "stdout": "{}"}, {"match": "issues/comments/10", "stdout": json.dumps(author_comment)}])
	run(env, f'gh api -X PATCH repos/{REPO}/issues/comments/10 -f body=new')
	patch = comment_calls(env)[-1]
	assert patch["args"] == ["api", "-X", "PATCH", f"repos/{REPO}/issues/comments/10", "-f", "body=new"]


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_embedded_signer_matches_the_source_file():
	helpers = (SCRIPTS / "gh_helpers.sh").read_text()
	start = helpers.index("<<'AI_COMMENT_SIGNER_PY'\n") + len("<<'AI_COMMENT_SIGNER_PY'\n")
	end = helpers.index("\nAI_COMMENT_SIGNER_PY\n", start) + 1
	assert helpers[start:end] == (SCRIPTS / "comment_signature.py").read_text(), (
		"re-embed scripts/comment_signature.py into scripts/gh_helpers.sh"
	)


def test_shim_uses_the_embedded_signer_not_a_staged_copy(env):
	result = run(env, 'printf "%s" "$AI_COMMENT_SIG_PY"')
	assert result.stdout.startswith(str(env["tmp"] / "runner" / "ai_comment_sig_shim."))


def test_activation_is_job_wide_inside_actions(env):
	gh_path = env["tmp"] / "github_path"
	gh_env = env["tmp"] / "github_env"
	gh_path.write_text("")
	gh_env.write_text("")
	run(env, "true", extra={"GITHUB_PATH": str(gh_path), "GITHUB_ENV": str(gh_env)})
	shim_dir = gh_path.read_text().strip()
	assert "ai_comment_sig_shim." in shim_dir
	# Only the shim dir is persisted: no variables reach later steps.
	assert gh_env.read_text() == ""
	# A later step: PATH already carries the shim, so sourcing again adds nothing.
	later = run(env, "true", extra={
		"GITHUB_PATH": str(gh_path), "GITHUB_ENV": str(gh_env), "PATH": f"{shim_dir}:{env['env']['PATH']}",
		"AI_COMMENT_SIG_SHIM_DIR": shim_dir,
	})
	assert later.returncode == 0
	assert gh_path.read_text().strip() == shim_dir


def _later_step(env, shim_dir, script, secret=True):
	full = {k: v for k, v in env["env"].items() if not k.startswith("AI_COMMENT_SIG_")}
	full["PATH"] = f"{shim_dir}:{full['PATH']}"
	if not secret:
		full.pop("AI_COMMENT_SIGNING_SECRET", None)
	return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=full, check=False)


def test_later_step_reuses_the_persisted_shim(env):
	gh_path = env["tmp"] / "github_path"
	gh_path.write_text("")
	run(env, "true", extra={"GITHUB_PATH": str(gh_path)})
	shim_dir = gh_path.read_text().strip()
	full = {k: v for k, v in env["env"].items() if not k.startswith("AI_COMMENT_SIG_")}
	full["PATH"] = f"{shim_dir}:{full['PATH']}"
	full["GITHUB_PATH"] = str(gh_path)
	later = subprocess.run(
		["bash", "-c", f'source "{SCRIPTS}/gh_helpers.sh"; printf "%s" "$AI_COMMENT_SIG_SHIM_DIR"'],
		capture_output=True, text=True, env=full, check=False,
	)
	assert later.returncode == 0, later.stderr
	assert later.stdout == shim_dir
	# No second shim dir is written or appended for the later step.
	assert gh_path.read_text().strip() == shim_dir


def test_later_step_without_shim_variables_still_verifies(env):
	gh_path = env["tmp"] / "github_path"
	gh_path.write_text("")
	run(env, "true", extra={"GITHUB_PATH": str(gh_path)})
	shim_dir = gh_path.read_text().strip()
	signed = cs.sign_body(SECRET, REPO, 5, "owner-login", "User", "journal")
	set_responses(env, [{"match": "issues/5/comments", "stdout": _page(bot_comment(signed))}])
	read = f"gh api repos/{REPO}/issues/5/comments --jq '.[0].user.login'"
	result = _later_step(env, shim_dir, read)
	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == "owner-login"
	# A later step that does not hold the secret gets the real gh, unchanged.
	plain = _later_step(env, shim_dir, read, secret=False)
	assert '"login": "github-actions[bot]"' in plain.stdout and "ai-comment-sig:v1" in plain.stdout


def test_python_child_without_gh_helpers_goes_through_the_shim(env):
	signed = cs.sign_body(SECRET, REPO, 5, "owner-login", "User", "journal")
	set_responses(env, [{"match": "issues/5/comments", "stdout": _page(bot_comment(signed))}])
	code = "import subprocess;print(subprocess.run(['gh','api','repos/%s/issues/5/comments','--jq','.[0].user.login'],capture_output=True,text=True).stdout)" % REPO
	result = run(env, f"python3 -c \"{code}\"")
	assert result.stdout.strip() == "owner-login"


# ---------------------------------------------------------------------------
# Activation from scripts that do not load the helpers (activate-only mode)
# ---------------------------------------------------------------------------

def _bare(env, script: str) -> subprocess.CompletedProcess:
	"""Run a script with no shim on PATH, as a job's first step would."""
	return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env["env"], check=False)


def test_activate_only_mode_installs_the_shim_without_the_library(env):
	result = _bare(env, f'''AI_COMMENT_SIG_ACTIVATE_ONLY=1 source "{SCRIPTS}/gh_helpers.sh" >/dev/null 2>&1
command -v gh
type gh_retry >/dev/null 2>&1 && echo library-defined || echo library-absent
printf "loaded=%s activate_only=%s\\n" "${{_GH_HELPERS_LOADED:-}}" "${{AI_COMMENT_SIG_ACTIVATE_ONLY:-}}"
source "{SCRIPTS}/gh_helpers.sh"
type gh_retry >/dev/null 2>&1 && echo library-defined || echo library-absent
''')
	assert result.returncode == 0, result.stderr
	lines = result.stdout.splitlines()
	assert "ai_comment_sig_shim." in lines[0]
	# None of the library's functions exist yet, so a caller's own are never overridden.
	assert lines[1] == "library-absent"
	assert lines[2] == "loaded= activate_only="
	# A later full source still loads the library.
	assert lines[3] == "library-defined"


def test_script_using_the_activate_only_line_posts_signed(env, tmp_path):
	# The line the comment-posting scripts carry, run from a copy next to gh_helpers.sh.
	support = tmp_path / "support"
	support.mkdir()
	(support / "gh_helpers.sh").write_text((SCRIPTS / "gh_helpers.sh").read_text())
	poster = support / "poster.sh"
	poster.write_text(
		"#!/usr/bin/env bash\nset -euo pipefail\n"
		'[ -z "${BASH_SOURCE[0]:-}" ] || AI_COMMENT_SIG_ACTIVATE_ONLY=1 source "$(dirname "${BASH_SOURCE[0]}")/gh_helpers.sh" >/dev/null 2>&1 || true\n'
		f'gh api "repos/{REPO}/issues/5/comments" -f body="## Single-issue security pass" >/dev/null\n'
	)
	result = _bare(env, f'bash "{poster}"')
	assert result.returncode == 0, result.stderr
	post = comment_calls(env)
	assert [c["token"] for c in post] == [JOB]
	assert cs.normalize_comment(bot_comment(post[0]["payload"]["body"]), SECRET)["user"]["login"] == "owner-login"


ACTIVATE_LINE = '[ -z "${BASH_SOURCE[0]:-}" ] || AI_COMMENT_SIG_ACTIVATE_ONLY=1 source "$(dirname "${BASH_SOURCE[0]}")/gh_helpers.sh" >/dev/null 2>&1 || true'
ACTIVATED_SCRIPTS = (
	"ai_engine_fallback_report.sh", "implement_handle_guard_block.sh", "implement_heal_preflight.sh",
	"orchestrate_poll_process.sh", "orchestrate_poll_smoke_fixture_reap.sh", "plan_split_dispatch.sh",
	"post_review_comment.sh", "review_autofix_step_evaluate_gate.sh", "review_conflict_resolve.sh",
	"review_final_ci_failed.sh", "review_freshness_ci_failed.sh", "review_merge_train.sh",
	"review_rb_judge_security_pass.sh", "review_single_issue_security_pass.sh", "security_audit.sh",
	"unblock_judge.sh", "validate_process.sh", "workflow_failure_heal_phase_report.sh",
	"workflow_failure_heal_pr_reconcile.sh",
)


@pytest.mark.parametrize("name", ACTIVATED_SCRIPTS)
def test_comment_posting_scripts_activate_the_shim(name):
	lines = (SCRIPTS / name).read_text().splitlines()
	assert ACTIVATE_LINE in lines, name
	# Before the first command that could post (right after `set -...`, or the shebang).
	index = lines.index(ACTIVATE_LINE)
	assert all(not line or line.startswith(("#", "set -")) for line in lines[: index - 2]), name


PY_GH_RUNNERS = {
	"attempt_journal": "_gh",
	"check_triage_resolution": "_gh_api",
	"operator_step_issue": "_gh",
	"provider_outage": "GitHub._run",
	"review_security_hold_sweep": "_run_gh",
}


@pytest.mark.parametrize("module,runner", sorted(PY_GH_RUNNERS.items()))
def test_python_gh_runner_activates_before_running_gh(module, runner):
	import ast
	tree = ast.parse((SCRIPTS / f"{module}.py").read_text())
	scopes = {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
	target = scopes[runner.split(".")[0]]
	if "." in runner:
		target = next(n for n in target.body if isinstance(n, ast.FunctionDef) and n.name == runner.split(".")[1])
	body = [n for n in target.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
	first = body[0]
	assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call), (module, runner)
	assert getattr(first.value.func, "id", "") == "_activate_signed_comments", (module, runner)


@pytest.mark.parametrize("module", sorted(PY_GH_RUNNERS))
def test_python_helpers_put_the_shim_on_path(env, module, monkeypatch):
	spec = importlib.util.spec_from_file_location(f"_sig_{module}", SCRIPTS / f"{module}.py")
	mod = importlib.util.module_from_spec(spec)
	monkeypatch.syspath_prepend(str(SCRIPTS))
	spec.loader.exec_module(mod)
	for key, value in env["env"].items():
		monkeypatch.setenv(key, value)
	mod._activate_signed_comments()
	first = os.environ["PATH"].split(os.pathsep)[0]
	assert "ai_comment_sig_shim." in first and (Path(first) / "gh").is_file()
	# Once per process: a second call changes nothing.
	mod._activate_signed_comments()
	assert os.environ["PATH"].split(os.pathsep)[0] == first


def test_python_helper_without_secret_leaves_path(env, monkeypatch):
	spec = importlib.util.spec_from_file_location("_sig_nosecret", SCRIPTS / "attempt_journal.py")
	mod = importlib.util.module_from_spec(spec)
	monkeypatch.syspath_prepend(str(SCRIPTS))
	spec.loader.exec_module(mod)
	monkeypatch.delenv("AI_COMMENT_SIGNING_SECRET", raising=False)
	before = os.environ["PATH"]
	mod._activate_signed_comments()
	assert os.environ["PATH"] == before


def test_full_state_part_still_fits_once_signed():
	spec = importlib.util.spec_from_file_location("_sig_state_v2", SCRIPTS / "orchestrate_state_v2.py")
	state = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(state)
	# Longest repository, login and ids the signature line can carry.
	repo = ("o" * 39) + "/" + ("r" * 100)
	login = "l" * 39
	part = "x" * state.DEFAULT_CHUNK_SIZE
	signed = cs.sign_body(SECRET, repo, 9_999_999_999, login, "User", part, 99_999_999_999)
	assert len(signed) <= cs.MAX_BODY_CHARS, len(signed) - cs.MAX_BODY_CHARS

