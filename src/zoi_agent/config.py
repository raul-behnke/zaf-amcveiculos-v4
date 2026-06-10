from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # OpenAI
    openai_api_key: str
    openai_model_updater: str = "gpt-4o"
    openai_model_responder: str = "gpt-4o"  # legacy — substituído por Patricia (Team leader)
    openai_model_patricia: str = "gpt-4o"  # Team leader (Agno)
    openai_model_inventory_expert: str = "gpt-4o"  # Team member especialista em estoque (Agno)
    openai_model_inventory_extractor: str = "gpt-4o-mini"  # legacy — substituído pelo EstoqueExpert
    openai_model_whisper: str = "whisper-1"

    # GHL
    ghl_pit_token: str
    ghl_location_id: str
    ghl_base_url: str = "https://services.leadconnectorhq.com"
    ghl_api_version: str = "2021-07-28"

    ghl_stock_custom_value_id: str
    ghl_faq_custom_value_id: str
    ghl_field_veiculo_interesse: str
    ghl_field_saudacao_prevendas: str
    ghl_calendar_id: str
    ghl_appointment_duration_min: int = 60
    ghl_handoff_workflow_id: str
    ghl_tag_agent_gate: str = "agente-ia"

    # Server
    webhook_secret: str
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_timezone: str = "America/Sao_Paulo"

    # Postgres
    database_url: str = "postgresql+asyncpg://zoi:zoi@localhost:5432/zoi_agent"

    # Cache TTL
    faq_cache_ttl_seconds: int = 300
    stock_cache_ttl_seconds: int = 300

    # Limits
    conversation_history_limit: int = 100
    inventory_search_limit: int = 10
    responder_max_bubbles: int = 3
    responder_sleep_min: float = 0.6
    responder_sleep_max: float = 1.2
    human_request_threshold: int = 2
    ai_identity_admit_at: int = 2

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"

    # Metrics
    metrics_enabled: bool = True
    metrics_port: int = 9090


settings = Settings()
