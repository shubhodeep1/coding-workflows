#!/usr/bin/env python3
"""Local slop-scan heuristics for review_autofix."""

from __future__ import annotations

import argparse
import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

HEREDOC_START_RE = re.compile(
	r"""
	\bpython3\b
	[^\n]*?
	(?P<operator><<-?)
	\s*
	(?P<quote>['"])
	(?P<tag>[A-Za-z_][A-Za-z0-9_]*)
	(?P=quote)
	.*$
	""",
	re.VERBOSE,
)
SAFE_HELPER_TOKEN_RE = re.compile(r"(?i)(safe|quiet|cleanup|best_effort|besteffort)")
UNLINK_TOKEN_RE = re.compile(r"(?i)(unlink|remove)")
SAFE_HELPER_HINT_RE = re.compile(
	r"(?i)(best[- ]effort|ignore (?:missing|enoent)|cleanup helper|safe unlink|quiet cleanup)"
)


@dataclass(frozen=True)
class SourceBlock:
	path: str
	source: str
	line_offset: int
	kind: str


def _parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="Detect a small set of AI-code-pattern smells in changed scripts."
	)
	parser.add_argument("--changed-files", required=True, type=Path)
	parser.add_argument("--repo-root", required=True, type=Path)
	parser.add_argument("--output", required=True, type=Path)
	return parser.parse_args()


def _normalize_path(path: str) -> str:
	normalized = path.replace("\\", "/")
	while normalized.startswith("./"):
		normalized = normalized[2:]
	return normalized


def _is_supported_path(path: str) -> bool:
	normalized = _normalize_path(path)
	if normalized.startswith("scripts/") and normalized.endswith((".py", ".sh")):
		return True
	if normalized.startswith("validation/") and normalized.endswith(".sh"):
		return True
	return False


def _load_changed_paths(path: Path) -> list[str]:
	return [
		_normalize_path(line.strip())
		for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
		if line.strip()
	]


def _relative_path(path: Path, repo_root: Path) -> str:
	try:
		return path.resolve().relative_to(repo_root.resolve()).as_posix()
	except ValueError:
		return path.as_posix()


def _is_docstring_expr(node: ast.stmt) -> bool:
	if not isinstance(node, ast.Expr):
		return False
	value_node = getattr(node, "value", None)
	if isinstance(value_node, ast.Constant):
		return isinstance(value_node.value, str)
	return isinstance(value_node, ast.Str)


def _dotted_name(node: ast.AST | None) -> str | None:
	if node is None:
		return None
	if isinstance(node, ast.Name):
		return node.id
	if isinstance(node, ast.Attribute):
		base = _dotted_name(node.value)
		return f"{base}.{node.attr}" if base else node.attr
	if isinstance(node, ast.Call):
		return _dotted_name(node.func)
	return None


def _iter_nodes(nodes: Iterable[ast.stmt]) -> Iterable[ast.AST]:
	for root in nodes:
		yield root
		for child in ast.walk(root):
			if child is root:
				continue
			yield child


def _is_file_operation_call(call: ast.Call) -> bool:
	dotted = _dotted_name(call.func)
	if dotted in {"os.unlink", "os.remove"}:
		return True
	return isinstance(call.func, ast.Attribute) and call.func.attr == "unlink"


def _is_process_kill_call(call: ast.Call) -> bool:
	dotted = _dotted_name(call.func)
	if dotted in {"os.kill", "os.killpg"}:
		return True
	return isinstance(call.func, ast.Attribute) and call.func.attr in {"kill", "terminate"}


def _matching_calls(nodes: Iterable[ast.stmt], predicate) -> list[str]:
	matches: list[str] = []
	for child in _iter_nodes(nodes):
		if not isinstance(child, ast.Call):
			continue
		if not predicate(child):
			continue
		matches.append(_dotted_name(child.func) or getattr(child.func, "attr", "call"))
	return matches


def _build_parent_map(tree: ast.AST) -> dict[int, ast.AST]:
	parent_map: dict[int, ast.AST] = {}
	for parent in ast.walk(tree):
		for child in ast.iter_child_nodes(parent):
			parent_map[id(child)] = parent
	return parent_map


def _nearest_enclosing_function(node: ast.AST, parent_map: dict[int, ast.AST]) -> ast.AST | None:
	current = parent_map.get(id(node))
	while current is not None:
		if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
			return current
		current = parent_map.get(id(current))
	return None


def _try_node_types() -> tuple[type[ast.AST], ...]:
	try_star = getattr(ast, "TryStar", None)
	return (ast.Try, try_star) if try_star is not None else (ast.Try,)


def _has_enclosing_try(node: ast.AST, stop_node: ast.AST, parent_map: dict[int, ast.AST]) -> bool:
	current = parent_map.get(id(node))
	while current is not None and current is not stop_node:
		if isinstance(current, _try_node_types()) or isinstance(current, (ast.With, ast.AsyncWith)):
			return True
		current = parent_map.get(id(current))
	return False


def _is_empty_pass_handler(handler: ast.ExceptHandler) -> bool:
	body = [stmt for stmt in handler.body if not _is_docstring_expr(stmt)]
	return bool(body) and all(isinstance(stmt, ast.Pass) for stmt in body)


def _excerpt(source_lines: list[str], start_line: int, end_line: int) -> str:
	start = max(1, start_line)
	end = min(len(source_lines), max(start, end_line))
	return "\n".join(source_lines[start - 1:end])


def _outer_line(block: SourceBlock, inner_line: int) -> int:
	return block.line_offset + inner_line


def _best_effort_cleanup_helper(
	node: ast.AST,
	parent_map: dict[int, ast.AST],
	source_lines: list[str],
) -> bool:
	function_node = _nearest_enclosing_function(node, parent_map)
	function_name = function_node.name if isinstance(function_node, (ast.FunctionDef, ast.AsyncFunctionDef)) else ""
	if function_name and SAFE_HELPER_TOKEN_RE.search(function_name) and UNLINK_TOKEN_RE.search(function_name):
		return True
	if isinstance(function_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
		docstring = ast.get_docstring(function_node) or ""
		if SAFE_HELPER_HINT_RE.search(docstring):
			return True
	start = max(1, getattr(node, "lineno", 1) - 2)
	end = min(len(source_lines), getattr(node, "end_lineno", getattr(node, "lineno", 1)) + 2)
	return bool(SAFE_HELPER_HINT_RE.search("\n".join(source_lines[start - 1:end])))


def _finding(
	block: SourceBlock,
	rule_id: str,
	line: int,
	message: str,
	excerpt: str,
	function_name: str | None = None,
	not_to_fix_reason: str | None = None,
	suppression_hint: str | None = None,
) -> dict[str, Any]:
	finding: dict[str, Any] = {
		"rule_id": rule_id,
		"path": block.path,
		"line": _outer_line(block, line),
		"message": message,
		"excerpt": excerpt,
		"source_kind": block.kind,
	}
	if function_name:
		finding["function"] = function_name
	if not_to_fix_reason:
		finding["not_to_fix_reason"] = not_to_fix_reason
	if suppression_hint:
		finding["suppression_hint"] = suppression_hint
	return finding


def _scan_python_block(block: SourceBlock) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
	source_lines = block.source.splitlines()
	findings: list[dict[str, Any]] = []
	suppressed_findings: list[dict[str, Any]] = []
	errors: list[dict[str, Any]] = []

	try:
		tree = ast.parse(block.source or "\n", filename=block.path)
	except SyntaxError as exc:
		errors.append(
			{
				"path": block.path,
				"line": _outer_line(block, exc.lineno or 1),
				"message": f"failed to parse {block.kind}: {exc.msg}",
			}
		)
		return findings, suppressed_findings, errors

	parent_map = _build_parent_map(tree)
	for node in ast.walk(tree):
		if isinstance(node, _try_node_types()):
			bare_pass_handlers = [handler for handler in node.handlers if handler.type is None and _is_empty_pass_handler(handler)]
			if not bare_pass_handlers:
				continue

			file_ops = _matching_calls(node.body, _is_file_operation_call)
			process_kills = _matching_calls(node.body, _is_process_kill_call)
			statement_count = sum(1 for stmt in node.body if not _is_docstring_expr(stmt))
			function_node = _nearest_enclosing_function(node, parent_map)
			function_name = function_node.name if isinstance(function_node, (ast.FunctionDef, ast.AsyncFunctionDef)) else None

			for handler in bare_pass_handlers:
				excerpt = _excerpt(
					source_lines,
					getattr(node, "lineno", handler.lineno),
					getattr(handler, "end_lineno", handler.lineno),
				)
				if process_kills:
					findings.append(
						_finding(
							block,
							"empty_catch_process_kill",
							handler.lineno,
							"Bare `except: pass` around process termination hides permission and process-state errors.",
							excerpt,
							function_name=function_name,
						)
					)
					continue

				if file_ops:
					finding = _finding(
						block,
						"empty_catch_file_op",
						handler.lineno,
						"Bare `except: pass` around file deletion hides unexpected filesystem errors.",
						excerpt,
						function_name=function_name,
					)
					if _best_effort_cleanup_helper(node, parent_map, source_lines):
						finding["not_to_fix_reason"] = "best_effort_cleanup_helper"
						finding["suppression_hint"] = "best-effort cleanup helper"
						suppressed_findings.append(finding)
					else:
						findings.append(finding)
					continue

				if statement_count >= 5:
					findings.append(
						_finding(
							block,
							"bare_except_pass_hides_nontrivial_work",
							handler.lineno,
							"Bare `except: pass` hides failures across a non-trivial try block.",
							excerpt,
							function_name=function_name,
						)
					)

		if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Await):
			continue

		function_node = _nearest_enclosing_function(node, parent_map)
		if not isinstance(function_node, ast.AsyncFunctionDef):
			continue
		if _has_enclosing_try(node, function_node, parent_map):
			continue

		findings.append(
			_finding(
				block,
				"redundant_return_await",
				node.lineno,
				"`return await` outside an enclosing `try` adds ceremony without changing control flow.",
				_excerpt(source_lines, node.lineno, getattr(node, "end_lineno", node.lineno)),
				function_name=function_node.name,
			)
		)

	return findings, suppressed_findings, errors


def _extract_python_heredoc_blocks(path: str, source: str) -> tuple[list[SourceBlock], list[dict[str, Any]]]:
	blocks: list[SourceBlock] = []
	errors: list[dict[str, Any]] = []
	lines = source.splitlines()
	line_index = 0

	while line_index < len(lines):
		match = HEREDOC_START_RE.search(lines[line_index])
		if match is None:
			line_index += 1
			continue

		tag = match.group("tag")
		allow_tabs = match.group("operator") == "<<-"
		body_lines: list[str] = []
		cursor = line_index + 1

		while cursor < len(lines):
			candidate = lines[cursor].lstrip("\t") if allow_tabs else lines[cursor]
			if candidate == tag:
				body = "\n".join(body_lines)
				if body_lines:
					body += "\n"
				blocks.append(
					SourceBlock(
						path=path,
						source=body,
						line_offset=line_index + 1,
						kind="python_heredoc",
					)
				)
				break
			body_lines.append(lines[cursor])
			cursor += 1
		else:
			errors.append(
				{
					"path": path,
					"line": line_index + 1,
					"message": f"unterminated python3 heredoc {tag!r}",
				}
			)
			return blocks, errors

		line_index = cursor + 1

	return blocks, errors


def collect_scan_result(
	paths: list[str],
	repo_root: Path,
	restrict_scope: bool = True,
) -> dict[str, Any]:
	scanned_files: list[str] = []
	findings: list[dict[str, Any]] = []
	suppressed_findings: list[dict[str, Any]] = []
	errors: list[dict[str, Any]] = []
	seen_paths: set[str] = set()

	for raw_path in paths:
		normalized_path = _normalize_path(raw_path)
		if not normalized_path:
			continue
		if restrict_scope and not _is_supported_path(normalized_path):
			continue

		candidate_path = Path(normalized_path)
		absolute_path = candidate_path if candidate_path.is_absolute() else repo_root / candidate_path
		if not absolute_path.is_file():
			continue

		relative_path = _relative_path(absolute_path, repo_root)
		if relative_path not in seen_paths:
			scanned_files.append(relative_path)
			seen_paths.add(relative_path)

		source = absolute_path.read_text(encoding="utf-8", errors="replace")
		blocks: list[SourceBlock]
		file_errors: list[dict[str, Any]]
		if absolute_path.suffix == ".py":
			blocks = [SourceBlock(path=relative_path, source=source, line_offset=0, kind="python")]
			file_errors = []
		else:
			blocks, file_errors = _extract_python_heredoc_blocks(relative_path, source)
		errors.extend(file_errors)

		for block in blocks:
			block_findings, block_suppressed, block_errors = _scan_python_block(block)
			findings.extend(block_findings)
			suppressed_findings.extend(block_suppressed)
			errors.extend(block_errors)

	def finding_sort_key(finding: dict[str, Any]) -> tuple[Any, Any, Any]:
		return (finding["path"], finding["line"], finding["rule_id"])

	result: dict[str, Any] = {
		"schema_version": 1,
		"collection_status": "ok",
		"scanned_files": sorted(scanned_files),
		"findings": sorted(findings, key=finding_sort_key),
		"suppressed_findings": sorted(suppressed_findings, key=finding_sort_key),
	}
	if errors:
		result["errors"] = sorted(
			errors,
			key=lambda error: (error["path"], error.get("line", 0), error["message"]),
		)
	return result


def main() -> int:
	args = _parse_args()
	result = collect_scan_result(
		paths=_load_changed_paths(args.changed_files),
		repo_root=args.repo_root.resolve(),
	)
	args.output.parent.mkdir(parents=True, exist_ok=True)
	args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
