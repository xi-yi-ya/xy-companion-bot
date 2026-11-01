import asyncio
from types import SimpleNamespace

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from plugin.context_policy import PolicyStore
from plugin.usage import UsageRecorder


def sdk_resource(create):
    resource = SimpleNamespace(create=create)
    provider = SimpleNamespace(client=SimpleNamespace(chat=SimpleNamespace(completions=resource)))
    return provider, resource


def reply():
    return SimpleNamespace(
        model="deepseek-flash",
        usage={"prompt_tokens": 10, "completion_tokens": 2},
        choices=[SimpleNamespace(finish_reason="stop")],
    )


async def test_reload_rebinds_closed_pool_and_never_stacks_usage_wrappers(store):
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return reply()

    provider, resource = sdk_resource(create)
    old_pool = AsyncConnectionPool(
        store.pool.conninfo, open=False, kwargs={"row_factory": dict_row}
    )
    await old_pool.open(wait=True)
    old = UsageRecorder(old_pool, PolicyStore(old_pool))
    old.install(provider)
    await old_pool.close()
    current = UsageRecorder(store.pool, store.policy)
    current.install(provider)
    current.install(provider)
    old.uninstall()  # A late old terminator must not detach its successor.
    assert resource._companion_usage_recorder is current
    assert (
        await resource.create(messages=[{"role": "user", "content": "你好"}])
    ).model == "deepseek-flash"
    assert len(calls) == 1
    assert (await current.metrics())["monthly"][0]["calls"] == 1
    current.uninstall()
    assert resource.create is create


async def test_legacy_wrapper_is_migrated_without_calling_the_closed_recorder(store):
    async def original(**kwargs):
        return reply()

    def legacy():
        async def create(**kwargs):
            raise AssertionError("Legacy wrapper must be unwrapped")
            return await original(**kwargs)

        create.__module__ = "data.plugins.astrbot_plugin_companion_memory.usage"
        return create

    provider, resource = sdk_resource(legacy())
    resource._companion_metered = True
    current = UsageRecorder(store.pool, store.policy)
    current.install(provider)
    assert (await resource.create(messages=[])).model == "deepseek-flash"
    current.uninstall()


async def test_response_in_flight_survives_reload_and_usage_moves_to_live_pool(store):
    entered, finish = asyncio.Event(), asyncio.Event()

    async def create(**kwargs):
        entered.set()
        await finish.wait()
        return reply()

    provider, resource = sdk_resource(create)
    old_pool = AsyncConnectionPool(
        store.pool.conninfo, open=False, kwargs={"row_factory": dict_row}
    )
    await old_pool.open(wait=True)
    old = UsageRecorder(old_pool, PolicyStore(old_pool))
    old.install(provider)
    pending = asyncio.create_task(resource.create(messages=[]))
    await entered.wait()
    current = UsageRecorder(store.pool, store.policy)
    current.install(provider)
    old.uninstall()
    await old_pool.close()
    finish.set()
    assert (await pending).model == "deepseek-flash"
    assert (await current.metrics())["monthly"][0]["calls"] == 1
    current.uninstall()
