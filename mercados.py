# -*- coding: utf-8 -*-
"""
Mercados de CONTAGEM (Over/Under) a partir do futebol.db — escanteios, chutes,
chutes ao gol, cartões. Generaliza o probabilidades.py (que faz 1X2) para
totais Over/Under.

Ideia central: gols 1X2 = ordenação de duas Poisson. Um total Over/Under é
mais simples — uma única Poisson na SOMA (λ_casa + λ_fora) e P(N > linha).

Modelos (mesma família do probabilidades.py):
  1. odds_implicitas — converte odds Over/Under do mercado (remove margem)
  2. poisson         — força "para/contra" por mando (do evento_estatistica)
  3. combinado       — média ponderada (PESO_ODDS, ajustável)

A FONTE das estatísticas é a tabela `evento_estatistica` (periodo='ALL'); a
fonte das odds é a tabela `odd`. Como os nomes exatos variam por liga/idioma,
cada mercado aceita uma lista de sinônimos — descubra os nomes reais do seu
banco com os comandos `stats` e `odds` (Fase 0).

Uso:
  py mercados.py stats                       # FASE 0: lista nomes de estatística no banco
  py mercados.py odds                        # FASE 0: lista nomes de mercado de odds no banco
  py mercados.py evento 15526121             # calcula todos os mercados de uma partida
  py mercados.py evento 15526121 --mercado escanteios
  py mercados.py proximos                    # todas as partidas não iniciadas
  py mercados.py proximos --mercado escanteios
"""
import argparse
import json
import math
import os
import sqlite3
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

PASTA = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(PASTA, "futebol.db")

PESO_ODDS = 0.70       # peso do mercado no modelo combinado
MAX_N = 30             # truncamento da Poisson (escanteios/chutes vão alto)
MIN_JOGOS = 3          # mínimo de jogos no mando p/ o Poisson ser confiável
JANELA = 19            # nº de jogos recentes no mando usados na média

# ---------------------------------------------------------------- configuração
# Para cada mercado lógico:
#   stat        : nomes possíveis em evento_estatistica.nome (sinônimos, case-insensitive)
#   odds        : trechos de odd.mercado (marketName) que indicam o mercado (LIKE)
#   linhas      : linhas Over/Under do TOTAL da partida (casa+fora) default
#   linhas_time : linhas Over/Under do total de UM time (mandante/visitante).
#                 Usa os λ separados do Poisson — só Poisson (odds por-time são raras).
# Confirme/ajuste com `py mercados.py stats` e `py mercados.py odds`.
# NOTA cartões: "Yellow cards" conta só amarelos; o mercado bet365 costuma somar
# amarelos+vermelhos (ou pontos de cartão). Ajuste os sinônimos conforme o stats.
MERCADOS = {
    "escanteios": {
        "stat":   ["Corner kicks", "Corners", "Escanteios"],
        "odds":   ["corner", "escanteio"],
        "linhas": [8.5, 9.5, 10.5, 11.5],
        "linhas_time": [3.5, 4.5, 5.5],
    },
    "chutes": {
        "stat":   ["Total shots", "Shots total", "Finalizações", "Total de finalizações"],
        "odds":   ["total shots", "shots", "finaliza"],
        "linhas": [20.5, 22.5, 24.5, 26.5],
        "linhas_time": [9.5, 11.5, 13.5],
    },
    "chutes_gol": {
        "stat":   ["Shots on target", "Shots on goal", "Finalizações no gol",
                   "Chutes no gol", "Finalizações no alvo"],
        "odds":   ["shots on target", "on target", "no gol", "no alvo"],
        "linhas": [6.5, 7.5, 8.5, 9.5],
        "linhas_time": [2.5, 3.5, 4.5],
    },
    "cartoes": {
        "stat":   ["Yellow cards", "Cards", "Cartões", "Cartões amarelos"],
        "odds":   ["card", "cartõe", "cartoes"],
        "linhas": [2.5, 3.5, 4.5, 5.5],
        "linhas_time": [1.5, 2.5],
    },
}


# ---------------------------------------------------------------- descoberta (FASE 0)
def listar_stats(con):
    """Lista os nomes de estatística presentes (periodo='ALL') e a cobertura."""
    linhas = con.execute(
        """SELECT grupo, nome,
                  COUNT(DISTINCT evento_id) AS n,
                  ROUND(AVG(casa_valor + fora_valor), 2) AS media_total
           FROM evento_estatistica
           WHERE periodo='ALL' AND casa_valor IS NOT NULL
           GROUP BY grupo, nome
           ORDER BY grupo, n DESC""").fetchall()
    if not linhas:
        print("Nenhuma estatística em evento_estatistica. Rode o backfill primeiro:\n"
              "  py coletor.py pendentes   (ou: py raspador.py pendentes --limite 150)")
        return
    print(f"{'grupo':<18}{'nome':<28}{'jogos':>7}{'média total':>13}")
    print("-" * 66)
    for grupo, nome, n, media in linhas:
        print(f"{(grupo or ''):<18}{(nome or ''):<28}{n:>7}{(media if media is not None else 0):>13}")
    print("\nAjuste MERCADOS['<mercado>']['stat'] em mercados.py com os nomes acima.")


def listar_odds(con):
    """Lista os marketName de odds presentes e quantos eventos têm cada um."""
    linhas = con.execute(
        """SELECT mercado, COUNT(DISTINCT evento_id) AS n,
                  COUNT(DISTINCT parametro) AS linhas
           FROM odd GROUP BY mercado ORDER BY n DESC""").fetchall()
    if not linhas:
        print("Nenhuma odd no banco. Rode: py coletor.py detalhes <evento_id>")
        return
    print(f"{'mercado (marketName)':<42}{'eventos':>8}{'linhas':>8}")
    print("-" * 58)
    for mercado, n, nlinhas in linhas:
        print(f"{(mercado or ''):<42}{n:>8}{nlinhas:>8}")
    print("\nAjuste MERCADOS['<mercado>']['odds'] com trechos dos nomes acima.")


# ---------------------------------------------------------------- estatística -> médias
def _ph(itens):
    """placeholders SQL '?,?,?' para uma lista."""
    return ",".join("?" * len(itens))


def medias_stat_time(con, time_id, em_casa, temp_id, nomes, n=JANELA):
    """(para, contra, n_jogos) médios da estatística para o time no mando dado.
    'para' = a estatística DO time; 'contra' = a do adversário no mesmo jogo.
    Retorna (None, None, 0) se houver < MIN_JOGOS jogos com a estatística."""
    if em_casa:
        col_para, col_contra, filtro = "casa_valor", "fora_valor", "e.casa_id=?"
    else:
        col_para, col_contra, filtro = "fora_valor", "casa_valor", "e.fora_id=?"
    q = f"""SELECT AVG(para), AVG(contra), COUNT(*) FROM (
              SELECT ee.{col_para} AS para, ee.{col_contra} AS contra
              FROM evento_estatistica ee
              JOIN evento e ON e.id = ee.evento_id
              WHERE ee.periodo='ALL' AND ee.nome IN ({_ph(nomes)})
                AND e.status='finished' AND e.temporada_id=? AND {filtro}
                AND ee.casa_valor IS NOT NULL
              ORDER BY e.inicio_ts DESC LIMIT ?)"""
    para, contra, qtd = con.execute(
        q, (*nomes, temp_id, time_id, n)).fetchone()
    if qtd < MIN_JOGOS:
        return None, None, qtd
    return para, contra, qtd


def media_liga(con, temp_id, nomes):
    """(média mando-casa, média mando-fora) da estatística na temporada."""
    q = f"""SELECT AVG(ee.casa_valor), AVG(ee.fora_valor)
            FROM evento_estatistica ee JOIN evento e ON e.id = ee.evento_id
            WHERE ee.periodo='ALL' AND ee.nome IN ({_ph(nomes)})
              AND e.status='finished' AND e.temporada_id=?
              AND ee.casa_valor IS NOT NULL"""
    return con.execute(q, (*nomes, temp_id)).fetchone()


# ---------------------------------------------------------------- Poisson Over/Under
def pois_pmf(lam, k):
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * lam ** k / math.factorial(k)


def pois_cdf(lam, k):
    """P(N <= k)."""
    return sum(pois_pmf(lam, i) for i in range(int(k) + 1))


def over_under(lam_total, linha):
    """P(Over), P(Under) para uma linha. Suporta meia-linha (sem push) e
    linha inteira (descarta o push e normaliza)."""
    base = math.floor(linha)
    if abs(linha - base - 0.5) < 1e-9:          # meia-linha: Over = N >= base+1
        p_under = pois_cdf(lam_total, base)
        return 1.0 - p_under, p_under
    # linha inteira: remove a probabilidade do empate exato (push) e normaliza
    p_push = pois_pmf(lam_total, int(linha))
    p_under = pois_cdf(lam_total, int(linha) - 1)
    p_over = 1.0 - p_under - p_push
    s = p_over + p_under or 1.0
    return p_over / s, p_under / s


def prob_poisson(con, evento_id, cfg):
    """λ_total (casa+fora) da estatística via força para/contra por mando."""
    ev = con.execute(
        "SELECT casa_id, fora_id, temporada_id FROM evento WHERE id=?",
        (evento_id,)).fetchone()
    if not ev:
        return None
    casa_id, fora_id, temp = ev
    nomes = cfg["stat"]
    liga_casa, liga_fora = media_liga(con, temp, nomes)
    if not liga_casa or not liga_fora:
        return None
    para_casa, contra_casa, _ = medias_stat_time(con, casa_id, True, temp, nomes)
    para_fora, contra_fora, _ = medias_stat_time(con, fora_id, False, temp, nomes)
    if None in (para_casa, contra_casa, para_fora, contra_fora):
        return None
    # mesma convenção do probabilidades.py: ataque do time x fraqueza do adversário
    lam_casa = para_casa * contra_fora / liga_casa
    lam_fora = para_fora * contra_casa / liga_fora
    lam_total = lam_casa + lam_fora
    return lam_total, {"lambda_casa": round(lam_casa, 3),
                       "lambda_fora": round(lam_fora, 3),
                       "lambda_total": round(lam_total, 3)}


# ---------------------------------------------------------------- odds implícitas
def odds_over_under(con, evento_id, cfg):
    """Lê odds Over/Under do mercado e devolve {linha: (p_over, p_under)}
    já normalizadas (sem margem). Casa marketName por LIKE (sinônimos)."""
    cond = " OR ".join("LOWER(mercado) LIKE ?" for _ in cfg["odds"])
    args = [f"%{t.lower()}%" for t in cfg["odds"]]
    linhas = con.execute(
        f"""SELECT parametro, escolha, odd_decimal FROM odd
            WHERE evento_id=? AND ({cond})""", (evento_id, *args)).fetchall()
    por_linha = {}
    for parametro, escolha, odd in linhas:
        try:
            linha = float(str(parametro).replace(",", "."))
        except (TypeError, ValueError):
            continue
        if not odd or odd <= 1:
            continue
        e = (escolha or "").strip().lower()
        lado = "over" if e in ("over", "mais", "acima") else (
               "under" if e in ("under", "menos", "abaixo") else None)
        if lado:
            por_linha.setdefault(linha, {})[lado] = odd
    out = {}
    for linha, o in por_linha.items():
        if "over" in o and "under" in o:
            ro, ru = 1.0 / o["over"], 1.0 / o["under"]
            s = ro + ru
            out[linha] = (ro / s, ru / s)
    return out


# ---------------------------------------------------------------- gravação
def gravar(con, evento_id, mercado, linha, modelo, p_over, p_under, det):
    con.execute(
        """INSERT INTO probabilidade_mercado
               (evento_id, mercado, linha, modelo, p_over, p_under,
                detalhes_json, calculado_em)
           VALUES (?,?,?,?,?,?,?,datetime('now'))
           ON CONFLICT(evento_id, mercado, linha, modelo) DO UPDATE SET
               p_over=excluded.p_over, p_under=excluded.p_under,
               detalhes_json=excluded.detalhes_json,
               calculado_em=datetime('now')""",
        (evento_id, mercado, linha, modelo, round(p_over, 4), round(p_under, 4),
         json.dumps(det, ensure_ascii=False)))


# ---------------------------------------------------------------- orquestração
def calcular_mercado(con, evento_id, mercado, cfg, verboso=True):
    pois = prob_poisson(con, evento_id, cfg)
    odds = odds_over_under(con, evento_id, cfg)
    # linhas a reportar: as do mercado (se houver) senão as default da config
    linhas = sorted(odds.keys()) if odds else cfg["linhas"]

    if verboso:
        det_lam = f" (λ_total={pois[0]:.2f})" if pois else " (sem Poisson)"
        print(f"\n  ── {mercado}{det_lam}")
        print(f"  {'linha':>6}  {'modelo':<16}{'Over':>8}{'Under':>8}")

    for linha in linhas:
        r_pois = over_under(pois[0], linha) if pois else None
        r_odds = odds.get(linha)
        if r_pois:
            gravar(con, evento_id, mercado, linha, "poisson",
                   r_pois[0], r_pois[1], pois[1])
        if r_odds:
            gravar(con, evento_id, mercado, linha, "odds_implicitas",
                   r_odds[0], r_odds[1], {"fonte": "odd"})
        if r_pois and r_odds:
            po = PESO_ODDS * r_odds[0] + (1 - PESO_ODDS) * r_pois[0]
            gravar(con, evento_id, mercado, linha, "combinado",
                   po, 1 - po, {"peso_odds": PESO_ODDS})
        if verboso:
            for nome, r in (("odds_implicitas", r_odds), ("poisson", r_pois)):
                if r:
                    print(f"  {linha:>6}  {nome:<16}{r[0]:>7.1%}{r[1]:>8.1%}")
            if r_pois and r_odds:
                po = PESO_ODDS * r_odds[0] + (1 - PESO_ODDS) * r_pois[0]
                print(f"  {linha:>6}  {'combinado':<16}{po:>7.1%}{1-po:>8.1%}")

    # totais por time (só Poisson; usa os λ separados do confronto)
    if pois and cfg.get("linhas_time"):
        for lado, lam_key in (("casa", "lambda_casa"), ("fora", "lambda_fora")):
            lam = pois[1].get(lam_key)
            if not lam:
                continue
            if verboso:
                print(f"  ── {mercado}_{lado} (λ={lam:.2f})")
            for linha in cfg["linhas_time"]:
                po, pu = over_under(lam, linha)
                gravar(con, evento_id, f"{mercado}_{lado}", linha, "poisson",
                       po, pu, {lam_key: round(lam, 3)})
                if verboso:
                    print(f"  {linha:>6}  {'poisson':<16}{po:>7.1%}{pu:>8.1%}")


def calcular_evento(con, evento_id, mercados=None, verboso=True):
    nomes = con.execute(
        """SELECT tc.nome, tf.nome FROM evento e
           JOIN time tc ON tc.id=e.casa_id JOIN time tf ON tf.id=e.fora_id
           WHERE e.id=?""", (evento_id,)).fetchone()
    if not nomes:
        print(f"Evento {evento_id} não está no banco — rode o coletor antes.")
        return
    alvos = mercados or list(MERCADOS.keys())
    if verboso:
        print(f"\n⚽ {nomes[0]} x {nomes[1]}  (evento {evento_id})")
    for m in alvos:
        calcular_mercado(con, evento_id, m, MERCADOS[m], verboso)
    con.commit()


def calcular_proximos(con, mercados=None):
    ids = [r[0] for r in con.execute(
        "SELECT id FROM evento WHERE status='notstarted' ORDER BY inicio_ts")]
    print(f"{len(ids)} partidas não iniciadas no banco.")
    for eid in ids:
        calcular_evento(con, eid, mercados)


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats")
    sub.add_parser("odds")
    pe = sub.add_parser("evento"); pe.add_argument("evento_id", type=int)
    pe.add_argument("--mercado", choices=list(MERCADOS), default=None)
    pp = sub.add_parser("proximos")
    pp.add_argument("--mercado", choices=list(MERCADOS), default=None)
    a = ap.parse_args()

    con = sqlite3.connect(DB)
    try:
        # garante a tabela (idempotente — schema.sql também a cria)
        con.executescript("""
            CREATE TABLE IF NOT EXISTS probabilidade_mercado (
                evento_id INTEGER NOT NULL, mercado TEXT NOT NULL,
                linha REAL NOT NULL, modelo TEXT NOT NULL,
                p_over REAL NOT NULL, p_under REAL NOT NULL,
                detalhes_json TEXT, calculado_em TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (evento_id, mercado, linha, modelo));
            CREATE INDEX IF NOT EXISTS ix_prob_mercado
                ON probabilidade_mercado(evento_id, mercado);""")
        if a.cmd == "stats":
            listar_stats(con)
        elif a.cmd == "odds":
            listar_odds(con)
        elif a.cmd == "evento":
            calcular_evento(con, a.evento_id,
                            [a.mercado] if a.mercado else None)
        else:
            calcular_proximos(con, [a.mercado] if a.mercado else None)
    finally:
        con.close()


if __name__ == "__main__":
    main()
