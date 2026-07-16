# ═══════════════════════════════════════════════════════════════════════════════
# run-local.ps1 — Start all AI OS application services as local Python processes
# No Docker required — all infrastructure runs as cloud services
# ═══════════════════════════════════════════════════════════════════════════════
#
# PREREQUISITES (all free, no Docker):
#   1. uv installed:  powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
#   2. .env file configured with cloud service credentials:
#      cp .env.ibmcloud.example .env  →  fill in IBM_API_KEY, WATSONX_PROJECT_ID,
#      NEO4J_URI/PASSWORD, QDRANT_HOST/API_KEY, REDIS_URL
#   3. Verify cloud services work:
#      uv run python scripts/verify_ibmcloud.py
#
# USAGE
# ─────
#   .\run-local.ps1              # Start all 7 services
#   .\run-local.ps1 -Stop        # Stop all services
#   .\run-local.ps1 -Status      # Show which services are running
#   .\run-local.ps1 -Logs        # Tail all service logs
#   .\run-local.ps1 -Logs enrichment   # Tail one service's log
#
# ═══════════════════════════════════════════════════════════════════════════════

param(
    [switch]$Stop,
    [switch]$Status,
    [string]$Logs = ""
)

$ErrorActionPreference = "Stop"

$SCRIPT_DIR  = Split-Path -Parent $MyInvocation.MyCommand.Definition
$LOG_DIR     = Join-Path $SCRIPT_DIR ".aios-local-logs"
$PID_DIR     = Join-Path $SCRIPT_DIR ".aios-local-pids"

# Service definitions: name → (module, port, working subdir, uvicorn?)
$SERVICES = [ordered]@{
    "memory"     = @{ Module = "memory.main:app";      Port = 8002; SubDir = "services/memory";     IsApi = $true }
    "agents"     = @{ Module = "agents.main";          Port = 0;    SubDir = "services/agents";     IsApi = $false }
    "connectors" = @{ Module = "connectors.main";      Port = 8010; SubDir = "services/connectors"; IsApi = $false }
    "ingestion"  = @{ Module = "ingestion.pipeline";   Port = 0;    SubDir = "services/ingestion";  IsApi = $false }
    "enrichment" = @{ Module = "enrichment.pipeline";  Port = 0;    SubDir = "services/enrichment"; IsApi = $false }
    "interface"  = @{ Module = "interface.main:app";   Port = 8001; SubDir = "services/interface";  IsApi = $true }
    "gateway"    = @{ Module = "gateway.main:app";     Port = 8000; SubDir = "services/gateway";    IsApi = $true }
}

function Write-Header([string]$Text) {
    Write-Host "`n$("═" * 55)" -ForegroundColor Cyan
    Write-Host "  $Text" -ForegroundColor Cyan
    Write-Host "$("═" * 55)" -ForegroundColor Cyan
}
function Write-Ok([string]$Text)   { Write-Host "  ✅ $Text" -ForegroundColor Green  }
function Write-Warn([string]$Text) { Write-Host "  ⚠️  $Text" -ForegroundColor Yellow }
function Write-Fail([string]$Text) { Write-Host "  ❌ $Text" -ForegroundColor Red    }
function Write-Info([string]$Text) { Write-Host "  $Text" }

function Get-PidFile([string]$n) { Join-Path $PID_DIR "$n.pid" }
function Get-LogFile([string]$n) { Join-Path $LOG_DIR "$n.log" }

function Is-Running([string]$n) {
    $f = Get-PidFile $n
    if (-not (Test-Path $f)) { return $false }
    $pid = Get-Content $f -ErrorAction SilentlyContinue
    if (-not $pid) { return $false }
    try { $null = Get-Process -Id ([int]$pid) -ErrorAction Stop; return $true }
    catch { Remove-Item $f -ErrorAction SilentlyContinue; return $false }
}

# ── Stop ──────────────────────────────────────────────────────────────────────

function Stop-AllServices {
    Write-Header "Stopping AI OS Services"
    $stopped = 0
    foreach ($name in $SERVICES.Keys) {
        $f = Get-PidFile $name
        if (Test-Path $f) {
            $pid = Get-Content $f -ErrorAction SilentlyContinue
            if ($pid) {
                try   { Stop-Process -Id ([int]$pid) -Force; Write-Ok "Stopped $name (PID $pid)"; $stopped++ }
                catch { Write-Info "$name was not running (PID $pid stale)" }
            }
            Remove-Item $f -ErrorAction SilentlyContinue
        } else {
            Write-Info "$name — not running"
        }
    }
    Write-Host ""
    Write-Info "Stopped $stopped service(s). Cloud services (Neo4j/Qdrant/Redis) are unaffected."
}

# ── Status ────────────────────────────────────────────────────────────────────

function Show-Status {
    Write-Header "AI OS Service Status"
    foreach ($name in $SERVICES.Keys) {
        $svc = $SERVICES[$name]
        if (Is-Running $name) {
            $pid   = Get-Content (Get-PidFile $name)
            $label = if ($svc.Port -gt 0) { "http://localhost:$($svc.Port)" } else { "background worker" }
            Write-Ok "$name  (PID $pid — $label)"
        } else {
            Write-Warn "$name  — stopped"
        }
    }
    Write-Host ""
    Write-Info "Interface:  http://localhost:8001"
    Write-Info "Gateway:    http://localhost:8000"
    Write-Info "Memory API: http://localhost:8002"
}

# ── Logs ──────────────────────────────────────────────────────────────────────

function Show-Logs([string]$ServiceName) {
    if ($ServiceName) {
        $f = Get-LogFile $ServiceName
        if (-not (Test-Path $f)) { Write-Fail "No log file for '$ServiceName'"; return }
        Write-Header "Tailing: $ServiceName (Ctrl+C to stop)"
        Get-Content $f -Wait -Tail 50
    } else {
        $files = Get-ChildItem $LOG_DIR -Filter "*.log" -ErrorAction SilentlyContinue
        if (-not $files) { Write-Warn "No log files yet — start services first"; return }
        Write-Header "Tailing all service logs (Ctrl+C to stop)"
        # Show recent lines from each then follow
        foreach ($f in $files) {
            $name = $f.BaseName
            Write-Host "`n--- $name ---" -ForegroundColor Cyan
            Get-Content $f.FullName -Tail 5
        }
        Write-Host "`n(Following all logs...)`n" -ForegroundColor DarkGray
        while ($true) {
            foreach ($f in $files) {
                $line = Get-Content $f.FullName -Tail 1 -ErrorAction SilentlyContinue
                if ($line) {
                    $ts = Get-Date -Format "HH:mm:ss"
                    Write-Host "[$ts][$($f.BaseName)] $line"
                }
            }
            Start-Sleep -Milliseconds 500
        }
    }
}

# ── Verify cloud services (quick ping) ────────────────────────────────────────

function Test-CloudServices {
    Write-Header "Verifying Cloud Service Connectivity"

    $env_file = Join-Path $SCRIPT_DIR ".env"
    if (-not (Test-Path $env_file)) {
        Write-Fail ".env file not found"
        Write-Info "Run: Copy-Item .env.ibmcloud.example .env"
        Write-Info "Then fill in your cloud credentials"
        return $false
    }
    Write-Ok ".env file found"

    # Read key env vars from .env
    $envVars = @{}
    Get-Content $env_file | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $parts = $line.Split("=", 2)
            $envVars[$parts[0].Trim()] = $parts[1].Trim()
        }
    }

    $ok = $true

    # Check IBM_API_KEY
    if (-not $envVars["IBM_API_KEY"]) {
        Write-Fail "IBM_API_KEY is not set in .env"
        Write-Info "Get it: cloud.ibm.com → Profile icon → API keys → Create"
        $ok = $false
    } else {
        Write-Ok "IBM_API_KEY is set"
    }

    # Check WATSONX_PROJECT_ID
    if (-not $envVars["WATSONX_PROJECT_ID"]) {
        Write-Fail "WATSONX_PROJECT_ID is not set in .env"
        Write-Info "Get it: dataplatform.cloud.ibm.com → your project → Manage → General"
        $ok = $false
    } else {
        Write-Ok "WATSONX_PROJECT_ID is set"
    }

    # Check NEO4J_URI + PASSWORD
    $neo4jUri = $envVars["NEO4J_URI"]
    $neo4jPw  = $envVars["NEO4J_PASSWORD"]
    if (-not $neo4jUri -or $neo4jUri -match "xxxxxxxx" -or -not $neo4jPw) {
        Write-Fail "NEO4J_URI or NEO4J_PASSWORD not set in .env"
        Write-Info "Get it: cloud.neo4j.com → your instance → Connect"
        $ok = $false
    } else {
        Write-Ok "Neo4j AuraDB credentials set"
    }

    # Check QDRANT_HOST + API_KEY
    $qdrantHost = $envVars["QDRANT_HOST"]
    $qdrantKey  = $envVars["QDRANT_API_KEY"]
    if (-not $qdrantHost -or $qdrantHost -match "xxxxxxxx" -or -not $qdrantKey) {
        Write-Fail "QDRANT_HOST or QDRANT_API_KEY not set in .env"
        Write-Info "Get it: cloud.qdrant.io → your cluster → Dashboard → Connection details + API Keys"
        $ok = $false
    } else {
        Write-Ok "Qdrant Cloud credentials set"
    }

    # Check REDIS_URL
    $redisUrl = $envVars["REDIS_URL"]
    if (-not $redisUrl -or $redisUrl -match "yourpassword" -or $redisUrl -match "host\.databases") {
        Write-Fail "REDIS_URL not configured in .env"
        Write-Info "Get it: cloud.ibm.com → Databases for Redis → Credentials → JSON → rediss block"
        $ok = $false
    } else {
        Write-Ok "REDIS_URL is set"
    }

    return $ok
}

# ── Start ─────────────────────────────────────────────────────────────────────

function Start-AllServices {
    Write-Header "AI OS for Companies — No-Docker Start"

    # Check uv
    $uvCmd = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uvCmd) {
        Write-Fail "uv is not installed"
        Write-Info 'Install: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"'
        exit 1
    }
    $uvVer = & uv --version 2>&1
    Write-Ok "uv found: $uvVer"

    # Verify cloud services
    $cloudOk = Test-CloudServices
    if (-not $cloudOk) {
        Write-Host ""
        Write-Fail "Cloud services not configured. Fill in .env and re-run."
        Write-Info "Run verification: uv run python scripts\verify_ibmcloud.py"
        exit 1
    }

    # Create directories
    New-Item -ItemType Directory -Force -Path $LOG_DIR | Out-Null
    New-Item -ItemType Directory -Force -Path $PID_DIR | Out-Null

    # Install workspace dependencies
    Write-Host ""
    Write-Info "Installing workspace dependencies..."
    Push-Location $SCRIPT_DIR
    try {
        $output = & uv sync --all-packages 2>&1 | Select-Object -Last 3
        $output | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
        Write-Ok "Dependencies installed"
    } finally {
        Pop-Location
    }

    # Start services
    Write-Host ""
    Write-Info "Starting application services..."
    Write-Host ""

    foreach ($name in $SERVICES.Keys) {
        $svc    = $SERVICES[$name]
        $workDir = Join-Path $SCRIPT_DIR $svc.SubDir
        $logFile = Get-LogFile $name
        $pidFile = Get-PidFile $name

        if (Is-Running $name) {
            Write-Warn "$name already running — skipping"
            continue
        }

        if ($svc.IsApi) {
            $cmd = "uv run uvicorn $($svc.Module) --host 0.0.0.0 --port $($svc.Port)"
        } else {
            $cmd = "uv run python -m $($svc.Module)"
        }

        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName               = "powershell.exe"
        $startInfo.Arguments              = "-NoProfile -NonInteractive -Command `"Set-Location '$workDir'; $cmd`" *> '$logFile' 2>&1"
        $startInfo.WindowStyle            = [System.Diagnostics.ProcessWindowStyle]::Hidden
        $startInfo.CreateNoWindow         = $true
        $startInfo.UseShellExecute        = $true
        $startInfo.WorkingDirectory       = $workDir

        try {
            $proc = [System.Diagnostics.Process]::Start($startInfo)
            $proc.Id | Out-File $pidFile -Encoding ASCII
            $label = if ($svc.Port -gt 0) { "→ http://localhost:$($svc.Port)" } else { "→ background worker" }
            Write-Ok "Started $name  (PID $($proc.Id))  $label"
            Start-Sleep -Milliseconds 400   # stagger startup
        } catch {
            Write-Fail "Failed to start $name : $_"
        }
    }

    # Wait for HTTP services
    Write-Host ""
    Write-Info "Waiting for HTTP services to become ready..."
    $httpServices = @(
        @{ Name = "memory";    Port = 8002 }
        @{ Name = "interface"; Port = 8001 }
        @{ Name = "gateway";   Port = 8000 }
    )
    foreach ($ep in $httpServices) {
        $ready = $false
        for ($i = 0; $i -lt 20; $i++) {
            Start-Sleep -Seconds 1
            try {
                $null = Invoke-WebRequest "http://localhost:$($ep.Port)/health" -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
                $ready = $true; break
            } catch { }
        }
        if ($ready) { Write-Ok "$($ep.Name) ready → http://localhost:$($ep.Port)" }
        else         { Write-Warn "$($ep.Name) not ready yet — check .aios-local-logs\$($ep.Name).log" }
    }

    # Summary
    Write-Header "AI OS is Running!"
    Write-Host ""
    Write-Info "Chat interface:    http://localhost:8001"
    Write-Info "API gateway:       http://localhost:8000"
    Write-Info "Memory API:        http://localhost:8002"
    Write-Host ""
    Write-Info "Ingest data (first time):"
    Write-Info "  uv run python scripts\backfill.py --days 30"
    Write-Host ""
    Write-Info "Send a query:"
    Write-Host '  Invoke-RestMethod http://localhost:8001/v1/chat -Method Post -Headers @{"Authorization"="Bearer test"} -ContentType "application/json" -Body '"'"'{"query":"What was worked on this week?","stream":false}'"'" -ForegroundColor DarkGray
    Write-Host ""
    Write-Info "Commands: .\run-local.ps1 -Stop | -Status | -Logs"
    Write-Info "Log files: $LOG_DIR"
}

# ── Entry point ───────────────────────────────────────────────────────────────

if ($Stop)              { Stop-AllServices; exit 0 }
if ($Status)            { Show-Status;      exit 0 }
if ($PSBoundParameters.ContainsKey("Logs")) { Show-Logs $Logs; exit 0 }

Start-AllServices
