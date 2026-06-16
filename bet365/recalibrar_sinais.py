# -*- coding: utf-8 -*-
"""
recalibrar_sinais.py — fecha o loop de calibracao (Stage E do plano).

O modelo cospe uma probabilidade (ex.: "78%"). Mas "78%" so VALE se, no historico,
sinais de ~78% acertarem ~78% das vezes. validar_sinais.py MEDE esse desvio; este
script o CORRIGE: ajusta um mapa monotono P_raw -> P_calibrado a partir dos sinais
ja liquidados (sinal_log com resultado_final) e grava em bet365/calibracao_correcao.json.

O alertador_valor.js (via /correcao) e o prob_aovivo.py carregam esse mapa e aplicam
na probabilidade ANTES de decidir. Mesmo algoritmo dos dois lados (paridade).

HONESTO: so vira util com amostra. n<40 sinais liquidados -> mapa IDENTIDADE (no-op).
  40<=n<80 -> Platt (logistica 1-D, poucos parametros, estavel).
  n>=80    -> isotonica binada (PAV), mais flexivel.
NAO transforma -EV em +EV: so faz a probabilidade prevista bater com a frequencia real,
o que melhora o gating (banda/VALOR). O juiz continua sendo o P&L do validar_sinais.py.

Uso:
  py bet365/recalibrar_sinais.py            # ajusta e grava o JSON (ou identidade se faltar dado)
  py bet365/recalibrar_sinais.py --listar   # so mostra o estado atual, nao grava
"""
import argparse
import json
import math
import os
import sys

PASTA = os.path.dirname(os.path.abspath(__file__))
SAIDA = os.path.join(PASTA, "calibracao_correcao.json")
MIN_AMOSTRA = 40        # abaixo disto: identidade (no-op)
MIN_ISOTONICA = 80      # a partir disto: isotonica; entre 40 e 80: Platt
CAP_CORRECAO = 0.15     # calibracao e NUDGE: nunca move a prob mais que isto (anti-overfit de amostra pequena)


def _com_cap(f, cap=CAP_CORRECAO):
    """Limita o quanto o mapa afasta P do valor cru -> seguranca contra amostra pequena/separavel."""
    return lambda p: max(0.01, min(0.99, max(p - cap, min(p + cap, f(p)))))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")


def carregar_pares():
    """(prob_raw, acertou) de cada sinal liquidado. Reusa o conectar() do validar_sinais."""
    sys.path.insert(0, PASTA)
    import validar_sinais
    con = validar_sinais.conectar()
    try:
        rows = con.execute("SELECT prob, acertou FROM sinal_log "
                           "WHERE resultado_final IS NOT NULL AND prob IS NOT NULL "
                           "AND acertou IS NOT NULL").fetchall()
    finally:
        con.close()
    return [(float(p), int(y)) for p, y in rows if 0.0 < p < 1.0]


def brier(pares, mapa):
    """Erro quadratico medio (Brier) aplicando `mapa` a cada prob."""
    if not pares:
        return None
    return sum((mapa(p) - y) ** 2 for p, y in pares) / len(pares)


# ---------------------------------------------------------- Platt (logistica 1-D)
def ajustar_platt(pares, iters=5000, lr=0.1):
    """q = 1/(1+exp(a*p+b)) minimizando log-loss. Retorna (a, b) na convencao do JS.
    Gradiente normalizado por n; z clampado p/ nao estourar exp."""
    a, b = -4.0, 2.0                        # chute inicial: crescente em p (a<0)
    n = len(pares)
    for _ in range(iters):
        ga = gb = 0.0
        for p, y in pares:
            z = max(-30.0, min(30.0, a * p + b))
            q = 1.0 / (1.0 + math.exp(z))   # q = sigmoid(-z): ∂NLL/∂a = -Σ(q-y)p
            ga += (q - y) * p
            gb += (q - y)
        a += lr * ga / n                    # descida do log-loss (sinal +, vide derivacao)
        b += lr * gb / n
    return a, b


# ---------------------------------------------------------- isotonica (PAV binada)
def ajustar_isotonica(pares, largura=0.05):
    """Bina por prob, tira a media de acerto por bin, e forca monotonicidade (PAV).
    Retorna lista de vertices [[prob_medio, calibrado], ...] crescente em prob."""
    bins = {}
    for p, y in pares:
        k = min(int(p / largura), int(1.0 / largura) - 1)
        b = bins.setdefault(k, [0.0, 0.0, 0])   # soma_p, soma_y, n
        b[0] += p; b[1] += y; b[2] += 1
    pontos = []
    for k in sorted(bins):
        sp, sy, c = bins[k]
        pontos.append([sp / c, sy / c, c])      # prob_medio, acerto_medio, peso
    # pool-adjacent-violators: garante calibrado nao-decrescente
    i = 0
    while i < len(pontos) - 1:
        if pontos[i][1] > pontos[i + 1][1]:     # violacao -> funde os dois blocos
            w = pontos[i][2] + pontos[i + 1][2]
            val = (pontos[i][1] * pontos[i][2] + pontos[i + 1][1] * pontos[i + 1][2]) / w
            pmed = (pontos[i][0] * pontos[i][2] + pontos[i + 1][0] * pontos[i + 1][2]) / w
            pontos[i] = [pmed, val, w]
            del pontos[i + 1]
            if i > 0:
                i -= 1                          # re-checa o bloco anterior
        else:
            i += 1
    return [[round(p, 4), round(v, 4)] for p, v, _ in pontos]


def construir(pares):
    """Escolhe o metodo pela amostra e devolve (dict_json, funcao_mapa)."""
    n = len(pares)
    if n < MIN_AMOSTRA:
        return {"metodo": "identidade",
                "_meta": {"n_settled": n, "min_amostra": MIN_AMOSTRA,
                          "obs": f"faltam {MIN_AMOSTRA - n} sinais liquidados p/ corrigir"}}, (lambda p: p)
    if n < MIN_ISOTONICA:
        a, b = ajustar_platt(pares)
        if not (-20.0 < a < 0.0):           # fit degenerou (amostra separavel / direcao errada) -> identidade
            return {"metodo": "identidade",
                    "_meta": {"n_settled": n, "min_amostra": MIN_AMOSTRA,
                              "obs": "ajuste Platt instavel (a fora de (-20,0)); mantendo identidade"}}, (lambda p: p)
        mapa = _com_cap(lambda p: 1.0 / (1.0 + math.exp(max(-30.0, min(30.0, a * p + b)))))
        return {"metodo": "platt", "a": round(a, 5), "b": round(b, 5), "cap": CAP_CORRECAO,
                "_meta": {"n_settled": n, "min_amostra": MIN_AMOSTRA}}, mapa
    pontos = ajustar_isotonica(pares)
    mapa = _com_cap(_mapa_isotonica(pontos))
    return {"metodo": "isotonic", "pontos": pontos, "cap": CAP_CORRECAO,
            "_meta": {"n_settled": n, "min_amostra": MIN_AMOSTRA}}, mapa


def _mapa_isotonica(pontos):
    """Interpolacao linear entre vertices (mesma logica do aplicarCorrecao JS/py)."""
    def f(p):
        if not pontos:
            return p
        if p <= pontos[0][0]:
            return pontos[0][1]
        if p >= pontos[-1][0]:
            return pontos[-1][1]
        for i in range(1, len(pontos)):
            if p <= pontos[i][0]:
                x0, y0 = pontos[i - 1]; x1, y1 = pontos[i]
                return y0 + (y1 - y0) * (p - x0) / ((x1 - x0) or 1.0)
        return pontos[-1][1]
    return f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listar", action="store_true", help="so mostra; nao grava o JSON")
    a = ap.parse_args()

    pares = carregar_pares()
    n = len(pares)
    doc, mapa = construir(pares)

    b_antes = brier(pares, lambda p: p)
    b_depois = brier(pares, mapa)
    # SEGURANCA: so deploia a correcao se ela MELHORA o Brier na amostra; senao volta p/ identidade.
    # Isso pega qualquer ajuste degenerado antes de ir pro ar (ex.: mapa que pinaria todos os sinais).
    if doc["metodo"] != "identidade" and not (b_antes is not None and b_depois < b_antes - 1e-9):
        doc = {"metodo": "identidade",
               "_meta": {"n_settled": n, "min_amostra": MIN_AMOSTRA, "obs": "sem ganho de Brier"}}
        mapa = (lambda p: p); b_depois = b_antes
    if b_antes is not None:
        doc["_meta"]["brier_antes"] = round(b_antes, 4)
        doc["_meta"]["brier_depois"] = round(b_depois, 4)

    print(f"Sinais liquidados: {n}  ->  metodo: {doc['metodo']}")
    if n:
        prev = sum(p for p, _ in pares) / n
        real = sum(y for _, y in pares) / n
        print(f"  prob media prevista {prev*100:.1f}%  vs  acerto real {real*100:.1f}%")
    if b_antes is not None:
        print(f"  Brier antes {b_antes:.4f}  ->  depois {b_depois:.4f}"
              + ("  (melhorou)" if b_depois < b_antes else "  (sem ganho)"))
    if doc["metodo"] == "identidade":
        print(f"  >> mapa IDENTIDADE: {doc['_meta'].get('obs','')}. Rode mais sinais ao vivo.")
    else:
        for p in (0.70, 0.80, 0.90):
            print(f"     {p*100:.0f}% previsto -> {mapa(p)*100:.1f}% calibrado")

    if a.listar:
        print("(--listar: nao gravei)")
        return
    with open(SAIDA, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    print(f"Gravado: {SAIDA}")


if __name__ == "__main__":
    main()
