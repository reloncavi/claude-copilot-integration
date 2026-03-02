# =============================================================================
# tools/ide_injector.py — Inyector de Pseudocódigo para IDE + Copilot
# Fase 2 del Plan: Automatización IDE (Semana 3-4)
# Objetivo: Recibir pseudocódigo de Claude → inyectarlo como comentarios
#           en archivos .py/.ts → Copilot los expande a código real
# =============================================================================

import os
import sys
import re
import shutil
import logging
import argparse
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# =============================================================================
# EXCEPCIONES CUSTOM
# =============================================================================

class InjectorError(Exception):
    pass

class AnchorNotFoundError(InjectorError):
    """Clase o función anchor no encontrada en el archivo."""
    pass

class MarkerCorruptedError(InjectorError):
    """Marker @claude:start sin su @claude:end correspondiente."""
    pass

class UnsupportedFileError(InjectorError):
    """Extensión de archivo no soportada."""
    pass


# =============================================================================
# LOGGER
# =============================================================================

def setup_logger(name: str = 'ide_injector') -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    level = os.environ.get('LOG_LEVEL', 'INFO').upper()
    logger.setLevel(getattr(logging, level, logging.INFO))

    handler = logging.StreamHandler(sys.stderr)
    formatter = logging.Formatter(
        fmt='[%(asctime)s] [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%SZ',
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False
    return logger


logger = setup_logger('ide_injector')


# =============================================================================
# CONSTANTES Y MARKERS
# =============================================================================

MARKER_START_TPL = "# --- @claude:start {tag} ---"
MARKER_END_TPL   = "# --- @claude:end {tag} ---"
MARKER_REGEX     = re.compile(r'(?:#|//) --- @claude:(start|end) (.+?) ---')

COMMENT_STYLES: dict[str, dict[str, str]] = {
    '.py': {'line': '# ',  'block_start': '',     'block_end': ''},
    '.ts': {'line': '// ', 'block_start': '// ',  'block_end': ''},
    '.js': {'line': '// ', 'block_start': '// ',  'block_end': ''},
}

SUPPORTED_EXTENSIONS = set(COMMENT_STYLES.keys())

MAX_BACKUPS_PER_FILE = 5


# =============================================================================
# ENUMS Y DTOs
# =============================================================================

class InjectionMode(Enum):
    APPEND      = 'append'       # agregar al final del archivo
    PREPEND     = 'prepend'      # agregar después de los imports
    REPLACE     = 'replace'      # reemplazar bloque existente entre markers
    AT_LINE     = 'at_line'      # insertar en línea específica
    AFTER_CLASS = 'after_class'  # insertar después de una clase
    AFTER_FUNC  = 'after_func'   # insertar después de una función


@dataclass
class InjectionRequest:
    """Solicitud de inyección de pseudocódigo."""
    pseudocode: str
    target_file: str
    mode: InjectionMode
    tag: str = 'default'
    line_number: Optional[int] = None
    anchor_name: Optional[str] = None
    create_backup: bool = True


@dataclass
class InjectionResult:
    """Resultado de una operación de inyección."""
    success: bool
    target_file: str
    lines_injected: int
    mode: InjectionMode
    backup_path: Optional[str] = None
    error: Optional[str] = None


# =============================================================================
# CLASE PRINCIPAL: IDEInjector
# =============================================================================

class IDEInjector:
    """
    Inyecta pseudocódigo de Claude en archivos del IDE como comentarios.
    Soporta múltiples modos de inserción y gestión de backups.
    """

    def __init__(self, workspace_root: str = '.'):
        self.workspace_root = Path(workspace_root).resolve()
        if not self.workspace_root.exists() or not self.workspace_root.is_dir():
            raise InjectorError(
                f"workspace_root no es un directorio válido: {workspace_root}"
            )

    # -------------------------------------------------------------------------
    # INYECCIÓN PRINCIPAL
    # -------------------------------------------------------------------------

    def inject(self, request: InjectionRequest) -> InjectionResult:
        """
        Punto de entrada principal. Valida, hace backup y ejecuta la inyección.
        Nunca lanza excepciones — encapsula errores en InjectionResult.
        """
        file_path = Path(request.target_file)
        backup_path: Optional[str] = None

        try:
            # 1. Validar archivo
            if not file_path.exists():
                raise FileNotFoundError(f"Archivo no encontrado: {request.target_file}")

            ext = file_path.suffix.lower()
            if ext not in SUPPORTED_EXTENSIONS:
                raise UnsupportedFileError(
                    f"Extensión no soportada: '{ext}'. "
                    f"Soportadas: {sorted(SUPPORTED_EXTENSIONS)}"
                )

            # 2. Crear backup antes de modificar
            if request.create_backup:
                backup_path = self.create_backup(request.target_file)

            # 3. Formatear pseudocódigo con markers
            block = self._format_block(request.pseudocode, request.tag, ext)

            # 4. Ejecutar según modo
            mode = request.mode
            if mode == InjectionMode.APPEND:
                lines_injected = self._inject_append(file_path, block)

            elif mode == InjectionMode.PREPEND:
                lines_injected = self._inject_prepend(file_path, block)

            elif mode == InjectionMode.REPLACE:
                lines_injected = self._inject_replace(file_path, block, request.tag)

            elif mode == InjectionMode.AT_LINE:
                if request.line_number is None:
                    raise ValueError("AT_LINE requiere line_number")
                lines_injected = self._inject_at_line(file_path, block, request.line_number)

            elif mode == InjectionMode.AFTER_CLASS:
                if not request.anchor_name:
                    raise ValueError("AFTER_CLASS requiere anchor_name")
                lines_injected = self._inject_after_anchor(
                    file_path, block, 'class', request.anchor_name
                )

            elif mode == InjectionMode.AFTER_FUNC:
                if not request.anchor_name:
                    raise ValueError("AFTER_FUNC requiere anchor_name")
                lines_injected = self._inject_after_anchor(
                    file_path, block, 'def', request.anchor_name
                )

            else:
                raise InjectorError(f"Modo de inyección desconocido: {mode}")

            logger.info(
                f"✅ Inyectado [{request.tag}] en {request.target_file} "
                f"({lines_injected} líneas, modo: {mode.value})"
            )

            return InjectionResult(
                success=True,
                target_file=request.target_file,
                lines_injected=lines_injected,
                mode=mode,
                backup_path=backup_path,
            )

        except FileNotFoundError as e:
            logger.error(f"❌ {e}")
            return InjectionResult(
                success=False, target_file=request.target_file,
                lines_injected=0, mode=request.mode, error=str(e),
            )
        except PermissionError as e:
            logger.error(f"❌ Sin permisos: {e}")
            return InjectionResult(
                success=False, target_file=request.target_file,
                lines_injected=0, mode=request.mode, error=str(e),
            )
        except Exception as e:
            logger.error(f"❌ Error inesperado en inyección: {e}")
            return InjectionResult(
                success=False, target_file=request.target_file,
                lines_injected=0, mode=request.mode, error=str(e),
            )

    # -------------------------------------------------------------------------
    # FORMATEO DEL BLOQUE
    # -------------------------------------------------------------------------

    def _format_block(self, pseudocode: str, tag: str, ext: str) -> list[str]:
        """
        Construye el bloque completo con:
        - Header con markers @claude:start y metadata
        - Pseudocódigo con prefijo de comentario (sin duplicar si ya es comentario)
        - Footer con marker @claude:end
        """
        style = COMMENT_STYLES.get(ext, COMMENT_STYLES['.py'])
        c = style['line']  # prefijo de comentario: '# ' o '// '

        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

        lines: list[str] = [
            f"{c}--- @claude:start {tag} ---",
            f"{c}Generado: {timestamp} | Modelo: claude-opus",
            f"{c}Instrucciones: Copilot debe expandir este pseudocódigo",
            "",
        ]

        for raw_line in pseudocode.splitlines():
            stripped = raw_line.rstrip()
            if not stripped:
                lines.append("")
            elif stripped.lstrip().startswith('#') or stripped.lstrip().startswith('//'):
                # Ya es comentario — no duplicar el prefijo
                lines.append(stripped)
            else:
                lines.append(f"{c}{stripped}")

        lines.extend([
            "",
            f"{c}--- @claude:end {tag} ---",
        ])

        return lines

    # -------------------------------------------------------------------------
    # MODOS DE INYECCIÓN
    # -------------------------------------------------------------------------

    def _inject_append(self, file_path: Path, block: list[str]) -> int:
        """Agrega el bloque al final del archivo."""
        existing = file_path.read_text(encoding='utf-8')
        separator = '\n\n' if existing.strip() else ''
        new_content = existing.rstrip('\n') + separator + '\n'.join(block) + '\n'
        file_path.write_text(new_content, encoding='utf-8')
        return len(block)

    def _inject_prepend(self, file_path: Path, block: list[str]) -> int:
        """
        Inserta el bloque DESPUÉS de la última línea de imports del archivo.
        Si no hay imports, inserta al inicio.
        """
        lines = file_path.read_text(encoding='utf-8').splitlines(keepends=True)

        # Encontrar la última línea de imports
        last_import_idx = -1
        import_re = re.compile(r'^\s*(import |from .+ import)')
        for i, line in enumerate(lines):
            if import_re.match(line):
                last_import_idx = i

        insert_at = last_import_idx + 1 if last_import_idx >= 0 else 0
        inject_lines = [''.join(l + '\n') for l in block]
        inject_lines = ['\n', '\n'] + inject_lines  # 2 líneas vacías de separación

        new_lines = lines[:insert_at] + inject_lines + lines[insert_at:]
        file_path.write_text(''.join(new_lines), encoding='utf-8')
        return len(block)

    def _inject_replace(self, file_path: Path, block: list[str], tag: str) -> int:
        """
        Reemplaza el bloque existente entre @claude:start {tag} y @claude:end {tag}.
        Si los markers no existen, hace APPEND con warning.
        """
        content = file_path.read_text(encoding='utf-8')
        ext = file_path.suffix.lower()
        c = COMMENT_STYLES.get(ext, COMMENT_STYLES['.py'])['line']

        start_marker = f"{c}--- @claude:start {tag} ---"
        end_marker   = f"{c}--- @claude:end {tag} ---"

        start_idx = content.find(start_marker)
        end_idx   = content.find(end_marker)

        if start_idx == -1 or end_idx == -1:
            logger.warning(
                f"⚠️  Markers para tag '{tag}' no encontrados. Haciendo APPEND."
            )
            return self._inject_append(file_path, block)

        # Incluir el end_marker en la zona a reemplazar
        end_idx += len(end_marker)

        # Calcular líneas eliminadas para el retorno neto
        replaced_chunk = content[start_idx:end_idx]
        lines_removed = replaced_chunk.count('\n')

        new_content = (
            content[:start_idx].rstrip('\n')
            + '\n\n'
            + '\n'.join(block)
            + '\n'
            + content[end_idx:].lstrip('\n')
        )
        file_path.write_text(new_content, encoding='utf-8')
        return len(block) - lines_removed

    def _inject_at_line(self, file_path: Path, block: list[str], line_number: int) -> int:
        """
        Inserta el bloque en la línea indicada (base 1).
        line_number = 1 → antes de la primera línea.
        """
        lines = file_path.read_text(encoding='utf-8').splitlines(keepends=True)
        total_lines = len(lines)

        if line_number < 1 or line_number > total_lines + 1:
            raise ValueError(
                f"line_number {line_number} fuera de rango "
                f"(archivo tiene {total_lines} líneas)"
            )

        insert_at = line_number - 1
        inject_lines = [l + '\n' for l in block]
        new_lines = lines[:insert_at] + inject_lines + lines[insert_at:]
        file_path.write_text(''.join(new_lines), encoding='utf-8')
        return len(block)

    def _inject_after_anchor(
        self,
        file_path: Path,
        block: list[str],
        anchor_type: str,  # 'class' o 'def'
        anchor_name: str,
    ) -> int:
        """
        Inserta el bloque después del final del cuerpo de la clase/función
        con nombre anchor_name.
        """
        lines = file_path.read_text(encoding='utf-8').splitlines(keepends=True)

        # Buscar la línea donde está el anchor
        anchor_pattern = re.compile(
            rf'^\s*{re.escape(anchor_type)}\s+{re.escape(anchor_name)}\s*[:(]'
        )
        anchor_line_idx: Optional[int] = None
        anchor_indent: int = 0

        for i, line in enumerate(lines):
            if anchor_pattern.match(line):
                anchor_line_idx = i
                anchor_indent = len(line) - len(line.lstrip())
                break

        if anchor_line_idx is None:
            raise AnchorNotFoundError(
                f"{anchor_type} '{anchor_name}' no encontrado en {file_path}"
            )

        # Encontrar el final del bloque por indentación
        end_idx = anchor_line_idx + 1
        for i in range(anchor_line_idx + 1, len(lines)):
            line = lines[i]
            stripped = line.rstrip()
            if not stripped:
                end_idx = i + 1
                continue
            current_indent = len(line) - len(line.lstrip())
            if current_indent <= anchor_indent:
                break
            end_idx = i + 1

        inject_lines = [l + '\n' for l in block]
        separator = ['\n', '\n']
        new_lines = lines[:end_idx] + separator + inject_lines + lines[end_idx:]
        file_path.write_text(''.join(new_lines), encoding='utf-8')
        return len(block)

    # -------------------------------------------------------------------------
    # UTILIDADES
    # -------------------------------------------------------------------------

    def list_injected_blocks(self, file_path: str) -> list[dict]:
        """
        Escanea el archivo y retorna todos los bloques @claude inyectados.
        Returns: [{"tag": str, "start_line": int, "end_line": int, "line_count": int}]
        """
        path = Path(file_path)
        if not path.exists():
            return []

        lines = path.read_text(encoding='utf-8').splitlines()
        starts: dict[str, int] = {}
        blocks: list[dict] = []

        for i, line in enumerate(lines, 1):
            m = MARKER_REGEX.search(line)
            if m:
                kind, tag = m.group(1), m.group(2).strip()
                if kind == 'start':
                    starts[tag] = i
                elif kind == 'end' and tag in starts:
                    start_line = starts.pop(tag)
                    blocks.append({
                        'tag': tag,
                        'start_line': start_line,
                        'end_line': i,
                        'line_count': i - start_line + 1,
                    })

        # Detectar markers sin cerrar
        for tag, start_line in starts.items():
            logger.warning(
                f"⚠️  Marker sin cerrar en {file_path}: "
                f"@claude:start '{tag}' en línea {start_line}"
            )

        return blocks

    def remove_block(self, file_path: str, tag: str) -> bool:
        """
        Elimina el bloque identificado por tag (inclusive los markers).
        Returns True si se eliminó, False si no existía.
        """
        path = Path(file_path)
        if not path.exists():
            return False

        content = path.read_text(encoding='utf-8')
        ext = path.suffix.lower()
        c = COMMENT_STYLES.get(ext, COMMENT_STYLES['.py'])['line']

        start_marker = f"{c}--- @claude:start {tag} ---"
        end_marker   = f"{c}--- @claude:end {tag} ---"

        start_idx = content.find(start_marker)
        end_idx   = content.find(end_marker)

        if start_idx == -1 or end_idx == -1:
            return False

        end_idx += len(end_marker)

        # Limpiar líneas vacías adyacentes
        before = content[:start_idx].rstrip('\n')
        after  = content[end_idx:].lstrip('\n')
        new_content = before + ('\n\n' if after else '\n') + after

        path.write_text(new_content, encoding='utf-8')
        logger.info(f"🗑️  Bloque [{tag}] eliminado de {file_path}")
        return True

    def create_backup(self, file_path: str) -> str:
        """
        Crea una copia de seguridad del archivo.
        Mantiene máximo MAX_BACKUPS_PER_FILE backups (elimina los más viejos).
        Returns: ruta del backup creado.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Archivo no encontrado: {file_path}")

        timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
        backup_path = path.with_suffix(f'{path.suffix}.bak.{timestamp}')
        shutil.copy2(path, backup_path)

        # Limpiar backups viejos
        pattern = f'{path.stem}{path.suffix}.bak.*'
        existing_backups = sorted(path.parent.glob(pattern))
        while len(existing_backups) > MAX_BACKUPS_PER_FILE:
            oldest = existing_backups.pop(0)
            oldest.unlink(missing_ok=True)
            logger.debug(f"Backup antiguo eliminado: {oldest}")

        logger.info(f"💾 Backup creado: {backup_path}")
        return str(backup_path)

    def restore_backup(
        self,
        file_path: str,
        backup_path: Optional[str] = None,
    ) -> bool:
        """
        Restaura el archivo desde un backup.
        Si backup_path no se especifica, usa el más reciente.
        Returns True si se restauró exitosamente.
        """
        path = Path(file_path)

        if backup_path:
            src = Path(backup_path)
        else:
            # Buscar el backup más reciente
            pattern = f'{path.stem}{path.suffix}.bak.*'
            candidates = sorted(path.parent.glob(pattern), reverse=True)
            if not candidates:
                logger.warning(f"No hay backups disponibles para {file_path}")
                return False
            src = candidates[0]

        if not src.exists():
            logger.error(f"Backup no encontrado: {src}")
            return False

        shutil.copy2(src, path)
        logger.info(f"♻️  Restaurado {file_path} desde {src}")
        return True


# =============================================================================
# BATCH INJECTOR
# =============================================================================

class BatchInjector:
    """
    Inyecta pseudocódigo en múltiples archivos de un módulo completo.
    Útil cuando Claude diseña router + service + repository en una sola respuesta.
    """

    def __init__(self, injector: IDEInjector):
        self.injector = injector

    def inject_module(
        self,
        module_design: dict[str, str],
        module_path: str,
    ) -> list[InjectionResult]:
        """
        Inyecta pseudocódigo en múltiples archivos de un módulo.

        Args:
            module_design: {"router.py": pseudocode, "service.py": pseudocode, ...}
            module_path: directorio base del módulo (ej: "app/modules/orders/")
        """
        base = Path(module_path)
        base.mkdir(parents=True, exist_ok=True)

        results: list[InjectionResult] = []
        total_lines = 0

        for filename, pseudocode in module_design.items():
            target = base / filename

            # Crear archivo con header mínimo si no existe
            if not target.exists():
                ext = target.suffix.lower()
                c = COMMENT_STYLES.get(ext, COMMENT_STYLES['.py'])['line']
                timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                header = (
                    f"{c}=============================================================\n"
                    f"{c}{filename} — Generado por Claude + Copilot Integration\n"
                    f"{c}Creado: {timestamp}\n"
                    f"{c}=============================================================\n\n"
                )
                target.write_text(header, encoding='utf-8')
                logger.info(f"📄 Archivo creado: {target}")

            request = InjectionRequest(
                pseudocode=pseudocode,
                target_file=str(target),
                mode=InjectionMode.APPEND,
                tag=filename.replace('.', '_'),
            )
            result = self.injector.inject(request)
            results.append(result)

            if result.success:
                total_lines += result.lines_injected

        ok_count = sum(1 for r in results if r.success)
        logger.info(
            f"📦 Módulo inyectado: {ok_count}/{len(results)} archivos, "
            f"{total_lines} líneas totales en '{module_path}'"
        )
        return results

    def inject_from_json(self, json_path: str) -> list[InjectionResult]:
        """
        Carga el diseño del módulo desde un JSON y delega a inject_module().

        Formato esperado:
        {
            "module_path": "app/modules/orders/",
            "files": {
                "router.py": "pseudocode...",
                "service.py": "pseudocode..."
            }
        }
        """
        path = Path(json_path)
        if not path.exists():
            raise FileNotFoundError(f"Archivo JSON no encontrado: {json_path}")

        data = None
        try:
            data = __import__('json').loads(path.read_text(encoding='utf-8'))
        except Exception as e:
            raise InjectorError(f"Error leyendo JSON de módulo: {e}") from e

        module_path = data.get('module_path', '.')
        files = data.get('files', {})

        if not files:
            raise InjectorError("El JSON no contiene archivos para inyectar (clave 'files')")

        return self.inject_module(files, module_path)


# =============================================================================
# FILE WATCHER (Fase 4 — Opcional)
# =============================================================================

class FileWatcher:
    """
    Monitorea el directorio de trabajo buscando archivos .claude.json.
    Cuando detecta uno, inyecta el pseudocódigo y lo marca como procesado.
    Requiere 'watchdog' instalado (pip install watchdog).
    """

    def __init__(self, injector: IDEInjector, watch_dir: str = '.'):
        self.injector = injector
        self.watch_dir = Path(watch_dir)
        self._running = False

    async def start(self) -> None:
        """Loop de polling. Detecta .claude.json → inyecta → renombra a .done.json."""
        import asyncio
        self._running = True
        logger.info(f"👁️  FileWatcher iniciado en '{self.watch_dir}'")

        while self._running:
            for json_file in self.watch_dir.glob('*.claude.json'):
                try:
                    data = __import__('json').loads(
                        json_file.read_text(encoding='utf-8')
                    )
                    target  = data.get('target', '')
                    pseudo  = data.get('pseudocode', '')
                    mode    = data.get('mode', 'replace')
                    tag     = data.get('tag', json_file.stem)

                    if target and pseudo:
                        request = InjectionRequest(
                            pseudocode=pseudo,
                            target_file=target,
                            mode=InjectionMode(mode),
                            tag=tag,
                        )
                        result = self.injector.inject(request)
                        status = 'ok' if result.success else 'error'
                        done_name = json_file.with_suffix(f'.{status}.done.json')
                        json_file.rename(done_name)
                        logger.info(
                            f"⚡ Auto-inyectado en '{target}' "
                            f"(desde {json_file.name})"
                        )
                except Exception as e:
                    logger.error(f"Error procesando {json_file.name}: {e}")
                    error_name = json_file.with_suffix('.error.json')
                    json_file.rename(error_name)

            await asyncio.sleep(2)  # polling cada 2 segundos

    async def stop(self) -> None:
        self._running = False
        logger.info("🛑 FileWatcher detenido.")


# =============================================================================
# CLI
# =============================================================================

def build_cli() -> argparse.ArgumentParser:
    """
    Construye el parser con subcomandos:
    inject, list, remove, batch, restore
    """
    parser = argparse.ArgumentParser(
        prog='ide_injector',
        description='Inyector de pseudocódigo Claude → Copilot',
    )
    subparsers = parser.add_subparsers(dest='command', required=True)

    # --- inject ---
    p_inject = subparsers.add_parser('inject', help='Inyectar pseudocódigo en un archivo')
    p_inject.add_argument('--file', required=True, help='Archivo destino (.py, .ts, .js)')
    p_inject.add_argument(
        '--mode', default='append',
        choices=[m.value for m in InjectionMode],
        help='Modo de inyección (default: append)',
    )
    p_inject.add_argument('--tag', default='default', help='Nombre del bloque')
    p_inject.add_argument('--line', type=int, help='Número de línea (para at_line)')
    p_inject.add_argument('--anchor', help='Nombre de clase/función (para after_class/after_func)')
    p_inject.add_argument(
        '--input', default=None,
        help='Archivo con pseudocódigo (default: stdin)',
    )
    p_inject.add_argument(
        '--no-backup', action='store_true',
        help='No crear backup antes de modificar',
    )

    # --- list ---
    p_list = subparsers.add_parser('list', help='Listar bloques @claude inyectados')
    p_list.add_argument('--file', required=True, help='Archivo a inspeccionar')

    # --- remove ---
    p_remove = subparsers.add_parser('remove', help='Eliminar un bloque @claude')
    p_remove.add_argument('--file', required=True, help='Archivo a modificar')
    p_remove.add_argument('--tag', required=True, help='Tag del bloque a eliminar')

    # --- batch ---
    p_batch = subparsers.add_parser('batch', help='Inyectar desde un JSON de módulo')
    p_batch.add_argument('--json', required=True, help='Archivo JSON con diseño del módulo')

    # --- restore ---
    p_restore = subparsers.add_parser('restore', help='Restaurar archivo desde backup')
    p_restore.add_argument('--file', required=True, help='Archivo a restaurar')
    p_restore.add_argument('--backup', default=None, help='Ruta específica del backup')

    return parser


def main() -> None:
    parser = build_cli()
    args = parser.parse_args()
    injector = IDEInjector(workspace_root='.')

    if args.command == 'inject':
        # Leer pseudocódigo de archivo o stdin
        if args.input:
            pseudo = Path(args.input).read_text(encoding='utf-8')
        else:
            logger.info("Leyendo pseudocódigo de stdin...")
            pseudo = sys.stdin.read()

        request = InjectionRequest(
            pseudocode=pseudo,
            target_file=args.file,
            mode=InjectionMode(args.mode),
            tag=args.tag,
            line_number=args.line,
            anchor_name=args.anchor,
            create_backup=not args.no_backup,
        )
        result = injector.inject(request)

        if result.success:
            print(f"✅ Inyectado: {result.lines_injected} líneas en {result.target_file}")
            if result.backup_path:
                print(f"   💾 Backup: {result.backup_path}")
        else:
            print(f"❌ Error: {result.error}", file=sys.stderr)
            sys.exit(1)

    elif args.command == 'list':
        blocks = injector.list_injected_blocks(args.file)
        if not blocks:
            print(f"ℹ️  No hay bloques @claude en {args.file}")
        else:
            print(f"📋 Bloques @claude en {args.file}:")
            for b in blocks:
                print(
                    f"  [{b['tag']}] "
                    f"líneas {b['start_line']}-{b['end_line']} "
                    f"({b['line_count']} líneas)"
                )

    elif args.command == 'remove':
        removed = injector.remove_block(args.file, args.tag)
        print(
            f"{'✅ Eliminado' if removed else '⚠️  No encontrado'}: "
            f"bloque [{args.tag}] en {args.file}"
        )

    elif args.command == 'batch':
        batch = BatchInjector(injector)
        results = batch.inject_from_json(args.json)
        ok = sum(1 for r in results if r.success)
        print(f"✅ {ok}/{len(results)} archivos inyectados")
        for r in results:
            status = "✅" if r.success else "❌"
            print(f"  {status} {r.target_file} ({r.lines_injected} líneas)")

    elif args.command == 'restore':
        restored = injector.restore_backup(args.file, getattr(args, 'backup', None))
        print(
            f"{'✅ Restaurado' if restored else '❌ No hay backup disponible'}: "
            f"{args.file}"
        )


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    main()
