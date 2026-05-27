"""Templates determinísticos pra renderizar veículos como bolhas pré-formatadas.

Decisão arquitetural: rendering em Python (não no prompt do LLM). Garante:
  - Consistência visual (mesmos emojis, mesmo formato, sempre)
  - Economia de tokens (LLM não reescreve cada campo)
  - Velocidade (orchestrator monta antes da chamada do responder)

Uso típico no orchestrator:
  if len(vehicles) == 1: bloco = render_vehicle_card(vehicles[0])
  elif len(vehicles) > 1: bloco = render_vehicle_list(vehicles)

O bloco resultante é enviado como UMA bolha pré-formatada, ANTES da bolha de
pergunta gerada pelo responder.
"""
from __future__ import annotations

from typing import Any


def _fmt_preco(preco: Any) -> str:
    if preco is None:
        return "—"
    try:
        v = float(preco)
        return f"R$ {v:,.0f}".replace(",", ".")
    except (TypeError, ValueError):
        return str(preco)


def _fmt_km(km: Any) -> str:
    if km is None:
        return "—"
    try:
        v = int(km)
        if v >= 1000:
            return f"{v / 1000:.0f}k km"
        return f"{v} km"
    except (TypeError, ValueError):
        return str(km)


def _fmt_km_full(km: Any) -> str:
    if km is None:
        return "—"
    try:
        v = int(km)
        return f"{v:,} km".replace(",", ".")
    except (TypeError, ValueError):
        return str(km)


def _short_cambio(cambio: str | None) -> str:
    if not cambio:
        return ""
    c = cambio.lower()
    if "automat" in c or "cvt" in c:
        return "automático"
    if "manual" in c or "mecanic" in c:
        return "manual"
    return cambio.lower()


def render_vehicle_card(v: dict[str, Any]) -> str:
    """Card rico pra 1 veículo (foco do lead). Retorna bolha pronta com
    quebras de linha. Aceita VehicleSummary.model_dump() ou dict cru do estoque."""
    titulo = v.get("titulo") or f"{v.get('marca') or ''} {v.get('modelo') or ''}".strip() or "Veículo"
    ano = v.get("ano") or "—"
    km = _fmt_km_full(v.get("quilometragem"))
    cambio = (v.get("cambio") or "—").capitalize()
    comb = (v.get("combustivel") or "—").capitalize()
    preco = _fmt_preco(v.get("preco"))
    opcionais = v.get("opcionais") or []
    if isinstance(opcionais, list) and opcionais:
        destaques = ", ".join(opcionais[:3])
    else:
        destaques = "—"

    lines = [
        f"🚗 *{titulo}*",
        "",
        f"📅 {ano}  •  🛣️ {km}",
        f"⚙️ {cambio}  •  ⛽ {comb}",
        f"💰 *{preco}*",
    ]
    if destaques and destaques != "—":
        lines.extend(["", f"✨ Destaques: {destaques}"])
    return "\n".join(lines)


def render_vehicle_list(vehicles: list[dict[str, Any]], header: str | None = None) -> str:
    """Lista compacta numerada (até 3). Retorna bolha pronta."""
    if not vehicles:
        return ""
    head = header or "Achei essas opções pra você:"
    badges = ["1️⃣", "2️⃣", "3️⃣"]
    lines: list[str] = [head, ""]
    for i, v in enumerate(vehicles[: len(badges)]):
        b = badges[i]
        marca = v.get("marca") or ""
        modelo = v.get("modelo") or ""
        ano = v.get("ano")
        if marca and modelo:
            nome = f"{marca} {modelo}"
        else:
            nome = v.get("titulo") or "Veículo"
        if ano:
            nome = f"{nome} {ano}"
        preco = _fmt_preco(v.get("preco"))
        km = _fmt_km(v.get("quilometragem"))
        cambio = _short_cambio(v.get("cambio"))
        meta = ", ".join(p for p in [km, cambio] if p)
        line = f"{b} *{nome}* — {preco}"
        if meta:
            line += f" ({meta})"
        lines.append(line)
    return "\n".join(lines)


def build_vehicle_blocks(
    *,
    exatos: list[dict[str, Any]],
    parecidos: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Decide qual template usar e retorna lista de bolhas prontas.
    - 1 exato -> card rico
    - 2+ exatos -> lista compacta
    - 0 exatos + parecidos -> aviso + lista parecidos
    - tudo vazio -> []
    """
    parecidos = parecidos or []
    blocks: list[str] = []
    if len(exatos) == 1:
        blocks.append(render_vehicle_card(exatos[0]))
    elif len(exatos) >= 2:
        blocks.append(render_vehicle_list(exatos))
    elif parecidos:
        blocks.append("Não achei exatamente o que você pediu, mas seguem parecidas:")
        blocks.append(render_vehicle_list(parecidos))
    return blocks
