#!/usr/bin/env python3
"""Write the `ai:claude-fix-claim` marker that stops two Claude fixers racing.

CLAUDE.md §26.H: before a Claude session fixes a conflict, a failed check, a
review hand-off, or a block on a `claude/*` pull request, it claims the PR's
current head with one comment. The §26 checker and the catch-all sweep read
the claims through `.claude/scripts/check_in_status.py --hand-back`
(`read_fix_claims`) and leave a claimed head alone until the head moves or
the lease (CLAUDE_FIX_CLAIM_LEASE_HOURS, default 3) ends. A `hold` claim parks
the head for a human decision when the hand-back cap is reached; it never
expires while the head stays the same.

Usage:

  claude_fix_claim.py body --head SHA --kind KIND --by ID
  claude_fix_claim.py post --repo OWNER/REPO --pr N --head SHA --kind KIND --by ID

KIND is one of conflict, ci, review, blocked, hold. ID names the claimant:
the session id (`session_…`) or `sweep-run-<run id>` for the sweep.

`post` makes two REST calls (CLAUDE.md §15): one read of the PR, to refuse a
claim on a head that is no longer current or a PR that is no longer open,
and one comment POST. It prints one JSON line (`posted`, `comment_id`,
`reason`) and exits 0 when the claim was posted, 1 when it was refused, and 2
when a read or write failed.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_CHECKER_PATH = Path(__file__).resolve().with_name("check_in_status.py")
_checker_spec = importlib.util.spec_from_file_location("check_in_status", _CHECKER_PATH)
check_in_status = importlib.util.module_from_spec(_checker_spec)
_checker_spec.loader.exec_module(check_in_status)

CLAIM_KINDS = ("conflict", "ci", "review", "blocked", "hold")
KIND_LABELS = {
	"conflict": "merge conflict",
	"ci": "failed checks",
	"review": "review findings",
	"blocked": "blocked PR",
}
CLAIMANT_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def claim_body(head: str, kind: str, by: str) -> str:
	"""Return the claim comment body; the last line is the marker the checker reads."""
	if not HEAD_RE.fullmatch(head or ""):
		raise ValueError("--head must be 40 lowercase hex characters")
	if kind not in CLAIM_KINDS:
		raise ValueError(f"--kind must be one of {', '.join(CLAIM_KINDS)}")
	if not CLAIMANT_RE.fullmatch(by or ""):
		raise ValueError("--by must be 1-80 characters of letters, digits, '_' or '-'")
	lease = check_in_status._env_positive_float("CLAUDE_FIX_CLAIM_LEASE_HOURS", check_in_status.DEFAULT_FIX_CLAIM_LEASE_HOURS)
	cap = int(check_in_status._env_positive_float("CLAUDE_FIX_HAND_BACK_CAP", check_in_status.DEFAULT_FIX_HAND_BACK_CAP))
	if kind == "hold":
		text = (f"**Claude fixes on hold:** this PR reached the cap of {cap} Claude hand-backs (conflict, CI, or block) "
			f"at head `{head[:12]}`. `{by}` has asked a human how to continue. The §26 checker and the catch-all sweep "
			"skip this head until someone pushes or that session resumes (CLAUDE.md §26.H).")
	else:
		text = (f"**Claude fix claim:** `{by}` is fixing the {KIND_LABELS[kind]} at head `{head[:12]}`. Other Claude "
			f"sessions and the catch-all sweep leave this PR alone until the head moves or the {lease:g}-hour lease ends "
			"(CLAUDE.md §26.H).")
	marker = f"<!-- ai:claude-fix-claim:v1 head={head} kind={kind} by={by} -->"
	if not check_in_status.FIX_CLAIM_RE.fullmatch(marker):
		raise ValueError("claim marker does not match check_in_status.FIX_CLAIM_RE")
	return f"{text}\n\n{marker}\n"


def _gh(args: list[str]) -> str:
	try:
		proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
	except (OSError, subprocess.TimeoutExpired) as exc:
		raise check_in_status.ReadError(f"gh {args[0]} failed: {exc}") from exc
	if proc.returncode != 0:
		detail = (proc.stderr or proc.stdout).strip().splitlines()
		raise check_in_status.ReadError(f"gh {' '.join(args[:3])} failed: {detail[-1] if detail else f'exit {proc.returncode}'}")
	return proc.stdout


def post_claim(repo: str, number: int, head: str, kind: str, by: str) -> tuple[int, dict]:
	"""Post one claim on `repo`#`number` after confirming `head` is its open head."""
	if not REPO_RE.fullmatch(repo or ""):
		raise ValueError("--repo must be OWNER/REPO")
	if number <= 0:
		raise ValueError("--pr must be a positive integer")
	body = claim_body(head, kind, by)
	pr = check_in_status.gh_api(f"repos/{repo}/pulls/{number}")
	if pr.get("state") != "open" or pr.get("merged"):
		return 1, {"posted": False, "reason": f"PR #{number} is not open"}
	current_head = (pr.get("head") or {}).get("sha")
	if current_head != head:
		return 1, {"posted": False, "reason": f"PR #{number} head moved to {str(current_head)[:12]}; claim the current head"}
	with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as payload_file:
		json.dump({"body": body}, payload_file)
		payload_path = payload_file.name
	try:
		output = _gh(["api", "-X", "POST", f"repos/{repo}/issues/{number}/comments", "--input", payload_path])
	finally:
		Path(payload_path).unlink(missing_ok=True)
	try:
		created = json.loads(output)
	except ValueError:
		created = {}
	return 0, {"posted": True, "comment_id": created.get("id") if isinstance(created, dict) else None, "reason": f"claimed PR #{number} head {head[:12]} for {kind} as {by}"}


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True)
	for name in ("body", "post"):
		command = sub.add_parser(name)
		if name == "post":
			command.add_argument("--repo", required=True)
			command.add_argument("--pr", type=int, required=True)
		command.add_argument("--head", required=True)
		command.add_argument("--kind", required=True, choices=CLAIM_KINDS)
		command.add_argument("--by", required=True)
	return parser


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	try:
		if args.command == "body":
			sys.stdout.write(claim_body(args.head, args.kind, args.by))
			return 0
		code, result = post_claim(args.repo, args.pr, args.head, args.kind, args.by)
	except ValueError as exc:
		print(json.dumps({"posted": False, "error": str(exc)}))
		return 1
	except (check_in_status.ReadError, KeyError, TypeError) as exc:
		print(json.dumps({"posted": False, "error": str(exc)}))
		return 2
	print(json.dumps(result))
	return code


if __name__ == "__main__":
	sys.exit(main())
