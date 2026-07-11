"""
Языковой провайдер для C#.
"""

import logging
from pathlib import Path
from typing import Any, Dict, Optional, Set

from core.language_support import LanguageSupport

logger = logging.getLogger(__name__)


class CsharpSupport(LanguageSupport):
    """Провайдер для C#."""

    def __init__(self, language: str = "csharp"):
        super().__init__(language)

    def create_analyzer(self):
        from analyzers.csharp_analyzer import CsharpAnalyzer
        return CsharpAnalyzer()

    def create_syntax_healer(self):
        from fixers.language_syntax.csharp_healer import CSharpSyntaxHealer
        return CSharpSyntaxHealer()

    def create_segmenter(self):
        from fixers.file_segmenter import FileSegmenter
        return FileSegmenter(language="csharp")

    def get_syntax_healer(self):
        return self.create_syntax_healer()

    def get_error_examples(self) -> Dict[str, str]:
        return {
            "CS1002": """\
--- a/Program.cs
+++ b/Program.cs
@@ -3,7 +3,7 @@
-Console.WriteLine("Hello World")
+Console.WriteLine("Hello World");
""",
            "CS0246": """\
--- a/Program.cs
+++ b/Program.cs
@@ -1,4 +1,5 @@
+using System.Collections.Generic;
 var list = new List<string>();
""",
            "CS1513": """\
--- a/Program.cs
+++ b/Program.cs
@@ -10,5 +10,6 @@
 if (x > 0)
 {
     Console.WriteLine("Positive");
+}
""",
        }

    def get_classification_rules(self) -> Dict[str, Dict[str, Any]]:
        return {
            "CS1002": {"class": "CRITICAL_SYNTAX", "allowed_actions": ["add_semicolon"]},
            "CS1513": {"class": "CRITICAL_SYNTAX", "allowed_actions": ["add_brace"]},
            "CS1003": {"class": "CRITICAL_SYNTAX", "allowed_actions": ["fix_syntax_heuristic"]},
            "CS1026": {"class": "CRITICAL_SYNTAX", "allowed_actions": ["fix_syntax_heuristic"]},
            "CS0246": {"class": "BLOCKING", "allowed_actions": ["add_using", "add_reference"]},
            "CS0103": {"class": "BLOCKING", "allowed_actions": ["define_variable", "add_import"]},
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
            'using ', 'namespace ', 'class ', 'struct ', 'interface ', 'enum ',
            'public ', 'private ', 'protected ', 'internal ', 'static ', 'void ',
            'int ', 'string ', 'bool ', 'var ', 'if ', 'else ', 'for ', 'while ',
            'try ', 'catch ', 'finally ', 'throw ', 'return ', 'new ',
        }

    def is_cascade_candidate(self, error: Dict[str, Any], file_errors: list) -> bool:
        code = error.get('code', '')
        is_critical = code in ('CS1002', 'CS1513', 'CS1003', 'CS1026')
        if is_critical and len(file_errors) >= 3:
            return True
        return False