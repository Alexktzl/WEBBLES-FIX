"""
Языковой провайдер для TypeScript.

Регистрируется отдельно от JavaScript, чтобы:
  * корректно классифицировать TS-коды (TS1005/TS2304/TS2322/…),
  * использовать выделенный `TypeScriptAnalyzer` (только `tsc --noEmit`),
  * проектные подсказки и memory-фильтр различали .ts от .js
    (см. Stage M.4 language-фильтр memory).

Контракт — как у `csharp_support.py` / `java_support.py`.
"""

import logging
from pathlib import Path
from typing import Any, Dict, Optional, Set

from core.language_support import LanguageSupport

logger = logging.getLogger(__name__)


class TypescriptSupport(LanguageSupport):
    """Провайдер для TypeScript (.ts/.tsx)."""

    def __init__(self, language: str = "typescript"):
        super().__init__(language)

    def create_analyzer(self):
        from analyzers.typescript_analyzer import TypeScriptAnalyzer
        return TypeScriptAnalyzer()

    def create_syntax_healer(self):
        # Базовые синтаксические правки совпадают с JS — переиспользуем.
        from fixers.language_syntax.javascript_healer import JavaScriptSyntaxHealer
        return JavaScriptSyntaxHealer()

    def create_segmenter(self):
        from fixers.file_segmenter import FileSegmenter
        # сегментер JS работает и на TS (та же блочная структура)
        return FileSegmenter(language="javascript")

    def get_syntax_healer(self):
        return self.create_syntax_healer()

    def get_error_examples(self) -> Dict[str, str]:
        return {
            "TS1005": """\
--- a/src/main.ts
+++ b/src/main.ts
@@ -3,3 +3,3 @@
-const x = 5
+const x = 5;
""",
            "TS2304": """\
--- a/src/main.ts
+++ b/src/main.ts
@@ -1,4 +1,5 @@
+import { foo } from './foo';
 const x = foo();
""",
            "TS2322": """\
--- a/src/main.ts
+++ b/src/main.ts
@@ -2,3 +2,3 @@
-const n: number = "hello";
+const n: string = "hello";
""",
        }

    def get_classification_rules(self) -> Dict[str, Dict[str, Any]]:
        return {
            # syntax
            "TS1005": {"class": "CRITICAL_SYNTAX", "allowed_actions": ["add_semicolon"]},
            "TS1109": {"class": "CRITICAL_SYNTAX", "allowed_actions": ["fix_syntax_heuristic"]},
            "TS1128": {"class": "CRITICAL_SYNTAX", "allowed_actions": ["fix_syntax_heuristic"]},
            "TS1131": {"class": "CRITICAL_SYNTAX", "allowed_actions": ["fix_syntax_heuristic"]},
            "TS1136": {"class": "CRITICAL_SYNTAX", "allowed_actions": ["fix_syntax_heuristic"]},
            # resolve / dependency
            "TS2304": {"class": "BLOCKING", "allowed_actions": ["add_import", "define_name"]},
            "TS2307": {"class": "BLOCKING", "allowed_actions": ["add_dependency", "fix_module_path"]},
            "TS2552": {"class": "BLOCKING", "allowed_actions": ["rename_to_existing"]},
            # types
            "TS2322": {"class": "BLOCKING", "allowed_actions": ["fix_type"]},
            "TS2339": {"class": "BLOCKING", "allowed_actions": ["fix_property"]},
            "TS2345": {"class": "BLOCKING", "allowed_actions": ["fix_arg_type"]},
            "TS2358": {"class": "BLOCKING", "allowed_actions": ["fix_type"]},
            "TS2367": {"class": "BLOCKING", "allowed_actions": ["fix_compare"]},
            "TS2532": {"class": "BLOCKING", "allowed_actions": ["add_null_guard"]},
            "TS2769": {"class": "BLOCKING", "allowed_actions": ["fix_overload"]},
            # warnings
            "TS6133": {"class": "WARNING", "allowed_actions": ["remove_unused"]},
            "TS7006": {"class": "WARNING", "allowed_actions": ["add_type_annotation"]},
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
            "import ", "export ", "from ", "default ", "type ", "interface ",
            "class ", "extends ", "implements ", "enum ", "namespace ",
            "const ", "let ", "var ", "function ", "return ", "if ", "else ",
            "for ", "while ", "do ", "switch ", "case ", "break ", "continue ",
            "try ", "catch ", "finally ", "throw ", "new ", "async ", "await ",
            "public ", "private ", "protected ", "readonly ", "static ",
        }

    def is_cascade_candidate(self, error: Dict[str, Any], file_errors: list) -> bool:
        code = error.get("code", "")
        is_critical = code in ("TS1005", "TS1109", "TS1128", "TS1131", "TS1136")
        if is_critical and len(file_errors) >= 3:
            return True
        return False
