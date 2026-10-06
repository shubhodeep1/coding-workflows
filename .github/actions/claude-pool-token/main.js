// Runs scripts/claude_pool_token.sh from this action's own checkout. The
// script writes the outputs and never fails; this wrapper only turns a
// crash into available=false so the job still runs codex (plan D1).
"use strict";

const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

function main()
{
	const script = path.resolve(__dirname, "..", "..", "..", "scripts", "claude_pool_token.sh");
	const env = { ...process.env };
	const configInput = (process.env.INPUT_CONFIG_PATH || "").trim();
	if (configInput) {
		env.CLAUDE_ENGINE_CONFIG = path.resolve(process.env.GITHUB_WORKSPACE || process.cwd(), configInput);
	}
	env.CLAUDE_POOL_PROBE = (process.env.INPUT_PROBE || "true").trim() === "false" ? "false" : "true";
	const result = spawnSync("bash", [script], { env, stdio: "inherit" });
	if (result.status !== 0) {
		console.log(`CLAUDE_POOL available=false reason=script_failed exit=${result.status}`);
		if (process.env.GITHUB_OUTPUT) {
			fs.appendFileSync(process.env.GITHUB_OUTPUT, "available=false\nreason=script_failed\naccounts=0\n");
		}
	}
	process.exitCode = 0;
}

main();
