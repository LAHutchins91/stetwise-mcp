-- Bind OAuth access tokens to this MCP resource. First-party session tokens are unchanged.
-- The resource is one setting, stetwise_private.settings.mcp_resource, the exact
-- APP_BASE_URL || '/mcp' string (https, no trailing slash, no query or fragment).
-- After applying this file:
--   update stetwise_private.settings set value = 'https://YOUR_HOST/mcp' where key = 'mcp_resource';
--   Dashboard: Authentication → Hooks → Custom Access Token → public.stetwise_access_token_hook
-- Re-applying is safe. The setting is not overwritten.
-- Apply this file after supabase/schema.sql. schema.sql skips this hook; this file
-- is still last so its revoke of service_role is the final grant state.
--
-- Supabase does not attach a session to the authorization it came from. Bind only when
-- at least one approved, unexpired authorization for this client_id and user exists and
-- every one of those rows normalizes to the configured URL. Another resource, or a null
-- resource, leaves aud as "authenticated". There is no newest-row shortcut.
-- A session row is trusted on refresh only when this rule wrote it (strict_match).
-- Rows written by the previous newest-authorization hook are re-checked.

create schema if not exists stetwise_private;
revoke all on schema stetwise_private from public, anon, authenticated;
grant usage on schema stetwise_private to supabase_auth_admin;

create table if not exists stetwise_private.oauth_resources (
  session_id uuid primary key references auth.sessions (id) on delete cascade,
  resource text not null,
  strict_match boolean not null default false
);
alter table stetwise_private.oauth_resources
  add column if not exists strict_match boolean not null default false;
revoke all on table stetwise_private.oauth_resources from public, anon, authenticated;
grant select, insert, update on table stetwise_private.oauth_resources to supabase_auth_admin;

create table if not exists stetwise_private.settings (
  key text primary key,
  value text not null
);
revoke all on table stetwise_private.settings from public, anon, authenticated;
grant select on table stetwise_private.settings to supabase_auth_admin;

insert into stetwise_private.settings (key, value)
values ('mcp_resource', 'http://localhost:3000/mcp')
on conflict (key) do nothing;

-- The hook runs as supabase_auth_admin and reads auth.oauth_authorizations.
-- A missing select fails the lookup, the hook returns the original event, and
-- every OAuth token stays aud "authenticated", so /mcp rejects it.
grant usage on schema auth to supabase_auth_admin;
grant select on table auth.oauth_authorizations to supabase_auth_admin;

-- Lowercase the scheme and host only. A trailing slash, query, fragment, or
-- different path stays different so it cannot match the configured URL.
create or replace function stetwise_private.normalize_mcp_resource(raw text)
returns text
language plpgsql
immutable
security invoker
set search_path = ''
as $norm$
declare
  matched text[];
  scheme text;
  host text;
  port text;
  path text;
begin
  if raw is null or raw = '' then
    return null;
  end if;
  if strpos(raw, '?') > 0 or strpos(raw, '#') > 0 or strpos(raw, '@') > 0 or strpos(raw, chr(92)) > 0 then
    return null;
  end if;
  matched := regexp_match(raw, '^([a-zA-Z][a-zA-Z0-9+.-]*)://([a-zA-Z0-9.-]+|\[[0-9a-fA-F:.]+\])(:[0-9]{1,5})?(/[^[:space:]?#]*)$');
  if matched is null then
    return null;
  end if;
  scheme := lower(matched[1]);
  host := lower(matched[2]);
  port := coalesce(matched[3], '');
  path := matched[4];
  if scheme not in ('http', 'https') then
    return null;
  end if;
  if host = '' or host like '.%' or host like '%.' or strpos(host, '..') > 0 then
    return null;
  end if;
  if path is null or path = '' or left(path, 1) <> '/' then
    return null;
  end if;
  return scheme || '://' || host || port || path;
end
$norm$;

revoke all on function stetwise_private.normalize_mcp_resource(text) from public, anon, authenticated;
revoke execute on function stetwise_private.normalize_mcp_resource(text) from service_role;
grant execute on function stetwise_private.normalize_mcp_resource(text) to supabase_auth_admin;
alter function stetwise_private.normalize_mcp_resource(text) set search_path = '';

create or replace function public.stetwise_access_token_hook(event jsonb)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $fn$
declare
  original jsonb := event;
  claims jsonb := coalesce(event->'claims', '{}'::jsonb);
  cid text := claims->>'client_id';
  sid uuid;
  uid uuid;
  expected text;
  wanted text;
  trusted text;
  open_count int;
  bad_count int;
begin
  if cid is null or btrim(cid) = '' then
    return original;
  end if;

  begin
    select s.value into expected
      from stetwise_private.settings s
     where s.key = 'mcp_resource';
    wanted := stetwise_private.normalize_mcp_resource(expected);
    if expected is null or btrim(expected) = '' or wanted is null then
      return original;
    end if;

    begin
      sid := nullif(btrim(claims->>'session_id'), '')::uuid;
      uid := nullif(btrim(claims->>'sub'), '')::uuid;
    exception when others then
      return original;
    end;
    if sid is null or uid is null then
      return original;
    end if;

    -- Refresh trusts only a row this rule wrote, and only while it still matches the setting.
    select r.resource into trusted
      from stetwise_private.oauth_resources r
     where r.session_id = sid
       and r.strict_match
       and stetwise_private.normalize_mcp_resource(r.resource) = wanted;

    if trusted is null then
      select count(*)::int,
             count(*) filter (
               where stetwise_private.normalize_mcp_resource(a.resource) is distinct from wanted
             )::int
        into open_count, bad_count
        from auth.oauth_authorizations a
       where a.client_id = cid::uuid
         and a.user_id = uid
         and a.status::text = 'approved'
         and a.expires_at > now();

      if open_count > 0 and bad_count = 0 then
        -- Persist the row before binding. A failed insert returns the original event
        -- below, so this token stays unbound instead of binding once and failing refresh
        -- after the approvals expire.
        insert into stetwise_private.oauth_resources (session_id, resource, strict_match)
        values (sid, expected, true)
        on conflict (session_id) do update
          set resource = excluded.resource,
              strict_match = true;
        trusted := expected;
      end if;
    end if;

    if trusted is not null then
      claims := jsonb_set(claims, '{aud}', jsonb_build_array('authenticated', expected));
      return jsonb_set(original, '{claims}', claims);
    end if;
    return original;
  exception when others then
    return original;
  end;
end
$fn$;

revoke all on function public.stetwise_access_token_hook(jsonb) from public, anon, authenticated;
revoke execute on function public.stetwise_access_token_hook(jsonb) from service_role;
grant execute on function public.stetwise_access_token_hook(jsonb) to supabase_auth_admin;
alter function public.stetwise_access_token_hook(jsonb) set search_path = '';
