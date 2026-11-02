CREATE TABLE IF NOT EXISTS persons (
    id text PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS bot_settings (
    id boolean PRIMARY KEY DEFAULT true CHECK(id),
    payload jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS persona_policy(id boolean PRIMARY KEY DEFAULT true CHECK(id),mode text NOT NULL DEFAULT 'confirm' CHECK(mode IN ('confirm','auto')),daily_limit integer NOT NULL DEFAULT 4 CHECK(daily_limit BETWEEN 1 AND 20));
CREATE TABLE IF NOT EXISTS qq_chat_settings (
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
ALTER TABLE messages ADD COLUMN IF NOT EXISTS platform text;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS platform_id text;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS chat_type text NOT NULL DEFAULT 'private' CHECK(chat_type IN ('private','group'));
ALTER TABLE messages ADD COLUMN IF NOT EXISTS group_id text;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS sender_id text;
-- Provenance is separate from memory ownership. Only infer known legacy formats.
UPDATE messages SET chat_type='group'
WHERE chat_type='private' AND (person_id LIKE 'qqgroup:%' OR conversation_id LIKE '%:GroupMessage:%');
UPDATE messages SET platform=CASE
    WHEN person_id LIKE 'qqgroup:%' OR conversation_id LIKE 'companion_qq:%' THEN 'aiocqhttp'
    WHEN conversation_id LIKE 'webchat:%' THEN 'webchat' ELSE NULL END
WHERE platform IS NULL;
UPDATE messages SET platform_id=split_part(conversation_id,':',1)
WHERE platform_id IS NULL AND conversation_id ~ '^[^:]+:(FriendMessage|GroupMessage):';
UPDATE messages SET group_id=split_part(person_id,':',3)
WHERE group_id IS NULL AND person_id ~ '^qqgroup:[^:]+:[0-9]+:';
UPDATE messages SET platform_id=split_part(person_id,':',2)
WHERE platform_id IS NULL AND person_id LIKE 'qqgroup:%';
UPDATE messages SET group_id=substring(conversation_id from ':GroupMessage:(?:[^:]*_)?([0-9]+)$')
WHERE group_id IS NULL AND chat_type='group';
UPDATE messages SET sender_id=CASE
    WHEN role='user' AND person_id ~ '^qqgroup:[^:]+:[0-9]+:[0-9]+$' THEN split_part(person_id,':',4)
    WHEN role='user' AND chat_type='group' THEN substring(content from '^\[群成员 [^\n]*（QQ ([0-9]+)） 发言\]')
    WHEN role='assistant' AND person_id ~ '^qqgroup:[^:]+:[0-9]+:room:[0-9]+$' THEN split_part(person_id,':',5)
    WHEN role='user' AND platform='aiocqhttp' AND conversation_id ~ ':FriendMessage:[0-9]+$' THEN split_part(conversation_id,':',3)
    ELSE NULL END
WHERE sender_id IS NULL;
CREATE INDEX IF NOT EXISTS messages_source ON messages(platform,chat_type,group_id,id DESC);
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
CREATE TABLE IF NOT EXISTS qq_group_scans (
    id bigserial PRIMARY KEY,
    person_id text NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    message_id bigint NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    decision text NOT NULL CHECK(decision IN ('reply','silent','error')),
    reason text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
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
-- Additive only: share the existing lock, in-flight reservation and no-reply gate.
ALTER TABLE proactive_state ADD COLUMN IF NOT EXISTS followup_due timestamptz;
ALTER TABLE proactive_state ADD COLUMN IF NOT EXISTS followup_expires timestamptz;
ALTER TABLE proactive_state ADD COLUMN IF NOT EXISTS followup_source bigint;
ALTER TABLE proactive_state ADD COLUMN IF NOT EXISTS followup_reason text;
ALTER TABLE proactive_deliveries ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'contact';
CREATE TABLE IF NOT EXISTS followup_settings(id boolean PRIMARY KEY DEFAULT true CHECK(id),payload jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS rhythm_settings(id boolean PRIMARY KEY DEFAULT true CHECK(id),payload jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS qq_reply_runs(
    source_id bigint PRIMARY KEY, status text NOT NULL, reason text,
    sent_count integer NOT NULL DEFAULT 0, created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS message_attachments(
    id uuid PRIMARY KEY,message_id bigint NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    kind text NOT NULL,name text NOT NULL,status text NOT NULL,path text,context_text text NOT NULL,error text,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE message_attachments DROP CONSTRAINT IF EXISTS message_attachments_message_id_kind_name_key;
CREATE INDEX IF NOT EXISTS attachments_message ON message_attachments(message_id);
CREATE TABLE IF NOT EXISTS qzone_settings(id boolean PRIMARY KEY DEFAULT true CHECK(id),payload jsonb NOT NULL,revision bigint NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS qzone_state(id boolean PRIMARY KEY DEFAULT true CHECK(id),account_qq text NOT NULL,current_run uuid,lease_until timestamptz,due_at timestamptz,last_browse timestamptz,last_success timestamptz,last_error text);
CREATE TABLE IF NOT EXISTS qzone_runs(id uuid PRIMARY KEY,account_qq text NOT NULL,status text NOT NULL CHECK(status IN ('reading','complete','error','cancelled')),started_at timestamptz NOT NULL,finished_at timestamptz,post_count integer NOT NULL DEFAULT 0,new_count integer NOT NULL DEFAULT 0,reviewed_count integer NOT NULL DEFAULT 0,error text);
CREATE TABLE IF NOT EXISTS qzone_posts(key text PRIMARY KEY,account_qq text NOT NULL,author_qq text NOT NULL,payload jsonb NOT NULL,posted_at timestamptz,first_seen timestamptz NOT NULL,last_seen timestamptz NOT NULL,reviewed boolean NOT NULL DEFAULT false,review jsonb);
CREATE TABLE IF NOT EXISTS qzone_candidates(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,post_key text NOT NULL UNIQUE REFERENCES qzone_posts(key),account_qq text NOT NULL,score double precision NOT NULL CHECK(score BETWEEN 0 AND 1),note text NOT NULL,reason text NOT NULL,status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sending','shared','unknown','dismissed')),delivery_id uuid REFERENCES proactive_deliveries(id),created_at timestamptz NOT NULL);
CREATE INDEX IF NOT EXISTS qzone_candidate_pending ON qzone_candidates(account_qq,status,created_at);
CREATE TABLE IF NOT EXISTS qzone_publish_settings(id boolean PRIMARY KEY DEFAULT true CHECK(id),payload jsonb NOT NULL,revision bigint NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS qzone_publish_state(account_qq text PRIMARY KEY,current_run uuid,lease_until timestamptz,next_consider_at timestamptz,last_error text,consecutive_failures integer NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS qzone_publications(id uuid PRIMARY KEY,account_qq text NOT NULL,status text NOT NULL CHECK(status IN ('generating','skipped','blocked','cancelled','publishing','sent','unknown','failed')),text text NOT NULL DEFAULT '',reason text NOT NULL DEFAULT '',error text,fingerprint text,persona_version bigint,created_at timestamptz NOT NULL,remote_started_at timestamptz,finished_at timestamptz,post_id text,url text);
CREATE INDEX IF NOT EXISTS qzone_publications_limits ON qzone_publications(account_qq,remote_started_at);
CREATE TABLE IF NOT EXISTS qq_agent_settings(id boolean PRIMARY KEY DEFAULT true CHECK(id),payload jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS qq_agent_accounts(account_qq text PRIMARY KEY,initialized_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS qq_requests(id text PRIMARY KEY,account_qq text NOT NULL,platform_id text NOT NULL,kind text NOT NULL CHECK(kind IN ('friend','group')),sub_type text NOT NULL,user_id text NOT NULL,group_id text,flag text NOT NULL,comment text NOT NULL,status text NOT NULL DEFAULT 'pending',created_at timestamptz NOT NULL DEFAULT now(),decided_at timestamptz);
CREATE TABLE IF NOT EXISTS qq_operations(id uuid PRIMARY KEY,account_qq text NOT NULL,action text NOT NULL,idempotency_key text NOT NULL,actor text NOT NULL,metadata jsonb NOT NULL,status text NOT NULL,message_id text,error text,created_at timestamptz NOT NULL DEFAULT now(),finished_at timestamptz,recalled_at timestamptz,UNIQUE(account_qq,idempotency_key));
CREATE TABLE IF NOT EXISTS qq_assets(id uuid PRIMARY KEY,name text NOT NULL,path text NOT NULL,size bigint NOT NULL,kind text NOT NULL CHECK(kind IN ('image','file')),created_at timestamptz NOT NULL DEFAULT now());
ALTER TABLE qq_assets ADD COLUMN IF NOT EXISTS approved boolean NOT NULL DEFAULT true;
CREATE TABLE IF NOT EXISTS qq_custom_settings(id boolean PRIMARY KEY DEFAULT true CHECK(id),payload jsonb NOT NULL,revision bigint NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS qq_custom_actions(id uuid PRIMARY KEY,account_qq text NOT NULL,kind text NOT NULL,payload jsonb NOT NULL,reason text NOT NULL DEFAULT '',status text NOT NULL,idempotency_key text NOT NULL,source_message_id bigint,created_at timestamptz NOT NULL DEFAULT now(),started_at timestamptz,finished_at timestamptz,operation_id uuid,error text,approved boolean NOT NULL DEFAULT false,UNIQUE(account_qq,idempotency_key));
CREATE TABLE IF NOT EXISTS qq_profile_cache(account_qq text PRIMARY KEY,payload jsonb NOT NULL,updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS qq_stickers(asset_id uuid PRIMARY KEY REFERENCES qq_assets(id),sha256 text NOT NULL UNIQUE,description text NOT NULL,tags jsonb NOT NULL,emotion text NOT NULL,scope text NOT NULL,source jsonb NOT NULL,embedding public.vector(512),created_at timestamptz NOT NULL DEFAULT now(),last_used_at timestamptz,use_count integer NOT NULL DEFAULT 0,qq_state text NOT NULL DEFAULT 'not_synced',qq_receipt jsonb NOT NULL DEFAULT '{}');
ALTER TABLE qq_stickers ADD COLUMN IF NOT EXISTS active boolean NOT NULL DEFAULT true;
CREATE TABLE IF NOT EXISTS qq_sticker_queue(message_id bigint PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,refs jsonb NOT NULL,scope text NOT NULL,status text NOT NULL DEFAULT 'pending',error text,created_at timestamptz NOT NULL DEFAULT now(),finished_at timestamptz);
CREATE OR REPLACE VIEW message_context AS
SELECT m.id,m.person_id,m.conversation_id,m.event_id,m.role,
    m.content || coalesce(a.body,'') AS content,m.created_at,
    CASE WHEN a.body IS NULL THEN m.token_count ELSE NULL END AS token_count,
    m.platform,m.platform_id,m.chat_type,m.group_id,m.sender_id
FROM messages m LEFT JOIN LATERAL (
    SELECT string_agg(context_text,E'\n' ORDER BY created_at,id) AS body FROM message_attachments WHERE message_id=m.id
) a ON true;
