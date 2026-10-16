#!/usr/bin/env python3
"""
Indexa o codigo-fonte do OpenKore (Perl) em um SQLite local.

Fonte: clone de https://github.com/OpenKore/openkore (licenca do projeto).
Complementa ingest.py: a wiki documenta CONFIGURACAO, o codigo define
COMPORTAMENTO. Muitas chaves existem no codigo e nao na wiki (e vice-versa).

Uso:
    python ingest_code.py --src ..\\openkore-src --db code.db
    python ingest_code.py --check

Extrai:
  * packages   -> 'AI::Attack'
  * subs       -> 'AI::Attack::shouldAttack' (com o corpo, para busca)
  * $config{chave} -> onde cada chave de configuracao e realmente lida
  * callHook('nome') -> ganchos (hooks) que os plugins usam
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

# Pastas que nao sao codigo do OpenKore (dados de jogo, deps de build, historico)
SKIP_DIRS = {
    ".git", "tables", "fields", "Distfiles", "build", "deps",
    "scons-local-3.1.2", "scons-local-3.1.3", "po", "doc", "test",
    ".github", "script-launcher",
}
CODE_SUFFIXES = {".pm", ".pl", ".t"}

SCHEMA_VERSION = 1
MAX_BODY = 6000

DDL = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS symbols(
  id        INTEGER PRIMARY KEY,
  kind      TEXT NOT NULL,        -- 'package' | 'sub'
  name      TEXT NOT NULL,        -- 'AI::Attack::shouldAttack'
  pkg       TEXT,
  file      TEXT NOT NULL,
  line      INTEGER,
  signature TEXT,
  body      TEXT,
  n_name    TEXT, n_body TEXT, n_file TEXT
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);
CREATE TABLE IF NOT EXISTS refs(
  id      INTEGER PRIMARY KEY,
  kind    TEXT NOT NULL,          -- 'config' | 'hook'
  name    TEXT NOT NULL,
  file    TEXT NOT NULL,
  line    INTEGER,
  context TEXT,
  n_name  TEXT
);
CREATE INDEX IF NOT EXISTS idx_refs_kind_name ON refs(kind, name);
"""


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


PKG_RE = re.compile(r"^package\s+([A-Za-z_][\w:]*)\s*;")
SUB_RE = re.compile(r"^sub\s+([A-Za-z_]\w*)\s*(.*)$")
CFG_RE = re.compile(r"\$config\s*(?:->)?\s*\{\s*'?([A-Za-z_]\w*)'?\s*\}")
HOOK_RE = re.compile(r"callHook\s*\(\s*'([^']+)'")
HOOK_RE2 = re.compile(r"callHook\s*\(\s*\"([^\"]+)\"")


def git_rev(root: Path) -> str | None:
    """Le o commit atual sem depender de subprocess."""
    git = root / ".git"
    head = git / "HEAD"
    if not head.exists():
        return None
    try:
        content = head.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not content.startswith("ref:"):
        return content[:12] or None
    ref = content.split(":", 1)[1].strip()
    direct = git / ref
    if direct.exists():
        try:
            return direct.read_text(encoding="utf-8").strip()[:12] or None
        except OSError:
            pass
    packed = git / "packed-refs"
    if packed.exists():
        try:
            for ln in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                if ln.endswith(" " + ref):
                    return ln.split(" ", 1)[0][:12] or None
        except OSError:
            pass
    return None


def code_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in CODE_SUFFIXES:
            continue
        rel_parts = p.relative_to(root).parts
        if any(part in SKIP_DIRS for part in rel_parts[:-1]):
            continue
        out.append(p)
    return sorted(out)


def read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def extract(root: Path, con: sqlite3.Connection) -> tuple[int, int, int, int]:
    """Percorre os fontes e grava symbols + refs."""
    con.execute("DELETE FROM symbols")
    con.execute("DELETE FROM refs")
    n_pkg = n_sub = n_cfg = n_hook = 0

    for path in code_files(root):
        rel = path.relative_to(root).as_posix()
        lines = read_lines(path)
        pkg = ""
        i = 0
        while i < len(lines):
            line = lines[i]
            raw = line.rstrip("\n")

            mp = PKG_RE.match(raw)
            if mp:
                pkg = mp.group(1)
                n_pkg += 1
                con.execute(
                    """INSERT INTO symbols(kind,name,pkg,file,line,signature,body,
                                           n_name,n_body,n_file)
                       VALUES('package',?,?,?,?,?,?,?,?,?)""",
                    (pkg, pkg, rel, i + 1, raw.strip(), "",
                     norm(pkg.replace("::", " ")), "", norm(rel)),
                )

            ms = SUB_RE.match(raw)
            if ms and not raw.rstrip().endswith(";"):
                name = ms.group(1)
                sig = ms.group(2).strip()
                body_lines: list[str] = []
                j = i + 1
                while j < len(lines):
                    if lines[j].startswith("}"):
                        break
                    body_lines.append(lines[j])
                    j += 1
                body = "\n".join(body_lines).strip()[:MAX_BODY]
                full = f"{pkg}::{name}" if pkg else name
                n_sub += 1
                con.execute(
                    """INSERT INTO symbols(kind,name,pkg,file,line,signature,body,
                                           n_name,n_body,n_file)
                       VALUES('sub',?,?,?,?,?,?,?,?,?)""",
                    (full, pkg, rel, i + 1, f"sub {name} {sig}".strip(), body,
                     norm(full.replace("::", " ")), norm(body), norm(rel)),
                )

            stripped = raw.strip()
            if stripped and not stripped.startswith("#"):
                mc = CFG_RE.search(raw)
                if mc:
                    n_cfg += 1
                    con.execute(
                        "INSERT INTO refs(kind,name,file,line,context,n_name)"
                        " VALUES('config',?,?,?,?,?)",
                        (mc.group(1), rel, i + 1, stripped[:200], norm(mc.group(1))),
                    )
                mh = HOOK_RE.search(raw) or HOOK_RE2.search(raw)
                if mh:
                    n_hook += 1
                    con.execute(
                        "INSERT INTO refs(kind,name,file,line,context,n_name)"
                        " VALUES('hook',?,?,?,?,?)",
                        (mh.group(1), rel, i + 1, stripped[:200], norm(mh.group(1))),
                    )
            i += 1

    con.execute(
        "INSERT INTO meta(k,v) VALUES('schema_version',?)"
        " ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(SCHEMA_VERSION),),
    )
    con.execute(
        "INSERT INTO meta(k,v) VALUES('git_rev',?)"
        " ON CONFLICT(k) DO UPDATE SET v=excluded.v", (git_rev(root) or "",),
    )
    con.execute(
        "INSERT INTO meta(k,v) VALUES('source_root',?)"
        " ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(root),),
    )
    con.commit()
    return n_pkg, n_sub, n_cfg, n_hook


def check(db: Path) -> int:
    if not db.exists():
        print(f"ERRO: {db} nao existe. Rode: python ingest_code.py --src <clone>",
              file=sys.stderr)
        return 1
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    meta = {r["k"]: r["v"] for r in con.execute("SELECT k,v FROM meta")}
    q = lambda s: con.execute(s).fetchone()[0]
    print(f"banco      : {db}")
    print(f"commit     : {meta.get('git_rev') or '?'}")
    print(f"packages   : {q('SELECT COUNT(*) FROM symbols WHERE kind=\"package\"')}")
    print(f"subs       : {q('SELECT COUNT(*) FROM symbols WHERE kind=\"sub\"')}")
    print(f"config refs: {q('SELECT COUNT(*) FROM refs WHERE kind=\"config\"')}")
    print(f"hooks      : {q('SELECT COUNT(*) FROM refs WHERE kind=\"hook\"')}")
    print(f"chaves unicas: {q('SELECT COUNT(DISTINCT name) FROM refs WHERE kind=\"config\"')}")
    print("\namostra:")
    for r in con.execute(
        "SELECT name,file,line FROM symbols WHERE kind='sub'"
        " AND name LIKE '%Attack%' LIMIT 6"
    ):
        print(f"  {r['name']:<44} {r['file']}:{r['line']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="Indexa o codigo do OpenKore em SQLite.")
    ap.add_argument("--src", type=Path, default=here.parent / "openkore-src",
                    help="pasta do clone do OpenKore")
    ap.add_argument("--db", type=Path, default=here / "code.db")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)

    if args.check:
        return check(args.db)

    root = args.src.resolve()
    if not (root / "src").is_dir():
        print(f"ERRO: {root} nao parece um clone do OpenKore (falta src/).",
              file=sys.stderr)
        return 1

    con = sqlite3.connect(str(args.db))
    con.executescript(DDL)
    print(f"Indexando {root} ...", file=sys.stderr)
    n_pkg, n_sub, n_cfg, n_hook = extract(root, con)
    print(
        f"OK: {n_pkg} packages, {n_sub} subs, {n_cfg} usos de $config "
        f"({con.execute('SELECT COUNT(DISTINCT name) FROM refs WHERE kind=\"config\"').fetchone()[0]} chaves unicas), "
        f"{n_hook} hooks.",
        file=sys.stderr,
    )
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
