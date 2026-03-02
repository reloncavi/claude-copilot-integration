#!/usr/bin/env bash
# =============================================================================
# docker-setup.sh — Levanta Redis y valida el entorno completo
# Proyecto: Claude + Copilot Integration
# Uso: bash docker-setup.sh [--reset] [--no-bootstrap]
# =============================================================================

set -euo pipefail

# --- Colores -----------------------------------------------------------------
GREEN='\033[92m'; YELLOW='\033[93m'; RED='\033[91m'
BLUE='\033[94m';  BOLD='\033[1m';    RESET='\033[0m'

ok()     { echo -e "  ${GREEN}✅ $*${RESET}"; }
warn()   { echo -e "  ${YELLOW}⚠️  $*${RESET}"; }
err()    { echo -e "  ${RED}❌ $*${RESET}"; }
info()   { echo -e "  ${BLUE}ℹ️  $*${RESET}"; }
header() { echo -e "\n${BOLD}${BLUE}──────────────────────────────────────────${RESET}"; \
           echo -e "${BOLD}${BLUE}  $*${RESET}"; \
           echo -e "${BOLD}${BLUE}──────────────────────────────────────────${RESET}"; }

# --- Flags -------------------------------------------------------------------
RESET_DATA=false
SKIP_BOOTSTRAP=false
for arg in "$@"; do
  case $arg in
    --reset)         RESET_DATA=true ;;
    --no-bootstrap)  SKIP_BOOTSTRAP=true ;;
  esac
done

# =============================================================================
# 1. VERIFICAR DOCKER
# =============================================================================
header "1. Docker"

if ! command -v docker &> /dev/null; then
  err "Docker no encontrado."
  info "Instala Docker Desktop desde: https://www.docker.com/products/docker-desktop"
  exit 1
fi

DOCKER_VERSION=$(docker --version | awk '{print $3}' | tr -d ',')
ok "Docker $DOCKER_VERSION"

if ! docker info &> /dev/null; then
  err "Docker daemon no está corriendo. Inicia Docker Desktop."
  exit 1
fi
ok "Docker daemon activo"

# =============================================================================
# 2. REDIS
# =============================================================================
header "2. Redis"

CONTAINER_NAME="claude-copilot-redis"
REDIS_PORT=6379

# Si existe y --reset, eliminar
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
  if [ "$RESET_DATA" = true ]; then
    warn "Eliminando contenedor Redis existente (--reset)..."
    docker stop "$CONTAINER_NAME" &> /dev/null || true
    docker rm "$CONTAINER_NAME" &> /dev/null || true
  fi
fi

# Levantar si no está corriendo
if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
  info "Iniciando contenedor Redis..."
  docker run -d \
    --name "$CONTAINER_NAME" \
    -p "${REDIS_PORT}:6379" \
    --restart unless-stopped \
    -v claude-copilot-redis-data:/data \
    redis:7-alpine \
    redis-server --appendonly yes --maxmemory 256mb --maxmemory-policy allkeys-lru \
    > /dev/null

  # Esperar a que Redis esté listo
  echo -n "  Esperando Redis"
  for i in $(seq 1 10); do
    if docker exec "$CONTAINER_NAME" redis-cli ping &> /dev/null; then
      echo ""
      break
    fi
    echo -n "."
    sleep 1
  done
fi

# Verificar que responde
if docker exec "$CONTAINER_NAME" redis-cli ping | grep -q "PONG"; then
  REDIS_VERSION=$(docker exec "$CONTAINER_NAME" redis-cli info server | grep redis_version | cut -d: -f2 | tr -d '\r')
  ok "Redis v${REDIS_VERSION} corriendo en localhost:${REDIS_PORT}"
  ok "Datos persistentes en volumen: claude-copilot-redis-data"
else
  err "Redis no responde. Revisa los logs: docker logs $CONTAINER_NAME"
  exit 1
fi

# =============================================================================
# 3. ARCHIVO .env
# =============================================================================
header "3. Variables de Entorno (.env)"

ENV_FILE=".env"

if [ ! -f "$ENV_FILE" ]; then
  info "Creando .env de ejemplo..."
  cat > "$ENV_FILE" << 'EOF'
# =============================================================================
# .env — Variables de entorno locales
# ⚠️  NUNCA commitear este archivo (ya está en .gitignore)
# =============================================================================

# --- REQUERIDA ---------------------------------------------------------------
ANTHROPIC_API_KEY=sk-ant-...        # Obtener en: console.anthropic.com/keys

# --- MODELOS (opcionales, ya tienen defaults) --------------------------------
DESIGN_MODEL=claude-opus-4-5        # Para design_feature() — máxima calidad
REVIEW_MODEL=claude-haiku-4-5-20251001  # Para review_code() — económico

# --- TOKENS (opcionales) ----------------------------------------------------
MAX_TOKENS_DESIGN=1500
MAX_TOKENS_REVIEW=800

# --- REDIS (levantar con: bash docker-setup.sh) ------------------------------
REDIS_URL=redis://localhost:6379/0

# --- PATHS ------------------------------------------------------------------
SYSTEM_PROMPT_PATH=system_prompt.txt

# --- LOGGING ----------------------------------------------------------------
LOG_LEVEL=INFO                      # DEBUG | INFO | WARNING | ERROR

# --- DATABASE (descomentar cuando tengas PostgreSQL) ------------------------
# DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/dbname
EOF
  warn ".env creado con valores de ejemplo. Edita ANTHROPIC_API_KEY antes de continuar."
else
  ok ".env ya existe"

  # Verificar que REDIS_URL apunta a localhost
  if grep -q "REDIS_URL" "$ENV_FILE"; then
    if grep "REDIS_URL" "$ENV_FILE" | grep -q "localhost"; then
      ok "REDIS_URL apunta a localhost ✓"
    else
      warn "REDIS_URL no apunta a localhost — actualizar si es necesario"
    fi
  else
    warn "REDIS_URL no está en .env — agregando..."
    echo "" >> "$ENV_FILE"
    echo "REDIS_URL=redis://localhost:${REDIS_PORT}/0" >> "$ENV_FILE"
    ok "REDIS_URL=redis://localhost:${REDIS_PORT}/0 agregado"
  fi
fi

# =============================================================================
# 4. ESTRUCTURA DE DIRECTORIOS
# =============================================================================
header "4. Estructura del Proyecto"

# Crear directorios necesarios
dirs=(
  ".github/workflows"
  "app/core"
  "app/modules"
  "tools"
  "tests"
)

for d in "${dirs[@]}"; do
  if [ ! -d "$d" ]; then
    mkdir -p "$d"
    ok "Directorio creado: $d"
  else
    ok "Directorio existe: $d"
  fi
done

# Copiar archivos a sus rutas correctas
copy_if_exists() {
  local src="$1"
  local dst="$2"
  if [ -f "$src" ] && [ ! -f "$dst" ]; then
    cp "$src" "$dst"
    ok "Copiado: $src → $dst"
  elif [ -f "$dst" ]; then
    ok "Ya existe: $dst"
  else
    warn "No encontrado: $src"
  fi
}

copy_if_exists "repository.py"        "app/core/repository.py"
copy_if_exists "ide_injector.py"      "tools/ide_injector.py"
copy_if_exists "claude-review.yml"    ".github/workflows/claude-review.yml"

# .gitignore
if [ ! -f ".gitignore" ]; then
  cat > ".gitignore" << 'EOF'
# Entorno local
.env
*.bak.*
__pycache__/
*.pyc
.pytest_cache/

# IDE
.vscode/
.idea/
*.iml

# Python
venv/
.venv/
dist/
build/
*.egg-info/

# Logs y métricas temporales
/tmp/*.diff
/tmp/*.json
/tmp/*.log
EOF
  ok ".gitignore creado"
else
  ok ".gitignore ya existe"
fi

# =============================================================================
# 5. VERIFICAR GITHUB SECRETS (instrucciones)
# =============================================================================
header "5. GitHub Secrets (para CI/CD)"

info "Para que claude-review.yml funcione en GitHub Actions, agrega este secret:"
echo ""
echo -e "  ${BOLD}Repositorio → Settings → Secrets → Actions → New repository secret${RESET}"
echo ""
echo -e "  ${BOLD}Name:${RESET}  ANTHROPIC_API_KEY"
echo -e "  ${BOLD}Value:${RESET} tu clave sk-ant-..."
echo ""
info "GITHUB_TOKEN se genera automáticamente — no necesitas configurarlo."

# =============================================================================
# 6. BOOTSTRAP
# =============================================================================
if [ "$SKIP_BOOTSTRAP" = false ]; then
  header "6. Validando Entorno (bootstrap.py)"

  if [ -f "bootstrap.py" ]; then
    # Cargar .env antes de ejecutar bootstrap
    if [ -f ".env" ]; then
      set -a
      # shellcheck disable=SC1090
      source <(grep -v '^#' .env | grep -v '^$') 2>/dev/null || true
      set +a
    fi

    PYTHONIOENCODING=utf-8 python bootstrap.py --skip-redis 2>&1 | tail -20 || true
  else
    warn "bootstrap.py no encontrado — ejecuta manualmente cuando esté disponible"
  fi
else
  info "Bootstrap omitido (--no-bootstrap)"
fi

# =============================================================================
# RESUMEN FINAL
# =============================================================================
header "✅ Setup Completado"

echo ""
echo -e "${BOLD}Comandos útiles:${RESET}"
echo ""
echo -e "  ${GREEN}# Verificar Redis${RESET}"
echo -e "  docker exec claude-copilot-redis redis-cli ping"
echo ""
echo -e "  ${GREEN}# Ver logs de Redis${RESET}"
echo -e "  docker logs claude-copilot-redis --tail 20"
echo ""
echo -e "  ${GREEN}# Probar el ciclo completo (cuando tengas créditos)${RESET}"
echo -e "  python bootstrap.py --full-test"
echo ""
echo -e "  ${GREEN}# Diseñar una feature${RESET}"
echo -e '  python orchestrator.py design "Crear endpoint de login con JWT"'
echo ""
echo -e "  ${GREEN}# Revisar un diff${RESET}"
echo -e "  git diff HEAD~1 > /tmp/last.diff && python orchestrator.py review /tmp/last.diff"
echo ""
echo -e "  ${GREEN}# Detener Redis${RESET}"
echo -e "  docker stop claude-copilot-redis"
echo ""
