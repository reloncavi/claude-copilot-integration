# =============================================================================
# orchestrator.py — Orquestador Principal Claude + Copilot Integration
# Versión: 1.0 | Stack: Python 3.12 · FastAPI · PostgreSQL · Redis · Celery
# Patrón: Single point of contact con la API de Anthropic
# =============================================================================

import anthropic
import os
import json
import sys
import logging
import hashlib
import asyncio
import subprocess
import re
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Literal, Optional
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False


# =============================================================================
# EXCEPCIONES CUSTOM
# =============================================================================

class OrchestratorError(Exception):
    pass

class OrchestratorAPIError(OrchestratorError):
    """Error en llamadas a la API de Anthropic."""
    pass

class OrchestratorTimeoutError(OrchestratorError):
    """Timeout en llamadas a la API."""
    pass

class OrchestratorParseError(OrchestratorError):
    """JSON inválido en respuesta de revisión."""
    pass

class ConfigError(OrchestratorError):
    """Variables de entorno faltantes o inválidas."""
    pass

class NoDiffError(OrchestratorError):
    """No hay cambios para revisar."""
    pass


# =============================================================================
# LOGGER
# =============================================================================

class TokenLogFilter(logging.Filter):
    """Filtro que agrega token_count al record si no existe."""
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, 'token_count'):
            record.token_count = '-'
        return True


def setup_logger(name: str = 'orchestrator') -> logging.Logger:
    """
    Configura logger con formato enriquecido para GitHub Actions.
    Nivel configurable via LOG_LEVEL env var (default: INFO).
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    level = os.environ.get('LOG_LEVEL', 'INFO').upper()
    logger.setLevel(getattr(logging, level, logging.INFO))

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(getattr(logging, level, logging.INFO))

    formatter = logging.Formatter(
        fmt='[%(asctime)s] [%(levelname)s] [tokens:%(token_count)s] %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%SZ'
    )
    handler.setFormatter(formatter)
    handler.addFilter(TokenLogFilter())

    logger.addHandler(handler)
    logger.propagate = False
    return logger


logger = setup_logger('orchestrator')


# =============================================================================
# CONFIGURACIÓN
# =============================================================================

@dataclass(frozen=True)
class OrchestratorConfig:
    """Configuración inmutable del orquestador. Cargada desde env vars."""
    anthropic_api_key: str
    design_model: str = 'claude-opus-4-6'
    review_model: str = 'claude-haiku-4-5-20251001'
    max_tokens_design: int = 1500
    max_tokens_review: int = 800
    system_prompt_path: str = 'system_prompt.txt'
    redis_url: str = 'redis://localhost:6379/0'
    cache_ttl_seconds: int = 86400  # 24h

    @classmethod
    def from_env(cls) -> 'OrchestratorConfig':
        """
        Carga configuración desde variables de entorno.
        Raises ConfigError si ANTHROPIC_API_KEY no está definida.
        """
        api_key = os.environ.get('ANTHROPIC_API_KEY')
        if not api_key:
            raise ConfigError(
                "ANTHROPIC_API_KEY no encontrada. "
                "Define la variable de entorno o crea un archivo .env"
            )
        return cls(
            anthropic_api_key=api_key,
            design_model=os.environ.get('DESIGN_MODEL', 'claude-opus-4-6'),
            review_model=os.environ.get('REVIEW_MODEL', 'claude-haiku-4-5-20251001'),
            max_tokens_design=int(os.environ.get('MAX_TOKENS_DESIGN', '1500')),
            max_tokens_review=int(os.environ.get('MAX_TOKENS_REVIEW', '800')),
            system_prompt_path=os.environ.get('SYSTEM_PROMPT_PATH', 'system_prompt.txt'),
            redis_url=os.environ.get('REDIS_URL', 'redis://localhost:6379/0'),
            cache_ttl_seconds=int(os.environ.get('CACHE_TTL_SECONDS', '86400')),
        )


# =============================================================================
# DTOs
# =============================================================================

SeverityLevel = Literal['critical', 'warning', 'suggestion']


@dataclass
class ReviewIssue:
    """Issue encontrado durante la revisión de código."""
    issue: str
    severity: SeverityLevel
    suggestion: str

    def to_dict(self) -> dict:
        return {
            'issue': self.issue,
            'severity': self.severity,
            'suggestion': self.suggestion,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'ReviewIssue':
        return cls(
            issue=str(data.get('issue', 'Issue desconocido')),
            severity=data.get('severity', 'suggestion'),
            suggestion=str(data.get('suggestion', 'Sin sugerencia')),
        )


# =============================================================================
# CACHE DEL SYSTEM PROMPT (Redis)
# Ahorro estimado: ~40% en input tokens (sección 6.1 del plan)
# =============================================================================

class SystemPromptCache:
    """
    Cache del system prompt en Redis.
    Carga el prompt UNA vez por sesión/día, no en cada llamada.
    Fallback a lectura directa si Redis no está disponible.
    """

    def __init__(self, config: OrchestratorConfig):
        self.config = config
        self._redis_client: Optional[object] = None
        self._memory_cache: dict[str, str] = {}  # fallback en memoria

    async def _get_redis(self):
        """Inicializa conexión Redis lazy. Retorna None si no disponible."""
        if not REDIS_AVAILABLE:
            return None
        if self._redis_client is None:
            try:
                self._redis_client = redis.from_url(
                    self.config.redis_url,
                    decode_responses=True,
                    socket_connect_timeout=2,
                )
                await self._redis_client.ping()
            except Exception as e:
                logger.warning(f"Redis no disponible ({e}). Usando cache en memoria.")
                self._redis_client = None
        return self._redis_client

    def _compute_hash(self, content: str) -> str:
        """SHA256 del contenido del prompt — sirve como cache key."""
        return hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]

    async def get_prompt(self) -> str:
        """
        Obtiene el system prompt con estrategia cache-aside:
        1. Leer archivo actual y calcular hash
        2. Buscar en Redis (o memoria) con key sysprompt:{hash}
        3. HIT → retornar cacheado
        4. MISS → cargar, cachear con TTL, retornar
        """
        prompt_path = Path(self.config.system_prompt_path)
        if not prompt_path.exists():
            logger.warning(
                f"system_prompt.txt no encontrado en '{prompt_path}'. "
                "Usando prompt vacío."
            )
            return ""

        content = prompt_path.read_text(encoding='utf-8')
        cache_key = f"sysprompt:{self._compute_hash(content)}"

        # Intentar Redis
        r = await self._get_redis()
        if r:
            try:
                cached = await r.get(cache_key)
                if cached:
                    logger.info(f"System prompt cache HIT ({cache_key})")
                    return cached
            except Exception as e:
                logger.warning(f"Error leyendo Redis: {e}")

        # Intentar cache en memoria
        if cache_key in self._memory_cache:
            logger.info(f"System prompt memory cache HIT ({cache_key})")
            return self._memory_cache[cache_key]

        # MISS — cargar y cachear
        logger.info(f"System prompt cache MISS — cargando y cacheando ({cache_key})")
        if r:
            try:
                await r.setex(cache_key, self.config.cache_ttl_seconds, content)
            except Exception as e:
                logger.warning(f"Error escribiendo Redis: {e}")

        self._memory_cache[cache_key] = content
        return content

    async def invalidate(self) -> None:
        """Elimina todas las keys sysprompt:* de Redis sin bloquearlo."""
        self._memory_cache.clear()
        r = await self._get_redis()
        if not r:
            logger.info("Cache en memoria limpiado.")
            return
        try:
            cursor = 0
            deleted = 0
            while True:
                cursor, keys = await r.scan(cursor, match='sysprompt:*', count=100)
                if keys:
                    await r.delete(*keys)
                    deleted += len(keys)
                if cursor == 0:
                    break
            logger.info(f"Cache invalidado: {deleted} keys eliminadas de Redis.")
        except Exception as e:
            logger.error(f"Error invalidando cache: {e}")


# =============================================================================
# TOKEN TRACKER
# KPI objetivo: < 1,500 tokens/feature (sección 7 del plan)
# =============================================================================

@dataclass
class TokenRecord:
    timestamp: str
    model: str
    input_tokens: int
    output_tokens: int
    task: str


class TokenTracker:
    """
    Monitorea consumo de tokens por sesión.
    Exporta métricas para Grafana/Datadog (Fase 4).
    """

    KPI_LIMIT = 1500  # tokens por feature

    def __init__(self):
        self.total_input: int = 0
        self.total_output: int = 0
        self.request_count: int = 0
        self._records: list[TokenRecord] = []

    def record(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        task: str,
    ) -> None:
        """Registra tokens de una llamada y verifica KPI."""
        total = input_tokens + output_tokens
        self.total_input += input_tokens
        self.total_output += output_tokens
        self.request_count += 1

        self._records.append(TokenRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            task=task,
        ))

        # Log con token count en el record
        extra = {'token_count': total}
        logger.info(
            f"[TOKENS] {task}: {input_tokens}in + {output_tokens}out = {total} ({model})",
            extra=extra,
        )

        session_total = self.total_input + self.total_output
        if session_total > self.KPI_LIMIT:
            logger.warning(
                f"⚠️  KPI EXCEDIDO: {session_total} tokens (límite: {self.KPI_LIMIT})",
                extra={'token_count': session_total},
            )

    def get_summary(self) -> dict:
        """Resumen de consumo de la sesión."""
        total = self.total_input + self.total_output
        return {
            'total_input': self.total_input,
            'total_output': self.total_output,
            'total': total,
            'request_count': self.request_count,
            'avg_per_request': round(total / self.request_count, 1) if self.request_count else 0,
            'kpi_status': 'OK' if total <= self.KPI_LIMIT else 'EXCEEDED',
        }

    def export_for_grafana(self) -> list[dict]:
        """Formato compatible con Grafana/Datadog para Fase 4."""
        return [
            {
                'timestamp_iso': r.timestamp,
                'model': r.model,
                'input_tokens': r.input_tokens,
                'output_tokens': r.output_tokens,
                'task_type': r.task,
            }
            for r in self._records
        ]

    def log_summary(self) -> None:
        """Imprime resumen al stderr para captura en GitHub Actions."""
        s = self.get_summary()
        logger.info(
            f"📊 TOKEN SUMMARY — total:{s['total']} "
            f"(in:{s['total_input']} + out:{s['total_output']}) | "
            f"requests:{s['request_count']} | "
            f"avg:{s['avg_per_request']} | "
            f"KPI:{s['kpi_status']}"
        )


# =============================================================================
# CLIENTE ANTHROPIC
# =============================================================================

class ClaudeClient:
    """
    Wrapper del SDK de Anthropic.
    Gestiona: cache del system prompt, retry con backoff, token tracking.
    """

    MAX_RETRIES = 3
    RETRY_BASE_DELAY = 1.0  # segundos

    def __init__(
        self,
        config: OrchestratorConfig,
        cache: SystemPromptCache,
        tracker: TokenTracker,
    ):
        self.config = config
        self.cache = cache
        self.tracker = tracker
        self._client = anthropic.Anthropic(api_key=config.anthropic_api_key)

    async def _call_api(
        self,
        model: str,
        max_tokens: int,
        user_message: str,
        task: str,
    ) -> str:
        """
        Llama a la API de Anthropic con:
        - Prompt caching nativo de Anthropic (cache_control ephemeral, ~90% ahorro en system prompt)
        - Retry exponencial en RateLimitError (3 intentos)
        - Tracking automático de tokens
        """
        system_prompt = await self.cache.get_prompt()
        messages = [{'role': 'user', 'content': user_message}]

        # System prompt con cache_control para que Anthropic lo cachee en sus servidores.
        # El SDK envía automáticamente el header anthropic-beta: prompt-caching-2024-07-31.
        if system_prompt:
            system: object = [{'type': 'text', 'text': system_prompt, 'cache_control': {'type': 'ephemeral'}}]
        else:
            system = anthropic.NOT_GIVEN

        last_error: Optional[Exception] = None
        for attempt in range(self.MAX_RETRIES):
            try:
                response = self._client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=messages,
                    extra_headers={'anthropic-beta': 'prompt-caching-2024-07-31'},
                )
                self.tracker.record(
                    model=model,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    task=task,
                )
                return response.content[0].text

            except anthropic.RateLimitError as e:
                delay = self.RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    f"Rate limit en intento {attempt + 1}/{self.MAX_RETRIES}. "
                    f"Reintentando en {delay:.1f}s..."
                )
                last_error = e
                await asyncio.sleep(delay)

            except anthropic.APITimeoutError as e:
                raise OrchestratorTimeoutError(
                    f"Timeout en llamada a Claude API ({task})"
                ) from e

            except anthropic.APIError as e:
                raise OrchestratorAPIError(
                    f"Error de API Claude ({task}): {e}"
                ) from e

        raise OrchestratorAPIError(
            f"Máximo de reintentos alcanzado para tarea '{task}'"
        ) from last_error

    # -------------------------------------------------------------------------
    # FASE 1: DISEÑO
    # -------------------------------------------------------------------------

    async def design_feature(self, requirement: str) -> str:
        """
        Claude como arquitecto: recibe requerimiento en lenguaje natural,
        devuelve pseudocódigo como comentarios Python listos para Copilot.
        Modelo: claude-opus (máxima capacidad para diseño).
        """
        if not requirement or not requirement.strip():
            raise ValueError("El requerimiento no puede estar vacío.")
        if len(requirement) > 2000:
            raise ValueError(
                f"Requerimiento demasiado largo ({len(requirement)} chars). Máximo: 2000."
            )

        prompt = (
            f"Diseña: {requirement.strip()}\n"
            f"Formato: comentarios Python listos para Copilot\n"
            f"Incluye: type hints, docstring, manejo de errores, nombres descriptivos\n"
            f"Patrón: Repository Pattern + Service Layer\n"
            f"NUNCA código completo, SOLO pseudocódigo como comentarios"
        )

        logger.info(f"🏗️  Diseñando feature: '{requirement[:80]}...' " if len(requirement) > 80 else f"🏗️  Diseñando feature: '{requirement}'")

        return await self._call_api(
            model=self.config.design_model,
            max_tokens=self.config.max_tokens_design,
            user_message=prompt,
            task='design',
        )

    # -------------------------------------------------------------------------
    # FASE 3: REVISIÓN
    # -------------------------------------------------------------------------

    async def review_code(self, diff: str) -> list[ReviewIssue]:
        """
        Claude como reviewer: recibe diff, devuelve lista de ReviewIssue.
        Modelo: claude-haiku (económico, rápido para revisiones).
        Solo el diff, NUNCA el archivo completo (ahorro ~70% en input tokens).
        """
        if not diff or not diff.strip():
            raise ValueError("El diff no puede estar vacío.")

        # Si el diff es muy largo, dividir en chunks
        if len(diff) > 3000:
            logger.info(f"Diff grande ({len(diff)} chars) — procesando en chunks")
            return await self._review_chunked(diff)

        return await self._review_single(diff)

    async def _review_single(self, diff: str) -> list[ReviewIssue]:
        """Revisa un diff en una sola llamada."""
        prompt = (
            f"Revisa este diff:\n```\n{diff}\n```\n"
            f"Responde SOLO JSON array: "
            f'[{{"issue": "...", "severity": "...", "suggestion": "..."}}]\n'
            f"severity: 'critical' | 'warning' | 'suggestion'\n"
            f"Si no hay issues, responde: []"
        )

        logger.info(f"🔍 Revisando diff ({len(diff)} chars)...")

        raw = await self._call_api(
            model=self.config.review_model,
            max_tokens=self.config.max_tokens_review,
            user_message=prompt,
            task='review',
        )

        return self._parse_review_json(raw)

    async def _review_chunked(self, diff: str, chunk_size: int = 3000) -> list[ReviewIssue]:
        """Divide un diff largo en chunks y los revisa en paralelo."""
        chunks = [diff[i:i + chunk_size] for i in range(0, len(diff), chunk_size)]
        logger.info(f"Procesando {len(chunks)} chunks de diff en paralelo...")

        results = await asyncio.gather(
            *[self._review_single(chunk) for chunk in chunks],
            return_exceptions=False,
        )
        all_issues: list[ReviewIssue] = [issue for sublist in results for issue in sublist]

        # Limitar a 10 issues priorizando por severidad (según system_prompt.txt)
        severity_order = {'critical': 0, 'warning': 1, 'suggestion': 2}
        all_issues.sort(key=lambda x: severity_order.get(x.severity, 3))
        return all_issues[:10]

    async def review_batch(self, diffs: list[str]) -> list[list[ReviewIssue]]:
        """
        Revisa múltiples diffs agrupándolos en una sola llamada.
        Ahorro: ~30% en overhead por request (sección 6.1 del plan).
        """
        if not diffs:
            return []

        # Agrupar en lotes de máximo 3 diffs o 4000 chars
        results: list[list[ReviewIssue]] = []
        group: list[tuple[int, str]] = []
        group_chars = 0

        async def _process_group(g: list[tuple[int, str]]) -> dict[int, list[ReviewIssue]]:
            separator_lines = [
                f"--- DIFF {idx + 1} ---\n{d}\n"
                for idx, d in g
            ]
            combined = "\n".join(separator_lines)
            prompt = (
                f"{combined}\n"
                f"Responde un JSON array de arrays, un sub-array por cada DIFF "
                f"en el mismo orden:\n"
                f'[[{{"issue":"...","severity":"...","suggestion":"..."}}], ...]\n'
                f"Si un DIFF no tiene issues, incluye un sub-array vacío []."
            )
            raw = await self._call_api(
                model=self.config.review_model,
                max_tokens=self.config.max_tokens_review,
                user_message=prompt,
                task='review-batch',
            )
            parsed = self._parse_batch_json(raw, len(g))
            return {original_idx: parsed[i] for i, (original_idx, _) in enumerate(g)}

        # Agrupar todos los diffs antes de procesar en paralelo
        results = [[] for _ in diffs]
        groups: list[list[tuple[int, str]]] = []

        for idx, diff in enumerate(diffs):
            if group and (len(diff) + group_chars > 4000 or len(group) >= 3):
                groups.append(group)
                group = []
                group_chars = 0
            group.append((idx, diff))
            group_chars += len(diff)

        if group:
            groups.append(group)

        # Procesar todos los grupos en paralelo
        logger.info(f"Procesando {len(groups)} grupo(s) de batch en paralelo...")
        group_results_list = await asyncio.gather(*[_process_group(g) for g in groups])

        for group_results in group_results_list:
            for orig_idx, issues in group_results.items():
                results[orig_idx] = issues

        return results

    def _parse_review_json(self, raw: str) -> list[ReviewIssue]:
        """
        Parsea JSON de respuesta de revisión.
        Retry automático si el JSON es inválido.
        """
        # Limpiar markdown fences si Claude las incluye
        clean = re.sub(r'```(?:json)?\s*', '', raw).strip()
        clean = clean.rstrip('`').strip()

        try:
            data = json.loads(clean)
            if not isinstance(data, list):
                logger.warning("Respuesta de revisión no es una lista JSON. Retornando vacío.")
                return []
            return [ReviewIssue.from_dict(item) for item in data if isinstance(item, dict)]
        except json.JSONDecodeError as e:
            logger.error(f"JSON inválido en respuesta de revisión: {e}\nRaw: {raw[:200]}")
            return [ReviewIssue(
                issue="Error al parsear la respuesta de Claude",
                severity="warning",
                suggestion="Revisar manualmente el diff. El modelo devolvió JSON inválido.",
            )]

    def _parse_batch_json(self, raw: str, expected_count: int) -> list[list[ReviewIssue]]:
        """Parsea un JSON de array de arrays para batch reviews."""
        clean = re.sub(r'```(?:json)?\s*', '', raw).strip().rstrip('`').strip()

        try:
            data = json.loads(clean)
            if not isinstance(data, list):
                return [[] for _ in range(expected_count)]
            result = []
            for sub in data:
                if isinstance(sub, list):
                    result.append([ReviewIssue.from_dict(i) for i in sub if isinstance(i, dict)])
                else:
                    result.append([])
            # Rellenar si faltan sub-arrays
            while len(result) < expected_count:
                result.append([])
            return result[:expected_count]
        except json.JSONDecodeError:
            return [[] for _ in range(expected_count)]


# =============================================================================
# IDE INJECTOR (integrado en el orquestador para el pipeline)
# =============================================================================

class IDEInjector:
    """
    Inyecta pseudocódigo de Claude como comentarios en archivos .py/.ts.
    Copilot los expande a código real en el IDE.
    """

    SUPPORTED_EXTENSIONS = {'.py', '.ts', '.js'}

    @staticmethod
    def inject_pseudocode(
        pseudocode: str,
        target_file: str,
        position: str = 'append',
        tag: str = 'default',
    ) -> None:
        """
        Inyecta pseudocódigo con markers @claude en el archivo destino.

        Args:
            pseudocode: texto a inyectar como comentarios
            target_file: ruta al archivo .py o .ts
            position: 'append' | 'replace'
            tag: nombre del bloque para identificación
        """
        path = Path(target_file)
        if not path.exists():
            raise FileNotFoundError(f"Archivo no encontrado: {target_file}")

        ext = path.suffix.lower()
        if ext not in IDEInjector.SUPPORTED_EXTENSIONS:
            raise ValueError(
                f"Extensión no soportada: {ext}. "
                f"Soportadas: {IDEInjector.SUPPORTED_EXTENSIONS}"
            )

        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        comment = '# ' if ext == '.py' else '// '

        # Construir bloque con markers
        lines = [
            f"{comment}--- @claude:start {tag} ---",
            f"{comment}Generado: {timestamp} | Modelo: claude-opus",
            f"{comment}Instrucciones: Copilot debe expandir este pseudocódigo",
            "",
        ]

        for line in pseudocode.splitlines():
            stripped = line.rstrip()
            if not stripped:
                lines.append("")
            elif stripped.startswith('#') or stripped.startswith('//'):
                lines.append(stripped)
            else:
                lines.append(f"{comment}{stripped}")

        lines.extend([
            "",
            f"{comment}--- @claude:end {tag} ---",
        ])

        existing = path.read_text(encoding='utf-8')

        if position == 'replace':
            start_marker = f"{comment}--- @claude:start {tag} ---"
            end_marker = f"{comment}--- @claude:end {tag} ---"
            start_idx = existing.find(start_marker)
            end_idx = existing.find(end_marker)

            if start_idx != -1 and end_idx != -1:
                end_idx += len(end_marker)
                new_content = (
                    existing[:start_idx].rstrip('\n')
                    + '\n\n'
                    + '\n'.join(lines)
                    + '\n'
                    + existing[end_idx:].lstrip('\n')
                )
                path.write_text(new_content, encoding='utf-8')
                logger.info(
                    f"Pseudocódigo REEMPLAZADO en {target_file} "
                    f"(tag: {tag}, {len(lines)} líneas)"
                )
                return
            else:
                logger.warning(
                    f"Markers no encontrados para tag '{tag}'. Haciendo APPEND."
                )

        # APPEND (default)
        separator = '\n\n' if existing.strip() else ''
        new_content = existing.rstrip('\n') + separator + '\n'.join(lines) + '\n'
        path.write_text(new_content, encoding='utf-8')
        logger.info(
            f"Pseudocódigo INYECTADO en {target_file} "
            f"(tag: {tag}, {len(lines)} líneas)"
        )

    @staticmethod
    def extract_markers(file_path: str) -> list[tuple[int, int]]:
        """
        Encuentra pares de markers @claude:start/@claude:end.
        Retorna lista de (línea_inicio, línea_fin) (base 1).
        """
        path = Path(file_path)
        if not path.exists():
            return []

        lines = path.read_text(encoding='utf-8').splitlines()
        marker_re = re.compile(r'@claude:(start|end)\s+(.+?)\s*---')

        starts: dict[str, int] = {}
        pairs: list[tuple[int, int]] = []

        for i, line in enumerate(lines, 1):
            m = marker_re.search(line)
            if m:
                kind, tag = m.group(1), m.group(2)
                if kind == 'start':
                    starts[tag] = i
                elif kind == 'end' and tag in starts:
                    pairs.append((starts.pop(tag), i))

        return pairs


# =============================================================================
# EXTRACTOR DE DIFFS
# =============================================================================

class DiffExtractor:
    """Obtiene diffs de git para enviar a revisión."""

    BINARY_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.ico', '.woff', '.pdf', '.zip'}
    EXCLUDED_PATTERNS = [
        '__pycache__', '.pyc', 'migrations/', 'node_modules/',
        '.lock', 'package-lock.json', 'yarn.lock', 'poetry.lock',
    ]

    @staticmethod
    def from_git(base_branch: str = 'origin/main') -> str:
        """
        Ejecuta git diff {base_branch}...HEAD.
        Raises NoDiffError si no hay cambios.
        """
        try:
            result = subprocess.run(
                ['git', 'diff', f'{base_branch}...HEAD'],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                # Intentar con HEAD~1 como fallback
                result = subprocess.run(
                    ['git', 'diff', 'HEAD~1...HEAD'],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
        except subprocess.TimeoutExpired:
            raise OrchestratorTimeoutError("Timeout ejecutando git diff")
        except FileNotFoundError:
            raise OrchestratorError("git no encontrado en el sistema")

        diff = result.stdout
        if not diff.strip():
            raise NoDiffError("No hay cambios para revisar en el diff")

        return DiffExtractor.filter_files(diff)

    @staticmethod
    def from_file(diff_path: str) -> str:
        """Lee un archivo .diff o .patch y valida su formato."""
        path = Path(diff_path)
        if not path.exists():
            raise FileNotFoundError(f"Archivo diff no encontrado: {diff_path}")

        content = path.read_text(encoding='utf-8', errors='replace')
        if not content.strip():
            raise NoDiffError(f"El archivo diff está vacío: {diff_path}")

        if not (content.startswith('diff --git') or content.startswith('---')):
            logger.warning(
                f"El archivo '{diff_path}' no parece un diff estándar. "
                "Procesando de todas formas."
            )

        return DiffExtractor.filter_files(content)

    @staticmethod
    def filter_files(
        diff: str,
        extensions: list[str] | None = None,
    ) -> str:
        """
        Filtra el diff para mantener solo archivos relevantes.
        Excluye: binarios, lockfiles, migrations, __pycache__.
        """
        if extensions is None:
            extensions = ['.py', '.ts', '.js']

        lines = diff.splitlines(keepends=True)
        result_lines: list[str] = []
        current_file_ok = False
        in_header = True

        for line in lines:
            # Detectar inicio de nuevo archivo en el diff
            if line.startswith('diff --git'):
                # Determinar si el archivo es relevante
                parts = line.split(' ')
                filename = parts[-1].strip() if len(parts) >= 2 else ''
                filename_b = filename.lstrip('b/')

                # Verificar extensión
                has_valid_ext = any(filename_b.endswith(ext) for ext in extensions)

                # Verificar exclusiones
                is_excluded = any(
                    pat in filename_b
                    for pat in DiffExtractor.EXCLUDED_PATTERNS
                )

                # Verificar binarios
                is_binary = any(
                    filename_b.endswith(ext)
                    for ext in DiffExtractor.BINARY_EXTENSIONS
                )

                current_file_ok = has_valid_ext and not is_excluded and not is_binary
                in_header = False

            if current_file_ok or in_header:
                result_lines.append(line)

        filtered = ''.join(result_lines)
        if not filtered.strip():
            raise NoDiffError("No hay archivos relevantes en el diff para revisar")

        return filtered



# =============================================================================
# REVIEW REPORTER
# Consulta la API de GitHub para generar un reporte del estado de revisiones
# autónomas de Claude en el repositorio.
# =============================================================================

class ReviewReporter:
    """
    Genera un reporte del estado de las revisiones automáticas de Claude.
    Consulta la API de GitHub para obtener métricas de revisiones recientes.

    Requiere:
        GITHUB_TOKEN (o GH_TOKEN): token con permisos read:repo y read:actions
        GITHUB_REPOSITORY:         formato 'owner/repo' (disponible en Actions)
    """

    GITHUB_API = 'https://api.github.com'
    CLAUDE_COMMENT_PREFIX = '🤖 **Claude'
    WORKFLOW_FILE = 'claude-review.yml'

    def __init__(self, github_token: str, repository: str):
        self.github_token = github_token
        self.repository = repository

    def _api_get(self, path: str) -> object:
        """Realiza GET a la API de GitHub y retorna el JSON parseado."""
        url = f"{self.GITHUB_API}{path}"
        req = urllib.request.Request(
            url,
            headers={
                'Authorization': f'Bearer {self.github_token}',
                'Accept': 'application/vnd.github.v3+json',
                'X-GitHub-Api-Version': '2022-11-28',
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            raise OrchestratorAPIError(
                f"GitHub API error {e.code} en {path}: {e.reason}"
            ) from e
        except Exception as e:
            raise OrchestratorAPIError(
                f"Error consultando GitHub API ({path}): {e}"
            ) from e

    def fetch_workflow_runs(self, limit: int = 20) -> list[dict]:
        """Obtiene las ejecuciones recientes del workflow de revisión."""
        try:
            data = self._api_get(
                f"/repos/{self.repository}/actions/workflows"
                f"/{self.WORKFLOW_FILE}/runs?per_page={limit}"
            )
            return data.get('workflow_runs', []) if isinstance(data, dict) else []
        except OrchestratorAPIError as e:
            logger.warning(f"No se pudieron obtener workflow runs: {e}")
            return []

    def fetch_recent_prs(self, limit: int = 20) -> list[dict]:
        """Obtiene los PRs más recientes (abiertos y cerrados)."""
        try:
            data = self._api_get(
                f"/repos/{self.repository}/pulls"
                f"?state=all&per_page={limit}&sort=updated&direction=desc"
            )
            return data if isinstance(data, list) else []
        except OrchestratorAPIError as e:
            logger.warning(f"No se pudieron obtener PRs: {e}")
            return []

    def fetch_pr_comments(self, pr_number: int) -> list[dict]:
        """Obtiene los comentarios de un PR."""
        try:
            data = self._api_get(
                f"/repos/{self.repository}/issues/{pr_number}/comments?per_page=100"
            )
            return data if isinstance(data, list) else []
        except OrchestratorAPIError as e:
            logger.warning(f"No se pudieron obtener comentarios del PR #{pr_number}: {e}")
            return []

    def _parse_claude_comment(self, body: str) -> dict:
        """Extrae métricas de un comentario de Claude."""
        return {
            'clean': '✅ Sin issues' in body,
            'has_critical': '🔴' in body,
            'has_warnings': '🟡' in body,
            'has_suggestions': '🔵' in body,
            'skipped': '⏭️' in body,
        }

    def generate_report(self, pr_limit: int = 20) -> dict:
        """
        Genera un reporte completo del estado de las revisiones autónomas.

        Returns:
            dict con campos: generated_at, repository, workflow_stats,
            review_summary, recent_reviews.
        """
        report: dict = {
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'repository': self.repository,
            'workflow_stats': {
                'total_runs': 0,
                'success': 0,
                'failure': 0,
                'cancelled': 0,
                'in_progress': 0,
            },
            'review_summary': {
                'total_prs_checked': 0,
                'prs_with_review': 0,
                'clean_reviews': 0,
                'critical_found': 0,
                'warnings_found': 0,
                'suggestions_found': 0,
                'skipped_reviews': 0,
            },
            'recent_reviews': [],
            'recent_workflow_runs': [],
        }

        # ── Workflow runs ──────────────────────────────────────────────────────
        runs = self.fetch_workflow_runs(limit=pr_limit)
        report['workflow_stats']['total_runs'] = len(runs)
        for run in runs:
            conclusion = run.get('conclusion') or run.get('status', '')
            if conclusion in report['workflow_stats']:
                report['workflow_stats'][conclusion] += 1
            elif conclusion == 'completed':
                report['workflow_stats']['success'] += 1

        report['recent_workflow_runs'] = [
            {
                'run_id': r['id'],
                'status': r.get('status'),
                'conclusion': r.get('conclusion'),
                'created_at': r['created_at'],
                'html_url': r['html_url'],
                'pr_number': (
                    r['pull_requests'][0]['number']
                    if r.get('pull_requests') else None
                ),
            }
            for r in runs[:10]
        ]

        # ── PR review comments ─────────────────────────────────────────────────
        prs = self.fetch_recent_prs(limit=pr_limit)
        report['review_summary']['total_prs_checked'] = len(prs)

        for pr in prs:
            pr_number = pr['number']
            comments = self.fetch_pr_comments(pr_number)

            claude_comment = next(
                (
                    c for c in comments
                    if c.get('body', '').startswith(self.CLAUDE_COMMENT_PREFIX)
                ),
                None,
            )

            if not claude_comment:
                continue

            metrics = self._parse_claude_comment(claude_comment['body'])
            report['review_summary']['prs_with_review'] += 1

            if metrics['clean']:
                report['review_summary']['clean_reviews'] += 1
            if metrics['has_critical']:
                report['review_summary']['critical_found'] += 1
            if metrics['has_warnings']:
                report['review_summary']['warnings_found'] += 1
            if metrics['has_suggestions']:
                report['review_summary']['suggestions_found'] += 1
            if metrics['skipped']:
                report['review_summary']['skipped_reviews'] += 1

            report['recent_reviews'].append({
                'pr_number': pr_number,
                'pr_title': pr['title'],
                'pr_state': pr['state'],
                'pr_url': pr['html_url'],
                'reviewed_at': claude_comment['updated_at'],
                **metrics,
            })

        return report

    def print_report(self, report: dict) -> None:
        """Imprime un reporte formateado al stdout."""
        bar = '=' * 62
        print(f"\n{bar}")
        print(f"  📊 Estado de Revisiones — Agentes Autónomos")
        print(f"  Repositorio: {report['repository']}")
        print(f"  Generado:    {report['generated_at']}")
        print(f"{bar}")

        ws = report['workflow_stats']
        print(f"\n⚙️  WORKFLOW ({self.WORKFLOW_FILE})")
        print(f"   Total ejecuciones:  {ws['total_runs']}")
        if ws['total_runs']:
            print(f"   ✅  Exitosas:       {ws['success']}")
            print(f"   ❌  Fallidas:       {ws['failure']}")
            print(f"   🔄  En progreso:    {ws['in_progress']}")

        rs = report['review_summary']
        print(f"\n📈 REVISIONES DE CÓDIGO (últimos {rs['total_prs_checked']} PRs)")
        print(f"   PRs con revisión:   {rs['prs_with_review']}")
        print(f"   ✅  Sin issues:      {rs['clean_reviews']}")
        print(f"   🔴  Con críticos:    {rs['critical_found']}")
        print(f"   🟡  Con warnings:    {rs['warnings_found']}")
        print(f"   🔵  Con sugerencias: {rs['suggestions_found']}")
        print(f"   ⏭️   Omitidas:        {rs['skipped_reviews']}")

        if report['recent_reviews']:
            print(f"\n📋 DETALLE DE REVISIONES RECIENTES:")
            for rev in report['recent_reviews']:
                icons: list[str] = []
                if rev['clean']:
                    icons.append('✅')
                if rev['has_critical']:
                    icons.append('🔴')
                if rev['has_warnings']:
                    icons.append('🟡')
                if rev['has_suggestions']:
                    icons.append('🔵')
                if rev['skipped']:
                    icons.append('⏭️')
                status = ' '.join(icons) or '⚪'
                state_label = '🟢' if rev['pr_state'] == 'open' else '🔒'
                title = rev['pr_title'][:45] + ('…' if len(rev['pr_title']) > 45 else '')
                print(f"   {state_label} PR #{rev['pr_number']:>4}  {status}  {title}")

        if report['recent_workflow_runs']:
            print(f"\n⏱️  ÚLTIMAS EJECUCIONES:")
            conclusion_icon = {
                'success': '✅', 'failure': '❌', 'cancelled': '⚪',
                'in_progress': '🔄', 'queued': '⏳',
            }
            for run in report['recent_workflow_runs'][:5]:
                conclusion = run.get('conclusion') or run.get('status') or '?'
                icon = conclusion_icon.get(conclusion, '❓')
                pr_info = f" → PR #{run['pr_number']}" if run['pr_number'] else ''
                print(f"   {icon}  Run #{run['run_id']}{pr_info} [{conclusion}]")

        print(f"\n{bar}\n")


# =============================================================================
# CLI PRINCIPAL
# =============================================================================

async def main() -> None:
    """
    Punto de entrada CLI.
    Uso:
      python orchestrator.py design "descripción del feature" [archivo_destino.py]
      python orchestrator.py review /path/to/file.diff
      python orchestrator.py review-batch
      python orchestrator.py invalidate-cache
    """
    if len(sys.argv) < 2:
        print(
            "Uso:\n"
            "  python orchestrator.py design <requerimiento> [archivo_destino]\n"
            "  python orchestrator.py review <archivo.diff | rama_base>\n"
            "  python orchestrator.py review-batch\n"
            "  python orchestrator.py invalidate-cache\n"
            "  python orchestrator.py report [--json]\n",
            file=sys.stderr,
        )
        sys.exit(1)

    command = sys.argv[1].lower()
    export_metrics = '--export-metrics' in sys.argv

    # Inicializar componentes
    try:
        config = OrchestratorConfig.from_env()
    except ConfigError as e:
        logger.error(f"❌ Error de configuración: {e}")
        sys.exit(1)

    cache = SystemPromptCache(config)
    tracker = TokenTracker()
    client = ClaudeClient(config, cache, tracker)

    try:
        # ------------------------------------------------------------------
        # COMANDO: design
        # ------------------------------------------------------------------
        if command == 'design':
            if len(sys.argv) >= 3:
                requirement = sys.argv[2]
            else:
                logger.info("Leyendo requerimiento de stdin...")
                requirement = sys.stdin.read().strip()

            target_file = sys.argv[3] if len(sys.argv) >= 4 else None

            pseudocode = await client.design_feature(requirement)

            if target_file:
                IDEInjector.inject_pseudocode(
                    pseudocode=pseudocode,
                    target_file=target_file,
                    position='append',
                )
                print(f"✅ Pseudocódigo inyectado en {target_file}")
            else:
                print(pseudocode)

        # ------------------------------------------------------------------
        # COMANDO: review
        # ------------------------------------------------------------------
        elif command == 'review':
            diff_source = sys.argv[2] if len(sys.argv) >= 3 else None

            if diff_source and Path(diff_source).exists():
                diff = DiffExtractor.from_file(diff_source)
            elif diff_source:
                diff = DiffExtractor.from_git(diff_source)
            else:
                diff = DiffExtractor.from_git()

            issues = await client.review_code(diff)
            output = json.dumps(
                [i.to_dict() for i in issues],
                indent=2,
                ensure_ascii=False,
            )
            print(output)

        # ------------------------------------------------------------------
        # COMANDO: review-batch
        # ------------------------------------------------------------------
        elif command == 'review-batch':
            diff = DiffExtractor.from_git()
            # Dividir por archivo (cada bloque diff --git es un chunk)
            chunks = re.split(r'(?=diff --git)', diff)
            chunks = [c.strip() for c in chunks if c.strip()]

            logger.info(f"Procesando batch de {len(chunks)} archivos...")
            results = await client.review_batch(chunks)

            all_issues = [issue for sublist in results for issue in sublist]
            output = json.dumps(
                [i.to_dict() for i in all_issues],
                indent=2,
                ensure_ascii=False,
            )
            print(output)

        # ------------------------------------------------------------------
        # COMANDO: invalidate-cache
        # ------------------------------------------------------------------
        elif command == 'invalidate-cache':
            await cache.invalidate()
            print("✅ Cache del system prompt invalidado.")

        # ------------------------------------------------------------------
        # COMANDO: report
        # Estado de revisiones de agentes autónomos vía GitHub API.
        # Requiere: GITHUB_TOKEN (o GH_TOKEN) y GITHUB_REPOSITORY env vars.
        # ------------------------------------------------------------------
        elif command == 'report':
            output_json = '--json' in sys.argv

            github_token = (
                os.environ.get('GITHUB_TOKEN')
                or os.environ.get('GH_TOKEN')
            )
            repository = os.environ.get('GITHUB_REPOSITORY')

            if not github_token:
                logger.error(
                    "❌ GITHUB_TOKEN (o GH_TOKEN) no definida. "
                    "Define la variable de entorno para consultar la API de GitHub."
                )
                sys.exit(1)

            if not repository:
                logger.error(
                    "❌ GITHUB_REPOSITORY no definida. "
                    "Formato esperado: 'owner/repo'."
                )
                sys.exit(1)

            reporter = ReviewReporter(
                github_token=github_token,
                repository=repository,
            )

            logger.info(f"📡 Consultando API de GitHub para '{repository}'...")
            report_data = reporter.generate_report()

            if output_json:
                print(json.dumps(report_data, indent=2, ensure_ascii=False))
            else:
                reporter.print_report(report_data)

        else:
            logger.error(f"Comando desconocido: '{command}'")
            sys.exit(1)

    except NoDiffError as e:
        logger.warning(f"⏭️  {e}")
        print(json.dumps([]))  # stdout vacío para GitHub Action
        sys.exit(0)

    except (OrchestratorAPIError, OrchestratorTimeoutError) as e:
        logger.error(f"❌ Error de API: {e}")
        sys.exit(1)

    except (FileNotFoundError, ValueError) as e:
        logger.error(f"❌ {e}")
        sys.exit(1)

    finally:
        # Siempre loguear resumen de tokens al stderr
        tracker.log_summary()

        # Exportar métricas a Datadog + Grafana + JSON local
        # Se activa con --export-metrics o si METRICS_ENABLED=true en el entorno
        raw_metrics = tracker.export_for_grafana()
        if raw_metrics and (export_metrics or os.environ.get('METRICS_ENABLED', 'false') == 'true'):
            try:
                # Import lazy para no romper si metrics_exporter.py no está presente
                sys.path.insert(0, str(Path(__file__).parent / 'tools'))
                sys.path.insert(0, str(Path(__file__).parent))
                from metrics_exporter import MetricsExporter
                exporter = MetricsExporter()
                exporter.export_all(raw_metrics)
            except ImportError:
                # Fallback: solo guardar JSON si metrics_exporter no está disponible
                metrics_path = Path(os.environ.get('METRICS_OUTPUT_PATH', '/tmp/token_metrics.json'))
                metrics_path.write_text(
                    json.dumps(raw_metrics, indent=2, ensure_ascii=False),
                    encoding='utf-8',
                )
                logger.info(f"📊 Métricas guardadas en {metrics_path} (metrics_exporter.py no encontrado)")
            except Exception as e:
                logger.warning(f"Error exportando métricas: {e}")


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    asyncio.run(main())
