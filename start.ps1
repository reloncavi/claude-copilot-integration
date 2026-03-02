# =============================================================================
# start.ps1 — Arranque del entorno Claude + Copilot Integration
# Uso: .\start.ps1
#      .\start.ps1 --full-test     (valida ciclo completo con tokens reales)
#      .\start.ps1 --no-redis      (skip Docker/Redis)
#      .\start.ps1 --no-bootstrap  (solo levanta servicios, sin validación)
# =============================================================================

param(
    [switch]$FullTest,
    [switch]$NoRedis,
    [switch]$NoBootstrap,
    [switch]$Help
)

# --- Colores -----------------------------------------------------------------
function Ok($msg)     { Write-Host "  ✅ $msg" -ForegroundColor Green }
function Warn($msg)   { Write-Host "  ⚠️  $msg" -ForegroundColor Yellow }
function Err($msg)    { Write-Host "  ❌ $msg" -ForegroundColor Red }
function Info($msg)   { Write-Host "  ℹ️  $msg" -ForegroundColor Cyan }
function Header($msg) {
    Write-Host ""
    Write-Host "  ──────────────────────────────────────────" -ForegroundColor Blue
    Write-Host "  $msg" -ForegroundColor Blue
    Write-Host "  ──────────────────────────────────────────" -ForegroundColor Blue
}

# --- Ayuda -------------------------------------------------------------------
if ($Help) {
    Write-Host @"

  start.ps1 — Arranque del entorno Claude + Copilot Integration

  Uso:
    .\start.ps1                 Arranque completo (recomendado)
    .\start.ps1 --full-test     Incluye test E2E con tokens reales
    .\start.ps1 --no-redis      Sin Docker/Redis (cache en memoria)
    .\start.ps1 --no-bootstrap  Solo levanta servicios, sin validar
    .\start.ps1 --help          Esta ayuda

"@
    exit 0
}

# =============================================================================
# HEADER
# =============================================================================
Clear-Host
Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Blue
Write-Host "  Claude + Copilot Integration — Arranque del Entorno" -ForegroundColor Blue
Write-Host "  $(Get-Date -Format 'dddd dd/MM/yyyy HH:mm')" -ForegroundColor DarkGray
Write-Host "  ============================================================" -ForegroundColor Blue

# =============================================================================
# 1. DIRECTORIO DEL PROYECTO
# =============================================================================
Header "1. Directorio del Proyecto"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir
Ok "Directorio: $ScriptDir"

# =============================================================================
# 2. CARGAR VARIABLES DE ENTORNO (.env)
# =============================================================================
Header "2. Variables de Entorno"

$EnvFile = Join-Path $ScriptDir ".env"

if (Test-Path $EnvFile) {
    $loaded = 0
    $missing = @()

    Get-Content $EnvFile | Where-Object {
        $_ -notmatch '^\s*#' -and $_.Trim() -ne ''
    } | ForEach-Object {
        $parts = $_ -split '=', 2
        if ($parts.Length -eq 2) {
            $key   = $parts[0].Trim()
            $value = $parts[1].Trim()
            [System.Environment]::SetEnvironmentVariable($key, $value, 'Process')
            $loaded++
        }
    }
    Ok ".env cargado ($loaded variables)"
} else {
    Warn ".env no encontrado — usando variables del sistema"
}

# Verificar variables críticas
$ApiKey = [System.Environment]::GetEnvironmentVariable('ANTHROPIC_API_KEY', 'Process')
if ($ApiKey -and $ApiKey -ne 'sk-ant-...') {
    $masked = $ApiKey.Substring(0, [Math]::Min(8, $ApiKey.Length)) + '...' + $ApiKey.Substring([Math]::Max(0, $ApiKey.Length - 4))
    Ok "ANTHROPIC_API_KEY = $masked"
} else {
    Err "ANTHROPIC_API_KEY no definida o es el valor de ejemplo"
    Warn "Edita el archivo .env y define ANTHROPIC_API_KEY=sk-ant-..."
    Write-Host ""
    $continue = Read-Host "  ¿Continuar de todas formas? (s/N)"
    if ($continue -ne 's' -and $continue -ne 'S') {
        Write-Host ""
        Write-Host "  Abre .env y agrega tu API key, luego vuelve a ejecutar .\start.ps1" -ForegroundColor Yellow
        exit 1
    }
}

# Mostrar estado de variables opcionales
$optionals = @{
    'REDIS_URL'        = 'redis://localhost:6379/0'
    'METRICS_ENABLED'  = 'false'
    'LOG_LEVEL'        = 'INFO'
    'DESIGN_MODEL'     = 'claude-opus-4-5'
    'REVIEW_MODEL'     = 'claude-haiku-4-5-20251001'
}
foreach ($var in $optionals.Keys) {
    $val = [System.Environment]::GetEnvironmentVariable($var, 'Process')
    if ($val) {
        Ok "${var} = $val"
    } else {
        Warn "${var} no definida — usando: $($optionals[$var])"
    }
}

# =============================================================================
# 3. REDIS (Docker)
# =============================================================================
Header "3. Redis (Cache del System Prompt)"

if ($NoRedis) {
    Warn "Redis omitido (--no-redis). Se usará cache en memoria."
    Warn "Esto reduce el ahorro de tokens entre sesiones (~40% menos eficiente)."
} else {
    $DockerAvailable = $null -ne (Get-Command docker -ErrorAction SilentlyContinue)

    if (-not $DockerAvailable) {
        Warn "Docker no encontrado — Redis usará cache en memoria"
        Info "Instala Docker Desktop: https://www.docker.com/products/docker-desktop"
    } else {
        $DockerRunning = $false
        try {
            docker info 2>$null | Out-Null
            $DockerRunning = $true
        } catch {}

        if (-not $DockerRunning) {
            Warn "Docker no está corriendo — inicia Docker Desktop"
            Info "Redis usará cache en memoria hasta que Docker esté activo"
        } else {
            $ContainerName = "claude-copilot-redis"

            # Verificar si el contenedor existe
            $exists = docker ps -a --format "{{.Names}}" 2>$null | Where-Object { $_ -eq $ContainerName }

            if ($exists) {
                # Intentar iniciar si está detenido
                docker start $ContainerName 2>$null | Out-Null
                Start-Sleep -Milliseconds 500

                # Verificar que responde
                $ping = docker exec $ContainerName redis-cli ping 2>$null
                if ($ping -eq "PONG") {
                    $version = docker exec $ContainerName redis-cli info server 2>$null |
                               Select-String "redis_version" |
                               ForEach-Object { $_ -replace "redis_version:", "" } |
                               ForEach-Object { $_.Trim() }
                    Ok "Redis v$version corriendo en localhost:6379"
                } else {
                    Warn "Redis no responde — cache en memoria activo"
                }
            } else {
                # Crear contenedor nuevo
                Info "Creando contenedor Redis por primera vez..."
                docker run -d `
                    --name $ContainerName `
                    -p 6379:6379 `
                    --restart unless-stopped `
                    -v claude-copilot-redis-data:/data `
                    redis:7-alpine `
                    redis-server --appendonly yes --maxmemory 256mb --maxmemory-policy allkeys-lru `
                    2>$null | Out-Null

                Start-Sleep -Seconds 2
                $ping = docker exec $ContainerName redis-cli ping 2>$null
                if ($ping -eq "PONG") {
                    Ok "Redis iniciado y corriendo en localhost:6379"
                } else {
                    Warn "Redis no responde aún — puede tardar unos segundos"
                }
            }
        }
    }
}

# =============================================================================
# 4. MÉTRICAS (Grafana Pushgateway — opcional)
# =============================================================================
$MetricsEnabled = [System.Environment]::GetEnvironmentVariable('METRICS_ENABLED', 'Process')

if ($MetricsEnabled -eq 'true') {
    Header "4. Métricas (Grafana Pushgateway)"

    $GrafanaUrl = [System.Environment]::GetEnvironmentVariable('METRICS_GRAFANA_URL', 'Process')
    if (-not $GrafanaUrl) { $GrafanaUrl = 'http://localhost:9091' }

    try {
        $response = Invoke-WebRequest -Uri "$GrafanaUrl/-/ready" -TimeoutSec 2 -ErrorAction Stop
        Ok "Grafana Pushgateway activo en $GrafanaUrl"
    } catch {
        Warn "Grafana Pushgateway no disponible en $GrafanaUrl"
        Info "Para activarlo: docker run -d -p 9091:9091 prom/pushgateway"
        Info "Las métricas se guardarán en /tmp/token_metrics.json como fallback"
    }
} else {
    Header "4. Métricas"
    Info "Métricas deshabilitadas (METRICS_ENABLED != true)"
    Info "Para activar: agrega METRICS_ENABLED=true en .env"
}

# =============================================================================
# 5. ARCHIVOS DEL PROYECTO
# =============================================================================
Header "5. Archivos del Proyecto"

$required = @{
    "orchestrator.py"   = "Orquestador principal"
    "ide_injector.py"   = "Inyector de pseudocódigo"
    "system_prompt.txt" = "System prompt del proyecto"
}

$optional = @{
    "metrics_exporter.py"                    = "Exportador de métricas"
    "repository.py"                          = "Repository Pattern base"
    "bootstrap.py"                           = "Validador de entorno"
    ".github/workflows/claude-review.yml"    = "GitHub Action para PRs"
}

$allRequired = $true
foreach ($file in $required.Keys) {
    if (Test-Path (Join-Path $ScriptDir $file)) {
        $size = (Get-Item (Join-Path $ScriptDir $file)).Length
        Ok "$file ($size bytes) — $($required[$file])"
    } else {
        Err "$file NO encontrado — $($required[$file])"
        $allRequired = $false
    }
}

foreach ($file in $optional.Keys) {
    if (Test-Path (Join-Path $ScriptDir $file)) {
        Ok "$file — $($optional[$file])"
    } else {
        Warn "$file no encontrado — $($optional[$file])"
    }
}

if (-not $allRequired) {
    Err "Faltan archivos críticos. Descárgalos desde el proyecto antes de continuar."
    exit 1
}

# =============================================================================
# 6. BOOTSTRAP (validación completa)
# =============================================================================
if (-not $NoBootstrap) {
    Header "6. Validación del Entorno (bootstrap.py)"

    if (Test-Path (Join-Path $ScriptDir "bootstrap.py")) {
        $bootstrapArgs = @()
        if ($FullTest)  { $bootstrapArgs += "--full-test" }
        if ($NoRedis)   { $bootstrapArgs += "--skip-redis" }

        python bootstrap.py @bootstrapArgs
    } else {
        Warn "bootstrap.py no encontrado — skip de validación"
    }
} else {
    Header "6. Validación"
    Info "Bootstrap omitido (--no-bootstrap)"
}

# =============================================================================
# RESUMEN FINAL + COMANDOS RÁPIDOS
# =============================================================================
Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host "  🚀 Entorno listo. Comandos disponibles:" -ForegroundColor Green
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  # Diseñar una feature (Claude genera pseudocódigo)" -ForegroundColor DarkGray
Write-Host '  python orchestrator.py design "descripcion de tu feature"' -ForegroundColor White
Write-Host ""
Write-Host "  # Diseñar e inyectar directamente en un archivo" -ForegroundColor DarkGray
Write-Host '  python orchestrator.py design "descripcion" app/modules/orders/service.py' -ForegroundColor White
Write-Host ""
Write-Host "  # Revisar el último commit" -ForegroundColor DarkGray
Write-Host "  git diff HEAD~1 > /tmp/last.diff && python orchestrator.py review /tmp/last.diff" -ForegroundColor White
Write-Host ""
Write-Host "  # Revisar rama actual vs main" -ForegroundColor DarkGray
Write-Host "  python orchestrator.py review origin/main" -ForegroundColor White
Write-Host ""
Write-Host "  # Ver bloques @claude inyectados en un archivo" -ForegroundColor DarkGray
Write-Host "  python ide_injector.py list --file app/modules/orders/service.py" -ForegroundColor White
Write-Host ""
Write-Host "  # Ver métricas de tokens acumuladas" -ForegroundColor DarkGray
Write-Host "  python metrics_exporter.py --source /tmp/token_metrics.json --summary" -ForegroundColor White
Write-Host ""
Write-Host "  # Detener Redis al terminar el día" -ForegroundColor DarkGray
Write-Host "  docker stop claude-copilot-redis" -ForegroundColor White
Write-Host ""
