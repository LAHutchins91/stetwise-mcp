-- Bind OAuth access tokens to this MCP resource. First-party session tokens are unchanged.
-- The resource is one setting, stetwise_private.settings.mcp_resource, the exact
-- APP_BASE_URL || '/mcp' string (https, no trailing slash, no query or fragment).
-- After applying this file:
--   update stetwise_private.settings set value = 'https://YOUR_HOST/mcp' where key = 'mcp_resource';
--   Dashboard: Authentication → Hooks → Custom Access Token → public.stetwise_access_token_hook
-- Re-applying is safe. The setting is not overwritten.

create schema if not exists stetwise_private;
revoke all on schema stetwise_private from public, anon, authenticated;
grant usage on schema stetwise_private to supabase_auth_admin;

create table if not exists stetwise_private.oauth_resources (
  session_id uuid primary key references auth.sessions (id) on delete cascade,
  resource text not null
);
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
  expected text;
  wanted text;
  requested text;
  got text;
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
    exception when others then
      return original;
    end;
    if sid is null then
      return original;
    end if;

    select r.resource into requested
      from stetwise_private.oauth_resources r
     where r.session_id = sid;
    if requested is not null and stetwise_private.normalize_mcp_resource(requested) is distinct from wanted then
      requested := null;
    end if;

    if requested is null then
      select a.resource into requested
        from auth.oauth_authorizations a
       where a.client_id = cid::uuid
         and a.user_id = nullif(claims->>'sub', '')::uuid
         and a.status::text = 'approved'
         and a.expires_at > now()
       order by a.created_at desc
       limit 1;
      got := stetwise_private.normalize_mcp_resource(requested);
      if got is not null and got = wanted then
        begin
          insert into stetwise_private.oauth_resources (session_id, resource)
          values (sid, expected)
          on conflict (session_id) do update set resource = excluded.resource;
        exception when others then
          null;
        end;
        requested := expected;
      else
        requested := null;
      end if;
    end if;

    if requested is not null and stetwise_private.normalize_mcp_resource(requested) = wanted then
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
grant execute on function public.stetwise_access_token_hook(jsonb) to supabase_auth_admin;
alter function public.stetwise_access_token_hook(jsonb) set search_path = '';
