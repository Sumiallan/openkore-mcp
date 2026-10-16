<#
.SYNOPSIS
    Constroi os tres indices do openkore-mcp e imprime a configuracao do cliente.

.DESCRIPTION
    Detecta o Python, baixa a wiki do OpenKore, clona e indexa o codigo-fonte
    Perl e (opcionalmente) gera os embeddings multilingues.

    Cada etapa e pulavel e idempotente: rodar de novo so reconstroi.

.EXAMPLE
    .\setup.ps1
    .\setup.ps1 -SkipEmbeddings
    .\setup.ps1 -PythonExe 'C:\Python313\python.exe' -SourceDir 'D:\src\openkore'
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [string] $PythonExe = '',
    [string] $SourceDir = '',
    [switch] $SkipWiki,
    [switch] $SkipCode,
    [switch] $SkipEmbeddings
)

$ErrorActionPreference = 'Stop'
# Com ErrorActionPreference=Stop, o PowerShell 7.3+ pode converter saida nao-zero
# de comandos nativos em excecao. Aqui queremos inspecionar $LASTEXITCODE e
# decidir o que fazer, entao desligamos esse comportamento quando ele existe.
if (Get-Variable PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}
$here = $PSScriptRoot
Set-Location $here

if (-not $SourceDir) { $SourceDir = Join-Path (Split-Path $here -Parent) 'openkore-src' }

function Write-Step([string] $Text) { Write-Host "`n=== $Text" -ForegroundColor Cyan }
function Write-Ok([string] $Text)   { Write-Host "    $Text" -ForegroundColor Green }
function Write-Warn([string] $Text) { Write-Host "    $Text" -ForegroundColor Yellow }

function Resolve-Python {
    param([string] $Explicit)

    if ($Explicit) {
        if (-not (Test-Path $Explicit)) { throw "Python nao encontrado em: $Explicit" }
        return (Resolve-Path $Explicit).Path
    }

    $candidates = @(
        @{ Exe = 'py';      Pre = @('-3') },
        @{ Exe = 'python';  Pre = @() },
        @{ Exe = 'python3'; Pre = @() }
    )
    foreach ($c in $candidates) {
        if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $path = & $c.Exe @($c.Pre) -c "import sys;print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $path) { return $path.Trim() }
        } catch { }
    }
    throw "Python 3.8+ nao encontrado. Instale de https://www.python.org/downloads/ e rode de novo, ou use -PythonExe <caminho>."
}

function Assert-PythonVersion {
    param([string] $Exe)
    $v = & $Exe -c "import sys;print('%d.%d' % sys.version_info[:2])"
    $parts = $v.Trim().Split('.')
    if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 8)) {
        throw "Python $v encontrado, mas e preciso 3.8+."
    }
    return $v.Trim()
}

function Test-PythonModule {
    <#
        Verifica se um modulo existe no interpretador alvo.

        NAO use '& $py -c "import x" 2>$null' para isso: com
        ErrorActionPreference='Stop', redirecionar o stderr de um comando nativo
        faz o PowerShell 5.1 converter aquela saida em erro terminante — e num
        venv limpo o import que falha imprime traceback no stderr, ou seja, o
        caso normal derrubava o instalador. find_spec nao levanta excecao, entao
        nao ha stderr nenhum, e o try/catch cobre o resto.
    #>
    param([string] $Exe, [string] $Module)
    try {
        & $Exe -c "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec('$Module') else 1)"
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

$py = Resolve-Python -Explicit $PythonExe
$ver = Assert-PythonVersion -Exe $py
Write-Host "openkore-mcp setup" -ForegroundColor White
Write-Ok "Python $ver -> $py"
Write-Ok "Repositorio -> $here"
Write-Ok "Codigo-fonte -> $SourceDir"

# ---------------------------------------------------------------- 1. wiki ---
if ($SkipWiki) {
    Write-Step '1/3 Wiki: pulado (-SkipWiki)'
} else {
    Write-Step '1/3 Indexando a wiki (~1 min, usa a rede)'
    & $py (Join-Path $here 'ingest.py') --md (Join-Path $here 'docs')
    if ($LASTEXITCODE -ne 0) { throw 'ingest.py falhou. A wiki pode estar fora do ar; tente de novo mais tarde.' }
    Write-Ok 'openkore.db criado'
}

# -------------------------------------------------------------- 2. codigo ---
if ($SkipCode) {
    Write-Step '2/3 Codigo: pulado (-SkipCode)'
} else {
    Write-Step '2/3 Indexando o codigo-fonte Perl'
    if (Test-Path (Join-Path $SourceDir 'src')) {
        Write-Ok "clone ja existe, reutilizando: $SourceDir"
    } else {
        if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
            Write-Warn 'git nao encontrado; pulando a camada de codigo.'
            $SkipCode = $true
        } else {
            Write-Ok 'clonando OpenKore/openkore (--depth 1, ~204 MB)...'
            & git clone --depth 1 https://github.com/OpenKore/openkore.git $SourceDir
            if ($LASTEXITCODE -ne 0) {
                Write-Warn 'clone falhou; pulando a camada de codigo.'
                $SkipCode = $true
            }
        }
    }
    if (-not $SkipCode) {
        & $py (Join-Path $here 'ingest_code.py') --src $SourceDir --db (Join-Path $here 'code.db')
        if ($LASTEXITCODE -ne 0) { Write-Warn 'ingest_code.py falhou; a camada de codigo fica indisponivel.' }
        else { Write-Ok 'code.db criado' }
    }
}

# ---------------------------------------------------------- 3. embeddings ---
if ($SkipEmbeddings) {
    Write-Step '3/3 Embeddings: pulado (-SkipEmbeddings); busca segue lexical'
} else {
    Write-Step '3/3 Embeddings multilingues (opcional)'

    if (-not (Test-PythonModule $py 'fastembed')) {
        Write-Ok 'instalando fastembed (pip install -r requirements.txt)...'
        # pip escreve progresso no stderr; sob ErrorActionPreference=Stop isso pode
        # virar erro terminante mesmo quando a instalacao da certo. Por isso o
        # resultado e verificado pelo modulo, nao pelo que o PowerShell acha.
        try {
            & $py -m pip install --quiet --disable-pip-version-check -r (Join-Path $here 'requirements.txt')
        } catch {
            Write-Warn "pip retornou um erro; verificando se mesmo assim instalou..."
        }
    }

    if (-not (Test-PythonModule $py 'fastembed')) {
        Write-Warn 'fastembed indisponivel; seguindo sem busca semantica.'
        Write-Warn "instale depois com:  & '$py' -m pip install -r requirements.txt"
    } else {
        & $py (Join-Path $here 'build_vectors.py')
        if ($LASTEXITCODE -eq 0) { Write-Ok 'vetores gerados' }
        else { Write-Warn 'build_vectors.py falhou; busca segue lexical.' }
    }
}

# ------------------------------------------------------------------ resumo ---
Write-Step 'Verificacao'
# Sem '2>&1' aqui: o servidor loga no stderr de proposito, e redirecionar
# stderr de comando nativo sob ErrorActionPreference=Stop derruba o script.
& $py (Join-Path $here 'server.py') --selftest 'ai_sync'

Write-Step 'Pronto. Registre no seu cliente MCP:'
$serverPath = (Join-Path $here 'server.py')
Write-Host @"

  Claude Desktop / Cursor / VS Code  ->  no arquivo de config MCP:

  {
    "mcpServers": {
      "openkore": {
        "command": "$($py -replace '\\','\\')",
        "args": ["$($serverPath -replace '\\','\\')"]
      }
    }
  }

  DSH  ->  em ~/.dsh/profiles/<perfil>/cordis.patch.yml, dentro da lista insert:

    - id: mcp-openkore
      name: "@deepseek-ai/dsh-mcp-client"
      config:
        serverName: openkore
        transport: stdio
        command: '$py'
        args: [ '$serverPath' ]
        toolCallTimeoutMs: 300000

"@ -ForegroundColor Gray
