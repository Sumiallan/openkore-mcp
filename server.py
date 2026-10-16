#!/usr/bin/env python3
"""
OpenKore MCP - servidor local (transporte stdio), sem dependencias obrigatorias.

Tres camadas de conhecimento, todas locais:
  1. Wiki  (configuracao)   -> openkore.db   (criado por ingest.py)
  2. Codigo (comportamento) -> code.db       (criado por ingest_code.py)
  3. Semantica (opcional)   -> tabela 'vectors' dentro dos dois bancos
                               (criada por build_vectors.py)

Se os vetores existirem, a busca vira hibrida (lexical + semantica, fundidas por
RRF) e responde perguntas em portugues que nao compartilham vocabulario com a
wiki em ingles. Se nao existirem, tudo continua funcionando em modo lexical.

stdout e reservado para o protocolo JSON-RPC; todo log vai para stderr.

Uso:
    python server.py                        # modo servidor (o cliente inicia)
    python server.py --selftest "query"     # testa no terminal, sem MCP
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

SERVER_NAME = "openkore-mcp"
SERVER_VERSION = "2.0.0"
DEFAULT_PROTOCOL = "2025-06-18"
DEFAULT_MODEL = "minishlab/potion-multilingual-128M"
HERE = Path(__file__).resolve().parent

# Uma chave de configuracao de verdade: comeca com letra, so [A-Za-z0-9_]
KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,60}$")

RRF_K = 60
SEM_MIN_COS = 0.30   # similaridade minima para um resultado so-semantico entrar

try:  # numpy so e necessario para a busca semantica
    import numpy as _np
except ImportError:  # pragma: no cover
    _np = None


# --------------------------------------------------------------------------- #
# Normalizacao / utilitarios
# --------------------------------------------------------------------------- #
def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


def has_run(hay: list[str], needle: list[str]) -> bool:
    """True se 'needle' aparece como sequencia contigua de tokens em 'hay'."""
    n = len(needle)
    if n == 0 or n > len(hay):
        return False
    return any(hay[i : i + n] == needle for i in range(len(hay) - n + 1))


def rrf_fuse(rankings: list[list[int]], weights: list[float] | None = None) -> dict[int, float]:
    """Reciprocal Rank Fusion: funde varias ordenacoes em uma pontuacao unica."""
    scores: dict[int, float] = {}
    for idx, ranking in enumerate(rankings):
        w = weights[idx] if weights else 1.0
        for rank, key in enumerate(ranking):
            scores[key] = scores.get(key, 0.0) + w / (RRF_K + rank + 1)
    return scores


# --------------------------------------------------------------------------- #
# Busca semantica opcional
# --------------------------------------------------------------------------- #
class VectorStore:
    """Carrega vetores float32 de uma tabela 'vectors' e faz busca por cosseno."""

    def __init__(self, con: sqlite3.Connection, meta: dict) -> None:
        self.ok = False
        self.model_name = meta.get("vec_model") or DEFAULT_MODEL
        self.built_at = meta.get("vec_built_at")
        self._model = None
        if _np is None:
            return
        try:
            rows = list(con.execute("SELECT ref_id, vec FROM vectors ORDER BY ref_id"))
        except sqlite3.Error:
            return
        if not rows:
            return
        blobs = [r[1] for r in rows]
        dim = len(blobs[0]) // 4
        if dim <= 0 or any(len(b) // 4 != dim for b in blobs):
            return
        self.ids = [r[0] for r in rows]
        self.pos = {rid: i for i, rid in enumerate(self.ids)}
        self.mat = _np.frombuffer(b"".join(blobs), dtype="float32").reshape(len(blobs), dim)
        self.dim = dim
        self.ok = True

    def _embedder(self):
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(model_name=self.model_name)
        return self._model

    def _embed_query(self, text: str):
        """Vetoriza a consulta usando o prefixo do modelo (E5 espera 'query: ')."""
        model = self._embedder()
        fn = getattr(model, "query_embed", None)
        if fn is not None:
            try:
                return list(fn([text]))[0]
            except Exception:
                pass
        return list(model.embed([text]))[0]

    def search(self, text: str, k: int = 40) -> list[tuple[int, float]]:
        """Retorna [(posicao_no_indice, similaridade)] em ordem decrescente."""
        if not self.ok:
            return []
        vec = _np.asarray(self._embed_query(text), dtype="float32")
        n = float(_np.linalg.norm(vec))
        if n == 0:
            return []
        sims = self.mat @ (vec / n)
        k = max(1, min(k, len(sims)))
        top = _np.argpartition(-sims, k - 1)[:k]
        top = top[_np.argsort(-sims[top])]
        return [(int(i), float(sims[i])) for i in top]


# --------------------------------------------------------------------------- #
# Camada 1: wiki
# --------------------------------------------------------------------------- #
class Corpus:
    def __init__(self, db: Path) -> None:
        if not db.exists():
            raise FileNotFoundError(
                f"indice nao encontrado: {db}\n"
                "Rode primeiro:  python ingest.py\n"
                "ou, para construir as tres camadas de uma vez:  .\\setup.cmd"
            )
        con = sqlite3.connect(str(db))
        con.row_factory = sqlite3.Row

        self.pages: dict[str, dict] = {}
        self.norm_titles: dict[str, str] = {}
        for r in con.execute("SELECT title,url,revid,ts,cats,clean FROM pages"):
            self.pages[r["title"]] = dict(r)
            self.norm_titles.setdefault(norm(r["title"]), r["title"])

        self.chunks: list[dict] = []
        self.id_to_pos: dict[int, int] = {}
        for r in con.execute(
            "SELECT id,page,kind,section,entry_key,body,"
            "n_page,n_section,n_key,n_body FROM chunks"
        ):
            page = self.pages.get(r["page"], {})
            self.id_to_pos[r["id"]] = len(self.chunks)
            self.chunks.append(
                {
                    "id": r["id"],
                    "page": r["page"],
                    "kind": r["kind"],
                    "section": r["section"] or "",
                    "entry_key": r["entry_key"] or "",
                    "body": r["body"] or "",
                    "np": set((r["n_page"] or "").split()),
                    "ns": set((r["n_section"] or "").split()),
                    "nk": set((r["n_key"] or "").split()),
                    "nb": set((r["n_body"] or "").split()),
                    "nb_text": r["n_body"] or "",
                    "np_text": r["n_page"] or "",
                    "nk_text": r["n_key"] or "",
                    "url": page.get("url", ""),
                    "revid": page.get("revid"),
                }
            )
        self.n_chunks = len(self.chunks)
        self.df: Counter[str] = Counter()
        for c in self.chunks:
            for t in c["nb"]:
                self.df[t] += 1
        self.meta = {r["k"]: r["v"] for r in con.execute("SELECT k,v FROM meta")}
        self.vectors = VectorStore(con, self.meta)
        con.close()
        self.wiki_keys = self._collect_keys()

    def _collect_keys(self) -> dict[str, str]:
        """Chaves documentadas na wiki, normalizadas para minusculas.

        A wiki usa o mesmo formato '; item' para coisas que nao sao chaves de
        configuracao: sintaxe do EventMacro (&rand, @cart), flags de linha de
        comando (--help) e diretivas (!include). Sem filtrar, a comparacao de
        cobertura virava lixo.
        """
        out: dict[str, str] = {}
        for c in self.chunks:
            if c["kind"] != "entry" or not c["entry_key"]:
                continue
            for part in c["entry_key"].split(" / "):
                k = re.sub(r"\s*[\(\[].*$", "", part).strip()
                if k and KEY_RE.match(k):
                    out.setdefault(k.lower(), k)
        return out

    # ------------------------------------------------------------- lexical --
    def idf(self, term: str) -> float:
        return math.log(1.0 + self.n_chunks / (1.0 + self.df.get(term, 0)))

    def _lexical(self, q: str, terms: list[str], kind: str) -> dict[int, tuple[float, int]]:
        """Retorna {posicao: (pontuacao, quantos termos da consulta o chunk cobre)}."""
        qtok = q.split()
        out: dict[int, tuple[float, int]] = {}
        for pos, c in enumerate(self.chunks):
            if kind == "entry" and c["kind"] != "entry":
                continue
            if kind == "section" and c["kind"] != "section":
                continue
            score = 0.0
            hits = 0
            for t in terms:
                w = 0.0
                if t in c["nk"]:
                    w += 6.0
                if t in c["np"]:
                    w += 3.0
                if t in c["ns"]:
                    w += 2.0
                if t in c["nb"]:
                    w += 1.0
                if w:
                    hits += 1
                    score += w * self.idf(t)
            if not hits:
                continue
            exact = False
            for cand in (c["np_text"], c["nk_text"]):
                if not cand:
                    continue
                ctok = cand.split()
                if q == cand:
                    score *= 3.0
                    exact = True
                    break
                if has_run(qtok, ctok):
                    score *= 1.0 + 0.8 * (len(ctok) / len(qtok))
                    break
                if has_run(ctok, qtok):
                    score *= 1.5
                    break
            if hits == len(terms):
                score *= 1.6
            if len(terms) > 1 and q in c["nb_text"]:
                score *= 1.4
            if c["kind"] == "entry":
                score *= 1.15
            out[pos] = (score, hits, exact)
        return out

    def search(self, query: str, limit: int = 8, kind: str = "both") -> dict:
        q = norm(query)
        terms = [t for t in q.split() if len(t) > 1 or t.isdigit()]
        if not terms:
            return {"query": query, "results": [], "count": 0, "note": "Empty query."}

        lex = self._lexical(q, terms, kind)
        lex_rank = sorted(lex, key=lambda p: -lex[p][0])
        # Peso adaptativo: uma pergunta em portugues ("meu bot fica parado sem atacar")
        # casa no lexico quase so por palavras comuns como "bot", gerando um ranking
        # fraco. Medimos a cobertura de termos do melhor hit e, se for baixa,
        # deixamos a semantica assumir. Numa consulta por identificador
        # ("teleportAuto_hp") a cobertura e 1.0 e o lexico continua mandando.
        top_cov = max((lex[p][1] for p in lex_rank[:10]), default=0)
        lex_weight = 0.4 + 0.6 * (top_cov / len(terms))

        sem_rank: list[int] = []
        sem_sim: dict[int, float] = {}
        if self.vectors.ok:
            for pos_in_vec, sim in self.vectors.search(query, k=60):
                rid = self.vectors.ids[pos_in_vec]
                pos = self.id_to_pos.get(rid)
                if pos is None:
                    continue
                c = self.chunks[pos]
                if kind == "entry" and c["kind"] != "entry":
                    continue
                if kind == "section" and c["kind"] != "section":
                    continue
                sem_rank.append(pos)
                sem_sim[pos] = sim

        if sem_rank:
            fused = rrf_fuse([lex_rank[:40], sem_rank], weights=[lex_weight, 1.0])
            order = sorted(fused, key=lambda p: -fused[p])
            mode = "hybrid"
        else:
            fused = {p: s for p, (s, _, _) in lex.items()}
            order = lex_rank
            mode = "lexical"

        # Exatidao vence consenso: o RRF premeia "aparece nas duas listas", o que
        # derrubava o acerto literal de um identificador (buscar 'ai_sync' devolvia a
        # pagina 'ai'). Casamento exato de titulo/chave e certeza, entao vai ao topo.
        pins = [p for p in lex_rank if lex[p][2]]
        pinned = set(pins)
        if pins:
            order = pins + [p for p in order if p not in pinned]

        results = []
        for pos in order:
            c = self.chunks[pos]
            in_lex = pos in lex
            in_sem = pos in sem_sim
            if not in_lex and in_sem and sem_sim[pos] < SEM_MIN_COS:
                continue
            matched = "both" if (in_lex and in_sem) else ("lexical" if in_lex else "semantic")
            item = {
                "page": c["page"],
                "section": c["section"] or None,
                "entry_key": c["entry_key"] or None,
                "kind": c["kind"],
                "snippet": self._snippet(c, terms),
                "url": c["url"],
                "revid": c["revid"],
                "matched_by": matched,
                "exact_match": pos in pinned,
            }
            if mode == "hybrid":
                item["score"] = round(fused[pos], 5)
                item["similarity"] = round(sem_sim.get(pos, 0.0), 3)
            else:
                item["score"] = round(fused[pos], 3)
            results.append(item)
            if len(results) >= max(1, min(limit, 50)):
                break

        return {
            "query": query,
            "mode": mode,
            "normalized_terms": terms,
            "count": len(results),
            "results": results,
            "note": None
            if results
            else (
                "No match in the wiki index. Tell the user the documentation does not "
                "cover this; do not invent configuration keys."
            ),
        }

    def _snippet(self, c: dict, terms: list[str], width: int = 700) -> str:
        raw = c["body"].strip()
        if len(raw) <= width:
            return raw
        low = c["nb_text"]
        pos = len(low)
        for t in terms:
            i = low.find(t)
            if i != -1:
                pos = min(pos, i)
        frac = pos / max(len(low), 1)
        start = max(0, int(frac * len(raw)) - width // 3)
        out = raw[start : start + width].strip()
        return ("..." if start > 0 else "") + out + (
            "..." if start + width < len(raw) else ""
        )

    # -------------------------------------------------------------- pagina --
    def _resolve_title(self, title: str) -> str | None:
        if title in self.pages:
            return title
        n = norm(title)
        if n in self.norm_titles:
            return self.norm_titles[n]
        for nt, orig in self.norm_titles.items():
            if n and (n in nt or nt in n):
                return orig
        return None

    def get_page(self, title: str) -> dict:
        real = self._resolve_title(title)
        if not real:
            return {
                "error": "page_not_found",
                "requested": title,
                "suggestions": self.list_pages(contains=title, limit=10)["pages"],
            }
        p = self.pages[real]
        return {
            "title": real,
            "url": p["url"],
            "revid": p["revid"],
            "last_edited": p["ts"],
            "categories": json.loads(p["cats"] or "[]"),
            "license": "CC BY-SA 3.0 - OpenKore Wiki",
            "chars": len(p["clean"] or ""),
            "text": p["clean"] or "",
        }

    def get_config_key(self, file: str = "", key: str = "") -> dict:
        nf = norm(file)
        ftokens = [t for t in nf.split() if t]
        ktokens = [t for t in norm(key).split() if t]
        if not ktokens and not ftokens:
            return {"error": "informe 'key' (e opcionalmente 'file')"}

        exact: list[dict] = []
        loose: list[dict] = []
        for c in self.chunks:
            if c["kind"] != "entry":
                continue
            if ftokens and not all(t in c["np"] for t in ftokens):
                continue
            if ktokens and not all(t in c["nk"] for t in ktokens):
                continue
            item = {
                "page": c["page"],
                "entry_key": c["entry_key"],
                "description": c["body"],
                "section": c["section"] or None,
                "url": c["url"],
                "revid": c["revid"],
            }
            (exact if set(ktokens) == c["nk"] else loose).append(item)

        order = lambda lst: sorted(lst, key=lambda x: (len(x["entry_key"]), x["entry_key"]))
        out = {
            "file": file or None,
            "key": key or None,
            "exact_matches": order(exact)[:10],
            "partial_matches": order(loose)[:10],
            "url": (order(exact) or order(loose) or [{}])[0].get("url"),
        }
        if not out["exact_matches"] and not out["partial_matches"]:
            out["fallback_search"] = self.search(f"{file} {key}".strip(), limit=5)["results"]
            out["note"] = (
                "No exact key found. The fallback_search results are page-level context; "
                "do not present them as a confirmed key name."
            )
        return out

    def list_pages(self, category: str = "", contains: str = "", limit: int = 200) -> dict:
        nc = norm(category)
        nt = norm(contains)
        out = []
        for title, p in sorted(self.pages.items()):
            if nc and nc not in norm(p["cats"] or ""):
                continue
            if nt and nt not in norm(title):
                continue
            out.append(title)
            if len(out) >= max(1, min(limit, 1000)):
                break
        return {"count": len(out), "pages": out}

    def stats(self) -> dict:
        cats: Counter[str] = Counter()
        for p in self.pages.values():
            for c in json.loads(p["cats"] or "[]"):
                cats[c] += 1
        entries = sum(1 for c in self.chunks if c["kind"] == "entry")
        indexed = self.meta.get("fetched_at")
        age_days, stale = None, False
        if indexed:
            try:
                age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(indexed)).days
                stale = age_days > 30
            except ValueError:
                pass
        return {
            "source": "https://openkore.com/wiki/  (MediaWiki, CC BY-SA 3.0)",
            "indexed_at": indexed,
            "age_days": age_days,
            "stale": stale,
            "stale_note": (
                f"Wiki snapshot is {age_days} days old; the live wiki may be newer. "
                "Say so when the answer depends on very recent changes."
            )
            if stale
            else None,
            "pages": len(self.pages),
            "chunks": self.n_chunks,
            "config_definition_chunks": entries,
            "section_chunks": self.n_chunks - entries,
            "top_categories": [c for c, _ in cats.most_common(15)],
            "semantic_search": self.vectors.ok,
            "semantic_model": self.vectors.model_name if self.vectors.ok else None,
        }


# --------------------------------------------------------------------------- #
# Camada 2: codigo Perl
# --------------------------------------------------------------------------- #
class CodeIndex:
    def __init__(self, db: Path) -> None:
        con = sqlite3.connect(str(db))
        con.row_factory = sqlite3.Row
        self.symbols: list[dict] = []
        self.id_to_pos: dict[int, int] = {}
        for r in con.execute(
            "SELECT id,kind,name,pkg,file,line,signature,body,n_name,n_body,n_file "
            "FROM symbols"
        ):
            self.id_to_pos[r["id"]] = len(self.symbols)
            self.symbols.append(
                {
                    "id": r["id"],
                    "kind": r["kind"],
                    "name": r["name"],
                    "pkg": r["pkg"] or "",
                    "file": r["file"],
                    "line": r["line"],
                    "signature": r["signature"] or "",
                    "body": r["body"] or "",
                    "nn": set((r["n_name"] or "").split()),
                    "nb": set((r["n_body"] or "").split()),
                    "nf": set((r["n_file"] or "").split()),
                    "n_name_text": r["n_name"] or "",
                }
            )
        self.refs: list[dict] = [
            dict(r)
            for r in con.execute(
                "SELECT kind,name,file,line,context FROM refs ORDER BY file,line"
            )
        ]
        self.meta = {r["k"]: r["v"] for r in con.execute("SELECT k,v FROM meta")}
        self.vectors = VectorStore(con, self.meta)
        con.close()
        self.n_sym = len(self.symbols)
        self.df: Counter[str] = Counter()
        for s in self.symbols:
            for t in s["nb"]:
                self.df[t] += 1
        self.code_keys = {
            r["name"]: r["name"] for r in self.refs if r["kind"] == "config"
        }

    def idf(self, term: str) -> float:
        return math.log(1.0 + self.n_sym / (1.0 + self.df.get(term, 0)))

    def _lexical(self, q: str, terms: list[str]) -> dict[int, tuple[float, int]]:
        """Retorna {posicao: (pontuacao, quantos termos da consulta o simbolo cobre)}."""
        qtok = q.split()
        out: dict[int, tuple[float, int]] = {}
        for pos, s in enumerate(self.symbols):
            score = 0.0
            hits = 0
            for t in terms:
                w = 0.0
                if t in s["nn"]:
                    w += 8.0
                if t in s["nf"]:
                    w += 2.0
                if t in s["nb"]:
                    w += 1.0
                if w:
                    hits += 1
                    score += w * self.idf(t)
            if not hits:
                continue
            exact = False
            if q == s["n_name_text"]:
                score *= 3.0
                exact = True
            elif has_run(qtok, s["n_name_text"].split()):
                score *= 1.6
            if hits == len(terms):
                score *= 1.5
            if s["kind"] == "sub":
                score *= 1.2
            out[pos] = (score, hits, exact)
        return out

    def search(self, query: str, limit: int = 10) -> dict:
        q = norm(query)
        terms = [t for t in q.split() if len(t) > 1 or t.isdigit()]
        if not terms:
            return {"query": query, "results": [], "count": 0, "note": "Empty query."}

        lex = self._lexical(q, terms)
        lex_rank = sorted(lex, key=lambda p: -lex[p][0])
        top_cov = max((lex[p][1] for p in lex_rank[:10]), default=0)
        lex_weight = 0.4 + 0.6 * (top_cov / len(terms))

        sem_rank: list[int] = []
        sem_sim: dict[int, float] = {}
        if self.vectors.ok:
            for pos_in_vec, sim in self.vectors.search(query, k=60):
                pos = self.id_to_pos.get(self.vectors.ids[pos_in_vec])
                if pos is None:
                    continue
                sem_rank.append(pos)
                sem_sim[pos] = sim

        if sem_rank:
            fused = rrf_fuse([lex_rank[:40], sem_rank], weights=[lex_weight, 1.0])
            order = sorted(fused, key=lambda p: -fused[p])
            mode = "hybrid"
        else:
            fused = {p: s for p, (s, _, _) in lex.items()}
            order = lex_rank
            mode = "lexical"

        pins = [p for p in lex_rank if lex[p][2]]
        pinned = set(pins)
        if pins:
            order = pins + [p for p in order if p not in pinned]

        results = []
        for pos in order:
            s = self.symbols[pos]
            in_lex, in_sem = pos in lex, pos in sem_sim
            if not in_lex and in_sem and sem_sim[pos] < SEM_MIN_COS:
                continue
            item = {
                "symbol": s["name"],
                "kind": s["kind"],
                "file": s["file"],
                "line": s["line"],
                "signature": s["signature"] or None,
                "snippet": (s["body"][:400].strip() or None),
                "matched_by": "both" if (in_lex and in_sem) else ("lexical" if in_lex else "semantic"),
                "exact_match": pos in pinned,
            }
            if mode == "hybrid":
                item["score"] = round(fused[pos], 5)
                item["similarity"] = round(sem_sim.get(pos, 0.0), 3)
            else:
                item["score"] = round(fused[pos], 3)
            results.append(item)
            if len(results) >= max(1, min(limit, 50)):
                break

        return {
            "query": query,
            "mode": mode,
            "repo": self.meta.get("source_root"),
            "commit": self.meta.get("git_rev") or None,
            "count": len(results),
            "results": results,
            "note": None
            if results
            else "No matching symbol. The feature may be named differently in the source.",
        }

    def get_symbol(self, name: str) -> dict:
        n = norm(name)
        exact = [s for s in self.symbols if s["name"].lower() == name.lower()]
        if exact:
            hits = exact
        else:
            hits = [s for s in self.symbols if n == s["n_name_text"]]
        if not hits:
            hits = [s for s in self.symbols if n and n in s["n_name_text"]]
        if not hits:
            return {
                "error": "symbol_not_found",
                "requested": name,
                "suggestions": [r["symbol"] for r in self.search(name, limit=8)["results"]],
            }
        out = []
        for s in hits[:6]:
            out.append(
                {
                    "symbol": s["name"],
                    "kind": s["kind"],
                    "file": s["file"],
                    "line": s["line"],
                    "signature": s["signature"] or None,
                    "body": s["body"],
                }
            )
        return {
            "requested": name,
            "count": len(out),
            "commit": self.meta.get("git_rev") or None,
            "symbols": out,
        }

    def find_in_source(self, name: str, kind: str = "") -> dict:
        """Onde uma chave de config (ou hook) e usada no codigo."""
        n = norm(name)
        matches = [
            r
            for r in self.refs
            if (not kind or r["kind"] == kind) and (n == norm(r["name"]) or n in norm(r["name"]))
        ]
        exact = [r for r in matches if r["name"].lower() == name.lower()]
        by_file: Counter[str] = Counter(r["file"] for r in exact or matches)
        return {
            "name": name,
            "kind": kind or "config|hook",
            "commit": self.meta.get("git_rev") or None,
            "count": len(exact or matches),
            "by_file": [{"file": f, "count": c} for f, c in by_file.most_common(12)],
            "occurrences": [
                {"kind": r["kind"], "name": r["name"], "file": r["file"],
                 "line": r["line"], "code": r["context"]}
                for r in (exact or matches)[:25]
            ],
            "note": None
            if (exact or matches)
            else (
                "This name is not referenced in the source. If the wiki documents it, it "
                "may be read dynamically, renamed, or not used at all."
            ),
        }

    def stats(self) -> dict:
        kinds = Counter(r["kind"] for r in self.refs)
        return {
            "source_root": self.meta.get("source_root"),
            "commit": self.meta.get("git_rev") or None,
            "indexed_at": self.meta.get("indexed_at"),
            "packages": sum(1 for s in self.symbols if s["kind"] == "package"),
            "subs": sum(1 for s in self.symbols if s["kind"] == "sub"),
            "config_usages": kinds.get("config", 0),
            "unique_config_keys": len(self.code_keys),
            "hooks": kinds.get("hook", 0),
            "semantic_search": self.vectors.ok,
        }


# --------------------------------------------------------------------------- #
# Camada 3: cobertura wiki x codigo
# --------------------------------------------------------------------------- #
def config_coverage(docs: Corpus, code: CodeIndex, key: str = "") -> dict:
    wiki = docs.wiki_keys
    src = {k.lower(): k for k in code.code_keys}
    out: dict = {
        "wiki_keys": len(wiki),
        "code_keys": len(src),
    }
    if key:
        k = key.lower()
        in_wiki = wiki.get(k)
        in_code = src.get(k)
        out["key"] = key
        out["in_wiki"] = bool(in_wiki)
        out["in_source"] = bool(in_code)
        if in_code:
            out["source_usages"] = code.find_in_source(in_code)["occurrences"][:10]
        if not in_wiki and not in_code:
            out["note"] = (
                "Not found in either layer. Do not invent it; ask the user to check the "
                "exact spelling."
            )
        elif in_code and not in_wiki:
            out["note"] = (
                "Exists in the source but is NOT documented in the wiki. Treat the source "
                "as the authority and cite file:line."
            )
        elif in_wiki and not in_code:
            out["note"] = (
                "Documented in the wiki but not referenced in the source snapshot. It may "
                "have been removed or renamed since the wiki page was written."
            )
        return out

    only_code = sorted(set(src) - set(wiki))
    only_wiki = sorted(set(wiki) - set(src))
    out["documented_but_not_in_source"] = [wiki[k] for k in only_wiki[:40]]
    out["documented_but_not_in_source_count"] = len(only_wiki)
    out["in_source_but_undocumented"] = [src[k] for k in only_code[:40]]
    out["in_source_but_undocumented_count"] = len(only_code)
    out["note"] = (
        "Gap analysis between the wiki snapshot and the source snapshot. Names are matched "
        "case-insensitively; a gap is a hint to investigate, not proof of absence."
    )
    return out


# --------------------------------------------------------------------------- #
# Ferramentas
# --------------------------------------------------------------------------- #
def _tools(code_available: bool, coverage_available: bool) -> list[dict]:
    tools = [
        {
            "name": "search_docs",
            "description": (
                "Search the official OpenKore wiki (configuration documentation). Use FIRST "
                "for questions about config.txt options, control files, console commands, "
                "plugins or serverType. Hybrid lexical+semantic search. Returns short "
                "snippets with url and revision - call get_page for the full page. If it "
                "returns nothing, say the wiki does not document the topic; never invent "
                "configuration keys."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keywords or a question, in any language."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    "kind": {"type": "string", "enum": ["entry", "section", "both"]},
                },
                "required": ["query"],
            },
        },
        {
            "name": "get_page",
            "description": "Return one full wiki page as cleaned text, with url and revision id.",
            "inputSchema": {
                "type": "object",
                "properties": {"title": {"type": "string", "description": "e.g. 'timeouts.txt'"}},
                "required": ["title"],
            },
        },
        {
            "name": "get_config_key",
            "description": (
                "Definition of one configuration key, optionally restricted to a control "
                "file: get_config_key(file='timeouts.txt', key='ai_sync')."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "key": {"type": "string"},
                },
                "required": ["key"],
            },
        },
        {
            "name": "list_pages",
            "description": "List wiki page titles, filtered by category or title substring.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "contains": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
            },
        },
        {
            "name": "docs_stats",
            "description": (
                "Coverage of both indexes: page/chunk counts, index age (stale flag), and "
                "whether semantic search is active. Use it to qualify an answer."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]
    if code_available:
        tools += [
            {
                "name": "search_code",
                "description": (
                    "Search the OpenKore Perl source (behaviour). Use this when the wiki is "
                    "silent, when a documented option does not match observed behaviour, or "
                    "for questions about how something works internally. Returns symbols "
                    "(packages and subs) with file:line - cite them."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "get_symbol",
                "description": "Full body of one Perl sub or package, with file and line.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "e.g. 'AI::Attack::shouldAttack' or 'shouldAttack'"}
                    },
                    "required": ["name"],
                },
            },
            {
                "name": "find_in_source",
                "description": (
                    "Where a configuration key or a plugin hook is actually used in the "
                    "source. Answers 'is this option really read by the code?'. "
                    "kind='config' (default) or 'hook'."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "e.g. 'teleportAuto_hp' or 'AI_start'"},
                        "kind": {"type": "string", "enum": ["config", "hook", ""]},
                    },
                    "required": ["name"],
                },
            },
        ]
    if coverage_available:
        tools.append(
            {
                "name": "config_coverage",
                "description": (
                    "Gap analysis between the wiki and the source: keys documented but absent "
                    "from the code, and keys used in the code but never documented. Call with "
                    "no arguments for the summary, or with a key to check that specific option. "
                    "This is how you tell the user what the wiki does NOT cover."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {"key": {"type": "string"}},
                },
            }
        )
    return tools


def call_tool(app: dict, name: str, args: dict) -> dict:
    docs: Corpus = app["docs"]
    code: CodeIndex | None = app["code"]

    if name == "search_docs":
        return docs.search(str(args.get("query", "")), int(args.get("limit", 8) or 8),
                           str(args.get("kind", "both") or "both"))
    if name == "get_page":
        return docs.get_page(str(args.get("title", "")))
    if name == "get_config_key":
        return docs.get_config_key(str(args.get("file", "") or ""), str(args.get("key", "") or ""))
    if name == "list_pages":
        return docs.list_pages(str(args.get("category", "") or ""), str(args.get("contains", "") or ""),
                               int(args.get("limit", 200) or 200))
    if name == "docs_stats":
        out = {"wiki": docs.stats()}
        if code:
            out["code"] = code.stats()
        return out
    if name == "search_code":
        if not code:
            return {"error": "code_index_missing",
                    "hint": "Rode: python ingest_code.py --src <clone do OpenKore>"}
        return code.search(str(args.get("query", "")), int(args.get("limit", 10) or 10))
    if name == "get_symbol":
        if not code:
            return {"error": "code_index_missing"}
        return code.get_symbol(str(args.get("name", "")))
    if name == "find_in_source":
        if not code:
            return {"error": "code_index_missing"}
        return code.find_in_source(str(args.get("name", "")), str(args.get("kind", "") or ""))
    if name == "config_coverage":
        if not code:
            return {"error": "code_index_missing"}
        return config_coverage(docs, code, str(args.get("key", "") or ""))
    raise KeyError(name)


# --------------------------------------------------------------------------- #
# Protocolo MCP (JSON-RPC 2.0 sobre stdio)
# --------------------------------------------------------------------------- #
def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def instructions(docs: Corpus, code: CodeIndex | None) -> str:
    parts = [
        "Local OpenKore knowledge base with two layers.",
        "WIKI (search_docs, get_page, get_config_key, list_pages): configuration "
        "documentation from openkore.com, CC BY-SA 3.0. Always cite the returned url.",
    ]
    if code:
        parts.append(
            "SOURCE (search_code, get_symbol, find_in_source): the Perl source at commit "
            f"{code.meta.get('git_rev') or '?'}. This is the authority on BEHAVIOUR - the "
            "wiki is often incomplete (many pages are stubs, some contain 'TODO'). Cite "
            "file:line."
        )
        parts.append(
            "config_coverage compares both layers, so you can tell the user what the wiki "
            "does not document."
        )
    if docs.vectors.ok:
        parts.append("Search is hybrid (lexical + multilingual semantic), so questions in "
                     "Portuguese work.")
    parts.append(
        "If both layers are silent, say the information is not available. Never invent "
        "configuration keys or sub names."
    )
    return " ".join(parts)


def handle(app: dict, msg: dict) -> dict | None:
    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    if mid is None:
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": instructions(app["docs"], app["code"]),
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "tools": _tools(app["code"] is not None, app["code"] is not None)
            },
        }
    if method == "tools/call":
        name = str(params.get("name", ""))
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        try:
            payload = call_tool(app, name, args)
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        except KeyError:
            result = {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}
        except Exception as e:
            result = {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"Method not found: {method}"}}


def serve(app: dict) -> int:
    docs = app["docs"]
    print(
        f"[{SERVER_NAME}] pronto: {len(docs.pages)} paginas wiki, {docs.n_chunks} chunks, "
        f"semantica={'on' if docs.vectors.ok else 'off'}"
        + (f", codigo={app['code'].n_sym} simbolos (commit {app['code'].meta.get('git_rev')})"
           if app["code"] else ", codigo=off"),
        file=sys.stderr,
    )
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            print(f"[{SERVER_NAME}] linha invalida ignorada", file=sys.stderr)
            continue
        if not isinstance(msg, dict):
            continue
        try:
            out = handle(app, msg)
        except Exception as e:
            out = {"jsonrpc": "2.0", "id": msg.get("id"),
                   "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}
        if out is not None:
            send(out)
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def selftest(app: dict, queries: list[str]) -> int:
    docs = app["docs"]
    print(json.dumps({"wiki": docs.stats(),
                      "code": app["code"].stats() if app["code"] else None},
                     ensure_ascii=False, indent=2))
    for q in queries:
        print(f"\n=== [wiki] {q} ===")
        r = docs.search(q, limit=3)
        print(f"    modo: {r.get('mode')}")
        for hit in r["results"]:
            label = hit["entry_key"] or hit["section"] or "-"
            print(f"  [{hit['score']:>8}] {hit['page']} :: {label}  ({hit['matched_by']})")
            print(f"             {hit['snippet'][:100]}")
        if not r["results"]:
            print(f"  (sem resultados)")
    if app["code"]:
        for q in queries[:3]:
            print(f"\n=== [codigo] {q} ===")
            for hit in app["code"].search(q, limit=3)["results"]:
                print(f"  [{hit['score']:>8}] {hit['symbol']}  {hit['file']}:{hit['line']}  ({hit['matched_by']})")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MCP server para o OpenKore (wiki + codigo).")
    ap.add_argument("--db", type=Path, default=HERE / "openkore.db")
    ap.add_argument("--code-db", type=Path, default=HERE / "code.db")
    ap.add_argument("--no-code", action="store_true", help="ignora o indice de codigo")
    ap.add_argument("--selftest", nargs="*", default=None)
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass

    try:
        docs = Corpus(args.db)
    except Exception as e:
        print(f"[{SERVER_NAME}] ERRO ao abrir a wiki: {e}", file=sys.stderr)
        return 1

    code: CodeIndex | None = None
    if not args.no_code and args.code_db.exists():
        try:
            code = CodeIndex(args.code_db)
        except Exception as e:
            print(f"[{SERVER_NAME}] aviso: code.db ilegivel ({e}); seguindo sem codigo",
                  file=sys.stderr)

    app = {"docs": docs, "code": code}

    if args.selftest is not None:
        return selftest(app, args.selftest or [
            "meu bot fica parado sem atacar",
            "teleport when hp is low",
            "ai_sync",
            "itemsTakeAuto",
        ])
    return serve(app)


if __name__ == "__main__":
    raise SystemExit(main())
