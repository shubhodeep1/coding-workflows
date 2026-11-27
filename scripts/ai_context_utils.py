import base64
import concurrent.futures
import ipaddress
import json
import os
import re
import socket
import urllib.parse
import urllib.request
from typing import Any, Callable


_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((https?://(?:[^()\s]+(?:\([^()\s]*\)[^()\s]*)*))\)", re.IGNORECASE)
_MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\((https?://(?:[^()\s]+(?:\([^()\s]*\)[^()\s]*)*))\)", re.IGNORECASE)
_HTML_IMG_RE = re.compile(r"<img\b[^>]*\bsrc=[\"'](https?://[^\"']+)[\"'][^>]*>", re.IGNORECASE)
_HTML_LINK_RE = re.compile(r"<a\b[^>]*\bhref=[\"'](https?://[^\"']+)[\"'][^>]*>", re.IGNORECASE)

_DOC_EXTENSIONS = {".doc", ".docx", ".odt"}
_TEXT_EXTENSIONS = {
	".txt",
	".md",
	".markdown",
	".log",
	".json",
	".xml",
	".yaml",
	".yml",
	".csv",
	".tsv",
	".ini",
	".cfg",
	".conf",
	".diff",
	".patch",
}


class AttachmentPolicy:
	def __init__(self) -> None:
		self.max_count = _env_int("AI_ATTACHMENT_MAX_COUNT", 8)
		self.max_total_bytes = _env_int("AI_ATTACHMENT_MAX_TOTAL_BYTES", 2 * 1024 * 1024)
		self.max_single_bytes = _env_int("AI_ATTACHMENT_MAX_SINGLE_BYTES", 512 * 1024)
		self.timeout_sec = _env_int("AI_ATTACHMENT_FETCH_TIMEOUT_SEC", 8)
		self.retries = _env_int("AI_ATTACHMENT_FETCH_RETRIES", 2)


def _env_int(name: str, default: int) -> int:
	try:
		return int(os.getenv(name, str(default)))
	except (TypeError, ValueError):
		return default


def _norm(value: Any) -> str:
	if value is None:
		return ""
	return str(value)


def _norm_or_unavailable(value: Any) -> str:
	if value is None:
		return "UNAVAILABLE"
	text = str(value)
	if text.strip() == "":
		return "UNAVAILABLE"
	return text


def _body(value: Any) -> str:
	text = _norm(value).replace("\r\n", "\n")
	if text == "":
		return ""
	return text


def _sort_by_created(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
	return sorted(items, key=lambda item: (_norm(item.get("created_at")), _norm(item.get("id"))))


def build_issue_envelope(issue: dict[str, Any], comments: list[dict[str, Any]], excluded_comment_ids: set[str] | None = None) -> str:
	excluded = excluded_comment_ids or set()
	filtered = []
	for comment in comments:
		comment_id = _norm(comment.get("id"))
		if comment_id in excluded:
			continue
		filtered.append(comment)

	ordered = _sort_by_created(filtered)
	lines = [
		"<issue>",
		f"title: {_norm(issue.get('title'))}",
		f"body: {_body(issue.get('body'))}",
		f"author: {_norm((issue.get('user') or {}).get('login'))}",
		f"created_at: {_norm(issue.get('created_at'))}",
		f"state: {_norm(issue.get('state'))}",
		f"comments_count: {len(ordered)}",
	]
	for index, comment in enumerate(ordered):
		user = comment.get("user") or {}
		lines.append(f"comment[{index}].author: {_norm(user.get('login'))}")
		lines.append(f"comment[{index}].created_at: {_norm(comment.get('created_at'))}")
		lines.append(f"comment[{index}].body: {_body(comment.get('body'))}")
	lines.append("</issue>")
	return "\n".join(lines)


def build_pull_request_envelope(
	pr: dict[str, Any],
	pr_files: list[dict[str, Any]],
	pr_comments: list[dict[str, Any]],
) -> str:
	ordered_files = sorted(pr_files, key=lambda item: _norm(item.get("filename")))
	ordered_comments = _sort_by_created(pr_comments)
	base = pr.get("base") or {}
	head = pr.get("head") or {}
	lines = [
		"<pull_request>",
		f"title: {_norm(pr.get('title'))}",
		f"body: {_body(pr.get('body'))}",
		f"author: {_norm((pr.get('user') or {}).get('login'))}",
		f"created_at: {_norm(pr.get('created_at'))}",
		f"state: {_norm(pr.get('state'))}",
		f"base_ref: {_norm(base.get('ref'))}",
		f"head_ref: {_norm(head.get('ref'))}",
		f"additions: {_norm(pr.get('additions'))}",
		f"deletions: {_norm(pr.get('deletions'))}",
		f"commit_count: {_norm(pr.get('commits'))}",
		f"changed_files_count: {len(ordered_files)}",
	]
	for index, file_item in enumerate(ordered_files):
		lines.append(f"changed_file[{index}].path: {_norm(file_item.get('filename'))}")
		lines.append(f"changed_file[{index}].change_type: {_norm(file_item.get('status'))}")
		lines.append(f"changed_file[{index}].additions: {_norm(file_item.get('additions'))}")
		lines.append(f"changed_file[{index}].deletions: {_norm(file_item.get('deletions'))}")
	lines.append(f"comments_count: {len(ordered_comments)}")
	for index, comment in enumerate(ordered_comments):
		user = comment.get("user") or {}
		lines.append(f"comment[{index}].author: {_norm(user.get('login'))}")
		lines.append(f"comment[{index}].created_at: {_norm(comment.get('created_at'))}")
		lines.append(f"comment[{index}].body: {_body(comment.get('body'))}")
	lines.append("</pull_request>")
	return "\n".join(lines)


def build_pull_request_reviews_envelope(
	reviews: list[dict[str, Any]],
	review_comments: list[dict[str, Any]],
) -> str:
	ordered_reviews = sorted(reviews, key=lambda item: (_norm(item.get("submitted_at")), _norm(item.get("id"))))
	comments_by_review: dict[str, list[dict[str, Any]]] = {}
	for comment in review_comments:
		review_id = _norm(comment.get("pull_request_review_id"))
		if review_id == "":
			continue
		comments_by_review.setdefault(review_id, []).append(comment)
	for review_id, bucket in comments_by_review.items():
		comments_by_review[review_id] = sorted(
			bucket,
			key=lambda item: (
				_norm(item.get("path")),
				_norm(item.get("line")),
				_norm(item.get("id")),
			),
		)

	lines = ["<pull_request_reviews>", f"reviews_count: {len(ordered_reviews)}"]
	for index, review in enumerate(ordered_reviews):
		review_id = _norm(review.get("id"))
		user = review.get("user") or {}
		linked_comments = comments_by_review.get(review_id, [])
		lines.append(f"review[{index}].reviewer: {_norm(user.get('login'))}")
		lines.append(f"review[{index}].submitted_at: {_norm(review.get('submitted_at'))}")
		lines.append(f"review[{index}].state: {_norm(review.get('state'))}")
		lines.append(f"review[{index}].body: {_body(review.get('body'))}")
		lines.append(f"review[{index}].line_comments_count: {len(linked_comments)}")
		for comment_index, comment in enumerate(linked_comments):
			c_user = comment.get("user") or {}
			lines.append(
				f"review[{index}].line_comment[{comment_index}].author: {_norm(c_user.get('login'))}"
			)
			lines.append(
				f"review[{index}].line_comment[{comment_index}].path: {_norm(comment.get('path'))}"
			)
			lines.append(
				f"review[{index}].line_comment[{comment_index}].line: {_norm(comment.get('line'))}"
			)
			lines.append(
				f"review[{index}].line_comment[{comment_index}].body: {_body(comment.get('body'))}"
			)
	lines.append("</pull_request_reviews>")
	return "\n".join(lines)


def build_review_comment_local_context(event_payload: dict[str, Any]) -> str:
	comment = event_payload.get("comment") or {}
	lines = [
		"<review_comment_local_context>",
		f"path: {_norm_or_unavailable(comment.get('path'))}",
		f"line: {_norm_or_unavailable(comment.get('line'))}",
		f"original_line: {_norm_or_unavailable(comment.get('original_line'))}",
		f"diff_hunk: {_norm_or_unavailable(comment.get('diff_hunk'))}",
		f"commit_id: {_norm_or_unavailable(comment.get('commit_id'))}",
		f"original_commit_id: {_norm_or_unavailable(comment.get('original_commit_id'))}",
		"</review_comment_local_context>",
	]
	return "\n".join(lines)


def extract_attachment_candidates(text: str) -> list[dict[str, Any]]:
	candidates: list[dict[str, Any]] = []
	for source, pattern in [
		("markdown_image", _MARKDOWN_IMAGE_RE),
		("markdown_link", _MARKDOWN_LINK_RE),
		("html_image", _HTML_IMG_RE),
		("html_link", _HTML_LINK_RE),
	]:
		for match in pattern.finditer(text):
			start, end = match.span(1)
			url = match.group(1)
			candidates.append({"source": source, "url": url, "start": start, "end": end})
	return sorted(candidates, key=lambda item: (item["start"], item["end"], item["url"]))


def _normalize_url(url: str) -> str:
	parts = urllib.parse.urlsplit(url)
	query = parts.query
	netloc = parts.netloc.rsplit("@", 1)[-1]
	return urllib.parse.urlunsplit((parts.scheme.lower(), netloc.lower(), parts.path, query, ""))


def _is_private_or_loopback_host(hostname: str) -> bool:
	host = hostname.strip().strip("[]").lower()
	if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
		return True
	try:
		ip_obj = ipaddress.ip_address(host)
	except ValueError:
		pass
	else:
		if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local:
			return True
	# Hostname is not a literal IP, perform DNS resolution to check all resolved addresses
	with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
		try:
			future = executor.submit(socket.getaddrinfo, host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
			addr_info = future.result(timeout=5)
		except (concurrent.futures.TimeoutError, concurrent.futures.CancelledError, socket.gaierror, OSError):
			# DNS resolution failed, cannot validate - treat as unsafe
			return True
	if not addr_info:
		return True
	for family, socktype, proto, canonname, sockaddr in addr_info:
		try:
			ip_obj = ipaddress.ip_address(sockaddr[0])
			if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local:
				return True
		except (ValueError, IndexError):
			continue
	return False


def _is_supported_mime(mime_type: str, filename: str) -> bool:
	mime = mime_type.lower()
	if mime.startswith("image/"):
		return True
	if mime.startswith("text/"):
		return True
	if mime in {"application/json", "application/xml", "text/xml", "application/x-yaml", "application/yaml"}:
		return True
	if mime == "application/octet-stream":
		lower_name = filename.lower()
		for suffix in (".txt", ".log", ".md", ".json", ".yaml", ".yml", ".diff", ".patch"):
			if lower_name.endswith(suffix):
				return True
	return False


def _sanitize_filename(name: str, fallback_index: int) -> str:
	candidate = re.sub(r"[^A-Za-z0-9._-]", "_", name.strip())
	candidate = candidate.strip("._")
	if candidate == "":
		return f"attachment_{fallback_index}.bin"
	return candidate[:120]


def _guess_filename(url: str, headers: dict[str, str], fallback_index: int) -> str:
	disposition = headers.get("content-disposition", "")
	match = re.search(r'filename="?([^";]+)"?', disposition, flags=re.IGNORECASE)
	if match:
		return _sanitize_filename(match.group(1), fallback_index)
	path = urllib.parse.urlsplit(url).path
	base = os.path.basename(path)
	return _sanitize_filename(base, fallback_index)


def classify_attachment_type(filename: str, mime_type: str) -> str:
	lower_name = _norm(filename).lower()
	lower_mime = _norm(mime_type).split(";", 1)[0].strip().lower()
	_, ext = os.path.splitext(lower_name)

	if lower_mime.startswith("image/") or ext in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}:
		return "image"
	if lower_mime == "application/pdf" or ext == ".pdf":
		return "pdf"
	if ext in _DOC_EXTENSIONS or lower_mime in {
		"application/msword",
		"application/vnd.openxmlformats-officedocument.wordprocessingml.document",
		"application/vnd.oasis.opendocument.text",
	}:
		return "doc"
	if lower_mime.startswith("text/") or lower_mime in {
		"application/json",
		"application/xml",
		"text/xml",
		"application/x-yaml",
		"application/yaml",
	}:
		return "text"
	if ext in _TEXT_EXTENSIONS:
		return "text"
	return "other"


def build_attachment_bundle_section(bundle_root: str, manifest: dict[str, Any] | list[dict[str, Any]]) -> str:
	if isinstance(manifest, dict):
		files = manifest.get("files")
		entries = files if isinstance(files, list) else []
	else:
		entries = manifest

	lines = [
		"ATTACHMENT BUNDLE",
		f"bundle_root: {bundle_root}",
		f"manifest_path: {os.path.join(bundle_root, 'manifest.json')}",
		f"entries_count: {len(entries)}",
	]

	for index, item in enumerate(entries):
		if not isinstance(item, dict):
			continue
		lines.append(f"entry[{index}].source_url: {_norm(item.get('source_url'))}")
		lines.append(f"entry[{index}].status: {_norm(item.get('status'))}")
		lines.append(f"entry[{index}].type: {_norm(item.get('detected_type'))}")
		lines.append(f"entry[{index}].original_path: {_norm(item.get('original_path'))}")
		derivatives = item.get("derivatives")
		if isinstance(derivatives, dict):
			for key in sorted(derivatives.keys()):
				value = derivatives.get(key)
				if isinstance(value, list):
					for d_idx, d_path in enumerate(value):
						lines.append(f"entry[{index}].derivatives.{key}[{d_idx}]: {_norm(d_path)}")
				else:
					lines.append(f"entry[{index}].derivatives.{key}: {_norm(value)}")
		if item.get("error"):
			lines.append(f"entry[{index}].error: {_norm(item.get('error'))}")

	return "\n".join(lines)


def fetch_attachment(
	url: str,
	policy: AttachmentPolicy,
	index: int,
	max_remaining_bytes: int,
) -> dict[str, Any]:
	parts = urllib.parse.urlsplit(url)
	if parts.scheme.lower() != "https":
		return {"status": "skip", "skip_reason": "non_https_url"}
	if not parts.hostname:
		return {"status": "skip", "skip_reason": "missing_hostname"}
	if _is_private_or_loopback_host(parts.hostname):
		return {"status": "skip", "skip_reason": "private_or_loopback_host"}
	try:
		port = parts.port
	except ValueError:
		return {"status": "skip", "skip_reason": "invalid_port"}
	host_for_request = parts.hostname
	if ":" in host_for_request and not host_for_request.startswith("["):
		host_for_request = f"[{host_for_request}]"
	if port:
		host_for_request = f"{host_for_request}:{port}"
	clean_url = urllib.parse.urlunsplit((parts.scheme, host_for_request, parts.path, parts.query, parts.fragment))

	headers = {"User-Agent": "codex-ai-context-builder/1.0"}
	gh_token = os.getenv("GH_TOKEN", "")
	hostname = parts.hostname.lower()
	# Use exact match or subdomain match to prevent leaking token to lookalike domains
	is_github_host = (
		hostname == "github.com"
		or hostname.endswith(".github.com")
		or hostname == "githubusercontent.com"
		or hostname.endswith(".githubusercontent.com")
	)
	if gh_token and is_github_host:
		headers["Authorization"] = f"Bearer {gh_token}"

	request = urllib.request.Request(clean_url, headers=headers)
	try:
		with urllib.request.urlopen(request, timeout=policy.timeout_sec) as response:
			try:
				final_parts = urllib.parse.urlsplit(response.geturl())
			except (AttributeError, TypeError, ValueError):
				return {"status": "skip", "skip_reason": "invalid_redirect_url"}
			if final_parts.scheme.lower() != "https":
				return {"status": "skip", "skip_reason": "redirect_to_non_https"}
			final_hostname = final_parts.hostname
			if final_hostname and _is_private_or_loopback_host(final_hostname):
				return {"status": "skip", "skip_reason": "redirect_to_private"}
			response_headers = {key.lower(): value for key, value in response.headers.items()}
			content_length = response_headers.get("content-length")
			if content_length:
				try:
					size_hint = int(content_length)
				except ValueError:
					size_hint = 0
				if size_hint > policy.max_single_bytes:
					return {"status": "skip", "skip_reason": "attachment_too_large_single"}
				if size_hint > max_remaining_bytes:
					return {"status": "skip", "skip_reason": "attachment_too_large_total"}
			content = response.read(policy.max_single_bytes + 1)
	except (urllib.error.URLError, TimeoutError, OSError) as e:
		return {"status": "skip", "skip_reason": f"fetch_error:{type(e).__name__}"}

	if len(content) > policy.max_single_bytes:
		return {"status": "skip", "skip_reason": "attachment_too_large_single"}
	if len(content) > max_remaining_bytes:
		return {"status": "skip", "skip_reason": "attachment_too_large_total"}

	mime_type = response_headers.get("content-type", "application/octet-stream").split(";")[0].strip().lower()
	filename = _guess_filename(url, response_headers, index)
	if not _is_supported_mime(mime_type, filename):
		return {"status": "skip", "skip_reason": "unsupported_mime"}

	part_type = "image" if mime_type.startswith("image/") else "text_or_file"
	item = {
		"status": "ok",
		"filename": filename,
		"mime_type": mime_type,
		"size_bytes": len(content),
		"content_base64": base64.b64encode(content).decode("ascii"),
		"part_type": part_type,
	}
	if mime_type.startswith("text/") or mime_type in {
		"application/json",
		"application/xml",
		"text/xml",
		"application/x-yaml",
		"application/yaml",
	}:
		item["text_preview"] = content.decode("utf-8", errors="replace")[:4000]
	return item


def ingest_attachments(
	text: str,
	candidates: list[dict[str, Any]],
	policy: AttachmentPolicy | None = None,
	fetcher: Callable[[str, AttachmentPolicy, int, int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
	active_policy = policy or AttachmentPolicy()
	active_fetcher = fetcher or fetch_attachment
	manifest: list[dict[str, Any]] = []
	parts: list[dict[str, Any]] = []
	url_to_token: dict[str, str] = {}
	seen_urls: set[str] = set()
	used_tokens: set[str] = set()
	total_bytes = 0
	success_count = 0

	for candidate in candidates:
		raw_url = candidate["url"]
		normalized_url = _normalize_url(raw_url)
		if normalized_url in seen_urls:
			manifest.append(
				{
					"source_url": raw_url,
					"normalized_url": normalized_url,
					"status": "skip",
					"skip_reason": "duplicate_url",
				}
			)
			continue
		seen_urls.add(normalized_url)

		if success_count >= active_policy.max_count:
			manifest.append(
				{
					"source_url": raw_url,
					"normalized_url": normalized_url,
					"status": "skip",
					"skip_reason": "max_attachment_count_reached",
				}
			)
			continue

		attempt = 0
		result: dict[str, Any] | None = None
		while attempt <= active_policy.retries:
			attempt += 1
			try:
				result = active_fetcher(raw_url, active_policy, success_count + 1, active_policy.max_total_bytes - total_bytes)
				break
			except Exception as exc:
				if attempt > active_policy.retries:
					result = {"status": "skip", "skip_reason": f"fetch_error:{exc.__class__.__name__}"}
					break

		if not result or result.get("status") != "ok":
			skip_reason = "unknown_fetch_error"
			if result:
				skip_reason = _norm(result.get("skip_reason")) or skip_reason
			manifest.append(
				{
					"source_url": raw_url,
					"normalized_url": normalized_url,
					"status": "skip",
					"skip_reason": skip_reason,
				}
			)
			continue

		size_bytes = int(result.get("size_bytes", 0))
		if total_bytes + size_bytes > active_policy.max_total_bytes:
			manifest.append(
				{
					"source_url": raw_url,
					"normalized_url": normalized_url,
					"status": "skip",
					"skip_reason": "attachment_too_large_total",
				}
			)
			continue

		total_bytes += size_bytes
		success_count += 1
		filename = _norm(result.get("filename"))
		token_base = f"@{filename}"
		token = token_base
		token_index = 2
		while token in used_tokens:
			token = f"{token_base}-{token_index}"
			token_index += 1
		used_tokens.add(token)
		url_to_token[normalized_url] = token

		part = {
			"type": "attachment",
			"token": token,
			"filename": filename,
			"mime_type": _norm(result.get("mime_type")),
			"size_bytes": size_bytes,
			"part_type": _norm(result.get("part_type")),
			"content_base64": _norm(result.get("content_base64")),
		}
		if "text_preview" in result:
			part["text_preview"] = _norm(result.get("text_preview"))
		parts.append(part)
		manifest.append(
			{
				"source_url": raw_url,
				"normalized_url": normalized_url,
				"status": "ok",
				"token": token,
				"filename": filename,
				"mime_type": _norm(result.get("mime_type")),
				"size_bytes": size_bytes,
			}
		)

	rewritten = text
	for candidate in sorted(candidates, key=lambda item: item["start"], reverse=True):
		normalized_url = _normalize_url(candidate["url"])
		token = url_to_token.get(normalized_url)
		if not token:
			continue
		start = candidate["start"]
		end = candidate["end"]
		rewritten = rewritten[:start] + token + rewritten[end:]

	return {
		"rewritten_text": rewritten,
		"parts": parts,
		"manifest": manifest,
		"stats": {
			"discovered": len(candidates),
			"ingested": len(parts),
			"skipped": len([item for item in manifest if item.get("status") != "ok"]),
			"total_bytes": total_bytes,
		},
	}


def build_prompt_parts(base_text: str, ingestion_result: dict[str, Any]) -> list[dict[str, Any]]:
	parts = [{"type": "text", "text": ingestion_result.get("rewritten_text", base_text)}]
	for part in ingestion_result.get("parts", []):
		parts.append(part)
	return parts


def append_structured_context_to_file(path: str, blocks: list[str]) -> None:
	with open(path, "w", encoding="utf-8") as handle:
		handle.write("\n\n".join(blocks) + "\n")


def parse_json(raw: str) -> Any:
	if not raw.strip():
		return []
	loaded = json.loads(raw)
	if isinstance(loaded, list):
		return loaded
	return [loaded]
