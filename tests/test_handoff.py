from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from zoi_agent.agent.schemas import Collected, SessionState
from zoi_agent.config import settings
from zoi_agent.tools import handoff as h


@pytest.fixture
def patches(monkeypatch):
    remove_mock = AsyncMock()
    note_mock = AsyncMock()
    wf_mock = AsyncMock()
    monkeypatch.setattr(h.gc, "remove_tag", remove_mock)
    monkeypatch.setattr(h.gc, "add_note", note_mock)
    monkeypatch.setattr(h.gw, "add_to_workflow", wf_mock)
    monkeypatch.setattr(h, "emit_event", AsyncMock())
    return {"remove": remove_mock, "note": note_mock, "wf": wf_mock}


def _state() -> SessionState:
    return SessionState(collected=Collected(nome="Raul", cidade="Joinville"))


@pytest.mark.asyncio
async def test_handoff_full_success(patches) -> None:
    res = await h.encaminhar_para_vendedor(
        contact_id="c1",
        state=_state(),
        terminal_reason="handoff_solicitado",
        handoff_reason="lead pediu",
    )
    assert res == {"tag_removed": True, "note_created": True, "workflow_added": True}
    patches["remove"].assert_awaited_once_with("c1", [settings.ghl_tag_agent_gate])
    note_arg = patches["note"].await_args.args[1]
    assert "handoff_solicitado" in note_arg
    assert "Raul" in note_arg
    assert "Joinville" in note_arg
    assert "lead pediu" in note_arg
    patches["wf"].assert_awaited_once_with("c1", settings.ghl_handoff_workflow_id)


@pytest.mark.asyncio
async def test_handoff_partial_failure_tag(patches) -> None:
    patches["remove"].side_effect = RuntimeError("403")
    res = await h.encaminhar_para_vendedor(
        contact_id="c1",
        state=_state(),
        terminal_reason="handoff_solicitado",
        handoff_reason="x",
    )
    assert res["tag_removed"] is False
    assert res["note_created"] is True
    assert res["workflow_added"] is True


@pytest.mark.asyncio
async def test_qualificados_counter_agendado(patches) -> None:
    from zoi_agent.metrics import QUALIFICADOS_TOTAL

    before = QUALIFICADOS_TOTAL.labels(com_agenda="sim")._value.get()
    await h.encaminhar_para_vendedor(
        contact_id="c1", state=_state(), terminal_reason="qualificado_agendado"
    )
    after = QUALIFICADOS_TOTAL.labels(com_agenda="sim")._value.get()
    assert after - before == 1


@pytest.mark.asyncio
async def test_qualificados_counter_sem_agenda(patches) -> None:
    from zoi_agent.metrics import QUALIFICADOS_TOTAL

    before = QUALIFICADOS_TOTAL.labels(com_agenda="nao")._value.get()
    await h.encaminhar_para_vendedor(
        contact_id="c1", state=_state(), terminal_reason="qualificado_sem_agenda"
    )
    after = QUALIFICADOS_TOTAL.labels(com_agenda="nao")._value.get()
    assert after - before == 1


@pytest.mark.asyncio
async def test_handoff_partial_failure_workflow(patches) -> None:
    patches["wf"].side_effect = RuntimeError("workflow gone")
    res = await h.encaminhar_para_vendedor(
        contact_id="c1",
        state=_state(),
        terminal_reason="handoff_erro",
        handoff_reason="x",
    )
    assert res["tag_removed"] is True
    assert res["note_created"] is True
    assert res["workflow_added"] is False
