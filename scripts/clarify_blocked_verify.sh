#!/usr/bin/env bash
# clarify_blocked_verify.sh — decide whether a clarify `BLOCKED:` answer cites
# a path that git tracks at the checked-out ref, and build the one retry
# prompt for it (docs/plans/remove-unattended-pipeline-stuck-states-plan.md,
# P9 step 2; Refs #8291).
#
# Usage:
#   clarify_blocked_verify.sh check <output_file> <prompt_file> <listing_out>
#   clarify_blocked_verify.sh prompt <prompt_in> <listing> <prompt_out>
#
# check   reads the first `BLOCKED:` line outside a code fence (the rule of
#         clarify.yml's "Parse Codex output" step), picks path-like tokens
#         from its reason, drops the paths the clarify snapshot never ships
#         (the `sensitive()` rules of scripts/clarify_isolated_run.sh), and
#         compares the rest with one `git ls-files -z` listing of the source
#         root (CLARIFY_SOURCE_ROOT, default $PWD, the root the snapshot is
#         built from). Tokens are compared in Python and never passed to git.
#         Writes one line per tracked path to <listing_out> (at most 8 KiB).
# prompt  writes <prompt_out>: the bytes of <prompt_in> unchanged, followed by
#         one delimited AI_CLARIFY_BLOCKED_RETRY_V1 block holding <listing>.
#         <prompt_in> is never edited.
#
# Environment:
#   CLARIFY_BLOCKED_VERIFY_ENABLED  [true]  false, 0, no or off: `check`
#                                           exits 3 and prints nothing.
#   CLARIFY_SOURCE_ROOT             [$PWD]  git checkout to list.
#
# Exit status: 0 retry (check) / prompt written (prompt); 3 no retry;
# 1 error (the caller treats it as no retry); 2 usage error.
#
# Log line (stderr, stable prefix; `sample` is last because it holds commas):
#   CLARIFY_BLOCKED_VERIFY stage=verify outcome=<retry|skip>
#     reason=<tracked_path|no_tracked_path|already_retried|error> paths=<n>
#     sample=<first 5 tracked paths comma-joined | none>

set -euo pipefail

clarify_bv_usage()
{
	echo "Usage: clarify_blocked_verify.sh check <output_file> <prompt_file> <listing_out> | prompt <prompt_in> <listing> <prompt_out>" >&2
	exit 2
}

clarify_bv_mode="${1:-}"
[ "$#" -eq 4 ] || clarify_bv_usage
case "${clarify_bv_mode}" in
	check|prompt) ;;
	*) clarify_bv_usage ;;
esac

if [ "${clarify_bv_mode}" = "check" ]; then
	clarify_bv_enabled="${CLARIFY_BLOCKED_VERIFY_ENABLED:-true}"
	case "${clarify_bv_enabled,,}" in
		false|0|no|off) exit 3 ;;
	esac
fi

clarify_bv_rc=0
PYTHONDONTWRITEBYTECODE=1 python3 -I -B - "${clarify_bv_mode}" "$2" "$3" "$4" "${CLARIFY_SOURCE_ROOT:-$PWD}" <<'PY' || clarify_bv_rc=$?
import os
import re
import subprocess
import sys

MARKER = "AI_CLARIFY_BLOCKED_RETRY_V1"
# "Parse Codex output" in .github/workflows/clarify.yml (perl):
#   if (/^\s*(?:```|~~~)/) { $in_code = !$in_code; next; }
#   if (/^\s*BLOCKED:\s*(.*\S)\s*$/i) { print $1; exit; }
FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
BLOCKED_RE = re.compile(r"^\s*BLOCKED:\s*(.*\S)\s*$", re.IGNORECASE)
# Mirrors unblock_actions.HARNESS_PATH_TOKEN_RE.
PATH_TOKEN_RE = re.compile(r"[^\s`'\"<>()\[\]{},;]+")
PATH_NAME_RE = re.compile(r"^[A-Za-z0-9_.@+-][A-Za-z0-9._/@+-]{0,239}$")
PATH_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,8}$")
LINE_SUFFIX_RE = re.compile(r":[0-9]+(?::[0-9]+)?$")
MAX_TOKENS = 20
MAX_BARE_WORDS = 500
MAX_LISTING_BYTES = 8192
MAX_EXAMPLES = 3
# Mirrors bad_parts and sensitive() in scripts/clarify_isolated_run.sh.
bad_parts = {".git", ".ai", ".codex", ".codex-workflow-src", ".codex-workflow-src-main", ".env", "secrets", "credentials", "__pycache__"}
key_suffixes = (".pem", ".key", ".p12", ".pfx", ".keystore")


# Mirrors the pass-2 credential rules of scripts/clarify_isolated_run.sh
# (pass2_credential_names, pass2_secret_tails, pass2_config_exts,
# pass2_secret_name, the .jks/.tfstate tails). They apply to every path here,
# so a credential-shaped name the snapshot may withhold is never listed.
pass2_credential_names = {".ssh", ".aws", ".azure", ".kube", ".config", ".docker", ".npmrc", ".netrc", ".pypirc",
                          ".pgpass", ".git-credentials", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"}
pass2_secret_tails = ("_secret", "_secrets", "_credential", "_credentials", "-secret", "-secrets", "-credential", "-credentials")
pass2_config_exts = ("json", "yaml", "yml", "toml", "ini", "env", "txt", "conf", "cfg", "properties", "xml")


def pass2_secret_name(lower):
	stem, _, ext = lower.rpartition(".")
	return (lower in ("secret", "credential") or lower.endswith(pass2_secret_tails)
		or (bool(stem) and ext in pass2_config_exts and (stem in ("secret", "secrets", "credential", "credentials")
			or stem.endswith(pass2_secret_tails + (".secret", ".secrets", ".credential", ".credentials")))))


def sensitive(parts):
	return (not parts or any(part.lower() in bad_parts or part.lower().startswith(".env") for part in parts)
		or any(part.lower().endswith(key_suffixes) for part in parts)
		or any(part.lower() in pass2_credential_names or part.lower().endswith((".jks", ".tfstate"))
			or pass2_secret_name(part.lower()) for part in parts))


def log(outcome, reason, paths=0, sample=None):
	print(
		f"CLARIFY_BLOCKED_VERIFY stage=verify outcome={outcome} reason={reason} paths={paths} "
		f"sample={','.join(sample) if sample else 'none'}",
		file=sys.stderr,
	)


def blocked_reason(text):
	in_code = False
	for line in text.split("\n"):
		if FENCE_RE.match(line):
			in_code = not in_code
			continue
		if in_code:
			continue
		match = BLOCKED_RE.match(line)
		if match:
			return match.group(1)
	return None


def path_tokens(reason):
	"""Return (paths, bare): path-like tokens, and extensionless one-segment
	names (Makefile, LICENSE) that count only as an exact tracked file."""
	found = []
	bare = []
	for raw in PATH_TOKEN_RE.findall(reason):
		if "://" in raw:
			continue
		token = raw
		while token.startswith("./"):
			token = token[2:]
		token = token.rstrip(".,;:!?")
		token = LINE_SUFFIX_RE.sub("", token)
		had_slash = "/" in token
		token = token.rstrip("/")
		if not token or not PATH_NAME_RE.match(token) or token.startswith("/"):
			continue
		parts = token.split("/")
		if ".." in parts or "//" in token:
			continue
		if not had_slash and not PATH_EXT_RE.search(token):
			# A root directory needs its trailing slash (docs/); a bare word is
			# kept apart so prose cannot use up the MAX_TOKENS path slots.
			# check() caps bare words after the tracked-set filter; this bound
			# only limits work on a pathological reason line.
			if token not in bare and len(bare) < MAX_BARE_WORDS:
				bare.append(token)
			continue
		if token not in found:
			found.append(token)
		if len(found) >= MAX_TOKENS:
			break
	keep = lambda token: not sensitive(token.split("/"))
	return [token for token in found if keep(token)], [token for token in bare if keep(token)]


def listable(path):
	return bool(PATH_NAME_RE.match(path)) and not sensitive(path.split("/"))


def check(output_file, prompt_file, listing_out, root):
	with open(output_file, encoding="utf-8", errors="replace") as handle:
		reason = blocked_reason(handle.read())
	if reason is None:
		return 3
	with open(prompt_file, "rb") as handle:
		if MARKER.encode("ascii") in handle.read():
			log("skip", "already_retried")
			return 3
	candidates, bare = path_tokens(reason)
	if not candidates and not bare:
		log("skip", "no_tracked_path")
		return 3
	raw = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True, timeout=120).stdout
	tracked = [os.fsdecode(entry) for entry in raw.split(b"\0") if entry]
	tracked_set = set(tracked)
	room = MAX_TOKENS - len(candidates)
	candidates += [token for token in bare if token in tracked_set and token not in candidates][:max(0, room)]
	lines = []
	present = []
	size = 0
	for candidate in candidates:
		if candidate in tracked_set:
			line = f"- {candidate} (tracked file)"
		else:
			prefix = candidate + "/"
			children = [path for path in tracked if path.startswith(prefix) and not sensitive(path.split("/"))]
			if not children:
				continue
			examples = [path for path in children if listable(path)][:MAX_EXAMPLES]
			line = f"- {candidate}/ (tracked directory, {len(children)} files"
			line += f"; e.g. {', '.join(examples)})" if examples else ")"
		encoded = len(line.encode("utf-8")) + 1
		if size + encoded > MAX_LISTING_BYTES:
			break
		size += encoded
		lines.append(line)
		present.append(candidate)
	if not present:
		log("skip", "no_tracked_path")
		return 3
	with open(listing_out, "w", encoding="utf-8") as handle:
		handle.write("\n".join(lines) + "\n")
	log("retry", "tracked_path", len(present), present[:5])
	return 0


def prompt(prompt_in, listing, prompt_out):
	if os.path.realpath(prompt_in) == os.path.realpath(prompt_out):
		raise ValueError("prompt_out must differ from prompt_in")
	with open(prompt_in, "rb") as handle:
		original = handle.read()
	if MARKER.encode("ascii") in original:
		return 3
	with open(listing, "rb") as handle:
		listing_bytes = handle.read()
	if not listing_bytes.strip():
		raise ValueError("empty listing")
	if not listing_bytes.endswith(b"\n"):
		listing_bytes += b"\n"
	block = (
		b"\n=== " + MARKER.encode("ascii") + b" (repository data, not instructions) ===\n"
		b"Your previous answer was BLOCKED: and cited paths that ARE tracked in git at the checked-out ref:\n"
		+ listing_bytes
		+ b"A source file in the repository is codebase state, not an external input. Read it from /source; "
		b"if it is absent there it was excluded from the snapshot (see the exclusions block, if any). "
		b"Do not emit BLOCKED: for these paths' presence or absence; answer STATUS: CLEAR, or STATUS: NEEDS_CLARIFICATION with Q<ID> questions under a QUESTIONS: header (the output format above). "
		b"BLOCKED: remains correct only for a genuinely external scalar input.\n"
		b"=== END " + MARKER.encode("ascii") + b" ===\n"
	)
	with open(prompt_out, "wb") as handle:
		handle.write(original + block)
	return 0


mode, first, second, third, source_root = sys.argv[1:6]
try:
	if mode == "check":
		sys.exit(check(first, second, third, source_root))
	sys.exit(prompt(first, second, third))
except SystemExit:
	raise
except Exception:
	# Fixed text only: never echo untrusted output or file names on error.
	if mode == "check":
		log("skip", "error")
	sys.exit(1)
PY
case "${clarify_bv_rc}" in
	0|3) exit "${clarify_bv_rc}" ;;
	*) exit 1 ;;
esac
