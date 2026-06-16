# -*- coding: utf-8 -*-
"""
prob_aovivo_mercado.py — probabilidade AO VIVO de mercados de CONTAGEM
Over/Under (escanteios, chutes, chutes ao gol, cartões) dado o minuto e a
contagem atual.

Análogo do prob_aovivo.py (que faz 1X2/gols). Como a contagem é um total
simples, é mais direto que o 1X2: não há ordenação de dois Poisson — só
P(total final > linha).

Modelo:
  - hazard h(t) por minuto (de mercado_cal.json; FORMA é prior, NÍVEL é real).
  - Λ_rem = integral de h(t) no tempo restante (+ acréscimo marginalizado).
  - BLEND DE RITMO: escala Λ pelo ritmo OBSERVADO no jogo
    (contagem_atual / esperado_até_agora), com shrinkage w=min(w_max, m/(m+K)).
    É o que torna o modelo responsivo — escanteios/chutes acumulam rápido.
  - Multiplicadores placar/vermelho por lado (time perdendo ataca mais ->
    mais escanteios; time com um a menos ataca menos).
  - N_final = contagem_atual + Poisson(Λ_rem). P(Over/Under linha).
  - Totais por time idem, usando o share (calibrado, blendado com o observado).
  - Banda de confiança: incerteza no Λ vira intervalo em P.

Uso:
  py bet365/prob_aovivo_mercado.py --mercado escanteios --min 70 --casa 5 --fora 3 --linha 9.5
  py bet365/prob_aovivo_mercado.py --mercado escanteios --min 80 --casa 6 --fora 4 --linha 10.5 --acrescimo 4
  py bet365/prob_aovivo_mercado.py --mercado chutes_gol --min 60 --casa 4 --fora 2 --linha 7.5
  # total de um time:
  py bet365/prob_aovivo_mercado.py --mercado escanteios --min 75 --casa 5 --fora 2 --linha 5.5 --lado casa

NÃO é conselho de aposta. Só a matemática do cenário.
"""
import argparse
import json
import math
import os
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

PASTA = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(PASTA)
sys.path.insert(0, RAIZ)
import mercados as MK   # reusa over_under / pois_* (mesma matemática do pré-jogo)

CAL_PATH = os.path.join(PASTA, "mercado_cal.json")


def carregar_cal(path=CAL_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# --------------------------------------------------- hazard / lambda restante
def lam_regular_restante(m, h_reg):
    """Integra o hazard regular (buckets de 5 min) de `m` até 90'."""
    if m >= 90:
        return 0.0
    total = 0.0
    for b in range(len(h_reg)):
        lo, hi = 5.0 * b, 5.0 * b + 5.0
        ov = min(90.0, hi) - max(m, lo)
        if ov > 0:
            total += h_reg[b] * ov
    return total


def lam_esperado_ate(m, h_reg):
    """Integra o hazard de 0 até `m` (esperado acumulado até agora)."""
    mm = min(m, 90.0)
    total = 0.0
    for b in range(len(h_reg)):
        lo, hi = 5.0 * b, 5.0 * b + 5.0
        ov = min(mm, hi) - max(0.0, lo)
        if ov > 0:
            total += h_reg[b] * ov
    return total


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def multiplicadores(gc, gf, m, restante, cal):
    """(M_casa, M_fora) na taxa de eventos: líder cria menos, perdedor cria mais;
    vermelho reduz o lado em desvantagem. Placar de GOLS move o ritmo de
    escanteios/chutes (não a contagem do próprio mercado)."""
    mu = cal["mult"]
    sigma, d0 = mu["sigma"], mu["d0"]
    lo, hi = mu["clampLo"], mu["clampHi"]
    d = gc - gf
    push = 0.5 + 0.5 * (min(m, 90.0) / 90.0)
    t = math.tanh(d / d0)
    m_c = 1.0 + sigma * (-t * push)   # casa liderando -> cria menos
    m_f = 1.0 + sigma * (t * push)
    return clamp(m_c, lo, hi), clamp(m_f, lo, hi)


def mult_vermelho(red_casa, red_fora, restante, cal):
    mu = cal["mult"]
    net = (red_casa or 0) - (red_fora or 0)
    neff = abs(net) * min(1.0, max(0.0, restante) / 30.0)
    if net > 0:     # casa com um a menos -> casa cria menos
        return mu["red_down"] ** neff, mu["red_up"] ** neff
    if net < 0:
        return mu["red_up"] ** neff, mu["red_down"] ** neff
    return 1.0, 1.0


# --------------------------------------------------- núcleo
def lam_restante_lados(m, c_casa, c_fora, cal, acrescimo, gc, gf,
                       red_casa, red_fora, escala=1.0):
    """Devolve (lam_rem_casa, lam_rem_fora) já com blend de ritmo, multiplicadores
    e marginalização do acréscimo. `escala` é o fator da banda de confiança."""
    h_reg = cal["h_reg_buckets"]
    pace = cal["pace"]
    share_base = cal["home_share"]
    C = c_casa + c_fora

    lam_reg_rem = lam_regular_restante(m, h_reg)
    # acréscimo: hazard regular médio * fator * minutos extras (marginalizado fora)
    taxa_media = cal["media_total"] / 90.0
    h_stop = taxa_media * cal.get("fator_acrescimo", 1.4)

    # blend de ritmo: compara contagem atual com o esperado até agora
    esp_ate = lam_esperado_ate(m, h_reg)
    w = min(pace["w_max"], m / (m + pace["K"])) if m > 0 else 0.0
    pace_factor = (C / esp_ate) if esp_ate > 0 and C > 0 else 1.0
    pf_eff = 1.0 + w * (pace_factor - 1.0)

    # share: base calibrado blendado com o observado no jogo
    share_obs = (c_casa / C) if C > 0 else share_base
    share_eff = (1.0 - w) * share_base + w * share_obs

    restante = max(0.0, 90.0 - m) + acrescimo
    M_c, M_f = multiplicadores(gc, gf, m, restante, cal)
    R_c, R_f = mult_vermelho(red_casa, red_fora, restante, cal)

    pmf = cal["stoppage_extra_pmf"]
    elapsed_stop = max(0.0, m - 90.0)
    lam_casa = lam_fora = 0.0
    wsum = 0.0
    for x, wx in enumerate(pmf):
        S = acrescimo + x
        rem_stop = max(0.0, S - elapsed_stop)
        lam_t = (lam_reg_rem + h_stop * rem_stop) * pf_eff * escala
        lam_casa += wx * lam_t * share_eff * M_c * R_c
        lam_fora += wx * lam_t * (1.0 - share_eff) * M_f * R_f
        wsum += wx
    if wsum > 0:
        lam_casa /= wsum
        lam_fora /= wsum
    return lam_casa, lam_fora, {"pace_factor": round(pace_factor, 3),
                                "pf_eff": round(pf_eff, 3),
                                "share_eff": round(share_eff, 3), "w": round(w, 3)}


def prob_over_under(m, c_casa, c_fora, linha, cal, lado="total", **kw):
    """P(Over), P(Under) do total (lado='total') ou de um time ('casa'/'fora')."""
    lam_c, lam_f, det = lam_restante_lados(m, c_casa, c_fora, cal, **kw)
    if lado == "casa":
        return (*MK.over_under(lam_c, linha - c_casa), det)
    if lado == "fora":
        return (*MK.over_under(lam_f, linha - c_fora), det)
    return (*MK.over_under(lam_c + lam_f, linha - (c_casa + c_fora)), det)


def banda(m, c_casa, c_fora, linha, cal, lado, **kw):
    sl = cal["sigma_log"]
    s = sl["base"] + (sl.get("sem_pace", 0.10) if (c_casa + c_fora) == 0 else 0.0)
    o_hi, _, _ = prob_over_under(m, c_casa, c_fora, linha, cal, lado,
                                 escala=math.exp(+s), **kw)
    o_lo, _, _ = prob_over_under(m, c_casa, c_fora, linha, cal, lado,
                                 escala=math.exp(-s), **kw)
    return min(o_lo, o_hi), max(o_lo, o_hi)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mercado", required=True, choices=list(MK.MERCADOS))
    ap.add_argument("--min", type=float, required=True)
    ap.add_argument("--casa", type=int, required=True, help="contagem atual do mandante")
    ap.add_argument("--fora", type=int, required=True, help="contagem atual do visitante")
    ap.add_argument("--linha", type=float, required=True)
    ap.add_argument("--lado", choices=["total", "casa", "fora"], default="total")
    ap.add_argument("--acrescimo", type=float, default=4.0)
    ap.add_argument("--gols-casa", type=int, default=0, help="placar de gols (move o ritmo)")
    ap.add_argument("--gols-fora", type=int, default=0)
    ap.add_argument("--vermelho-casa", type=int, default=0)
    ap.add_argument("--vermelho-fora", type=int, default=0)
    a = ap.parse_args()

    cal_all = carregar_cal()
    cal = cal_all.get(a.mercado)
    if not cal:
        print(f"Sem calibração para '{a.mercado}'. Rode: py bet365/calibrar_mercado.py")
        return

    kw = dict(acrescimo=a.acrescimo, gc=a.gols_casa, gf=a.gols_fora,
              red_casa=a.vermelho_casa, red_fora=a.vermelho_fora)
    p_over, p_under, det = prob_over_under(a.min, a.casa, a.fora, a.linha, cal,
                                           a.lado, **kw)
    blo, bhi = banda(a.min, a.casa, a.fora, a.linha, cal, a.lado, **kw)

    C = a.casa + a.fora if a.lado == "total" else (a.casa if a.lado == "casa" else a.fora)
    odd = lambda p: f"{1/p:6.2f}" if p > 0 else "  inf"
    rest = max(0.0, 90.0 - a.min) + a.acrescimo
    print(f"\n{a.mercado} ({a.lado})  linha {a.linha}  |  atual {a.casa}-{a.fora} "
          f"(={C})  aos {a.min:.0f}'  |  ~{rest:.0f} min restantes")
    print(f"[ritmo] fator={det['pace_factor']} (efetivo {det['pf_eff']}, peso {det['w']})  "
          f"share casa {det['share_eff']*100:.0f}%")
    print("-" * 56)
    print(f"  OVER  {a.linha}: {p_over*100:6.2f}%   odd justa {odd(p_over)}   "
          f"[banda {blo*100:.0f}-{bhi*100:.0f}%]")
    print(f"  UNDER {a.linha}: {p_under*100:6.2f}%   odd justa {odd(p_under)}")
    print("Obs: banda larga = entrada incerta. Odd real do bet365 vem com margem.")


if __name__ == "__main__":
    main()
