# -*- coding: utf-8 -*-
"""
Casamento de nomes de time entre fontes EXTERNAS (football-data.co.uk,
the-odds-api, ...) e a tabela `time` do futebol.db (origem SofaScore).

O risco nº 1 da reformulação multi-fonte é casar o time ERRADO (junta históricos
de clubes distintos que compartilham um token — ex.: "Atletico-MG" vs "Atlético
Goianiense", "Botafogo RJ" vs "Botafogo-SP"). Por isso a política é CONSERVADORA:

  1. `time_alias(fonte, nome_fonte) -> time_id` é a FONTE DA VERDADE (memoiza,
     idempotente entre execuções). Inclui seeds manuais.
  2. AUTO-LINK só em MATCH EXATO do nome normalizado (com desempate por país).
     Nada de fuzzy automático — fuzzy só gera SUGESTÃO no log p/ revisão humana.
  3. Sem match exato => cria time sintético com id NEGATIVO (SofaScore usa
     positivos => zero colisão). É a identidade correta para ligas que não
     raspamos; para ligas de overlap, semear o alias antes (semear()).

Fuzzy candidatos próximos (score >= SUGERIR) vão para mapeamento_excecoes.log
com o nome/id sugerido — para você confirmar e semear, NUNCA linkados sozinhos.
"""
import hashlib
import os
import re
import unicodedata

from rapidfuzz import fuzz, process

SUGERIR = 84          # >= => loga sugestão de match p/ revisão (NÃO linka)
PASTA = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(PASTA, "mapeamento_excecoes.log")

# sufixos/ruído de nome de clube removidos na normalização (NÃO inclui tokens
# discriminantes como mg/go/rj/sp — esses distinguem clubes homônimos).
_SUFIXOS = {
    "fc", "cf", "sc", "afc", "fk", "ec", "se", "if", "bk",
    "club", "calcio", "ssd", "ssc", "cp", "sv", "vfb", "vfl", "tsv", "fsv",
}


def _norm(s):
    """minúsculas, sem acento, sem pontuação, sem sufixos genéricos de clube."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    toks = [t for t in s.split() if t and t not in _SUFIXOS]
    return " ".join(toks) or re.sub(r"\s+", " ", s).strip()


def _id_sintetico(fonte, nome_fonte):
    """id NEGATIVO determinístico (estável entre execuções, != 0)."""
    h = hashlib.sha1(f"{fonte}|{_norm(nome_fonte)}".encode()).hexdigest()
    return -(int(h[:15], 16) & ((1 << 62) - 1)) - 1


def garantir_tabela(con):
    con.execute(
        """CREATE TABLE IF NOT EXISTS time_alias (
               fonte      TEXT NOT NULL,
               nome_fonte TEXT NOT NULL,
               time_id    INTEGER NOT NULL REFERENCES time(id),
               PRIMARY KEY (fonte, nome_fonte))""")


def semear(con, fonte, nome_fonte, time_id):
    """Insere/atualiza um alias confirmado manualmente (revisão das ligas de overlap)."""
    garantir_tabela(con)
    con.execute(
        "INSERT INTO time_alias (fonte, nome_fonte, time_id) VALUES (?,?,?) "
        "ON CONFLICT(fonte, nome_fonte) DO UPDATE SET time_id=excluded.time_id",
        (fonte, nome_fonte, time_id))
    con.commit()


class Matcher:
    """Resolve nomes de uma fonte externa -> time_id, com cache em memória.
    Instancie UMA vez por execução de coleta e reutilize (carrega candidatos só 1x)."""

    def __init__(self, con):
        self.con = con
        garantir_tabela(con)
        self.alias = {
            (f, n): tid for f, n, tid in
            con.execute("SELECT fonte, nome_fonte, time_id FROM time_alias")
        }
        self._norms = []                  # nomes normalizados (paralelo a _ids)
        self._ids = []
        self._nomes = []                  # nome original (p/ mensagem de sugestão)
        self._exato = {}                  # norm -> [(time_id, pais), ...]
        for tid, nome, curto, pais in con.execute(
                "SELECT id, nome, nome_curto, pais FROM time"):
            for nm in (nome, curto):
                self._add_candidato(nm, tid, pais)
        self._casados = self._criados = self._sugeridos = 0

    def _add_candidato(self, nome, tid, pais=None):
        n = _norm(nome)
        if not n:
            return
        self._norms.append(n)
        self._ids.append(tid)
        self._nomes.append(nome)
        self._exato.setdefault(n, []).append((tid, pais))

    def _log(self, msg):
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")

    def _match_exato(self, n, pais):
        cands = self._exato.get(n)
        if not cands:
            return None
        ids = {tid for tid, _ in cands}
        if len(ids) == 1:                 # nome+nome_curto do MESMO time: ok
            return next(iter(ids))
        if pais:                          # homônimos reais: desempata por país
            mesmos = {tid for tid, p in cands if p and _norm(p) == _norm(pais)}
            if len(mesmos) == 1:
                return next(iter(mesmos))
        self._log(f"[EXATO-AMBÍGUO] '{n}' casa {len(ids)} clubes distintos "
                  f"-> criando sintético por segurança")
        return None

    def resolver(self, fonte, nome_fonte, pais=None, criar=True):
        """Retorna time_id para (fonte, nome_fonte). Auto-link só em match EXATO."""
        if not nome_fonte:
            return None
        chave = (fonte, nome_fonte)
        if chave in self.alias:
            return self.alias[chave]

        n = _norm(nome_fonte)
        tid = self._match_exato(n, pais)
        if tid is not None:
            self._registrar(fonte, nome_fonte, tid)
            self._casados += 1
            return tid

        # fuzzy NUNCA linka — só sugere no log p/ revisão/seed manual
        if self._norms:
            achou = process.extractOne(n, self._norms, scorer=fuzz.token_sort_ratio)
            if achou and achou[1] >= SUGERIR:
                _, score, idx = achou
                self._sugeridos += 1
                self._log(f"[SUGESTÃO score={score:.0f}] {fonte}: '{nome_fonte}' ~ "
                          f"'{self._nomes[idx]}' (time_id {self._ids[idx]}). "
                          f"Se for o mesmo clube, semear: "
                          f"mapeamento_times.semear(con,'{fonte}','{nome_fonte}',{self._ids[idx]})")

        if not criar:
            return None
        novo = _id_sintetico(fonte, nome_fonte)
        self.con.execute(
            "INSERT INTO time (id, nome, pais) VALUES (?,?,?) "
            "ON CONFLICT(id) DO NOTHING", (novo, nome_fonte, pais))
        self._registrar(fonte, nome_fonte, novo)
        self._add_candidato(nome_fonte, novo, pais)   # casa repetições exatas na run
        self._criados += 1
        return novo

    def _registrar(self, fonte, nome_fonte, tid):
        self.con.execute(
            "INSERT INTO time_alias (fonte, nome_fonte, time_id) VALUES (?,?,?) "
            "ON CONFLICT(fonte, nome_fonte) DO UPDATE SET time_id=excluded.time_id",
            (fonte, nome_fonte, tid))
        self.alias[(fonte, nome_fonte)] = tid

    def resumo(self):
        return (f"casados(exato)={self._casados} criados(sintético)={self._criados} "
                f"sugestões(logadas p/ revisar)={self._sugeridos}")
