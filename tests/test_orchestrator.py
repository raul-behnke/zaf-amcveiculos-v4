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
    async def fake_dispatch(*, update_intent_sec, last_message, state):
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
async def test_terminal_handoff_dispara_tool(monkeypatch, patch_deps) -> None:
    """update com terminal_reason=handoff_solicitado deve chamar encaminhar_para_vendedor."""

    async def updater_with_handoff(*, history, state, last_message):
        return StateUpdate(
            stage="fechado",
            collected=Collected(),
            missing=[],
            next_action="handoff",
            sentiment="irritado",
            intent="opt_out",
            should_handoff=True,
            terminal_reason="handoff_solicitado",
            handoff_reason="lead pediu pra parar",
        )

    handoff_calls: list[dict] = []

    async def fake_handoff(*, contact_id, motivo, terminal_reason):
        handoff_calls.append(
            {"contact_id": contact_id, "motivo": motivo, "terminal_reason": terminal_reason}
        )
        return {"tag_removed": True, "note_created": True, "workflow_added": True}

    monkeypatch.setattr(orch, "run_updater", updater_with_handoff)
    monkeypatch.setattr(orch, "encaminhar_para_vendedor", fake_handoff)

    task = await orch.process_turn("c1", "para de me mandar mensagem")
    await task

    assert len(handoff_calls) == 1
    assert handoff_calls[0]["contact_id"] == "c1"
    assert handoff_calls[0]["terminal_reason"] == "handoff_solicitado"
    assert "lead pediu pra parar" in handoff_calls[0]["motivo"]
    # state foi gravado como fechado
    assert patch_deps["store"]["c1"].terminal_reason == "handoff_solicitado"
    assert patch_deps["store"]["c1"].stage == "fechado"


@pytest.mark.asyncio
async def test_chosen_slot_dispara_book(monkeypatch, patch_deps) -> None:
    """update.chosen_slot_iso -> book_appointment chamado e terminal_reason=qualificado_agendado."""
    book_mock = AsyncMock(return_value={"appointment": {"id": "apt-9"}})
    monkeypatch.setattr(orch, "book_appointment", book_mock)

    async def upd_with_slot(*, history, state, last_message):
        return StateUpdate(
            stage="fechamento",
            collected=Collected(nome="Raul", veiculo_interesse="Duster"),
            missing=[],
            next_action="confirmar agendamento",
            sentiment="positivo",
            intent="agendamento",
            chosen_slot_iso="2026-06-03T09:30:00-03:00",
        )

    monkeypatch.setattr(orch, "run_updater", upd_with_slot)

    task = await orch.process_turn("c1", "pode ser 03/06 09:30")
    await task

    book_mock.assert_awaited_once()
    kwargs = book_mock.await_args.kwargs
    assert kwargs["slot_iso"] == "2026-06-03T09:30:00-03:00"
    assert kwargs["lead_name"] == "Raul"
    assert kwargs["modelo"] == "Duster"

    saved = patch_deps["store"]["c1"]
    assert saved.terminal_reason == "qualificado_agendado"
    assert saved.appointment["id"] == "apt-9"
    assert saved.stage == "fechado"


@pytest.mark.asyncio
async def test_book_fail_vira_handoff_erro(monkeypatch, patch_deps) -> None:
    book_mock = AsyncMock(side_effect=RuntimeError("slot ocupado"))
    handoff_mock = AsyncMock(return_value={"tag_removed": True, "note_created": True, "workflow_added": True})
    monkeypatch.setattr(orch, "book_appointment", book_mock)
    monkeypatch.setattr(orch, "encaminhar_para_vendedor", handoff_mock)

    async def upd_with_slot(*, history, state, last_message):
        return StateUpdate(
            stage="fechamento",
            collected=Collected(nome="Raul", veiculo_interesse="Duster"),
            missing=[],
            next_action="x",
            sentiment="positivo",
            intent="agendamento",
            chosen_slot_iso="2026-06-03T09:30:00-03:00",
        )

    monkeypatch.setattr(orch, "run_updater", upd_with_slot)

    task = await orch.process_turn("c1", "pode ser 03/06 09:30")
    await task

    handoff_mock.assert_awaited_once()
    assert handoff_mock.await_args.kwargs["terminal_reason"] == "handoff_erro"
    assert patch_deps["store"]["c1"].terminal_reason == "handoff_erro"


@pytest.mark.asyncio
async def test_terminal_qualificado_nao_dispara_handoff_em_S11(monkeypatch, patch_deps) -> None:
    """terminal_reason=qualificado_agendado fica pra S13; orchestrator S11 só chama
    handoff em {handoff_solicitado, handoff_erro}."""

    async def updater_quali(*, history, state, last_message):
        return StateUpdate(
            stage="fechado",
            collected=Collected(),
            missing=[],
            next_action="x",
            sentiment="positivo",
            intent="agendamento",
            terminal_reason="qualificado_agendado",
        )

    handoff_calls: list[int] = []

    async def fake_handoff(**kwargs):
        handoff_calls.append(1)
        return {"tag_removed": True, "note_created": True, "workflow_added": True}

    monkeypatch.setattr(orch, "run_updater", updater_quali)
    monkeypatch.setattr(orch, "encaminhar_para_vendedor", fake_handoff)

    task = await orch.process_turn("c1", "marca amanha")
    await task
    assert handoff_calls == []
    assert patch_deps["store"]["c1"].terminal_reason == "qualificado_agendado"


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
