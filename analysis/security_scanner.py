"""
Stage K — встроенный детектор уязвимостей (детерминированный, без сети/LLM).

Зачем отдельно от Semgrep: Semgrep — опциональный внешний бинарь (может быть
не установлен, требует `--config=auto`, иногда сеть). Этот сканер работает
всегда, офлайн, на чистых regex/строковых правилах, и его результат
**воспроизводим до символа** — поэтому он годится для бенчмарка (см.
`tests/run_benchmark.py`, security-кейсы).

Контракт находки совпадает с тем, как `AnalyzeStage` конвертирует Semgrep:
`file, line, column, message, code, severity, error_type`. Дополнительно
сканер кладёт `error_class="SECURITY"` (для веса в `ErrorClassifier`,
см. `analysis/error_classifier.py`), `confidence` и `autofixable` —
последнее подсказывает rule-based security-фиксеру (K.7), можно ли чинить
автоматически (тривиальный однострочный фикс) или вести в NEEDS_REVIEW.

Коды, которые эмитит сканер (под них уже заведены DO/DON'T в
`analysis/constraints/error_constraints.py` и кейсы в бенчмарке):
    hardcoded_secret, dangerous_eval, unsafe_deserialization,
    sql_injection, command_injection, xss

Принципы (как и у остального проекта):
* Лучше пропустить, чем поднять ложную тревогу: правила узкие и операционные.
* Каждое правило — одна строка контекста (line-oriented). Многострочный
  анализ (taint) сюда не входит — это honest-ограничение, как regex в
  symbol_graph/project_symbol_index.
* Комментарии-строки пропускаются (грубо, по префиксу), чтобы закомментированный
  `eval(...)` не считался дырой.
"""

from __future__ import annotations

import io
import logging
import re
import tokenize as _tokenize
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

# tokenize.TokenizeError was renamed to TokenizeError→TokenError in Python 3.13
_TOKENIZE_ERROR = getattr(_tokenize, 'TokenizeError', _tokenize.TokenError)

logger = logging.getLogger(__name__)


# Нормализация имени языка → семейство правил.
_LANG_FAMILY = {
    "python": "python", "py": "python",
    "javascript": "js", "js": "js", "jsx": "js",
    "typescript": "js", "ts": "js", "tsx": "js",
    "rust": "rust", "rs": "rust",
    "csharp": "csharp", "cs": "csharp", "c#": "csharp",
    "cpp": "cpp", "c++": "cpp", "cxx": "cpp", "cc": "cpp", "c": "cpp",
}

# Префиксы строк-комментариев по семейству — такие строки пропускаем.
_COMMENT_PREFIX = {
    "python": ("#",),
    "js": ("//",),
    "rust": ("//",),
    "csharp": ("//",),
    "cpp": ("//",),
}

# Паттерны опасного кода, которые могут встречаться в комментариях
# (закомментированный exploit, забытый отладочный вызов).
# Используются только через _extract_comment_tokens (tokenize-путь),
# поэтому '#' внутри строк — например x = "abc#eval(d)" — не является
# COMMENT-токеном и сюда не попадёт.
_CODE_IN_COMMENT = re.compile(
    r"(?<![\.\w])(eval|exec)\s*\("
    r"|(?<![\.\w])os\.system\s*\("
    r"|subprocess\."
    r"|(?<![\.\w])pickle\.loads?\s*\("
    r"|(?<![\.\w])yaml\.load\s*\(",
)


def _extract_comment_tokens(source: str) -> List[Tuple[int, str]]:
    """Возвращает (lineno, comment_string) для всех COMMENT-токенов в Python-источнике.

    Использует tokenize, поэтому '#' внутри строкового литерала
    (e.g. x = "abc#eval(d)") НЕ попадает в результат — это не comment-токен.
    При TokenizeError (неполный/битый исходник) возвращает то, что успело разобраться.
    """
    out: List[Tuple[int, str]] = []
    try:
        for tok_type, tok_string, tok_start, _end, _line in _tokenize.generate_tokens(
            io.StringIO(source).readline
        ):
            if tok_type == _tokenize.COMMENT:
                out.append((tok_start[0], tok_string))
    except _TOKENIZE_ERROR:
        pass
    return out


# Ключевые слова в имени переменной, выдающие секрет.
_SECRET_NAME = re.compile(
    r"(secret|passwd|password|pwd|token|api[_-]?key|apikey|access[_-]?key|"
    r"private[_-]?key|auth[_-]?token|client[_-]?secret)",
    re.IGNORECASE,
)
# Признак чтения секрета из окружения — такие строки НЕ являются находкой.
_ENV_READ = re.compile(
    r"(os\.environ|os\.getenv|getenv|process\.env|std::env::var|dotenv|"
    r"environ\[)",
    re.IGNORECASE,
)


def _is_hardcoded_secret_py(line: str) -> bool:
    if _ENV_READ.search(line):
        return False
    m = re.search(r"""^\s*([A-Za-z_][A-Za-z0-9_\.]*)\s*=\s*(['"])(.*?)\2""", line)
    if not m:
        return False
    name, value = m.group(1), m.group(3)
    return bool(_SECRET_NAME.search(name)) and len(value) >= 6


def _is_hardcoded_secret_js(line: str) -> bool:
    if _ENV_READ.search(line):
        return False
    m = re.search(
        r"""(?:const|let|var)\s+([A-Za-z0-9_$]+)\s*=\s*(['"`])(.*?)\2""", line
    )
    if not m:
        return False
    name, value = m.group(1), m.group(3)
    return bool(_SECRET_NAME.search(name)) and len(value) >= 6


def _is_hardcoded_secret_rust(line: str) -> bool:
    if _ENV_READ.search(line):
        return False
    m = re.search(
        r"""let\s+(?:mut\s+)?([A-Za-z0-9_]+)\s*(?::\s*&?[A-Za-z0-9_]+\s*)?=\s*"(.*?)"""
        r'"',
        line,
    )
    if not m:
        return False
    name, value = m.group(1), m.group(2)
    return bool(_SECRET_NAME.search(name)) and len(value) >= 4


def _is_sql_injection_rust(line: str) -> bool:
    """Rust SQL-инъекция (2026-07-09): динамическая сборка SQL, переданная в
    query-исполнитель. Узко и операционно, как python-детектор:
    1) есть вызов-исполнитель запроса (query/execute/sql_query/query_as/...);
    2) в строке присутствует SQL-ключевое слово;
    3) SQL строится динамически — format!/конкатенация/push_str.
    Параметризованный `.bind(...)` без format! не триггерит (нет п.3)."""
    if not re.search(r"\b(query|query_as|query_scalar|execute|sql_query|batch_execute)\s*\(",
                     line):
        return False
    # sqlx AssertSqlSafe(...) — санкционированный escape-hatch, разработчик
    # явно ручается за безопасность динамической строки (аналог параметризации).
    # Живая проверка 2026-07-09 (sqlx/tests): единственное срабатывание на 524
    # файла rusqlite+sqlx было именно AssertSqlSafe(format!(...)) в тесте.
    # Принцип «лучше пропустить, чем ложная тревога» → исключаем.
    if "AssertSqlSafe" in line:
        return False
    has_sql = re.search(r"""['"].*\b(SELECT|INSERT|UPDATE|DELETE|FROM|WHERE)\b""",
                        line, re.IGNORECASE)
    if not has_sql:
        return False
    # динамическая сборка: format! / конкатенация (+ &) / push_str
    dynamic = re.search(r"""format!\s*\(|\+\s*&|\.push_str\s*\(|"\s*\+""", line)
    return bool(dynamic)


def _is_sql_injection_py(line: str) -> bool:
    if not re.search(r"\.execute(?:many)?\s*\(", line):
        return False
    has_sql = re.search(r"""['"].*\b(SELECT|INSERT|UPDATE|DELETE|FROM|WHERE)\b""",
                        line, re.IGNORECASE)
    if not has_sql:
        return False
    # динамическая сборка строки: конкатенация / % / .format / f-string
    dynamic = re.search(r"""(\+|%|\.format\s*\(|f['"])""", line)
    return bool(dynamic)


# ---- C# детекторы (2026-07-11, security-фиксеры на все языки) ----
def _is_sql_injection_csharp(line: str) -> bool:
    if not re.search(r"\b(SqlCommand|CommandText|OleDbCommand|MySqlCommand|"
                     r"NpgsqlCommand|FromSqlRaw|ExecuteSqlRaw)\b", line):
        return False
    has_sql = re.search(r"""['"$].*\b(SELECT|INSERT|UPDATE|DELETE|FROM|WHERE)\b""",
                        line, re.IGNORECASE)
    if not has_sql:
        return False
    # динамика: интерполяция $"..." / конкатенация / String.Format
    return bool(re.search(r'\$"|"\s*\+|\+\s*"|String\.Format', line))


def _is_command_injection_csharp(line: str) -> bool:
    if not re.search(r"\bProcess\.Start\b|\bProcessStartInfo\b", line):
        return False
    return bool(re.search(r'\$"|"\s*\+|\+\s*"|String\.Format', line))


def _is_hardcoded_secret_csharp(line: str) -> bool:
    if _ENV_READ.search(line) or "GetEnvironmentVariable" in line or "IConfiguration" in line:
        return False
    m = re.search(r'(?:const\s+string|readonly\s+string|string|var)\s+'
                  r'([A-Za-z0-9_]+)\s*=\s*"(.*?)"', line)
    if not m:
        return False
    return bool(_SECRET_NAME.search(m.group(1))) and len(m.group(2)) >= 4


def _is_unsafe_deser_csharp(line: str) -> bool:
    return bool(re.search(
        r"\b(BinaryFormatter|NetDataContractSerializer|LosFormatter|SoapFormatter|"
        r"ObjectStateFormatter)\b", line))


# ---- C++ детекторы ----
def _is_command_injection_cpp(line: str) -> bool:
    if not re.search(r"\b(system|popen|_popen|execlp|execvp)\s*\(", line):
        return False
    # динамика: конкатенация / .c_str() построенной строки / sprintf-буфер
    return bool(re.search(r"\+|\.c_str\s*\(\)|sprintf|snprintf|format", line))


def _is_buffer_overflow_cpp(line: str) -> bool:
    # небезопасные строковые функции без границ
    if not re.search(r"\b(strcpy|strcat|sprintf|gets|vsprintf|scanf)\s*\(", line):
        return False
    # безопасные аналоги на той же строке — не тревога
    if re.search(r"\b(strncpy|strncat|snprintf|vsnprintf|fgets)\b", line):
        return False
    return True


def _is_hardcoded_secret_cpp(line: str) -> bool:
    if "getenv" in line:
        return False
    m = re.search(r'(?:const\s+)?(?:char\s*\*|std::string|auto|string)\s+'
                  r'([A-Za-z0-9_]+)\s*=\s*"(.*?)"', line)
    if not m:
        return False
    return bool(_SECRET_NAME.search(m.group(1))) and len(m.group(2)) >= 4


# Описание одного правила. `match` — компилированный regex ИЛИ предикат(line).
class _Rule:
    __slots__ = ("code", "severity", "confidence", "autofixable", "message",
                 "match", "exclude")

    def __init__(self, code: str, severity: str, confidence: float,
                 autofixable: bool, message: str,
                 match: Union["re.Pattern[str]", Callable[[str], bool]],
                 exclude: "Optional[re.Pattern[str]]" = None):
        self.code = code
        self.severity = severity
        self.confidence = confidence
        self.autofixable = autofixable
        self.message = message
        self.match = match
        self.exclude = exclude

    def hit(self, line: str) -> bool:
        if self.exclude is not None and self.exclude.search(line):
            return False
        if callable(self.match):
            return self.match(line)
        return self.match.search(line) is not None


# Синглтон-правило для code_in_comment (используется вне _RULES,
# в отдельном tokenize-пути).
_RULE_CODE_IN_COMMENT = _Rule(
    "code_in_comment", "warning", 0.40, False,
    "dangerous code pattern inside comment (commented-out or debug code?)",
    _CODE_IN_COMMENT,
)

# `(?<![\.\w])` — не часть `obj.eval` и не часть слова (`retrieval`).
_RULES: Dict[str, List[_Rule]] = {
    "python": [
        # autofixable=True: RuleBasedFixer._py_hardcoded_secret заменит
        # `NAME = "literal"` на `NAME = os.environ["NAME"]` и добавит `import os`.
        _Rule("hardcoded_secret", "high", 0.85, True,
              "hardcoded secret literal in source",
              _is_hardcoded_secret_py),
        # autofixable=False: RuleBasedFixer._py_dangerous_eval обрабатывает только
        # `eval(input(...))`. Произвольный `eval(x)` требует LLM/NEEDS_REVIEW.
        _Rule("dangerous_eval", "high", 0.70, False,
              "use of eval()/exec() on dynamic input",
              re.compile(r"(?<![\.\w])(eval|exec)\s*\(")),
        _Rule("unsafe_deserialization", "high", 0.85, True,
              "unsafe deserialization (yaml.load / pickle)",
              re.compile(r"(?<![\.\w])yaml\.load\s*\(|(?<![\.\w])pickle\.loads?\s*\(")),
        _Rule("sql_injection", "high", 0.60, False,
              "SQL query built via string formatting/concatenation",
              _is_sql_injection_py),
        _Rule("command_injection", "high", 0.60, False,
              "shell command from untrusted input",
              re.compile(r"(?<![\.\w])os\.system\s*\(|shell\s*=\s*True")),
    ],
    "js": [
        _Rule("hardcoded_secret", "high", 0.85, False,
              "hardcoded secret literal in source",
              _is_hardcoded_secret_js),
        _Rule("dangerous_eval", "high", 0.70, False,
              "use of eval()/new Function() on dynamic input",
              re.compile(r"(?<![\.\w])eval\s*\(|new\s+Function\s*\(")),
        _Rule("xss", "high", 0.80, True,
              "untrusted value written to the DOM as HTML",
              re.compile(r"\.(inner|outer)HTML\s*=(?!=)|"
                         r"document\.write\s*\(|\.insertAdjacentHTML\s*\(")),
        _Rule("command_injection", "high", 0.60, False,
              "shell command from untrusted input",
              re.compile(r"child_process|(?<![\.\w])exec(Sync)?\s*\([^)]*\+")),
    ],
    "rust": [
        _Rule("hardcoded_secret", "high", 0.85, False,
              "hardcoded secret literal in source",
              _is_hardcoded_secret_rust),
        _Rule("command_injection", "high", 0.60, False,
              "shell command from untrusted input",
              re.compile(r"""Command::new\s*\(\s*"(sh|bash|zsh|cmd|powershell)"\s*\)|"""
                         r'\.arg\s*\(\s*"-c"\s*\)')),
        _Rule("unsafe_deserialization", "medium", 0.70, False,
              "unsafe deserialization of untrusted data",
              re.compile(r"serde_pickle|bincode::deserialize")),
        # 2026-07-09: SQL-инъекция через format!/конкатенацию в query.
        # autofixable=False — контекст-зависимо, чинит LLM по SECURITY_EXAMPLE
        # (sqlx bind $1) под стражем O.20.
        _Rule("sql_injection", "high", 0.60, False,
              "SQL built dynamically from untrusted input",
              _is_sql_injection_rust),
    ],
    # 2026-07-11: security-фиксеры на все готовые языки. autofixable=False —
    # контекст-зависимо, чинит LLM по SECURITY_EXAMPLES под стражем O.20.
    "csharp": [
        _Rule("hardcoded_secret", "high", 0.85, False,
              "hardcoded secret literal in source", _is_hardcoded_secret_csharp),
        _Rule("sql_injection", "high", 0.60, False,
              "SQL built dynamically from untrusted input", _is_sql_injection_csharp),
        _Rule("command_injection", "high", 0.60, False,
              "shell/process command from untrusted input", _is_command_injection_csharp),
        _Rule("unsafe_deserialization", "high", 0.75, False,
              "unsafe deserializer (BinaryFormatter/etc.) executes code",
              _is_unsafe_deser_csharp),
    ],
    "cpp": [
        _Rule("hardcoded_secret", "high", 0.85, False,
              "hardcoded secret literal in source", _is_hardcoded_secret_cpp),
        _Rule("command_injection", "high", 0.60, False,
              "shell command from untrusted input (system/popen)",
              _is_command_injection_cpp),
        _Rule("buffer_overflow", "high", 0.70, False,
              "unbounded string function (strcpy/sprintf/gets)",
              _is_buffer_overflow_cpp),
    ],
}


class SecurityScanner:
    """Детерминированный сканер уязвимостей по строковым паттернам."""

    # Лимит файлов за прогон — чтобы сканер не уходил в минуты на гигантах.
    MAX_FILES = 1000
    # Синхронно с `agent/project_tree.SKIP_DIRS` и `AnalyzeStage._AUDIT_SKIP_DIRS`:
    # помимо служебных и кэшевых каталогов закрываем `.webbles_backups`
    # (копии файлов из прошлых прогонов — реальные ошибки они не отражают,
    # но шумят как "повторные" находки security-сканера) и `.webbles`.
    SKIP_DIRS = {"target", "node_modules", ".git", "venv", ".venv",
                 "__pycache__", ".webbles_fix", ".webbles", ".webbles_backups",
                 ".webles_sandbox", ".webbles_sandbox",
                 "dist", "build", ".idea", ".vscode", ".tox", ".pytest_cache",
                 ".next", ".cache", ".gradle", ".mvn", "statistic"}
    EXTS = {
        "python": (".py",),
        "js": (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"),
        "rust": (".rs",),
        "csharp": (".cs",),
        "cpp": (".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hxx"),
    }

    @staticmethod
    def _family(language: str) -> Optional[str]:
        return _LANG_FAMILY.get((language or "").strip().lower())

    def scan_source(self, source: str, language: str,
                    file_rel: str = "") -> List[Dict[str, Any]]:
        """Сканирует текст одного файла, возвращает список error-dict'ов."""
        family = self._family(language)
        if not family or not source:
            return []
        rules = _RULES.get(family, [])
        if not rules:
            return []
        comment_prefixes = _COMMENT_PREFIX.get(family, ())
        findings: List[Dict[str, Any]] = []
        for lineno, raw in enumerate(source.splitlines(), start=1):
            stripped = raw.lstrip()
            if comment_prefixes and stripped.startswith(comment_prefixes):
                continue
            for rule in rules:
                try:
                    if rule.hit(raw):
                        findings.append(self._to_error(rule, file_rel, lineno, raw))
                except Exception:  # одно битое правило не валит весь скан
                    continue

        # Python only: detect dangerous patterns inside real COMMENT tokens.
        # tokenize correctly distinguishes '#' in strings from actual comments,
        # so x = "abc#eval(d)" does NOT trigger (no COMMENT token on that line).
        if family == "python":
            lines = source.splitlines()
            for lineno, comment_text in _extract_comment_tokens(source):
                try:
                    if _RULE_CODE_IN_COMMENT.hit(comment_text):
                        raw_line = lines[lineno - 1] if 0 < lineno <= len(lines) else ""
                        findings.append(
                            self._to_error(_RULE_CODE_IN_COMMENT, file_rel, lineno, raw_line)
                        )
                except Exception:
                    continue

        return findings

    def scan_file(self, path: Union[str, Path], language: str,
                  rel: Optional[str] = None) -> List[Dict[str, Any]]:
        p = Path(path)
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            logger.debug("SecurityScanner: не прочитан %s: %s", p, e)
            return []
        return self.scan_source(text, language, rel if rel is not None else str(p))

    def scan_project(self, work_dir: Union[str, Path],
                     language: str) -> List[Dict[str, Any]]:
        """Обходит исходники нужного языка в проекте (кроме служебных каталогов)."""
        family = self._family(language)
        if not family:
            return []
        exts = self.EXTS.get(family, ())
        root = Path(work_dir)
        findings: List[Dict[str, Any]] = []
        count = 0
        for path in sorted(root.rglob("*")):
            if count >= self.MAX_FILES:
                logger.info("SecurityScanner: достигнут лимит %d файлов", self.MAX_FILES)
                break
            if not path.is_file() or path.suffix not in exts:
                continue
            if any(part in self.SKIP_DIRS for part in path.parts):
                continue
            count += 1
            try:
                rel = str(path.relative_to(root))
            except ValueError:
                rel = str(path)
            findings.extend(self.scan_file(path, language, rel=rel))
        return findings

    @staticmethod
    def _to_error(rule: _Rule, file_rel: str, lineno: int,
                  raw: str) -> Dict[str, Any]:
        col = len(raw) - len(raw.lstrip()) + 1
        return {
            "file": file_rel,
            "line": lineno,
            "column": col,
            "message": rule.message,
            "code": rule.code,
            "severity": rule.severity,
            "error_type": "security",
            "error_class": "SECURITY",
            "confidence": rule.confidence,
            "autofixable": rule.autofixable,
        }
