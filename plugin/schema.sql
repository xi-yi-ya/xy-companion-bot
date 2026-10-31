CREATE TABLE IF NOT EXISTS persons (
    id text PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS bot_settings (
    id boolean PRIMARY KEY DEFAULT true CHECK(id),
    payload jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
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
