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
