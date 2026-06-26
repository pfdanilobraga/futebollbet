# -*- coding: utf-8 -*-
"""
Engenharia de features para o modelo de ML (Estágio 3).

Princípio central: ZERO VAZAMENTO (no leakage). As features de uma partida
são calculadas usando APENAS dados de partidas anteriores ao seu apito inicial.
A mesma função é usada no treino e na previsão, evitando train/serve skew.

Features produzidas (todas NULL-safe — viram NaN quando faltam dados):
  Forma / gols (sempre disponíveis a partir de `evento`):
    - ppg_casa_mando, ppg_fora_mando .... pontos/jogo no mando específico (temporada)
    - gf_casa, ga_casa, gf_fora, ga_fora . média de gols pró/contra recentes (mando)
    - ppg5_casa, ppg5_fora ............... pontos/jogo nos últimos 5 (qualquer mando)
    - forma5_casa, forma5_fora ........... saldo de gols nos últimos 5
    - descanso_casa, descanso_fora ....... dias desde o último jogo
  Confronto direto:
    - h2h_saldo .......................... (vitórias casa - vitórias fora) em jogos anteriores entre eles
  Mercado (quando há odds coletadas):
    - imp_casa, imp_empate, imp_fora ..... probabilidade implícita das odds (sem margem)
    - tem_odds ........................... 1 se há odds 1X2; 0 caso contrário (separa o
                                           regime sem-mercado, evitando que a imputação
                                           injete sinal de mercado falso)
  Elenco / xG (entram automaticamente quando `escalacao` / `chute` forem coletados):
    - nota_xi_casa, nota_xi_fora ......... nota média do XI titular (média histórica dos titulares)
    - xg_casa, xg_fora ................... xG médio criado nos últimos jogos (mando)
  Rating dinâmico (pi-ratings, Constantinou & Fenton 2013):
    - pi_casa ............................ rating de CASA do mandante (R_H)
    - pi_fora ............................ rating de FORA do visitante (R_A)
    - pi_exp_gd .......................... diferença de gols esperada do confronto
                                           (ajusta pela força do adversário — o que médias
                                           de 5 jogos ignoram; com retornos decrescentes na
                                           margem). NaN nos primeiros jogos de cada time.

Alvo (target): resultado 1X2 -> classe 'H' (casa), 'D' (empate), 'A' (fora).
"""
import math
import sqlite3
from collections import defaultdict

JANELA_RECENTE = 5          # nº de jogos para médias "recentes"

# pi-ratings (Constantinou & Fenton 2013) — parâmetros tunados da literatura
PI_LAMBDA = 0.035           # taxa de aprendizado do rating do mando direto
PI_GAMMA = 0.7              # fração do ajuste propagada ao rating do outro mando
PI_C = 3.0                  # escala da função de retornos decrescentes
PI_B = 10.0                 # base do log/exp
PI_MIN_JOGOS = 5            # cold-start: rating só "confiável" após N jogos do time


# ----------------------------------------------------------------- util odds
def _implicitas(con, evento_id):
    linhas = con.execute(
        """SELECT escolha, odd_decimal FROM odd
           WHERE evento_id=? AND mercado='Full time' AND parametro=''""",
        (evento_id,)).fetchall()
    odds = {e: o for e, o in linhas if o and o > 1}
    if not all(k in odds for k in ("1", "X", "2")):
        return (None, None, None)
    bruto = {k: 1.0 / odds[k] for k in ("1", "X", "2")}
    s = sum(bruto.values())
    return (bruto["1"] / s, bruto["X"] / s, bruto["2"] / s)


# ----------------------------------------------------------------- núcleo
class HistoricoLiga:
    """Mantém, em memória, o histórico de jogos já 'vistos' para cada time,
    permitindo calcular features incrementalmente em ordem cronológica."""

    def __init__(self, con):
        self.con = con
        # notas individuais por evento; só são CONSUMIDAS via registrar() em ordem
        # cronológica (anti-vazamento — a média histórica do jogador nunca enxerga
        # o jogo atual nem jogos futuros).
        self._notas_por_evento = self._carregar_notas_por_evento()
        # acumulador incremental: jogador_id -> [soma_notas, n_jogos] já vistos
        self._nota_acum = defaultdict(lambda: [0.0, 0])
        self._xg_evento = self._carregar_xg_evento()
        # por time -> lista de dicts {ts, mando, gf, ga, pts, adversario, evento_id}
        self.por_time = defaultdict(list)
        # pi-ratings: time_id -> [R_casa, R_fora, n_jogos] (atualizado em registrar)
        # chavear por time já separa as ligas — cada time só joga na própria.
        self._pi = defaultdict(lambda: [0.0, 0.0, 0])

    def _carregar_notas_por_evento(self):
        """evento_id -> [(jogador_id, nota), ...]. Carregar é leitura pura de dados;
        a garantia de zero-vazamento vem de só agregar essas notas em registrar(),
        que roda em ordem cronológica e depois de já calcular as features do jogo."""
        d = defaultdict(list)
        for eid, jid, nota in self.con.execute(
                "SELECT evento_id, jogador_id, nota FROM escalacao "
                "WHERE nota IS NOT NULL"):
            d[eid].append((jid, nota))
        return d

    def _carregar_xg_evento(self):
        """xG total por time em cada evento (a partir do shotmap)."""
        d = defaultdict(dict)  # evento_id -> {eh_casa: xg_total}
        for eid, eh_casa, xg in self.con.execute(
                "SELECT evento_id, eh_casa, SUM(xg) FROM chute "
                "WHERE xg IS NOT NULL GROUP BY evento_id, eh_casa"):
            d[eid][eh_casa] = xg
        return d

    # ---- agregados sobre o histórico já visto de um time ----
    @staticmethod
    def _ppg(jogos, mando=None):
        sel = [j for j in jogos if mando is None or j["mando"] == mando]
        if not sel:
            return None
        return sum(j["pts"] for j in sel) / len(sel)

    @staticmethod
    def _media(jogos, campo, mando=None, n=None):
        sel = [j for j in jogos if mando is None or j["mando"] == mando]
        if n:
            sel = sel[-n:]
        if not sel:
            return None
        return sum(j[campo] for j in sel) / len(sel)

    def _h2h_saldo(self, casa_id, fora_id, ts):
        """Vitórias do mandante - vitórias do visitante em confrontos anteriores."""
        c = f = 0
        for j in self.por_time[casa_id]:
            if j["adversario"] == fora_id and j["ts"] < ts:
                if j["pts"] == 3:
                    c += 1
                elif j["pts"] == 0:
                    f += 1
        return c - f

    def _nota_xi(self, evento_id, time_id):
        """Nota média histórica dos 11 titulares deste jogo, usando só a média
        acumulada de jogos ANTERIORES de cada jogador (sem vazamento)."""
        notas = []
        for (jid,) in self.con.execute(
                "SELECT jogador_id FROM escalacao "
                "WHERE evento_id=? AND time_id=? AND titular=1",
                (evento_id, time_id)):
            soma, n = self._nota_acum[jid]
            if n:
                notas.append(soma / n)
        return sum(notas) / len(notas) if notas else None

    @staticmethod
    def _pi_gd(R):
        """Converte um rating pi em diferença de gols esperada (inverso de psi),
        com retornos decrescentes: gd = sign(R)*(b^(|R|/c) - 1)."""
        g = PI_B ** (abs(R) / PI_C) - 1.0
        return g if R >= 0 else -g

    def _pi_features(self, casa, fora):
        """(pi_casa, pi_fora, pi_exp_gd) ou (None,None,None) em cold-start."""
        rc, rf = self._pi[casa], self._pi[fora]
        if rc[2] < PI_MIN_JOGOS or rf[2] < PI_MIN_JOGOS:
            return None, None, None
        exp_gd = self._pi_gd(rc[0]) - self._pi_gd(rf[1])
        return rc[0], rf[1], exp_gd

    def _pi_atualizar(self, casa, fora, gc, gf):
        """Atualiza os pi-ratings após uma partida (chamado em registrar)."""
        rc, rf = self._pi[casa], self._pi[fora]
        ghat = self._pi_gd(rc[0]) - self._pi_gd(rf[1])     # gd esperado (casa-fora)
        e = (gc - gf) - ghat                                # erro (perspectiva da casa)
        passo = PI_LAMBDA * PI_C * math.log10(1 + abs(e)) * (1 if e >= 0 else -1)
        rc0, rf1 = rc[0], rf[1]
        rc[0] = rc0 + passo                                 # R_casa do mandante
        rc[1] = rc[1] + PI_GAMMA * (rc[0] - rc0)            # propaga ao R_fora dele
        rf[1] = rf1 - passo                                 # R_fora do visitante
        rf[0] = rf[0] + PI_GAMMA * (rf[1] - rf1)            # propaga ao R_casa dele
        rc[2] += 1
        rf[2] += 1

    def _xg_recente(self, time_id, mando):
        """xG médio criado nos últimos jogos no mando (precisa de chute coletado)."""
        vals = []
        for j in self.por_time[time_id][-JANELA_RECENTE * 2:]:
            if j["mando"] != mando:
                continue
            xg = self._xg_evento.get(j["evento_id"], {}).get(
                1 if mando == "casa" else 0)
            if xg is not None:
                vals.append(xg)
        return sum(vals) / len(vals) if vals else None

    # ---- features de UMA partida (usando só o que já foi visto) ----
    def features(self, ev):
        casa, fora, ts = ev["casa_id"], ev["fora_id"], ev["inicio_ts"]
        hc, hf = self.por_time[casa], self.por_time[fora]
        ult_casa = hc[-1]["ts"] if hc else None
        ult_fora = hf[-1]["ts"] if hf else None
        imp = _implicitas(self.con, ev["id"])
        pi_casa, pi_fora, pi_exp_gd = self._pi_features(casa, fora)
        return {
            "ppg_casa_mando":  self._ppg(hc, "casa"),
            "ppg_fora_mando":  self._ppg(hf, "fora"),
            "gf_casa":  self._media(hc, "gf", "casa", JANELA_RECENTE),
            "ga_casa":  self._media(hc, "ga", "casa", JANELA_RECENTE),
            "gf_fora":  self._media(hf, "gf", "fora", JANELA_RECENTE),
            "ga_fora":  self._media(hf, "ga", "fora", JANELA_RECENTE),
            "ppg5_casa": self._ppg(hc[-JANELA_RECENTE:]),
            "ppg5_fora": self._ppg(hf[-JANELA_RECENTE:]),
            "forma5_casa": self._media(hc[-JANELA_RECENTE:], "saldo"),
            "forma5_fora": self._media(hf[-JANELA_RECENTE:], "saldo"),
            "descanso_casa": (ts - ult_casa) / 86400 if ult_casa else None,
            "descanso_fora": (ts - ult_fora) / 86400 if ult_fora else None,
            "h2h_saldo": self._h2h_saldo(casa, fora, ts),
            "imp_casa": imp[0], "imp_empate": imp[1], "imp_fora": imp[2],
            "tem_odds": 1.0 if imp[0] is not None else 0.0,
            "nota_xi_casa": self._nota_xi(ev["id"], casa),
            "nota_xi_fora": self._nota_xi(ev["id"], fora),
            "xg_casa": self._xg_recente(casa, "casa"),
            "xg_fora": self._xg_recente(fora, "fora"),
            "pi_casa": pi_casa, "pi_fora": pi_fora, "pi_exp_gd": pi_exp_gd,
        }

    # ---- registra o RESULTADO de uma partida no histórico ----
    def registrar(self, ev):
        gc, gf = ev["gols_casa"], ev["gols_fora"]
        if gc is None or gf is None:
            return
        pts_casa = 3 if gc > gf else (1 if gc == gf else 0)
        pts_fora = 3 if gf > gc else (1 if gc == gf else 0)
        self.por_time[ev["casa_id"]].append(dict(
            ts=ev["inicio_ts"], mando="casa", gf=gc, ga=gf, saldo=gc - gf,
            pts=pts_casa, adversario=ev["fora_id"], evento_id=ev["id"]))
        self.por_time[ev["fora_id"]].append(dict(
            ts=ev["inicio_ts"], mando="fora", gf=gf, ga=gc, saldo=gf - gc,
            pts=pts_fora, adversario=ev["casa_id"], evento_id=ev["id"]))
        # acumula as notas individuais DESTE jogo — passam a contar só para os
        # PRÓXIMOS jogos de cada jogador (entra depois de features() ter rodado)
        for jid, nota in self._notas_por_evento.get(ev["id"], ()):
            self._nota_acum[jid][0] += nota
            self._nota_acum[jid][1] += 1
        # atualiza pi-ratings com o resultado deste jogo (também pós-features)
        self._pi_atualizar(ev["casa_id"], ev["fora_id"], gc, gf)


COLUNAS = [
    "ppg_casa_mando", "ppg_fora_mando", "gf_casa", "ga_casa", "gf_fora", "ga_fora",
    "ppg5_casa", "ppg5_fora", "forma5_casa", "forma5_fora",
    "descanso_casa", "descanso_fora", "h2h_saldo",
    "imp_casa", "imp_empate", "imp_fora", "tem_odds",
    "nota_xi_casa", "nota_xi_fora", "xg_casa", "xg_fora",
    "pi_casa", "pi_fora", "pi_exp_gd",
]


def _resultado_classe(ev):
    gc, gf = ev["gols_casa"], ev["gols_fora"]
    if gc is None or gf is None:
        return None
    return "H" if gc > gf else ("D" if gc == gf else "A")


def construir_dataset(con):
    """Percorre TODOS os jogos finalizados em ordem cronológica, calcula as
    features de cada um (com o histórico até então) e devolve uma lista de
    dicts: features + alvo + metadados. Pronto para virar DataFrame."""
    eventos = [dict(r) for r in con.execute(
        """SELECT id, casa_id, fora_id, inicio_ts, temporada_id, rodada,
                  gols_casa, gols_fora
           FROM evento WHERE status='finished' AND gols_casa IS NOT NULL
           ORDER BY inicio_ts, id""")]
    hist = HistoricoLiga(con)
    linhas = []
    for ev in eventos:
        feat = hist.features(ev)        # ANTES de registrar (sem vazamento)
        feat["alvo"] = _resultado_classe(ev)
        feat["evento_id"] = ev["id"]
        feat["inicio_ts"] = ev["inicio_ts"]
        feat["temporada_id"] = ev["temporada_id"]
        linhas.append(feat)
        hist.registrar(ev)              # agora o resultado entra no histórico
    return linhas


def features_para_evento(con, evento_id):
    """Calcula as features de UM evento (tipicamente futuro/não iniciado),
    usando todo o histórico finalizado disponível no banco."""
    ev = con.execute(
        """SELECT id, casa_id, fora_id, inicio_ts FROM evento WHERE id=?""",
        (evento_id,)).fetchone()
    if not ev:
        return None
    ev = dict(zip(["id", "casa_id", "fora_id", "inicio_ts"], ev))
    hist = HistoricoLiga(con)
    for r in con.execute(
            """SELECT id, casa_id, fora_id, inicio_ts, gols_casa, gols_fora
               FROM evento
               WHERE status='finished' AND gols_casa IS NOT NULL
                 AND inicio_ts < ? ORDER BY inicio_ts, id""",
            (ev["inicio_ts"],)):
        hist.registrar(dict(zip(
            ["id", "casa_id", "fora_id", "inicio_ts", "gols_casa", "gols_fora"], r)))
    return hist.features(ev)


def features_para_eventos(con, evento_ids):
    """Versão em LOTE: calcula features de vários eventos (futuros) construindo o
    histórico UMA única vez (vs. uma vez por evento). Como os jogos não iniciados
    estão todos após os finalizados, um único histórico "até agora" serve a todos.
    Retorna {evento_id: feat}. Crucial p/ performance com banco grande (136k+)."""
    if not evento_ids:
        return {}
    hist = HistoricoLiga(con)
    for r in con.execute(
            """SELECT id, casa_id, fora_id, inicio_ts, gols_casa, gols_fora
               FROM evento WHERE status='finished' AND gols_casa IS NOT NULL
               ORDER BY inicio_ts, id"""):
        hist.registrar(dict(zip(
            ["id", "casa_id", "fora_id", "inicio_ts", "gols_casa", "gols_fora"], r)))
    out = {}
    for chunk_ini in range(0, len(evento_ids), 500):     # IN (...) em lotes
        chunk = evento_ids[chunk_ini:chunk_ini + 500]
        qs = ",".join("?" * len(chunk))
        for ev in con.execute(
                f"SELECT id, casa_id, fora_id, inicio_ts FROM evento WHERE id IN ({qs})",
                tuple(chunk)):
            d = dict(zip(["id", "casa_id", "fora_id", "inicio_ts"], ev))
            out[d["id"]] = hist.features(d)
    return out


if __name__ == "__main__":
    # diagnóstico rápido: quantos jogos e cobertura de cada feature
    import os
    con = sqlite3.connect(os.path.join(os.path.dirname(__file__), "futebol.db"))
    con.row_factory = sqlite3.Row
    ds = construir_dataset(con)
    print(f"{len(ds)} jogos finalizados no dataset.")
    if ds:
        n = len(ds)
        print("\nCobertura por feature (% de jogos com valor):")
        for col in COLUNAS:
            preenchidos = sum(1 for r in ds if r.get(col) is not None)
            print(f"  {col:<16} {preenchidos/n:6.1%}")
        from collections import Counter
        print("\nDistribuição do alvo:", dict(Counter(r["alvo"] for r in ds)))
