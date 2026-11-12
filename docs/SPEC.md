# VOA · Demo de automações de receita recorrente

Demo comercial para apresentar à VOA (voa.com.pt, ionizadores de água, Portugal). Dados fictícios.
Envio real de WhatsApp SÓ para o número de demonstração do Rafael. Aprovado em 12/11/2026.

## 3 réguas
1. **Mês grátis APOLLO**: D0 boas-vindas · D7 check-in · D21 faltam 7 dias · D28 último dia → "continue por 1€/dia*, responda SIM".
2. **Filtro e manutenção**: aviso antes da troca (escolha de horário 1/2/3) · confirmação do horário · upsell SHOWER ATHENA depois da visita.
3. **Cobrança do aluguer**: aviso no dia da falha · lembrete · último aviso, com link de pagamento SIMULADO.

## Contrato entre as peças (não mudar sem atualizar aqui)

Arquivos na raiz do projeto:

| Arquivo | Dono | Conteúdo |
|---|---|---|
| `config.json` | backend | prazos (marcados "confirmar com a VOA") + bloco `envio` |
| `clientes.json` | backend | 8 clientes fictícios portugueses |
| `mensagens.json` | copy (sessão principal) | textos por chave, com `{nome}`, `{produto}`, `{data}`, `{horario1..3}` |
| `motor.py` | backend | regras das réguas |
| `server.py` | backend | HTTP, só stdlib |
| `painel.html` | painel | UI única, sem build |
| `tests/` | backend | unittest |

### config.json
Os valores reais de `envio` e `servidor` ficam em `config.local.json` (fora do git), que o `server.py` sobrepõe ao `config.json`.
```json
{
  "data_inicio_demo": "2026-11-12",
  "teste": {"dias": [0, 7, 21, 28]},
  "filtro": {"meses_troca": 12, "aviso_dias_antes": 7, "_obs": "confirmar com a VOA"},
  "cobranca": {"dias_apos_falha": [0, 3, 7], "_obs": "confirmar com a VOA"},
  "envio": {"instancia": "rafael-business", "owner_esperado": "<número da instância>", "destino_unico": "<número de demonstração>"},
  "servidor": {"porta": 8792, "hosts": ["127.0.0.1", "<IP Tailscale>"]}
}
```

### clientes.json
Lista de objetos:
`{"id": "c1", "nome": "Ana Ferreira", "cidade": "Lisboa", "produto": "APOLLO", "tipo": "teste"|"aluguer", "inicio_teste": "YYYY-MM-DD"|null, "data_instalacao": "YYYY-MM-DD", "ultima_troca_filtro": "YYYY-MM-DD"|null, "pagamento_falhou_em": "YYYY-MM-DD"|null}`
Os 8 clientes são escolhidos para que, avançando de 0 a 30 dias a partir de `data_inicio_demo`, TODAS as chaves de mensagem disparem pelo menos uma vez. Nenhum cliente guarda telefone: o destino vem sempre de `config.envio.destino_unico`.

### Chaves de `mensagens.json`
`teste_d0`, `teste_d7`, `teste_d21`, `teste_d28`, `filtro_aviso`, `filtro_confirmacao`, `filtro_upsell_shower`, `cobranca_d0`, `cobranca_d3`, `cobranca_d7`.
Regra do filtro: `filtro_aviso` em (troca − aviso_dias_antes); `filtro_confirmacao` no dia seguinte ao aviso (resposta do cliente simulada); `filtro_upsell_shower` no dia seguinte à troca. Troca = `ultima_troca_filtro` (ou `data_instalacao`) + `meses_troca`.

### motor.py
`eventos_do_dia(data: date, clientes, config, mensagens) -> list[dict]` com `{"cliente_id", "regua": "teste"|"filtro"|"cobranca", "chave", "texto"}`. Função pura, sem I/O.

### server.py (porta 8792, escuta só nos hosts do config)
- `GET /` → `painel.html`
- `GET /api/estado?dia=N` (N = dias desde `data_inicio_demo`, 0..60) → `{"data": "YYYY-MM-DD", "dia": N, "clientes": [...], "eventos": [...], "linha_do_tempo": [eventos de todos os dias 0..N]}`
- `POST /api/enviar` body `{"cliente_id", "chave", "dia"}` → o servidor RECALCULA o texto pelo motor (nunca aceita texto vindo da página); evento inexistente naquele dia = 400. Envia pela Evolution só para `destino_unico`. Antes: `connectionState` = `open` e `ownerJid` contém `owner_esperado`; senão 503 com o motivo. Resposta `{"key_id", "status"}`.
- `GET /api/status?key_id=...` → `{"status": "PENDING"|"SERVER_ACK"|"DELIVERY_ACK"|"READ"|"ERROR"|"desconhecido"}`
- Chave da Evolution lida de `~/.zxlab-mission-control/.env` (`EVOLUTION_API_KEY`) no servidor; nunca vai para a página nem para log.
- Um envio por pedido; nada de lote, nada de agendamento.

### painel.html
Página única servida pelo `server.py`, chamadas para as rotas acima. Cores VOA: #7CBEEE, #235560, #1ca7b2, #123f48; fontes Manrope + Inter; logo em texto "VOΛ". Telas: lista de clientes com o estado de cada régua, botões "avançar 1 dia" / "voltar" / "dia 0", linha do tempo, telemóvel com a conversa do cliente selecionado e botão "Enviar para o meu WhatsApp" por mensagem, mostrando o status (PENDING = "não confirmado"). Português de Portugal. Funciona no celular. Modo estático: se `/api/estado` não responder, usa `demo-estatica.json` (gerado pelo backend) e esconde o botão de envio.

## Fora do escopo
CRM/cobrança reais da VOA, agendamento automático, respostas automáticas do cliente.
