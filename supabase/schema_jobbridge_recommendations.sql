create extension if not exists pgcrypto;
create schema if not exists jobbridge_private;

revoke all on schema jobbridge_private from public, anon, authenticated;
grant usage on schema jobbridge_private to service_role;

create table if not exists jobbridge_private.recommendation_requests (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  user_id uuid null references auth.users(id) on delete set null,
  client_session_id text null,
  request_payload jsonb not null,
  model_features jsonb not null,
  scoring_preferences jsonb not null default '{}'::jsonb,
  client_context jsonb not null default '{}'::jsonb,
  contract_version text not null default 'profile_contract_v1',
  model_version text not null,
  caller text not null default 'api'
    check (caller in ('lambda', 'lightsail', 'local', 'api')),
  status text not null default 'succeeded'
    check (status in ('started', 'succeeded', 'failed')),
  fallback_used boolean not null default false
);

create table if not exists jobbridge_private.recommendation_results (
  id uuid primary key default gen_random_uuid(),
  request_id uuid not null references jobbridge_private.recommendation_requests(id) on delete cascade,
  created_at timestamptz not null default now(),
  report_json jsonb not null,
  top_job_class text null,
  top_score integer null check (top_score between 0 and 100),
  latency_ms numeric null check (latency_ms >= 0),
  model_version text not null
);

create index if not exists recommendation_requests_created_at_idx
  on jobbridge_private.recommendation_requests (created_at desc);

create index if not exists recommendation_requests_user_id_idx
  on jobbridge_private.recommendation_requests (user_id, created_at desc);

create index if not exists recommendation_requests_model_idx
  on jobbridge_private.recommendation_requests (contract_version, model_version, created_at desc);

create index if not exists recommendation_results_request_id_idx
  on jobbridge_private.recommendation_results (request_id);

alter table jobbridge_private.recommendation_requests enable row level security;
alter table jobbridge_private.recommendation_results enable row level security;

-- No anon/authenticated policies are defined for MVP recommendation data.
-- The inference server writes through server-side service_role credentials only.
revoke all on jobbridge_private.recommendation_requests from anon, authenticated;
revoke all on jobbridge_private.recommendation_results from anon, authenticated;

grant insert, select on jobbridge_private.recommendation_requests to service_role;
grant insert, select on jobbridge_private.recommendation_results to service_role;

-- Optional future "my reports" policies:
-- 1. add owner_user_id uuid not null references auth.users(id)
-- 2. grant select to authenticated
-- 3. create select policies using owner_user_id = auth.uid()
