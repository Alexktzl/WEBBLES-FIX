"""Языковой провайдер для Go. Регистрируется автоматически."""

import logging
from pathlib import Path
from typing import Any, Dict, Optional, Set

from core.language_support import LanguageSupport

logger = logging.getLogger(__name__)


class GoSupport(LanguageSupport):
    """Провайдер для Go (.go)."""

    def __init__(self, language: str = "go"):
        super().__init__(language)

    def create_analyzer(self):
        from analyzers.go_analyzer import GoAnalyzer
        return GoAnalyzer()

    def create_syntax_healer(self):
        from fixers.language_syntax.go_healer import GoSyntaxHealer
        return GoSyntaxHealer()

    def create_segmenter(self):
        from fixers.file_segmenter import FileSegmenter
        return FileSegmenter(language="go")

    def get_syntax_healer(self):
        return self.create_syntax_healer()

    def get_error_examples(self) -> Dict[str, str]:
        return {
            "GO_UNDEFINED": """\
--- a/main.go
+++ b/main.go
@@ -3,3 +3,4 @@
+import "fmt"
 func main() {
     fmt.Println("hi")
""",
            "GO_UNUSED_IMP": """\
--- a/main.go
+++ b/main.go
@@ -1,4 +1,3 @@
-import "fmt"
 func main() {
""",
        }

    def get_classification_rules(self) -> Dict[str, Dict[str, Any]]:
        return {
            "GO_SYNTAX":     {"class": "CRITICAL_SYNTAX", "allowed_actions": ["fix_syntax_heuristic"]},
            "GO_RETURN":     {"class": "BLOCKING",        "allowed_actions": ["add_return"]},
            "GO_UNDEFINED":  {"class": "BLOCKING",        "allowed_actions": ["add_import", "define_symbol"]},
            "GO_IMPORT":     {"class": "BLOCKING",        "allowed_actions": ["add_dependency", "fix_module"]},
            "GO_TYPES":      {"class": "BLOCKING",        "allowed_actions": ["fix_type", "add_cast"]},
            "GO_ARGS":       {"class": "BLOCKING",        "allowed_actions": ["fix_arity"]},
            "GO_OPERAND":    {"class": "BLOCKING",        "allowed_actions": ["fix_type"]},
            "GO_UNUSED_IMP": {"class": "CLEANUP",         "allowed_actions": ["remove_import"]},
            "GO_UNUSED_VAR": {"class": "CLEANUP",         "allowed_actions": ["remove_or_use"]},
            "GO_SHADOW":     {"class": "WARNING",         "allowed_actions": ["rename"]},
            "GO_UNSAFE":     {"class": "WARNING",         "allowed_actions": ["review"]},
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
            "package ", "import ", "func ", "type ", "struct ", "interface ",
            "var ", "const ", "if ", "else ", "for ", "switch ", "case ",
            "default ", "break ", "continue ", "return ", "go ", "defer ",
            "select ", "chan ", "map ", "range ", "fallthrough ",
        }

    def is_cascade_candidate(self, error: Dict[str, Any], file_errors: list) -> bool:
        code = error.get("code", "")
        if code in ("GO_SYNTAX", "GO_RETURN") and len(file_errors) >= 3:
            return True
        return False
