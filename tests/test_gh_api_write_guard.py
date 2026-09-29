#!/usr/bin/env python3
"""Behaviour and wiring contract for the `gh api` permission guard (CLAUDE.md §23.H).

Covers the pieces that can silently detach the mechanism:
  1. The classification — reads and §23.B routine writes are not prompted,
     every other write (and any call the guard cannot read) is.
  2. The whole-call decision — allow only for one simple call (with safe
     helpers) or a read-only `for` loop over literal IDs, no decision for
     other multi-part commands, ask as soon as any call is a write.
  3. The fail-closed contract for malformed payloads and internal errors.
  4. The settings.json wiring (and the removed `gh api` ask rules), template
     parity, and the prose in CLAUDE.md / seed-repo / ci.yml.
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
GUARD_PATH = REPO_ROOT / ".claude" / "hooks" / "gh_api_write_guard.py"
TEMPLATE_GUARD_PATH = REPO_ROOT / "workflow-templates" / ".claude" / "hooks" / "gh_api_write_guard.py"
SETTINGS_PATH = REPO_ROOT / ".claude" / "settings.json"
TEMPLATE_SETTINGS_PATH = REPO_ROOT / "workflow-templates" / ".claude" / "settings.json"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
SEED_REPO_COMMAND = REPO_ROOT / ".claude" / "commands" / "seed-repo.md"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

LOCAL_SLUG = "shubhodeep1/coding-workflows"


def _load_guard():
	spec = importlib.util.spec_from_file_location("gh_api_write_guard", GUARD_PATH)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


guard = _load_guard()


@pytest.fixture(autouse=True)
def _fixed_local_slug(monkeypatch):
	monkeypatch.setattr(guard, "local_repo_slug", lambda cwd: LOCAL_SLUG)


def _decide(command: str) -> str | None:
	decision, _reason = guard.evaluate({"tool_name": "Bash", "tool_input": {"command": command}})
	return decision


def _run_hook(stdin_text: str) -> subprocess.CompletedProcess:
	env = dict(os.environ)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	return subprocess.run(
		[sys.executable, str(GUARD_PATH)],
		input=stdin_text,
		capture_output=True,
		text=True,
		env=env,
		check=False,
		cwd=str(REPO_ROOT),
	)


# ──────────────────────────────────────────────────────────────────
# The four commands that prompted in /implement-plan-claude sessions
# ──────────────────────────────────────────────────────────────────

# Verbatim (the heredoc body shortened) from the permission prompts that
# stopped unattended stage sessions. Each is a read or a routine write inside
# a multi-part command, so the guard makes no decision and the allow list /
# Auto-mode classifier decides, instead of the old ask rules forcing a prompt.
OBSERVED_PROMPTS = {
	"security follow-up search": (
		"gh api -X GET search/issues -f q='repo:shubhodeep1/coding-workflows is:issue label:ai:security "
		"created:>=2026-09-24' -f per_page=50 --jq '.items[] | [.number, .state, ([.labels[].name]|join(\",\")), "
		".title] | @tsv' | sort -n; echo ---; for n in 4303 4327 4365 4375 4516; do gh api "
		"repos/shubhodeep1/coding-workflows/pulls/$n --jq '[.number,.state,.merged_at,.base.ref,"
		".merge_commit_sha[0:7],.title]|@tsv'; done; echo ---; gh api repos/shubhodeep1/coding-workflows/issues/4546 "
		"--jq '[.number,.state,(.pull_request!=null),([.labels[].name]|join(\",\")),.title]|@tsv'"
	),
	"PR body update": (
		"F=/tmp/x/scratchpad/body4549.md; python3 - \"$F\" <<'EOF'\n"
		"import sys\n"
		"p=sys.argv[1]; s=open(p).read()\n"
		"reps=[(\"(candidates, fire/claim)\",\"(candidates, `queue-pending`)\")]\n"
		"for a,b in reps:\n"
		"    assert s.count(a)==1, a[:40]; s=s.replace(a,b)\n"
		"open(p,'w').write(s)\n"
		"EOF\n"
		"gh api -X PATCH repos/shubhodeep1/coding-workflows/pulls/4549 -F body=@$F --jq .html_url"
	),
	"issue list with query params": (
		"gh api -X GET repos/shubhodeep1/coding-workflows/issues -f labels=ai:security -f state=all "
		"-f since=2026-09-24T00:00:00Z -f per_page=100 --jq '.[] | select(.pull_request==null) | [.number, "
		".state, .closed_at, ([.labels[].name]|join(\",\")), .title[0:90]] | @tsv' | sort -n"
	),
	"commit search": (
		"gh api -X GET search/commits -f q='repo:shubhodeep1/coding-workflows \"[ai-autofix] apply PR fixes\" "
		"committer-date:>2026-09-25' -f per_page=30 --jq '.total_count, (.items[] | [.sha[0:8], "
		".commit.committer.date] | @tsv)' 2>&1 | head -30"
	),
}


@pytest.mark.parametrize("name", sorted(OBSERVED_PROMPTS))
def test_observed_prompts_are_no_longer_forced(name):
	assert _decide(OBSERVED_PROMPTS[name]) != guard.DECISION_ASK


# Later prompts from the same sessions whose whole command the guard now
# approves: `gh api` reads / routine writes (a §23.C allowlisted dispatch,
# a comment edit) beside `cd`, `sleep`, `2>&1`, and safe pipe filters.
OBSERVED_APPROVABLE_PROMPTS = {
	"issue list piped to sort": OBSERVED_PROMPTS["issue list with query params"],
	"commit search piped to head": OBSERVED_PROMPTS["commit search"],
	"security-audit dispatch then run lookup": (
		"gh api -X POST repos/shubhodeep1/coding-workflows/actions/workflows/security-audit.yml/dispatches -f ref=main "
		"-f 'inputs[ref]=claude/implement-plan-issue-4550-readme-pickup-restart-command' 2>&1; sleep 10; gh api "
		"\"repos/shubhodeep1/coding-workflows/actions/workflows/security-audit.yml/runs?per_page=2\" --jq "
		"'.workflow_runs[] | [.id,.status,.event,.created_at,.display_title] | @tsv'"
	),
	"progress comment edit after cd": (
		"cd /tmp/claude-0/-home-user-coding-workflows/8771d1c3-3ca6-520c-a7ca-c77ecd033a58/scratchpad && gh api "
		"-X PATCH repos/shubhodeep1/coding-workflows/issues/comments/5846305455 -F body=@pc.md --jq .updated_at"
	),
}


@pytest.mark.parametrize("name", sorted(OBSERVED_APPROVABLE_PROMPTS))
def test_observed_prompts_with_safe_helpers_are_allowed(name):
	assert _decide(OBSERVED_APPROVABLE_PROMPTS[name]) == guard.DECISION_ALLOW


# Prompts whose command also runs code the guard does not vet (a loop with
# `python3 -c` and `$(...)`, a `python3` heredoc and a `$S` redirect). Their
# `gh api` calls are reads or routine writes, so the guard forces nothing and
# leaves the decision to the allow list / Auto-mode classifier.
OBSERVED_UNVETTED_PROMPTS = {
	"validate dispatch with polling loop": (
		"gh api -X POST repos/shubhodeep1/coding-workflows/actions/workflows/internal-validate.yml/dispatches -f ref=main "
		"-f 'inputs[tracking_issue]=0' -f 'inputs[pr_number]=0' "
		"-f 'inputs[target_ref]=claude/implement-plan-issue-4550-readme-pickup-restart-command' && echo dispatched; "
		"for i in 1 2 3 4 5 6; do python3 -c 'import time; time.sleep(5)'; r=$(gh api "
		"\"repos/shubhodeep1/coding-workflows/actions/workflows/internal-validate.yml/runs?per_page=1&event=workflow_dispatch\" "
		"--jq '.workflow_runs[0] | {id,created_at,status}'); echo \"$r\"; echo \"$r\" | grep -q "
		"'\"created_at\":\"2026-09-27T0[2-9]:[0-5][0-9]' && break; done; date -u"
	),
	"progress comment rewrite through python heredoc": (
		"S=/tmp/claude-0/x/scratchpad; gh api repos/shubhodeep1/coding-workflows/issues/comments/5846305455 --jq .body "
		"> $S/body.md && python3 - \"$S/body.md\" <<'EOF'\n"
		"import sys\n"
		"p=sys.argv[1]; b=open(p).read()\n"
		"old_stage=\"**Stage:** security-pass 1/5\"\n"
		"new_stage=\"**Stage:** validation 1/3\"\n"
		"assert old_stage in b\n"
		"b=b.replace(old_stage,new_stage)\n"
		"open(p,'w').write(b)\n"
		"EOF\n"
		"gh api -X PATCH repos/shubhodeep1/coding-workflows/issues/comments/5846305455 -F body=@$S/body.md --jq '.updated_at'"
	),
}


@pytest.mark.parametrize("name", sorted(OBSERVED_UNVETTED_PROMPTS))
def test_observed_prompts_with_unvetted_code_get_no_decision(name):
	assert _decide(OBSERVED_UNVETTED_PROMPTS[name]) is None


# ──────────────────────────────────────────────────────────────────
# Single simple calls: allow reads and routine writes
# ──────────────────────────────────────────────────────────────────

ALLOWED_SIMPLE_CALLS = [
	"gh api repos/shubhodeep1/coding-workflows --jq .default_branch",
	"gh api -X GET search/issues -f q='repo:a/b is:issue' -f per_page=50 --jq '.items[].number'",
	"gh api --method=get repos/a/b/pulls -f state=open",
	"gh api -XHEAD repos/a/b",
	"gh api graphql -f query='query($o:String!){ repository(owner:$o, name:\"x\"){ id } }' -F o=a",
	"gh api -X PATCH repos/shubhodeep1/coding-workflows/pulls/4549 -F body=@/tmp/body.md --jq .html_url",
	"gh api -X PATCH repos/shubhodeep1/coding-workflows/issues/12 -f title=New -f body=Text",
	"gh api repos/shubhodeep1/coding-workflows/pulls -f title=T -f head=claude/x -f base=main -f body=B",
	"gh api repos/shubhodeep1/coding-workflows/issues/5/comments -f body=hello",
	"gh api -X PATCH repos/shubhodeep1/coding-workflows/issues/comments/99 -f body=edited",
	"gh api repos/shubhodeep1/coding-workflows/pulls/5/comments/77/replies -f body=done",
	"gh api -X PATCH repos/shubhodeep1/coding-workflows/pulls/comments/77 -f body=edited",
	"gh api repos/shubhodeep1/coding-workflows/issues/5/labels -f 'labels[]=ai:review'",
	"gh api -X DELETE repos/shubhodeep1/coding-workflows/issues/5/labels/ai:review-blocked",
	"gh api repos/shubhodeep1/coding-workflows/pulls/5/requested_reviewers -f 'reviewers[]=someone'",
	"gh api repos/{owner}/{repo}/issues/5/comments -f body=hi",
	"gh api repos/SHUBHODEEP1/Coding-Workflows/issues/5/comments -f body=case-insensitive",
]


@pytest.mark.parametrize("command", ALLOWED_SIMPLE_CALLS)
def test_simple_read_or_routine_call_is_allowed(command):
	assert _decide(command) == guard.DECISION_ALLOW


# ──────────────────────────────────────────────────────────────────
# Non-routine writes and unreadable calls: ask
# ──────────────────────────────────────────────────────────────────

ASK_CALLS = {
	"close PR": "gh api -X PATCH repos/shubhodeep1/coding-workflows/pulls/4549 -f state=closed",
	"close issue": "gh api -X PATCH repos/shubhodeep1/coding-workflows/issues/4 -f state=closed -f body=x",
	"change base": "gh api -X PATCH repos/shubhodeep1/coding-workflows/pulls/1 -f base=other",
	"delete branch": "gh api repos/shubhodeep1/coding-workflows/git/refs/heads/main -X DELETE",
	"implicit POST": "gh api repos/shubhodeep1/coding-workflows/git/refs -f ref=refs/heads/x -f sha=abc",
	"merge": "gh api -X PUT repos/shubhodeep1/coding-workflows/pulls/1/merge",
	"dispatch": "gh api repos/shubhodeep1/coding-workflows/actions/workflows/x.yml/dispatches -f ref=main",
	"repo settings": "gh api -X PATCH repos/shubhodeep1/coding-workflows -f visibility=public",
	"replace labels": "gh api -X PUT repos/shubhodeep1/coding-workflows/issues/5/labels -f 'labels[]=x'",
	"clear labels": "gh api -X DELETE repos/shubhodeep1/coding-workflows/issues/5/labels",
	"other repo": "gh api repos/evil/repo/issues/1/comments -f body=hi",
	"input body": "gh api -X PATCH repos/shubhodeep1/coding-workflows/pulls/1 --input body.json",
	"dynamic write path": "gh api repos/shubhodeep1/coding-workflows/issues/$N/comments -f body=hi",
	"method override header": "gh api repos/a/b -H 'X-HTTP-Method-Override: DELETE'",
	"graphql mutation": "gh api graphql -f query='mutation { addStar(input:{starrableId:\"x\"}) { clientMutationId } }'",
	"graphql from file": "gh api graphql -F query=@q.graphql",
	"graphql expanded": "gh api graphql -f query=\"$Q\"",
	"unknown flag": "gh api -R a/b repos/a/b",
	"no endpoint": "gh api -X GET",
	"two endpoints": "gh api repos/a/b repos/c/d",
	"unknown method": "gh api -X PURGE repos/a/b",
	"write inside compound": "echo start && gh api -X DELETE repos/shubhodeep1/coding-workflows/git/refs/heads/x",
	"write in loop": "for n in 1 2; do gh api -X PATCH repos/shubhodeep1/coding-workflows/issues/$n -f state=closed; done",
	"unquoted substitution": "echo $(gh api -X DELETE repos/shubhodeep1/coding-workflows/git/refs/heads/x)",
	"quoted substitution": "echo \"$(gh api -X DELETE repos/a/b)\"",
	"unquoted backtick": "echo `gh api -X DELETE repos/a/b`",
	"backtick assignment": "x=`gh api -X DELETE repos/a/b`",
	"double-quoted backtick": "echo \"`gh api -X DELETE repos/a/b`\"",
	"backtick inside quoted substitution": "echo \"$(echo `gh api -X DELETE repos/a/b`)\"",
	"unterminated backtick": "echo `gh api -X DELETE repos/a/b",
	"quoted paren inside quoted substitution": "echo \"$(echo \")\"; gh api -X DELETE repos/a/b; echo \"(\")\"",
	"single-quoted paren inside quoted substitution": "echo \"$(echo ')'; gh api -X DELETE repos/a/b)\"",
	"backtick paren inside quoted substitution": "echo \"$(echo `echo )`; gh api -X DELETE repos/a/b)\"",
	"substitution in field": "gh api repos/{owner}/{repo}/issues/1/comments -f body=\"$(gh api -X DELETE repos/a/b)\"",
	"bash -c": "bash -c 'gh api -X DELETE repos/a/b'",
	"sudo": "sudo gh api -X DELETE repos/a/b",
	"xargs": "echo 1 | xargs -I{} gh api repos/a/b/pulls/{}",
	"heredoc into bash": "bash <<'EOF'\ngh api -X DELETE repos/a/b\nEOF",
	"heredoc into python": "python3 - <<'EOF'\nimport os; os.system('gh api -X DELETE repos/a/b')\nEOF",
	"unbalanced quotes": "gh api repos/a/b -f body='oops",
}


@pytest.mark.parametrize("name", sorted(ASK_CALLS))
def test_non_routine_or_unreadable_call_asks(name):
	assert _decide(ASK_CALLS[name]) == guard.DECISION_ASK


def test_routine_write_asks_when_local_repo_is_unknown(monkeypatch):
	monkeypatch.setattr(guard, "local_repo_slug", lambda cwd: "")
	assert _decide("gh api repos/shubhodeep1/coding-workflows/issues/5/comments -f body=x") == guard.DECISION_ASK
	# The placeholders always mean the local checkout's repository.
	assert _decide("gh api repos/{owner}/{repo}/issues/5/comments -f body=x") == guard.DECISION_ALLOW


# ──────────────────────────────────────────────────────────────────
# No decision: no gh api call, gh api as data, or reads in compound commands
# ──────────────────────────────────────────────────────────────────

NO_DECISION_COMMANDS = [
	"ls -la && git status",
	"gh pr view 5 -R a/b",
	"git commit -m \"Use gh api -X DELETE carefully\"",
	"grep -rn \"gh api\" .claude/commands | head",
	"git commit -F- <<'EOF'\nmention gh api -X DELETE here\nEOF",
	"git commit -m \"$(cat <<'EOF'\nDocument gh api -X DELETE\nEOF\n)\"",
	"echo $(gh api repos/a/b --jq .id)",
	# Single-quoted text is data: Bash runs no substitution inside it.
	"sed -i 's|^- Last note: .*|- Last note: `gh api user` = me|' docs/log.md && git add docs/log.md",
	"f=docs/log.md && sed -i 's|a|`gh api user`|' $f && git commit -q -m \"docs\" && git push -q origin b 2>&1 | tail -2",
	"git commit -m 'uses `gh api user` to check auth'",
	"echo 'later: $(gh api -X DELETE repos/a/b)'",
	"git commit -m \"see \\`gh api user\\` in the docs\"",
	"gh api repos/a/b/pulls/$n --jq .title",
	"gh api -X PATCH repos/shubhodeep1/coding-workflows/pulls/1 -F body=@\"$F\"",
	"gh api -X GET search/issues -f 'q=repo:a/b is:open' > /tmp/out.json",
]


@pytest.mark.parametrize("command", NO_DECISION_COMMANDS)
def test_commands_without_a_forced_outcome_get_no_decision(command):
	assert _decide(command) is None


# ──────────────────────────────────────────────────────────────────
# Safe helpers beside gh api calls (CLAUDE.md §23.H)
# ──────────────────────────────────────────────────────────────────

APPROVABLE_WITH_HELPERS = [
	"gh api repos/a/b/pulls --jq '.[].number' | sort -n",
	"gh api repos/a/b/pulls --jq '.[].number' | sort -rn -u",
	"gh api repos/a/b/pulls --jq '.[] | [.number,.title] | @tsv' | sort -t , -k2,2 -r",
	"gh api repos/a/b/pulls --jq '.[].number' | sort -k 1 -t:",
	"gh api repos/a/b/pulls --jq '.[].number' 2>&1 | head -30",
	"gh api repos/a/b/pulls --jq '.[].number' | head -n 5",
	"gh api repos/a/b/pulls --jq '.[].number' | tail -n5",
	"gh api repos/a/b/pulls --jq '.[].number' | sort -n | head -3",
	"gh api repos/a/b/pulls --jq '.[].number' | wc -l",
	"cd /tmp/scratch && gh api repos/a/b --jq .id",
	"cd /tmp/scratch; gh api repos/a/b --jq .id; echo ---; gh api repos/c/d --jq .id",
	"gh api repos/a/b --jq .id; sleep 10; gh api repos/a/b --jq .id",
	"gh api repos/a/b --jq .id && sleep 2.5 && true",
	"echo \"step one\" && gh api repos/a/b --jq .id;",
]


@pytest.mark.parametrize("command", APPROVABLE_WITH_HELPERS)
def test_gh_api_with_safe_helpers_is_allowed(command):
	assert _decide(command) == guard.DECISION_ALLOW


NOT_APPROVABLE_HELPERS = [
	"gh api repos/a/b/pulls --jq '.[].number' | sort -o /tmp/x",
	"gh api repos/a/b/pulls --jq '.[].number' | sort /etc/passwd",
	"gh api repos/a/b/pulls --jq '.[].number' | head /etc/passwd",
	"gh api repos/a/b/pulls --jq '.[].number' | tee /tmp/x",
	"gh api repos/a/b/pulls --jq '.[].number' | sh",
	"gh api repos/a/b --jq .id && rm -rf /tmp/x",
	"gh api repos/a/b --jq .id > /tmp/x",
	"gh api repos/a/b --jq .id 2>/tmp/err",
	"gh api repos/a/b --jq .id &",
	"gh api repos/a/b --jq .id || echo failed",
	"echo hi | gh api repos/a/b --input -",
	"sleep 1; sleep 2",
	"cd /tmp a b && gh api repos/a/b",
	"sleep forever; gh api repos/a/b",
	"cd ~/x && gh api repos/a/b",
	"echo $HOME && gh api repos/a/b",
	"(gh api repos/a/b)",
	"gh api repos/a/b ; ; gh api repos/c/d",
	"true 1 && gh api repos/a/b",
]


@pytest.mark.parametrize("command", NOT_APPROVABLE_HELPERS)
def test_gh_api_beside_anything_else_is_not_allowed(command):
	assert _decide(command) != guard.DECISION_ALLOW


# ──────────────────────────────────────────────────────────────────
# Read-only for loops over literal IDs (CLAUDE.md §23.H, issue #4786)
# ──────────────────────────────────────────────────────────────────

# The #4707 implement session waited about 3.5 hours on this read.
INCIDENT_READ_LOOP = (
	"for r in 36242690892 36224773465 36205375333 36078283644 35966436009; "
	"do gh run view $r --json createdAt,updatedAt,conclusion; done"
)

READ_LOOP_ALLOWED = {
	"incident gh run view": INCIDENT_READ_LOOP,
	"gh api jobs": "for r in 1 2; do gh api repos/o/r/actions/runs/$r/jobs --jq '.jobs[].name'; done",
	"braced variable": "for r in 1 2; do gh api repos/o/r/actions/runs/${r}/jobs; done",
	"two reads joined by &&": (
		"for r in 1 2; do gh run view $r --json status && gh api repos/o/r/actions/runs/$r --jq .conclusion; done"
	),
	"read piped into head": "for r in 1 2; do gh api repos/o/r/actions/runs/$r/jobs --jq '.jobs[].name' | head -5; done",
	"echo then run list": "for r in a b; do echo $r; gh run list -R o/r -L 5 --json databaseId; done",
	"pr view": "for r in 4703 4704; do gh pr view $r --json state,mergedAt; done",
	"stderr to stdout": "for r in 1 2; do gh run view $r --log-failed 2>&1 | tail -n 20; done",
	"double-quoted variable": "for r in 1 2; do gh run view \"$r\" --json status; echo \"run ${r}\"; done",
	"literal echo and long flag with value": "for r in 1; do echo ---; gh pr view $r --repo=o/r --comments; done",
}


@pytest.mark.parametrize("name", sorted(READ_LOOP_ALLOWED))
def test_read_only_loop_over_literal_ids_is_allowed(name):
	assert _decide(READ_LOOP_ALLOWED[name]) == guard.DECISION_ALLOW


READ_LOOP_NO_DECISION = {
	"token with $": "for r in $X 2; do gh run view $r; done",
	"token with glob": "for r in *; do gh run view $r; done",
	"quoted token": "for r in '1' 2; do gh run view $r; done",
	"token starting with -": "for r in --web; do gh pr view $r; done",
	"substitution in tokens": "for r in $(seq 3); do gh run view $r; done",
	"variable in --jq value": "for r in 1 2; do gh run view 5 --jq $r; done",
	"variable in -R value": "for r in 1 2; do gh pr view 5 -R $r; done",
	"variable in field": "for r in 1 2; do gh api -X GET search/issues -f q=$r; done",
	"variable after ?": "for r in 1 2; do gh api \"repos/o/r/pulls?page=$r\"; done",
	"variable as first path segment": "for r in graphql; do gh api $r -X GET; done",
	"file redirect": "for r in 1 2; do gh run view $r > /tmp/x; done",
	"stderr file redirect": "for r in 1 2; do gh api repos/o/r/actions/runs/$r 2>/tmp/e; done",
	"python3 in body": "for r in 1 2; do gh run view $r; python3 -c 'print(1)'; done",
	"nested loop": "for r in 1 2; do for s in 3; do gh run view $s; done; done",
	"while loop": "while true; do gh run view 5; done",
	"gh run view --web": "for r in 1; do gh run view --web $r; done",
	"gh pr view -w": "for r in 1; do gh pr view -w $r; done",
	"uppercase loop variable": "for PATH in .; do gh run view 5; done",
	"proxy loop variable": "for https_proxy in evil.example; do gh api repos/o/r/actions/runs/1; done",
	"gh issue view": "for r in 1; do gh issue view $r; done",
	"newline-separated loop": "for r in 1 2\ndo gh run view $r\ndone",
	"loop after another command": "echo x; for r in 1; do gh run view $r; done",
	"gh by path": "for r in 1; do /usr/bin/gh run view $r; done",
	"other variable": "for r in 1; do gh run view $OTHER; done",
	"longer variable name": "for r in 1; do gh run view $rx; done",
	"backslash": "for r in 1; do gh run view \\$r; done",
	"or-list": "for r in 1; do gh run view $r || true; done",
	"piped into sh": "for r in 1; do gh run view $r | sh; done",
	"done piped": "for r in 1; do gh run view $r; done | head",
	"trailing semicolon after done": "for r in 1; do gh api repos/o/r/actions/runs/$r; done;",
	"gh api read beside other variable": "for r in 1; do gh api repos/o/r/actions/runs/$r; echo $HOME; done",
	"routine write in body": (
		"for r in 1 2; do echo $r; gh api repos/shubhodeep1/coding-workflows/issues/5/comments -f body=hi; done"
	),
	# A quoted `gh api` never matches the raw-text fast path, so it is never
	# classified; the loop must not be allowed around it.
	"single-quoted api DELETE beside a read": (
		"for r in 1; do gh run view $r; gh 'api' -X DELETE repos/o/r/issues/$r; done"
	),
	"double-quoted gh DELETE beside a read": "for r in 1; do gh pr view $r; \"gh\" api -X DELETE repos/o/r/issues/1; done",
	"split api word mutation beside a read": (
		"for r in 1; do gh run list; gh ap''i graphql -f query='mutation{x}'; done"
	),
	"quoted api read beside a read": "for r in 1; do gh run view $r; gh 'api' repos/o/r/actions/runs/$r; done",
}


@pytest.mark.parametrize("name", sorted(READ_LOOP_NO_DECISION))
def test_loop_outside_the_read_only_shape_gets_no_decision(name):
	assert _decide(READ_LOOP_NO_DECISION[name]) is None


READ_LOOP_ASK = {
	"write in loop": ASK_CALLS["write in loop"],
	"DELETE in loop": "for r in 1 2; do gh api -X DELETE repos/o/r/issues/$r; done",
	"POST beside a read": (
		"for r in 1 2; do gh api repos/o/r/actions/runs/$r; gh api repos/o/r/git/refs -f ref=x -f sha=abc; done"
	),
}


@pytest.mark.parametrize("name", sorted(READ_LOOP_ASK))
def test_write_in_a_loop_still_asks(name):
	assert _decide(READ_LOOP_ASK[name]) == guard.DECISION_ASK


def test_hook_process_allows_the_incident_read_loop():
	result = _run_hook(json.dumps({"tool_name": "Bash", "tool_input": {"command": INCIDENT_READ_LOOP}}))
	assert result.returncode == 0
	assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"


# ──────────────────────────────────────────────────────────────────
# Dispatches of the allowlisted workflows (Q7)
# ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("workflow", sorted(guard.DISPATCHABLE_WORKFLOWS))
def test_allowlisted_workflow_dispatch_is_routine(workflow):
	command = (
		f"gh api -X POST repos/shubhodeep1/coding-workflows/actions/workflows/{workflow}/dispatches "
		"-f ref=main -f 'inputs[target_ref]=claude/x'"
	)
	assert _decide(command) == guard.DECISION_ALLOW


@pytest.mark.parametrize(
	"command",
	[
		"gh api repos/shubhodeep1/coding-workflows/actions/workflows/release.yml/dispatches -f ref=main",
		"gh api repos/shubhodeep1/coding-workflows/actions/workflows/security-audit.yml/dispatches -f ref=main -f extra=1",
		"gh api repos/other/repo/actions/workflows/security-audit.yml/dispatches -f ref=main",
		"gh api repos/shubhodeep1/coding-workflows/dispatches -f event_type=x",
		"gh api -X POST repos/shubhodeep1/coding-workflows/actions/runs/1/rerun",
	],
)
def test_other_dispatches_ask(command):
	assert _decide(command) == guard.DECISION_ASK


@pytest.mark.parametrize("path", [SETTINGS_PATH, TEMPLATE_SETTINGS_PATH])
def test_dispatchable_workflows_match_the_gh_workflow_run_allow_rules(path):
	allow = json.loads(path.read_text(encoding="utf-8"))["permissions"]["allow"]
	allowlisted = {
		rule[len("Bash(gh workflow run ") : -len(" *)")]
		for rule in allow
		if rule.startswith("Bash(gh workflow run ") and rule.endswith(" *)")
	}
	assert allowlisted == set(guard.DISPATCHABLE_WORKFLOWS)


def test_non_bash_tools_get_no_decision():
	payload = {"tool_name": "Edit", "tool_input": {"command": "gh api -X DELETE repos/a/b"}}
	assert guard.evaluate(payload) == (None, "")


def test_ask_reason_names_the_offending_call():
	decision, reason = guard.evaluate(
		{"tool_name": "Bash", "tool_input": {"command": "gh api -X PATCH repos/shubhodeep1/coding-workflows/pulls/1 -f state=closed"}}
	)
	assert decision == guard.DECISION_ASK
	assert "PATCH repos/shubhodeep1/coding-workflows/pulls/1" in reason
	assert "state" in reason
	assert "§23.H" in reason


# ──────────────────────────────────────────────────────────────────
# Parsing helpers
# ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
	("args", "method"),
	[
		(["repos/a/b"], "GET"),
		(["repos/a/b", "-f", "x=1"], "POST"),
		(["repos/a/b", "--input", "f.json"], "POST"),
		(["-X", "patch", "repos/a/b"], "PATCH"),
		(["--method", "DELETE", "repos/a/b"], "DELETE"),
		(["-XPUT", "repos/a/b"], "PUT"),
		(["-X", "GET", "repos/a/b", "-f", "q=1"], "GET"),
	],
)
def test_effective_method_matches_gh(args, method):
	assert guard.parse_gh_api_args(args)["method"] == method


def test_field_keys_drop_array_suffixes():
	parsed = guard.parse_gh_api_args(["repos/a/b", "-f", "labels[]=x", "-F", "a[b][c]=1"])
	assert [key for _role, key, _value in parsed["fields"]] == ["labels", "a"]


@pytest.mark.parametrize(
	("url", "slug"),
	[
		("https://github.com/shubhodeep1/coding-workflows.git", "shubhodeep1/coding-workflows"),
		("git@github.com:shubhodeep1/coding-workflows.git", "shubhodeep1/coding-workflows"),
		("http://local_proxy@127.0.0.1:1234/git/shubhodeep1/coding-workflows", "shubhodeep1/coding-workflows"),
		("https://evilgithub.com/a/b", ""),
		("http://127.0.0.1:1234/other/a/b", ""),
		("not a url", ""),
	],
)
def test_extract_repo_slug(url, slug):
	assert guard.extract_repo_slug(url) == slug


def test_heredoc_bodies_are_split_out():
	stripped, heredocs = guard.strip_heredoc_bodies("cat <<'EOF'\nbody line\nEOF\necho after")
	assert stripped == "cat <<'EOF'\necho after"
	assert heredocs == [("cat ", True, "body line")]


def test_redirects_are_not_arguments():
	segments = guard.shell_segments("gh api repos/a/b 2>&1 > out.txt | head -3")
	assert segments == [["gh", "api", "repos/a/b"], ["head", "-3"]]


# ──────────────────────────────────────────────────────────────────
# Hook process and the fail-closed contract
# ──────────────────────────────────────────────────────────────────


def test_hook_process_emits_ask_json_for_a_write():
	payload = {"tool_name": "Bash", "tool_input": {"command": "gh api -X DELETE repos/a/b/git/refs/heads/x"}}
	result = _run_hook(json.dumps(payload))
	assert result.returncode == 0
	output = json.loads(result.stdout)["hookSpecificOutput"]
	assert output["hookEventName"] == "PreToolUse"
	assert output["permissionDecision"] == "ask"


def test_hook_process_is_silent_without_gh_api():
	result = _run_hook(json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}}))
	assert result.returncode == 0
	assert result.stdout == ""


@pytest.mark.parametrize("stdin_text", ["", "   "])
def test_empty_payload_is_allowed_silently(stdin_text):
	result = _run_hook(stdin_text)
	assert result.returncode == 0
	assert result.stdout == ""


@pytest.mark.parametrize("stdin_text", ["not json", "[1, 2, 3]", '"a string"'])
def test_malformed_payload_asks(stdin_text):
	result = _run_hook(stdin_text)
	assert result.returncode == 0
	assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_internal_exception_asks(monkeypatch, capsys):
	def boom(payload):
		raise RuntimeError("kaboom")

	monkeypatch.setattr(guard, "evaluate", boom)
	monkeypatch.setattr(sys, "stdin", type("S", (), {"read": staticmethod(lambda: "{}")})())
	assert guard.main() == 0
	output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
	assert output["permissionDecision"] == "ask"
	assert "kaboom" in output["permissionDecisionReason"]


def test_hook_has_no_environment_escape_hatch():
	source = GUARD_PATH.read_text(encoding="utf-8")
	assert "os.environ" not in source
	assert "getenv" not in source


def test_hook_issues_no_github_api_calls():
	source = GUARD_PATH.read_text(encoding="utf-8")
	assert '["gh"' not in source
	assert "api.github.com" not in source


# ──────────────────────────────────────────────────────────────────
# Wiring
# ──────────────────────────────────────────────────────────────────


def _guard_entries(settings: dict) -> list[dict]:
	return [
		entry
		for entry in settings["hooks"]["PreToolUse"]
		if any("gh_api_write_guard.py" in hook.get("command", "") for hook in entry.get("hooks", []))
	]


@pytest.mark.parametrize("path", [SETTINGS_PATH, TEMPLATE_SETTINGS_PATH])
def test_settings_wire_the_guard_once_under_bash(path):
	settings = json.loads(path.read_text(encoding="utf-8"))
	entries = _guard_entries(settings)
	assert len(entries) == 1
	entry = entries[0]
	assert entry["matcher"] == guard.SETTINGS_MATCHER == "Bash"
	assert len(entry["hooks"]) == 1
	hook = entry["hooks"][0]
	assert hook["type"] == "command"
	assert hook["command"].startswith("python3 ")
	assert hook["command"].endswith('/.claude/hooks/gh_api_write_guard.py')


@pytest.mark.parametrize("path", [SETTINGS_PATH, TEMPLATE_SETTINGS_PATH])
def test_settings_no_longer_carry_gh_api_ask_rules(path):
	# A permissions.ask rule prompts even when a hook allows (and in Auto mode),
	# so any `gh api` ask rule would bring the prompts back.
	permissions = json.loads(path.read_text(encoding="utf-8"))["permissions"]
	assert not any(rule.startswith("Bash(gh api") for rule in permissions.get("ask", []))


@pytest.mark.parametrize("path", [SETTINGS_PATH, TEMPLATE_SETTINGS_PATH])
def test_other_bash_guards_stay_wired(path):
	settings = json.loads(path.read_text(encoding="utf-8"))
	commands = [
		hook["command"]
		for entry in settings["hooks"]["PreToolUse"]
		if entry.get("matcher") == "Bash"
		for hook in entry["hooks"]
	]
	assert any("pr_merge_status_guard.py" in command for command in commands)


def test_template_parity():
	assert TEMPLATE_GUARD_PATH.read_text(encoding="utf-8") == GUARD_PATH.read_text(encoding="utf-8")
	assert TEMPLATE_SETTINGS_PATH.read_text(encoding="utf-8") == SETTINGS_PATH.read_text(encoding="utf-8")


def test_claude_md_documents_the_guard():
	text = CLAUDE_MD.read_text(encoding="utf-8")
	assert "### H) Permission Guard for `gh api`" in text
	assert ".claude/hooks/gh_api_write_guard.py" in text
	assert "tests/test_gh_api_write_guard.py" in text


def test_seed_repo_command_ships_the_hook():
	assert "hooks/gh_api_write_guard.py" in SEED_REPO_COMMAND.read_text(encoding="utf-8")


def test_ci_runs_this_file():
	assert "tests/test_gh_api_write_guard.py" in CI_WORKFLOW.read_text(encoding="utf-8")


# ──────────────────────────────────────────────────────────────────
# Substitution scanning (quote-aware)
# ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("command, bodies", [
	("echo `a b`", ["a b"]),
	("echo '`a b`'", []),
	("echo \"`a b`\"", ["a b"]),
	("echo \"$(a (b) c)\" d", ["a (b) c"]),
	("echo $(a b)", []),
	("echo '$(a b)' \"$(c)\"", ["c"]),
	("echo \\`a\\` b", []),
	("echo `a", ["a"]),
	("echo \"$(a", ["a"]),
	("echo \"$(echo \")\"; b)\"", ["echo \")\"; b"]),
	("echo \"$(echo ')'; b)\"", ["echo ')'; b"]),
	("echo \"$(echo \\) b)\"", ["echo \\) b"]),
	("echo \"$(echo `echo )` b)\"", ["echo `echo )` b"]),
	("echo \"$(echo `echo \"` b)\"", ["echo `echo \"` b"]),
])
def test_substitution_bodies_follow_bash_quoting(command, bodies):
	assert guard.substitution_bodies(command) == bodies
