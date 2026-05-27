from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from zoi_agent.audio import whisper as wmod


@pytest.fixture(autouse=True)
def reset_openai(monkeypatch):
    fake = MagicMock()
    fake.audio = MagicMock()
    fake.audio.transcriptions = MagicMock()
    fake.audio.transcriptions.create = AsyncMock(return_value="oi tudo bem")
    monkeypatch.setattr(wmod, "get_openai", lambda: fake)
    return fake


@pytest.mark.asyncio
async def test_transcribe_bytes_ok(reset_openai) -> None:
    out = await wmod.transcribe_bytes(b"\x00\x01", filename="x.ogg")
    assert out == "oi tudo bem"
    reset_openai.audio.transcriptions.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_transcribe_url_downloads(monkeypatch, reset_openai) -> None:
    async def fake_download(url: str, *, timeout: float = 30.0):
        return b"AUDIO", "file.ogg"

    monkeypatch.setattr(wmod, "_download", fake_download)
    text = await wmod.transcribe_url("https://x/file.ogg")
    assert text == "oi tudo bem"


@pytest.mark.asyncio
async def test_transcribe_many_concat(monkeypatch) -> None:
    async def fake_transcribe(url, language="pt"):
        return {"a": "primeiro audio", "b": "segundo audio"}[url]

    monkeypatch.setattr(wmod, "transcribe_url", fake_transcribe)
    out = await wmod.transcribe_many(["a", "b"])
    assert "primeiro audio" in out
    assert "segundo audio" in out
    assert out.count("\n") == 1


@pytest.mark.asyncio
async def test_transcribe_many_empty() -> None:
    assert await wmod.transcribe_many([]) == ""


@pytest.mark.asyncio
async def test_transcribe_retry_on_failure(monkeypatch) -> None:
    calls = {"n": 0}
    fake = MagicMock()
    fake.audio = MagicMock()
    fake.audio.transcriptions = MagicMock()

    async def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.TransportError("boom")
        return "ok final"

    fake.audio.transcriptions.create = flaky
    monkeypatch.setattr(wmod, "get_openai", lambda: fake)
    text = await wmod.transcribe_bytes(b"x", filename="x.ogg")
    assert text == "ok final"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_transcribe_many_propagates_failure(monkeypatch) -> None:
    async def fake_transcribe(url, language="pt"):
        if url == "bad":
            raise RuntimeError("whisper down")
        return "ok"

    monkeypatch.setattr(wmod, "transcribe_url", fake_transcribe)
    with pytest.raises(wmod.TranscriptionError):
        await wmod.transcribe_many(["a", "bad"])
