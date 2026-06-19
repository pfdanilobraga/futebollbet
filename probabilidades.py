# -*- coding: utf-8 -*-
"""
Cálculo de probabilidades de vitória a partir do futebol.db

Modelos:
  1. odds_implicitas — converte as odds 1X2 em probabilidade (remove margem da casa)
  2. poisson         — força de ataque/defesa por gols marcados/sofridos (casa/fora)
  3. combinado       — pool LOGARÍTMICO (geométrico) das probabilidades:
                       p ∝ p_odds^w · p_poisson^(1-w), renormalizado (w=PESO_ODDS).
                       Superior à média linear (não fica sub-confiante). Onde não há
                       odds, só o 'poisson' é gravado (equivale a w=1, só modelo).

Uso:
  py probabilidades.py evento 15526121          # uma partida
  py probabilidades.py proximos                 # todas as partidas não iniciadas no banco
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
PESO_ODDS = 0.70          # peso do mercado no modelo combinado
MAX_GOLS = 10             # truncamento da matriz de Poisson
MIN_JOGOS = 3             # mínimo de jogos no mando para o Poisson ser confiável


# ------------------------------------------------------------ modelos
def prob_implicita_odds(con, evento_id):
    """1X2 do mercado 'Full time' -> probabilidades normalizadas."""
    linhas = con.execute(
        """SELECT escolha, odd_decimal FROM odd
           WHERE evento_id=? AND mercado='Full time' AND parametro=''""",
        (evento_id,)).fetchall()
    odds = {e: o for e, o in linhas if o and o > 1}
    if not all(k in odds for k in ("1", "X", "2")):
        return None
    bruto = {k: 1.0 / odds[k] for k in ("1", "X", "2")}
    soma = sum(bruto.values())          # >1 por causa da margem da casa
    return (bruto["1"] / soma, bruto["X"] / soma, bruto["2"] / soma,
            {"odds": odds, "margem": round(soma - 1, 4)})


def medias_time(con, time_id, em_casa, temporada_id, n_jogos=19):
    """(gols marcados, gols sofridos) médios do time no mando certo.
    Retorna (None, None) se houver menos de MIN_JOGOS jogos — amostra
    insuficiente (ex.: time de playoff vindo de outra divisão)."""
    if em_casa:
        q = """SELECT AVG(gols_casa), AVG(gols_fora), COUNT(*) FROM (
                 SELECT gols_casa, gols_fora FROM evento
                 WHERE casa_id=? AND status='finished' AND temporada_id=?
                   AND gols_casa IS NOT NULL
                 ORDER BY inicio_ts DESC LIMIT ?)"""
    else:
        q = """SELECT AVG(gols_fora), AVG(gols_casa), COUNT(*) FROM (
                 SELECT gols_casa, gols_fora FROM evento
                 WHERE fora_id=? AND status='finished' AND temporada_id=?
                   AND gols_casa IS NOT NULL
                 ORDER BY inicio_ts DESC LIMIT ?)"""
    m, s, n = con.execute(q, (time_id, temporada_id, n_jogos)).fetchone()
    if n < MIN_JOGOS:
        return None, None
    return m, s


def prob_poisson(con, evento_id):
    """Modelo de Poisson independente com força de ataque/defesa casa-fora."""
    ev = con.execute(
        "SELECT casa_id, fora_id, temporada_id FROM evento WHERE id=?",
        (evento_id,)).fetchone()
    if not ev:
        return None
    casa_id, fora_id, temp_id = ev

    # médias da liga (base) no mando
    lg = con.execute(
        """SELECT AVG(gols_casa), AVG(gols_fora) FROM evento
           WHERE status='finished' AND temporada_id=?""", (temp_id,)).fetchone()
    media_liga_casa, media_liga_fora = lg
    if not media_liga_casa or not media_liga_fora:
        return None

    atq_casa, def_casa = medias_time(con, casa_id, True, temp_id)
    atq_fora, def_fora = medias_time(con, fora_id, False, temp_id)
    if None in (atq_casa, def_casa, atq_fora, def_fora):
        return None

    # lambda = força de ataque x fraqueza da defesa adversária, escalada pela liga
    lam_casa = (atq_casa / media_liga_casa) * (def_fora / media_liga_casa) * media_liga_casa
    lam_fora = (atq_fora / media_liga_fora) * (def_casa / media_liga_fora) * media_liga_fora

    def pois(lam, k):
        return math.exp(-lam) * lam ** k / math.factorial(k)

    p1 = px = p2 = 0.0
    for gc in range(MAX_GOLS + 1):
        for gf in range(MAX_GOLS + 1):
            p = pois(lam_casa, gc) * pois(lam_fora, gf)
            if gc > gf:
                p1 += p
            elif gc == gf:
                px += p
            else:
                p2 += p
    soma = p1 + px + p2
    return (p1 / soma, px / soma, p2 / soma,
            {"lambda_casa": round(lam_casa, 3), "lambda_fora": round(lam_fora, 3)})


# ------------------------------------------------------------ blend
def blend_loglinear(p_odds, p_pois, w):
    """Pool logarítmico (geométrico): p_k ∝ p_odds_k^w · p_pois_k^(1-w).
    Externamente bayesiano e menos sub-confiante que a média linear."""
    out = [(max(a, 1e-12) ** w) * (max(b, 1e-12) ** (1 - w))
           for a, b in zip(p_odds, p_pois)]
    s = sum(out)
    return tuple(o / s for o in out)


# ------------------------------------------------------------ orquestração
def gravar(con, evento_id, modelo, res):
    p1, px, p2, det = res
    con.execute(
        """INSERT INTO probabilidade
               (evento_id, modelo, p_casa, p_empate, p_fora, detalhes_json, calculado_em)
           VALUES (?,?,?,?,?,?,datetime('now'))
           ON CONFLICT(evento_id, modelo) DO UPDATE SET
               p_casa=excluded.p_casa, p_empate=excluded.p_empate,
               p_fora=excluded.p_fora, detalhes_json=excluded.detalhes_json,
               calculado_em=datetime('now')""",
        (evento_id, modelo, round(p1, 4), round(px, 4), round(p2, 4),
         json.dumps(det, ensure_ascii=False)))


def calcular_evento(con, evento_id, verboso=True):
    nomes = con.execute(
        """SELECT tc.nome, tf.nome FROM evento e
           JOIN time tc ON tc.id=e.casa_id JOIN time tf ON tf.id=e.fora_id
           WHERE e.id=?""", (evento_id,)).fetchone()
    if not nomes:
        print(f"Evento {evento_id} não está no banco — rode o coletor antes.")
        return

    r_odds = prob_implicita_odds(con, evento_id)
    r_pois = prob_poisson(con, evento_id)
    # limpa resultados antigos de modelos que não puderam ser calculados agora
    for modelo, r in (("odds_implicitas", r_odds), ("poisson", r_pois),
                      ("combinado", r_odds and r_pois)):
        if not r:
            con.execute("DELETE FROM probabilidade WHERE evento_id=? AND modelo=?",
                        (evento_id, modelo))
    if r_odds:
        gravar(con, evento_id, "odds_implicitas", r_odds)
    if r_pois:
        gravar(con, evento_id, "poisson", r_pois)
    if r_odds and r_pois:
        comb = blend_loglinear(r_odds[:3], r_pois[:3], PESO_ODDS)
        gravar(con, evento_id, "combinado",
               (*comb, {"peso_odds": PESO_ODDS, "blend": "log-linear"}))
    con.commit()

    if verboso:
        print(f"\n⚽ {nomes[0]} x {nomes[1]}  (evento {evento_id})")
        print(f"{'modelo':<18}{'casa':>8}{'empate':>9}{'fora':>8}")
        for nome, r in (("odds_implicitas", r_odds), ("poisson", r_pois)):
            if r:
                print(f"{nome:<18}{r[0]:>7.1%}{r[1]:>9.1%}{r[2]:>8.1%}")
        if r_odds and r_pois:
            print(f"{'combinado':<18}{comb[0]:>7.1%}{comb[1]:>9.1%}{comb[2]:>8.1%}")
        if not r_odds and not r_pois:
            print("  sem dados suficientes (colete odds e/ou resultados da temporada)")


def calcular_proximos(con):
    ids = [r[0] for r in con.execute(
        "SELECT id FROM evento WHERE status='notstarted' ORDER BY inicio_ts")]
    print(f"{len(ids)} partidas não iniciadas no banco.")
    for eid in ids:
        calcular_evento(con, eid)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("evento"); p.add_argument("evento_id", type=int)
    sub.add_parser("proximos")
    a = ap.parse_args()

    con = sqlite3.connect(DB)
    try:
        if a.cmd == "evento":
            calcular_evento(con, a.evento_id)
        else:
            calcular_proximos(con)
    finally:
        con.close()


if __name__ == "__main__":
    main()
