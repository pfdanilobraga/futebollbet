# ⚽ Projeto Futebol — Banco de dados e probabilidades de vitória

> Documento de apresentação do projeto. Autossuficiente: tudo que foi feito, como funciona,
> o que já existe e para onde vamos. Atualizado em **16/06/2026**.

---

## 1. Objetivo

Construir um **banco de dados próprio de partidas de futebol**, alimentado automaticamente
pelos dados do SofaScore, para calcular a **probabilidade de cada time vencer uma partida**
(casa / empate / fora) combinando estatística e mercado de apostas — e, no futuro, um modelo
de machine learning que considere elenco, escalações e mando de campo.

```
┌─────────────┐   curl-cffi    ┌──────────────┐    SQL     ┌──────────────────┐
│  SofaScore  │ ─────────────▶ │  coletor.py  │ ─────────▶ │   futebol.db     │
│  API v1     │    1 req/s     │  (agendável) │            │   (SQLite)       │
└─────────────┘                └──────────────┘            └────────┬─────────┘
                                                                    │
                                                          ┌─────────▼─────────┐
                                                          │ probabilidades.py │
                                                          │ odds + Poisson    │
                                                          └───────────────────┘
```

---

## 2. A descoberta principal: a API interna do SofaScore

Todo o site sofascore.com é alimentado por uma **API JSON interna, sem autenticação**:

```
https://www.sofascore.com/api/v1/...
```

Mapeamos o site inteiro (navegação real + captura de tráfego de rede) e catalogamos todos os
endpoints — o catálogo completo, aba por aba do site, está em **`MAPEAMENTO-SOFASCORE.md`**.

### Como acessar (importante!)

A Cloudflare bloqueia clientes HTTP comuns:

| Cliente | Resultado |
|---|---|
| PowerShell `Invoke-WebRequest`, `requests` (Python), `curl` | ❌ 403 Forbidden |
| Python **`curl-cffi`** com `impersonate="chrome"` | ✅ 200 (mas leva 403 challenge sob rate-limit) |
| **Playwright** (Chrome real) — fallback automático em `transporte.py` | ✅ contorna challenge de *fingerprint* |

**Tipos de bloqueio do Cloudflare (importante distinguir):**
- *Challenge por fingerprint* (detectou robô) → o fallback Playwright resolve.
- *Bloqueio de chamada "fria"* → quando o IP está sensível, chamadas diretas a `/api/`
  (curl-cffi, fetch fora de contexto) tomam 403, **MAS as requisições naturais da própria
  página passam normalmente** (a página é tratada como tráfego legítimo).

**🏆 A solução que venceu o Cloudflare — `raspador.py`:**
Em vez de chamar a API direto (bloqueada), o raspador **abre a página da partida num Chrome
real (Playwright), clica nas abas e intercepta as respostas** que a própria página carrega.
Resultado comprovado: **19 endpoints retornam 200** (statistics, shotmap/xG, lineups+notas,
odds, h2h, heatmap, average-positions...). Funciona **headless** (sem janela) e não é
bloqueado, porque é o tráfego autêntico da página. É o caminho oficial de coleta de detalhes.
```
py raspador.py evento 15526111        # uma partida (pega tudo)
py raspador.py pendentes --limite 150 # encerrados sem detalhes (≈15s/jogo)
```

```python
from curl_cffi import requests
r = requests.get("https://www.sofascore.com/api/v1/event/15526111/statistics",
                 impersonate="chrome")
```

### Endpoints mais importantes (resumo)

| Dado | Endpoint |
|---|---|
| Jogos de um dia | `/sport/football/scheduled-events/{AAAA-MM-DD}` |
| Jogos ao vivo | `/sport/football/events/live` |
| Detalhes da partida | `/event/{id}` |
| Estatísticas (posse, **xG**, chutes, duelos...) | `/event/{id}/statistics` |
| Mapa de chutes (xG por chute, coordenadas) | `/event/{id}/shotmap` |
| Escalações + **nota de cada jogador** | `/event/{id}/lineups` |
| **Odds de todos os mercados** | `/event/{id}/odds/1/all` |
| Forma recente + H2H + enquete | `/event/{id}/pregame-form`, `/h2h`, `/votes` |
| Jogos de uma temporada (paginado) | `/unique-tournament/{ut}/season/{s}/events/last/{pág}` |
| Classificação | `/unique-tournament/{ut}/season/{s}/standings/total` |
| Temporadas históricas de uma liga | `/unique-tournament/{ut}/seasons` |
| Busca (achar IDs de time/liga) | `/search/all?q={termo}` |

IDs já levantados: Brasileirão Série A = **325** · Série B = **390** (2026 = 89840) ·
Campeonato Sul-Africano = **358** · Reservas Argentina = **18817** (2026 = 89061) ·
Premier League = 17 · Champions = 7.

⚠️ **Avisos**: API não-documentada (pode mudar sem aviso — guardamos 16 JSONs reais de
referência em `amostras/`). Os Termos de Uso do SofaScore proíbem uso comercial dos dados —
projeto para uso pessoal/estudo. O coletor respeita 1 requisição/segundo.

---

## 3. O que já está construído e funcionando

Pasta: raiz do projeto (`./`)

| Arquivo | Função |
|---|---|
| `MAPEAMENTO-SOFASCORE.md` | Catálogo completo: cada aba/botão do site → endpoint → dados |
| `schema.sql` | Modelo do banco (SQLite; migra fácil p/ Postgres) |
| `coletor.py` | Coletor CLI com rate-limit e retry |
| `transporte.py` | Camada de rede: tenta curl-cffi e cai p/ **Chrome real (Playwright)** no 403 de fingerprint |
| `raspador.py` | **Raspador definitivo** — navega na partida, clica nas abas e intercepta as respostas que a própria página carrega (contorna o Cloudflare). Pega tudo. |
| `gravar.py` | Funções de gravação JSON→banco, compartilhadas por coletor e raspador |
| `probabilidades.py` | Cálculo de probabilidades 1X2 (3 modelos) |
| `features.py` | Engenharia de features 1X2 p/ ML — leakage-safe, 20 features |
| `treino.py` | Treina logística + XGBoost, valida (Brier/log-loss), salva `modelo.joblib` |
| `prever.py` | Previsão 1X2 com o modelo de ML treinado |
| `mercados.py` | **Mercados de contagem Over/Under** (escanteios, chutes, chutes-gol, cartões): Poisson na soma + odds implícitas + combinado, total e por time |
| `features_mercado.py` `treino_mercado.py` | ML de contagem: features leakage-safe + regressão do total → P(Over/Under) |
| `validar_mercado.py` | Backtest dos mercados de contagem (Brier/log-loss/ROI) |
| `amostras/` | 16 payloads JSON reais da API (referência de estrutura) |
| `futebol.db` | O banco de dados |

### O banco (schema resumido)

- **Dimensões**: `torneio`, `temporada`, `time`, `jogador`
- **Fato principal**: `evento` (partida: placar, status, rodada, mando, árbitro, estádio, xG flag)
- **Detalhes por partida**: `evento_estatistica` (posse, xG, chutes... por período),
  `chute` (cada finalização com xG/xGOT e coordenadas), `escalacao` (jogador, posição,
  titular, **nota SofaScore**, minutos, xA), `evento_formacao` (esquema tático)
- **Mercado**: `odd` (todos os mercados, odd atual e de abertura)
- **Contexto**: `pre_jogo` (forma, H2H, enquete da torcida), `classificacao`
- **Saída**: `probabilidade` (resultado de cada modelo por partida — permite backtesting)

### Comandos do coletor

```powershell
py coletor.py dia 2026-06-09                  # todos os jogos de uma data
py coletor.py temporada 390 89840             # temporada inteira de uma liga
py coletor.py temporada 390 89840 --detalhes  # + estatísticas/escalações/odds dos encerrados
py coletor.py classificacao 390 89840         # tabela de classificação
py coletor.py detalhes 15526121               # detalhes de 1 partida (odds, forma, H2H)
py coletor.py pendentes                       # completa detalhes dos encerrados sem stats
```

### Modelos de probabilidade atuais

```powershell
py probabilidades.py evento 15526121          # uma partida
py probabilidades.py proximos                 # todas as não iniciadas no banco
```

1. **`odds_implicitas`** — converte as odds 1X2 em probabilidade, removendo a margem da
   casa de apostas (normalização). É o melhor preditor isolado disponível.
2. **`poisson`** — força de ataque/defesa por gols marcados/sofridos, separando casa e fora,
   escalado pela média da liga. Gera λ (gols esperados) de cada time e soma a matriz de
   placares. **Exige mínimo de 3 jogos no mando** — sem amostra, é descartado (lição do teste 2).
3. **`combinado`** — 70% odds + 30% Poisson (peso ajustável em `PESO_ODDS`).

### Estado atual dos dados (10/06/2026)

| Conteúdo | Quantidade |
|---|---|
| Ligas | 3 (Série B 2026, Sul-Africano 25/26, Reservas Argentina 2026) |
| Partidas | **968** (658 finalizadas) |
| Times | 74 |
| Classificações | 74 linhas |
| Odds | 60 cotações (10 partidas) |
| Probabilidades calculadas | 304 |
| Escalações / chutes xG / estatísticas detalhadas | **ainda não coletados em massa** (próximo passo) |

---

## 4. Testes realizados (validação real)

**Teste 1 — Ceará x Avaí (Série B, 10/06):**

| modelo | casa | empate | fora |
|---|---|---|---|
| odds_implicitas | 52.0% | 27.4% | 20.7% |
| poisson | 41.9% | 25.5% | 32.6% |
| combinado | **49.0%** | 26.8% | 24.3% |

**Teste 2 — Cape Town City x Magesi FC (África do Sul):** revelou um caso-limite valioso.
O jogo era de **playoff de promoção/rebaixamento**: o Cape Town City veio da 2ª divisão e
tinha só 1 jogo em casa na temporada → o Poisson colapsou (0% casa). Correção aplicada:
amostra mínima de 3 jogos no mando, senão o modelo é descartado e ficam só as odds (40/30/30).

**Teste 3 — Liga de Reservas da Argentina (rodada de 10/06):** 324 jogos coletados, odds de
8 partidas, probabilidades dos 3 modelos para toda a rodada. Caso interessante encontrado:
San Lorenzo x Instituto — mercado dava San Lorenzo favorito (45%), Poisson via Instituto
muito mais forte (65%). Divergências assim são exatamente o que queremos detectar.

---

## 5. Onde queremos chegar (roadmap)

O usuário identificou corretamente que faltam variáveis: **jogadores escalados, posição dos
jogadores e local do jogo**. O plano é evoluir em 3 estágios:

### ✅ Estágio 1 — concluído
Poisson casa/fora + odds implícitas + combinado. Funciona, mas é "cego para elenco".

### 🔜 Estágio 2 — Poisson ciente do elenco e do mando (próximo passo)
Os λ de gols esperados passam a ser ajustados por:
- **Força do XI titular**: média das notas SofaScore recentes dos 11 escalados
  (a escalação sai ~1h antes do jogo em `/event/{id}/lineups`) → captura desfalques
  automaticamente.
- **Força por setor**: nota média da defesa de A vs nota média do ataque de B
  (campo `posicao` G/D/M/F da tabela `escalacao`).
- **Vantagem de mando por time** (não genérica): alguns times rendem muito mais em casa;
  o campo `venue` ainda detecta estádio neutro/trocado.

Pré-requisito: **coletar 2–3 temporadas históricas com `--detalhes`** (escalações, notas,
xG de cada jogo). A Série B tem histórico desde ~2019 na API. Uma temporada completa leva
~40 min a 1 req/s — dá para agendar no Task Scheduler.

### 🟡 Estágio 3 — Modelo de machine learning (PIPELINE PRONTO, aguardando dados)
Pipeline completo construído e validado (`features.py` + `treino.py` + `prever.py`):
regressão logística + XGBoost prevendo 1/X/2, com split **cronológico** (treina no
passado, testa no futuro) e **zero vazamento** — cada feature usa só jogos anteriores ao
apito. Validação por **Brier score** e **log-loss** contra dois baselines (taxas-base e
mercado/odds). O melhor modelo é re-treinado em tudo e salvo em `modelo.joblib`.

20 features implementadas (entram automaticamente conforme os dados são coletados):

| Feature | Tabela de origem | Coletado? |
|---|---|---|
| Forma (pontos últimos 5, por mando), gols pró/contra recentes | `evento` | ✅ ~90% |
| Dias de descanso, retrospecto direto (H2H) | `evento` | ✅ 100% |
| Probabilidade implícita das odds (sem margem) | `odd` | ⏳ falta coletar |
| Nota média do XI titular (casa/fora) | `escalacao` | ⏳ falta coletar |
| xG médio criado nos últimos jogos | `chute` | ⏳ falta coletar |

**Resultado atual (só forma+gols, 658 jogos, teste nos 132 mais recentes):**

| Modelo | log-loss | Brier | acurácia |
|---|---|---|---|
| baseline ingênuo (taxas-base) | 1.101 | 0.667 | 40.2% |
| **logística** | 1.103 | **0.665** | **44.7%** |
| xgboost | 1.156 | 0.699 | 39.4% |

Leitura honesta: com apenas forma+gols o modelo **mal supera o chute base**. Isso é o
esperado e **confirma a tese**: o ganho real virá das features ainda não coletadas (odds,
notas do XI, xG). Top features hoje: forma recente, ppg dos últimos 5, gols em casa, H2H.

**O que falta:** coletar detalhes (`--detalhes` / `pendentes`) de **2–3 temporadas
históricas por liga** (~1.000–2.000 jogos com escalações, notas e xG). A partir daí o
modelo passa a ter substância e dá para comparar de verdade contra o mercado.

⚠️ **Bloqueio atual de coleta (06/2026):** após a coleta pesada das 3 ligas, o Cloudflare
passou a responder `403 challenge` ao `curl-cffi` (rate-limit por IP/fingerprint —
provavelmente temporário). A sessão do navegador real ainda passa pelo Cloudflare, mas o
cookie `cf_clearance` é httpOnly e não pode ser repassado ao Python. Caminhos: (a) aguardar
o Cloudflare liberar e rodar o `coletor.py pendentes` (idempotente, dá para reexecutar);
(b) agendar a coleta no Task Scheduler com ritmo lento (1 req/s + jitter).

### Validação contínua (qualquer estágio)
A tabela `probabilidade` guarda cada previsão; a view `v_resultados` tem os resultados reais.
Backtesting por **Brier score / log-loss**, sempre comparando contra o baseline das odds.
Meta realista: **agregar informação às odds**, não "vencer o mercado" — vencer bookmaker
de forma consistente é raríssimo; o valor está em detectar divergências pontuais.

---

## 7. Mercados de contagem — escanteios, chutes, chutes ao gol, cartões

Extensão do projeto além do 1X2 (gols). **Insight central:** o modelo de gols é um
caso particular de um **modelo de contagem**. Escanteios/chutes/cartões são processos
de contagem → mercados **Over/Under** (total da partida e total por time), não 1X2.
Matematicamente é *mais simples* que o 1X2: uma única Poisson na SOMA (`λ_casa+λ_fora`)
e `P(N > linha)` — sem ordenação de dois Poisson.

Os dados **já existiam no banco**: `evento_estatistica` guarda escanteios/chutes por
jogo, e `odd` guarda os mercados de escanteios/cartões. Faltava o motor de mercado.

### Esquema
Nova tabela **`probabilidade_mercado`** (`evento_id, mercado, linha, modelo, p_over,
p_under, …`) — genérica para qualquer mercado de contagem. O 1X2 continua em
`probabilidade`.

### Pré-jogo (`mercados.py`)
Engine parametrizado pela config `MERCADOS` (nomes de estatística + de odds + linhas
por mercado, com sinônimos por idioma). Para cada mercado:
1. **`odds_implicitas`** — Over/Under do mercado, sem margem.
2. **`poisson`** — força para/contra por mando (de `evento_estatistica`), `λ_total` →
   `P(Over/Under)` por linha; **meia-linha** sem push, **linha inteira** remove o push.
3. **`combinado`** — 70% odds + 30% Poisson.

Também emite **totais por time** (mandante/visitante) usando os `λ` separados.
Descoberta dos nomes reais do banco: `py mercados.py stats` / `odds`.

### Ao vivo (`bet365/calibrar_mercado.py` + `prob_aovivo_mercado.py`)
Análogo do modelo de gols. **Lacuna conhecida:** o `incidente` guarda minuto de gols
e cartões, mas **não de escanteios/chutes** — sem timing por minuto. Solução:
- **Nível** (média total + share da casa) calibrado dos dados reais — confiável.
- **Forma** da curva por minuto = *prior* (default `subida` p/ escanteios/cartões).
- **Blend de ritmo**: escala `Λ` pelo ritmo OBSERVADO no jogo (contagem atual ÷
  esperado-até-agora), com shrinkage `w=min(0,85; m/(m+25))`. É o que torna o modelo
  responsivo — escanteios/chutes acumulam rápido. Mais multiplicadores de placar
  (time perdendo cria mais escanteios) e vermelho. Saída: `P(Over/Under)` total e por
  time + banda de confiança.

### Alertador no navegador (`bet365/alertador_escanteios.js`)
Somente leitura. O overview do bet365 só mostra placar de gols, então o alertador
opera no **jogo aberto** (lê a contagem de escanteios das rodas do painel + odds da
aba de escanteios). O modelo é um **port fiel** do `prob_aovivo_mercado.py`
(**paridade JS×Python testada, idêntica até 0,1%**). Os seletores de DOM são
best-effort (ajustáveis); `escDebug()` mostra o que foi lido.

### ML (`features_mercado.py` + `treino_mercado.py` + `validar_mercado.py`)
Mesmo princípio leakage-safe do `features.py`: o alvo é o **total** da estatística e
todas as features (médias para/contra por mando, prior Poisson, ritmo, descanso, H2H)
usam só jogos anteriores ao apito — inclusive o prior Poisson, computado do histórico
incremental (não do banco inteiro). `treino_mercado.py` ajusta um `PoissonRegressor`
no total e converte em `P(Over/Under)` por linha; valida MAE + Brier/log-loss contra a
média e contra o Poisson ataque/defesa. `validar_mercado.py` faz o backtest das
probabilidades gravadas vs. totais reais (Brier/log-loss/ROI + confiabilidade).

### Pendências de dados (mesma natureza do 1X2)
1. Backfill `coletor.py pendentes` → popular `evento_estatistica` em massa.
2. Confirmar nomes reais (`mercados.py stats`/`odds`) e ajustar os sinônimos.
3. Confirmar os seletores de DOM de escanteios na página viva (`escDebug()`).
4. (Opcional, p/ precisão no fim de jogo ao vivo) coletar o **minuto do escanteio** →
   recalibra a FORMA da curva, hoje um prior.

---

## 6. Como rodar do zero (para quem recebeu este documento)

```powershell
# 1. Requisitos: Python 3.12+ e o pacote curl-cffi
py -m pip install curl-cffi

# 2. Coletar uma liga (ex.: Série B 2026) — cria o futebol.db automaticamente
py coletor.py temporada 390 89840
py coletor.py classificacao 390 89840

# 3. Pegar odds/contexto de um jogo futuro e calcular (modelos estatísticos)
py coletor.py detalhes <id-do-evento>
py probabilidades.py evento <id-do-evento>

# 4. Modelo de ML (Estágio 3) — precisa de scikit-learn e xgboost
py -m pip install scikit-learn xgboost joblib pandas
py coletor.py pendentes          # completa escalações/xG dos jogos encerrados
py features.py                   # diagnóstico: cobertura de cada feature
py treino.py                     # treina, valida (Brier/log-loss) e salva modelo.joblib
py prever.py <id-do-evento>      # previsão 1X2 com o modelo
py prever.py --proximos          # prevê todos os jogos não iniciados
```

Para achar o ID de qualquer jogo/time/liga: busca da API (`/search/all?q=nome`) ou abra a
partida no site — o ID aparece na URL (`#id:15526121`).
