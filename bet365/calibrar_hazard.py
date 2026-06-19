# -*- coding: utf-8 -*-
"""
calibrar_hazard.py — calibra o modelo de probabilidade AO VIVO a partir dos
dados reais do futebol.db e grava bet365/hazard_cal.json.

SOMENTE LEITURA do banco. Roda quando quiser re-calibrar (ex.: depois que o
backfill do SofaScore acumular mais jogos). prob_aovivo.py e alertador_valor.js
leem o JSON resultante.

O que calibra (ver MAPEAMENTO-BET365.md / plano):
  - taxa real de gols/minuto (curva de hazard por bucket de 5 min, 0..89)
  - hazard dos acrescimos (1T e 2T) por minuto jogado
  - share de gols da casa (corrige o 50/50 chutado)
  - PMF de minutos extras de acrescimo (PRIOR — o length anunciado nao esta no
    banco ainda; o gravar.py nao captura o campo `length` do injuryTime)
  - efeito de cartao vermelho (PRIOR, com estimativa grosseira do banco)

Uso:
  py bet365/calibrar_hazard.py
  py bet365/calibrar_hazard.py --db ../futebol.db --out hazard_cal.json
"""
import argparse
import json
import os
import sqlite3
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

PASTA = os.path.dirname(os.path.abspath(__file__))

# Exposicao media (min jogados) de acrescimo por tempo — usada pra converter
# "gols no acrescimo" em hazard/min. Sao estimativas (o banco nao tem o length
# anunciado); ajustaveis. 2T joga mais acrescimo que 1T.
EXPOSICAO_ACRESCIMO_1T = 2.3
EXPOSICAO_ACRESCIMO_2T = 4.5

# PRIOR: minutos extras JOGADOS alem do anunciado (o arbitro sempre estende).
# Calibravel quando o length anunciado for coletado (Stage 2). Soma = 1.
STOPPAGE_EXTRA_PMF = [0.45, 0.28, 0.15, 0.08, 0.04]

# PRIOR efeito de vermelho (multiplicador na taxa de gols por lado, por cartao):
#   time com um a menos ataca menos (down), sofre mais (up).
RED_DOWN = 0.74   # taxa do time reduzido
RED_UP   = 1.30   # taxa do adversario

# Coeficientes dos multiplicadores ao vivo (consumidos pelo modelo).
MULT = {
    "beta_xg":   0.5,    # amortecimento do ratio de xG (ruidoso)
    "gamma":     0.20,   # peso do indice de pressao (fallback de momentum)
    "sigma":     0.35,   # forca do efeito de placar
    "d0":        1.5,    # escala de diferenca de gols no tanh
    "dead":      0.8,    # multiplicador "jogo morto"
    "clampLo":   0.45,
    "clampHi":   2.2,
}

# Incerteza base (banda de confianca) — log-sigma de Lambda.
SIGMA_LOG = {"base": 0.30, "sem_stats": 0.10, "instavel": 0.10,
             "por_mult": 0.15, "liga_rasa": 0.10}

N_BUCKETS = 18   # 18 buckets de 5 min cobrem 0..89


def calibrar(db):
    con = sqlite3.connect(db)
    c = con.cursor()
    one = lambda q, *a: c.execute(q, a).fetchone()

    n_jogos = one("SELECT COUNT(*) FROM evento WHERE status='finished'")[0]
    gols_jogo = one("SELECT AVG(gols_casa+gols_fora) FROM evento "
                    "WHERE status='finished' AND gols_casa IS NOT NULL")[0] or 2.6

    tot_gols = one("SELECT COUNT(*) FROM incidente WHERE tipo='goal'")[0]
    gols_casa = one("SELECT COUNT(*) FROM incidente WHERE tipo='goal' AND eh_casa=1")[0]
    home_share = (gols_casa / tot_gols) if tot_gols else 0.549

    # hazard regular por bucket de 5 min (exclui gols de acrescimo de 45'/90')
    bucket_gols = [0] * N_BUCKETS
    for b, n in c.execute(
        "SELECT (minuto-1)/5 AS b, COUNT(*) FROM incidente "
        "WHERE tipo='goal' AND minuto IS NOT NULL AND minuto BETWEEN 1 AND 90 "
        "AND NOT(minuto IN (45,90) AND acrescimo>0) GROUP BY b", ).fetchall():
        if 0 <= b < N_BUCKETS:
            bucket_gols[b] = n
    expo_bucket = max(1, n_jogos) * 5.0  # cada jogo joga 5 min por bucket

    # hazard de acrescimo (gols por min jogado de acrescimo)
    g_stop_2t = one("SELECT COUNT(*) FROM incidente WHERE tipo='goal' AND minuto=90 AND acrescimo>0")[0]
    g_stop_1t = one("SELECT COUNT(*) FROM incidente WHERE tipo='goal' AND minuto=45 AND acrescimo>0")[0]
    h_stop_2h_raw = g_stop_2t / (max(1, n_jogos) * EXPOSICAO_ACRESCIMO_2T)
    h_stop_1h_raw = g_stop_1t / (max(1, n_jogos) * EXPOSICAO_ACRESCIMO_1T)

    # NORMALIZACAO: a tabela incidente esta incompleta (nem todo jogo teve
    # backfill), entao gols/jogo do incidente < gols/jogo do placar real.
    # A FORMA da curva e valida; corrigimos o NIVEL escalando tudo pra que o
    # total integrado de gols por jogo bata com gols_jogo (placar real).
    total_reg = sum(g / max(1, n_jogos) for g in bucket_gols)            # gols regulares/jogo (incidente)
    total_stop = (h_stop_2h_raw * EXPOSICAO_ACRESCIMO_2T
                  + h_stop_1h_raw * EXPOSICAO_ACRESCIMO_1T)               # gols acrescimo/jogo
    total_incidente = total_reg + total_stop
    escala = (gols_jogo / total_incidente) if total_incidente > 0 else 1.0

    h_reg = [round(g / expo_bucket * escala, 6) for g in bucket_gols]
    h_stop_2h = round(h_stop_2h_raw * escala, 6)
    h_stop_1h = round(h_stop_1h_raw * escala, 6)

    # estimativa grosseira do efeito de vermelho (apenas informativa; usa prior)
    n_red = one("SELECT COUNT(*) FROM incidente WHERE tipo='card' "
                "AND (detalhe LIKE '%red%' OR detalhe='yellowRed')")[0]

    # distribuição EMPÍRICA do timing dos gols de acréscimo (minuto absoluto).
    # NÃO é a PMF do modelo (que é "extra ALÉM do anunciado", S=A+x) — para
    # calibrar aquela precisamos do injuryTime anunciado, que o banco não tem.
    # Medir aqui torna visível a cauda real (gols em 90+5..+15) que o prior
    # trunca em +4. Serve de referência p/ a calibração futura.
    def _timing(minuto_base):
        rows = c.execute(
            "SELECT acrescimo, COUNT(*) FROM incidente WHERE tipo='goal' "
            "AND minuto=? AND acrescimo>0 GROUP BY acrescimo ORDER BY acrescimo",
            (minuto_base,)).fetchall()
        tot = sum(n for _, n in rows)
        dist = {int(a): round(n / tot, 4) for a, n in rows} if tot else {}
        alem4 = round(sum(n for a, n in rows if a > 4) / tot, 4) if tot else 0.0
        return {"n": tot, "dist": dist, "frac_alem_de_4": alem4}
    timing_2t = _timing(90)
    timing_1t = _timing(45)

    media_min = round(gols_jogo / 90.0, 6)  # gols/min medio (combinado, ja normalizado)

    cal = {
        "_meta": {
            "fonte": os.path.basename(db),
            "n_jogos": n_jogos,
            "gols_por_jogo": round(gols_jogo, 4),
            "gols_incidente_total": tot_gols,
            "n_vermelhos": n_red,
            "gols_jogo_incidente": round(total_incidente, 4),
            "escala_normalizacao": round(escala, 4),
            "obs": "Curva (forma) vem do incidente; nivel escalado x%.2f pra bater "
                   "com gols/jogo do placar real (incidente esta incompleto). "
                   "PMF de acrescimo e efeito de vermelho sao PRIOR; recalibrar "
                   "quando o length anunciado (injuryTime) for coletado." % escala,
            # MEDIDO (informativo): timing real dos gols de acrescimo. A PMF do
            # modelo e "extra ALEM do anunciado"; aqui e o minuto ABSOLUTO. A
            # diferenca vs stoppage_extra_pmf so fecha com o injuryTime anunciado.
            "stoppage_timing_2t": timing_2t,
            "stoppage_timing_1t": timing_1t,
        },
        "gols_por_jogo": round(gols_jogo, 4),
        "home_share": round(home_share, 4),
        "taxa_media_min": round(media_min, 6),     # gols/min combinado (referencia)
        "h_reg_buckets": h_reg,                    # 18 valores: gols/min por bucket 0..17 (min 0..89)
        "h_stop_1h": h_stop_1h,                    # gols/min jogado no acrescimo do 1T
        "h_stop_2h": h_stop_2h,                    # gols/min jogado no acrescimo do 2T
        "exposicao_acrescimo": {"t1": EXPOSICAO_ACRESCIMO_1T, "t2": EXPOSICAO_ACRESCIMO_2T},
        "stoppage_extra_pmf": STOPPAGE_EXTRA_PMF,
        "red": {"down": RED_DOWN, "up": RED_UP},
        "mult": MULT,
        "sigma_log": SIGMA_LOG,
    }
    con.close()
    return cal


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(PASTA, "..", "futebol.db"))
    ap.add_argument("--out", default=os.path.join(PASTA, "hazard_cal.json"))
    a = ap.parse_args()

    cal = calibrar(a.db)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(cal, f, ensure_ascii=False, indent=1)

    m = cal["_meta"]
    print(f"Calibrado de {m['fonte']}: {m['n_jogos']} jogos, "
          f"{m['gols_por_jogo']} gols/jogo, share casa {cal['home_share']}")
    print(f"  taxa media: {cal['taxa_media_min']}/min  "
          f"(curva: bucket0={cal['h_reg_buckets'][0]} .. bucket16={cal['h_reg_buckets'][16]})")
    print(f"  acrescimo 2T: {cal['h_stop_2h']}/min  |  1T: {cal['h_stop_1h']}/min")
    t2 = cal["_meta"]["stoppage_timing_2t"]
    print(f"  timing acrescimo 2T (medido, n={t2['n']}): {t2['frac_alem_de_4']:.0%} "
          f"dos gols ocorrem ALEM de 90+4 (o prior stoppage_extra_pmf trunca em +4)")
    print(f"  -> {a.out}")


if __name__ == "__main__":
    main()
