"""
Инкрементальная песочница для планирования в webles_conveyor.
Обеспечивает быстрое copy-on-write изолированное выполнение с использованием hardlink'ов.
Состояния идентифицируются детерминированными хешами на основе содержимого файлов.
"""

import hashlib
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class IncrementalSandbox:
    """
    Управляет постоянным корнем песочницы с семантикой copy-on-write.
    Состояние определяется исключительно файловой системой; каждое состояние — директория,
    имя которой равно хешу её отслеживаемого содержимого. После создания состояние неизменяемо.
    """

    def __init__(self, base_path: Path, source_project: Path):
        self.base_path = base_path
        self.source_project = source_project.resolve()
        self.base_path.mkdir(parents=True, exist_ok=True)

        # Исходный снапшот (только для чтения)
        self.base_snapshot = self.base_path / "base"
        self._ensure_base_snapshot()

        # Зарегистрированные состояния: хеш -> путь
        self.states: Dict[str, Path] = {}

        # Регистрируем базовое состояние
        base_hash = self._compute_state_hash(self.base_snapshot)
        self.states[base_hash] = self.base_snapshot

    def _ensure_base_snapshot(self) -> None:
        """Создаёт базовый снапшот с помощью hardlink'ов, если его ещё нет."""
        if self.base_snapshot.exists():
            return
        logger.info(f"Создание базового снапшота в {self.base_snapshot}")
        self._copy_with_hardlinks(self.source_project, self.base_snapshot, visited=set())

    def _copy_with_hardlinks(self, src: Path, dst: Path, visited: set) -> None:
        """
        Рекурсивное копирование с hardlink'ами. Защита от бесконечной рекурсии через visited.
        """
        # Проверяем, не зациклились ли мы (симлинки)
        real_src = src.resolve()
        if real_src in visited:
            logger.warning(f"Обнаружена циклическая ссылка: {src} -> {real_src}")
            return
        visited.add(real_src)

        if not src.exists():
            return
        if src.is_symlink():
            # Символические ссылки не копируем, просто пропускаем
            return
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)
        elif src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            for item in sorted(src.iterdir(), key=lambda p: str(p)):
                if self._should_skip(item):
                    continue
                self._copy_with_hardlinks(item, dst / item.name, visited)

    def _hardlink_tree(self, src: Path, dst: Path, visited: Optional[set] = None) -> None:
        """Рекурсивно создаёт hardlink'и от src к dst."""
        if visited is None:
            visited = set()
        real_src = src.resolve()
        if real_src in visited:
            return
        visited.add(real_src)

        if not src.exists():
            return
        if src.is_symlink():
            return
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)
        elif src.is_dir():
            for item in sorted(src.iterdir(), key=lambda p: str(p)):
                self._hardlink_tree(item, dst / item.name, visited)

    def _should_skip(self, path: Path) -> bool:
        # Пропускаем служебные папки и саму песочницу
        skip_names = {
            ".git", "__pycache__", "node_modules", "target",
            "venv", ".venv", "env", "dist", "build", ".cache",
            ".webles_sandbox", ".webles_conveyor_state.json", ".webles_baseline.json"
        }
        return path.name in skip_names

    def _compute_file_snapshot(self, state_dir: Path) -> Dict[str, str]:
        """Вычисляет детерминированный снапшот всех отслеживаемых файлов в директории."""
        snapshot = {}
        tracked_extensions = {".rs", ".py", ".js", ".ts"}
        config_files = {"Cargo.toml", "Cargo.lock", "package.json", "package-lock.json",
                        "tsconfig.json", "pyproject.toml", "requirements.txt"}

        for root, dirs, files in os.walk(state_dir):
            dirs[:] = [d for d in dirs if d not in {
                ".git", "__pycache__", "node_modules", "target",
                "venv", ".venv", "env", "dist", "build", ".cache", ".webles_sandbox"
            }]
            for file in sorted(files):
                file_path = Path(root) / file
                rel_path = str(file_path.relative_to(state_dir))
                ext = file_path.suffix
                if ext in tracked_extensions or file in config_files:
                    try:
                        content = file_path.read_bytes()
                        snapshot[rel_path] = hashlib.sha256(content).hexdigest()
                    except Exception as e:
                        logger.debug(f"Не удалось хешировать {file_path}: {e}")
        return snapshot

    def _compute_state_hash(self, state_dir: Path) -> str:
        """Детерминированный хеш файлового дерева."""
        snapshot = self._compute_file_snapshot(state_dir)
        snapshot_str = json.dumps(snapshot, sort_keys=True)
        return hashlib.sha256(snapshot_str.encode("utf-8")).hexdigest()[:16]

    def _predict_patched_hash(
        self,
        parent_state_hash: str,
        file_rel_path: str,
        patched_content: str
    ) -> str:
        parent_dir = self.states[parent_state_hash]
        snapshot = self._compute_file_snapshot(parent_dir)
        new_hash = hashlib.sha256(patched_content.encode("utf-8")).hexdigest()
        snapshot[file_rel_path] = new_hash
        snapshot_str = json.dumps(snapshot, sort_keys=True)
        return hashlib.sha256(snapshot_str.encode("utf-8")).hexdigest()[:16]

    def create_state(self, parent_state_hash: str) -> Tuple[str, Path]:
        if parent_state_hash not in self.states:
            raise ValueError(f"Неизвестный родительский хеш: {parent_state_hash}")
        return parent_state_hash, self.states[parent_state_hash]

    def apply_patch(
        self,
        parent_state_hash: str,
        file_rel_path: str,
        patched_content: str
    ) -> Tuple[str, Path]:
        if parent_state_hash not in self.states:
            raise ValueError(f"Неизвестный родительский хеш: {parent_state_hash}")

        new_hash = self._predict_patched_hash(parent_state_hash, file_rel_path, patched_content)

        if new_hash in self.states:
            return new_hash, self.states[new_hash]

        parent_dir = self.states[parent_state_hash]
        new_dir = self.base_path / new_hash
        new_dir.mkdir(parents=True, exist_ok=True)

        self._hardlink_tree(parent_dir, new_dir)

        target_file = new_dir / file_rel_path
        try:
            target_file.parent.mkdir(parents=True, exist_ok=True)
            # target_file сейчас hardlink на тот же inode, что и файл в parent_dir
            # (а через цепочку parent'ов — и в base_snapshot, и в исходном
            # project_path). write_text() открывает файл с truncate "на месте":
            # без unlink эта запись правит данные по ВСЕМ путям, шарящим inode,
            # включая настоящий исходник проекта. Сначала рвём hardlink.
            target_file.unlink(missing_ok=True)
            # newline="" — отключаем трансляцию \n -> \r\n, иначе на Windows
            # фактический хеш файла не совпадёт с предсказанным (_predict_patched_hash)
            target_file.write_text(patched_content, encoding="utf-8", newline="")
        except Exception as e:
            logger.error(f"Не удалось записать пропатченный файл: {e}")
            shutil.rmtree(new_dir, ignore_errors=True)
            raise

        actual_hash = self._compute_state_hash(new_dir)
        if actual_hash != new_hash:
            logger.error(f"Несовпадение хеша! предсказан {new_hash}, фактический {actual_hash}")
            shutil.rmtree(new_dir, ignore_errors=True)
            raise RuntimeError("Несовпадение хеша состояния после патча")

        self.states[new_hash] = new_dir
        return new_hash, new_dir

    def get_project_path(self, state_hash: str) -> Optional[Path]:
        return self.states.get(state_hash)

    def cleanup_state(self, state_hash: str) -> None:
        if state_hash not in self.states:
            return
        state_dir = self.states.pop(state_hash)
        try:
            shutil.rmtree(state_dir)
        except Exception as e:
            logger.warning(f"Не удалось очистить состояние песочницы {state_hash}: {e}")

    def cleanup_all(self) -> None:
        for state_dir in list(self.states.values()):
            if state_dir != self.base_snapshot:
                shutil.rmtree(state_dir, ignore_errors=True)
        self.states.clear()
        if self.base_snapshot.exists():
            shutil.rmtree(self.base_snapshot, ignore_errors=True)