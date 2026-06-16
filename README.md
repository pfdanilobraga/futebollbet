# Futebol — coleta SofaScore + probabilidades ao vivo (bet365)

Projeto pessoal de análise esportiva. Duas partes:

1. **Coletor SofaScore** — alimenta um banco SQLite de partidas (resultados,
   estatísticas, escalações, xG, odds, incidentes) e calcula probabilidades 1X2
   (odds implícitas + Poisson de ataque/defesa + modelo ML).
2. **Alertador ao vivo bet365** (`bet365/`) — **somente leitura**: monitora jogos
   no fim, calcula a probabilidade do resultado atual se manter (modelo de hazard
   calibrado nos dados coletados) e **sinaliza** janelas de valor. Não aposta —
   o humano decide e clica.
3. **Mercados de contagem** (escanteios, chutes, chutes ao gol, cartões) — Over/Under
   pré-jogo e ao vivo, reaproveitando o mesmo banco. Total de contagem = Poisson na
   soma (`mercados.py`), com modelo ao vivo de hazard + blend de ritmo
   (`bet365/prob_aovivo_mercado.py`) e ML opcional (`treino_mercado.py`).

## ⚠️ Aviso importante

- **Uso pessoal/educacional e de pesquisa.** Não é aconselhamento de aposta.
- **Somente leitura.** O alertador **não automatiza apostas** — apenas sinaliza.
  Automatizar cliques de aposta viola os Termos de Uso do bet365.
- **Respeite os ToS.** Os Termos do bet365 e do SofaScore proíbem scraping/uso
  comercial/redistribuição. Os **dados raspados não estão neste repositório**
  (veja `.gitignore`) — só o código.
- **Aposta é −EV.** A casa precifica e suspende mercados mais rápido que você
  clica. A ferramenta melhora *timing e disciplina*, não transforma em lucro.
  Defina limite de perda e aposte só o que pode perder. Jogo responsável.
- **Sem afiliação** com bet365 ou SofaScore. Sem garantia de qualquer tipo.

## Requisitos

```bash
pip install curl-cffi playwright scikit-learn xgboost joblib pandas numpy
playwright install chromium
```
(Windows: use `py -m pip install ...`. Nem tudo é necessário pra todo módulo.)

## Uso rápido

**Coleta + probabilidades (SofaScore):**
```bash
py coletor.py dia 2026-06-15        # coleta jogos do dia
py probabilidades.py proximos       # 1X2 dos jogos não iniciados
py raspador.py pendentes --limite 50
py manutencao.py                    # rotina diária (coleta + re-treino)
```

**Alertador ao vivo (bet365):**
```bash
py bet365/calibrar_hazard.py                 # gera bet365/hazard_cal.json
py bet365/prob_aovivo.py --min 89 --casa 1 --fora 1   # testa um cenário
py bet365/gerar_bookmarklet.py               # gera bet365/bookmarklet.html
```
No navegador, página Ao-Vivo→Futebol do bet365: cole `bet365/alertador_valor.js`
no console (ou instale o bookmarklet) e rode `iniciarValor()`.

Cross-check opcional (segunda fonte ao vivo):
```bash
py bet365/sofascore_live.py servir --forca   # serve http://localhost:8765
# console: iniciarValor({ssUrl:'http://localhost:8765'})
```

Validação dos sinais (P&L real):
```bash
py bet365/validar_sinais.py
```

**Mercados de contagem (escanteios, chutes, chutes ao gol, cartões):**
```bash
py mercados.py stats                 # descobre os nomes de estatística no banco
py mercados.py odds                  # descobre os marketName de odds no banco
py mercados.py proximos              # Over/Under (todos os mercados) dos jogos não iniciados
py mercados.py evento <id> --mercado escanteios
py validar_mercado.py --mercado escanteios --modelo combinado   # backtest + P&L

py bet365/calibrar_mercado.py        # gera bet365/mercado_cal.json (nível real + forma prior)
py bet365/prob_aovivo_mercado.py --mercado escanteios --min 75 --casa 5 --fora 3 --linha 9.5

py features_mercado.py --mercado escanteios   # diagnóstico de cobertura das features
py treino_mercado.py --mercado escanteios     # ML do total -> P(Over/Under) (precisa scikit-learn)
```
No navegador (partida aberta, painel de stats + aba de escanteios): cole
`bet365/alertador_escanteios.js` e rode `escDebug()` (confere a leitura do DOM)
e `iniciarEscanteios()`.

## Estrutura

| Arquivo | O quê |
|---|---|
| `coletor.py` `raspador.py` `transporte.py` `gravar.py` | coleta SofaScore (REST + Playwright) |
| `schema.sql` | esquema do `futebol.db` (inclui `probabilidade_mercado`) |
| `probabilidades.py` `features.py` `treino.py` `prever.py` | modelos 1X2 / ML |
| `mercados.py` | Over/Under de contagem (escanteios/chutes/chutes-gol/cartões): Poisson + odds + combinado, total e por time |
| `features_mercado.py` `treino_mercado.py` | ML de contagem: features leakage-safe + regressão do total → P(Over/Under) |
| `validar_mercado.py` | backtest dos mercados de contagem (Brier/log-loss/ROI) |
| `manutencao.py` | rotina agendada |
| `bet365/prob_aovivo.py` | modelo de probabilidade ao vivo 1X2 (hazard λ(t) + força-time) |
| `bet365/calibrar_mercado.py` `bet365/prob_aovivo_mercado.py` | mercados de contagem ao vivo (hazard + blend de ritmo) → `mercado_cal.json` |
| `bet365/alertador_valor.js` | alertador 1X2 no navegador (tiers WATCH→ARM→GREEN) |
| `bet365/alertador_escanteios.js` | alertador de escanteios Over/Under no navegador (port do modelo, paridade testada) |
| `bet365/sofascore_live.py` | cross-check SofaScore (servidor local) |
| `bet365/calibrar_hazard.py` | calibração 1X2 → `hazard_cal.json` |
| `bet365/validar_sinais.py` | backtest de P&L (sinais ao vivo 1X2) |
| `bet365/MAPEAMENTO-BET365.md` `MAPEAMENTO-SOFASCORE.md` | documentação |

## Licença

Sem licença formal — todos os direitos reservados ao autor. Uso por terceiros
sujeito aos avisos acima.
