create schema if not exists jobbridge_private;

revoke all on schema jobbridge_private from public, anon, authenticated;
grant usage on schema jobbridge_private to service_role;

create table if not exists jobbridge_private.job_postings_live (
  id bigserial primary key,
  posting_id text not null,
  source_system text not null default 'kead',
  source_dataset_id text not null default '15117692',
  source_endpoint text not null
    check (source_endpoint in ('job_list_env', 'job_list')),
  source_endpoints jsonb not null default '[]'::jsonb
    check (jsonb_typeof(source_endpoints) = 'array'),
  source_posting_key text not null,
  rno text null,
  posting_date date null,
  offer_registered_date date null,
  registered_date date null,
  recruit_period_raw text null,
  recruit_start date null,
  recruit_end date null,
  company_name text null,
  job_title text null,
  employment_type text null,
  entry_type text null,
  wage_type text null,
  wage_raw text null,
  wage_amount numeric null,
  required_career text null,
  required_education text null,
  address_raw text null,
  sido text null,
  sigungu text null,
  target_job_class_candidate text null,
  job_class_mapping_method text null,
  reference_large text null,
  reference_mid text null,
  reference_small text null,
  agency_name text null,
  contact_phone text null,
  env_both_hands text null,
  env_eyesight text null,
  env_handwork text null,
  env_lift_power text null,
  env_lstn_talk text null,
  env_stnd_walk text null,
  has_environment_detail boolean not null default false,
  raw_payload jsonb not null,
  payload_hash text not null,
  is_active boolean not null default true,
  fetched_at timestamptz not null,
  last_seen_at timestamptz not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint job_postings_live_source_posting_key_key unique (source_posting_key)
);

create index if not exists job_postings_live_offer_registered_date_idx
  on jobbridge_private.job_postings_live (offer_registered_date desc);

create index if not exists job_postings_live_active_recruit_end_idx
  on jobbridge_private.job_postings_live (is_active, recruit_end);

create index if not exists job_postings_live_region_idx
  on jobbridge_private.job_postings_live (sido, sigungu);

create index if not exists job_postings_live_target_class_idx
  on jobbridge_private.job_postings_live (target_job_class_candidate);

create index if not exists job_postings_live_job_title_idx
  on jobbridge_private.job_postings_live (job_title);

create index if not exists job_postings_live_last_seen_at_idx
  on jobbridge_private.job_postings_live (last_seen_at desc);

create index if not exists job_postings_live_raw_payload_gin_idx
  on jobbridge_private.job_postings_live using gin (raw_payload);

create or replace function jobbridge_private.set_job_postings_live_updated_at()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists set_job_postings_live_updated_at
  on jobbridge_private.job_postings_live;

create trigger set_job_postings_live_updated_at
before update on jobbridge_private.job_postings_live
for each row
execute function jobbridge_private.set_job_postings_live_updated_at();

alter table jobbridge_private.job_postings_live enable row level security;

revoke all on jobbridge_private.job_postings_live from anon, authenticated;
grant select, insert, update on jobbridge_private.job_postings_live to service_role;
grant usage, select on sequence jobbridge_private.job_postings_live_id_seq to service_role;

comment on table jobbridge_private.job_postings_live is
  'KEAD realtime job postings collected from job_list_env and job_list. Upsert conflict key is source_posting_key.';

comment on column jobbridge_private.job_postings_live.source_posting_key is
  'Dedupe key: sha256 over stable posting fields. rno/rnum are endpoint row numbers and are not used as identity.';

comment on column jobbridge_private.job_postings_live.source_endpoints is
  'JSON array of KEAD endpoints where the posting appeared. job_list_env values are preferred when duplicates are merged.';

notify pgrst, 'reload schema';
