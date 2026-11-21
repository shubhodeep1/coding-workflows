#!/usr/bin/env python3
"""Stateful fake `gh` for tests/test_merge_lease.py.

State lives in $FAKE_GH_STATE (JSON) and every call is appended to
$FAKE_GH_LOG. It models just enough of the GitHub REST API for the merge
lease (scripts/pr_checks_lib.sh) and the merge-train release tick
(scripts/review_merge_train.sh): refs with a fast-forward-only PATCH, commits
with parents, PR listings, files, labels, statuses, compare, update-branch,
workflow dispatch and `gh pr merge`.

State keys (all optional): refs {name: sha}, commits {sha: {message, tree,
parents}}, pulls [PR objects], files {pr: [paths]}, statuses {sha: [status]},
compare {..}, closed [PR numbers whose GET pulls/<n> reads closed], user "login",
runs {sha: [workflow runs]} (actions/runs?head_sha=), check_runs {sha: [check-runs]}
(commits/<sha>/check-runs), fail ["substring", ...] (any call whose joined
argv contains one fails), move_ref_on_patch {ref: sha} (simulates a peer
winning the race: applied before the first PATCH of that ref; a list of SHAs
is applied one per PATCH), graphql {..}.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from urllib.parse import parse_qs, urlparse


def load() -> dict:
	with open(os.environ["FAKE_GH_STATE"], encoding="utf-8") as fh:
		return json.load(fh)


def save(state: dict) -> None:
	with open(os.environ["FAKE_GH_STATE"], "w", encoding="utf-8") as fh:
		json.dump(state, fh)


def emit(obj, jq_filter: str | None) -> None:
	text = json.dumps(obj)
	if jq_filter:
		res = subprocess.run(["jq", "-r", jq_filter], input=text, capture_output=True, text=True)
		if res.returncode != 0:
			sys.stderr.write(res.stderr)
			sys.exit(1)
		sys.stdout.write(res.stdout)
	else:
		sys.stdout.write(text)


def fail(msg: str = "fake failure") -> None:
	sys.stderr.write(msg + "\n")
	sys.exit(1)


def is_ancestor(state: dict, ancestor: str, sha: str) -> bool:
	seen = set()
	stack = [sha]
	while stack:
		cur = stack.pop()
		if cur == ancestor:
			return True
		if cur in seen:
			continue
		seen.add(cur)
		stack.extend(state.get("commits", {}).get(cur, {}).get("parents", []))
	return False


def main() -> None:
	argv = sys.argv[1:]
	with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as fh:
		fh.write("gh " + " ".join(argv) + "\n")
	state = load()
	joined = " ".join(argv)
	for needle in state.get("fail", []):
		if needle in joined:
			fail(f"forced failure on {needle}")
	if argv[:2] == ["workflow", "run"]:
		state.setdefault("dispatched", []).append(argv)
		save(state)
		return
	if argv[:2] == ["pr", "merge"]:
		state.setdefault("merged", []).append(argv)
		save(state)
		return
	if not argv or argv[0] != "api":
		fail("unexpected: " + joined)
	method, endpoint, jq_filter = "GET", None, None
	fields: dict[str, list[str]] = {}
	i = 1
	while i < len(argv):
		a = argv[i]
		if a == "-X":
			method = argv[i + 1]
			i += 2
		elif a in ("-f", "-F"):
			k, _, v = argv[i + 1].partition("=")
			fields.setdefault(k, []).append(v)
			i += 2
		elif a == "--jq":
			jq_filter = argv[i + 1]
			i += 2
		elif a in ("--paginate", "--slurp"):
			i += 1
		else:
			if endpoint is None:
				endpoint = a
			i += 1
	slurp = "--slurp" in argv
	url = urlparse(endpoint or "")
	path = url.path
	query = parse_qs(url.query)
	parts = path.split("/")
	if path == "user":
		emit({"login": state.get("user", "pipeline-bot")}, jq_filter)
		return
	if path == "graphql":
		emit(state.get("graphql", {"data": {"repository": {"pullRequest": {"closingIssuesReferences": {"nodes": []}}}}}), jq_filter)
		return
	# repos/o/r/...
	rest = "/".join(parts[3:])
	if rest.startswith("git/matching-refs/"):
		prefix = "refs/" + rest[len("git/matching-refs/"):]
		emit([{"ref": r, "object": {"sha": s}} for r, s in sorted(state.get("refs", {}).items()) if r.startswith(prefix)], jq_filter)
		return
	if rest.startswith("git/commits") and method == "GET":
		sha = rest.split("/")[-1]
		c = state.get("commits", {}).get(sha)
		if c is None:
			fail("no commit")
		emit({"sha": sha, "message": c["message"], "tree": {"sha": c["tree"]}, "parents": [{"sha": p} for p in c["parents"]]}, jq_filter)
		return
	if rest == "git/commits" and method == "POST":
		msg = fields.get("message", [""])[0]
		tree = fields.get("tree", [""])[0]
		parents = fields.get("parents[]", [])
		sha = hashlib.sha1((msg + tree + ",".join(parents) + str(len(state.get("commits", {})))).encode()).hexdigest()
		state.setdefault("commits", {})[sha] = {"message": msg, "tree": tree, "parents": parents}
		save(state)
		emit({"sha": sha}, jq_filter)
		return
	if rest == "git/refs" and method == "POST":
		ref = fields["ref"][0]
		if ref in state.get("refs", {}):
			fail("Reference already exists (HTTP 422)")
		state.setdefault("refs", {})[ref] = fields["sha"][0]
		save(state)
		emit({"ref": ref}, jq_filter)
		return
	if rest.startswith("git/refs/") and method == "PATCH":
		ref = "refs/" + rest[len("git/refs/"):]
		moves = state.get("move_ref_on_patch", {})
		if ref in moves:
			move = moves[ref]
			if isinstance(move, list):
				# One SHA per PATCH of that ref (repeated lost races).
				if move:
					state.setdefault("refs", {})[ref] = move.pop(0)
				if not move:
					moves.pop(ref)
			else:
				state.setdefault("refs", {})[ref] = moves.pop(ref)
		cur = state.get("refs", {}).get(ref)
		new = fields["sha"][0]
		if cur is None:
			save(state)
			fail("not found")
		if fields.get("force", ["false"])[0] != "true" and not is_ancestor(state, cur, new):
			save(state)
			fail("Update is not a fast forward (HTTP 422)")
		state["refs"][ref] = new
		save(state)
		emit({"ref": ref}, jq_filter)
		return
	if len(parts) >= 5 and parts[3] == "commits" and len(parts) == 5 and method == "GET":
		emit({"commit": {"tree": {"sha": "f" * 40}}}, jq_filter)
		return
	if len(parts) >= 6 and parts[3] == "commits" and parts[5] == "statuses":
		emit(state.get("statuses", {}).get(parts[4], []), jq_filter)
		return
	if parts[3:4] == ["statuses"] and method == "POST":
		sha = parts[4]
		entry = {"context": fields["context"][0], "state": fields["state"][0], "creator": {"login": state.get("user", "pipeline-bot")}}
		state.setdefault("statuses", {}).setdefault(sha, []).insert(0, entry)
		save(state)
		emit(entry, jq_filter)
		return
	if parts[3:4] == ["compare"]:
		if "compare" not in state:
			fail("no compare")
		emit(state["compare"], jq_filter)
		return
	if parts[3:4] == ["issues"] and len(parts) >= 6 and parts[5] == "labels":
		num = parts[4]
		labels = state.setdefault("labels", {}).setdefault(num, [])
		if method == "POST":
			for name in fields.get("labels[]", []):
				if name not in labels:
					labels.append(name)
			save(state)
			emit([{"name": n} for n in labels], jq_filter)
			return
		if method == "DELETE":
			from urllib.parse import unquote
			name = unquote("/".join(parts[6:]))
			if name in labels:
				labels.remove(name)
			save(state)
			return
	if parts[3:4] == ["issues"] and len(parts) >= 6 and parts[5] == "comments":
		emit([[]] if slurp else [], jq_filter)
		return
	if parts[3:4] == ["pulls"] and len(parts) == 4:
		base = (query.get("base") or [None])[0]
		pulls = [dict(p) for p in state.get("pulls", []) if base is None or p.get("base", {}).get("ref") == base]
		for p in pulls:
			p["labels"] = [{"name": n} for n in state.get("labels", {}).get(str(p["number"]), [])]
		emit([pulls] if slurp else pulls, jq_filter)
		return
	if parts[3:4] == ["pulls"] and len(parts) >= 6 and parts[5] == "files":
		files = [{"filename": f} for f in state.get("files", {}).get(parts[4], [])]
		emit([files] if slurp else files, jq_filter)
		return
	if parts[3:4] == ["pulls"] and len(parts) >= 6 and parts[5] == "update-branch":
		state.setdefault("updated", []).append(parts[4])
		save(state)
		emit({}, jq_filter)
		return
	if parts[3:4] == ["pulls"] and len(parts) == 5:
		if int(parts[4]) in state.get("closed", []):
			emit({"number": int(parts[4]), "state": "closed"}, jq_filter)
			return
		for p in state.get("pulls", []):
			if str(p["number"]) == parts[4]:
				emit(p, jq_filter)
				return
		fail("no pr")
	if parts[3:5] == ["actions", "runs"] or (len(parts) > 4 and parts[3] == "actions"):
		run_sha = (query.get("head_sha") or [""])[0]
		runs = state.get("runs", {}).get(run_sha, []) if parts[3:5] == ["actions", "runs"] else []
		emit({"total_count": len(runs), "workflow_runs": runs}, jq_filter)
		return
	if parts[3:4] == ["check-runs"] or (len(parts) > 5 and parts[5] == "check-runs"):
		check_runs = state.get("check_runs", {}).get(parts[4], []) if len(parts) > 5 else []
		listing = {"total_count": len(check_runs), "check_runs": check_runs}
		emit([listing] if (slurp and check_runs) else listing, jq_filter)
		return
	emit({}, jq_filter)


if __name__ == "__main__":
	main()
