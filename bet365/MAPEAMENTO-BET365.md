# Mapeamento bet365.bet.br — varredura de 2026-06-13

Varredura feita via navegador real conectado (sessão logada, saldo R$5,00),
página **Ao-Vivo → Futebol** (`https://www.bet365.bet.br/#/IP/B1`).
`#/IP/B1` = In-Play (ao vivo), esporte `B1` = futebol no código interno deles.

## TL;DR — "tem API?"

**Não no sentido SofaScore.** O bet365 NÃO expõe uma API REST com JSON limpo de
odds. A arquitetura é:

| Camada | O que é | Dá pra consumir? |
|---|---|---|
| `www.bet365.bet.br/Api/1/Blob?...` | Config/localização (módulos, traduções, layout). Blob codificado. | Sim, mas **não tem odds** — só metadados de UI |
| **WebSocket de push (pserver/"zap")** | O dado ao vivo de verdade: odds, placares, minuto, mercados. Protocolo **proprietário delimitado** (não JSON), assinatura por tópicos. | Tecnicamente sim, mas brutalmente difícil + viola ToS |
| DOM renderizado | O navegador decodifica o WS e pinta a tela. | **Sim — caminho prático e confiável** |

**Conclusão:** o jeito sustentável de "varrer" o bet365 é **raspar o DOM já
renderizado** (`raspador_console.js`), não reimplementar o protocolo do WebSocket.

## Por que é diferente do SofaScore

- SofaScore = API REST JSON. Blindagem = Cloudflare (resolvido com curl-cffi /
  requisição natural via Playwright).
- bet365 = **sem REST de dados**. O dado chega por um **WebSocket de push binário**
  com encoding proprietário (delimitadores `\x01 \x02 \x08`, pares chave-valor,
  assinatura por "tópicos"). Reimplementar o decoder é trabalho de engenharia
  reversa contínua — eles mudam o protocolo de tempos em tempos justamente pra
  quebrar quem faz isso. Não vale a pena pro objetivo do projeto.

## O que apareceu na rede (85 requisições capturadas)

### bet365 (próprio)
- `GET www.bet365.bet.br/Api/1/Blob?33,www-sports,lsm-Default/3/...` — blobs de
  config dos módulos do sportsbook. O `33,www-sports` é o "grupo" e os pares
  `nome/versão/flags` (ex.: `ipe-BR/13`, `acc/5089`, `ipm/5055`) são os módulos
  in-play (ipe=in-play event, ipm=in-play main, ipn=in-play nav, ipv=in-play
  video...). Retorna o layout, NÃO as odds.
- `content001.bet365.bet.br/SoccerSilks/*.svg` — CDN de assets: camisas dos times
  (ex.: `Real Madrid Home 25_26 Front.svg`, `Arsenal_Home_23.svg`) e bandeiras
  (`zflag_*.svg`). Útil se um dia quiser logos/camisas.
- `www.bet365.bet.br/uicountersapi/...` — telemetria de contadores de UI (deu 503).

### Terceiros (telemetria/marketing — ignorar)
- `firebase.googleapis.com` — config do Firebase Web SDK
- `optimove.net` (web-popup, stream, realtime) — plataforma de CRM/marketing
- `googletagmanager.com` + `analytics.google.com` — Google Analytics (GA4, deu 503)

### WebSocket
**Não aparece na captura HTTP** — o leitor de rede pega XHR/Fetch/documentos, mas
não os frames do WebSocket. A instância do socket vive num *closure* fechado do
bundle JS (inacessível por varredura de `window`). Para pegar a URL + frames brutos,
use Playwright com `page.on("websocket")` → ver `ws_sniffer.py`.

## Globais JS relevantes (engine do app)

- `ns_gen5_config` → `PushedConfigManager`, `ApplicationConfig`, `Domain`.
  `PushedConfigManager.prototype` tem `subscribe`, `initialiseSubscription`,
  `subscribeToProtectedTopic`, `getIsInPlayAvailable`, `getIsPushBalanceEnabled`...
  → confirma o modelo de **assinatura por tópicos sobre o WebSocket**.
- `appLib`, `AppLib`, `AppLibX`, `appModule` — engine "gen5" do sportsbook.

## Estrutura do DOM (in-play overview) — base do raspador

```
.ovm-Competition                     (1 por liga; 45 ligas no snapshot)
  .ovm-CompetitionHeader             "Áustria - Regionalliga\n1\nX\n2"
  .ovm-FixtureList
    .ovm-Fixture                     (1 por jogo; 76 jogos no snapshot)
      [class*="TeamName"]            nomes dos 2 times
      .ovm-ScorePill                 (2 = placar casa / fora)
      .ovm-InPlayTimer               minuto ("84:56")
      .ovm-ParticipantOddsOnly       (3 = odd 1 / X / 2)
```

Snapshot de exemplo (2026-06-13 ~13:40): 76 jogos ao vivo, 45 ligas. Maior
concentração: Polônia IV Liga (11 jogos), Áustria Regionalliga (4), Marrocos GNF 2 (3).

## Avisos

- **ToS:** o bet365 proíbe scraping/automação nos Termos. Uso pessoal de leitura é
  uma coisa; redistribuir ou automatizar apostas é outra. Manter como leitura de
  dados próprios, baixa frequência.
- As odds são **decimais** (ex.: `1.008`, `26.00`) — formato europeu, já prontas.
  Diferente do SofaScore que vinha fracionário.
- Não automatizar cliques de aposta. O raspador é **somente leitura**.

## Alertador v2 — sinal de aposta no fim do jogo (tiers + freshness)

Sistema de sinalização AO VIVO somente-leitura. **NÃO aposta** — sinaliza a janela,
você clica. Construído sobre a constatação dura: *"GREEN 40s antes do apito" é
impossível* (o apito é imprevisível, o acréscimo anunciado é só um piso, o relógio
do bet365 deriva). Então o GREEN = *"fundo nos acréscimos, mercado aberto, relógio
verificado, todos os critérios batidos"*.

### Modelo (calibrado no futebol.db)
`calibrar_hazard.py` lê o banco e gera `hazard_cal.json`: curva de hazard de gols
por minuto (o fim de jogo é ~1,7× a média — bucket 90+ é o mais quente),
`home_share=0.549`, acréscimo como distribuição (PMF). `prob_aovivo.py` e
`alertador_valor.js` usam: integra λ(t) no tempo restante, marginaliza o acréscimo,
aplica multiplicadores (placar/vermelho/momentum), convolui 2 Poisson → P(1/X/2) +
P(resultado se mantém) + **banda de confiança** (alarga quando a entrada é incerta).
Re-calibrar: `py bet365/calibrar_hazard.py` (quando o backfill acumular mais jogos).

### Blend de força-por-time (ataque × defesa) — opcional
Quando há dado de time, ajusta o modelo global: λ do confronto via Dixon-Coles
(`atq_casa×def_fora/média_casa`, `atq_fora×def_casa/média_fora` — convenção do
[probabilidades.py](../probabilidades.py)). Disso sai **nível** `r=total/2.27` e
**share** `λ_casa/total`, cada um **shrinkado** por `w=min(0.85, n/(n+5))`. Sem
dado de time (ou n<3) → `r=1, share=0.5489` = **modelo global EXATO** (fallback).
Fontes: `prob_aovivo.py --evento <id>` (do `futebol.db`, média de liga real) e
`sofascore_live.py forca/servir --forca` (do SofaScore ao vivo, baseline = ambiente
de gols dos 2 times). Python e JS dão números **idênticos** (paridade testada).
H2H é botão opcional (`peso_h2h`, default 0 — a medir com `validar_sinais.py`, não
embutido por padrão: confronto direto é sinal fraco, ~ruído no fim de jogo).
NOTA: a força é efeito de 2ª ordem no fim (o placar domina); ajuda mais em
WATCH/ARM e em jogo equilibrado entre times desiguais.

### Momentum (pressão ao vivo) — opcional, via SofaScore
Multiplicador `momC/momF` (default 1.0). O SofaScore **não tem "ataques perigosos"**
(métrica bet365/Sportradar); usamos equivalentes mais fortes do `/event/{id}/statistics`:
**xG (0.40), finalizações no gol (0.25), toques na área (0.20), total de finalizações
(0.10), posse (0.05)** — posse pesa pouco (engana). Calcula a *share* do mandante em
cada métrica (clampada [0.15,0.85] anti-ruído), média ponderada → `ph`; `momC=exp(0.5·2·(ph−0.5))`
limitado [0.6,1.7]. Time perdendo pressionando → seu λ sobe → P(resultado se manter)
cai (testado: 1-0 89' com fora pressionando 91,7%→88,4%). Usa o 2T (recente), cai pro
jogo todo; cache 25s; só jogos ≥ gate (75'). Liga com `sofascore_live.py servir --momentum`.
Teste: `py bet365/sofascore_live.py momento <event_id>`.

### Tiers (alertador_valor.js)
`IDLE → WATCH(80') → VALOR(75'+) → ARM(88'+, valor✓, mercado aberto) → GREEN`. GREEN só com:
minuto ≥ `90 + (acréscimo − buffer)` (ou 92' se desconhecido), `fr.strict`, mercado
aberto, sem surto de gol, confirmado por 2 scans. Precedência `GREEN>ARM>VALOR>WATCH`.
Cor: cinza/roxo(VALOR)/âmbar/verde-pulsante. CASA=mandante (col.1), EMPATE=X, FORA=visitante
(col.2). No VALOR/ARM/GREEN, **acende a célula exata** (`.ovm-ParticipantOddsOnly[idx]`) do
resultado dominante = onde clicar.

### v2.7 — precisão + sinal mais cedo (5 alavancas; teto honesto −EV permanece)
- **Cartão vermelho (A):** `lerVermelhos(fx)` lê o marcador 🟥 do DOM (lado pela posição vertical
  vs os 2 `TeamName`; 0/0 se não separar) → alimenta `redC/redF` no `probs()` (antes era fixo 0).
  Fallback SofaScore `/event/{id}/incidents` (`vermelhos_confronto`). Badge mostra `🟥c-f`.
- **Banda de confiança (B):** `bandaProbsIdx` (porta de `banda_confianca`) escala Λ por `exp(±σ)`;
  `armOk` gateia no **limite inferior** `pLo` (não no ponto) quando `cfg.usarBanda`. Auto-exige
  colchão maior quanto mais cedo (banda mais larga). σ = base 0.30 + sem_stats 0.10 + por_mult 0.15
  (paridade Py↔JS verificada <1pp). Badge mostra `[lo–hi%]`. Toggle "banda" no painel.
- **Tier VALOR (C):** roxo, de `valorMin`(75'); dispara com `pLo ≥ probMin+valorMargin` E
  `oddTela/justo ≥ 1+valorEdge` E mercado aberto. **Não** é GREEN (mais variância), **sem** beep/log.
  Input `🎯 +pp` no painel.
- **Acréscimo real (D):** `lerAcrescimoPainel()` lê "90+N" do relógio do painel do jogo aberto
  (só `tot≥90` + nome casado) → melhora `greenMin/greenCeil`.
- **Loop de calibração (E):** `recalibrar_sinais.py` lê `sinal_log` liquidado e ajusta
  P_raw→P_calib (n<40 identidade; 40–79 Platt; ≥80 isotônica-PAV), **capado a ±0.15** e só
  deploya se melhora o Brier. Serve em `/correcao`; `aplicarCorrecao`(JS)/`aplicar_correcao`(Py)
  aplicam (idênticos, parity verificada). `sinal_log.prob` logado é o **cru** (não recorrige).
  `calibracao_correcao.json` no `.gitignore`.

### Travas de mercado (modelo só vale p/ 1X2 = "Resultado Final")
1. **Aba:** lê `.ovm-ClassificationMarketSwitcherMenu_Item-active`; se não for
   "Resultado Final" (ex.: "Próximo Gol"/"Partida - Gols"), não sinaliza e avisa na barra.
2. **Por-jogo:** ignora fixture cujo texto casa `/Marcar o \d|Próximo Gol/` (bet365 às
   vezes troca o 1X2 de um jogo por "Marcar o Xº Gol" — as 3 odds não seriam 1/X/2).
Scan varre `.ovm-Fixture` direto (funciona na view agrupada E na ordenada/classificação).

### Freshness (anti relógio-fantasma)
Relógio lido em `MM:SS`. Barra GREEN se: minuto > teto (`90+A+4` ou 98), relógio
congelado por ≥2 scans (CONGELADO), ou SofaScore diz `finished` (SS-FIM). Resolve o
bug do 118' (jogo encerrado marcado STALE em vez de "100% VALOR").

### Cross-check SofaScore (caminho completo)
`sofascore_live.py servir` sobe um servidor em `http://localhost:8765` (CORS) que
busca `/sport/football/events/live` (via `transporte.py`), casa os confrontos
(normalização de nome + Jaccard) e serve minuto/status/acréscimo autoritativos.
O alerter consome com `iniciarValor({ssUrl:'http://localhost:8765'})`. Mata GREEN
em jogo `finished`, confirma o relógio e usa o acréscimo anunciado real.
⚠️ A API do SofaScore toma 403 por reputação de IP após coleta pesada — esperar esfriar.

### Validação (sinal_log + P&L)
Com `ssUrl` ligado, cada GREEN é logado em `sinal_log` (via POST `/log` no servidor).
`validar_sinais.py` liquida contra o resultado real (tabela `evento` ou `--fetch`) e
mostra taxa de acerto, P&L/ROI e diagrama de confiabilidade (prob prevista vs real).
**Esperado −EV**: o sinal melhora timing/disciplina, não vira +EV. Limite de perda duro.

### Arquivos
`calibrar_hazard.py`, `hazard_cal.json`, `prob_aovivo.py`, `alertador_valor.js`,
`sofascore_live.py`, `validar_sinais.py`. Tabela `sinal_log` no `schema.sql`.

### Como rodar (caminho completo)
1. `py bet365/calibrar_hazard.py` (uma vez / quando recalibrar).
2. `py bet365/sofascore_live.py servir` (deixa rodando; precisa IP do SofaScore livre).
3. No console do bet365 (#/IP/B1, aba fresca): colar `alertador_valor.js` →
   `iniciarValor({ssUrl:'http://localhost:8765'})`. Sem SofaScore: só `iniciarValor()`.
4. Depois dos jogos: `py bet365/validar_sinais.py` pra ver o P&L real.
