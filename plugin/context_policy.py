"""Experience-first ceilings, not targets that have to be filled."""

from psycopg.types.json import Jsonb

DEFAULT_POLICY = {
    "version": 3,
    "history_tiers": [24000, 90000, 240000],
    "history_max": 240000,
    "compact_at": 216000,
    "retain_recent": 168000,
    "summary_input": 72000,
    "summary_output": 10000,
    "summary_budget": 12000,
    "memory_budget": 30000,
    "core_budget": 8000,
    "active_budget": 12000,
    "archive_recall_budget": 8000,
    "random_budget": 2200,
    "retrieval_budget": 12000,
    "persona_l0_budget": 6000,
    "persona_l1_budget": 6000,
    "reply_output": 12000,
    "daily_input": 72000,
    "daily_known": 12000,
    "daily_output": 16000,
    "working_input": 480000,
    "model_context": 1048576,
    "model_output": 393216,
    "tool_reserve": 32000,
    "safety_reserve": 32000,
    "idle_gap_hours": 8,
    "reentry_recent": 24000,
}


class PolicyStore:
    def __init__(self, pool):
        self.pool = pool

    async def get(self):
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute("SELECT payload FROM context_policy WHERE id=true")
            ).fetchone()
        return {**DEFAULT_POLICY, **(row["payload"] if row else {})}

    async def migrate(self, conn):
        await conn.execute(
            "INSERT INTO context_policy(id,payload) VALUES(true,%s) ON CONFLICT DO NOTHING",
            (Jsonb(DEFAULT_POLICY),),
        )
        await conn.execute(
            "UPDATE context_policy SET payload=payload || %s WHERE (payload->>'version')::integer<3",
            (Jsonb({"version": 3, "random_budget": 2200}),),
        )
