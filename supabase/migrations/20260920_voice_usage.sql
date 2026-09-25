-- Filect Voice: per-request audio-seconds log.
-- Drives accurate cost tracking + the daily fair-use ceiling in the `transcribe`
-- edge function. Written ONLY by the service role (the edge function); RLS is on
-- with no policies, so it stays invisible to anon/authenticated clients — same
-- locked-down posture as abandoned_checkouts, contact_messages, etc.

create table if not exists public.voice_usage (
  id          bigint generated always as identity primary key,
  user_id     uuid not null references auth.users(id) on delete cascade,
  day         date not null default (now() at time zone 'utc')::date,
  seconds     numeric not null default 0,
  created_at  timestamptz not null default now()
);

create index if not exists voice_usage_user_day_idx on public.voice_usage (user_id, day);

alter table public.voice_usage enable row level security;
-- Intentionally no policies: service_role (edge function) bypasses RLS; clients
-- must never read or write this table directly.
