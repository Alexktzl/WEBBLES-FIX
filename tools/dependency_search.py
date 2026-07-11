"""
Агрегатор поиска безопасных версий пакетов.
Ищет на crates.io, в GitHub Releases (для git-зависимостей) и в базе RustSec.
Поддерживает major_lock для безопасного обновления в пределах текущей мажорной версии.
"""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


class DependencySearch:
    """Ищет максимальную стабильную версию пакета."""

    # ------------------------------------------------------------------
    # Основные публичные методы
    # ------------------------------------------------------------------

    @staticmethod
    def search(package_name: str) -> Optional[str]:
        """
        Поиск без ограничений по версии.
        Возвращает самую свежую стабильную версию из трёх источников.
        """
        version = DependencySearch._search_cratesio(package_name, major_filter=None)
        if version:
            logger.info(f"Версия {package_name} найдена на crates.io: {version}")
            return version

        version = DependencySearch._search_rustsec(package_name, major_filter=None)
        if version:
            logger.info(f"Безопасная версия {package_name} найдена через RustSec: {version}")
            return version

        version = DependencySearch._search_github(package_name, major_filter=None)
        if version:
            logger.info(f"Версия {package_name} найдена через GitHub Releases: {version}")
            return version

        return None

    @staticmethod
    def search_safe_version(
        package_name: str,
        current_version: Optional[str] = None,
        major_lock: bool = True
    ) -> Optional[str]:
        """
        Поиск безопасной версии с учётом major_lock.
        Если major_lock == True и current_version указана, возвращаем
        максимальную версию в рамках той же мажорной цифры.
        Если major_lock == False или current_version не задана, работаем как search().
        Возвращает None, если подходящая версия не найдена.
        """
        if not major_lock or not current_version:
            return DependencySearch.search(package_name)

        major = current_version.split('.')[0] if '.' in current_version else current_version
        logger.debug(f"major_lock active for {package_name}: current={current_version}, major={major}")

        # 1. crates.io с фильтром по мажорной версии
        version = DependencySearch._search_cratesio(package_name, major_filter=major)
        if version:
            logger.info(f"Безопасная версия {package_name} (major-locked) найдена на crates.io: {version}")
            return version

        # 2. RustSec с фильтром
        version = DependencySearch._search_rustsec(package_name, major_filter=major)
        if version:
            logger.info(f"Безопасная версия {package_name} (major-locked) найдена через RustSec: {version}")
            return version

        # 3. GitHub с фильтром
        version = DependencySearch._search_github(package_name, major_filter=major)
        if version:
            logger.info(f"Безопасная версия {package_name} (major-locked) найдена через GitHub Releases: {version}")
            return version

        logger.info(f"Не найдена безопасная версия {package_name} в мажорной ветке {major}")
        return None

    # ------------------------------------------------------------------
    # Поиск на crates.io
    # ------------------------------------------------------------------

    @staticmethod
    def _search_cratesio(package_name: str, major_filter: Optional[str] = None) -> Optional[str]:
        """
        Возвращает максимальную не-yanked версию.
        Если major_filter задан, рассматриваем только версии с такой же мажорной цифрой.
        """
        try:
            import requests
            url = f"https://crates.io/api/v1/crates/{package_name}"
            resp = requests.get(url, headers={"User-Agent": "webbles_fix"}, timeout=10)
            if resp.status_code != 200:
                return None
            data = resp.json()
            versions = data.get("crate", {}).get("versions", [])
            best = None
            for ver in versions:
                if ver.get("yanked", False):
                    continue
                num = ver["num"]
                if not re.match(r'^\d+\.\d+\.\d+', num):
                    continue
                if major_filter is not None and not num.startswith(f"{major_filter}."):
                    continue
                # Выбираем максимальную (сравнение строк для семверов работает корректно)
                if best is None or _version_greater(num, best):
                    best = num
            return best
        except Exception as e:
            logger.debug(f"crates.io error for {package_name}: {e}")
        return None

    # ------------------------------------------------------------------
    # Поиск в RustSec Advisory DB
    # ------------------------------------------------------------------

    @staticmethod
    def _search_rustsec(package_name: str, major_filter: Optional[str] = None) -> Optional[str]:
        """
        Ищет patched-версии в RustSec, возвращает максимальную.
        Если major_filter задан, фильтрует по мажорной цифре.
        """
        try:
            import requests
            url = f"https://rustsec.org/api/v1/advisories?query={package_name}"
            resp = requests.get(url, headers={"User-Agent": "webbles_fix"}, timeout=10)
            if resp.status_code != 200:
                return None
            advisories = resp.json()
            best = None
            for adv in advisories:
                patched = adv.get("advisory", {}).get("patched", {})
                if not patched:
                    continue
                for constraint in patched.values():
                    match = re.search(r'(\d+\.\d+\.\d+)', constraint)
                    if match:
                        ver = match.group(1)
                        if major_filter is not None and not ver.startswith(f"{major_filter}."):
                            continue
                        if best is None or _version_greater(ver, best):
                            best = ver
            return best
        except Exception as e:
            logger.debug(f"RustSec error for {package_name}: {e}")
        return None

    # ------------------------------------------------------------------
    # Поиск на GitHub (Releases и Tags)
    # ------------------------------------------------------------------

    @staticmethod
    def _search_github(package_name: str, major_filter: Optional[str] = None) -> Optional[str]:
        """
        Ищет последний релизный тег через GitHub API, используя данные crates.io.
        Если major_filter задан, выбирает только теги с совпадающей мажорной версией.
        """
        repo_url = DependencySearch._get_repository_url(package_name)
        if not repo_url:
            return None

        match = re.search(r'github\.com/([^/]+)/([^/]+?)(?:\.git)?$', repo_url)
        if not match:
            return None
        owner, repo = match.group(1), match.group(2)

        # Сначала пробуем последний релиз
        version = DependencySearch._get_latest_release(owner, repo, major_filter)
        if version:
            return version

        # Запасной вариант: список тегов
        version = DependencySearch._get_latest_tag(owner, repo, major_filter)
        return version

    @staticmethod
    def _get_repository_url(package_name: str) -> Optional[str]:
        """Извлекает URL репозитория из данных crates.io."""
        try:
            import requests
            url = f"https://crates.io/api/v1/crates/{package_name}"
            resp = requests.get(url, headers={"User-Agent": "webbles_fix"}, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("crate", {}).get("repository", "")
        except Exception as e:
            logger.debug(f"Failed to get repository URL for {package_name}: {e}")
        return None

    @staticmethod
    def _get_latest_release(owner: str, repo: str, major_filter: Optional[str] = None) -> Optional[str]:
        """Получает последний релизный тег через GitHub API."""
        try:
            import requests
            url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
            resp = requests.get(url, headers={"User-Agent": "webbles_fix"}, timeout=10)
            if resp.status_code != 200:
                return None
            data = resp.json()
            tag = data.get("tag_name", "")
            if tag.startswith("v"):
                tag = tag[1:]
            if re.match(r'^\d+\.\d+\.\d+', tag):
                if major_filter is None or tag.startswith(f"{major_filter}."):
                    return tag
        except Exception as e:
            logger.debug(f"GitHub Releases API error for {owner}/{repo}: {e}")
        return None

    @staticmethod
    def _get_latest_tag(owner: str, repo: str, major_filter: Optional[str] = None) -> Optional[str]:
        """Запасной вариант: получает список тегов и берёт последний подходящий."""
        try:
            import requests
            url = f"https://api.github.com/repos/{owner}/{repo}/tags"
            resp = requests.get(url, headers={"User-Agent": "webbles_fix"}, timeout=10)
            if resp.status_code != 200:
                return None
            tags = resp.json()
            for tag_info in tags:
                tag = tag_info.get("name", "")
                if tag.startswith("v"):
                    tag = tag[1:]
                if re.match(r'^\d+\.\d+\.\d+', tag):
                    if major_filter is None or tag.startswith(f"{major_filter}."):
                        return tag
        except Exception as e:
            logger.debug(f"GitHub Tags API error for {owner}/{repo}: {e}")
        return None


# ------------------------------------------------------------------
# Вспомогательные функции сравнения версий (без библиотек)
# ------------------------------------------------------------------

def _version_greater(a: str, b: str) -> bool:
    """True, если a > b в смысле семвер (x.y.z)."""
    parts_a = [int(x) for x in a.split('.')[:3]]
    parts_b = [int(x) for x in b.split('.')[:3]]
    return parts_a > parts_b


def get_current_version_from_toml(file_path: str, package_name: str) -> Optional[str]:
    """
    Извлекает текущую версию пакета из Cargo.toml.
    Поддерживает простые и составные формы.
    Возвращает строку версии или None.
    """
    try:
        import tomlkit
        with open(file_path, "rb") as f:
            doc = tomlkit.load(f)
        for section_name in ("dependencies", "dev-dependencies", "build-dependencies"):
            section = doc.get(section_name, {})
            if package_name in section:
                dep = section[package_name]
                if isinstance(dep, dict):
                    return dep.get("version", None)
                elif isinstance(dep, str):
                    return dep
    except Exception as e:
        logger.debug(f"Error reading Cargo.toml for version: {e}")
    return None