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
import {
	CHATGPT_MAX_ACCOUNTS,
	CHATGPT_MAX_BODY_BYTES,
	CHATGPT_REGISTRY_NAME,
	CHATGPT_WRITER_AUDIENCE,
	ChatgptAccount,
	applyChatgptPut,
	chatgptGate,
	normalizeChatgptUsage,
	parseChatgptPut,
	stripRefreshToken,
	type ChatgptNamespace,
	type ChatgptStorage,
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

/* ---------- ChatGPT account store (chatgpt-account-reviewer-trial-plan.md, Phase 1) ---------- */

const WRITER_WF = "shubhodeep1/claude-workers/.github/workflows/chatgpt-pool-refresh.yml";
const SEED_A = "a".repeat(64);
const SEED_B = "b".repeat(64);
const DAY_MS = 24 * 3600 * 1000;

interface FakeNamespace extends ChatgptNamespace
{
	instances: Map<string, { object: ChatgptAccount; data: Map<string, unknown> }>;
}

function fakeNamespace(): FakeNamespace
{
	const instances = new Map<string, { object: ChatgptAccount; data: Map<string, unknown> }>();
	const instance = (name: string) => {
		let entry = instances.get(name);
		if (!entry) {
			const data = new Map<string, unknown>();
			const storage: ChatgptStorage = {
				get: async (key) => (data.has(key) ? structuredClone(data.get(key)) : undefined),
				put: async (key, value) => {
					data.set(key, structuredClone(value));
				},
			};
			entry = { object: new ChatgptAccount({ storage }), data };
			instances.set(name, entry);
		}
		return entry;
	};
	return {
		instances,
		idFromName: (name) => name,
		get: (id) => ({ fetch: (input, init) => instance(String(id)).object.fetch(new Request(input, init)) }),
	};
}

function chatgptEnv(namespace: ChatgptNamespace | undefined, extra: Partial<Env> = {}): Env
{
	return { ...ENV, CHATGPT_ACCOUNTS: namespace, CHATGPT_GATE: "0.9", ...extra };
}

function writerClaims(overrides: Record<string, unknown> = {}): Record<string, unknown>
{
	return claims({
		aud: CHATGPT_WRITER_AUDIENCE,
		repository: "shubhodeep1/claude-workers",
		job_workflow_ref: `${WRITER_WF}@refs/heads/main`,
		ref: "refs/heads/main",
		...overrides,
	});
}

function readerClaims(overrides: Record<string, unknown> = {}): Record<string, unknown>
{
	return claims({ job_workflow_ref: `${WF}review_autofix.yml@refs/heads/main`, ...overrides });
}

/** Claims issued at `ms` so a later setNow still passes the iat age check. */
function at(ms: number, base: Record<string, unknown>): Record<string, unknown>
{
	const seconds = Math.floor(ms / 1000);
	return { ...base, iat: seconds - 10, nbf: seconds - 10, exp: seconds + 300 };
}

function authJson(name: string): Record<string, unknown>
{
	return {
		OPENAI_API_KEY: null,
		tokens: { id_token: `id-${name}`, access_token: `access-${name}`, refresh_token: `refresh-${name}`, account_id: `acct-${name}` },
		last_refresh: "2026-10-04T00:00:00Z",
	};
}

function usage(fivePct: number | null, weeklyPct: number | null, extra: { five?: Record<string, unknown>; weekly?: Record<string, unknown> } = {}): Record<string, unknown>
{
	const result: Record<string, unknown> = {};
	if (fivePct !== null) {
		result.primary = { used_percent: fivePct, window_minutes: 300, ...extra.five };
	}
	if (weeklyPct !== null) {
		result.secondary = { used_percent: weeklyPct, window_minutes: 10080, ...extra.weekly };
	}
	return result;
}

function putBody(name: string, overrides: Record<string, unknown> = {}): Record<string, unknown>
{
	return {
		seed_sha256: SEED_A,
		base_generation: 0,
		reseed: true,
		outcome: "ok",
		forced_refresh: false,
		usage: null,
		checked_at: new Date(NOW_MS).toISOString(),
		auth_json: authJson(name),
		...overrides,
	};
}

function chatgptRequest(token: string | null, method: string, path: string, body?: unknown): Request
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

async function writerPut(s: Stub, env: Env, name: string, body: Record<string, unknown> | string, ms: number = NOW_MS)
{
	return call(s, chatgptRequest(await sign(at(ms, writerClaims())), "PUT", `/v1/chatgpt/refresh/${name}`, body), env);
}

async function writerGet(s: Stub, env: Env, name: string)
{
	return call(s, chatgptRequest(await sign(writerClaims()), "GET", `/v1/chatgpt/refresh/${name}`), env);
}

async function pick(s: Stub, env: Env, ms: number = NOW_MS)
{
	return call(s, chatgptRequest(await sign(at(ms, readerClaims())), "POST", "/v1/chatgpt"), env);
}

async function status(s: Stub, env: Env, ms: number = NOW_MS)
{
	return call(s, chatgptRequest(await sign(at(ms, readerClaims())), "GET", "/v1/chatgpt/status"), env);
}

/** Seed an account and, optionally, write usage / outcome in a second PUT. */
async function seedAccount(s: Stub, env: Env, name: string, extra: Record<string, unknown> = {}, ms: number = NOW_MS)
{
	const result = await writerPut(s, env, name, putBody(name, extra), ms);
	expect(result).toEqual({ status: 200, body: { generation: 1 } });
}

describe("chatgpt routes", () => {
	it("answers 405 for a wrong method and 404 for an unknown path, before auth", async () => {
		const s = stub();
		const env = chatgptEnv(fakeNamespace());
		expect((await call(s, chatgptRequest(null, "GET", "/v1/chatgpt"), env))).toEqual({ status: 405, body: { error: "method_not_allowed" } });
		expect((await call(s, chatgptRequest(null, "POST", "/v1/chatgpt/status"), env)).status).toBe(405);
		expect((await call(s, chatgptRequest(null, "POST", "/v1/chatgpt/refresh/ALPHA"), env)).status).toBe(405);
		expect((await call(s, chatgptRequest(null, "DELETE", "/v1/chatgpt/refresh/ALPHA"), env)).status).toBe(405);
		expect((await call(s, chatgptRequest(null, "GET", "/v1/chatgpt/x"), env))).toEqual({ status: 404, body: { error: "not_found" } });
		expect(s.calls).toEqual([]);
	});

	for (const name of ["bad-name", "alpha", "", "A/B", "A".repeat(65)]) {
		it(`answers 400 invalid_account for ${JSON.stringify(name)}`, async () => {
			const s = stub();
			const { status: code, body } = await call(s, chatgptRequest(await sign(writerClaims()), "GET", `/v1/chatgpt/refresh/${name}`), chatgptEnv(fakeNamespace()));
			expect([code, body]).toEqual([400, { error: "invalid_account" }]);
		});
	}

	it("a missing bearer is refused", async () => {
		const s = stub();
		expect((await call(s, chatgptRequest(null, "POST", "/v1/chatgpt"), chatgptEnv(fakeNamespace())))).toEqual({ status: 403, body: { error: "missing_token" } });
	});
});

describe("chatgpt reader auth", () => {
	it("grants a coding-workflows reusable-workflow token on both read routes", async () => {
		const s = stub();
		const env = chatgptEnv(fakeNamespace());
		await seedAccount(s, env, "ALPHA");
		expect((await pick(s, env)).status).toBe(200);
		expect((await status(s, env)).status).toBe(200);
	});

	const refused: [string, () => Record<string, unknown>, string][] = [
		["a writer-audience token", () => writerClaims({ repository: "shubhodeep1/digital_pa", job_workflow_ref: `${WF}review_autofix.yml@refs/heads/main` }), "bad_audience"],
		["a workflow outside ALLOWED_WORKFLOW_FILES", () => readerClaims({ job_workflow_ref: `${WF}evil.yml@refs/heads/main` }), "workflow_not_allowed"],
		["a claude-workers token", () => readerClaims({ repository: "shubhodeep1/claude-workers", job_workflow_ref: `${WRITER_WF}@refs/heads/main` }), "repo_not_allowed"],
		["a consumer's own workflow", () => readerClaims({ job_workflow_ref: "shubhodeep1/digital_pa/.github/workflows/ai-review.yml@refs/heads/main" }), "wrong_workflow"],
	];
	for (const [name, build, reason] of refused) {
		for (const [method, path] of [["POST", "/v1/chatgpt"], ["GET", "/v1/chatgpt/status"]]) {
			it(`refuses ${name} on ${method} ${path}`, async () => {
				const s = stub();
				const env = chatgptEnv(fakeNamespace());
				await seedAccount(s, env, "ALPHA");
				const { status: code, body } = await call(s, chatgptRequest(await sign(build()), method, path), env);
				expect([code, body]).toEqual([403, { error: reason }]);
			});
		}
	}
});

describe("chatgpt writer auth", () => {
	const refused: [string, Record<string, unknown>, string][] = [
		["a wrong repository", { repository: "shubhodeep1/digital_pa" }, "writer_repo_not_allowed"],
		["a fork with the same workflow", { repository: "evil/claude-workers", repository_owner: "evil", job_workflow_ref: "evil/claude-workers/.github/workflows/chatgpt-pool-refresh.yml@refs/heads/main" }, "wrong_owner"],
		["a wrong workflow file", { job_workflow_ref: "shubhodeep1/claude-workers/.github/workflows/claude-pool-key-sync.yml@refs/heads/main" }, "writer_workflow_not_allowed"],
		["a workflow in another repository", { job_workflow_ref: "shubhodeep1/coding-workflows/.github/workflows/chatgpt-pool-refresh.yml@refs/heads/main" }, "writer_workflow_not_allowed"],
		["a missing job_workflow_ref", { job_workflow_ref: undefined }, "writer_workflow_not_allowed"],
		["a feature-branch ref", { job_workflow_ref: `${WRITER_WF}@refs/heads/dev`, ref: "refs/heads/dev" }, "writer_ref_not_allowed"],
		["a SHA ref", { job_workflow_ref: `${WRITER_WF}@${SHA}` }, "writer_ref_not_allowed"],
		["a wrong ref claim", { ref: "refs/heads/dev" }, "writer_ref_not_allowed"],
		["a missing ref claim", { ref: undefined }, "writer_ref_not_allowed"],
		["the reader audience", { aud: AUDIENCE }, "bad_audience"],
		["a wrong owner", { repository_owner: "someone-else" }, "wrong_owner"],
	];
	for (const [name, overrides, reason] of refused) {
		for (const method of ["GET", "PUT"]) {
			it(`refuses ${name} on ${method}`, async () => {
				const s = stub();
				const namespace = fakeNamespace();
				const body = method === "PUT" ? putBody("ALPHA") : undefined;
				const { status: code, body: reply } = await call(s, chatgptRequest(await sign(writerClaims(overrides)), method, "/v1/chatgpt/refresh/ALPHA", body), chatgptEnv(namespace));
				expect([code, reply]).toEqual([403, { error: reason }]);
				expect(namespace.instances.size).toBe(0);
			});
		}
	}

	it("refuses a coding-workflows reader token on GET and PUT", async () => {
		const s = stub();
		const namespace = fakeNamespace();
		const env = chatgptEnv(namespace);
		for (const [method, body] of [["GET", undefined], ["PUT", putBody("ALPHA")]] as const) {
			const reader = await sign(readerClaims());
			expect((await call(s, chatgptRequest(reader, method, "/v1/chatgpt/refresh/ALPHA", body), env)).body.error).toBe("bad_audience");
			const readerWithWriterAudience = await sign(readerClaims({ aud: CHATGPT_WRITER_AUDIENCE }));
			expect((await call(s, chatgptRequest(readerWithWriterAudience, method, "/v1/chatgpt/refresh/ALPHA", body), env)).body.error).toBe("writer_repo_not_allowed");
		}
		expect(namespace.instances.size).toBe(0);
	});

	it("an invalid writer config fails closed", async () => {
		const s = stub();
		const env = chatgptEnv(fakeNamespace(), { CHATGPT_WRITER_WORKFLOW: "../evil" });
		expect((await writerGet(s, env, "ALPHA"))).toEqual({ status: 503, body: { error: "writer_config_invalid" } });
	});

	it("unset writer config uses the defaults", async () => {
		const s = stub();
		const env = chatgptEnv(fakeNamespace(), { CHATGPT_WRITER_REPOSITORY: undefined, CHATGPT_WRITER_WORKFLOW: undefined });
		expect((await writerGet(s, env, "ALPHA"))).toEqual({ status: 404, body: { error: "not_seeded" } });
	});
});

describe("chatgpt empty pool and store", () => {
	it("with no accounts, POST /v1/chatgpt is 503 chatgpt_pool_empty and status lists nothing", async () => {
		const s = stub();
		const env = chatgptEnv(fakeNamespace());
		expect(await pick(s, env)).toEqual({ status: 503, body: { error: "chatgpt_pool_empty" } });
		expect(await status(s, env)).toEqual({ status: 200, body: { gate: 0.9, accounts: [] } });
		expect(s.logs).toContain("CHATGPT_POOL broker chatgpt_refused route=pick reason=chatgpt_pool_empty status=503 repository=shubhodeep1/digital_pa account=none");
	});

	it("without the binding every route is 503 chatgpt_store_unavailable", async () => {
		const s = stub();
		const env = chatgptEnv(undefined);
		expect((await pick(s, env)).body.error).toBe("chatgpt_store_unavailable");
		expect((await status(s, env)).body.error).toBe("chatgpt_store_unavailable");
		expect((await writerGet(s, env, "ALPHA")).body.error).toBe("chatgpt_store_unavailable");
		expect((await writerPut(s, env, "ALPHA", putBody("ALPHA"))).status).toBe(503);
	});
});

describe("chatgpt seed, read and compare-and-swap", () => {
	it("GET on an unseeded account is 404 not_seeded", async () => {
		const s = stub();
		expect(await writerGet(s, chatgptEnv(fakeNamespace()), "ALPHA")).toEqual({ status: 404, body: { error: "not_seeded" } });
	});

	it("a reseed at generation 0 seeds, and GET returns the full file (refresh token included)", async () => {
		const s = stub();
		const env = chatgptEnv(fakeNamespace());
		await seedAccount(s, env, "ALPHA");
		expect(await writerGet(s, env, "ALPHA")).toEqual({ status: 200, body: { auth_json: authJson("ALPHA"), generation: 1, seed_sha256: SEED_A } });
	});

	it("a stale base_generation is 409 generation_conflict and leaves the record unchanged", async () => {
		const s = stub();
		const env = chatgptEnv(fakeNamespace());
		await seedAccount(s, env, "ALPHA");
		const stale = await writerPut(s, env, "ALPHA", putBody("ALPHA", { reseed: false, base_generation: 0, auth_json: authJson("OTHER") }));
		expect(stale).toEqual({ status: 409, body: { error: "generation_conflict" } });
		expect((await writerGet(s, env, "ALPHA")).body).toEqual({ auth_json: authJson("ALPHA"), generation: 1, seed_sha256: SEED_A });
	});

	it("two writes from the same base: the first wins, the second conflicts", async () => {
		const s = stub();
		const env = chatgptEnv(fakeNamespace());
		await seedAccount(s, env, "ALPHA");
		const body = putBody("ALPHA", { reseed: false, base_generation: 1 });
		const [first, second] = await Promise.all([writerPut(s, env, "ALPHA", body), writerPut(s, env, "ALPHA", body)]);
		expect([first, second].map((r) => r.status).sort()).toEqual([200, 409]);
		expect((await writerGet(s, env, "ALPHA")).body.generation).toBe(2);
	});

	it("a reseed on an existing account checks base_generation and replaces the seed and file", async () => {
		const s = stub();
		const env = chatgptEnv(fakeNamespace());
		await seedAccount(s, env, "ALPHA");
		expect((await writerPut(s, env, "ALPHA", putBody("ALPHA", { seed_sha256: SEED_B, base_generation: 0, auth_json: authJson("NEW") }))).body.error).toBe("generation_conflict");
		expect(await writerPut(s, env, "ALPHA", putBody("ALPHA", { seed_sha256: SEED_B, base_generation: 1, auth_json: authJson("NEW") }))).toEqual({ status: 200, body: { generation: 2 } });
		expect((await writerGet(s, env, "ALPHA")).body).toEqual({ auth_json: authJson("NEW"), generation: 2, seed_sha256: SEED_B });
	});

	it("a reseed without auth_json is 400 auth_json_required", async () => {
		const s = stub();
		expect((await writerPut(s, chatgptEnv(fakeNamespace()), "ALPHA", putBody("ALPHA", { auth_json: undefined })))).toEqual({ status: 400, body: { error: "auth_json_required" } });
	});

	it("a non-reseed write with a different seed is 409 seed_mismatch", async () => {
		const s = stub();
		const env = chatgptEnv(fakeNamespace());
		await seedAccount(s, env, "ALPHA");
		expect((await writerPut(s, env, "ALPHA", putBody("ALPHA", { reseed: false, base_generation: 1, seed_sha256: SEED_B })))).toEqual({ status: 409, body: { error: "seed_mismatch" } });
	});

	it("a non-reseed write on an unseeded account is 404 not_seeded", async () => {
		const s = stub();
		expect((await writerPut(s, chatgptEnv(fakeNamespace()), "ALPHA", putBody("ALPHA", { reseed: false })))).toEqual({ status: 404, body: { error: "not_seeded" } });
	});

	it("a write without auth_json keeps the stored file and bumps the generation", async () => {
		const s = stub();
		const env = chatgptEnv(fakeNamespace());
		await seedAccount(s, env, "ALPHA");
		expect(await writerPut(s, env, "ALPHA", putBody("ALPHA", { reseed: false, base_generation: 1, auth_json: undefined, outcome: "timeout" }))).toEqual({ status: 200, body: { generation: 2 } });
		expect((await writerGet(s, env, "ALPHA")).body).toEqual({ auth_json: authJson("ALPHA"), generation: 2, seed_sha256: SEED_A });
		const listed = (await status(s, env)).body.accounts as Record<string, unknown>[];
		expect(listed[0].last_outcome).toBe("timeout");
	});
});

describe("chatgpt PUT validation", () => {
	const invalid: [string, Record<string, unknown> | string, number, string][] = [
		["a bad seed_sha256", putBody("ALPHA", { seed_sha256: "ABC" }), 400, "invalid_body"],
		["an upper-case seed_sha256", putBody("ALPHA", { seed_sha256: "A".repeat(64) }), 400, "invalid_body"],
		["a negative base_generation", putBody("ALPHA", { base_generation: -1 }), 400, "invalid_body"],
		["a non-integer base_generation", putBody("ALPHA", { base_generation: 1.5 }), 400, "invalid_body"],
		["a string base_generation", putBody("ALPHA", { base_generation: "0" }), 400, "invalid_body"],
		["a non-boolean reseed", putBody("ALPHA", { reseed: "yes" }), 400, "invalid_body"],
		["an unknown outcome", putBody("ALPHA", { outcome: "great" }), 400, "invalid_body"],
		["an auth_json without tokens.access_token", putBody("ALPHA", { auth_json: { tokens: { refresh_token: "r" } } }), 400, "invalid_auth_json"],
		["an auth_json that is a list", putBody("ALPHA", { auth_json: [] }), 400, "invalid_auth_json"],
		["a non-object body", "[1, 2]", 400, "invalid_body"],
		["invalid JSON", "{", 400, "invalid_body"],
		["a body over 64 KiB", putBody("ALPHA", { padding: "x".repeat(CHATGPT_MAX_BODY_BYTES) }), 413, "body_too_large"],
	];
	for (const [name, body, code, reason] of invalid) {
		it(`refuses ${name}`, async () => {
			const s = stub();
			const namespace = fakeNamespace();
			expect(await writerPut(s, chatgptEnv(namespace), "ALPHA", body)).toEqual({ status: code, body: { error: reason } });
			expect(namespace.instances.size).toBe(0);
		});
	}

	it("accepts malformed usage, checked_at and forced_refresh as null / false", async () => {
		const s = stub();
		const env = chatgptEnv(fakeNamespace());
		await seedAccount(s, env, "ALPHA", { usage: { primary: "junk", secondary: { used_percent: "50" } }, checked_at: "not a date", forced_refresh: "yes" });
		const [entry] = (await status(s, env)).body.accounts as Record<string, unknown>[];
		expect(entry).toMatchObject({
			checked_at: null,
			forced_refresh: false,
			usage: { five_hour: null, weekly: null, resets: { five_hour: null, weekly: null } },
			usable: true,
			reason: "ok",
		});
	});

	it("normalizes usage: primary is the 5-hour window, secondary the weekly one", () => {
		const base = Date.UTC(2026, 9, 4, 12, 0, 0);
		expect(normalizeChatgptUsage(usage(25, 140, { five: { resets_in_seconds: 60 }, weekly: { resets_at: base / 1000 + 3600 } }), base)).toEqual({
			five_hour: 0.25,
			weekly: 1,
			resets: { five_hour: "2026-10-04T12:01:00.000Z", weekly: "2026-10-04T13:00:00.000Z" },
		});
		expect(normalizeChatgptUsage({ primary: { used_percent: -5, resets_at: "2026-10-05T00:00:00Z" } }, base)).toEqual({
			five_hour: 0,
			weekly: null,
			resets: { five_hour: "2026-10-05T00:00:00.000Z", weekly: null },
		});
		expect(normalizeChatgptUsage(null, base)).toEqual({ five_hour: null, weekly: null, resets: { five_hour: null, weekly: null } });
	});
});

describe("chatgpt refresh-token stripping", () => {
	it("POST /v1/chatgpt returns the file without tokens.refresh_token; the store keeps it", async () => {
		const s = stub();
		const env = chatgptEnv(fakeNamespace());
		await seedAccount(s, env, "ALPHA");
		const { status: code, body } = await pick(s, env);
		expect(code).toBe(200);
		const expected = authJson("ALPHA");
		delete (expected.tokens as Record<string, unknown>).refresh_token;
		expect(body).toEqual({ account: "ALPHA", auth_json: expected });
		expect(JSON.stringify(body)).not.toContain("refresh-ALPHA");
		expect(((await writerGet(s, env, "ALPHA")).body.auth_json as { tokens: Record<string, unknown> }).tokens.refresh_token).toBe("refresh-ALPHA");
	});

	it("stripRefreshToken leaves its input untouched", () => {
		const input = authJson("X");
		const stripped = stripRefreshToken(input);
		expect((stripped.tokens as Record<string, unknown>).refresh_token).toBeUndefined();
		expect((input.tokens as Record<string, unknown>).refresh_token).toBe("refresh-X");
	});
});

describe("chatgpt selection, gate and staleness", () => {
	it("picks the lowest weekly usage, then the lower 5-hour usage, then the name", async () => {
		const s = stub();
		const env = chatgptEnv(fakeNamespace());
		await seedAccount(s, env, "ALPHA", { usage: usage(10, 50) });
		await seedAccount(s, env, "BETA", { usage: usage(30, 20) });
		await seedAccount(s, env, "GAMMA", { usage: usage(10, 20) });
		expect((await pick(s, env)).body.account).toBe("GAMMA");
		await seedAccount(s, env, "DELTA", { usage: usage(10, 20) });
		expect((await pick(s, env)).body.account).toBe("DELTA");
	});

	it("skips an account with either window at or above the gate", async () => {
		const s = stub();
		const env = chatgptEnv(fakeNamespace());
		await seedAccount(s, env, "ALPHA", { usage: usage(90, 5) });
		await seedAccount(s, env, "BETA", { usage: usage(5, 90) });
		await seedAccount(s, env, "GAMMA", { usage: usage(80, 89) });
		expect((await pick(s, env)).body.account).toBe("GAMMA");
		const reasons = ((await status(s, env)).body.accounts as Record<string, unknown>[]).map((a) => [a.account, a.usable, a.reason]);
		expect(reasons).toEqual([["ALPHA", false, "over_gate"], ["BETA", false, "over_gate"], ["GAMMA", true, "ok"]]);
	});

	it("all accounts over the gate is chatgpt_pool_empty", async () => {
		const s = stub();
		const env = chatgptEnv(fakeNamespace());
		await seedAccount(s, env, "ALPHA", { usage: usage(95, 5) });
		expect((await pick(s, env)).body.error).toBe("chatgpt_pool_empty");
	});

	it("counts a window whose reset time has passed as 0", async () => {
		const s = stub();
		const env = chatgptEnv(fakeNamespace());
		await seedAccount(s, env, "ALPHA", { usage: usage(95, 10, { five: { resets_at: NOW - 60 } }) });
		await seedAccount(s, env, "BETA", { usage: usage(5, 50) });
		expect((await pick(s, env)).body.account).toBe("ALPHA");
	});

	for (const outcome of ["auth_failed", "quota_exhausted"]) {
		it(`skips an account whose last outcome is ${outcome}`, async () => {
			const s = stub();
			const env = chatgptEnv(fakeNamespace());
			await seedAccount(s, env, "ALPHA", { outcome });
			expect((await pick(s, env)).body.error).toBe("chatgpt_pool_empty");
			const [entry] = (await status(s, env)).body.accounts as Record<string, unknown>[];
			expect([entry.usable, entry.reason]).toEqual([false, outcome]);
		});
	}

	for (const outcome of ["timeout", "run_failed"]) {
		it(`keeps an account whose last outcome is ${outcome}`, async () => {
			const s = stub();
			const env = chatgptEnv(fakeNamespace());
			await seedAccount(s, env, "ALPHA", { outcome });
			expect((await pick(s, env)).body.account).toBe("ALPHA");
		});
	}

	it("drops an account not written for 7 days", async () => {
		const s = stub();
		const env = chatgptEnv(fakeNamespace());
		await seedAccount(s, env, "ALPHA");
		const later = NOW_MS + 7 * DAY_MS + 60 * 1000;
		s.setNow(later);
		await seedAccount(s, env, "BETA", { usage: usage(50, 80) }, later);
		expect((await pick(s, env, later)).body.account).toBe("BETA");
		const entries = (await status(s, env, later)).body.accounts as Record<string, unknown>[];
		expect(entries.map((a) => [a.account, a.usable, a.reason])).toEqual([["ALPHA", false, "stale"], ["BETA", true, "ok"]]);
	});

	it("treats usage null as unknown, so the account stays usable", async () => {
		const s = stub();
		const env = chatgptEnv(fakeNamespace());
		await seedAccount(s, env, "ALPHA", { usage: null });
		expect((await pick(s, env)).body.account).toBe("ALPHA");
	});

	it("an invalid CHATGPT_GATE falls back to 0.9", async () => {
		const s = stub();
		for (const gate of ["5", "0", "-1", "abc", ""]) {
			const env = chatgptEnv(fakeNamespace(), { CHATGPT_GATE: gate });
			await seedAccount(s, env, "ALPHA", { usage: usage(5, 91) });
			await seedAccount(s, env, "BETA", { usage: usage(5, 89) });
			expect((await pick(s, env)).body.account).toBe("BETA");
			expect((await status(s, env)).body.gate).toBe(0.9);
		}
		expect(chatgptGate({ CHATGPT_GATE: "0.5" })).toBe(0.5);
	});

	it("a lower CHATGPT_GATE gates more accounts", async () => {
		const s = stub();
		const env = chatgptEnv(fakeNamespace(), { CHATGPT_GATE: "0.5" });
		await seedAccount(s, env, "ALPHA", { usage: usage(5, 60) });
		expect((await pick(s, env)).body.error).toBe("chatgpt_pool_empty");
	});
});

describe("chatgpt status, registry and logs", () => {
	it("status never contains auth_json, a seed hash or any token", async () => {
		const s = stub();
		const env = chatgptEnv(fakeNamespace());
		await seedAccount(s, env, "ALPHA", { usage: usage(10, 20), forced_refresh: true });
		const { status: code, body } = await status(s, env);
		expect(code).toBe(200);
		const text = JSON.stringify(body);
		for (const forbidden of ["auth_json", "seed_sha256", "tokens", SEED_A, "access-ALPHA", "refresh-ALPHA", "id-ALPHA"]) {
			expect(text).not.toContain(forbidden);
		}
		expect(body).toEqual({
			gate: 0.9,
			accounts: [{
				account: "ALPHA",
				generation: 1,
				last_refresh: new Date(NOW_MS).toISOString(),
				last_outcome: "ok",
				forced_refresh: true,
				checked_at: new Date(NOW_MS).toISOString(),
				usage: { five_hour: 0.1, weekly: 0.2, resets: { five_hour: null, weekly: null } },
				usable: true,
				reason: "ok",
			}],
		});
	});

	it("the registry is updated on PUT; a listed name without a record is not_seeded", async () => {
		const s = stub();
		const namespace = fakeNamespace();
		const env = chatgptEnv(namespace);
		await seedAccount(s, env, "BETA");
		await seedAccount(s, env, "ALPHA");
		expect(namespace.instances.get(CHATGPT_REGISTRY_NAME)?.data.get("names")).toEqual(["ALPHA", "BETA"]);
		// A failed reseed still registers the name (D2) but writes no record.
		expect((await writerPut(s, env, "GAMMA", putBody("GAMMA", { base_generation: 3 }))).status).toBe(409);
		const entries = (await status(s, env)).body.accounts as Record<string, unknown>[];
		expect(entries.map((a) => [a.account, a.usable, a.reason])).toEqual([["ALPHA", true, "ok"], ["BETA", true, "ok"], ["GAMMA", false, "not_seeded"]]);
	});

	it("the registry refuses a new name past the account cap", async () => {
		const s = stub();
		const namespace = fakeNamespace();
		const env = chatgptEnv(namespace);
		const names = Array.from({ length: CHATGPT_MAX_ACCOUNTS }, (_, i) => `A${i}`);
		await namespace.get(namespace.idFromName(CHATGPT_REGISTRY_NAME)).fetch("https://x/registry/list");
		namespace.instances.get(CHATGPT_REGISTRY_NAME)?.data.set("names", names);
		expect(await writerPut(s, env, "ZED", putBody("ZED"))).toEqual({ status: 507, body: { error: "registry_full" } });
		expect(namespace.instances.has("ZED")).toBe(false);
		expect((await writerPut(s, env, "A1", putBody("A1"))).status).toBe(200);
	});

	it("log lines carry reason codes and account names, never a token, seed hash or JWT", async () => {
		const s = stub();
		const env = chatgptEnv(fakeNamespace());
		const writerToken = await sign(writerClaims());
		await call(s, chatgptRequest(writerToken, "PUT", "/v1/chatgpt/refresh/ALPHA", putBody("ALPHA")), env);
		await call(s, chatgptRequest(writerToken, "GET", "/v1/chatgpt/refresh/ALPHA"), env);
		const readerToken = await sign(readerClaims());
		await call(s, chatgptRequest(readerToken, "POST", "/v1/chatgpt"), env);
		await call(s, chatgptRequest(readerToken, "GET", "/v1/chatgpt/status"), env);
		await call(s, chatgptRequest(readerToken, "PUT", "/v1/chatgpt/refresh/ALPHA", putBody("ALPHA")), env);
		const text = s.logs.join("\n");
		for (const secret of ["access-ALPHA", "refresh-ALPHA", "id-ALPHA", SEED_A, writerToken, readerToken, "sk-ant"]) {
			expect(text).not.toContain(secret);
		}
		expect(s.logs).toContain("CHATGPT_POOL broker chatgpt_written account=ALPHA generation=1 reason=ok reseed=true forced_refresh=false");
		expect(s.logs).toContain("CHATGPT_POOL broker chatgpt_read account=ALPHA generation=1 reason=ok");
		expect(s.logs).toContain("CHATGPT_POOL broker chatgpt_granted route=pick repository=shubhodeep1/digital_pa account=ALPHA reason=selected usable=1");
		expect(s.logs).toContain("CHATGPT_POOL broker chatgpt_status repository=shubhodeep1/digital_pa accounts=1 usable=1");
		expect(s.logs).toContain("CHATGPT_POOL broker chatgpt_refused route=refresh reason=bad_audience status=403 repository=unknown account=ALPHA");
	});
});

describe("chatgpt store leaves /v1/pool alone", () => {
	it("POST /v1/pool ignores the CHATGPT_* env and never touches the store", async () => {
		const s = stub();
		const namespace = fakeNamespace();
		const { status: code, body } = await call(s, request(await sign(claims())), chatgptEnv(namespace, { CHATGPT_GATE: "0.1" }));
		expect(code).toBe(200);
		expect(body.gate).toBe(0.9);
		expect(Object.keys(body).sort()).toEqual(["accounts", "gate", "probe_model"]);
		expect(namespace.instances.size).toBe(0);
	});

	it("applyChatgptPut is a pure compare-and-swap", () => {
		const put = parseChatgptPut(JSON.stringify(putBody("ALPHA")), NOW_MS);
		const first = applyChatgptPut(undefined, put, "ALPHA", NOW_MS);
		expect(first.status).toBe(200);
		expect(applyChatgptPut(first.record, put, "ALPHA", NOW_MS)).toEqual({ status: 409, body: { error: "generation_conflict" } });
	});
});
