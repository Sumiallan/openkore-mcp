# openkore-mcp

A local [MCP](https://modelcontextprotocol.io) server that gives an AI agent
**two layers of knowledge about [OpenKore](https://github.com/OpenKore/openkore)** —
the official wiki and the Perl source code — with no dependencies beyond the
Python standard library.

Runs entirely on your machine. After the indexes are built, it never touches the
network.

```
┌─ Layer 1 · WIKI ──── openkore.com (MediaWiki)  → "how do I configure X?"
├─ Layer 2 · SOURCE ── Perl source @ git commit  → "how does it actually work?"
└─ Layer 3 · SEMANTIC ─ embeddings (optional)    → "my bot just stands there"
```

---

## Why two layers

The wiki is the configuration reference, but it is **incomplete**, and that is
measurable rather than a matter of opinion:

- Of a 50-page sample, about half are stubs under 200 bytes (`BaseLevel` = 99
  bytes, `Delay` = 89).
- `timeouts.txt` contains entries literally marked `TODO: add description`.
- The Main Page has a "Current Wiki TODO's" section admitting stale and orphan
  pages.

Comparing both indexes on this machine:

| | Keys documented in the wiki | Keys read by the source |
|---|---|---|
| Count | 961 | 447 |
| **Only in one layer** | 655 | **141** |

Those 141 options (`castleWatch_active`, `autoWarp_from`, `avoidSkill`,
`buyerShopAuto_open`, …) exist in the code and are **not documented anywhere**.
An agent trained only on the wiki will never know they exist. That gap is the
reason this server indexes both, and `config_coverage` exists to expose it.

---

## Requirements

- **Python 3.8+** (tested on 3.13). Nothing else — the three core scripts use
  only the standard library.
- **git** (only for the source layer)
- Optional: `pip install fastembed` for semantic search

Roughly 300 MB of disk: the source clone (~204 MB) plus the indexes (~35 MB).

## Quick start

```powershell
git clone https://github.com/Sumiallan/openkore-mcp.git
cd openkore-mcp
.\setup.cmd
```

`setup.cmd` detects Python, builds all three layers and prints the config block
for your MCP client. Flags: `-SkipWiki`, `-SkipCode`, `-SkipEmbeddings`,
`-PythonExe <path>`, `-SourceDir <path>`.

Measured end to end on a clean checkout with an empty virtualenv:
**6 min 32 s** — wiki ~1 min, 204 MB source clone ~4 min, `pip install` ~1 min,
embeddings ~3 s. Downloads: ~260 MB for the source and dependencies, plus
~500 MB for the embedding model on first run (~760 MB total).

> **Why `setup.cmd` and not `setup.ps1`?** Windows ships with script execution
> disabled by default, so `.\setup.ps1` fails with *"running scripts is disabled
> on this system"*. The `.cmd` wrapper calls PowerShell with
> `-ExecutionPolicy Bypass`, so it works by double-click, from `cmd` or from
> PowerShell without changing any machine setting. If you prefer the script
> directly:
>
> ```powershell
> powershell -ExecutionPolicy Bypass -File .\setup.ps1
> ```

## Manual install

**1 · Wiki index** (~1 minute, uses the network)

```powershell
python ingest.py --md docs
```

~22 requests with a 3-second pause, honouring the site's `robots.txt`
(`Crawl-delay: 3`). Everything is written in a single transaction, so a failure
never leaves a half-built index.

**2 · Source index** (~10 seconds, fully offline)

```powershell
git clone --depth 1 https://github.com/OpenKore/openkore.git ..\openkore-src
python ingest_code.py --src ..\openkore-src --db code.db
```

**3 · Embeddings** (optional, ~6 seconds after the model downloads)

```powershell
pip install -r requirements.txt
python build_vectors.py
```

Without this step everything still works — search just stays lexical.

**Check it**

```powershell
python server.py --selftest
```

---

## Register with your MCP client

The server speaks MCP over **stdio**: your client launches it as a child process.
There is no port, no daemon and no service to install.

### Claude Desktop, Cursor, VS Code

Add to your MCP config (`claude_desktop_config.json`, `.cursor/mcp.json`, …),
replacing the two paths:

```json
{
  "mcpServers": {
    "openkore": {
      "command": "<PYTHON_EXE>",
      "args": ["<ABSOLUTE_PATH_TO_REPO>/server.py"]
    }
  }
}
```

### DSH

Append to the `insert:` list in your profile patch
(`~/.dsh/profiles/desktop/cordis.patch.yml`):

```yaml
    - id: mcp-openkore
      name: "@deepseek-ai/dsh-mcp-client"
      config:
        serverName: openkore
        transport: stdio
        command: '<PYTHON_EXE>'
        args: [ '<ABSOLUTE_PATH_TO_REPO>/server.py' ]
        toolCallTimeoutMs: 300000
```

`toolCallTimeoutMs` is generous because the first semantic query loads the
embedding model on demand.

### Any other client

`server.py` reads newline-delimited JSON-RPC 2.0 on stdin and writes it on
stdout. It implements `initialize`, `ping`, `tools/list` and `tools/call`. Logs
go to stderr, so stdout stays clean.

---

## Tools

Nine tools, exposed conditionally: the source tools appear only when `code.db`
exists, `config_coverage` only when both do.

### Wiki layer

| Tool | Use |
|---|---|
| `search_docs` | Hybrid search. Returns short snippets with `url` + revision. |
| `get_page` | One full page as clean text. |
| `get_config_key` | One option's definition, optionally scoped to a file. |
| `list_pages` | What exists — check before claiming something is absent. |
| `docs_stats` | Coverage, index age, `stale` flag, active model. |

### Source layer

| Tool | Use |
|---|---|
| `search_code` | Search symbols (packages + subs). Returns `file:line`. |
| `get_symbol` | Full body of one sub or package. |
| `find_in_source` | Where a config key or hook is actually read. |
| `config_coverage` | Gap analysis between the two layers. |

### Examples

```jsonc
// "What does ai_sync do?"
get_config_key(file="timeouts.txt", key="ai_sync")
// → entry_key "ai_sync ()", description, url, revid

// "Is this option really read by the code?"
find_in_source(name="teleportAuto_hp")
// → src/AI/CoreLogic.pm:3652
//   ($config{teleportAuto_hp} && percent_hp($char) <= $config{teleportAuto_hp})

// "How is attack distance handled internally?"
find_in_source(name="attackDistance")
// → 10 sites, incl. src/Actor/You.pm:401 where Kore WRITES the config at runtime

// "What does the wiki not cover?"
config_coverage()
// → 141 keys used in the source and never documented
```

The `attackDistance` example is the kind of thing only the source tells you: the
wiki describes the option; the code reveals that `src/Actor/You.pm` mutates it at
runtime based on equipped items.

---

## How it works

**Two-tier chunking.** The wiki is a dictionary of config keys (`; key` followed
by `: description`). Splitting by heading alone makes one chunk carry 15
different keys and retrieval misses the target. So every definition becomes an
atomic record, alongside the prose sections.

**Normalisation that handles camelCase.** `teleportAuto_hp` is indexed as
`teleport auto hp`, so `teleport`, `teleportAuto_hp` and *"teleport when hp is
low"* all land in the same place. `timeouts.txt` becomes `timeouts txt`, so
`timeouts` matches too.

**Adaptive lexical weight.** In a Portuguese question the lexical side matches
mostly on common words like "bot" and produces a poor ranking. The server
measures how many query terms the best hit covers and, when coverage is low,
lets the semantic side take over. For an identifier query coverage is high and
lexical stays in charge.

**Exactness beats consensus.** RRF rewards "appears in both lists", which
demoted literal matches — searching `ai_sync` returned the `ai` page. An exact
title or key match is a certainty, not a heuristic, so those results are pinned
to the top and flagged `exact_match: true`. That is why a pinned result can show
a lower score than the next one: the score is fusion, the order is certainty.

**Citations are mandatory.** Wiki results carry `url` + `revid`; source results
carry `file:line` + commit. An empty search returns an explicit instruction to
say "not documented" rather than guess. Configuration documentation is exactly
the domain where a model invents plausible names — `ai_teleport_hp` exists,
`ai_teleport_hpp` does not.

**Nothing is fetched at query time.** The server opens two SQLite files and
never makes a network request, so the wiki being down has no effect on daily
use. Only re-indexing needs the network, and it fails cleanly when it can't.

---

## Maintenance

```powershell
python ingest.py --update        # re-fetch only changed pages (recentchanges)
git -C ..\openkore-src pull      # update the source
python ingest_code.py --src ..\openkore-src --db code.db
python build_vectors.py          # re-embed after any re-index
```

`--update` never deletes; pages removed from the wiki stay in the index. Run a
full re-ingest if that matters to you.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `indice nao encontrado: openkore.db` | Run `python ingest.py`. |
| Source tools missing | `code.db` not found. Run `ingest_code.py`. |
| `semantic_search: false` in `docs_stats` | `fastembed` not installed, or `build_vectors.py` not run. |
| First query is slow | The embedding model loads on demand (measured: ~20 s first time). |
| `database is locked` | Another process is writing the index. Wait for it to finish. |
| Dimension mismatch error | Vectors were rebuilt with a different model than the one recorded. Re-run `build_vectors.py`. |

## Model choice

Measured on this project's corpus (9834 passages, CPU only):

| Model | Build | Dims | Source search: *"meu bot fica parado sem atacar"* |
|---|---|---|---|
| `paraphrase-multilingual-MiniLM-L12-v2` | 4.5 min | 384 | `merchantdb::connect` ❌ |
| **`potion-multilingual-128M`** (default) | **6.3 s** | 256 | **`AI::Attack::finishAttacking`** ✅ |
| `intfloat/multilingual-e5-large` | >70 min | 1024 | not tested (aborted on cost) |

The smallest and fastest model won on both axes. For maximum quality:

```powershell
python build_vectors.py --model Qwen/Qwen3-Embedding-0.6B-Q   # 1024 dims, ~31 min
```

## Limitations

- **Semantic search has a ceiling.** For a vague question like *"my bot stands
  still and doesn't attack"*, neither the 384-dim nor the 256-dim model puts
  `attackAuto` first. The ceiling is the question, not the model: it doesn't
  point at any single key. Pushing to 1024 dims will not fix ambiguity.
- **`config_coverage` is a hint, not proof.** Keys read dynamically never appear
  as `$config{...}`, so "not in the source" can be a false negative. The tool
  says so in its own `note` field.
- **The source layer is a snapshot.** Without `git pull` + `ingest_code.py` it
  ages silently — which is why the commit hash appears in every source result.
- **The wiki layer ages too.** `docs_stats` reports `age_days` and a `stale` flag
  past 30 days.

## Licence and attribution

- **Code in this repository: MIT** — see [LICENSE](LICENSE).
- **Wiki content** is [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/).
  This repository **does not redistribute it**: `*.db` and `docs/` are
  gitignored and rebuilt locally by `ingest.py`. Every result carries its
  `url` and `revid` for attribution.
- **OpenKore source** is not redistributed either; `ingest_code.py` indexes a
  clone you make yourself.
