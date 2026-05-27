"""Testes do orchestrator com mocks pesados (sem GHL/OpenAI reais)."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from zoi_agent import orchestrator as orch
from zoi_agent.agent.schemas import Collected, SessionState, StateUpdate


@pytest.fixture(autouse=True)
def patch_deps(monkeypatch):
    # state: stateless por teste; vamos manter num dict
    store: dict[str, SessionState] = {}

    async def fake_load_or_new(cid: str) -> SessionState:
        return store.get(cid) or SessionState()

    async def fake_save(cid: str, st: SessionState) -> None:
        store[cid] = st

    monkeypatch.setattr(orch.session_repo, "load_or_new", fake_load_or_new)
    monkeypatch.setattr(orch.session_repo, "save", fake_save)

    # history fetch
    async def fake_history(cid: str):
        return [], "conv-1"

    monkeypatch.setattr(orch, "_fetch_history", fake_history)

    # updater
    async def fake_updater(*, history, state, last_message):
        return StateUpdate(
            stage="descoberta",
            collected=Collected(nome="Lead"),
            missing=["intencao"],
            next_action="x",
            sentiment="neutro",
            intent="qualificar",
        )

    monkeypatch.setattr(orch, "run_updater", fake_updater)

    # responder
    sent: list[str] = []

    async def fake_responder(*, state, update, history, last_message, tool_outputs):
        return [f"resp[{last_message}]:b1", "b2"]

    monkeypatch.setattr(orch, "run_responder", fake_responder)

    # send_bubble = registra em sent
    async def fake_send(*, contact_id, conversation_id, text):
        sent.append(f"{contact_id}|{text}")

    monkeypatch.setattr(orch, "_send_bubble", fake_send)

    # zera sleeps p/ não atrasar
    monkeypatch.setattr(orch.settings, "responder_sleep_min", 0.0)
    monkeypatch.setattr(orch.settings, "responder_sleep_max", 0.0)

    # tools
    async def fake_dispatch(*, update_intent_sec, last_message):
        return {}

    monkeypatch.setattr(orch, "_dispatch_tools", fake_dispatch)

    # clear task table
    orch._TASKS.clear()
    return {"store": store, "sent": sent}


@pytest.mark.asyncio
async def test_pipeline_envia_bolhas_e_salva(patch_deps) -> None:
    task = await orch.process_turn("c1", "oi me chamo Raul")
    await task
    sent = patch_deps["sent"]
    assert any("c1|resp[oi me chamo Raul]:b1" in s for s in sent)
    assert any("c1|b2" in s for s in sent)
    assert "c1" in patch_deps["store"]
    assert patch_deps["store"]["c1"].collected.nome == "Lead"


@pytest.mark.asyncio
async def test_preempcao_cancela_anterior(monkeypatch, patch_deps) -> None:
    """C22: 3 turnos em sequência -- só último completa, anteriores cancelam."""
    completed: list[str] = []

    # responder lento (simula LLM demorado)
    async def slow_responder(*, state, update, history, last_message, tool_outputs):
        await asyncio.sleep(0.5)
        completed.append(last_message)
        return [f"resp[{last_message}]"]

    monkeypatch.setattr(orch, "run_responder", slow_responder)

    t1 = await orch.process_turn("c1", "msg1")
    await asyncio.sleep(0.05)
    t2 = await orch.process_turn("c1", "msg2")
    await asyncio.sleep(0.05)
    t3 = await orch.process_turn("c1", "msg3")

    # t1 e t2 devem estar cancelados
    for t in (t1, t2):
        with pytest.raises(asyncio.CancelledError):
            await t
    await t3

    assert completed == ["msg3"]
    sent_str = " ".join(patch_deps["sent"])
    assert "resp[msg3]" in sent_str
    assert "resp[msg1]" not in sent_str
    assert "resp[msg2]" not in sent_str


@pytest.mark.asyncio
async def test_terminal_state_para_de_responder(monkeypatch, patch_deps) -> None:
    patch_deps["store"]["c1"] = SessionState(
        stage="fechado", terminal_reason="handoff_solicitado"
    )

    called = {"updater": 0, "send": 0}

    async def counting_updater(*args, **kwargs):
        called["updater"] += 1
        return StateUpdate(
            stage="descoberta", collected=Collected(),
            missing=[], next_action="x", sentiment="neutro", intent="qualificar",
        )

    async def counting_send(*args, **kwargs):
        called["send"] += 1

    monkeypatch.setattr(orch, "run_updater", counting_updater)
    monkeypatch.setattr(orch, "_send_bubble", counting_send)

    task = await orch.process_turn("c1", "oi de novo")
    await task

    assert called["updater"] == 0  # nem entrou no pipeline
    assert called["send"] == 0


@pytest.mark.asyncio
async def test_shield_protege_send_em_andamento(monkeypatch, patch_deps) -> None:
    """Se um novo turno chegar enquanto bolhas estão sendo enviadas, o send em
    andamento NÃO deve ser interrompido (asyncio.shield)."""
    send_log: list[str] = []

    async def slow_send(*, contact_id, conversation_id, text):
        await asyncio.sleep(0.15)
        send_log.append(text)

    monkeypatch.setattr(orch, "_send_bubble", slow_send)

    async def fast_responder(*, state, update, history, last_message, tool_outputs):
        return [f"{last_message}-a", f"{last_message}-b"]

    monkeypatch.setattr(orch, "run_responder", fast_responder)

    t1 = await orch.process_turn("c1", "X")
    # deixa começar o send
    await asyncio.sleep(0.05)
    t2 = await orch.process_turn("c1", "Y")
    # t1 será cancelado, mas shield deve garantir que ao menos parte do send rode
    # NOTE: shield protege o awaitable interno se a Task cancelar; ainda assim o
    # `await asyncio.shield(...)` levanta CancelledError no caller. Vamos
    # consumir e verificar que pelo menos a 1ª bolha foi enviada.
    with pytest.raises(asyncio.CancelledError):
        await t1
    await t2

    # 1ª bolha do turno X já tinha começado antes do cancel
    assert any(s.startswith("X-") for s in send_log)
    # turno Y completa normalmente
    assert any(s.startswith("Y-") for s in send_log)
