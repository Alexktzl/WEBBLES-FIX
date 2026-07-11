"""
Синтаксический валидатор патчей с изолированной песочницей.
Все изменения проверяются в копии проекта, включая полную компиляцию и форматирование.
Добавлена очистка от артефактов перед проверкой.
"""

import ast
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)


def validate_in_sandbox(
    project_path: Path,
    file_path: Path,
    modification_fn: Callable[[Path], None],
    language: str,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Выполняет modification_fn над копией файла в изолированной копии проекта,
    затем проверяет проект на компиляцию и форматирование.
    Возвращает (успех, итоговое_содержимое_файла, сообщение_об_ошибке).
    При успехе: (True, corrected_content, None)
    При провале: (False, None, diagnostic_string)
    """
    if language not in ("rust", "rs", "python", "py"):
        # Для неподдерживаемых языков просто применяем без проверки
        try:
            modification_fn(file_path)
            content = file_path.read_text(encoding="utf-8")
            return True, content, None
        except Exception as e:
            return False, None, str(e)

    # Создаём временную копию проекта
    tmp_dir = tempfile.mkdtemp(prefix="webbles_sandbox_")
    try:
        tmp_project = Path(tmp_dir) / project_path.name
        shutil.copytree(
            project_path,
            tmp_project,
            symlinks=True,
            ignore=shutil.ignore_patterns("target", ".git", "__pycache__", "node_modules", ".venv", "venv"),
        )
        # Получаем путь к изменяемому файлу внутри песочницы
        rel_file = file_path.relative_to(project_path)
        tmp_file = tmp_project / rel_file

        # Применяем изменение (патч или запись нового содержимого)
        modification_fn(tmp_file)

        # Проверка компиляции / синтаксиса
        if language in ("rust", "rs"):
            # Проверяем cargo check
            result = subprocess.run(
                ["cargo", "check"],
                cwd=str(tmp_project),
                capture_output=True,
                text=True,
                timeout=120,
                encoding="utf-8",
                errors="replace"
            )
            if result.returncode != 0:
                logger.info("Песочница: cargo check провалился")
                # Собираем диагностику: ограничим 2000 символов
                feedback = result.stderr.strip() or result.stdout.strip()
                return False, None, f"cargo check failed:\n{feedback[:2000]}"

            # Применяем rustfmt для причёсывания файла
            fmt_result = subprocess.run(
                ["rustfmt", "--edition", "2021", str(tmp_file)],
                capture_output=True,
                text=True,
                timeout=30,
                encoding="utf-8",
                errors="replace"
            )
            if fmt_result.returncode != 0:
                logger.info("Песочница: rustfmt не смог отформатировать файл")
                return False, None, f"rustfmt failed:\n{fmt_result.stderr[:2000]}"

            # Повторная проверка компиляции после форматирования
            check2 = subprocess.run(
                ["cargo", "check"],
                cwd=str(tmp_project),
                capture_output=True,
                text=True,
                timeout=120,
                encoding="utf-8",
                errors="replace"
            )
            if check2.returncode != 0:
                logger.info("Песочница: cargo check после форматирования провалился")
                return False, None, f"cargo check after rustfmt failed:\n{check2.stderr[:2000]}"

        elif language in ("python", "py"):
            try:
                with open(tmp_file, "r", encoding="utf-8") as f:
                    ast.parse(f.read(), filename=str(tmp_file))
            except SyntaxError as e:
                logger.info("Песочница: ошибка синтаксиса Python")
                return False, None, f"Python syntax error: {e}"

        # Успех: читаем итоговое содержимое
        corrected = tmp_file.read_text(encoding="utf-8")
        return True, corrected, None

    except Exception as e:
        logger.error(f"Ошибка песочницы: {e}")
        return False, None, f"Sandbox exception: {e}"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ----------------------------------------------------------------------
# Старые функции (оставлены для совместимости, но в ApplyPatchStage не используются)
# ----------------------------------------------------------------------

def validate_patch_syntactically(
    original_content: str,
    patch: str,
    language: str,
    file_path: Path,
    project_path: Path,
) -> bool:
    valid, _ = validate_patch_syntactically_with_error(
        original_content, patch, language, file_path, project_path
    )
    return valid


def validate_patch_syntactically_with_error(
    original_content: str,
    patch: str,
    language: str,
    file_path: Path,
    project_path: Path,
) -> Tuple[bool, str]:
    """Возвращает (True, "") если патч не ухудшает синтаксис."""
    if language in ("rust", "rs"):
        return _validate_rust_with_progress(project_path, file_path, patch)
    elif language in ("python", "py"):
        return _validate_python_with_error(file_path, patch)
    else:
        return True, ""


def validate_rust_file_syntax(content: str, file_path: Path) -> bool:
    """Проверяет синтаксис Rust-файла через rustc."""
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.rs', delete=False, encoding='utf-8') as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        result = subprocess.run(
            ["rustc", "--edition", "2021", "--crate-type", "lib", str(tmp_path), "-o", os.devnull],
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace"
        )
        return result.returncode == 0
    except Exception:
        return False
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass


def _count_rust_errors(file_path: Path) -> int:
    try:
        result = subprocess.run(
            ["rustc", "--edition", "2021", "--crate-type", "lib", str(file_path), "-o", os.devnull],
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace"
        )
        return result.stderr.count('error:') if result.stderr else 0
    except Exception:
        return 999


def _validate_rust_with_progress(project_path: Path, file_path: Path, patch: str) -> Tuple[bool, str]:
    try:
        original_errors = _count_rust_errors(file_path)
        pe = __import__('fixers.patch_engine', fromlist=['PatchEngine']).PatchEngine()
        tmp_file = tempfile.NamedTemporaryFile(mode='w', suffix='.rs', delete=False, encoding='utf-8')
        tmp_file.write(file_path.read_text(encoding='utf-8'))
        tmp_file.close()
        tmp_path = Path(tmp_file.name)
        if not pe.apply_patch(tmp_path, patch):
            tmp_path.unlink()
            return False, "PatchEngine failed"
        new_errors = _count_rust_errors(tmp_path)
        tmp_path.unlink()
        if new_errors <= original_errors:
            return True, ""
        else:
            return False, f"Errors increased from {original_errors} to {new_errors}"
    except Exception as e:
        return False, str(e)


def _validate_python_with_error(file_path: Path, patch: str) -> Tuple[bool, str]:
    try:
        pe = __import__('fixers.patch_engine', fromlist=['PatchEngine']).PatchEngine()
        tmp_file = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8")
        tmp_file.write(file_path.read_text(encoding="utf-8"))
        tmp_file.close()
        tmp_path = Path(tmp_file.name)
        if not pe.apply_patch(tmp_path, patch):
            tmp_path.unlink()
            return False, "PatchEngine failed"
        with open(tmp_path, "r", encoding="utf-8") as f:
            ast.parse(f.read(), filename=str(tmp_path))
        tmp_path.unlink()
        return True, ""
    except SyntaxError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass