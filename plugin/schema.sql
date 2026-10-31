CREATE TABLE IF NOT EXISTS persons (
    id text PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS bot_settings (
    id boolean PRIMARY KEY DEFAULT true CHECK(id),
    payload jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS persona_profile (
    id boolean PRIMARY KEY DEFAULT true CHECK(id),
    payload jsonb NOT NULL,
    version integer NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS persona_proposals (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    person_id text NOT NULL REFERENCES persons(id),
    layer text NOT NULL CHECK(layer IN ('L0','L1')),
    field text NOT NULL,
    old_value text NOT NULL,
    new_value text NOT NULL,
    reason text NOT NULL,
    sources jsonb NOT NULL,
    base_version integer NOT NULL,
    base_traits_hash text NOT NULL DEFAULT '',
    origin text NOT NULL,
    target_trait_id bigint,
    fingerprint text NOT NULL UNIQUE,
    status text NOT NULL DEFAULT 'checking' CHECK(status IN ('checking','pending','approved','rejected','blocked','stale')),
    check_result jsonb NOT NULL DEFAULT '{}',
    approved_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE persona_proposals ADD COLUMN IF NOT EXISTS base_traits_hash text NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS persona_traits (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    person_id text NOT NULL REFERENCES persons(id),
    field text NOT NULL,
    content text NOT NULL,
    proposal_id bigint NOT NULL REFERENCES persona_proposals(id),
    active boolean NOT NULL DEFAULT true,
    needs_review boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS persona_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    person_id text REFERENCES persons(id),
    proposal_id bigint REFERENCES persona_proposals(id),
    action text NOT NULL,
    before_value jsonb,
    after_value jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS persona_jobs (
    person_id text NOT NULL REFERENCES persons(id),
    day date NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    error text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(person_id,day)
);
ALTER TABLE persona_jobs ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
CREATE TABLE IF NOT EXISTS web_settings (
    id boolean PRIMARY KEY DEFAULT true CHECK(id),
    payload jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS web_requests (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    person_id text NOT NULL REFERENCES persons(id),
    message_id bigint NOT NULL,
    action text NOT NULL CHECK(action IN ('search','fetch')),
    query_or_url text NOT NULL,
    success boolean NOT NULL,
    result_count integer NOT NULL DEFAULT 0,
    error text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS platform_accounts (
    platform text NOT NULL,
    account_id text NOT NULL,
    person_id text NOT NULL REFERENCES persons(id),
    PRIMARY KEY(platform, account_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    person_id text NOT NULL REFERENCES persons(id),
    conversation_id text NOT NULL,
    event_id text NOT NULL,
    role text NOT NULL CHECK(role IN ('user', 'assistant')),
    content text NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE(conversation_id, event_id, role)
);
CREATE INDEX IF NOT EXISTS messages_person_time ON messages(person_id, created_at);
CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id, id DESC);
CREATE TABLE IF NOT EXISTS memories (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    person_id text NOT NULL REFERENCES persons(id),
    content text NOT NULL,
    kind text NOT NULL CHECK(kind IN ('ordinary', 'core')),
    status text NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'archived', 'invalid')),
    last_user_mentioned_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    superseded_by bigint REFERENCES memories(id)
);
CREATE INDEX IF NOT EXISTS memories_person_status ON memories(person_id, status, kind);
ALTER TABLE memories ADD COLUMN IF NOT EXISTS embedding public.vector(512);
ALTER TABLE memories ADD COLUMN IF NOT EXISTS embedding_model text;
CREATE TABLE IF NOT EXISTS memory_chunks(
    memory_id bigint NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    chunk_no integer NOT NULL,content text NOT NULL,embedding public.vector(512) NOT NULL,
    embedding_model text NOT NULL,PRIMARY KEY(memory_id,chunk_no)
);
CREATE TABLE IF NOT EXISTS memory_sources (
    memory_id bigint NOT NULL REFERENCES memories(id),
    message_id bigint NOT NULL REFERENCES messages(id),
    quote text NOT NULL,
    PRIMARY KEY(memory_id, message_id)
);
CREATE TABLE IF NOT EXISTS memory_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    person_id text NOT NULL REFERENCES persons(id),
    memory_id bigint NOT NULL REFERENCES memories(id),
    action text NOT NULL,
    message_id bigint REFERENCES messages(id),
    details jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS daily_jobs (
    person_id text NOT NULL REFERENCES persons(id),
    day date NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    checkpoint bigint NOT NULL DEFAULT 0,
    error text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(person_id, day)
);
CREATE TABLE IF NOT EXISTS context_runs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    person_id text NOT NULL REFERENCES persons(id),
    message_id bigint NOT NULL REFERENCES messages(id),
    injected_ids jsonb NOT NULL,
    random_memory_id bigint REFERENCES memories(id),
    estimated_memory_tokens integer NOT NULL,
    estimated_history_tokens integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE messages ADD COLUMN IF NOT EXISTS token_count integer;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS importance real NOT NULL DEFAULT 0.5;
ALTER TABLE context_runs ADD COLUMN IF NOT EXISTS details jsonb NOT NULL DEFAULT '{}';
CREATE TABLE IF NOT EXISTS context_policy(id boolean PRIMARY KEY DEFAULT true CHECK(id),payload jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS continuity_summaries(
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    person_id text NOT NULL REFERENCES persons(id),
    covered_through bigint NOT NULL,
    payload jsonb NOT NULL,
    source_tokens integer NOT NULL,
    summary_tokens integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS continuity_latest ON continuity_summaries(person_id,id DESC);
CREATE TABLE IF NOT EXISTS api_usage(
    id uuid PRIMARY KEY,person_id text, message_id bigint, category text NOT NULL, model text,
    request_id text, started_at timestamptz NOT NULL, finished_at timestamptz NOT NULL DEFAULT now(),
    input_tokens integer, output_tokens integer,cache_hit_tokens integer,cache_miss_tokens integer,
    planned_input_tokens integer,latency_ms integer,first_token_ms integer,
    finish_reason text,error text,estimated_usd_low double precision,estimated_usd_high double precision,
    usage jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS api_usage_time ON api_usage(started_at);
CREATE TABLE IF NOT EXISTS proactive_settings(
    id boolean PRIMARY KEY DEFAULT true CHECK(id),payload jsonb NOT NULL,updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS proactive_state(
    person_id text PRIMARY KEY REFERENCES persons(id),last_user_id bigint NOT NULL,last_user_at timestamptz NOT NULL,
    due_at timestamptz,interval_seconds integer NOT NULL,awaiting_reply boolean NOT NULL DEFAULT false,
    current_run uuid,attempts integer NOT NULL DEFAULT 0,updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS proactive_deliveries(
    id uuid PRIMARY KEY,person_id text NOT NULL REFERENCES persons(id),source_user_id bigint NOT NULL,
    target jsonb NOT NULL,status text NOT NULL CHECK(status IN ('generating','sending','sent','unknown','skipped','cancelled','failed')),
    text text,reason text,error text,receipt jsonb NOT NULL DEFAULT '{}',lease_until timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),sent_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS proactive_delivery_once ON proactive_deliveries(person_id,source_user_id) WHERE status IN ('sending','sent','unknown');
CREATE TABLE IF NOT EXISTS message_attachments(
    id uuid PRIMARY KEY,message_id bigint NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    kind text NOT NULL,name text NOT NULL,status text NOT NULL,path text,context_text text NOT NULL,error text,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE message_attachments DROP CONSTRAINT IF EXISTS message_attachments_message_id_kind_name_key;
CREATE INDEX IF NOT EXISTS attachments_message ON message_attachments(message_id);
CREATE OR REPLACE VIEW message_context AS
SELECT m.id,m.person_id,m.conversation_id,m.event_id,m.role,
    m.content || coalesce(a.body,'') AS content,m.created_at,
    CASE WHEN a.body IS NULL THEN m.token_count ELSE NULL END AS token_count
FROM messages m LEFT JOIN LATERAL (
    SELECT string_agg(context_text,E'\n' ORDER BY created_at,id) AS body FROM message_attachments WHERE message_id=m.id
) a ON true;
