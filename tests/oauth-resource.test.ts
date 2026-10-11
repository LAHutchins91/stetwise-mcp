import http from "node:http";
import type { Server } from "node:http";
import { once } from "node:events";
import fs from "node:fs";
import path from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { PGlite } from "@electric-sql/pglite";
import { repoRoot } from "../src/config.js";
import { validateMcpClaims } from "../src/mcp-claims.js";
import { app } from "../src/server.js";

const hookSql = fs.readFileSync(path.join(repoRoot(), "supabase/oauth_resource_binding.sql"), "utf8");
const slot = (index: number) => "$" + index;
const expected = "https://stetwise.example/mcp";
const user = "11111111-1111-4111-8111-111111111111";
const clientA = "33333333-3333-4333-8333-333333333333";
const clientB = "44444444-4444-4444-8444-444444444444";

const bootstrap = `
create schema if not exists auth;
create table if not exists auth.users (id uuid primary key);
create table if not exists auth.sessions (
  id uuid primary key,
  user_id uuid
);
create table if not exists auth.oauth_authorizations (
  id uuid primary key,
  client_id uuid,
  user_id uuid,
  status text,
  expires_at timestamptz,
  resource text,
  created_at timestamptz not null default now()
);
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then
    create role anon nologin;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'supabase_auth_admin') then
    create role supabase_auth_admin nologin;
  end if;
end
$$;
`;

type HookEvent = { claims?: { aud?: unknown; marker?: string } };
type HookRow = { event: HookEvent };

function oauthEvent(sessionId: string, clientId: string | null, method = "oauth") {
  const claims: Record<string, unknown> = {
    sub: user,
    session_id: sessionId,
    aud: "authenticated",
    role: "authenticated",
    scope: "email",
    marker: "keep"
  };
  if (clientId) claims.client_id = clientId;
  return { user_id: user, authentication_method: method, claims };
}

describe("OAuth resource audience", () => {
  const db = new PGlite();

  beforeAll(async () => {
    await db.exec(bootstrap);
    await db.exec(hookSql);
    await db.exec(hookSql);
    await db.query(`update stetwise_private.settings set value = ${slot(1)} where key = 'mcp_resource'`, [expected]);
  });

  afterAll(async () => {
    await db.close();
  });

  async function callHook(event: ReturnType<typeof oauthEvent>): Promise<HookEvent | undefined> {
    await db.exec("set role supabase_auth_admin");
    try {
      const result = await db.query<HookRow>(`select public.stetwise_access_token_hook(${slot(1)}::jsonb) as event`, [
        JSON.stringify(event)
      ]);
      return result.rows[0]?.event;
    } finally {
      await db.exec("reset role");
    }
  }

  async function audience(sessionId: string, clientId: string | null, method = "oauth"): Promise<unknown> {
    await db.query(`insert into auth.sessions (id, user_id) values (${slot(1)}, ${slot(2)}) on conflict do nothing`, [sessionId, user]);
    const event = await callHook(oauthEvent(sessionId, clientId, method));
    return event?.claims?.aud;
  }

  async function setLatest(clientId: string, id: string, resource: string | null) {
    await db.query(`delete from auth.oauth_authorizations where client_id = ${slot(1)}`, [clientId]);
    await approve(clientId, id, resource, "0 minutes");
  }

  async function approve(clientId: string, id: string, resource: string | null, age: string) {
    await db.query(
      `insert into auth.oauth_authorizations (id, client_id, user_id, status, expires_at, resource, created_at)
       values (${slot(1)}, ${slot(2)}, ${slot(3)}, 'approved', now() + interval '10 minutes', ${slot(4)}, now() - ${slot(5)}::interval)`,
      [id, clientId, user, resource, age]
    );
  }

  it("binds only an exact MCP URL and leaves every other token as authenticated", async () => {
    const exact = "55555555-5555-4555-8555-555555555551";
    const evil = "55555555-5555-4555-8555-555555555552";
    const wrongPath = "55555555-5555-4555-8555-555555555553";
    const slash = "55555555-5555-4555-8555-555555555554";
    const http = "55555555-5555-4555-8555-555555555555";
    const missing = "55555555-5555-4555-8555-555555555556";
    const query = "55555555-5555-4555-8555-555555555557";
    const hostOnly = "55555555-5555-4555-8555-555555555558";
    const suffix = "55555555-5555-4555-8555-555555555559";
    const cased = "55555555-5555-4555-8555-55555555555a";

    await setLatest(clientA, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1", expected);
    expect(await audience(exact, clientA)).toEqual(["authenticated", expected]);

    await setLatest(clientA, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2", "https://evil.example/mcp");
    expect(await audience(evil, clientA)).toBe("authenticated");

    await setLatest(clientA, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3", "https://stetwise.example/other");
    expect(await audience(wrongPath, clientA)).toBe("authenticated");

    await setLatest(clientA, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa4", `${expected}/`);
    expect(await audience(slash, clientA)).toBe("authenticated");

    await setLatest(clientA, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa5", "http://stetwise.example/mcp");
    expect(await audience(http, clientA)).toBe("authenticated");

    await setLatest(clientA, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa6", null);
    expect(await audience(missing, clientA)).toBe("authenticated");

    await setLatest(clientA, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa7", `${expected}?next=/mcp`);
    expect(await audience(query, clientA)).toBe("authenticated");

    await setLatest(clientA, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa8", "https://stetwise.example");
    expect(await audience(hostOnly, clientA)).toBe("authenticated");

    await setLatest(clientA, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa9", `${expected}/extra`);
    expect(await audience(suffix, clientA)).toBe("authenticated");

    await setLatest(clientA, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab", "HTTPS://Stetwise.Example/mcp");
    expect(await audience(cased, clientA)).toEqual(["authenticated", expected]);
  });

  it("leaves a first-party session token unchanged", async () => {
    const sessionId = "55555555-5555-4555-8555-555555555560";
    await setLatest(clientA, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaac", expected);
    await db.query(`insert into auth.sessions (id, user_id) values (${slot(1)}, ${slot(2)}) on conflict do nothing`, [sessionId, user]);
    const input = oauthEvent(sessionId, null, "password");
    expect(await callHook(input)).toEqual(input);
  });

  it("does not let another client's grant or a stored evil resource bind this server", async () => {
    const crossed = "55555555-5555-4555-8555-555555555561";
    const poisoned = "55555555-5555-4555-8555-555555555562";
    const refresh = "55555555-5555-4555-8555-555555555563";

    await setLatest(clientB, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaad", expected);
    await setLatest(clientA, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaae", "https://evil.example/mcp");
    expect(await audience(crossed, clientA)).toBe("authenticated");

    await db.query(`insert into auth.sessions (id, user_id) values (${slot(1)}, ${slot(2)}) on conflict do nothing`, [poisoned, user]);
    await db.query(`insert into stetwise_private.oauth_resources (session_id, resource) values (${slot(1)}, ${slot(2)})`, [
      poisoned,
      "https://evil.example/mcp"
    ]);
    expect(await audience(poisoned, clientA)).toBe("authenticated");

    await db.query(`insert into auth.sessions (id, user_id) values (${slot(1)}, ${slot(2)}) on conflict do nothing`, [refresh, user]);
    await db.query(
      `insert into stetwise_private.oauth_resources (session_id, resource, strict_match) values (${slot(1)}, ${slot(2)}, false) on conflict (session_id) do update set resource = excluded.resource, strict_match = false`,
      [refresh, expected]
    );
    expect(await audience(refresh, clientA)).toBe("authenticated");
    const kept = await db.query<{ strict_match: boolean }>(
      `select strict_match from stetwise_private.oauth_resources where session_id = ${slot(1)}`,
      [refresh]
    );
    expect(kept.rows[0]?.strict_match).toBe(false);
  });

  it("binds only when every open authorization is the configured URL", async () => {
    const evilFirst = "55555555-5555-4555-8555-555555555571";
    const realFirst = "55555555-5555-4555-8555-555555555572";
    const alone = "55555555-5555-4555-8555-555555555573";
    const withNull = "55555555-5555-4555-8555-555555555574";

    await db.query(`delete from auth.oauth_authorizations where client_id = ${slot(1)}`, [clientA]);
    await approve(clientA, "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1", "https://evil.example/mcp", "2 minutes");
    await approve(clientA, "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2", expected, "1 minute");
    expect(await audience(evilFirst, clientA)).toBe("authenticated");
    const raced = await db.query<{ n: number }>(
      `select count(*)::int as n from stetwise_private.oauth_resources where session_id = ${slot(1)} and strict_match`,
      [evilFirst]
    );
    expect(raced.rows[0]?.n).toBe(0);

    await db.query(`delete from auth.oauth_authorizations where client_id = ${slot(1)}`, [clientA]);
    await approve(clientA, "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb3", expected, "2 minutes");
    await approve(clientA, "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb4", "https://evil.example/mcp", "1 minute");
    expect(await audience(realFirst, clientA)).toBe("authenticated");

    await db.query(`delete from auth.oauth_authorizations where client_id = ${slot(1)}`, [clientA]);
    await approve(clientA, "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb5", expected, "0 minutes");
    expect(await audience(alone, clientA)).toEqual(["authenticated", expected]);
    const seeded = await db.query<{ strict_match: boolean; resource: string }>(
      `select strict_match, resource from stetwise_private.oauth_resources where session_id = ${slot(1)}`,
      [alone]
    );
    expect(seeded.rows[0]).toEqual({ strict_match: true, resource: expected });
    await db.query(`delete from auth.oauth_authorizations where client_id = ${slot(1)}`, [clientA]);
    expect(await audience(alone, clientA)).toEqual(["authenticated", expected]);

    await db.query(`delete from auth.oauth_authorizations where client_id = ${slot(1)}`, [clientA]);
    await approve(clientA, "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb6", expected, "1 minute");
    await approve(clientA, "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb7", null, "0 minutes");
    expect(await audience(withNull, clientA)).toBe("authenticated");
  });

  it("documents the residual: a no-resource code binds once a matching approval is the only open one", async () => {
    // Supabase does not attach the exchanged code to an authorization row. While a
    // no-resource approval is still open, every-row matching refuses the bind. After
    // that row leaves the window, a matching approval for the same client and user
    // (about 10 minutes) is the only open row, and this session binds. Low risk: that
    // user already approved this client for this exact server.
    const sessionId = "55555555-5555-4555-8555-555555555575";
    const nullId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb8";
    await db.query(`delete from auth.oauth_authorizations where client_id = ${slot(1)}`, [clientA]);
    await approve(clientA, "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb9", expected, "1 minute");
    await approve(clientA, nullId, null, "0 minutes");
    expect(await audience(sessionId, clientA)).toBe("authenticated");

    await db.query(`update auth.oauth_authorizations set expires_at = now() - interval '1 minute' where id = ${slot(1)}`, [nullId]);
    const later = "55555555-5555-4555-8555-555555555576";
    expect(await audience(later, clientA)).toEqual(["authenticated", expected]);
  });

  it("keeps execute limited to supabase_auth_admin and an empty search_path", async () => {
    const privileges = await db.query<{
      anon_exec: boolean;
      auth_exec: boolean;
      public_exec: boolean;
      admin_exec: boolean;
      admin_select: boolean;
      config: string[] | null;
    }>(`
      select has_function_privilege('anon', 'public.stetwise_access_token_hook(jsonb)', 'execute') as anon_exec,
             has_function_privilege('authenticated', 'public.stetwise_access_token_hook(jsonb)', 'execute') as auth_exec,
             has_function_privilege('public', 'public.stetwise_access_token_hook(jsonb)', 'execute') as public_exec,
             has_function_privilege('supabase_auth_admin', 'public.stetwise_access_token_hook(jsonb)', 'execute') as admin_exec,
             has_table_privilege('supabase_auth_admin', 'auth.oauth_authorizations', 'select') as admin_select,
             p.proconfig as config
        from pg_proc p
        join pg_namespace n on n.oid = p.pronamespace
       where n.nspname = 'public' and p.proname = 'stetwise_access_token_hook'
    `);
    const row = privileges.rows[0];
    expect(row?.anon_exec).toBe(false);
    expect(row?.auth_exec).toBe(false);
    expect(row?.public_exec).toBe(false);
    expect(row?.admin_exec).toBe(true);
    expect(row?.admin_select).toBe(true);
    expect(row?.config?.some((item) => item.startsWith("search_path="))).toBe(true);
    expect(row?.config?.join(" ")).not.toContain("public");
    const setting = await db.query<{ value: string }>("select value from stetwise_private.settings where key = 'mcp_resource'");
    expect(setting.rows[0]?.value).toBe(expected);
    await db.exec(hookSql);
    const kept = await db.query<{ value: string }>("select value from stetwise_private.settings where key = 'mcp_resource'");
    expect(kept.rows[0]?.value).toBe(expected);
  });
});

function jwt(claims: Record<string, unknown>) {
  const header = Buffer.from(JSON.stringify({ alg: "none", typ: "JWT" })).toString("base64url");
  const body = Buffer.from(JSON.stringify(claims)).toString("base64url");
  return `${header}.${body}.sig`;
}

describe("/mcp audience", () => {
  let mcp: Server;
  let auth: Server;
  let base = "";
  let issuer = "";
  const previous = {
    app: process.env.APP_BASE_URL,
    supabase: process.env.SUPABASE_URL,
    anon: process.env.SUPABASE_ANON_KEY
  };
  const account = "22222222-2222-4222-8222-222222222222";

  beforeAll(async () => {
    auth = http.createServer((req, res) => {
      if (req.url?.startsWith("/auth/v1/user")) {
        res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify({ id: account, email: "owner@stetwise.example" }));
        return;
      }
      res.writeHead(404).end("{}");
    });
    auth.listen(0, "127.0.0.1");
    await once(auth, "listening");
    const authAddress = auth.address();
    const authPort = typeof authAddress === "object" && authAddress ? authAddress.port : 0;
    issuer = `http://127.0.0.1:${authPort}/auth/v1`;
    process.env.SUPABASE_URL = `http://127.0.0.1:${authPort}`;
    process.env.SUPABASE_ANON_KEY = "test-anon";

    mcp = app.listen(0);
    await once(mcp, "listening");
    const address = mcp.address();
    const port = typeof address === "object" && address ? address.port : 0;
    base = `http://127.0.0.1:${port}`;
    process.env.APP_BASE_URL = base;
  });

  afterAll(async () => {
    process.env.APP_BASE_URL = previous.app;
    if (previous.supabase === undefined) delete process.env.SUPABASE_URL;
    else process.env.SUPABASE_URL = previous.supabase;
    if (previous.anon === undefined) delete process.env.SUPABASE_ANON_KEY;
    else process.env.SUPABASE_ANON_KEY = previous.anon;
    await new Promise<void>((resolve) => mcp.close(() => resolve()));
    await new Promise<void>((resolve) => auth.close(() => resolve()));
  });

  function claims(aud: unknown, clientId: string | null = clientA) {
    const body: Record<string, unknown> = {
      sub: account,
      iss: issuer,
      aud,
      role: "authenticated",
      exp: Math.floor(Date.now() / 1000) + 600,
      session_id: "66666666-6666-4666-8666-666666666666",
      scope: "email"
    };
    if (clientId) body.client_id = clientId;
    return body;
  }

  async function call(token: string) {
    const response = await fetch(`${base}/mcp`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        accept: "application/json, text/event-stream",
        authorization: `Bearer ${token}`
      },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "get_usage", arguments: {} } })
    });
    return { status: response.status, text: await response.text() };
  }

  it("accepts a token bound to this MCP URL and rejects the others", async () => {
    const resource = `${base}/mcp`;
    const bound = claims(["authenticated", resource]);
    expect(() => validateMcpClaims(jwt(bound), account, issuer, resource)).not.toThrow();
    expect(() => validateMcpClaims(jwt(claims("authenticated")), account, issuer, resource)).toThrow(/Reconnect/);
    expect(() => validateMcpClaims(jwt(claims(["authenticated", "https://evil.example/mcp"])), account, issuer, resource)).toThrow(/Reconnect/);

    const accepted = await call(jwt(bound));
    expect(accepted.status, accepted.text).toBe(200);
    expect(accepted.text).toContain("reads");

    const unbound = await call(jwt(claims("authenticated")));
    expect(unbound.status).toBe(401);

    const evil = await call(jwt(claims(["authenticated", "https://evil.example/mcp"])));
    expect(evil.status).toBe(401);

    const slash = await call(jwt(claims(["authenticated", `${resource}/`])));
    expect(slash.status).toBe(401);

    const https = await call(jwt(claims(["authenticated", resource.replace("http://", "https://")])));
    expect(https.status).toBe(401);

    const firstParty = await call(jwt(claims("authenticated", null)));
    expect(firstParty.status).toBe(401);
  });
});
