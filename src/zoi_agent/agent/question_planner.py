"""Planner determinístico da próxima pergunta.

Resolve estruturalmente os 3 vícios:
  - REPETIDA: cruza missing real com state.last_asked_fields (rolling window
    das últimas perguntas feitas) e pula se foi pedido 2x sem resposta.
  - AMBÍGUA: 1 campo por turno; frase canônica vem do Python.
  - SEM LÓGICA: ignora update.missing/next_action; recalcula missing do
    state.collected real após o merge.

Não usa LLM. Apenas regras determinísticas. O responder LLM recebe a
`NextQuestion` no payload e veste de persona — mas o TEMA é fixo aqui.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from zoi_agent.agent.schemas import Collected, SessionState, StateUpdate

QuestionIntent = Literal["funil", "foco", "agendamento", "duvida", "nenhum"]


@dataclass
class NextQuestion:
    field: str | None
    intent: QuestionIntent
    canonical_text: str
    skip_funnel_reason: str | None = None


# Frases-tema por campo. Responder pode variar tom mas mantém o tópico.
CANONICAL_QUESTIONS: dict[str, str] = {
    "nome": "Como posso te chamar?",
    "veiculo_interesse": "Qual veículo te interessou?",
    "veiculo_interesse_confirmado": "Esse foi o que chamou atenção?",
    "intencao": "É compra direta ou tá pensando em trocar seu atual?",
    "possui_troca": "Vai usar algum carro como troca?",
    "troca_completa.modelo": "Qual o modelo do seu atual?",
    "troca_completa.ano": "E o ano dele?",
    "troca_completa.km": "Quilometragem aproximada?",
    "troca_completa.quitado": "Tá quitado?",
    "motivo_compra_ou_troca": "O que te levou a procurar agora?",
    "forma_pagamento": "À vista, financiado ou consórcio?",
    "cidade": "De qual cidade você é?",
    "interesse_agendamento": "Quer agendar uma visita pra ver pessoalmente?",
}


# Ordem PRIORITY com subcampos granulares de troca_completa.
# vehicle_focus tratado dentro da regra de "apresentação" (veicula_origem flow).
PRIORITY_FUNNEL: tuple[str, ...] = (
    "nome",
    "veiculo_interesse",
    "veiculo_interesse_confirmado",
    "intencao",
    "possui_troca",
    "troca_completa.modelo",
    "troca_completa.ano",
    "troca_completa.km",
    "troca_completa.quitado",
    "motivo_compra_ou_troca",
    "forma_pagamento",
    "cidade",
    "interesse_agendamento",
)


def _is_filled(c: Collected, field: str) -> bool:
    """True se o campo está preenchido (não-null E não-vazio)."""
    if field.startswith("troca_completa."):
        sub = field.split(".", 1)[1]
        if c.possui_troca is not True:
            # Subcampos só são relevantes se possui_troca=true
            return True  # tratamos como "não-aplicável = preenchido" pra pular
        t = c.troca_completa
        if t is None:
            return False
        val = getattr(t, sub, None)
        return val is not None and val != ""
    if field == "veiculo_interesse_confirmado":
        return c.veiculo_interesse_confirmado is True
    val = getattr(c, field, None)
    if val is None or val == "":
        return False
    return True


def compute_missing(c: Collected) -> list[str]:
    """Retorna missing[] AO VIVO do collected real. Substitui update.missing
    do LLM (que pode estar desatualizado)."""
    return [f for f in PRIORITY_FUNNEL if not _is_filled(c, f)]


def _was_asked_recently(state: SessionState, field: str, *, window: int = 2) -> int:
    """Quantas vezes esse field aparece nas últimas `window` perguntas."""
    recent = state.last_asked_fields[-window:] if state.last_asked_fields else []
    return sum(1 for f in recent if f == field)


def plan_next_question(
    *,
    state: SessionState,
    update: StateUpdate,
    history: list[dict] | None = None,
) -> NextQuestion:
    """Decide a próxima pergunta DEPOIS do merge. O state passado JÁ tem o
    update aplicado (merge_into_state já rodou)."""
    # 1. Terminal -> sem pergunta
    if update.terminal_reason or state.terminal_reason:
        return NextQuestion(
            field=None, intent="nenhum",
            canonical_text="", skip_funnel_reason="terminal",
        )

    # 2. Dúvida operacional -> primeiro responde a dúvida, missing fica suspenso 1 turno
    if update.intent_secundario == "duvida_operacional":
        return NextQuestion(
            field=None, intent="duvida",
            canonical_text="",
            skip_funnel_reason="responder dúvida do lead antes de avançar",
        )

    # 3. Apresentação em andamento (pre_bubbles foram preparados) ->
    #    pergunta de FOCO, não funil.
    if update.intent_secundario == "ver_outros_carros" or update.intent == "apresentar":
        # Sem campo do funil — responder decide entre singular/plural via
        # tools.vehicles_presented_count.
        return NextQuestion(
            field=None, intent="foco",
            canonical_text="Algum desses chamou sua atenção?",
            skip_funnel_reason="apresentação ativa",
        )

    # 3b. Apresentação IMINENTE da origem do CRM: o orchestrator vai renderizar
    #     cards do veiculo_origem neste mesmo turno. Pede FOCO antes de nome —
    #     evita "Esse te interessou? Como posso te chamar?" (2 perguntas/turno).
    #     Roda só uma vez: depois que origem_apresentada=True, cai pro funil normal.
    if (
        state.veiculo_origem
        and not state.origem_apresentada
        and not state.collected.veiculo_interesse_confirmado
    ):
        return NextQuestion(
            field=None, intent="foco",
            canonical_text="Esse te interessou?",
            skip_funnel_reason="apresentação iminente da origem do CRM",
        )

    # 4. Gate de agendamento atendido -> ramo de slots/booking.
    if (
        state.collected.interesse_agendamento is True
        and state.collected.veiculo_interesse_confirmado is True
    ):
        return NextQuestion(
            field=None, intent="agendamento",
            canonical_text="Qual horário fica melhor pra você?",
            skip_funnel_reason="agendamento",
        )

    # 5. Funil — primeiro missing que NÃO foi perguntado 2x sem resposta.
    missing = compute_missing(state.collected)
    if not missing:
        # 10 campos OK, sem agendamento ainda; ainda assim pergunte agendamento
        return NextQuestion(
            field="interesse_agendamento", intent="funil",
            canonical_text=CANONICAL_QUESTIONS["interesse_agendamento"],
        )

    chosen = None
    for f in missing:
        asked_count = _was_asked_recently(state, f, window=2)
        if asked_count >= 2:
            # Tentamos 2x sem resposta utilizável — pula esse e tenta o próximo.
            continue
        chosen = f
        break

    # Se TUDO foi tentado 2x, força o primeiro missing (não dá pra escapar mais).
    if chosen is None:
        chosen = missing[0]

    return NextQuestion(
        field=chosen, intent="funil",
        canonical_text=CANONICAL_QUESTIONS.get(chosen, "Me passa essa informação?"),
    )


def push_asked_field(state: SessionState, field: str | None, *, window: int = 5) -> None:
    """Registra que este campo foi perguntado neste turno. Mantém rolling window."""
    if not field:
        return
    state.last_asked_fields = (state.last_asked_fields + [field])[-window:]
