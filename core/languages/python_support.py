"""
Языковой провайдер для Python (P.0 mypy + P.1 ruff).
"""

import ast
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from core.language_support import LanguageSupport

logger = logging.getLogger(__name__)


class PythonSupport(LanguageSupport):
    """Провайдер для Python (язык: python)."""

    def __init__(self, language: str = "python"):
        super().__init__(language)

    def create_analyzer(self):
        from analyzers.python_analyzer import PythonAnalyzer
        return PythonAnalyzer()

    def create_syntax_healer(self):
        from fixers.syntax_repair import CriticalSyntaxHealer
        return CriticalSyntaxHealer()

    def create_segmenter(self):
        from fixers.file_segmenter import FileSegmenter
        return FileSegmenter(language="python")

    def get_syntax_healer(self):
        from fixers.language_syntax.python_healer import PythonSyntaxHealer
        return PythonSyntaxHealer()

    def get_error_examples(self) -> Dict[str, str]:
        return {
            "E0602": "--- a/x.py\n+++ b/x.py\n",
            "E999":  "--- a/x.py\n+++ b/x.py\n",
            "NameError": "--- a/x.py\n+++ b/x.py\n",
            "TypeError": "--- a/x.py\n+++ b/x.py\n",
            "AttributeError": "--- a/x.py\n+++ b/x.py\n",
        }

    def get_prompt_enhancements(self, error: Dict[str, Any]) -> str:
        msg = (error.get("message", "") or "").lower()
        code = error.get("code", "")
        hints = []
        if "unexpected eof" in msg or "unexpected end" in msg:
            hints.append("Note: Unexpected EOF in Python often means a missing closing bracket or quote.")
        if "indentation" in msg:
            hints.append("IndentationError: Ensure consistent use of spaces.")
        if "can't assign" in msg or "cannot assign" in msg:
            hints.append("Check the left-hand side of the assignment.")
        if code in ("E999", "invalid-syntax"):
            hints.append("SyntaxError in Python: check colons, parentheses, quotes.")
        return "\n".join(hints) if hints else ""

    def get_classification_rules(self) -> Dict[str, Dict[str, Any]]:
        return {
            # ---- legacy / flake8 / built-in exceptions ----
            "E0602": {"class": "CLEANUP", "allowed_actions": ["remove"]},
            "F841":  {"class": "CLEANUP", "allowed_actions": ["suppress", "remove"]},
            "E999":          {"class": "CRITICAL_SYNTAX", "allowed_actions": ["fix_syntax_heuristic"]},
            "E902":          {"class": "CRITICAL_SYNTAX", "allowed_actions": ["fix_syntax_heuristic"]},
            "invalid-syntax": {"class": "CRITICAL_SYNTAX", "allowed_actions": ["fix_syntax_heuristic"]},
            "NameError":      {"class": "BLOCKING", "allowed_actions": ["define_variable", "add_import"]},
            "TypeError":      {"class": "BLOCKING", "allowed_actions": ["edit_line", "add_cast"]},
            "AttributeError": {"class": "BLOCKING", "allowed_actions": ["fix_method", "add_import"]},
            # ---- P.0: mypy codes ----
            "name-defined":     {"class": "BLOCKING", "allowed_actions": ["define_variable", "add_import"]},
            "used-before-def":  {"class": "BLOCKING", "allowed_actions": ["define_variable"]},
            "attr-defined":     {"class": "BLOCKING", "allowed_actions": ["fix_method", "rename_attr"]},
            "arg-type":         {"class": "BLOCKING", "allowed_actions": ["edit_line", "add_cast"]},
            "return-value":     {"class": "BLOCKING", "allowed_actions": ["edit_line", "add_cast"]},
            "assignment":       {"class": "BLOCKING", "allowed_actions": ["edit_line", "add_cast"]},
            "union-attr":       {"class": "BLOCKING", "allowed_actions": ["edit_line", "narrow_type"]},
            "call-arg":         {"class": "BLOCKING", "allowed_actions": ["edit_line"]},
            "call-overload":    {"class": "BLOCKING", "allowed_actions": ["edit_line"]},
            "index":            {"class": "BLOCKING", "allowed_actions": ["edit_line"]},
            "operator":         {"class": "BLOCKING", "allowed_actions": ["edit_line"]},
            "list-item":        {"class": "BLOCKING", "allowed_actions": ["edit_line"]},
            "dict-item":        {"class": "BLOCKING", "allowed_actions": ["edit_line"]},
            "type-arg":         {"class": "BLOCKING", "allowed_actions": ["edit_line"]},
            "override":         {"class": "STRUCTURAL", "allowed_actions": ["edit_line"]},
            "import":           {"class": "BLOCKING", "allowed_actions": ["add_dependency", "edit_import"]},
            "import-not-found": {"class": "BLOCKING", "allowed_actions": ["add_dependency", "edit_import"]},
            "import-untyped":   {"class": "WARNING",  "allowed_actions": ["suppress", "add_stubs"]},
            "unreachable":      {"class": "CLEANUP",  "allowed_actions": ["remove"]},
            "no-redef":         {"class": "CLEANUP",  "allowed_actions": ["rename"]},
            "var-annotated":    {"class": "WARNING",  "allowed_actions": ["add_annotation"]},
            "no-untyped-def":   {"class": "WARNING",  "allowed_actions": ["add_annotation"]},
            "no-untyped-call":  {"class": "WARNING",  "allowed_actions": ["add_annotation"]},
            "annotation-unchecked": {"class": "WARNING", "allowed_actions": ["suppress"]},
            "redundant-cast":   {"class": "CLEANUP",  "allowed_actions": ["remove"]},
            "unused-coroutine": {"class": "CLEANUP",  "allowed_actions": ["edit_line"]},
            # ---- P.1: ruff codes ----
            # bugbear B - real bugs
            "B006": {"class": "BLOCKING", "allowed_actions": ["edit_line"]},
            "B007": {"class": "CLEANUP",  "allowed_actions": ["rename"]},
            "B008": {"class": "BLOCKING", "allowed_actions": ["edit_line"]},
            "B011": {"class": "BLOCKING", "allowed_actions": ["edit_line"]},
            "B015": {"class": "BLOCKING", "allowed_actions": ["edit_line"]},
            "B018": {"class": "BLOCKING", "allowed_actions": ["edit_line"]},
            "B020": {"class": "CLEANUP",  "allowed_actions": ["rename"]},
            "B023": {"class": "BLOCKING", "allowed_actions": ["edit_line"]},
            "B904": {"class": "STRUCTURAL", "allowed_actions": ["edit_line"]},
            # security S (bandit-lite) -> SECURITY class
            "S101": {"class": "WARNING",  "allowed_actions": ["edit_line"]},
            "S102": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "S105": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "S106": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "S108": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "S110": {"class": "WARNING",  "allowed_actions": ["edit_line"]},
            "S301": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "S307": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "S308": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "S311": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "S324": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "S501": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "S506": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "S602": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "S605": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "S608": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            # SecurityScanner custom codes (hardcoded_secret, sql_injection, etc.)
            "hardcoded_secret":     {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "sql_injection":        {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "command_injection":    {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "dangerous_eval":       {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "unsafe_deserialization": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "code_in_comment":        {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            # bandit B### codes (MEDIUM/HIGH → SECURITY, LOW → WARNING)
            "B102": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B103": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B105": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B106": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B107": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B108": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B110": {"class": "WARNING",  "allowed_actions": ["edit_line"]},
            "B112": {"class": "WARNING",  "allowed_actions": ["edit_line"]},
            "B201": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B301": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B302": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B303": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B304": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B305": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B306": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B307": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B308": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B310": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B311": {"class": "WARNING",  "allowed_actions": ["edit_line"]},
            "B312": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B313": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B314": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B315": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B316": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B317": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B318": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B319": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B320": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B321": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B323": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B324": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B325": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B401": {"class": "WARNING",  "allowed_actions": ["edit_line"]},
            "B402": {"class": "WARNING",  "allowed_actions": ["edit_line"]},
            "B403": {"class": "WARNING",  "allowed_actions": ["edit_line"]},
            "B404": {"class": "WARNING",  "allowed_actions": ["edit_line"]},
            "B405": {"class": "WARNING",  "allowed_actions": ["edit_line"]},
            "B406": {"class": "WARNING",  "allowed_actions": ["edit_line"]},
            "B407": {"class": "WARNING",  "allowed_actions": ["edit_line"]},
            "B408": {"class": "WARNING",  "allowed_actions": ["edit_line"]},
            "B409": {"class": "WARNING",  "allowed_actions": ["edit_line"]},
            "B410": {"class": "WARNING",  "allowed_actions": ["edit_line"]},
            "B411": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B412": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B413": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B501": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B502": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B503": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B504": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B505": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B506": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B507": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B601": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B602": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B603": {"class": "WARNING",  "allowed_actions": ["edit_line"]},
            "B604": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B605": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B606": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B607": {"class": "WARNING",  "allowed_actions": ["edit_line"]},
            "B608": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B609": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B610": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B611": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B701": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B702": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            "B703": {"class": "SECURITY", "allowed_actions": ["edit_line"]},
            # flake8 style — явно CLEANUP, чтобы не конкурировать с security-ошибками
            "E101": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E111": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E114": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E117": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E121": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E122": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E123": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E124": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E125": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E126": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E127": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E128": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E129": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E131": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E201": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E202": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E203": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E211": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E221": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E225": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E226": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E228": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E231": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E241": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E251": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E261": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E262": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E265": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E266": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E271": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E272": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E301": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E302": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E303": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E304": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E305": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E306": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E401": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E501": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E711": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E712": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E721": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "E741": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "W191": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "W291": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "W292": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "W293": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "W391": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "W503": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "W504": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            # pyflakes F
            "F401": {"class": "CLEANUP",  "allowed_actions": ["remove"]},
            "F811": {"class": "CLEANUP",  "allowed_actions": ["rename"]},
            "F821": {"class": "BLOCKING", "allowed_actions": ["define_variable", "add_import"]},
            # pyupgrade UP
            "UP006": {"class": "WARNING", "allowed_actions": ["edit_line"]},
            "UP007": {"class": "WARNING", "allowed_actions": ["edit_line"]},
            "UP008": {"class": "WARNING", "allowed_actions": ["edit_line"]},
            "UP009": {"class": "CLEANUP", "allowed_actions": ["remove"]},
            "UP015": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "UP018": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "UP032": {"class": "WARNING", "allowed_actions": ["edit_line"]},
            # isort I
            "I001": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "I002": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            # pep8-naming N
            "N802": {"class": "WARNING", "allowed_actions": ["rename"]},
            "N803": {"class": "WARNING", "allowed_actions": ["rename"]},
            "N806": {"class": "WARNING", "allowed_actions": ["rename"]},
            "N818": {"class": "WARNING", "allowed_actions": ["rename"]},
            # simplify SIM
            "SIM102": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "SIM108": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "SIM117": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            # comprehensions C4
            "C401": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "C408": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            "C416": {"class": "CLEANUP", "allowed_actions": ["edit_line"]},
            # debugger / print T
            "T201": {"class": "CLEANUP", "allowed_actions": ["remove"]},
            "T203": {"class": "CLEANUP", "allowed_actions": ["remove"]},
        }

    def get_class_weight(self, error_class: str) -> Optional[float]:
        weights = {
            "CRITICAL_SYNTAX": 200.0,
            "BLOCKING": 100.0,
            "SECURITY": 90.0,
            "STRUCTURAL": 70.0,
            "CLEANUP": 30.0,
            "WARNING": 10.0,
            "UNKNOWN": 50.0,
        }
        return weights.get(error_class)

    def get_initial_signatures_provider(self):
        def collect(project_path: Path, context) -> Dict[str, Set]:
            try:
                from analyzers.python_analyzer import PythonAnalyzer
                analyzer = PythonAnalyzer()
                errors = analyzer.analyze(project_path)
                from core.contract import ErrorSignature
                sigs: Dict[str, Set] = {}
                for err in errors:
                    fname = err.get("file", "")
                    if fname:
                        sig_set = sigs.setdefault(fname, set())
                        sig_set.add(ErrorSignature.from_error(err))
                return sigs
            except Exception as e:
                logger.warning(f"Cannot collect initial sigs for Python: {e}")
                return {}
        return collect

    def supports_feature(self, feature: str) -> bool:
        if feature == "tree_sitter":
            return False
        if feature == "linter":
            return True
        return False

    @staticmethod
    def get_default_config() -> Dict[str, Any]:
        return {
            "pipeline": {
                "max_iterations": 15,
                "use_planning": False,
                "invariant_check": False,
            },
        }

    def get_code_keywords(self) -> set:
        return {
            'def ', 'class ', 'import ', 'from ', 'if ', 'elif ', 'else:', 'for ',
            'while ', 'with ', 'try:', 'except ', 'finally:', 'return ', 'print(',
            'assert ', 'raise ', 'yield ', 'lambda ', '@', 'async def '
        }

    def is_cascade_candidate(self, error: Dict[str, Any], file_errors: list) -> bool:
        code = error.get('code', '')
        msg = (error.get('message', '') or '').lower()
        is_critical = code in ('E999', 'invalid-syntax') or 'syntaxerror' in msg or 'indentationerror' in msg
        if is_critical and len(file_errors) >= 3:
            return True
        return False
