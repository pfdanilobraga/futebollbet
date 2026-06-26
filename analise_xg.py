# -*- coding: utf-8 -*-
"""
Análise honesta do impacto do xG: compara RPS do MODELO vs RPS do MERCADO
(odds devig) segmentado por jogos COM xG (Big-5 enriquecidas pelo Understat) vs
SEM xG. Se o modelo não bate o mercado nem onde há xG denso + odds, o teto de
mercado é definitivo (xG já está embutido no preço).

Lê oof_preds.csv (previsões out-of-fold do treino.py) + odds do futebol.db.
Uso: py analise_xg.py
"""
import csv
import os
import sqlite3
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

PASTA = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(PASTA, "futebol.db")
OOF = os.path.join(PASTA, "oof_preds.csv")
ORDEM = ("H", "D", "A")


def rps(p_hda, alvo):
    """RPS de 3 saídas ordenadas (H,D,A). p_hda = (pH,pD,pA); alvo em {H,D,A}."""
    c1 = p_hda[0]
    c2 = p_hda[0] + p_hda[1]
    o1 = 1.0 if alvo == "H" else 0.0
    o2 = 1.0 if alvo in ("H", "D") else 0.0
    return 0.5 * ((c1 - o1) ** 2 + (c2 - o2) ** 2)


def mercado_implicito(con, eid):
    linhas = con.execute(
        "SELECT escolha, odd_decimal FROM odd WHERE evento_id=? AND mercado='Full time' AND parametro=''",
        (eid,)).fetchall()
    o = {e: v for e, v in linhas if v and v > 1}
    if not all(k in o for k in ("1", "X", "2")):
        return None
    inv = {k: 1.0 / o[k] for k in ("1", "X", "2")}
    s = sum(inv.values())
    return (inv["1"] / s, inv["X"] / s, inv["2"] / s)


def main():
    if not os.path.exists(OOF):
        sys.exit("oof_preds.csv não existe — rode treino.py antes.")
    con = sqlite3.connect(DB)
    com_xg = {e for (e,) in con.execute(
        "SELECT DISTINCT evento_id FROM chute WHERE xg IS NOT NULL")}

    segs = {}   # nome -> [soma_rps_modelo, soma_rps_mercado, n] (pareado: só jogos c/ odds)
    def add(seg, rps_m, rps_mk):
        s = segs.setdefault(seg, [0.0, 0.0, 0])
        s[0] += rps_m
        s[1] += rps_mk
        s[2] += 1

    with open(OOF, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            eid = int(row["evento_id"])
            alvo = row["alvo"]
            if alvo not in ORDEM:
                continue
            mk = mercado_implicito(con, eid)
            if mk is None:                       # comparação PAREADA: só jogos com odds
                continue
            pm = (float(row["p_casa"]), float(row["p_empate"]), float(row["p_fora"]))
            rps_m = rps(pm, alvo)
            rps_mk = rps(mk, alvo)
            seg = "COM xG" if eid in com_xg else "SEM xG"
            add(seg, rps_m, rps_mk)
            add("TODOS", rps_m, rps_mk)
    con.close()

    print(f"{'segmento':<10} {'n':>8} {'RPS modelo':>11} {'RPS mercado':>12} {'Δ (mod-merc)':>13}")
    print("-" * 58)
    for seg in ("TODOS", "COM xG", "SEM xG"):
        if seg not in segs:
            continue
        sm, smk, n = segs[seg]
        rps_mod = sm / n if n else float("nan")
        rps_mer = smk / n if n else float("nan")
        print(f"{seg:<10} {n:>8} {rps_mod:>11.4f} {rps_mer:>12.4f} {rps_mod - rps_mer:>+13.4f}")
    print("\nLeitura: Δ < 0 => modelo BATE o mercado nesse segmento; Δ ≈ 0 => empata (teto);")
    print("Δ > 0 => modelo perde p/ o mercado. xG só vira edge se 'COM xG' tiver Δ << 0.")


if __name__ == "__main__":
    main()
