# Relatório de Interações em Produção — Patricia (AMC Veículos)

Base: 16 conversas reais (`conversations_dump.md`) + auditoria manual do operador.
Período: 05/06/2026.
Norma: `QUALIFICACAO_E_COMPORTAMENTO.md`.

---

## 1. Quadro geral

| Métrica | Valor |
|---|---|
| Conversas analisadas | 16 |
| Conversas sem interação (só evento de oportunidade) | 1 (Débora) |
| Conversas com tentativa real de qualificação pela IA | 15 |
| **Conversas interrompidas por agente humano (Patricia/Jonas/Ruhan)** | **13** |
| **% de interrupção humana sobre tentativas reais (13/15)** | **86,7%** |
| % de interrupção humana sobre o total (13/16) | 81,3% |
| Qualificações concluídas só pela IA (10/10 + agendamento) | 1 (Raul) — 6,7% |
| Qualificações parciais antes do takeover | 14 (média ~3/10 campos) |

Leitura: a IA opera como "pré-aquecedor". Em quase 9 de cada 10 atendimentos onde ela começa, um humano assume antes do fechamento — na maioria das vezes **antes mesmo da IA ter chance de fechar o funil**.

---

## 2. Aderência ao fluxo comercial (ordem PRIORITY)

| Padrão observado | Conversas | Diagnóstico |
|---|---|---|
| Pulou ou atrasou pergunta de **nome** | Delia, Neusa (tardia) | Quebra da ordem PRIORITY (campo #1). |
| Não respeitou **dado já fornecido** pelo lead (repergunta) | Selma (repetiu "compra direta ou troca" depois do lead já dizer que era troca + modelo + ano) | Viola "nunca repete dado já no state". |
| Avançou para próximo campo **ignorando pergunta aberta** do lead | Neusa ("dono homem ou mulher?" ignorado), Scheila (sequência KM-cidade-fotos-valor sem fechar nada) | Quebra da regra mestra (responde dúvida + 1 campo). |
| Pulou para qualificação **antes de apresentar veículo** com foco | Jorge (perguntou nome após apresentar 3 fotos, sem confirmar foco) | `vehicle_focus_definido` ainda falso. |
| Cumpriu ordem PRIORITY corretamente | Raul, parcialmente Neusa | Único fechamento limpo: Raul. |

**Aderência média ao fluxo: baixa**. Estimativa: ~30% das tentativas seguem PRIORITY sem ruptura.

---

## 3. Qualidade das respostas

### Acertos consistentes
- Envio inicial de ficha do veículo de origem com campos corretos do estoque (Neusa-EcoSport, Selma-Renegade, Cristiano-Cruze, Raul-Montana).
- Resposta a "quantos donos" usando dado real do estoque (Neusa).
- Filtro automático correto na primeira passada para Jorge (compactos < 50k).

### Falhas recorrentes de qualidade
| Falha | Casos | Gravidade |
|---|---|---|
| **Disparo de modelos sem sentido após objeção** ("nenhum me chamou atenção" e a IA segue listando) | Delia | GRAVE — soa robô, queima lead. |
| **Promessa comercial sem tool** ("até 18x no cartão") | Raul | GRAVE — anti-alucinação violada. |
| **Filtro composto falhou** (Mobi + automático → resultado errado) | Delia | GRAVE — `search_inventory` precisa fallback explícito. |
| **Ignorar pergunta do lead** e seguir funil mecanicamente | Neusa, Scheila | MÉDIA — fere rapport. |
| **Repergunta de campo já coletado** | Selma | MÉDIA — fere "nunca repete". |
| **Sequência caótica de perguntas** (KM → cidade → fotos → valor sem encadeamento) | Scheila | GRAVE — desorienta lead. |
| **Não trata "imagens internas/externas"** (envia tudo ou nada) | Jorge | LIMITAÇÃO TÉCNICA — não há flag no estoque. |
| **Veículo com apenas 1 foto** envia mesmo assim (era pra dizer "sem foto cadastrada") | Jorge (VW UP) | MÉDIA — desvio de regra do §7 do doc-base. |
| **Não oferece alternativa quando veículo está vendido** | Selma (Renegade vendido — IA encerrou em vez de oferecer parecidos) | GRAVE — perde lead qualificado. |

---

## 4. Capacidade de qualificação

| Conversa | Campos coletados antes do takeover | Observação |
|---|---|---|
| Raul | **10/10** + agendamento | Único caso ouro. |
| Delia | 0/10 | Lead irritado antes da IA pedir nome. |
| Neusa | 3/10 (nome, intenção, motivo iniciado) | Takeover por Ruhan no campo #7. |
| Scheila | 2/10 (interesse, troca-iniciada) | Sequência travou na troca. |
| Selma | 2/10 (interesse, troca-completa) | Repergunta destruiu confiança. |
| Jorge | 3/10 (interesse, foco-parcial, nome, intenção) | Takeover por Jonas no campo #5. |
| Solange | 1/10 (nome) | Lead pediu endereço → Patricia assumiu. |
| Cristiano | 1/10 (interesse) | Pedido de simulação → Ruhan. |
| Arildo, Maria, Ana, Jocelito, Sérgio, Deus Na Frente, Rogério | 0–1/10 | Takeover imediato pós-saudação. |
| Débora | 0/10 | Sem interação. |

**Média de campos coletados quando a IA segue até o fim ou até o takeover**: ~1,8/10.
Excluindo Raul e Débora: **~1,4/10**.

A IA **não chega a qualificar** na maioria dos casos — não porque falhe sempre, mas porque o time humano assume cedo (ver §6).

---

## 5. Falhas operacionais identificadas

### Técnicas (precisam de patch no produto)
1. **Filtro composto fraco em `search_inventory`** — falhou em "Mobi automático" (Delia).
2. **Sem capacidade de diferenciar fotos internas vs externas** — limitação do schema do estoque (Jorge).
3. **Regra "1 foto = não envia" não foi aplicada** no caso do VW UP (Jorge).
4. **Sem rotina de "veículo vendido → oferecer parecidos"** (Selma).
5. **Falta de detecção de pergunta pendente do lead** — IA atropela perguntas abertas (Neusa, Scheila).

### Comportamentais (precisam de ajuste de prompt)
1. **Promessa comercial sem dado** (Raul: "até 18x").
2. **Disparo de modelos em loop** após objeção (Delia) — falta "se lead negou, pare de listar e pergunte critério".
3. **Repergunta de campos já no state** (Selma).
4. **Não trata pedido de simulação financeira** — deveria ter uma rota clara (FAQ ou handoff explícito), hoje ou prevê demais ou escala mal (Neusa-Ruhan, Cristiano-Ruhan).

### De integração / governança
1. **Takeover humano sem critério padronizado** — Patricia/Jonas/Ruhan assumem em pontos diferentes, sem regra clara de "quando deixar a IA terminar".
2. **Saudação dispara mas IA é interrompida antes do 1º turno real** em 7 conversas (Maria, Ana, Solange, Jocelito, Sérgio, Deus Na Frente, Rogério) — saudação vira "ping" e o humano puxa.

---

## 6. Interrupção humana — onde, quando, por quê

| Tipo de interrupção | Casos | % do total |
|---|---|---|
| **Interrupção imediata** (humano assume logo após saudação, sem dar chance à IA) | Maria, Ana, Solange, Jocelito, Sérgio, Deus Na Frente, Rogério | 7/16 = 43,8% |
| **Interrupção precoce** (1–3 turnos, antes do funil avançar) | Arildo, Selma | 2/16 = 12,5% |
| **Interrupção justificada por escalonamento** (lead pediu simulação/ligação/humano) | Delia, Neusa, Cristiano, Scheila | 4/16 = 25,0% |
| **Sem interrupção** (IA tocou até o fim) | Raul, Débora | 2/16 = 12,5% |

**Conclusão**:
- **56,3%** das interrupções humanas são "preventivas" (imediatas ou precoces) — o time não confia que a IA vá fechar.
- **25%** são interrupções legítimas, onde a IA fez handoff implícito ou recebeu pedido que não sabe tratar.
- **Só 6,3%** (1 caso: Raul) representa qualificação 100% IA.

---

## 7. Recomendações priorizadas

| # | Ação | Impacto esperado |
|---|---|---|
| 1 | **Travar promessas comerciais** no prompt (parcelas, % entrada, financiamento) — toda menção vira "vou confirmar com o consultor". | Elimina alucinação tipo Raul-18x. |
| 2 | **Regra anti-loop**: após 1ª objeção a lista de modelos, parar de sugerir e perguntar critério explícito (orçamento, câmbio, ano). | Resolve Delia. |
| 3 | **Detector de pergunta pendente do lead** no updater: se lead fez pergunta aberta, responder ANTES de avançar funil. | Resolve Neusa, Scheila. |
| 4 | **Tool `vehicle_unavailable_fallback`**: quando estoque retorna 0 ou veículo marcado vendido, agente busca top 3 parecidos automaticamente. | Resolve Selma. |
| 5 | **Rota explícita para "simulação de financiamento"**: cria handoff `solicitou_simulacao` com nota dedicada, em vez de Ruhan ter que assumir cru. | Resolve Cristiano, Neusa, Scheila. |
| 6 | **Política de takeover humano formalizada**: combinar com o time quando a IA deve ser deixada terminar (ex.: até o gate de agendamento) vs quando entrar (irritação, simulação, pedido de humano). | Reduz interrupção preventiva (56% hoje). |
| 7 | **Memória de campo coletado**: bloqueio duro no responder para nunca emitir pergunta de campo já presente em `collected`. | Resolve Selma. |
| 8 | **Flag de "1 foto = não envia"** auditada nos testes — falhou na produção. | Resolve Jorge-UP. |

---

## 8. Veredito

A IA funciona como **disparador de saudação + ficha técnica + 1–2 perguntas de qualificação**, mas raramente fecha. Dois fatores se somam:

1. **Falhas próprias da IA** em 3 frentes: aderência ao funil, anti-alucinação comercial, tratamento de objeção/pergunta aberta.
2. **Cultura de takeover preventivo** do time humano: na metade dos casos, o humano nem deixa a IA tentar.

Antes de ampliar o escopo da Patricia, **calibrar o gate de takeover** com o time e patchar as 5 falhas técnicas do §5 — sem isso, qualquer melhoria de prompt fica invisível, porque a IA não chega ao fechamento.

Único caso de qualificação completa autônoma (Raul) prova que o pipeline funciona quando a IA é deixada operar.
