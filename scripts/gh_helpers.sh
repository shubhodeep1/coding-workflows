#!/usr/bin/env bash
# gh_helpers.sh — Rate-limit-aware GitHub API retry helpers.
#
# Source this file in workflow steps and shell scripts that interact
# with the GitHub API via the `gh` CLI or `curl`.
#
# Provides:
#   gh_retry             — run a gh CLI command with rate-limit detection + retry
#   gh_retry_to_file     — like gh_retry but captures stdout to a specified file
#   gh_api_json_to_file  — like gh_retry_to_file but also validates JSON output
#   curl_gh_api          — run a curl command against GitHub API with retry
#   gh_api_retry         — classified `gh api` retry, exit 0/1/2/75 (see below)
#
# Rate limit detection: on 403/429 "rate limit" responses the helper
# queries GitHub's GET /rate_limit endpoint (not itself rate-limited)
# to read X-RateLimit-Reset, then sleeps until reset+1 s (capped at
# 600 s, floored at 1 s, fallback 30 s).  Other transient failures
# use exponential backoff (1 s, 2 s, 4 s, …).  curl_gh_api reads the
# 403/429 response headers directly instead: a numeric Retry-After
# (secondary rate limit) takes precedence over X-RateLimit-Reset —
# see _parse_reset_header.

# Guard against double-sourcing
if [ "${_GH_HELPERS_LOADED:-}" = "1" ]; then
	return 0 2>/dev/null || true
fi
_GH_HELPERS_LOADED=1

# ---------------------------------------------------------------
# Signed job-token comments (run-volume plan item 15, decision D16).
#
# Opt-in by the repository secret AI_COMMENT_SIGNING_SECRET (env of the
# same name, unset = no change at all). When set, a `gh` shim goes first on
# PATH for this process and every child: comment creates that cannot start
# an `issue_comment` wrapper are signed and posted with AI_COMMENT_JOB_TOKEN,
# and comment reads return verified signed comments as if the PAT wrote
# them. See scripts/comment_signature.py. Every other `gh` call execs the
# real binary directly.
#
# The shim and the signer are written from this file into a temp dir under
# RUNNER_TEMP, so they travel wherever gh_helpers.sh is staged (the embedded
# signer is kept byte-identical to scripts/comment_signature.py by
# tests/test_comment_signature.py). Inside Actions the first activation in a
# job also appends the shim dir (and nothing else) to GITHUB_PATH, so every
# later step of the job (Python scripts run straight from a workflow step
# included) goes through the shim. The hardened check-failure triage job
# never gets the secret, so it never activates there.
# ---------------------------------------------------------------
_ai_comment_sig_activate()
{
	[ -n "${AI_COMMENT_SIGNING_SECRET:-}" ] || return 0
	if [ -n "${AI_COMMENT_SIG_SHIM_DIR:-}" ] && [ -x "${AI_COMMENT_SIG_SHIM_DIR}/gh" ]; then
		case ":${PATH}:" in
			*":${AI_COMMENT_SIG_SHIM_DIR}:"*) return 0 ;;
		esac
	fi
	local _acs_real _acs_dir _acs_prev _acs_prev_oldifs="${IFS}"
	# A later step of the job: an earlier step's shim dir is already on PATH
	# (through GITHUB_PATH). Reuse it rather than writing another one.
	IFS=":"
	for _acs_prev in ${PATH}; do
		case "${_acs_prev}" in
			*/ai_comment_sig_shim.*)
				if [ -x "${_acs_prev}/gh" ] && [ -f "${_acs_prev}/comment_signature.py" ]; then
					IFS="${_acs_prev_oldifs}"
					export AI_COMMENT_SIG_SHIM_DIR="${_acs_prev}"
					export AI_COMMENT_SIG_PY="${_acs_prev}/comment_signature.py"
					return 0
				fi
				;;
		esac
	done
	IFS="${_acs_prev_oldifs}"
	if ! command -v python3 >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
		echo "::warning::AI_COMMENT_SIG outcome=inactive reason=python3_or_jq_missing" >&2
		return 0
	fi
	# Resolve the real gh, never a shim from an outer activation (that would
	# make the shim exec itself).
	_acs_real=""
	local _acs_entry _acs_oldifs="${IFS}"
	IFS=":"
	for _acs_entry in ${PATH}; do
		case "${_acs_entry}" in
			*/ai_comment_sig_shim.*) continue ;;
		esac
		if [ -n "${_acs_entry}" ] && [ -x "${_acs_entry}/gh" ] && [ ! -d "${_acs_entry}/gh" ]; then
			_acs_real="${_acs_entry}/gh"
			break
		fi
	done
	IFS="${_acs_oldifs}"
	if [ -z "${_acs_real}" ]; then
		return 0
	fi
	if ! _acs_dir="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/ai_comment_sig_shim.XXXXXX" 2>/dev/null)"; then
		echo "::warning::AI_COMMENT_SIG outcome=inactive reason=mktemp_failed" >&2
		return 0
	fi
	if ! cat > "${_acs_dir}/gh" 2>/dev/null <<'AI_COMMENT_SIG_SHIM'
#!/usr/bin/env bash
# gh shim written by scripts/gh_helpers.sh (_ai_comment_sig_activate).
if [ -z "${AI_COMMENT_SIG_REAL_GH:-}" ] || [ ! -x "${AI_COMMENT_SIG_REAL_GH}" ]; then
	# Environment stripped (e.g. `env -i PATH=...`): find the next gh on PATH.
	_shim_self_dir="$(cd "$(dirname "$0")" && pwd)"
	AI_COMMENT_SIG_REAL_GH=""
	_shim_oldifs="${IFS}"
	IFS=":"
	for _shim_entry in ${PATH}; do
		case "${_shim_entry}" in
			"${_shim_self_dir}"|*/ai_comment_sig_shim.*) continue ;;
		esac
		if [ -n "${_shim_entry}" ] && [ -x "${_shim_entry}/gh" ]; then
			AI_COMMENT_SIG_REAL_GH="${_shim_entry}/gh"
			break
		fi
	done
	IFS="${_shim_oldifs}"
	[ -n "${AI_COMMENT_SIG_REAL_GH}" ] || { echo "gh: not found (AI_COMMENT_SIG shim)" >&2; exit 127; }
	export AI_COMMENT_SIG_REAL_GH
fi
case "${1:-}" in
	api)
		case " $* " in
			*comments*|*" graphql "*) ;;
			*) exec "${AI_COMMENT_SIG_REAL_GH}" "$@" ;;
		esac
		;;
	issue|pr)
		[ "${2:-}" = "comment" ] || exec "${AI_COMMENT_SIG_REAL_GH}" "$@"
		;;
	*) exec "${AI_COMMENT_SIG_REAL_GH}" "$@" ;;
esac
if [ ! -f "${AI_COMMENT_SIG_PY:-}" ]; then
	# Later step of the job (reached through GITHUB_PATH): the signer sits next to the shim.
	AI_COMMENT_SIG_PY="$(cd "$(dirname "$0")" && pwd)/comment_signature.py"
	export AI_COMMENT_SIG_PY
fi
if [ -z "${AI_COMMENT_SIGNING_SECRET:-}" ] || [ ! -f "${AI_COMMENT_SIG_PY}" ]; then
	exec "${AI_COMMENT_SIG_REAL_GH}" "$@"
fi
exec python3 -I -B "${AI_COMMENT_SIG_PY}" gh-shim -- "$@"
AI_COMMENT_SIG_SHIM
	then
		echo "::warning::AI_COMMENT_SIG outcome=inactive reason=shim_write_failed" >&2
		return 0
	fi
	chmod 0755 "${_acs_dir}/gh" || return 0
	_ai_comment_sig_write_signer "${_acs_dir}/comment_signature.py" || {
		echo "::warning::AI_COMMENT_SIG outcome=inactive reason=signer_write_failed" >&2
		return 0
	}
	export AI_COMMENT_SIG_REAL_GH="${_acs_real}"
	export AI_COMMENT_SIG_PY="${_acs_dir}/comment_signature.py"
	export AI_COMMENT_SIG_SHIM_DIR="${_acs_dir}"
	export PATH="${_acs_dir}:${PATH}"
	hash -r 2>/dev/null || true
	# Only the shim dir is persisted (no variables, no credentials): the shim
	# finds the real gh and its signer itself, and passes every call through
	# in a later step that does not hold AI_COMMENT_SIGNING_SECRET.
	if [ -n "${GITHUB_PATH:-}" ] && [ -w "${GITHUB_PATH}" ]; then
		printf '%s\n' "${_acs_dir}" >> "${GITHUB_PATH}" || true
	fi
	echo "AI_COMMENT_SIG outcome=active shim=${_acs_dir}" >&2
}
# Embedded copy of scripts/comment_signature.py (kept identical by
# tests/test_comment_signature.py; edit the .py file, then re-embed).
_ai_comment_sig_write_signer()
{
	cat > "$1" <<'AI_COMMENT_SIGNER_PY'
#!/usr/bin/env python3
"""Signed job-token comments (run-volume plan item 15, decision D16).

A comment posted with `GH_PAT` starts the four `issue_comment` wrappers
(`internal-implement.yml`, `internal-clarify.yml`, `internal-plan.yml`,
`internal-orchestrate-clarify-respond.yml`, or the consumer `ai-*` copies);
one posted with the job's `GITHUB_TOKEN` (`github-actions[bot]`) starts none.
Most pipeline comments are trusted by author: readers keep only comments
written by the PAT login. This module lets those comments move to the job
token without weakening that check.

Opt-in: everything here is inert unless the repository secret
`AI_COMMENT_SIGNING_SECRET` is set (env of the same name). Rollback without
losing trust in comments already signed: set the repository variable
`AI_COMMENT_SIGNING_POST=false` (env of the same name, default `true`); new
comments then go out with the caller's token again, unsigned, while reads keep
verifying. Deleting the secret instead makes every signed comment read as an
untrusted `github-actions[bot]` comment. With the secret set:

* **Post.** A new issue/PR comment that cannot start a wrapper is signed
  (HMAC-SHA256 over repository, issue number, PAT login, PAT account type,
  PAT user id and the body) and posted with `AI_COMMENT_JOB_TOKEN`. The
  signature is one hidden line appended to the body:
  `<!-- ai-comment-sig:v1 repo=<owner/repo> issue=<n> as=<login> type=<User|Bot> uid=<id> sig=<64 hex> -->`.
  A body that could start a wrapper (`/approved`, `/answer`, `/reclarify`,
  `Clarification required`) or that the hardened plan-approval readers match
  (`Implementation Plan`) is posted unchanged with the caller's token.
* **Edit.** A body edit of a comment that `github-actions[bot]` wrote is
  re-signed, so an in-place refresh keeps the comment trusted.
* **Read.** Every `issues/.../comments` read and every `gh api graphql`
  response is normalized: a comment by `github-actions[bot]` whose signature
  verifies is returned as the PAT would have written it (`user.login` = the
  signed login, `user.type` = the signed type, `user.id` = the signed id,
  `author_association` = `OWNER` for the repository owner, else
  `COLLABORATOR`), with the signature line removed from `body`. The signed
  repository and issue must match the comment's own `issue_url` / `url`
  when it carries one; a GraphQL node that selects neither is not
  location-checked. An
  unsigned or badly signed bot comment is returned unchanged, so it stays
  untrusted. Readers need no change.

Entry points
------------
gh-shim -- <gh args...>
    Run by the `gh` shim that `scripts/gh_helpers.sh` puts first on `PATH`
    when the secret is set. Calls the real `gh` (`AI_COMMENT_SIG_REAL_GH`)
    and returns its exit status. Anything that is not a comment create, a
    comment body edit or a comments read is passed through untouched.
sign --repo R --issue N --as LOGIN [--type User|Bot]
    Read a body on stdin, print the signed body (tests and diagnostics).
normalize
    Read a JSON stream (one value or `--paginate` pages) on stdin, print it
    normalized, one compact value per input value.

API calls (§15)
---------------
* One `GET /user` per job for the PAT identity (login, type, id), cached in
  `${RUNNER_TEMP:-${TMPDIR:-/tmp}}/ai_comment_sig_identity_<token hash>.json`.
* Create: the one POST, with the job token. When GitHub refuses the job
  token (401/403, `Resource not accessible by integration`), the original
  command is re-run once with the caller's token, unsigned, and the refusal
  is cached for the job so later posts skip the job token. Any other failure
  is returned as is and never resent: a timeout may already have created the
  comment (gh_retry sends a POST once, issue #6634).
* Edit: one extra `GET issues/comments/<id>` to learn the author and issue.
* Read: no extra call.

Fail-open: any parse error, a missing identity, a `{owner}/{repo}`
placeholder endpoint, an unsupported flag (`--template`, nested `-f key[]=`
fields) or a body over 65,536 characters falls back to running the original
command unchanged.

Log prefix: `AI_COMMENT_SIG` (`outcome=`), on stderr.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import tempfile
from typing import Any

SIG_VERSION = "v1"
BOT_LOGIN = "github-actions[bot]"
MAX_BODY_CHARS = 65_536
SECRET_ENV = "AI_COMMENT_SIGNING_SECRET"
JOB_TOKEN_ENV = "AI_COMMENT_JOB_TOKEN"
POST_SWITCH_ENV = "AI_COMMENT_SIGNING_POST"
REAL_GH_ENV = "AI_COMMENT_SIG_REAL_GH"
LOG_PREFIX = "AI_COMMENT_SIG"

_LOGIN = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?"
_REPO = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
# The line names the repository and issue it is bound to, so a reader that
# has no issue context of its own (a GraphQL node) can still verify it.
SIG_LINE_RE = re.compile(
	r"\n<!-- ai-comment-sig:v1 repo=(?P<repo>" + _REPO + r") issue=(?P<issue>[0-9]+) as=(?P<login>" + _LOGIN
	+ r") type=(?P<type>User|Bot)(?: uid=(?P<uid>[0-9]+))? sig=(?P<sig>[0-9a-f]{64}) -->\Z"
)
_ISSUE_URL_RE = re.compile(r"/repos/(?P<repo>[^/]+/[^/]+)/issues/(?P<num>[0-9]+)/?$")
_HTML_URL_RE = re.compile(r"^https://github\.com/(?P<repo>[^/]+/[^/]+)/(?:issues|pull)/(?P<num>[0-9]+)(?:[#/?].*)?$")
_CREATE_RE = re.compile(r"^repos/(?P<repo>[^/{}]+/[^/{}]+)/issues/(?P<num>[0-9]+)/comments$")
_EDIT_RE = re.compile(r"^repos/(?P<repo>[^/{}]+/[^/{}]+)/issues/comments/(?P<id>[0-9]+)$")
_READ_RE = re.compile(r"^repos/[^/]+/[^/]+/issues/(?:[0-9]+/comments|comments(?:/[0-9]+)?)$")
# GraphQL reports the Actions bot as `github-actions` (type Bot).
_GRAPHQL_BOT_LOGINS = ("github-actions", BOT_LOGIN)

# Bodies that must keep the caller's (PAT) author. The first four are what the
# `issue_comment` wrappers start on; the plan heading is what the hardened
# plan-approval jobs in workflow-templates/ai-implement.yml and ai-plan.yml
# read by the approver login. Matched anywhere in the body and ignoring case
# (the wrappers' contains/startsWith ignore case), so a body that could match
# a wrapper predicate is never moved.
_KEEP_SUBSTRINGS = ("/approved", "/answer", "/reclarify", "clarification required")
_KEEP_PLAN_RE = re.compile(r"(?im)^\s*implementation\s+plan\b")

_REFUSED_RE = re.compile(
	r"HTTP 40[13]|Resource not accessible by integration|Bad credentials|Requires authentication",
	re.IGNORECASE,
)
_RATE_LIMITED_RE = re.compile(r"rate limit", re.IGNORECASE)

# `gh api` flags that take a value (short and long forms).
_API_VALUE_FLAGS = {
	"-X": "method", "--method": "method",
	"-H": "header", "--header": "header",
	"-f": "raw", "--raw-field": "raw",
	"-F": "typed", "--field": "typed",
	"--input": "input",
	"-q": "jq", "--jq": "jq",
	"-t": "template", "--template": "template",
	"--hostname": "other", "--cache": "other", "-p": "other", "--preview": "other",
}
_API_BOOL_FLAGS = {"-i", "--include", "--paginate", "--slurp", "--silent", "--verbose"}


def log(outcome: str, **fields: object) -> None:
	extra = " ".join(f"{k}={v}" for k, v in fields.items())
	print(f"{LOG_PREFIX} outcome={outcome} {extra}".rstrip(), file=sys.stderr)


# ---------------------------------------------------------------------------
# Signing and verification
# ---------------------------------------------------------------------------

def canonical(repo: str, issue: int, login: str, account_type: str, body: str, uid: int | None = None) -> bytes:
	digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
	return (
		f"ai-comment-sig:{SIG_VERSION}\nrepo={repo.lower()}\nissue={int(issue)}"
		f"\nas={login}\ntype={account_type}\nuid={'' if uid is None else int(uid)}\nbody_sha256={digest}"
	).encode("utf-8")


def signature(secret: str, repo: str, issue: int, login: str, account_type: str, body: str, uid: int | None = None) -> str:
	return hmac.new(secret.encode("utf-8"), canonical(repo, issue, login, account_type, body, uid), hashlib.sha256).hexdigest()


def strip_signature(body: str) -> str:
	"""Remove a trailing signature line, if any (used before re-signing)."""
	match = SIG_LINE_RE.search(body)
	return body[: match.start()] if match else body


def sign_body(secret: str, repo: str, issue: int, login: str, account_type: str, body: str, uid: int | None = None) -> str:
	body = strip_signature(body)
	sig = signature(secret, repo, issue, login, account_type, body, uid)
	uid_field = "" if uid is None else f" uid={int(uid)}"
	return (
		f"{body}\n<!-- ai-comment-sig:{SIG_VERSION} repo={repo.lower()} issue={int(issue)} "
		f"as={login} type={account_type}{uid_field} sig={sig} -->"
	)


def keep_caller_author(body: str) -> bool:
	"""True when the body could start a wrapper or is a plan comment."""
	lowered = body.lower()
	return any(token in lowered for token in _KEEP_SUBSTRINGS) or bool(_KEEP_PLAN_RE.search(body))


def _comment_location(comment: dict) -> tuple[str, int] | None:
	"""Repository and issue a comment object itself says it belongs to."""
	match = _ISSUE_URL_RE.search(str(comment.get("issue_url") or ""))
	if match:
		return match.group("repo"), int(match.group("num"))
	for key in ("url", "html_url"):
		match = _HTML_URL_RE.match(str(comment.get(key) or ""))
		if match:
			return match.group("repo"), int(match.group("num"))
	return None


def verify_body(body: str, secret: str, location: tuple[str, int] | None, comment_id: object = None):
	"""Return (original_body, login, account_type, repo, uid) when the signature holds.

	The signed repository and issue must match the comment's own location
	when the object carries one (REST `issue_url`, GraphQL `url`).
	"""
	match = SIG_LINE_RE.search(body)
	if not match:
		return None
	repo, issue = match.group("repo"), int(match.group("issue"))
	if location is not None and (location[0].lower() != repo.lower() or location[1] != issue):
		log("invalid", comment_id=comment_id, reason="location_mismatch")
		return None
	original = body[: match.start()]
	uid = int(match.group("uid")) if match.group("uid") else None
	expected = signature(secret, repo, issue, match.group("login"), match.group("type"), original, uid)
	if not hmac.compare_digest(expected, match.group("sig")):
		log("invalid", comment_id=comment_id)
		return None
	return original, match.group("login"), match.group("type"), repo, uid


def _association(login: str, repo: str) -> str:
	return "OWNER" if login.lower() == repo.split("/", 1)[0].lower() else "COLLABORATOR"


def normalize_comment(comment: Any, secret: str) -> Any:
	"""Rewrite a validly signed bot comment (REST or GraphQL shape)."""
	if not isinstance(comment, dict) or not isinstance(comment.get("body"), str):
		return comment
	user = comment.get("user")
	author = comment.get("author")
	if isinstance(user, dict) and user.get("login") == BOT_LOGIN:
		verified = verify_body(comment["body"], secret, _comment_location(comment), comment.get("id"))
		if verified is None:
			return comment
		original, login, account_type, repo, uid = verified
		normalized = dict(comment)
		normalized["body"] = original
		normalized["user"] = dict(user, login=login, type=account_type)
		if uid is not None:
			# Readers that trust the PAT by numeric id (`.user.id == $actor`).
			normalized["user"]["id"] = uid
		normalized["author_association"] = _association(login, repo)
		normalized["ai_comment_sig"] = {"poster": BOT_LOGIN}
		return normalized
	if isinstance(author, dict) and author.get("login") in _GRAPHQL_BOT_LOGINS:
		verified = verify_body(comment["body"], secret, _comment_location(comment), comment.get("id") or comment.get("databaseId"))
		if verified is None:
			return comment
		original, login, account_type, repo, uid = verified
		normalized = dict(comment)
		normalized["body"] = original
		normalized_author = dict(author, login=login)
		if "__typename" in normalized_author:
			normalized_author["__typename"] = account_type
		if uid is not None and "databaseId" in normalized_author:
			normalized_author["databaseId"] = uid
		normalized["author"] = normalized_author
		if "authorAssociation" in normalized:
			normalized["authorAssociation"] = _association(login, repo)
		return normalized
	return comment


def normalize_value(value: Any, secret: str) -> Any:
	"""Normalize every comment object anywhere in a REST or GraphQL response."""
	if isinstance(value, list):
		return [normalize_value(item, secret) for item in value]
	if isinstance(value, dict):
		value = normalize_comment(value, secret)
		return {key: normalize_value(item, secret) if isinstance(item, (dict, list)) else item for key, item in value.items()}
	return value


def iter_json_stream(text: str):
	decoder = json.JSONDecoder()
	index = 0
	length = len(text)
	while True:
		while index < length and text[index].isspace():
			index += 1
		if index >= length:
			return
		value, index = decoder.raw_decode(text, index)
		yield value


# ---------------------------------------------------------------------------
# gh command parsing
# ---------------------------------------------------------------------------

class Unsupported(Exception):
	pass


_STDIN_CONSUMED = False


def _read_source(source: str) -> str:
	"""Read `-` (stdin) or a file; remember stdin so the original command is not re-run."""
	global _STDIN_CONSUMED
	if source == "-":
		_STDIN_CONSUMED = True
		return sys.stdin.read()
	with open(source, encoding="utf-8") as handle:
		return handle.read()


def parse_api_args(args: list[str]) -> dict:
	"""Parse `gh api` arguments (without the leading `api`)."""
	parsed: dict[str, Any] = {"endpoint": None, "method": None, "fields": [], "input": None,
		"jq": None, "template": None, "flags": [], "include": False}
	index = 0
	while index < len(args):
		arg = args[index]
		name, value = arg, None
		if arg.startswith("--") and "=" in arg:
			name, value = arg.split("=", 1)
		elif len(arg) > 2 and arg[:2] in _API_VALUE_FLAGS and not arg.startswith("--"):
			name, value = arg[:2], arg[2:]
		kind = _API_VALUE_FLAGS.get(name)
		if kind:
			if value is None:
				index += 1
				if index >= len(args):
					raise Unsupported(f"{name} without a value")
				value = args[index]
			if kind == "method":
				parsed["method"] = value.upper()
			elif kind in ("raw", "typed"):
				parsed["fields"].append((kind, value))
			elif kind == "input":
				parsed["input"] = value
			elif kind == "jq":
				parsed["jq"] = value
			elif kind == "template":
				parsed["template"] = value
			else:
				parsed["flags"] += [name, value]
		elif arg in _API_BOOL_FLAGS:
			if arg in ("-i", "--include"):
				parsed["include"] = True
			parsed["flags"].append(arg)
		elif arg.startswith("-") and arg != "-":
			raise Unsupported(f"unknown flag {arg}")
		elif parsed["endpoint"] is None:
			parsed["endpoint"] = arg
		else:
			raise Unsupported("second positional argument")
		index += 1
	if parsed["endpoint"] is None:
		raise Unsupported("no endpoint")
	if parsed["method"] is None:
		parsed["method"] = "POST" if (parsed["fields"] or parsed["input"]) else "GET"
	return parsed


def endpoint_path(endpoint: str) -> str:
	path = endpoint.split("?", 1)[0]
	if path.startswith("https://api.github.com/"):
		path = path[len("https://api.github.com/"):]
	return path.lstrip("/")


def _typed_value(raw: str) -> Any:
	if raw == "true":
		return True
	if raw == "false":
		return False
	if raw == "null":
		return None
	if re.fullmatch(r"-?[0-9]+", raw):
		return int(raw)
	if raw.startswith("@"):
		return _read_source(raw[1:])
	return raw


def build_payload(parsed: dict) -> dict:
	"""The JSON body gh would send. Raises Unsupported for shapes we do not rebuild."""
	if parsed["input"] is not None:
		if parsed["fields"]:
			raise Unsupported("--input with fields")
		source = parsed["input"]
		text = _read_source(source)
		payload = json.loads(text)
		if not isinstance(payload, dict):
			raise Unsupported("--input is not an object")
		return payload
	payload: dict[str, Any] = {}
	for kind, item in parsed["fields"]:
		if "=" not in item:
			raise Unsupported("field without =")
		key, raw = item.split("=", 1)
		if "[" in key or key in payload:
			raise Unsupported("nested or repeated field")
		payload[key] = raw if kind == "raw" else _typed_value(raw)
	return payload


def parse_comment_cli(args: list[str]) -> dict:
	"""Parse `gh issue|pr comment <n|url> ...` into an api-shaped request."""
	kind, rest = args[0], args[1:]
	target = None
	body = None
	repo = os.environ.get("GH_REPO") or None
	index = 0
	while index < len(rest):
		arg = rest[index]
		if arg in ("-b", "--body", "-F", "--body-file", "-R", "--repo"):
			index += 1
			if index >= len(rest):
				raise Unsupported(f"{arg} without a value")
			value = rest[index]
			if arg in ("-b", "--body"):
				body = value
			elif arg in ("-F", "--body-file"):
				body = _read_source(value)
			else:
				repo = value
		elif arg.startswith(("--body=", "--body-file=", "--repo=")):
			name, value = arg.split("=", 1)
			if name == "--body":
				body = value
			elif name == "--body-file":
				body = _read_source(value)
			else:
				repo = value
		elif arg.startswith("-"):
			raise Unsupported(f"unsupported {kind} comment flag {arg}")
		elif target is None:
			target = arg
		else:
			raise Unsupported("second positional argument")
		index += 1
	if target is None or body is None:
		raise Unsupported("missing target or body")
	url = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/(?:issues|pull)/([0-9]+)/?", target)
	if url:
		repo, number = url.group(1), url.group(2)
	elif re.fullmatch(r"#?[0-9]+", target):
		number = target.lstrip("#")
	else:
		raise Unsupported("unrecognised target")
	if not repo:
		raise Unsupported("repository unknown")
	return {"repo": repo, "issue": int(number), "body": body}


# ---------------------------------------------------------------------------
# Running gh
# ---------------------------------------------------------------------------

def real_gh() -> str:
	path = os.environ.get(REAL_GH_ENV, "")
	if not path or not os.access(path, os.X_OK):
		raise SystemExit(f"{LOG_PREFIX} outcome=error reason=real_gh_unknown")
	return path


def run_gh(args: list[str], env: dict | None = None, stdin_data: str | None = None) -> subprocess.CompletedProcess:
	return subprocess.run(
		[real_gh(), *args], capture_output=True, text=True, check=False,
		env=env if env is not None else os.environ.copy(), input=stdin_data,
	)


def passthrough(args: list[str]) -> int:
	os.execv(real_gh(), [real_gh(), *args])
	return 127  # pragma: no cover


def emit(proc: subprocess.CompletedProcess) -> int:
	sys.stdout.write(proc.stdout)
	sys.stderr.write(proc.stderr)
	return proc.returncode


def _state_dir() -> str:
	return os.environ.get("RUNNER_TEMP") or os.environ.get("TMPDIR") or "/tmp"


def _token_key(token: str) -> str:
	return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def caller_identity() -> tuple[str, str, int] | None:
	"""(login, type, id) of the caller's token, cached per job (one GET /user)."""
	token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
	if not token:
		return None
	cache = os.path.join(_state_dir(), f"ai_comment_sig_identity_{_token_key(token)}.json")
	try:
		with open(cache, encoding="utf-8") as handle:
			cached = json.load(handle)
		if (re.fullmatch(_LOGIN, cached.get("login", "")) and cached.get("type") in ("User", "Bot")
				and isinstance(cached.get("id"), int) and not isinstance(cached.get("id"), bool)):
			return cached["login"], cached["type"], cached["id"]
	except (OSError, ValueError, AttributeError):
		pass
	proc = run_gh(["api", "user", "--jq", "{login: .login, type: .type, id: .id}"])
	if proc.returncode != 0:
		return None
	try:
		identity = json.loads(proc.stdout)
	except ValueError:
		return None
	if not isinstance(identity, dict):
		return None
	login, account_type, uid = identity.get("login", ""), identity.get("type", ""), identity.get("id")
	if login == BOT_LOGIN or not re.fullmatch(_LOGIN, str(login)) or account_type not in ("User", "Bot"):
		return None
	if not isinstance(uid, int) or isinstance(uid, bool):
		return None
	try:
		with open(cache, "w", encoding="utf-8") as handle:
			json.dump({"login": login, "type": account_type, "id": uid}, handle)
	except OSError:
		pass
	return login, account_type, uid


def _refusal_flag() -> str:
	return os.path.join(_state_dir(), f"ai_comment_sig_job_token_refused_{_token_key(os.environ.get(JOB_TOKEN_ENV, ''))}")


def _output_args(parsed: dict) -> list[str]:
	out = list(parsed["flags"])
	if parsed["jq"] is not None:
		out += ["--jq", parsed["jq"]]
	if parsed["template"] is not None:
		out += ["--template", parsed["template"]]
	return out


def send_plain(original: list[str], endpoint: str, method: str, payload: dict, output_args: list[str]) -> int:
	"""Run the request unchanged with the caller's token.

	Re-executes the original command unless its body came from stdin, which
	this process has already read; then the rebuilt payload is sent instead.
	"""
	if not _STDIN_CONSUMED:
		return passthrough(original)
	with tempfile.NamedTemporaryFile("w", suffix=".json", dir=_state_dir(), delete=False, encoding="utf-8") as handle:
		json.dump(payload, handle)
		payload_path = handle.name
	try:
		return emit(run_gh(["api", endpoint, "--method", method, "--input", payload_path, *output_args]))
	finally:
		os.unlink(payload_path)


def post_signed(original: list[str], repo: str, issue: int, payload: dict, output_args: list[str], secret: str) -> int:
	endpoint = f"repos/{repo}/issues/{issue}/comments"

	def plain() -> int:
		return send_plain(original, endpoint, "POST", payload, output_args)

	body = payload.get("body")
	if not isinstance(body, str) or keep_caller_author(body):
		return plain()
	current = os.environ.get("GITHUB_REPOSITORY", "")
	job_token = os.environ.get(JOB_TOKEN_ENV, "")
	if os.environ.get(POST_SWITCH_ENV, "true").strip().lower() == "false":
		return plain()
	if not job_token or repo.lower() != current.lower() or os.path.exists(_refusal_flag()):
		return plain()
	identity = caller_identity()
	if identity is None:
		log("skip", reason="identity_unknown", issue=issue)
		return plain()
	signed = sign_body(secret, repo, issue, identity[0], identity[1], body, identity[2])
	if len(signed) > MAX_BODY_CHARS:
		log("skip", reason="body_too_long", issue=issue)
		return plain()
	with tempfile.NamedTemporaryFile("w", suffix=".json", dir=_state_dir(), delete=False, encoding="utf-8") as handle:
		json.dump(dict(payload, body=signed), handle)
		payload_path = handle.name
	env = os.environ.copy()
	env["GH_TOKEN"] = job_token
	env["GITHUB_TOKEN"] = job_token
	try:
		proc = run_gh(["api", endpoint, "--method", "POST", "--input", payload_path, *_strip_jq(output_args)], env=env)
	finally:
		os.unlink(payload_path)
	if proc.returncode == 0:
		log("posted", issue=issue, token="job")
		sys.stderr.write(proc.stderr)
		jq_filter = None
		if "--jq" in output_args:
			jq_filter = output_args[output_args.index("--jq") + 1]
		head, text = "", proc.stdout
		if "-i" in output_args or "--include" in output_args:
			parts = _split_include(text)
			if parts is not None:
				head, text = parts
		return write_normalized(text, head, jq_filter, secret)
	if _REFUSED_RE.search(proc.stderr or ""):
		if _RATE_LIMITED_RE.search(proc.stderr or ""):
			# A rate-limit 403 created nothing and says nothing about the
			# token's permissions: fall back for this comment only.
			log("fallback", reason="job_token_rate_limited", issue=issue)
			return plain()
		log("fallback", reason="job_token_refused", issue=issue)
		try:
			open(_refusal_flag(), "w").close()
		except OSError:
			pass
		return plain()
	# Ambiguous failure: the comment may exist already. Never resend.
	log("failed", issue=issue, rc=proc.returncode)
	return emit(proc)


def edit_signed(original: list[str], parsed: dict, repo: str, comment_id: str, secret: str) -> int:
	payload = build_payload(parsed)
	endpoint = f"repos/{repo}/issues/comments/{comment_id}"

	def plain() -> int:
		return send_plain(original, endpoint, "PATCH", payload, _output_args(parsed))

	body = payload.get("body")
	if not isinstance(body, str):
		return plain()
	proc = run_gh(["api", endpoint])
	try:
		current = json.loads(proc.stdout) if proc.returncode == 0 else None
	except ValueError:
		current = None
	if not isinstance(current, dict) or (current.get("user") or {}).get("login") != BOT_LOGIN:
		return plain()
	location = _comment_location(current)
	verified = verify_body(str(current.get("body") or ""), secret, location, comment_id) if location else None
	if verified is None:
		# Never sign an edit of a comment that was not validly signed: that
		# would turn an untrusted bot comment into a trusted one.
		return plain()
	# Re-sign for the same issue and account as the original, so an edit
	# cannot change where or whose comment it is.
	_, login, account_type, _, uid = verified
	signed = sign_body(secret, location[0], location[1], login, account_type, body, uid)
	if len(signed) > MAX_BODY_CHARS:
		return plain()
	with tempfile.NamedTemporaryFile("w", suffix=".json", dir=_state_dir(), delete=False, encoding="utf-8") as handle:
		json.dump(dict(payload, body=signed), handle)
		payload_path = handle.name
	try:
		proc = run_gh(["api", endpoint, "--method", "PATCH", "--input", payload_path, *_output_args(parsed)])
	finally:
		os.unlink(payload_path)
	log("edited" if proc.returncode == 0 else "edit_failed", comment_id=comment_id)
	return emit(proc)


def _apply_jq(stream: str, jq_filter: str) -> int:
	"""Run a --jq filter the way gh does: per input value, raw strings, exit 1 on error."""
	filtered = subprocess.run(["jq", "-rcS", jq_filter], input=stream, capture_output=True, text=True, check=False)
	sys.stdout.write(filtered.stdout)
	sys.stderr.write(filtered.stderr)
	return 0 if filtered.returncode == 0 else 1


def write_normalized(text: str, head: str, jq_filter: str | None, secret: str) -> int:
	"""Print a response body normalized, then filtered by --jq when given."""
	try:
		values = [normalize_value(value, secret) for value in iter_json_stream(text)]
	except ValueError:
		# Not JSON: nothing to normalize. gh would hand the same text to jq.
		sys.stdout.write(head)
		if jq_filter is None:
			sys.stdout.write(text)
			return 0
		return _apply_jq(text, jq_filter)
	sys.stdout.write(head)
	if jq_filter is None:
		sys.stdout.write("".join(json.dumps(value, ensure_ascii=False, separators=(",", ":")) for value in values))
		return 0
	return _apply_jq("\n".join(json.dumps(value, ensure_ascii=False) for value in values), jq_filter)


def _split_include(text: str) -> tuple[str, str] | None:
	split = re.search(r"\r?\n\r?\n", text)
	if split is None:
		return None
	return text[: split.end()], text[split.end():]


def read_normalized(args: list[str], parsed: dict, secret: str) -> int:
	if parsed["template"] is not None:
		return passthrough(args)
	proc = run_gh(["api", *_strip_jq(args[1:])])
	if proc.returncode != 0:
		# gh prints the error body and exits non-zero without running --jq.
		return emit(proc)
	text = proc.stdout
	head = ""
	if parsed["include"]:
		parts = _split_include(text)
		if parts is None:
			# Unexpected shape: re-run the original command so --jq still applies.
			return passthrough(args)
		head, text = parts
	sys.stderr.write(proc.stderr)
	return write_normalized(text, head, parsed["jq"], secret)


def _strip_jq(args: list[str]) -> list[str]:
	out = []
	index = 0
	while index < len(args):
		arg = args[index]
		if arg in ("-q", "--jq"):
			index += 2
			continue
		if arg.startswith("--jq=") or (arg.startswith("-q") and len(arg) > 2):
			index += 1
			continue
		out.append(arg)
		index += 1
	return out


def gh_shim(args: list[str]) -> int:
	secret = os.environ.get(SECRET_ENV, "")
	if not secret or not args:
		return passthrough(args)
	try:
		if args[0] in ("issue", "pr") and len(args) > 1 and args[1] == "comment":
			request = parse_comment_cli([args[0], *args[2:]])
			return post_signed(args, request["repo"], request["issue"], {"body": request["body"]}, ["--jq", ".html_url"], secret)
		if args[0] != "api":
			return passthrough(args)
		parsed = parse_api_args(args[1:])
		path = endpoint_path(parsed["endpoint"])
		create = _CREATE_RE.match(path)
		if create and parsed["method"] == "POST":
			if parsed["template"] is not None:
				return passthrough(args)
			payload = build_payload(parsed)
			return post_signed(args, create.group("repo"), int(create.group("num")), payload, _output_args(parsed), secret)
		edit = _EDIT_RE.match(path)
		if edit and parsed["method"] == "PATCH":
			return edit_signed(args, parsed, edit.group("repo"), edit.group("id"), secret)
		if _READ_RE.match(path) and parsed["method"] == "GET":
			return read_normalized(args, parsed, secret)
		if path == "graphql":
			return read_normalized(args, parsed, secret)
	except (Unsupported, OSError, ValueError, UnicodeDecodeError) as exc:
		if _STDIN_CONSUMED:
			# The body was read from stdin, so the original command cannot be
			# re-run with it. Fail loudly rather than post an empty comment.
			log("error", reason=type(exc).__name__, stdin="consumed")
			print(f"gh: comment request could not be parsed ({exc})", file=sys.stderr)
			return 1
		log("passthrough", reason=type(exc).__name__)
	return passthrough(args)


def main(argv: list[str]) -> int:
	if argv and argv[0] == "gh-shim":
		rest = argv[1:]
		if rest and rest[0] == "--":
			rest = rest[1:]
		return gh_shim(rest)
	parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
	sub = parser.add_subparsers(dest="cmd", required=True)
	p_sign = sub.add_parser("sign")
	p_sign.add_argument("--repo", required=True)
	p_sign.add_argument("--issue", required=True, type=int)
	p_sign.add_argument("--as", dest="login", required=True)
	p_sign.add_argument("--type", default="User", choices=("User", "Bot"))
	sub.add_parser("normalize")
	args = parser.parse_args(argv)
	secret = os.environ.get(SECRET_ENV, "")
	if not secret:
		print(f"{SECRET_ENV} is not set", file=sys.stderr)
		return 2
	if args.cmd == "sign":
		sys.stdout.write(sign_body(secret, args.repo, args.issue, args.login, args.type, sys.stdin.read()))
		return 0
	for value in iter_json_stream(sys.stdin.read()):
		print(json.dumps(normalize_value(value, secret), ensure_ascii=False, separators=(",", ":")))
	return 0


if __name__ == "__main__":
	sys.exit(main(sys.argv[1:]))
AI_COMMENT_SIGNER_PY
}
_ai_comment_sig_activate
# Activate-only mode: a script that posts comments but does not otherwise use
# these helpers runs `AI_COMMENT_SIG_ACTIVATE_ONLY=1 source .../gh_helpers.sh`
# to put the shim on PATH without defining the rest of the library (so none of
# its own functions are overridden). A later full source still loads it.
if [ "${AI_COMMENT_SIG_ACTIVATE_ONLY:-}" = "1" ]; then
	unset _GH_HELPERS_LOADED AI_COMMENT_SIG_ACTIVATE_ONLY
	return 0 2>/dev/null || true
fi

# Shared PAT counters are only an estimate of job usage: other jobs may run
# concurrently. /rate_limit does not debit the primary REST allowance.
gh_pat_budget()
{
	local phase="$1" workflow="$2" job="$3" snapshot_file="$4" response payload header_resource header_remaining header_reset remaining reset used="unknown" previous_remaining previous_reset
	case "${phase}" in start|end) ;; *) return 1 ;; esac
	remaining="unknown"
	reset="unknown"
	if response="$(gh api -i rate_limit 2>/dev/null)"; then
		payload="${response}"
		if [[ "${response}" == *$'\r\n\r\n'* ]]; then
			payload="${response##*$'\r\n\r\n'}"
		elif [[ "${response}" == *$'\n\n'* ]]; then
			payload="${response##*$'\n\n'}"
		fi
		read -r remaining reset < <(printf '%s' "${payload}" | jq -r '
			if (.resources.core.remaining | type) == "number" and
			   (.resources.core.reset | type) == "number" and
			   .resources.core.remaining >= 0 and .resources.core.reset > 0 then
				[(.resources.core.remaining | floor), (.resources.core.reset | floor)] | @tsv
			else "unknown\tunknown" end' 2>/dev/null || printf 'unknown\tunknown')
		# Headers describe the authenticated request, not the overview.
		# Override core only when the response identifies the core resource.
		header_resource="$(printf '%s\n' "${response}" | tr -d '\r' | grep -im1 '^x-ratelimit-resource:' | cut -d: -f2 | tr -d '[:space:]' || true)"
		if [ "${header_resource}" = "core" ]; then
			header_remaining="$(printf '%s\n' "${response}" | tr -d '\r' | grep -im1 '^x-ratelimit-remaining:' | cut -d: -f2 | tr -d '[:space:]' || true)"
			header_reset="$(printf '%s\n' "${response}" | tr -d '\r' | grep -im1 '^x-ratelimit-reset:' | cut -d: -f2 | tr -d '[:space:]' || true)"
			if [[ "${header_remaining}" =~ ^[0-9]+$ && "${header_reset}" =~ ^[0-9]+$ ]]; then
				remaining="${header_remaining}"
				reset="${header_reset}"
			fi
		fi
	fi
	if [ "${phase}" = "start" ]; then
		if [ "${remaining}" != "unknown" ] && [ "${reset}" != "unknown" ]; then
			printf '%s %s\n' "${remaining}" "${reset}" > "${snapshot_file}" 2>/dev/null || true
		fi
	elif [ -r "${snapshot_file}" ]; then
		read -r previous_remaining previous_reset < "${snapshot_file}" || true
		if [[ "${previous_remaining:-}" =~ ^[0-9]+$ ]] && [[ "${previous_reset:-}" =~ ^[0-9]+$ ]] &&
		   [[ "${remaining}" =~ ^[0-9]+$ ]] && [ "${reset}" = "${previous_reset}" ] &&
		   [ "${previous_remaining}" -ge "${remaining}" ]; then
			used=$((previous_remaining - remaining))
		fi
	fi
	printf 'GH_PAT_BUDGET phase=%s workflow=%s job=%s remaining=%s reset=%s used_in_job=%s\n' \
		"${phase}" "${workflow}" "${job}" "${remaining}" "${reset}" "${used}"
	return 0
}

# Synchronized job-local PR state for the reviewer and editor watchdogs.
# With no job-local runner directory, retain the legacy live-read behavior.
# A cache entry is never used as merge authorization; merge paths still read
# the current head. A read failure remains an uncached conservative "open".
gh_review_pr_state()
{
	local repo="$1" number="$2" root="${RUNNER_TEMP:-}" cache_file now saved_repo saved_number saved_state expires state
	if [[ ! "${repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || [[ ! "${number}" =~ ^[1-9][0-9]*$ ]]; then
		printf 'open\n'
		return 0
	fi
	if [ -z "${root}" ] || ! command -v flock >/dev/null 2>&1; then
		gh_retry gh api "repos/${repo}/pulls/${number}" --jq .state 2>/dev/null | grep -xE 'open|closed|merged' || echo open
		return 0
	fi
	cache_file="${root}/review_pr_state_${GITHUB_RUN_ID:-local}_${GITHUB_RUN_ATTEMPT:-1}_${repo//\//_}_${number}"
	(
		flock -x 9 || { echo open; exit 0; }
		now="$(date +%s)"
		if [ -r "${cache_file}" ]; then
			read -r saved_repo saved_number saved_state expires < "${cache_file}" || true
			if [ "${saved_repo:-}" = "${repo}" ] && [ "${saved_number:-}" = "${number}" ] &&
			   [[ "${expires:-}" =~ ^[0-9]+$ ]] && [ "${expires}" -gt "${now}" ] &&
			   [ "${saved_state:-}" = "open" ]; then
				printf '%s\n' "${saved_state}"
				exit 0
			fi
		fi
		state="$(gh_retry gh api "repos/${repo}/pulls/${number}" --jq .state 2>/dev/null)" || state=""
		if [[ "${state}" =~ ^(open|closed|merged)$ ]]; then
			if [ "${state}" = "open" ]; then
				printf '%s %s %s %s\n' "${repo}" "${number}" "${state}" "$((now + 120))" > "${cache_file}.tmp" &&
					mv -f "${cache_file}.tmp" "${cache_file}"
			fi
			printf '%s\n' "${state}"
		else
			printf 'open\n'
		fi
	) 9>"${cache_file}.lock"
}

# ---------------------------------------------------------------
# Run-local PR read cache (issue #9928, GH_PAT budget part E).
#
# gh_pr_cached_read <kind> <repo> <number> [<head_sha>]
#   kind: pull | issue_comments | reviews | review_comments | files | check_runs
#   Prints the JSON for the PR resource (an object for pull and check_runs,
#   one flat array for the paginated list kinds) and returns 0; returns 1 with
#   no stdout on invalid input or when the live read fails.
#   One fetch per kind, PR, head SHA and job: entries live under
#   ${RUNNER_TEMP}/gh_pr_read_cache (mode 700) and are keyed
#   <run_id>_<attempt>_<job>_<repo>_<number>_<kind>_<sha|na>. Each entry has a
#   sidecar .meta line "<repo> <number> <kind> <sha|na> <fetched_epoch>" and a
#   .lock file (holding the same identity) that one flock covers check, fetch
#   and write with, so concurrent reviewer subshells make a single call.
#   Freshness: pull, check_runs and every entry with no head SHA (na) expire
#   after 120 s; SHA-keyed list kinds stay valid until evicted. A pull payload
#   whose state is not "open" is never stored (as in gh_review_pr_state), and a
#   pull entry whose head differs from the requested SHA is a miss.
#   Fail-open: with REVIEW_RUN_READ_CACHE_ENABLED=false|0|no|off (any case),
#   no RUNNER_TEMP, no flock, or an unusable cache directory or lock, the read
#   is a plain live call; a cache write failure still prints the live result.
#   Each cached read appends "hit <kind>" or "miss <kind>" to
#   ${RUNNER_TEMP}/gh_pr_read_cache/<run_id>_<attempt>.stats, which the
#   codex-agent end step summarises as GH_PR_READ_CACHE hits=<n> misses=<n>.
#   Never a merge authorisation: merge paths keep their live head read.
#
# gh_pr_cache_evict <repo> <number> [kind…]
#   Removes this run's entries for the PR (all kinds when none are given),
#   under each entry's lock so an in-flight fetch cannot store stale data
#   after the eviction. Always returns 0.
#
# _gh_pr_cache_evict_for_cmd <command…>
#   Called by gh_retry, gh_retry_to_file and gh_api_retry after every attempt
#   of their command, so a PR write made through them (label add/remove,
#   comment post, update-branch, edit, merge) evicts that PR's entries. Returns
#   at once when the cache directory does not exist (every job that never
#   called gh_pr_cached_read). Never writes to stdout.
# ---------------------------------------------------------------
_gh_pr_cache_enabled()
{
	case "$(printf '%s' "${REVIEW_RUN_READ_CACHE_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" in
		false|0|no|off) return 1 ;;
	esac
	return 0
}

_gh_pr_cache_dir()
{
	[ -n "${RUNNER_TEMP:-}" ] || return 1
	printf '%s/gh_pr_read_cache\n' "${RUNNER_TEMP}"
}

_gh_pr_cache_run_prefix()
{
	local run_id="${GITHUB_RUN_ID:-local}" attempt="${GITHUB_RUN_ATTEMPT:-1}"
	[[ "${run_id}" =~ ^[A-Za-z0-9_-]+$ ]] || run_id="local"
	[[ "${attempt}" =~ ^[0-9]+$ ]] || attempt="1"
	printf '%s_%s\n' "${run_id}" "${attempt}"
}

# _gh_pr_cache_fetch <kind> <repo> <number> <sha> <outfile> — live read,
# normalised to compact JSON of the expected type in <outfile>.
_gh_pr_cache_fetch()
{
	local kind="$1" repo="$2" number="$3" sha="$4" outfile="$5" raw rc=0 filter
	raw="$(mktemp "${TMPDIR:-/tmp}/gh_pr_cache_fetch.XXXXXX" 2>/dev/null)" || return 1
	case "${kind}" in
		pull) gh_retry_to_file "${raw}" gh api "repos/${repo}/pulls/${number}" || rc=$? ;;
		issue_comments) gh_retry_to_file "${raw}" gh api --paginate "repos/${repo}/issues/${number}/comments?per_page=100" || rc=$? ;;
		reviews) gh_retry_to_file "${raw}" gh api --paginate "repos/${repo}/pulls/${number}/reviews?per_page=100" || rc=$? ;;
		review_comments) gh_retry_to_file "${raw}" gh api --paginate "repos/${repo}/pulls/${number}/comments?per_page=100" || rc=$? ;;
		files) gh_retry_to_file "${raw}" gh api --paginate "repos/${repo}/pulls/${number}/files?per_page=100" || rc=$? ;;
		check_runs) gh_retry_to_file "${raw}" gh api --paginate "repos/${repo}/commits/${sha}/check-runs?per_page=100" || rc=$? ;;
		*) rc=1 ;;
	esac
	if [ "${rc}" -ne 0 ]; then
		rm -f "${raw}"
		return 1
	fi
	case "${kind}" in
		pull) filter='if length == 1 and (.[0] | type) == "object" then .[0] else error("not one object") end' ;;
		check_runs) filter='if length > 0 and all(.[]; type == "object") then {total_count: (.[0].total_count // 0), check_runs: (map(.check_runs // []) | add // [])} else error("not check-run pages") end' ;;
		*) filter='if all(.[]; type == "array") then (add // []) else error("not array pages") end' ;;
	esac
	jq -c -s "${filter}" "${raw}" > "${outfile}" 2>/dev/null || rc=1
	rm -f "${raw}"
	return "${rc}"
}

# _gh_pr_cache_live <kind> <repo> <number> <sha> — uncached read to stdout.
_gh_pr_cache_live()
{
	local out rc=0
	out="$(mktemp "${TMPDIR:-/tmp}/gh_pr_cache_live.XXXXXX" 2>/dev/null)" || return 1
	if _gh_pr_cache_fetch "$1" "$2" "$3" "$4" "${out}"; then
		cat "${out}" || rc=$?
	else
		rc=1
	fi
	rm -f "${out}"
	return "${rc}"
}

# _gh_pr_cache_entry_fresh <entry> <repo> <number> <kind> <sha|na> <now>
_gh_pr_cache_entry_fresh()
{
	local entry="$1" repo="$2" number="$3" kind="$4" sha_key="$5" now="$6"
	local m_repo="" m_number="" m_kind="" m_sha="" m_epoch="" age type_filter
	[ -r "${entry}" ] && [ -r "${entry}.meta" ] || return 1
	read -r m_repo m_number m_kind m_sha m_epoch < "${entry}.meta" 2>/dev/null || return 1
	[ "${m_repo}" = "${repo}" ] && [ "${m_number}" = "${number}" ] && [ "${m_kind}" = "${kind}" ] && [ "${m_sha}" = "${sha_key}" ] || return 1
	[[ "${m_epoch}" =~ ^[0-9]+$ ]] || return 1
	if [ "${kind}" = "pull" ] || [ "${kind}" = "check_runs" ] || [ "${sha_key}" = "na" ]; then
		age=$(( now - m_epoch ))
		[ "${age}" -ge 0 ] && [ "${age}" -lt 120 ] || return 1
	fi
	case "${kind}" in
		pull) type_filter='type == "object" and .state == "open" and ($sha == "na" or .head.sha == $sha)' ;;
		check_runs) type_filter='type == "object" and (.check_runs | type) == "array"' ;;
		*) type_filter='type == "array"' ;;
	esac
	jq -e --arg sha "${sha_key}" "${type_filter}" "${entry}" >/dev/null 2>&1
}

gh_pr_cached_read()
{
	local kind="${1:-}" repo="${2:-}" number="${3:-}" sha="${4:-}"
	local dir="" job sha_key entry stats
	case "${kind}" in
		pull|issue_comments|reviews|review_comments|files|check_runs) ;;
		*) return 1 ;;
	esac
	if [[ ! "${repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || [[ ! "${number}" =~ ^[1-9][0-9]*$ ]] \
		|| [[ "${repo}" =~ (^|/)\.\.?(/|$) ]]; then
		return 1
	fi
	[[ "${sha}" =~ ^[0-9a-f]{40}$ ]] || sha=""
	if [ "${kind}" = "check_runs" ] && [ -z "${sha}" ]; then
		return 1
	fi
	sha_key="${sha:-na}"
	if ! _gh_pr_cache_enabled || ! command -v flock >/dev/null 2>&1 \
		|| ! dir="$(_gh_pr_cache_dir)" || ! { [ -d "${dir}" ] || mkdir -m 700 "${dir}" 2>/dev/null || [ -d "${dir}" ]; } \
		|| [ ! -d "${dir}" ] || [ ! -w "${dir}" ]; then
		_gh_pr_cache_live "${kind}" "${repo}" "${number}" "${sha}"
		return $?
	fi
	job="$(printf '%s' "${GH_PR_READ_CACHE_JOB:-${GITHUB_JOB:-job}}" | tr -cd 'A-Za-z0-9_-')"
	[ -n "${job}" ] || job="job"
	entry="${dir}/$(_gh_pr_cache_run_prefix)_${job}_${repo//\//_}_${number}_${kind}_${sha_key}.json"
	stats="${dir}/$(_gh_pr_cache_run_prefix).stats"
	# The lock file carries the entry identity so eviction can find entries
	# whose first fetch is still in flight.
	if ! { [ -s "${entry}.lock" ] || printf '%s %s %s %s\n' "${repo}" "${number}" "${kind}" "${sha_key}" >> "${entry}.lock"; } 2>/dev/null; then
		_gh_pr_cache_live "${kind}" "${repo}" "${number}" "${sha}"
		return $?
	fi
	(
		if ! flock -x -w 300 9; then
			_gh_pr_cache_live "${kind}" "${repo}" "${number}" "${sha}"
			exit $?
		fi
		now="$(date +%s)"
		if _gh_pr_cache_entry_fresh "${entry}" "${repo}" "${number}" "${kind}" "${sha_key}" "${now}"; then
			printf 'hit %s\n' "${kind}" >> "${stats}" 2>/dev/null || true
			cat "${entry}"
			exit $?
		fi
		printf 'miss %s\n' "${kind}" >> "${stats}" 2>/dev/null || true
		fetched="$(mktemp "${entry}.tmp.XXXXXX" 2>/dev/null)" || fetched=""
		if [ -z "${fetched}" ]; then
			_gh_pr_cache_live "${kind}" "${repo}" "${number}" "${sha}"
			exit $?
		fi
		if ! _gh_pr_cache_fetch "${kind}" "${repo}" "${number}" "${sha}" "${fetched}"; then
			rm -f "${fetched}"
			exit 1
		fi
		store=1
		if [ "${kind}" = "pull" ] && ! jq -e --arg sha "${sha_key}" '.state == "open" and ($sha == "na" or .head.sha == $sha)' "${fetched}" >/dev/null 2>&1; then
			store=0
		fi
		out="${fetched}"
		if [ "${store}" = "1" ] && mv -f "${fetched}" "${entry}" 2>/dev/null; then
			out="${entry}"
			meta_tmp="$(mktemp "${entry}.meta.tmp.XXXXXX" 2>/dev/null)" || meta_tmp=""
			if [ -z "${meta_tmp}" ] \
				|| ! printf '%s %s %s %s %s\n' "${repo}" "${number}" "${kind}" "${sha_key}" "${now}" > "${meta_tmp}" 2>/dev/null \
				|| ! mv -f "${meta_tmp}" "${entry}.meta" 2>/dev/null; then
				rm -f "${entry}.meta" ${meta_tmp:+"${meta_tmp}"} 2>/dev/null || true
			fi
		fi
		rc=0
		cat "${out}" || rc=$?
		[ "${out}" = "${entry}" ] || rm -f "${out}"
		exit "${rc}"
	) 9>>"${entry}.lock"
}

# _gh_pr_cache_evict_match <repo|""> <number|""> [kind…] — evict this run's
# entries matching the repo and number (empty = any).
_gh_pr_cache_evict_match()
{
	local want_repo="${1:-}" want_number="${2:-}" dir prefix lock entry l_repo l_number l_kind k matched
	shift 2 2>/dev/null || set --
	dir="$(_gh_pr_cache_dir)" || return 0
	[ -d "${dir}" ] || return 0
	prefix="$(_gh_pr_cache_run_prefix)_"
	for lock in "${dir}/${prefix}"*.json.lock; do
		[ -f "${lock}" ] || continue
		l_repo=""; l_number=""; l_kind=""
		read -r l_repo l_number l_kind _ < "${lock}" 2>/dev/null || true
		[ -n "${l_repo}" ] || continue
		[ -z "${want_repo}" ] || [ "${l_repo}" = "${want_repo}" ] || continue
		[ -z "${want_number}" ] || [ "${l_number}" = "${want_number}" ] || continue
		if [ "$#" -gt 0 ]; then
			matched=0
			for k in "$@"; do
				[ "${k}" = "${l_kind}" ] && matched=1
			done
			[ "${matched}" = "1" ] || continue
		fi
		entry="${lock%.lock}"
		if command -v flock >/dev/null 2>&1; then
			# Wait for an in-flight fetch of this entry; remove it regardless
			# after 60 s so a hung read cannot keep a stale entry alive.
			( flock -x -w 60 9 || true; rm -f "${entry}" "${entry}.meta" ) 9>>"${lock}" 2>/dev/null \
				|| rm -f "${entry}" "${entry}.meta" 2>/dev/null || true
		else
			rm -f "${entry}" "${entry}.meta" 2>/dev/null || true
		fi
	done
	return 0
}

gh_pr_cache_evict()
{
	local repo="${1:-}" number="${2:-}"
	if [[ ! "${repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || [[ ! "${number}" =~ ^[1-9][0-9]*$ ]]; then
		return 0
	fi
	shift 2
	_gh_pr_cache_evict_match "${repo}" "${number}" "$@" || true
	return 0
}

_gh_pr_cache_evict_for_cmd()
{
	[ -n "${RUNNER_TEMP:-}" ] && [ -d "${RUNNER_TEMP}/gh_pr_read_cache" ] || return 0
	local -a gpce_args=()
	local gpce_a gpce_next="" gpce_method="" gpce_fields=0 gpce_ep gpce_repo="" gpce_num
	local -a gpce_nums=()
	local gpce_repo_re='^repos/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(issues|pulls)/([1-9][0-9]*)(/|$)'
	local gpce_url_re='^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(pull|issues)/([1-9][0-9]*)'
	case "${1:-} ${2:-}" in
		"_safe_gh_jq "*) gpce_args=("${@:2}") ;;
		"gh api") gpce_args=("${@:3}") ;;
		"gh pr"|"gh issue")
			case "${3:-}" in
				edit|comment|close|reopen|merge|ready|review|lock|unlock) ;;
				*) return 0 ;;
			esac
			# Over-eviction only costs a re-read, so every numeric or PR-URL
			# positional counts.
			for gpce_a in "${@:4}"; do
				if [ "${gpce_next}" = "repo" ]; then
					gpce_repo="${gpce_a}"
					gpce_next=""
					continue
				fi
				case "${gpce_a}" in
					-R|--repo) gpce_next="repo" ;;
					--repo=*) gpce_repo="${gpce_a#--repo=}" ;;
					-*) ;;
					*)
						if [[ "${gpce_a}" =~ ^#?([1-9][0-9]*)$ ]]; then
							gpce_nums+=("${BASH_REMATCH[1]}")
						elif [[ "${gpce_a}" =~ ${gpce_url_re} ]]; then
							_gh_pr_cache_evict_match "${BASH_REMATCH[1]}" "${BASH_REMATCH[3]}" || true
						fi
						;;
				esac
			done
			[[ "${gpce_repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || gpce_repo=""
			for gpce_num in ${gpce_nums[@]+"${gpce_nums[@]}"}; do
				_gh_pr_cache_evict_match "${gpce_repo}" "${gpce_num}" || true
			done
			return 0
			;;
		*) return 0 ;;
	esac
	for gpce_a in ${gpce_args[@]+"${gpce_args[@]}"}; do
		case "${gpce_next}" in
			method) gpce_method="${gpce_a}"; gpce_next=""; continue ;;
			value) gpce_next=""; continue ;;
		esac
		case "${gpce_a}" in
			-X|--method) gpce_next="method" ;;
			--method=*) gpce_method="${gpce_a#--method=}" ;;
			-X*) gpce_method="${gpce_a#-X}" ;;
			-f|-F|--field|--raw-field|--input) gpce_fields=1; gpce_next="value" ;;
			--field=*|--raw-field=*|--input=*|-f?*|-F?*) gpce_fields=1 ;;
			*) if _gh_api_flag_takes_value "${gpce_a}"; then gpce_next="value"; fi ;;
		esac
	done
	gpce_method="$(printf '%s' "${gpce_method}" | tr '[:lower:]' '[:upper:]')"
	if [ "${gpce_method}" = "GET" ] || { [ -z "${gpce_method}" ] && [ "${gpce_fields}" != "1" ]; }; then
		return 0
	fi
	gpce_ep="$(_gh_api_endpoint ${gpce_args[@]+"${gpce_args[@]}"})"
	gpce_ep="${gpce_ep#/}"
	gpce_ep="${gpce_ep%%\?*}"
	if [ "${gpce_ep}" = "graphql" ]; then
		if _gh_api_args_unsafe_post ${gpce_args[@]+"${gpce_args[@]}"}; then
			_gh_pr_cache_evict_match "" "" || true
		fi
	elif [[ "${gpce_ep}" =~ ${gpce_repo_re} ]]; then
		_gh_pr_cache_evict_match "${BASH_REMATCH[1]}" "${BASH_REMATCH[3]}" || true
	elif [[ "${gpce_ep}" =~ ^repos/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(issues|pulls)/comments/ ]]; then
		_gh_pr_cache_evict_match "${BASH_REMATCH[1]}" "" issue_comments review_comments || true
	elif [[ "${gpce_ep}" =~ ^repos/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(check-runs|statuses/) ]]; then
		_gh_pr_cache_evict_match "${BASH_REMATCH[1]}" "" check_runs || true
	fi
	return 0
}

_GH_HELPERS_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "scripts")"
if [ -f "${_GH_HELPERS_SCRIPT_DIR}/emit_event.sh" ]; then
	# shellcheck disable=SC1091
	source "${_GH_HELPERS_SCRIPT_DIR}/emit_event.sh"
elif [ -f "scripts/emit_event.sh" ]; then
	# shellcheck disable=SC1091
	source scripts/emit_event.sh
fi
unset _GH_HELPERS_SCRIPT_DIR
if ! type emit_event >/dev/null 2>&1; then
	emit_event()
	{
		return 0
	}
fi

# ---------------------------------------------------------------
# _is_gh_rate_limit — detect rate-limit text in stderr / body.
# Returns 0 (true) if the text indicates a rate limit.
# ---------------------------------------------------------------
_is_gh_rate_limit()
{
	printf '%s' "$1" | grep -qiE 'rate limit|abuse detection|secondary rate|HTTP 429'
}

# ---------------------------------------------------------------
# _gh_actions_escape — escape a string for safe interpolation into
# a GitHub Actions workflow command (e.g. ::warning::, ::error::).
#
# Per the workflow-command format, '%', CR, and LF must be encoded
# so they cannot terminate the command line, break annotations, or
# enable workflow-command injection from untrusted stderr.
#
#   %  → %25     (must be first to avoid double-encoding)
#   \r → %0D
#   \n → %0A
# ---------------------------------------------------------------
_gh_actions_escape()
{
	local s="$1"
	s="${s//%/%25}"
	s="${s//$'\r'/%0D}"
	s="${s//$'\n'/%0A}"
	printf '%s' "${s}"
}

# ---------------------------------------------------------------
# _gh_final_stderr_excerpt — print a bounded excerpt of a stderr
# capture file for every `::warning::  stderr: ...` line of
# gh_retry, gh_retry_to_file and gh_api_json_to_file (the
# per-attempt warnings and the final-failure line) and for the
# permanent and transient give-up paths of gh_api_retry (SEC-001,
# follow-up to issue #8095).
#
# The final-failure paths used to `cat` the raw stderr after their
# `failed after N attempts` annotation: unbounded, and a line
# starting with `::` was read as a workflow command. Callers wrap
# this helper's output in _gh_actions_escape and print it as a
# single `::warning::  stderr: ...` line, so the cap below applies
# to every caller, not only the final-failure lines.
#
#   * at most the first 2048 bytes are printed;
#   * NUL bytes are stripped so the caller's $(...) does not make
#     bash print an unescaped "ignored null byte" warning;
#   * the fixed suffix " [truncated]" is appended when cut.
#
# Logging only: always returns 0 (fail-open). The cap is a local
# literal on purpose, not an env var or global.
# ---------------------------------------------------------------
_gh_final_stderr_excerpt()
{
	local _gfse_file="$1"
	local _gfse_cap=2048
	local _gfse_bytes
	_gfse_bytes="$({ wc -c < "${_gfse_file}"; } 2>/dev/null | tr -d '[:space:]' || true)"
	head -c "${_gfse_cap}" "${_gfse_file}" 2>/dev/null | tr -d '\000' || true
	if [[ "${_gfse_bytes}" =~ ^[0-9]+$ ]] && [ "${_gfse_bytes}" -gt "${_gfse_cap}" ]; then
		printf '%s' ' [truncated]'
	fi
	return 0
}

# ---------------------------------------------------------------
# _is_gh_permanent_failure — detect deterministic, non-retryable
# failures that no amount of retrying will fix.
#
# Matches:
#   * label-edit idempotent cases:   'X' not found
#     (gh prints this when --remove-label targets a label that is
#     not on the issue; retrying just burns API budget.)
#   * HTTP 404 Not Found             (resource doesn't exist)
#   * HTTP 400 Bad Request           (malformed request)
#   * HTTP 401 / "Bad credentials" / "Requires authentication"
#                                    (invalid or missing token)
#   * HTTP 422 Unprocessable Entity  (validation / already-exists)
#   * "Resource not accessible by integration"      (GITHUB_TOKEN scope)
#   * "Resource not accessible by personal access token" (PAT scope)
#   * "Commenting is disabled on issues with more than 2500 comments"
#     (GitHub's per-issue comment cap; it stays until comments are deleted)
#
# Generic HTTP 403 is deliberately NOT matched: gh_retry and
# gh_retry_to_file check permanent failures before rate limits, and
# secondary rate limits / content-creation throttles also answer 403.
#
# Returns 0 (true) if the text indicates such a failure.
# Conservative on purpose — only matches conditions that retrying
# cannot resolve.
# ---------------------------------------------------------------
_is_gh_permanent_failure()
{
	printf '%s' "$1" | grep -qiE "['\"][^'\"]+['\"] not found|gh: Not Found|HTTP 404|404 Not Found|status code 404|HTTP 400|HTTP 401|HTTP 422|Bad credentials|Requires authentication|Resource not accessible by (integration|personal access token)|Commenting is disabled on issues with more than [0-9]+ comments"
}

# ---------------------------------------------------------------
# _sleep_until_reset — compute wait from an epoch timestamp.
#
# Caps at 600 s, floors at 1 s, falls back to 30 s if the
# timestamp is empty or unparseable.
# ---------------------------------------------------------------
_sleep_until_reset()
{
	local reset_epoch="$1"
	local wait_secs

	if [ -n "${reset_epoch}" ] && [ "${reset_epoch}" -gt 0 ] 2>/dev/null; then
		wait_secs=$(( reset_epoch - $(date +%s) + 1 ))
		[ "${wait_secs}" -lt 1 ] && wait_secs=1
		[ "${wait_secs}" -gt 600 ] && wait_secs=600
	else
		wait_secs=30
	fi

	echo "::warning::  Rate limit resets in ${wait_secs}s (computed reset epoch: ${reset_epoch:-unknown})" >&2
	sleep "${wait_secs}"
}

# ---------------------------------------------------------------
# _parse_reset_header — derive the rate-limit reset epoch from a
# header dump file (produced by curl -D).
#
# Precedence:
#   1. Retry-After (integer seconds) → now + seconds. GitHub sends
#      this on secondary rate limits (abuse detection); it is the
#      authoritative wait and is usually well under a minute.
#   2. X-RateLimit-Reset (epoch) → primary window reset.
#
# Without step 1 a secondary-limit 403 was timed against the
# primary window, which can be up to an hour away, so the caller
# slept the full 600 s cap in _sleep_until_reset instead of the
# few seconds GitHub asked for. A non-numeric Retry-After (the
# HTTP-date form) is ignored and falls through to step 2.
#
# Prints the epoch timestamp to stdout; empty string on failure.
# Always returns 0: a missing header or header file is the empty
# string, never a non-zero status, so the function is safe under
# `set -euo pipefail` even outside an `if` / `||` context.
# ---------------------------------------------------------------
_parse_reset_header()
{
	local header_file="$1"
	local _retry_after_secs
	_retry_after_secs=$(grep -i '^retry-after:' "${header_file}" 2>/dev/null \
		| head -1 | sed 's/^[^:]*:[[:space:]]*//; s/[[:space:]]*$//' | tr -d '\r' || true)
	case "${_retry_after_secs}" in
		''|*[!0-9]*) : ;;
		*)
			echo "::warning::  Retry-After: ${_retry_after_secs}s present (secondary rate limit); honouring it over X-RateLimit-Reset" >&2
			# 10# forces decimal: a zero-padded value such as "08" would
			# otherwise be parsed as octal and abort the arithmetic.
			echo $(( $(date +%s) + 10#${_retry_after_secs} ))
			return 0
			;;
	esac
	grep -i '^x-ratelimit-reset:' "${header_file}" 2>/dev/null \
		| head -1 | sed 's/^[^:]*:[[:space:]]*//; s/[[:space:]]*$//' | tr -d '\r' || true
	return 0
}

# ---------------------------------------------------------------
# _gh_rate_limit_wait — query GitHub's /rate_limit endpoint via
# the gh CLI and sleep until the reset window passes.
#
# Falls back to 30 s if the header cannot be parsed.
# ---------------------------------------------------------------
_gh_rate_limit_wait()
{
	# [bucket] (core | graphql | search …, default core): wait for the reset
	# of the bucket that was limited, read from the /rate_limit body. The
	# response header describes only the core bucket, so it is the fallback.
	local _bucket="${1:-core}" _resp _reset_ts=""
	[[ "${_bucket}" =~ ^[a-z_]+$ ]] || _bucket=core
	_resp=$(gh api -i /rate_limit 2>/dev/null) || _resp=""
	if [ -n "${_resp}" ]; then
		_reset_ts=$(printf '%s\n' "${_resp}" | tr -d '\r' | awk 'f { print } /^$/ { f = 1 }' \
			| jq -r --arg b "${_bucket}" '.resources[$b].reset // empty' 2>/dev/null | head -1) || _reset_ts=""
		if [[ "${_reset_ts}" =~ ^[0-9]+$ ]]; then
			# Exact reset of the limited bucket for gh_rate_limit_breaker_active.
			_gh_rate_limit_trip_breaker "${_bucket}" "${_reset_ts}"
		else
			_reset_ts=""
		fi
		if [ -z "${_reset_ts}" ]; then
			_reset_ts=$(printf '%s\n' "${_resp}" \
				| grep -i '^x-ratelimit-reset:' | head -1 \
				| awk '{print $2}' | tr -d '\r') || true
		fi
	fi
	_sleep_until_reset "${_reset_ts}"
}

# _gh_rate_limit_wait_for <stderr text> <command…> — a secondary limit waits
# 60 s (gh's stderr carries no retry-after); a primary limit waits for the
# reset of the command's bucket.
_gh_rate_limit_wait_for()
{
	local _text="$1"; shift
	if printf '%s' "${_text}" | grep -qiE 'secondary rate|abuse detection'; then
		echo "::warning::  Secondary rate limit; waiting 60s" >&2
		sleep 60
	else
		_gh_rate_limit_wait "$(_gh_cmd_bucket "$@")"
	fi
}

# ---------------------------------------------------------------
# Circuit breaker: rate-limit flag file.
#
# When any gh_retry / curl_gh_api helper detects a GitHub API
# rate limit, it touches this file.  Callers within the same job
# can check gh_rate_limit_breaker_tripped to short-circuit further
# API calls and let the next scheduled workflow run handle them
# instead.  (orchestrate_poll.yml previously consumed this flag to
# suppress an end-of-run self-retrigger dispatch; that path has
# been removed — the flag is still written for other scripts that
# want per-job back-pressure.)
#
# The file path defaults to /tmp/.gh_rate_limit_circuit_breaker
# and can be overridden via GH_RATE_LIMIT_BREAKER_FILE env var.
# ---------------------------------------------------------------
_GH_RATE_LIMIT_BREAKER_FILE="${GH_RATE_LIMIT_BREAKER_FILE:-/tmp/.gh_rate_limit_circuit_breaker}"

_gh_rate_limit_trip_breaker()
{
	# [bucket] [reset_epoch] [low]: with arguments, also append a
	# "<bucket> <reset_epoch> [low]" line read by gh_rate_limit_breaker_active.
	# The bare file still means "tripped" for gh_rate_limit_breaker_tripped.
	touch "${_GH_RATE_LIMIT_BREAKER_FILE}" 2>/dev/null || true
	if [[ "${1:-}" =~ ^[a-z_]+$ ]] && [[ "${2:-}" =~ ^[0-9]+$ ]]; then
		printf '%s %s%s\n' "$1" "$2" "${3:+ $3}" >> "${_GH_RATE_LIMIT_BREAKER_FILE}" 2>/dev/null || true
	fi
}

# gh_rate_limit_breaker_tripped — returns 0 (true) if a rate
# limit was encountered at any point during this run.
gh_rate_limit_breaker_tripped()
{
	[ -f "${_GH_RATE_LIMIT_BREAKER_FILE}" ]
}

# ---------------------------------------------------------------
# _gh_ratelimit_tg_alert — Telegram admin alert when a GitHub
# API rate limit is hit in any workflow or script.
#
# Throttled to at most one message per
# TG_GH_RATELIMIT_ALERT_COOLDOWN_SECS (default 3600 s = 1 h).
#
# State persistence: a Telegram pinned message in the admin chat
# carries an embedded marker `<!-- gh_rl_ts:EPOCH -->`.  The
# function reads the pinned message via `getChat`, suppresses the
# alert if the marker is within the cooldown window, otherwise
# sends a new message and re-pins it (unpinning the stale pin
# best-effort).  This deliberately avoids any GitHub API call so
# the throttle still works while the GitHub API itself is the
# resource being limited.
#
# Fail-closed semantics: on any read/pin error the function
# returns without sending — if pinning fails AFTER the message is
# sent, the sent message is deleted so the invariant
# "≤ 1 alert per cooldown window" is preserved even under
# transient Telegram failures.
#
# Alert level: DEBUG. gh_retry already sleeps until the reset and
# retries, so the pipeline handles a rate-limit hit on its own and no
# human action is needed. The function honours `ALERT_MSG_LEVEL` (the
# same global threshold `tg_helpers.sh::tg_send_msg` uses); any
# threshold above DEBUG, including the default WARNING, skips it.
#
# Env (all optional, function no-ops when creds are missing):
#   TG_BOT_SECRET, TG_ADMIN_CHAT_ID (fallback TG_CHAT_ID)
#   TG_GH_RATELIMIT_ALERT_COOLDOWN_SECS (default 3600)
#   ALERT_MSG_LEVEL (default WARNING; suppresses when > DEBUG)
#   GITHUB_WORKFLOW, GITHUB_SERVER_URL, GITHUB_REPOSITORY,
#   GITHUB_RUN_ID — used to build a descriptive message.
#
# Emits: best-effort ::warning:: log lines on persistence failure.
# Returns: always 0 — never block a rate-limit retry path.
# ---------------------------------------------------------------
_gh_ratelimit_tg_alert()
{
	# Resolve chat id; no-op without creds.
	local _chat_id="${TG_ADMIN_CHAT_ID:-${TG_CHAT_ID:-}}"
	if [ -z "${TG_BOT_SECRET:-}" ] || [ -z "${_chat_id}" ]; then
		return 0
	fi

	# jq is required for the Telegram state dance; skip silently
	# if not installed (all repo runners already have it).
	if ! command -v jq >/dev/null 2>&1; then
		return 0
	fi

	# Honour the global ALERT_MSG_LEVEL threshold the same way
	# tg_helpers.sh::tg_send_msg does. This alert is DEBUG level,
	# so any threshold above DEBUG (WARNING, the default, or ERROR,
	# CRITICAL, SILENT) suppresses the rate-limit alert (no send, no
	# pin update, cooldown window is not advanced).
	# SILENT (added with the test-and-mark-stable smoke-gate change)
	# must be in the suppress list explicitly; the permissive default
	# below would otherwise let SILENT through and the gate would
	# still see rate-limit pings.
	case "$(printf '%s' "${ALERT_MSG_LEVEL:-WARNING}" | tr '[:lower:]' '[:upper:]')" in
		DEBUG) : ;;
		WARNING|ERROR|CRITICAL|SILENT) return 0 ;;
		*) : ;;  # unknown value → match tg_helpers.sh permissive default
	esac

	# Cooldown window (seconds). Reject non-numeric values, fall
	# back to 3600.
	local _cooldown="${TG_GH_RATELIMIT_ALERT_COOLDOWN_SECS:-3600}"
	case "${_cooldown}" in
		''|*[!0-9]*) _cooldown=3600 ;;
	esac

	local _tg_base="https://api.telegram.org/bot${TG_BOT_SECRET}"

	# --- Read current pinned message state (fail-closed on error) ---
	local _get_chat_resp
	_get_chat_resp=$(curl -sS --max-time 10 -X POST \
		"${_tg_base}/getChat" \
		-d "chat_id=${_chat_id}" 2>/dev/null) || return 0
	[ -n "${_get_chat_resp}" ] || return 0
	if ! printf '%s' "${_get_chat_resp}" | jq -e '.ok == true' >/dev/null 2>&1; then
		return 0
	fi

	local _pinned_text _prev_pin_id _last_epoch _now
	_pinned_text=$(printf '%s' "${_get_chat_resp}" \
		| jq -r '.result.pinned_message.text // ""' 2>/dev/null) || return 0
	_prev_pin_id=$(printf '%s' "${_get_chat_resp}" \
		| jq -r '.result.pinned_message.message_id // ""' 2>/dev/null) || return 0

	_last_epoch=$(printf '%s' "${_pinned_text}" \
		| sed -n 's/.*<!-- gh_rl_ts:\([0-9]\{1,\}\) -->.*/\1/p' | tail -1)

	_now=$(date +%s)
	if [ -n "${_last_epoch}" ] && [ "${_last_epoch}" -gt 0 ] 2>/dev/null; then
		local _age=$(( _now - _last_epoch ))
		if [ "${_age}" -ge 0 ] && [ "${_age}" -lt "${_cooldown}" ]; then
			# Within cooldown — suppress.
			return 0
		fi
	fi

	# --- Best-effort de-race across concurrent workflow runs ---
	#
	# The getChat→send→pin sequence is not atomic: two runners can
	# both read an old marker, both decide the cooldown has expired,
	# and both go on to send+pin alerts, producing duplicate pings.
	# Add a short randomized jitter (1–3 s) and then re-read the
	# pinned message before sending. If a concurrent runner already
	# published a fresh alert in the gap, `_recheck_epoch` will be
	# within the current cooldown window and we suppress. This does
	# not eliminate the race (Telegram has no client-side locks),
	# but shrinks the window by orders of magnitude. The jitter cost
	# is negligible inside a rate-limit branch that is already about
	# to sleep up to 600 s waiting for the GH reset window.
	local _jitter_secs
	_jitter_secs=$(( (RANDOM % 3) + 1 ))
	sleep "${_jitter_secs}"

	local _recheck_resp _recheck_text _recheck_epoch
	_recheck_resp=$(curl -sS --max-time 10 -X POST \
		"${_tg_base}/getChat" \
		-d "chat_id=${_chat_id}" 2>/dev/null) || return 0
	[ -n "${_recheck_resp}" ] || return 0
	if ! printf '%s' "${_recheck_resp}" | jq -e '.ok == true' >/dev/null 2>&1; then
		return 0
	fi
	_recheck_text=$(printf '%s' "${_recheck_resp}" \
		| jq -r '.result.pinned_message.text // ""' 2>/dev/null) || return 0
	_recheck_epoch=$(printf '%s' "${_recheck_text}" \
		| sed -n 's/.*<!-- gh_rl_ts:\([0-9]\{1,\}\) -->.*/\1/p' | tail -1)
	_now=$(date +%s)
	if [ -n "${_recheck_epoch}" ] && [ "${_recheck_epoch}" -gt 0 ] 2>/dev/null; then
		local _recheck_age=$(( _now - _recheck_epoch ))
		if [ "${_recheck_age}" -ge 0 ] && [ "${_recheck_age}" -lt "${_cooldown}" ]; then
			# Another concurrent run already published a fresh
			# pinned alert during the jitter window — suppress.
			return 0
		fi
	fi
	# Refresh _prev_pin_id from the rechecked state so the later
	# unpin guard still targets the right message id if it changed.
	_prev_pin_id=$(printf '%s' "${_recheck_resp}" \
		| jq -r '.result.pinned_message.message_id // ""' 2>/dev/null) || _prev_pin_id=""
	_last_epoch="${_recheck_epoch}"

	# --- Build message body ---
	local _workflow="${GITHUB_WORKFLOW:-unknown}"
	local _repo="${GITHUB_REPOSITORY:-unknown}"
	local _run_url=""
	if [ -n "${GITHUB_SERVER_URL:-}" ] && [ -n "${GITHUB_REPOSITORY:-}" ] && [ -n "${GITHUB_RUN_ID:-}" ]; then
		_run_url="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
	fi
	local _body
	_body="🔍 DEBUG: GitHub API rate limit hit — workflow=${_workflow} repo=${_repo}"
	if [ -n "${_run_url}" ]; then
		_body="${_body} run=${_run_url}"
	fi
	# Embed dedup marker on a new line (sticky inside pinned text).
	_body="${_body}
<!-- gh_rl_ts:${_now} -->"

	# --- Send the alert message ---
	local _send_resp _new_msg_id
	_send_resp=$(curl -sS --max-time 10 -X POST \
		"${_tg_base}/sendMessage" \
		-d "chat_id=${_chat_id}" \
		-d "disable_web_page_preview=true" \
		--data-urlencode "text=${_body}" 2>/dev/null) || return 0
	[ -n "${_send_resp}" ] || return 0
	if ! printf '%s' "${_send_resp}" | jq -e '.ok == true' >/dev/null 2>&1; then
		return 0
	fi
	_new_msg_id=$(printf '%s' "${_send_resp}" \
		| jq -r '.result.message_id // ""' 2>/dev/null)
	[ -n "${_new_msg_id}" ] || return 0

	# --- Pin new message (persists state). Fail-closed: on pin
	# failure, delete the message we just sent so the cooldown
	# invariant is preserved. ---
	local _pin_resp _pin_ok=0
	_pin_resp=$(curl -sS --max-time 10 -X POST \
		"${_tg_base}/pinChatMessage" \
		-d "chat_id=${_chat_id}" \
		-d "message_id=${_new_msg_id}" \
		-d "disable_notification=true" 2>/dev/null) || _pin_resp=""

	if [ -n "${_pin_resp}" ] && printf '%s' "${_pin_resp}" | jq -e '.ok == true' >/dev/null 2>&1; then
		_pin_ok=1
	fi

	if [ "${_pin_ok}" -ne 1 ]; then
		echo "::warning::_gh_ratelimit_tg_alert: failed to pin new alert message (fail-closed); rolling back sent message" >&2
		curl -sS --max-time 10 -X POST \
			"${_tg_base}/deleteMessage" \
			-d "chat_id=${_chat_id}" \
			-d "message_id=${_new_msg_id}" >/dev/null 2>&1 || true
		return 0
	fi

	# --- Best-effort unpin the previous stale pin so the admin
	# chat keeps a single sticky rate-limit alert.
	#
	# IMPORTANT: only unpin when the previous pinned message was
	# itself one of OUR rate-limit alerts. `_last_epoch` is
	# extracted from the `<!-- gh_rl_ts:EPOCH -->` marker in the
	# previous pin's text, so a non-empty value proves the previous
	# pin carried the marker. If an operator has pinned an
	# unrelated important message (ops notice, runbook, etc.),
	# leave it alone — the new rate-limit pin will still be the
	# most-recent pin returned by `getChat` for cooldown reads,
	# which is all we need. ---
	if [ -n "${_last_epoch}" ] && [ "${_last_epoch}" -gt 0 ] 2>/dev/null \
		&& [ -n "${_prev_pin_id}" ] \
		&& [ "${_prev_pin_id}" != "${_new_msg_id}" ]; then
		curl -sS --max-time 10 -X POST \
			"${_tg_base}/unpinChatMessage" \
			-d "chat_id=${_chat_id}" \
			-d "message_id=${_prev_pin_id}" >/dev/null 2>&1 || true
	fi

	return 0
}

# ---------------------------------------------------------------
# gh_api_retry — rate-limit-aware `gh api` wrapper (issue #5873 / #6634).
#
# Usage:
#   gh_api_retry [--idempotent] [--optional] <gh api args…>
#
# Each attempt runs `gh api -i "$@"` (without -i for --paginate, whose
# headers would interleave with page bodies), splits the status line and
# headers from the body, and writes ONLY the successful attempt's body
# (the --jq output when --jq is given) to stdout. A failed attempt's
# body never reaches stdout.
#
# Failure classes (_gh_api_classify; scripts/gh_api_retry.py is the
# Python twin and shares tests/fixtures/gh_api_retry/classify_cases.json):
#   primary    403/429 with x-ratelimit-remaining: 0 → wait until
#              x-ratelimit-reset of the bucket named by x-ratelimit-resource
#              (else the endpoint's bucket: core / graphql / search)
#   secondary  numeric retry-after, a "secondary rate limit" / "abuse
#              detection" message, or a bare 429 → wait retry-after (60 s
#              when absent)
#   transient  5xx, 408, or no status (network error) → exponential backoff
#              (base 2 s, 0-2 s jitter, capped at GH_RETRY_BACKOFF_CAP_SECS)
#   permanent  every other 4xx, a non-limit 403 → no retry
#
# Never sleeps after the last attempt. A rate-limit wait longer than
# GH_RETRY_RATE_LIMIT_MAX_WAIT_SECS (default 600) gives up at once.
# A POST create (see _gh_api_args_unsafe_post) makes ONE attempt unless
# --idempotent (or GH_RETRY_IDEMPOTENT=true) marks it safe to repeat.
# --optional returns 75 without calling GitHub while the breaker for the
# endpoint's bucket is active (a recorded limit or low remaining budget).
#
# Return codes:
#   0  success
#   1  transient failure, attempts exhausted
#   2  permanent failure                       (GH_API_RETRY_RC_PERMANENT)
#   75 rate-limited and gave up / optional call skipped
#                                              (GH_API_RETRY_RC_RATE_LIMITED)
# When not called inside $(...), GH_API_RETRY_LAST_STATUS and
# GH_API_RETRY_LAST_KIND describe the last attempt.
#
# Log line (stderr): GH_API_RETRY outcome=<retry|gave_up|permanent|skipped>
#   kind=<…> bucket=<…> status=<NNN|none> attempt=<i>/<n> wait_secs=<s>
#   endpoint=<escaped>. Response headers, bodies and tokens are never logged.
#
# API budget (CLAUDE.md §15): no new calls in steady state; a retry repeats
# only a failed call, and a --paginate rate limit reads /rate_limit, which
# does not count against the primary allowance.
# ---------------------------------------------------------------
GH_API_RETRY_RC_PERMANENT=2
GH_API_RETRY_RC_RATE_LIMITED=75

# Flags of `gh api` that consume the following argument as their value.
_gh_api_flag_takes_value()
{
	case "$1" in
		-X|--method|-f|-F|--field|--raw-field|--input|-H|--header|-q|--jq|-t|--template|--hostname|-p|--preview|--cache) return 0 ;;
	esac
	return 1
}

# _gh_api_endpoint <gh api args…> — first positional argument.
_gh_api_endpoint()
{
	local _a _skip=0
	for _a in "$@"; do
		if [ "${_skip}" = "1" ]; then _skip=0; continue; fi
		if _gh_api_flag_takes_value "${_a}"; then _skip=1; continue; fi
		case "${_a}" in
			-*) continue ;;
		esac
		printf '%s\n' "${_a}"
		return 0
	done
	return 0
}

# _gh_api_bucket <gh api args…> — rate-limit resource of the endpoint.
_gh_api_bucket()
{
	local _ep
	_ep="$(_gh_api_endpoint "$@")"
	_ep="${_ep#/}"
	case "${_ep}" in
		graphql) printf 'graphql\n' ;;
		search/*) printf 'search\n' ;;
		*) printf 'core\n' ;;
	esac
}

# _gh_cmd_bucket <full command…> — bucket of a gh_retry-style command.
_gh_cmd_bucket()
{
	if [ "${1:-}" = "gh" ] && [ "${2:-}" = "api" ]; then
		_gh_api_bucket "${@:3}"
	elif [ "${1:-}" = "_safe_gh_jq" ]; then
		_gh_api_bucket "${@:2}"
	else
		printf 'core\n'
	fi
}

# _gh_api_args_unsafe_post <gh api args…>
# True (0) when the call is a non-idempotent POST create:
#   * explicit -X POST / -XPOST / --method POST / --method=POST (any case);
#   * no method and any -f/-F/--field/--raw-field/--input (gh infers POST).
# An explicit GET/PUT/PATCH/DELETE is safe. The graphql endpoint is a read
# (retryable) unless its document starts with "mutation": an inline query=,
# a query=@file, or the .query of an --input JSON file. A document that cannot
# be read (missing file, stdin "-", invalid JSON) counts as a mutation.
_gh_api_args_unsafe_post()
{
	local _a _next="" _method="" _fields=0 _mutation=0 _val _ep _input="" _has_input=0 _qf _q
	for _a in "$@"; do
		_val=""
		case "${_next}" in
			method) _method="${_a}"; _next=""; continue ;;
			field) _fields=1; _val="${_a}"; _next="" ;;
			input) _fields=1; _has_input=1; _input="${_a}"; _next=""; continue ;;
			skip) _next=""; continue ;;
		esac
		if [ -z "${_val}" ]; then
			case "${_a}" in
				-X|--method) _next=method; continue ;;
				--method=*) _method="${_a#--method=}"; continue ;;
				-X*) _method="${_a#-X}"; continue ;;
				-f|-F|--field|--raw-field) _next=field; continue ;;
				--field=*) _fields=1; _val="${_a#--field=}" ;;
				--raw-field=*) _fields=1; _val="${_a#--raw-field=}" ;;
				-f?*|-F?*) _fields=1; _val="${_a#-?}" ;;
				--input) _next=input; continue ;;
				--input=*) _fields=1; _has_input=1; _input="${_a#--input=}"; continue ;;
				*)
					if _gh_api_flag_takes_value "${_a}"; then _next=skip; fi
					continue
					;;
			esac
		fi
		case "${_val}" in
			query=@*)
				_qf="${_val#query=@}"
				if [ "${_qf}" = "-" ] || [ ! -r "${_qf}" ] \
					|| _gh_graphql_doc_is_mutation "$(head -c 65536 "${_qf}" 2>/dev/null)"; then
					_mutation=1
				fi
				;;
			query=*)
				if _gh_graphql_doc_is_mutation "${_val#query=}"; then
					_mutation=1
				fi
				;;
		esac
	done
	_method="$(printf '%s' "${_method}" | tr '[:lower:]' '[:upper:]')"
	if [ -n "${_method}" ] && [ "${_method}" != "POST" ]; then
		return 1
	fi
	if [ -z "${_method}" ] && [ "${_fields}" != "1" ]; then
		return 1
	fi
	_ep="$(_gh_api_endpoint "$@")"
	_ep="${_ep#/}"
	if [ "${_ep}" = "graphql" ]; then
		if [ "${_has_input}" = "1" ]; then
			if [ "${_input}" = "-" ] || [ ! -r "${_input}" ] \
				|| ! _q="$(jq -r 'if type == "object" then (.query // "") else error("not an object") end' "${_input}" 2>/dev/null)" \
				|| _gh_graphql_doc_is_mutation "${_q}"; then
				_mutation=1
			fi
		fi
		[ "${_mutation}" = "1" ]
		return $?
	fi
	return 0
}

# _gh_graphql_doc_is_mutation <document> — true when any operation keyword
# (at the start of the document or after a closing brace, ignoring whitespace
# and # comments) is "mutation". A document holding a query and a mutation,
# where operationName may select the mutation, therefore counts as a mutation.
# A field named "mutation" after a "}" also matches; that only costs a retry.
# Commas are insignificant in GraphQL, so "}, mutation" also matches.
# String literals ("…" and """…""") are skipped before comments are cut, so a
# "#" inside a string cannot hide a later mutation (scripts/gh_api_retry.py
# graphql_doc_is_mutation uses the same scan).
# Pure bash: no pipeline, so callers running under pipefail cannot see a
# SIGPIPE status.
_gh_graphql_doc_is_mutation()
{
	local _line _out="" _rest _pre _in_block=0
	local _re='(^|\})[[:space:],]*[Mm][Uu][Tt][Aa][Tt][Ii][Oo][Nn]([^A-Za-z0-9_]|$)'
	local _str_re='^"([^"\\]|\\.)*"(.*)$'
	while IFS= read -r _line || [ -n "${_line}" ]; do
		_rest="${_line}"
		while [ -n "${_rest}" ]; do
			if [ "${_in_block}" = "1" ]; then
				# Inside a block string: find the closing """ (\""" is an escape).
				_pre="${_rest%%\"\"\"*}"
				if [ "${_pre}" = "${_rest}" ]; then
					break
				fi
				_rest="${_rest#*\"\"\"}"
				if [ "${_pre: -1}" != "\\" ]; then
					_in_block=0
					_out+=" "
				fi
				continue
			fi
			_pre="${_rest%%[\"#]*}"
			_out+="${_pre}"
			_rest="${_rest:${#_pre}}"
			[ -n "${_rest}" ] || break
			if [ "${_rest:0:1}" = "#" ]; then
				break
			fi
			if [ "${_rest:0:3}" = '"""' ]; then
				_in_block=1
				_rest="${_rest:3}"
				continue
			fi
			if [[ "${_rest}" =~ ${_str_re} ]]; then
				_out+=" "
				_rest="${BASH_REMATCH[2]}"
			else
				break
			fi
		done
		_out+=" "
	done <<< "$1"
	[[ "${_out}" =~ ${_re} ]]
}

# _gh_cmd_is_unsafe_post <full command…> — gh_retry-style command check.
# Covers `gh api …`, `_safe_gh_jq …` and gh's own create subcommands
# (`gh issue|pr create|comment`, `gh workflow run`, `gh release create`).
# `gh issue edit --add-label` and other edits stay retryable.
_gh_cmd_is_unsafe_post()
{
	if [ "${1:-}" = "_safe_gh_jq" ]; then
		_gh_api_args_unsafe_post "${@:2}"
		return $?
	fi
	[ "${1:-}" = "gh" ] || return 1
	case "${2:-} ${3:-}" in
		"api "*) _gh_api_args_unsafe_post "${@:3}"; return $? ;;
		"issue create"|"issue comment"|"pr create"|"pr comment"|"workflow run"|"release create") return 0 ;;
	esac
	return 1
}

# _gh_cmd_log_summary <full command…> — bounded, credential-free summary of
# a gh_retry-style command for failure diagnostics (issue #8095, SEC-001).
# Prints the gh subcommand words, the `gh api` endpoint without its query,
# or a plain issue/PR number, and the names of the flags passed. Flag values
# (comment bodies, fields, titles, --input files) are never printed. The
# output is at most 200 characters, uses a restricted character set and has
# every `::` broken up, so it cannot form a workflow command.
_gh_cmd_log_summary()
{
	local _ghsum_words="" _ghsum_target="" _ghsum_flags="" _ghsum_rest_start=2 _ghsum_api=0
	local _ghsum_word_re='^[a-z][a-z-]{0,30}$'
	local _ghsum_num_re='^[0-9]{1,12}$'
	local _ghsum_cmd_re='^[A-Za-z0-9_.-]{1,40}$'
	local _ghsum_flag_re='^--?[A-Za-z][A-Za-z0-9-]{0,30}$'
	local _ghsum_bad='[^A-Za-z0-9._~/{}@:-]'
	local -a _ghsum_args=("$@")
	local _ghsum_n="${#_ghsum_args[@]}"
	if [ "${1:-}" = "gh" ] && [ "${2:-}" = "api" ]; then
		_ghsum_words="gh api"; _ghsum_api=1; _ghsum_rest_start=2
	elif [ "${1:-}" = "_safe_gh_jq" ]; then
		# `gh_retry _safe_gh_jq <endpoint> …` passes the wrapper name first.
		_ghsum_words="_safe_gh_jq gh api"; _ghsum_api=1; _ghsum_rest_start=1
	elif [ "${1:-}" = "gh" ]; then
		_ghsum_words="gh"
		local _ghsum_i=1 _ghsum_count=0
		while [ "${_ghsum_i}" -lt "${_ghsum_n}" ] && [ "${_ghsum_count}" -lt 2 ]; do
			[[ "${_ghsum_args[${_ghsum_i}]}" =~ ${_ghsum_word_re} ]] || break
			_ghsum_words+=" ${_ghsum_args[${_ghsum_i}]}"
			_ghsum_i=$(( _ghsum_i + 1 )); _ghsum_count=$(( _ghsum_count + 1 ))
		done
		if [ "${_ghsum_i}" -lt "${_ghsum_n}" ] && [[ "${_ghsum_args[${_ghsum_i}]}" =~ ${_ghsum_num_re} ]]; then
			_ghsum_target="${_ghsum_args[${_ghsum_i}]}"
			_ghsum_i=$(( _ghsum_i + 1 ))
		fi
		_ghsum_rest_start="${_ghsum_i}"
	elif [ "${_ghsum_n}" -gt 0 ]; then
		if [[ "${1}" =~ ${_ghsum_cmd_re} ]]; then _ghsum_words="${1}"; else _ghsum_words="<command>"; fi
		_ghsum_rest_start=1
	fi
	if [ "${_ghsum_api}" = "1" ]; then
		local _ghsum_ep
		_ghsum_ep="$(_gh_api_endpoint "${_ghsum_args[@]:${_ghsum_rest_start}}")"
		_ghsum_ep="${_ghsum_ep%%\?*}"
		_ghsum_ep="${_ghsum_ep%%#*}"
		_ghsum_ep="${_ghsum_ep//${_ghsum_bad}/_}"
		_ghsum_target="${_ghsum_ep:0:120}"
	fi
	# Flag names only. A flag without an attached value always treats the
	# next token as its value and skips it, even one that starts with "-",
	# so a value such as `--project --x` is never logged as a flag name.
	# Only the known boolean flags listed below leave the next token alone.
	# Over-skipping only drops information from the log line.
	local _ghsum_j="${_ghsum_rest_start}" _ghsum_skip=0 _ghsum_force=0 _ghsum_a _ghsum_name _ghsum_attached
	local _ghsum_pos_seen=0
	while [ "${_ghsum_j}" -lt "${_ghsum_n}" ]; do
		_ghsum_a="${_ghsum_args[${_ghsum_j}]}"
		_ghsum_j=$(( _ghsum_j + 1 ))
		if [ "${_ghsum_skip}" = "1" ]; then
			_ghsum_skip=0
			if [ "${_ghsum_force}" = "1" ]; then _ghsum_force=0; continue; fi
			case "${_ghsum_a}" in -*) ;; *) continue ;; esac
		fi
		[ "${_ghsum_a}" = "--" ] && break
		case "${_ghsum_a}" in
			-*) ;;
			*)
				# Non-api gh: the first positional counts only as a plain number.
				if [ "${_ghsum_api}" = "0" ] && [ "${_ghsum_words%% *}" = "gh" ] && [ "${_ghsum_pos_seen}" = "0" ] \
					&& [ -z "${_ghsum_target}" ] && [[ "${_ghsum_a}" =~ ${_ghsum_num_re} ]]; then
					_ghsum_target="${_ghsum_a}"
				fi
				_ghsum_pos_seen=1
				continue
				;;
		esac
		_ghsum_name="${_ghsum_a}"; _ghsum_attached=0
		case "${_ghsum_a}" in
			--*=*) _ghsum_name="${_ghsum_a%%=*}"; _ghsum_attached=1 ;;
			--*) ;;
			-?*)
				if [ "${#_ghsum_a}" -gt 2 ]; then _ghsum_name="${_ghsum_a:0:2}"; _ghsum_attached=1; fi
				;;
		esac
		if [ "${_ghsum_attached}" != "1" ]; then
			case "${_ghsum_name}" in
				--paginate|--slurp|-i|--include|--silent|--verbose|--web|--draft|--fill|--squash|--merge|--rebase|--auto|--admin|--delete-branch|--yes|--disable-auto|--comments) ;;
				*) _ghsum_skip=1; _ghsum_force=1 ;;
			esac
		fi
		[[ "${_ghsum_name}" =~ ${_ghsum_flag_re} ]] || continue
		case " ${_ghsum_flags} " in
			*" ${_ghsum_name} "*) ;;
			*) _ghsum_flags="${_ghsum_flags:+${_ghsum_flags} }${_ghsum_name}" ;;
		esac
	done
	local _ghsum_out="${_ghsum_words}"
	if [ -n "${_ghsum_target}" ]; then _ghsum_out="${_ghsum_out:+${_ghsum_out} }${_ghsum_target}"; fi
	if [ -n "${_ghsum_flags}" ]; then _ghsum_out="${_ghsum_out:+${_ghsum_out} }(flags: ${_ghsum_flags})"; fi
	while [[ "${_ghsum_out}" == *::* ]]; do
		_ghsum_out="${_ghsum_out//::/: :}"
	done
	printf '%s' "${_ghsum_out:0:200}"
}

# _gh_api_header <headers_file> <name> — value of the first matching header.
_gh_api_header()
{
	[ -r "$1" ] || return 0
	grep -i "^$2:" "$1" 2>/dev/null | head -1 | sed 's/^[^:]*:[[:space:]]*//; s/[[:space:]]*$//' | tr -d '\r' || true
}

# _gh_api_classify <status> <headers_file> <stderr_file> <body_file>
# Prints "<kind> <wait_secs> <bucket>": kind is primary|secondary|transient|
# permanent; wait_secs is -1 when a primary limit carries no reset (the
# caller then reads /rate_limit) and 0 for permanent/transient; bucket is
# the x-ratelimit-resource header or "-". GH_API_RETRY_NOW overrides the
# clock (tests only).
_gh_api_classify()
{
	local _status="$1" _hdr="$2" _err="$3" _body="$4" _text _remaining _reset _retry_after _resource _now _wait
	_now="${GH_API_RETRY_NOW:-$(date +%s)}"
	_text="$(cat "${_err}" "${_body}" 2>/dev/null || true)"
	if ! [[ "${_status}" =~ ^[0-9]{3}$ ]]; then
		_status="$(grep -oE 'HTTP [0-9]{3}' "${_err}" 2>/dev/null | head -1 | grep -oE '[0-9]{3}' || true)"
	fi
	_remaining="$(_gh_api_header "${_hdr}" 'x-ratelimit-remaining')"
	_reset="$(_gh_api_header "${_hdr}" 'x-ratelimit-reset')"
	_retry_after="$(_gh_api_header "${_hdr}" 'retry-after')"
	_resource="$(_gh_api_header "${_hdr}" 'x-ratelimit-resource')"
	[[ "${_resource}" =~ ^[a-z_]+$ ]] || _resource="-"
	_wait=-1
	if [[ "${_reset}" =~ ^[0-9]+$ ]]; then
		_wait=$(( 10#${_reset} - _now + 1 ))
		[ "${_wait}" -lt 1 ] && _wait=1
	fi
	if [ -z "${_status}" ] || [ "${_status}" = "403" ] || [ "${_status}" = "429" ]; then
		if [ "${_remaining}" = "0" ] && [ -n "${_status}" ]; then
			printf 'primary %s %s\n' "${_wait}" "${_resource}"
			return 0
		fi
		if [[ "${_retry_after}" =~ ^[0-9]+$ ]] && [ -n "${_status}" ]; then
			printf 'secondary %s %s\n' "$(( 10#${_retry_after} ))" "${_resource}"
			return 0
		fi
		if printf '%s' "${_text}" | grep -qiE 'secondary rate|abuse detection'; then
			printf 'secondary 60 %s\n' "${_resource}"
			return 0
		fi
		if printf '%s' "${_text}" | grep -qiE 'rate limit'; then
			printf 'primary %s %s\n' "${_wait}" "${_resource}"
			return 0
		fi
		if [ "${_status}" = "429" ]; then
			printf 'secondary 60 %s\n' "${_resource}"
			return 0
		fi
		if [ "${_status}" = "403" ] || _is_gh_permanent_failure "${_text}"; then
			printf 'permanent 0 %s\n' "${_resource}"
			return 0
		fi
		printf 'transient 0 %s\n' "${_resource}"
		return 0
	fi
	case "${_status}" in
		408|5[0-9][0-9]) printf 'transient 0 %s\n' "${_resource}" ;;
		*) printf 'permanent 0 %s\n' "${_resource}" ;;
	esac
}

# _gh_rate_limit_reset_for_bucket <bucket> — reset epoch from /rate_limit.
_gh_rate_limit_reset_for_bucket()
{
	local _b="${1:-core}"
	[[ "${_b}" =~ ^[a-z_]+$ ]] || _b=core
	gh api rate_limit --jq ".resources.${_b}.reset // empty" 2>/dev/null | tr -d '[:space:]' || true
}

# gh_rate_limit_breaker_active [bucket] — true while a breaker line for the
# bucket (default core) has a reset epoch in the future. The file is not
# trusted for anything but skipping optional work; malformed lines are ignored.
gh_rate_limit_breaker_active()
{
	local _b="${1:-core}"
	[ -r "${_GH_RATE_LIMIT_BREAKER_FILE}" ] || return 1
	awk -v b="${_b}" -v now="$(date +%s)" '
		$1 == b && $2 ~ /^[0-9]+$/ && ($2 + 0) > (now + 0) { found = 1 }
		END { exit(found ? 0 : 1) }
	' "${_GH_RATE_LIMIT_BREAKER_FILE}" 2>/dev/null
}

_gh_api_retry_log()
{
	# $1 outcome $2 kind $3 bucket $4 status $5 attempt $6 max $7 wait $8 endpoint
	echo "GH_API_RETRY outcome=$1 kind=$2 bucket=$3 status=${4:-none} attempt=$5/$6 wait_secs=$7 endpoint=$(_gh_actions_escape "$8")" >&2
}

gh_api_retry()
{
	local _idem=0 _opt=0
	while :; do
		case "${1:-}" in
			--idempotent) _idem=1; shift ;;
			--optional) _opt=1; shift ;;
			*) break ;;
		esac
	done
	[ "${GH_RETRY_IDEMPOTENT:-false}" = "true" ] && _idem=1
	local _max="${GH_RETRY_MAX_ATTEMPTS:-5}" _max_wait="${GH_RETRY_RATE_LIMIT_MAX_WAIT_SECS:-600}"
	local _cap="${GH_RETRY_BACKOFF_CAP_SECS:-120}" _low="${GH_API_RETRY_LOW_BUDGET_REMAINING:-100}"
	[[ "${_max}" =~ ^[1-9][0-9]*$ ]] || _max=5
	[[ "${_max_wait}" =~ ^[0-9]+$ ]] || _max_wait=600
	[[ "${_cap}" =~ ^[0-9]+$ ]] || _cap=120
	[[ "${_low}" =~ ^[0-9]+$ ]] || _low=100
	local _bucket _endpoint _paginate=0 _a
	_bucket="$(_gh_api_bucket "$@")"
	_endpoint="$(_gh_api_endpoint "$@")"
	for _a in "$@"; do [ "${_a}" = "--paginate" ] && _paginate=1; done
	if [ "${_idem}" != "1" ] && _gh_api_args_unsafe_post "$@"; then
		_max=1
	fi
	GH_API_RETRY_LAST_STATUS=""
	GH_API_RETRY_LAST_KIND=""
	if [ "${_opt}" = "1" ] && gh_rate_limit_breaker_active "${_bucket}"; then
		GH_API_RETRY_LAST_KIND="skipped"
		_gh_api_retry_log skipped breaker "${_bucket}" "" 0 "${_max}" 0 "${_endpoint}"
		return 75
	fi
	local _dir
	if ! _dir=$(mktemp -d "${TMPDIR:-/tmp}/gh_api_retry.XXXXXX" 2>/dev/null); then
		echo "::error::gh_api_retry: mktemp failed; aborting without running" >&2
		return 1
	fi
	local _attempt=1 _rc _status _kind _wait _hb _lines _now _reset _remaining _class _eff_bucket _copy_rc
	while [ "${_attempt}" -le "${_max}" ]; do
		: > "${_dir}/headers"
		_rc=0
		if [ "${_paginate}" = "1" ]; then
			gh api "$@" > "${_dir}/body" 2> "${_dir}/err" || _rc=$?
		else
			gh api -i "$@" > "${_dir}/raw" 2> "${_dir}/err" || _rc=$?
			# Split at the FIRST blank line: status line + headers, then body.
			_lines="$(awk '{ sub(/\r$/, "") } $0 == "" { print NR; exit }' "${_dir}/raw" 2>/dev/null || true)"
			if [[ "${_lines}" =~ ^[0-9]+$ ]] && head -1 "${_dir}/raw" | grep -qE '^HTTP/[0-9.]+ [0-9]{3}'; then
				head -n "${_lines}" "${_dir}/raw" | tr -d '\r' > "${_dir}/headers"
				tail -n +"$(( _lines + 1 ))" "${_dir}/raw" > "${_dir}/body"
			else
				cp "${_dir}/raw" "${_dir}/body"
			fi
		fi
		# Issue #9928: a PR write evicts the run-local read cache.
		_gh_pr_cache_evict_for_cmd gh api "$@" || true
		_status="$(sed -n '1s/^HTTP\/[0-9.]* \([0-9][0-9][0-9]\).*/\1/p' "${_dir}/headers" 2>/dev/null || true)"
		GH_API_RETRY_LAST_STATUS="${_status}"
		if [ "${_rc}" -eq 0 ]; then
			GH_API_RETRY_LAST_KIND="ok"
			_copy_rc=0
			cat "${_dir}/body" || _copy_rc=$?
			_remaining="$(_gh_api_header "${_dir}/headers" 'x-ratelimit-remaining')"
			_reset="$(_gh_api_header "${_dir}/headers" 'x-ratelimit-reset')"
			_eff_bucket="$(_gh_api_header "${_dir}/headers" 'x-ratelimit-resource')"
			[[ "${_eff_bucket}" =~ ^[a-z_]+$ ]] || _eff_bucket="${_bucket}"
			if [[ "${_remaining}" =~ ^[0-9]+$ ]] && [[ "${_reset}" =~ ^[0-9]+$ ]] && [ "${_remaining}" -lt "${_low}" ]; then
				_gh_rate_limit_trip_breaker "${_eff_bucket}" "${_reset}" low
			fi
			rm -rf "${_dir}"
			return "${_copy_rc}"
		fi
		_class="$(_gh_api_classify "${_status}" "${_dir}/headers" "${_dir}/err" "${_dir}/body")"
		read -r _kind _wait _hb <<< "${_class}"
		_eff_bucket="${_bucket}"
		if [ -n "${_hb}" ] && [ "${_hb}" != "-" ]; then _eff_bucket="${_hb}"; fi
		GH_API_RETRY_LAST_KIND="${_kind}"
		if [ -z "${GH_API_RETRY_LAST_STATUS}" ]; then
			GH_API_RETRY_LAST_STATUS="$(grep -oE 'HTTP [0-9]{3}' "${_dir}/err" 2>/dev/null | head -1 | grep -oE '[0-9]{3}' || true)"
		fi
		case "${_kind}" in
			permanent)
				_gh_api_retry_log permanent permanent "${_eff_bucket}" "${GH_API_RETRY_LAST_STATUS}" "${_attempt}" "${_max}" 0 "${_endpoint}"
				if [ -s "${_dir}/err" ]; then
					echo "::warning::  stderr: $(_gh_actions_escape "$(_gh_final_stderr_excerpt "${_dir}/err")")" >&2
				fi
				rm -rf "${_dir}"
				return 2
				;;
			primary|secondary)
				_now="$(date +%s)"
				if ! [[ "${_wait}" =~ ^[0-9]+$ ]]; then
					_reset="$(_gh_rate_limit_reset_for_bucket "${_eff_bucket}")"
					if [[ "${_reset}" =~ ^[0-9]+$ ]]; then
						_wait=$(( _reset - _now + 1 ))
						[ "${_wait}" -lt 1 ] && _wait=1
					else
						_wait=60
					fi
				fi
				_gh_ratelimit_tg_alert
				_gh_rate_limit_trip_breaker "${_eff_bucket}" "$(( _now + _wait ))"
				if [ "${_attempt}" -ge "${_max}" ] || [ "${_wait}" -gt "${_max_wait}" ]; then
					_gh_api_retry_log gave_up "${_kind}" "${_eff_bucket}" "${GH_API_RETRY_LAST_STATUS}" "${_attempt}" "${_max}" "${_wait}" "${_endpoint}"
					rm -rf "${_dir}"
					return 75
				fi
				_gh_api_retry_log retry "${_kind}" "${_eff_bucket}" "${GH_API_RETRY_LAST_STATUS}" "${_attempt}" "${_max}" "${_wait}" "${_endpoint}"
				sleep "${_wait}"
				;;
			*)
				if [ "${_attempt}" -ge "${_max}" ]; then
					_gh_api_retry_log gave_up transient "${_eff_bucket}" "${GH_API_RETRY_LAST_STATUS}" "${_attempt}" "${_max}" 0 "${_endpoint}"
					if [ "${_max}" = "1" ] && [ "${_idem}" != "1" ]; then
						echo "::notice::gh_api_retry: single attempt for non-idempotent create: $(_gh_actions_escape "${_endpoint}")" >&2
					fi
					if [ -s "${_dir}/err" ]; then
						echo "::warning::  stderr: $(_gh_actions_escape "$(_gh_final_stderr_excerpt "${_dir}/err")")" >&2
					fi
					rm -rf "${_dir}"
					return 1
				fi
				_wait=$(( 2 * (2 ** (_attempt - 1)) + RANDOM % 3 ))
				[ "${_wait}" -gt "${_cap}" ] && _wait="${_cap}"
				_gh_api_retry_log retry transient "${_eff_bucket}" "${GH_API_RETRY_LAST_STATUS}" "${_attempt}" "${_max}" "${_wait}" "${_endpoint}"
				sleep "${_wait}"
				;;
		esac
		_attempt=$(( _attempt + 1 ))
	done
	rm -rf "${_dir}"
	return 1
}

# ---------------------------------------------------------------
# gh_retry — Execute a gh CLI command with automatic retry.
#
# Rate-limit errors  → wait until X-RateLimit-Reset, retry (up to max_attempts).
# Other failures     → exponential backoff 1 s, 2 s, 4 s, …
#
# Stdout contract: each attempt's stdout is buffered in a temp file
# and only the attempt that succeeds is copied to the caller's stdout.
# `gh api` prints the error response body to stdout when a call fails,
# so without the buffer a retry that succeeded after a rate limit
# handed the caller the error bodies followed by the real response
# (issue #5495: clarify.yml's `gh_retry gh api … > file` then read
# `null`, `null`, `5016`). A failed attempt's stdout is dropped; one
# warning line reports its size, never its content, because callers
# grep gh_retry's stderr to pick a branch. A failure that never
# succeeds writes nothing to stdout and returns 1. If the successful
# attempt's output cannot be delivered (the reader closed the pipe),
# the copy's non-zero status is returned and the command is not re-run.
# The copy uses bash's builtin printf in a subshell (141 on SIGPIPE, 1 on
# EPIPE when SIGPIPE is ignored), not cat, so a closed reader gives a
# non-zero status whichever cat is installed: on ubuntu-26.04 runners Rust
# coreutils' cat exited 0 there (issues #9225, #9227).
#
# Usage:
#   gh_retry gh api repos/owner/repo/issues
#   gh_retry gh issue edit 42 --add-label bug
# ---------------------------------------------------------------
gh_retry()
{
	# Q2=A (issue #6634): a POST create makes one attempt unless a leading
	# --idempotent or GH_RETRY_IDEMPOTENT=true marks it safe to repeat.
	local _ghr_idem="${GH_RETRY_IDEMPOTENT:-false}"
	if [ "${1:-}" = "--idempotent" ]; then _ghr_idem=true; shift; fi
	local max_attempts="${GH_RETRY_MAX_ATTEMPTS:-5}"
	if [ "${_ghr_idem}" != "true" ] && _gh_cmd_is_unsafe_post "$@"; then
		max_attempts=1
	fi
	local attempt=1
	local stderr_file stdout_file
	if ! stderr_file=$(mktemp "${TMPDIR:-/tmp}/gh_retry_stderr.XXXXXX" 2>/dev/null); then
		echo "::error::gh_retry: failed to create stderr temp file (mktemp failed); aborting without running: $(_gh_cmd_log_summary "$@")" >&2
		return 1
	fi
	if ! stdout_file=$(mktemp "${TMPDIR:-/tmp}/gh_retry_stdout.XXXXXX" 2>/dev/null); then
		echo "::error::gh_retry: failed to create stdout temp file (mktemp failed); aborting without running: $(_gh_cmd_log_summary "$@")" >&2
		rm -f "${stderr_file}"
		return 1
	fi

	while [ "${attempt}" -le "${max_attempts}" ]; do
		local _gh_retry_cmd_rc=0
		"$@" >"${stdout_file}" 2>"${stderr_file}" || _gh_retry_cmd_rc=$?
		# Issue #9928: a PR write evicts the run-local read cache, whatever
		# its status (a failed attempt may still have been applied).
		_gh_pr_cache_evict_for_cmd "$@" || true
		if [ "${_gh_retry_cmd_rc}" -eq 0 ]; then
			local _gh_retry_replay_rc=0
			# Replay with builtin printf, not cat (issues #9225, #9227): the
			# ubuntu-26.04 runner's cat exited 0 after the reader closed the
			# pipe. The subshell takes the SIGPIPE (141) or EPIPE (1), so the
			# caller's shell survives, both temp files are removed and the
			# command is not re-run. Chunks split at NUL keep the output
			# byte-exact (NULs, backslashes, trailing newlines).
			(
				_gh_retry_replay_chunk=""
				while IFS= read -r -d '' _gh_retry_replay_chunk; do
					builtin printf '%s\0' "${_gh_retry_replay_chunk}" || exit "$?"
				done
				if [ -n "${_gh_retry_replay_chunk}" ]; then
					builtin printf '%s' "${_gh_retry_replay_chunk}" || exit "$?"
				fi
				exit 0
			) < "${stdout_file}" || _gh_retry_replay_rc=$?
			rm -f "${stderr_file}" "${stdout_file}"
			return "${_gh_retry_replay_rc}"
		fi

		if [ -s "${stdout_file}" ]; then
			local _gh_retry_stdout_bytes
			_gh_retry_stdout_bytes=$(wc -c < "${stdout_file}" 2>/dev/null | tr -d '[:space:]' || true)
			echo "::warning::  gh_retry: dropped ${_gh_retry_stdout_bytes:-?} bytes of stdout from failed attempt ${attempt}/${max_attempts}" >&2
		fi

		local stderr_content
		stderr_content=$(cat "${stderr_file}" 2>/dev/null || true)

		if _is_gh_permanent_failure "${stderr_content}"; then
			echo "::warning::gh command failed with non-retryable error (attempt ${attempt}/${max_attempts}); not retrying: $(_gh_cmd_log_summary "$@")" >&2
			if [ -n "${stderr_content}" ]; then
				echo "::warning::  stderr: $(_gh_actions_escape "$(_gh_final_stderr_excerpt "${stderr_file}")")" >&2
			fi
			rm -f "${stderr_file}" "${stdout_file}"
			return 1
		fi

		if _is_gh_rate_limit "${stderr_content}"; then
			echo "::warning::GitHub API rate limit hit (attempt ${attempt}/${max_attempts}), waiting for reset…" >&2
			_gh_ratelimit_tg_alert
			_gh_rate_limit_trip_breaker "$(_gh_cmd_bucket "$@")" "$(( $(date +%s) + 60 ))"
			if [ "${attempt}" -lt "${max_attempts}" ]; then
				_gh_rate_limit_wait_for "${stderr_content}" "$@"
			fi
		else
			local wait_secs=$(( 2 ** (attempt - 1) ))
			echo "::warning::gh command failed (attempt ${attempt}/${max_attempts}), retrying in ${wait_secs}s…" >&2
			if [ -n "${stderr_content}" ]; then
				echo "::warning::  stderr: $(_gh_actions_escape "$(_gh_final_stderr_excerpt "${stderr_file}")")" >&2
			fi
			if [ "${attempt}" -lt "${max_attempts}" ]; then
				sleep "${wait_secs}"
			fi
		fi

		attempt=$(( attempt + 1 ))
	done

	if [ "${max_attempts}" = "1" ] && [ "${_ghr_idem}" != "true" ] && _gh_cmd_is_unsafe_post "$@"; then
		echo "::notice::gh_retry: single attempt for non-idempotent create: $(_gh_cmd_log_summary "$@")" >&2
	fi
	echo "::error::gh command failed after ${max_attempts} attempts: $(_gh_cmd_log_summary "$@")" >&2
	if [ -s "${stderr_file}" ]; then
		echo "::warning::  stderr: $(_gh_actions_escape "$(_gh_final_stderr_excerpt "${stderr_file}")")" >&2
	fi
	rm -f "${stderr_file}" "${stdout_file}"
	return 1
}

# ---------------------------------------------------------------
# gh_retry_to_file — Like gh_retry but captures stdout to a file.
#
# Each retry truncates the file so only the last attempt's output
# remains.  Useful for paginated / raw-content downloads.
#
# Usage:
#   gh_retry_to_file /tmp/result.json gh api repos/owner/repo/pulls/1
# ---------------------------------------------------------------
gh_retry_to_file()
{
	# [--idempotent] <outfile> <command…>. Each attempt is buffered; only a
	# successful attempt's output is written to <outfile>, and after a final
	# failure <outfile> is truncated, so a failed body never stays in it.
	local _ghr_idem="${GH_RETRY_IDEMPOTENT:-false}"
	if [ "${1:-}" = "--idempotent" ]; then _ghr_idem=true; shift; fi
	local outfile="$1"; shift
	local max_attempts="${GH_RETRY_MAX_ATTEMPTS:-5}"
	if [ "${_ghr_idem}" != "true" ] && _gh_cmd_is_unsafe_post "$@"; then
		max_attempts=1
	fi
	local attempt=1
	local stderr_file attempt_file
	if ! stderr_file=$(mktemp "${TMPDIR:-/tmp}/gh_retry_stderr.XXXXXX" 2>/dev/null); then
		echo "::error::gh_retry_to_file: failed to create stderr temp file (mktemp failed); aborting without running: $(_gh_cmd_log_summary "$@")" >&2
		return 1
	fi
	if ! attempt_file=$(mktemp "${TMPDIR:-/tmp}/gh_retry_stdout.XXXXXX" 2>/dev/null); then
		echo "::error::gh_retry_to_file: failed to create stdout temp file (mktemp failed); aborting without running: $(_gh_cmd_log_summary "$@")" >&2
		rm -f "${stderr_file}"
		return 1
	fi

	while [ "${attempt}" -le "${max_attempts}" ]; do
		local _ghr_cmd_rc=0
		"$@" > "${attempt_file}" 2>"${stderr_file}" || _ghr_cmd_rc=$?
		# Issue #9928: a PR write evicts the run-local read cache.
		_gh_pr_cache_evict_for_cmd "$@" || true
		if [ "${_ghr_cmd_rc}" -eq 0 ]; then
			local _ghr_copy_rc=0
			cat "${attempt_file}" > "${outfile}" || _ghr_copy_rc=$?
			rm -f "${stderr_file}" "${attempt_file}"
			return "${_ghr_copy_rc}"
		fi

		local stderr_content
		stderr_content=$(cat "${stderr_file}" 2>/dev/null || true)

		if _is_gh_permanent_failure "${stderr_content}"; then
			echo "::warning::gh command failed with non-retryable error (attempt ${attempt}/${max_attempts}); not retrying: $(_gh_cmd_log_summary "$@")" >&2
			if [ -n "${stderr_content}" ]; then
				echo "::warning::  stderr: $(_gh_actions_escape "$(_gh_final_stderr_excerpt "${stderr_file}")")" >&2
			fi
			rm -f "${stderr_file}" "${attempt_file}"
			: > "${outfile}" 2>/dev/null || true
			return 1
		fi

		if _is_gh_rate_limit "${stderr_content}"; then
			echo "::warning::GitHub API rate limit hit (attempt ${attempt}/${max_attempts}), waiting for reset…" >&2
			_gh_ratelimit_tg_alert
			_gh_rate_limit_trip_breaker "$(_gh_cmd_bucket "$@")" "$(( $(date +%s) + 60 ))"
			if [ "${attempt}" -lt "${max_attempts}" ]; then
				_gh_rate_limit_wait_for "${stderr_content}" "$@"
			fi
		else
			local wait_secs=$(( 2 ** (attempt - 1) ))
			echo "::warning::gh command failed (attempt ${attempt}/${max_attempts}), retrying in ${wait_secs}s…" >&2
			if [ -n "${stderr_content}" ]; then
				echo "::warning::  stderr: $(_gh_actions_escape "$(_gh_final_stderr_excerpt "${stderr_file}")")" >&2
			fi
			if [ "${attempt}" -lt "${max_attempts}" ]; then
				sleep "${wait_secs}"
			fi
		fi

		attempt=$(( attempt + 1 ))
	done

	echo "::error::gh command failed after ${max_attempts} attempts: $(_gh_cmd_log_summary "$@")" >&2
	if [ -s "${stderr_file}" ]; then
		echo "::warning::  stderr: $(_gh_actions_escape "$(_gh_final_stderr_excerpt "${stderr_file}")")" >&2
	fi
	rm -f "${stderr_file}" "${attempt_file}"
	: > "${outfile}" 2>/dev/null || true
	return 1
}

# ---------------------------------------------------------------
# _safe_gh_jq — run gh api and suppress stdout on failure.
#
# When gh api receives a non-2xx response (e.g. 403 rate limit),
# it dumps the raw error JSON to stdout WITHOUT applying the --jq
# filter.  The common shell pattern
#   val="$(gh api ... --jq '.field' 2>/dev/null || echo "fallback")"
# is broken because the error JSON on stdout combines with the
# fallback string, producing garbage that fails equality checks.
#
# This function captures stdout to a temp file, checks the exit
# code, and only emits output on success.  On failure it outputs
# nothing and returns 1, so `|| echo "fallback"` works correctly.
#
# Usage:
#   val="$(_safe_gh_jq "repos/o/r/pulls/1" --jq '.state' || echo "open")"
# ---------------------------------------------------------------
_safe_gh_jq()
{
	local _tmpf
	if ! _tmpf=$(mktemp "${TMPDIR:-/tmp}/_safe_gh_jq.XXXXXX" 2>/dev/null); then
		echo "::error::_safe_gh_jq: failed to create temp file (mktemp failed); aborting without running: $(_gh_cmd_log_summary gh api "$@")" >&2
		return 1
	fi
	if gh api "$@" > "${_tmpf}"; then
		cat "${_tmpf}"
		rm -f "${_tmpf}"
		return 0
	fi
	rm -f "${_tmpf}"
	return 1
}

# ---------------------------------------------------------------
# gh_api_json_to_file — Fetch a GitHub API JSON response to a file,
# with JSON validation and rate-limit-aware retry.
#
# Like gh_retry_to_file but additionally validates the response body
# with `jq empty`.  If the API call succeeds but the response is not
# valid JSON (e.g. truncated due to a network interruption), the
# attempt is retried with exponential backoff.
#
# Usage:
#   tmp="$(mktemp)"
#   gh_api_json_to_file "$tmp" gh api repos/owner/repo/issues/1
#   jq -r '.title' "$tmp"
# ---------------------------------------------------------------
gh_api_json_to_file()
{
	local _ghr_idem="${GH_RETRY_IDEMPOTENT:-false}"
	if [ "${1:-}" = "--idempotent" ]; then _ghr_idem=true; shift; fi
	local outfile="$1"; shift
	local max_attempts="${GH_RETRY_MAX_ATTEMPTS:-5}"
	if [ "${_ghr_idem}" != "true" ] && _gh_cmd_is_unsafe_post "$@"; then
		max_attempts=1
	fi
	local attempt=1
	local stderr_file
	if ! stderr_file=$(mktemp "${TMPDIR:-/tmp}/gh_api_json_stderr.XXXXXX" 2>/dev/null); then
		echo "::error::gh_api_json_to_file: failed to create stderr temp file (mktemp failed); aborting without running: $(_gh_cmd_log_summary "$@")" >&2
		return 1
	fi

	while [ "${attempt}" -le "${max_attempts}" ]; do
		: > "${outfile}"
		if "$@" > "${outfile}" 2>"${stderr_file}"; then
			if [ -s "${outfile}" ] && jq empty "${outfile}" >/dev/null 2>&1; then
				rm -f "${stderr_file}"
				return 0
			fi
			local wait_secs=$(( 2 ** (attempt - 1) ))
			echo "::warning::gh api returned invalid JSON (attempt ${attempt}/${max_attempts}), retrying in ${wait_secs}s…" >&2
			# Never print the response itself (issue #8095, SEC-002): it may
			# hold private API data. Size, hash prefix and category only.
			local _ghj_bytes _ghj_sha="" _ghj_cat
			_ghj_bytes="$(wc -c < "${outfile}" 2>/dev/null | tr -d '[:space:]' || true)"
			[[ "${_ghj_bytes}" =~ ^[0-9]+$ ]] || _ghj_bytes="unknown"
			if command -v sha256sum >/dev/null 2>&1; then
				_ghj_sha="$(sha256sum < "${outfile}" 2>/dev/null | cut -c1-12 || true)"
			elif command -v shasum >/dev/null 2>&1; then
				_ghj_sha="$(shasum -a 256 < "${outfile}" 2>/dev/null | cut -c1-12 || true)"
			fi
			[[ "${_ghj_sha}" =~ ^[0-9a-f]{12}$ ]] || _ghj_sha="unavailable"
			if [ -s "${outfile}" ]; then _ghj_cat="invalid_json"; else _ghj_cat="empty"; fi
			echo "::warning::gh api response summary (attempt ${attempt}/${max_attempts}): bytes=${_ghj_bytes} sha256=${_ghj_sha} category=${_ghj_cat}" >&2
			if [ "${attempt}" -lt "${max_attempts}" ]; then
				sleep "${wait_secs}"
			fi
		else
			local stderr_content
			stderr_content=$(cat "${stderr_file}" 2>/dev/null || true)

			if _is_gh_rate_limit "${stderr_content}"; then
				echo "::warning::GitHub API rate limit hit (attempt ${attempt}/${max_attempts}), waiting for reset…" >&2
				_gh_ratelimit_tg_alert
				_gh_rate_limit_trip_breaker "$(_gh_cmd_bucket "$@")" "$(( $(date +%s) + 60 ))"
				if [ "${attempt}" -lt "${max_attempts}" ]; then
					_gh_rate_limit_wait_for "${stderr_content}" "$@"
				fi
			else
				local wait_secs=$(( 2 ** (attempt - 1) ))
				echo "::warning::gh command failed (attempt ${attempt}/${max_attempts}), retrying in ${wait_secs}s…" >&2
				if [ -n "${stderr_content}" ]; then
					echo "::warning::  stderr: $(_gh_actions_escape "$(_gh_final_stderr_excerpt "${stderr_file}")")" >&2
				fi
				if [ "${attempt}" -lt "${max_attempts}" ]; then
					sleep "${wait_secs}"
				fi
			fi
		fi

		attempt=$(( attempt + 1 ))
	done

	echo "::error::gh api failed to return valid JSON after ${max_attempts} attempts: $(_gh_cmd_log_summary "$@")" >&2
	if [ -s "${stderr_file}" ]; then
		echo "::warning::  stderr: $(_gh_actions_escape "$(_gh_final_stderr_excerpt "${stderr_file}")")" >&2
	fi
	rm -f "${stderr_file}"
	: > "${outfile}"
	return 1
}

# ---------------------------------------------------------------
# curl_gh_api — curl wrapper with rate-limit retry for GitHub API.
#
# Captures HTTP status code.  On 429 or 403-with-rate-limit body,
# sleeps 30 s and retries.  Other errors use exponential backoff.
# Outputs the response body on success (HTTP 2xx).
#
# Usage:
#   curl_gh_api -s \
#       -H "Authorization: token ${GH_TOKEN}" \
#       -H "Accept: application/vnd.github.v3+json" \
#       "https://api.github.com/repos/owner/repo/issues/1/comments"
# ---------------------------------------------------------------
curl_gh_api()
{
	local max_attempts="${GH_RETRY_MAX_ATTEMPTS:-5}"
	local attempt=1
	local body_file header_file
	if ! body_file=$(mktemp "${TMPDIR:-/tmp}/curl_gh_body.XXXXXX" 2>/dev/null); then
		echo "::error::curl_gh_api: failed to create body temp file (mktemp failed); aborting without calling curl" >&2
		return 1
	fi
	if ! header_file=$(mktemp "${TMPDIR:-/tmp}/curl_gh_hdr.XXXXXX" 2>/dev/null); then
		echo "::error::curl_gh_api: failed to create header temp file (mktemp failed); aborting without calling curl" >&2
		rm -f "${body_file}"
		return 1
	fi

	while [ "${attempt}" -le "${max_attempts}" ]; do
		: > "${body_file}"
		: > "${header_file}"
		local http_code
		http_code=$(curl -o "${body_file}" -D "${header_file}" -w '%{http_code}' "$@" 2>/dev/null) || http_code="000"

		if [ "${http_code}" -ge 200 ] 2>/dev/null && [ "${http_code}" -lt 300 ] 2>/dev/null; then
			cat "${body_file}"
			rm -f "${body_file}" "${header_file}"
			return 0
		fi

		local body_content
		body_content=$(cat "${body_file}" 2>/dev/null || true)

		if [ "${http_code}" = "429" ] || { [ "${http_code}" = "403" ] && _is_gh_rate_limit "${body_content}"; }; then
			echo "::warning::GitHub API rate limit (HTTP ${http_code}, attempt ${attempt}/${max_attempts}), waiting for reset…" >&2
			_gh_ratelimit_tg_alert
			local _reset_ts
			_reset_ts=$(_parse_reset_header "${header_file}")
			_gh_rate_limit_trip_breaker "$(_gh_api_header "${header_file}" 'x-ratelimit-resource')" "${_reset_ts}"
			if [ "${attempt}" -lt "${max_attempts}" ]; then
				_sleep_until_reset "${_reset_ts}"
			fi
		else
			local wait_secs=$(( 2 ** (attempt - 1) ))
			echo "::warning::GitHub API curl failed HTTP ${http_code} (attempt ${attempt}/${max_attempts}), retrying in ${wait_secs}s…" >&2
			if [ "${attempt}" -lt "${max_attempts}" ]; then
				sleep "${wait_secs}"
			fi
		fi

		attempt=$(( attempt + 1 ))
	done

	rm -f "${body_file}" "${header_file}"
	echo "::error::GitHub API curl failed after ${max_attempts} attempts" >&2
	return 1
}

# ---------------------------------------------------------------
# _gh_pr_with_all_comments_rest — legacy REST parity fetch path.
#
# Emits JSON object:
# {
#   "meta": {"title", "body", "head_ref", "base_ref", "head_sha"},
#   "comments": [{"author", "body", "created_at"}],
#   "review_comments": [{"author", "path", "line", "body"}]
# }
#
# Incomplete-context signal (BUG-002): when the PR metadata read (not
# preloaded), the issue-comments read or the review-comments read fails
# (gh_retry exits non-zero, or its jq transform fails), the value still
# falls back to {} / [] so the shape is unchanged, and two additive
# top-level keys are appended after "review_comments":
#   "comments_incomplete": true,
#   "incomplete_reasons": [subset of "pr_meta_read_failed",
#     "issue_comments_read_failed", "review_comments_read_failed", in that
#     order]
# plus one stderr line:
#   ::warning::PR_COMMENTS_CONTEXT_INCOMPLETE helper=gh_pr_with_all_comments
#     owner=<o> repo=<r> pr=<n> reasons=<csv>
# Both keys are absent (never false) when every read succeeded, so the
# complete output stays byte-identical to the H4 parity fixtures. A read
# that succeeds with empty stdout is not flagged. Response bodies and gh
# stderr are never logged.
#
# Manual fixture capture (H4 parity):
#   source scripts/gh_helpers.sh
#   _gh_pr_with_all_comments_rest OWNER REPO PR_NUMBER \
#     > scripts/fixtures/issue-timeline/rest_pr_with_comments_fixture.json
#   gh_pr_with_all_comments OWNER REPO PR_NUMBER \
#     > scripts/fixtures/issue-timeline/graphql_pr_with_comments_fixture.json
# ---------------------------------------------------------------
_gh_pr_with_all_comments_rest()
{
	local owner="$1"
	local repo="$2"
	local pr_number="$3"
	local preloaded_meta_json="${4:-}"
	local repo_path="${owner}/${repo}"

	local meta_json comments_json review_comments_json
	# BUG-002: capture each gh_retry status on its own instead of through the
	# `gh_retry ... | jq -s 'add // []' || echo '[]'` pipeline. Without
	# pipefail a failed read printed nothing, jq turned the empty stdin into
	# [] with exit 0, and the failure looked like a PR with no comments. The
	# `x="$(...)" || rc=$?` / `if ...; then` forms keep callers under set -e
	# alive; printf is a builtin, so large bodies never reach argv (#7658).
	local _prc_raw="" _prc_rc=0 _prc_reasons=""
	if [ -n "${preloaded_meta_json}" ] && echo "${preloaded_meta_json}" | jq -e 'type == "object"' >/dev/null 2>&1; then
		meta_json="$(echo "${preloaded_meta_json}" | jq -c '{
			title: (.title // ""),
			body: (.body // ""),
			head_ref: (.head_ref // .head.ref // .headRefName // ""),
			base_ref: (.base_ref // .base.ref // .baseRefName // ""),
			head_sha: (.head_sha // .head.sha // .headSha // "")
		}' 2>/dev/null || echo '{}')"
	else
		_prc_rc=0
		_prc_raw="$(gh_retry gh api "repos/${repo_path}/pulls/${pr_number}" 2>/dev/null)" || _prc_rc=$?
		if [ "${_prc_rc}" -eq 0 ] && meta_json="$(printf '%s' "${_prc_raw}" \
			| jq -c '{title: .title, body: .body, head_ref: .head.ref, base_ref: .base.ref, head_sha: .head.sha}' 2>/dev/null)"; then
			:
		else
			meta_json='{}'
			_prc_reasons="${_prc_reasons:+${_prc_reasons},}pr_meta_read_failed"
		fi
		_prc_raw=""
	fi
	_prc_rc=0
	_prc_raw="$(gh_retry gh api --paginate "repos/${repo_path}/issues/${pr_number}/comments" 2>/dev/null)" || _prc_rc=$?
	if [ "${_prc_rc}" -eq 0 ] && comments_json="$(printf '%s' "${_prc_raw}" \
		| jq -c -s 'add // [] | [.[] | {author: .user.login, body: .body, created_at: .created_at}] | sort_by((.created_at // ""), (.author // ""), (.body // ""))' 2>/dev/null)"; then
		:
	else
		comments_json='[]'
		_prc_reasons="${_prc_reasons:+${_prc_reasons},}issue_comments_read_failed"
	fi
	_prc_raw=""
	_prc_rc=0
	_prc_raw="$(gh_retry gh api --paginate "repos/${repo_path}/pulls/${pr_number}/comments" 2>/dev/null)" || _prc_rc=$?
	if [ "${_prc_rc}" -eq 0 ] && review_comments_json="$(printf '%s' "${_prc_raw}" \
		| jq -c -s 'add // [] | [.[] | {author: .user.login, path: .path, line: .line, body: .body}] | sort_by((.path // ""), (.line // 0), (.author // ""), (.body // ""))' 2>/dev/null)"; then
		:
	else
		review_comments_json='[]'
		_prc_reasons="${_prc_reasons:+${_prc_reasons},}review_comments_read_failed"
	fi
	_prc_raw=""

	if [ -n "${_prc_reasons}" ]; then
		echo "::warning::PR_COMMENTS_CONTEXT_INCOMPLETE helper=gh_pr_with_all_comments owner=${owner} repo=${repo} pr=${pr_number} reasons=${_prc_reasons}" >&2
	fi

	# Pass the JSON through process substitution, never through argv: Linux
	# caps a single argument at 131,072 bytes (MAX_ARG_STRLEN), and a PR's
	# issue comments can serialize past that, so `--argjson` made jq exit 126
	# ("Argument list too long") with no output (issue #7658;
	# tele-funtoken-msg-scoring PR #3933, run 39131897079). An empty input
	# degrades to {} / []. The reason CSV holds only fixed tokens (< 100
	# bytes), so --arg is safe; jq object `+` keeps key order, so the output
	# is byte-identical to the pre-BUG-002 shape when nothing failed.
	jq -cn \
		--slurpfile meta <(printf '%s' "${meta_json}") \
		--slurpfile comments <(printf '%s' "${comments_json}") \
		--slurpfile review_comments <(printf '%s' "${review_comments_json}") \
		--arg incomplete_reasons_csv "${_prc_reasons}" \
		'{meta: ($meta[0] // {}), comments: ($comments[0] // []), review_comments: ($review_comments[0] // [])}
		+ (if $incomplete_reasons_csv == "" then {} else {comments_incomplete: true, incomplete_reasons: ($incomplete_reasons_csv | split(","))} end)'
}

# ---------------------------------------------------------------
# gh_pr_with_all_comments — GraphQL-first consolidated PR context.
#
# Inputs:
#   gh_pr_with_all_comments <owner> <repo> <pr_number> [preloaded_meta_json]
#
# Optional:
#   preloaded_meta_json — JSON object with pre-fetched PR metadata.
#   Accepts either legacy flat keys (`headRefName`/`baseRefName`) or
#   normalized keys (`head_ref`/`base_ref`/`head_sha`).
#
# Emits JSON object:
# {
#   "meta": {"title", "body", "head_ref", "base_ref", "head_sha"},
#   "comments": [{"author", "body", "created_at"}],
#   "review_comments": [{"author", "path", "line", "body"}]
# }
#
# Mandatory fail-open fallback to REST parity path when:
# - GraphQL request fails after retry budget
# - GraphQL payload has errors or transform fails
# - Any pagination boundary is hit (`hasNextPage=true` for PR comments,
#   reviews, or nested review comments)
#
# All fallback paths emit:
#   ::warning::rate_limit_audit_fallback helper=gh_pr_with_all_comments ...
#
# On the REST fallback only, a failed metadata / issue-comments /
# review-comments read appends "comments_incomplete": true and
# "incomplete_reasons": [...] and logs PR_COMMENTS_CONTEXT_INCOMPLETE (see
# _gh_pr_with_all_comments_rest). The keys are absent, never false, when
# the context is complete; the GraphQL success path never emits them.
# ---------------------------------------------------------------
gh_pr_with_all_comments()
{
	local owner="$1"
	local repo="$2"
	local pr_number="$3"
	local preloaded_meta_json="${4:-}"

	local _fallback_reason=""
	local _gql_file
	local goto_fallback=0
	if ! _gql_file=$(mktemp "${TMPDIR:-/tmp}/gh_pr_with_all_comments.XXXXXX" 2>/dev/null); then
		_fallback_reason="mktemp_failed"
		goto_fallback=1
	fi

	if [ "${goto_fallback:-0}" -eq 0 ]; then
		local gql_query
		gql_query='query($owner: String!, $name: String!, $number: Int!) {
		repository(owner: $owner, name: $name) {
			pullRequest(number: $number) {
				title
				body
				headRefName
				baseRefName
				headRefOid
				comments(first: 100) {
					nodes {
						author { login }
						body
						createdAt
					}
					pageInfo { hasNextPage }
				}
				reviews(first: 50) {
					nodes {
						comments(first: 100) {
							nodes {
								author { login }
								path
								line
								body
							}
							pageInfo { hasNextPage }
						}
					}
					pageInfo { hasNextPage }
				}
			}
		}
	}'

		if ! gh_api_json_to_file "${_gql_file}" \
			gh api graphql \
			-f query="${gql_query}" \
			-F owner="${owner}" \
			-F name="${repo}" \
			-F number="${pr_number}"; then
			_fallback_reason="graphql_request_failed"
			goto_fallback=1
		fi
	fi

	if [ "${goto_fallback:-0}" -eq 0 ]; then
		if ! jq -e '.errors | not or length == 0' "${_gql_file}" >/dev/null 2>&1; then
			_fallback_reason="graphql_errors"
			goto_fallback=1
		fi
	fi

	if [ "${goto_fallback:-0}" -eq 0 ]; then
		if ! jq -e '.data.repository.pullRequest != null' "${_gql_file}" >/dev/null 2>&1; then
			_fallback_reason="graphql_missing_pr"
			goto_fallback=1
		fi
	fi

	if [ "${goto_fallback:-0}" -eq 0 ]; then
		local has_next
		has_next="$(jq -r '
			.data.repository.pullRequest as $pr
			| (
				($pr.comments.pageInfo.hasNextPage // false)
				or ($pr.reviews.pageInfo.hasNextPage // false)
				or ([$pr.reviews.nodes[]?.comments.pageInfo.hasNextPage // false] | any)
			)
		' "${_gql_file}" 2>/dev/null || echo 'true')"
		if [ "${has_next}" = "true" ]; then
			_fallback_reason="graphql_has_next_page"
			goto_fallback=1
		fi
	fi

	if [ "${goto_fallback:-0}" -eq 0 ]; then
		if jq -c '
			.data.repository.pullRequest as $pr
			| {
				meta: {
					title: ($pr.title // ""),
					body: ($pr.body // ""),
					head_ref: ($pr.headRefName // ""),
					base_ref: ($pr.baseRefName // ""),
					head_sha: ($pr.headRefOid // "")
				},
				comments: (
					[
						($pr.comments.nodes // [])[]
						| {
							author: (.author.login // null),
							body: (.body // ""),
							created_at: (.createdAt // null)
						}
					]
					| sort_by((.created_at // ""), (.author // ""), (.body // ""))
				),
				review_comments: (
					[
						($pr.reviews.nodes // [])[]
						| (.comments.nodes // [])[]
						| {
							author: (.author.login // null),
							path: (.path // null),
							line: (.line // null),
							body: (.body // "")
						}
					]
					| sort_by((.path // ""), (.line // 0), (.author // ""), (.body // ""))
				)
			}
		' "${_gql_file}"; then
			rm -f "${_gql_file}"
			return 0
		fi
		_fallback_reason="graphql_transform_failed"
		goto_fallback=1
	fi

	rm -f "${_gql_file:-}"
	echo "::warning::rate_limit_audit_fallback helper=gh_pr_with_all_comments reason=${_fallback_reason:-unknown} owner=${owner} repo=${repo} pr=${pr_number}" >&2
	_gh_pr_with_all_comments_rest "${owner}" "${repo}" "${pr_number}" "${preloaded_meta_json}"
}

_gh_issue_timeline_with_cross_refs_rest()
{
	local owner="$1"
	local repo="$2"
	local issue_number="$3"
	local timeline_json
	local pr_urls
	local pr_url
	local pr_json
	local pr_lookup_json='{}'
	local github_api_base="${GITHUB_API_URL:-https://api.github.com}"
	github_api_base="${github_api_base%/}"
	local pr_api_prefix="${github_api_base}/repos/${owner}/${repo}/pulls/"

	if ! timeline_json="$(gh_retry gh api --paginate "repos/${owner}/${repo}/issues/${issue_number}/timeline" 2>/dev/null | jq -s 'add // []' 2>/dev/null)"; then
		return 1
	fi

	pr_urls="$(printf '%s' "${timeline_json}" | jq -r '[.[] | select(.event == "cross-referenced" and (.source.issue.pull_request.url? | type == "string")) | .source.issue.pull_request.url] | unique | .[]?' 2>/dev/null || true)"
	if [ -n "${pr_urls}" ]; then
		while IFS= read -r pr_url; do
			[ -n "${pr_url}" ] || continue
			if [[ "${pr_url}" != "${pr_api_prefix}"* ]]; then
				continue
			fi
			# The full PR object (body up to 65,536 characters, escaped) can pass
			# the 131,072-byte per-argument limit, so jq reads it with
			# --slurpfile, never from argv (issue #7658).
			if pr_json="$(gh_retry gh api "${pr_url}" 2>/dev/null)" && printf '%s' "${pr_json}" | jq -e 'type == "object"' >/dev/null 2>&1; then
				pr_lookup_json="$(jq -c --arg url "${pr_url}" --slurpfile pr <(printf '%s' "${pr_json}") '($pr[0] // {}) as $pr | . + {($url): {ok: true, number: ($pr.number // null), state: ($pr.state // null), merged_at: ($pr.merged_at // null), merged: (($pr.merged_at != null) or ($pr.merged == true))}}' <(printf '%s\n' "${pr_lookup_json}") 2>/dev/null || printf '%s' "${pr_lookup_json}")"
			else
				pr_lookup_json="$(jq -c --arg url "${pr_url}" '. + {($url): {ok: false}}' <(printf '%s\n' "${pr_lookup_json}") 2>/dev/null || printf '%s' "${pr_lookup_json}")"
			fi
		done <<< "${pr_urls}"
	fi

	# pr_lookup_json grows by one entry per cross-referenced PR with no cap,
	# so jq reads it with --slurpfile, never from argv (issue #7658).
	printf '%s' "${timeline_json}" | jq -c --slurpfile pr_lookup <(printf '%s' "${pr_lookup_json}") --arg pr_api_prefix "${pr_api_prefix}" '
		($pr_lookup[0] // {}) as $pr_lookup
		| map(
			if (.event == "cross-referenced") and (.source.issue.pull_request.url? | type == "string") then
				.source.issue.pull_request.url as $url
				| if ($url | startswith($pr_api_prefix) | not) then
					.source.issue.pull_request = null
				else
					($pr_lookup[$url] // null) as $enrich
					| if ($enrich != null) and ($enrich.ok == true) then
						.source.issue |= (. + {
							number: ($enrich.number // .number // null),
							state: ($enrich.state // null),
							merged_at: ($enrich.merged_at // null),
							merged: ($enrich.merged // false),
							lookup_failed: false
						})
					elif ($enrich != null) and ($enrich.ok == false) then
						.source.issue |= (. + {
							merged: false,
							lookup_failed: true
						})
					else
						.
					end
				end
			else
				.
			end
		)
		| map(select((.event == "cross-referenced") or (.event == "closed")))
	' 2>/dev/null
}

# gh_issue_timeline_with_cross_refs emits the legacy timeline-event shape used
# across jq consumers in scripts/orchestrate_poll_process.sh.
#
# Contract (array of event objects):
# - `.event` (e.g. "cross-referenced", "closed")
# - `.source.issue.number`
# - `.source.issue.pull_request.url` (REST API URL when source is a PR, else null)
# - additive PR enrichment fields for merged checks:
#   `.source.issue.state`, `.source.issue.merged_at`, `.source.issue.merged`,
#   `.source.issue.lookup_failed`
#
# Maintainer fixture capture (manual, replace OWNER/REPO/ISSUE):
# - REST helper output (legacy enriched shape): . scripts/gh_helpers.sh && _gh_issue_timeline_with_cross_refs_rest OWNER REPO ISSUE > scripts/fixtures/issue-timeline/rest_timeline_fixture.json
# - GraphQL helper output (GraphQL-first, fail-open to REST): . scripts/gh_helpers.sh && gh_issue_timeline_with_cross_refs OWNER REPO ISSUE > scripts/fixtures/issue-timeline/graphql_timeline_fixture.json
gh_issue_timeline_with_cross_refs()
{
	local owner="$1"
	local repo="$2"
	local issue_number="$3"
	local graphql_query
	local graphql_json
	local has_next_page
	local transformed_json
	local graphql_api_base="${GITHUB_API_URL:-https://api.github.com}"
	graphql_api_base="${graphql_api_base%/}"

	graphql_query='query($owner: String!, $repo: String!, $issue_number: Int!) {
  repository(owner: $owner, name: $repo) {
    issue(number: $issue_number) {
      timelineItems(first: 100, itemTypes: [CROSS_REFERENCED_EVENT, CLOSED_EVENT]) {
        pageInfo {
          hasNextPage
        }
        nodes {
          __typename
          ... on CrossReferencedEvent {
            source {
              __typename
              ... on PullRequest {
                number
                state
                mergedAt
                repository {
                  name
                  owner {
                    login
                  }
                }
              }
              ... on Issue {
                number
              }
            }
          }
        }
      }
    }
  }
}'

	if ! graphql_json="$(gh_retry gh api graphql -f query="${graphql_query}" -f owner="${owner}" -f repo="${repo}" -F issue_number="${issue_number}" 2>/dev/null)"; then
		echo "::warning::rate_limit_audit_fallback helper=gh_issue_timeline_with_cross_refs reason=graphql_failed owner=${owner} repo=${repo} issue=${issue_number}" >&2
		_gh_issue_timeline_with_cross_refs_rest "${owner}" "${repo}" "${issue_number}"
		return $?
	fi

	if ! printf '%s' "${graphql_json}" | jq -e '.data.repository.issue.timelineItems.nodes | type == "array"' >/dev/null 2>&1; then
		echo "::warning::rate_limit_audit_fallback helper=gh_issue_timeline_with_cross_refs reason=graphql_payload_invalid owner=${owner} repo=${repo} issue=${issue_number}" >&2
		_gh_issue_timeline_with_cross_refs_rest "${owner}" "${repo}" "${issue_number}"
		return $?
	fi

	if printf '%s' "${graphql_json}" | jq -e '.errors? | (type == "array" and length > 0)' >/dev/null 2>&1; then
		echo "::warning::rate_limit_audit_fallback helper=gh_issue_timeline_with_cross_refs reason=graphql_errors owner=${owner} repo=${repo} issue=${issue_number}" >&2
		_gh_issue_timeline_with_cross_refs_rest "${owner}" "${repo}" "${issue_number}"
		return $?
	fi

	has_next_page="$(printf '%s' "${graphql_json}" | jq -r '.data.repository.issue.timelineItems.pageInfo.hasNextPage // false' 2>/dev/null || echo "true")"
	if [ "${has_next_page}" = "true" ]; then
		echo "::warning::rate_limit_audit_fallback helper=gh_issue_timeline_with_cross_refs reason=timeline_has_next_page owner=${owner} repo=${repo} issue=${issue_number}" >&2
		_gh_issue_timeline_with_cross_refs_rest "${owner}" "${repo}" "${issue_number}"
		return $?
	fi

	if ! transformed_json="$(printf '%s' "${graphql_json}" | jq -c --arg api_base "${graphql_api_base}" --arg owner "${owner}" --arg repo "${repo}" '
		def source_issue($source):
			if ($source | type) != "object" then
				{
					number: null,
					pull_request: null,
					state: null,
					merged_at: null,
					merged: false,
					lookup_failed: false
				}
			elif $source.__typename == "PullRequest" then
				{
					number: ($source.number // null),
					pull_request: (
					if ($source.number != null)
						and ($source.repository.owner.login? != null)
						and ($source.repository.name? != null)
						and (($source.repository.owner.login | ascii_downcase) == ($owner | ascii_downcase))
						and (($source.repository.name | ascii_downcase) == ($repo | ascii_downcase)) then
						{url: ($api_base + "/repos/" + $owner + "/" + $repo + "/pulls/" + ($source.number | tostring))}
					else
							null
						end
					),
					state: (
						if ($source.state // null) == "OPEN" then "open"
						elif (($source.state // null) == "CLOSED") or (($source.state // null) == "MERGED") then "closed"
						else null
						end
					),
					merged_at: ($source.mergedAt // null),
					merged: (($source.mergedAt != null) or (($source.state // "") == "MERGED")),
					lookup_failed: false
				}
			else
				{
					number: ($source.number // null),
					pull_request: null,
					state: null,
					merged_at: null,
					merged: false,
					lookup_failed: false
				}
			end;

		[
			.data.repository.issue.timelineItems.nodes[]?
			| if .__typename == "CrossReferencedEvent" then
				{
					event: "cross-referenced",
					source: {
						issue: source_issue(.source)
					}
				}
			  elif .__typename == "ClosedEvent" then
				{event: "closed"}
			  else
				empty
			  end
		]
	' 2>/dev/null)"; then
		echo "::warning::rate_limit_audit_fallback helper=gh_issue_timeline_with_cross_refs reason=graphql_transform_failed owner=${owner} repo=${repo} issue=${issue_number}" >&2
		_gh_issue_timeline_with_cross_refs_rest "${owner}" "${repo}" "${issue_number}"
		return $?
	fi

	printf '%s\n' "${transformed_json}"
}

# ---------------------------------------------------------------
# _autofix_pr_named_review_runs — list the review wrapper runs that were
# dispatched for one PR.
#
# Motivation (issue #4898): the review dispatches now run from the default
# branch (review_autofix.yml's two retrigger steps, the sweep since #4618,
# the poller and merge train since #4701), so their head_branch and head_sha
# are the default branch's and no branch-scoped lookup can see them. The
# review wrappers name every workflow_dispatch run for its PR:
#   internal-review.yml (this repo):  "Internal: AI Review & Autofix [pr:<N>]"
#   ai-review.yml (consumer repos):   "AI Review [pr:<N>]"
# A workflow_dispatch run's name comes from the dispatched ref's workflow
# file, never from PR text, so an exact name match identifies the PR. This
# is the same match as _pr_named_review_dispatch_runs in
# scripts/orchestrate_poll_process.sh, which review_autofix.yml does not
# source; this one uses REST like its two callers below.
#
# Input:
#   $1 pr_number — must match ^[1-9][0-9]*$
#   $2 status    — optional REST status filter (e.g. "completed")
#   $3 since_epoch — optional trusted GitHub run creation time; narrows
#                    the budget listing to this head's push window
#
# Output (stdout): one JSON array of the matching runs:
#   [{id, status, conclusion, created_at, path}].
#
# Return: 0 on success (possibly []); 1 on an invalid PR number (no call),
#   an API error, an empty response, or unparseable JSON. Each caller
#   decides whether a failure fails open or closed.
#
# API calls: one repo metadata read (default branch) and paged GETs for
#   internal-review.yml and ai-review.yml, only after a branch lookup miss.
#   The branch-filtered list cannot contain these default-branch dispatches;
#   an unfiltered first page can omit them behind unrelated workflows. A
#   budget lookup adds a created>= filter from the head's trusted run time
#   so historical review runs cannot exhaust the page budget unnecessarily.
#   The peer lookup leaves time unfiltered so even an old active run counts.
#   A missing wrapper (HTTP 404) contributes no runs; any other read/parse or
#   pagination-cap failure returns 1, so callers retain their own fail-open
#   (peer) / fail-closed (budget) semantics. Input and output are arrays of
#   Actions run records; at most 10 pages per wrapper, no partial output.
# ---------------------------------------------------------------
# Per-shell memo of the default branch for _autofix_pr_named_review_runs
# (issue #6629: read it at most once per run). Both callers below run in the
# step shell, but they call _autofix_pr_named_review_runs inside $(...), so a
# variable set there would be lost. They call
# _autofix_review_default_branch_prime in their own shell first. A failed
# read is memoized too, so the second caller fails the same way without
# another call. The memo is never initialised from the environment.
# Input: none (GITHUB_REPOSITORY). Output: none; always returns 0.
# API calls: one `GET repos/<repo>` per shell and repository.
_AUTOFIX_REVIEW_DEFAULT_BRANCH=""
_AUTOFIX_REVIEW_DEFAULT_BRANCH_STATE=""
_AUTOFIX_REVIEW_DEFAULT_BRANCH_REPO=""
_autofix_review_default_branch_prime()
{
	local prime_value=""
	[ -n "${GITHUB_REPOSITORY:-}" ] || return 0
	if [ "${_AUTOFIX_REVIEW_DEFAULT_BRANCH_REPO:-}" = "${GITHUB_REPOSITORY}" ] \
		&& [ -n "${_AUTOFIX_REVIEW_DEFAULT_BRANCH_STATE:-}" ]; then
		return 0
	fi
	prime_value=$(gh_retry gh api -X GET "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' 2>/dev/null) || prime_value=""
	_AUTOFIX_REVIEW_DEFAULT_BRANCH_REPO="${GITHUB_REPOSITORY}"
	if [[ "${prime_value}" =~ ^[A-Za-z0-9._/-]+$ ]] && [ "${prime_value}" != "null" ]; then
		_AUTOFIX_REVIEW_DEFAULT_BRANCH="${prime_value}"
		_AUTOFIX_REVIEW_DEFAULT_BRANCH_STATE="ok"
	else
		_AUTOFIX_REVIEW_DEFAULT_BRANCH=""
		_AUTOFIX_REVIEW_DEFAULT_BRANCH_STATE="failed"
	fi
	return 0
}

_autofix_pr_named_review_runs()
{
	local pr_number="${1:-}"
	local status_filter="${2:-}"
	local review_since_epoch="${3:-}"

	if ! [[ "${pr_number}" =~ ^[1-9][0-9]*$ ]] || [ -z "${GITHUB_REPOSITORY:-}" ]; then
		return 1
	fi
	local -a review_status_args=()
	if [ -n "${status_filter}" ]; then
		review_status_args=(-f "status=${status_filter}")
	fi
	local -a review_created_args=()
	if [ -n "${review_since_epoch}" ]; then
		[[ "${review_since_epoch}" =~ ^[0-9]+$ ]] || return 1
		local review_since_iso
		review_since_iso=$(jq -nr --argjson t "${review_since_epoch}" '$t | todate' 2>/dev/null) || return 1
		[[ "${review_since_iso}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] || return 1
		review_created_args=(-f "created=>=${review_since_iso}")
	fi

	# Neither branch-filtered call can provide the repository's default
	# branch. Read it once here instead of trusting a caller-supplied ref.
	# The two callers prime a per-shell memo first (issue #6629: at most one
	# read per run); without a memo for this repository, read it here.
	local review_default_branch
	if [ "${_AUTOFIX_REVIEW_DEFAULT_BRANCH_REPO:-}" = "${GITHUB_REPOSITORY}" ] \
		&& [ "${_AUTOFIX_REVIEW_DEFAULT_BRANCH_STATE:-}" = "failed" ]; then
		return 1
	elif [ "${_AUTOFIX_REVIEW_DEFAULT_BRANCH_REPO:-}" = "${GITHUB_REPOSITORY}" ] \
		&& [ "${_AUTOFIX_REVIEW_DEFAULT_BRANCH_STATE:-}" = "ok" ]; then
		review_default_branch="${_AUTOFIX_REVIEW_DEFAULT_BRANCH:-}"
	else
		review_default_branch=$(gh_retry gh api -X GET "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' 2>/dev/null) || return 1
	fi
	[[ "${review_default_branch}" =~ ^[A-Za-z0-9._/-]+$ ]] || return 1
	# A response without .default_branch prints the literal "null", which
	# passes the regex; fail closed like the poller's resolver (issue #6629).
	[ "${review_default_branch}" != "null" ] || return 1
	local review_wrapper review_page review_response review_total review_count review_page_count
	local review_error_file review_all='[]' review_wrapper_runs
	review_error_file=$(mktemp "${TMPDIR:-/tmp}/autofix_pr_named_runs.XXXXXX") || return 1
	for review_wrapper in internal-review.yml ai-review.yml; do
		review_page=1
		review_wrapper_runs='[]'
		while :; do
			[ "${review_page}" -le 10 ] || { rm -f "${review_error_file}"; return 1; }
			# -X GET is required: -f parameters otherwise make gh api POST.
			if ! review_response=$(gh_retry gh api -X GET \
				-H "Accept: application/vnd.github+json" \
				"/repos/${GITHUB_REPOSITORY}/actions/workflows/${review_wrapper}/runs" \
				-f "event=workflow_dispatch" -f "per_page=100" -f "page=${review_page}" \
				"${review_status_args[@]}" "${review_created_args[@]}" 2>"${review_error_file}"); then
				if [ "${review_page}" -eq 1 ] && grep -q 'HTTP 404' "${review_error_file}"; then
					break
				fi
				rm -f "${review_error_file}"
				return 1
			fi
			if ! printf '%s' "${review_response}" | jq -es \
				'length == 1 and (.[0] | (.total_count | type == "number" and . >= 0 and . == floor) and (.workflow_runs | type == "array" and length <= 100 and all(.[]; type == "object" and (.id | type == "number"))))' >/dev/null 2>&1; then
				rm -f "${review_error_file}"
				return 1
			fi
			review_total=$(printf '%s' "${review_response}" | jq -r '.total_count')
			review_page_count=$(printf '%s' "${review_response}" | jq -r '.workflow_runs | length')
			# Append only after a complete page. Never let a partial listing
			# suppress a needed review or open an unbounded retry loop.
			review_wrapper_runs=$(printf '%s\n%s\n' "${review_wrapper_runs}" "${review_response}" \
				| jq -cs '.[0] + .[1].workflow_runs | unique_by(.id)') || { rm -f "${review_error_file}"; return 1; }
			review_count=$(printf '%s' "${review_wrapper_runs}" | jq -r 'length')
			if [ "${review_count}" -gt "${review_total}" ]; then
				rm -f "${review_error_file}"
				return 1
			fi
			if [ "${review_count}" -ge "${review_total}" ]; then
				break
			fi
			if [ "${review_page_count}" -lt 100 ]; then
				rm -f "${review_error_file}"
				return 1
			fi
			review_page=$(( review_page + 1 ))
		done
		review_all=$(printf '%s\n%s\n' "${review_all}" "${review_wrapper_runs}" | jq -cs '.[0] + .[1]') \
			|| { rm -f "${review_error_file}"; return 1; }
	done
	rm -f "${review_error_file}"
	printf '%s' "${review_all}" | jq -c --arg pr "${pr_number}" --arg branch "${review_default_branch}" --arg repo "${GITHUB_REPOSITORY}" '
		# The wrapper path, with an "@<ref>" suffix and a leading
		# "<this repo>/" prefix removed (same rule as the poller, issue #6629).
		def review_path:
			((.path // "") | if type == "string" then . else "" end | sub("@.*$"; "")) as $p
			| if ($p | ascii_downcase | startswith(($repo | ascii_downcase) + "/"))
				then $p[(($repo | length) + 1):]
				else $p
				end;
		[.[]
		 | select(type == "object")
		 # A null/empty head_branch is accepted (issue #6629, Q2): GitHub can
		 # report one on a real default-branch dispatch (#4928), while a
		 # branch-copy spoof always reports its own non-empty branch.
		 | select(.event == "workflow_dispatch" and (((.head_branch // "") == "") or .head_branch == $branch))
		 | select((review_path == ".github/workflows/internal-review.yml" and .display_title == ("Internal: AI Review & Autofix [pr:" + $pr + "]"))
		     or (review_path == ".github/workflows/ai-review.yml" and .display_title == ("AI Review [pr:" + $pr + "]")))
		 | {id, status, conclusion, created_at, path}]
	' 2>/dev/null
}

# One-shot snapshot of the branch `actions/runs` page (MERGE-001).
# Process-local: the assignments below reset any inherited value when this
# file is sourced, so the environment can never supply a snapshot. Written
# only by autofix_retrigger_has_inflight_peer after its response passed the
# empty and shape checks; read at most once, and then cleared, by the next
# autofix_changes_lost_head_retry_consumed call in the same shell. The key
# is "<owner/repo>:<branch>"; neither part can contain ':'.
_AUTOFIX_BRANCH_RUNS_SNAPSHOT_KEY=""
_AUTOFIX_BRANCH_RUNS_SNAPSHOT_JSON=""

# ---------------------------------------------------------------
# autofix_retrigger_has_inflight_peer — detect an already-queued or
# already-running peer review/autofix run on the same PR head branch,
# so the caller can skip an otherwise-redundant workflow_dispatch.
#
# Motivation:
#   review_autofix.yml finishes a commit-and-push cycle and then
#   dispatches the next review run from the default branch (the
#   PR-named wrapper first, issue #4898) as a fallback in case the
#   merge-ref for the synchronize event is unbuildable.
#   In the common case the push already fires pull_request.synchronize
#   → internal-review.yml → review_autofix.yml, so both runs land in
#   the same `pr-autofix-${PR}` concurrency group with
#   cancel-in-progress: false. A redundant dispatch can still queue,
#   surface in the Actions UI, and consume a workflow_dispatch API
#   call. This helper lets the
#   retrigger step skip its dispatch when a peer run is already
#   in flight.
#
# Input:
#   $1 pr_number      — PR number: used in log lines, and must match
#                       ^[1-9][0-9]*$ for the PR-named lookup below
#   $2 head_branch    — PR head branch name (required for filtering)
#   $3 current_run_id — github.run_id of the CURRENT run, so we can
#                       exclude ourselves from the peer check.
#
# Output (stdout):
#   AUTOFIX_PEER_CHECK pr=<n> branch=<b> current_run=<r> \
#     peer_count=<n> peer_run=<id|-> peer_path=<path|->
#   An AUTOFIX_PEER_QUERY_FAILED line is emitted on API error
#   (stderr) so callers can distinguish a true no-peer result from a
#   probe failure when grepping logs.
#
# Return:
#   0 — at least one in-flight peer found; caller SHOULD skip dispatch
#   1 — no peer found, or the check failed; caller SHOULD proceed
#       (fail-open: never block dispatch on a detection failure)
#
# PR-named runs (issue #4898):
#   Review runs dispatched from the default branch (both retrigger steps,
#   the sweep, the poller, the merge train) have the default branch as
#   head_branch, so the branch lookup cannot see them. When it finds no
#   peer and $1 is a valid PR number, the helper also counts queued or
#   running workflow_dispatch runs named for the PR
#   (_autofix_pr_named_review_runs), excluding the current run. The
#   AUTOFIX_PEER_CHECK line keeps its exact field set (a pinned log
#   contract); a PR-named peer shows up in peer_run / peer_path.
#
# API calls:
#   1 `gh api GET /repos/{repo}/actions/runs` call per invocation,
#   wrapped in gh_retry (rate-limit aware), plus 1 PR-named
#   `?event=workflow_dispatch` call only when the branch lookup found no
#   peer.  The helper itself always makes its live branch call and
#   clears any earlier snapshot first, because the set of in-flight runs
#   is mutable between calls.  Once the branch response passes the empty
#   and shape checks it is kept as a process-local one-shot snapshot
#   keyed by repository and branch (_AUTOFIX_BRANCH_RUNS_SNAPSHOT_*).
#   Its only reader is the next autofix_changes_lost_head_retry_consumed
#   call in the same shell, so the editor-changes-lost step's no-peer
#   path makes 1 branch `actions/runs` call instead of 2.  A caller that
#   runs this helper inside `$(...)` loses the snapshot, and the budget
#   probe then makes its own live call.  The PR-named lookup is never
#   cached.
#
# CLAUDE.md §15 audit:
#   The prior retrigger path dispatched unconditionally, so there is
#   no existing gh call here to extend.  A single list-runs call
#   replaces the wasted dispatch on the collision path, so net API
#   cost is negative when a peer is found and neutral otherwise.  The
#   branch-filtered call cannot return default-branch runs, and widening
#   it to an unfiltered page would drop older same-branch runs, so the
#   PR-named lookup is a second call on the no-peer path only.
# ---------------------------------------------------------------
autofix_retrigger_has_inflight_peer()
{
	local pr_number="${1:-}"
	local head_branch="${2:-}"
	local current_run_id="${3:-}"

	# Clear first: a failed or early-returning call must never leave an
	# older snapshot for the budget probe.
	_AUTOFIX_BRANCH_RUNS_SNAPSHOT_KEY=""
	_AUTOFIX_BRANCH_RUNS_SNAPSHOT_JSON=""

	if [ -z "${head_branch}" ] || [ -z "${current_run_id}" ] || [ -z "${GITHUB_REPOSITORY:-}" ]; then
		echo "AUTOFIX_PEER_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch:-?} reason=missing_inputs" >&2
		return 1
	fi

	local response
	# -X GET is REQUIRED, not decorative: `gh api` infers POST whenever
	# any -f/-F parameter is supplied and no method is given, and
	# POST /repos/{repo}/actions/runs is not a route — it 404s, which
	# _is_gh_permanent_failure classifies as non-retryable, so the
	# probe dies instantly with reason=api_error.  Dropping this flag
	# silently breaks the probe (tele-funtoken-msg-scoring#3763,
	# review run 32732281452).
	if ! response=$(gh_retry gh api \
		-X GET \
		-H "Accept: application/vnd.github+json" \
		"/repos/${GITHUB_REPOSITORY}/actions/runs" \
		-f "branch=${head_branch}" \
		-f "per_page=30" \
		2>/dev/null); then
		echo "AUTOFIX_PEER_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch} reason=api_error" >&2
		return 1
	fi

	if [ -z "${response}" ]; then
		echo "AUTOFIX_PEER_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch} reason=empty_response" >&2
		return 1
	fi
	if ! printf '%s' "${response}" | jq -es 'length == 1 and (.[0].workflow_runs | type == "array")' >/dev/null 2>&1; then
		echo "AUTOFIX_PEER_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch} reason=jq_error" >&2
		return 1
	fi
	# Validated: keep it for one read by autofix_changes_lost_head_retry_consumed.
	_AUTOFIX_BRANCH_RUNS_SNAPSHOT_KEY="${GITHUB_REPOSITORY}:${head_branch}"
	_AUTOFIX_BRANCH_RUNS_SNAPSHOT_JSON="${response}"

	# Filter: in-flight (queued or in_progress), not ourselves, and
	# running one of the review/autofix workflow files. Match by
	# workflow file path so renamed jobs in consumer repos still count
	# as peers. The first matched run in API order is surfaced in logs.
	local peer_info peer_zq_cutoff peer_zq_def
	# Issue #9473: a queued run older than ZOMBIE_RUN_MAX_QUEUED_HOURS is
	# wedged (zero jobs, cannot be cancelled) and is not a peer.
	peer_zq_cutoff="$(zombie_queued_cutoff_epoch 2>/dev/null || echo 0)"
	[[ "${peer_zq_cutoff}" =~ ^[0-9]+$ ]] || peer_zq_cutoff=0
	peer_zq_def="$(zombie_queued_jq_def 2>/dev/null || printf '%s' 'def is_wedged_queued($cutoff): false;')"
	if declare -F zombie_queued_log_ignored >/dev/null 2>&1; then
		# Log only runs this guard would otherwise count: review-family
		# workflow paths, never the current run.
		zombie_queued_log_ignored autofix_inflight_peer "${peer_zq_cutoff}" <<< "$(printf '%s' "${response}" | jq -c --arg current "${current_run_id}" '[.workflow_runs[]? | select(type == "object" and ((.id | tostring) != $current) and ((.path // "") | tostring | test("(^|/)(review_autofix|internal-review|ai-review)\\.ya?ml$")))]' 2>/dev/null || echo '[]')"
	fi
	if ! peer_info=$(printf '%s' "${response}" | jq -r --arg current "${current_run_id}" --argjson zq_cutoff "${peer_zq_cutoff}" "${peer_zq_def}"'
		[
			.workflow_runs[]?
			| select(.status == "queued" or .status == "pending" or .status == "in_progress")
			| select(is_wedged_queued($zq_cutoff) | not)
			| select((.id | tostring) != $current)
			| select(.path | test("(^|/)(review_autofix|internal-review|ai-review)\\.ya?ml$"))
		]
		| {count: length, first_id: (.[0].id // "-"), first_path: (.[0].path // "-")}
		| "\(.count) \(.first_id) \(.first_path)"
	' 2>/dev/null); then
		echo "AUTOFIX_PEER_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch} reason=jq_error" >&2
		return 1
	fi

	local peer_count peer_run peer_path
	peer_count=$(printf '%s' "${peer_info}" | awk '{print $1}')
	peer_run=$(printf '%s' "${peer_info}" | awk '{print $2}')
	peer_path=$(printf '%s' "${peer_info}" | awk '{print $3}')
	if ! [ "${peer_count:-0}" -gt 0 ] 2>/dev/null && [[ "${pr_number}" =~ ^[1-9][0-9]*$ ]]; then
		# Default-branch dispatch runs are invisible to the branch lookup
		# above (issue #4898); look for one named for this PR. Fail open.
		local pr_named_runs pr_named_info
		declare -F _autofix_review_default_branch_prime >/dev/null && _autofix_review_default_branch_prime
		if pr_named_runs=$(_autofix_pr_named_review_runs "${pr_number}") \
			&& { ! declare -F zombie_queued_log_ignored >/dev/null 2>&1 || zombie_queued_log_ignored autofix_inflight_peer "${peer_zq_cutoff}" <<< "${pr_named_runs}"; } \
			&& pr_named_info=$(printf '%s' "${pr_named_runs}" | jq -r --arg current "${current_run_id}" --argjson zq_cutoff "${peer_zq_cutoff}" "${peer_zq_def}"'
				[
					.[]
					| select(.status == "queued" or .status == "in_progress" or .status == "pending" or .status == "waiting" or .status == "requested")
					| select(is_wedged_queued($zq_cutoff) | not)
					| select((.id | tostring) != $current)
				]
				| {count: length, first_id: (.[0].id // "-"), first_path: (.[0].path // "-")}
				| "\(.count) \(.first_id) \(.first_path)"
			' 2>/dev/null); then
			peer_count=$(printf '%s' "${pr_named_info}" | awk '{print $1}')
			peer_run=$(printf '%s' "${pr_named_info}" | awk '{print $2}')
			peer_path=$(printf '%s' "${pr_named_info}" | awk '{print $3}')
		else
			echo "AUTOFIX_PEER_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch} reason=pr_named_api_error" >&2
		fi
	fi

	echo "AUTOFIX_PEER_CHECK pr=${pr_number:-?} branch=${head_branch} current_run=${current_run_id:-?} peer_count=${peer_count:-0} peer_run=${peer_run:--} peer_path=${peer_path:--}"
	emit_event "AUTOFIX_PEER_CHECK" \
		"pr=${pr_number:-?}" \
		"branch=${head_branch}" \
		"current_run=${current_run_id:-?}" \
		"peer_count=${peer_count:-0}" \
		"peer_run=${peer_run:--}" \
		"peer_path=${peer_path:--}"

	if [ "${peer_count:-0}" -gt 0 ] 2>/dev/null; then
		return 0
	fi
	return 1
}

# ---------------------------------------------------------------
# autofix_changes_lost_head_retry_consumed — decide whether the current
# PR head SHA has already consumed its one automated editor-changes-lost
# re-dispatch.
#
# Motivation:
#   The "Re-dispatch review on editor-changes-lost" step in
#   review_autofix.yml must be reachable from workflow_dispatch runs
#   (every autofix iteration after the first is one — the pull_request
#   twins are concurrency-cancelled), yet must not loop: a changes-lost
#   iteration produces NO commit, so the head SHA never advances and an
#   unbounded re-dispatch would spin forever on the same head.  The
#   bound that replaces the old `github.event.pull_request.number`
#   event-payload guard (unreachable on dispatch runs — see the
#   tele-funtoken-msg-scoring#3757 stall, run 32659591000) is the head
#   SHA itself: a completed, non-cancelled review run already recorded
#   on this exact head means this run IS the automated retry, so no
#   further dispatch is allowed.
#
# Input:
#   $1 pr_number      — PR number: used in log lines, and must match
#                       ^[1-9][0-9]*$ when the PR-named lookup below runs
#   $2 head_branch    — PR head branch name (required for filtering)
#   $3 current_run_id — github.run_id of the CURRENT run (excluded)
#   $4 head_sha       — the PR head commit this run reviewed (required)
#   $5 head_commit_epoch — optional, accepted and ignored: the head
#                       commit's committer time (`git log -1 --format=%ct`).
#                       The PR author sets it, so it is never used as the
#                       push-time bound (issue #5523); it stays in the
#                       signature so existing callers keep working
#   $6 event_name     — optional: the current run's event
#                       (GITHUB_EVENT_NAME, the caller's event inside a
#                       reusable workflow); see "Unnamed dispatch runs"
#
# PR-named runs (issue #4898):
#   The retry is dispatched from the default branch, so its head_sha is
#   the default branch's and the branch lookup never counts it. When the
#   branch lookup counts nothing, the helper also counts completed,
#   non-cancelled workflow_dispatch runs named for the PR
#   (_autofix_pr_named_review_runs), excluding the current run, that were
#   created at or after the head's push-time bound: the earliest
#   created_at of any run in the branch page whose head_sha is this head
#   (the push's pull_request run, cancelled twins included, and any other
#   workflow the push triggered). GitHub records both fields, so neither
#   can be set by the PR author. The head commit's committer time is not
#   used (issue #5523): a backdated commit would pull earlier heads'
#   reviews into this head's count and suppress its retry. No run on the
#   head, an invalid PR number, or a failed call fails closed.
#
# Unnamed dispatch runs (issue #4898, conformance audit):
#   A retry dispatched from the default branch under a name the PR-named
#   match cannot see (a differently named caller wrapper, review_autofix.yml
#   itself, or an ai-review.yml that predates the "[pr:<N>]" run name) is
#   invisible to both lookups, so the next retry would count nothing and
#   dispatch again without bound once the head's pull_request twin was
#   concurrency-cancelled. When $6 is workflow_dispatch and the current
#   run is not among the PR-named runs, the budget is consumed
#   (reason=unnamed_dispatch_run, fail closed): such a run never
#   dispatches an automated retry. The PR-named page is therefore fetched
#   without a status filter (still one call) and filtered to completed
#   runs locally, so the in-progress current run is in it. An empty $6
#   (a caller that predates it) skips this check; the budget line then
#   shows event=-, so a caller that drops the event is visible in the log.
#
# Output (stdout):
#   AUTOFIX_CHANGES_LOST_BUDGET pr=<n> branch=<b> head_sha=<sha> \
#     current_run=<r> prior_completed=<n> pr_named_completed=<n|-> \
#     event=<$6|->
#   An AUTOFIX_CHANGES_LOST_BUDGET_QUERY_FAILED line is emitted on
#   probe failure (stderr).
#
# Return:
#   0 — budget consumed (a prior completed non-cancelled review run
#       exists on this head, or the probe failed); caller MUST skip
#       the dispatch.  Fail-CLOSED, unlike the peer helper above: this
#       helper guards an otherwise-unbounded dispatch loop, so an
#       unanswerable probe keeps the pre-fix no-dispatch behaviour.
#   1 — budget available; caller may dispatch one automated retry.
#
# API calls:
#   0 branch `gh api GET /repos/{repo}/actions/runs` calls when the
#   peer helper left a snapshot for the same repository and branch in
#   this shell (it is consumed and cleared), else 1 live call wrapped in
#   gh_retry (§15: same single branch-scoped list the peer helper
#   issues; the two probes run back-to-back in one step at most once per
#   review run), plus 1 PR-named `?event=workflow_dispatch` call only
#   when the branch lookup counted nothing.  The snapshot is cleared on
#   entry whether or not it matches, so a second call or a call for
#   another branch never reads it.
#   Race reasoning: this probe counts *completed* runs.  The snapshot is
#   taken before the peer helper's in-flight filter, its PR-named
#   `?event=workflow_dispatch` lookup (one or more API calls on the
#   no-peer path) and its `emit_event`, and then the caller's
#   `git rev-parse` / `git log` run, so it is typically seconds old, not
#   milliseconds.  A run that starts and completes inside that window
#   is not counted, so this probe can allow one more retry than a fresh
#   call would.  A run in flight at snapshot time already made the peer
#   probe report a peer, and a live call has its own window too, so the
#   reuse only widens an existing race by those seconds.  A missing or mismatched
#   snapshot, or a failed or malformed peer response (never stored),
#   falls back to the live call; the empty and shape checks run on
#   whichever response is used, and every fail-closed return is kept.
#   The branch-filtered call cannot return default-branch runs, so no
#   existing call could carry the PR-named match.
# ---------------------------------------------------------------
autofix_changes_lost_head_retry_consumed()
{
	local pr_number="${1:-}"
	local head_branch="${2:-}"
	local current_run_id="${3:-}"
	local head_sha="${4:-}"
	# Accepted for caller compatibility and not used (issue #5523).
	local head_commit_epoch="${5:-}"
	local run_event_name="${6:-}"
	# One-shot: take the peer helper's snapshot and clear it whether or
	# not it matches, so no later call can read it.
	local branch_runs_snapshot_key="${_AUTOFIX_BRANCH_RUNS_SNAPSHOT_KEY:-}"
	local branch_runs_snapshot_json="${_AUTOFIX_BRANCH_RUNS_SNAPSHOT_JSON:-}"
	_AUTOFIX_BRANCH_RUNS_SNAPSHOT_KEY=""
	_AUTOFIX_BRANCH_RUNS_SNAPSHOT_JSON=""

	if [ -z "${head_branch}" ] || [ -z "${current_run_id}" ] || [ -z "${head_sha}" ] || [ -z "${GITHUB_REPOSITORY:-}" ]; then
		echo "AUTOFIX_CHANGES_LOST_BUDGET_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch:-?} reason=missing_inputs" >&2
		return 0
	fi

	local response
	if [ -n "${branch_runs_snapshot_json}" ] \
		&& [ "${branch_runs_snapshot_key}" = "${GITHUB_REPOSITORY}:${head_branch}" ]; then
		response="${branch_runs_snapshot_json}"
	else
		# -X GET is REQUIRED, not decorative: `gh api` infers POST whenever
		# any -f/-F parameter is supplied and no method is given, and
		# POST /repos/{repo}/actions/runs is not a route — it 404s, which
		# _is_gh_permanent_failure classifies as non-retryable, so the
		# probe dies instantly with reason=api_error.  Dropping this flag
		# silently breaks the probe (tele-funtoken-msg-scoring#3763,
		# review run 32732281452).
		if ! response=$(gh_retry gh api \
			-X GET \
			-H "Accept: application/vnd.github+json" \
			"/repos/${GITHUB_REPOSITORY}/actions/runs" \
			-f "branch=${head_branch}" \
			-f "per_page=30" \
			2>/dev/null); then
			echo "AUTOFIX_CHANGES_LOST_BUDGET_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch} reason=api_error" >&2
			return 0
		fi
	fi

	if [ -z "${response}" ]; then
		echo "AUTOFIX_CHANGES_LOST_BUDGET_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch} reason=empty_response" >&2
		return 0
	fi
	if ! printf '%s' "${response}" | jq -es 'length == 1 and (.[0].workflow_runs | type == "array")' >/dev/null 2>&1; then
		echo "AUTOFIX_CHANGES_LOST_BUDGET_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch} reason=jq_error" >&2
		return 0
	fi

	# Completed, non-cancelled review-family runs on this exact head,
	# excluding the current run.  A cancelled run is the concurrency-
	# cancelled pull_request twin of a dispatch run and never executed
	# the editor, so it does not consume the budget.
	local prior_completed
	if ! prior_completed=$(printf '%s' "${response}" | jq -r \
		--arg current "${current_run_id}" \
		--arg head "${head_sha}" '
		[
			.workflow_runs[]?
			| select(.status == "completed")
			| select((.conclusion // "") != "cancelled")
			| select((.id | tostring) != $current)
			| select((.head_sha // "") == $head)
			| select(.path | test("(^|/)(review_autofix|internal-review|ai-review)\\.ya?ml$"))
		]
		| length
	' 2>/dev/null); then
		echo "AUTOFIX_CHANGES_LOST_BUDGET_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch} reason=jq_error" >&2
		return 0
	fi

	local pr_named_completed="-"
	if ! [ "${prior_completed:-0}" -gt 0 ] 2>/dev/null; then
		# The retry runs from the default branch (issue #4898), so the
		# branch lookup above never sees it; count the completed runs named
		# for this PR since the head was pushed. Fail closed throughout.
		if ! [[ "${pr_number}" =~ ^[1-9][0-9]*$ ]]; then
			echo "AUTOFIX_CHANGES_LOST_BUDGET_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch} reason=invalid_pr_number" >&2
			return 0
		fi
		# Push-time bound: GitHub-recorded run times on this head only,
		# never the author-set commit time (issue #5523).
		local push_bound
		if ! push_bound=$(printf '%s' "${response}" | jq -r \
			--arg head "${head_sha}" '
			[
				.workflow_runs[]?
				| select((.head_sha // "") == $head)
				| (.created_at // "")
				| (try fromdateiso8601 catch empty)
			]
			| if length == 0 then "" else (min | floor | tostring) end
		' 2>/dev/null) || [ -z "${push_bound}" ]; then
			echo "AUTOFIX_CHANGES_LOST_BUDGET_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch} reason=missing_head_time" >&2
			return 0
		fi
		local pr_named_runs
		declare -F _autofix_review_default_branch_prime >/dev/null && _autofix_review_default_branch_prime
		if ! pr_named_runs=$(_autofix_pr_named_review_runs "${pr_number}" "" "${push_bound}"); then
			echo "AUTOFIX_CHANGES_LOST_BUDGET_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch} reason=pr_named_api_error" >&2
			return 0
		fi
		if [ "${run_event_name}" = "workflow_dispatch" ] \
			&& ! printf '%s' "${pr_named_runs}" | jq -e --arg current "${current_run_id}" \
				'any(.[]; (.id | tostring) == $current)' >/dev/null 2>&1; then
			# This dispatch run is not named for the PR, so a retry it
			# dispatched could never count it (see "Unnamed dispatch runs").
			echo "AUTOFIX_CHANGES_LOST_BUDGET_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch} reason=unnamed_dispatch_run" >&2
			return 0
		fi
		if ! pr_named_completed=$(printf '%s' "${pr_named_runs}" | jq -r \
			--arg current "${current_run_id}" \
			--argjson bound "${push_bound}" '
			[
				.[]
				| select(.status == "completed")
				| select((.conclusion // "") != "cancelled")
				| select((.id | tostring) != $current)
				| select(((.created_at // "") | (try fromdateiso8601 catch -1)) >= $bound)
			]
			| length
		' 2>/dev/null) || ! [[ "${pr_named_completed}" =~ ^[0-9]+$ ]]; then
			echo "AUTOFIX_CHANGES_LOST_BUDGET_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch} reason=pr_named_jq_error" >&2
			return 0
		fi
		prior_completed=$(( ${prior_completed:-0} + pr_named_completed ))
	fi

	echo "AUTOFIX_CHANGES_LOST_BUDGET pr=${pr_number:-?} branch=${head_branch} head_sha=${head_sha} current_run=${current_run_id} prior_completed=${prior_completed:-0} pr_named_completed=${pr_named_completed} event=${run_event_name:--}"

	if [ "${prior_completed:-0}" -gt 0 ] 2>/dev/null; then
		return 0
	fi
	return 1
}

# ---------------------------------------------------------------
# Prompt-input embedding helpers.
#
# Editor / reviewer / judge prompts that used to tell the model
# "Read the following files: - $FOO" pay a per-file exec round-trip
# (cat) that eats reasoning budget before the model can act.  The
# helpers below let the prompt builder inline file contents directly
# while enforcing two invariants the model cannot guarantee on its
# own:
#
#   1. **Per-file cap** — never emit more than `byte_cap` bytes from
#      a single file (default 100000).  For diff files the caller
#      should pass a larger cap and `truncate_mode=diff`, which keeps
#      the head of the file aligned to the previous `^diff --git`
#      boundary so the model never sees a half-hunk.
#   2. **Total-prompt budget** — across ALL `_embed_input_file`
#      invocations in the current shell process the cumulative bytes
#      stay under `_PROMPT_BUDGET_TOTAL_BYTES` (default 800000 ≈
#      200k tokens at ~4 bytes/token).  Sized to fit comfortably
#      within a 272k-token context window (the former gpt-5.5
#      capacity-fallback floor) once the static prefix (system
#      instructions / agents / pipeline doc, ~10k tokens) and the
#      response budget (~30k tokens) are subtracted.  The current
#      primary (gpt-6-sol) and fallback (gpt-5.6-sol) both have 1.05M
#      windows, so this is headroom.  After the budget is exhausted
#      later files are replaced with an explicit "(omitted — budget exhausted)"
#      marker so the model knows context is incomplete.
#
# The budget tracker uses a state file under $TMPDIR so it survives
# subshell invocations from $(...) command-substitution inside a
# heredoc.  Callers should run `_init_prompt_budget` before assembling
# the prompt and `_cleanup_prompt_budget` after the prompt is written.
# ---------------------------------------------------------------

# Default total prompt budget (bytes).  Set the env var BEFORE sourcing
# gh_helpers.sh to override.  See header comment above for the sizing
# rationale (200k tokens within a 272k context window after static prefix
# + response budget; the gpt-6-sol / gpt-5.6-sol defaults have 1.05M).
: "${_PROMPT_BUDGET_TOTAL_BYTES:=800000}"

# Resolve a stable per-process state-file path.  $$ in a subshell is
# the parent shell's PID, so all $(...) invocations under one parent
# share the same file.
_prompt_budget_state_file()
{
	printf '%s/_prompt_input_budget_state.%s\n' "${TMPDIR:-/tmp}" "$$"
}

# Initialise (or reset) the running-total state file.  Optionally
# override the per-process total cap with the first argument.
_init_prompt_budget()
{
	local _cap="${1:-${_PROMPT_BUDGET_TOTAL_BYTES}}"
	local _state
	_state="$(_prompt_budget_state_file)"
	export _PROMPT_BUDGET_TOTAL_BYTES="${_cap}"
	printf '0\n' > "${_state}" 2>/dev/null || true
}

# Remove the per-process state file.  Idempotent.
_cleanup_prompt_budget()
{
	local _state
	_state="$(_prompt_budget_state_file)"
	rm -f "${_state}" 2>/dev/null || true
}

# _embed_utf8_safe_cut <path> <n>
#
# Print <n> reduced (by at most 3 bytes) so that `head -c <n> <path>`
# never ends inside a multi-byte UTF-8 sequence.  A byte cut mid-character
# leaves invalid UTF-8 in the prompt, which the Codex CLI's strict stdin
# reader rejects (#9261).  Only the up-to-4 bytes before offset <n> are
# inspected with `od`; ASCII or a complete sequence returns <n> unchanged.
# Fails open: any `od` failure or unexpected output returns <n> unchanged
# (the pre-#9261 behaviour); `sanitize_codex_prompt_file` remains the
# last line of defence downstream.
_embed_utf8_safe_cut()
{
	local _cut_path="${1:-}"
	local _cut_n="${2:-0}"
	if ! [[ "${_cut_n}" =~ ^[0-9]+$ ]] || [ "${_cut_n}" -le 0 ] || [ -z "${_cut_path}" ]; then
		printf '%s\n' "${_cut_n}"
		return 0
	fi
	local _cut_window=4
	if [ "${_cut_n}" -lt 4 ]; then
		_cut_window="${_cut_n}"
	fi
	local _cut_raw
	if ! _cut_raw="$(od -An -tu1 -j "$(( _cut_n - _cut_window ))" -N "${_cut_window}" -- "${_cut_path}" 2>/dev/null)"; then
		printf '%s\n' "${_cut_n}"
		return 0
	fi
	local -a _cut_bytes=()
	read -r -a _cut_bytes <<< "$(printf '%s' "${_cut_raw}" | tr '\n' ' ')" || true
	if [ "${#_cut_bytes[@]}" -ne "${_cut_window}" ]; then
		printf '%s\n' "${_cut_n}"
		return 0
	fi
	local _cut_byte
	for _cut_byte in "${_cut_bytes[@]}"; do
		if ! [[ "${_cut_byte}" =~ ^[0-9]+$ ]]; then
			printf '%s\n' "${_cut_n}"
			return 0
		fi
	done
	# Step back over trailing continuation bytes (0x80..0xBF), at most 3.
	local _cut_idx=$(( ${#_cut_bytes[@]} - 1 ))
	local _cut_cont=0
	while [ "${_cut_idx}" -ge 0 ] && [ "${_cut_cont}" -lt 3 ] \
		&& [ "${_cut_bytes[${_cut_idx}]}" -ge 128 ] && [ "${_cut_bytes[${_cut_idx}]}" -le 191 ]; do
		_cut_cont=$(( _cut_cont + 1 ))
		_cut_idx=$(( _cut_idx - 1 ))
	done
	if [ "${_cut_idx}" -lt 0 ]; then
		printf '%s\n' "${_cut_n}"
		return 0
	fi
	local _cut_lead="${_cut_bytes[${_cut_idx}]}"
	local _cut_seq_len=0
	if [ "${_cut_lead}" -ge 192 ] && [ "${_cut_lead}" -le 223 ]; then
		_cut_seq_len=2
	elif [ "${_cut_lead}" -ge 224 ] && [ "${_cut_lead}" -le 239 ]; then
		_cut_seq_len=3
	elif [ "${_cut_lead}" -ge 240 ] && [ "${_cut_lead}" -le 247 ]; then
		_cut_seq_len=4
	fi
	# Lead byte plus its continuation bytes before <n> do not complete the
	# sequence: cut before the lead byte instead.
	if [ "${_cut_seq_len}" -gt 0 ] && [ "${_cut_seq_len}" -gt $(( _cut_cont + 1 )) ]; then
		printf '%s\n' "$(( _cut_n - _cut_cont - 1 ))"
		return 0
	fi
	printf '%s\n' "${_cut_n}"
}

# _embed_input_file <path> [byte_cap] [truncate_mode]
#
#   byte_cap        — defaults to 100000.  Caller can pass a larger
#                     value for the few files that genuinely need it
#                     (diffs, reviewer bundles, comment dumps).
#   truncate_mode   — `head` (default) or `diff`.  `diff` mode keeps
#                     the file truncated at the last whole `^diff --git`
#                     boundary that fits, so the model never sees a
#                     half-hunk that would mis-scope its findings.
#
# Output is plain bytes safe to drop into a heredoc; no shell expansion
# is performed on the file content.  When the per-file cap or the
# global budget is hit, an explicit `[... TRUNCATED ...]` / `(omitted —
# budget exhausted)` marker is emitted so the model knows the section
# is incomplete instead of silently believing it saw everything.
_embed_input_file()
{
	local _path="${1:-}"
	local _cap="${2:-100000}"
	local _mode="${3:-head}"
	if [ -z "${_path}" ] || [ ! -e "${_path}" ]; then
		printf '(missing)\n'
		return 0
	fi
	if [ ! -s "${_path}" ]; then
		printf '(empty)\n'
		return 0
	fi

	# Honour the running total budget.  If we have no remaining
	# budget at all, emit the omitted-marker and return.
	local _state _used _budget_remaining _effective_cap
	_state="$(_prompt_budget_state_file)"
	_used=0
	if [ -f "${_state}" ]; then
		_used="$(cat "${_state}" 2>/dev/null)"
		[[ "${_used}" =~ ^[0-9]+$ ]] || _used=0
	fi
	_budget_remaining=$(( _PROMPT_BUDGET_TOTAL_BYTES - _used ))
	if [ "${_budget_remaining}" -le 0 ]; then
		printf '(omitted — total prompt input budget %d bytes exhausted; %d bytes already inlined)\n' \
			"${_PROMPT_BUDGET_TOTAL_BYTES}" "${_used}"
		return 0
	fi

	_effective_cap="${_cap}"
	if [ "${_budget_remaining}" -lt "${_effective_cap}" ]; then
		_effective_cap="${_budget_remaining}"
	fi

	local _size
	_size="$(wc -c < "${_path}" 2>/dev/null | tr -d '[:space:]')"
	[[ "${_size}" =~ ^[0-9]+$ ]] || _size=0

	local _emit_bytes
	if [ "${_size}" -le "${_effective_cap}" ]; then
		# Whole file fits; emit verbatim.  No need for a trailing newline:
		# command substitution `$(...)` strips trailing newlines and the
		# enclosing heredoc places its own \n after the substitution, so
		# the closing fence always lands on its own line.
		cat -- "${_path}"
		_emit_bytes="${_size}"
	else
		# File would overflow the effective cap.  In `diff` mode, walk
		# back from the cap to the last `^diff --git` boundary so the
		# model never sees a half-hunk; in `head` mode just take the
		# first _effective_cap bytes.  Either way emit an explicit
		# truncation marker so the model knows the section is partial.
		local _head_bytes="${_effective_cap}"
		if [ "${_mode}" = "diff" ]; then
			# Find the last `^diff --git` whose start-of-line lies <= cap.
			# `grep -b` outputs `BYTE_OFFSET:LINE_CONTENT` (no line numbers).
			# awk picks the largest byte offset still inside the cap; we
			# skip offset 0 because truncating before the first hunk would
			# emit nothing (head mode handles that case correctly).
			local _boundary
			_boundary="$(grep -bE -- '^diff --git ' "${_path}" 2>/dev/null \
				| awk -F: -v cap="${_effective_cap}" \
					'($1+0) > 0 && ($1+0) <= cap { last=$1+0 } END { print last+0 }')"
			if [ -n "${_boundary}" ] && [ "${_boundary}" -gt 0 ]; then
				_head_bytes="${_boundary}"
			fi
		fi
		# Never end the shown head inside a multi-byte UTF-8 character
		# (#9261).  The marker and budget accounting below use the trimmed
		# count.  A `diff`-mode boundary is a line start, so it is unchanged.
		_head_bytes="$(_embed_utf8_safe_cut "${_path}" "${_head_bytes}")"
		head -c "${_head_bytes}" -- "${_path}"
		printf '\n[... TRUNCATED — file is %s bytes; first %s bytes shown above (mode=%s, per-file cap=%s, budget remaining was %s) ...]\n' \
			"${_size}" "${_head_bytes}" "${_mode}" "${_cap}" "${_budget_remaining}"
		_emit_bytes="${_head_bytes}"
	fi

	# Update running total.  Best-effort: if the state file write
	# fails we still emitted the content (don't poison the prompt).
	if [ -n "${_emit_bytes}" ] && [ "${_emit_bytes}" -gt 0 ] 2>/dev/null; then
		printf '%s\n' "$(( _used + _emit_bytes ))" > "${_state}" 2>/dev/null || true
	fi
}

# ---------------------------------------------------------------
# sanitize_codex_prompt_file <path>
#
# Rewrite <path> in place with any invalid UTF-8 byte sequences
# stripped, so the Codex CLI's strict UTF-8 stdin reader never
# rejects the prompt on the first invalid byte. This is the
# last-line-of-defence sanitisation for prompt files that get piped
# to `codex … exec … < "${path}"`.
#
# Background: Codex CLI reads its prompt from stdin as a UTF-8
# string and aborts on the first invalid byte with
#   `Failed to read prompt from stdin: input is not valid UTF-8
#    (invalid byte at offset N). Convert it to UTF-8 and retry`
# Any upstream byte-based truncation that lands mid-codepoint
# (e.g. mawk's byte-oriented `substr` on a 3-byte em-dash) — or any
# other corruption that leaks invalid bytes into an embedded input
# file — deterministically kills the editor, and the retry loop is
# impotent because the same bad bytes survive into the regenerated
# prompt. This helper makes that failure mode impossible at the
# stdin boundary.
#
# Behaviour notes:
#   • Best-effort. No-op if <path> is missing/empty or if the temp
#     file write fails.
#   • Prefers `iconv -f UTF-8 -t UTF-8//IGNORE` when available.
#     GNU iconv exits non-zero whenever it discards bytes even though
#     the rewritten output is correct, so we accept any non-empty
#     output regardless of exit status.
#   • Falls back to `python3` UTF-8 decode/encode with
#     `errors="ignore"` when iconv is missing, unsupported (musl),
#     or discards the entire file. That preserves the documented
#     "last line of defence" behaviour even for all-invalid inputs.
#   • Idempotent. Running on already-valid UTF-8 leaves the file
#     byte-identical (modulo the mv/rename, which the caller's
#     downstream `wc -c` / `sha256sum` instrumentation will reflect).
#   • Silent on success. The caller's existing `wc -c` / `sha256sum`
#     echo lines around the codex pipe will surface any byte-count
#     delta after sanitisation, so we don't add log noise here.
# ---------------------------------------------------------------
sanitize_codex_prompt_file()
{
	local _path="${1:-}"
	if [ -z "${_path}" ] || [ ! -f "${_path}" ]; then
		return 0
	fi
	local _tmp
	_tmp="$(mktemp "${_path}.utf8XXXXXX" 2>/dev/null)" || return 0
	if command -v iconv >/dev/null 2>&1; then
		# //IGNORE discards invalid sequences. iconv exits non-zero
		# whenever it skips any, but the output IS correct, so don't
		# gate the rewrite on $? — gate on whether output was produced.
		iconv -f UTF-8 -t UTF-8//IGNORE < "${_path}" > "${_tmp}" 2>/dev/null || true
		if [ -s "${_tmp}" ] || [ ! -s "${_path}" ]; then
			mv "${_tmp}" "${_path}" 2>/dev/null || rm -f "${_tmp}"
			return 0
		fi
		: > "${_tmp}" 2>/dev/null || { rm -f "${_tmp}"; return 0; }
	fi
	if command -v python3 >/dev/null 2>&1; then
		if PYTHONSAFEPATH=1 python3 - "${_path}" "${_tmp}" <<'PY' 2>/dev/null
from pathlib import Path
import sys

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
dst.write_bytes(src.read_bytes().decode("utf-8", "ignore").encode("utf-8"))
PY
		then
			mv "${_tmp}" "${_path}" 2>/dev/null || rm -f "${_tmp}"
			return 0
		fi
	fi
	rm -f "${_tmp}"
}

# ---------------------------------------------------------------
# extract_repo_scoped_issue_refs_from_text <owner/repo> <text>
#
# Print deduplicated issue numbers referenced by strict current-repo
# closing keywords or full repo-scoped issue URLs/paths.
#
# Accepted:
#   Fixes #12
#   closes #34
#   owner/repo/issues/56
#   https://github.com/owner/repo/issues/78
#
# Rejected:
#   issue #12
#   issues/12
#   Closes: #12
#   https://github.com/owner/repo/issues/78#issuecomment-1
#     (any URL/path followed by a `#fragment` points at a comment or
#     event on the issue, not at the issue as the PR's subject;
#     #5776 / PR #5649 relabelled an issue `ai:merged` from such a link)
#   https://github.com/owner/repo/issues/78?view=plain#issuecomment-1
#     (a `?query` after the number is not a boundary either, so a query
#     string cannot hide a following `#fragment`; a bare URL with only a
#     query string is conservatively not counted as a link)
#
# Fail-open:
#   empty text or malformed repository input emits no matches
# ---------------------------------------------------------------
extract_repo_scoped_issue_refs_from_text()
{
	local _repository="${1:-}"
	local _text="${2:-}"
	local _repository_escaped

	if [ -z "${_repository}" ] || [ -z "${_text}" ] || ! [[ "${_repository}" =~ ^[^/]+/[^/]+$ ]]; then
		return 0
	fi

	_repository_escaped="$(printf '%s' "${_repository}" | sed 's/[][\\.^$*+?(){}|]/\\&/g')"
	printf '%s\n' "${_text}" \
		| grep -oiE "((^|[^[:alnum:]_])github\\.com/${_repository_escaped}/issues/[0-9]+([^[:alnum:]_#?]|$)|(^|[^[:alnum:]_])${_repository_escaped}/issues/[0-9]+([^[:alnum:]_#?]|$)|(^|[^[:alnum:]_/-])(close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)[[:space:]]+#[[:space:]]*[0-9]+([^[:alnum:]_]|$))" \
		| sed -nE 's/.*[^0-9]([0-9]+)[^0-9]*$/\1/p' \
		| sort -un || true
}

# ---------------------------------------------------------------
# Wedged queued workflow runs (issue #9473)
#
# GitHub can leave a workflow run in status=queued with zero jobs.
# The API refuses to clear it (run 43095142301: cancel 409,
# force-cancel 409, DELETE 403), so every "is a run already active?"
# guard would treat it as active forever. These helpers generalise the
# sweep's `is_stale_queued` rule (review_autofix_sweep.yml) into one
# shared age-based filter:
#
#   a run is wedged when status == "queued" and
#   created_at <= now - ZOMBIE_RUN_MAX_QUEUED_HOURS hours.
#
# ZOMBIE_RUN_MAX_QUEUED_HOURS: default 24 (the same knob the P11b
# reaper reads); 0 or any value that is not a whole number disables
# the filter. Only `queued` ages out: in_progress, pending, waiting
# and requested never do. A missing or unparseable created_at keeps
# the run active (fail-safe). Callers filter the JSON they already
# fetched, so the helpers add no API calls (CLAUDE.md §14).
#
# zombie_queued_cutoff_epoch: prints the cutoff epoch, or 0 when the
#   filter is disabled (invalid value, 0, or unreadable clock).
# zombie_queued_jq_def: prints jq definitions for embedding at the
#   front of a caller's program; `is_wedged_queued($cutoff)` takes the
#   cutoff as a number (pass it with --argjson).
# zombie_queued_log_ignored <guard> <cutoff>: reads one JSON document
#   on stdin (an array of runs or {workflow_runs:[...]}) and prints,
#   on stderr, one line per wedged run:
#     ZOMBIE_RUN_IGNORED run=<id> workflow=<path> age_h=<n> guard=<guard>
#   at most once per run per guard per shell process. Always returns 0.
# ---------------------------------------------------------------
zombie_queued_cutoff_epoch()
{
	local _zq_hours="${ZOMBIE_RUN_MAX_QUEUED_HOURS:-24}" _zq_now=""
	if ! [[ "${_zq_hours}" =~ ^[0-9]{1,5}$ ]]; then
		echo "::warning::ZOMBIE_RUN_MAX_QUEUED_HOURS is not a whole number of hours; wedged-queued filter disabled" >&2
		printf '0'
		return 0
	fi
	_zq_hours=$((10#${_zq_hours}))
	if [ "${_zq_hours}" -le 0 ]; then
		printf '0'
		return 0
	fi
	_zq_now="$(date -u +%s 2>/dev/null || true)"
	if ! [[ "${_zq_now}" =~ ^[0-9]+$ ]] || [ "${_zq_now}" -le $((_zq_hours * 3600)) ]; then
		printf '0'
		return 0
	fi
	printf '%s' "$((_zq_now - _zq_hours * 3600))"
}

zombie_queued_jq_def()
{
	cat <<'JQ_DEF'
def zq_created_epoch:
  ((.created_at // .createdAt) as $zq_ts
   | if ($zq_ts | type) == "string" and ($zq_ts | length) > 0
     then (try ($zq_ts | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601) catch null)
     else null end);
def zq_run_id: (.id // .databaseId // "-");
def zq_workflow: (.path // .workflowName // .name // "-");
def is_wedged_queued($cutoff):
  type == "object"
  and ($cutoff | type) == "number" and $cutoff > 0
  and ((.status // "") | if type == "string" then ascii_downcase else "" end) == "queued"
  and (zq_created_epoch as $zq_c | $zq_c != null and $zq_c <= $cutoff);
JQ_DEF
}

zombie_queued_log_ignored()
{
	local _zq_guard="${1:-unknown}" _zq_cutoff="${2:-0}" _zq_now="" _zq_rows="" _zq_id _zq_wf _zq_age
	_zq_guard="$(printf '%s' "${_zq_guard}" | tr -c 'A-Za-z0-9_.-' '_')"
	if ! [[ "${_zq_cutoff}" =~ ^[0-9]+$ ]] || [ "${_zq_cutoff}" -le 0 ]; then
		cat >/dev/null 2>&1 || true
		return 0
	fi
	_zq_now="$(date -u +%s 2>/dev/null || true)"
	[[ "${_zq_now}" =~ ^[0-9]+$ ]] || _zq_now=0
	_zq_rows="$(jq -r --argjson zq_cutoff "${_zq_cutoff}" --argjson zq_now "${_zq_now}" "$(zombie_queued_jq_def)"'
		(if type == "array" then .[] elif type == "object" then ((.workflow_runs // []) | if type == "array" then .[] else empty end) else empty end)
		| select(is_wedged_queued($zq_cutoff))
		| [(zq_run_id | tostring | gsub("[^A-Za-z0-9_.-]"; "_")),
		   (zq_workflow | tostring | gsub("[^A-Za-z0-9_./@-]"; "_")),
		   (if $zq_now > 0 then (((($zq_now - zq_created_epoch) / 3600) | floor) | tostring) else "-" end)]
		| @tsv' 2>/dev/null || true)"
	[ -n "${_zq_rows}" ] || return 0
	if ! declare -p _ZOMBIE_RUN_IGNORED_SEEN >/dev/null 2>&1; then
		declare -gA _ZOMBIE_RUN_IGNORED_SEEN=()
	fi
	while IFS=$'\t' read -r _zq_id _zq_wf _zq_age; do
		[ -n "${_zq_id}" ] || continue
		[ -z "${_ZOMBIE_RUN_IGNORED_SEEN["${_zq_guard}:${_zq_id}"]+x}" ] || continue
		_ZOMBIE_RUN_IGNORED_SEEN["${_zq_guard}:${_zq_id}"]=1
		echo "ZOMBIE_RUN_IGNORED run=${_zq_id} workflow=${_zq_wf} age_h=${_zq_age} guard=${_zq_guard}" >&2
	done <<< "${_zq_rows}"
	return 0
}

# ---------------------------------------------------------------
# Release-gate runner wait (issue #10037)
#
# release_gate_oldest_queued_wait <repo> <limit_minutes> <zq_cutoff_epoch>
#
# Measures how long the oldest live queued run of <repo> has waited for a
# runner, for the release scripts' runner_wait_long skip
# (scripts/auto_release_stable.sh, scripts/promote_main_cycle.sh).
# Only runs created between the wedged-queued cutoff (zombie_queued_cutoff_epoch,
# 0 = no lower bound) and now - <limit_minutes> are read, so the answer is
# "is any live run queued for at least the limit, and the oldest one's age".
#
# Output: exactly one line on stdout, always exit 0 (fail open):
#   outcome=<long|short|read_failed> oldest_queued_min=<n|-> queued_in_window=<n|-> calls=<0|1|2>
#
# API calls (§14): no existing call in the callers returns queued runs'
# created_at (they list workflow-scoped runs or a queued count), so:
#   call 1  GET repos/<repo>/actions/runs?status=queued&created=<lower>..<upper>&per_page=100
#   call 2  only when total_count > 100: the last page (newest first, so the
#           oldest runs are there), capped at page 10 (the API's 1,000-result
#           limit).
# Any failed or unparseable read prints outcome=read_failed.
# ---------------------------------------------------------------
release_gate_oldest_queued_wait()
{
	local _rgw_repo="${1:-}" _rgw_limit="${2:-}" _rgw_cutoff="${3:-0}"
	local _rgw_now="" _rgw_upper_epoch="" _rgw_upper="" _rgw_lower="" _rgw_json="" _rgw_total="" _rgw_page="" _rgw_calls=0 _rgw_oldest=""
	local _rgw_jq_def=""
	if ! [[ "${_rgw_repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || ! [[ "${_rgw_limit}" =~ ^[0-9]{1,6}$ ]]; then
		echo "outcome=read_failed oldest_queued_min=- queued_in_window=- calls=0"
		return 0
	fi
	[[ "${_rgw_cutoff}" =~ ^[0-9]+$ ]] || _rgw_cutoff=0
	_rgw_now="$(date -u +%s 2>/dev/null || true)"
	if ! [[ "${_rgw_now}" =~ ^[0-9]+$ ]]; then
		echo "outcome=read_failed oldest_queued_min=- queued_in_window=- calls=0"
		return 0
	fi
	_rgw_upper_epoch=$(( _rgw_now - 10#${_rgw_limit} * 60 ))
	_rgw_upper="$(date -u -d "@${_rgw_upper_epoch}" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || true)"
	if [ "${_rgw_cutoff}" -gt 0 ]; then
		_rgw_lower="$(date -u -d "@$(( _rgw_cutoff + 1 ))" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || true)"
	else
		_rgw_lower="1970-01-01T00:00:00Z"
	fi
	if [ -z "${_rgw_upper}" ] || [ -z "${_rgw_lower}" ]; then
		echo "outcome=read_failed oldest_queued_min=- queued_in_window=- calls=0"
		return 0
	fi
	if declare -F zombie_queued_jq_def >/dev/null 2>&1; then
		_rgw_jq_def="$(zombie_queued_jq_def 2>/dev/null || true)"
	fi
	[ -n "${_rgw_jq_def}" ] || _rgw_jq_def='def is_wedged_queued($cutoff): false;'
	_rgw_calls=1
	if ! _rgw_json="$(gh_retry gh api -X GET "repos/${_rgw_repo}/actions/runs" -f status=queued -f "created=${_rgw_lower}..${_rgw_upper}" -f per_page=100 2>/dev/null)"; then
		echo "outcome=read_failed oldest_queued_min=- queued_in_window=- calls=${_rgw_calls}"
		return 0
	fi
	_rgw_total="$(printf '%s' "${_rgw_json}" | jq -r 'if (.total_count | type) == "number" and (.workflow_runs | type) == "array" then .total_count else empty end' 2>/dev/null || true)"
	if ! [[ "${_rgw_total}" =~ ^[0-9]+$ ]]; then
		echo "outcome=read_failed oldest_queued_min=- queued_in_window=- calls=${_rgw_calls}"
		return 0
	fi
	if [ "${_rgw_total}" -gt 100 ]; then
		_rgw_page=$(( (_rgw_total + 99) / 100 ))
		[ "${_rgw_page}" -le 10 ] || _rgw_page=10
		_rgw_calls=2
		if ! _rgw_json="$(gh_retry gh api -X GET "repos/${_rgw_repo}/actions/runs" -f status=queued -f "created=${_rgw_lower}..${_rgw_upper}" -f per_page=100 -f "page=${_rgw_page}" 2>/dev/null)" \
			|| ! printf '%s' "${_rgw_json}" | jq -e '(.workflow_runs | type) == "array"' >/dev/null 2>&1; then
			echo "outcome=read_failed oldest_queued_min=- queued_in_window=- calls=${_rgw_calls}"
			return 0
		fi
	fi
	# Oldest created_at among live queued runs at or before the upper bound
	# (the server-side range is re-checked here).
	_rgw_oldest="$(printf '%s' "${_rgw_json}" | jq -r --argjson zq_cutoff "${_rgw_cutoff}" --argjson upper "${_rgw_upper_epoch}" "${_rgw_jq_def}"'
		[.workflow_runs[]? | select(type == "object")
			| select(((.status // "") | tostring | ascii_downcase) == "queued")
			| select(is_wedged_queued($zq_cutoff) | not)
			| ((.created_at // "") | tostring | sub("\\.[0-9]+Z$"; "Z") | (try fromdateiso8601 catch null)) as $c
			| select($c != null and $c <= $upper) | $c] | min // empty' 2>/dev/null || true)"
	if [ -z "${_rgw_oldest}" ]; then
		echo "outcome=short oldest_queued_min=- queued_in_window=${_rgw_total} calls=${_rgw_calls}"
		return 0
	fi
	if ! [[ "${_rgw_oldest}" =~ ^[0-9]+$ ]]; then
		echo "outcome=read_failed oldest_queued_min=- queued_in_window=- calls=${_rgw_calls}"
		return 0
	fi
	echo "outcome=long oldest_queued_min=$(( (_rgw_now - _rgw_oldest) / 60 )) queued_in_window=${_rgw_total} calls=${_rgw_calls}"
	return 0
}
