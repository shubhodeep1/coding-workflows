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
 * POST /v1/chatgpt (ChatGPT account reviewer trial, Phase 1) hands one ChatGPT
 * credential to a trusted job. It runs the same verify/authorize path as
 * /v1/pool with its own audience (`coding-workflows-chatgpt-pool`) and its own
 * workflow allowlist, CHATGPT_ALLOWED_WORKFLOW_FILES, which is empty until a
 * later phase wires a workflow in, so every caller is refused for now. The
 * credential lives in the ChatgptPoolStore Durable Object, seeded from the
 * CHATGPT_POOL_SEED secret (a new secret value re-seeds it) and refreshed in
 * the object when it nears expiry; the rotated refresh token is written back
 * to the object and never leaves it. Success: 200
 * `{"access_token","id_token","account_id","expires_at"}`. Refusal 403 and
 * broker-side trouble 503, as for /v1/pool. Log lines carry the CHATGPT_POOL
 * prefix and never a token, the seed or the JWT.
 */

/** The parts of a Durable Object storage API the ChatGPT store uses. */
export interface DurableObjectStorageLike {
	get(key: string): Promise<unknown>;
	put(key: string, value: unknown): Promise<void>;
}

/** The parts of a Durable Object stub the Worker uses. */
export interface DurableObjectStubLike {
	fetch(input: string, init?: RequestInit): Promise<Response>;
}

/** The parts of a Durable Object namespace binding the Worker uses. */
export interface DurableObjectNamespaceLike {
	idFromName(name: string): unknown;
	get(id: unknown): DurableObjectStubLike;
}

export interface Env {
	CLAUDE_POOL_TOKENS?: string;
	PROBE_MODEL?: string;
	GATE?: string;
	/** Optional read-only token for the GitHub compare calls; unauthenticated reads when unset. Never logged. */
	BROKER_GITHUB_READ_TOKEN?: string;
	/** Secret: the ChatGPT credential JSON (flat or `{"tokens": {...}}`). Never logged. */
	CHATGPT_POOL_SEED?: string;
	/** The OAuth token endpoint for the refresh; empty (the default) turns refresh off. */
	CHATGPT_REFRESH_URL?: string;
	/** The OAuth client id for the refresh; empty (the default) turns refresh off. */
	CHATGPT_OAUTH_CLIENT_ID?: string;
	/** Refresh when fewer seconds than this remain (default 1800, limited to 300-86400). */
	CHATGPT_REFRESH_BEFORE_SECONDS?: string;
	/** The ChatgptPoolStore Durable Object namespace. */
	CHATGPT_POOL?: DurableObjectNamespaceLike;
}

export interface Deps {
	fetch: (input: string, init?: RequestInit) => Promise<Response>;
	now: () => number;
	log: (line: string) => void;
	/** Tests only: replaces CHATGPT_ALLOWED_WORKFLOW_FILES. The production handler never sets it. */
	chatgptAllowedWorkflowFiles?: readonly string[];
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
/** The OIDC audience /v1/chatgpt accepts; a /v1/pool token is refused there and the reverse. */
export const CHATGPT_AUDIENCE = "coding-workflows-chatgpt-pool";
export const CHATGPT_PATH = "/v1/chatgpt";
/**
 * Exact reusable workflow files that may receive the ChatGPT credential: the
 * .github/workflows/*.yml files that use .github/actions/chatgpt-pool-token.
 * tests/test_chatgpt_pool_token.py keeps this list equal to that set. Empty
 * in Phase 1, so /v1/chatgpt refuses every caller with workflow_not_allowed.
 */
export const CHATGPT_ALLOWED_WORKFLOW_FILES: readonly string[] = Object.freeze([]);
export const CHATGPT_POOL_OBJECT_NAME = "default";
export const CHATGPT_STORE_KEY = "credential";
/** A credential is handed out only with at least this many seconds left. */
export const CHATGPT_MIN_GRANT_SECONDS = 300;
export const DEFAULT_CHATGPT_REFRESH_BEFORE_SECONDS = 1800;
export const CHATGPT_REFRESH_TIMEOUT_MS = 10000;
export const CHATGPT_TOKEN_RE = /^[A-Za-z0-9_.-]{20,16384}$/;
export const CHATGPT_REFRESH_TOKEN_RE = /^[A-Za-z0-9_.~+/=-]{8,16384}$/;
export const CHATGPT_ACCOUNT_RE = /^[A-Za-z0-9_-]{1,128}$/;
const CHATGPT_ERROR_CODE_RE = /^[a-z_]{1,40}$/;
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

	async function verify(jwt: string, expectedAudience: string): Promise<Record<string, unknown>>
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
		if (!(audience === expectedAudience || (Array.isArray(audience) && audience.includes(expectedAudience)))) {
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

	async function authorize(claims: Record<string, unknown>, env: Env, allowedFiles: readonly string[]): Promise<string>
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
		if (!allowedFiles.includes(workflow.file)) {
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

	/**
	 * POST /v1/chatgpt: the same OIDC verification and authorization as
	 * /v1/pool (own audience and allowlist), then one grant from the
	 * ChatgptPoolStore Durable Object. The answer is rebuilt from four checked
	 * fields, so a refresh token can never leave the broker.
	 */
	async function handleChatgpt(request: Request, env: Env): Promise<Response>
	{
		let repository = "";
		try {
			const authorization = request.headers.get("authorization") || "";
			const match = /^Bearer[ \t]+([A-Za-z0-9_.-]+)$/i.exec(authorization);
			if (!match) {
				throw new Refusal(403, "missing_token");
			}
			repository = await authorize(
				await verify(match[1], CHATGPT_AUDIENCE),
				env,
				deps.chatgptAllowedWorkflowFiles ?? CHATGPT_ALLOWED_WORKFLOW_FILES,
			);
			const namespace = env.CHATGPT_POOL;
			if (!namespace || typeof namespace.get !== "function" || typeof namespace.idFromName !== "function") {
				throw new Refusal(503, "store_unavailable");
			}
			let response: Response;
			try {
				response = await namespace
					.get(namespace.idFromName(CHATGPT_POOL_OBJECT_NAME))
					.fetch("https://chatgpt-pool.internal/grant", { method: "POST" });
			} catch {
				throw new Refusal(503, "store_unavailable");
			}
			let body: unknown;
			try {
				body = await response.json();
			} catch {
				body = undefined;
			}
			const fields = body && typeof body === "object" && !Array.isArray(body) ? (body as Record<string, unknown>) : {};
			if (response.status !== 200) {
				const code = fields.error;
				throw new Refusal(503, typeof code === "string" && CHATGPT_ERROR_CODE_RE.test(code) ? code : "store_error");
			}
			const { access_token: accessToken, id_token: idToken, account_id: accountId, expires_at: expiresAt } = fields;
			if (
				typeof accessToken !== "string" || !CHATGPT_TOKEN_RE.test(accessToken) ||
				typeof idToken !== "string" || !CHATGPT_TOKEN_RE.test(idToken) ||
				typeof accountId !== "string" || !CHATGPT_ACCOUNT_RE.test(accountId) ||
				typeof expiresAt !== "number" || !Number.isSafeInteger(expiresAt) || expiresAt <= 0
			) {
				throw new Refusal(503, "store_error");
			}
			const expiresIn = expiresAt - Math.floor(deps.now() / 1000);
			deps.log(`CHATGPT_POOL broker granted repository=${repository} expires_in=${expiresIn} refreshed=${fields.refreshed === true}`);
			return json(200, { access_token: accessToken, id_token: idToken, account_id: accountId, expires_at: expiresAt });
		} catch (error) {
			if (error instanceof Refusal) {
				deps.log(`CHATGPT_POOL broker refused reason=${error.reason} status=${error.status} repository=${repository || "unknown"}`);
				return json(error.status, { error: error.reason });
			}
			deps.log("CHATGPT_POOL broker error reason=internal");
			return json(500, { error: "internal" });
		}
	}

	return async function handle(request: Request, env: Env): Promise<Response>
	{
		const url = new URL(request.url);
		if (url.pathname !== "/v1/pool" && url.pathname !== CHATGPT_PATH) {
			return json(404, { error: "not_found" });
		}
		if (request.method !== "POST") {
			return json(405, { error: "method_not_allowed" });
		}
		if (url.pathname === CHATGPT_PATH) {
			return handleChatgpt(request, env);
		}
		let repository = "";
		try {
			const authorization = request.headers.get("authorization") || "";
			const match = /^Bearer[ \t]+([A-Za-z0-9_.-]+)$/i.exec(authorization);
			if (!match) {
				throw new Refusal(403, "missing_token");
			}
			repository = await authorize(await verify(match[1], AUDIENCE), env, ALLOWED_WORKFLOW_FILES);
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

interface ChatgptCredential
{
	seed_fingerprint: string;
	access_token: string;
	id_token: string;
	refresh_token: string;
	account_id: string;
	/** Epoch seconds; 0 when unknown (the next grant then refreshes). */
	expires_at: number;
	refreshed_at: number;
	refresh_rejected: boolean;
}

interface ChatgptTokens
{
	access_token: string;
	id_token: string;
	refresh_token: string;
	account_id: string;
}

interface StoreResult
{
	status: number;
	body: Record<string, unknown>;
}

function chatgptTokensFrom(value: unknown): ChatgptTokens | null
{
	if (!value || typeof value !== "object" || Array.isArray(value)) {
		return null;
	}
	let fields = value as Record<string, unknown>;
	const nested = fields.tokens;
	if (nested && typeof nested === "object" && !Array.isArray(nested)) {
		fields = nested as Record<string, unknown>;
	}
	const { access_token: accessToken, id_token: idToken, refresh_token: refreshToken, account_id: accountId } = fields;
	if (
		typeof accessToken !== "string" || !CHATGPT_TOKEN_RE.test(accessToken) ||
		typeof idToken !== "string" || !CHATGPT_TOKEN_RE.test(idToken) ||
		typeof refreshToken !== "string" || !CHATGPT_REFRESH_TOKEN_RE.test(refreshToken) ||
		typeof accountId !== "string" || !CHATGPT_ACCOUNT_RE.test(accountId)
	) {
		return null;
	}
	return { access_token: accessToken, id_token: idToken, refresh_token: refreshToken, account_id: accountId };
}

function storedCredential(value: unknown): ChatgptCredential | null
{
	const tokens = chatgptTokensFrom(value);
	if (!tokens) {
		return null;
	}
	const fields = value as Record<string, unknown>;
	if (
		typeof fields.seed_fingerprint !== "string" ||
		typeof fields.expires_at !== "number" || !Number.isSafeInteger(fields.expires_at) ||
		typeof fields.refreshed_at !== "number" || !Number.isSafeInteger(fields.refreshed_at) ||
		typeof fields.refresh_rejected !== "boolean"
	) {
		return null;
	}
	return {
		...tokens,
		seed_fingerprint: fields.seed_fingerprint,
		expires_at: fields.expires_at,
		refreshed_at: fields.refreshed_at,
		refresh_rejected: fields.refresh_rejected,
	};
}

/** The `exp` claim of a JWT (payload decode only, no signature check), or null. */
export function jwtExpiry(token: string): number | null
{
	const parts = token.split(".");
	if (parts.length !== 3) {
		return null;
	}
	try {
		const exp = decodeJsonSegment(parts[1]).exp;
		return typeof exp === "number" && Number.isSafeInteger(exp) && exp > 0 ? exp : null;
	} catch {
		return null;
	}
}

async function sha256Hex(text: string): Promise<string>
{
	const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)));
	return Array.from(digest, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function chatgptRefreshBefore(env: Env): number
{
	const raw = (env.CHATGPT_REFRESH_BEFORE_SECONDS ?? "").trim();
	if (!/^[0-9]{1,6}$/.test(raw)) {
		return DEFAULT_CHATGPT_REFRESH_BEFORE_SECONDS;
	}
	const value = Number(raw);
	return value >= CHATGPT_MIN_GRANT_SECONDS && value <= 86400 ? value : DEFAULT_CHATGPT_REFRESH_BEFORE_SECONDS;
}

/**
 * The ChatGPT credential store behind /v1/chatgpt, run inside the
 * ChatgptPoolStore Durable Object. grant() seeds (or re-seeds, when the
 * CHATGPT_POOL_SEED value changed) the stored credential, refreshes it when it
 * nears expiry, and answers 200 with the access token, id token, account id
 * and expiry, or 503 with a reason code. Grants run one at a time: a Durable
 * Object's input gate opens during the outbound refresh call, so without this
 * a second request could start a second refresh with the same refresh token.
 * A rotated refresh token is written to storage before anything else is
 * checked. Nothing here logs a token, the seed or its fingerprint.
 */
export function createChatgptStore(storage: DurableObjectStorageLike, deps: Pick<Deps, "fetch" | "now" | "log">)
{
	let queue: Promise<unknown> = Promise.resolve();

	function fail(error: string): StoreResult
	{
		return { status: 503, body: { error } };
	}

	async function save(record: ChatgptCredential): Promise<boolean>
	{
		try {
			await storage.put(CHATGPT_STORE_KEY, { ...record });
			return true;
		} catch {
			deps.log("CHATGPT_POOL store write_failed");
			return false;
		}
	}

	type RefreshOutcome =
		| { kind: "ok"; record: ChatgptCredential }
		| { kind: "rejected" | "failed" | "unavailable" | "invalid" | "expiry_unknown" | "write_failed" };

	async function refresh(record: ChatgptCredential, url: string, clientId: string, now: number): Promise<RefreshOutcome>
	{
		let response: Response;
		try {
			response = await deps.fetch(url, {
				method: "POST",
				headers: { "content-type": "application/json", accept: "application/json", "user-agent": "claude-pool-broker" },
				body: JSON.stringify({ client_id: clientId, grant_type: "refresh_token", refresh_token: record.refresh_token }),
				signal: AbortSignal.timeout(CHATGPT_REFRESH_TIMEOUT_MS),
			});
		} catch {
			return { kind: "unavailable" };
		}
		let body: unknown;
		try {
			body = await response.json();
		} catch {
			body = undefined;
		}
		const fields = body && typeof body === "object" && !Array.isArray(body) ? (body as Record<string, unknown>) : {};
		if (response.status === 401 || (response.status === 400 && fields.error === "invalid_grant")) {
			// RFC 6749 section 5.2: the refresh token is dead. Latch so no later
			// grant calls out with it again; a new seed value clears the latch.
			const latched = await save({ ...record, refresh_rejected: true });
			return latched ? { kind: "rejected" } : { kind: "write_failed" };
		}
		if (response.status >= 400 && response.status < 500) {
			return { kind: "failed" };
		}
		if (response.status !== 200 || body === undefined) {
			return { kind: "unavailable" };
		}
		const rotated = fields.refresh_token;
		const accessToken = fields.access_token;
		const idToken = fields.id_token;
		const refreshToken = typeof rotated === "string" && CHATGPT_REFRESH_TOKEN_RE.test(rotated) ? rotated : record.refresh_token;
		const accessValid = typeof accessToken === "string" && CHATGPT_TOKEN_RE.test(accessToken);
		const expiry = accessValid ? jwtExpiry(accessToken as string) : null;
		const next: ChatgptCredential = accessValid
			? {
				...record,
				refresh_token: refreshToken,
				access_token: accessToken as string,
				id_token: typeof idToken === "string" && CHATGPT_TOKEN_RE.test(idToken) ? idToken : record.id_token,
				expires_at: expiry ?? 0,
				refreshed_at: now,
				refresh_rejected: false,
			}
			: { ...record, refresh_token: refreshToken };
		// Save first: the old refresh token may already be spent, so the rotated
		// one must survive any later check that fails.
		if (!(await save(next))) {
			return { kind: "write_failed" };
		}
		if (!accessValid) {
			return { kind: "invalid" };
		}
		if (expiry === null) {
			return { kind: "expiry_unknown" };
		}
		return { kind: "ok", record: next };
	}

	async function grantOnce(env: Env): Promise<StoreResult>
	{
		const seedRaw = env.CHATGPT_POOL_SEED;
		if (typeof seedRaw !== "string" || seedRaw.trim() === "") {
			return fail("credential_not_seeded");
		}
		let seedValue: unknown;
		try {
			seedValue = JSON.parse(seedRaw);
		} catch {
			return fail("seed_invalid");
		}
		const seed = chatgptTokensFrom(seedValue);
		if (!seed) {
			return fail("seed_invalid");
		}
		const fingerprint = await sha256Hex(seedRaw);
		let record: ChatgptCredential | null;
		try {
			record = storedCredential(await storage.get(CHATGPT_STORE_KEY));
		} catch {
			return fail("store_read_failed");
		}
		if (!record || record.seed_fingerprint !== fingerprint) {
			// A new secret value is the operator's only write path; it also
			// clears a latched refresh rejection.
			record = {
				...seed,
				seed_fingerprint: fingerprint,
				expires_at: jwtExpiry(seed.access_token) ?? 0,
				refreshed_at: 0,
				refresh_rejected: false,
			};
			if (!(await save(record))) {
				return fail("store_write_failed");
			}
			deps.log("CHATGPT_POOL store seeded");
		}
		if (record.refresh_rejected) {
			return fail("credential_refresh_rejected");
		}
		const now = Math.floor(deps.now() / 1000);
		let refreshed = false;
		if (record.expires_at - now < chatgptRefreshBefore(env)) {
			const url = (env.CHATGPT_REFRESH_URL ?? "").trim();
			const clientId = (env.CHATGPT_OAUTH_CLIENT_ID ?? "").trim();
			if (!url.startsWith("https://") || clientId === "") {
				if (record.expires_at - now < CHATGPT_MIN_GRANT_SECONDS) {
					return fail("refresh_not_configured");
				}
			} else {
				const outcome = await refresh(record, url, clientId, now);
				if (outcome.kind === "ok") {
					record = outcome.record;
					refreshed = true;
					deps.log("CHATGPT_POOL store refreshed");
				} else {
					deps.log(`CHATGPT_POOL store refresh_failed reason=${outcome.kind}`);
					switch (outcome.kind) {
						case "rejected":
							return fail("credential_refresh_rejected");
						case "write_failed":
							return fail("store_write_failed");
						case "expiry_unknown":
							return fail("credential_expiry_unknown");
						default:
							// Not a rejection: the current token is still handed
							// out while it has enough life left.
							if (record.expires_at - now < CHATGPT_MIN_GRANT_SECONDS) {
								return fail(
									outcome.kind === "failed"
										? "credential_refresh_failed"
										: outcome.kind === "invalid"
											? "credential_refresh_invalid"
											: "credential_refresh_unavailable",
								);
							}
					}
				}
			}
		}
		if (record.expires_at - now < CHATGPT_MIN_GRANT_SECONDS) {
			return fail("credential_expired");
		}
		return {
			status: 200,
			body: {
				access_token: record.access_token,
				id_token: record.id_token,
				account_id: record.account_id,
				expires_at: record.expires_at,
				refreshed,
			},
		};
	}

	return {
		async grant(env: Env): Promise<Response>
		{
			const run = queue.then(() => grantOnce(env));
			queue = run.catch(() => undefined);
			let result: StoreResult;
			try {
				result = await run;
			} catch {
				deps.log("CHATGPT_POOL store error reason=internal");
				result = fail("store_error");
			}
			return json(result.status, result.body);
		},
	};
}

/**
 * The Durable Object that holds the ChatGPT credential (binding CHATGPT_POOL,
 * migration tag v1 in wrangler.toml). Only reachable through the binding; it
 * answers only POST /grant.
 */
export class ChatgptPoolStore
{
	private readonly env: Env;
	private readonly store: ReturnType<typeof createChatgptStore>;

	constructor(state: { storage: DurableObjectStorageLike }, env: Env)
	{
		this.env = env;
		this.store = createChatgptStore(state.storage, {
			fetch: (input, init) => fetch(input, init),
			now: () => Date.now(),
			log: (line) => console.log(line),
		});
	}

	async fetch(request: Request): Promise<Response>
	{
		const url = new URL(request.url);
		if (url.pathname !== "/grant" || request.method !== "POST") {
			return json(404, { error: "not_found" });
		}
		return this.store.grant(this.env);
	}
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
