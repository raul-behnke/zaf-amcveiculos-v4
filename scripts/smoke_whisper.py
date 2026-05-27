"""Smoke C10/C11: gera TTS pt-BR -> whisper transcreve -> valida.

C10: 1 áudio.
C11: 2 áudios (concat).
"""
from __future__ import annotations

import asyncio
import sys

from zoi_agent.audio.whisper import transcribe_bytes, transcribe_many
from zoi_agent.llm import get_openai

PHRASES = [
    "Olá, tudo bem? Estou procurando um veículo SUV automático.",
    "Aceitam financiamento? Qual é a entrada mínima?",
]


async def tts(text: str) -> bytes:
    client = get_openai()
    resp = await client.audio.speech.create(
        model="tts-1",
        voice="alloy",
        input=text,
        response_format="mp3",
    )
    return resp.read()


async def main() -> int:
    print("=== gerando TTS ===")
    audios = []
    for i, p in enumerate(PHRASES):
        a = await tts(p)
        print(f"  [{i}] {len(a)} bytes  | original: {p!r}")
        audios.append(a)

    print("\n=== C10: transcrição 1 áudio ===")
    t1 = await transcribe_bytes(audios[0], filename="c10.mp3")
    print(f"  -> {t1!r}")

    print("\n=== C11: 2 áudios via gather + concat ===")
    # transcribe_many usa URLs; aqui chamamos transcribe_bytes em paralelo
    t2_parts = await asyncio.gather(
        transcribe_bytes(audios[0], filename="c11a.mp3"),
        transcribe_bytes(audios[1], filename="c11b.mp3"),
    )
    t2 = "\n".join(t2_parts)
    print(f"  -> {t2!r}")

    # Validação simples por substring (case-insensitive)
    assert "suv" in t1.lower(), f"esperava 'SUV' em {t1!r}"
    assert "financiamento" in t2.lower(), f"esperava 'financiamento' em {t2!r}"
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
