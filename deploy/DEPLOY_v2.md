# Deploy v2 — Telemetria canônica (envelope v1)

> Atualização de uma instância **que já roda Fase 1/2** em `lucas-amc.appzoi.com.br`.
> Inclui migração de schema obrigatória (`create_all` NÃO altera tabela existente).
> Sem novas dependências Python (`pyproject` inalterado) → imagem builda igual.

## Pré-checagem (na VPS)
```bash
cd /opt/lucas-amc
git log --oneline -1            # confirmar que o pull trará o v2
docker compose -f deploy/compose.prod.yml ps   # postgres + app de pé
```

## 1. Backup do banco (OBRIGATÓRIO antes de migrar)
```bash
cd /opt/lucas-amc/deploy
docker compose -f compose.prod.yml exec -T postgres \
  pg_dump -U zoi zoi_agent | gzip > /var/backups/lucas-amc-pre-v2-$(date +%F-%H%M).sql.gz
```

## 2. Puxar o código v2
```bash
cd /opt/lucas-amc && git pull
```

## 3. Aplicar a migração de schema (ANTES de subir a app v2)
`create_all` é aditivo-only: cria tabela nova mas não altera `agent_events` existente,
nem reformula `pricing`. A migração faz isso. `pricing` é só seed → recriada no boot.
```bash
cd /opt/lucas-amc/deploy
docker compose -f compose.prod.yml exec -T postgres \
  psql -U zoi -d zoi_agent < migrations/v2_canonical_envelope.sql
```
Esperado: `BEGIN ... COMMIT` sem erro. (Backfill de `event_id`/`client`/`occurred_at`
nas linhas antigas + DROP de `pricing`.)

## 4. Rebuild + subir a app v2
Boot recria `pricing` (forma canônica) e roda `seed_pricing()`.
```bash
docker compose -f compose.prod.yml up -d --build app
docker compose -f compose.prod.yml logs --tail=40 app   # ver "db_schema_ready"
```

## 5. Validar
```bash
# Health
curl -fsSL http://127.0.0.1:8000/health        # {"status":"ok","db":true}

# Pricing canônico recriado
docker compose -f compose.prod.yml exec -T postgres \
  psql -U zoi -d zoi_agent -c \
  "SELECT model, kind, price_usd, usd_brl_rate, pricing_version FROM pricing ORDER BY 1,2;"
# esperado: gpt-4o(input 2.50/output 10), gpt-4o-mini(0.15/0.60),
#           whisper-1(audio_minute 0.006); rate 5.40; version 2026-06-17

# Linhas antigas migradas: event_id único, client preenchido
docker compose -f compose.prod.yml exec -T postgres \
  psql -U zoi -d zoi_agent -c \
  "SELECT count(*) total,
          count(*) FILTER (WHERE event_id IS NULL) sem_id,
          count(*) FILTER (WHERE client<>'amc') client_errado,
          (count(DISTINCT event_id)=count(*)) id_unico
   FROM agent_events;"
# esperado: sem_id=0, client_errado=0, id_unico=t
```

Smoke de evento novo (após 1 turno real do agente):
```bash
docker compose -f compose.prod.yml exec -T postgres \
  psql -U zoi -d zoi_agent -c \
  "SELECT event_type, schema_version, client, cost_usd, cost_brl, usd_brl_rate
   FROM agent_events WHERE event_type='LLM_CALL' ORDER BY id DESC LIMIT 1;"
# esperado: schema_version=1, client='amc', cost_brl>0
```

## Rollback
```bash
# 1) volta código
cd /opt/lucas-amc && git checkout 2b02953 && cd deploy
docker compose -f compose.prod.yml up -d --build app
# 2) (se preciso) restaura banco do backup do passo 1
gunzip -c /var/backups/lucas-amc-pre-v2-<TS>.sql.gz | \
  docker compose -f compose.prod.yml exec -T postgres psql -U zoi -d zoi_agent
```

## Notas
- App v1→v2 sem novas libs; se o build falhar é cache/registry, não dependência.
- A migração é idempotente (re-rodar não duplica colunas/linhas).
- `created_at` (insert) preservado; `occurred_at` (fato) = `created_at` nas linhas antigas.
