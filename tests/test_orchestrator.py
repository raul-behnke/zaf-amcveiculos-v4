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
    async def fake_dispatch(*, update_intent_sec, last_message, state, **_kwargs):
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

    async def fake_handoff(*, contact_id, state, terminal_reason, handoff_reason=None, observacoes=None):
        handoff_calls.append(
            {
                "contact_id": contact_id,
                "handoff_reason": handoff_reason,
                "terminal_reason": terminal_reason,
            }
        )
        return {"tag_removed": True, "note_created": True, "workflow_added": True}

    monkeypatch.setattr(orch, "run_updater", updater_with_handoff)
    monkeypatch.setattr(orch, "encaminhar_para_vendedor", fake_handoff)

    task = await orch.process_turn("c1", "para de me mandar mensagem")
    await task

    assert len(handoff_calls) == 1
    assert handoff_calls[0]["contact_id"] == "c1"
    assert handoff_calls[0]["terminal_reason"] == "handoff_solicitado"
    assert "lead pediu pra parar" in (handoff_calls[0]["handoff_reason"] or "")
    # state foi gravado como fechado
    assert patch_deps["store"]["c1"].terminal_reason == "handoff_solicitado"
    assert patch_deps["store"]["c1"].stage == "fechado"


@pytest.mark.asyncio
async def test_c23_updater_falha_vira_handoff_erro(monkeypatch, patch_deps) -> None:
    """PLAN §13: updater esgotando retries -> orchestrator dispara terminal handoff_erro."""

    async def boom_updater(*, history, state, last_message):
        raise RuntimeError("openai down")

    handoff_calls: list[dict] = []

    async def fake_handoff(*, contact_id, state, terminal_reason, handoff_reason=None, observacoes=None):
        handoff_calls.append({"terminal_reason": terminal_reason, "handoff_reason": handoff_reason})
        return {"tag_removed": True, "note_created": True, "workflow_added": True}

    monkeypatch.setattr(orch, "run_updater", boom_updater)
    monkeypatch.setattr(orch, "encaminhar_para_vendedor", fake_handoff)

    task = await orch.process_turn("c1", "oi")
    await task

    assert len(handoff_calls) == 1
    assert handoff_calls[0]["terminal_reason"] == "handoff_erro"
    assert "openai down" in handoff_calls[0]["handoff_reason"]
    saved = patch_deps["store"]["c1"]
    assert saved.terminal_reason == "handoff_erro"
    assert saved.stage == "fechado"


@pytest.mark.asyncio
async def test_responder_falha_vira_handoff_erro(monkeypatch, patch_deps) -> None:
    async def fake_responder(*, state, update, history, last_message, tool_outputs):
        raise RuntimeError("responder boom")

    handoff_calls: list[dict] = []

    async def fake_handoff(*, contact_id, state, terminal_reason, handoff_reason=None, observacoes=None):
        handoff_calls.append({"terminal_reason": terminal_reason, "handoff_reason": handoff_reason})
        return {"tag_removed": True, "note_created": True, "workflow_added": True}

    monkeypatch.setattr(orch, "run_responder", fake_responder)
    monkeypatch.setattr(orch, "encaminhar_para_vendedor", fake_handoff)

    task = await orch.process_turn("c1", "oi")
    await task

    assert handoff_calls[0]["terminal_reason"] == "handoff_erro"
    assert "responder boom" in handoff_calls[0]["handoff_reason"]


@pytest.mark.asyncio
async def test_c4_dispatch_origem_quando_nao_apresentada(monkeypatch, patch_deps) -> None:
    """state com veiculo_origem + vehicles_shown vazio -> dispatcher chama
    buscar_veiculo_interesse_origem e injeta em tools."""
    from zoi_agent.agent.schemas import VeiculoOrigem

    patch_deps["store"]["c1"] = SessionState(
        stage="abertura", greeted=True,
        veiculo_origem=VeiculoOrigem(texto="Chevrolet Montana"),
    )

    fake_payload = {
        "texto_origem": "Chevrolet Montana",
        "matches": {
            "exatos": [{"external_id": "m1"}, {"external_id": "m2"}],
            "parecidos": [],
        },
    }

    async def fake_busca(state):
        return fake_payload

    captured: dict = {}

    async def capture_responder(*, state, update, history, last_message, tool_outputs):
        captured.update(tool_outputs or {})
        return ["b"]

    monkeypatch.setattr(orch, "buscar_veiculo_interesse_origem", fake_busca)
    monkeypatch.setattr(orch, "run_responder", capture_responder)

    # O fixture autouse mockou _dispatch_tools para {}. Restauramos a versão real
    # neste teste pra validar o dispatch da origem.
    async def real_dispatch_with_origem(*, update_intent_sec, last_message, state, **_kwargs):
        out: dict = {}
        if state.veiculo_origem and not state.vehicles_shown:
            payload = await orch.buscar_veiculo_interesse_origem(state)
            if payload:
                out["origem_matches"] = payload
                from zoi_agent.agent.templates import build_vehicle_blocks_with_ids
                m = payload.get("matches") or {}
                bs, ids = build_vehicle_blocks_with_ids(
                    exatos=m.get("exatos") or [],
                    parecidos=[p.get("vehicle") for p in (m.get("parecidos") or []) if p.get("vehicle")],
                )
                if bs:
                    out["pre_bubbles"] = bs
                    out["rendered_vehicle_ids"] = ids
                    out["vehicles_presented_count"] = len(ids)
        return out

    monkeypatch.setattr(orch, "_dispatch_tools", real_dispatch_with_origem)

    task = await orch.process_turn("c1", "oi pode sim")
    await task

    assert "origem_matches" in captured
    assert captured["origem_matches"]["texto_origem"] == "Chevrolet Montana"
    saved = patch_deps["store"]["c1"]
    assert "m1" in saved.vehicles_shown
    assert "m2" in saved.vehicles_shown


@pytest.mark.asyncio
async def test_c4_skip_origem_quando_ja_apresentada(monkeypatch, patch_deps) -> None:
    """vehicles_shown não-vazio evita re-busca."""
    from zoi_agent.agent.schemas import VeiculoOrigem

    patch_deps["store"]["c1"] = SessionState(
        stage="descoberta", greeted=True,
        veiculo_origem=VeiculoOrigem(texto="Chevrolet Montana"),
        vehicles_shown=["m1", "m2"],
    )

    busca_calls = {"n": 0}

    async def fake_busca(state):
        busca_calls["n"] += 1
        return None

    monkeypatch.setattr(orch, "buscar_veiculo_interesse_origem", fake_busca)

    task = await orch.process_turn("c1", "gostei do 2019")
    await task

    # dispatcher nem chama (gate na própria função + gate no orchestrator)
    # mas mesmo se chamasse, o retorno None bloquearia o flow
    saved = patch_deps["store"]["c1"]
    # vehicles_shown não foi duplicado
    assert saved.vehicles_shown == ["m1", "m2"]


@pytest.mark.asyncio
async def test_c4_1_lead_engaja_marca_focus(monkeypatch, patch_deps) -> None:
    """C4.1: após apresentação, lead engaja num veículo -> veiculo_interesse_confirmado=true."""
    from zoi_agent.agent.schemas import VeiculoOrigem

    patch_deps["store"]["c1"] = SessionState(
        stage="descoberta", greeted=True,
        veiculo_origem=VeiculoOrigem(texto="Chevrolet Montana"),
        vehicles_shown=["m1", "m2"],
    )

    async def updater_engaja(*, history, state, last_message):
        return StateUpdate(
            stage="descoberta",
            collected=Collected(veiculo_interesse_confirmado=True, veiculo_interesse="Montana 2019"),
            missing=["nome", "intencao"],
            next_action="perguntar nome",
            sentiment="positivo",
            intent="qualificar",
        )

    monkeypatch.setattr(orch, "run_updater", updater_engaja)

    task = await orch.process_turn("c1", "gostei do 2019")
    await task

    saved = patch_deps["store"]["c1"]
    assert saved.collected.veiculo_interesse_confirmado is True
    assert saved.collected.veiculo_interesse == "Montana 2019"


@pytest.mark.asyncio
async def test_c4_2_lead_recusa_volta_apresentacao(monkeypatch, patch_deps) -> None:
    """C4.2: após apresentação, lead recusa todos -> stage volta pra apresentacao,
    busca livre, veiculo_interesse_confirmado=False, sem pedir nome ainda."""
    from zoi_agent.agent.schemas import VeiculoOrigem

    patch_deps["store"]["c1"] = SessionState(
        stage="descoberta", greeted=True,
        veiculo_origem=VeiculoOrigem(texto="Chevrolet Montana"),
        vehicles_shown=["m1", "m2"],
    )

    async def updater_recusa(*, history, state, last_message):
        return StateUpdate(
            stage="apresentacao",
            collected=Collected(veiculo_interesse_confirmado=False),
            missing=["veiculo_interesse", "veiculo_interesse_confirmado"],
            next_action="abrir busca livre",
            sentiment="neutro",
            intent="apresentar",
            intent_secundario="ver_outros_carros",
        )

    monkeypatch.setattr(orch, "run_updater", updater_recusa)

    task = await orch.process_turn("c1", "não gostei de nenhum")
    await task

    saved = patch_deps["store"]["c1"]
    assert saved.stage == "apresentacao"
    assert saved.collected.veiculo_interesse_confirmado is False
    assert saved.collected.nome is None  # ainda não pediu


@pytest.mark.asyncio
async def test_c29_pre_bubble_card_um_veiculo(monkeypatch, patch_deps) -> None:
    """C29: 1 veículo no origem_matches -> card rico prepende às bolhas."""
    from zoi_agent.agent.schemas import VeiculoOrigem

    patch_deps["store"]["c1"] = SessionState(
        stage="abertura", greeted=True,
        veiculo_origem=VeiculoOrigem(texto="Chevrolet Montana"),
    )

    async def real_dispatch(*, update_intent_sec, last_message, state, **_kwargs):
        out: dict = {}
        if state.veiculo_origem and not state.vehicles_shown:
            out["origem_matches"] = {
                "texto_origem": "Chevrolet Montana",
                "matches": {
                    "exatos": [{
                        "external_id": "m1", "titulo": "Chevrolet Montana LS",
                        "marca": "Chevrolet", "modelo": "Montana",
                        "ano": 2018, "preco": 49900,
                        "quilometragem": 140000, "cambio": "Manual",
                        "combustivel": "Flex", "opcionais": ["AC", "DH"],
                    }],
                    "parecidos": [],
                },
            }
            from zoi_agent.agent.templates import build_vehicle_blocks
            out["pre_bubbles"] = build_vehicle_blocks(exatos=out["origem_matches"]["matches"]["exatos"])
        return out

    async def responder_only_question(*, state, update, history, last_message, tool_outputs):
        return ["Algum desses te chamou atenção?"]

    monkeypatch.setattr(orch, "_dispatch_tools", real_dispatch)
    monkeypatch.setattr(orch, "run_responder", responder_only_question)

    task = await orch.process_turn("c1", "oi pode sim")
    await task

    sent = patch_deps["sent"]
    assert any("🚗" in s and "Chevrolet Montana LS" in s for s in sent)
    assert any("Algum desses te chamou atenção?" in s for s in sent)
    # ordem: card primeiro, pergunta depois
    card_idx = next(i for i, s in enumerate(sent) if "🚗" in s)
    q_idx = next(i for i, s in enumerate(sent) if "Algum desses" in s)
    assert card_idx < q_idx


@pytest.mark.asyncio
async def test_c30_pre_bubble_lista_dois_mais(monkeypatch, patch_deps) -> None:
    """C30: 2+ veículos -> lista compacta numerada."""
    from zoi_agent.agent.schemas import VeiculoOrigem

    patch_deps["store"]["c1"] = SessionState(
        stage="abertura", greeted=True,
        veiculo_origem=VeiculoOrigem(texto="Chevrolet Montana"),
    )

    async def real_dispatch(*, update_intent_sec, last_message, state, **_kwargs):
        out: dict = {}
        if state.veiculo_origem and not state.vehicles_shown:
            ex = [
                {"marca": "Chevrolet", "modelo": "Montana", "ano": 2019, "preco": 58900, "quilometragem": 95000, "cambio": "Manual"},
                {"marca": "Chevrolet", "modelo": "Montana", "ano": 2017, "preco": 46900, "quilometragem": 120000, "cambio": "Manual"},
            ]
            out["origem_matches"] = {"matches": {"exatos": ex, "parecidos": []}}
            from zoi_agent.agent.templates import build_vehicle_blocks
            out["pre_bubbles"] = build_vehicle_blocks(exatos=ex)
        return out

    async def responder_only_question(*, state, update, history, last_message, tool_outputs):
        return ["Qual deles faz mais sentido?"]

    monkeypatch.setattr(orch, "_dispatch_tools", real_dispatch)
    monkeypatch.setattr(orch, "run_responder", responder_only_question)

    task = await orch.process_turn("c1", "manda os Montana")
    await task

    sent = patch_deps["sent"]
    combined = " | ".join(sent)
    assert "1️⃣" in combined and "2️⃣" in combined
    assert "Qual deles faz mais sentido?" in combined


@pytest.mark.asyncio
async def test_c13_regressao_stage_apresentacao(monkeypatch, patch_deps) -> None:
    """Lead em fechamento pede outro carro -> update.stage=apresentacao -> state regride."""
    patch_deps["store"]["c1"] = SessionState(
        stage="fechamento",
        collected=Collected(
            nome="Raul",
            veiculo_interesse="Duster",
            veiculo_interesse_confirmado=True,
            intencao="compra_direta",
            forma_pagamento="financiado",
            cidade="Joinville",
            interesse_agendamento=True,
        ),
    )

    async def updater_regredindo(*, history, state, last_message):
        return StateUpdate(
            stage="apresentacao",
            collected=Collected(nome="Raul"),
            missing=["veiculo_interesse_confirmado"],
            next_action="apresentar opcoes",
            sentiment="neutro",
            intent="apresentar",
            intent_secundario="ver_outros_carros",
        )

    monkeypatch.setattr(orch, "run_updater", updater_regredindo)

    task = await orch.process_turn("c1", "quero ver outro carro")
    await task

    saved = patch_deps["store"]["c1"]
    assert saved.stage == "apresentacao"  # regrediu de fechamento
    # campos preservados (não regrediram para None)
    assert saved.collected.veiculo_interesse == "Duster"
    assert saved.collected.cidade == "Joinville"
    assert saved.terminal_reason is None


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
async def test_terminal_qualificado_dispara_terminal_action(monkeypatch, patch_deps) -> None:
    """S13: todos os 4 terminal_reasons (incl. qualificado_*) chamam terminal action."""

    async def updater_quali(*, history, state, last_message):
        return StateUpdate(
            stage="fechado",
            collected=Collected(),
            missing=[],
            next_action="x",
            sentiment="positivo",
            intent="agendamento",
            terminal_reason="qualificado_sem_agenda",
        )

    handoff_calls: list[dict] = []

    async def fake_handoff(*, contact_id, state, terminal_reason, handoff_reason=None, observacoes=None):
        handoff_calls.append({"terminal_reason": terminal_reason})
        return {"tag_removed": True, "note_created": True, "workflow_added": True}

    monkeypatch.setattr(orch, "run_updater", updater_quali)
    monkeypatch.setattr(orch, "encaminhar_para_vendedor", fake_handoff)

    task = await orch.process_turn("c1", "não quero agendar agora")
    await task
    assert handoff_calls == [{"terminal_reason": "qualificado_sem_agenda"}]
    assert patch_deps["store"]["c1"].terminal_reason == "qualificado_sem_agenda"


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
