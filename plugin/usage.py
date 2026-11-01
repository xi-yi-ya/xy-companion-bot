"""Record real API usage once per network call, including streamed tool rounds."""

import asyncio
import contextvars
import inspect
import json
import logging
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

from psycopg.types.json import Jsonb
from psycopg_pool import PoolClosed

from .token_counting import counter

SCOPE = contextvars.ContextVar("companion_api_scope", default={})


def context_limit_error(exc):
    text = str(exc).lower().replace("_", " ")
    return "context length" in text or "context window" in text or "maximum context" in text


@contextmanager
def usage_scope(category, **metadata):
    token = SCOPE.set({**SCOPE.get(), **metadata, "category": category})
    try:
        yield
    finally:
        SCOPE.reset(token)


def cost_range(hit, miss, output, at):
    """Official USD rates. Weekday peak holidays remain an explicit range."""
    hit, miss, output = (n or 0 for n in (hit, miss, output))
    peak = at.weekday() < 5 and (1 <= at.hour < 4 or 6 <= at.hour < 10)
    off = (hit * 0.003 + miss * 0.15 + output * 0.6) / 1_000_000
    return off, off * 2 if peak else off


def read_usage(usage):
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    usage = usage or {}
    prompt = usage.get("prompt_tokens")
    hit = usage.get(
        "prompt_cache_hit_tokens",
        (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
    )
    miss = usage.get(
        "prompt_cache_miss_tokens", None if prompt is None else max(0, prompt - (hit or 0))
    )
    return usage, prompt, usage.get("completion_tokens"), hit, miss


class UsageRecorder:
    def __init__(self, pool, policy):
        self.pool, self.policy = pool, policy
        self.resources = []

    def uninstall(self):
        for resource, wrapper, original in self.resources:
            if (
                getattr(resource, "_companion_usage_recorder", None) is self
                and resource.create is wrapper
            ):
                resource.create = original
                resource._companion_metered = False
                resource._companion_usage_recorder = None
        self.resources.clear()

    async def save(self, uid, scope, start, elapsed, first, result, usage, error, planned):
        raw, inp, out, hit, miss = read_usage(usage)
        raw = {
            **raw,
            "offered_tools": scope.get("offered_tools", []),
            "network_route": scope.get("network_route"),
        }
        low, high = cost_range(hit, miss, out, start) if inp is not None else (None, None)
        choices = getattr(result, "choices", None) or []
        reason = getattr(choices[0], "finish_reason", None) if choices else None
        async with self.pool.connection() as conn:
            await conn.execute(
                """INSERT INTO api_usage(id,person_id,message_id,category,model,request_id,started_at,
                input_tokens,output_tokens,cache_hit_tokens,cache_miss_tokens,planned_input_tokens,latency_ms,first_token_ms,
                finish_reason,error,estimated_usd_low,estimated_usd_high,usage)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (
                    uid,
                    scope.get("person_id"),
                    scope.get("message_id"),
                    scope.get("category", "native_other"),
                    getattr(result, "model", "deepseek-flash"),
                    getattr(result, "id", None),
                    start,
                    inp,
                    out,
                    hit,
                    miss,
                    planned,
                    round(elapsed * 1000),
                    round(first * 1000) if first is not None else None,
                    reason,
                    error,
                    low,
                    high,
                    Jsonb(raw),
                ),
            )

    def install(self, provider):
        handler = getattr(provider, "_handle_api_error", None)
        if handler and not getattr(provider, "_companion_no_context_pop", False):

            async def handle_error(exc, *args, **kwargs):
                # AstrBot has another fallback here that pops old records on
                # a provider limit error. Keep the complete sources and fail.
                if context_limit_error(exc):
                    raise ValueError("模型拒绝了完整上下文；历史未裁剪，请整理后重试。") from exc
                return await handler(exc, *args, **kwargs)

            provider._handle_api_error = handle_error
            provider._companion_no_context_pop = True
        resource = provider.client.chat.completions
        if getattr(resource, "_companion_usage_recorder", None) is self:
            return
        original = getattr(resource, "_companion_original_create", None)
        if original is None:
            original = resource.create
            # Migrate wrappers left by versions before lifecycle ownership was
            # recorded. Recover our own original SDK call, never stack wrappers.
            if getattr(resource, "_companion_metered", False) and inspect.isfunction(original):
                recovered = inspect.getclosurevars(original).nonlocals.get("original")
                if callable(recovered) and original.__module__.endswith(".usage"):
                    original = recovered

        async def record(*args):
            active = getattr(resource, "_companion_usage_recorder", None)
            if active is None:
                return
            try:
                await active.save(*args)
            except PoolClosed:
                # A completed model response must survive telemetry shutdown.
                logging.getLogger(__name__).warning(
                    "Usage pool closed during reload; response preserved"
                )

        async def create(*args, **kwargs):
            policy = await self.policy.get()
            scope = dict(SCOPE.get())
            scope["network_route"] = (
                "deepseek_direct"
                if getattr(getattr(provider.client, "_client", None), "__dict__", {}).get(
                    "_companion_deepseek_direct"
                )
                else "provider_default"
            )
            scope["offered_tools"] = [
                t.get("function", {}).get("name")
                for t in kwargs.get("tools", [])
                if isinstance(t, dict)
            ]
            maximum = scope.get("max_output", policy["reply_output"])
            extra = dict(kwargs.get("extra_body") or {})
            extra.pop("max_tokens", None)
            extra["thinking"] = {"type": "disabled"}
            kwargs.update(max_tokens=maximum, extra_body=extra)
            kwargs["model"] = "deepseek-flash"
            if kwargs.get("stream"):
                kwargs["stream_options"] = {
                    **(kwargs.get("stream_options") or {}),
                    "include_usage": True,
                }
            planned = await asyncio.to_thread(
                lambda: (
                    counter().messages(kwargs.get("messages", []))
                    + counter().text(kwargs.get("tools", []))
                )
            )
            if planned + maximum + policy["safety_reserve"] > policy["model_context"]:
                await record(
                    uuid.uuid4(),
                    scope,
                    datetime.now(UTC),
                    0,
                    None,
                    None,
                    None,
                    "LocalContextLimit",
                    planned,
                )
                raise ValueError("当前完整输入超过模型上下文上限，已停止请求，未静默截断历史。")
            uid = uuid.uuid4()
            start = datetime.now(UTC)
            clock = time.monotonic()
            try:
                response = await original(*args, **kwargs)
            except BaseException as exc:
                await record(
                    uid,
                    scope,
                    start,
                    time.monotonic() - clock,
                    None,
                    None,
                    None,
                    "ProviderContextLimit" if context_limit_error(exc) else type(exc).__name__,
                    planned,
                )
                raise
            if not kwargs.get("stream"):
                await record(
                    uid,
                    scope,
                    start,
                    time.monotonic() - clock,
                    None,
                    response,
                    getattr(response, "usage", None),
                    None,
                    planned,
                )
                return response

            async def stream():
                result = None
                usage = None
                first = None
                error = None
                try:
                    async for chunk in response:
                        if result is None or getattr(chunk, "choices", None):
                            result = chunk
                        if first is None and any(
                            getattr(getattr(choice, "delta", None), "content", None)
                            or getattr(getattr(choice, "delta", None), "reasoning_content", None)
                            or getattr(getattr(choice, "delta", None), "tool_calls", None)
                            for choice in (getattr(chunk, "choices", None) or [])
                        ):
                            first = time.monotonic() - clock
                        if getattr(chunk, "usage", None):
                            usage = chunk.usage
                        yield chunk
                except BaseException as exc:
                    error = type(exc).__name__
                    raise
                finally:
                    await response.close()
                    await record(
                        uid,
                        scope,
                        start,
                        time.monotonic() - clock,
                        first,
                        result,
                        usage,
                        error,
                        planned,
                    )

            return stream()

        resource.create = create
        resource._companion_metered = True
        resource._companion_usage_recorder = self
        resource._companion_original_create = original
        self.resources.append((resource, create, original))

    async def metrics(self):
        async with self.pool.connection() as conn:
            daily = await (
                await conn.execute("""SELECT (started_at AT TIME ZONE 'Asia/Shanghai')::date AS day,
              category,count(*) AS calls,sum(input_tokens) AS input_tokens,sum(output_tokens) AS output_tokens,
              sum(cache_hit_tokens) AS cache_hit_tokens,sum(cache_miss_tokens) AS cache_miss_tokens,
              avg(latency_ms)::integer AS latency_ms,avg(first_token_ms)::integer AS first_token_ms,
              sum(estimated_usd_low) AS usd_low,sum(estimated_usd_high) AS usd_high,
              count(*) FILTER(WHERE input_tokens IS NULL) AS missing_usage,
              count(*) FILTER(WHERE finish_reason='length') AS output_truncations
              FROM api_usage GROUP BY day,category ORDER BY day DESC,category LIMIT 100""")
            ).fetchall()
            contexts = await (
                await conn.execute(
                    "SELECT details,estimated_memory_tokens,estimated_history_tokens,created_at FROM context_runs ORDER BY id DESC LIMIT 30"
                )
            ).fetchall()
            monthly = await (
                await conn.execute("""SELECT to_char(started_at AT TIME ZONE 'Asia/Shanghai','YYYY-MM') AS month,
                count(*) AS calls,sum(input_tokens) AS input_tokens,sum(output_tokens) AS output_tokens,
                sum(cache_hit_tokens) AS cache_hit_tokens,sum(cache_miss_tokens) AS cache_miss_tokens,
                sum(estimated_usd_low) AS usd_low,sum(estimated_usd_high) AS usd_high,
                count(*) FILTER(WHERE input_tokens IS NULL) AS missing_usage,
                count(*) FILTER(WHERE error IN ('LocalContextLimit','ProviderContextLimit')) AS context_rejections
                FROM api_usage GROUP BY month ORDER BY month DESC""")
            ).fetchall()
            health = await (
                await conn.execute(
                    "SELECT count(*) AS builds,count(*) FILTER(WHERE details->>'silent_truncation'='true') AS silent_truncations FROM context_runs"
                )
            ).fetchone()
            web_fees = await (
                await conn.execute("""SELECT to_char(created_at AT TIME ZONE 'Asia/Shanghai','YYYY-MM') AS month,
                count(*) FILTER(WHERE success=true AND result_count>0) AS billable_calls,
                count(*) FILTER(WHERE success=true AND result_count>0)*0.00075 AS usd
                FROM web_requests GROUP BY month ORDER BY month DESC""")
            ).fetchall()
        return json.loads(
            json.dumps(
                {
                    "daily": daily,
                    "monthly": monthly,
                    "context_health": health,
                    "web_fees_monthly": web_fees,
                    "recent_contexts": contexts,
                },
                default=str,
            )
        )
