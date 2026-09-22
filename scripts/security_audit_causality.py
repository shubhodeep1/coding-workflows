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
	references: frozenset[str]
	wildcard_import: bool
	indeterminate: bool


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
		self.references: set[str] = set()
		self.wildcard_import = False
		self.indeterminate = False

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

	def visit_Name(self, node: ast.Name) -> None:
		if isinstance(node.ctx, ast.Load):
			self.references.add(node.id)

	def visit_Call(self, node: ast.Call) -> None:
		if isinstance(node.func, ast.Name):
			self.references.add(node.func.id)
			if node.func.id in {"eval", "exec", "__import__"}:
				self.indeterminate = True
		elif isinstance(node.func, ast.Attribute):
			self.references.add(node.func.attr)
			if node.func.attr in {"import_module", "getattr", "setattr"}:
				self.indeterminate = True
		self.generic_visit(node)

	def visit_Subscript(self, node: ast.Subscript) -> None:
		if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
			if node.value.func.id in {"globals", "locals", "vars"}:
				self.indeterminate = True
		self.generic_visit(node)


def _symbols_for_source(file_name: str, source: str) -> list[Symbol]:
	tree = ast.parse(source, filename=file_name)
	module_name = file_name[:-3].replace("/", ".")
	symbols: list[Symbol] = []
	module_collector = _CallCollector()
	module_body: list[ast.stmt] = []
	module_indeterminate = False
	for top_level_node in tree.body:
		if isinstance(top_level_node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
			for decorator in getattr(top_level_node, "decorator_list", []):
				module_body.append(ast.Expr(value=decorator))
				module_collector.visit(decorator)
			continue
		module_body.append(top_level_node)
		module_collector.visit(top_level_node)
		if not isinstance(
			top_level_node,
			(ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Expr, ast.Pass),
		):
			module_indeterminate = True
	symbols.append(
		Symbol(
			qualified_name=f"{module_name}.__module__",
			leaf_name="__module__",
			file=file_name,
			start=1,
			end=max((int(getattr(node, "end_lineno", 1)) for node in tree.body), default=1),
			semantic_dump=_semantic_dump(ast.Module(body=module_body, type_ignores=[])),
			references=frozenset(module_collector.references),
			wildcard_import=module_collector.wildcard_import,
			indeterminate=module_indeterminate or module_collector.indeterminate,
		)
	)

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
					references=frozenset(collector.references),
					wildcard_import=collector.wildcard_import,
					indeterminate=collector.indeterminate,
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
	return min(
		candidates,
		key=lambda symbol: (
			symbol.leaf_name == "__module__",
			symbol.end - symbol.start,
			-symbol.start,
		),
	)


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
			if symbol.qualified_name not in selected and symbol.references.intersection(target_names)
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
	if head_ambiguous or base_ambiguous or any(
		symbol.wildcard_import or symbol.indeterminate for symbol in [*base_scope, *head_scope]
	):
		status = "indeterminate"
	return {
		"causal_scope_schema": SCHEMA,
		"causal_scope_status": status,
		"causal_scope_fingerprint": "sha256:" + hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest(),
		"causal_scope_files": causal_files,
		"causal_scope_symbol": target.qualified_name[:300],
		"causal_scope_changed_files": changed_files,
	}


def _load_object(path: Path, label: str) -> dict[str, object]:
	try:
		payload = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, UnicodeError, json.JSONDecodeError) as exc:
		raise ValueError(f"{label} JSON is unavailable or malformed") from exc
	if not isinstance(payload, dict):
		raise ValueError(f"{label} must be a JSON object")
	return payload


def _revalidate_waiver(
	repo: Path,
	audited_head: str,
	current_head: str,
	finding: dict[str, object],
	waiver: dict[str, object],
) -> dict[str, object]:
	key_pattern = re.compile(r"sha256:[0-9a-f]{64}")
	sha_pattern = re.compile(r"[0-9a-fA-F]{40,64}")
	finding_key = str(finding.get("waiver_match_key") or "")
	waiver_key = str(waiver.get("waiver_match_key") or "")
	if key_pattern.fullmatch(finding_key) is None or finding_key != waiver_key:
		return {"valid": False, "reason": "waiver key mismatch"}
	if sha_pattern.fullmatch(audited_head) is None or sha_pattern.fullmatch(current_head) is None:
		return {"valid": False, "reason": "immutable audit heads are required"}
	if str(waiver.get("audited_head_sha") or "") != audited_head:
		return {"valid": False, "reason": "waiver audited head mismatch"}
	for row in (finding, waiver):
		if (
			row.get("causal_scope_schema") != SCHEMA
			or row.get("causal_scope_status") != "complete"
			or key_pattern.fullmatch(str(row.get("causal_scope_fingerprint") or "")) is None
			or not isinstance(row.get("causal_scope_files"), list)
		):
			return {"valid": False, "reason": "causal scope is incomplete"}
	if (
		finding.get("causal_scope_fingerprint") != waiver.get("causal_scope_fingerprint")
		or finding.get("causal_scope_files") != waiver.get("causal_scope_files")
	):
		return {"valid": False, "reason": "persisted causal scope mismatch"}
	ancestor = _git(repo, "merge-base", "--is-ancestor", audited_head, current_head)
	if ancestor.returncode != 0:
		return {"valid": False, "reason": "audited head is not an ancestor"}
	file_name = _safe_relative_path(str(finding.get("file") or ""))
	line = finding.get("line")
	if isinstance(line, bool) or not isinstance(line, int) or line < 1:
		return {"valid": False, "reason": "finding line is invalid"}
	try:
		current_scope = analyze(repo, file_name, line, audited_head, current_head)
	except (OSError, SyntaxError, UnicodeError, ValueError, tarfile.TarError):
		return {"valid": False, "reason": "current causal scope is unavailable"}
	if current_scope.get("causal_scope_status") != "complete":
		return {"valid": False, "reason": "current causal scope is indeterminate"}
	if (
		current_scope.get("causal_scope_fingerprint") != finding.get("causal_scope_fingerprint")
		or current_scope.get("causal_scope_files") != finding.get("causal_scope_files")
	):
		return {"valid": False, "reason": "current causal scope changed"}
	changed_result = subprocess.run(
		["git", "diff", "--name-only", "-z", f"{audited_head}..{current_head}"],
		cwd=repo,
		capture_output=True,
		check=False,
	)
	if changed_result.returncode != 0:
		return {"valid": False, "reason": "changed path set is unavailable"}
	try:
		changed_paths = [
			_safe_relative_path(raw.decode("utf-8"))
			for raw in changed_result.stdout.split(b"\0")
			if raw
		]
	except (UnicodeError, ValueError):
		return {"valid": False, "reason": "changed path set is invalid"}
	causal_files = set(str(path) for path in current_scope["causal_scope_files"])
	try:
		current_symbols, _current_sources = _snapshot(repo, current_head)
	except (OSError, SyntaxError, UnicodeError, ValueError, tarfile.TarError):
		return {"valid": False, "reason": "repository causal graph is unavailable"}
	for changed_path in changed_paths:
		if changed_path in causal_files:
			return {"valid": False, "reason": "causal file changed"}
		path_suffix = PurePosixPath(changed_path).suffix.lower()
		if path_suffix == ".py":
			# The complete repository-wide Python graph above proves this module
			# does not reference the sink or any selected reverse caller.
			changed_symbols = [symbol for symbol in current_symbols if symbol.file == changed_path]
			if not changed_symbols or any(
				symbol.indeterminate or symbol.wildcard_import for symbol in changed_symbols
			):
				return {"valid": False, "reason": "changed Python module is indeterminate"}
			continue
		if path_suffix in {".md", ".rst", ".txt"}:
			continue
		return {"valid": False, "reason": "changed non-Python trust-boundary input"}
	return {"valid": True, "reason": "causal scope unchanged"}


def _revalidation_main(arguments: list[str]) -> int:
	parser = argparse.ArgumentParser()
	parser.add_argument("--repo", required=True)
	parser.add_argument("--audited-head", required=True)
	parser.add_argument("--current-head", required=True)
	parser.add_argument("--finding-json", required=True)
	parser.add_argument("--waiver-json", required=True)
	args = parser.parse_args(arguments)
	try:
		repo = Path(args.repo).resolve(strict=True)
		finding = _load_object(Path(args.finding_json), "finding")
		waiver = _load_object(Path(args.waiver_json), "waiver")
		result = _revalidate_waiver(repo, args.audited_head, args.current_head, finding, waiver)
	except (OSError, UnicodeError, ValueError) as exc:
		result = {"valid": False, "reason": str(exc)[:300]}
	print(json.dumps(result, ensure_ascii=True, sort_keys=True))
	return 0


def main() -> int:
	if len(__import__("sys").argv) > 1 and __import__("sys").argv[1] == "revalidate-waiver":
		return _revalidation_main(__import__("sys").argv[2:])
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
