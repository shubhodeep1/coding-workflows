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
		"env -u FUNTOKEN_IO_CF -u FT_GAMES_CF -u DIGITALOCEAN_ACCESS_TOKEN -u GH_TOKEN -u GITHUB_TOKEN",
		"Never run a Cloudflare mutation in the same turn that proposes it",
		"Secret values never transit the chat",
	):
		assert required in cloudflare, (command, required)
	assert "Never execute unmerged code with credentials" in text.split("## Rules", 1)[1]
	assert "verified default-branch commit" in text.split("## Tool Access", 1)[1]
