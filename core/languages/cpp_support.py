"""
Языковой провайдер для C++.
"""

import logging
from pathlib import Path
from typing import Any, Dict, Optional, Set

from core.language_support import LanguageSupport

logger = logging.getLogger(__name__)


class CppSupport(LanguageSupport):
    """Провайдер для C++."""

    def __init__(self, language: str = "cpp"):
        super().__init__(language)

    def create_analyzer(self):
        from analyzers.cpp_analyzer import CppAnalyzer
        return CppAnalyzer()

    def create_syntax_healer(self):
        from fixers.language_syntax.cpp_healer import CppSyntaxHealer
        return CppSyntaxHealer()

    def create_segmenter(self):
        from fixers.file_segmenter import FileSegmenter
        return FileSegmenter(language="cpp")

    def get_syntax_healer(self):
        return self.create_syntax_healer()

    def get_error_examples(self) -> Dict[str, str]:
        return {
            "C2143": """\
--- a/main.cpp
+++ b/main.cpp
@@ -3,7 +3,7 @@
 int main() {
-    std::cout << "Hello" << std::endl
+    std::cout << "Hello" << std::endl;
}
""",
            "C1083": """\
--- a/main.cpp
+++ b/main.cpp
@@ -1,3 +1,4 @@
+#include <string>
 int main() {
     std::string s = "Hello";
""",
        }

    def get_classification_rules(self) -> Dict[str, Dict[str, Any]]:
        return {
            # Синтетические коды g++ (CppAnalyzer._extract_synthetic_code)
            "GCC_SEMI":         {"class": "CRITICAL_SYNTAX", "allowed_actions": ["add_semicolon", "fix_syntax_heuristic"]},
            "GCC_BRACE":        {"class": "CRITICAL_SYNTAX", "allowed_actions": ["fix_syntax_heuristic"]},
            "GCC_PAREN":        {"class": "CRITICAL_SYNTAX", "allowed_actions": ["fix_syntax_heuristic"]},
            "GCC_SYNTAX":       {"class": "CRITICAL_SYNTAX", "allowed_actions": ["fix_syntax_heuristic"]},
            "GCC_INCLUDE":      {"class": "BLOCKING",        "allowed_actions": ["add_include"]},
            "GCC_UNDECLARED":   {"class": "BLOCKING",        "allowed_actions": ["define_variable", "add_include"]},
            "GCC_REDEFINITION": {"class": "BLOCKING",        "allowed_actions": ["remove_duplicate"]},
            # MSVC-коды (cl.exe) — оставлены ради обратной совместимости
            "C2143": {"class": "CRITICAL_SYNTAX", "allowed_actions": ["add_semicolon", "fix_syntax_heuristic"]},
            "C2059": {"class": "CRITICAL_SYNTAX", "allowed_actions": ["fix_syntax_heuristic"]},
            "C1083": {"class": "BLOCKING", "allowed_actions": ["add_include"]},
            "C2065": {"class": "BLOCKING", "allowed_actions": ["define_variable", "add_include"]},
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
            '#include', 'int ', 'void ', 'char ', 'bool ', 'float ', 'double ',
            'if ', 'else ', 'for ', 'while ', 'switch ', 'case ', 'break ', 'continue ',
            'return ', 'class ', 'struct ', 'namespace ', 'using ', 'public:', 'private:',
            'try ', 'catch ', 'throw ', 'new ', 'delete ',
        }

    def is_cascade_candidate(self, error: Dict[str, Any], file_errors: list) -> bool:
        code = error.get('code', '')
        # Синтетические g++-коды и старые MSVC-коды
        is_critical = code in ('GCC_SEMI', 'GCC_BRACE', 'GCC_PAREN', 'GCC_SYNTAX',
                               'C2143', 'C2059')
        if is_critical and len(file_errors) >= 3:
            return True
        return False