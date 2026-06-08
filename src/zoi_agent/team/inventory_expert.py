"""EstoqueExpert — Agno Agent especialista no estoque AMC.

Recebe contexto da conversa, raciocina sobre os ~36 veículos do pátio (no system
prompt), e devolve InventoryDecision estruturada. Patricia (Team leader) consome
essa decisão pra tecer bolhas finais.

Decisões arquiteturais:
  - Inventário INTEIRO no system prompt (formato compacto, ~80 chars/veículo).
  - Sem queries / filtros determinísticos — raciocínio LLM-puro sobre o conjunto.
  - Tools de leitura: puxar_ficha_veiculo (spec completa) + preparar_fotos_veiculo.
  - Output: InventoryDecision (action + veiculos_selecionados + hint_narrativo).
"""
from __future__ import annotations

from typing import Any

from agno.agent import Agent
from agno.models.openai import OpenAIChat
from agno.tools import tool

from zoi_agent.config import settings
from zoi_agent.logging import get_logger
from zoi_agent.team.schemas import InventoryDecision
from zoi_agent.tools.inventory import get_vehicle_details, load_inventory

log = get_logger(__name__)


# --- Inventory snapshot helpers --------------------------------------------


def _fmt_preco(preco: Any) -> str:
    try:
        v = float(preco)
        return f"R${v/1000:.1f}k"
    except (TypeError, ValueError):
        return "?"


def _fmt_km(km: Any) -> str:
    try:
        v = int(km)
        return f"{v//1000}k km" if v >= 1000 else f"{v} km"
    except (TypeError, ValueError):
        return "?"


def format_inventory_snapshot(inv: list[dict]) -> str:
    """Lista compacta do inventário pra system prompt do EstoqueExpert.

    Formato por veículo (~80-110 chars):
      [ID] Marca Modelo Versão | ANO | KMk km | cambio | combustivel | R$Xk | categoria | cor | opc1, opc2, opc3
    """
    lines: list[str] = []
    for v in inv:
        eid = v.get("external_id") or "?"
        marca = (v.get("marca") or "").strip()
        modelo = (v.get("modelo") or "").strip()
        versao = (v.get("versao") or "").strip()
        ano = v.get("ano") or "?"
        km = _fmt_km(v.get("quilometragem"))
        cambio = (v.get("cambio") or "?").lower()
        comb = (v.get("combustivel") or "?").lower()
        preco = _fmt_preco(v.get("preco"))
        cat = (v.get("carroceria") or v.get("categoria") or "").strip()
        cor = (v.get("cor") or "").strip()
        opc_list = v.get("opcionais") or []
        opc = ", ".join(str(o) for o in opc_list[:3])
        nome = " ".join(p for p in [marca, modelo, versao] if p)
        lines.append(
            f"- [{eid}] {nome} | {ano} | {km} | {cambio} | {comb} | {preco} | {cat} | {cor} | {opc}"
        )
    return "\n".join(lines)


# --- Tools (Agno) -----------------------------------------------------------


@tool
async def puxar_ficha_veiculo(external_id: str) -> dict[str, Any]:
    """Puxa ficha técnica COMPLETA de UM veículo (specs, opcionais, descrição).

    Use SEMPRE que o lead pergunta característica específica (ex: 'tem direção
    elétrica?', 'tem multimídia?', 'qual a cor?') antes de afirmar. Se a ficha
    não traz a info, devolva indicação clara — NÃO invente. A Patricia vai
    posicionar como 'vou confirmar com o consultor' nesses casos.

    Args:
        external_id: ID do veículo no inventário (o '[ID]' do snapshot).

    Returns:
        Dict com todos os campos do veículo, ou {"error": "..."} se não achar.
    """
    d = await get_vehicle_details(external_id)
    if not d:
        log.warning("puxar_ficha_veiculo_not_found", external_id=external_id)
        return {"error": f"veículo {external_id} não encontrado no inventário"}
    return d


# --- Agent builder ----------------------------------------------------------


_INSTRUCTIONS = [
    "Você é o EstoqueExpert da AMC Veículos — especialista no pátio.",
    "Você NÃO fala direto com o lead. Sua função é decidir QUAIS veículos "
    "apresentar (ou se não cabe apresentar agora) e devolver InventoryDecision "
    "estruturada pra Patricia (Team leader) tecer as bolhas finais.",
    "",
    "## REGRAS DE DECISÃO (action)",
    "1. `mostrar_card_unico`: APENAS 1 veículo claramente é o foco — ex: lead "
    "nomeou modelo específico e existe match exato no estoque, OU lead engajou "
    "num veículo já apresentado e você quer reforçar.",
    "2. `mostrar_card_lista`: 2 a 3 veículos comparáveis fazem sentido — ex: "
    "lead pediu 'SUVs até 80k' e há 2-3 opções.",
    "3. `comentar_em_texto`: o veículo já foi apresentado antes (está em "
    "vehicles_shown), e o lead pediu spec específica OU está conversando sobre "
    "ele — NÃO re-renderize card, responda em prosa. Ex: lead pergunta 'tem "
    "direção elétrica?' do veículo já no foco.",
    "4. `perguntar_refinamento`: SOMENTE quando o pedido é amplo demais E "
    "aplicar direto resultaria em mais de ~5 veículos. Ex: lead disse "
    "'queria um seminovo qualquer' — pergunte faixa de preço OU uso (cidade/"
    "estrada/família/trabalho). Se lead já especificou bem (modelo, faixa, "
    "câmbio, ou conjunto pequeno casa), NÃO pergunte — mostre direto.",
    "5. `nao_mostrar`: o turno do lead NÃO é sobre estoque (ex: ele pergunta "
    "endereço, horário de atendimento, está em qualificação pessoal). Devolva "
    "veiculos_selecionados=[].",
    "",
    "## ANTI-ALUCINAÇÃO",
    "- Se lead pergunta característica específica (direção elétrica, multimídia, "
    "tipo de pneu, etc) E você não tem certeza pela linha resumo, USE a tool "
    "`puxar_ficha_veiculo` antes de decidir. Não chute.",
    "- Se a ficha NÃO traz a info, devolva hint_narrativo='ficha não confirma "
    "essa característica — sinalizar verificação com consultor'.",
    "- NUNCA invente external_id. Use apenas IDs que aparecem no snapshot "
    "INVENTÁRIO atual.",
    "",
    "## ANTI-REPETIÇÃO",
    "- Veja `state.vehicles_shown` no input: veículos JÁ apresentados.",
    "- NÃO repita o mesmo card se já foi mostrado (use `comentar_em_texto` "
    "se for sobre ele) OU escolha veículo diferente quando lead pediu 'outras "
    "opções'.",
    "",
    "## MODELO ESPECÍFICO NOMINADO (regra suprema)",
    "Quando o lead nomeia marca/modelo específico (ex: 'tem algum FOX?'), "
    "esse desejo VENCE qualquer outro contexto (origem do anúncio, etc).",
    "- Se há match exato no estoque -> `mostrar_card_unico` (ou lista, se 2-3).",
    "- Se NÃO há match -> hint_narrativo='não temos esse modelo, mas separei "
    "alternativas próximas' + escolha 1-3 parecidos por categoria/faixa de "
    "preço/uso similar.",
    "",
    "## REFINAMENTO — quando perguntar",
    "Critério único: pedido amplo + muitos candidatos (>5). Exemplos:",
    "- Lead: 'tem alguma SUV?' e há 8 SUVs -> perguntar refinamento (preço? "
    "câmbio? uso?).",
    "- Lead: 'SUVs até 80k automática' e há 2 SUVs assim -> NÃO refine, mostre.",
    "- Lead: 'queria um pra trabalhar' e há ~10 utilitários -> perguntar "
    "(carga, cidade vs estrada, faixa).",
    "Sua pergunta deve ser CURTA e UMA dimensão por vez.",
    "",
    "## hint_narrativo (campo opcional, MUITO útil)",
    "Use pra passar pra Patricia o ÂNGULO de venda baseado na história do "
    "lead. Ex: 'lead falou em fretes — posicione por robustez e km baixo'.",
    "",
    "## FOTOS (`enviar_fotos_de`)",
    "- Preencha com o external_id do veículo SOMENTE quando o lead pediu "
    "foto explicitamente ('manda foto', 'me mostra', 'tem foto?') OU "
    "quando você decidiu que mostrar foto vai converter melhor.",
    "- O orquestrador envia as fotos em paralelo, sob shield — você não "
    "precisa chamar nenhuma tool de envio.",
    "- NÃO envie foto do mesmo veículo se ele já recebeu foto nesta "
    "conversa (use vehicles_shown como pista).",
    "",
    "## texto_sugerido_apresentacao (opcional, raro)",
    "Use SOMENTE quando você tem uma frase de abertura muito boa em mente. "
    "Patricia pode reescrever. Em dúvida, deixe None.",
    "",
    "## motivo_geral (obrigatório, NÃO vai pro lead)",
    "Raciocínio interno em 1-2 frases. Vai pros logs. Ex: 'lead engajou na "
    "Montana, perguntou direção. Ficha confirma hidráulica. Comentar em texto.'",
]


async def build_inventory_expert() -> Agent:
    """Constrói uma instância do EstoqueExpert com snapshot atual do inventário.

    Chamado uma vez por turno (cache TTL do inventário 5min cobre múltiplas
    construções sem custo de API GHL extra).
    """
    inv = await load_inventory()
    snapshot = format_inventory_snapshot(inv)
    inventory_context = (
        f"INVENTÁRIO ATUAL DA AMC ({len(inv)} veículos ativos):\n"
        f"Formato: [external_id] Marca Modelo Versão | ano | km | câmbio | "
        f"combustível | preço | categoria | cor | top-3 opcionais\n\n"
        f"{snapshot}"
    )

    return Agent(
        name="EstoqueExpert",
        model=OpenAIChat(id=settings.openai_model_inventory_expert),
        description="Especialista no estoque da AMC Veículos (Joinville/SC).",
        role="Decide quais veículos apresentar dado o contexto da conversa.",
        instructions=_INSTRUCTIONS,
        additional_context=inventory_context,
        tools=[puxar_ficha_veiculo],
        output_schema=InventoryDecision,
        markdown=False,
        telemetry=False,
    )
