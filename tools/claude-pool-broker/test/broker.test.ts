import { beforeAll, describe, expect, it } from "vitest";
import {
	AUDIENCE,
	ISSUER,
	JWKS_URL,
	REGISTRY_TTL_MS,
	REGISTRY_URL,
	createHandler,
	parsePool,
	type Deps,
	type Env,
} from "../src/index";

const NOW_MS = Date.UTC(2026, 9, 4, 12, 0, 0);
const NOW = Math.floor(NOW_MS / 1000);
const WORKFLOW_SHA = "a".repeat(40);
const MAIN_TIP = "b".repeat(40);
const STABLE_TIP = "c".repeat(40);
const TAG_SHA = "d".repeat(40);
const API = "https://api.github.com/repos/shubhodeep1/coding-workflows";
const POOL = JSON.stringify([
	{ name: "BETA", token: "sk-ant-oat01-beta" },
	{ name: "ALPHA", token: "sk-ant-oat01-alpha" },
]);
const ENV: Env = { CLAUDE_POOL_TOKENS: POOL, PROBE_MODEL: "claude-haiku-4-5-20251001", GATE: "0.9" };

let signingKey: CryptoKeyPair;
let otherKey: CryptoKeyPair;
let publicJwk: JsonWebKey;

function b64url(bytes: Uint8Array): string
{
	let binary = "";
	for (const byte of bytes) {
		binary += String.fromCharCode(byte);
	}
	return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function b64urlJson(value: unknown): string
{
	return b64url(new TextEncoder().encode(JSON.stringify(value)));
}

function claims(overrides: Record<string, unknown> = {}): Record<string, unknown>
{
	return {
		iss: ISSUER,
		aud: AUDIENCE,
		iat: NOW - 30,
		nbf: NOW - 30,
		exp: NOW + 300,
		repository: "shubhodeep1/digital_pa",
		repository_owner: "shubhodeep1",
		job_workflow_ref: "shubhodeep1/coding-workflows/.github/workflows/clarify.yml@refs/heads/main",
		job_workflow_sha: WORKFLOW_SHA,
		...overrides,
	};
}

async function sign(payload: Record<string, unknown>, options: { key?: CryptoKeyPair; kid?: string; alg?: string } = {}): Promise<string>
{
	const header = b64urlJson({ alg: options.alg ?? "RS256", kid: options.kid ?? "key-1", typ: "JWT" });
	const body = b64urlJson(payload);
	const signature = await crypto.subtle.sign(
		"RSASSA-PKCS1-v1_5",
		(options.key ?? signingKey).privateKey,
		new TextEncoder().encode(`${header}.${body}`),
	);
	return `${header}.${body}.${b64url(new Uint8Array(signature))}`;
}

interface Stub
{
	deps: Deps;
	calls: string[];
	logs: string[];
	setNow: (ms: number) => void;
	registry: { ok: boolean; body: unknown };
	jwks: { ok: boolean };
	api: { main: { name: string; protected: boolean; commit: { sha: string } }; stable: { name: string; protected: boolean; commit: { sha: string } }; comparison: { status: string; ahead_by: number; behind_by: number; base_commit: { sha: string } }; tagRef: unknown; tag: unknown; unavailable: string[] };
}

function stub(): Stub
{
	let now = NOW_MS;
	const calls: string[] = [];
	const logs: string[] = [];
	const registry = { ok: true, body: ["shubhodeep1/digital_pa", "shubhodeep1/btc_sweeper"] as unknown };
	const jwks = { ok: true };
	const api = {
		main: { name: "main", protected: true, commit: { sha: MAIN_TIP } },
		stable: { name: "stable", protected: true, commit: { sha: STABLE_TIP } },
		comparison: { status: "ahead", ahead_by: 1, behind_by: 0, base_commit: { sha: WORKFLOW_SHA } },
		tagRef: { ref: "refs/tags/stable", object: { type: "tag", sha: TAG_SHA } } as unknown,
		tag: { object: { type: "commit", sha: WORKFLOW_SHA } } as unknown,
		unavailable: [] as string[],
	};
	const deps: Deps = {
		now: () => now,
		log: (line) => logs.push(line),
		fetch: async (input) => {
			calls.push(input);
			if (api.unavailable.includes(input)) {
				return new Response("down", { status: 503 });
			}
			if (input === `${API}/branches/main`) {
				return Response.json(api.main);
			}
			if (input === `${API}/branches/stable`) {
				return Response.json(api.stable);
			}
			if (input.startsWith(`${API}/compare/`)) {
				return Response.json(api.comparison);
			}
			if (input === `${API}/git/ref/tags/stable`) {
				return Response.json(api.tagRef);
			}
			if (input === `${API}/git/tags/${TAG_SHA}`) {
				return Response.json(api.tag);
			}
			if (input === JWKS_URL) {
				if (!jwks.ok) {
					return new Response("down", { status: 503 });
				}
				return Response.json({ keys: [{ ...publicJwk, kid: "key-1", alg: "RS256", use: "sig" }] });
			}
			if (input === REGISTRY_URL) {
				if (!registry.ok) {
					return new Response("down", { status: 500 });
				}
				return Response.json(registry.body);
			}
			return new Response("unexpected", { status: 599 });
		},
	};
	return { deps, calls, logs, setNow: (ms) => (now = ms), registry, jwks, api };
}

function request(token: string | null, init: { method?: string; path?: string } = {}): Request
{
	const headers: Record<string, string> = {};
	if (token !== null) {
		headers.authorization = `Bearer ${token}`;
	}
	return new Request(`https://claude-pool-broker.example.workers.dev${init.path ?? "/v1/pool"}`, {
		method: init.method ?? "POST",
		headers,
	});
}

async function call(s: Stub, req: Request, env: Env = ENV): Promise<{ status: number; body: Record<string, unknown> }>
{
	const response = await createHandlerFor(s)(req, env);
	expect(response.headers.get("cache-control")).toBe("no-store");
	return { status: response.status, body: (await response.json()) as Record<string, unknown> };
}

const handlers = new WeakMap<Stub, ReturnType<typeof createHandler>>();
function createHandlerFor(s: Stub)
{
	let handler = handlers.get(s);
	if (!handler) {
		handler = createHandler(s.deps);
		handlers.set(s, handler);
	}
	return handler;
}

beforeAll(async () => {
	const params = { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" };
	signingKey = (await crypto.subtle.generateKey(params, true, ["sign", "verify"])) as CryptoKeyPair;
	otherKey = (await crypto.subtle.generateKey(params, true, ["sign", "verify"])) as CryptoKeyPair;
	publicJwk = await crypto.subtle.exportKey("jwk", signingKey.publicKey);
});

describe("granted", () => {
	it("returns the pool to an allowlisted consumer job of a coding-workflows reusable workflow", async () => {
		const s = stub();
		const { status, body } = await call(s, request(await sign(claims())));
		expect(status).toBe(200);
		expect(body).toEqual({
			accounts: [
				{ name: "ALPHA", token: "sk-ant-oat01-alpha" },
				{ name: "BETA", token: "sk-ant-oat01-beta" },
			],
			probe_model: "claude-haiku-4-5-20251001",
			gate: 0.9,
		});
		expect(s.logs.join("\n")).not.toContain("sk-ant");
		expect(s.logs).toContain("CLAUDE_POOL broker granted repository=shubhodeep1/digital_pa accounts=2");
		expect(s.calls).toContain(`${API}/compare/${WORKFLOW_SHA}...${MAIN_TIP}`);
	});

	it("accepts coding-workflows itself without the registry entry", async () => {
		const s = stub();
		s.registry.ok = false;
		const token = await sign(claims({ repository: "shubhodeep1/coding-workflows" }));
		expect((await call(s, request(token))).status).toBe(200);
		expect(s.calls).not.toContain(REGISTRY_URL);
	});

	it("accepts an audience list that contains the audience", async () => {
		const s = stub();
		expect((await call(s, request(await sign(claims({ aud: ["other", AUDIENCE] }))))).status).toBe(200);
	});

	it("accepts the owner's and reusable workflow's canonical spelling regardless of case", async () => {
		const s = stub();
		const token = await sign(claims({
			repository_owner: "ShubhoDeep1",
			repository: "ShubhoDeep1/Digital_PA",
			job_workflow_ref: "ShubhoDeep1/Coding-Workflows/.github/workflows/clarify.yml@refs/heads/main",
		}));
		expect((await call(s, request(token))).status).toBe(200);
	});

	it("accepts a pinned commit only if it belongs to a protected branch", async () => {
		const s = stub();
		const token = await sign(claims({ job_workflow_ref: `shubhodeep1/coding-workflows/.github/workflows/implement.yml@${WORKFLOW_SHA}` }));
		expect((await call(s, request(token))).status).toBe(200);
		expect(s.calls).toContain(`${API}/branches/main`);
	});

	it("accepts the exact annotated stable release tag when it resolves to the signed SHA", async () => {
		const s = stub();
		const token = await sign(claims({ job_workflow_ref: "shubhodeep1/coding-workflows/.github/workflows/review_autofix.yml@refs/tags/stable" }));
		expect((await call(s, request(token))).status).toBe(200);
		expect(s.calls).toContain(`${API}/git/tags/${TAG_SHA}`);
	});

	it("accepts the stable branch only while it is protected", async () => {
		const s = stub();
		const token = await sign(claims({ job_workflow_ref: "shubhodeep1/coding-workflows/.github/workflows/plan.yml@refs/heads/stable" }));
		expect((await call(s, request(token))).status).toBe(200);
		expect(s.calls).toContain(`${API}/compare/${WORKFLOW_SHA}...${STABLE_TIP}`);
	});

	it("accepts an approved SHA that is already the protected branch tip", async () => {
		const s = stub();
		s.api.main.commit.sha = WORKFLOW_SHA;
		s.api.comparison = { status: "identical", ahead_by: 0, behind_by: 0, base_commit: { sha: WORKFLOW_SHA } };
		expect((await call(s, request(await sign(claims())))).status).toBe(200);
	});
});

describe("refused (403)", () => {
	const cases: [string, Record<string, unknown>, string][] = [
		["wrong owner", { repository_owner: "someone-else", repository: "someone-else/digital_pa" }, "wrong_owner"],
		["owner claim spoofed against the repository", { repository: "evil/digital_pa" }, "wrong_owner"],
		["repository not in the registry", { repository: "shubhodeep1/not-a-consumer" }, "repo_not_allowed"],
		["a consumer's own workflow file", { job_workflow_ref: "shubhodeep1/digital_pa/.github/workflows/ai-clarify.yml@refs/heads/main" }, "wrong_workflow"],
		["a lookalike workflow prefix", { job_workflow_ref: "shubhodeep1/coding-workflows-fork/.github/workflows/x.yml@refs/heads/main" }, "wrong_workflow"],
		["an unapproved workflow on main", { job_workflow_ref: "shubhodeep1/coding-workflows/.github/workflows/claude-engine-smoke.yml@refs/heads/main" }, "wrong_workflow"],
		["a prefix lookalike workflow", { job_workflow_ref: "shubhodeep1/coding-workflows/.github/workflows/clarify.yml.evil@refs/heads/main" }, "wrong_workflow"],
		["an unapproved branch", { job_workflow_ref: "shubhodeep1/coding-workflows/.github/workflows/clarify.yml@refs/heads/feature" }, "wrong_workflow"],
		["an unqualified stable ref", { job_workflow_ref: "shubhodeep1/coding-workflows/.github/workflows/clarify.yml@stable" }, "wrong_workflow"],
		["missing workflow SHA", { job_workflow_sha: undefined }, "bad_workflow_sha"],
		["malformed workflow SHA", { job_workflow_sha: "bad" }, "bad_workflow_sha"],
		["mismatched pin", { job_workflow_ref: `shubhodeep1/coding-workflows/.github/workflows/clarify.yml@${"f".repeat(40)}` }, "workflow_sha_mismatch"],
		["no job_workflow_ref", { job_workflow_ref: undefined }, "wrong_workflow"],
		["expired", { exp: NOW - 120 }, "expired"],
		["issued too long ago", { iat: NOW - 700, nbf: NOW - 700 }, "too_old"],
		["not yet valid", { nbf: NOW + 600 }, "not_yet_valid"],
		["issued in the future", { iat: NOW + 600 }, "not_yet_valid"],
		["wrong issuer", { iss: "https://evil.example" }, "bad_issuer"],
		["wrong audience", { aud: "sts.amazonaws.com" }, "bad_audience"],
	];
	for (const [name, overrides, reason] of cases) {
		it(name, async () => {
			const s = stub();
			const { status, body } = await call(s, request(await sign(claims(overrides))));
			expect(status).toBe(403);
			expect(body).toEqual({ error: reason });
			if (reason === "wrong_workflow" || reason === "bad_workflow_sha" || reason === "workflow_sha_mismatch") {
				expect(s.calls.some((url) => url.startsWith(API))).toBe(false);
			}
		});
	}

	it("rejects a signed commit not on either protected branch", async () => {
		const s = stub();
		s.api.comparison = { status: "diverged", ahead_by: 1, behind_by: 1, base_commit: { sha: WORKFLOW_SHA } };
		const { status, body } = await call(s, request(await sign(claims({ job_workflow_ref: `shubhodeep1/coding-workflows/.github/workflows/clarify.yml@${WORKFLOW_SHA}` }))));
		expect([status, body.error]).toEqual([403, "unapproved_workflow_sha"]);
		expect(body).not.toHaveProperty("accounts");
	});

	it("rejects an unprotected branch even when its tip matches the signed commit", async () => {
		const s = stub();
		s.api.main.protected = false;
		const { status, body } = await call(s, request(await sign(claims())));
		expect([status, body.error]).toEqual([403, "unapproved_workflow_sha"]);
		expect(s.calls.some((url) => url.includes("/compare/"))).toBe(false);
	});

	it("rejects a commit ahead of the protected branch tip", async () => {
		const s = stub();
		s.api.comparison = { status: "behind", ahead_by: 0, behind_by: 1, base_commit: { sha: WORKFLOW_SHA } };
		const token = await sign(claims({ job_workflow_ref: `shubhodeep1/coding-workflows/.github/workflows/clarify.yml@${WORKFLOW_SHA}` }));
		const { status, body } = await call(s, request(token));
		expect([status, body.error]).toEqual([403, "unapproved_workflow_sha"]);
	});

	it("rejects a release tag moved to a different commit", async () => {
		const s = stub();
		s.api.tag = { object: { type: "commit", sha: STABLE_TIP } };
		const { status, body } = await call(s, request(await sign(claims({ job_workflow_ref: "shubhodeep1/coding-workflows/.github/workflows/clarify.yml@refs/tags/stable" }))));
		expect([status, body.error]).toEqual([403, "workflow_sha_mismatch"]);
		expect(s.calls.some((url) => url.includes("/branches/"))).toBe(false);
	});

	it("a bad signature", async () => {
		const s = stub();
		const { status, body } = await call(s, request(await sign(claims(), { key: otherKey })));
		expect([status, body.error]).toEqual([403, "bad_signature"]);
	});

	it("a tampered payload", async () => {
		const s = stub();
		const token = await sign(claims({ repository: "shubhodeep1/not-a-consumer" }));
		const [header, , signature] = token.split(".");
		const forged = `${header}.${b64urlJson(claims())}.${signature}`;
		expect((await call(s, request(forged))).body.error).toBe("bad_signature");
	});

	it("an unknown key id, after one JWKS refresh", async () => {
		const s = stub();
		expect((await call(s, request(await sign(claims(), { kid: "nope" })))).body.error).toBe("unknown_key");
		expect(s.calls.filter((url) => url === JWKS_URL).length).toBe(1);
	});

	it("alg none and HS256", async () => {
		const s = stub();
		const none = `${b64urlJson({ alg: "none", kid: "key-1" })}.${b64urlJson(claims())}.`;
		expect((await call(s, request(none))).body.error).toBe("bad_algorithm");
		expect((await call(s, request(await sign(claims(), { alg: "HS256" })))).body.error).toBe("bad_algorithm");
	});

	it("missing or malformed bearer", async () => {
		const s = stub();
		expect((await call(s, request(null))).body.error).toBe("missing_token");
		expect((await call(s, request("a.b"))).body.error).toBe("malformed_token");
		expect((await call(s, request("!!!.@@@.###"))).body.error).toBe("missing_token");
		expect((await call(s, request("eyJ.eyJ.sig"))).body.error).toBe("malformed_token");
	});

	it("accepts a case-insensitive bearer scheme", async () => {
		const s = stub();
		const token = await sign(claims());
		const req = request(null);
		req.headers.set("authorization", `bearer  ${token}`);
		expect((await call(s, req)).status).toBe(200);
	});
});

describe("broker-side failures", () => {
	it("fails closed on missing or malformed GitHub authorization evidence", async () => {
		for (const variant of ["outage", "malformed_branch", "malformed_comparison", "malformed_tag"]) {
			const s = stub();
			if (variant === "outage") {
				s.api.unavailable.push(`${API}/branches/main`, `${API}/branches/stable`);
			} else if (variant === "malformed_branch") {
				s.api.main.commit.sha = "not-a-sha";
				s.api.stable.commit.sha = "not-a-sha";
			} else if (variant === "malformed_comparison") {
				s.api.comparison.base_commit.sha = "not-the-requested-sha";
			} else {
				s.api.tagRef = { ref: "refs/tags/other", object: { type: "commit", sha: WORKFLOW_SHA } };
			}
			const ref = variant === "malformed_tag" ? "refs/tags/stable" : WORKFLOW_SHA;
			const token = await sign(claims({ job_workflow_ref: `shubhodeep1/coding-workflows/.github/workflows/clarify.yml@${ref}` }));
			const { status, body } = await call(s, request(token));
			expect([status, body.error]).toEqual([503, "workflow_evidence_unavailable"]);
			expect(body).not.toHaveProperty("accounts");
			expect(s.logs.join("\n")).not.toContain("sk-ant");
			expect(s.calls.every((url) => url === JWKS_URL || url === REGISTRY_URL || url.startsWith(`${API}/`))).toBe(true);
		}
	});

	it("a protected release branch can approve a pin when main has diverged", async () => {
		const s = stub();
		s.api.unavailable.push(`${API}/branches/main`);
		const token = await sign(claims({ job_workflow_ref: `shubhodeep1/coding-workflows/.github/workflows/clarify.yml@${WORKFLOW_SHA}` }));
		expect((await call(s, request(token))).status).toBe(200);
		expect(s.calls).toContain(`${API}/branches/stable`);
	});

	it("does not reuse formerly valid branch evidence after an outage", async () => {
		const s = stub();
		const token = await sign(claims());
		expect((await call(s, request(token))).status).toBe(200);
		s.api.unavailable.push(`${API}/branches/main`);
		const { status, body } = await call(s, request(token));
		expect([status, body.error]).toEqual([503, "workflow_evidence_unavailable"]);
		expect(body).not.toHaveProperty("accounts");
	});
	it("JWKS unreachable on a cold start is a 503", async () => {
		const s = stub();
		s.jwks.ok = false;
		const { status, body } = await call(s, request(await sign(claims())));
		expect([status, body.error]).toEqual([503, "jwks_unavailable"]);
	});

	it("registry fetch failure without a cached copy is a 503", async () => {
		const s = stub();
		s.registry.ok = false;
		const { status, body } = await call(s, request(await sign(claims())));
		expect([status, body.error]).toEqual([503, "registry_unavailable"]);
	});

	it("registry fetch failure with a cached copy keeps the last good list", async () => {
		const s = stub();
		expect((await call(s, request(await sign(claims())))).status).toBe(200);
		s.registry.ok = false;
		s.setNow(NOW_MS + REGISTRY_TTL_MS + 1000);
		const later = Math.floor((NOW_MS + REGISTRY_TTL_MS + 1000) / 1000);
		const token = await sign(claims({ iat: later - 10, nbf: later - 10, exp: later + 300 }));
		expect((await call(s, request(token))).status).toBe(200);
		expect(s.logs).toContain("CLAUDE_POOL broker registry_fetch_failed using=last_good");
	});

	it("the registry is cached for ten minutes", async () => {
		const s = stub();
		await call(s, request(await sign(claims())));
		await call(s, request(await sign(claims())));
		expect(s.calls.filter((url) => url === REGISTRY_URL).length).toBe(1);
	});

	it("a registry change is picked up after the cache expires", async () => {
		const s = stub();
		await call(s, request(await sign(claims())));
		s.registry.body = ["shubhodeep1/btc_sweeper"];
		s.setNow(NOW_MS + REGISTRY_TTL_MS + 1000);
		const later = Math.floor((NOW_MS + REGISTRY_TTL_MS + 1000) / 1000);
		const token = await sign(claims({ iat: later - 10, nbf: later - 10, exp: later + 300 }));
		expect((await call(s, request(token))).body.error).toBe("repo_not_allowed");
	});

	it("a missing or empty pool secret is a 503", async () => {
		const s = stub();
		expect((await call(s, request(await sign(claims())), {})).body.error).toBe("pool_empty");
		expect((await call(s, request(await sign(claims())), { CLAUDE_POOL_TOKENS: "[]" })).status).toBe(503);
	});

	it("an invalid gate or probe model falls back to the defaults", async () => {
		const s = stub();
		const { body } = await call(s, request(await sign(claims())), { CLAUDE_POOL_TOKENS: POOL, GATE: "5", PROBE_MODEL: "Bad Model" });
		expect([body.gate, body.probe_model]).toEqual([0.9, "claude-haiku-4-5-20251001"]);
	});
});

describe("routing", () => {
	it("only POST /v1/pool", async () => {
		const s = stub();
		expect((await call(s, request(null, { path: "/" }))).status).toBe(404);
		expect((await call(s, request(null, { method: "GET" }))).status).toBe(405);
		expect(s.calls).toEqual([]);
	});
});

describe("parsePool", () => {
	it("drops invalid and duplicate entries and sorts by name", () => {
		const raw = JSON.stringify([
			{ name: "B", token: "t2" },
			{ name: "A", token: "t1" },
			{ name: "A", token: "dup" },
			{ name: "lower", token: "x" },
			{ name: "C", token: "has space" },
			{ name: "D", token: "" },
			{ name: "E" },
			"junk",
		]);
		expect(parsePool(raw)).toEqual([
			{ name: "A", token: "t1" },
			{ name: "B", token: "t2" },
		]);
		expect(parsePool("{")).toEqual([]);
		expect(parsePool('{"name":"A"}')).toEqual([]);
		expect(parsePool(undefined)).toEqual([]);
	});
});
