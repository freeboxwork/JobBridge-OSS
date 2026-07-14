create schema if not exists jobbridge_private;

revoke all on schema jobbridge_private from public, anon, authenticated;
grant usage on schema jobbridge_private to service_role;

create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  email text null,
  display_name text null,
  profile_json jsonb not null default '{}'::jsonb,
  capabilities_json jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint profiles_email_length_check
    check (email is null or char_length(email) <= 320),
  constraint profiles_display_name_length_check
    check (display_name is null or char_length(display_name) <= 80)
);

alter table public.profiles
  add column if not exists profile_json jsonb not null default '{}'::jsonb,
  add column if not exists capabilities_json jsonb not null default '[]'::jsonb;

-- Keep service_role keys server-side only. Browser clients use RLS below.
revoke all on public.profiles from public, anon, authenticated;
grant usage on schema public to authenticated, service_role;
grant select, insert, update on public.profiles to authenticated;
grant select, insert, update, delete on public.profiles to service_role;

alter table public.profiles enable row level security;

drop policy if exists profiles_select_own
  on public.profiles;

create policy profiles_select_own
on public.profiles
for select
to authenticated
using ((select auth.uid()) = id);

drop policy if exists profiles_insert_own
  on public.profiles;

create policy profiles_insert_own
on public.profiles
for insert
to authenticated
with check ((select auth.uid()) = id);

drop policy if exists profiles_update_own
  on public.profiles;

create policy profiles_update_own
on public.profiles
for update
to authenticated
using ((select auth.uid()) = id)
with check ((select auth.uid()) = id);

create or replace function public.set_profiles_updated_at()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

revoke all on function public.set_profiles_updated_at() from public, anon, authenticated;

drop trigger if exists set_profiles_updated_at
  on public.profiles;

create trigger set_profiles_updated_at
before update on public.profiles
for each row
execute function public.set_profiles_updated_at();

create or replace function jobbridge_private.handle_new_auth_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  initial_email text;
  initial_display_name text;
begin
  -- raw_user_meta_data is user-editable: copy display defaults only, never authorize with it.
  initial_email := nullif(coalesce(new.email, new.raw_user_meta_data ->> 'email'), '');
  initial_display_name := nullif(coalesce(
    new.raw_user_meta_data ->> 'name',
    new.raw_user_meta_data ->> 'full_name',
    new.raw_user_meta_data ->> 'display_name'
  ), '');

  insert into public.profiles (id, email, display_name)
  values (new.id, initial_email, initial_display_name)
  on conflict (id) do update
  set
    email = coalesce(public.profiles.email, excluded.email),
    display_name = coalesce(public.profiles.display_name, excluded.display_name),
    updated_at = case
      when (public.profiles.email is null and excluded.email is not null)
        or (public.profiles.display_name is null and excluded.display_name is not null)
      then now()
      else public.profiles.updated_at
    end;

  return new;
end;
$$;

revoke all on function jobbridge_private.handle_new_auth_user()
  from public, anon, authenticated;

drop trigger if exists on_auth_user_created
  on auth.users;

create trigger on_auth_user_created
after insert on auth.users
for each row
execute function jobbridge_private.handle_new_auth_user();

comment on table public.profiles is
  'JobBridge profile rows linked 1:1 to Supabase Auth users. RLS limits clients to their own row.';

comment on column public.profiles.email is
  'Initial contact email copied from auth.users.email, with metadata only as a display fallback.';

comment on column public.profiles.display_name is
  'Initial display name copied from raw_user_meta_data. Do not use metadata for authorization.';

comment on column public.profiles.profile_json is
  'Optional JobBridge profile snapshot owned by the authenticated user.';

comment on column public.profiles.capabilities_json is
  'Optional self-reported capability snapshot owned by the authenticated user.';

comment on function jobbridge_private.handle_new_auth_user() is
  'Creates a profile row after auth.users insert; metadata is copied only for display defaults.';

notify pgrst, 'reload schema';
