<#
.SYNOPSIS
    Bring the optional memory stack up with the fewest manual steps — PowerShell
    parity of scripts/setup-memory.sh. Auto-runs the cheap/safe parts (pip deps)
    and confirms-and-runs the invasive ones (start Qdrant via Docker, pull the
    Ollama model, install the graphify CLI). Nothing is left as "read the docs".

    Idempotent + best-effort: anything present is reused; a prerequisite that
    can't be auto-installed prints the EXACT command.
.PARAMETER Plugins
    Comma-separated: qdrant-session-memory,graphify-code-graph. Default: whatever
    is present under .sumela/memory-plugins/.
.PARAMETER Yes
    Auto-confirm every action (agent/CI after the user opted in).
.PARAMETER NonInteractive
    Never prompt; skip invasive steps and print their exact commands.
#>
param(
    [string]$Plugins = "",
    [switch]$Yes,
    [switch]$NonInteractive
)

$ErrorActionPreference = "Continue"

# --- anchor to the nearest .sumela/ ---
$root = (Get-Location).Path
while ((-not (Test-Path (Join-Path $root ".sumela"))) -and ($root -ne (Split-Path $root -Parent))) {
    $root = Split-Path $root -Parent
}
if (-not (Test-Path (Join-Path $root ".sumela"))) { Write-Host "setup-memory: no .sumela/ found from here upward."; exit 1 }
Set-Location $root

$plugDir   = ".sumela/memory-plugins"
$qHost     = if ($env:QDRANT_HOST) { $env:QDRANT_HOST } else { "localhost" }
$qPort     = if ($env:QDRANT_PORT) { $env:QDRANT_PORT } else { "6333" }
$embedModel = if ($env:SUMELA_EMBED_MODEL) { $env:SUMELA_EMBED_MODEL } else { "qwen3-embedding:0.6b" }

$py = if (Get-Command python3 -ErrorAction SilentlyContinue) { "python3" } elseif (Get-Command python -ErrorAction SilentlyContinue) { "python" } else { $null }
if (-not $py) { Write-Host "python3/python not found — the memory stack needs Python 3.10+."; exit 1 }

$script:manual = 0
function Ok($m)   { Write-Host "  ok    $m" -ForegroundColor Green }
function Todo($m) { Write-Host "  todo  $m" -ForegroundColor Yellow; $script:manual++ }
function Info($m) { Write-Host "  info  $m" -ForegroundColor Cyan }
function Section($m) { Write-Host ""; Write-Host $m -ForegroundColor White }

function Confirm-Step($msg) {
    if ($Yes) { return $true }
    if ($NonInteractive) { return $false }
    if (-not [Environment]::UserInteractive) { return $false }
    $a = Read-Host "  $msg [Y/n]"
    if ($a -match '^(n|no)$') { return $false }
    return $true   # default Yes on Enter
}

# Register a memory plugin in SKILL_REGISTRY.md if its files are present but it
# isn't registered — makes `-Plugins X` an "enable it (now or later)" command.
function Register-Plugin($p) {
    $reg = ".sumela/SKILL_REGISTRY.md"
    if (-not (Test-Path $reg)) { return }
    $content = Get-Content $reg -Raw
    if ($content -match ('<name>' + [regex]::Escape($p) + '</name>')) { return }  # already registered
    $nl = [char]10; $bt = [char]96
    $entry = $nl + '<skill activation="lazy">' + $nl + '<name>' + $p + '</name>' + $nl +
        '<description>Memory plugin — see ' + $bt + '.sumela/memory-plugins/' + $p + '/SKILL.md' + $bt + ' for routing and prerequisites.</description>' + $nl +
        '<path>.sumela/memory-plugins/' + $p + '/SKILL.md</path>' + $nl + '</skill>' + $nl
    if ($content.Contains('</available_skills>')) {
        $content.Replace('</available_skills>', $entry + $nl + '</available_skills>') | Set-Content $reg -NoNewline
        Ok "Registered $p in SKILL_REGISTRY.md"
    } else { Todo "register $p in SKILL_REGISTRY.md by hand" }
}

function Qdrant-Up {
    try {
        $r = Invoke-WebRequest -Uri "http://${qHost}:${qPort}/readyz" -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
        return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 300)
    } catch { return $false }
}

function Want($name) {
    if ($Plugins) { return (",$Plugins," -like "*,$name,*") }
    return (Test-Path (Join-Path $plugDir $name))
}

Write-Host "SumelaOS memory setup  ($root)"

# Enable explicitly-requested plugins (register now; the "add a plugin later" path).
if ($Plugins) {
    Section "Enable plugins"
    foreach ($p in ($Plugins -split ",")) {
        $p = $p.Trim(); if (-not $p) { continue }
        if (Test-Path (Join-Path $plugDir $p)) { Register-Plugin $p }
        else { Todo "Plugin '$p' is not present under $plugDir/ — fetch it first (re-run scripts/bootstrap.ps1, or ask your agent to add it), then re-run this." }
    }
}

# === Qdrant session memory ===
if ((Want "qdrant-session-memory") -and (Test-Path (Join-Path $plugDir "qdrant-session-memory"))) {
    Section "Qdrant session memory"
    $req = Join-Path $plugDir "qdrant-session-memory/requirements.txt"
    if (Test-Path $req) {
        & $py -m pip install -q -r $req *> $null
        if ($LASTEXITCODE -eq 0) { Ok "Python deps installed ($req)" } else { Todo "pip install failed — run: $py -m pip install -r $req" }
    }

    # Ollama: install is platform-specific (guide); model pull is cheap (confirm-and-run).
    if (Get-Command ollama -ErrorAction SilentlyContinue) {
        $modelRoot = ($embedModel -split ":")[0]
        if ((ollama list 2>$null | Out-String) -match [regex]::Escape($modelRoot)) {
            Ok "Ollama model present ($embedModel)"
        } elseif (Confirm-Step "Pull the embedding model '$embedModel' now (~hundreds of MB)?") {
            ollama pull $embedModel
            if ($LASTEXITCODE -eq 0) { Ok "Pulled $embedModel" } else { Todo "ollama pull failed — run: ollama pull $embedModel" }
        } else { Todo "Pull the embedding model: ollama pull $embedModel" }
    } else {
        Todo "Install Ollama (https://ollama.com/download), then: ollama pull $embedModel"
    }

    # Qdrant server: reuse if running; else Docker (confirm); else guide.
    if (Qdrant-Up) {
        Ok "Qdrant reachable at ${qHost}:${qPort}"
    } elseif (Get-Command docker -ErrorAction SilentlyContinue) {
        if (Confirm-Step "Start a local Qdrant via Docker (qdrant/qdrant on :${qPort})?") {
            if ((docker ps -a --format '{{.Names}}' 2>$null) -contains "sumela-qdrant") {
                docker start sumela-qdrant *> $null
            } else {
                docker run -d --name sumela-qdrant -p "${qPort}:6333" -v sumela-qdrant-storage:/qdrant/storage qdrant/qdrant *> $null
            }
            $n = 0; while ($n -lt 20 -and -not (Qdrant-Up)) { $n++; Start-Sleep -Seconds 1 }
            if (Qdrant-Up) { Ok "Qdrant started (container: sumela-qdrant)" } else { Todo "Qdrant container started but not ready yet — re-run once it is up" }
        } else { Todo "Start Qdrant: docker run -d --name sumela-qdrant -p ${qPort}:6333 -v sumela-qdrant-storage:/qdrant/storage qdrant/qdrant" }
    } else {
        Todo "Start Qdrant (install Docker, then): docker run -d -p ${qPort}:6333 -v sumela-qdrant-storage:/qdrant/storage qdrant/qdrant"
    }

    if (Qdrant-Up) {
        $s = Join-Path $plugDir "qdrant-session-memory/scripts"
        if (Test-Path (Join-Path $s "setup-qdrant.py")) {
            & $py (Join-Path $s "setup-qdrant.py") *> $null
            if ($LASTEXITCODE -eq 0) { Ok "Qdrant collections ready" } else { Todo "setup-qdrant.py failed — run: $py $s/setup-qdrant.py" }
        }
        # Seed if the wiki dir exists OR the project configured extra ingest dirs.
        $extraDirs = ""
        if (Test-Path (Join-Path $s "resolve-ingest-dirs.py")) { $extraDirs = (& $py (Join-Path $s "resolve-ingest-dirs.py") 2>$null | Out-String).Trim() }
        if ((Test-Path (Join-Path $s "ingest-wiki-to-qdrant.py")) -and ((Test-Path "docs/second-brain/wiki") -or $extraDirs)) {
            # Three-valued exit (mirrors setup-memory.sh): 0 seeded, 2 seeded with some
            # pages stale, anything else could not run. Collapsing 2 into the failure
            # branch told the operator seeding was skipped when nearly all of it landed.
            $wikiLog = (& $py (Join-Path $s "ingest-wiki-to-qdrant.py") 2>&1 | Out-String)
            switch ($LASTEXITCODE) {
                0 { Ok "Seeded wiki_pages" }
                2 {
                    Info "Seeded wiki_pages, but some pages were skipped — re-run to finish: $py $s/ingest-wiki-to-qdrant.py"
                    ($wikiLog -split "`n" | Select-String '^\[warn\]' | Select-Object -First 5) |
                        ForEach-Object { Write-Host "    $_" }
                }
                default { Info "wiki_pages seeding skipped (will sync on pull)" }
            }
        }
        Info "code_chunks left empty — built automatically on the next pull that touches code (or run now: $py $plugDir/qdrant-session-memory/scripts/ingest-code-to-qdrant.py)"
    }
}

# === Graphify code graph ===
if ((Want "graphify-code-graph") -and (Test-Path (Join-Path $plugDir "graphify-code-graph"))) {
    Section "Graphify code graph"
    $req = Join-Path $plugDir "graphify-code-graph/requirements.txt"
    if (Test-Path $req) { & $py -m pip install -q -r $req *> $null; if ($LASTEXITCODE -eq 0) { Ok "Python deps installed ($req)" } else { Info "pip deps skipped" } }

    if (Get-Command graphify -ErrorAction SilentlyContinue) {
        Ok "graphify CLI present"
    } elseif ((Get-Command uv -ErrorAction SilentlyContinue) -and (Confirm-Step "Install the graphify CLI via 'uv tool install graphifyy'?")) {
        uv tool install graphifyy *> $null
        if ($LASTEXITCODE -eq 0) { Ok "graphify installed (uv)" } else { Todo "install failed — run: uv tool install graphifyy" }
    } elseif ((Get-Command pipx -ErrorAction SilentlyContinue) -and (Confirm-Step "Install the graphify CLI via 'pipx install graphifyy'?")) {
        pipx install graphifyy *> $null
        if ($LASTEXITCODE -eq 0) { Ok "graphify installed (pipx)" } else { Todo "install failed — run: pipx install graphifyy" }
    } else {
        Todo "Install the graphify CLI: uv tool install graphifyy   (or: pipx install graphifyy)"
    }

    if (Get-Command graphify -ErrorAction SilentlyContinue) {
        if (Confirm-Step "Build the code graph now (graphify update . — AST-only, no API key)?") {
            # AST-only build: the `update <path>` subcommand is graphify's no-LLM
            # code-only path and works as the FIRST build too (verified on 0.8.35 with
            # no pre-existing graph.json). Bare `graphify .` attempts SEMANTIC
            # extraction of doc/paper/image files and hard-fails without an LLM API
            # key, which this plugin deliberately does not require (AST-only by design).
            # Output is intentionally NOT suppressed so graphify's own viz/limit
            # warnings reach the user. Success is gated on the ARTIFACT THE QUERY PATH
            # READS — graph.json — not on exit 0 and NOT on graph.html: Tier-2
            # (query-graph.py) reads graph.json, the wiki insights page comes from
            # GRAPH_REPORT.md, and nothing reads the viz. Above graphify's ~5000-node
            # limit the viz is skipped by design; forcing it re-ran clustering and wrote
            # a several-hundred-MB HTML nobody can open.
            graphify update .
            $built = $LASTEXITCODE
            # BOTH terms matter: exit code catches a failed rebuild, the artifact catches
            # a "clean" exit that produced nothing. The artifact alone would report success
            # whenever a stale graph.json survives a failed rebuild.
            if (($built -eq 0) -and (Test-Path "graphify-out/graph.json")) {
                if (Test-Path "graphify-out/graph.html") {
                    Ok "Code graph built (graphify-out/, incl. interactive graph.html)"
                } else {
                    Ok "Code graph built (graphify-out/graph.json — Tier-2 queries ready)"
                    Info "interactive graph.html not built (graphify skips the viz above ~5000 nodes) — nothing in the query path needs it. Want it anyway: `$env:GRAPHIFY_VIZ_NODE_LIMIT=100000000; graphify cluster-only .   (SUMELA_GRAPHIFY_VIZ=1 covers the pull-time sync only, not this script)"
                }
            } else {
                $staleNote = if (Test-Path "graphify-out/graph.json") { "graphify-out/graph.json is stale — left as-is" } else { "no graphify-out/graph.json" }
                Todo "build failed (exit $built; $staleNote) — run: graphify update ."
            }
        } else { Todo "Build the code graph: graphify update ." }
    }
}

Section "Summary"
if ($script:manual -eq 0) {
    Write-Host "Memory stack ready. Nothing left to do by hand." -ForegroundColor Green
} else {
    Write-Host "$($script:manual) item(s) need a one-time manual step — each is listed above with the exact command. Re-run 'pwsh scripts/setup-memory.ps1' afterwards to finish wiring." -ForegroundColor Yellow
}
exit 0
