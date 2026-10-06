"""Safety contract for the Cloudflare execution exception in /deploy-activate."""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
COMMAND_PATHS = (
	ROOT / ".claude/commands/deploy-activate.md",
	ROOT / "workflow-templates/.claude/commands/deploy-activate.md",
)


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
	assert "run the project's checks and `wrangler deploy --dry-run`" in cloudflare_steps
	assert "when I confirm (`done` / `go`), run it yourself" in cloudflare_steps
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
	assert "default mode: exact `wrangler` / `curl` commands for me to run" in cloudflare_steps
	assert "Do not diagnose the credential from `GET /client/v4/user/tokens/verify`" in cloudflare_steps
	assert "Never print the credential or either half" in cloudflare_steps


def test_identifiers_are_verified_and_persisted(cloudflare_steps: str) -> None:
	assert "Before any Cloudflare call, read the `## Cloudflare resources` table" in cloudflare_steps
	assert "prefer discovering a zone ID with a read over asking" in cloudflare_steps
	assert "verify it resolves with one read call" in cloudflare_steps
	assert "**add it to that table**" in cloudflare_steps
	assert "commit/push it together with the activation-log bookkeeping" in cloudflare_steps
