#!/usr/bin/env python3
"""Build a conservative Python trust-boundary and reverse-caller scope."""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import re
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


SCHEMA = "security_audit_causal_scope.v1"
MAX_FILES = 500
MAX_FILE_BYTES = 1_000_000
MAX_SYMBOLS = 12_000
MAX_CAUSAL_SYMBOLS = 200
MAX_REVERSE_DEPTH = 4


@dataclass(frozen=True)
class Symbol:
	qualified_name: str
	leaf_name: str
	file: str
	start: int
	end: int
	semantic_dump: str
	calls: frozenset[str]
	wildcard_import: bool


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		["git", *args], cwd=repo, text=True, encoding="utf-8", errors="replace",
		capture_output=True, check=False,
	)


def _safe_relative_path(value: str) -> str:
	candidate = PurePosixPath(value)
	if candidate.is_absolute() or ".." in candidate.parts:
		raise ValueError("file must be repository-relative")
	normalized = candidate.as_posix().removeprefix("./")
	if not normalized or normalized == "." or any(ord(char) < 32 for char in normalized):
		raise ValueError("file is invalid")
	return normalized


def _source_at(repo: Path, ref: str, file_name: str) -> str | None:
	result = _git(repo, "show", f"{ref}:{file_name}")
	if result.returncode != 0:
		return None
	if len(result.stdout.encode("utf-8", errors="replace")) > MAX_FILE_BYTES:
		raise ValueError("Python source exceeds analysis bound")
	return result.stdout


def _semantic_dump(node: ast.AST) -> str:
	return ast.dump(node, annotate_fields=True, include_attributes=False)


def _module_import_dump(source: str, file_name: str) -> str:
	tree = ast.parse(source, filename=file_name)
	imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
	return _semantic_dump(ast.Module(body=imports, type_ignores=[]))


class _CallCollector(ast.NodeVisitor):
	def __init__(self) -> None:
		self.calls: set[str] = set()
		self.wildcard_import = False

	def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
		return

	def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
		return

	def visit_ClassDef(self, node: ast.ClassDef) -> None:
		return

	def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
		if any(alias.name == "*" for alias in node.names):
			self.wildcard_import = True
		self.generic_visit(node)

	def visit_Call(self, node: ast.Call) -> None:
		if isinstance(node.func, ast.Name):
			self.calls.add(node.func.id)
		elif isinstance(node.func, ast.Attribute):
			self.calls.add(node.func.attr)
		self.generic_visit(node)


def _symbols_for_source(file_name: str, source: str) -> list[Symbol]:
	tree = ast.parse(source, filename=file_name)
	module_name = file_name[:-3].replace("/", ".")
	symbols: list[Symbol] = []

	def visit_body(body: list[ast.stmt], parents: tuple[str, ...]) -> None:
		for node in body:
			if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
				continue
			qualified_parts = (module_name, *parents, node.name)
			collector = _CallCollector()
			for decorator in getattr(node, "decorator_list", []):
				collector.visit(decorator)
			for child in node.body:
				collector.visit(child)
			semantic_node = ast.Module(
				body=[
					ast.Expr(value=decorator)
					for decorator in getattr(node, "decorator_list", [])
				] + [
					child for child in node.body
					if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
				],
				type_ignores=[],
			)
			symbols.append(
				Symbol(
					qualified_name=".".join(qualified_parts),
					leaf_name=node.name,
					file=file_name,
					start=int(getattr(node, "lineno", 1)),
					end=int(getattr(node, "end_lineno", getattr(node, "lineno", 1))),
					semantic_dump=_semantic_dump(semantic_node),
					calls=frozenset(collector.calls),
					wildcard_import=collector.wildcard_import,
				)
			)
			visit_body(node.body, (*parents, node.name))

	visit_body(tree.body, ())
	return symbols


def _snapshot(repo: Path, ref: str) -> tuple[list[Symbol], dict[str, str]]:
	listing = _git(repo, "ls-tree", "-r", "--name-only", ref)
	if listing.returncode != 0:
		raise ValueError("Git snapshot is unavailable")
	files = sorted(
		_safe_relative_path(line) for line in listing.stdout.splitlines()
		if line.endswith(".py")
	)
	if len(files) > MAX_FILES:
		raise ValueError("Python file graph exceeds analysis bound")
	symbols: list[Symbol] = []
	sources: dict[str, str] = {}
	if not files:
		return symbols, sources
	archive_result = subprocess.run(
		["git", "archive", "--format=tar", ref, "--", *files],
		cwd=repo,
		capture_output=True,
		check=False,
	)
	if archive_result.returncode != 0:
		raise ValueError("Git snapshot archive is unavailable")
	with tarfile.open(fileobj=io.BytesIO(archive_result.stdout), mode="r:") as archive:
		for archive_member in archive.getmembers():
			if not archive_member.isfile():
				continue
			file_name = _safe_relative_path(archive_member.name)
			if file_name not in files or archive_member.size > MAX_FILE_BYTES:
				raise ValueError("Python source exceeds analysis bound")
			extracted_source = archive.extractfile(archive_member)
			if extracted_source is None:
				raise ValueError("Python source is unavailable from archive")
			sources[file_name] = extracted_source.read().decode("utf-8")
	if set(sources) != set(files):
		raise ValueError("Python snapshot contains unsupported file entries")
	for file_name, source in sources.items():
		symbols.extend(_symbols_for_source(file_name, source))
		if len(symbols) > MAX_SYMBOLS:
			raise ValueError("Python symbol graph exceeds analysis bound")
	return symbols, sources


def _target_symbol(symbols: list[Symbol], file_name: str, line: int) -> Symbol | None:
	candidates = [
		symbol for symbol in symbols
		if symbol.file == file_name and symbol.start <= line <= symbol.end
	]
	if not candidates:
		return None
	return min(candidates, key=lambda symbol: (symbol.end - symbol.start, -symbol.start))


def _scope_for_target(
	symbols: list[Symbol], target: Symbol,
) -> tuple[list[Symbol], bool]:
	by_leaf: dict[str, list[Symbol]] = {}
	for symbol in symbols:
		by_leaf.setdefault(symbol.leaf_name, []).append(symbol)
	selected = {target.qualified_name: target}
	frontier = [target]
	ambiguous = False
	for _depth in range(MAX_REVERSE_DEPTH):
		target_names = {symbol.leaf_name for symbol in frontier}
		if any(len(by_leaf.get(name, [])) > 1 for name in target_names):
			ambiguous = True
		callers = [
			symbol for symbol in symbols
			if symbol.qualified_name not in selected and symbol.calls.intersection(target_names)
		]
		if not callers:
			break
		for caller in callers:
			selected[caller.qualified_name] = caller
		if len(selected) > MAX_CAUSAL_SYMBOLS:
			raise ValueError("reverse-call graph exceeds analysis bound")
		frontier = callers
	return sorted(selected.values(), key=lambda item: item.qualified_name), ambiguous


def analyze(repo: Path, file_name: str, line: int, base: str, head: str) -> dict[str, object]:
	head_symbols, head_sources = _snapshot(repo, head)
	target = _target_symbol(head_symbols, file_name, line)
	if target is None:
		raise ValueError("cited line is not inside a Python function or class")
	head_scope, head_ambiguous = _scope_for_target(head_symbols, target)

	base_symbols, base_sources = _snapshot(repo, base)
	base_target = next(
		(symbol for symbol in base_symbols if symbol.qualified_name == target.qualified_name), None
	)
	base_scope: list[Symbol] = []
	base_ambiguous = False
	if base_target is not None:
		base_scope, base_ambiguous = _scope_for_target(base_symbols, base_target)

	causal_files = sorted({symbol.file for symbol in [*base_scope, *head_scope]} | {file_name})
	if len(causal_files) > MAX_CAUSAL_SYMBOLS:
		raise ValueError("causal file set exceeds analysis bound")
	def scope_signatures(scope: list[Symbol], sources: dict[str, str]) -> dict[str, str]:
		rows_by_file: dict[str, list[dict[str, str]]] = {}
		for symbol in scope:
			rows_by_file.setdefault(symbol.file, []).append(
				{"symbol": symbol.qualified_name, "semantic": symbol.semantic_dump}
			)
		return {
			file_name: json.dumps(
				{
					"imports": _module_import_dump(sources[file_name], file_name),
					"symbols": sorted(rows, key=lambda row: row["symbol"]),
				},
				ensure_ascii=True,
				separators=(",", ":"),
				sort_keys=True,
			)
			for file_name, rows in rows_by_file.items()
		}

	base_signatures = scope_signatures(base_scope, base_sources)
	head_signatures = scope_signatures(head_scope, head_sources)
	changed_files = sorted(
		file_name for file_name in causal_files
		if base_signatures.get(file_name) != head_signatures.get(file_name)
	)

	fingerprint_rows = []
	for symbol in head_scope:
		fingerprint_rows.append(
			{
				"symbol": symbol.qualified_name,
				"semantic": symbol.semantic_dump,
				"imports": _module_import_dump(head_sources[symbol.file], symbol.file),
			}
		)
	fingerprint_payload = json.dumps(fingerprint_rows, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
	status = "complete"
	if head_ambiguous or base_ambiguous or any(symbol.wildcard_import for symbol in [*base_scope, *head_scope]):
		status = "indeterminate"
	return {
		"causal_scope_schema": SCHEMA,
		"causal_scope_status": status,
		"causal_scope_fingerprint": "sha256:" + hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest(),
		"causal_scope_files": causal_files,
		"causal_scope_symbol": target.qualified_name[:300],
		"causal_scope_changed_files": changed_files,
	}


def main() -> int:
	parser = argparse.ArgumentParser()
	parser.add_argument("--repo", required=True)
	parser.add_argument("--file", required=True)
	parser.add_argument("--line", required=True, type=int)
	parser.add_argument("--base", required=True)
	parser.add_argument("--head", required=True)
	args = parser.parse_args()
	result: dict[str, object]
	try:
		repo = Path(args.repo).resolve(strict=True)
		file_name = _safe_relative_path(args.file)
		if args.line < 1:
			raise ValueError("line must be positive")
		if re.fullmatch(r"[0-9a-fA-F]{40,64}", args.base) is None or re.fullmatch(
			r"[0-9a-fA-F]{40,64}", args.head
		) is None:
			raise ValueError("base and head must be immutable commit IDs")
		result = analyze(repo, file_name, args.line, args.base, args.head)
	except (OSError, SyntaxError, UnicodeError, ValueError, tarfile.TarError) as exc:
		result = {
			"causal_scope_schema": SCHEMA,
			"causal_scope_status": "indeterminate",
			"causal_scope_fingerprint": "",
			"causal_scope_files": [],
			"causal_scope_symbol": "",
			"causal_scope_changed_files": [],
			"reason": str(exc)[:300],
		}
	print(json.dumps(result, ensure_ascii=True, sort_keys=True))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
