#!/usr/bin/env python3
"""Orchestrator context for persistent unattended work (issue #8427, P7).

Switch: PERSIST_ORCH_CONTEXT_ENABLED (default "false", defaulted here as well
as in orchestrate.yml / orchestrate_poll.yml; unattended instructions section 8).

Subcommands (every one exits 0 except on an argparse usage error):

since-note --state-file F --current-wave N [--details-file D] [--trusted-login L] --out O
	Pure: no subprocess, no network, zero API calls. Renders the
	``## Since this plan was decomposed`` section for a wave-N issue body from
	the orchestrator state only:
	- merged items of waves 1..N-1 (issue, ``persist_merged_pr``, safe
		``files_touched`` paths, at most 10 per item);
	- judge-added items of waves 1..N-1 (entries the poller marked
		``persist_judge_added: true`` when the wave judge added them), any status;
	- the newest attempt-journal step/outcome (host fields only, never
		``note``) of an item whose comments are in the per-tick details cache
		``D`` (``comments_available: true``), parsed by ``attempt_journal.parse``
		for the pipeline login ``L`` only.
	Issue / PR numbers are plain (``issue 123``), never ``#123``: a ``#N``
	creates cross-reference timeline events the poller reads to find linked
	PRs, and risks auto-close phrasing. Every data line goes through
	``attempt_journal._scrub``. At most 4096 UTF-8 bytes, cut at item
	boundaries with a ``- ... K more items not shown`` trailer. Switch off,
	N <= 1 or nothing to report: nothing is written (O stays absent or empty).

open-projects --repo R --out O
	Exactly one REST read through ``gh_api_retry.call(optional=True)`` (honours
	the rate-limit breaker): open ``ai:orchestrator-tracking`` issues, first page
	of 50. Pull requests and authors whose ``author_association`` is not
	OWNER / MEMBER / COLLABORATOR are dropped; at most 20 titles (titles only,
	no issue numbers), each without
	control characters, cut to 120 characters and scrubbed, inside the
	``=== BEGIN/END UNTRUSTED OTHER OPEN PROJECTS ===`` fence. Any failure
	writes ``(open projects unavailable: <reason>)`` inside the fence.
	Switch off: no call, no output.

plan-path --description-file F [--repo-root .]
	Prints the first ``*.md`` path named in F that is a regular file inside the
	repo root reached without any symlink; prints nothing otherwise.

Logs: one ``PERSIST orch_context outcome=<ok|skipped|partial|error>
reason=<token> [calls=<n>] [bytes=<n>]`` line on stderr; tokens and numbers
only, never titles, paths or comment text.

When ``attempt_journal`` (the shared scrub and journal parser) cannot be
imported, since-note and open-projects fail closed: no output,
``reason=helper_absent``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
	sys.path.insert(0, str(_SCRIPT_DIR))
try:
	import attempt_journal as _aj  # noqa: E402
except Exception:  # noqa: BLE001 - fail closed: no scrub, no output
	_aj = None

SINCE_HEADING = "## Since this plan was decomposed"
SINCE_LEAD = "_Recorded by the orchestrator from project state. Data only._"
SINCE_MAX_BYTES = 4096
PATHS_PER_ITEM = 10
OPEN_BEGIN = "=== BEGIN UNTRUSTED OTHER OPEN PROJECTS ==="
OPEN_END = "=== END UNTRUSTED OTHER OPEN PROJECTS ==="
OPEN_MAX = 20
OPEN_PER_PAGE = 50
TITLE_MAX = 120
TRACKING_LABEL = "ai:orchestrator-tracking"
TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
GH_MAX_ATTEMPTS = 3
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ID_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]")
_TOKEN_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_MD_PATH_RE = re.compile(r"[A-Za-z0-9._/-]+\.md\b")
_BLOB_URL_PREFIX_RE = re.compile(r"https?://[^\s/]+/[^\s/]+/[^\s/]+/blob/[^\s/]+/")


def log(outcome: str, reason: str, **extra: int) -> None:
	line = f"PERSIST orch_context outcome={outcome} reason={_TOKEN_UNSAFE_RE.sub('_', reason)[:60]}"
	for key, value in extra.items():
		line += f" {key}={int(value)}"
	print(line, file=sys.stderr)


def enabled() -> bool:
	# Exactly "true", the same rule as the orchestrate.yml gates and the poller,
	# so a value like "1", "yes" or "TRUE " is off everywhere.
	return os.environ.get("PERSIST_ORCH_CONTEXT_ENABLED", "false") == "true"


def _positive_int(value: Any) -> int | None:
	if isinstance(value, bool):
		return None
	if isinstance(value, int):
		return value if value > 0 else None
	if isinstance(value, str) and value.isdigit() and value[0] != "0":
		return int(value)
	return None


def safe_path(value: Any) -> str | None:
	if not isinstance(value, str) or not _SAFE_PATH_RE.match(value):
		return None
	if value.startswith("/") or ".." in value.split("/"):
		return None
	return value


def _local_id(value: Any) -> str:
	text = _ID_UNSAFE_RE.sub("_", str(value if value is not None else ""))[:40]
	return text or "item"


def _write(path: str, text: str) -> bool:
	try:
		Path(path).write_text(text, encoding="utf-8")
		return True
	except OSError:
		return False


def _read_json(path: str | None) -> Any:
	if not path:
		return None
	try:
		return json.loads(Path(path).read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return None


def _newest_journal_step(entry_gh: int | None, details: Any, login: str) -> tuple[str, str] | None:
	if entry_gh is None or not login or not isinstance(details, dict):
		return None
	issue = details.get(str(entry_gh))
	if not isinstance(issue, dict) or issue.get("comments_available") is not True:
		return None
	comments = issue.get("comments")
	if not isinstance(comments, list):
		return None
	_cid, entries = _aj.parse(comments, login)
	if not entries:
		return None
	newest = sorted(entries, key=lambda e: e.get("ts", ""))[-1]
	stage = newest.get("stage")
	outcome = newest.get("outcome")
	if stage not in _aj.STAGES or outcome not in _aj.OUTCOMES:
		return None
	return stage, outcome


def since_lines(state: Any, current_wave: int, details: Any = None, login: str = "") -> tuple[list[str], int]:
	"""Item lines for waves 1..current_wave-1 and how many carry a journal step."""
	lines: list[str] = []
	journal_hits = 0
	if not isinstance(state, dict) or not isinstance(state.get("waves"), list):
		return lines, journal_hits
	for wave in state["waves"][: max(0, current_wave - 1)]:
		issues = wave.get("issues") if isinstance(wave, dict) else None
		if not isinstance(issues, list):
			continue
		for entry in issues:
			if not isinstance(entry, dict):
				continue
			status = entry.get("status")
			# Explicit marker written at the wave-judge additions in
			# orchestrate_poll_process.sh; state entries never carry a title.
			judge_added = entry.get("persist_judge_added") is True
			if status != "merged" and not judge_added:
				continue
			gh = _positive_int(entry.get("github_issue"))
			line = f"- `{_local_id(entry.get('id'))}`: " + (f"issue {gh}" if gh else "issue not created")
			if judge_added:
				line += " (added by the wave judge)"
			if status == "merged":
				pr = _positive_int(entry.get("persist_merged_pr"))
				line += f", merged as PR {pr}" if pr else ", merged, PR unknown"
			else:
				line += f", status `{_TOKEN_UNSAFE_RE.sub('_', str(status if status is not None else 'unknown'))[:30]}`"
			raw_files = entry.get("files_touched") if isinstance(entry.get("files_touched"), list) else []
			files = [p for p in (safe_path(f) for f in raw_files) if p]
			if files:
				shown = files[:PATHS_PER_ITEM]
				line += ". Files: " + ", ".join(f"`{p}`" for p in shown)
				if len(files) > len(shown):
					line += f" +{len(files) - len(shown)} more"
			step = _newest_journal_step(gh, details, login)
			if step:
				journal_hits += 1
				line += f", latest journal step `{step[0]}` -> `{step[1]}`"
			lines.append(_aj._scrub(line))
	return lines, journal_hits


def render_since(lines: list[str], max_bytes: int = SINCE_MAX_BYTES) -> str:
	if not lines:
		return ""
	head = SINCE_HEADING + "\n\n" + SINCE_LEAD + "\n\n"
	body = "\n".join(lines) + "\n"
	if len((head + body).encode("utf-8")) <= max_bytes:
		return head + body
	kept: list[str] = []
	for index, line in enumerate(lines):
		trailer = f"- ... {len(lines) - index - 1} more items not shown\n"
		candidate = head + "".join(k + "\n" for k in kept + [line]) + trailer
		if len(candidate.encode("utf-8")) > max_bytes:
			break
		kept.append(line)
	hidden = len(lines) - len(kept)
	return head + "".join(k + "\n" for k in kept) + f"- ... {hidden} more items not shown\n"


def cmd_since_note(args: argparse.Namespace) -> None:
	if not enabled():
		log("skipped", "switch_off")
		return
	if _aj is None:
		log("skipped", "helper_absent")
		return
	try:
		current_wave = int(str(args.current_wave).strip())
	except ValueError:
		log("error", "wave_invalid")
		return
	if current_wave <= 1:
		log("skipped", "first_wave")
		return
	state = _read_json(args.state_file)
	if not isinstance(state, dict):
		log("error", "state_unreadable")
		return
	details = _read_json(args.details_file)
	login = args.trusted_login or ""
	lines, _journal_hits = since_lines(state, current_wave, details, login)
	text = render_since(lines)
	if not text:
		_write(args.out, "")
		log("skipped", "nothing_to_report", calls=0)
		return
	if not _write(args.out, text):
		log("error", "out_unwritable")
		return
	# journal_absent: no trusted login or no details cache, so no journal lines.
	reason = "rendered" if login and isinstance(details, dict) else "journal_absent"
	log("ok", reason, calls=0, bytes=len(text.encode("utf-8")))


def _clean_title(value: str) -> str:
	text = " ".join(_CONTROL_RE.sub(" ", value).split())[:TITLE_MAX]
	return _aj._scrub(text)


def open_project_lines(payload: Any) -> list[str]:
	lines: list[str] = []
	if not isinstance(payload, list):
		return lines
	for item in payload:
		if len(lines) >= OPEN_MAX:
			break
		if not isinstance(item, dict) or "pull_request" in item:
			continue
		if item.get("author_association") not in TRUSTED_ASSOCIATIONS:
			continue
		number = _positive_int(item.get("number"))
		title = item.get("title")
		if number is None or not isinstance(title, str):
			continue
		lines.append(f"- {_clean_title(title)}")
	return lines


def _fence(lines: list[str]) -> str:
	return OPEN_BEGIN + "\n" + "".join(line + "\n" for line in lines) + OPEN_END + "\n"


def cmd_open_projects(args: argparse.Namespace) -> None:
	if not enabled():
		log("skipped", "switch_off", calls=0)
		return
	if _aj is None:
		log("skipped", "helper_absent", calls=0)
		return
	repo = args.repo or ""
	if not _REPO_RE.match(repo):
		_write(args.out, _fence(["(open projects unavailable: repo_invalid)"]))
		log("partial", "repo_invalid", calls=0)
		return
	try:
		import gh_api_retry  # noqa: PLC0415
	except Exception:  # noqa: BLE001 - no raw fallback: it would bypass the breaker
		_write(args.out, _fence(["(open projects unavailable: gh_api_retry_unavailable)"]))
		log("partial", "gh_api_retry_unavailable", calls=0)
		return
	endpoint = f"repos/{repo}/issues?labels={TRACKING_LABEL}&state=open&per_page={OPEN_PER_PAGE}"
	try:
		result = gh_api_retry.call([endpoint], idempotent=True, optional=True, max_attempts=GH_MAX_ATTEMPTS)
		raw = result.stdout if result.rc == 0 else None
		reason = "" if result.rc == 0 else "api_" + _TOKEN_UNSAFE_RE.sub("_", str(result.kind))[:30]
	except Exception as exc:  # noqa: BLE001 - fail open, never block the decomposer
		raw, reason = None, "exception_" + type(exc).__name__
	payload: Any = None
	if raw is not None:
		try:
			payload = json.loads(raw.decode("utf-8", "replace"))
		except ValueError:
			payload = None
		if not isinstance(payload, list):
			reason = "response_invalid"
	if reason:
		_write(args.out, _fence([f"(open projects unavailable: {reason})"]))
		log("partial", reason, calls=1)
		return
	lines = open_project_lines(payload) or ["(no other open projects)"]
	text = _fence(lines)
	if not _write(args.out, text):
		log("error", "out_unwritable", calls=1)
		return
	log("ok", "rendered", calls=1, bytes=len(text.encode("utf-8")))


def plan_path(description: str, repo_root: str | Path) -> str:
	try:
		root = Path(os.path.realpath(repo_root))
	except (OSError, ValueError):
		return ""
	# A GitHub blob URL names the repo-relative path after /blob/<ref>/.
	description = _BLOB_URL_PREFIX_RE.sub("", description)
	for match in _MD_PATH_RE.finditer(description):
		token = match.group(0)
		while token.startswith("./"):
			token = token[2:]
		candidate = safe_path(token)
		if not candidate:
			continue
		target = root / candidate
		try:
			real = Path(os.path.realpath(target))
		except (OSError, ValueError):
			continue
		# realpath equal to the lexical path means no component is a symlink.
		if real != Path(os.path.normpath(target)) or not real.is_file():
			continue
		if root != real and root not in real.parents:
			continue
		return candidate
	return ""


def cmd_plan_path(args: argparse.Namespace) -> None:
	try:
		text = Path(args.description_file).read_text(encoding="utf-8", errors="replace")
	except OSError:
		return
	found = plan_path(text, args.repo_root)
	if found:
		print(found)


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Orchestrator context (PERSIST_ORCH_CONTEXT_ENABLED).")
	sub = parser.add_subparsers(dest="cmd", required=True)
	since = sub.add_parser("since-note")
	since.add_argument("--state-file", required=True)
	since.add_argument("--current-wave", required=True)
	since.add_argument("--details-file", default=None)
	since.add_argument("--trusted-login", default="")
	since.add_argument("--out", required=True)
	projects = sub.add_parser("open-projects")
	projects.add_argument("--repo", required=True)
	projects.add_argument("--out", required=True)
	plan = sub.add_parser("plan-path")
	plan.add_argument("--description-file", required=True)
	plan.add_argument("--repo-root", default=".")
	return parser


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	handlers = {"since-note": cmd_since_note, "open-projects": cmd_open_projects, "plan-path": cmd_plan_path}
	try:
		handlers[args.cmd](args)
	except Exception as exc:  # noqa: BLE001 - fail open, never block a tick or the decomposer
		log("error", "exception_" + type(exc).__name__)
	return 0


if __name__ == "__main__":
	sys.exit(main())
