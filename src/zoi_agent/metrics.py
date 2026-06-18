from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

TURNS_TOTAL = Counter(
    "zoi_turns_total",
    "Total de turnos processados pelo agente",
    ["stage", "intent"],
)

HANDOFF_TOTAL = Counter(
    "zoi_handoff_total",
    "Total de handoffs disparados",
    ["reason"],
)

QUALIFICADOS_TOTAL = Counter(
    "zoi_qualificados_total",
    "Leads qualificados",
    ["com_agenda"],
)

ABANDONED_TOTAL = Counter(
    "zoi_abandoned_total",
    "Sessões encerradas por abandono (endpoint /abandon)",
)

LLM_LATENCY = Histogram(
    "zoi_llm_latency_seconds",
    "Latência das chamadas LLM",
    ["component"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 4, 8, 16),
)

GHL_REQUEST_LATENCY = Histogram(
    "zoi_ghl_request_latency_seconds",
    "Latência das chamadas GHL",
    ["operation"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 4),
)


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
