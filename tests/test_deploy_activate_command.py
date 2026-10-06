"""Contract for credential-free Cloudflare checks in /deploy-activate."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMMANDS = (
	ROOT / ".claude" / "commands" / "deploy-activate.md",
	ROOT / "workflow-templates" / ".claude" / "commands" / "deploy-activate.md",
)


@pytest.mark.parametrize("command", COMMANDS)
def test_cloudflare_preflight_and_deploy_boundaries(command: Path):
	raw_text = command.read_text(encoding="utf-8")
	text = " ".join(raw_text.split())
	cloudflare = text.split("## Cloudflare Steps", 1)[1].split("## Output Format", 1)[0]
	assert not raw_text.startswith("---")
	assert "run the project's checks and `wrangler deploy --dry-run` (where available) first" not in cloudflare
	assert "Optional local checks, including `--dry-run`" not in cloudflare
	for required in (
		"DEPLOY_SHA",
		"defaultBranchRef",
		".protected",
		"rev-parse HEAD",
		"status --porcelain",
		"unmerged",
		"with credentials",
		"wrangler deploy --dry-run",
		"env -i",
		"--network none",
		"check-runs",
		'env -i PATH="$PATH" HOME="$(mktemp -d)" CLOUDFLARE_ACCOUNT_ID="$CF_ACCOUNT_ID" CLOUDFLARE_API_TOKEN="$CF_API_TOKEN" wrangler deploy',
		"Never run a Cloudflare mutation in the same turn that proposes it",
		"Secret values never transit the chat",
	):
		assert required in cloudflare, (command, required)
	assert "Never execute unmerged code with credentials" in text.split("## Rules", 1)[1]
	assert "verified default-branch commit" in text.split("## Tool Access", 1)[1]
	assert "When Wrangler and a safe sandbox are available, require a successful `wrangler deploy --dry-run`" in cloudflare
	assert "a failed dry run blocks deployment" in cloudflare
	assert "If no such isolation is available, skip local checks and rely on the check-runs" in cloudflare
	assert "env -u FUNTOKEN_IO_CF" not in cloudflare
	assert "fall back to guide-and-paste" not in cloudflare
	assert "mark the Cloudflare step BLOCKED in the activation log and give only a corrective verification step" in cloudflare
	assert "Never emit a Worker deploy command, including one for the operator to run" in cloudflare


def test_shared_cloudflare_deploy_boundary():
	shared_document = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
	shared_rule = " ".join((
		shared_document.split("### C) Worker Deploys & Edits", 1)[1]
		.split("### D) Destructive", 1)[0]
	).split())
	for required in (
		"Never execute unmerged code with credentials",
		"default branch to be protected",
		"clean detached worktree pinned",
		"recheck the API branch tip and protection",
		"GitHub check-runs for the pinned",
		"failing or pending",
		"credential-free, no-egress sandbox",
		"unrelated session credentials stripped",
		"provisioning a narrower token is an operator task",
	):
		assert required.lower() in shared_rule.lower(), required
	assert "do not add an extra approval round" in shared_rule
	assert "do not offer a manual Worker deploy as a workaround" in shared_rule
	assert "### D) Destructive & Account-Level Writes" in shared_document

COMMAND_PATHS = COMMANDS


@pytest.fixture(params=COMMAND_PATHS, ids=("live", "template"))
def cloudflare_steps(request: pytest.FixtureRequest) -> str:
	command_text = request.param.read_text(encoding="utf-8")
	return command_text.split("## Cloudflare Steps\n", 1)[1].split("\n## Output Format", 1)[0]


def test_credential_selection_is_site_specific(cloudflare_steps: str) -> None:
	assert "`FUNTOKEN_IO_CF` covers `funtoken.io`" in cloudflare_steps
	assert "`FT_GAMES_CF` covers `ft.games` and `5m.fun`" in cloudflare_steps
	assert "different accounts and not interchangeable" in cloudflare_steps
	assert "target site is neither, stop and ask" in cloudflare_steps
	assert "split the value on the first colon" in cloudflare_steps
	assert "`CLOUDFLARE_ACCOUNT_ID` / `CLOUDFLARE_API_TOKEN` exported" in cloudflare_steps


def test_mutations_require_a_confirmed_step(cloudflare_steps: str) -> None:
	assert "**Reads**" in cloudflare_steps and "self-serve" in cloudflare_steps
	assert "recheck the pin and run the approved change yourself" in cloudflare_steps
	assert "Never run a Cloudflare mutation in the same turn that proposes it" in cloudflare_steps
	assert "operations are ask-first on top of the step loop" in cloudflare_steps
	assert "Deleting a Worker, route, custom domain, or cron trigger" in cloudflare_steps
	assert "creating, changing, or deleting DNS records or zone settings" in cloudflare_steps
	assert "before the step is emitted. After approval, you run it" in cloudflare_steps


def test_worker_secrets_remain_operator_only(cloudflare_steps: str) -> None:
	assert "For a Worker secret (`wrangler secret put`), emit the exact command for me to run" in cloudflare_steps
	assert "never ask me to paste the value" in cloudflare_steps
	assert "never set it yourself from a value seen in the conversation" in cloudflare_steps


def test_credential_failure_falls_back_without_token_leak(cloudflare_steps: str) -> None:
	assert "If the matching credential is unset" in cloudflare_steps
	assert "`workers/scripts` list) returns 401/403" in cloudflare_steps
	assert "mark the Cloudflare step BLOCKED in the activation log" in cloudflare_steps
	assert "Do not give manual Cloudflare API or deploy commands or ask me to run them" in cloudflare_steps
	assert "resume only when the matching session credential works and all deploy preconditions above have been verified" in cloudflare_steps
	assert "Do not diagnose the credential from `GET /client/v4/user/tokens/verify`" in cloudflare_steps
	assert "Never print the credential or either half" in cloudflare_steps


def test_identifiers_are_verified_and_persisted(cloudflare_steps: str) -> None:
	assert "Before any Cloudflare call, read the `## Cloudflare resources` table" in cloudflare_steps
	assert "prefer discovering a zone ID with a read over asking" in cloudflare_steps
	assert "verify it resolves with one read call" in cloudflare_steps
	assert "**add it to that table**" in cloudflare_steps
	assert "commit/push it together with the activation-log bookkeeping" in cloudflare_steps
