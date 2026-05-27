from __future__ import annotations

from zoi_agent.agent.schemas import (
    Collected,
    SessionState,
    TrocaInfo,
)
from zoi_agent.tools.terminal import build_consolidated_note


def _full_state(appointment=None) -> SessionState:
    return SessionState(
        collected=Collected(
            nome="Raul",
            veiculo_interesse="Renault Duster",
            veiculo_interesse_confirmado=True,
            intencao="troca",
            possui_troca=True,
            troca_completa=TrocaInfo(modelo="Gol", ano=2001, km=280000, quitado=True),
            motivo_compra_ou_troca="precisando de SUV",
            forma_pagamento="financiado",
            cidade="Joinville",
            interesse_agendamento=True,
        ),
        appointment=appointment,
    )


def test_note_qualificado_agendado() -> None:
    state = _full_state(
        appointment={"slot_iso": "2026-06-03T09:30:00-03:00", "id": "apt-1", "modelo": "Duster"},
    )
    note = build_consolidated_note(
        state=state, terminal_reason="qualificado_agendado", observacoes="primeira visita"
    )
    assert "[ZOI] Qualificação — qualificado_agendado" in note
    assert "Lead: Raul" in note
    assert "Cidade: Joinville" in note
    assert "Veículo de interesse: Renault Duster" in note
    assert "Foco definido: sim" in note
    assert "Intenção: troca" in note
    assert "Possui troca: sim" in note
    assert "Troca: Gol 2001 280.000km quitado" in note
    assert "Motivo: precisando de SUV" in note
    assert "Pagamento: financiado" in note
    assert "Agendamento: 03/06/2026 09:30" in note
    assert "Handoff: -" in note
    assert "Observações: primeira visita" in note


def test_note_qualificado_sem_agenda() -> None:
    state = _full_state()
    note = build_consolidated_note(
        state=state, terminal_reason="qualificado_sem_agenda"
    )
    assert "qualificado_sem_agenda" in note
    assert "Agendamento: sem agendamento marcado" in note
    assert "Handoff: -" in note


def test_note_handoff_solicitado() -> None:
    state = SessionState(collected=Collected(nome="Raul"))
    note = build_consolidated_note(
        state=state,
        terminal_reason="handoff_solicitado",
        handoff_reason="lead pediu vendedor 2x",
    )
    assert "handoff_solicitado" in note
    assert "Lead: Raul" in note
    assert "Handoff: lead pediu vendedor 2x" in note
    assert "Cidade: -" in note
    assert "Veículo de interesse: -" in note


def test_note_handoff_erro() -> None:
    note = build_consolidated_note(
        state=SessionState(),
        terminal_reason="handoff_erro",
        handoff_reason="falha LLM updater 3x",
    )
    assert "handoff_erro" in note
    assert "Handoff: falha LLM updater 3x" in note


def test_note_troca_sem_detalhes() -> None:
    state = SessionState(
        collected=Collected(nome="X", possui_troca=True, troca_completa=None),
    )
    note = build_consolidated_note(
        state=state, terminal_reason="handoff_solicitado", handoff_reason="x"
    )
    assert "Troca: sim, sem detalhes" in note


def test_note_sem_troca() -> None:
    state = SessionState(
        collected=Collected(nome="X", possui_troca=False),
    )
    note = build_consolidated_note(
        state=state, terminal_reason="handoff_solicitado", handoff_reason="x"
    )
    assert "Possui troca: não" in note
    assert "Troca: -" in note
