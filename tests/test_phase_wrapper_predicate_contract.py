#!/usr/bin/env python3
"""Contract tests for reusable phase predicates and their caller wrappers."""

from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent

PHASE_WORKFLOWS = {
	"clarify": (
		".github/workflows/clarify.yml",
		".github/workflows/internal-clarify.yml",
		"workflow-templates/ai-clarify.yml",
	),
	"plan": (
		".github/workflows/plan.yml",
		".github/workflows/internal-plan.yml",
		"workflow-templates/ai-plan.yml",
	),
	"implement": (
		".github/workflows/implement.yml",
		".github/workflows/internal-implement.yml",
		"workflow-templates/ai-implement.yml",
	),
	"respond": (
		".github/workflows/orchestrate_clarify_respond.yml",
		".github/workflows/internal-orchestrate-clarify-respond.yml",
		"workflow-templates/ai-orchestrate-clarify-respond.yml",
	),
}


def _normalized_job_predicate(relative_path: str, job_name: str) -> str:
	workflow_path = REPO_ROOT / relative_path
	workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
	predicate = workflow["jobs"][job_name]["if"]
	assert isinstance(predicate, str), f"{relative_path} jobs.{job_name}.if must be a string"
	return " ".join(predicate.split())


def _canonical_predicate(job_name: str) -> str:
	return _normalized_job_predicate(PHASE_WORKFLOWS[job_name][0], job_name)


def _assert_clauses(predicate: str, clauses: tuple[str, ...]) -> None:
	for clause in clauses:
		assert clause in predicate, f"Predicate missing security-sensitive clause: {clause}"


# The consumer ai-implement.yml template runs verify-automated-approval before
# implement (hardened automation, vars.AI_PIPELINE_APPROVER_LOGIN). Its job
# predicate is the canonical one plus exactly these wrapper-only clauses.
IMPLEMENT_TEMPLATE_ONLY_CLAUSES = (
	("!cancelled() && ", ""),
	(
		"contains(github.event.comment.body, '[auto-approved-by-plan]') && "
		"needs.verify-automated-approval.outputs.approved == 'true'",
		"contains(github.event.comment.body, '[auto-approved-by-plan]')",
	),
)


def _without_template_only_clauses(wrapper_path: str, predicate: str) -> str:
	if wrapper_path != "workflow-templates/ai-implement.yml":
		return predicate
	for clause, replacement in IMPLEMENT_TEMPLATE_ONLY_CLAUSES:
		assert predicate.count(clause) == 1, f"{wrapper_path} lost wrapper-only clause {clause!r}"
		predicate = predicate.replace(clause, replacement)
	return predicate


def test_internal_and_consumer_predicates_match_reusable_workflows() -> None:
	for job_name, workflow_paths in PHASE_WORKFLOWS.items():
		canonical = _normalized_job_predicate(workflow_paths[0], job_name)
		for wrapper_path in workflow_paths[1:]:
			actual = _without_template_only_clauses(wrapper_path, _normalized_job_predicate(wrapper_path, job_name))
			assert actual == canonical, f"{wrapper_path} jobs.{job_name}.if drifted from {workflow_paths[0]}"


def test_clarify_predicate_preserves_opened_and_trusted_reclarify_routes() -> None:
	_assert_clauses(
		_canonical_predicate("clarify"),
		(
			"github.event_name == 'issues'",
			"github.event.action == 'opened'",
			"'ai:orchestrator-tracking'",
			"'ai:security-audit'",
			"'ai:retro'",
			"!contains(toJson(github.event.issue.labels.*.name), 'ai:operator-step')",
			"!contains(toJson(github.event.issue.labels.*.name), 'ai:provider-outage')",
			# Consumer health notices are operator reports, not work items.
			"!contains(toJson(github.event.issue.labels.*.name), 'ai:consumer-health')",
			# Scheduled-validation failure issues are automation reports (#7553).
			"!contains(toJson(github.event.issue.labels.*.name), 'ai:scheduled-validation-failed')",
			# The heal intake report queue issue is automation state, not a task.
			"!contains(toJson(github.event.issue.labels.*.name), 'ai:heal-intake-queue')",
			"github.event.issue.user.type == 'User'",
			"contains(fromJson('[\"OWNER\",\"MEMBER\",\"COLLABORATOR\"]'), github.event.issue.author_association)",
			"github.event.issue.user.type == 'Bot'",
			# Opt-in hardened automation: only the dedicated orchestrator app,
			# with the managed label and provenance marker (GITHUB_TOKEN
			# issues never trigger workflows, so no shared bot login).
			"vars.AI_ORCHESTRATOR_APP_LOGIN != ''",
			"github.event.issue.user.login == vars.AI_ORCHESTRATOR_APP_LOGIN",
			"contains(toJson(github.event.issue.labels.*.name), 'ai:orchestrator-managed')",
			"contains(github.event.issue.body, '<!-- ai-orchestrator-provenance:v1 ')",
			"!contains(toJson(github.event.issue.labels.*.name), 'ai:auto-heal-source')",
			"github.event.issue.pull_request == null",
			"github.event.comment.user.type == 'User'",
			"contains(fromJson('[\"OWNER\",\"MEMBER\",\"COLLABORATOR\"]'), github.event.comment.author_association)",
			"startsWith(github.event.comment.body, '/reclarify')",
			"contains(github.event.comment.body, fromJson('\"\\n/reclarify\"'))",
			"!contains(github.event.comment.body, '<!-- ai:')",
			"!contains(github.event.comment.body, 'To restart clarification reply:')",
		),
	)
	opened, reclarify = _canonical_predicate("clarify").split(" || (github.event_name == 'issue_comment'", 1)
	assert "github.event.issue.user.type == 'User'" in opened
	assert "github.event.issue.user.login == vars.AI_ORCHESTRATOR_APP_LOGIN" in opened
	assert "github.event.comment.user.type == 'User'" in reclarify
	assert "github.event.issue.author_association" not in reclarify


# Issue #6630: /reclarify on a later line of a human comment starts clarify,
# while automation comments that mention or echo it never do.
RECLARIFY_CLAUSE = (
	"(startsWith(github.event.comment.body, '/reclarify') || "
	"(contains(github.event.comment.body, fromJson('\"\\n/reclarify\"')) && "
	"!contains(github.event.comment.body, '<!-- ai:') && "
	"!contains(github.event.comment.body, 'To restart clarification reply:')))"
)

_PLAN_COMMENT_TRAILER = "Implementation Plan\n\nplan text\n\nTo proceed reply:\n\n/approved\n\nTo restart clarification reply:\n\n/reclarify\n"

RECLARIFY_CASES = (
	("Answer to the blocker.\n\n/reclarify", True),
	("Answer\r\n/reclarify", True),
	("/reclarify", True),
	("/reclarify [auto-requeued-by-poller]\n<!-- ai:reclarify-replay:v1 source=1 -->", True),
	("/reclarify\n<!-- ai:security-dependency-released:7 -->", True),
	("please run /reclarify later", False),
	("Clarification blocked: human input required.\n\nProvide the input, then run /reclarify or continue with /answer.", False),
	("text\n/reclarify\n<!-- ai:clarification-questions -->", False),
	(_PLAN_COMMENT_TRAILER + "\n<!-- ai:plan-proposal:v1 -->\n", False),
	(_PLAN_COMMENT_TRAILER, False),
	(" /reclarify", False),
	("> /reclarify", False),
	("Answer\n/Reclarify", True),
)


def _model_reclarify_clause(body: str) -> bool:
	# GitHub's startsWith/contains compare case-insensitively.
	lowered = body.lower()
	return lowered.startswith("/reclarify") or (
		"\n/reclarify" in lowered
		and "<!-- ai:" not in lowered
		and "to restart clarification reply:" not in lowered
	)


def _clarify_bash_snippet() -> str:
	text = (REPO_ROOT / ".github/workflows/clarify.yml").read_text(encoding="utf-8")
	blocks = []
	start = 0
	while True:
		begin = text.find('_rc_body_lc="${COMMENT_BODY,,}"', start)
		if begin < 0:
			break
		end = text.index('_rc_is_command="true"\n', begin)
		end = text.index("fi\n", end) + len("fi\n")
		line_start = text.rfind("\n", 0, begin) + 1
		blocks.append("\n".join(line.strip() for line in text[line_start:end].splitlines()))
		start = end
	assert len(blocks) == 2, "route and requeue steps must both carry the reclarify classifier"
	assert blocks[0] == blocks[1], "route and requeue reclarify classifiers drifted"
	return blocks[0]


def test_clarify_reclarify_clause_is_pinned_in_every_wrapper() -> None:
	for relative_path in PHASE_WORKFLOWS["clarify"]:
		text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
		assert RECLARIFY_CLAUSE in text, f"{relative_path} lost the any-line /reclarify clause"


def test_reclarify_classifiers_agree_with_the_job_predicate() -> None:
	import os
	import shutil
	import subprocess
	import sys

	sys.path.insert(0, str(REPO_ROOT / "scripts"))
	import orchestrate_lib

	snippet = _clarify_bash_snippet()
	poller = (REPO_ROOT / "scripts/orchestrate_poll_process.sh").read_text(encoding="utf-8")
	jq_line = next(line for line in poller.splitlines() if line.startswith("RECLARIFY_COMMAND_JQ_DEF="))
	cases = list(RECLARIFY_CASES) + [("x" * 70000 + "\n/reclarify", True)]
	for body, expected in cases:
		assert _model_reclarify_clause(body) is expected, body[:80]
		assert orchestrate_lib.is_reclarify_command_body(body) is expected, body[:80]
		bash = subprocess.run(
			["bash", "-c", "set -euo pipefail\n" + snippet + '\nprintf %s "${_rc_is_command}"'],
			env={"COMMENT_BODY": body, "PATH": os.environ["PATH"]}, capture_output=True, text=True, check=True,
		)
		assert bash.stdout == ("true" if expected else "false"), body[:80]
		if shutil.which("jq") is None:
			# The poller itself needs jq; CI runners carry it.
			continue
		jq = subprocess.run(
			["bash", "-c", jq_line + '\njq -n --arg b "${COMMENT_BODY}" "${RECLARIFY_COMMAND_JQ_DEF}"\' $b | is_reclarify_command\''],
			env={"COMMENT_BODY": body, "PATH": os.environ["PATH"]}, capture_output=True, text=True, check=True,
		)
		assert jq.stdout.strip() == ("true" if expected else "false"), body[:80]


def test_plan_comment_carries_automation_marker_after_its_reclarify_line() -> None:
	text = (REPO_ROOT / ".github/workflows/plan.yml").read_text(encoding="utf-8")
	begin = text.index('echo "To restart clarification reply:"')
	block = text[begin:text.index('} > "${PLAN_COMMENT_FILE}"', begin)]
	assert block.index('echo "/reclarify"') < block.index('echo "<!-- ai:plan-proposal:v1 -->"')

def test_plan_predicate_preserves_trusted_human_and_bot_answer_routes() -> None:
	_assert_clauses(
		_canonical_predicate("plan"),
		(
			"github.event_name == 'issue_comment'",
			"github.event.action == 'created'",
			"github.event.issue.pull_request == null",
			"github.event.comment.user.type == 'User'",
			"contains(fromJson('[\"OWNER\",\"MEMBER\",\"COLLABORATOR\"]'), github.event.comment.author_association)",
			"github.event.comment.user.type == 'Bot'",
			"vars.AI_PIPELINE_APPROVER_LOGIN != ''",
			"github.event.comment.user.login == vars.AI_PIPELINE_APPROVER_LOGIN",
			"startsWith(github.event.comment.body, '/answer')",
			# Hardened mode: trusted users cannot pass an automation marker.
			"vars.AI_PIPELINE_APPROVER_LOGIN == '' || ( !contains(github.event.comment.body, '[auto-answered-by-clarify]') && !contains(github.event.comment.body, '[auto-answered-by-orchestrator]') )",
			"'[auto-answered-by-clarify]'",
			"'[auto-answered-by-orchestrator]'",
			# Hardened mode: the repository owner's own account (GH_PAT) may post the marker.
			"github.event.comment.author_association == 'OWNER'",
			"github.event.comment.user.login == github.repository_owner",
		),
	)


def test_implement_predicate_preserves_trusted_human_and_bot_approval_routes() -> None:
	_assert_clauses(
		_canonical_predicate("implement"),
		(
			"github.event_name == 'issue_comment'",
			"github.event.action == 'created'",
			"github.event.issue.pull_request == null",
			"github.event.comment.user.type == 'User'",
			"contains(fromJson('[\"OWNER\",\"MEMBER\",\"COLLABORATOR\"]'), github.event.comment.author_association)",
			"github.event.comment.user.type == 'Bot'",
			"vars.AI_PIPELINE_APPROVER_LOGIN != ''",
			"github.event.comment.user.login == vars.AI_PIPELINE_APPROVER_LOGIN",
			"startsWith(github.event.comment.body, '/approved')",
			"vars.AI_PIPELINE_APPROVER_LOGIN == '' || !contains(github.event.comment.body, '[auto-approved-by-plan]')",
			"'[auto-approved-by-plan]'",
			# Hardened mode: the repository owner's own account (GH_PAT) may post the marker.
			"github.event.comment.author_association == 'OWNER'",
			"github.event.comment.user.login == github.repository_owner",
		),
	)


def test_clarify_response_predicate_preserves_actor_and_content_guards() -> None:
	_assert_clauses(
		_canonical_predicate("respond"),
		(
			"github.event_name == 'issue_comment'",
			"github.event.action == 'created'",
			"github.event.issue.pull_request == null",
			"github.event.comment.user.type == 'Bot'",
			"vars.AI_PIPELINE_APPROVER_LOGIN != ''",
			"github.event.comment.user.login == vars.AI_PIPELINE_APPROVER_LOGIN",
			"github.event.comment.user.type == 'User'",
			"contains(fromJson('[\"OWNER\",\"MEMBER\",\"COLLABORATOR\"]'), github.event.comment.author_association)",
			"contains(github.event.comment.body, 'Clarification required')",
		),
	)


README_WRAPPER_EXAMPLES = {
	"clarify": "ai-clarify",
	"plan": "ai-plan",
	"implement": "ai-implement",
}


def _readme_wrapper_example(template_name: str) -> dict:
	"""Return the parsed ```yaml block that README.md shows for one core wrapper."""
	readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
	heading = f"**`.github/workflows/{template_name}.yml`**"
	heading_index = readme.find(heading)
	assert heading_index >= 0, f"README missing heading for {template_name}.yml"
	fence_start = readme.find("```yaml\n", heading_index)
	assert fence_start >= 0, f"README missing YAML fence after {template_name}.yml heading"
	fence_start += len("```yaml\n")
	fence_end = readme.find("\n```", fence_start)
	assert fence_end >= 0, f"README missing closing fence for {template_name}.yml example"
	raw_example = readme[fence_start:fence_end]
	assert "@<40-character-release-sha> # stable" in raw_example
	return yaml.safe_load(raw_example)


def test_readme_core_wrapper_examples_carry_reusable_predicates() -> None:
	# README's "Create wrapper workflows" section is what a consumer hand-copies
	# when it does not install workflow-templates/ verbatim. An example without
	# the job-level predicate is the SEC-B06 shape (any commenter dispatches a
	# secrets-inheriting run), so the examples must match the canonical
	# reusable-workflow predicate exactly like the shipped templates do.
	for job_name, template_name in README_WRAPPER_EXAMPLES.items():
		example = _readme_wrapper_example(template_name)
		predicate = example["jobs"][job_name].get("if")
		assert isinstance(predicate, str), f"README {template_name}.yml example lacks jobs.{job_name}.if"
		assert " ".join(predicate.split()) == _canonical_predicate(job_name), (
			f"README {template_name}.yml example predicate drifted from the reusable workflow"
		)
		expected_uses = f"shubhodeep1/coding-workflows/.github/workflows/{PHASE_WORKFLOWS[job_name][0].rsplit('/', 1)[1]}@<40-character-release-sha>"
		assert example["jobs"][job_name]["uses"] == expected_uses


# Issue #7328: the poller's PR-dependency release (/reclarify plus the
# pr-dependency-released marker, posted with GH_PAT) restarts clarify as an
# automated run. The job predicate admits it from any User association; the
# route step verifies the author is the GH_PAT login before trusting it.
RELEASE_MARKER_CLAUSE = (
	"(github.event_name == 'issue_comment' && github.event.action == 'created' && "
	"github.event.issue.pull_request == null && github.event.comment.user.type == 'User' && "
	"!contains(fromJson('[\"OWNER\",\"MEMBER\",\"COLLABORATOR\"]'), github.event.comment.author_association) && "
	"startsWith(github.event.comment.body, fromJson('\"/reclarify\\n<!-- ai:pr-dependency-released:\"')))"
)

# Issue #7657: issue_pr_status.yml restarts clarify after a merged PR that
# delivered only part of the issue, with the same shape and a different
# marker; the route step verifies it exactly like the release.
PARTIAL_DELIVERY_RESTART_CLAUSE = (
	"(github.event_name == 'issue_comment' && github.event.action == 'created' && "
	"github.event.issue.pull_request == null && github.event.comment.user.type == 'User' && "
	"!contains(fromJson('[\"OWNER\",\"MEMBER\",\"COLLABORATOR\"]'), github.event.comment.author_association) && "
	"startsWith(github.event.comment.body, fromJson('\"/reclarify\\n<!-- ai:partial-delivery-restart:\"')))"
)

RELEASE_CONCURRENCY_GROUP = (
	"ai-clarify-${{ github.event.issue.number }}${{ (github.event_name == 'issue_comment' && "
	"!contains(fromJson('[\"OWNER\",\"MEMBER\",\"COLLABORATOR\"]'), github.event.comment.author_association)) "
	"&& format('-release-{0}', github.event.comment.id) || '' }}"
)

_GH_STUB = """gh_retry()
{
	if [ "${STUB_LOGIN_FAIL:-}" = "1" ]; then
		return 1
	fi
	case "$*" in
		*issues/comments/*)
			[ -n "${STUB_SOURCE_COMMENT:-}" ] || return 1
			printf '%s\\n' "${STUB_SOURCE_COMMENT}"
			;;
		*) printf '%s\\n' "${STUB_LOGIN}" ;;
	esac
}
"""


def _clarify_route_step() -> dict:
	workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/clarify.yml").read_text(encoding="utf-8"))
	return next(step for step in workflow["jobs"]["clarify"]["steps"] if step.get("name") == "Decide clarify route")


def _release_block() -> str:
	script = _clarify_route_step()["run"]
	begin = script.index("# BEGIN pr-dependency release classification (#7328)")
	end = script.index("# END pr-dependency release classification (#7328)")
	return script[begin:end]


def _run_release_block(body: str, association: str, comment_login: str, token_login: str = "pipeline-bot", login_fail: bool = False, forced: str = "true", source_comment: str = "") -> dict:
	import os
	import subprocess
	import tempfile

	with tempfile.TemporaryDirectory() as workdir:
		scripts_dir = Path(workdir) / "scripts"
		scripts_dir.mkdir()
		(scripts_dir / "gh_helpers.sh").write_text(_GH_STUB, encoding="utf-8")
		script = (
			"set -euo pipefail\n"
			f"IS_FORCED_RECLARIFY={forced}\n"
			+ _release_block()
			+ '\nprintf "release=%s forced=%s trusted=%s\\n" "${IS_AUTOMATED_RELEASE}" "${IS_FORCED_RECLARIFY}" "${COMMENT_ROUTE_TRUSTED}"\n'
		)
		env = {
			"PATH": os.environ["PATH"],
			"EVENT_NAME": "issue_comment",
			"ISSUE_NUMBER": "42",
			"COMMENT_BODY": body,
			"COMMENT_AUTHOR_ASSOCIATION": association,
			"COMMENT_USER_LOGIN": comment_login,
			"STUB_LOGIN": token_login,
			"STUB_LOGIN_FAIL": "1" if login_fail else "",
			"STUB_SOURCE_COMMENT": source_comment,
			"GITHUB_REPOSITORY": "o/r",
		}
		result = subprocess.run(["bash", "-c", script], env=env, cwd=workdir, capture_output=True, text=True, check=True)
	last = result.stdout.strip().splitlines()[-1]
	values = dict(part.split("=", 1) for part in last.split())
	values["stdout"] = result.stdout
	return values


RELEASE_BODY = "/reclarify\n<!-- ai:pr-dependency-released:42 -->"


def test_clarify_release_marker_clause_is_pinned_in_every_wrapper() -> None:
	for relative_path in PHASE_WORKFLOWS["clarify"]:
		predicate = _normalized_job_predicate(relative_path, "clarify")
		assert RELEASE_MARKER_CLAUSE in predicate, f"{relative_path} lost the PR-dependency release clause"
		assert PARTIAL_DELIVERY_RESTART_CLAUSE in predicate, f"{relative_path} lost the partial-delivery restart clause"
		# The release and partial-delivery restart clauses are the only paths
		# for an author without a trusted association; ordinary /reclarify
		# comments still require one.
		assert predicate.count("!contains(fromJson('[\"OWNER\",\"MEMBER\",\"COLLABORATOR\"]'), github.event.comment.author_association)") == 2, relative_path
		_assert_clauses(
			predicate,
			(
				"contains(fromJson('[\"OWNER\",\"MEMBER\",\"COLLABORATOR\"]'), github.event.comment.author_association) && "
				"(startsWith(github.event.comment.body, '/reclarify')",
			),
		)


def test_clarify_release_runs_in_its_own_concurrency_group() -> None:
	workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/clarify.yml").read_text(encoding="utf-8"))
	concurrency = workflow["jobs"]["clarify"]["concurrency"]
	assert " ".join(str(concurrency["group"]).split()) == RELEASE_CONCURRENCY_GROUP
	assert concurrency["cancel-in-progress"] is True


def test_clarify_release_classification_verifies_the_pipeline_login() -> None:
	verified_owner = _run_release_block(RELEASE_BODY, "OWNER", "Pipeline-Bot")
	assert (verified_owner["release"], verified_owner["forced"], verified_owner["trusted"]) == ("true", "false", "true")
	assert "gate=pr_dependency_release reason=verified outcome=restart issue=42" in verified_owner["stdout"]

	verified_none = _run_release_block(RELEASE_BODY, "NONE", "pipeline-bot")
	assert (verified_none["release"], verified_none["forced"], verified_none["trusted"]) == ("true", "false", "true")

	crlf = _run_release_block(RELEASE_BODY.replace("\n", "\r\n") + "\r\n", "NONE", "pipeline-bot")
	assert crlf["release"] == "true"

	mismatch_owner = _run_release_block(RELEASE_BODY, "OWNER", "maintainer")
	assert (mismatch_owner["release"], mismatch_owner["forced"], mismatch_owner["trusted"]) == ("false", "true", "true")
	assert "reason=author_mismatch outcome=human_reclarify" in mismatch_owner["stdout"]

	mismatch_none = _run_release_block(RELEASE_BODY, "NONE", "outsider")
	assert (mismatch_none["release"], mismatch_none["trusted"]) == ("false", "false")
	assert "reason=author_mismatch outcome=skip" in mismatch_none["stdout"]

	for association, trusted in (("OWNER", "true"), ("NONE", "false")):
		failed = _run_release_block(RELEASE_BODY, association, "pipeline-bot", login_fail=True)
		assert (failed["release"], failed["trusted"]) == ("false", trusted)
		assert "reason=identity_unavailable" in failed["stdout"]

	bad_login = _run_release_block(RELEASE_BODY, "NONE", "", token_login="")
	assert (bad_login["release"], bad_login["trusted"]) == ("false", "false")

	for body in (
		"/reclarify\n<!-- ai:pr-dependency-released:43 -->",
		RELEASE_BODY + "\nextra",
		"/RECLARIFY\n<!-- ai:pr-dependency-released:42 -->",
		"text\n" + RELEASE_BODY,
	):
		other = _run_release_block(body, "NONE", "pipeline-bot")
		assert (other["release"], other["trusted"]) == ("false", "false"), body
		assert "pr_dependency_release" not in other["stdout"], body
		trusted_other = _run_release_block(body, "MEMBER", "pipeline-bot")
		assert (trusted_other["release"], trusted_other["forced"], trusted_other["trusted"]) == ("false", "true", "true"), body


def test_clarify_release_replay_is_verified_against_its_source_comment() -> None:
	import json

	replay = "/reclarify [auto-requeued-by-poller]\n<!-- ai:reclarify-replay:v1 source=777 -->"

	def source(login: str = "Pipeline-Bot", issue: str = "42", body: str = RELEASE_BODY) -> str:
		return json.dumps({"user": {"login": login}, "issue_url": f"https://api.github.com/repos/o/r/issues/{issue}", "body": body})

	verified = _run_release_block(replay, "OWNER", "pipeline-bot", source_comment=source(body=RELEASE_BODY + "\r\n"))
	assert (verified["release"], verified["forced"], verified["trusted"]) == ("true", "false", "true")
	assert "reason=verified_replay outcome=restart issue=42" in verified["stdout"]

	for bad in (
		source(login="maintainer"),
		source(issue="142"),
		source(body="/reclarify"),
		"",
	):
		other = _run_release_block(replay, "OWNER", "pipeline-bot", source_comment=bad)
		assert (other["release"], other["forced"], other["trusted"]) == ("false", "true", "true"), bad
		assert "reason=replay_source_unverified outcome=human_reclarify" in other["stdout"], bad

	by_human = _run_release_block(replay, "OWNER", "maintainer", source_comment=source())
	assert (by_human["release"], by_human["forced"]) == ("false", "true")
	assert "reason=author_mismatch outcome=human_reclarify" in by_human["stdout"]

	plain = _run_release_block("/reclarify [auto-requeued-by-poller]\n<!-- ai:reclarify-replay:v1 source=x -->", "OWNER", "pipeline-bot", source_comment=source())
	assert (plain["release"], plain["forced"]) == ("false", "true")
	assert "pr_dependency_release" not in plain["stdout"]


def test_clarify_memory_events_skip_untrusted_comments() -> None:
	workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/clarify.yml").read_text(encoding="utf-8"))
	steps = {step.get("name"): step for step in workflow["jobs"]["clarify"]["steps"]}
	gate = "steps.clarify_route.outputs.comment_route_trusted != 'false'"
	for name in ("Record clarification run start", "Record clarification run completed", "Record clarification run failed"):
		assert gate in str(steps[name].get("if", "")), name
	assert 'echo "comment_route_trusted=${COMMENT_ROUTE_TRUSTED}" >> "$GITHUB_OUTPUT"' in _clarify_route_step()["run"]
	# A failure before the route step leaves the output empty; an untrusted
	# association must still record nothing.
	failed_if = " ".join(str(steps["Record clarification run failed"]["if"]).split())
	assert "steps.clarify_route.outputs.comment_route_trusted == 'true' || github.event_name != 'issue_comment' || contains(fromJson('[\"OWNER\",\"MEMBER\",\"COLLABORATOR\"]'), github.event.comment.author_association)" in failed_if


def test_clarify_release_replay_format_matches_the_poller() -> None:
	# The route step recognises the poller's replay body exactly; pin both
	# sides so a format change in either breaks this test.
	poller = (REPO_ROOT / "scripts/orchestrate_poll_process.sh").read_text(encoding="utf-8")
	assert '-f body="/reclarify [auto-requeued-by-poller]\n<!-- ai:reclarify-replay:v1 source=${source_id} -->"' in poller
	assert '_rel_replay_prefix="/reclarify [auto-requeued-by-poller]"$\'\\n\'"<!-- ai:reclarify-replay:v1 source="' in _release_block()


def test_clarify_route_untrusted_comment_writes_nothing() -> None:
	script = _clarify_route_step()["run"]
	assert "gate=route reason=untrusted_comment_author outcome=skip issue=${ISSUE_NUMBER}" in script
	security_hold = script[script.index("gate=security_dependency reason=held"):script.index("PR_DEPENDENCY_VERDICT=")]
	assert security_hold.index('if [ "${COMMENT_ROUTE_TRUSTED}" = "true" ]; then') < security_hold.index("labels[]=ai:clarification")
	pr_hold = script[script.index("gate=pr_dependency reason="):script.index("CHECK_TRIAGE_RESOLVED=")]
	assert pr_hold.index('[ "${COMMENT_ROUTE_TRUSTED}" = "true" ]') < pr_hold.index("labels[]=ai:clarification")
	fast_path = script[script.index("# Only a verified orchestrator fast path"):script.index("orchestrator_fast_path=true")]
	assert '[ "${COMMENT_ROUTE_TRUSTED}" = "true" ]' in fast_path
	assert 'echo "is_automated_release=${IS_AUTOMATED_RELEASE}" >> "$GITHUB_OUTPUT"' in script
	step_env = _clarify_route_step()["env"]
	assert step_env["COMMENT_USER_LOGIN"] == "${{ github.event.comment.user.login || '' }}"
	assert step_env["COMMENT_AUTHOR_ASSOCIATION"] == "${{ github.event.comment.author_association || '' }}"


def test_clarify_human_answer_marker_still_follows_forced_reclarify_only() -> None:
	workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/clarify.yml").read_text(encoding="utf-8"))
	step = next(step for step in workflow["jobs"]["clarify"]["steps"] if step.get("name") == "Post clarification questions")
	assert step["env"]["CLARIFY_HUMAN_ANSWER_REQUESTED"] == "${{ steps.clarify_route.outputs.is_forced_reclarify }}"


def test_clarify_route_restarts_a_verified_release_as_automated() -> None:
	import json
	import os
	import shutil
	import subprocess
	import tempfile

	# The route step needs jq; CI runners carry it. Fail rather than pass
	# without running the scenarios.
	assert shutil.which("jq") is not None, "jq is required for the clarify route test"
	step = _clarify_route_step()
	with tempfile.TemporaryDirectory() as workdir:
		root = Path(workdir)
		(root / "scripts").mkdir()
		(root / "scripts" / "gh_helpers.sh").write_text(_GH_STUB, encoding="utf-8")
		host_python_dir = root / "host_python"
		host_python_dir.mkdir()
		for name in ("security_dependency.py", "pr_dependency.py"):
			(host_python_dir / name).write_bytes((REPO_ROOT / "scripts" / name).read_bytes())
		meta_path = root / "issue.json"
		meta_path.write_text(json.dumps({
			"number": 42, "repository_url": "https://api.github.com/repos/o/r", "state": "open",
			"user": {"type": "User", "login": "maintainer"}, "labels": [{"name": "ai:clarification"}], "author_association": "OWNER",
		}), encoding="utf-8")
		for association, login, expected in (
			("NONE", "pipeline-bot", {"is_automated_release": "true", "is_forced_reclarify": "false", "skip_codex": "false"}),
			("NONE", "outsider", {"is_automated_release": "false", "skip_codex": "true", "orchestrator_fast_path": "false"}),
			("OWNER", "maintainer", {"is_automated_release": "false", "is_forced_reclarify": "true", "skip_codex": "false"}),
		):
			output_path = root / "output"
			output_path.write_text("", encoding="utf-8")
			env = {
				"PATH": os.environ["PATH"],
				"ISSUE_META_FILE": str(meta_path),
				"GITHUB_OUTPUT": str(output_path),
				"CLARIFY_HOST_PYTHON_DIR": str(host_python_dir),
				"EVENT_NAME": "issue_comment",
				"EVENT_ACTION": "created",
				"COMMENT_BODY": RELEASE_BODY,
				"COMMENT_AUTHOR_ASSOCIATION": association,
				"COMMENT_USER_LOGIN": login,
				"STUB_LOGIN": "pipeline-bot",
				"RUN_ID": "1",
				"ISSUE_NUMBER": "42",
				"CHECK_TRIAGE_RESOLVED_GATE_ENABLED": "true",
				"DEFAULT_BRANCH": "main",
				# The runner exports GITHUB_REPOSITORY; the route step reads it.
				"GITHUB_REPOSITORY": "o/r",
			}
			script = step["run"].replace("${{ github.repository }}", "o/r")
			result = subprocess.run(["bash", "-c", script], env=env, cwd=workdir, capture_output=True, text=True, check=True)
			outputs = dict(line.split("=", 1) for line in output_path.read_text(encoding="utf-8").splitlines() if "=" in line)
			for key, value in expected.items():
				assert outputs.get(key) == value, (association, login, key, outputs, result.stdout)


# --- Least-privilege consumer templates (consumer tele-funtoken-msg-scoring) ---


def _template_paths() -> list[Path]:
	return sorted((REPO_ROOT / "workflow-templates").glob("*.yml"))


def test_consumer_templates_forward_only_declared_secrets() -> None:
	"""A template never hands the shared workflow every repository secret: it
	forwards exactly the secrets the called reusable workflow declares."""
	import re

	checked = 0
	for template in _template_paths():
		text = template.read_text(encoding="utf-8")
		assert "secrets: inherit" not in text, template.name
		workflow = yaml.safe_load(text)
		for job_name, job in workflow["jobs"].items():
			uses = job.get("uses", "")
			match = re.match(r"shubhodeep1/coding-workflows/\.github/workflows/([\w.-]+)@", uses)
			if not match:
				continue
			reusable = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / match.group(1)).read_text(encoding="utf-8"))
			on_block = reusable.get(True) or reusable.get("on")
			declared = set((on_block.get("workflow_call") or {}).get("secrets") or {})
			forwarded = job.get("secrets") or {}
			assert isinstance(forwarded, dict), (template.name, job_name)
			assert set(forwarded) == declared, (template.name, job_name, sorted(forwarded), sorted(declared))
			for name, value in forwarded.items():
				assert value == "${{ secrets.%s }}" % name, (template.name, name, value)
			checked += 1
	assert checked >= 17, checked


def test_consumer_templates_accept_no_shared_actions_bot() -> None:
	"""GITHUB_TOKEN comments and issues never trigger workflows, so the
	shared github-actions[bot] login is never a trusted automation author."""
	for template in _template_paths():
		workflow = yaml.safe_load(template.read_text(encoding="utf-8"))
		for job_name, job in workflow["jobs"].items():
			assert "github-actions[bot]" not in str(job.get("if", "")), (template.name, job_name)


def test_hardened_automation_signs_and_verifies_plan_approval() -> None:
	plan_text = (REPO_ROOT / "workflow-templates" / "ai-plan.yml").read_text(encoding="utf-8")
	implement_text = (REPO_ROOT / "workflow-templates" / "ai-implement.yml").read_text(encoding="utf-8")
	plan = yaml.safe_load(plan_text)
	implement = yaml.safe_load(implement_text)
	sign = plan["jobs"]["sign-automated-approval"]
	assert sign["needs"] == "plan"
	assert "vars.AI_PIPELINE_APPROVER_LOGIN != ''" in sign["if"]
	assert "needs.plan.result == 'success'" in sign["if"]
	verify = implement["jobs"]["verify-automated-approval"]
	assert "vars.AI_PIPELINE_APPROVER_LOGIN != ''" in verify["if"]
	assert implement["jobs"]["implement"]["needs"] == "verify-automated-approval"
	assert implement["permissions"].get("actions") == "read"
	# The approval secret stays in the wrapper jobs; it is never forwarded
	# to a reusable model job.
	for workflow in (plan, implement):
		for job in workflow["jobs"].values():
			assert "AI_PIPELINE_APPROVAL_SECRET" not in (job.get("secrets") or {})
	assert "APPROVAL_SECRET: ${{ secrets.AI_PIPELINE_APPROVAL_SECRET }}" in plan_text
	assert "APPROVAL_SECRET: ${{ secrets.AI_PIPELINE_APPROVAL_SECRET }}" in implement_text
	for field in ("issue", "plan_sha256", "run_id", "target_ref", "target_sha"):
		assert f"{field}=" in plan_text and field in implement_text, field
	assert "hmac.compare_digest(signature, expected)" in implement_text
	assert 'run_path != ".github/workflows/ai-plan.yml"' in implement_text
	assert "target ref advanced after approval" in implement_text
	assert 'int(item.get("id") or 0) > trigger_comment_id' in plan_text

	def canonical_lines(text: str) -> list[str]:
		start = text.index("canonical = (")
		end = text.index(")", text.index("target_sha={target_sha}", start))
		return [line.strip() for line in text[start:end].splitlines()]

	# Signer and verifier must build the exact same signed string.
	assert canonical_lines(plan_text) == canonical_lines(implement_text)


def test_hardened_automation_marker_exclusions_are_opt_in() -> None:
	"""With vars.AI_PIPELINE_APPROVER_LOGIN unset the trusted-user branches
	keep accepting the PAT-posted automation markers, as before; with it set
	they reject them."""
	plan = " ".join(yaml.safe_load((REPO_ROOT / "workflow-templates" / "ai-plan.yml").read_text(encoding="utf-8"))["jobs"]["plan"]["if"].split())
	assert (
		"vars.AI_PIPELINE_APPROVER_LOGIN == '' || ( !contains(github.event.comment.body, '[auto-answered-by-clarify]') && "
		"!contains(github.event.comment.body, '[auto-answered-by-orchestrator]') )"
	) in plan
	implement = " ".join(yaml.safe_load((REPO_ROOT / "workflow-templates" / "ai-implement.yml").read_text(encoding="utf-8"))["jobs"]["implement"]["if"].split())
	assert "vars.AI_PIPELINE_APPROVER_LOGIN == '' || !contains(github.event.comment.body, '[auto-approved-by-plan]')" in implement
	for name in ("ai-clarify.yml", "ai-plan.yml", "ai-implement.yml", "ai-orchestrate-clarify-respond.yml"):
		text = (REPO_ROOT / "workflow-templates" / name).read_text(encoding="utf-8")
		assert "!contains(toJson(github.event.issue.labels.*.name), 'ai:auto-heal-source')" in text, name


def main() -> int:
	tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
