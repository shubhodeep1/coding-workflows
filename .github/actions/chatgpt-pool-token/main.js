// Runs scripts/chatgpt_pool_token.sh from this action's own checkout. The
// script writes the outputs and never fails; this wrapper only turns a
// crash into available=false so the job carries on.
"use strict";

const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

function main()
{
	const script = path.resolve(__dirname, "..", "..", "..", "scripts", "chatgpt_pool_token.sh");
	const env = { ...process.env };
	const configInput = (process.env.INPUT_CONFIG_PATH || "").trim();
	if (configInput) {
		env.CHATGPT_POOL_CONFIG = path.resolve(process.env.GITHUB_WORKSPACE || process.cwd(), configInput);
	}
	env.CHATGPT_POOL_MODE = (process.env.INPUT_MODE || "token").trim() || "token";
	const result = fs.existsSync(script) ? spawnSync("bash", [script], { env, stdio: "inherit" }) : { status: null };
	if (result.status !== 0) {
		console.log(`CHATGPT_POOL available=false reason=script_failed exit=${result.status}`);
		if (process.env.GITHUB_OUTPUT) {
			fs.appendFileSync(process.env.GITHUB_OUTPUT, "available=false\nreason=script_failed\naccount=\ncodex_home=\nstatus=\n");
		}
	}
	process.exitCode = 0;
}

main();
