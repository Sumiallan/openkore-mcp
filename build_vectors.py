#!/usr/bin/env python3
"""
Gera embeddings multilingues para os indices e grava no proprio SQLite.

Isto e OPCIONAL: o server.py funciona sem isso (busca lexical). Com os vetores,
ele faz busca hibrida (lexical + semantica, fundidas por RRF), o que resolve
perguntas em portugues que nao compartilham vocabulario com a wiki em ingles
(ex.: "meu bot fica parado sem atacar" -> attackAuto / AI).

Dependencias (so para este script):
    pip install fastembed

Modelo padrao: minishlab/potion-multilingual-128M (256 dims, 0,5 GB, estatico).
Medido nesta maquina: 6,3 s para vetorizar os 9834 trechos, contra 4,5 min do
paraphrase-multilingual-MiniLM-L12-v2 e >70 min do intfloat/multilingual-e5-large.
Alem de 40x mais rapido, acertou melhor na camada de codigo. Para qualidade
maxima, --model Qwen/Qwen3-Embedding-0.6B-Q (1024 dims, ~31 min).

Uso:
    python build_vectors.py                    # vetoriza docs + codigo
    python build_vectors.py --only docs
    python build_vectors.py --check
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_MODEL = "minishlab/potion-multilingual-128M"

DDL = """
CREATE TABLE IF NOT EXISTS vectors(
  ref_id INTEGER PRIMARY KEY,
  vec    BLOB NOT NULL
);
"""


def load_fastembed(model_name: str):
    try:
        from fastembed import TextEmbedding
    except ImportError:
        print(
            "ERRO: fastembed nao instalado.\n"
            "      pip install fastembed\n"
            "(os vetores sao opcionais; sem eles o servidor usa busca lexical)",
            file=sys.stderr,
        )
        raise SystemExit(1)
    print(f"Carregando {model_name} (baixa na primeira vez)...", file=sys.stderr)
    return TextEmbedding(model_name=model_name)


def l2_normalize(mat):
    import numpy as np

    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (mat / norms).astype("float32")


def embed_passages(model, texts: list[str]) -> list:
    """Vetoriza passagens, aplicando o prefixo do modelo quando ele existe.

    Modelos E5 esperam 'passage: ' no indice e 'query: ' na consulta; usar
    embed() nos dois lados degrada bastante o resultado. fastembed expoe
    passage_embed/query_embed justamente para isso.
    """
    fn = getattr(model, "passage_embed", None)
    if fn is not None:
        try:
            return list(fn(texts, batch_size=64))
        except Exception as e:  # modelo sem suporte a prefixo
            print(f"  (passage_embed indisponivel: {e}; usando embed)", file=sys.stderr)
    return list(model.embed(texts, batch_size=64))


def embed_table(db: Path, model, model_name: str, sql: str, label: str,
                limit_chars: int = 1200) -> int:
    if not db.exists():
        print(f"  {label}: {db.name} nao existe, pulando", file=sys.stderr)
        return 0
    con = sqlite3.connect(str(db))
    con.executescript(DDL)
    rows = list(con.execute(sql))
    if not rows:
        print(f"  {label}: nada para vetorizar", file=sys.stderr)
        return 0

    ids = [r[0] for r in rows]
    texts = [(r[1] or "")[:limit_chars].replace("\n", " ").strip() for r in rows]

    t0 = time.time()
    vectors = embed_passages(model, texts)
    mat = l2_normalize(__import__("numpy").asarray(vectors, dtype="float32"))

    con.execute("DELETE FROM vectors")
    con.executemany(
        "INSERT INTO vectors(ref_id, vec) VALUES(?,?)",
        [(i, mat[k].tobytes()) for k, i in enumerate(ids)],
    )
    dim = int(mat.shape[1])
    for k, v in (
        ("vec_model", model_name),
        ("vec_dim", str(dim)),
        ("vec_count", str(len(ids))),
        ("vec_built_at", datetime.now(timezone.utc).isoformat(timespec="seconds")),
    ):
        con.execute(
            "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, v),
        )
    con.commit()
    con.close()
    print(
        f"  {label}: {len(ids)} vetores de {dim} dims em {time.time() - t0:.1f}s -> {db.name}",
        file=sys.stderr,
    )
    return len(ids)


DOCS_SQL = """
SELECT id, COALESCE(page,'') || ' | ' || COALESCE(section,'') || ' | ' ||
       COALESCE(entry_key,'') || ' | ' || COALESCE(body,'')
FROM chunks
"""
CODE_SQL = """
SELECT id, COALESCE(name,'') || ' | ' || COALESCE(signature,'') || ' | ' ||
       COALESCE(body,'')
FROM symbols WHERE kind = 'sub'
"""


def check(db: Path) -> int:
    if not db.exists():
        print(f"{db.name}: nao existe")
        return 0
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    meta = {r["k"]: r["v"] for r in con.execute("SELECT k,v FROM meta")}
    n = con.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
    print(f"{db.name}: {n} vetores | modelo={meta.get('vec_model', '-')} "
          f"dim={meta.get('vec_dim', '-')} em {meta.get('vec_built_at', '-')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Embeddings multilingues dos indices.")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--docs-db", type=Path, default=HERE / "openkore.db")
    ap.add_argument("--code-db", type=Path, default=HERE / "code.db")
    ap.add_argument("--only", choices=["docs", "code", "both"], default="both")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)

    if args.check:
        check(args.docs_db)
        check(args.code_db)
        return 0

    model = load_fastembed(args.model)
    if args.only in ("docs", "both"):
        embed_table(args.docs_db, model, args.model, DOCS_SQL, "docs")
    if args.only in ("code", "both"):
        embed_table(args.code_db, model, args.model, CODE_SQL, "codigo")
    print("Pronto. O servidor passa a usar busca hibrida automaticamente.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
