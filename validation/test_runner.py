"""
Stage G.1 + G.2 — Tests as error source.

Запускает тестовый набор проекта (`cargo test` / `pytest` / `jest`) и
превращает падения в Webbles error-dict'ы с `code = "TEST_FAILURE"`. Это
открывает семантические баги, которые компилятор/линтер не видят: код
компилируется, но ведёт себя неверно — об этом говорит только тест.

Дизайн:
  * `TestRunner.run(project_path, language) -> (passed, errors)`.
  * Диспетчер по языку; `available(language)` — дешёвая проверка наличия тула.
  * При отсутствии тула / таймауте / краше — graceful: `(True, [])`
    (нет сигнала о падении ≠ есть падение; не блокируем pipeline).
  * Парсеры — статические методы над СЫРЫМ текстом вывода, поэтому
    тестируются на зафиксированных образцах без реального прогона.

error-dict совместим с выдачей анализаторов (см. analyzers/base_analyzer.py):
  file, line, column, message, code, severity, error_type, error_class.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TEST_FAILURE_CODE = "TEST_FAILURE"
TEST_FAILURE_CLASS = "TEST_FAILURE"


@dataclass
class TestFailure:
    """Одно упавшее тестовое наблюдение до нормализации в error-dict."""

    name: str = ""
    file: str = ""
    line: int = 0
    message: str = ""

    def to_error(self) -> Dict[str, Any]:
        msg = self.message.strip() or "test failed"
        if self.name:
            msg = f"{self.name}: {msg}"
        return {
            "file": self.file or "",
            "line": int(self.line or 0),
            "column": 0,
            "message": msg,
            "code": TEST_FAILURE_CODE,
            "severity": "error",
            "error_type": "test",
            "error_class": TEST_FAILURE_CLASS,
            # confidence/base_weight — чтобы не переклассифицировалось в UNKNOWN
            # (контракт analyzers/base_analyzer.py).
            "confidence": 0.9,
            "base_weight": 10.0,
            # Имя теста пригодится при test-pass валидации (G.5).
            "test_name": self.name,
        }


class TestRunner:
    """Запуск тестов проекта + нормализация падений в error-dict'ы."""

    TIMEOUT_SEC = 300

    # --- какой инструмент за какой язык отвечает ---------------------
    _TOOL = {
        "rust": "cargo", "rs": "cargo",
        "python": "pytest", "py": "pytest",
        "javascript": "npx", "js": "npx",
        "typescript": "npx", "ts": "npx",
    }

    # -----------------------------------------------------------------
    def available(self, language: str) -> bool:
        tool = self._TOOL.get((language or "").lower())
        return bool(tool) and shutil.which(tool) is not None

    # -----------------------------------------------------------------
    def run(self, project_path, language: str) -> Tuple[bool, List[Dict[str, Any]]]:
        """Прогнать тесты. Вернуть `(passed, errors)`.

        `passed=True, errors=[]` — тесты прошли ИЛИ тул недоступен/упал
        (мы не выдумываем падений на пустом месте). `passed=False` +
        непустой `errors` — есть реальные падения.
        """
        lang = (language or "").lower()
        if not self.available(language):
            logger.info("TestRunner: тул для %s недоступен — пропуск", language or "?")
            return True, []

        path = Path(project_path)
        if lang in ("rust", "rs"):
            return self._run(path, ["cargo", "test", "--no-fail-fast"],
                             self.parse_cargo_test)
        if lang in ("python", "py"):
            return self._run(path, ["pytest", "-q", "--no-header", "-rN"],
                             self.parse_pytest)
        if lang in ("javascript", "js", "typescript", "ts"):
            return self._run(path, ["npx", "jest", "--ci"],
                             self.parse_jest)
        return True, []

    # -----------------------------------------------------------------
    def _run(self, path: Path, cmd: List[str], parser) -> Tuple[bool, List[Dict[str, Any]]]:
        try:
            proc = subprocess.run(cmd, cwd=str(path), capture_output=True,
                                  text=True, timeout=self.TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            logger.warning("TestRunner: %s превысил таймаут %ds", cmd[0], self.TIMEOUT_SEC)
            return True, []
        except Exception as e:
            logger.debug("TestRunner: %s упал: %s", cmd[0], e)
            return True, []

        # rc == 0 → тесты прошли. Парсим всегда из обоих потоков.
        combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
        failures = parser(combined)
        if proc.returncode == 0:
            return True, []
        errors = [f.to_error() for f in failures]
        # rc != 0, но парсер ничего не выделил — отдаём один обобщённый
        # error-dict, чтобы факт падения не потерялся.
        if not errors:
            errors = [TestFailure(name="", message="test suite failed "
                                  f"(exit code {proc.returncode})").to_error()]
        logger.info("TestRunner: %d падений тестов", len(errors))
        return False, errors

    # =================================================================
    # Парсеры (статические, над сырым текстом) — G.2
    # =================================================================

    # ---- cargo test -------------------------------------------------
    # Блоки деталей:
    #   ---- tests::adds_correctly stdout ----
    #   thread 'tests::adds_correctly' panicked at 'assertion failed:
    #   `(left == right)`', src/lib.rs:42:9
    _CARGO_HEAD_RE = re.compile(r"^----\s+(\S+)\s+stdout\s+----\s*$", re.MULTILINE)
    _CARGO_PANIC_RE = re.compile(
        r"panicked at\s+'?(?P<msg>.*?)'?\s*,\s*(?P<file>[^\s:]+):(?P<line>\d+):\d+",
        re.DOTALL,
    )
    # Новый формат rustc (1.65+): "panicked at src/lib.rs:42:9:\n<msg>"
    _CARGO_PANIC_NEW_RE = re.compile(
        r"panicked at\s+(?P<file>[^\s:]+):(?P<line>\d+):\d+:\s*\n\s*(?P<msg>.+)",
    )

    @classmethod
    def parse_cargo_test(cls, text: str) -> List["TestFailure"]:
        if not text:
            return []
        failures: List[TestFailure] = []
        # Разбиваем на блоки по заголовкам "---- <name> stdout ----".
        heads = list(cls._CARGO_HEAD_RE.finditer(text))
        for i, h in enumerate(heads):
            name = h.group(1)
            start = h.end()
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            block = text[start:end]
            file, line, msg = "", 0, ""
            m = cls._CARGO_PANIC_NEW_RE.search(block) or cls._CARGO_PANIC_RE.search(block)
            if m:
                file = m.group("file")
                line = int(m.group("line"))
                msg = (m.group("msg") or "").strip()
            failures.append(TestFailure(name=name, file=file, line=line,
                                        message=msg or "test panicked"))
        # Фоллбэк: блоков деталей нет, но есть секция "failures:" с именами.
        if not failures:
            for name in cls._cargo_failure_names(text):
                failures.append(TestFailure(name=name, message="test failed"))
        return failures

    @staticmethod
    def _cargo_failure_names(text: str) -> List[str]:
        names: List[str] = []
        in_block = False
        for ln in text.splitlines():
            s = ln.strip()
            if s == "failures:":
                in_block = True
                continue
            if in_block:
                if not s or s.startswith("test result"):
                    in_block = False
                    continue
                # строки вида "    tests::adds_correctly"
                if not s.endswith("stdout ----"):
                    names.append(s)
        # уникализируем, сохраняя порядок
        seen = set()
        return [n for n in names if not (n in seen or seen.add(n))]

    # ---- pytest -----------------------------------------------------
    # Короткая сводка (-rN / по умолчанию с -ra):
    #   FAILED tests/test_math.py::test_add - assert 3 == 4
    #   ERROR tests/test_x.py::test_y - ImportError: ...
    _PYTEST_SUMMARY_RE = re.compile(
        r"^(?:FAILED|ERROR)\s+(?P<path>[^\s:]+)::(?P<name>[^\s]+?)(?:\s+-\s+(?P<msg>.*))?$",
        re.MULTILINE,
    )

    @classmethod
    def parse_pytest(cls, text: str) -> List["TestFailure"]:
        if not text:
            return []
        failures: List[TestFailure] = []
        seen = set()
        for m in cls._PYTEST_SUMMARY_RE.finditer(text):
            path = m.group("path")
            name = m.group("name")
            key = (path, name)
            if key in seen:
                continue
            seen.add(key)
            failures.append(TestFailure(
                name=name, file=path, line=0,
                message=(m.group("msg") or "test failed").strip(),
            ))
        return failures

    # ---- jest -------------------------------------------------------
    #   ● math › adds two numbers
    #     ...
    #       at Object.<anonymous> (test/math.test.js:5:19)
    # Только `●` несёт блок детали падения со стеком. `✕`/`×` — это строки
    # статуса в дереве тестов (без файла/строки); их не парсим, иначе
    # получаем дубль без span.
    _JEST_BULLET_RE = re.compile(r"^\s*●\s+(?P<name>.+?)\s*$", re.MULTILINE)
    _JEST_AT_RE = re.compile(r"\(?(?P<file>[^\s()]+\.[jt]sx?):(?P<line>\d+):\d+\)?")

    @classmethod
    def parse_jest(cls, text: str) -> List["TestFailure"]:
        if not text:
            return []
        failures: List[TestFailure] = []
        bullets = list(cls._JEST_BULLET_RE.finditer(text))
        for i, b in enumerate(bullets):
            name = b.group("name").strip()
            start = b.end()
            end = bullets[i + 1].start() if i + 1 < len(bullets) else len(text)
            block = text[start:end]
            file, line = "", 0
            m = cls._JEST_AT_RE.search(block)
            if m:
                file = m.group("file")
                line = int(m.group("line"))
            # первая непустая строка блока как сообщение
            msg = next((ln.strip() for ln in block.splitlines() if ln.strip()), "")
            failures.append(TestFailure(name=name, file=file, line=line,
                                        message=msg or "test failed"))
        return failures
