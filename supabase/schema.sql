-- Stetwise by Ouroboros. Apply in the Supabase SQL editor when the project exists.
-- This script does not create Stripe products or prices.
-- Caps are enforced by the application from environment variables and passed into the functions below.
-- There is no overage price and no metered usage item.
-- Schedule POST /internal/purge on the file host. It deletes rows past expires_at and unreferenced blobs.

create table if not exists public.profiles (
  id uuid primary key references auth.users (id) on delete cascade,
  email text,
  plan text not null default 'trial',
  subscription_status text not null default 'trialing',
  retention_hours integer not null default 24 check (retention_hours in (24, 168)),
  stripe_customer_id text,
  stripe_subscription_id text,
  current_period_end timestamptz,
  cancel_at_period_end boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.usage_monthly (
  owner_id uuid not null references public.profiles (id) on delete cascade,
  month text not null check (month ~ '^[0-9]{4}-[0-9]{2}$'),
  edits integer not null default 0 check (edits >= 0),
  files integer not null default 0 check (files >= 0),
  recalculations integer not null default 0 check (recalculations >= 0),
  primary key (owner_id, month)
);

create table if not exists public.files (
  id uuid primary key,
  owner_id uuid not null references public.profiles (id) on delete cascade,
  filename text not null,
  kind text not null,
  head_sha text not null,
  size integer not null check (size >= 0),
  created_at timestamptz not null default now(),
  last_used_at timestamptz not null default now(),
  expires_at timestamptz not null
);

create table if not exists public.file_versions (
  file_id uuid not null references public.files (id) on delete cascade,
  number integer not null check (number > 0),
  sha256 text not null,
  size integer not null check (size >= 0),
  op text not null check (op in ('import', 'apply', 'restore')),
  created_at timestamptz not null default now(),
  primary key (file_id, number)
);

create table if not exists public.proposals (
  id uuid primary key,
  owner_id uuid not null references public.profiles (id) on delete cascade,
  file_id uuid not null references public.files (id) on delete cascade,
  base_sha text not null,
  result_sha text not null,
  size integer not null,
  diff jsonb not null,
  warnings jsonb not null default '[]'::jsonb,
  verification jsonb not null,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,
  applied_at timestamptz
);

create table if not exists public.upload_nonces (
  nonce text primary key,
  owner_id uuid not null references public.profiles (id) on delete cascade,
  file_id uuid not null,
  filename text not null,
  exp timestamptz not null
);

create table if not exists public.audit_events (
  id bigint generated always as identity primary key,
  owner_id uuid,
  op text not null,
  file_id uuid,
  sha256 text,
  size integer,
  code text,
  created_at timestamptz not null default now()
);

create table if not exists public.support_requests (
  id uuid primary key,
  email text not null,
  message text not null,
  created_at timestamptz not null default now()
);

alter table public.audit_events alter column owner_id drop not null;

create index if not exists files_expires_at_idx on public.files (expires_at);
create index if not exists proposals_expires_at_idx on public.proposals (expires_at);
create index if not exists upload_nonces_exp_idx on public.upload_nonces (exp);

alter table public.profiles enable row level security;
alter table public.usage_monthly enable row level security;
alter table public.files enable row level security;
alter table public.file_versions enable row level security;
alter table public.proposals enable row level security;
alter table public.upload_nonces enable row level security;
alter table public.audit_events enable row level security;

drop policy if exists profiles_owner on public.profiles;
create policy profiles_owner on public.profiles
  for select using (id = auth.uid());
drop policy if exists usage_owner on public.usage_monthly;
create policy usage_owner on public.usage_monthly
  for select using (owner_id = auth.uid());
drop policy if exists files_owner on public.files;
create policy files_owner on public.files
  for select using (owner_id = auth.uid());
drop policy if exists versions_owner on public.file_versions;
create policy versions_owner on public.file_versions
  for select using (exists (
    select 1 from public.files where files.id = file_versions.file_id and files.owner_id = auth.uid()
  ));

insert into storage.buckets (id, name, public)
values ('stetwise-files', 'stetwise-files', false)
on conflict (id) do nothing;

-- Blobs live at {user_id}/{sha256} in the private stetwise-files bucket.
-- The service role writes them. Clients receive only signed download links from the MCP server.
-- A scheduled call to POST /internal/purge deletes rows past expires_at and unreferenced blobs.
-- Default expires_at is last_used_at plus 24 hours. retention_hours may be 168.

create or replace function public.stetwise_lock_profile(p_owner uuid)
returns public.profiles
language plpgsql
security definer
set search_path = public
as $$
declare
  found_profile public.profiles;
begin
  select * into found_profile from public.profiles where id = p_owner for update;
  if not found then
    raise exception 'stetwise:not_found:account' using errcode = 'P0001';
  end if;
  if found_profile.subscription_status not in ('active', 'trialing') then
    raise exception 'stetwise:access' using errcode = 'P0001';
  end if;
  return found_profile;
end;
$$;

create or replace function public.stetwise_bump(
  p_owner uuid,
  p_month text,
  p_metric text,
  p_cap integer,
  p_tier text
) returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
  used integer;
begin
  insert into public.usage_monthly (owner_id, month)
  values (p_owner, p_month)
  on conflict (owner_id, month) do nothing;

  if p_metric = 'edits' then
    select edits into used from public.usage_monthly where owner_id = p_owner and month = p_month for update;
    if used >= p_cap then
      raise exception 'stetwise:usage_cap:edits:%:%:%', used, p_cap, p_tier using errcode = 'P0001';
    end if;
    update public.usage_monthly set edits = edits + 1 where owner_id = p_owner and month = p_month returning edits into used;
  elsif p_metric = 'files' then
    select files into used from public.usage_monthly where owner_id = p_owner and month = p_month for update;
    if used >= p_cap then
      raise exception 'stetwise:usage_cap:files:%:%:%', used, p_cap, p_tier using errcode = 'P0001';
    end if;
    update public.usage_monthly set files = files + 1 where owner_id = p_owner and month = p_month returning files into used;
  elsif p_metric = 'recalculations' then
    select recalculations into used from public.usage_monthly where owner_id = p_owner and month = p_month for update;
    if used >= p_cap then
      raise exception 'stetwise:usage_cap:recalculations:%:%:%', used, p_cap, p_tier using errcode = 'P0001';
    end if;
    update public.usage_monthly
      set recalculations = recalculations + 1
      where owner_id = p_owner and month = p_month
      returning recalculations into used;
  else
    raise exception 'stetwise:bad_metric' using errcode = 'P0001';
  end if;
  return used;
end;
$$;

create or replace function public.stetwise_reserve_upload(
  p_owner uuid,
  p_file_id uuid,
  p_nonce text,
  p_filename text,
  p_exp timestamptz,
  p_month text,
  p_files_cap integer,
  p_tier text
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  used integer;
begin
  perform public.stetwise_lock_profile(p_owner);
  insert into public.usage_monthly (owner_id, month)
  values (p_owner, p_month)
  on conflict (owner_id, month) do nothing;
  select usage_monthly.files into used from public.usage_monthly where owner_id = p_owner and month = p_month for update;
  if used >= p_files_cap then
    raise exception 'stetwise:usage_cap:files:%:%:%', used, p_files_cap, p_tier using errcode = 'P0001';
  end if;
  insert into public.upload_nonces (nonce, owner_id, file_id, filename, exp)
  values (p_nonce, p_owner, p_file_id, p_filename, p_exp);
  return jsonb_build_object('file_id', p_file_id, 'nonce', p_nonce);
end;
$$;

create or replace function public.stetwise_commit_file(
  p_owner uuid,
  p_file_id uuid,
  p_filename text,
  p_kind text,
  p_sha text,
  p_size integer,
  p_nonce text,
  p_month text,
  p_files_cap integer,
  p_max_bytes integer,
  p_tier text,
  p_now timestamptz
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  profile public.profiles;
  nonce_row public.upload_nonces;
  file_id uuid;
  filename text;
  expires_at timestamptz;
begin
  if p_sha !~ '^[a-f0-9]{64}$' then
    raise exception 'stetwise:bad_hash' using errcode = 'P0001';
  end if;
  if p_size > p_max_bytes then
    raise exception 'stetwise:file_too_large:%:%:%', p_size, p_max_bytes, p_tier using errcode = 'P0001';
  end if;
  profile := public.stetwise_lock_profile(p_owner);
  file_id := p_file_id;
  filename := p_filename;
  if p_nonce is not null and length(p_nonce) > 0 then
    select * into nonce_row from public.upload_nonces where nonce = p_nonce for update;
    if not found or nonce_row.owner_id <> p_owner or nonce_row.exp <= p_now then
      raise exception 'stetwise:expired_link' using errcode = 'P0001';
    end if;
    file_id := nonce_row.file_id;
    filename := nonce_row.filename;
  end if;
  perform public.stetwise_bump(p_owner, p_month, 'files', p_files_cap, p_tier);
  if p_nonce is not null and length(p_nonce) > 0 then
    delete from public.upload_nonces where nonce = p_nonce;
  end if;
  expires_at := p_now + make_interval(hours => profile.retention_hours);
  insert into public.files (id, owner_id, filename, kind, head_sha, size, created_at, last_used_at, expires_at)
  values (file_id, p_owner, filename, p_kind, p_sha, p_size, p_now, p_now, expires_at);
  insert into public.file_versions (file_id, number, sha256, size, op, created_at)
  values (file_id, 1, p_sha, p_size, 'import', p_now);
  insert into public.audit_events (owner_id, op, file_id, sha256, size, code)
  values (p_owner, 'import', file_id, p_sha, p_size, 'ok');
  return jsonb_build_object(
    'file_id', file_id,
    'filename', filename,
    'kind', p_kind,
    'size', p_size,
    'sha256', p_sha,
    'version', 1,
    'created_at', p_now,
    'last_used_at', p_now,
    'expires_at', expires_at
  );
end;
$$;

create or replace function public.stetwise_save_proposal(
  p_id uuid,
  p_owner uuid,
  p_file_id uuid,
  p_base_sha text,
  p_result_sha text,
  p_size integer,
  p_diff jsonb,
  p_warnings jsonb,
  p_verification jsonb,
  p_now timestamptz
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  profile public.profiles;
  target public.files;
  expires_at timestamptz;
begin
  profile := public.stetwise_lock_profile(p_owner);
  select * into target from public.files where id = p_file_id for update;
  if not found or target.owner_id <> p_owner then
    raise exception 'stetwise:not_found:file' using errcode = 'P0001';
  end if;
  if target.expires_at <= p_now then
    raise exception 'stetwise:expired_file' using errcode = 'P0001';
  end if;
  if target.head_sha <> p_base_sha then
    raise exception 'stetwise:stale_version:propose' using errcode = 'P0001';
  end if;
  target.last_used_at := p_now;
  target.expires_at := p_now + make_interval(hours => profile.retention_hours);
  update public.files
    set last_used_at = target.last_used_at, expires_at = target.expires_at
    where id = target.id;
  expires_at := p_now + interval '30 minutes';
  insert into public.proposals (
    id, owner_id, file_id, base_sha, result_sha, size, diff, warnings, verification, created_at, expires_at
  ) values (
    p_id, p_owner, target.id, p_base_sha, p_result_sha, p_size, p_diff, coalesce(p_warnings, '[]'::jsonb), p_verification, p_now, expires_at
  );
  return jsonb_build_object(
    'id', p_id,
    'owner_id', p_owner,
    'file_id', target.id,
    'base_sha', p_base_sha,
    'result_sha', p_result_sha,
    'size', p_size,
    'diff', p_diff,
    'warnings', coalesce(p_warnings, '[]'::jsonb),
    'verification', p_verification,
    'created_at', p_now,
    'expires_at', expires_at,
    'applied_at', null
  );
end;
$$;

create or replace function public.stetwise_publish_edit(
  p_owner uuid,
  p_proposal uuid,
  p_base text,
  p_month text,
  p_edits_cap integer,
  p_tier text,
  p_now timestamptz
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  profile public.profiles;
  proposal public.proposals;
  target public.files;
  version_number integer;
  used_edits integer;
  used_files integer;
  used_recalculations integer;
begin
  profile := public.stetwise_lock_profile(p_owner);
  select * into proposal from public.proposals where id = p_proposal for update;
  if not found or proposal.owner_id <> p_owner then
    raise exception 'stetwise:not_found:proposal' using errcode = 'P0001';
  end if;
  select * into target from public.files where id = proposal.file_id for update;
  if not found or target.owner_id <> p_owner then
    raise exception 'stetwise:not_found:file' using errcode = 'P0001';
  end if;
  if target.expires_at <= p_now then
    raise exception 'stetwise:expired_file' using errcode = 'P0001';
  end if;
  if proposal.base_sha <> p_base then
    raise exception 'stetwise:stale_version:proposal' using errcode = 'P0001';
  end if;
  select coalesce(max(number), 0) into version_number from public.file_versions where file_id = target.id;
  if proposal.applied_at is not null and target.head_sha = proposal.result_sha then
    select u.edits, u.files, u.recalculations into used_edits, used_files, used_recalculations
      from public.usage_monthly u where u.owner_id = p_owner and u.month = p_month;
    return jsonb_build_object(
      'file_id', target.id,
      'version', version_number,
      'sha256', target.head_sha,
      'size', target.size,
      'idempotent', true,
      'edits', coalesce(used_edits, 0),
      'files', coalesce(used_files, 0),
      'recalculations', coalesce(used_recalculations, 0),
      'retention_hours', profile.retention_hours
    );
  end if;
  if proposal.expires_at <= p_now then
    raise exception 'stetwise:expired' using errcode = 'P0001';
  end if;
  if target.head_sha <> proposal.base_sha then
    raise exception 'stetwise:stale_version:current' using errcode = 'P0001';
  end if;
  perform public.stetwise_bump(p_owner, p_month, 'edits', p_edits_cap, p_tier);
  version_number := version_number + 1;
  insert into public.file_versions (file_id, number, sha256, size, op, created_at)
  values (target.id, version_number, proposal.result_sha, proposal.size, 'apply', p_now);
  update public.files
    set head_sha = proposal.result_sha,
        size = proposal.size,
        last_used_at = p_now,
        expires_at = p_now + make_interval(hours => profile.retention_hours)
    where id = target.id;
  update public.proposals set applied_at = p_now where id = proposal.id;
  select u.edits, u.files, u.recalculations into used_edits, used_files, used_recalculations
    from public.usage_monthly u where u.owner_id = p_owner and u.month = p_month;
  insert into public.audit_events (owner_id, op, file_id, sha256, size, code)
  values (p_owner, 'apply_edit', target.id, proposal.result_sha, proposal.size, 'ok');
  return jsonb_build_object(
    'file_id', target.id,
    'version', version_number,
    'sha256', proposal.result_sha,
    'size', proposal.size,
    'idempotent', false,
    'edits', used_edits,
    'files', used_files,
    'recalculations', used_recalculations,
    'retention_hours', profile.retention_hours
  );
end;
$$;

create or replace function public.stetwise_restore(
  p_owner uuid,
  p_file_id uuid,
  p_version integer,
  p_now timestamptz
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  profile public.profiles;
  target public.files;
  source public.file_versions;
  version_number integer;
  used_edits integer;
  used_files integer;
  used_recalculations integer;
  month_key text;
begin
  profile := public.stetwise_lock_profile(p_owner);
  select * into target from public.files where id = p_file_id for update;
  if not found or target.owner_id <> p_owner then
    raise exception 'stetwise:not_found:file' using errcode = 'P0001';
  end if;
  if target.expires_at <= p_now then
    raise exception 'stetwise:expired_file' using errcode = 'P0001';
  end if;
  select * into source from public.file_versions where file_id = target.id and number = p_version;
  if not found then
    raise exception 'stetwise:not_found:version' using errcode = 'P0001';
  end if;
  select coalesce(max(number), 0) + 1 into version_number from public.file_versions where file_id = target.id;
  insert into public.file_versions (file_id, number, sha256, size, op, created_at)
  values (target.id, version_number, source.sha256, source.size, 'restore', p_now);
  update public.files
    set head_sha = source.sha256,
        size = source.size,
        last_used_at = p_now,
        expires_at = p_now + make_interval(hours => profile.retention_hours)
    where id = target.id;
  month_key := to_char(p_now at time zone 'utc', 'YYYY-MM');
  select u.edits, u.files, u.recalculations into used_edits, used_files, used_recalculations
    from public.usage_monthly u where u.owner_id = p_owner and u.month = month_key;
  insert into public.audit_events (owner_id, op, file_id, sha256, size, code)
  values (p_owner, 'restore_version', target.id, source.sha256, source.size, 'ok');
  return jsonb_build_object(
    'file_id', target.id,
    'version', version_number,
    'sha256', source.sha256,
    'size', source.size,
    'idempotent', false,
    'edits', coalesce(used_edits, 0),
    'files', coalesce(used_files, 0),
    'recalculations', coalesce(used_recalculations, 0),
    'retention_hours', profile.retention_hours
  );
end;
$$;

create or replace function public.stetwise_record_recalculation(
  p_owner uuid,
  p_month text,
  p_cap integer,
  p_tier text
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  used_edits integer;
  used_files integer;
  used_recalculations integer;
begin
  perform public.stetwise_lock_profile(p_owner);
  perform public.stetwise_bump(p_owner, p_month, 'recalculations', p_cap, p_tier);
  select u.edits, u.files, u.recalculations into used_edits, used_files, used_recalculations
    from public.usage_monthly u where u.owner_id = p_owner and u.month = p_month;
  return jsonb_build_object('edits', used_edits, 'files', used_files, 'recalculations', used_recalculations);
end;
$$;

create or replace function public.stetwise_set_retention(p_owner uuid, p_hours integer)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  profile public.profiles;
begin
  if p_hours not in (24, 168) then
    raise exception 'stetwise:bad_retention' using errcode = 'P0001';
  end if;
  profile := public.stetwise_lock_profile(p_owner);
  update public.profiles set retention_hours = p_hours, updated_at = now() where id = p_owner;
  update public.files
    set expires_at = last_used_at + make_interval(hours => p_hours)
    where owner_id = p_owner;
  profile.retention_hours := p_hours;
  return jsonb_build_object(
    'id', profile.id,
    'email', profile.email,
    'plan', profile.plan,
    'subscription_status', profile.subscription_status,
    'retention_hours', profile.retention_hours,
    'stripe_customer_id', profile.stripe_customer_id,
    'stripe_subscription_id', profile.stripe_subscription_id,
    'current_period_end', profile.current_period_end,
    'cancel_at_period_end', profile.cancel_at_period_end
  );
end;
$$;

create or replace function public.stetwise_purge(p_now timestamptz)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  file_count integer;
  dropped jsonb;
  kept jsonb;
begin
  drop table if exists pg_temp.stetwise_purge_going;
  drop table if exists pg_temp.stetwise_purge_drop;
  create temp table stetwise_purge_going (file_id uuid primary key, owner_id uuid);
  create temp table stetwise_purge_drop (owner_id uuid, sha256 text, primary key (owner_id, sha256));

  insert into stetwise_purge_going (file_id, owner_id)
  select id, owner_id from public.files where expires_at <= p_now;

  insert into stetwise_purge_drop (owner_id, sha256)
  select going.owner_id, versions.sha256
    from public.file_versions versions
    join stetwise_purge_going going on going.file_id = versions.file_id
  on conflict do nothing;

  insert into stetwise_purge_drop (owner_id, sha256)
  select proposals.owner_id, proposals.result_sha
    from public.proposals proposals
    where proposals.file_id in (select file_id from stetwise_purge_going)
       or proposals.expires_at <= p_now
  on conflict do nothing;

  delete from public.files where id in (select file_id from stetwise_purge_going);
  get diagnostics file_count = row_count;
  delete from public.proposals where expires_at <= p_now;
  delete from public.upload_nonces where exp <= p_now;

  delete from stetwise_purge_drop candidate
  where exists (
    select 1
      from public.file_versions versions
      join public.files live on live.id = versions.file_id
      where live.owner_id = candidate.owner_id and versions.sha256 = candidate.sha256
  ) or exists (
    select 1 from public.proposals proposals
    where proposals.owner_id = candidate.owner_id
      and proposals.result_sha = candidate.sha256
      and proposals.expires_at > p_now
  );

  select coalesce(jsonb_agg(jsonb_build_object('owner_id', owner_id, 'sha256', sha256)), '[]'::jsonb)
    into dropped from stetwise_purge_drop;
  select coalesce(jsonb_agg(jsonb_build_object('owner_id', owner_id, 'sha256', sha256)), '[]'::jsonb)
    into kept
    from (
      select distinct live.owner_id, versions.sha256
        from public.file_versions versions
        join public.files live on live.id = versions.file_id
      union
      select proposals.owner_id, proposals.result_sha
        from public.proposals proposals
        where proposals.expires_at > p_now
    ) referenced;

  return jsonb_build_object('files', file_count, 'drop', dropped, 'keep', kept);
end;
$$;

do $$
declare
  fn regprocedure;
begin
  for fn in
    select p.oid::regprocedure
    from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname like 'stetwise_%'
  loop
    execute format('revoke all on function %s from public', fn);
    execute format('revoke all on function %s from anon, authenticated', fn);
    execute format('grant execute on function %s to service_role', fn);
  end loop;
end $$;
