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
  (HMAC-SHA256 over repository, issue number, PAT login, PAT account type and
  the body) and posted with `AI_COMMENT_JOB_TOKEN`. The signature is one
  hidden line appended to the body:
  `<!-- ai-comment-sig:v1 as=<login> type=<User|Bot> sig=<64 hex> -->`.
  A body that could start a wrapper (`/approved`, `/answer`, `/reclarify`,
  `Clarification required`) or that the hardened plan-approval readers match
  (`Implementation Plan`) is posted unchanged with the caller's token.
* **Edit.** A body edit of a comment that `github-actions[bot]` wrote is
  re-signed, so an in-place refresh keeps the comment trusted.
* **Read.** Every `issues/.../comments` read is normalized: a comment by
  `github-actions[bot]` whose signature verifies is returned exactly as the
  PAT would have written it (`user.login` = the signed login, `user.type` =
  the signed type, `author_association` = `OWNER` for the repository owner,
  else `COLLABORATOR`), with the signature line removed from `body`. An
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
* One `GET /user` per job for the PAT identity, cached in
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
SIG_LINE_RE = re.compile(
	r"\n<!-- ai-comment-sig:v1 as=(?P<login>" + _LOGIN + r") type=(?P<type>User|Bot) sig=(?P<sig>[0-9a-f]{64}) -->\Z"
)
_ISSUE_URL_RE = re.compile(r"/repos/(?P<repo>[^/]+/[^/]+)/issues/(?P<num>[0-9]+)/?$")
_CREATE_RE = re.compile(r"^repos/(?P<repo>[^/{}]+/[^/{}]+)/issues/(?P<num>[0-9]+)/comments$")
_EDIT_RE = re.compile(r"^repos/(?P<repo>[^/{}]+/[^/{}]+)/issues/comments/(?P<id>[0-9]+)$")
_READ_RE = re.compile(r"^repos/[^/]+/[^/]+/issues/(?:[0-9]+/comments|comments(?:/[0-9]+)?)$")

# Bodies that must keep the caller's (PAT) author. The first four are what the
# `issue_comment` wrappers start on; the plan heading is what the hardened
# plan-approval jobs in workflow-templates/ai-implement.yml and ai-plan.yml
# read by the approver login. Matched anywhere in the body, so a body that
# could match a wrapper predicate is never moved.
_KEEP_SUBSTRINGS = ("/approved", "/answer", "/reclarify", "Clarification required")
_KEEP_PLAN_RE = re.compile(r"(?im)^\s*implementation\s+plan\b")

_REFUSED_RE = re.compile(
	r"HTTP 40[13]|Resource not accessible by integration|Bad credentials|Requires authentication",
	re.IGNORECASE,
)

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

def canonical(repo: str, issue: int, login: str, account_type: str, body: str) -> bytes:
	digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
	return (
		f"ai-comment-sig:{SIG_VERSION}\nrepo={repo.lower()}\nissue={int(issue)}"
		f"\nas={login}\ntype={account_type}\nbody_sha256={digest}"
	).encode("utf-8")


def signature(secret: str, repo: str, issue: int, login: str, account_type: str, body: str) -> str:
	return hmac.new(secret.encode("utf-8"), canonical(repo, issue, login, account_type, body), hashlib.sha256).hexdigest()


def strip_signature(body: str) -> str:
	"""Remove a trailing signature line, if any (used before re-signing)."""
	match = SIG_LINE_RE.search(body)
	return body[: match.start()] if match else body


def sign_body(secret: str, repo: str, issue: int, login: str, account_type: str, body: str) -> str:
	body = strip_signature(body)
	sig = signature(secret, repo, issue, login, account_type, body)
	return f"{body}\n<!-- ai-comment-sig:{SIG_VERSION} as={login} type={account_type} sig={sig} -->"


def keep_caller_author(body: str) -> bool:
	"""True when the body could start a wrapper or is a plan comment."""
	return any(token in body for token in _KEEP_SUBSTRINGS) or bool(_KEEP_PLAN_RE.search(body))


def _comment_location(comment: dict) -> tuple[str, int] | None:
	match = _ISSUE_URL_RE.search(str(comment.get("issue_url") or ""))
	if not match:
		return None
	return match.group("repo"), int(match.group("num"))


def normalize_comment(comment: Any, secret: str) -> Any:
	if not isinstance(comment, dict):
		return comment
	user = comment.get("user")
	body = comment.get("body")
	if not isinstance(user, dict) or user.get("login") != BOT_LOGIN or not isinstance(body, str):
		return comment
	match = SIG_LINE_RE.search(body)
	if not match:
		return comment
	location = _comment_location(comment)
	if location is None:
		log("unverifiable", comment_id=comment.get("id"), reason="no_issue_url")
		return comment
	repo, issue = location
	original = body[: match.start()]
	expected = signature(secret, repo, issue, match.group("login"), match.group("type"), original)
	if not hmac.compare_digest(expected, match.group("sig")):
		log("invalid", comment_id=comment.get("id"))
		return comment
	login = match.group("login")
	owner = repo.split("/", 1)[0]
	normalized = dict(comment)
	normalized["body"] = original
	normalized["user"] = dict(user, login=login, type=match.group("type"))
	normalized["author_association"] = "OWNER" if login.lower() == owner.lower() else "COLLABORATOR"
	normalized["ai_comment_sig"] = {"poster": BOT_LOGIN}
	return normalized


def normalize_value(value: Any, secret: str) -> Any:
	if isinstance(value, list):
		return [normalize_value(item, secret) for item in value]
	return normalize_comment(value, secret)


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


def caller_identity() -> tuple[str, str] | None:
	"""(login, type) of the caller's token, cached per job (one GET /user)."""
	token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
	if not token:
		return None
	cache = os.path.join(_state_dir(), f"ai_comment_sig_identity_{_token_key(token)}.json")
	try:
		with open(cache, encoding="utf-8") as handle:
			cached = json.load(handle)
		if re.fullmatch(_LOGIN, cached.get("login", "")) and cached.get("type") in ("User", "Bot"):
			return cached["login"], cached["type"]
	except (OSError, ValueError, AttributeError):
		pass
	proc = run_gh(["api", "user", "--jq", "{login: .login, type: .type}"])
	if proc.returncode != 0:
		return None
	try:
		identity = json.loads(proc.stdout)
	except ValueError:
		return None
	login, account_type = identity.get("login", ""), identity.get("type", "")
	if login == BOT_LOGIN or not re.fullmatch(_LOGIN, login) or account_type not in ("User", "Bot"):
		return None
	try:
		with open(cache, "w", encoding="utf-8") as handle:
			json.dump({"login": login, "type": account_type}, handle)
	except OSError:
		pass
	return login, account_type


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
	signed = sign_body(secret, repo, issue, identity[0], identity[1], body)
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
		proc = run_gh(["api", endpoint, "--method", "POST", "--input", payload_path, *output_args], env=env)
	finally:
		os.unlink(payload_path)
	if proc.returncode == 0:
		log("posted", issue=issue, token="job")
		return emit(proc)
	if _REFUSED_RE.search(proc.stderr or ""):
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
	old = SIG_LINE_RE.search(str(current.get("body") or ""))
	if location is None or old is None or not hmac.compare_digest(
		signature(secret, location[0], location[1], old.group("login"), old.group("type"), str(current["body"])[: old.start()]),
		old.group("sig"),
	):
		# Never sign an edit of a comment that was not validly signed: that
		# would turn an untrusted bot comment into a trusted one.
		return plain()
	# Re-sign as the account that signed the original, so an edit cannot
	# change whose comment it is.
	signed = sign_body(secret, location[0], location[1], old.group("login"), old.group("type"), body)
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


def read_normalized(args: list[str], parsed: dict, secret: str) -> int:
	if parsed["template"] is not None:
		return passthrough(args)
	raw_args = ["api", *_strip_jq(args[1:])]
	proc = run_gh(raw_args)
	if proc.returncode != 0:
		return emit(proc)
	text = proc.stdout
	head = ""
	if parsed["include"]:
		split = re.search(r"\r?\n\r?\n", text)
		if split is None:
			return emit(proc)
		head, text = text[: split.end()], text[split.end():]
	try:
		values = [normalize_value(value, secret) for value in iter_json_stream(text)]
	except ValueError:
		return emit(proc)
	sys.stderr.write(proc.stderr)
	if parsed["jq"] is None:
		sys.stdout.write(head + "".join(json.dumps(value, ensure_ascii=False, separators=(",", ":")) for value in values))
		return 0
	stream = "\n".join(json.dumps(value, ensure_ascii=False) for value in values)
	filtered = subprocess.run(["jq", "-rcS", parsed["jq"]], input=stream, capture_output=True, text=True, check=False)
	sys.stdout.write(head + filtered.stdout)
	sys.stderr.write(filtered.stderr)
	return filtered.returncode


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
