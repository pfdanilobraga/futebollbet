# -*- coding: utf-8 -*-
"""
calibrar_mercado.py — calibra os modelos AO VIVO de mercados de CONTAGEM
(escanteios, chutes, chutes ao gol, cartões) a partir do futebol.db e grava
bet365/mercado_cal.json.

SOMENTE LEITURA do banco. Análogo do calibrar_hazard.py (que calibra GOLS),
mas para totais de contagem. Diferença importante:

  O `incidente` guarda o minuto de GOLS e CARTÕES, mas NÃO de escanteios/chutes.
  Logo, para a maioria dos mercados NÃO há timing por minuto no banco. Então:
    - NÍVEL (média total e share da casa): calibrado dos dados REAIS
      (evento_estatistica) — confiável.
    - FORMA da curva por minuto (h_reg): é um PRIOR (default = quase plano).
      O modelo ao vivo compensa isso fazendo BLEND com o ritmo OBSERVADO no
      jogo (ver prob_aovivo_mercado.py), que domina conforme o jogo avança.

  Recalibrar a FORMA só vale quando o minuto de cada escanteio for coletado
  (fonte nova — graph/incidentes estendidos do SofaScore).

Uso:
  py bet365/calibrar_mercado.py
  py bet365/calibrar_mercado.py --db ../futebol.db --out mercado_cal.json
"""
import argparse
import json
import os
import sqlite3
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

PASTA = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(PASTA)
sys.path.insert(0, RAIZ)
import mercados as MK   # reusa a config de mercados (nomes de estatística)

N_BUCKETS = 18          # 18 buckets de 5 min cobrem 0..89

# PRIOR de FORMA da curva por bucket (pesos relativos; normalizado depois).
# "flat"   = hazard constante (default seguro — sem dado de timing).
# "subida" = leve aumento no 2T + fim de jogo (mão na bola, pressão no fim).
SHAPES = {
    "flat":   [1.0] * N_BUCKETS,
    "subida": [0.85, 0.90, 0.95, 1.00, 1.00, 1.00, 1.05, 1.05, 1.05,   # 1T
               1.00, 1.05, 1.05, 1.10, 1.10, 1.15, 1.20, 1.25, 1.35],  # 2T (fim mais quente)
}
# forma usada por mercado (escanteios/cartões sobem no fim; chutes ~plano)
SHAPE_POR_MERCADO = {
    "escanteios": "subida", "cartoes": "subida",
    "chutes": "flat", "chutes_gol": "flat",
}

# hazard de acréscimo (por min jogado) como fração da taxa média regular.
# Sem timing de escanteio no banco -> PRIOR. O fim de jogo é mais intenso.
FATOR_ACRESCIMO = 1.4
# PMF de minutos extras jogados além do anunciado (igual ao modelo de gols).
STOPPAGE_EXTRA_PMF = [0.45, 0.28, 0.15, 0.08, 0.04]

# blend ritmo-observado x base (shrinkage por minutos jogados) — consumido ao vivo
PACE = {"K": 25.0, "w_max": 0.85}   # w = min(w_max, m/(m+K))
# multiplicadores ao vivo (placar/vermelho) — priors, mesma família do modelo de gols
MULT = {"sigma": 0.30, "d0": 1.5, "clampLo": 0.5, "clampHi": 1.8,
        "red_down": 0.80, "red_up": 1.20}
SIGMA_LOG = {"base": 0.25, "sem_pace": 0.10}


def calibrar_mercado(con, mercado, cfg):
    nomes = cfg["stat"]
    ph = ",".join("?" * len(nomes))
    row = con.execute(
        f"""SELECT COUNT(*) AS n,
                   AVG(casa_valor + fora_valor) AS media_total,
                   SUM(casa_valor) AS sc, SUM(casa_valor + fora_valor) AS st
            FROM evento_estatistica ee JOIN evento e ON e.id = ee.evento_id
            WHERE ee.periodo='ALL' AND ee.nome IN ({ph})
              AND e.status='finished' AND ee.casa_valor IS NOT NULL""",
        nomes).fetchone()
    n, media_total, sc, st = row
    if not n or not media_total or not st:
        return None
    home_share = sc / st
    shape_nome = SHAPE_POR_MERCADO.get(mercado, "flat")
    shape = SHAPES[shape_nome]

    # converte pesos de forma em taxa/min por bucket, escalando p/ que o
    # integral em 90 min = media_total (o NÍVEL vem do dado real).
    soma_pesos = sum(shape)
    h_reg = [round(media_total * w / soma_pesos / 5.0, 6) for w in shape]
    return {
        "n_jogos": n,
        "media_total": round(media_total, 4),
        "home_share": round(home_share, 4),
        "shape": shape_nome,
        "h_reg_buckets": h_reg,                 # taxa/min por bucket 0..17 (min 0..89)
        "fator_acrescimo": FATOR_ACRESCIMO,
        "stoppage_extra_pmf": STOPPAGE_EXTRA_PMF,
        "pace": PACE,
        "mult": MULT,
        "sigma_log": SIGMA_LOG,
    }


def calibrar(db):
    con = sqlite3.connect(db)
    try:
        out = {"_meta": {"fonte": os.path.basename(db),
                         "obs": "NÍVEL (media_total/home_share) é real; FORMA da "
                                "curva é PRIOR — o blend de ritmo observado domina "
                                "ao vivo. Recalibrar forma quando houver minuto de "
                                "escanteio coletado."}}
        for mercado, cfg in MK.MERCADOS.items():
            cal = calibrar_mercado(con, mercado, cfg)
            if cal:
                out[mercado] = cal
        return out
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(PASTA, "..", "futebol.db"))
    ap.add_argument("--out", default=os.path.join(PASTA, "mercado_cal.json"))
    a = ap.parse_args()

    cal = calibrar(a.db)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(cal, f, ensure_ascii=False, indent=1)

    mercados = [k for k in cal if not k.startswith("_")]
    if not mercados:
        print("Nenhum mercado calibrado — evento_estatistica vazio? "
              "Rode: py coletor.py pendentes")
    for m in mercados:
        c = cal[m]
        print(f"{m:<12} {c['n_jogos']:>5} jogos  media_total={c['media_total']:.2f}  "
              f"share_casa={c['home_share']:.2f}  forma={c['shape']}")
    print(f"-> {a.out}")


if __name__ == "__main__":
    main()
