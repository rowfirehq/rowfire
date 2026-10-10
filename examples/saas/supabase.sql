-- The demo's third data source: the product itself, running on Supabase.
--
-- Workspaces and their hourly API usage. The sample trigger
-- `free_workspace_near_quota` fires when a free workspace has used 80% of its
-- monthly quota: a nudge to upgrade, and the kind of rule a growth team writes.
--
-- Run it once in a Supabase project (SQL editor, or `psql` as `postgres`). It
-- creates the two tables in `public`, seeds sixty days of history, and
-- schedules an hourly pg_cron job that keeps adding usage and the occasional
-- new signup, so the trigger has something to fire on without anyone
-- touching it. Then point Rowfire at the project:
--
--   ROWFIRE_DEMO_SUPABASE_DSN=supabase://<project ref>
--   SUPABASE_ACCESS_TOKEN=<a token that can read the project>
--
-- Rowfire reads it through Supabase's read-only query endpoint, as
-- `supabase_read_only_user`, which reads past row level security; the tables
-- have RLS on and no policies, so the project's public API exposes nothing.
--
-- Safe to run again: it drops and rebuilds everything it created.

create extension if not exists pg_cron;

select cron.unschedule(jobid) from cron.job where jobname = 'rowfire-demo-tick';
drop schema if exists rowfire_demo cascade;
drop table if exists public.api_usage;
drop table if exists public.workspaces;

create table public.workspaces (
  id bigint generated always as identity primary key,
  name text not null,
  owner_email text not null,
  plan text not null default 'free' check (plan in ('free', 'pro', 'team')),
  monthly_quota integer not null,
  created_at timestamptz not null default now()
);

create table public.api_usage (
  id bigint generated always as identity primary key,
  workspace_id bigint not null references public.workspaces (id) on delete cascade,
  calls integer not null check (calls >= 0),
  recorded_at timestamptz not null default now()
);
create index on public.api_usage (workspace_id, recorded_at);
create index on public.api_usage (recorded_at);

alter table public.workspaces enable row level security;
alter table public.api_usage enable row level security;
comment on table public.workspaces is 'Rowfire demo data (examples/saas/supabase.sql)';
comment on table public.api_usage is 'Rowfire demo data (examples/saas/supabase.sql)';

-- The simulation lives outside `public`, so the project's API never offers it.
create schema rowfire_demo;
revoke all on schema rowfire_demo from public, anon, authenticated;

-- How busy a workspace is, in calls a day: fixed per workspace, so the same
-- ones are the heavy users month after month.
create function rowfire_demo.daily_calls(workspace_id bigint)
returns integer
language sql
immutable
set search_path = ''
as $$
  select 150 + (abs(hashtext('rowfire-demo-' || workspace_id::text)) % 2900)
$$;

create function rowfire_demo.quota(plan text)
returns integer
language sql
immutable
set search_path = ''
as $$
  select case plan when 'free' then 30000 when 'pro' then 250000 else 1000000 end
$$;

-- A new workspace, named from a small list so the demo reads like a product.
create function rowfire_demo.sign_up(at_time timestamptz, plan text default 'free')
returns bigint
language plpgsql
set search_path = ''
as $$
declare
  companies text[] := array[
    'Northwind', 'Lumen', 'Brightpath', 'Cobalt', 'Fernhill', 'Quillstack',
    'Harbor', 'Tessellate', 'Kestrel', 'Orbitly', 'Mapleworks', 'Pinecrest',
    'Sundial', 'Waypoint', 'Driftwood', 'Larkspur', 'Ironbark', 'Nimbus'
  ];
  people text[] := array[
    'ada', 'grace', 'linus', 'margaret', 'alan', 'radia', 'ken', 'barbara',
    'dennis', 'frances', 'tim', 'hedy', 'donald', 'karen', 'guido', 'anita'
  ];
  company text := companies[1 + floor(random() * array_length(companies, 1))::int];
  person text := people[1 + floor(random() * array_length(people, 1))::int];
  new_id bigint;
begin
  insert into public.workspaces (name, owner_email, plan, monthly_quota, created_at)
  values (
    company || ' ' || (100 + floor(random() * 900))::int,
    person || '@' || lower(company) || '.example',
    plan,
    rowfire_demo.quota(plan),
    at_time
  )
  returning id into new_id;
  return new_id;
end
$$;

-- One hour of activity: every workspace's calls for the hour, now and then a
-- new signup, and the oldest history trimmed so the project stays small.
create function rowfire_demo.tick(at_time timestamptz default now())
returns void
language plpgsql
set search_path = ''
as $$
begin
  if random() < 0.03 then
    perform rowfire_demo.sign_up(at_time);
  end if;

  insert into public.api_usage (workspace_id, calls, recorded_at)
  select w.id,
         greatest(0, round(rowfire_demo.daily_calls(w.id) / 24.0 * (0.4 + random() * 1.2)))::int,
         at_time
  from public.workspaces w
  where w.created_at <= at_time;

  delete from public.api_usage where recorded_at < at_time - interval '75 days';
  delete from public.workspaces
  where created_at < at_time - interval '120 days'
    and id not in (select id from public.workspaces order by created_at desc limit 40);
end
$$;

revoke all on all functions in schema rowfire_demo from public, anon, authenticated;

-- Sixty days of history, an hour at a time, starting from a dozen workspaces.
do $$
declare
  start timestamptz := date_trunc('hour', now()) - interval '60 days';
  at_hour timestamptz;
begin
  for i in 1..12 loop
    perform rowfire_demo.sign_up(
      start - (random() * interval '30 days'),
      (array['free', 'free', 'free', 'pro', 'team'])[1 + floor(random() * 5)::int]
    );
  end loop;
  at_hour := start;
  while at_hour <= now() loop
    perform rowfire_demo.tick(at_hour);
    at_hour := at_hour + interval '1 hour';
  end loop;
end
$$;

select cron.schedule('rowfire-demo-tick', '0 * * * *', 'select rowfire_demo.tick()');
