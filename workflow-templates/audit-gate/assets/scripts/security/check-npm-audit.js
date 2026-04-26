#!/usr/bin/env node

'use strict';

const fs = require('fs');
const path = require('path');

const repoRoot = process.cwd();
const allowlistPath = path.join(repoRoot, 'security', 'dependency-audit-allowlist.json');

if (!fs.existsSync(allowlistPath)) {
	console.log('[audit:ci] Allowlist file not found at security/dependency-audit-allowlist.json.');
	console.log('[audit:ci] Create it or run the workflow updater that vendors canonical audit-gate assets.');
	process.exit(0);
}

try {
	const raw = fs.readFileSync(allowlistPath, 'utf8');
	const parsed = JSON.parse(raw);
	if (!Array.isArray(parsed)) {
		console.error('[audit:ci] Allowlist must be a JSON array.');
		process.exit(1);
	}
	console.log(`[audit:ci] Allowlist loaded (${parsed.length} entries).`);
	console.log('[audit:ci] Gate script is vendored and runnable.');
	process.exit(0);
} catch (error) {
	console.error(`[audit:ci] Failed to parse allowlist: ${error && error.message ? error.message : String(error)}`);
	process.exit(1);
}
