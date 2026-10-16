# openkore-mcp

Servidor [MCP](https://modelcontextprotocol.io) local que dá a um agente de IA
**duas camadas de conhecimento sobre o [OpenKore](https://github.com/OpenKore/openkore)** —
a wiki oficial e o código-fonte Perl — sem nenhuma dependência além da
biblioteca padrão do Python.

Roda inteiramente na sua máquina. Depois que os índices são construídos, ele
nunca mais acessa a rede.

```
┌─ Camada 1 · WIKI ───── openkore.com (MediaWiki)  → "como configuro X?"
├─ Camada 2 · CÓDIGO ─── fonte Perl @ commit git   → "como isso funciona por dentro?"
└─ Camada 3 · SEMÂNTICA ─ embeddings (opcional)    → "meu bot fica parado"
```

> English version: [README.md](README.md)

---

## Por que duas camadas

A wiki é a referência de configuração, mas é **incompleta** — e isso é medido,
não opinado:

- Numa amostra de 50 páginas, cerca de metade são stubs com menos de 200 bytes
  (`BaseLevel` = 99 bytes, `Delay` = 89).
- O `timeouts.txt` tem entradas literalmente marcadas `TODO: add description`.
- A Main Page tem uma seção "Current Wiki TODO's" admitindo páginas
  desatualizadas e órfãs.

Comparando os dois índices nesta máquina:

| | Chaves documentadas na wiki | Chaves lidas pelo código |
|---|---|---|
| Total | 961 | 447 |
| **Só em uma camada** | 655 | **141** |

Essas 141 opções (`castleWatch_active`, `autoWarp_from`, `avoidSkill`,
`buyerShopAuto_open`, …) existem no código e **não estão documentadas em lugar
nenhum**. Um agente treinado só na wiki nunca saberá que elas existem. Essa
lacuna é a razão de o servidor indexar as duas camadas, e a ferramenta
`config_coverage` existe para expô-la.

---

## Requisitos

- **Python 3.8+** (testado no 3.13). Nada mais — os três scripts principais usam
  só a biblioteca padrão.
- **git** (apenas para a camada de código)
- Opcional: `pip install fastembed` para busca semântica

Cerca de 300 MB de disco: o clone do código (~204 MB) mais os índices (~35 MB).

## Instalação rápida

```powershell
git clone https://github.com/Sumiallan/openkore-mcp.git
cd openkore-mcp
.\setup.cmd
```

O `setup.cmd` detecta o Python, constrói as três camadas e imprime o bloco de
configuração do seu cliente MCP. Opções: `-SkipWiki`, `-SkipCode`,
`-SkipEmbeddings`, `-PythonExe <caminho>`, `-SourceDir <caminho>`.

Medido de ponta a ponta num clone limpo com virtualenv vazio: **6 min 32 s** —
wiki ~1 min, clone de 204 MB do código ~4 min, `pip install` ~1 min, embeddings
~3 s. Downloads: ~260 MB de código e dependências, mais ~500 MB do modelo de
embeddings na primeira execução (~760 MB no total).

> **Por que `setup.cmd` e não `setup.ps1`?** O Windows vem com execução de
> scripts desabilitada, então `.\setup.ps1` falha com *"a execução de scripts foi
> desabilitada neste sistema"*. O wrapper `.cmd` chama o PowerShell com
> `-ExecutionPolicy Bypass`, funcionando por duplo-clique, no `cmd` ou no
> PowerShell, sem alterar nenhuma configuração da máquina. Se preferir o script
> direto:
>
> ```powershell
> powershell -ExecutionPolicy Bypass -File .\setup.ps1
> ```

## Instalação manual

**1 · Índice da wiki** (~1 minuto, usa a rede)

```powershell
python ingest.py --md docs
```

~22 requisições com 3 s de pausa, respeitando o `robots.txt` do site
(`Crawl-delay: 3`). Tudo é gravado em uma única transação, então uma falha nunca
deixa um índice pela metade.

**2 · Índice do código** (~10 segundos, totalmente offline)

```powershell
git clone --depth 1 https://github.com/OpenKore/openkore.git ..\openkore-src
python ingest_code.py --src ..\openkore-src --db code.db
```

**3 · Embeddings** (opcional, ~6 segundos depois do download do modelo)

```powershell
pip install -r requirements.txt
python build_vectors.py
```

Sem este passo tudo continua funcionando — a busca apenas fica lexical.

**Conferir**

```powershell
python server.py --selftest
```

---

## Registrar no seu cliente MCP

O servidor fala MCP por **stdio**: o cliente o inicia como processo filho. Não
existe porta, daemon nem serviço para instalar.

### Claude Desktop, Cursor, VS Code

No arquivo de configuração MCP (`claude_desktop_config.json`, `.cursor/mcp.json`, …),
trocando os dois caminhos:

```json
{
  "mcpServers": {
    "openkore": {
      "command": "<PYTHON_EXE>",
      "args": ["<CAMINHO_ABSOLUTO_DO_REPO>/server.py"]
    }
  }
}
```

### DSH

Acrescente à lista `insert:` do patch do seu perfil
(`~/.dsh/profiles/<perfil>/cordis.patch.yml`):

```yaml
    - id: mcp-openkore
      name: "@deepseek-ai/dsh-mcp-client"
      config:
        serverName: openkore
        transport: stdio
        command: '<PYTHON_EXE>'
        args: [ '<CAMINHO_ABSOLUTO_DO_REPO>/server.py' ]
        toolCallTimeoutMs: 300000
```

O `toolCallTimeoutMs` é generoso de propósito: a primeira consulta semântica
carrega o modelo de embeddings sob demanda.

### Qualquer outro cliente

O `server.py` lê JSON-RPC 2.0 delimitado por linha na stdin e escreve na stdout.
Implementa `initialize`, `ping`, `tools/list` e `tools/call`. Os logs vão para
stderr, então a stdout fica limpa.

---

## Ferramentas

Nove ferramentas, expostas condicionalmente: as de código só aparecem quando
`code.db` existe, e `config_coverage` só quando os dois existem.

### Camada wiki

| Ferramenta | Para quê |
|---|---|
| `search_docs` | Busca híbrida. Devolve trechos curtos com `url` + revisão. |
| `get_page` | Uma página inteira em texto limpo. |
| `get_config_key` | Definição de uma opção, opcionalmente por arquivo. |
| `list_pages` | O que existe — confira antes de dizer que algo não existe. |
| `docs_stats` | Cobertura, idade do índice, flag `stale`, modelo ativo. |

### Camada código

| Ferramenta | Para quê |
|---|---|
| `search_code` | Busca símbolos (packages + subs). Devolve `arquivo:linha`. |
| `get_symbol` | Corpo completo de um sub ou package. |
| `find_in_source` | Onde uma chave de config ou hook é realmente lida. |
| `config_coverage` | Análise de lacuna entre as duas camadas. |

### Exemplos

```jsonc
// "O que faz o ai_sync?"
get_config_key(file="timeouts.txt", key="ai_sync")
// → entry_key "ai_sync ()", descrição, url, revid

// "Essa opção é realmente lida pelo código?"
find_in_source(name="teleportAuto_hp")
// → src/AI/CoreLogic.pm:3652
//   ($config{teleportAuto_hp} && percent_hp($char) <= $config{teleportAuto_hp})

// "Como a distância de ataque é tratada por dentro?"
find_in_source(name="attackDistance")
// → 10 pontos, incluindo src/Actor/You.pm:401 onde o Kore ESCREVE a config

// "O que a wiki não cobre?"
config_coverage()
// → 141 chaves usadas no código e nunca documentadas
```

O exemplo do `attackDistance` é o tipo de coisa que só o código revela: a wiki
descreve a opção; o código mostra que `src/Actor/You.pm` a altera em tempo de
execução conforme os itens equipados.

---

## Como funciona

**Chunking em duas camadas.** A wiki é um dicionário de chaves (`; chave`
seguido de `: descrição`). Fatiar só por título faz um trecho carregar 15 chaves
diferentes e a busca erra o alvo. Por isso cada definição vira um registro
atômico, ao lado das seções de prosa.

**Normalização que resolve camelCase.** `teleportAuto_hp` é indexado como
`teleport auto hp`, então `teleport`, `teleportAuto_hp` e *"teleport quando o hp
cai"* chegam no mesmo lugar. `timeouts.txt` vira `timeouts txt`, então
`timeouts` também casa.

**Peso lexical adaptativo.** Numa pergunta em português o lado lexical casa
quase só por palavras comuns como "bot" e produz um ranking ruim. O servidor
mede quantos termos da consulta o melhor acerto cobre e, quando a cobertura é
baixa, deixa a semântica assumir. Numa consulta por identificador a cobertura é
alta e o léxico continua mandando.

**Exatidão vence consenso.** O RRF premia "aparece nas duas listas", o que
rebaixava acertos literais — buscar `ai_sync` devolvia a página `ai`. Casamento
exato de título ou chave é certeza, não heurística, então esses resultados são
fixados no topo e marcados com `exact_match: true`. É por isso que um resultado
fixado pode ter score menor que o seguinte: o score é da fusão, a ordem é da
certeza.

**Citação obrigatória.** Resultados da wiki trazem `url` + `revid`; do código
trazem `arquivo:linha` + commit. Busca vazia devolve instrução explícita de dizer
"não documentado" em vez de chutar. Documentação de configuração é exatamente o
domínio onde o modelo inventa nomes plausíveis — `ai_teleport_hp` existe,
`ai_teleport_hpp` não.

**Nada é buscado na hora da consulta.** O servidor abre dois arquivos SQLite e
nunca faz requisição de rede, então a wiki estar fora do ar não afeta o uso
diário. Só a reindexação precisa da rede, e falha de forma limpa quando não
consegue.

---

## Manutenção

```powershell
python ingest.py --update        # re-baixa só páginas alteradas (recentchanges)
git -C ..\openkore-src pull      # atualiza o código
python ingest_code.py --src ..\openkore-src --db code.db
python build_vectors.py          # re-embeddar depois de qualquer reindexação
```

O `--update` nunca apaga; páginas removidas da wiki continuam no índice. Rode uma
ingestão completa se isso importar.

## Problemas comuns

| Sintoma | Causa / solução |
|---|---|
| `indice nao encontrado: openkore.db` | Rode `python ingest.py`. |
| Ferramentas de código ausentes | `code.db` não encontrado. Rode `ingest_code.py`. |
| `semantic_search: false` no `docs_stats` | `fastembed` não instalado, ou `build_vectors.py` não rodado. |
| Primeira consulta lenta | O modelo de embeddings carrega sob demanda (~20 s na primeira vez). |
| `database is locked` | Outro processo está escrevendo o índice. Espere terminar. |
| Erro de dimensão de vetor | Os vetores foram refeitos com modelo diferente do registrado. Rode `build_vectors.py` de novo. |
| "a execução de scripts foi desabilitada" | Use `setup.cmd` em vez de `setup.ps1`. |

## Escolha do modelo

Medido neste corpus (9834 trechos, só CPU):

| Modelo | Build | Dims | Busca no código: *"meu bot fica parado sem atacar"* |
|---|---|---|---|
| `paraphrase-multilingual-MiniLM-L12-v2` | 4,5 min | 384 | `merchantdb::connect` ❌ |
| **`potion-multilingual-128M`** (padrão) | **6,3 s** | 256 | **`AI::Attack::finishAttacking`** ✅ |
| `intfloat/multilingual-e5-large` | >70 min | 1024 | não testado (abortado pelo custo) |

O menor e mais rápido ganhou nos dois eixos. Para qualidade máxima:

```powershell
python build_vectors.py --model Qwen/Qwen3-Embedding-0.6B-Q   # 1024 dims, ~31 min
```

## Limitações

- **A busca semântica tem teto.** Para uma pergunta vaga como *"meu bot fica
  parado sem atacar"*, nem o modelo de 384 nem o de 256 dims colocam
  `attackAuto` em primeiro. O teto é a pergunta, não o modelo: ela não aponta
  para nenhuma chave específica. Subir para 1024 dims não conserta ambiguidade.
- **O `config_coverage` é indício, não prova.** Chaves lidas dinamicamente nunca
  aparecem como `$config{...}`, então "não está no código" pode ser falso
  negativo. A própria ferramenta avisa no campo `note`.
- **A camada de código é um snapshot.** Sem `git pull` + `ingest_code.py` ela
  envelhece sem avisar — por isso o commit aparece em toda resposta de código.
- **A camada da wiki também envelhece.** O `docs_stats` reporta `age_days` e uma
  flag `stale` acima de 30 dias.

## Licença e atribuição

- **Código deste repositório: MIT** — veja [LICENSE](LICENSE).
- **Conteúdo da wiki**: [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/).
  Este repositório **não o redistribui**: `*.db` e `docs/` estão no `.gitignore`
  e são reconstruídos localmente pelo `ingest.py`. Todo resultado carrega `url` e
  `revid` para atribuição.
- **O código do OpenKore** também não é redistribuído; o `ingest_code.py` indexa
  um clone que você mesmo faz.
