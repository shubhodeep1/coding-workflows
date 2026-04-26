#!/usr/bin/env node

'use strict';

const childProcess = require('child_process');
const fs = require('fs');
const path = require('path');

const repoRoot = process.cwd();
const allowlistPath = path.join(repoRoot, 'security', 'dependency-audit-allowlist.json');
const GHSA_PATTERN = /\bGHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}\b/i;
const CVE_PATTERN = /\bCVE-\d{4}-\d{4,}\b/i;

function toUniqueStrings(values)
{
	if (!Array.isArray(values)) {
		return [];
	}

	const seen = new Set();
	const output = [];
	for (const value of values) {
		if (typeof value !== 'string') {
			continue;
		}
		const normalized = value.trim();
		if (!normalized || seen.has(normalized)) {
			continue;
		}
		seen.add(normalized);
		output.push(normalized);
	}
	return output;
}

function normalizeSeverity(value)
{
	if (typeof value !== 'string') {
		return '';
	}
	return value.trim().toLowerCase();
}

function normalizePackageName(value)
{
	if (typeof value !== 'string') {
		return '';
	}
	return value.trim();
}

function extractPatternValue(value, pattern)
{
	if (typeof value !== 'string') {
		return null;
	}
	const match = value.match(pattern);
	if (!match || !match[0]) {
		return null;
	}
	return match[0].toUpperCase();
}

function extractAdvisoryId(record)
{
	if (!record || typeof record !== 'object') {
		return null;
	}

	const ghsaCandidates = [
		record.advisoryId,
		record.ghsaId,
		record.ghsa,
		record.githubAdvisoryId,
		record.github_advisory_id,
		record.id,
		record.url,
		record.title,
		record.source,
		record.name,
	];
	for (const candidate of ghsaCandidates) {
		const ghsa = extractPatternValue(candidate, GHSA_PATTERN);
		if (ghsa) {
			return ghsa;
		}
	}

	const cveCandidates = [
		record.advisoryId,
		record.cve,
		record.id,
		record.url,
		record.title,
		record.source,
		record.name,
	];
	if (Array.isArray(record.cves)) {
		for (const cveEntry of record.cves) {
			if (typeof cveEntry === 'string') {
				cveCandidates.push(cveEntry);
				continue;
			}
			if (cveEntry && typeof cveEntry === 'object') {
				cveCandidates.push(cveEntry.id, cveEntry.cve, cveEntry.name, cveEntry.title, cveEntry.url);
			}
		}
	}
	for (const candidate of cveCandidates) {
		const cve = extractPatternValue(candidate, CVE_PATTERN);
		if (cve) {
			return cve;
		}
	}

	return null;
}

function buildIdentityKey(finding)
{
	return `${finding.severity}|${finding.package}|${finding.advisoryId}`;
}

function extractPackagesFromNodePath(nodePath)
{
	if (typeof nodePath !== 'string') {
		return [];
	}
	const segments = nodePath.split('/').filter(Boolean);
	const packages = [];
	for (let index = 0; index < segments.length; index += 1) {
		if (segments[index] !== 'node_modules' || index + 1 >= segments.length) {
			continue;
		}
		let packageName = segments[index + 1];
		if (packageName.startsWith('@') && index + 2 < segments.length) {
			packageName = `${packageName}/${segments[index + 2]}`;
			index += 1;
		}
		packages.push(packageName);
	}
	return packages;
}

function collectViaPackages(vulnerability)
{
	const collected = [];
	if (Array.isArray(vulnerability.via)) {
		for (const viaEntry of vulnerability.via) {
			if (typeof viaEntry === 'string') {
				collected.push(viaEntry);
				continue;
			}
			if (!viaEntry || typeof viaEntry !== 'object') {
				continue;
			}
			if (typeof viaEntry.name === 'string') {
				collected.push(viaEntry.name);
			}
			if (typeof viaEntry.dependency === 'string') {
				collected.push(viaEntry.dependency);
			}
			if (Array.isArray(viaEntry.viaPackages)) {
				collected.push(...viaEntry.viaPackages);
			}
		}
	}

	if (Array.isArray(vulnerability.nodes)) {
		for (const nodePath of vulnerability.nodes) {
			collected.push(...extractPackagesFromNodePath(nodePath));
		}
	}

	return toUniqueStrings(collected);
}

function normalizeAllowlistEntries(allowlist)
{
	const normalized = [];
	const invalidIndices = [];

	for (let index = 0; index < allowlist.length; index += 1) {
		const entry = allowlist[index];
		if (!entry || typeof entry !== 'object' || Array.isArray(entry)) {
			invalidIndices.push(index + 1);
			continue;
		}

		const packageName = normalizePackageName(
			entry.package || entry.name || entry.dependency || entry.module || entry.moduleName,
		);
		const severity = normalizeSeverity(entry.severity);
		const advisoryId = extractAdvisoryId(entry);
		if (!packageName || !severity || !advisoryId) {
			invalidIndices.push(index + 1);
			continue;
		}

		normalized.push({
			package: packageName,
			severity,
			advisoryId,
			viaPackages: toUniqueStrings(entry.viaPackages),
			key: buildIdentityKey({ package: packageName, severity, advisoryId }),
		});
	}

	return { normalized, invalidIndices };
}

function normalizeAuditFindings(payload)
{
	if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
		return {
			findings: [],
			identityErrors: ['npm audit payload was not an object.'],
		};
	}

	const vulnerabilities = payload.vulnerabilities;
	if (!vulnerabilities || typeof vulnerabilities !== 'object' || Array.isArray(vulnerabilities)) {
		return {
			findings: [],
			identityErrors: ['npm audit payload did not include a vulnerabilities object.'],
		};
	}

	const findingsByKey = new Map();
	const identityErrors = [];

	for (const [fallbackPackage, vulnerability] of Object.entries(vulnerabilities)) {
		if (!vulnerability || typeof vulnerability !== 'object' || Array.isArray(vulnerability)) {
			continue;
		}

		const packageName = normalizePackageName(vulnerability.name || fallbackPackage);
		const severity = normalizeSeverity(vulnerability.severity);
		if (!packageName || !severity) {
			continue;
		}

		const advisoryRecords = Array.isArray(vulnerability.via)
			? vulnerability.via.filter((entry) => entry && typeof entry === 'object')
			: [];
		const candidateRecords = advisoryRecords.length > 0 ? advisoryRecords : [vulnerability];
		const viaPackages = collectViaPackages(vulnerability).filter((entry) => entry !== packageName);

		for (const advisoryRecord of candidateRecords) {
			const advisoryId = extractAdvisoryId(advisoryRecord);
			if (!advisoryId) {
				identityErrors.push(
					`missing advisory id for package=${packageName} severity=${severity}`,
				);
				continue;
			}

			const key = buildIdentityKey({
				package: packageName,
				severity,
				advisoryId,
			});
			const existing = findingsByKey.get(key);
			if (!existing) {
				findingsByKey.set(key, {
					package: packageName,
					severity,
					advisoryId,
					viaPackages,
					key,
				});
				continue;
			}
			existing.viaPackages = toUniqueStrings([...existing.viaPackages, ...viaPackages]);
		}
	}

	return {
		findings: Array.from(findingsByKey.values()),
		identityErrors,
	};
}

function runNpmAudit()
{
	const result = childProcess.spawnSync('npm', ['audit', '--json'], {
		cwd: repoRoot,
		encoding: 'utf8',
		maxBuffer: 20 * 1024 * 1024,
	});

	if (result.error) {
		throw new Error(`Failed to execute npm audit --json: ${result.error.message}`);
	}

	const stdout = typeof result.stdout === 'string' ? result.stdout : '';
	const stderr = typeof result.stderr === 'string' ? result.stderr.trim() : '';
	if (!stdout.trim()) {
		throw new Error(`npm audit --json returned empty output${stderr ? ` (stderr: ${stderr})` : ''}`);
	}

	try {
		return JSON.parse(stdout);
	} catch (error) {
		const message = error && error.message ? error.message : String(error);
		throw new Error(`Failed to parse npm audit JSON output: ${message}${stderr ? ` (stderr: ${stderr})` : ''}`);
	}
}

if (!fs.existsSync(allowlistPath)) {
	console.log('[audit:ci] Allowlist file not found at security/dependency-audit-allowlist.json.');
	console.log('[audit:ci] Create it or run the workflow updater that vendors canonical audit-gate assets.');
	process.exit(0);
}

let parsed;
try {
	const raw = fs.readFileSync(allowlistPath, 'utf8');
	parsed = JSON.parse(raw);
} catch (error) {
	console.error(`[audit:ci] Failed to parse allowlist: ${error && error.message ? error.message : String(error)}`);
	process.exit(1);
}

if (!Array.isArray(parsed)) {
	console.error('[audit:ci] Allowlist must be a JSON array.');
	process.exit(1);
}

const normalizedAllowlist = normalizeAllowlistEntries(parsed);
if (normalizedAllowlist.invalidIndices.length > 0) {
	console.error(
		`[audit:ci] Allowlist entries at indices ${normalizedAllowlist.invalidIndices.join(', ')} must include severity, package, and advisory id (GHSA preferred, CVE fallback).`,
	);
	process.exit(1);
}

console.log(`[audit:ci] Allowlist loaded (${parsed.length} entries).`);

const allowlistKeySet = new Set(normalizedAllowlist.normalized.map((entry) => entry.key));
let auditPayload;
try {
	auditPayload = runNpmAudit();
} catch (error) {
	console.error(`[audit:ci] ${error && error.message ? error.message : String(error)}`);
	process.exit(1);
}

const normalizedFindings = normalizeAuditFindings(auditPayload);
if (normalizedFindings.identityErrors.length > 0) {
	console.error('[audit:ci] Unable to normalize advisory identifiers from npm audit payload:');
	for (const identityError of normalizedFindings.identityErrors) {
		console.error(`[audit:ci] - ${identityError}`);
	}
	process.exit(1);
}

if (normalizedFindings.findings.length === 0) {
	console.log('[audit:ci] No vulnerabilities reported by npm audit.');
	process.exit(0);
}

const unmatched = normalizedFindings.findings.filter((finding) => !allowlistKeySet.has(finding.key));
if (unmatched.length === 0) {
	console.log(`[audit:ci] All ${normalizedFindings.findings.length} vulnerability finding(s) matched allowlist entries.`);
	process.exit(0);
}

console.error(`[audit:ci] Found ${unmatched.length} unallowlisted vulnerability finding(s):`);
for (const finding of unmatched) {
	const viaPackagesText = finding.viaPackages.length > 0
		? ` viaPackages=${finding.viaPackages.join(' > ')}`
		: '';
	console.error(
		`[audit:ci] - severity=${finding.severity} package=${finding.package} advisoryId=${finding.advisoryId}${viaPackagesText}`,
	);
}
process.exit(1);
