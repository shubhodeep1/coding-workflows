// Deletes the pool directory (every token file) at the end of the job.
"use strict";

const fs = require("fs");
const path = require("path");

function post()
{
	const runnerTemp = process.env.RUNNER_TEMP || "/tmp";
	const poolDir = process.env.CLAUDE_ENGINE_POOL_DIR || path.join(runnerTemp, "claude-pool");
	if (!/^\/.+\/claude-pool(-[^/]*)?$/.test(poolDir) || poolDir.split("/").includes("..")) {
		console.log("CLAUDE_POOL cleanup skipped reason=pool_dir_invalid");
		return;
	}
	try {
		fs.rmSync(poolDir, { recursive: true, force: true });
		console.log("CLAUDE_POOL cleanup removed=true");
	} catch (error) {
		console.log(`::warning::CLAUDE_POOL cleanup failed: ${error.code || "error"}`);
	}
}

post();
