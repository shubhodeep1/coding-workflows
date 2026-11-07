import { beforeAll, describe, expect, it } from "vitest";
import {
	AUDIENCE,
	GITHUB_API,
	ISSUER,
	JWKS_URL,
	REGISTRY_TTL_MS,
	REGISTRY_URL,
	VERIFIED_SHA_TTL_MS,
	CHATGPT_MAX_ACCOUNTS,
	CHATGPT_STALE_MS,
	ChatgptAccount,
	WRITER_AUDIENCE,
	applyChatgptWrite,
	chatgptUsable,
	createHandler,
	parseChatgptWrite,
	parsePool,
	parseWorkflowRef,
	stripRefreshToken,
	type ChatgptNamespace,
	type ChatgptRecord,
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

// ---------------------------------------------------------------------------
// ChatGPT account store (plan Phase 1).
// ---------------------------------------------------------------------------

const WRITER_REF = "shubhodeep1/claude-workers/.github/workflows/chatgpt-pool-refresh.yml@refs/heads/main";
const SEED = "a".repeat(64);
const SEED2 = "b".repeat(64);
const CHECKED_AT = "2026-10-04T11:59:00Z";

interface FakeStore
{
	namespace: ChatgptNamespace;
	mapFor: (name: string) => Map<string, unknown>;
	failing: { get: boolean };
}

/** A ChatgptNamespace of real ChatgptAccount objects over Map storage with a serial mutex. */
function fakeStore(): FakeStore
{
	const storage = new Map<string, Map<string, unknown>>();
	const objects = new Map<string, ChatgptAccount>();
	const failing = { get: false };
	function storageFor(name: string): Map<string, unknown>
	{
		let map = storage.get(name);
		if (!map) {
			map = new Map();
			storage.set(name, map);
		}
		return map;
	}
	const namespace: ChatgptNamespace = {
		idFromName: (name) => `id:${name}`,
		get: (id) => {
			const name = String(id).slice(3);
			let object = objects.get(name);
			if (!object) {
				const map = storageFor(name);
				let chain: Promise<unknown> = Promise.resolve();
				object = new ChatgptAccount({
					storage: {
						get: async (key) => {
							if (failing.get) {
								throw new Error("storage down");
							}
							return structuredClone(map.get(key));
						},
						put: async (key, value) => {
							map.set(key, structuredClone(value));
						},
					},
					blockConcurrencyWhile: <T>(callback: () => Promise<T>): Promise<T> => {
						const run = chain.then(callback);
						chain = run.catch(() => undefined);
						return run;
					},
				});
				objects.set(name, object);
			}
			const target = object;
			return { fetch: (input, init) => target.fetch(new Request(input, init)) };
		},
	};
	return { namespace, mapFor: storageFor, failing };
}

function authJson(name: string, extra: Record<string, unknown> = {}): Record<string, unknown>
{
	return {
		OPENAI_API_KEY: null,
		tokens: { id_token: `id-secret-${name}`, access_token: `at-secret-${name}`, refresh_token: `rt-secret-${name}`, account_id: `acct-${name}` },
		last_refresh: "2026-10-04T00:00:00Z",
		...extra,
	};
}

function record(name: string, overrides: Partial<ChatgptRecord> = {}): ChatgptRecord
{
	return {
		auth_json: authJson(name),
		seed_sha256: SEED,
		generation: 3,
		last_refresh: new Date(NOW_MS - 3600 * 1000).toISOString(),
		last_outcome: "ok",
		forced_refresh: false,
		checked_at: CHECKED_AT,
		usage: { five_hour: 0.1, weekly: 0.2, resets: { five_hour: 100, weekly: "2026-10-08T00:00:00Z" } },
		...overrides,
	};
}

function seedRecord(store: FakeStore, name: string, value: ChatgptRecord | null): void
{
	const registry = store.mapFor("chatgpt-registry");
	const names = ((registry.get("names") as string[] | undefined) ?? []).filter((n) => n !== name);
	names.push(name);
	names.sort();
	registry.set("names", names);
	if (value) {
		store.mapFor(name).set("record", value);
	}
}

function storedRecord(store: FakeStore, name: string): ChatgptRecord | undefined
{
	return store.mapFor(name).get("record") as ChatgptRecord | undefined;
}

function writerClaims(overrides: Record<string, unknown> = {}): Record<string, unknown>
{
	return claims({
		aud: WRITER_AUDIENCE,
		repository: "shubhodeep1/claude-workers",
		ref: "refs/heads/main",
		workflow_ref: WRITER_REF,
		job_workflow_ref: WRITER_REF,
		...overrides,
	});
}

function chatgptRequest(token: string | null, path: string, method: string, body?: unknown): Request
{
	const headers: Record<string, string> = {};
	if (token !== null) {
		headers.authorization = `Bearer ${token}`;
	}
	if (body !== undefined) {
		headers["content-type"] = "application/json";
	}
	return new Request(`https://claude-pool-broker.example.workers.dev${path}`, {
		method,
		headers,
		body: body === undefined ? undefined : typeof body === "string" ? body : JSON.stringify(body),
	});
}

async function rawCall(s: Stub, req: Request, env: Env): Promise<{ status: number; text: string }>
{
	const response = await createHandlerFor(s)(req, env);
	expect(response.headers.get("cache-control")).toBe("no-store");
	return { status: response.status, text: await response.text() };
}

function writeBody(overrides: Record<string, unknown> = {}): Record<string, unknown>
{
	return {
		seed_sha256: SEED,
		base_generation: 0,
		reseed: true,
		outcome: "ok",
		forced_refresh: false,
		usage: { primary: { used_percent: 12, window_minutes: 300, resets_in_seconds: 900 }, secondary: { used_percent: 34, window_minutes: 10080, resets_at: 1760000000 } },
		checked_at: CHECKED_AT,
		auth_json: authJson("NEW"),
		...overrides,
	};
}

function chatgptEnv(store: FakeStore | null, extra: Partial<Env> = {}): Env
{
	return { ...ENV, ...(store ? { CHATGPT_ACCOUNTS: store.namespace } : {}), ...extra };
}

describe("chatgpt read", () => {
	it("an empty store answers 503 chatgpt_pool_empty", async () => {
		const s = stub();
		const { status, text } = await rawCall(s, chatgptRequest(await sign(claims()), "/v1/chatgpt", "POST"), chatgptEnv(fakeStore()));
		expect([status, text]).toEqual([503, '{"error":"chatgpt_pool_empty"}']);
		expect(s.logs).toContain("CHATGPT_POOL broker refused route=read reason=chatgpt_pool_empty status=503");
	});

	it("picks the lowest weekly usage and strips the refresh token", async () => {
		const s = stub();
		const store = fakeStore();
		seedRecord(store, "ALPHA", record("ALPHA", { usage: { five_hour: 0.1, weekly: 0.5, resets: { five_hour: null, weekly: null } } }));
		seedRecord(store, "BETA", record("BETA", { usage: { five_hour: 0.4, weekly: 0.2, resets: { five_hour: null, weekly: null } } }));
		seedRecord(store, "GAMMA", record("GAMMA", { usage: { five_hour: null, weekly: null, resets: { five_hour: null, weekly: null } } }));
		const { status, body } = await call(s, chatgptRequest(await sign(claims()), "/v1/chatgpt", "POST"), chatgptEnv(store));
		expect(status).toBe(200);
		expect(body.account).toBe("BETA");
		const expected = authJson("BETA");
		delete (expected.tokens as Record<string, unknown>).refresh_token;
		expect(body.auth_json).toEqual(expected);
		expect(JSON.stringify(body)).not.toContain("rt-secret");
		expect((storedRecord(store, "BETA")?.auth_json.tokens as Record<string, unknown>).refresh_token).toBe("rt-secret-BETA");
		expect(s.logs).toContain("CHATGPT_POOL broker granted repository=shubhodeep1/digital_pa account=BETA");
	});

	it("sorts unknown weekly usage after known usage, ties by name", async () => {
		const s = stub();
		const store = fakeStore();
		const unknown = { five_hour: null, weekly: null, resets: { five_hour: null, weekly: null } };
		seedRecord(store, "ZED", record("ZED", { usage: unknown }));
		seedRecord(store, "ECHO", record("ECHO", { usage: unknown }));
		expect((await call(s, chatgptRequest(await sign(claims()), "/v1/chatgpt", "POST"), chatgptEnv(store))).body.account).toBe("ECHO");
		seedRecord(store, "YANKEE", record("YANKEE", { usage: { ...unknown, weekly: 0.8 } }));
		expect((await call(s, chatgptRequest(await sign(claims()), "/v1/chatgpt", "POST"), chatgptEnv(store))).body.account).toBe("YANKEE");
	});

	it("skips an account at or above the gate, honours CHATGPT_GATE and falls back on an invalid value", async () => {
		const s = stub();
		const store = fakeStore();
		seedRecord(store, "ALPHA", record("ALPHA", { usage: { five_hour: 0.1, weekly: 0.9, resets: { five_hour: null, weekly: null } } }));
		seedRecord(store, "BETA", record("BETA", { usage: { five_hour: 0.95, weekly: 0.1, resets: { five_hour: null, weekly: null } } }));
		expect((await call(s, chatgptRequest(await sign(claims()), "/v1/chatgpt", "POST"), chatgptEnv(store))).body.error).toBe("chatgpt_pool_empty");
		expect((await call(s, chatgptRequest(await sign(claims()), "/v1/chatgpt", "POST"), chatgptEnv(store, { CHATGPT_GATE: "0.99" }))).body.account).toBe("BETA");
		for (const bad of ["5", "0", "nope", "-1"]) {
			expect((await call(s, chatgptRequest(await sign(claims()), "/v1/chatgpt", "POST"), chatgptEnv(store, { CHATGPT_GATE: bad }))).body.error).toBe("chatgpt_pool_empty");
		}
	});

	it("drops an account not written for 7 days and keeps one just inside", async () => {
		const s = stub();
		const store = fakeStore();
		seedRecord(store, "OLD", record("OLD", { last_refresh: new Date(NOW_MS - CHATGPT_STALE_MS).toISOString(), usage: { five_hour: 0, weekly: 0, resets: { five_hour: null, weekly: null } } }));
		seedRecord(store, "FRESH", record("FRESH", { last_refresh: new Date(NOW_MS - CHATGPT_STALE_MS + 60_000).toISOString() }));
		expect((await call(s, chatgptRequest(await sign(claims()), "/v1/chatgpt", "POST"), chatgptEnv(store))).body.account).toBe("FRESH");
		seedRecord(store, "FRESH", record("FRESH", { last_refresh: null }));
		expect((await call(s, chatgptRequest(await sign(claims()), "/v1/chatgpt", "POST"), chatgptEnv(store))).body.error).toBe("chatgpt_pool_empty");
	});

	it("skips auth_failed and quota_exhausted accounts and listed-but-unseeded names", async () => {
		const s = stub();
		const store = fakeStore();
		seedRecord(store, "AUTH", record("AUTH", { last_outcome: "auth_failed", usage: { five_hour: 0, weekly: 0, resets: { five_hour: null, weekly: null } } }));
		seedRecord(store, "QUOTA", record("QUOTA", { last_outcome: "quota_exhausted", usage: { five_hour: 0, weekly: 0, resets: { five_hour: null, weekly: null } } }));
		seedRecord(store, "EMPTY", null);
		expect((await call(s, chatgptRequest(await sign(claims()), "/v1/chatgpt", "POST"), chatgptEnv(store))).body.error).toBe("chatgpt_pool_empty");
		seedRecord(store, "TIMEOUT", record("TIMEOUT", { last_outcome: "timeout" }));
		expect((await call(s, chatgptRequest(await sign(claims()), "/v1/chatgpt", "POST"), chatgptEnv(store))).body.account).toBe("TIMEOUT");
	});

	it("refuses a writer-audience token and a disallowed workflow", async () => {
		const s = stub();
		const store = fakeStore();
		seedRecord(store, "ALPHA", record("ALPHA"));
		expect(await rawCall(s, chatgptRequest(await sign(writerClaims()), "/v1/chatgpt", "POST"), chatgptEnv(store))).toEqual({ status: 403, text: '{"error":"bad_audience"}' });
		const evil = await sign(claims({ job_workflow_ref: "shubhodeep1/coding-workflows/.github/workflows/evil.yml@refs/heads/main" }));
		expect(await rawCall(s, chatgptRequest(evil, "/v1/chatgpt", "POST"), chatgptEnv(store))).toEqual({ status: 403, text: '{"error":"workflow_not_allowed"}' });
		expect((await rawCall(s, chatgptRequest(null, "/v1/chatgpt", "POST"), chatgptEnv(store))).status).toBe(403);
	});

	it("a missing binding or a storage failure is 503, never 500", async () => {
		const s = stub();
		expect(await rawCall(s, chatgptRequest(await sign(claims()), "/v1/chatgpt", "POST"), chatgptEnv(null))).toEqual({ status: 503, text: '{"error":"chatgpt_store_unavailable"}' });
		const store = fakeStore();
		seedRecord(store, "ALPHA", record("ALPHA"));
		store.failing.get = true;
		expect(await rawCall(s, chatgptRequest(await sign(claims()), "/v1/chatgpt", "POST"), chatgptEnv(store))).toEqual({ status: 503, text: '{"error":"chatgpt_store_unavailable"}' });
		const broken: ChatgptNamespace = { idFromName: () => { throw new Error("boom"); }, get: () => ({ fetch: async () => new Response("x") }) };
		expect((await rawCall(s, chatgptRequest(await sign(claims()), "/v1/chatgpt", "POST"), chatgptEnv(null, { CHATGPT_ACCOUNTS: broken }))).status).toBe(503);
		const notJson: ChatgptNamespace = { idFromName: (n) => n, get: () => ({ fetch: async () => new Response("not json", { status: 200 }) }) };
		expect((await rawCall(s, chatgptRequest(await sign(claims()), "/v1/chatgpt", "POST"), chatgptEnv(null, { CHATGPT_ACCOUNTS: notJson }))).status).toBe(503);
	});

	it("routing: wrong methods and unknown paths", async () => {
		const s = stub();
		const env = chatgptEnv(fakeStore());
		expect((await rawCall(s, chatgptRequest(null, "/v1/chatgpt", "GET"), env)).status).toBe(405);
		expect((await rawCall(s, chatgptRequest(null, "/v1/chatgpt/status", "POST"), env)).status).toBe(405);
		expect((await rawCall(s, chatgptRequest(null, "/v1/chatgpt/refresh/ALPHA", "POST"), env)).status).toBe(405);
		expect(await rawCall(s, chatgptRequest(null, "/v1/chatgpt/other", "GET"), env)).toEqual({ status: 404, text: '{"error":"not_found"}' });
		expect(s.calls).toEqual([]);
	});
});

describe("chatgpt status", () => {
	it("returns metadata only", async () => {
		const s = stub();
		const store = fakeStore();
		seedRecord(store, "ALPHA", record("ALPHA"));
		seedRecord(store, "BETA", record("BETA", { last_outcome: "auth_failed", generation: 7 }));
		seedRecord(store, "EMPTY", null);
		const { status, text } = await rawCall(s, chatgptRequest(await sign(claims()), "/v1/chatgpt/status", "GET"), chatgptEnv(store));
		expect(status).toBe(200);
		for (const secret of ["auth_json", "refresh_token", "seed_sha256", "secret", SEED]) {
			expect(text).not.toContain(secret);
		}
		const body = JSON.parse(text);
		expect(body.gate).toBe(0.9);
		expect(body.accounts).toEqual([
			{ account: "ALPHA", generation: 3, last_refresh: record("ALPHA").last_refresh, last_outcome: "ok", usage: { five_hour: 0.1, weekly: 0.2, resets: { five_hour: 100, weekly: "2026-10-08T00:00:00Z" } }, usable: true, reason: null },
			{ account: "BETA", generation: 7, last_refresh: record("BETA").last_refresh, last_outcome: "auth_failed", usage: { five_hour: 0.1, weekly: 0.2, resets: { five_hour: 100, weekly: "2026-10-08T00:00:00Z" } }, usable: false, reason: "auth_rejected" },
			{ account: "EMPTY", generation: 0, last_refresh: null, last_outcome: null, usage: { five_hour: null, weekly: null, resets: { five_hour: null, weekly: null } }, usable: false, reason: "not_seeded" },
		]);
		expect(s.logs).toContain("CHATGPT_POOL broker status accounts=3");
	});

	it("an empty store is an empty list", async () => {
		const s = stub();
		const { status, body } = await call(s, chatgptRequest(await sign(claims()), "/v1/chatgpt/status", "GET"), chatgptEnv(fakeStore()));
		expect([status, body]).toEqual([200, { accounts: [], gate: 0.9 }]);
	});

	it("refuses a writer token", async () => {
		const s = stub();
		expect(await rawCall(s, chatgptRequest(await sign(writerClaims()), "/v1/chatgpt/status", "GET"), chatgptEnv(fakeStore()))).toEqual({ status: 403, text: '{"error":"bad_audience"}' });
	});
});

describe("chatgpt writer", () => {
	async function writer(s: Stub, store: FakeStore | null, method: string, name: string, body?: unknown, extra: { claims?: Record<string, unknown>; env?: Partial<Env> } = {}): Promise<{ status: number; text: string }>
	{
		const token = await sign(writerClaims(extra.claims ?? {}));
		return rawCall(s, chatgptRequest(token, `/v1/chatgpt/refresh/${name}`, method, body), chatgptEnv(store, extra.env ?? {}));
	}

	it("GET on an unknown account is 404 not_seeded, byte for byte", async () => {
		const s = stub();
		expect(await writer(s, fakeStore(), "GET", "ALPHA")).toEqual({ status: 404, text: '{"error":"not_seeded"}' });
	});

	it("a reseed creates generation 1 and GET returns the full file", async () => {
		const s = stub();
		const store = fakeStore();
		expect(await writer(s, store, "PUT", "ALPHA", writeBody())).toEqual({ status: 200, text: '{"generation":1}' });
		const got = await writer(s, store, "GET", "ALPHA");
		expect(got.status).toBe(200);
		expect(got.text).toBe(JSON.stringify({ auth_json: authJson("NEW"), generation: 1, seed_sha256: SEED }));
		const stored = storedRecord(store, "ALPHA");
		expect(stored?.last_refresh).toBe(new Date(NOW_MS).toISOString());
		expect(stored?.usage).toEqual({ five_hour: 0.12, weekly: 0.34, resets: { five_hour: 900, weekly: 1760000000 } });
		expect(store.mapFor("chatgpt-registry").get("names")).toEqual(["ALPHA"]);
		expect(s.logs).toContain("CHATGPT_POOL broker refresh account=ALPHA outcome=ok generation=1 reseed=true");
		// The reseeded account is now served (refresh token stripped).
		const read = await call(s, chatgptRequest(await sign(claims()), "/v1/chatgpt", "POST"), chatgptEnv(store));
		expect(read.body.account).toBe("ALPHA");
		expect(JSON.stringify(read.body)).not.toContain("rt-secret");
	});

	it("a non-reseed PUT on an unknown account is 404 not_seeded", async () => {
		const s = stub();
		const body = writeBody({ reseed: false });
		expect(await writer(s, fakeStore(), "PUT", "ALPHA", body)).toEqual({ status: 404, text: '{"error":"not_seeded"}' });
	});

	it("a stale base_generation is 409 generation_conflict", async () => {
		const s = stub();
		const store = fakeStore();
		await writer(s, store, "PUT", "ALPHA", writeBody());
		expect(await writer(s, store, "PUT", "ALPHA", writeBody({ reseed: false, base_generation: 0 }))).toEqual({ status: 409, text: '{"error":"generation_conflict"}' });
		expect(await writer(s, store, "PUT", "ALPHA", writeBody({ reseed: false, base_generation: 2 }))).toEqual({ status: 409, text: '{"error":"generation_conflict"}' });
		expect(await writer(s, store, "PUT", "ALPHA", writeBody({ reseed: false, base_generation: 1 }))).toEqual({ status: 200, text: '{"generation":2}' });
	});

	it("a PUT without auth_json keeps the stored file and last_refresh", async () => {
		const s = stub();
		const store = fakeStore();
		await writer(s, store, "PUT", "ALPHA", writeBody());
		const before = storedRecord(store, "ALPHA");
		s.setNow(NOW_MS + 60_000);
		const body = writeBody({ reseed: false, base_generation: 1, outcome: "timeout", usage: null, seed_sha256: SEED2 });
		delete body.auth_json;
		expect(await writer(s, store, "PUT", "ALPHA", body)).toEqual({ status: 200, text: '{"generation":2}' });
		const after = storedRecord(store, "ALPHA");
		expect(after?.auth_json).toEqual(before?.auth_json);
		expect(after?.last_refresh).toBe(before?.last_refresh);
		expect(after?.seed_sha256).toBe(SEED);
		expect(after?.last_outcome).toBe("timeout");
		expect(after?.usage).toEqual(before?.usage);
	});

	it("an unreported usage window keeps its stored reading, so a gated account stays gated", async () => {
		const s = stub();
		const store = fakeStore();
		await writer(s, store, "PUT", "ALPHA", writeBody({ usage: { primary: { used_percent: 10, resets_in_seconds: 900 }, secondary: { used_percent: 97, resets_at: 1760000000 } } }));
		const body = writeBody({ reseed: false, base_generation: 1, outcome: "timeout", usage: { primary: { used_percent: 20 }, secondary: null } });
		delete body.auth_json;
		expect((await writer(s, store, "PUT", "ALPHA", body)).status).toBe(200);
		expect(storedRecord(store, "ALPHA")?.usage).toEqual({ five_hour: 0.2, weekly: 0.97, resets: { five_hour: null, weekly: 1760000000 } });
		const read = await call(s, chatgptRequest(await sign(claims()), "/v1/chatgpt", "POST"), chatgptEnv(store));
		expect(read.body.error).toBe("chatgpt_pool_empty");
	});

	it("a PUT with auth_json replaces the file and advances last_refresh", async () => {
		const s = stub();
		const store = fakeStore();
		await writer(s, store, "PUT", "ALPHA", writeBody());
		s.setNow(NOW_MS + 60_000);
		const body = writeBody({ reseed: false, base_generation: 1, auth_json: authJson("ROTATED") });
		expect((await writer(s, store, "PUT", "ALPHA", body)).status).toBe(200);
		expect(storedRecord(store, "ALPHA")?.auth_json).toEqual(authJson("ROTATED"));
		expect(storedRecord(store, "ALPHA")?.last_refresh).toBe(new Date(NOW_MS + 60_000).toISOString());
	});

	it("a reseed with a new hash replaces the record but still checks base_generation", async () => {
		const s = stub();
		const store = fakeStore();
		await writer(s, store, "PUT", "ALPHA", writeBody());
		expect(await writer(s, store, "PUT", "ALPHA", writeBody({ seed_sha256: SEED2, auth_json: authJson("RESEED") }))).toEqual({ status: 409, text: '{"error":"generation_conflict"}' });
		expect(await writer(s, store, "PUT", "ALPHA", writeBody({ seed_sha256: SEED2, base_generation: 1, auth_json: authJson("RESEED") }))).toEqual({ status: 200, text: '{"generation":2}' });
		const got = JSON.parse((await writer(s, store, "GET", "ALPHA")).text);
		expect(got).toEqual({ auth_json: authJson("RESEED"), generation: 2, seed_sha256: SEED2 });
	});

	const invalidBodies: [string, unknown][] = [
		["not JSON", "{"],
		["an array", "[]"],
		["a bad seed hash", writeBody({ seed_sha256: "ABC" })],
		["a negative base_generation", writeBody({ base_generation: -1 })],
		["a fractional base_generation", writeBody({ base_generation: 1.5 })],
		["a string reseed", writeBody({ reseed: "true" })],
		["an unknown outcome", writeBody({ outcome: "weird" })],
		["a missing forced_refresh", writeBody({ forced_refresh: undefined })],
		["a usage string", writeBody({ usage: "x" })],
		["a negative used_percent", writeBody({ usage: { primary: { used_percent: -1 } } })],
		["a bad checked_at", writeBody({ checked_at: "yesterday" })],
		["an auth_json without tokens", writeBody({ auth_json: { OPENAI_API_KEY: null } })],
		["a reseed without auth_json", writeBody({ auth_json: undefined })],
		["an oversized body", writeBody({ auth_json: { tokens: { id_token: "x".repeat(70000) } } })],
	];
	for (const [name, body] of invalidBodies) {
		it(`refuses ${name} with 400 invalid_body`, async () => {
			const s = stub();
			const store = fakeStore();
			expect(await writer(s, store, "PUT", "ALPHA", body)).toEqual({ status: 400, text: '{"error":"invalid_body"}' });
			expect(storedRecord(store, "ALPHA")).toBeUndefined();
		});
	}

	for (const name of ["alpha", "AL-PHA", "A%2FB", "A/B", "", "X".repeat(65)]) {
		it(`an invalid account name ${JSON.stringify(name)} is 404 not_found`, async () => {
			const s = stub();
			expect(await writer(s, fakeStore(), "GET", name)).toEqual({ status: 404, text: '{"error":"not_found"}' });
		});
	}

	const refusals: [string, Record<string, unknown>, string][] = [
		["a wrong repository", { repository: "shubhodeep1/coding-workflows" }, "repo_not_allowed"],
		["a wrong workflow file", { workflow_ref: WRITER_REF.replace("chatgpt-pool-refresh.yml", "other.yml"), job_workflow_ref: WRITER_REF.replace("chatgpt-pool-refresh.yml", "other.yml") }, "workflow_not_allowed"],
		["a wrong ref claim", { ref: "refs/heads/feature" }, "workflow_ref_not_allowed"],
		["a feature-branch workflow_ref", { workflow_ref: WRITER_REF.replace("refs/heads/main", "refs/heads/feature") }, "workflow_ref_not_allowed"],
		["a job_workflow_ref elsewhere", { job_workflow_ref: "shubhodeep1/coding-workflows/.github/workflows/clarify.yml@refs/heads/main" }, "workflow_not_allowed"],
		["no job_workflow_ref", { job_workflow_ref: undefined }, "workflow_not_allowed"],
		["a read-audience token", { aud: AUDIENCE }, "bad_audience"],
		["an expired token", { exp: NOW - 120 }, "expired"],
	];
	for (const [name, overrides, reason] of refusals) {
		it(`refuses ${name}`, async () => {
			const s = stub();
			const store = fakeStore();
			seedRecord(store, "ALPHA", record("ALPHA"));
			expect(await writer(s, store, "GET", "ALPHA", undefined, { claims: overrides })).toEqual({ status: 403, text: JSON.stringify({ error: reason }) });
			expect(await writer(s, store, "PUT", "ALPHA", writeBody({ base_generation: 3 }), { claims: overrides })).toEqual({ status: 403, text: JSON.stringify({ error: reason }) });
			expect(storedRecord(store, "ALPHA")?.generation).toBe(3);
			expect(s.calls.filter((url) => url === REGISTRY_URL || url.startsWith(GITHUB_API))).toEqual([]);
		});
	}

	it("accepts the writer repository in any case", async () => {
		const s = stub();
		const ref = "ShubhoDeep1/Claude-Workers/.github/workflows/chatgpt-pool-refresh.yml@refs/heads/main";
		expect((await writer(s, fakeStore(), "GET", "ALPHA", undefined, { claims: { repository: "ShubhoDeep1/Claude-Workers", workflow_ref: ref, job_workflow_ref: ref } })).status).toBe(404);
	});

	it("an invalid CHATGPT_WRITER_* value fails closed with 503", async () => {
		const s = stub();
		for (const env of [{ CHATGPT_WRITER_REPOSITORY: "not a repo" }, { CHATGPT_WRITER_WORKFLOW: "../x.yml" }, { CHATGPT_WRITER_WORKFLOW: "" }]) {
			expect(await writer(s, fakeStore(), "GET", "ALPHA", undefined, { env })).toEqual({ status: 503, text: '{"error":"writer_config_invalid"}' });
		}
	});

	it("honours the configured writer repository and workflow", async () => {
		const s = stub();
		const ref = "shubhodeep1/other/.github/workflows/w.yml@refs/heads/main";
		const env = { CHATGPT_WRITER_REPOSITORY: "shubhodeep1/other", CHATGPT_WRITER_WORKFLOW: "w.yml" };
		expect((await writer(s, fakeStore(), "GET", "ALPHA", undefined, { env })).text).toBe('{"error":"repo_not_allowed"}');
		expect((await writer(s, fakeStore(), "GET", "ALPHA", undefined, { env, claims: { repository: "shubhodeep1/other", workflow_ref: ref, job_workflow_ref: ref } })).status).toBe(404);
	});

	it("a missing binding or a storage throw is 503, never 500", async () => {
		const s = stub();
		expect(await writer(s, null, "GET", "ALPHA")).toEqual({ status: 503, text: '{"error":"chatgpt_store_unavailable"}' });
		expect(await writer(s, null, "PUT", "ALPHA", writeBody())).toEqual({ status: 503, text: '{"error":"chatgpt_store_unavailable"}' });
		const store = fakeStore();
		store.failing.get = true;
		expect(await writer(s, store, "GET", "ALPHA")).toEqual({ status: 503, text: '{"error":"chatgpt_store_unavailable"}' });
		expect(await writer(s, store, "PUT", "ALPHA", writeBody())).toEqual({ status: 503, text: '{"error":"chatgpt_store_unavailable"}' });
	});

	it("a full registry refuses a new account with 503 chatgpt_registry_full", async () => {
		const s = stub();
		const store = fakeStore();
		for (let i = 0; i < CHATGPT_MAX_ACCOUNTS; i++) {
			seedRecord(store, `ACCT_${String(i).padStart(2, "0")}`, null);
		}
		expect(await writer(s, store, "PUT", "ALPHA", writeBody())).toEqual({ status: 503, text: '{"error":"chatgpt_registry_full"}' });
		expect(await writer(s, store, "PUT", "ACCT_00", writeBody())).toEqual({ status: 200, text: '{"generation":1}' });
	});

	it("serialises concurrent writers: one wins, one conflicts", async () => {
		const s = stub();
		const store = fakeStore();
		await writer(s, store, "PUT", "ALPHA", writeBody());
		const results = await Promise.all([
			writer(s, store, "PUT", "ALPHA", writeBody({ reseed: false, base_generation: 1 })),
			writer(s, store, "PUT", "ALPHA", writeBody({ reseed: false, base_generation: 1 })),
		]);
		expect(results.map((r) => r.status).sort()).toEqual([200, 409]);
		expect(storedRecord(store, "ALPHA")?.generation).toBe(2);
	});
});

describe("chatgpt logs", () => {
	it("never carry a token, the JWT, auth_json or the seed hash", async () => {
		const s = stub();
		const store = fakeStore();
		const tokens: string[] = [];
		const jwtRead = await sign(claims());
		const jwtWriter = await sign(writerClaims());
		tokens.push(jwtRead, jwtWriter);
		const env = chatgptEnv(store);
		await createHandlerFor(s)(chatgptRequest(jwtWriter, "/v1/chatgpt/refresh/ALPHA", "PUT", writeBody()), env);
		await createHandlerFor(s)(chatgptRequest(jwtWriter, "/v1/chatgpt/refresh/ALPHA", "GET"), env);
		await createHandlerFor(s)(chatgptRequest(jwtWriter, "/v1/chatgpt/refresh/ALPHA", "PUT", writeBody()), env);
		await createHandlerFor(s)(chatgptRequest(jwtWriter, "/v1/chatgpt/refresh/ALPHA", "PUT", "{"), env);
		await createHandlerFor(s)(chatgptRequest(jwtRead, "/v1/chatgpt", "POST"), env);
		await createHandlerFor(s)(chatgptRequest(jwtRead, "/v1/chatgpt/status", "GET"), env);
		await createHandlerFor(s)(chatgptRequest(jwtRead, "/v1/chatgpt/refresh/ALPHA", "GET"), env);
		await createHandlerFor(s)(chatgptRequest(jwtWriter, "/v1/chatgpt", "POST"), env);
		const text = s.logs.join("\n");
		expect(s.logs.length).toBeGreaterThan(5);
		for (const secret of [...tokens, "id-secret", "at-secret", "rt-secret", "acct-NEW", SEED, "auth_json", "tokens"]) {
			expect(text).not.toContain(secret);
		}
		expect(s.logs.every((line) => line.startsWith("CHATGPT_POOL broker ") || line.startsWith("CLAUDE_POOL broker "))).toBe(true);
	});
});

describe("/v1/pool unchanged", () => {
	it("returns the same body and makes the same calls with a CHATGPT_ACCOUNTS binding", async () => {
		const plain = stub();
		const withStore = stub();
		const store = fakeStore();
		seedRecord(store, "ALPHA", record("ALPHA"));
		const a = await call(plain, request(await sign(claims())));
		const b = await call(withStore, request(await sign(claims())), chatgptEnv(store));
		expect(b).toEqual(a);
		expect(withStore.calls).toEqual(plain.calls);
		expect(withStore.logs).toEqual(plain.logs);
	});

	it("refuses a writer-audience token", async () => {
		const s = stub();
		expect((await call(s, request(await sign(writerClaims())))).body.error).toBe("bad_audience");
	});
});

describe("chatgpt helpers", () => {
	it("applyChatgptWrite: not_seeded beats conflict, CAS, seed kept", () => {
		const write = parseChatgptWrite(writeBody({ reseed: false, base_generation: 5 }));
		expect(applyChatgptWrite(null, write, "t")).toEqual({ status: 404, body: { error: "not_seeded" } });
		const stored = record("ALPHA", { generation: 5, seed_sha256: SEED2 });
		const result = applyChatgptWrite(stored, write, "2026-10-04T12:00:00.000Z");
		expect(result.status).toBe(200);
		expect(result.record?.seed_sha256).toBe(SEED2);
		expect(result.record?.generation).toBe(6);
		expect(applyChatgptWrite(stored, parseChatgptWrite(writeBody({ reseed: false, base_generation: 4 })), "t").status).toBe(409);
	});

	it("parseChatgptWrite: accepts +00:00, null windows and clamps over-100 readings", () => {
		const write = parseChatgptWrite(writeBody({ checked_at: "2026-10-04T11:59:00.123456+00:00", usage: { primary: null, secondary: { used_percent: 140 } } }));
		expect(write.usage).toEqual({ primary: null, secondary: { used_percent: 100, reset: null } });
		expect(parseChatgptWrite(writeBody({ usage: null })).usage).toEqual({ primary: null, secondary: null });
	});

	it("chatgptUsable reasons", () => {
		expect(chatgptUsable(null, 0.9, NOW_MS)).toBe("not_seeded");
		expect(chatgptUsable(record("A"), 0.9, NOW_MS)).toBeNull();
		expect(chatgptUsable(record("A", { last_outcome: "auth_failed" }), 0.9, NOW_MS)).toBe("auth_rejected");
		expect(chatgptUsable(record("A", { last_outcome: "quota_exhausted" }), 0.9, NOW_MS)).toBe("quota_exhausted");
		expect(chatgptUsable(record("A", { last_refresh: "garbage" }), 0.9, NOW_MS)).toBe("stale");
		expect(chatgptUsable(record("A"), 0.15, NOW_MS)).toBe("gated");
	});

	it("stripRefreshToken copies and leaves the input intact", () => {
		const input = authJson("A");
		const copy = stripRefreshToken(input);
		expect((copy.tokens as Record<string, unknown>).refresh_token).toBeUndefined();
		expect((input.tokens as Record<string, unknown>).refresh_token).toBe("rt-secret-A");
	});
});
