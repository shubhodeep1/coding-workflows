// Deletes the ChatGPT account directory (every auth.json) at the end of the job.
"use strict";

const fs = require("fs");
const path = require("path");

function post()
{
	const runnerTemp = process.env.RUNNER_TEMP || "/tmp";
	const poolDir = process.env.CHATGPT_POOL_DIR || path.join(runnerTemp, "chatgpt-pool");
	if (!path.isAbsolute(runnerTemp) || !path.isAbsolute(poolDir) ||
		path.resolve(path.dirname(poolDir)) !== path.resolve(runnerTemp) ||
		!/^chatgpt-pool(?:-[^/]+)?$/.test(path.basename(poolDir)) ||
		poolDir.split("/").some(part => part === ".." || part === ".")) {
		console.log("CHATGPT_POOL cleanup skipped reason=pool_dir_invalid");
		return;
	}
	try {
		if (fs.lstatSync(poolDir, { throwIfNoEntry: false })?.isSymbolicLink()) {
			console.log("CHATGPT_POOL cleanup skipped reason=pool_dir_invalid");
			return;
		}
		fs.rmSync(poolDir, { recursive: true, force: true });
		console.log("CHATGPT_POOL cleanup removed=true");
	} catch (error) {
		console.log(`::warning::CHATGPT_POOL cleanup failed: ${error.code || "error"}`);
	}
}

post();
