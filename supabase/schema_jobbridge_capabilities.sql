create schema if not exists jobbridge_private;

revoke all on schema jobbridge_private from public, anon, authenticated;
grant usage on schema jobbridge_private to service_role;

create table if not exists jobbridge_private.capability_categories (
  id text primary key,
  label text not null,
  summary text not null default '',
  target_job_class text not null default '',
  sort_order integer not null default 0,
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint capability_categories_id_check
    check (id ~ '^[a-z0-9][a-z0-9_-]*$')
);

create table if not exists jobbridge_private.capability_groups (
  id text primary key,
  category_id text not null references jobbridge_private.capability_categories(id) on delete cascade,
  label text not null,
  summary text not null default '',
  sort_order integer not null default 0,
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint capability_groups_id_check
    check (id ~ '^[a-z0-9][a-z0-9_-]*$')
);

create table if not exists jobbridge_private.capability_items (
  id text primary key,
  group_id text not null references jobbridge_private.capability_groups(id) on delete cascade,
  label text not null,
  ncs_code text not null default '',
  definition text not null default '',
  sort_order integer not null default 0,
  is_active boolean not null default true,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint capability_items_id_check
    check (id ~ '^[a-z0-9][a-z0-9_-]*$')
);

create table if not exists jobbridge_private.capability_disability_rules (
  id bigint generated always as identity primary key,
  capability_item_id text not null references jobbridge_private.capability_items(id) on delete cascade,
  disability_type text not null,
  severity text null,
  fit_level text not null,
  fit_label text not null default '',
  support_needs text[] not null default array[]::text[],
  notes text[] not null default array[]::text[],
  reason text not null default '',
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint capability_disability_rules_level_check
    check (fit_level in ('suitable', 'caution', 'blocked', 'review')),
  constraint capability_disability_rules_unique
    unique (capability_item_id, disability_type, severity)
);

create index if not exists capability_groups_category_idx
  on jobbridge_private.capability_groups(category_id, sort_order);

create index if not exists capability_items_group_idx
  on jobbridge_private.capability_items(group_id, sort_order);

create index if not exists capability_disability_rules_lookup_idx
  on jobbridge_private.capability_disability_rules(capability_item_id, disability_type, severity)
  where is_active;

alter table jobbridge_private.capability_categories enable row level security;
alter table jobbridge_private.capability_groups enable row level security;
alter table jobbridge_private.capability_items enable row level security;
alter table jobbridge_private.capability_disability_rules enable row level security;

revoke all on jobbridge_private.capability_categories from public, anon, authenticated;
revoke all on jobbridge_private.capability_groups from public, anon, authenticated;
revoke all on jobbridge_private.capability_items from public, anon, authenticated;
revoke all on jobbridge_private.capability_disability_rules from public, anon, authenticated;

grant select, insert, update, delete on jobbridge_private.capability_categories to service_role;
grant select, insert, update, delete on jobbridge_private.capability_groups to service_role;
grant select, insert, update, delete on jobbridge_private.capability_items to service_role;
grant select, insert, update, delete on jobbridge_private.capability_disability_rules to service_role;
grant usage, select on sequence jobbridge_private.capability_disability_rules_id_seq to service_role;

comment on table jobbridge_private.capability_categories is
  'JobBridge challenge recommendation capability category master data. Server-side API reads this with service_role.';

comment on table jobbridge_private.capability_groups is
  'Second-level capability groups under each category.';

comment on table jobbridge_private.capability_items is
  'Selectable fine-grained capability items shown in the challenge recommendation profile UI.';

comment on table jobbridge_private.capability_disability_rules is
  'Optional disability-specific overrides for capability fit. Use for reviewed service rules before frontend exposure.';

notify pgrst, 'reload schema';
