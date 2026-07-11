"""
Языковой провайдер для JavaScript и TypeScript.
Реализует все методы, необходимые конвейеру Webbles Fix для работы с JS/TS-проектами.
Добавлен метод get_syntax_healer для подключения модуля синтаксического ремонта.
"""

import logging
from pathlib import Path
from typing import Any, Dict, Optional, Set

from core.language_support import LanguageSupport

logger = logging.getLogger(__name__)


class JavaScriptSupport(LanguageSupport):
    """Провайдер для JavaScript/TypeScript (языки: javascript, typescript, js, ts)."""

    def __init__(self, language: str = "javascript"):
        super().__init__(language)

    def create_analyzer(self):
        from analyzers.js_analyzer import JSAnalyzer
        return JSAnalyzer()

    def create_syntax_healer(self):
        from fixers.syntax_repair import CriticalSyntaxHealer
        return CriticalSyntaxHealer()

    def create_segmenter(self):
        from fixers.file_segmenter import FileSegmenter
        return FileSegmenter(language="javascript")

    def get_syntax_healer(self):
        from fixers.language_syntax.javascript_healer import JavaScriptSyntaxHealer
        return JavaScriptSyntaxHealer()

    def get_error_examples(self) -> Dict[str, str]:
        return {
            "no-unused-vars": """\
--- a/module.js
+++ b/module.js
@@ -3,7 +3,7 @@
 function main() {
-    const unused = "this is never used";
+    // const unused = "this is never used"; // removed
     console.log("Hello");
}
""",
            "no-undef": """\
--- a/script.js
+++ b/script.js
@@ -1,4 +1,5 @@
 function greet() {
+    const name = "World";  // define the variable
     console.log("Hello " + name);
 }
""",
            "semi": """\
--- a/app.js
+++ b/app.js
@@ -5,7 +5,7 @@
 const message = "Hello"
-console.log(message)
+console.log(message);
""",
            "TS2304": """\
--- a/module.ts
+++ b/module.ts
@@ -1,4 +1,5 @@
+import { SomeType } from './types';
 function process(data: SomeType) {
     return data.value;
 }
""",
        }

    def get_prompt_enhancements(self, error: Dict[str, Any]) -> str:
        msg = error.get("message", "").lower()
        hints = []
        if "cannot read propert" in msg or "is undefined" in msg:
            hints.append(
                "This often means you tried to access a property on a null/undefined value. "
                "Consider adding optional chaining (?.) or a null check."
            )
        if "is not a function" in msg:
            hints.append(
                "Check if the variable was not assigned the expected function or prototype chain."
            )
        if "unexpected token" in msg:
            hints.append(
                "Syntax error: check for missing closing braces, brackets, or quotes."
            )
        return "\n".join(hints) if hints else ""

    def get_classification_rules(self) -> Dict[str, Dict[str, Any]]:
        return {
            "no-unused-vars": {"class": "CLEANUP", "allowed_actions": ["suppress", "remove"]},
            "@typescript-eslint/no-unused-vars": {"class": "CLEANUP", "allowed_actions": ["suppress", "remove"]},
            "no-undef": {"class": "BLOCKING", "allowed_actions": ["define_variable", "add_import"]},
            "TS2304": {"class": "BLOCKING", "allowed_actions": ["add_import", "define_type"]},
            "TS2552": {"class": "BLOCKING", "allowed_actions": ["fix_type"]},
            "semi": {"class": "CLEANUP", "allowed_actions": ["add_semicolon"]},
            "no-unreachable": {"class": "CLEANUP", "allowed_actions": ["remove"]},
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
            try:
                from analyzers.js_analyzer import JSAnalyzer
                analyzer = JSAnalyzer()
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
                logger.warning(f"Не удалось собрать исходные сигнатуры JS/TS: {e}")
                return {}
        return collect

    def supports_feature(self, feature: str) -> bool:
        if feature == "linter":
            return True
        if feature == "type_checker":
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
            'function ', 'const ', 'let ', 'var ', 'if ', 'else ', 'for ', 'while ',
            'do ', 'switch ', 'case ', 'break ', 'continue ', 'return ', 'import ',
            'export ', 'class ', 'new ', 'console.', 'document.', '=>', 'async ',
            'try {', 'catch ', 'finally ', 'throw '
        }

    def is_cascade_candidate(self, error: Dict[str, Any], file_errors: list) -> bool:
        msg = error.get('message', '').lower()
        code = error.get('code', '')
        is_critical = (
            'syntaxerror' in msg or
            'unexpected token' in msg or
            code in ('TS1005', 'TS1109', 'TS1128')
        )
        if is_critical and len(file_errors) >= 3:
            return True
        return False