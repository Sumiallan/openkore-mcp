#!/usr/bin/env python3
"""
Ingere a wiki oficial do OpenKore (MediaWiki 1.43) em um SQLite local.

Fonte: https://openkore.com/api.php  (licenca do conteudo: CC BY-SA 3.0)

Uso:
    python ingest.py                       # baixa tudo e cria openkore.db
    python ingest.py --md docs             # tambem exporta 1 .md por pagina
    python ingest.py --limit 40            # teste rapido (40 paginas)
    python ingest.py --update              # re-baixa so o que mudou (recentchanges)
    python ingest.py --check               # valida o banco, sem rede

Etica de crawling (robots.txt de openkore.com):
  * User-Agent identificado
  * Crawl-delay: 3 -> 3s de pausa entre requisicoes
  * Nao usa Special:Export (bloqueado no robots.txt); usa api.php da raiz
  * ~763 artigos = ~16 requests = ~1 minuto
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://openkore.com/api.php"
WIKI = "https://openkore.com/wiki/"
UA = "openkore-docs-mcp/1.0 (uso pessoal; contato: local)"
CRAWL_DELAY = 3.0
BATCH = 50
SCHEMA_VERSION = 1


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def api_get(params: dict, tries: int = 5) -> dict:
    q = {**params, "format": "json", "formatversion": "2"}
    url = API + "?" + urllib.parse.urlencode(q)
    last: object = None
    for attempt in range(tries):
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 502, 503, 504):
                wait = CRAWL_DELAY * (2**attempt)
                print(f"  HTTP {e.code}; aguardando {wait:.0f}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            time.sleep(CRAWL_DELAY * (2**attempt))
    raise RuntimeError(f"falhou apos {tries} tentativas: {last}")


def fetch_all(limit: int | None = None, quiet: bool = False) -> list[dict]:
    """Lista todas as paginas do namespace principal com conteudo e categorias."""
    out: list[dict] = []
    cont: dict | None = None
    while True:
        p = {
            "action": "query",
            "generator": "allpages",
            "gaplimit": str(BATCH),
            "gapnamespace": "0",
            "prop": "revisions|categories",
            "rvprop": "ids|timestamp|content",
            "rvslots": "main",
            "cllimit": "max",
        }
        if cont:
            p.update(cont)
        if limit:
            p["gaplimit"] = str(max(1, min(BATCH, limit - len(out))))
        data = api_get(p)
        for pg in data.get("query", {}).get("pages", []):
            revs = pg.get("revisions") or []
            if not revs:
                continue
            rev = revs[0]
            slot = (rev.get("slots") or {}).get("main") or {}
            out.append(
                {
                    "title": pg["title"],
                    "revid": rev.get("revid"),
                    "ts": rev.get("timestamp"),
                    "cats": [c["title"] for c in pg.get("categories", [])],
                    "wikitext": slot.get("content") or "",
                }
            )
        if not quiet:
            print(f"  {len(out)} paginas...", file=sys.stderr)
        if limit and len(out) >= limit:
            out = out[:limit]
            break
        cont = data.get("continue")
        if not cont:
            break
        time.sleep(CRAWL_DELAY)
    return out


def fetch_titles(titles: list[str]) -> list[dict]:
    """Re-baixa paginas especificas (usado pelo --update)."""
    out: list[dict] = []
    for i in range(0, len(titles), BATCH):
        chunk = titles[i : i + BATCH]
        data = api_get(
            {
                "action": "query",
                "titles": "|".join(chunk),
                "prop": "revisions|categories",
                "rvprop": "ids|timestamp|content",
                "rvslots": "main",
                "cllimit": "max",
            }
        )
        for pg in data.get("query", {}).get("pages", []):
            revs = pg.get("revisions") or []
            if not revs or pg.get("missing"):
                continue
            rev = revs[0]
            slot = (rev.get("slots") or {}).get("main") or {}
            out.append(
                {
                    "title": pg["title"],
                    "revid": rev.get("revid"),
                    "ts": rev.get("timestamp"),
                    "cats": [c["title"] for c in pg.get("categories", [])],
                    "wikitext": slot.get("content") or "",
                }
            )
        if i + BATCH < len(titles):
            time.sleep(CRAWL_DELAY)
    return out


# --------------------------------------------------------------------------- #
# Normalizacao e limpeza
# --------------------------------------------------------------------------- #
def norm(text: str) -> str:
    """Normaliza para busca: sem acento, camelCase separado, separadores -> espaco.

    teleportAuto_hp -> 'teleport auto hp'    (acha 'teleport' e 'teleportAuto_hp')
    timeouts.txt    -> 'timeouts txt'        (acha 'timeouts' e 'timeouts.txt')
    """
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


def clean_wikitext(text: str) -> str:
    t = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    t = re.sub(r"\{\|.*?\|\}", "", t, flags=re.S)  # tabelas
    t = re.sub(r"\[\[(?:File|Image|Category|Categoria):[^\[\]]*\]\]", "", t, flags=re.I)
    t = re.sub(r"\{\{[^{}]*\}\}", "", t)  # templates simples
    t = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", t)  # [[a|b]] -> b
    t = re.sub(r"\[\[([^\]]*)\]\]", r"\1", t)  # [[a]] -> a
    t = re.sub(r"\[https?://\S+\s+([^\]]*)\]", r"\1", t)  # [url texto] -> texto
    t = re.sub(r"\[https?://\S+\]", "", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = t.replace("'''''", "").replace("'''", "").replace("''", "")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


HEAD = re.compile(r"^(={2,6})\s*(.+?)\s*\1\s*$")


def split_sections(text: str) -> list[tuple[str, str]]:
    """Divide em (caminho da secao, corpo) respeitando a hierarquia de titulos."""
    stack: list[str] = []
    out: list[tuple[str, str]] = []
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            out.append((" > ".join(stack) if stack else "(introducao)", body))
        buf.clear()

    for line in text.splitlines():
        m = HEAD.match(line)
        if m:
            flush()
            level = len(m.group(1))
            del stack[level - 2 :]
            stack.append(m.group(2))
        else:
            buf.append(line)
    flush()
    return out


def extract_entries(body: str) -> list[tuple[str, str]]:
    """Extrai definicoes no formato da wiki: '; chave' seguido de linhas ': descricao'.

    Suporta varias chaves compartilhando uma descricao:
        ; ai_x (<seconds>)
        ; ai_y (<seconds>)
        : ver PR#3145
    """
    entries: list[tuple[str, str]] = []
    keys: list[str] = []
    desc: list[str] = []

    def flush() -> None:
        if keys and desc:
            k = " / ".join(x.strip() for x in keys)
            d = " ".join(x.strip() for x in desc).strip()
            if k and d:
                entries.append((k, d))
        keys.clear()
        desc.clear()

    for line in body.splitlines():
        s = line.strip()
        if s.startswith(";"):
            if desc:
                flush()
            keys.append(s[1:])
        elif s.startswith(":"):
            desc.append(s[1:])
        elif s == "":
            continue
        else:
            flush()
    flush()
    return entries


def build_chunks(page: dict) -> list[dict]:
    clean = clean_wikitext(page["wikitext"])
    rows: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for section, body in split_sections(clean):
        key = ("section", section, body[:200])
        if key not in seen:
            seen.add(key)
            rows.append({"kind": "section", "section": section, "entry_key": "", "body": body})
        for k, d in extract_entries(body):
            k2 = ("entry", section, k.lower())
            if k2 in seen:
                continue
            seen.add(k2)
            rows.append({"kind": "entry", "section": section, "entry_key": k, "body": d})
    return rows


# --------------------------------------------------------------------------- #
# Banco
# --------------------------------------------------------------------------- #
DDL = """
CREATE TABLE IF NOT EXISTS pages(
  title    TEXT PRIMARY KEY,
  url      TEXT NOT NULL,
  revid    INTEGER,
  ts       TEXT,
  cats     TEXT,
  size     INTEGER,
  wikitext TEXT,
  clean    TEXT
);
CREATE TABLE IF NOT EXISTS chunks(
  id        INTEGER PRIMARY KEY,
  page      TEXT NOT NULL,
  kind      TEXT NOT NULL,
  section   TEXT,
  entry_key TEXT,
  body      TEXT,
  n_page    TEXT, n_section TEXT, n_key TEXT, n_body TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunks_page ON chunks(page);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def connect(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    con.executescript(DDL)
    return con


def store(con: sqlite3.Connection, pages: list[dict], replace: bool = True) -> tuple[int, int]:
    n_pages = n_chunks = 0
    for pg in pages:
        clean = clean_wikitext(pg["wikitext"])
        if replace:
            con.execute("DELETE FROM chunks WHERE page = ?", (pg["title"],))
        con.execute(
            """INSERT INTO pages(title,url,revid,ts,cats,size,wikitext,clean)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(title) DO UPDATE SET
                 url=excluded.url, revid=excluded.revid, ts=excluded.ts,
                 cats=excluded.cats, size=excluded.size,
                 wikitext=excluded.wikitext, clean=excluded.clean""",
            (
                pg["title"],
                WIKI + urllib.parse.quote(pg["title"].replace(" ", "_")),
                pg["revid"],
                pg["ts"],
                json.dumps(pg["cats"], ensure_ascii=False),
                len(pg["wikitext"]),
                pg["wikitext"],
                clean,
            ),
        )
        n_pages += 1
        for c in build_chunks(pg):
            con.execute(
                """INSERT INTO chunks(page,kind,section,entry_key,body,
                                     n_page,n_section,n_key,n_body)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    pg["title"],
                    c["kind"],
                    c["section"],
                    c["entry_key"],
                    c["body"],
                    norm(pg["title"]),
                    norm(c["section"]),
                    norm(c["entry_key"]),
                    norm(c["body"]),
                ),
            )
            n_chunks += 1
    con.execute(
        "INSERT INTO meta(k,v) VALUES('schema_version',?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (str(SCHEMA_VERSION),),
    )
    con.commit()
    return n_pages, n_chunks


def export_markdown(con: sqlite3.Connection, outdir: Path) -> int:
    outdir.mkdir(parents=True, exist_ok=True)
    n = 0
    for r in con.execute("SELECT title,url,revid,ts,cats,clean FROM pages"):
        name = re.sub(r'[<>:"/\\|?*]', "_", r["title"])[:120] or "sem-titulo"
        cats = ", ".join(json.loads(r["cats"] or "[]"))
        head = (
            f"# {r['title']}\n\n"
            f"> Fonte: {r['url']}  \n"
            f"> Revision: {r['revid']} ({r['ts']})  \n"
            f"> Categorias: {cats}  \n"
            f"> Conteudo CC BY-SA 3.0 - OpenKore Wiki\n\n---\n\n"
        )
        (outdir / f"{name}.md").write_text(head + (r["clean"] or ""), encoding="utf-8")
        n += 1
    return n


def check(db: Path) -> int:
    if not db.exists():
        print(f"ERRO: {db} nao existe. Rode: python ingest.py", file=sys.stderr)
        return 1
    con = connect(db)
    pages = con.execute("SELECT COUNT(*) c FROM pages").fetchone()["c"]
    chunks = con.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"]
    entries = con.execute("SELECT COUNT(*) c FROM chunks WHERE kind='entry'").fetchone()["c"]
    meta = {r["k"]: r["v"] for r in con.execute("SELECT k,v FROM meta")}
    print(f"banco      : {db}")
    print(f"paginas    : {pages}")
    print(f"chunks     : {chunks}  (entradas de config: {entries})")
    print(f"coletado em: {meta.get('fetched_at', '?')}")
    print("\namostra de chaves indexadas:")
    for r in con.execute(
        "SELECT page, entry_key FROM chunks WHERE kind='entry' "
        "AND entry_key LIKE '%ai\\_%' ESCAPE '\\' LIMIT 5"
    ):
        print(f"  {r['page']:<20} {r['entry_key']}")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Ingere a wiki do OpenKore em SQLite.")
    ap.add_argument("--db", default="openkore.db", type=Path)
    ap.add_argument("--md", type=Path, default=None, help="exporta Markdown nesse diretorio")
    ap.add_argument("--limit", type=int, default=None, help="limita o numero de paginas")
    ap.add_argument("--update", action="store_true", help="re-baixa apenas paginas alteradas")
    ap.add_argument("--check", action="store_true", help="valida o banco e sai")
    args = ap.parse_args(argv)

    if args.check:
        return check(args.db)

    con = connect(args.db)

    if args.update:
        row = con.execute("SELECT v FROM meta WHERE k='fetched_at'").fetchone()
        if not row:
            print("Banco vazio; rode uma ingestao completa primeiro.", file=sys.stderr)
            return 1
        since = row["v"]
        print(f"Buscando alteracoes desde {since}...", file=sys.stderr)
        data = api_get(
            {
                "action": "query",
                "list": "recentchanges",
                "rcprop": "title|timestamp",
                "rctype": "edit|new",
                "rcnamespace": "0",
                "rclimit": "500",
                "rcend": since,
            }
        )
        titles = sorted({c["title"] for c in data.get("query", {}).get("recentchanges", [])})
        if not titles:
            print("Nada mudou.", file=sys.stderr)
            return 0
        print(f"{len(titles)} paginas alteradas.", file=sys.stderr)
        pages = fetch_titles(titles)
    else:
        print("Baixando a wiki (respeitando Crawl-delay de 3s)...", file=sys.stderr)
        pages = fetch_all(limit=args.limit)

    if not pages:
        print("Nenhuma pagina obtida.", file=sys.stderr)
        return 1

    n_pages, n_chunks = store(con, pages)
    con.execute(
        "INSERT INTO meta(k,v) VALUES('fetched_at',?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"),),
    )
    con.execute(
        "INSERT INTO meta(k,v) VALUES('source',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (API,),
    )
    con.commit()

    total_p = con.execute("SELECT COUNT(*) c FROM pages").fetchone()["c"]
    total_c = con.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"]
    print(
        f"\nOK: {n_pages} paginas gravadas ({total_p} no banco), "
        f"{n_chunks} chunks novos ({total_c} no banco).",
        file=sys.stderr,
    )

    if args.md:
        n = export_markdown(con, args.md)
        print(f"Markdown: {n} arquivos em {args.md}", file=sys.stderr)

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
