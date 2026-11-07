/**
 * claude-pool-broker — hands the Claude account pool to trusted Actions jobs.
 *
 * docs/plans/replace-claude-sessions-with-cli-engine-plan.md, "Token broker".
 *
 * POST /v1/pool with `Authorization: Bearer <GitHub Actions OIDC token>`
 * (audience `coding-workflows-claude-pool`). The token must:
 *   - verify against GitHub's JWKS (RS256), with iss, aud, exp, nbf, and an
 *     iat at most 10 minutes old;
 *   - come from repository_owner `shubhodeep1`;
 *   - come from shubhodeep1/coding-workflows or a repository listed in its
 *     .github/ai/consumer_repos.json (fetched from main, cached 10 minutes,
 *     last good copy kept when a fetch fails);
 *   - name a job_workflow_ref under shubhodeep1/coding-workflows/.github/workflows/,
 *     i.e. a job of a coding-workflows reusable workflow, never a consumer's
 *     own workflow file;
 *   - name one of the exact workflow files in ALLOWED_WORKFLOW_FILES (the
 *     reusable workflows that use .github/actions/claude-pool-token), at
 *     refs/heads/main or at a 40-hex commit SHA that equals the signed
 *     job_workflow_sha and that GitHub's compare API reports as an ancestor
 *     of (or identical to) main or stable. Any other ref, including a
 *     feature branch or refs/heads/stable given as a branch, is refused.
 *     A verification call that fails, times out or is rate limited is
 *     refused too (403 workflow_sha_unverifiable); nothing is granted on
 *     uncertainty (issue #6636).
 *   - or, for shubhodeep1/coding-workflows only, name refs/heads/promote-candidate
 *     (the branch its internal-*.yml wrappers call), with a signed 40-hex
 *     job_workflow_sha that passes the same main/stable ancestry check. The
 *     branch may be unprotected, so the ref alone never grants anything.
 *
 * Success: 200 `{"accounts":[{"name","token"}],"probe_model","gate"}`.
 * Refusal: 403 `{"error": <reason code>}`. Broker-side trouble (JWKS or
 * registry unreachable, pool secret missing): 503, which clients treat as
 * "Claude unavailable" and run codex (plan D1).
 *
 * The pool is the CLAUDE_POOL_TOKENS secret (a JSON array of {name, token}),
 * written only by claude-workers' key-sync workflow. Nothing here logs a
 * token or the OIDC JWT; log lines carry reason codes and the repository.
 *
 * ChatGPT account store (docs/plans/chatgpt-account-reviewer-trial-plan.md,
 * Phase 1): one ChatgptAccount Durable Object per account name, listed in the
 * "chatgpt-registry" instance of the same class.
 *   - POST /v1/chatgpt (the /v1/pool OIDC check, unchanged): the usable
 *     account with the lowest weekly usage, `{account, auth_json}` with
 *     tokens.refresh_token removed; 503 chatgpt_pool_empty when none.
 *   - GET /v1/chatgpt/status (same check): per-account metadata, no token.
 *   - GET / PUT /v1/chatgpt/refresh/<NAME> (writer OIDC token only: audience
 *     WRITER_AUDIENCE, CHATGPT_WRITER_REPOSITORY, CHATGPT_WRITER_WORKFLOW at
 *     refs/heads/main): the single writer, claude-workers'
 *     chatgpt-pool-refresh.yml, reads the full file and writes it back with a
 *     compare-and-swap on `generation`. The broker never refreshes a token.
 * Every refusal on these routes is 403 (400 for a malformed writer body, 404
 * for an unknown route or account), broker-side trouble is 503, never 500.
 * Log lines carry reason codes and account names only.
 */

export interface Env {
	CLAUDE_POOL_TOKENS?: string;
	PROBE_MODEL?: string;
	GATE?: string;
	/** Optional read-only token for the GitHub compare calls; unauthenticated reads when unset. Never logged. */
	BROKER_GITHUB_READ_TOKEN?: string;
	/** ChatGPT usage gate in (0, 1]; DEFAULT_CHATGPT_GATE when unset or invalid. */
	CHATGPT_GATE?: string;
	/** The only repository whose workflow may write the ChatGPT store; an invalid value fails closed (503). */
	CHATGPT_WRITER_REPOSITORY?: string;
	/** The writer's workflow file name; an invalid value fails closed (503). */
	CHATGPT_WRITER_WORKFLOW?: string;
	/** Durable Object namespace of ChatgptAccount; missing means 503 chatgpt_store_unavailable. */
	CHATGPT_ACCOUNTS?: ChatgptNamespace;
}

/** The part of a Durable Object stub the broker uses. */
export interface ChatgptStub {
	fetch: (input: string, init?: RequestInit) => Promise<Response>;
}

/** The part of a Durable Object namespace the broker uses (no workers-types dependency). */
export interface ChatgptNamespace {
	idFromName: (name: string) => unknown;
	get: (id: unknown) => ChatgptStub;
}

/** The part of DurableObjectState that ChatgptAccount uses. */
export interface ChatgptStateLike {
	storage: {
		get: (key: string) => Promise<unknown>;
		put: (key: string, value: unknown) => Promise<void>;
	};
	blockConcurrencyWhile: <T>(callback: () => Promise<T>) => Promise<T>;
}

export interface Deps {
	fetch: (input: string, init?: RequestInit) => Promise<Response>;
	now: () => number;
	log: (line: string) => void;
}

export const ISSUER = "https://token.actions.githubusercontent.com";
export const JWKS_URL = `${ISSUER}/.well-known/jwks`;
export const AUDIENCE = "coding-workflows-claude-pool";
export const OWNER = "shubhodeep1";
export const SELF_REPO = "shubhodeep1/coding-workflows";
export const WORKFLOW_PREFIX = "shubhodeep1/coding-workflows/.github/workflows/";
export const REGISTRY_URL =
	"https://raw.githubusercontent.com/shubhodeep1/coding-workflows/main/.github/ai/consumer_repos.json";
export const MAX_TOKEN_AGE_SECONDS = 600;
export const CLOCK_SKEW_SECONDS = 60;
export const REGISTRY_TTL_MS = 10 * 60 * 1000;
export const JWKS_TTL_MS = 60 * 60 * 1000;
const JWKS_REFRESH_FLOOR_MS = 60 * 1000;
/**
 * Exact reusable workflow files that may receive the pool: the
 * .github/workflows/*.yml files that use .github/actions/claude-pool-token.
 * tests/test_claude_pool_token.py keeps this list equal to that set.
 */
export const ALLOWED_WORKFLOW_FILES: readonly string[] = Object.freeze([
	"check_failure_triage.yml",
	"clarify.yml",
	"claude-engine-smoke.yml",
	"implement.yml",
	"issue_pr_status.yml",
	"orchestrate.yml",
	"orchestrate_clarify_respond.yml",
	"orchestrate_poll.yml",
	"plan.yml",
	"review_autofix.yml",
	"security-audit.yml",
	"unblock_judge.yml",
	"validate.yml",
	"validation-refresh.yml",
	"workflow-failure-heal-intake.yml",
	"workflow-log-analysis.yml",
]);
export const TRUSTED_BRANCH_REF = "refs/heads/main";
/**
 * The branch coding-workflows' internal-*.yml wrappers call
 * (docs/plans/pin-promote-cycle-to-candidate-plan.md, Phase 2). Accepted only
 * for SELF_REPO and only when the signed job_workflow_sha is an ancestor of
 * (or identical to) main or stable.
 */
export const CANDIDATE_BRANCH_REF = "refs/heads/promote-candidate";
/**
 * A SHA-pinned workflow is trusted when it is reachable from one of these
 * branches. Assumption: both are protected and never force-pushed; release
 * tags are cut on stable, so ancestry of stable covers "equals a release tag".
 */
export const ANCESTOR_BRANCHES: readonly string[] = Object.freeze(["main", "stable"]);
export const GITHUB_API = "https://api.github.com/repos/shubhodeep1/coding-workflows";
export const VERIFY_TIMEOUT_MS = 5000;
export const VERIFIED_SHA_TTL_MS = 6 * 60 * 60 * 1000;
export const VERIFIED_SHA_MAX = 256;
export const SHA_RE = /^[0-9a-f]{40}$/;
/** Audience of the writer's OIDC token; reads keep AUDIENCE. */
export const WRITER_AUDIENCE = "coding-workflows-chatgpt-pool-writer";
export const DEFAULT_CHATGPT_WRITER_REPOSITORY = "shubhodeep1/claude-workers";
export const DEFAULT_CHATGPT_WRITER_WORKFLOW = "chatgpt-pool-refresh.yml";
export const DEFAULT_CHATGPT_GATE = 0.9;
/** An account whose auth.json was not written for this long drops out of /v1/chatgpt. */
export const CHATGPT_STALE_MS = 7 * 24 * 3600 * 1000;
export const CHATGPT_MAX_BODY_BYTES = 65536;
export const CHATGPT_MAX_ACCOUNTS = 32;
/** Lowercase, so it never equals an account name (^[A-Z0-9_]+$). */
export const CHATGPT_REGISTRY_OBJECT = "chatgpt-registry";
export const CHATGPT_OUTCOMES: readonly string[] = Object.freeze(["ok", "timeout", "auth_failed", "quota_exhausted", "run_failed"]);
const CHATGPT_WRITER_WORKFLOW_RE = /^[A-Za-z0-9_.-]{1,100}\.ya?ml$/;
const CHATGPT_CHECKED_AT_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|\+00:00)$/;
const SEED_SHA256_RE = /^[0-9a-f]{64}$/;
const CHATGPT_STORE_ORIGIN = "https://chatgpt-account.internal";
const DEFAULT_PROBE_MODEL = "claude-haiku-4-5-20251001";
const DEFAULT_GATE = 0.9;
const MAX_JWT_LENGTH = 8192;
const ACCOUNT_NAME_RE = /^[A-Z0-9_]{1,64}$/;
const REPO_RE = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const MODEL_RE = /^[a-z0-9][a-z0-9.-]{0,79}$/;

interface Jwk {
	kid?: string;
	kty?: string;
	n?: string;
	e?: string;
	alg?: string;
	use?: string;
}

class Refusal extends Error
{
	constructor(
		public readonly status: number,
		public readonly reason: string,
	) {
		super(reason);
	}
}

function json(status: number, body: unknown): Response
{
	return new Response(JSON.stringify(body), {
		status,
		headers: { "content-type": "application/json", "cache-control": "no-store" },
	});
}

function base64UrlDecode(segment: string): Uint8Array
{
	if (!/^[A-Za-z0-9_-]*$/.test(segment)) {
		throw new Refusal(403, "malformed_token");
	}
	const padded = segment.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((segment.length + 3) % 4);
	let binary: string;
	try {
		binary = atob(padded);
	} catch {
		throw new Refusal(403, "malformed_token");
	}
	const bytes = new Uint8Array(binary.length);
	for (let i = 0; i < binary.length; i++) {
		bytes[i] = binary.charCodeAt(i);
	}
	return bytes;
}

function decodeJsonSegment(segment: string): Record<string, unknown>
{
	try {
		const value = JSON.parse(new TextDecoder().decode(base64UrlDecode(segment)));
		if (value && typeof value === "object" && !Array.isArray(value)) {
			return value as Record<string, unknown>;
		}
	} catch (error) {
		if (error instanceof Refusal) {
			throw error;
		}
	}
	throw new Refusal(403, "malformed_token");
}

function isNumber(value: unknown): value is number
{
	return typeof value === "number" && Number.isFinite(value);
}

/** Parse the CLAUDE_POOL_TOKENS secret; invalid entries are dropped, duplicates keep the first. */
export function parsePool(raw: string | undefined): { name: string; token: string }[]
{
	if (!raw) {
		return [];
	}
	let value: unknown;
	try {
		value = JSON.parse(raw);
	} catch {
		return [];
	}
	if (!Array.isArray(value)) {
		return [];
	}
	const seen = new Set<string>();
	const accounts: { name: string; token: string }[] = [];
	for (const entry of value) {
		if (!entry || typeof entry !== "object") {
			continue;
		}
		const { name, token } = entry as { name?: unknown; token?: unknown };
		if (typeof name !== "string" || !ACCOUNT_NAME_RE.test(name) || seen.has(name)) {
			continue;
		}
		if (typeof token !== "string" || token.length === 0 || /\s/.test(token)) {
			continue;
		}
		seen.add(name);
		accounts.push({ name, token });
	}
	return accounts.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
}

/**
 * Split a job_workflow_ref into its workflow file and ref. Returns null unless
 * the ref has exactly one `@`, its path is WORKFLOW_PREFIX (case-insensitive)
 * followed by a single file name segment.
 */
export function parseWorkflowRef(value: unknown): { file: string; ref: string } | null
{
	if (typeof value !== "string") {
		return null;
	}
	const parts = value.split("@");
	if (parts.length !== 2) {
		return null;
	}
	const [path, ref] = parts;
	if (path.length <= WORKFLOW_PREFIX.length || path.slice(0, WORKFLOW_PREFIX.length).toLowerCase() !== WORKFLOW_PREFIX) {
		return null;
	}
	const file = path.slice(WORKFLOW_PREFIX.length);
	if (file.includes("/") || file.includes("\\")) {
		return null;
	}
	return { file, ref };
}

export type ChatgptOutcome = "ok" | "timeout" | "auth_failed" | "quota_exhausted" | "run_failed";
type ChatgptReset = number | string | null;

export interface ChatgptUsage {
	five_hour: number | null;
	weekly: number | null;
	resets: { five_hour: ChatgptReset; weekly: ChatgptReset };
}

/** One account's stored record (Durable Object key "record"). */
export interface ChatgptRecord {
	auth_json: Record<string, unknown>;
	seed_sha256: string;
	generation: number;
	last_refresh: string | null;
	last_outcome: ChatgptOutcome;
	forced_refresh: boolean;
	checked_at: string;
	usage: ChatgptUsage;
}

interface ChatgptWindow {
	used_percent: number | null;
	reset: ChatgptReset;
}

/** A validated PUT /v1/chatgpt/refresh/<NAME> body. */
export interface ChatgptWrite {
	seed_sha256: string;
	base_generation: number;
	reseed: boolean;
	outcome: ChatgptOutcome;
	forced_refresh: boolean;
	usage: { primary: ChatgptWindow | null; secondary: ChatgptWindow | null };
	checked_at: string;
	auth_json: Record<string, unknown> | null;
}

export interface ChatgptWriteResult {
	status: number;
	body: Record<string, unknown>;
	record?: ChatgptRecord;
}

function isPlainObject(value: unknown): value is Record<string, unknown>
{
	return !!value && typeof value === "object" && !Array.isArray(value);
}

function invalidBody(): never
{
	throw new Refusal(400, "invalid_body");
}

function parseReset(value: unknown): ChatgptReset
{
	if (value === undefined || value === null) {
		return null;
	}
	if (isNumber(value)) {
		return value;
	}
	if (typeof value === "string" && value.length <= 64) {
		return value;
	}
	return invalidBody();
}

function parseWindow(value: unknown): ChatgptWindow | null
{
	if (value === undefined || value === null) {
		return null;
	}
	if (!isPlainObject(value)) {
		return invalidBody();
	}
	let usedPercent: number | null = null;
	if (value.used_percent !== undefined && value.used_percent !== null) {
		if (!isNumber(value.used_percent) || value.used_percent < 0) {
			return invalidBody();
		}
		// Assumption: a reading above 100 is an exhausted window, not a reason
		// to drop the write (dropping it would lose a rotated auth.json).
		usedPercent = Math.min(value.used_percent, 100);
	}
	if (value.window_minutes !== undefined && value.window_minutes !== null && !isNumber(value.window_minutes)) {
		return invalidBody();
	}
	const resetsAt = parseReset(value.resets_at);
	const resetsIn = parseReset(value.resets_in_seconds);
	return { used_percent: usedPercent, reset: resetsAt ?? resetsIn };
}

/** Validate a writer PUT body (plan Phase 1 wire contract); throws 400 invalid_body. */
export function parseChatgptWrite(raw: unknown): ChatgptWrite
{
	if (!isPlainObject(raw)) {
		return invalidBody();
	}
	const { seed_sha256, base_generation, reseed, outcome, forced_refresh, usage, checked_at, auth_json } = raw;
	if (typeof seed_sha256 !== "string" || !SEED_SHA256_RE.test(seed_sha256)) {
		return invalidBody();
	}
	if (typeof base_generation !== "number" || !Number.isSafeInteger(base_generation) || base_generation < 0) {
		return invalidBody();
	}
	if (typeof reseed !== "boolean" || typeof forced_refresh !== "boolean") {
		return invalidBody();
	}
	if (typeof outcome !== "string" || !CHATGPT_OUTCOMES.includes(outcome)) {
		return invalidBody();
	}
	if (usage !== null && usage !== undefined && !isPlainObject(usage)) {
		return invalidBody();
	}
	const usageObject = isPlainObject(usage) ? usage : {};
	if (typeof checked_at !== "string" || checked_at.length > 40 || !CHATGPT_CHECKED_AT_RE.test(checked_at)) {
		return invalidBody();
	}
	let authJson: Record<string, unknown> | null = null;
	if (auth_json !== undefined && auth_json !== null) {
		if (!isPlainObject(auth_json) || !isPlainObject(auth_json.tokens)) {
			return invalidBody();
		}
		authJson = auth_json;
	}
	if (reseed && authJson === null) {
		return invalidBody();
	}
	return {
		seed_sha256,
		base_generation,
		reseed,
		outcome: outcome as ChatgptOutcome,
		forced_refresh,
		usage: { primary: parseWindow(usageObject.primary), secondary: parseWindow(usageObject.secondary) },
		checked_at,
		auth_json: authJson,
	};
}

/**
 * The compare-and-swap of one writer PUT. A reseed replaces the record and its
 * seed_sha256 but still checks base_generation (0 for a new account); any
 * other write needs an existing record and keeps its seed_sha256 and, when
 * auth_json is absent, the stored file. last_refresh is the broker's clock at
 * the last write that stored an auth_json.
 */
export function applyChatgptWrite(stored: ChatgptRecord | null, write: ChatgptWrite, nowIso: string): ChatgptWriteResult
{
	if (!stored && !write.reseed) {
		return { status: 404, body: { error: "not_seeded" } };
	}
	const current = stored ? stored.generation : 0;
	if (write.base_generation !== current) {
		return { status: 409, body: { error: "generation_conflict" } };
	}
	const usage: ChatgptUsage = {
		five_hour: write.usage.primary?.used_percent == null ? null : write.usage.primary.used_percent / 100,
		weekly: write.usage.secondary?.used_percent == null ? null : write.usage.secondary.used_percent / 100,
		resets: {
			five_hour: write.usage.primary?.reset ?? null,
			weekly: write.usage.secondary?.reset ?? null,
		},
	};
	const generation = current + 1;
	let record: ChatgptRecord;
	if (write.reseed || !stored) {
		record = {
			auth_json: write.auth_json as Record<string, unknown>,
			seed_sha256: write.seed_sha256,
			generation,
			last_refresh: nowIso,
			last_outcome: write.outcome,
			forced_refresh: write.forced_refresh,
			checked_at: write.checked_at,
			usage,
		};
	} else {
		record = {
			auth_json: write.auth_json ?? stored.auth_json,
			seed_sha256: stored.seed_sha256,
			generation,
			last_refresh: write.auth_json ? nowIso : stored.last_refresh,
			last_outcome: write.outcome,
			forced_refresh: write.forced_refresh,
			checked_at: write.checked_at,
			usage,
		};
	}
	return { status: 200, body: { generation }, record };
}

/**
 * Why an account cannot serve /v1/chatgpt, or null when it can: not_seeded,
 * auth_rejected (last outcome auth_failed), quota_exhausted, stale (no
 * auth.json written for CHATGPT_STALE_MS) or gated (a known window at or above
 * the gate).
 */
export function chatgptUsable(record: ChatgptRecord | null, gate: number, nowMs: number): string | null
{
	if (!record || !isPlainObject(record.auth_json)) {
		return "not_seeded";
	}
	if (record.last_outcome === "auth_failed") {
		return "auth_rejected";
	}
	if (record.last_outcome === "quota_exhausted") {
		return "quota_exhausted";
	}
	const refreshed = typeof record.last_refresh === "string" ? Date.parse(record.last_refresh) : Number.NaN;
	if (!Number.isFinite(refreshed) || nowMs - refreshed >= CHATGPT_STALE_MS) {
		return "stale";
	}
	const usage = record.usage;
	for (const value of [usage?.five_hour, usage?.weekly]) {
		if (isNumber(value) && value >= gate) {
			return "gated";
		}
	}
	return null;
}

/** A deep copy of auth_json without tokens.refresh_token. */
export function stripRefreshToken(authJson: Record<string, unknown>): Record<string, unknown>
{
	const copy = JSON.parse(JSON.stringify(authJson)) as Record<string, unknown>;
	if (isPlainObject(copy.tokens)) {
		delete copy.tokens.refresh_token;
	}
	return copy;
}

/**
 * One ChatGPT account (or, as the CHATGPT_REGISTRY_OBJECT instance, the list
 * of account names). A plain class with fetch(), so the vitest suite runs in
 * Node without cloudflare:workers. Reached only through the Worker's own
 * stub calls; its paths are not public routes.
 */
export class ChatgptAccount
{
	constructor(
		private readonly state: ChatgptStateLike,
		_env?: unknown,
	) {}

	async fetch(request: Request): Promise<Response>
	{
		const path = new URL(request.url).pathname;
		if (path === "/record") {
			if (request.method === "GET") {
				const stored = await this.state.storage.get("record");
				return isPlainObject(stored) ? json(200, stored) : json(404, { error: "not_seeded" });
			}
			if (request.method === "PUT") {
				let write: ChatgptWrite;
				let now: unknown;
				try {
					const body = (await request.json()) as { write?: unknown; now?: unknown };
					write = parseChatgptWrite(body.write);
					now = body.now;
				} catch {
					return json(400, { error: "invalid_body" });
				}
				if (typeof now !== "string") {
					return json(400, { error: "invalid_body" });
				}
				const result = await this.state.blockConcurrencyWhile(async () => {
					const stored = await this.state.storage.get("record");
					const outcome = applyChatgptWrite(isPlainObject(stored) ? (stored as unknown as ChatgptRecord) : null, write, now as string);
					if (outcome.status === 200 && outcome.record) {
						await this.state.storage.put("record", outcome.record);
					}
					return outcome;
				});
				return json(result.status, result.body);
			}
			return json(405, { error: "method_not_allowed" });
		}
		if (path === "/names") {
			if (request.method === "GET") {
				const names = await this.state.storage.get("names");
				return json(200, { names: Array.isArray(names) ? names : [] });
			}
			if (request.method === "POST") {
				let name: unknown;
				try {
					name = ((await request.json()) as { name?: unknown }).name;
				} catch {
					return json(400, { error: "invalid_body" });
				}
				if (typeof name !== "string" || !ACCOUNT_NAME_RE.test(name)) {
					return json(400, { error: "invalid_body" });
				}
				return this.state.blockConcurrencyWhile(async () => {
					const stored = await this.state.storage.get("names");
					const names = Array.isArray(stored) ? (stored as string[]).slice() : [];
					if (names.includes(name as string)) {
						return json(200, { names });
					}
					if (names.length >= CHATGPT_MAX_ACCOUNTS) {
						return json(507, { error: "chatgpt_registry_full" });
					}
					names.push(name as string);
					names.sort();
					await this.state.storage.put("names", names);
					return json(200, { names });
				});
			}
			return json(405, { error: "method_not_allowed" });
		}
		return json(404, { error: "not_found" });
	}
}

/** The bearer JWT of a request; 403 missing_token otherwise. */
function bearerToken(request: Request): string
{
	const authorization = request.headers.get("authorization") || "";
	const match = /^Bearer[ \t]+([A-Za-z0-9_.-]+)$/i.exec(authorization);
	if (!match) {
		throw new Refusal(403, "missing_token");
	}
	return match[1];
}

type ShaVerdict = "trusted" | "untrusted" | "unavailable";

export function createHandler(deps: Deps)
{
	let jwks: { keys: Jwk[]; fetchedAt: number } | null = null;
	let registry: { repos: Set<string>; fetchedAt: number } | null = null;
	// Positive-only cache: an ancestor of a protected branch stays one.
	const verifiedShas = new Map<string, number>();

	async function compareStatus(branch: string, sha: string, env: Env): Promise<"yes" | "no" | "unavailable">
	{
		const headers: Record<string, string> = {
			accept: "application/vnd.github+json",
			"x-github-api-version": "2022-11-28",
			"user-agent": "claude-pool-broker",
		};
		const readToken = env.BROKER_GITHUB_READ_TOKEN;
		if (typeof readToken === "string" && readToken.trim() !== "") {
			headers.authorization = `Bearer ${readToken.trim()}`;
		}
		let response: Response;
		try {
			response = await deps.fetch(`${GITHUB_API}/compare/${branch}...${sha}?per_page=1`, {
				headers,
				signal: AbortSignal.timeout(VERIFY_TIMEOUT_MS),
			});
		} catch {
			return "unavailable";
		}
		if (response.status === 404 || response.status === 422) {
			return "no";
		}
		if (response.status !== 200) {
			return "unavailable";
		}
		let body: unknown;
		try {
			body = await response.json();
		} catch {
			return "unavailable";
		}
		const status = body && typeof body === "object" ? (body as { status?: unknown }).status : undefined;
		if (status === "behind" || status === "identical") {
			return "yes";
		}
		if (status === "ahead" || status === "diverged") {
			return "no";
		}
		return "unavailable";
	}

	async function verifyTrustedSha(sha: string, env: Env): Promise<ShaVerdict>
	{
		const now = deps.now();
		const cachedAt = verifiedShas.get(sha);
		if (cachedAt !== undefined) {
			if (now - cachedAt < VERIFIED_SHA_TTL_MS) {
				deps.log(`CLAUDE_POOL broker workflow_sha_check sha=${sha.slice(0, 12)} result=trusted source=cache`);
				return "trusted";
			}
			verifiedShas.delete(sha);
		}
		let verdict: ShaVerdict = "untrusted";
		for (const branch of ANCESTOR_BRANCHES) {
			const result = await compareStatus(branch, sha, env);
			if (result === "unavailable") {
				verdict = "unavailable";
				break;
			}
			if (result === "yes") {
				verdict = "trusted";
				break;
			}
		}
		if (verdict === "trusted") {
			verifiedShas.set(sha, now);
			while (verifiedShas.size > VERIFIED_SHA_MAX) {
				const oldest = verifiedShas.keys().next().value;
				if (oldest === undefined) {
					break;
				}
				verifiedShas.delete(oldest);
			}
		}
		deps.log(`CLAUDE_POOL broker workflow_sha_check sha=${sha.slice(0, 12)} result=${verdict} source=api`);
		return verdict;
	}

	async function loadJwks(force: boolean): Promise<Jwk[]>
	{
		const now = deps.now();
		if (jwks && !force && now - jwks.fetchedAt < JWKS_TTL_MS) {
			return jwks.keys;
		}
		if (jwks && force && now - jwks.fetchedAt < JWKS_REFRESH_FLOOR_MS) {
			return jwks.keys;
		}
		try {
			const response = await deps.fetch(JWKS_URL, { headers: { accept: "application/json" } });
			if (!response.ok) {
				throw new Error(`status ${response.status}`);
			}
			const body = (await response.json()) as { keys?: unknown };
			if (!Array.isArray(body.keys)) {
				throw new Error("no keys");
			}
			jwks = { keys: body.keys as Jwk[], fetchedAt: now };
			return jwks.keys;
		} catch {
			if (jwks) {
				return jwks.keys;
			}
			throw new Refusal(503, "jwks_unavailable");
		}
	}

	async function loadRegistry(): Promise<Set<string>>
	{
		const now = deps.now();
		if (registry && now - registry.fetchedAt < REGISTRY_TTL_MS) {
			return registry.repos;
		}
		try {
			const response = await deps.fetch(REGISTRY_URL, { headers: { accept: "application/json" } });
			if (!response.ok) {
				throw new Error(`status ${response.status}`);
			}
			const body: unknown = await response.json();
			if (!Array.isArray(body) || !body.every((item) => typeof item === "string")) {
				throw new Error("registry is not a list of repositories");
			}
			const repos = new Set<string>([SELF_REPO.toLowerCase()]);
			for (const item of body as string[]) {
				if (REPO_RE.test(item)) {
					repos.add(item.toLowerCase());
				}
			}
			registry = { repos, fetchedAt: now };
			return repos;
		} catch {
			if (registry) {
				deps.log("CLAUDE_POOL broker registry_fetch_failed using=last_good");
				return registry.repos;
			}
			throw new Refusal(503, "registry_unavailable");
		}
	}

	async function verify(jwt: string, audienceRequired: string = AUDIENCE): Promise<Record<string, unknown>>
	{
		if (jwt.length > MAX_JWT_LENGTH) {
			throw new Refusal(403, "malformed_token");
		}
		const parts = jwt.split(".");
		if (parts.length !== 3) {
			throw new Refusal(403, "malformed_token");
		}
		const header = decodeJsonSegment(parts[0]);
		const claims = decodeJsonSegment(parts[1]);
		const signature = base64UrlDecode(parts[2]);
		if (header.alg !== "RS256" || typeof header.kid !== "string") {
			throw new Refusal(403, "bad_algorithm");
		}
		let key = (await loadJwks(false)).find((candidate) => candidate.kid === header.kid);
		if (!key) {
			key = (await loadJwks(true)).find((candidate) => candidate.kid === header.kid);
		}
		if (!key || key.kty !== "RSA" || typeof key.n !== "string" || typeof key.e !== "string") {
			throw new Refusal(403, "unknown_key");
		}
		let valid = false;
		try {
			const cryptoKey = await crypto.subtle.importKey(
				"jwk",
				{ kty: "RSA", n: key.n, e: key.e, alg: "RS256", ext: true },
				{ name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
				false,
				["verify"],
			);
			valid = await crypto.subtle.verify(
				"RSASSA-PKCS1-v1_5",
				cryptoKey,
				signature,
				new TextEncoder().encode(`${parts[0]}.${parts[1]}`),
			);
		} catch {
			valid = false;
		}
		if (!valid) {
			throw new Refusal(403, "bad_signature");
		}
		const now = Math.floor(deps.now() / 1000);
		if (claims.iss !== ISSUER) {
			throw new Refusal(403, "bad_issuer");
		}
		const audience = claims.aud;
		if (!(audience === audienceRequired || (Array.isArray(audience) && audience.includes(audienceRequired)))) {
			throw new Refusal(403, "bad_audience");
		}
		if (!isNumber(claims.exp) || claims.exp <= now - CLOCK_SKEW_SECONDS) {
			throw new Refusal(403, "expired");
		}
		if (claims.nbf !== undefined && (!isNumber(claims.nbf) || claims.nbf > now + CLOCK_SKEW_SECONDS)) {
			throw new Refusal(403, "not_yet_valid");
		}
		if (!isNumber(claims.iat) || claims.iat > now + CLOCK_SKEW_SECONDS) {
			throw new Refusal(403, "not_yet_valid");
		}
		if (now - claims.iat > MAX_TOKEN_AGE_SECONDS) {
			throw new Refusal(403, "too_old");
		}
		return claims;
	}

	async function authorize(claims: Record<string, unknown>, env: Env): Promise<string>
	{
		if (typeof claims.repository_owner !== "string" || claims.repository_owner.toLowerCase() !== OWNER) {
			throw new Refusal(403, "wrong_owner");
		}
		const repository = claims.repository;
		if (typeof repository !== "string" || !REPO_RE.test(repository)) {
			throw new Refusal(403, "repo_not_allowed");
		}
		if (!repository.toLowerCase().startsWith(`${OWNER}/`)) {
			throw new Refusal(403, "wrong_owner");
		}
		if (repository.toLowerCase() !== SELF_REPO.toLowerCase()) {
			const repos = await loadRegistry();
			if (!repos.has(repository.toLowerCase())) {
				throw new Refusal(403, "repo_not_allowed");
			}
		}
		const workflow = parseWorkflowRef(claims.job_workflow_ref);
		if (!workflow) {
			throw new Refusal(403, "wrong_workflow");
		}
		if (!ALLOWED_WORKFLOW_FILES.includes(workflow.file)) {
			throw new Refusal(403, "workflow_not_allowed");
		}
		if (workflow.ref === TRUSTED_BRANCH_REF) {
			return repository;
		}
		if (workflow.ref === CANDIDATE_BRANCH_REF && repository.toLowerCase() === SELF_REPO.toLowerCase()) {
			const signedSha = claims.job_workflow_sha;
			if (typeof signedSha !== "string" || !SHA_RE.test(signedSha)) {
				throw new Refusal(403, "workflow_sha_mismatch");
			}
			const verdict = await verifyTrustedSha(signedSha, env);
			if (verdict === "trusted") {
				return repository;
			}
			throw new Refusal(403, verdict === "untrusted" ? "workflow_sha_unverified" : "workflow_sha_unverifiable");
		}
		if (SHA_RE.test(workflow.ref)) {
			const signedSha = claims.job_workflow_sha;
			if (typeof signedSha !== "string" || signedSha !== workflow.ref) {
				throw new Refusal(403, "workflow_sha_mismatch");
			}
			const verdict = await verifyTrustedSha(workflow.ref, env);
			if (verdict === "trusted") {
				return repository;
			}
			throw new Refusal(403, verdict === "untrusted" ? "workflow_sha_unverified" : "workflow_sha_unverifiable");
		}
		throw new Refusal(403, "workflow_ref_not_allowed");
	}

	function writerConfig(env: Env): { repository: string; workflow: string }
	{
		const repository = env.CHATGPT_WRITER_REPOSITORY ?? DEFAULT_CHATGPT_WRITER_REPOSITORY;
		const workflow = env.CHATGPT_WRITER_WORKFLOW ?? DEFAULT_CHATGPT_WRITER_WORKFLOW;
		if (typeof repository !== "string" || !REPO_RE.test(repository)) {
			throw new Refusal(503, "writer_config_invalid");
		}
		if (typeof workflow !== "string" || !CHATGPT_WRITER_WORKFLOW_RE.test(workflow)) {
			throw new Refusal(503, "writer_config_invalid");
		}
		return { repository, workflow };
	}

	function checkWriterWorkflowRef(value: unknown, config: { repository: string; workflow: string }): void
	{
		if (typeof value !== "string") {
			throw new Refusal(403, "workflow_not_allowed");
		}
		const parts = value.split("@");
		if (parts.length !== 2) {
			throw new Refusal(403, "workflow_not_allowed");
		}
		const [path, ref] = parts;
		const prefix = `${config.repository}/.github/workflows/`.toLowerCase();
		if (path.length <= prefix.length || path.slice(0, prefix.length).toLowerCase() !== prefix) {
			throw new Refusal(403, "workflow_not_allowed");
		}
		if (path.slice(prefix.length) !== config.workflow) {
			throw new Refusal(403, "workflow_not_allowed");
		}
		if (ref !== TRUSTED_BRANCH_REF) {
			throw new Refusal(403, "workflow_ref_not_allowed");
		}
	}

	/**
	 * The ChatGPT writer: CHATGPT_WRITER_REPOSITORY's CHATGPT_WRITER_WORKFLOW
	 * on refs/heads/main, as a top-level workflow (workflow_ref and
	 * job_workflow_ref both name it), so the token is never handed to a
	 * reusable workflow elsewhere. No registry fetch, no compare call.
	 */
	function authorizeWriter(claims: Record<string, unknown>, env: Env): string
	{
		const config = writerConfig(env);
		const repository = claims.repository;
		if (typeof repository !== "string" || repository.toLowerCase() !== config.repository.toLowerCase()) {
			throw new Refusal(403, "repo_not_allowed");
		}
		checkWriterWorkflowRef(claims.workflow_ref, config);
		checkWriterWorkflowRef(claims.job_workflow_ref, config);
		if (claims.ref !== TRUSTED_BRANCH_REF) {
			throw new Refusal(403, "workflow_ref_not_allowed");
		}
		return repository;
	}

	function chatgptGate(env: Env): number
	{
		const gate = Number(env.CHATGPT_GATE ?? DEFAULT_CHATGPT_GATE);
		return Number.isFinite(gate) && gate > 0 && gate <= 1 ? gate : DEFAULT_CHATGPT_GATE;
	}

	async function storeCall(env: Env, objectName: string, method: string, path: string, body?: unknown): Promise<{ status: number; body: Record<string, unknown> }>
	{
		const namespace = env.CHATGPT_ACCOUNTS;
		if (!namespace || typeof namespace.idFromName !== "function" || typeof namespace.get !== "function") {
			throw new Refusal(503, "chatgpt_store_unavailable");
		}
		let response: Response;
		try {
			const stub = namespace.get(namespace.idFromName(objectName));
			const init: RequestInit = { method };
			if (body !== undefined) {
				init.body = JSON.stringify(body);
				init.headers = { "content-type": "application/json" };
			}
			response = await stub.fetch(`${CHATGPT_STORE_ORIGIN}${path}`, init);
		} catch {
			throw new Refusal(503, "chatgpt_store_unavailable");
		}
		let parsed: unknown;
		try {
			parsed = await response.json();
		} catch {
			throw new Refusal(503, "chatgpt_store_unavailable");
		}
		if (!isPlainObject(parsed)) {
			throw new Refusal(503, "chatgpt_store_unavailable");
		}
		return { status: response.status, body: parsed };
	}

	async function loadChatgptAccounts(env: Env): Promise<{ name: string; record: ChatgptRecord | null }[]>
	{
		const listed = await storeCall(env, CHATGPT_REGISTRY_OBJECT, "GET", "/names");
		const names = listed.body.names;
		if (listed.status !== 200 || !Array.isArray(names)) {
			throw new Refusal(503, "chatgpt_store_unavailable");
		}
		const valid = [...new Set(names.filter((name): name is string => typeof name === "string" && ACCOUNT_NAME_RE.test(name)))].sort();
		return Promise.all(
			valid.map(async (name) => {
				const stored = await storeCall(env, name, "GET", "/record");
				if (stored.status === 404) {
					return { name, record: null };
				}
				if (stored.status !== 200) {
					throw new Refusal(503, "chatgpt_store_unavailable");
				}
				return { name, record: stored.body as unknown as ChatgptRecord };
			}),
		);
	}

	function statusUsage(record: ChatgptRecord | null): ChatgptUsage
	{
		const usage = record && isPlainObject(record.usage) ? record.usage : null;
		const resets = usage && isPlainObject(usage.resets) ? usage.resets : null;
		const reset = (value: unknown): ChatgptReset => (isNumber(value) || typeof value === "string" ? value : null);
		return {
			five_hour: usage && isNumber(usage.five_hour) ? usage.five_hour : null,
			weekly: usage && isNumber(usage.weekly) ? usage.weekly : null,
			resets: { five_hour: reset(resets?.five_hour), weekly: reset(resets?.weekly) },
		};
	}

	async function readBody(request: Request): Promise<unknown>
	{
		const declared = Number(request.headers.get("content-length") ?? "0");
		if (Number.isFinite(declared) && declared > CHATGPT_MAX_BODY_BYTES) {
			throw new Refusal(400, "invalid_body");
		}
		let text: string;
		try {
			text = await request.text();
		} catch {
			throw new Refusal(400, "invalid_body");
		}
		if (new TextEncoder().encode(text).length > CHATGPT_MAX_BODY_BYTES) {
			throw new Refusal(400, "invalid_body");
		}
		try {
			return JSON.parse(text);
		} catch {
			throw new Refusal(400, "invalid_body");
		}
	}

	async function handleChatgpt(request: Request, env: Env, url: URL): Promise<Response>
	{
		const path = url.pathname;
		let route: "read" | "status" | "writer";
		let writerName = "";
		if (path === "/v1/chatgpt") {
			route = "read";
			if (request.method !== "POST") {
				return json(405, { error: "method_not_allowed" });
			}
		} else if (path === "/v1/chatgpt/status") {
			route = "status";
			if (request.method !== "GET") {
				return json(405, { error: "method_not_allowed" });
			}
		} else if (path.startsWith("/v1/chatgpt/refresh/")) {
			route = "writer";
			writerName = path.slice("/v1/chatgpt/refresh/".length);
			if (request.method !== "GET" && request.method !== "PUT") {
				return json(405, { error: "method_not_allowed" });
			}
		} else {
			return json(404, { error: "not_found" });
		}
		try {
			const jwt = bearerToken(request);
			if (route === "writer") {
				authorizeWriter(await verify(jwt, WRITER_AUDIENCE), env);
				if (!ACCOUNT_NAME_RE.test(writerName)) {
					throw new Refusal(404, "not_found");
				}
				if (request.method === "GET") {
					const stored = await storeCall(env, writerName, "GET", "/record");
					if (stored.status === 404) {
						deps.log(`CHATGPT_POOL broker refresh_read account=${writerName} result=not_seeded`);
						return json(404, { error: "not_seeded" });
					}
					const record = stored.body as unknown as ChatgptRecord;
					if (stored.status !== 200 || !isPlainObject(record.auth_json) || !Number.isSafeInteger(record.generation) || typeof record.seed_sha256 !== "string") {
						throw new Refusal(503, "chatgpt_store_unavailable");
					}
					deps.log(`CHATGPT_POOL broker refresh_read account=${writerName} result=found generation=${record.generation}`);
					return json(200, { auth_json: record.auth_json, generation: record.generation, seed_sha256: record.seed_sha256 });
				}
				// The account object re-validates the raw body itself.
				const rawWrite = await readBody(request);
				const write = parseChatgptWrite(rawWrite);
				if (write.reseed) {
					// Idempotent; a failed account write below leaves a listed but
					// unseeded name, which the read routes skip.
					const added = await storeCall(env, CHATGPT_REGISTRY_OBJECT, "POST", "/names", { name: writerName });
					if (added.status === 507) {
						throw new Refusal(503, "chatgpt_registry_full");
					}
					if (added.status !== 200) {
						throw new Refusal(503, "chatgpt_store_unavailable");
					}
				}
				const result = await storeCall(env, writerName, "PUT", "/record", { write: rawWrite, now: new Date(deps.now()).toISOString() });
				if (result.status === 200 && Number.isSafeInteger(result.body.generation)) {
					deps.log(`CHATGPT_POOL broker refresh account=${writerName} outcome=${write.outcome} generation=${result.body.generation} reseed=${write.reseed}`);
					return json(200, { generation: result.body.generation });
				}
				if (result.status === 404 || result.status === 409) {
					const reason = result.status === 404 ? "not_seeded" : "generation_conflict";
					deps.log(`CHATGPT_POOL broker refused route=writer reason=${reason} status=${result.status} account=${writerName}`);
					return json(result.status, { error: reason });
				}
				throw new Refusal(503, "chatgpt_store_unavailable");
			}
			const repository = await authorize(await verify(jwt), env);
			const gate = chatgptGate(env);
			const nowMs = deps.now();
			const accounts = await loadChatgptAccounts(env);
			if (route === "status") {
				deps.log(`CHATGPT_POOL broker status accounts=${accounts.length}`);
				return json(200, {
					accounts: accounts.map(({ name, record }) => {
						const reason = chatgptUsable(record, gate, nowMs);
						return {
							account: name,
							generation: record && Number.isSafeInteger(record.generation) ? record.generation : 0,
							last_refresh: record && typeof record.last_refresh === "string" ? record.last_refresh : null,
							last_outcome: record && typeof record.last_outcome === "string" ? record.last_outcome : null,
							usage: statusUsage(record),
							usable: reason === null,
							reason,
						};
					}),
					gate,
				});
			}
			const usable = accounts.filter(({ record }) => chatgptUsable(record, gate, nowMs) === null);
			usable.sort((a, b) => {
				const wa = isNumber(a.record?.usage?.weekly) ? (a.record?.usage?.weekly as number) : Number.POSITIVE_INFINITY;
				const wb = isNumber(b.record?.usage?.weekly) ? (b.record?.usage?.weekly as number) : Number.POSITIVE_INFINITY;
				if (wa !== wb) {
					return wa < wb ? -1 : 1;
				}
				return a.name < b.name ? -1 : a.name > b.name ? 1 : 0;
			});
			const chosen = usable[0];
			if (!chosen || !chosen.record) {
				throw new Refusal(503, "chatgpt_pool_empty");
			}
			deps.log(`CHATGPT_POOL broker granted repository=${repository} account=${chosen.name}`);
			return json(200, { account: chosen.name, auth_json: stripRefreshToken(chosen.record.auth_json) });
		} catch (error) {
			if (error instanceof Refusal) {
				deps.log(`CHATGPT_POOL broker refused route=${route} reason=${error.reason} status=${error.status}`);
				return json(error.status, { error: error.reason });
			}
			deps.log(`CHATGPT_POOL broker error route=${route} reason=internal`);
			return json(503, { error: "chatgpt_store_unavailable" });
		}
	}

	return async function handle(request: Request, env: Env): Promise<Response>
	{
		const url = new URL(request.url);
		if (url.pathname === "/v1/chatgpt" || url.pathname.startsWith("/v1/chatgpt/")) {
			return handleChatgpt(request, env, url);
		}
		if (url.pathname !== "/v1/pool") {
			return json(404, { error: "not_found" });
		}
		if (request.method !== "POST") {
			return json(405, { error: "method_not_allowed" });
		}
		let repository = "";
		try {
			repository = await authorize(await verify(bearerToken(request)), env);
			const accounts = parsePool(env.CLAUDE_POOL_TOKENS);
			if (accounts.length === 0) {
				throw new Refusal(503, "pool_empty");
			}
			const gate = Number(env.GATE ?? DEFAULT_GATE);
			const probeModel = env.PROBE_MODEL && MODEL_RE.test(env.PROBE_MODEL) ? env.PROBE_MODEL : DEFAULT_PROBE_MODEL;
			deps.log(`CLAUDE_POOL broker granted repository=${repository} accounts=${accounts.length}`);
			return json(200, {
				accounts,
				probe_model: probeModel,
				gate: Number.isFinite(gate) && gate > 0 && gate <= 1 ? gate : DEFAULT_GATE,
			});
		} catch (error) {
			if (error instanceof Refusal) {
				deps.log(`CLAUDE_POOL broker refused reason=${error.reason} status=${error.status} repository=${repository || "unknown"}`);
				return json(error.status, { error: error.reason });
			}
			deps.log("CLAUDE_POOL broker error reason=internal");
			return json(500, { error: "internal" });
		}
	};
}

const handler = createHandler({
	fetch: (input, init) => fetch(input, init),
	now: () => Date.now(),
	log: (line) => console.log(line),
});

export default {
	fetch(request: Request, env: Env): Promise<Response>
	{
		return handler(request, env);
	},
};
