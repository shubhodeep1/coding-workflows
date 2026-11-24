#!/usr/bin/env python3
"""Priority lane (issue #10048): one operator label on an issue moves its PR
to the front of the review sweep, the final-CI sweep and the merge-lease grant,
and holds other dispatches back (for a bounded time) while a priority run waits
for a runner.

The sweep cases run the enumerate step's shell as extracted from the shipped
workflow against a fake `gh`; the lease cases drive the shipped merge train
through the stateful fake of tests/test_merge_lease.py. The final-CI sweep
cases live in tests/test_review_final_ci.py.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))

from test_review_autofix_sweep_merge_queued_skip import extract_step_script  # noqa: E402
from test_merge_lease import _Harness, _gates_ok, _pull, _record, _seed_lease  # noqa: E402

LIB = REPO_ROOT / "scripts" / "pr_checks_lib.sh"
HEAD_GATE = REPO_ROOT / "scripts" / "review_sweep_head_gate.sh"
SWEEP_WF = REPO_ROOT / ".github" / "workflows" / "review_autofix_sweep.yml"

FAKE_SWEEP_GH = r'''#!/usr/bin/env python3
import json, os, sys
root = os.environ["FAKE_DIR"]
args = sys.argv[1:]
def log(name, line):
	with open(os.path.join(root, name), "a") as fh:
		fh.write(line + "\n")
def fixture(name, default):
	path = os.path.join(root, name)
	if os.path.exists(path):
		with open(path) as fh:
			return json.load(fh)
	return default
log("calls.log", json.dumps(args))
if args[:2] == ["workflow", "run"]:
	log("dispatches.log", " ".join(args))
	sys.exit(0)
if args[:2] == ["label", "create"]:
	log("labels.log", " ".join(args))
	sys.exit(0)
if args[:1] != ["api"]:
	sys.exit(1)
if args[1:2] == ["graphql"]:
	query = next(a.split("=", 1)[1] for a in args if a.startswith("query="))
	log("graphql.log", json.dumps(query))
	if os.path.exists(os.path.join(root, "graphql_fails")):
		sys.exit(1)
	sys.stdout.write(json.dumps(fixture("graphql.json", {})))
	sys.exit(0)
endpoint = next((a for a in args[1:] if a.startswith("repos/")), "")
if "--jq" in args and args[args.index("--jq") + 1] == ".default_branch":
	sys.stdout.write("main\n")
	sys.exit(0)
if endpoint.endswith("/pulls"):
	sys.stdout.write(json.dumps(fixture("pulls.json", [])))
	sys.exit(0)
if endpoint.endswith("/actions/runs") and "status=queued" in args:
	log("queued.log", json.dumps(args))
	if os.path.exists(os.path.join(root, "queued_fails")):
		sys.exit(1)
	sys.stdout.write(json.dumps({"total_count": 0, "workflow_runs": fixture("queued.json", [])}))
	sys.exit(0)
if "/actions/workflows/" in endpoint:
	sys.stdout.write(json.dumps({"total_count": 0, "workflow_runs": []}))
	sys.exit(0)
sys.stderr.write("unexpected gh call: " + " ".join(args) + "\n")
sys.exit(1)
'''


def _iso(seconds_ago: int) -> str:
	return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


def _rest_pr(number: int, *, head_ref: str | None = None, title: str = "", body: str = "", updated_at: str | None = None,
		labels: list[str] | None = None) -> dict:
	return {"number": number, "draft": False, "title": title or f"PR {number}", "body": body,
		"head": {"ref": head_ref or f"ai/issue-{number}", "sha": f"{number:040x}"},
		"labels": [{"name": name} for name in (labels or [])], "created_at": "2026-11-01T00:00:00Z",
		"updated_at": updated_at if updated_at is not None else _iso(60), "user": {"login": "someone"}}


def _node(number: int, *, gh_head_ref: str | None = None, cross_repo: bool = False,
		closing: list[tuple[int, list[str]]] | None = None) -> dict:
	sha = f"{number:040x}"
	return {"number": number, "headRefOid": sha, "headRefName": gh_head_ref or f"ai/issue-{number}",
		"isCrossRepository": cross_repo,
		"closingIssuesReferences": {"nodes": [{"number": n, "labels": {"nodes": [{"name": name} for name in labels]}}
			for n, labels in (closing or [])]},
		"commits": {"nodes": [{"commit": {"oid": sha, "committedDate": "2026-11-01T00:00:00Z",
			"status": {"context": {"state": "PENDING"}}}}]}}


def _page(nodes: list[dict], label_present: bool = True) -> dict:
	return {"data": {"repository": {"priorityLabel": {"name": "ai:priority"} if label_present else None,
		"pullRequests": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}}}


def _queued_run(title: str, seconds_ago: int, *, event: str = "workflow_dispatch", head_branch: str | None = "main") -> dict:
	return {"id": 1, "event": event, "status": "queued", "display_title": title, "name": title,
		"head_branch": head_branch, "created_at": _iso(seconds_ago)}


@unittest.skipUnless(shutil.which("jq"), "jq is required")
class SweepPriorityLaneTest(unittest.TestCase):
	def setUp(self) -> None:
		self.tmp = Path(os.environ.get("TMPDIR", "/tmp")) / f"sweep_lane_{os.getpid()}_{self._testMethodName}"
		if self.tmp.exists():
			shutil.rmtree(self.tmp)
		self.tmp.mkdir(parents=True)
		self.addCleanup(shutil.rmtree, self.tmp, True)

	def run_sweep(self, prs: list[dict], nodes: list[dict], *, queued: list[dict] | None = None, label_present: bool = True,
			flags: tuple[str, ...] = (), **env: str) -> subprocess.CompletedProcess:
		(self.tmp / "pulls.json").write_text(json.dumps(prs), encoding="utf-8")
		(self.tmp / "graphql.json").write_text(json.dumps(_page(nodes, label_present)), encoding="utf-8")
		(self.tmp / "queued.json").write_text(json.dumps(queued or []), encoding="utf-8")
		for flag in flags:
			(self.tmp / flag).write_text("", encoding="utf-8")
		bin_dir = self.tmp / "bin"
		bin_dir.mkdir(exist_ok=True)
		gh = bin_dir / "gh"
		gh.write_text(FAKE_SWEEP_GH, encoding="utf-8")
		gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
		support = self.tmp / ".ci-rerun-support"
		if not support.exists():
			support.mkdir()
			(support / "scripts").symlink_to(REPO_ROOT / "scripts")
		full_env = {
			"PATH": f"{bin_dir}:{os.environ['PATH']}", "RUNNER_TEMP": str(self.tmp), "REPOSITORY": "o/r",
			"GH_TOKEN": "x", "READ_TOKEN": "x", "HEAD_REF_FILTER": "", "DRY_RUN": "false",
			"ALLOW_WORKFLOW_EDITS": "true", "SWEEP_STALE_QUEUED_MINUTES": "120", "FAKE_DIR": str(self.tmp),
			"GH_RETRY_MAX_ATTEMPTS": "1", "PYTHONDONTWRITEBYTECODE": "1",
		}
		full_env.update(env)
		return subprocess.run(["bash", "-c", extract_step_script()], env=full_env, capture_output=True, text=True, cwd=self.tmp)

	def lines(self, name: str) -> list[str]:
		path = self.tmp / name
		return path.read_text().splitlines() if path.exists() else []

	def dispatched(self) -> list[str]:
		return [line.split("pr_number=")[1].split()[0] for line in self.lines("dispatches.log")]

	def _three(self) -> tuple[list[dict], list[dict]]:
		prs = [_rest_pr(10), _rest_pr(11), _rest_pr(12)]
		nodes = [_node(10, closing=[(10, [])]), _node(11, closing=[(11, ["bug"])]), _node(12, closing=[(12, ["ai:priority"])])]
		return prs, nodes

	def test_priority_pr_is_dispatched_before_older_prs(self) -> None:
		prs, nodes = self._three()
		result = self.run_sweep(prs, nodes)
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(self.dispatched(), ["12", "10", "11"])
		self.assertIn("PRIORITY_LANE pr=12 outcome=first reason=verified_label surface=sweep", result.stdout)
		self.assertIn("priority=1", result.stderr)
		self.assertIn("skipped_priority_lane=0", result.stdout)
		snapshot = json.loads((self.tmp / "ci-cancelled-pr-snapshot.json").read_text())
		self.assertEqual([(row["number"], row["priority_lane"]) for row in snapshot], [(12, True), (10, False), (11, False)])
		self.assertTrue(all("updated_at" in row for row in snapshot))
		# No priority run is queued: one queued-runs read, no hold.
		self.assertEqual(len(self.lines("queued.log")), 1)
		self.assertIn("PRIORITY_LANE_HOLD outcome=none", result.stderr)

	def test_unverified_claims_give_no_priority(self) -> None:
		prs = [_rest_pr(13), _rest_pr(14), _rest_pr(15), _rest_pr(16),
			_rest_pr(17, title="ai:priority please", body="Fixes #17 ai:priority", labels=["ai:priority"])]
		nodes = [
			_node(13),  # no closing reference
			_node(14, closing=[(99, ["ai:priority"])]),  # label on a different issue
			_node(15, cross_repo=True, closing=[(15, ["ai:priority"])]),  # fork head
			_node(16, gh_head_ref="ai/issue-99", closing=[(16, ["ai:priority"])]),  # headRefName mismatch
			_node(17, closing=[(17, [])]),  # title, body and PR label only
		]
		result = self.run_sweep(prs, nodes)
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(self.dispatched(), ["13", "14", "15", "16", "17"])
		self.assertNotIn("PRIORITY_LANE pr=", result.stdout)
		self.assertEqual(self.lines("queued.log"), [])

	def test_queued_priority_run_defers_other_prs(self) -> None:
		for title in ("Internal: AI Review & Autofix [pr:12]", "Security Audit (PR #12)", "CI [pr:12]", "CI [pr:12] [final]"):
			with self.subTest(title=title):
				for name in ("dispatches.log", "queued.log"):
					(self.tmp / name).unlink(missing_ok=True)
				prs, nodes = self._three()
				result = self.run_sweep(prs, nodes, queued=[_queued_run(title, 30 * 60)])
				self.assertEqual(result.returncode, 0, result.stderr)
				self.assertEqual(self.dispatched(), ["12"])
				self.assertIn("PRIORITY_LANE_HOLD outcome=active pr=12 age_min=30", result.stderr)
				self.assertIn("PRIORITY_LANE pr=10 outcome=deferred reason=priority_run_queued surface=sweep", result.stdout)
				self.assertIn("skipped_priority_lane=2", result.stdout)
				self.assertIn("hold=true\npriority=12\n", (self.tmp / "priority-lane-hold.txt").read_text())

	def test_runs_that_must_not_start_the_hold(self) -> None:
		cases = {
			"too_recent": _queued_run("Internal: AI Review & Autofix [pr:12]", 5 * 60),
			"wedged": _queued_run("Internal: AI Review & Autofix [pr:12]", 30 * 3600),
			"push_event": _queued_run("CI [pr:12]", 30 * 60, event="push"),
			"branch_review": _queued_run("Internal: AI Review & Autofix [pr:12]", 30 * 60, head_branch="ai/issue-12"),
			"other_pr": _queued_run("Internal: AI Review & Autofix [pr:10]", 30 * 60),
		}
		for name, run in cases.items():
			with self.subTest(case=name):
				(self.tmp / "dispatches.log").unlink(missing_ok=True)
				prs, nodes = self._three()
				result = self.run_sweep(prs, nodes, queued=[run])
				self.assertEqual(result.returncode, 0, result.stderr)
				self.assertEqual(self.dispatched(), ["12", "10", "11"])
				self.assertIn("skipped_priority_lane=0", result.stdout)

	def test_pr_held_past_the_cap_is_dispatched(self) -> None:
		prs = [_rest_pr(10, updated_at="2020-01-01T00:00:00Z"), _rest_pr(11, updated_at=""), _rest_pr(12)]
		_, nodes = self._three()
		result = self.run_sweep(prs, nodes, queued=[_queued_run("CI [pr:12]", 30 * 60)])
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(self.dispatched(), ["12", "10", "11"])
		self.assertIn("PRIORITY_LANE pr=10 outcome=released reason=max_defer surface=sweep", result.stdout)
		self.assertIn("PRIORITY_LANE pr=11 outcome=released reason=since_unknown surface=sweep", result.stdout)

	def test_failed_queue_read_holds_nothing(self) -> None:
		prs, nodes = self._three()
		result = self.run_sweep(prs, nodes, queued=[_queued_run("CI [pr:12]", 30 * 60)], flags=("queued_fails",))
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(self.dispatched(), ["12", "10", "11"])
		self.assertIn("PRIORITY_LANE_HOLD outcome=read_failed", result.stderr)

	def test_failed_graphql_read_keeps_the_order(self) -> None:
		prs, nodes = self._three()
		result = self.run_sweep(prs, nodes, queued=[_queued_run("CI [pr:12]", 30 * 60)], flags=("graphql_fails",))
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(self.dispatched(), ["10", "11", "12"])
		self.assertEqual(self.lines("queued.log"), [])

	def test_lane_off_restores_the_previous_query_and_order(self) -> None:
		prs, nodes = self._three()
		result = self.run_sweep(prs, nodes, queued=[_queued_run("CI [pr:12]", 30 * 60)], PRIORITY_LANE_ENABLED="off")
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(self.dispatched(), ["10", "11", "12"])
		self.assertEqual(self.lines("queued.log"), [])
		self.assertNotIn("PRIORITY_LANE pr=", result.stdout)
		query = json.loads(self.lines("graphql.log")[0])
		expected = subprocess.run(["bash", "-c", f'source {str(HEAD_GATE)!r}; printf "%s" "$REVIEW_SWEEP_HEAD_GATE_QUERY"'],
			capture_output=True, text=True, check=True).stdout
		self.assertEqual(query, expected)
		self.assertNotIn("closingIssuesReferences", query)
		snapshot = json.loads((self.tmp / "ci-cancelled-pr-snapshot.json").read_text())
		self.assertTrue(all("priority_lane" not in row for row in snapshot))

	def test_label_is_created_only_when_missing(self) -> None:
		prs, nodes = self._three()
		self.run_sweep(prs, nodes, label_present=True)
		self.assertEqual(self.lines("labels.log"), [])
		result = self.run_sweep(prs, nodes, label_present=False)
		self.assertEqual(result.returncode, 0, result.stderr)
		labels = self.lines("labels.log")
		self.assertEqual(len(labels), 1)
		self.assertIn("label create ai:priority --repo o/r", labels[0])
		self.assertIn("PRIORITY_LANE_LABEL outcome=created label=ai:priority", result.stderr)

	def test_workflow_wires_the_settings(self) -> None:
		text = SWEEP_WF.read_text(encoding="utf-8")
		step = text[text.index("- name: Enumerate open PRs and dispatch internal-review.yml"):]
		step = step[:step.index("\n      - name:")]
		for line in ("MERGE_TRAIN_PRIORITY_LABELS: ${{ vars.MERGE_TRAIN_PRIORITY_LABELS || 'ai:workflow-heal,ai:security,ai:priority' }}",
				"PRIORITY_LANE_ENABLED: ${{ vars.PRIORITY_LANE_ENABLED || 'true' }}",
				"PRIORITY_LANE_WAIT_MINUTES: ${{ vars.PRIORITY_LANE_WAIT_MINUTES || '15' }}",
				"PRIORITY_LANE_MAX_DEFER_MINUTES: ${{ vars.PRIORITY_LANE_MAX_DEFER_MINUTES || '120' }}"):
			self.assertIn(line, step)


def _lib(body: str, **env: str) -> subprocess.CompletedProcess:
	full_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GITHUB_REPOSITORY": "o/r", **env}
	return subprocess.run(["bash", "-c", f"source {str(LIB)!r}\n{body}"], env=full_env, capture_output=True, text=True, check=False)


@unittest.skipUnless(shutil.which("jq"), "jq is required")
class LeaseOrderInputTest(unittest.TestCase):
	ROWS = json.dumps([
		{"number": n, "draft": False, "head": {"ref": f"ai/issue-{n}", "sha": f"{n:040x}", "repo": {"full_name": "o/r"}},
			"labels": [{"name": "ai:merge-lease-wait"}] + ([{"name": "ai:security"}] if n == 9 else [])}
		for n in (5, 6, 9)
	])

	def test_order_key_is_unchanged_without_the_input(self) -> None:
		res = _lib('_merge_lease_order_key 5 "x" "ai/issue-5"; echo; _merge_lease_order_key 5 "x" "ai/issue-5" ""; echo;'
			' _merge_lease_order_key 5 "x" "ai/issue-5" 0; echo; _merge_lease_order_key 5 "x" "ai/issue-5" 1')
		self.assertEqual(res.stdout.split(), ["1000000000005", "1000000000005", "1000000000005", "0000000000005"])

	def test_priority_csv_moves_a_waiter_into_the_priority_group(self) -> None:
		body = ('_merge_lease_queue_first "$ROWS" "" "" "" "$P"; echo " ${MERGE_LEASE_QUEUE_PRS}";'
			' _merge_lease_queue_order "$ROWS" "" "" "$P"; echo')
		self.assertEqual(_lib(body, ROWS=self.ROWS, P="").stdout.split(), ["9", "9,5,6", "9,5,6"])
		self.assertEqual(_lib(body, ROWS=self.ROWS, P="6").stdout.split(), ["6", "6,9,5", "6,9,5"])
		# An input that is not a list of PR numbers is ignored.
		self.assertEqual(_lib(body, ROWS=self.ROWS, P="6;rm").stdout.split(), ["9", "9,5,6"] + ["9,5,6"])


@unittest.skipUnless(shutil.which("jq"), "jq is required")
class ParityTest(unittest.TestCase):
	"""PR_PRIORITY_LANE_JQ_DEF (the sweep) and _mt_pr_is_priority (the merge
	train) must give the same answer on the same metadata."""

	CASES = [
		("ai/issue-5", "ai/issue-5", [(5, ["ai:priority"])], True),
		("ai/issue-5", "ai/issue-5", [(5, ["ai:security", "bug"])], True),
		("ai/issue-5", "ai/issue-5", [(5, ["bug"])], False),
		("ai/issue-5", "ai/issue-5", [(6, ["ai:priority"])], False),
		("ai/issue-5", "ai/issue-5", [], False),
		("ai/issue-5", "ai/issue-6", [(5, ["ai:priority"])], False),
		("feature/5", "feature/5", [(5, ["ai:priority"])], False),
		("ai/issue-0", "ai/issue-0", [(0, ["ai:priority"])], False),
	]

	def test_both_rule_sets_agree(self) -> None:
		h = _Harness({})
		for head, gh_head, closing, expected in self.CASES:
			with self.subTest(head=head, gh_head=gh_head, closing=closing):
				meta = json.dumps({"head_ref": gh_head, "created_at": "", "closing": [{"number": n, "labels": names} for n, names in closing], "timeline": None})
				res = h.train_fn('_MT_META_CACHE[5]="$META"; _mt_pr_is_priority 5 "$HEAD_REF"; echo "rc=$?"',
					{"META": meta, "HEAD_REF": head})
				train = "rc=0" in res.stdout
				row = json.dumps({"head_ref": head, "gh_head_ref": gh_head, "cross_repo": False,
					"closing": [{"number": n, "labels": names} for n, names in closing]})
				res = _lib('printf "%s" "$ROW" | jq -r --arg p ai/issue- --argjson w \'["ai:priority","ai:security","ai:workflow-heal"]\''
					' "${PR_PRIORITY_LANE_JQ_DEF}"\' pr_priority_lane_verified($p; $w) == true\'', ROW=row)
				sweep = res.stdout.strip() == "true"
				self.assertEqual((train, sweep), (expected, expected), res.stderr)


def _rows(pulls: list[dict], labels: dict[str, list[str]], updated: dict[int, str]) -> str:
	return "\n".join(json.dumps({"number": p["number"], "head": p["head"]["ref"], "head_repo": p["head"]["repo"]["full_name"],
		"head_sha": p["head"]["sha"], "base": p["base"]["ref"], "draft": False, "labels": labels.get(str(p["number"]), []),
		"updated_at": updated.get(p["number"], _iso(60)), "title": ""}) for p in pulls)


def _meta_graphql(priority: dict[int, tuple[int, list[str]]], pulls: list[dict]) -> dict:
	repo = {}
	for p in pulls:
		n = p["number"]
		closing_num, closing_labels = priority.get(n, (n, []))
		repo[f"p{n}"] = {"number": n, "headRefName": p["head"]["ref"], "createdAt": "2026-11-17T00:00:00Z",
			"closingIssuesReferences": {"nodes": [{"number": closing_num, "labels": {"nodes": [{"name": name} for name in closing_labels]}}]}}
	return {"data": {"repository": repo}}


@unittest.skipUnless(shutil.which("jq"), "jq is required")
class LeaseTickPriorityLaneTest(unittest.TestCase):
	def _tick(self, pulls: list[dict], labels: dict[str, list[str]], priority: dict[int, tuple[int, list[str]]],
			*, active_runs: list[dict] | None = None, updated: dict[int, str] | None = None, env: dict[str, str] | None = None):
		st = {"pulls": pulls, "labels": labels, "files": {str(p["number"]): ["shared.py"] for p in pulls}, "closed": [4],
			"graphql": _meta_graphql(priority, pulls), "active_runs": active_runs or []}
		_seed_lease(st, _record(4))
		_gates_ok(st, *[p["number"] for p in pulls])
		h = _Harness(st)
		res = h.train_fn('_mt_merge_lease_tick "${PRS_JSONL}" ""', {"PRS_JSONL": _rows(pulls, labels, updated or {}), **(env or {})})
		return h, res.stdout + res.stderr

	def test_verified_linked_issue_priority_is_granted_before_a_lower_number(self) -> None:
		pulls = [_pull(5), _pull(6)]
		labels = {"5": ["ai:merge-lease-wait"], "6": ["ai:merge-lease-wait"]}
		h, out = self._tick(pulls, labels, {6: (6, ["ai:priority"])})
		self.assertIn("MERGE_LEASE pr=6 base=main mode=tick outcome=granted", out)
		self.assertIn("pr=6", h.record())

	def test_unverified_claims_and_the_switch_keep_the_number_order(self) -> None:
		cases = {
			"label_on_other_issue": ([_pull(5), _pull(6)], {6: (9, ["ai:priority"])}, {}),
			"no_priority_label": ([_pull(5), _pull(6)], {6: (6, ["bug"])}, {}),
			"fork_head": ([_pull(5), {**_pull(6), "head": {**_pull(6)["head"], "repo": {"full_name": "fork/repo"}}}],
				{6: (6, ["ai:priority"])}, {}),
			"lane_off": ([_pull(5), _pull(6)], {6: (6, ["ai:priority"])}, {"PRIORITY_LANE_ENABLED": "false"}),
		}
		for name, (pulls, priority, env) in cases.items():
			with self.subTest(case=name):
				labels = {"5": ["ai:merge-lease-wait"], "6": ["ai:merge-lease-wait"]}
				h, out = self._tick(pulls, labels, priority, env=env)
				self.assertIn("MERGE_LEASE pr=5 base=main mode=tick outcome=granted", out)

	def _queued_review(self, pr: int, seconds_ago: int) -> dict:
		title = f"Internal: AI Review & Autofix [pr:{pr}]"
		return {"id": 77, "event": "workflow_dispatch", "status": "queued", "display_title": title, "name": title,
			"head_branch": "main", "head_sha": "9" * 40, "created_at": _iso(seconds_ago),
			"path": ".github/workflows/internal-review.yml"}

	def test_hold_leaves_the_lease_free_and_the_waiter_labelled(self) -> None:
		pulls = [_pull(5), _pull(6)]
		labels = {"5": ["ai:merge-lease-wait"]}
		h, out = self._tick(pulls, labels, {6: (6, ["ai:priority"])}, active_runs=[self._queued_review(6, 30 * 60)])
		self.assertIn("PRIORITY_LANE_HOLD outcome=active pr=6", out)
		self.assertIn("PRIORITY_LANE pr=5 outcome=deferred reason=priority_run_queued surface=lease", out)
		self.assertNotIn("outcome=granted", out)
		self.assertNotIn("outcome=no_waiter", out)
		self.assertIn("ai:merge-lease-wait", h.state["labels"]["5"])
		self.assertNotIn("dispatched", h.state)

	def test_hold_releases_a_waiter_past_the_cap_and_ignores_wedged_runs(self) -> None:
		pulls = [_pull(5), _pull(6)]
		labels = {"5": ["ai:merge-lease-wait"]}
		h, out = self._tick(pulls, labels, {6: (6, ["ai:priority"])}, active_runs=[self._queued_review(6, 30 * 60)],
			updated={5: "2020-01-01T00:00:00Z"})
		self.assertIn("PRIORITY_LANE pr=5 outcome=released reason=max_defer surface=lease", out)
		self.assertIn("MERGE_LEASE pr=5 base=main mode=tick outcome=granted", out)
		h, out = self._tick(pulls, labels, {6: (6, ["ai:priority"])}, active_runs=[self._queued_review(6, 30 * 3600)])
		self.assertNotIn("outcome=deferred", out)
		self.assertIn("MERGE_LEASE pr=5 base=main mode=tick outcome=granted", out)

	def test_poll_and_close_release_steps_pass_the_settings(self) -> None:
		for name in ("orchestrate_poll.yml", "cancel_on_pr_close.yml"):
			text = (REPO_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
			at = text.index("PRIORITY_LANE_ENABLED:")
			end = text.find("\n      - name:", at)
			step = text[text.rindex("- name:", 0, at):end if end != -1 else len(text)]
			with self.subTest(workflow=name):
				self.assertIn("review_merge_train.sh release", step)
				for line in ("PRIORITY_LANE_ENABLED: ${{ vars.PRIORITY_LANE_ENABLED || 'true' }}",
						"PRIORITY_LANE_WAIT_MINUTES: ${{ vars.PRIORITY_LANE_WAIT_MINUTES || '15' }}",
						"PRIORITY_LANE_MAX_DEFER_MINUTES: ${{ vars.PRIORITY_LANE_MAX_DEFER_MINUTES || '120' }}",
						"MERGE_TRAIN_PRIORITY_LABELS: ${{ vars.MERGE_TRAIN_PRIORITY_LABELS || 'ai:workflow-heal,ai:security,ai:priority' }}"):
					self.assertIn(line, step)
		poll = (REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml").read_text(encoding="utf-8")
		self.assertEqual(poll.count("PRIORITY_LANE_ENABLED:"), 1)


if __name__ == "__main__":
	unittest.main()
