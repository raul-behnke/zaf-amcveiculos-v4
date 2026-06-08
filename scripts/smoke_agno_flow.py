"""Smoke test do pipeline Agno: roda cenários canônicos do conversations_dump
ATACANDO o orchestrator com mocks de GHL send/save (mantém Updater/EstoqueExpert/
Patricia reais contra OpenAI + inventário real do GHL).

Cobre 6 cenários críticos:
  1. Sinal de estoque — detecção determinística (sem LLM)
  2. Blindagem InventoryDecision — IDs inexistentes filtrados, action degrada
  3. EstoqueExpert standalone — 1º turno com origem (modelo fora do estoque)
  4. EstoqueExpert standalone — lead pede SUV genérica
  5. Patricia standalone — sem sinal de estoque (turno de funil puro)
  6. Pipeline completo (_run_turn) — burst inicial com veiculo_origem

Uso: .venv/bin/python scripts/smoke_agno_flow.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from unittest.mock import AsyncMock, patch

# Garante src/ no path
sys.path.insert(0, "src")


PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
INFO = "\033[36mℹ\033[0m"


async def test_1_signal_detection():
    """Pure-Python: sinal de estoque deve disparar nos casos certos."""
    from zoi_agent.agent.schemas import (
        Collected,
        PreferenciaHorario,
        SessionState,
        StateUpdate,
        VeiculoOrigem,
    )
    from zoi_agent.team.runner import detect_inventory_signal

    def _state(**kw):
        return SessionState(**kw)

    def _update(**kw):
        defaults = dict(
            stage="abertura",
            collected=Collected(),
            missing=[],
            next_action="seguir funil",
            sentiment="neutro",
            intent="qualificar",
            topics=[],
        )
        defaults.update(kw)
        return StateUpdate(**defaults)

    cases = [
        # (label, state, update, last_msg, expect_trigger)
        ("1.1 primeiro_turno_com_origem",
         _state(veiculo_origem=VeiculoOrigem(texto="Nissan Sentra")),
         _update(intent="apresentar"),
         "Olá", True),
        ("1.2 lead_nomeou_modelo via topic",
         _state(),
         _update(intent="apresentar", topics=["ver_outros_carros"]),
         "tem algum Fox?", True),
        ("1.3 pedido_foto",
         _state(vehicles_shown=["abc"]),
         _update(intent="qualificar", topics=["pedido_foto"]),
         "manda foto", True),
        ("1.4 qualificação pura — nome",
         _state(vehicles_shown=["abc"], last_card_external_id="abc"),
         _update(intent="qualificar"),
         "Raul", False),
        ("1.5 lead diz 'Ok'",
         _state(vehicles_shown=["abc"]),
         _update(intent="qualificar"),
         "Ok", False),
        ("1.6 keyword 'tem mais'",
         _state(vehicles_shown=["abc"]),
         _update(intent="qualificar"),
         "tem mais opções?", True),
    ]
    fails = 0
    for label, st, up, msg, expected in cases:
        sig = detect_inventory_signal(state=st, update=up, last_message=msg)
        ok = sig["trigger"] == expected
        mark = PASS if ok else FAIL
        print(f"  {mark} {label} (trigger={sig['trigger']}, reasons={sig['reasons']})")
        if not ok:
            fails += 1
    return fails


async def test_2_blindagem_validation():
    """InventoryDecision com IDs inválidos / action incoerente deve degradar."""
    from zoi_agent.team.runner import _validate_inventory_decision
    from zoi_agent.team.schemas import InventoryDecision, VeiculoSelecionado
    from zoi_agent.tools.inventory import load_inventory

    inv = await load_inventory()
    if not inv:
        print(f"  {FAIL} 2.0 inventário vazio — não dá pra validar")
        return 1
    real_id = str(inv[0]["external_id"])
    print(f"  {INFO} usando real_id={real_id} de inventário ({len(inv)} veículos)")

    cases = [
        # (label, decision_input, expected_action)
        (
            "2.1 mostrar_card_unico com ID FAKE -> nao_mostrar",
            InventoryDecision(
                action="mostrar_card_unico",
                veiculos_selecionados=[VeiculoSelecionado(external_id="999999", motivo_individual="fake")],
                motivo_geral="teste",
            ),
            "nao_mostrar",
        ),
        (
            "2.2 mostrar_card_unico com 1 ID válido -> stay",
            InventoryDecision(
                action="mostrar_card_unico",
                veiculos_selecionados=[VeiculoSelecionado(external_id=real_id, motivo_individual="real")],
                motivo_geral="teste",
            ),
            "mostrar_card_unico",
        ),
        (
            "2.3 mostrar_card_lista com 1 ID válido -> downgrade unico",
            InventoryDecision(
                action="mostrar_card_lista",
                veiculos_selecionados=[VeiculoSelecionado(external_id=real_id, motivo_individual="real")],
                motivo_geral="teste",
            ),
            "mostrar_card_unico",
        ),
        (
            "2.4 perguntar_refinamento sem texto -> nao_mostrar",
            InventoryDecision(
                action="perguntar_refinamento",
                pergunta_refinamento=None,
                motivo_geral="teste",
            ),
            "nao_mostrar",
        ),
        (
            "2.5 comentar_em_texto sem veiculos -> nao_mostrar",
            InventoryDecision(
                action="comentar_em_texto",
                veiculos_selecionados=[],
                motivo_geral="teste",
            ),
            "nao_mostrar",
        ),
        (
            "2.6 enviar_fotos_de FAKE -> filtrado",
            InventoryDecision(
                action="comentar_em_texto",
                veiculos_selecionados=[VeiculoSelecionado(external_id=real_id, motivo_individual="x")],
                enviar_fotos_de="999999",
                motivo_geral="teste",
            ),
            "comentar_em_texto",  # action mantém, mas enviar_fotos_de=None
        ),
    ]
    fails = 0
    for label, decision, expected_action in cases:
        validated, warnings = await _validate_inventory_decision(decision)
        ok = validated.action == expected_action
        extra_ok = True
        if label.startswith("2.6"):
            extra_ok = validated.enviar_fotos_de is None
        mark = PASS if (ok and extra_ok) else FAIL
        print(f"  {mark} {label} -> action={validated.action}, warnings={warnings}")
        if not (ok and extra_ok):
            fails += 1
    return fails


async def test_3_inventory_expert_origem():
    """EstoqueExpert: 1º turno com origem=Sentra (modelo provavelmente fora do estoque)."""
    from zoi_agent.agent.question_planner import NextQuestion
    from zoi_agent.agent.schemas import Collected, SessionState, StateUpdate, VeiculoOrigem
    from zoi_agent.team.runner import _base_payload, _call_inventory_expert_with_retry

    state = SessionState(veiculo_origem=VeiculoOrigem(texto="Nissan Sentra 2020"))
    update = StateUpdate(
        stage="abertura", collected=Collected(), missing=[], next_action="x",
        sentiment="neutro", intent="apresentar", topics=["ver_outros_carros"],
    )
    next_q = NextQuestion(field=None, intent="foco", canonical_text="Esse foi o que chamou atenção?")
    payload = _base_payload(
        state=state, update=update, next_question=next_q,
        history=[], last_message="Olá, pode me dizer mais",
        tom_turno="descontraido", acknowledge_hint=None,
        slots=None, vehicle_in_focus=None, booking_result=None,
    )
    decision = await _call_inventory_expert_with_retry(payload, max_attempts=2)
    if not decision:
        print(f"  {FAIL} 3.1 EstoqueExpert retornou None")
        return 1
    n = len(decision.veiculos_selecionados)
    print(f"  {INFO} 3.1 action={decision.action} n_veiculos={n} motivo={decision.motivo_geral[:100]!r}")
    # Critério: deve devolver alternativas (mostrar_card_lista) OU avisar que não tem (comentar_em_texto/refinamento)
    ok = decision.action in ("mostrar_card_lista", "mostrar_card_unico", "comentar_em_texto", "perguntar_refinamento")
    # Se mostrar_card_*, veículos válidos devem existir
    if decision.action.startswith("mostrar_card_") and n == 0:
        ok = False
        print(f"  {FAIL} 3.1 INCOERÊNCIA action mostrar sem veículos")
    else:
        mark = PASS if ok else FAIL
        print(f"  {mark} 3.1 decisão coerente")
    return 0 if ok else 1


async def test_4_inventory_expert_suv():
    """EstoqueExpert: pedido genérico 'tem SUV?' — deve refinamentar ou listar."""
    from zoi_agent.agent.question_planner import NextQuestion
    from zoi_agent.agent.schemas import Collected, SessionState, StateUpdate
    from zoi_agent.team.runner import _base_payload, _call_inventory_expert_with_retry

    state = SessionState()
    update = StateUpdate(
        stage="apresentacao", collected=Collected(), missing=[], next_action="x",
        sentiment="neutro", intent="apresentar", topics=["ver_outros_carros"],
    )
    next_q = NextQuestion(field=None, intent="foco", canonical_text="Esse foi o que chamou atenção?")
    payload = _base_payload(
        state=state, update=update, next_question=next_q,
        history=[], last_message="Tem alguma SUV?",
        tom_turno="descontraido", acknowledge_hint=None,
        slots=None, vehicle_in_focus=None, booking_result=None,
    )
    decision = await _call_inventory_expert_with_retry(payload, max_attempts=2)
    if not decision:
        print(f"  {FAIL} 4.1 None")
        return 1
    n = len(decision.veiculos_selecionados)
    print(f"  {INFO} 4.1 action={decision.action} n_veiculos={n} motivo={decision.motivo_geral[:100]!r}")
    ok = decision.action in ("mostrar_card_lista", "mostrar_card_unico", "perguntar_refinamento")
    if decision.action.startswith("mostrar_card_") and n == 0:
        ok = False
    mark = PASS if ok else FAIL
    print(f"  {mark} 4.1 SUV broad: ação plausível")
    return 0 if ok else 1


async def test_5_patricia_no_estoque():
    """Patricia standalone: turno de qualificação puro, sem sinal de estoque."""
    import json as _json
    from zoi_agent.agent.question_planner import NextQuestion
    from zoi_agent.agent.schemas import Collected, SessionState, StateUpdate
    from zoi_agent.team.runner import _base_payload, build_patricia_agent
    from zoi_agent.team.schemas import BubbleSequence

    state = SessionState(vehicles_shown=["abc"], last_card_external_id="abc")
    state.collected = Collected(veiculo_interesse="Montana 2018", veiculo_interesse_confirmado=True)
    update = StateUpdate(
        stage="descoberta", collected=state.collected, missing=["nome"], next_action="x",
        sentiment="neutro", intent="qualificar", topics=[],
    )
    next_q = NextQuestion(field="nome", intent="funil", canonical_text="Como posso te chamar?")
    payload = _base_payload(
        state=state, update=update, next_question=next_q,
        history=[], last_message="Interessou",
        tom_turno="descontraido", acknowledge_hint=None,
        slots=None, vehicle_in_focus=None, booking_result=None,
    )
    payload["inventory_decision"] = None
    payload["cards_renderizados_serao_inseridos_entre_bolhas"] = False
    payload["_contrato_apresentacao"] = (
        "NÃO HAVERÁ cards neste turno. PROIBIDO dizer 'separei opções' ou prometer veículos."
    )
    patricia = build_patricia_agent()
    result = await patricia.arun(input=_json.dumps(payload, ensure_ascii=False))
    content = getattr(result, "content", None)
    ok = isinstance(content, BubbleSequence)
    if not ok:
        print(f"  {FAIL} 5.1 Patricia output não é BubbleSequence: {type(content).__name__}")
        return 1
    print(f"  {INFO} 5.1 abertura={content.abertura!r}")
    print(f"  {INFO} 5.1 fechamento={content.fechamento!r}")
    # Verifica que NÃO menciona "separei opções"
    full = ((content.abertura or "") + " " + content.fechamento).lower()
    bad_phrases = ["separei", "olha essas", "achei essas", "opções pra você"]
    has_bad = any(b in full for b in bad_phrases)
    mark2 = FAIL if has_bad else PASS
    print(f"  {mark2} 5.2 sem promessa falsa de veículos (bad={has_bad})")
    # Verifica que tem pergunta no fechamento
    has_question = "?" in content.fechamento
    mark3 = PASS if has_question else FAIL
    print(f"  {mark3} 5.3 fechamento tem pergunta")
    return 0 if (ok and not has_bad and has_question) else 1


async def test_6_pipeline_complete():
    """Pipeline completo via _run_turn mocking GHL/DB."""
    from zoi_agent.agent.schemas import SessionState, VeiculoOrigem
    from zoi_agent.orchestrator import _run_turn

    fake_state = SessionState(veiculo_origem=VeiculoOrigem(texto="Volkswagen Crossfox 2008"))
    sent_messages: list[dict] = []
    saved_states: list[SessionState] = []

    async def fake_load(cid):
        return fake_state

    async def fake_save(cid, st):
        saved_states.append(st)

    async def fake_fetch_history(cid):
        return [], "FAKE_CONV_ID"

    async def fake_send_message(**kwargs):
        sent_messages.append(kwargs)
        return {"ok": True}

    patches = [
        patch("zoi_agent.orchestrator.session_repo.load_or_new", side_effect=fake_load),
        patch("zoi_agent.orchestrator.session_repo.save", side_effect=fake_save),
        patch("zoi_agent.orchestrator._fetch_history", side_effect=fake_fetch_history),
        patch("zoi_agent.orchestrator.ghl_conv.send_message", side_effect=fake_send_message),
    ]
    for p in patches:
        p.start()
    try:
        await _run_turn("test_contact_001", "Olá, pode sim. Tem fotos?")
    finally:
        for p in patches:
            p.stop()

    print(f"  {INFO} 6.1 sent_messages count={len(sent_messages)}")
    for i, sm in enumerate(sent_messages):
        msg = sm.get("message") or "[ATTACHMENT]"
        print(f"  {INFO} 6.1   bolha[{i}]: {msg[:120]!r}")

    fails = 0
    if not sent_messages:
        print(f"  {FAIL} 6.2 NENHUMA bolha enviada")
        fails += 1
    else:
        print(f"  {PASS} 6.2 ao menos 1 bolha enviada")

    # Verifica que se mencionou alternativa Sentra/Crossfox, tem card real
    text_concat = " ".join((sm.get("message") or "") for sm in sent_messages).lower()
    promise_words = ["separei", "olha essas", "alternativas", "opções pra você"]
    has_promise = any(w in text_concat for w in promise_words)
    has_card = any("🚗" in (sm.get("message") or "") for sm in sent_messages)
    if has_promise and not has_card:
        print(f"  {FAIL} 6.3 promessa de opções SEM card real (bug original!)")
        fails += 1
    else:
        print(f"  {PASS} 6.3 promessa de opções é coerente com card real (promise={has_promise}, card={has_card})")
    return fails


async def main():
    print(f"\n{INFO} Validação interna do pipeline Agno\n")

    total_fails = 0

    print("== Test 1: Signal Detection (puro Python) ==")
    total_fails += await test_1_signal_detection()

    print("\n== Test 2: Blindagem da InventoryDecision (puro Python, com inventário real) ==")
    total_fails += await test_2_blindagem_validation()

    print("\n== Test 3: EstoqueExpert — 1º turno com origem Sentra (LLM real) ==")
    total_fails += await test_3_inventory_expert_origem()

    print("\n== Test 4: EstoqueExpert — pedido SUV genérica (LLM real) ==")
    total_fails += await test_4_inventory_expert_suv()

    print("\n== Test 5: Patricia standalone — sem sinal (LLM real) ==")
    total_fails += await test_5_patricia_no_estoque()

    print("\n== Test 6: Pipeline completo via _run_turn (Updater+EstoqueExpert+Patricia reais, GHL mocked) ==")
    total_fails += await test_6_pipeline_complete()

    print()
    if total_fails == 0:
        print(f"{PASS} TODOS os testes passaram")
    else:
        print(f"{FAIL} {total_fails} teste(s) falharam")
    return total_fails


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
