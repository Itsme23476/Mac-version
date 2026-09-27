-- Per-user monthly counter for LLM query-distillation calls (voice search).
-- Abuse guard: caps the OpenAI spend per user. Same RLS posture as voice_usage —
-- RLS on, NO policies, so only the service_role (the distill-query edge function)
-- can read/write it.
create table if not exists public.voice_search_usage (
  user_id    uuid        not null,
  month      text        not null,               -- 'YYYY-MM' (UTC)
  count      integer     not null default 0,
  updated_at timestamptz not null default now(),
  primary key (user_id, month)
);

alter table public.voice_search_usage enable row level security;

-- Atomic upsert-and-increment; returns the new count for the month.
create or replace function public.increment_voice_search(p_user_id uuid, p_month text)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
  new_count integer;
begin
  insert into public.voice_search_usage (user_id, month, count, updated_at)
  values (p_user_id, p_month, 1, now())
  on conflict (user_id, month)
  do update set count = voice_search_usage.count + 1, updated_at = now()
  returning count into new_count;
  return new_count;
end;
$$;
