# -*- coding: utf-8 -*-
"""
Mostra, de forma legível, as PREVISÕES do modelo para os próximos jogos —
o jeito simples de "ver/testar" o resultado da reformulação de dados.

Uso:  py ver_previsoes.py            # próximos 25 jogos com previsão
      py ver_previsoes.py 40         # próximos 40
"""
import datetime as dt
import os
import sqlite3
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "futebol.db")


def odds_mercado(con, eid):
    o = {e: v for e, v in con.execute(
        "SELECT escolha, odd_decimal FROM odd WHERE evento_id=? AND mercado='Full time' AND parametro=''",
        (eid,))}
    if all(k in o for k in ("1", "X", "2")):
        return o["1"], o["X"], o["2"]
    return None


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 25
    agora = int(dt.datetime.now().timestamp()) - 3 * 3600   # inclui jogos começando agora
    con = sqlite3.connect(DB)
    linhas = con.execute(
        """SELECT e.id, e.inicio_ts, COALESCE(t.nome,'?') liga, tc.nome, tf.nome,
                  p.p_casa, p.p_empate, p.p_fora
           FROM evento e
           JOIN time tc ON tc.id=e.casa_id JOIN time tf ON tf.id=e.fora_id
           LEFT JOIN torneio t ON t.id=e.torneio_id
           JOIN probabilidade p ON p.evento_id=e.id AND p.modelo='ml'
           WHERE e.status='notstarted' AND e.inicio_ts >= ?
           ORDER BY e.inicio_ts LIMIT ?""", (agora, n)).fetchall()

    if not linhas:
        print("Nenhum jogo futuro com previsão ainda. Rode antes:  py prever.py --proximos")
        return

    print(f"\n{'quando':<16}{'liga':<22}{'jogo':<34}{'modelo (C/E/F)':<20}{'mercado (1/X/2)'}")
    print("-" * 112)
    for eid, ts, liga, casa, fora, pc, pe, pf in linhas:
        quando = dt.datetime.fromtimestamp(ts).strftime("%d/%m %H:%M")
        jogo = f"{casa[:15]} x {fora[:15]}"
        palpite = max((("casa", pc), ("empate", pe), ("fora", pf)), key=lambda x: x[1])
        modelo = f"{pc:.0%}/{pe:.0%}/{pf:.0%} ->{palpite[0]}"
        mk = odds_mercado(con, eid)
        merc = f"{mk[0]:.2f}/{mk[1]:.2f}/{mk[2]:.2f}" if mk else "(sem odds)"
        print(f"{quando:<16}{liga[:21]:<22}{jogo:<34}{modelo:<20}{merc}")
    print(f"\n{len(linhas)} jogos. 'modelo' = prob. casa/empate/fora; 'mercado' = odds 1X2 atuais.")
    con.close()


if __name__ == "__main__":
    main()
