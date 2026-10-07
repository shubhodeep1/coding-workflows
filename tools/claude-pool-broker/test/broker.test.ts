import { beforeAll, describe, expect, it } from "vitest";
import {
	AUDIENCE,
	GITHUB_API,
	ISSUER,
	JWKS_URL,
	REGISTRY_TTL_MS,
	REGISTRY_URL,
	VERIFIED_SHA_TTL_MS,
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

describe("parseWorkflowRef", () => {
	it("splits the file and ref", () => {
		expect(parseWorkflowRef(`${WF}plan.yml@refs/heads/main`)).toEqual({ file: "plan.yml", ref: "refs/heads/main" });
		expect(parseWorkflowRef("shubhodeep1/coding-workflows-fork/.github/workflows/plan.yml@refs/heads/main")).toBeNull();
		expect(parseWorkflowRef(undefined)).toBeNull();
		expect(parseWorkflowRef(`${WF}@refs/heads/main`)).toBeNull();
	});
});
