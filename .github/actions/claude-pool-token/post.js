// Deletes the pool directory (every token file) at the end of the job.
"use strict";

const fs = require("fs");
const path = require("path");

function post()
{
	const runnerTemp = process.env.RUNNER_TEMP || "/tmp";
	const poolDir = process.env.CLAUDE_ENGINE_POOL_DIR || path.join(runnerTemp, "claude-pool");
	if (!path.isAbsolute(runnerTemp) || !path.isAbsolute(poolDir) ||
		path.resolve(path.dirname(poolDir)) !== path.resolve(runnerTemp) ||
		!/^claude-pool(?:-[^/]+)?$/.test(path.basename(poolDir)) ||
		poolDir.split("/").some(part => part === ".." || part === ".")) {
		console.log("CLAUDE_POOL cleanup skipped reason=pool_dir_invalid");
		return;
	}
	try {
		if (fs.lstatSync(poolDir, { throwIfNoEntry: false })?.isSymbolicLink()) {
			console.log("CLAUDE_POOL cleanup skipped reason=pool_dir_invalid");
			return;
		}
		fs.rmSync(poolDir, { recursive: true, force: true });
		console.log("CLAUDE_POOL cleanup removed=true");
	} catch (error) {
		console.log(`::warning::CLAUDE_POOL cleanup failed: ${error.code || "error"}`);
	}
}

post();
