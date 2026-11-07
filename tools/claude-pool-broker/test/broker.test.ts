import { beforeAll, describe, expect, it } from "vitest";
import {
	AUDIENCE,
	CHATGPT_ALLOWED_WORKFLOW_FILES,
	CHATGPT_AUDIENCE,
	CHATGPT_STORE_KEY,
	ChatgptPoolStore,
	GITHUB_API,
	ISSUER,
	JWKS_URL,
	REGISTRY_TTL_MS,
	REGISTRY_URL,
	VERIFIED_SHA_TTL_MS,
	createChatgptStore,
	createHandler,
	parsePool,
	parseWorkflowRef,
	type Deps,
	type Env,
} from "../src/index";

const NOW_MS = Date.UTC(2026, 9, 4, 12, 0, 0);
const NOW = Math.floor(NOW_MS / 1000);
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
	/** Keyed by "<branch>...<sha>"; a missing entry answers 404. */
	compare: Record<string, CompareAnswer>;
	headers: Record<string, Record<string, string>>;
}

type CompareAnswer = { status: number; body?: unknown; raw?: string } | "throw";

function stub(): Stub
{
	let now = NOW_MS;
	const calls: string[] = [];
	const logs: string[] = [];
	const registry = { ok: true, body: ["shubhodeep1/digital_pa", "shubhodeep1/btc_sweeper"] as unknown };
	const jwks = { ok: true };
	const compare: Record<string, CompareAnswer> = {};
	const headers: Record<string, Record<string, string>> = {};
	const deps: Deps = {
		now: () => now,
		log: (line) => logs.push(line),
		fetch: async (input, init) => {
			calls.push(input);
			if (input.startsWith(`${GITHUB_API}/compare/`)) {
				headers[input] = { ...((init?.headers as Record<string, string>) ?? {}) };
				const key = input.slice(`${GITHUB_API}/compare/`.length).replace(/\?per_page=1$/, "");
				const answer = compare[key] ?? { status: 404, body: { message: "Not Found" } };
				if (answer === "throw") {
					throw new Error("network down");
				}
				if (answer.raw !== undefined) {
					return new Response(answer.raw, { status: answer.status });
				}
				return Response.json(answer.body ?? {}, { status: answer.status });
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
	return { deps, calls, logs, setNow: (ms) => (now = ms), registry, jwks, compare, headers };
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
	});

	it("accepts coding-workflows itself without the registry entry", async () => {
		const s = stub();
		s.registry.ok = false;
		const token = await sign(claims({ repository: "shubhodeep1/coding-workflows", job_workflow_ref: "shubhodeep1/coding-workflows/.github/workflows/claude-engine-smoke.yml@refs/heads/main" }));
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
});

describe("refused (403)", () => {
	const cases: [string, Record<string, unknown>, string][] = [
		["wrong owner", { repository_owner: "someone-else", repository: "someone-else/digital_pa" }, "wrong_owner"],
		["owner claim spoofed against the repository", { repository: "evil/digital_pa" }, "wrong_owner"],
		["repository not in the registry", { repository: "shubhodeep1/not-a-consumer" }, "repo_not_allowed"],
		["a consumer's own workflow file", { job_workflow_ref: "shubhodeep1/digital_pa/.github/workflows/ai-clarify.yml@refs/heads/main" }, "wrong_workflow"],
		["a lookalike workflow prefix", { job_workflow_ref: "shubhodeep1/coding-workflows-fork/.github/workflows/x.yml@refs/heads/main" }, "wrong_workflow"],
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
		});
	}

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

const SHA = "0123456789abcdef0123456789abcdef01234567";
const WF = "shubhodeep1/coding-workflows/.github/workflows/";

function pinned(sha: string = SHA, extra: Record<string, unknown> = {}): Record<string, unknown>
{
	return claims({ job_workflow_ref: `${WF}clarify.yml@${sha}`, job_workflow_sha: sha, ...extra });
}

function compareCalls(s: Stub): string[]
{
	return s.calls.filter((url) => url.startsWith(`${GITHUB_API}/compare/`));
}

describe("workflow identity", () => {
	it("refs/heads/main is granted without a compare call", async () => {
		const s = stub();
		expect((await call(s, request(await sign(claims())))).status).toBe(200);
		expect(compareCalls(s)).toEqual([]);
	});

	for (const status of ["behind", "identical"]) {
		it(`a SHA ${status} main is granted`, async () => {
			const s = stub();
			s.compare[`main...${SHA}`] = { status: 200, body: { status } };
			expect((await call(s, request(await sign(pinned())))).status).toBe(200);
			expect(compareCalls(s)).toEqual([`${GITHUB_API}/compare/main...${SHA}?per_page=1`]);
			const sent = s.headers[compareCalls(s)[0]];
			expect(sent["user-agent"]).toBe("claude-pool-broker");
			expect(sent.authorization).toBeUndefined();
		});
	}

	it("a SHA diverged from main but behind stable is granted", async () => {
		const s = stub();
		s.compare[`main...${SHA}`] = { status: 200, body: { status: "diverged" } };
		s.compare[`stable...${SHA}`] = { status: 200, body: { status: "behind" } };
		expect((await call(s, request(await sign(pinned())))).status).toBe(200);
		expect(compareCalls(s).length).toBe(2);
	});

	it("a trusted SHA is cached and re-verified after the TTL", async () => {
		const s = stub();
		s.compare[`main...${SHA}`] = { status: 200, body: { status: "behind" } };
		await call(s, request(await sign(pinned())));
		expect((await call(s, request(await sign(pinned())))).status).toBe(200);
		expect(compareCalls(s).length).toBe(1);
		expect(s.logs.some((line) => line.includes("result=trusted source=cache"))).toBe(true);
		const laterMs = NOW_MS + VERIFIED_SHA_TTL_MS + 1000;
		s.setNow(laterMs);
		const later = Math.floor(laterMs / 1000);
		// Re-fetch the registry and JWKS at the later time too; both are stubbed.
		await call(s, request(await sign(pinned(SHA, { iat: later - 10, nbf: later - 10, exp: later + 300 }))));
		expect(compareCalls(s).length).toBe(2);
	});

	it("sends the optional read token but never logs it", async () => {
		const s = stub();
		s.compare[`main...${SHA}`] = { status: 200, body: { status: "behind" } };
		const env = { ...ENV, BROKER_GITHUB_READ_TOKEN: "ghp_read_only_secret" };
		expect((await call(s, request(await sign(pinned())), env)).status).toBe(200);
		expect(s.headers[compareCalls(s)[0]].authorization).toBe("Bearer ghp_read_only_secret");
		expect(s.logs.join("\n")).not.toContain("ghp_read_only_secret");
	});

	const refused: [string, string, string][] = [
		["a workflow file outside the allowlist", `${WF}evil.yml@refs/heads/main`, "workflow_not_allowed"],
		["an allowlisted name in a different case", `${WF}Clarify.yml@refs/heads/main`, "workflow_not_allowed"],
		["a nested workflow path", `${WF}sub/clarify.yml@refs/heads/main`, "wrong_workflow"],
		["a double @", `${WF}clarify.yml@refs/heads/main@x`, "wrong_workflow"],
		["no ref", `${WF}clarify.yml`, "wrong_workflow"],
		["refs/heads/stable as a branch", `${WF}clarify.yml@refs/heads/stable`, "workflow_ref_not_allowed"],
		["a feature branch", `${WF}clarify.yml@refs/heads/feature`, "workflow_ref_not_allowed"],
		["a tag ref", `${WF}clarify.yml@refs/tags/v1.2.3`, "workflow_ref_not_allowed"],
		["a pull request ref", `${WF}clarify.yml@refs/pull/1/merge`, "workflow_ref_not_allowed"],
		["a short SHA", `${WF}clarify.yml@0123456`, "workflow_ref_not_allowed"],
		["an uppercase SHA", `${WF}clarify.yml@${SHA.toUpperCase()}`, "workflow_ref_not_allowed"],
		["an empty ref", `${WF}clarify.yml@`, "workflow_ref_not_allowed"],
	];
	for (const [name, ref, reason] of refused) {
		it(`refuses ${name}`, async () => {
			const s = stub();
			const { status, body } = await call(s, request(await sign(claims({ job_workflow_ref: ref, job_workflow_sha: SHA }))));
			expect([status, body.error]).toEqual([403, reason]);
			expect(compareCalls(s)).toEqual([]);
		});
	}

	it("refuses a SHA ref without a matching signed job_workflow_sha", async () => {
		const s = stub();
		s.compare[`main...${SHA}`] = { status: 200, body: { status: "behind" } };
		expect((await call(s, request(await sign(pinned(SHA, { job_workflow_sha: undefined }))))).body.error).toBe("workflow_sha_mismatch");
		expect((await call(s, request(await sign(pinned(SHA, { job_workflow_sha: "f".repeat(40) }))))).body.error).toBe("workflow_sha_mismatch");
		expect(compareCalls(s)).toEqual([]);
	});

	const untrusted: [string, CompareAnswer, CompareAnswer][] = [
		["diverged from both branches", { status: 200, body: { status: "diverged" } }, { status: 200, body: { status: "diverged" } }],
		["ahead of both branches", { status: 200, body: { status: "ahead" } }, { status: 200, body: { status: "ahead" } }],
		["unknown to both branches", { status: 404 }, { status: 422 }],
	];
	for (const [name, main, stable] of untrusted) {
		it(`refuses a SHA ${name}`, async () => {
			const s = stub();
			s.compare[`main...${SHA}`] = main;
			s.compare[`stable...${SHA}`] = stable;
			const { status, body } = await call(s, request(await sign(pinned())));
			expect([status, body.error]).toEqual([403, "workflow_sha_unverified"]);
		});
	}

	const unavailable: [string, CompareAnswer][] = [
		["a 403 rate limit", { status: 403, body: { message: "API rate limit exceeded" } }],
		["a 429", { status: 429, body: {} }],
		["a 500", { status: 500, body: {} }],
		["a thrown fetch", "throw"],
		["invalid JSON", { status: 200, raw: "not json" }],
		["an unknown status", { status: 200, body: { status: "weird" } }],
	];
	for (const [name, answer] of unavailable) {
		it(`fails closed on ${name}`, async () => {
			const s = stub();
			s.compare[`main...${SHA}`] = answer;
			s.compare[`stable...${SHA}`] = { status: 200, body: { status: "behind" } };
			const { status, body } = await call(s, request(await sign(pinned())));
			expect([status, body.error]).toEqual([403, "workflow_sha_unverifiable"]);
			expect(compareCalls(s).length).toBe(1);
		});
	}

	it("does not cache an unverifiable result", async () => {
		const s = stub();
		s.compare[`main...${SHA}`] = { status: 503, body: {} };
		expect((await call(s, request(await sign(pinned())))).body.error).toBe("workflow_sha_unverifiable");
		s.compare[`main...${SHA}`] = { status: 200, body: { status: "behind" } };
		expect((await call(s, request(await sign(pinned())))).status).toBe(200);
	});
});

describe("promote-candidate branch", () => {
	const SELF = "shubhodeep1/coding-workflows";
	function candidate(extra: Record<string, unknown> = {}): Record<string, unknown>
	{
		return claims({ repository: SELF, job_workflow_ref: `${WF}clarify.yml@refs/heads/promote-candidate`, job_workflow_sha: SHA, ...extra });
	}

	for (const status of ["behind", "identical"]) {
		it(`is granted to the self repo when the signed SHA is ${status} main`, async () => {
			const s = stub();
			s.compare[`main...${SHA}`] = { status: 200, body: { status } };
			expect((await call(s, request(await sign(candidate())))).status).toBe(200);
			expect(compareCalls(s)).toEqual([`${GITHUB_API}/compare/main...${SHA}?per_page=1`]);
		});
	}

	it("is refused when the signed SHA is diverged from main and stable", async () => {
		const s = stub();
		s.compare[`main...${SHA}`] = { status: 200, body: { status: "diverged" } };
		s.compare[`stable...${SHA}`] = { status: 200, body: { status: "diverged" } };
		const { status, body } = await call(s, request(await sign(candidate())));
		expect([status, body.error]).toEqual([403, "workflow_sha_unverified"]);
	});

	it("is refused when the compare call is unverifiable", async () => {
		const s = stub();
		s.compare[`main...${SHA}`] = { status: 503, body: {} };
		const { status, body } = await call(s, request(await sign(candidate())));
		expect([status, body.error]).toEqual([403, "workflow_sha_unverifiable"]);
	});

	for (const [name, sha] of [["missing", undefined], ["malformed", "abc123"], ["uppercase", SHA.toUpperCase()]] as const) {
		it(`is refused when the SHA claim is ${name}`, async () => {
			const s = stub();
			s.compare[`main...${SHA}`] = { status: 200, body: { status: "behind" } };
			const { status, body } = await call(s, request(await sign(candidate({ job_workflow_sha: sha }))));
			expect([status, body.error]).toEqual([403, "workflow_sha_mismatch"]);
			expect(compareCalls(s)).toEqual([]);
		});
	}

	it("is refused for a consumer repository", async () => {
		const s = stub();
		s.compare[`main...${SHA}`] = { status: 200, body: { status: "behind" } };
		const { status, body } = await call(s, request(await sign(candidate({ repository: "shubhodeep1/digital_pa" }))));
		expect([status, body.error]).toEqual([403, "workflow_ref_not_allowed"]);
		expect(compareCalls(s)).toEqual([]);
	});
});

describe("parseWorkflowRef", () => {
	it("splits the file and ref", () => {
		expect(parseWorkflowRef(`${WF}plan.yml@refs/heads/main`)).toEqual({ file: "plan.yml", ref: "refs/heads/main" });
		expect(parseWorkflowRef("shubhodeep1/coding-workflows-fork/.github/workflows/plan.yml@refs/heads/main")).toBeNull();
		expect(parseWorkflowRef(undefined)).toBeNull();
		expect(parseWorkflowRef(`${WF}@refs/heads/main`)).toBeNull();
	});
});

const REFRESH_URL = "https://auth.example.test/oauth/token";
const CHATGPT_WF = `${WF}review_autofix.yml@refs/heads/main`;

function chatgptJwt(payload: Record<string, unknown>): string
{
	return `${b64urlJson({ alg: "RS256", typ: "JWT" })}.${b64urlJson(payload)}.${"s".repeat(24)}`;
}

function chatgptSeed(overrides: Record<string, unknown> = {}, expOffset = 3600): string
{
	return JSON.stringify({
		auth_mode: "chatgpt",
		tokens: {
			access_token: chatgptJwt({ exp: NOW + expOffset, kind: "access" }),
			id_token: chatgptJwt({ exp: NOW + expOffset, kind: "id" }),
			refresh_token: "rt_seed_refresh_token_value",
			account_id: "acct-1234",
			...overrides,
		},
	});
}

class MapStorage
{
	values = new Map<string, unknown>();
	puts = 0;
	failPut = false;
	async get(key: string): Promise<unknown>
	{
		const value = this.values.get(key);
		return value === undefined ? undefined : structuredClone(value);
	}
	async put(key: string, value: unknown): Promise<void>
	{
		if (this.failPut) {
			throw new Error("storage down");
		}
		this.puts += 1;
		this.values.set(key, structuredClone(value));
	}
}

interface ChatgptHarness
{
	s: Stub;
	env: Env;
	storage: MapStorage;
	refreshBodies: string[];
	refreshAnswers: CompareAnswer[];
	objectNames: unknown[];
	handler: ReturnType<typeof createHandler>;
}

function chatgptHarness(options: { allowed?: readonly string[]; binding?: boolean; stubThrows?: boolean } = {}): ChatgptHarness
{
	const s = stub();
	const storage = new MapStorage();
	const refreshBodies: string[] = [];
	const refreshAnswers: CompareAnswer[] = [];
	const objectNames: unknown[] = [];
	const store = createChatgptStore(storage, {
		now: s.deps.now,
		log: s.deps.log,
		fetch: async (input, init) => {
			if (input !== REFRESH_URL) {
				return s.deps.fetch(input, init);
			}
			refreshBodies.push(String(init?.body));
			const answer = refreshAnswers.shift() ?? { status: 500, body: {} };
			if (answer === "throw") {
				throw new Error("network down");
			}
			if (answer.raw !== undefined) {
				return new Response(answer.raw, { status: answer.status });
			}
			return Response.json(answer.body ?? {}, { status: answer.status });
		},
	});
	const h: ChatgptHarness = {
		s,
		env: { ...ENV, CHATGPT_POOL_SEED: chatgptSeed() },
		storage,
		refreshBodies,
		refreshAnswers,
		objectNames,
		handler: createHandler({ ...s.deps, chatgptAllowedWorkflowFiles: options.allowed ?? ["review_autofix.yml"] }),
	};
	if (options.binding !== false) {
		h.env.CHATGPT_POOL = {
			idFromName: (name: string) => {
				objectNames.push(name);
				return `id:${name}`;
			},
			get: () => ({
				fetch: async (input: string, init?: RequestInit) => {
					if (options.stubThrows) {
						throw new Error("object down");
					}
					expect(input).toBe("https://chatgpt-pool.internal/grant");
					expect(init?.method).toBe("POST");
					return store.grant(h.env);
				},
			}),
		};
	}
	return h;
}

function chatgptClaims(overrides: Record<string, unknown> = {}): Record<string, unknown>
{
	return claims({ aud: CHATGPT_AUDIENCE, job_workflow_ref: CHATGPT_WF, ...overrides });
}

async function callChatgpt(h: ChatgptHarness, token: string | null): Promise<{ status: number; body: Record<string, unknown> }>
{
	const response = await h.handler(request(token, { path: "/v1/chatgpt" }), h.env);
	expect(response.headers.get("cache-control")).toBe("no-store");
	return { status: response.status, body: (await response.json()) as Record<string, unknown> };
}

function withRefresh(h: ChatgptHarness, expOffset: number): void
{
	h.env.CHATGPT_POOL_SEED = chatgptSeed({}, expOffset);
	h.env.CHATGPT_REFRESH_URL = REFRESH_URL;
	h.env.CHATGPT_OAUTH_CLIENT_ID = "client-test-id";
}

function expectNoChatgptSecrets(h: ChatgptHarness, extra: string[] = []): void
{
	const text = h.s.logs.join("\n");
	const seed = JSON.parse(h.env.CHATGPT_POOL_SEED ?? "{}") as { tokens?: Record<string, string> };
	for (const secret of [...Object.values(seed.tokens ?? {}).filter((v) => v !== "acct-1234"), ...extra]) {
		expect(text).not.toContain(secret);
	}
	expect(text).not.toContain("eyJ");
	expect(text).not.toContain("rt_");
}

describe("chatgpt: granted", () => {
	it("returns exactly the four credential fields and never the refresh token", async () => {
		const h = chatgptHarness();
		const { status, body } = await callChatgpt(h, await sign(chatgptClaims()));
		expect(status).toBe(200);
		const seed = JSON.parse(h.env.CHATGPT_POOL_SEED ?? "").tokens;
		expect(body).toEqual({ access_token: seed.access_token, id_token: seed.id_token, account_id: "acct-1234", expires_at: NOW + 3600 });
		expect(Object.keys(body).sort()).toEqual(["access_token", "account_id", "expires_at", "id_token"]);
		expect(h.objectNames).toEqual(["default"]);
		expect(h.s.logs).toContain("CHATGPT_POOL store seeded");
		expect(h.s.logs).toContain("CHATGPT_POOL broker granted repository=shubhodeep1/digital_pa expires_in=3600 refreshed=false");
		const stored = (await h.storage.get(CHATGPT_STORE_KEY)) as Record<string, unknown>;
		expect(stored.refresh_token).toBe("rt_seed_refresh_token_value");
		expect(stored.seed_fingerprint).toMatch(/^[0-9a-f]{64}$/);
		expectNoChatgptSecrets(h, [String(stored.seed_fingerprint)]);
		expect(h.refreshBodies).toEqual([]);
	});

	it("does not re-seed while the seed is unchanged", async () => {
		const h = chatgptHarness();
		await callChatgpt(h, await sign(chatgptClaims()));
		await callChatgpt(h, await sign(chatgptClaims()));
		expect(h.s.logs.filter((line) => line === "CHATGPT_POOL store seeded").length).toBe(1);
		expect(h.storage.puts).toBe(1);
	});

	it("drops a refresh token and extra keys the object returns", async () => {
		const h = chatgptHarness();
		const access = chatgptJwt({ exp: NOW + 900 });
		h.env.CHATGPT_POOL = {
			idFromName: () => "id",
			get: () => ({ fetch: async () => Response.json({ access_token: access, id_token: access, account_id: "a", expires_at: NOW + 900, refresh_token: "rt_leak", extra: 1 }) }),
		};
		const { status, body } = await callChatgpt(h, await sign(chatgptClaims()));
		expect(status).toBe(200);
		expect(body).toEqual({ access_token: access, id_token: access, account_id: "a", expires_at: NOW + 900 });
	});

	it("leaves /v1/pool unchanged", async () => {
		const h = chatgptHarness();
		const response = await h.handler(request(await sign(claims())), h.env);
		expect(response.status).toBe(200);
		expect(((await response.json()) as Record<string, unknown>).accounts).toHaveLength(2);
		expect(h.objectNames).toEqual([]);
	});
});

describe("chatgpt: refused (403)", () => {
	it("refuses every caller with the production allowlist", async () => {
		expect(CHATGPT_ALLOWED_WORKFLOW_FILES).toEqual([]);
		const h = chatgptHarness();
		const s = h.s;
		const response = await createHandler(s.deps)(request(await sign(chatgptClaims()), { path: "/v1/chatgpt" }), h.env);
		expect(response.status).toBe(403);
		expect(await response.json()).toEqual({ error: "workflow_not_allowed" });
		expect(h.objectNames).toEqual([]);
		expect(s.logs).toContain("CHATGPT_POOL broker refused reason=workflow_not_allowed status=403 repository=unknown");
	});

	it("refuses a /v1/pool token, and /v1/pool refuses a /v1/chatgpt token", async () => {
		const h = chatgptHarness();
		expect((await callChatgpt(h, await sign(chatgptClaims({ aud: AUDIENCE })))).body.error).toBe("bad_audience");
		const pool = await h.handler(request(await sign(claims({ aud: CHATGPT_AUDIENCE }))), h.env);
		expect(pool.status).toBe(403);
		expect(await pool.json()).toEqual({ error: "bad_audience" });
	});

	const cases: [string, Record<string, unknown>, string][] = [
		["wrong owner", { repository_owner: "someone-else", repository: "someone-else/digital_pa" }, "wrong_owner"],
		["a repository not in the registry", { repository: "shubhodeep1/not-a-consumer" }, "repo_not_allowed"],
		["a workflow outside the allowlist", { job_workflow_ref: `${WF}clarify.yml@refs/heads/main` }, "workflow_not_allowed"],
		["a feature branch", { job_workflow_ref: `${WF}review_autofix.yml@refs/heads/feature` }, "workflow_ref_not_allowed"],
		["a consumer's own workflow", { job_workflow_ref: "shubhodeep1/digital_pa/.github/workflows/ai-review.yml@refs/heads/main" }, "wrong_workflow"],
		["a token issued too long ago", { iat: NOW - 700, nbf: NOW - 700 }, "too_old"],
		["an expired token", { exp: NOW - 120 }, "expired"],
	];
	for (const [name, overrides, reason] of cases) {
		it(name, async () => {
			const h = chatgptHarness();
			const { status, body } = await callChatgpt(h, await sign(chatgptClaims(overrides)));
			expect([status, body.error]).toEqual([403, reason]);
			expect(h.objectNames).toEqual([]);
		});
	}

	it("a bad signature or a missing bearer", async () => {
		const h = chatgptHarness();
		expect((await callChatgpt(h, await sign(chatgptClaims(), { key: otherKey }))).body.error).toBe("bad_signature");
		expect((await callChatgpt(h, null)).body.error).toBe("missing_token");
	});

	it("a SHA-pinned workflow that is not an ancestor of main or stable", async () => {
		const h = chatgptHarness();
		h.s.compare[`main...${SHA}`] = { status: 200, body: { status: "diverged" } };
		h.s.compare[`stable...${SHA}`] = { status: 200, body: { status: "ahead" } };
		const token = await sign(chatgptClaims({ job_workflow_ref: `${WF}review_autofix.yml@${SHA}`, job_workflow_sha: SHA }));
		expect((await callChatgpt(h, token)).body.error).toBe("workflow_sha_unverified");
	});
});

describe("chatgpt: broker-side failures (503)", () => {
	it("no Durable Object binding", async () => {
		const h = chatgptHarness({ binding: false });
		const { status, body } = await callChatgpt(h, await sign(chatgptClaims()));
		expect([status, body.error]).toEqual([503, "store_unavailable"]);
	});

	it("the Durable Object call throws", async () => {
		const h = chatgptHarness({ stubThrows: true });
		expect((await callChatgpt(h, await sign(chatgptClaims()))).body.error).toBe("store_unavailable");
	});

	it("the object answers an unknown error code or a bad shape", async () => {
		const h = chatgptHarness();
		h.env.CHATGPT_POOL = { idFromName: () => "id", get: () => ({ fetch: async () => Response.json({ error: "Bad Code!" }, { status: 503 }) }) };
		expect((await callChatgpt(h, await sign(chatgptClaims()))).body).toEqual({ error: "store_error" });
		h.env.CHATGPT_POOL = { idFromName: () => "id", get: () => ({ fetch: async () => Response.json({ access_token: "short" }) }) };
		const { status, body } = await callChatgpt(h, await sign(chatgptClaims()));
		expect([status, body.error]).toEqual([503, "store_error"]);
	});

	it("no seed", async () => {
		const h = chatgptHarness();
		delete h.env.CHATGPT_POOL_SEED;
		const { status, body } = await callChatgpt(h, await sign(chatgptClaims()));
		expect([status, body.error]).toEqual([503, "credential_not_seeded"]);
		expect(h.s.logs).toContain("CHATGPT_POOL broker refused reason=credential_not_seeded status=503 repository=shubhodeep1/digital_pa");
	});

	for (const [name, seed] of [
		["unparseable", "{"],
		["missing a field", chatgptSeed({ refresh_token: undefined })],
		["a short access token", chatgptSeed({ access_token: "short" })],
		["a bad account id", chatgptSeed({ account_id: "acct 1" })],
	] as const) {
		it(`a seed that is ${name}`, async () => {
			const h = chatgptHarness();
			h.env.CHATGPT_POOL_SEED = seed;
			expect((await callChatgpt(h, await sign(chatgptClaims()))).body.error).toBe("seed_invalid");
			expect(h.storage.puts).toBe(0);
		});
	}

	it("a flat seed is accepted", async () => {
		const h = chatgptHarness();
		h.env.CHATGPT_POOL_SEED = JSON.stringify(JSON.parse(chatgptSeed()).tokens);
		expect((await callChatgpt(h, await sign(chatgptClaims()))).status).toBe(200);
	});

	it("refresh off: an expiring token is refused, one with enough life is granted", async () => {
		const h = chatgptHarness();
		h.env.CHATGPT_POOL_SEED = chatgptSeed({}, 100);
		expect((await callChatgpt(h, await sign(chatgptClaims()))).body.error).toBe("refresh_not_configured");
		h.env.CHATGPT_POOL_SEED = chatgptSeed({}, 1000);
		expect((await callChatgpt(h, await sign(chatgptClaims()))).status).toBe(200);
		h.env.CHATGPT_REFRESH_URL = "http://insecure.example/token";
		h.env.CHATGPT_OAUTH_CLIENT_ID = "client";
		h.env.CHATGPT_POOL_SEED = chatgptSeed({}, 100);
		expect((await callChatgpt(h, await sign(chatgptClaims()))).body.error).toBe("refresh_not_configured");
		expect(h.refreshBodies).toEqual([]);
	});

	it("a refresh saves the rotated token before returning", async () => {
		const h = chatgptHarness();
		withRefresh(h, 100);
		const access = chatgptJwt({ exp: NOW + 7200, kind: "new-access" });
		const id = chatgptJwt({ exp: NOW + 7200, kind: "new-id" });
		h.refreshAnswers.push({ status: 200, body: { access_token: access, id_token: id, refresh_token: "rt_rotated_token_value" } });
		const { status, body } = await callChatgpt(h, await sign(chatgptClaims()));
		expect(status).toBe(200);
		expect(body).toEqual({ access_token: access, id_token: id, account_id: "acct-1234", expires_at: NOW + 7200 });
		expect(JSON.parse(h.refreshBodies[0])).toEqual({ client_id: "client-test-id", grant_type: "refresh_token", refresh_token: "rt_seed_refresh_token_value" });
		const stored = (await h.storage.get(CHATGPT_STORE_KEY)) as Record<string, unknown>;
		expect([stored.refresh_token, stored.access_token, stored.expires_at, stored.refreshed_at]).toEqual(["rt_rotated_token_value", access, NOW + 7200, NOW]);
		expect(h.s.logs).toContain("CHATGPT_POOL store refreshed");
		expect(h.s.logs).toContain("CHATGPT_POOL broker granted repository=shubhodeep1/digital_pa expires_in=7200 refreshed=true");
		expectNoChatgptSecrets(h, [access, id, "rt_rotated_token_value"]);
	});

	it("two concurrent grants make one refresh call", async () => {
		const h = chatgptHarness();
		withRefresh(h, 100);
		h.refreshAnswers.push({ status: 200, body: { access_token: chatgptJwt({ exp: NOW + 7200 }), refresh_token: "rt_rotated_token_value" } });
		const [a, b] = await Promise.all([callChatgpt(h, await sign(chatgptClaims())), callChatgpt(h, await sign(chatgptClaims()))]);
		expect([a.status, b.status]).toEqual([200, 200]);
		expect(h.refreshBodies.length).toBe(1);
	});

	for (const [name, answer] of [["a 5xx", { status: 502, body: {} }], ["a thrown fetch", "throw"], ["invalid JSON", { status: 200, raw: "nope" }]] as [string, CompareAnswer][]) {
		it(`a refresh that fails with ${name} is unavailable and keeps the credential`, async () => {
			const h = chatgptHarness();
			withRefresh(h, 100);
			h.refreshAnswers.push(answer);
			expect((await callChatgpt(h, await sign(chatgptClaims()))).body.error).toBe("credential_refresh_unavailable");
			const stored = (await h.storage.get(CHATGPT_STORE_KEY)) as Record<string, unknown>;
			expect([stored.refresh_token, stored.refresh_rejected]).toEqual(["rt_seed_refresh_token_value", false]);
		});
	}

	it("an unavailable refresh still grants a token with enough life left", async () => {
		const h = chatgptHarness();
		withRefresh(h, 1000);
		h.refreshAnswers.push({ status: 503, body: {} });
		const { status, body } = await callChatgpt(h, await sign(chatgptClaims()));
		expect([status, body.expires_at]).toEqual([200, NOW + 1000]);
		expect(h.s.logs).toContain("CHATGPT_POOL store refresh_failed reason=unavailable");
	});

	for (const [name, answer] of [["invalid_grant", { status: 400, body: { error: "invalid_grant" } }], ["a 401", { status: 401, body: {} }]] as [string, CompareAnswer][]) {
		it(`${name} latches the rejection until the seed changes`, async () => {
			const h = chatgptHarness();
			withRefresh(h, 1000);
			h.refreshAnswers.push(answer);
			expect((await callChatgpt(h, await sign(chatgptClaims()))).body.error).toBe("credential_refresh_rejected");
			expect((await callChatgpt(h, await sign(chatgptClaims()))).body.error).toBe("credential_refresh_rejected");
			expect(h.refreshBodies.length).toBe(1);
			h.env.CHATGPT_POOL_SEED = chatgptSeed({ refresh_token: "rt_new_seed_refresh_token" }, 100);
			h.refreshAnswers.push({ status: 200, body: { access_token: chatgptJwt({ exp: NOW + 7200 }) } });
			expect((await callChatgpt(h, await sign(chatgptClaims()))).status).toBe(200);
			expect(JSON.parse(h.refreshBodies[1]).refresh_token).toBe("rt_new_seed_refresh_token");
			const stored = (await h.storage.get(CHATGPT_STORE_KEY)) as Record<string, unknown>;
			expect([stored.refresh_token, stored.refresh_rejected]).toEqual(["rt_new_seed_refresh_token", false]);
		});
	}

	it("another 4xx fails without latching", async () => {
		const h = chatgptHarness();
		withRefresh(h, 100);
		h.refreshAnswers.push({ status: 400, body: { error: "invalid_request" } }, { status: 400, body: { error: "invalid_request" } });
		expect((await callChatgpt(h, await sign(chatgptClaims()))).body.error).toBe("credential_refresh_failed");
		expect((await callChatgpt(h, await sign(chatgptClaims()))).body.error).toBe("credential_refresh_failed");
		expect(h.refreshBodies.length).toBe(2);
	});

	it("an unreadable exp keeps the rotated token and fails closed", async () => {
		const h = chatgptHarness();
		withRefresh(h, 100);
		h.refreshAnswers.push({ status: 200, body: { access_token: chatgptJwt({ sub: "no-exp" }), refresh_token: "rt_rotated_token_value" } });
		expect((await callChatgpt(h, await sign(chatgptClaims()))).body.error).toBe("credential_expiry_unknown");
		const stored = (await h.storage.get(CHATGPT_STORE_KEY)) as Record<string, unknown>;
		expect(stored.refresh_token).toBe("rt_rotated_token_value");
	});

	it("a refresh answer without a usable access token keeps the rotated refresh token", async () => {
		const h = chatgptHarness();
		withRefresh(h, 100);
		h.refreshAnswers.push({ status: 200, body: { access_token: "x", refresh_token: "rt_rotated_token_value" } });
		expect((await callChatgpt(h, await sign(chatgptClaims()))).body.error).toBe("credential_refresh_invalid");
		const stored = (await h.storage.get(CHATGPT_STORE_KEY)) as Record<string, unknown>;
		expect(stored.refresh_token).toBe("rt_rotated_token_value");
	});

	it("a storage write failure", async () => {
		const h = chatgptHarness();
		h.storage.failPut = true;
		expect((await callChatgpt(h, await sign(chatgptClaims()))).body.error).toBe("store_write_failed");
		expect(h.s.logs).toContain("CHATGPT_POOL store write_failed");
	});

	it("an already expired seed", async () => {
		const h = chatgptHarness();
		withRefresh(h, -10);
		h.refreshAnswers.push({ status: 200, body: { access_token: chatgptJwt({ exp: NOW + 200 }) } });
		expect((await callChatgpt(h, await sign(chatgptClaims()))).body.error).toBe("credential_expired");
	});
});

describe("chatgpt: routing", () => {
	it("only POST /v1/chatgpt next to /v1/pool", async () => {
		const h = chatgptHarness();
		for (const path of ["/v1/chatgpt/", "/v1/chatgptx", "/v1/chatgpt/grant", "/grant"]) {
			expect((await h.handler(request(null, { path }), h.env)).status).toBe(404);
		}
		expect((await h.handler(request(null, { path: "/v1/chatgpt", method: "GET" }), h.env)).status).toBe(405);
		expect(h.s.calls).toEqual([]);
	});

	it("the Durable Object answers only POST /grant", async () => {
		const storage = new MapStorage();
		const object = new ChatgptPoolStore({ storage }, {});
		expect((await object.fetch(new Request("https://chatgpt-pool.internal/other", { method: "POST" }))).status).toBe(404);
		expect((await object.fetch(new Request("https://chatgpt-pool.internal/grant"))).status).toBe(404);
		const response = await object.fetch(new Request("https://chatgpt-pool.internal/grant", { method: "POST" }));
		expect(response.status).toBe(503);
		expect(await response.json()).toEqual({ error: "credential_not_seeded" });
	});
});
