-- Stetwise by Ouroboros. Apply in the Supabase SQL editor when the project exists.
-- Caps are enforced by the application from environment variables.
-- There is no overage price and no metered usage item.

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
  owner_id uuid not null,
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

alter table public.profiles enable row level security;
alter table public.usage_monthly enable row level security;
alter table public.files enable row level security;
alter table public.file_versions enable row level security;
alter table public.proposals enable row level security;
alter table public.upload_nonces enable row level security;
alter table public.audit_events enable row level security;

create policy profiles_owner on public.profiles
  for select using (id = auth.uid());
create policy usage_owner on public.usage_monthly
  for select using (owner_id = auth.uid());
create policy files_owner on public.files
  for select using (owner_id = auth.uid());
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
