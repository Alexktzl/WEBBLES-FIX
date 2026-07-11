"""Языковой провайдер для Kotlin. Регистрируется автоматически."""

import logging
from pathlib import Path
from typing import Any, Dict, Optional, Set

from core.language_support import LanguageSupport

logger = logging.getLogger(__name__)


class KotlinSupport(LanguageSupport):
    """Провайдер для Kotlin (.kt/.kts)."""

    def __init__(self, language: str = "kotlin"):
        super().__init__(language)

    def create_analyzer(self):
        from analyzers.kotlin_analyzer import KotlinAnalyzer
        return KotlinAnalyzer()

    def create_syntax_healer(self):
        from fixers.language_syntax.kotlin_healer import KotlinSyntaxHealer
        return KotlinSyntaxHealer()

    def create_segmenter(self):
        from fixers.file_segmenter import FileSegmenter
        return FileSegmenter(language="kotlin")

    def get_syntax_healer(self):
        return self.create_syntax_healer()

    def get_error_examples(self) -> Dict[str, str]:
        return {
            "KT_UNRESOLVED": """\
--- a/Main.kt
+++ b/Main.kt
@@ -1,3 +1,4 @@
+import kotlin.collections.List
 fun main() {
     val xs: List<Int> = listOf(1)
""",
            "KT_NULLSAFE": """\
--- a/Main.kt
+++ b/Main.kt
@@ -2,3 +2,3 @@
-    val n = s.length
+    val n = s?.length ?: 0
""",
        }

    def get_classification_rules(self) -> Dict[str, Dict[str, Any]]:
        return {
            "KT_SYNTAX":       {"class": "CRITICAL_SYNTAX", "allowed_actions": ["fix_syntax_heuristic"]},
            "KT_UNRESOLVED":   {"class": "BLOCKING",        "allowed_actions": ["add_import", "define_symbol"]},
            "KT_TYPES":        {"class": "BLOCKING",        "allowed_actions": ["fix_type"]},
            "KT_ARGS":         {"class": "BLOCKING",        "allowed_actions": ["fix_arity"]},
            "KT_NULLSAFE":     {"class": "BLOCKING",        "allowed_actions": ["add_null_guard", "use_safe_call"]},
            "KT_RETURN":       {"class": "BLOCKING",        "allowed_actions": ["add_return"]},
            "KT_UNUSED_PARAM": {"class": "WARNING",         "allowed_actions": ["rename_with_underscore"]},
            "KT_UNUSED_VAR":   {"class": "WARNING",         "allowed_actions": ["remove_or_use"]},
            "KT_UNUSED_EXPR":  {"class": "WARNING",         "allowed_actions": ["remove"]},
            "KT_DEPRECATED":   {"class": "WARNING",         "allowed_actions": ["replace_api"]},
        }

    def get_class_weight(self, error_class: str) -> Optional[float]:
        return {"CRITICAL_SYNTAX": 200.0, "BLOCKING": 100.0,
                "STRUCTURAL": 70.0, "CLEANUP": 30.0,
                "WARNING": 10.0, "UNKNOWN": 50.0}.get(error_class)

    def get_initial_signatures_provider(self):
        def collect(project_path: Path, context) -> Dict[str, Set]:
            return {}
        return collect

    def supports_feature(self, feature: str) -> bool:
        return False

    @staticmethod
    def get_default_config() -> Dict[str, Any]:
        return {"pipeline": {"max_iterations": 15, "use_planning": False,
                             "invariant_check": False}}

    def get_code_keywords(self) -> set:
        return {
            "package ", "import ", "fun ", "val ", "var ", "class ",
            "object ", "interface ", "enum class ", "data class ",
            "sealed class ", "abstract ", "open ", "override ", "private ",
            "public ", "protected ", "internal ", "if ", "else ", "when ",
            "for ", "while ", "do ", "break ", "continue ", "return ",
            "throw ", "try ", "catch ", "finally ", "in ", "is ", "as ",
            "suspend ", "inline ", "noinline ", "crossinline ", "lateinit ",
        }

    def is_cascade_candidate(self, error: Dict[str, Any], file_errors: list) -> bool:
        code = error.get("code", "")
        if code == "KT_SYNTAX" and len(file_errors) >= 3:
            return True
        return False
