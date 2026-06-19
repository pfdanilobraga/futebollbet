# -*- coding: utf-8 -*-
"""
Funções puras de gravação: recebem o JSON cru da API/HTML do SofaScore e
escrevem no futebol.db. Compartilhadas pelo `coletor.py` (via /api/) e pelo
`raspador.py` (via navegador). Nenhuma faz rede — só transformam JSON -> banco.

Toda função aceita o JSON podendo ser None (degrada sem erro) e é idempotente.
"""


# ----------------------------------------------------------------- util
def odd_decimal(frac):
    """'19/25' -> 1.76 ; None/inválido -> None"""
    if not frac:
        return None
    try:
        num, den = frac.split("/")
        return round(int(num) / int(den) + 1, 4)
    except Exception:
        return None


def upsert_time(con, t):
    if not t or not t.get("id"):
        return None
    con.execute(
        "INSERT INTO time (id, nome, nome_curto, slug, pais) VALUES (?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET nome=excluded.nome",
        (t["id"], t.get("name"), t.get("shortName"), t.get("slug"),
         (t.get("country") or {}).get("name")))
    return t["id"]


# ----------------------------------------------------------------- evento
def gravar_evento(con, ev):
    """Insere/atualiza um evento (do /api/v1/event/{id} ou de listas)."""
    if not ev or not ev.get("id"):
        return None
    ut = (ev.get("tournament") or {}).get("uniqueTournament") or {}
    if ut.get("id"):
        con.execute(
            "INSERT INTO torneio (id, nome, slug, pais) VALUES (?,?,?,?) "
            "ON CONFLICT(id) DO NOTHING",
            (ut["id"], ut.get("name"), ut.get("slug"),
             (ut.get("category") or {}).get("name")))
    se = ev.get("season") or {}
    if se.get("id") and ut.get("id"):
        con.execute(
            "INSERT INTO temporada (id, torneio_id, nome, ano) VALUES (?,?,?,?) "
            "ON CONFLICT(id) DO NOTHING",
            (se["id"], ut["id"], se.get("name"), se.get("year")))
    casa = upsert_time(con, ev.get("homeTeam"))
    fora = upsert_time(con, ev.get("awayTeam"))
    hs, as_ = ev.get("homeScore") or {}, ev.get("awayScore") or {}
    con.execute(
        """INSERT INTO evento (id, custom_id, temporada_id, torneio_id, rodada,
               casa_id, fora_id, inicio_ts, status, vencedor,
               gols_casa, gols_fora, gols_casa_1t, gols_fora_1t, tem_xg,
               arbitro, estadio, atualizado_em)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
           ON CONFLICT(id) DO UPDATE SET
               status=excluded.status, vencedor=excluded.vencedor,
               gols_casa=excluded.gols_casa, gols_fora=excluded.gols_fora,
               gols_casa_1t=excluded.gols_casa_1t, gols_fora_1t=excluded.gols_fora_1t,
               tem_xg=excluded.tem_xg, arbitro=excluded.arbitro,
               estadio=excluded.estadio, atualizado_em=datetime('now')""",
        (ev["id"], ev.get("customId"), se.get("id"), ut.get("id"),
         (ev.get("roundInfo") or {}).get("round"), casa, fora,
         ev.get("startTimestamp"), (ev.get("status") or {}).get("type"),
         ev.get("winnerCode"), hs.get("current"), as_.get("current"),
         hs.get("period1"), as_.get("period1"),
         1 if ev.get("hasXg") else 0,
         (ev.get("referee") or {}).get("name"),
         (ev.get("venue") or {}).get("name") or
         ((ev.get("venue") or {}).get("stadium") or {}).get("name")))
    return ev["id"]


# ----------------------------------------------------------------- odds
def gravar_odds(con, eid, od):
    if not od:
        return 0
    n = 0
    for m in od.get("markets", []):
        param = str(m.get("choiceGroup") or "")
        for c in m.get("choices", []):
            dec = odd_decimal(c.get("fractionalValue"))
            if dec is None:
                continue
            con.execute(
                """INSERT INTO odd (evento_id, mercado, parametro, escolha,
                       odd_decimal, odd_abertura, coletado_em)
                   VALUES (?,?,?,?,?,?,datetime('now'))
                   ON CONFLICT(evento_id, mercado, parametro, escolha)
                   DO UPDATE SET odd_decimal=excluded.odd_decimal,
                                 coletado_em=datetime('now')""",
                (eid, m.get("marketName"), param, c.get("name"),
                 dec, odd_decimal(c.get("initialFractionalValue"))))
            n += 1
    return n


# ----------------------------------------------------------------- pré-jogo
def gravar_prejogo(con, eid, pregame_form=None, h2h=None, votes=None):
    ph = (pregame_form or {}).get("homeTeam") or {}
    pa = (pregame_form or {}).get("awayTeam") or {}
    duel = (h2h or {}).get("teamDuel") or {}
    voto = (votes or {}).get("vote") or {}
    con.execute(
        """INSERT INTO pre_jogo VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(evento_id) DO UPDATE SET
               casa_forma=excluded.casa_forma, fora_forma=excluded.fora_forma,
               casa_nota_media=excluded.casa_nota_media,
               fora_nota_media=excluded.fora_nota_media,
               casa_posicao=excluded.casa_posicao, fora_posicao=excluded.fora_posicao,
               h2h_casa_v=excluded.h2h_casa_v, h2h_fora_v=excluded.h2h_fora_v,
               h2h_empates=excluded.h2h_empates,
               votos_casa=excluded.votos_casa, votos_empate=excluded.votos_empate,
               votos_fora=excluded.votos_fora""",
        (eid, ",".join(ph.get("form") or []), ",".join(pa.get("form") or []),
         ph.get("avgRating"), pa.get("avgRating"),
         ph.get("position"), pa.get("position"),
         duel.get("homeWins"), duel.get("awayWins"), duel.get("draws"),
         voto.get("vote1"), voto.get("voteX"), voto.get("vote2")))


# ----------------------------------------------------------------- estatísticas
def gravar_estatisticas(con, eid, st):
    if not st:
        return 0
    n = 0
    for per in st.get("statistics", []):
        for g in per.get("groups", []):
            for item in g.get("statisticsItems", []):
                con.execute(
                    "INSERT OR REPLACE INTO evento_estatistica VALUES (?,?,?,?,?,?,?,?)",
                    (eid, per.get("period"), g.get("groupName"), item.get("name"),
                     item.get("homeValue"), item.get("awayValue"),
                     str(item.get("home")), str(item.get("away"))))
                n += 1
    return n


# ----------------------------------------------------------------- shotmap
def gravar_shotmap(con, eid, sm):
    if not sm:
        return 0
    n = 0
    for s in sm.get("shotmap", []):
        p = s.get("player") or {}
        if p.get("id"):
            con.execute(
                "INSERT INTO jogador (id, nome, posicao) VALUES (?,?,?) "
                "ON CONFLICT(id) DO NOTHING",
                (p["id"], p.get("name"), p.get("position")))
        pc = s.get("playerCoordinates") or {}
        con.execute(
            "INSERT OR REPLACE INTO chute VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (s.get("id"), eid, p.get("id"), 1 if s.get("isHome") else 0,
             s.get("time"), s.get("shotType"), s.get("situation"),
             s.get("bodyPart"), s.get("xg"), s.get("xgot"),
             pc.get("x"), pc.get("y")))
        n += 1
    return n


# ----------------------------------------------------------------- escalações
def gravar_lineups(con, eid, casa_id, fora_id, ln):
    if not ln:
        return 0
    n = 0
    for lado, time_id in (("home", casa_id), ("away", fora_id)):
        bloco = ln.get(lado) or {}
        notas = []
        for pl in bloco.get("players", []):
            p = pl.get("player") or {}
            stt = pl.get("statistics") or {}
            if not p.get("id"):
                continue
            con.execute(
                "INSERT INTO jogador (id, nome, posicao, data_nasc) "
                "VALUES (?,?,?,?) ON CONFLICT(id) DO NOTHING",
                (p["id"], p.get("name"), p.get("position"),
                 p.get("dateOfBirthTimestamp")))
            if stt.get("rating"):
                notas.append(stt["rating"])
            con.execute(
                "INSERT OR REPLACE INTO escalacao VALUES (?,?,?,?,?,?,?,?)",
                (eid, p["id"], time_id, 0 if pl.get("substitute") else 1,
                 pl.get("position"), stt.get("rating"),
                 stt.get("minutesPlayed"), stt.get("expectedAssists")))
            n += 1
        con.execute(
            "INSERT OR REPLACE INTO evento_formacao VALUES (?,?,?,?)",
            (eid, time_id, bloco.get("formation"),
             round(sum(notas) / len(notas), 2) if notas else None))
    return n


# ----------------------------------------------------------------- incidentes
def gravar_incidentes(con, eid, inc):
    """`inc` pode ser o dict {incidents:[...]} ou já a lista de incidentes."""
    if not inc:
        return 0
    lista = inc.get("incidents") if isinstance(inc, dict) else inc
    if not lista:
        return 0
    con.execute("DELETE FROM incidente WHERE evento_id=?", (eid,))
    n = 0
    for i, it in enumerate(lista):
        p = it.get("player") or {}
        # Para incidentes 'injuryTime' o acréscimo ANUNCIADO vem em `length`
        # (não em addedTime). Guardamos em `acrescimo` — é o dado que faltava p/
        # calibrar a PMF "extra além do anunciado" do modelo ao vivo (prob_aovivo).
        acr = it.get("length") if it.get("incidentType") == "injuryTime" else it.get("addedTime")
        con.execute(
            "INSERT OR REPLACE INTO incidente VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, i, it.get("time"), acr,
             it.get("incidentType"), it.get("incidentClass"),
             1 if it.get("isHome") else (0 if it.get("isHome") is False else None),
             p.get("name") or it.get("playerName"), p.get("id"),
             (it.get("assist1") or {}).get("name"),
             it.get("homeScore"), it.get("awayScore")))
        n += 1
    return n


# ----------------------------------------------------------------- classificação
def gravar_standings(con, temporada_id, st):
    if not st:
        return 0
    n = 0
    for grupo in st.get("standings", []):
        for r in grupo.get("rows", []):
            upsert_time(con, r.get("team"))
            if not (r.get("team") or {}).get("id"):
                continue
            con.execute(
                """INSERT INTO classificacao (temporada_id, time_id, tipo, posicao,
                       jogos, vitorias, empates, derrotas, gols_pro, gols_contra,
                       pontos, coletado_em)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
                   ON CONFLICT(temporada_id, time_id, tipo) DO UPDATE SET
                       posicao=excluded.posicao, jogos=excluded.jogos,
                       vitorias=excluded.vitorias, empates=excluded.empates,
                       derrotas=excluded.derrotas, gols_pro=excluded.gols_pro,
                       gols_contra=excluded.gols_contra, pontos=excluded.pontos,
                       coletado_em=datetime('now')""",
                (temporada_id, r["team"]["id"], "total", r.get("position"),
                 r.get("matches"), r.get("wins"), r.get("draws"), r.get("losses"),
                 r.get("scoresFor"), r.get("scoresAgainst"), r.get("points")))
            n += 1
    return n
