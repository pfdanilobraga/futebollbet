# -*- coding: utf-8 -*-
"""
sofascore_live.py — segunda fonte AO VIVO (SofaScore) pro alertador do bet365.

Pra que serve: o relogio do bet365 deriva (clock-fantasma) e ele nao expoe o
acrescimo anunciado de forma confiavel. O SofaScore da um minuto autoritativo,
o status (inprogress/finished) e o acrescimo (injuryTime). Cruzando os dois, o
alertador:
  - mata GREEN se o SofaScore disser que o jogo ja terminou (finished);
  - confirma se o minuto do bet365 bate com o real (anti-deriva);
  - usa o acrescimo anunciado de verdade no calculo do GREEN.

Arquitetura (caminho "completo"): o bet365 LOGADO fica no seu Chrome real (onde
o alertador roda no console). Este script roda em paralelo, busca o SofaScore
(curl-cffi -> Playwright via transporte.py) e SERVE um cross.json em
http://localhost:8765 com CORS liberado. O alertador faz fetch desse endpoint a
cada scan e popula window.__ssCross. (Chrome permite http://localhost mesmo a
partir de pagina https — localhost e "secure context".)

Uso:
  py bet365/sofascore_live.py live           # lista jogos ao vivo (debug)
  py bet365/sofascore_live.py casar "Flora Tallinn" "Levadia"   # testa matcher
  py bet365/sofascore_live.py servir          # sobe o servidor em :8765
  py bet365/sofascore_live.py servir --porta 8765 --intervalo 12

Depois, no console do bet365:  iniciarValor({ssUrl:'http://localhost:8765'})
"""
import argparse
import json
import os
import sys
import time
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import transporte  # noqa: E402  (camada curl-cffi -> Playwright do projeto)

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

# tokens removidos na normalizacao de nome de time (ruido que difere entre sites)
_RUIDO = {"fc", "cf", "sc", "ac", "afc", "cd", "ca", "club", "clube", "calcio",
          "ssd", "ssc", "us", "as", "if", "if1", "sk", "fk", "bk", "ik", "il",
          "de", "do", "da", "the", "fa"}


def normalizar(nome):
    """Nome de time -> chave robusta (minuscula, sem acento, sem ruido)."""
    if not nome:
        return ""
    s = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode()
    s = s.lower()
    for ch in "._-/'":
        s = s.replace(ch, " ")
    toks = [t for t in s.split() if t and t not in _RUIDO]
    # reserva/feminino mantidos como marcador (ii, b, u21, w)
    return " ".join(toks)


def chave(home, away):
    return normalizar(home) + "|" + normalizar(away)


# ---------------------------------------------------------------- live
def _minuto(ev, agora):
    """Estima o minuto corrente a partir do inicio do periodo + status."""
    st = ev.get("status", {}) or {}
    code, desc = st.get("code"), (st.get("description") or "").lower()
    t = ev.get("time", {}) or {}
    ini = t.get("currentPeriodStartTimestamp")
    if ini is None:
        return None
    decorrido = max(0.0, (agora - ini) / 60.0)
    # 2T: codes 7/8 ou descricao com "2nd"; soma 45
    if code in (7, 8) or "2nd" in desc or "second" in desc:
        return 45.0 + decorrido
    if code in (6,) or "1st" in desc or "first" in desc:
        return min(45.0 + 0, decorrido)
    # intervalo/outros: usa o que der
    return decorrido


def _injury(ev):
    """Acrescimo anunciado do 2T (minutos) se disponivel; senao None."""
    t = ev.get("time", {}) or {}
    v = t.get("injuryTime2")
    if isinstance(v, int) and 0 < v < 30:   # ignora sentinela/absurdo (ex.: 999)
        return v
    return None


def eventos_ao_vivo():
    """Lista normalizada de jogos de futebol em andamento no SofaScore."""
    status, data = transporte.buscar("/sport/football/events/live", ok_404=True)
    if status != 200 or not data:
        return [], status
    agora = time.time()
    out = []
    for ev in data.get("events", []):
        stype = (ev.get("status", {}) or {}).get("type")
        if stype not in ("inprogress", "finished"):
            continue
        home = (ev.get("homeTeam", {}) or {}).get("name", "")
        away = (ev.get("awayTeam", {}) or {}).get("name", "")
        liga = ((ev.get("tournament", {}) or {}).get("name")
                or (ev.get("tournament", {}) or {}).get("slug") or "")
        hs = (ev.get("homeScore", {}) or {}).get("current")
        as_ = (ev.get("awayScore", {}) or {}).get("current")
        out.append({
            "home": home, "away": away, "liga": liga,
            "casa_norm": normalizar(home), "fora_norm": normalizar(away),
            "casa_id": (ev.get("homeTeam", {}) or {}).get("id"),
            "fora_id": (ev.get("awayTeam", {}) or {}).get("id"),
            "min": round(_minuto(ev, agora), 1) if _minuto(ev, agora) is not None else None,
            "status": stype, "injury": _injury(ev),
            "placar": f"{hs}-{as_}" if hs is not None else None,
            "event_id": ev.get("id"),
        })
    return out, 200


# ---------------------------------------------------------------- forca por time
GOLS_JOGO = 2.2682                    # media global (hazard_cal.json)
MEDIA_CASA = GOLS_JOGO * 0.5489       # baseline de gols do mandante (~1.245)
MEDIA_FORA = GOLS_JOGO * (1 - 0.5489) # baseline do visitante (~1.023)
TAXAS_TTL = 6 * 3600                  # cache da forma do time: 6h (muda devagar)
_cache_taxas = {}                     # (team_id, em_casa) -> (atq, def, n, ts)


def taxas_time(team_id, em_casa, agora):
    """(ataque, defesa, n) do time no mando, dos ultimos jogos (SofaScore). Cacheado 6h."""
    ck = (team_id, em_casa)
    c = _cache_taxas.get(ck)
    if c and agora - c[3] < TAXAS_TTL:
        return c[0], c[1], c[2]
    st, data = transporte.buscar(f"/team/{team_id}/events/last/0", ok_404=True)
    if st != 200 or not data:
        return None, None, 0
    feitos, sofridos = [], []
    for ev in data.get("events", []):
        if (ev.get("status", {}) or {}).get("type") != "finished":
            continue
        eh_casa = (ev.get("homeTeam", {}) or {}).get("id") == team_id
        if eh_casa != em_casa:
            continue                  # so conta jogos no mando certo
        hs = (ev.get("homeScore", {}) or {}).get("current")
        as_ = (ev.get("awayScore", {}) or {}).get("current")
        if hs is None or as_ is None:
            continue
        gf, ga = (hs, as_) if eh_casa else (as_, hs)
        feitos.append(gf); sofridos.append(ga)
    n = len(feitos)
    if n == 0:
        _cache_taxas[ck] = (None, None, 0, agora); return None, None, 0
    atq, dfe = sum(feitos) / n, sum(sofridos) / n
    _cache_taxas[ck] = (atq, dfe, n, agora)
    return atq, dfe, n


def forca_confronto(casa_id, fora_id, agora):
    """{lam_casa, lam_fora, n} do confronto (ataque x defesa), ou None.
    Mesma matematica do probabilidades.py. Como nao temos a media da liga ao
    vivo de graca, usamos o AMBIENTE de gols dos proprios 2 times como baseline
    (corrige o vies que um baseline global fixo daria em liga defensiva/goleadora;
    o nivel `r` do blend fica absoluto vs a media global, sem chamada extra)."""
    if not casa_id or not fora_id:
        return None
    atq_c, def_c, nc = taxas_time(casa_id, True, agora)
    atq_f, def_f, nf = taxas_time(fora_id, False, agora)
    if None in (atq_c, def_c, atq_f, def_f):
        return None
    # total tipico de gols no ambiente dos 2 times -> baseline casa/fora
    total_amb = (atq_c + def_c + atq_f + def_f) / 2.0 or GOLS_JOGO
    base_casa = max(0.2, total_amb * 0.5489)
    base_fora = max(0.2, total_amb * (1 - 0.5489))
    return {"lam_casa": atq_c * def_f / base_casa,
            "lam_fora": atq_f * def_c / base_fora,
            "n": min(nc, nf, 19)}


# ---------------------------------------------------------------- momentum (pressao ao vivo)
import math  # noqa: E402

MOM_TTL = 25            # momentum muda durante o jogo -> cache curto (s)
MOM_GAMMA = 0.5         # forca do efeito (exp)
# SofaScore nao tem "ataques perigosos"; usamos metricas mais fortes. Pesos somam 1.
# (Sem "Big chances": baixa contagem por tempo e ruidosa; o xG ja capta qualidade.)
MOM_PESOS = {
    "Expected goals": 0.40,
    "Shots on target": 0.25,
    "Touches in penalty area": 0.20,   # ~ "ataque perigoso" (so existe no periodo ALL)
    "Total shots": 0.10,
    "Ball possession": 0.05,           # posse sozinha engana -> peso baixo
}
SHARE_CLAMP = (0.15, 0.85)             # 1 evento isolado nao domina o indice
_cache_mom = {}         # event_id -> (mom_casa, mom_fora, ts)


def _stats_periodo(data, periodo):
    """{nome_stat: (home, away)} de um periodo (ALL/1ST/2ND). {} se ausente."""
    for p in data.get("statistics", []):
        if p.get("period") == periodo:
            out = {}
            for g in p.get("groups", []):
                for it in g.get("statisticsItems", []):
                    out[it.get("name")] = (it.get("homeValue"), it.get("awayValue"))
            return out
    return {}


def momentum_de_stats(data):
    """{mom_casa, mom_fora} a partir do payload /statistics. None se sem dado.
    Usa o 2T (dominancia recente); cai pro jogo todo se 2T ausente. PURO/testavel."""
    stats = _stats_periodo(data, "2ND") or _stats_periodo(data, "ALL")
    if not stats:
        return None
    soma_w = soma_share = 0.0
    for nome, w in MOM_PESOS.items():
        v = stats.get(nome)
        if not v or v[0] is None or v[1] is None:
            continue
        h, a = float(v[0]), float(v[1])
        if h + a <= 0:
            continue
        sh = max(SHARE_CLAMP[0], min(SHARE_CLAMP[1], h / (h + a)))  # clamp anti-ruido
        soma_share += w * sh              # fracao do mandante nessa metrica
        soma_w += w
    if soma_w == 0:
        return None
    ph = soma_share / soma_w               # pressao do mandante em [0,1]; 0.5 = equilibrio
    mom_casa = max(0.6, min(1.7, math.exp(MOM_GAMMA * 2 * (ph - 0.5))))
    mom_fora = max(0.6, min(1.7, math.exp(MOM_GAMMA * 2 * (0.5 - ph))))
    return {"casa": round(mom_casa, 3), "fora": round(mom_fora, 3), "ph": round(ph, 3)}


def momentum_confronto(event_id, agora):
    """{casa, fora} (multiplicadores de momentum) do jogo ao vivo, ou None. Cache 25s."""
    if not event_id:
        return None
    c = _cache_mom.get(event_id)
    if c and agora - c[2] < MOM_TTL:
        return {"casa": c[0], "fora": c[1]}
    st, data = transporte.buscar(f"/event/{event_id}/statistics", ok_404=True)
    if st != 200 or not data:
        return None
    m = momentum_de_stats(data)
    if not m:
        return None
    _cache_mom[event_id] = (m["casa"], m["fora"], agora)
    return m


CARD_TTL = 30           # cartoes mudam pouco -> cache curto (s)
_cache_cards = {}       # event_id -> (redC, redF, ts)


def vermelhos_confronto(event_id, agora):
    """{redC, redF}: vermelhos (red + 2o amarelo) por lado, do SofaScore. None se sem dado.
    Fallback do (A): so usado quando o DOM do bet365 nao trouxe o cartao."""
    if not event_id:
        return None
    c = _cache_cards.get(event_id)
    if c and agora - c[2] < CARD_TTL:
        return {"redC": c[0], "redF": c[1]}
    st, data = transporte.buscar(f"/event/{event_id}/incidents", ok_404=True)
    if st != 200 or not data:
        return None
    rc = rf = 0
    for it in data.get("incidents", []):
        if it.get("incidentType") != "card":
            continue
        if (it.get("incidentClass") or "").lower() in ("red", "yellowred"):
            if it.get("isHome"):
                rc += 1
            else:
                rf += 1
    _cache_cards[event_id] = (rc, rf, agora)
    return {"redC": rc, "redF": rf}


def casar(b365_home, b365_away, eventos):
    """Acha o evento SofaScore que corresponde ao confronto do bet365."""
    h, a = normalizar(b365_home), normalizar(b365_away)
    melhor, score = None, 0.0
    for ev in eventos:
        s = _sim(h, ev["casa_norm"]) * 0.5 + _sim(a, ev["fora_norm"]) * 0.5
        if s > score:
            melhor, score = ev, s
    return (melhor, round(score, 2)) if score >= 0.6 else (None, round(score, 2))


def _sim(x, y):
    """Similaridade simples por tokens (Jaccard) + contencao de substring."""
    if not x or not y:
        return 0.0
    if x == y:
        return 1.0
    sx, sy = set(x.split()), set(y.split())
    jac = len(sx & sy) / len(sx | sy) if (sx | sy) else 0.0
    sub = 1.0 if (x in y or y in x) else 0.0
    return max(jac, sub * 0.9)


def montar_cross(com_forca=False, com_momentum=False, gate_min=75):
    """{chave(home,away): {min,status,injury,placar,event_id[,forca][,mom]}}.
    com_forca/com_momentum: anexa forca-time e/ou momentum SO nos jogos com
    minuto>=gate_min (limita chamadas extras; forca cache 6h, momentum 25s)."""
    eventos, status = eventos_ao_vivo()
    agora = time.time()
    cross = {}
    for ev in eventos:
        entry = {
            "min": ev["min"], "status": ev["status"],
            "injury": ev["injury"], "placar": ev["placar"],
            "event_id": ev["event_id"],
        }
        if ev["min"] is not None and ev["min"] >= gate_min:
            if com_forca:
                f = forca_confronto(ev.get("casa_id"), ev.get("fora_id"), agora)
                if f:
                    entry["forca"] = f
            if com_momentum:
                m = momentum_confronto(ev.get("event_id"), agora)
                if m:
                    entry["mom"] = {"casa": m["casa"], "fora": m["fora"]}
                rv = vermelhos_confronto(ev.get("event_id"), agora)   # (A) fallback de cartao vermelho
                if rv and (rv["redC"] or rv["redF"]):
                    entry["redC"] = rv["redC"]; entry["redF"] = rv["redF"]
        cross[ev["casa_norm"] + "|" + ev["fora_norm"]] = entry
    return cross, status, len(eventos)


def logar_sinal(rec):
    """Grava um sinal GREEN do alertador na tabela sinal_log do futebol.db."""
    import sqlite3
    db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "futebol.db")
    con = sqlite3.connect(db)
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "schema.sql"), encoding="utf-8") as f:
        con.executescript(f.read())
    con.execute(
        "INSERT INTO sinal_log (liga,casa,fora,event_id,minuto,placar,resultado,"
        "prob,breakeven,odd_tela,acrescimo) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (rec.get("liga"), rec.get("casa"), rec.get("fora"), rec.get("event_id"),
         rec.get("minuto"), rec.get("placar"), rec.get("resultado"), rec.get("prob"),
         rec.get("breakeven"), rec.get("odd_tela"), rec.get("acrescimo")))
    con.commit(); con.close()


# ---------------------------------------------------------------- servidor
def servir(porta=8765, intervalo=12, com_forca=False, com_momentum=False, gate_min=75):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    cache = {"cross": {}, "ts": 0.0}

    def atualizar():
        if not com_momentum:        # sem --momentum: nao toca no SofaScore (IP bloqueado) — so /forca
            return
        if time.time() - cache["ts"] < intervalo:
            return
        try:
            cross, st, n = montar_cross(com_forca, com_momentum, gate_min)
            cache["cross"] = cross
            cache["ts"] = time.time()
            extra = []
            if com_forca:
                extra.append(f"{sum(1 for v in cross.values() if 'forca' in v)} forca")
            if com_momentum:
                extra.append(f"{sum(1 for v in cross.values() if 'mom' in v)} momentum")
            print(f"  cross atualizado: {n} jogos ao vivo (status {st})"
                  + (" | " + ", ".join(extra) if extra else ""), flush=True)
        except Exception as e:
            print(f"  ! erro ao atualizar: {e}", file=sys.stderr)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

        def do_OPTIONS(self):
            self.send_response(204); self._cors(); self.end_headers()

        def _json(self, obj):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors(); self.send_header("Cache-Control", "no-store"); self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            from urllib.parse import urlparse, parse_qs
            u = urlparse(self.path)
            if u.path.startswith("/forca"):       # /forca?casa=X&fora=Y -> forca FBref (cache local)
                import fbref_forca
                q = parse_qs(u.query)
                casa = (q.get("casa", [""])[0]); fora = (q.get("fora", [""])[0])
                return self._json(fbref_forca.forca_times(casa, fora) or {})
            if u.path.startswith("/correcao"):     # /correcao -> mapa de calibracao (loop E)
                p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibracao_correcao.json")
                try:
                    with open(p, encoding="utf-8") as f:
                        return self._json(json.load(f))
                except (OSError, ValueError):
                    return self._json({"metodo": "identidade"})
            atualizar()                            # senao: cross do SofaScore (momentum/min/status)
            self._json(cache["cross"])

        def do_POST(self):                       # /log : grava um sinal GREEN
            n = int(self.headers.get("Content-Length", 0))
            try:
                rec = json.loads(self.rfile.read(n) or b"{}")
                logar_sinal(rec)
                print(f"  + sinal logado: {rec.get('casa')} x {rec.get('fora')} "
                      f"{rec.get('resultado')} @{rec.get('odd_tela')}", flush=True)
                ok = True
            except Exception as e:
                print(f"  ! erro ao logar sinal: {e}", file=sys.stderr); ok = False
            self.send_response(200 if ok else 500); self._cors()
            self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps({"ok": ok}).encode())

    atualizar()
    srv = HTTPServer(("127.0.0.1", porta), H)
    print(f"Servindo cross em http://localhost:{porta}  (refresh {intervalo}s)")
    print("No console do bet365:  iniciarValor({ssUrl:'http://localhost:%d'})" % porta)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        transporte.fechar()


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("live")
    pc = sub.add_parser("casar"); pc.add_argument("home"); pc.add_argument("away")
    ps = sub.add_parser("servir")
    ps.add_argument("--porta", type=int, default=8765)
    ps.add_argument("--intervalo", type=int, default=12)
    ps.add_argument("--forca", action="store_true", help="anexa forca-time aos jogos no fim")
    ps.add_argument("--momentum", action="store_true", help="anexa pressao ao vivo (xG/finalizacoes/posse)")
    ps.add_argument("--gate-min", type=int, default=75, help="minuto a partir do qual busca forca/momentum")
    pf = sub.add_parser("forca"); pf.add_argument("casa_id", type=int); pf.add_argument("fora_id", type=int)
    pm = sub.add_parser("momento"); pm.add_argument("event_id", type=int)
    a = ap.parse_args()

    if a.cmd == "live":
        evs, st = eventos_ao_vivo()
        print(f"status {st} — {len(evs)} jogos ao vivo")
        for e in sorted(evs, key=lambda x: -(x["min"] or 0))[:25]:
            print(f"  {e['min']}' {e['status']:10} {e['placar']}  +{e['injury']}  "
                  f"{e['home']} x {e['away']}  ({e['liga']})")
        transporte.fechar()
    elif a.cmd == "casar":
        evs, st = eventos_ao_vivo()
        ev, sc = casar(a.home, a.away, evs)
        print(f"match (sim {sc}):", (f"{ev['home']} x {ev['away']} {ev['min']}' "
              f"{ev['status']} +{ev['injury']}") if ev else "NENHUM")
        transporte.fechar()
    elif a.cmd == "forca":
        print(forca_confronto(a.casa_id, a.fora_id, time.time()))
        transporte.fechar()
    elif a.cmd == "momento":
        print(momentum_confronto(a.event_id, time.time()))
        transporte.fechar()
    elif a.cmd == "servir":
        servir(a.porta, a.intervalo, a.forca, a.momentum, a.gate_min)


if __name__ == "__main__":
    main()
