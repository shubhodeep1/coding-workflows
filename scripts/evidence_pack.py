#!/usr/bin/env python3
"""Evidence packs (issue #8427, phase P4 of
docs/plans/persistent-unattended-workflows-plan.md).

Host-side helper that builds a short, fenced block of repository evidence for
the clarify, plan and implement prompts:

    OPEN PULL REQUESTS TOUCHING THESE PATHS  same-repo, trusted-author open PRs
                                             whose changed files overlap the paths
    RECENT HISTORY                           per-path `git log` subjects of the
                                             default branch
    DEFAULT BRANCH RUNS                      recent completed default-branch runs
                                             (workflow name, conclusion, created_at)

Every line is third-party text (PR titles, commit subjects, workflow names,
paths named by issue text), so the block is fenced
``=== BEGIN/END UNTRUSTED EVIDENCE PACK ===`` and every body line goes through
``attempt_journal._scrub`` (log-command neutralising, ``<!--``, ``#N``,
``@name`` and fence-forgery breaking). Callers append it after the dynamic
context. The model never holds the token used here.

Switch: PERSIST_EVIDENCE_PACK_ENABLED (default ``false``, defaulted here per
the unattended instructions section 8). With it off ``build`` writes nothing,
makes no call and logs
``PERSIST evidence_pack outcome=skipped calls=0 bytes=0 reason=switch_off``.
Byte cap: --max-bytes, else PERSIST_EVIDENCE_PACK_MAX_BYTES, else 12000;
clamped to 1024..65536. Truncation is on line boundaries and always keeps the
end fence.

API budget (section 14): at most 4 logical calls per build, all reads through
``gh_api_retry.call(optional=True)`` (honours the rate-limit breaker):
1 REST ``pulls?state=open`` page (50 newest by update), at most 2 aliased
GraphQL reads of PR file lists (25 PRs each), 1 REST ``actions/runs`` page.
No call is made for PRs when no path was found. Audited existing calls: the
callers (clarify, plan, implement) hold no open-PR file lists or run lists,
so none can be reused.

Fail-open: ``build`` exits 0 once its arguments parse. A missing helper
import, an invalid --repo or any exception writes a stub block holding
``(evidence unavailable: <reason>)``. Logs are tokens and counts only:
``PERSIST evidence_pack outcome=<ok|partial|skipped> calls=<n> bytes=<n>
[reason=<token>]``.

History fetch (guarded): ``git fetch`` of the default branch into the private
ref ``refs/persist/history`` (never ``refs/heads/*``, deleted after use) with
``--depth=200`` only on an already-shallow checkout and ``--filter=blob:none``
only when ``extensions.partialclone`` is ``origin``, so a full clone never
becomes shallow.

CLI:
	build --repo R --out F [--default-branch B] [--repo-root D] [--paths-file F]
		[--text-file F] [--plan-file F] [--max-bytes N]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
	sys.path.insert(0, str(_SCRIPT_DIR))
try:
	import attempt_journal as _aj  # noqa: E402
	import targeted_file_context as _tfc  # noqa: E402
except Exception:  # noqa: BLE001 - fail open: the build writes the stub
	_aj = None
	_tfc = None

BEGIN_FENCE = "=== BEGIN UNTRUSTED EVIDENCE PACK (host-built; titles and subjects are third-party text) ==="
END_FENCE = "=== END UNTRUSTED EVIDENCE PACK ==="
SECTION_PRS = "OPEN PULL REQUESTS TOUCHING THESE PATHS"
SECTION_HISTORY = "RECENT HISTORY"
SECTION_RUNS = "DEFAULT BRANCH RUNS"
DEFAULT_MAX_BYTES = 12000
MIN_MAX_BYTES = 1024
MAX_MAX_BYTES = 65536
MAX_PATHS = 10
MAX_PRS = 10
TITLE_MAX = 120
SUBJECT_MAX = 120
PATH_MAX = 300
LOG_PER_PATH = 8
PR_PAGE = 50
GRAPHQL_BATCH = 25
GRAPHQL_MAX_QUERIES = 2
RUNS_PAGE = 10
# Advisory reads: one attempt each, so a build makes at most 4 reads (plus
# gh_api_retry's one free `rate_limit` lookup when a read is rate-limited
# without a reset header) and never sleeps on a retry or rate limit.
GH_MAX_ATTEMPTS = 1
FREE_TEXT_CANDIDATES_MAX = 200
FETCH_TIMEOUT_SECS = 60
HISTORY_REF = "refs/persist/history"
BOT_LOGIN = "github-actions[bot]"
TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_CONCLUSION_RE = re.compile(r"^[a-z_]{1,40}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_BARE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_./-])((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9]+)")


def log(outcome: str, calls: int, size: int, reason: str = "") -> None:
	line = f"PERSIST evidence_pack outcome={outcome} calls={calls} bytes={size}"
	if reason:
		line += " reason=" + re.sub(r"[^A-Za-z0-9_.-]", "_", reason)[:80]
	print(line, file=sys.stderr)


def enabled() -> bool:
	return os.environ.get("PERSIST_EVIDENCE_PACK_ENABLED", "false").strip().lower() == "true"


def max_bytes(arg: str | None) -> int:
	raw = arg if arg not in (None, "") else os.environ.get("PERSIST_EVIDENCE_PACK_MAX_BYTES", "")
	try:
		value = int(str(raw).strip())
	except ValueError:
		return DEFAULT_MAX_BYTES
	return min(MAX_MAX_BYTES, max(MIN_MAX_BYTES, value))


def valid_branch(name: Any) -> bool:
	if not isinstance(name, str) or not _BRANCH_RE.match(name):
		return False
	if ".." in name or name.startswith(("-", "/")) or name.endswith((".lock", "/", ".")) or "//" in name:
		return False
	return True


def resolve_default_branch(arg: str | None) -> str:
	name: Any = arg or ""
	if not name:
		event_path = os.environ.get("GITHUB_EVENT_PATH", "")
		if event_path:
			try:
				event = json.loads(Path(event_path).read_text(encoding="utf-8"))
				repo = event.get("repository") if isinstance(event, dict) else None
				name = repo.get("default_branch", "") if isinstance(repo, dict) else ""
			except (OSError, ValueError):
				name = ""
	return name if valid_branch(name) else ""


def _one_line(value: Any, limit: int) -> str:
	text = " ".join(str(value if value is not None else "").split())
	return text[:limit]


# --- paths ----------------------------------------------------------------------

def _git(root: Path, args: list[str], timeout: int = FETCH_TIMEOUT_SECS) -> subprocess.CompletedProcess | None:
	env = dict(os.environ)
	env["GIT_TERMINAL_PROMPT"] = "0"
	try:
		return subprocess.run(
			["git", "--literal-pathspecs", "-C", str(root), *args],
			capture_output=True, text=True, errors="replace", timeout=timeout, env=env, check=False,
		)
	except (OSError, subprocess.SubprocessError):
		return None


def safe_rel_path(value: Any, root: Path, allow_missing: bool = False) -> str | None:
	"""Relative path of a regular file inside root, or None.

	allow_missing (trusted --paths-file / --plan-file entries only) also keeps a
	path that does not exist yet, so a file the plan creates still reaches the
	open-PR overlap check. Its existing parent must still resolve inside root,
	and an existing entry must still be a regular file that is not a symlink.
	"""
	if not isinstance(value, str):
		return None
	value = value.strip()
	if not value or len(value) > PATH_MAX or _CONTROL_RE.search(value) or "\\" in value:
		return None
	while value.startswith("./"):
		value = value[2:]
	if not value or value.startswith(("/", "-", "~")):
		return None
	parts = value.split("/")
	if any(part in ("", ".", "..") for part in parts) or parts[0] == ".git":
		return None
	candidate = root / value
	try:
		if candidate.is_symlink():
			return None
		if allow_missing and not os.path.lexists(candidate):
			resolved = candidate.resolve(strict=False)
			rel = resolved.relative_to(root)
			if rel.parts and rel.parts[0] == ".git":
				return None
			return value
		resolved = candidate.resolve(strict=True)
		rel = resolved.relative_to(root)
	except (OSError, ValueError, RuntimeError):
		return None
	if rel.parts and rel.parts[0] == ".git":
		return None
	if not resolved.is_file():
		return None
	return value


def _read_text(path: str | None) -> str:
	if not path:
		return ""
	try:
		return Path(path).read_text(encoding="utf-8", errors="replace")
	except OSError:
		return ""


def _likely_section(text: str) -> str:
	"""Text from the last Files-likely-to-change heading onward ('' when none)."""
	lines = text.splitlines()
	last = -1
	for index, line in enumerate(lines):
		if _tfc.LIKELY_HEADER_RE.search(line):
			last = index
	return "\n".join(lines[last:]) if last >= 0 else ""


def _tracked(root: Path, candidates: list[str]) -> set[str]:
	if not candidates:
		return set()
	proc = _git(root, ["ls-files", "-z", "--", *candidates])
	if proc is None or proc.returncode != 0:
		return set()
	return {item for item in proc.stdout.split("\0") if item}


def collect_paths(paths_file: str | None, text_file: str | None, plan_file: str | None, repo_root: str | Path) -> list[str]:
	"""At most MAX_PATHS safe paths: --paths-file, then --plan-file, then --text-file."""
	root = Path(repo_root).resolve()
	found: list[str] = []
	seen: set[str] = set()

	def add(candidate: Any, allow_missing: bool = False) -> None:
		if len(found) >= MAX_PATHS:
			return
		path = safe_rel_path(candidate, root, allow_missing)
		if path and path not in seen:
			seen.add(path)
			found.append(path)

	# Trusted inputs: a planned new file is kept so open PRs adding it are found.
	for line in _read_text(paths_file).splitlines():
		add(line, allow_missing=True)
	plan_text = _read_text(plan_file)
	if plan_text:
		for path in _tfc.extract_paths_from_plan(_likely_section(plan_text)):
			add(path, allow_missing=True)
	text = _read_text(text_file)
	if text and len(found) < MAX_PATHS:
		# Untrusted issue text: every candidate must also be a tracked file.
		ordered: list[str] = []
		local_seen: set[str] = set()
		raw_candidates = list(_tfc.extract_paths_from_plan(_likely_section(text)))
		for token in _tfc.PATH_IN_BACKTICKS_RE.findall(text):
			normalised = _tfc.normalize_path(token)
			if normalised:
				raw_candidates.append(normalised)
		raw_candidates.extend(_BARE_PATH_RE.findall(text))
		for candidate in raw_candidates:
			path = safe_rel_path(candidate, root)
			if path and path not in local_seen and path not in seen:
				local_seen.add(path)
				ordered.append(path)
			if len(ordered) >= FREE_TEXT_CANDIDATES_MAX:
				break
		tracked = _tracked(root, ordered)
		for path in ordered:
			if path in tracked:
				add(path)
	return found


# --- builder ----------------------------------------------------------------------

class Builder:
	def __init__(self) -> None:
		self.calls = 0
		self.partial = False
		self.reason = ""

	def gh(self, args: list[str], idempotent: bool = False) -> bytes | None:
		"""One logical gh api read through gh_api_retry (optional: honours the breaker)."""
		try:
			import gh_api_retry  # noqa: PLC0415
		except Exception:  # noqa: BLE001
			# No raw fallback call: it would bypass the rate-limit breaker. The
			# reason goes on the single final PERSIST line.
			self.partial = True
			self.reason = self.reason or "gh_api_retry_unavailable"
			return None
		self.calls += 1
		result = gh_api_retry.call(args, idempotent=idempotent, optional=True, max_attempts=GH_MAX_ATTEMPTS)
		return result.stdout if result.rc == 0 else None

	def _json(self, raw: bytes | None) -> Any:
		if raw is None:
			return None
		try:
			return json.loads(raw.decode("utf-8", "replace"))
		except ValueError:
			return None

	def open_prs(self, repo: str, paths: list[str]) -> list[str] | None:
		if not paths:
			return ["(no paths to match)"]
		data = self._json(self.gh([f"repos/{repo}/pulls?state=open&sort=updated&direction=desc&per_page={PR_PAGE}"]))
		if not isinstance(data, list):
			return None
		trusted: list[tuple[int, str]] = []
		for pr in data[:PR_PAGE]:
			if not isinstance(pr, dict):
				continue
			number = pr.get("number")
			if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
				continue
			head = pr.get("head")
			head_repo = head.get("repo") if isinstance(head, dict) else None
			if not isinstance(head_repo, dict) or head_repo.get("full_name") != repo:
				continue
			user = pr.get("user")
			login = user.get("login") if isinstance(user, dict) else None
			if login != BOT_LOGIN and pr.get("author_association") not in TRUSTED_ASSOCIATIONS:
				continue
			trusted.append((number, pr.get("title") if isinstance(pr.get("title"), str) else ""))
		if not trusted:
			return ["(none)"]
		owner, name = repo.split("/", 1)
		wanted = set(paths)
		files_by_pr: dict[int, list[str]] = {}
		failed = False
		limit = GRAPHQL_BATCH * GRAPHQL_MAX_QUERIES
		for start in range(0, min(len(trusted), limit), GRAPHQL_BATCH):
			chunk = trusted[start:start + GRAPHQL_BATCH]
			aliases = "".join(
				"p%d:pullRequest(number:%d){files(first:100){nodes{path}}}" % (number, number) for number, _ in chunk
			)
			query = "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){" + aliases + "}}"
			data = self._json(self.gh(
				["graphql", "-f", f"query={query}", "-f", f"owner={owner}", "-f", f"name={name}"], idempotent=True,
			))
			repository = ((data or {}).get("data") or {}).get("repository") if isinstance(data, dict) else None
			if not isinstance(repository, dict):
				failed = True
				continue
			for number, _ in chunk:
				node = repository.get(f"p{number}")
				files = node.get("files") if isinstance(node, dict) else None
				nodes = files.get("nodes") if isinstance(files, dict) else None
				if not isinstance(nodes, list):
					continue
				files_by_pr[number] = [n.get("path") for n in nodes if isinstance(n, dict) and isinstance(n.get("path"), str)]
		lines: list[str] = []
		for number, title in trusted:
			overlap = [p for p in paths if p in wanted and p in files_by_pr.get(number, [])]
			if not overlap:
				continue
			lines.append(f"#{number} {_one_line(title, TITLE_MAX)} -- {', '.join(overlap)}")
			if len(lines) >= MAX_PRS:
				break
		if failed:
			self.partial = True
			lines.append("(some pull request file lists unavailable)")
		return lines or ["(none)"]

	def history(self, root: Path, branch: str, paths: list[str]) -> list[str]:
		if not paths:
			return ["(no paths)"]
		shallow = _git(root, ["rev-parse", "--is-shallow-repository"])
		partial_clone = _git(root, ["config", "--get", "extensions.partialclone"])
		fetch = ["fetch", "--no-tags", "--no-write-fetch-head", "--quiet"]
		if shallow is not None and shallow.returncode == 0 and shallow.stdout.strip() == "true":
			fetch.append("--depth=200")
		if partial_clone is not None and partial_clone.returncode == 0 and partial_clone.stdout.strip() == "origin":
			fetch.append("--filter=blob:none")
		fetch += ["origin", f"+refs/heads/{branch}:{HISTORY_REF}"]
		proc = _git(root, fetch)
		if proc is None or proc.returncode != 0:
			self.partial = True
			return ["history unavailable"]
		lines: list[str] = []
		try:
			for path in paths[:MAX_PATHS]:
				lines.append(f"{path}:")
				log_proc = _git(root, ["log", "-n", str(LOG_PER_PATH), "--no-color", "--format=%h %as %s", HISTORY_REF, "--", path])
				if log_proc is None or log_proc.returncode != 0:
					self.partial = True
					lines.append("  (history unavailable)")
					continue
				entries = [line for line in log_proc.stdout.splitlines() if line.strip()]
				if not entries:
					lines.append("  (no commits)")
				for entry in entries[:LOG_PER_PATH]:
					lines.append("  " + _one_line(entry, 20 + SUBJECT_MAX))
		finally:
			_git(root, ["update-ref", "-d", HISTORY_REF])
		return lines

	def branch_health(self, repo: str, branch: str) -> list[str] | None:
		data = self._json(self.gh([
			f"repos/{repo}/actions/runs?branch={quote(branch, safe='')}&status=completed&per_page={RUNS_PAGE}",
		]))
		runs = data.get("workflow_runs") if isinstance(data, dict) else None
		if not isinstance(runs, list):
			return None
		lines: list[str] = []
		for run in runs[:RUNS_PAGE]:
			if not isinstance(run, dict):
				continue
			# `name` is the workflow name; `display_title` can carry PR titles and is never rendered.
			name = _one_line(run.get("name"), TITLE_MAX) or "-"
			conclusion = run.get("conclusion")
			conclusion = conclusion if isinstance(conclusion, str) and _CONCLUSION_RE.match(conclusion) else "-"
			created = run.get("created_at")
			created = created if isinstance(created, str) and _TS_RE.match(created) else "-"
			lines.append(f"{name} {conclusion} {created}")
		return lines or ["(none)"]


# --- render -----------------------------------------------------------------------

def _scrub_line(line: str) -> str:
	return _aj._scrub(_CONTROL_RE.sub(" ", str(line)))


def render(sections: list[tuple[str, list[str] | None]], max_bytes_value: int = DEFAULT_MAX_BYTES) -> str:
	"""Fenced block; each body line scrubbed; truncated on line boundaries to max_bytes."""
	body: list[str] = []
	for title, lines in sections:
		body.append(title)
		for line in (lines if lines is not None else ["(unavailable)"]):
			body.append(_scrub_line(line))
	budget = max_bytes_value - len(BEGIN_FENCE.encode("utf-8")) - len(END_FENCE.encode("utf-8")) - 2
	reserve = len(f"({len(body)} lines omitted)".encode("utf-8")) + 1
	out: list[str] = []
	omitted = 0
	for index, line in enumerate(body):
		size = len(line.encode("utf-8")) + 1
		need = size + (reserve if index < len(body) - 1 else 0)
		if need > budget:
			omitted = len(body) - index
			break
		out.append(line)
		budget -= size
	if omitted:
		out.append(f"({omitted} lines omitted)")
	return "\n".join([BEGIN_FENCE, *out, END_FENCE]) + "\n"


def stub(reason: str) -> str:
	token = re.sub(r"[^A-Za-z0-9_.-]", "_", reason)[:80]
	return "\n".join([BEGIN_FENCE, f"(evidence unavailable: {token})", END_FENCE]) + "\n"


def _write(path: str, text: str) -> bool:
	try:
		Path(path).write_text(text, encoding="utf-8")
	except OSError:
		return False
	return True


def _finish(out: str, text: str, outcome: str, calls: int, reason: str = "") -> None:
	if not _write(out, text):
		log("skipped", calls, 0, "out_unwritable")
		return
	log(outcome, calls, len(text.encode("utf-8")), reason)


def cmd_build(args: argparse.Namespace, builder: Builder) -> None:
	cap = max_bytes(args.max_bytes)
	if _aj is None or _tfc is None:
		_finish(args.out, stub("helper_absent"), "skipped", 0, "helper_absent")
		return
	repo = args.repo or ""
	if not _REPO_RE.match(repo) or ".." in repo:
		_finish(args.out, stub("invalid_repo"), "skipped", 0, "invalid_repo")
		return
	root = Path(args.repo_root or ".").resolve()
	paths = collect_paths(args.paths_file, args.text_file, args.plan_file, root)
	branch = resolve_default_branch(args.default_branch)
	prs = builder.open_prs(repo, paths)
	if prs is None:
		builder.partial = True
	reason = ""
	if branch:
		hist: list[str] | None = builder.history(root, branch, paths)
		runs = builder.branch_health(repo, branch)
		if runs is None:
			builder.partial = True
	else:
		reason = "default_branch_unknown"
		builder.partial = True
		hist = ["(skipped: default branch unknown)"]
		runs = ["(skipped: default branch unknown)"]
	reason = reason or builder.reason
	text = render([(SECTION_PRS, prs), (SECTION_HISTORY, hist), (SECTION_RUNS, runs)], cap)
	_finish(args.out, text, "partial" if builder.partial else "ok", builder.calls, reason)


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Evidence pack (PERSIST_EVIDENCE_PACK_ENABLED).")
	sub = parser.add_subparsers(dest="cmd", required=True)
	build = sub.add_parser("build")
	build.add_argument("--repo", required=True)
	build.add_argument("--out", required=True)
	build.add_argument("--default-branch", default="")
	build.add_argument("--repo-root", default=".")
	build.add_argument("--paths-file")
	build.add_argument("--text-file")
	build.add_argument("--plan-file")
	build.add_argument("--max-bytes", default=None)
	return parser


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	# A timeout's SIGTERM raises SystemExit, so the finally in Builder.history
	# still deletes refs/persist/history.
	signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
	if not enabled():
		log("skipped", 0, 0, "switch_off")
		return 0
	builder = Builder()
	try:
		cmd_build(args, builder)
	except Exception as exc:  # noqa: BLE001 - fail open, never block a stage
		_finish(args.out, stub("exception_" + type(exc).__name__), "skipped", builder.calls, "exception_" + type(exc).__name__)
	return 0


if __name__ == "__main__":
	sys.exit(main())
