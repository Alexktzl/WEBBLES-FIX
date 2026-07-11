"""
Снимок дерева файлов проекта для левой панели UI.

Чистая функция без зависимостей от движка/LLM. Идёт по проекту, отдаёт плоский
список записей `{path, kind, depth}` — UI рисует их без перестройки в дерево.
Skip-каталоги те же, что и в audit-mode (`AnalyzeStage._AUDIT_SKIP_DIRS`).

Используется как payload события `EventType.PROJECT_TREE`:
    {"root": "<абсолютный путь>", "items": [...], "truncated": bool}
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Каталоги, в которые НЕ заходим: служебные, тяжёлые, кэшевые. Список общий с
# семантическим аудитом (`core/stages/analyze_stage.py::_AUDIT_SKIP_DIRS`) и
# чуть расширен — sandbox-каталоги Webbles, IDE-папки.
SKIP_DIRS = {
    "target", "node_modules", ".git", "venv", ".venv",
    "__pycache__", ".webbles_fix", ".webbles", ".webbles_backups",
    ".webles_sandbox", ".webbles_sandbox",
    "dist", "build", ".idea", ".vscode", ".tox", ".pytest_cache",
    ".next", ".cache", ".gradle", ".mvn", "statistic",
}
# По умолчанию ограничиваем — у больших проектов UI может задохнуться.
MAX_FILES = 2000


def scan_project_tree(
    root,
    *,
    max_files: int = MAX_FILES,
    skip_dirs: Optional[set] = None,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Идёт по `root` и собирает плоский список `{path, kind, depth}`.

    Сортировка детерминированная: внутри одного каталога сначала папки, потом
    файлы, по имени. Возвращает кортеж `(items, truncated)`. `truncated=True`,
    если упёрлись в лимит — UI покажет «… ещё N».

    Любая ошибка чтения отдельной директории — пропуск (никогда не ронять
    скан, главное — отдать что есть).
    """
    skip = set(SKIP_DIRS if skip_dirs is None else skip_dirs)
    items: List[Dict[str, Any]] = []
    truncated = False
    root_path = Path(root).resolve()
    if not root_path.exists():
        return [], False

    def _walk(current: Path, depth: int) -> None:
        nonlocal truncated
        if truncated:
            return
        try:
            entries = list(current.iterdir())
        except (OSError, PermissionError):
            return
        # Сортировка: dir первыми, по имени (регистронезависимо для красоты).
        entries.sort(key=lambda p: (0 if p.is_dir() else 1, p.name.lower()))
        for entry in entries:
            if truncated:
                return
            name = entry.name
            if entry.is_dir():
                if name in skip or name.startswith("."):
                    # Скрытые директории (как .git) — не показываем.
                    if name not in {"."}:
                        continue
                rel = str(entry.relative_to(root_path)).replace("\\", "/")
                items.append({"path": rel, "kind": "dir", "depth": depth})
                _walk(entry, depth + 1)
            elif entry.is_file():
                # Файлы — скрытые тоже отбрасываем (.DS_Store и подобные).
                if name.startswith("."):
                    continue
                rel = str(entry.relative_to(root_path)).replace("\\", "/")
                items.append({"path": rel, "kind": "file", "depth": depth})
                if len(items) >= max_files:
                    truncated = True
                    return

    _walk(root_path, 0)
    return items, truncated


def build_tree_event_payload(root, *, max_files: int = MAX_FILES) -> Dict[str, Any]:
    """Готовит payload для `EventType.PROJECT_TREE`."""
    items, truncated = scan_project_tree(root, max_files=max_files)
    return {
        "root": str(Path(root).resolve()),
        "items": items,
        "truncated": truncated,
        "total": len(items),
    }
