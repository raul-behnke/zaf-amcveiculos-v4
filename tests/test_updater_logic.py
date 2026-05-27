"""Testes puros (sem LLM) de schemas + merge."""
from __future__ import annotations

from zoi_agent.agent.schemas import (
    Collected,
    SessionState,
    StateUpdate,
    TrocaInfo,
    compute_missing,
)
from zoi_agent.agent.updater import merge_into_state


def test_compute_missing_empty() -> None:
    c = Collected()
    miss = compute_missing(c)
    assert miss[0] == "nome"
    assert "interesse_agendamento" in miss
    # troca_completa só aparece se possui_troca=True
    assert "troca_completa" not in miss


def test_compute_missing_com_troca() -> None:
    c = Collected(possui_troca=True)
    miss = compute_missing(c)
    assert "troca_completa" in miss


def test_compute_missing_troca_completa_ok() -> None:
    c = Collected(possui_troca=True, troca_completa=TrocaInfo(modelo="Gol", ano=2018, km=80000, quitado=True))
    miss = compute_missing(c)
    assert "troca_completa" not in miss


def test_merge_preserva_campos_existentes() -> None:
    state = SessionState(collected=Collected(nome="Raul", cidade="Joinville"))
    upd = StateUpdate(
        stage="descoberta",
        collected=Collected(nome="Outro"),  # tenta sobrescrever
        missing=["intencao"],
        next_action="perguntar intencao",
        sentiment="neutro",
        intent="qualificar",
    )
    new = merge_into_state(state, upd)
    assert new.collected.nome == "Raul"  # não regrediu
    assert new.collected.cidade == "Joinville"
    assert new.stage == "descoberta"


def test_merge_preenche_campo_vazio() -> None:
    state = SessionState(collected=Collected(nome="Raul"))
    upd = StateUpdate(
        stage="descoberta",
        collected=Collected(nome="Raul", cidade="Joinville"),
        missing=[],
        next_action="x",
        sentiment="neutro",
        intent="qualificar",
    )
    new = merge_into_state(state, upd)
    assert new.collected.cidade == "Joinville"


def test_merge_increments_counters() -> None:
    state = SessionState(humano_solicitado_count=1)
    upd = StateUpdate(
        stage="descoberta",
        collected=Collected(),
        missing=[],
        next_action="x",
        sentiment="neutro",
        intent="pedido_humano",
        humano_solicitado_count_delta=1,
    )
    new = merge_into_state(state, upd)
    assert new.humano_solicitado_count == 2


def test_merge_delta_clamped() -> None:
    state = SessionState()
    upd = StateUpdate(
        stage="abertura",
        collected=Collected(),
        missing=[],
        next_action="x",
        sentiment="neutro",
        intent="qualificar",
        humano_solicitado_count_delta=5,  # LLM mandou demais
    )
    new = merge_into_state(state, upd)
    assert new.humano_solicitado_count == 1


def test_terminal_reason_propagated() -> None:
    state = SessionState()
    upd = StateUpdate(
        stage="fechado",
        collected=Collected(),
        missing=[],
        next_action="handoff",
        sentiment="irritado",
        intent="opt_out",
        should_handoff=True,
        terminal_reason="handoff_solicitado",
    )
    new = merge_into_state(state, upd)
    assert new.terminal_reason == "handoff_solicitado"
    assert new.stage == "fechado"


def test_vehicle_focus_promovido() -> None:
    state = SessionState(collected=Collected(vehicle_focus_definido=False))
    upd = StateUpdate(
        stage="descoberta",
        collected=Collected(vehicle_focus_definido=True),
        missing=[],
        next_action="x",
        sentiment="neutro",
        intent="qualificar",
    )
    new = merge_into_state(state, upd)
    assert new.collected.vehicle_focus_definido is True
