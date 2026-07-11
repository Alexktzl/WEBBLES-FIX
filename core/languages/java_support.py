"""
Языковой провайдер для Java.

Регистрируется автоматически через `_discover_and_register` в
`core/languages/__init__.py`. Контракт — как у `csharp_support.py`/`cpp_support.py`.
"""

import logging
from pathlib import Path
from typing import Any, Dict, Optional, Set

from core.language_support import LanguageSupport

logger = logging.getLogger(__name__)


class JavaSupport(LanguageSupport):
    """Провайдер для Java."""

    def __init__(self, language: str = "java"):
        super().__init__(language)

    def create_analyzer(self):
        from analyzers.java_analyzer import JavaAnalyzer
        return JavaAnalyzer()

    def create_syntax_healer(self):
        from fixers.language_syntax.java_healer import JavaSyntaxHealer
        return JavaSyntaxHealer()

    def create_segmenter(self):
        from fixers.file_segmenter import FileSegmenter
        return FileSegmenter(language="java")

    def get_syntax_healer(self):
        return self.create_syntax_healer()

    def get_error_examples(self) -> Dict[str, str]:
        return {
            "JAVAC_SEMI": """\
--- a/Main.java
+++ b/Main.java
@@ -3,3 +3,3 @@
-        int x = 5
+        int x = 5;
""",
            "JAVAC_PACKAGE": """\
--- a/Main.java
+++ b/Main.java
@@ -1,4 +1,5 @@
+import java.util.List;
 public class Main {
""",
            "JAVAC_BRACE": """\
--- a/Main.java
+++ b/Main.java
@@ -5,3 +5,4 @@
     int x = 5;
+}
 }
""",
        }

    def get_classification_rules(self) -> Dict[str, Dict[str, Any]]:
        return {
            "JAVAC_SEMI":      {"class": "CRITICAL_SYNTAX", "allowed_actions": ["add_semicolon"]},
            "JAVAC_BRACE":     {"class": "CRITICAL_SYNTAX", "allowed_actions": ["add_brace"]},
            "JAVAC_PAREN":     {"class": "CRITICAL_SYNTAX", "allowed_actions": ["fix_syntax_heuristic"]},
            "JAVAC_EOF":       {"class": "CRITICAL_SYNTAX", "allowed_actions": ["fix_syntax_heuristic"]},
            "JAVAC_SYNTAX":    {"class": "CRITICAL_SYNTAX", "allowed_actions": ["fix_syntax_heuristic"]},
            "JAVAC_SYMBOL":    {"class": "BLOCKING",        "allowed_actions": ["add_import", "define_variable"]},
            "JAVAC_PACKAGE":   {"class": "BLOCKING",        "allowed_actions": ["add_import", "add_dependency"]},
            "JAVAC_TYPES":     {"class": "BLOCKING",        "allowed_actions": ["fix_type"]},
            "JAVAC_OPERAND":   {"class": "BLOCKING",        "allowed_actions": ["fix_type"]},
            "JAVAC_RETURN":    {"class": "BLOCKING",        "allowed_actions": ["add_return"]},
            "JAVAC_UNINIT":    {"class": "BLOCKING",        "allowed_actions": ["initialize_variable"]},
            "JAVAC_UNCHECKED": {"class": "WARNING",         "allowed_actions": ["add_generics_or_suppress"]},
            "JAVAC_DEPRECATED": {"class": "WARNING",        "allowed_actions": ["replace_api_or_suppress"]},
        }

    def get_class_weight(self, error_class: str) -> Optional[float]:
        weights = {
            "CRITICAL_SYNTAX": 200.0,
            "BLOCKING": 100.0,
            "STRUCTURAL": 70.0,
            "CLEANUP": 30.0,
            "WARNING": 10.0,
            "UNKNOWN": 50.0,
        }
        return weights.get(error_class)

    def get_initial_signatures_provider(self):
        def collect(project_path: Path, context) -> Dict[str, Set]:
            return {}
        return collect

    def supports_feature(self, feature: str) -> bool:
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
            "package ", "import ", "public ", "private ", "protected ",
            "static ", "final ", "abstract ", "class ", "interface ", "enum ",
            "extends ", "implements ", "void ", "int ", "long ", "double ",
            "boolean ", "String ", "var ", "if ", "else ", "for ", "while ",
            "do ", "switch ", "case ", "break ", "continue ", "return ",
            "new ", "throw ", "throws ", "try ", "catch ", "finally ",
        }

    def is_cascade_candidate(self, error: Dict[str, Any], file_errors: list) -> bool:
        code = error.get("code", "")
        is_critical = code in ("JAVAC_SEMI", "JAVAC_BRACE", "JAVAC_PAREN",
                               "JAVAC_EOF", "JAVAC_SYNTAX")
        if is_critical and len(file_errors) >= 3:
            return True
        return False
