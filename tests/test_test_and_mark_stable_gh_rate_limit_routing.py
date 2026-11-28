#!/usr/bin/env python3
"""Issue #10225: the release gate's GitHub reads wait out rate limits.

Two gates in a row failed on "API rate limit exceeded" (runs 45927857867 and
46009933351). The second one failed in Phase 7 on a plain `gh api` read with
a 3 x 5 s retry, although scripts/gh_helpers.sh already has `gh_api_retry`,
which waits for the reset of the limited bucket and gives up at once when
the reset is further away than GH_RETRY_RATE_LIMIT_MAX_WAIT_SECS.

These checks keep it that way:

* (a) the five scripts/stable_e2e_step_*.sh bodies and every run: body of
  .github/workflows/test-and-mark-stable.yml make no raw `gh api` read,
  `gh run view|list|watch`, `gh pr view|list|checks|diff` or
  `gh issue view|list` outside ALLOWLIST, and every ALLOWLIST entry still
  matches something (a stale entry fails);
* (b) every job that holds the PAT bounds the helper's waits with
  GH_RETRY_MAX_ATTEMPTS and GH_RETRY_RATE_LIMIT_MAX_WAIT_SECS, and the worst
  case of one read fits the job's limits;
* (c) every PAT job fills its GH_PAT_BUDGET start line from one
  `gh api rate_limit` read (which does not use up the core allowance) and
  its end line through gh_pat_budget, with the `unknown` fallback kept.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_REL = ".github/workflows/test-and-mark-stable.yml"
WORKFLOW = REPO_ROOT / WORKFLOW_REL
STEP_SCRIPTS = (
	"scripts/stable_e2e_step_verify_cancel_on_close.sh",
	"scripts/stable_e2e_step_verify_bait_removed.sh",
	"scripts/stable_e2e_step_deep_verify.sh",
	"scripts/stable_e2e_step_wait_plan.sh",
	"scripts/stable_e2e_step_wait_review.sh",
)

# Jobs that hold secrets.GH_PAT as GH_TOKEN. `validate` holds no PAT by
# design (issue #9887, D6): a job-token rate_limit read would report the
# wrong bucket, so its GH_PAT_BUDGET lines stay `unknown`.
PAT_JOBS = (
	"e2e-smoke-test",
	"workflow-log-analysis-test",
	"orphan-workflows-test",
	"orchestrate-decompose-test",
	"validate-standalone-test",
	"clarify-rejects-unsolvable-test",
	"e2e-alt-model-test",
)
BUDGET_EXEMPT_JOBS = {
	"validate": "holds no GH_PAT (issue #9887 D6); the job token's numbers would be misread as PAT numbers",
}

# (path, substring of the logical line, reason). Each raw read must contain
# the substring of an entry for its file; each entry must match at least once.
_BAIT = "scripts/stable_e2e_step_verify_bait_removed.sh"
ALLOWLIST: tuple[tuple[str, str, str], ...] = (
	(
		_BAIT,
		'if gh api -i "$@" > "${response_file}"',
		"inside the step's own gh_api_with_retry, which already waits for the reset within the poll deadline",
	),
	(
		_BAIT,
		"reset_epoch=$(gh api /rate_limit",
		"wait_for_gh_rate_limit's one-shot /rate_limit probe; it does not use up the core allowance",
	),
	(
		WORKFLOW_REL,
		'if out="$(gh api "$@")"; then',
		"source job: its own retry wrapper, github.token, runs before any checkout so gh_helpers.sh is not on disk",
	),
	(
		WORKFLOW_REL,
		'if ! gh api -H "Accept: application/vnd.github.raw+json"',
		"source job: github.token read before any checkout, gh_helpers.sh is not on disk",
	),
	(
		WORKFLOW_REL,
		"gpb_reading=\"$(gh api rate_limit --jq",
		"GH_PAT_BUDGET start read: /rate_limit does not use up the allowance, runs before checkout, falls back to unknown",
	),
	(
		WORKFLOW_REL,
		"stall_diag_json=$(timeout 30 gh api",
		"Phase 3b stall diagnostics: bounded on purpose by timeout 30 and fail soft (|| true)",
	),
	(
		WORKFLOW_REL,
		'DW_TIMEOUT_JOBS="$(timeout 20 gh api',
		"dispatch-watch timeout inventory: bounded on purpose by timeout 20 on the failure path, fail soft",
	),
	(
		WORKFLOW_REL,
		'child_status="$(timeout 20 gh api',
		"cancel-on-gate-cancel cleanup: bounded on purpose by timeout 20 while the gate is being cancelled",
	),
	(
		WORKFLOW_REL,
		'PROJECT_BRANCH_PROBE_ERR="$(gh api',
		"existence probe that captures gh's own stderr (2>&1) to tell 404 from other errors; routing would change it",
	),
	(
		WORKFLOW_REL,
		'RELEASE_SUPERSEDED_BY="$(gh api',
		"notify job (timeout-minutes 5): a deliberate exception to raising the limit, as a reset wait would delay the "
		"failure report by up to 20 min only to pick its header; a failed read keeps the FAILED header",
	),
	(
		WORKFLOW_REL,
		'TIMEOUT_INV_RUNS="$(_safe_gh_jq',
		"Phase 4d review-timeout inventory: failure-path diagnostics with timeout-minutes 3 and continue-on-error; "
		"a reset wait cannot fit and a failed read already prints '(review run inventory fetch failed)'",
	),
	(
		WORKFLOW_REL,
		'TIMEOUT_INV_JOBS="$(_safe_gh_jq',
		"Phase 4d review-timeout inventory job lookup: same 3-minute diagnostic step, fail soft per run",
	),
	(
		WORKFLOW_REL,
		'gh_api_retry() { local cleanup_fallback_out; cleanup_fallback_out="$(gh api "$@")" || return 1;',
		"always() cleanup fallback when the checkout's scripts/gh_helpers.sh is missing, so cleanup stays best-effort",
	),
)

# A call starts a command: line start, after `$(` or a backtick, a shell operator, a
# group or function-body brace, a subshell `( `, a keyword, or after
# `timeout N` / `command` / `env` / a leading `NAME=value ` assignment or a
# `$VAR ` / `${VAR} ` word. `gh` inside echo text such as
# "::warning::gh api failed" or "(gh api returned empty)" is prose, and
# `gh_retry gh api ...` already goes through a helper.
_GH_CALL = re.compile(
	r"(?:^|^\(|<\(|\$\(|\(\s|[;|&!{}`)]|\b(?:then|do|if|elif|while|until|else)\b|\btimeout\s+\d+|\bcommand|\benv"
	r"|\b[A-Za-z_][A-Za-z0-9_]*=[^\s()]*\s|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?\s)\s*gh\s+(api|run|pr|issue)\b(.*)"
)
# `_safe_gh_jq` (scripts/gh_helpers.sh) makes one plain `gh api` attempt and
# never waits for a rate-limit reset, so a call to it counts as a raw read;
# `gh_retry _safe_gh_jq ...` goes through a helper and is not matched.
_SAFE_GH_JQ_CALL = re.compile(r"(?:^|^\(|<\(|\$\(|\(\s|[;|&!{}`)]|\b(?:then|do|if|elif|while|until|else)\b)\s*_safe_gh_jq\s")
_READ_SUBCOMMANDS = {
	"run": {"view", "list", "watch"},
	"pr": {"view", "list", "checks", "diff"},
	"issue": {"view", "list"},
}
_WRITE_FLAG = re.compile(r"(?:^|\s)(?:-f|-F|--field|--raw-field|--input)(?:\s|=|$)")
_METHOD = re.compile(r"(?:^|\s)(?:-X|--method)(?:\s+|=)[\"']?([A-Za-z]+)")
# Quoted spans (e.g. a `--jq` filter) other than a bare word such as "POST":
# a ` -f ` or ` -X POST` inside them is not a flag (review of #10225).
_QUOTED_SPAN = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
_QUOTED_WORD = re.compile(r"['\"][A-Za-z]+['\"]")


def _logical_lines(text: str) -> list[str]:
	"""Comment lines dropped, backslash continuations joined."""
	out: list[str] = []
	buf = ""
	for raw in text.splitlines():
		stripped = raw.strip()
		if not buf and stripped.startswith("#"):
			continue
		if stripped.startswith("#"):
			continue
		if stripped.endswith("\\"):
			buf += stripped[:-1] + " "
			continue
		out.append(buf + stripped)
		buf = ""
	if buf:
		out.append(buf)
	return out


def _is_raw_read(sub: str, rest: str) -> bool:
	if sub == "api":
		rest = _QUOTED_SPAN.sub(lambda m: m.group(0) if _QUOTED_WORD.fullmatch(m.group(0)) else "''", rest)
		method = _METHOD.search(rest)
		if method and method.group(1).upper() != "GET":
			return False
		if _WRITE_FLAG.search(rest) and not method:
			return False
		return True
	words = rest.split()
	return bool(words) and words[0] in _READ_SUBCOMMANDS[sub]


def _command_segments(line: str) -> list[str]:
	"""Split a logical line at unquoted `||`, `&&`, `;` and `|`.

	Review of #10225: a joined `gh api GET ... || gh api POST -f ...` line was
	classified as a write because the POST's flags sat in the GET's `rest`, and
	the greedy capture hid any later call on the line. Each command is now
	classified on its own flags only.
	"""
	segments: list[str] = []
	buf: list[str] = []
	quote = ""
	i = 0
	while i < len(line):
		ch = line[i]
		if quote:
			buf.append(ch)
			if ch == "\\" and quote == '"' and i + 1 < len(line):
				buf.append(line[i + 1])
				i += 2
				continue
			if ch == quote:
				quote = ""
			i += 1
			continue
		if ch == "\\" and i + 1 < len(line):
			buf.append(line[i:i + 2])
			i += 2
			continue
		if ch in ("'", '"'):
			quote = ch
			buf.append(ch)
			i += 1
			continue
		if line[i:i + 2] in ("||", "&&"):
			segments.append("".join(buf))
			buf = []
			i += 2
			continue
		if ch in (";", "|"):
			segments.append("".join(buf))
			buf = []
			i += 1
			continue
		buf.append(ch)
		i += 1
	segments.append("".join(buf))
	return segments


def raw_reads(text: str) -> list[str]:
	found: list[str] = []
	for line in _logical_lines(text):
		if _SAFE_GH_JQ_CALL.search(line):
			found.append(line)
			continue
		if any(_is_raw_read(sub, rest) for segment in _command_segments(line) for sub, rest in _gh_calls(segment)):
			found.append(line)
	return found


def _gh_calls(segment: str) -> list[tuple[str, str]]:
	"""Each gh call of a segment with its own arguments only.

	Review of #10225: calls chained inside a quoted `$(...)` or backtick
	substitution stay in one segment, and the greedy `(.*)` gave the first
	call the later call's flags (a GET then read as a write). Each call's
	`rest` now ends where the next gh call starts, and that call is scanned
	on its own.
	"""
	calls: list[tuple[str, str]] = []
	match = _GH_CALL.search(segment)
	while match:
		nxt = _GH_CALL.search(segment, match.start(2))
		calls.append((match.group(1), segment[match.start(2):nxt.start() if nxt else len(segment)]))
		match = nxt
	return calls


def _raw_read_positions(line: str) -> list[int]:
	"""Offset in `line` of each raw read on one logical line.

	Review of #10225: the allowlist used to approve a whole logical line when
	any needle matched it, so a new raw read joined to an allowlisted call
	passed unseen. Each raw read is now located on its own (the offset of its
	`api`/`run`/`pr`/`issue` word, or of `_safe_gh_jq`) so every one of them
	must sit inside an allowlist needle.
	"""
	positions: list[int] = []
	for match in _SAFE_GH_JQ_CALL.finditer(line):
		positions.append(match.start() + match.group(0).rfind("_safe_gh_jq"))
	cursor = 0
	for segment in _command_segments(line):
		# Segments are contiguous slices of `line` in order; operators between
		# them are dropped, so the next one starts at or after the cursor.
		offset = line.find(segment, cursor) if segment else cursor
		offset = cursor if offset < 0 else offset
		cursor = offset + len(segment)
		match = _GH_CALL.search(segment)
		while match:
			nxt = _GH_CALL.search(segment, match.start(2))
			rest = segment[match.start(2):nxt.start() if nxt else len(segment)]
			if _is_raw_read(match.group(1), rest):
				positions.append(offset + match.start(1))
			match = nxt
	return sorted(set(positions))


def _unlisted_reads(path: str, line: str, allowlist: tuple[tuple[str, str, str], ...]) -> tuple[list[int], set[int]]:
	"""Raw-read offsets on `line` no allowlist needle covers, and the entries used."""
	unlisted: list[int] = []
	used: set[int] = set()
	for pos in _raw_read_positions(line):
		covering = [
			i
			for i, (p, needle, _r) in enumerate(allowlist)
			if p == path and any(
				m.start() <= pos < m.start() + len(needle) for m in re.finditer(re.escape(needle), line)
			)
		]
		if not covering:
			unlisted.append(pos)
		used.update(covering)
	return unlisted, used


def _workflow() -> dict:
	"""Parse the jobs of the workflow without a YAML dependency.

	Returns {job_id: {"env": {...}, "timeout-minutes": str, "steps": [...]}}
	where each step is {"name", "if", "timeout-minutes", "run", "text"}.
	"""
	text = WORKFLOW.read_text(encoding="utf-8")
	body = text[text.index("\njobs:\n") + len("\njobs:\n"):]
	jobs: dict = {}
	for chunk in re.split(r"\n(?=  [A-Za-z0-9_-]+:\n)", "\n" + body):
		match = re.match(r"\n?  ([A-Za-z0-9_-]+):\n", chunk)
		if not match:
			continue
		job_id = match.group(1)
		head, _sep, steps_text = chunk.partition("\n    steps:\n")
		job: dict = {"env": {}, "steps": []}
		timeout = re.search(r"^    timeout-minutes: (\S+)", head, re.M)
		if timeout:
			job["timeout-minutes"] = timeout.group(1)
		env_block = re.search(r"^    env:\n((?:      .*\n|\s*\n)+)", head + "\n", re.M)
		if env_block:
			for line in env_block.group(1).splitlines():
				kv = re.match(r"^      ([A-Za-z_][A-Za-z0-9_]*): ?(.*)$", line)
				if kv:
					job["env"][kv.group(1)] = kv.group(2).strip().strip('"')
		for step_text in re.split(r"\n(?=      - )", "\n" + steps_text)[1:]:
			step: dict = {"text": step_text}
			name = re.search(r"^      - name: (.*)$", step_text, re.M)
			if name:
				step["name"] = name.group(1).strip().strip("\"'")
			for key in ("if", "timeout-minutes"):
				val = re.search(rf"^        {key}: (.*)$", step_text, re.M)
				if val:
					step[key] = val.group(1).strip()
			lines = step_text.split("\n")
			run_idx = [i for i, line in enumerate(lines) if re.match(r"^        run: ?", line)]
			if run_idx:
				first = lines[run_idx[0]][len("        run:"):].strip()
				if first.startswith("|") or first.startswith(">"):
					run_lines = []
					for line in lines[run_idx[0] + 1:]:
						if line.strip() and not line.startswith(" " * 10):
							break
						run_lines.append(line[10:])
					step["run"] = "\n".join(run_lines).rstrip("\n") + "\n"
				else:
					step["run"] = first + "\n"
			job["steps"].append(step)
		jobs[job_id] = job
	return {"jobs": jobs}


def _run_bodies() -> list[tuple[str, str, str]]:
	bodies: list[tuple[str, str, str]] = []
	for job_id, job in _workflow()["jobs"].items():
		for step in job["steps"]:
			run = step.get("run")
			if isinstance(run, str):
				bodies.append((job_id, str(step.get("name", "")), run))
	return bodies


def _all_raw_reads() -> list[tuple[str, str]]:
	reads: list[tuple[str, str]] = []
	for rel in STEP_SCRIPTS:
		for line in raw_reads((REPO_ROOT / rel).read_text(encoding="utf-8")):
			reads.append((rel, line))
	for _job, _name, body in _run_bodies():
		for line in raw_reads(body):
			reads.append((WORKFLOW_REL, line))
	return reads


def test_scanner_recognises_reads_and_writes() -> None:
	text = "\n".join(
		[
			'X=$(gh api "repos/o/r/pulls/1" --jq .state)',
			'gh api "repos/o/r/pulls/1" -X PATCH -f state=closed',
			'gh api -X GET "repos/o/r/issues" -f per_page=1',
			"gh api repos/o/r/merges \\",
			"  -f base=a -f head=b",
			"# gh api repos/o/r/comment-only",
			'gh run view 5 --repo o/r --json status',
			'gh pr comment 5 --body x',
			'gh workflow run x.yml',
			'gh_api_retry "repos/o/r/pulls/1"',
			'command gh api -i "$@"',
			'GH_TOKEN=x gh api repos/o/r/issues',
			'A=1 B="2" gh pr view 5',
			'Y="$(gh_retry gh api repos/o/r/x)"',
			'env gh issue list --repo o/r',
			'f() { gh api repos/o/r/labels; }',
			'( gh run list --repo o/r )',
			'${SUDO} gh api repos/o/r/hooks',
			'echo "::warning::gh api failed (gh api returned empty)"',
			'if ! R="$(_safe_gh_jq "repos/o/r/runs" --jq .x)"; then',
			'Z="$(gh_retry _safe_gh_jq repos/o/r/y)"',
			# Review of #10225: a GET joined to a continued POST is still a read.
			'gh api "repos/o/r/labels/x" >/dev/null 2>&1 || \\',
			'  gh api "repos/o/r/labels" \\',
			'    -f name="x" >/dev/null 2>&1 || true',
			'gh_api_retry "repos/o/r/labels/y" >/dev/null 2>&1 || gh api "repos/o/r/labels" -f name="y"',
			"V=$(gh api repos/o/r/z --jq '.a | .b' 2>/dev/null) || V=\"\"",
		]
	)
	got = raw_reads(text)
	assert got == [
		'X=$(gh api "repos/o/r/pulls/1" --jq .state)',
		'gh api -X GET "repos/o/r/issues" -f per_page=1',
		"gh run view 5 --repo o/r --json status",
		'command gh api -i "$@"',
		'GH_TOKEN=x gh api repos/o/r/issues',
		'A=1 B="2" gh pr view 5',
		'env gh issue list --repo o/r',
		'f() { gh api repos/o/r/labels; }',
		'( gh run list --repo o/r )',
		'${SUDO} gh api repos/o/r/hooks',
		'if ! R="$(_safe_gh_jq "repos/o/r/runs" --jq .x)"; then',
		'gh api "repos/o/r/labels/x" >/dev/null 2>&1 ||  gh api "repos/o/r/labels"  -f name="x" >/dev/null 2>&1 || true',
		"V=$(gh api repos/o/r/z --jq '.a | .b' 2>/dev/null) || V=\"\"",
	], got


def test_scanner_treats_backtick_substitution_as_a_command_start() -> None:
	# Review of #10225: backtick command substitution starts a command too.
	text = "\n".join(
		[
			"R=`gh api repos/o/r/issues`",
			"S=`_safe_gh_jq repos/o/r/runs --jq .x`",
			"T=`gh_api_retry repos/o/r/issues`",
		]
	)
	got = raw_reads(text)
	assert got == ["R=`gh api repos/o/r/issues`", "S=`_safe_gh_jq repos/o/r/runs --jq .x`"], got


def test_scanner_splits_calls_chained_inside_a_quoted_substitution() -> None:
	# Review of #10225: a GET chained to a write inside one quoted `$(...)` or
	# backtick substitution is judged on its own flags, so it is still a read.
	text = "\n".join(
		[
			'X="$(gh api repos/o/r/issues; gh api repos/o/r/labels -f name=x)"',
			'Y="`gh api repos/o/r/issues; gh api repos/o/r/labels -f name=x`"',
			'Z="$(gh api repos/o/r/labels -f name=x; gh api repos/o/r/issues)"',
			'W="$(gh api repos/o/r/labels -f name=x; gh api repos/o/r/hooks -X POST)"',
			'V="$(gh_api_retry repos/o/r/issues; gh api repos/o/r/labels -f name=x)"',
		]
	)
	got = raw_reads(text)
	assert got == [
		'X="$(gh api repos/o/r/issues; gh api repos/o/r/labels -f name=x)"',
		'Y="`gh api repos/o/r/issues; gh api repos/o/r/labels -f name=x`"',
		'Z="$(gh api repos/o/r/labels -f name=x; gh api repos/o/r/issues)"',
	], got


def test_scanner_ignores_flags_inside_quotes_and_knows_more_command_starts() -> None:
	# Review of #10225: a ` -f ` or ` -X POST` inside a quoted filter is not a
	# flag, a quoted bare method still is, and `elif`, a case-pattern `)`,
	# `<(` and a line-start `(` start a command.
	text = "\n".join(
		[
			"Q=$(gh api repos/o/r/q --jq 'select(.body | contains(\" -f \"))')",
			'P=$(gh api repos/o/r/p --jq ". | contains(\\" -X POST\\")")',
			'gh api repos/o/r/labels -X "POST" -f name=x',
			"elif gh api repos/o/r/e; then",
			"*) gh run list --repo o/r ;;",
			"while read -r n; do :; done < <(gh api repos/o/r/w)",
			"(gh api repos/o/r/s)",
			"} && _safe_gh_jq repos/o/r/t",
			'echo "x (gh api returned empty)"',
		]
	)
	got = raw_reads(text)
	assert got == [
		"Q=$(gh api repos/o/r/q --jq 'select(.body | contains(\" -f \"))')",
		'P=$(gh api repos/o/r/p --jq ". | contains(\\" -X POST\\")")',
		"elif gh api repos/o/r/e; then",
		"*) gh run list --repo o/r ;;",
		"while read -r n; do :; done < <(gh api repos/o/r/w)",
		"(gh api repos/o/r/s)",
		"} && _safe_gh_jq repos/o/r/t",
	], got


def test_allowlist_covers_each_read_not_the_whole_line() -> None:
	# Review of #10225: an allowlisted call must not hide a second raw read
	# on the same logical line (for example one joined by `|| \` or `;`).
	allow = (("f.sh", 'A="$(timeout 20 gh api', "fixture"),)
	ok = 'A="$(timeout 20 gh api repos/o/r/x)" || true'
	both = 'A="$(timeout 20 gh api repos/o/r/x)" || B="$(gh api repos/o/r/y)"'
	safe = 'A="$(timeout 20 gh api repos/o/r/x)"; C="$(_safe_gh_jq repos/o/r/z)"'
	write = 'A="$(timeout 20 gh api repos/o/r/x)" || gh api repos/o/r/labels -f name=x'
	assert _unlisted_reads("f.sh", ok, allow) == ([], {0})
	missing, used = _unlisted_reads("f.sh", both, allow)
	assert missing == [both.index("api repos/o/r/y")] and used == {0}, missing
	missing, _used = _unlisted_reads("f.sh", safe, allow)
	assert missing == [safe.index("_safe_gh_jq")], missing
	assert _unlisted_reads("f.sh", write, allow) == ([], {0})
	assert _unlisted_reads("other.sh", ok, allow)[0] == [ok.index("api repos")]


def test_gate_reads_go_through_rate_limit_aware_helpers() -> None:
	for path, needle, reason in ALLOWLIST:
		assert reason.strip(), (path, needle)
	unlisted: list[str] = []
	used: set[int] = set()
	for path, line in _all_raw_reads():
		# Every raw read on the line needs its own covering needle (review of
		# #10225): one allowlisted call must not hide another read beside it.
		missing, hit = _unlisted_reads(path, line, ALLOWLIST)
		if missing:
			unlisted.append(f"{path}: {line} (unlisted read at offset(s) {missing})")
		used.update(hit)
	assert not unlisted, "raw gh reads outside the allowlist (route through gh_api_retry):\n" + "\n".join(unlisted)
	stale = [ALLOWLIST[i][:2] for i in range(len(ALLOWLIST)) if i not in used]
	assert not stale, f"allowlist entries match nothing: {stale}"


def _job_env(job: dict) -> dict:
	return dict(job.get("env") or {})


def test_pat_jobs_bound_helper_waits() -> None:
	jobs = _workflow()["jobs"]
	for job_id in PAT_JOBS:
		job = jobs[job_id]
		env = _job_env(job)
		assert "secrets.GH_PAT" in env.get("GH_TOKEN", ""), job_id
		attempts = int(env["GH_RETRY_MAX_ATTEMPTS"])
		wait = int(env["GH_RETRY_RATE_LIMIT_MAX_WAIT_SECS"])
		assert 1 <= attempts <= 3, job_id
		assert wait == 600, job_id  # the helper's default: no wait is shortened
		worst = (attempts - 1) * wait
		assert worst < int(job["timeout-minutes"]) * 60, job_id
		if job_id == "e2e-smoke-test":
			assert worst <= int(env["E2E_FINALIZATION_RESERVE_MINUTES"]) * 60
		if job_id == "orchestrate-decompose-test":
			# Its dispatch step has no step limit: the 3000 s watch, the step's
			# dispatch rate-limit cap (#10205) and 360 s for the other steps
			# share the job limit with one read's reset wait (#10225).
			cap = re.search(r'DISPATCH_RATE_LIMIT_MAX_WAIT_SECS: "([0-9]+)"', "".join(st["text"] for st in job["steps"]))
			assert cap is not None, job_id
			assert 3000 + int(cap.group(1)) + 360 + worst <= int(job["timeout-minutes"]) * 60, job_id
		for step in job.get("steps") or []:
			run = step.get("run") or ""
			if "timeout-minutes" in step and re.search(r"\bgh_(api_)?retry\b", run):
				assert worst < int(step["timeout-minutes"]) * 60, (job_id, step.get("name"))


def _budget_steps(job_id: str, job: dict) -> tuple[dict, dict]:
	steps = job.get("steps") or []
	start = [s for s in steps if s.get("name") == f"Record GH_PAT budget at {job_id} start"]
	end = [s for s in steps if s.get("name") == f"Record GH_PAT budget at {job_id} end"]
	assert len(start) == 1 and len(end) == 1, job_id
	return start[0], end[0]


def test_pat_jobs_fill_gh_pat_budget_lines() -> None:
	jobs = _workflow()["jobs"]
	for job_id in PAT_JOBS:
		job = jobs[job_id]
		start, end = _budget_steps(job_id, job)
		assert (job.get("steps") or [])[0] is start, job_id
		body = start["run"]
		assert "gh api rate_limit --jq '[.resources.core.remaining, .resources.core.reset] | @tsv'" in body, job_id
		assert "^[0-9]+[[:space:]][0-9]+$" in body, job_id
		assert f'"${{RUNNER_TEMP}}/gh_pat_budget_{job_id}"' in body, job_id
		assert f"job={job_id} remaining=unknown reset=unknown used_in_job=unknown" in body, job_id
		assert "no rate_limit read" not in str(start), job_id
		assert end.get("if") == "always()", job_id
		ebody = end["run"]
		assert f'gh_pat_budget end test-and-mark-stable {job_id} "${{RUNNER_TEMP}}/gh_pat_budget_{job_id}"' in ebody, job_id
		assert f"job={job_id} remaining=unknown reset=unknown used_in_job=unknown ${{counter_keys}}" in ebody, job_id
		assert "gh-call-counter-summary" in ebody, job_id
	for job_id, reason in BUDGET_EXEMPT_JOBS.items():
		assert reason
		start, end = _budget_steps(job_id, jobs[job_id])
		assert "rate_limit" not in start["run"] and "gh_pat_budget end" not in end["run"]
		assert "secrets.GH_PAT" not in str(jobs[job_id].get("env") or {})


def test_budget_steps_never_fail_the_job() -> None:
	"""Run every start body with a failing and a garbage `gh`: exit 0, unknown."""
	import os
	import subprocess
	import tempfile

	jobs = _workflow()["jobs"]
	for reply, expect in (("", "remaining=unknown reset=unknown"), ("4990\t1795810000", "remaining=4990 reset=1795810000"), ("x\ty", "remaining=unknown")):
		for job_id in PAT_JOBS:
			start, _end = _budget_steps(job_id, jobs[job_id])
			with tempfile.TemporaryDirectory() as tmp:
				stub = (
					f"gh() {{ if [ -n '{reply}' ]; then printf '{reply}\\n'; return 0; fi; return 1; }}\n"
				)
				proc = subprocess.run(
					["bash", "-e", "-c", stub + start["run"]],
					env=dict(os.environ, RUNNER_TEMP=tmp),
					capture_output=True,
					text=True,
					check=False,
				)
				assert proc.returncode == 0, (job_id, proc.stderr)
				assert f"GH_PAT_BUDGET phase=start workflow=test-and-mark-stable job={job_id} {expect}" in proc.stdout, (job_id, proc.stdout)
				snap = Path(tmp) / f"gh_pat_budget_{job_id}"
				if expect.startswith("remaining=4990"):
					assert snap.read_text(encoding="utf-8").strip() == "4990 1795810000"
				else:
					assert not snap.exists()


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	sys.exit(main())
