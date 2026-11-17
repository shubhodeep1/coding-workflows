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
