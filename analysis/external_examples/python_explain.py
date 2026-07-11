"""
Stage P.3 — PythonExplainProvider: параллель `rustc --explain Exxxx` для Python.

Источники объяснения:
  1) `ruff rule <code>` — для ruff-кодов (F401, B006, S102, UP006, и т.д.).
     Отдаёт описание правила + пример «до/после». Покрывает большинство
     P.1-кодов, которые мы детектируем.
  2) `mypy --help-codes` — для mypy-кодов (name-defined, arg-type, etc.).
     mypy не даёт per-code примеров через CLI, но печатает короткое
     описание. Этого достаточно как пояснения в case-file.

Оба тула опциональны. Если ни ruff, ни mypy не в PATH — провайдер
возвращает []. Это безопасно: case-file просто не получит секцию
EXTERNAL EXAMPLES для python-ошибок, что и было до P.3.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from analysis.external_examples.provider import Example, ExampleSearchProvider

logger = logging.getLogger(__name__)


# Ruff-коды: 1-5 заглавных букв + цифры (F401, B006, S102, UP006, SIM102, COM812, PLE0101).
_RUFF_CODE_RE = re.compile(r"^[A-Z]{1,5}\d{2,4}$")
# Mypy-коды: kebab-case (name-defined, attr-defined, arg-type, return-value).
_MYPY_CODE_RE = re.compile(r"^[a-z][a-z0-9-]*$")

# Блок ```python ... ``` в выводе ruff rule.
_PY_FENCE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)


class PythonExplainProvider(ExampleSearchProvider):
    """Объясняет mypy/ruff-коды через локальные CLI-команды.

    Поведение каскадное:
      * ruff-код  → `ruff rule <code>`, если ruff в PATH;
      * mypy-код  → описание из встроенной таблицы (mypy CLI не отдаёт
                    per-code объяснения), плюс ссылка на docs.
    """
    name = "python_explain"
    TIMEOUT_SEC = 10

    # Описания самых частых mypy-кодов — компактные, для case-file.
    # mypy `--help-codes` есть, но не отдаёт описаний на отдельный код,
    # поэтому держим встроенную таблицу. Полный список см. mypy docs.
    _MYPY_DESCRIPTIONS = {
        "name-defined":     "Name is not defined in the current scope. Add an import or define it locally.",
        "used-before-def":  "Name is used before it is defined. Reorder definitions or use forward references.",
        "attr-defined":     "Attribute is not declared on this type. Use an existing attribute or narrow the type with isinstance().",
        "arg-type":         "Argument type does not match the parameter annotation. Convert the value or widen the signature.",
        "return-value":     "Returned value does not match the annotated return type. Convert or widen the return annotation.",
        "assignment":       "RHS type is incompatible with the LHS annotation. Convert the value or fix the annotation.",
        "union-attr":       "Optional/Union attribute access without narrowing. Use `if x is None` / `isinstance(x, T)` first.",
        "call-arg":         "Wrong keyword/positional arguments to the function. Match the signature exactly.",
        "call-overload":    "No overload of the function matches these argument types.",
        "index":            "Container is not indexable with a key of this type.",
        "operator":         "Operator is not supported between these operand types.",
        "list-item":        "List literal element has wrong type.",
        "dict-item":        "Dict literal key/value has wrong type.",
        "type-arg":         "Wrong number of type arguments for a generic.",
        "override":         "Method override has incompatible signature with parent.",
        "import":           "Module cannot be found; check `requirements.txt` and import path.",
        "import-not-found": "Module cannot be found in site-packages or stubs.",
        "import-untyped":   "Module has no type stubs; install `types-<package>` or set `ignore_missing_imports`.",
        "unreachable":      "Code below a terminating statement (return/raise) is unreachable.",
        "no-redef":         "Name is redefined; rename one of the definitions.",
        "var-annotated":    "Variable needs an explicit annotation (e.g. `xs: list[int] = []`).",
        "no-untyped-def":   "Function lacks type annotations.",
    }

    def available(self) -> bool:
        # Доступен, если есть хотя бы один из CLI или есть встроенная mypy-таблица
        # (последняя — всегда есть, так что available=True если язык python).
        # Чтобы не дёргать subprocess впустую — фактическое наличие ruff
        # проверяется внутри _explain_ruff_code.
        return True

    def search(self, error: Dict[str, Any], language: str,
               max_results: int = 3) -> List[Example]:
        if (language or "").lower() not in ("python", "py"):
            return []
        code = (error.get("code") or "").strip()
        if not code:
            return []

        # 1) Ruff-код.
        if _RUFF_CODE_RE.match(code):
            ex = self._explain_ruff_code(code)
            if ex:
                return [ex]

        # 2) Mypy-код.
        if _MYPY_CODE_RE.match(code):
            ex = self._explain_mypy_code(code)
            if ex:
                return [ex]

        return []

    # -----------------------------------------------------------------
    def _explain_ruff_code(self, code: str) -> Optional[Example]:
        if not shutil.which("ruff"):
            return None
        try:
            proc = subprocess.run(
                ["ruff", "rule", code],
                capture_output=True, text=True,
                timeout=self.TIMEOUT_SEC,
            )
        except Exception as e:
            logger.debug("ruff rule %s failed: %s", code, e)
            return None
        if proc.returncode != 0:
            return None
        text = proc.stdout or ""
        if not text.strip():
            return None
        snippet, title = self._parse_ruff_text(text, code)
        return Example(
            source=self.name,
            title=title or f"ruff rule {code}",
            snippet=snippet,
            url=f"https://docs.astral.sh/ruff/rules/#{code.lower()}",
            score=0.85,
        )

    def _explain_mypy_code(self, code: str) -> Optional[Example]:
        desc = self._MYPY_DESCRIPTIONS.get(code)
        if not desc:
            return None
        return Example(
            source=self.name,
            title=f"mypy {code}",
            snippet=desc,
            url=f"https://mypy.readthedocs.io/en/stable/error_code_list.html#code-{code}",
            score=0.80,
        )

    # -----------------------------------------------------------------
    @staticmethod
    def _parse_ruff_text(text: str, code: str):
        """Первый ```python``` блок → snippet; первая строка → title."""
        snippet = ""
        m = _PY_FENCE_RE.search(text)
        if m:
            snippet = m.group(1).strip()
        title = ""
        for ln in text.splitlines():
            s = ln.strip()
            if not s or s.startswith("```") or s.startswith("#"):
                continue
            title = s
            break
        if len(title) > 200:
            title = title[:197] + "..."
        if not snippet:
            snippet = text.strip()
        if len(snippet) > 1500:
            snippet = snippet[:1500].rstrip() + "\n... [truncated]"
        return snippet, title
