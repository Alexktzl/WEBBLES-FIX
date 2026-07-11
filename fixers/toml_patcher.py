"""
Семантический патчер для Cargo.toml (и других TOML-файлов).
Обновляет или добавляет зависимости без повреждения структуры файла.
Теперь сохраняет оригинальные символы перевода строки (LF / CRLF / CR).
Все ключевые операции покрыты DEBUG-логами.
"""

import logging
from pathlib import Path
from typing import List, Optional

import tomlkit

logger = logging.getLogger(__name__)


def _determine_line_ending(file_path: Path) -> str:
    """Определяет, какой символ новой строки используется в файле."""
    logger.debug("Определение переносов строк для %s", file_path)
    try:
        with open(file_path, 'rb') as f:
            chunk = f.read(4096)
        if b'\r\n' in chunk:
            logger.debug("Обнаружен CRLF")
            return '\r\n'
        elif b'\r' in chunk:
            logger.debug("Обнаружен CR")
            return '\r'
    except Exception as e:
        logger.debug("Ошибка определения переносов: %s", e)
    logger.debug("Используется LF")
    return '\n'


def _write_with_line_ending(file_path: Path, doc, line_ending: str) -> None:
    """Записывает TOML-документ с заданными окончаниями строк."""
    logger.debug("Запись %s с переносами строк %r", file_path, line_ending)
    text = tomlkit.dumps(doc)
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    if line_ending == '\r\n':
        text = text.replace('\n', '\r\n')
    elif line_ending == '\r':
        text = text.replace('\n', '\r')
    with open(file_path, 'w', encoding='utf-8', newline='') as f:
        f.write(text)
    logger.debug("Файл успешно записан")


def update_dependency_version(file_path: Path, dep_name: str, new_version: str) -> bool:
    """
    Обновляет версию зависимости в Cargo.toml.
    Поддерживает простые (dep = "ver") и составные (dep = { version = "ver", ... }) формы.
    Возвращает True, если обновление выполнено.
    """
    logger.debug("update_dependency_version: файл=%s, зависимость=%s -> %s", file_path, dep_name, new_version)
    try:
        line_ending = _determine_line_ending(file_path)
        with open(file_path, "rb") as f:
            doc = tomlkit.load(f)
        logger.debug("Файл успешно загружен")
    except Exception as e:
        logger.warning("Не удалось загрузить TOML-файл %s: %s", file_path, e)
        return False

    updated = False

    for section_name in ("dependencies", "dev-dependencies", "build-dependencies"):
        if section_name in doc:
            section_updated = _update_in_section(doc[section_name], dep_name, new_version)
            if section_updated:
                logger.debug("Обновление в секции [%s]", section_name)
            updated |= section_updated

    if updated:
        try:
            _write_with_line_ending(file_path, doc, line_ending)
            logger.info("Зависимость %s обновлена до %s в %s", dep_name, new_version, file_path.name)
        except Exception as e:
            logger.error("Не удалось записать обновлённый %s: %s", file_path, e)
            return False
        return True

    logger.debug("Зависимость %s не найдена в секциях", dep_name)
    return False


def upsert_dependency(
    file_path: Path,
    dep_name: str,
    new_version: str,
    features: Optional[List[str]] = None,
) -> bool:
    """
    Обновляет существующую или добавляет новую зависимость в [dependencies] Cargo.toml.
    Если зависимость уже есть — обновляет версию (с сохранением структуры).
    Если нет — создаёт запись (простую или составную, в зависимости от наличия features).
    Сохраняет оригинальные переводы строк.
    Возвращает True, если изменения записаны на диск.
    """
    logger.debug("upsert_dependency: файл=%s, зависимость=%s -> %s, features=%s",
                 file_path, dep_name, new_version, features)
    try:
        line_ending = _determine_line_ending(file_path)
        with open(file_path, "rb") as f:
            doc = tomlkit.load(f)
        logger.debug("Файл успешно загружен")
    except Exception as e:
        logger.warning("Не удалось загрузить TOML-файл %s: %s", file_path, e)
        return False

    if "dependencies" not in doc:
        doc["dependencies"] = tomlkit.table()
        logger.debug("Создана секция [dependencies]")
    deps = doc["dependencies"]

    updated = False

    if dep_name in deps:
        logger.debug("Зависимость %s уже существует, обновление", dep_name)
        updated = _update_in_section(deps, dep_name, new_version)
    else:
        if features:
            dep_table = tomlkit.inline_table()
            dep_table["version"] = new_version
            dep_table["features"] = tomlkit.array(features)
            deps[dep_name] = dep_table
            logger.debug("Добавлена составная зависимость %s (features: %s)", dep_name, features)
        else:
            deps[dep_name] = new_version
            logger.debug("Добавлена простая зависимость %s", dep_name)
        updated = True

    if updated:
        try:
            _write_with_line_ending(file_path, doc, line_ending)
            logger.info("Файл %s обновлён: %s -> %s", file_path.name, dep_name, new_version)
        except Exception as e:
            logger.error("Не удалось записать обновлённый %s: %s", file_path, e)
            return False
        return True

    return False


def _update_in_section(section, dep_name: str, new_version: str) -> bool:
    """Внутренняя функция обновления версии в конкретной секции (dependencies, dev-deps, build-deps)."""
    if dep_name not in section:
        return False

    dep_value = section[dep_name]
    if isinstance(dep_value, dict):
        if "version" in dep_value:
            old_version = dep_value["version"]
            dep_value["version"] = new_version
            logger.debug("Обновлена версия в словаре: %s -> %s", old_version, new_version)
            return True
    elif isinstance(dep_value, str):
        old_version = dep_value
        section[dep_name] = new_version
        logger.debug("Обновлена версия: %s -> %s", old_version, new_version)
        return True
    logger.debug("Зависимость %s не является строкой или словарём, тип=%s", dep_name, type(dep_value).__name__)
    return False