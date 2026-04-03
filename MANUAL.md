# Manual de Usuario — Claude Copilot

## ¿Qué es?

Pipeline de dos fases que une Claude (arquitecto/revisor) con GitHub Copilot (implementador):

1. **Design** → Claude Opus recibe un requerimiento y devuelve pseudocódigo como comentarios Python que Copilot expande en código real.
2. **Review** → Claude Haiku recibe un git diff y devuelve un JSON con los problemas encontrados.

---

## Requisitos previos

- `ANTHROPIC_API_KEY` configurada en `~/Dev/claude/Claude-Copilot/.env`
- Shell con `source ~/.bashrc` ejecutado al menos una vez
- Docker Desktop (opcional — solo si quieres Redis para cache local)

---

## Comando global: `cc`

Invocable desde **cualquier proyecto o carpeta**:

```bash
cc <comando> [argumentos] [--export-metrics]
```

---

## Comandos principales

### `design` — Diseñar una feature

Genera pseudocódigo comentado listo para que Copilot lo implemente.

```bash
# Imprimir pseudocódigo en pantalla
cc design "Crear endpoint REST de registro de usuario"

# Inyectar directamente en un archivo
cc design "Crear endpoint REST de registro de usuario" app/service.py
```

**Salida (stdout):** bloque de comentarios Python delimitado por marcadores `@claude`.

---

### `review` — Revisar un diff

Analiza cambios y devuelve un JSON con los issues encontrados.

```bash
# Revisar cambios respecto a origin/main (default)
cc review

# Revisar contra otra rama
cc review origin/develop

# Revisar desde un archivo .diff
cc review /tmp/mis-cambios.diff
```

**Salida (stdout):** JSON array de issues.

```json
[
  {
    "issue": "Falta validación del campo email",
    "severity": "critical",
    "suggestion": "Agregar validator con regex o pydantic EmailStr"
  },
  {
    "issue": "Variable `data` sin tipo anotado",
    "severity": "warning",
    "suggestion": "Anotar como dict[str, Any]"
  }
]
```

| Severity | Descripción |
|---|---|
| `critical` | Bloquea merge — bug, seguridad, error de lógica |
| `warning` | Deuda técnica importante |
| `suggestion` | Mejora opcional |

---

### `review-batch` — Revisión en lote

Revisa todos los archivos cambiados en el working tree, agrupando hasta 3 diffs por llamada API. Los grupos se procesan en paralelo.

```bash
cc review-batch
```

Útil antes de hacer un commit o PR para tener una visión completa.

---

### `invalidate-cache` — Limpiar caché del system prompt

El system prompt se cachea automáticamente en los servidores de Anthropic (prompt caching nativo, TTL ~5 min) y localmente en memoria o Redis. Úsalo si modificas `system_prompt.txt` y quieres forzar la recarga inmediata.

```bash
cc invalidate-cache
```

---

### `report` — Estado de revisiones de agentes autónomos

Consulta la API de GitHub para obtener un resumen de cómo van las revisiones automáticas de Claude en los PRs del repositorio.

```bash
# Reporte formateado en consola
cc report

# Reporte en JSON (para scripts o integración con otras herramientas)
cc report --json
```

**Requiere:**
- `GITHUB_TOKEN` (o `GH_TOKEN`) — token con permisos `read:repo` y `read:actions`
- `GITHUB_REPOSITORY` — en formato `owner/repo`

**Salida de ejemplo:**

```
==============================================================
  📊 Estado de Revisiones — Agentes Autónomos
  Repositorio: reloncavi/claude-copilot-integration
  Generado:    2026-04-03T19:49:00+00:00
==============================================================

⚙️  WORKFLOW (claude-review.yml)
   Total ejecuciones:  12
   ✅  Exitosas:       10
   ❌  Fallidas:        2
   🔄  En progreso:     0

📈 REVISIONES DE CÓDIGO (últimos 20 PRs)
   PRs con revisión:    8
   ✅  Sin issues:       5
   🔴  Con críticos:     1
   🟡  Con warnings:     2
   🔵  Con sugerencias:  3
   ⏭️   Omitidas:         1
```

También se puede ejecutar automáticamente cada lunes via el workflow `.github/workflows/review-report.yml`, que publica el reporte en el GitHub Actions Summary.

---

## IDE Injector (`ide_injector.py`)

Gestión avanzada de bloques de pseudocódigo inyectados en archivos.

```bash
# Inyectar desde stdin (modos: append, prepend, replace, at_line, after_class, after_func)
cc design "..." | python ide_injector.py inject --file app/service.py --mode append --tag login

# Listar bloques inyectados en un archivo
python ide_injector.py list --file app/service.py

# Eliminar un bloque por tag
python ide_injector.py remove --file app/service.py --tag login

# Restaurar archivo desde backup automático
python ide_injector.py restore --file app/service.py

# Inyectar múltiples módulos desde JSON
python ide_injector.py batch --json module_design.json
```

### Modos de inyección

| Modo | Dónde inserta |
|---|---|
| `append` | Al final del archivo |
| `prepend` | Al inicio del archivo |
| `replace` | Reemplaza un bloque `@claude` existente con el mismo tag |
| `at_line` | En una línea específica (`--line N`) |
| `after_class` | Después de una clase (`--anchor NombreClase`) |
| `after_func` | Después de una función (`--anchor nombre_func`) |

---

## Flujo de trabajo típico

```bash
# 1. Entrar al proyecto
cd ~/Dev/mi-proyecto

# 2. Diseñar la feature
cc design "Crear servicio de autenticación con JWT" app/auth/service.py

# 3. Copilot expande el pseudocódigo en código real (en el IDE)

# 4. Revisar los cambios antes del commit
cc review

# 5. Corregir los issues críticos, luego hacer commit
git add . && git commit -m "feat: auth service"

# 6. Abrir PR en GitHub → Claude revisa automáticamente y comenta
#    Si hay issues críticos 🔴 el check bloquea el merge
cc review origin/main  # también disponible localmente
```

---

## Flags adicionales

```bash
# Exportar métricas de tokens a /tmp/token_metrics.json (formato Grafana)
cc design "..." --export-metrics
cc review --export-metrics
```

---

## Variables de entorno clave (en `.env`)

| Variable | Default | Descripción |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | **Requerida** |
| `DESIGN_MODEL` | `claude-opus-4-6` | Modelo para design |
| `REVIEW_MODEL` | `claude-haiku-4-5-20251001` | Modelo para review |
| `MAX_TOKENS_DESIGN` | `1500` | Límite de tokens por diseño |
| `MAX_TOKENS_REVIEW` | `800` | Límite de tokens por revisión |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

---

## Archivos relevantes

| Archivo | Rol |
|---|---|
| `~/bin/cc` | Wrapper global — punto de entrada desde cualquier carpeta |
| `~/.bashrc` | Añade `~/bin` al PATH |
| `~/Dev/claude/Claude-Copilot/.env` | API key y configuración |
| `~/Dev/claude/Claude-Copilot/system_prompt.txt` | Instrucciones de Claude (no en repo) |
| `~/Dev/claude/Claude-Copilot/orchestrator.py` | Motor principal |
| `~/Dev/claude/Claude-Copilot/ide_injector.py` | Gestión de bloques inyectados |
| `.github/workflows/claude-review.yml` | GitHub Action — revisión automática en cada PR |

---

## Solución de problemas

| Síntoma | Causa probable | Solución |
|---|---|---|
| `command not found: cc` | PATH no actualizado | `source ~/.bashrc` |
| `ANTHROPIC_API_KEY no definida` | Key no en `.env` | Editar `~/.dev/claude/Claude-Copilot/.env` |
| `system_prompt.txt no encontrado` | Archivo faltante | Crear o copiar en el directorio del proyecto |
| `Redis no responde` | Docker no corre | Sin problema — el orquestador usa cache en memoria automáticamente |
| `KPI EXCEDIDO` | Diff muy grande | Usar `review-batch` o dividir el diff |
| PR check falla con "issues críticos" | Claude encontró bugs/seguridad | Resolver los issues 🔴 CRITICAL antes de mergear |
