-- Private server-owned clips and Gemini caption embeddings. No public airtime authority.
create schema if not exists extensions;
create extension if not exists vector with schema extensions;

create table public.breadcast_clips (
  id text primary key check (id ~ '^[0-9a-f]{64}$'),
  event_id text not null,
  run_id text not null,
  kind text not null check (kind in ('original', 'chunk', 'replay')),
  sha256 text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  bucket text not null,
  object_path text not null,
  bytes bigint not null check (bytes between 1 and 536870912),
  metadata jsonb not null check (jsonb_typeof(metadata) = 'object'),
  created_at timestamptz not null default now()
);

create table public.breadcast_scenes (
  event_id text not null,
  run_id text not null,
  scene_id text not null,
  revision integer not null check (revision > 0),
  index_version text not null,
  embedding_version text not null,
  model_version text not null,
  embedding extensions.vector(768) not null,
  body jsonb not null,
  created_at timestamptz not null default now(),
  primary key (event_id, run_id, scene_id, revision, index_version, embedding_version, model_version),
  check (body->'scene'->>'scene_id' = scene_id),
  check ((body->'scene'->>'revision')::integer = revision),
  check (body->'scene'->'source'->>'event_id' = event_id),
  check (body->'scene'->'source'->>'run_id' = run_id),
  check (body->>'index_version' = index_version),
  check (body->>'embedding_version' = embedding_version)
);

create index breadcast_scene_scope on public.breadcast_scenes (event_id, run_id, scene_id, revision desc);
create index breadcast_scene_vector on public.breadcast_scenes
  using hnsw (embedding extensions.vector_cosine_ops);
create index breadcast_clip_scope on public.breadcast_clips (event_id, run_id, kind);

alter table public.breadcast_clips enable row level security;
alter table public.breadcast_scenes enable row level security;
revoke all on public.breadcast_clips, public.breadcast_scenes from public, anon, authenticated;
grant select, insert, delete on public.breadcast_clips, public.breadcast_scenes to service_role;
-- No browser RLS policy: only the authenticated media runtime can read or write.

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('breadcast-clips', 'breadcast-clips', false, 52428800, array['video/mp4'])
on conflict (id) do nothing;

create or replace function public.breadcast_search(
  p_event_id text, p_run_id text, p_index_version text, p_embedding_version text,
  p_model_version text, p_embedding extensions.vector(768), p_scene_ids text[], p_limit integer default 5
) returns table (scene_id text, revision integer, score double precision, body jsonb)
language plpgsql stable security invoker set search_path = public, extensions
as $$
begin
  if p_limit < 1 or p_limit > 10 or coalesce(cardinality(p_scene_ids), 0) > 4096 or
      extensions.vector_dims(p_embedding) <> 768 then
    raise exception 'Invalid search bounds';
  end if;
  return query
    with current_scenes as (
      select distinct on (s.scene_id) s.scene_id, s.revision, s.body, s.embedding
      from public.breadcast_scenes s
      where s.event_id = p_event_id and s.run_id = p_run_id
        and s.index_version = p_index_version and s.embedding_version = p_embedding_version
        and s.model_version = p_model_version and s.scene_id = any(p_scene_ids)
      order by s.scene_id, s.revision desc
    )
    select s.scene_id, s.revision, (1 - (s.embedding <=> p_embedding))::double precision, s.body
    from current_scenes s where s.body->'scene'->>'status' <> 'retracted'
    order by s.embedding <=> p_embedding, s.scene_id limit p_limit;
end;
$$;
revoke all on function public.breadcast_search(text, text, text, text, text, extensions.vector, text[], integer)
  from public, anon, authenticated;
grant execute on function public.breadcast_search(text, text, text, text, text, extensions.vector, text[], integer)
  to service_role;
