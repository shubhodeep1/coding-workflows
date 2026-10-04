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
 *     own workflow file.
 *
 * Success: 200 `{"accounts":[{"name","token"}],"probe_model","gate"}`.
 * Refusal: 403 `{"error": <reason code>}`. Broker-side trouble (JWKS or
 * registry unreachable, pool secret missing): 503, which clients treat as
 * "Claude unavailable" and run codex (plan D1).
 *
 * The pool is the CLAUDE_POOL_TOKENS secret (a JSON array of {name, token}),
 * written only by claude-workers' key-sync workflow. Nothing here logs a
 * token or the OIDC JWT; log lines carry reason codes and the repository.
 */

export interface Env {
	CLAUDE_POOL_TOKENS?: string;
	PROBE_MODEL?: string;
	GATE?: string;
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

export function createHandler(deps: Deps)
{
	let jwks: { keys: Jwk[]; fetchedAt: number } | null = null;
	let registry: { repos: Set<string>; fetchedAt: number } | null = null;

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

	async function verify(jwt: string): Promise<Record<string, unknown>>
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
		if (!(audience === AUDIENCE || (Array.isArray(audience) && audience.includes(AUDIENCE)))) {
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

	async function authorize(claims: Record<string, unknown>): Promise<string>
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
		const workflowRef = claims.job_workflow_ref;
		if (typeof workflowRef !== "string" || !workflowRef.toLowerCase().startsWith(WORKFLOW_PREFIX)) {
			throw new Refusal(403, "wrong_workflow");
		}
		return repository;
	}

	return async function handle(request: Request, env: Env): Promise<Response>
	{
		const url = new URL(request.url);
		if (url.pathname !== "/v1/pool") {
			return json(404, { error: "not_found" });
		}
		if (request.method !== "POST") {
			return json(405, { error: "method_not_allowed" });
		}
		let repository = "";
		try {
			const authorization = request.headers.get("authorization") || "";
			const match = /^Bearer[ \t]+([A-Za-z0-9_.-]+)$/i.exec(authorization);
			if (!match) {
				throw new Refusal(403, "missing_token");
			}
			repository = await authorize(await verify(match[1]));
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
