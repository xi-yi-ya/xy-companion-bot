from types import SimpleNamespace

import httpx

from plugin.provider_network import direct_environment, ensure_direct


async def test_only_official_host_is_direct_and_existing_pool_remains_open():
    async with httpx.AsyncClient(proxy="socks5://127.0.0.1:7890") as client:
        provider = SimpleNamespace(
            client=SimpleNamespace(
                _client=client, base_url=httpx.URL("https://api.deepseek.com/v1/")
            )
        )
        original = client._transport_for_url(httpx.URL("https://example.com"))
        assert ensure_direct(provider)
        direct = client._transport_for_url(
            httpx.URL("https://api.deepseek.com/v1/chat/completions")
        )
        assert direct is not original and not client.is_closed
        assert (
            client._transport_for_url(httpx.URL("https://api.deepseek.com.evil.invalid"))
            is original
        )
        assert (
            ensure_direct(provider)
            and client._transport_for_url(httpx.URL("https://api.deepseek.com")) is direct
        )


async def test_nonofficial_provider_is_untouched():
    async with httpx.AsyncClient() as client:
        provider = SimpleNamespace(
            client=SimpleNamespace(_client=client, base_url=httpx.URL("https://example.com"))
        )
        assert not ensure_direct(provider)


def test_direct_environment_preserves_other_exclusions(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    direct_environment()
    direct_environment()
    import os

    assert os.environ["NO_PROXY"].split(",") == ["localhost", "127.0.0.1", "api.deepseek.com"]
