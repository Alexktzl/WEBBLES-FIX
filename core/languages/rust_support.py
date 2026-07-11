"""
Языковой провайдер для Rust.
Реализует все методы, необходимые конвейеру Webbles Fix для работы с Rust-проектами.
Добавлены методы get_code_keywords и is_cascade_candidate для SyntaxDisasterRecovery.
Добавлен метод get_syntax_healer для подключения модуля синтаксического ремонта.
"""

import logging
from pathlib import Path
from typing import Any, Dict, Optional, Set

from core.language_support import LanguageSupport

logger = logging.getLogger(__name__)


class RustSupport(LanguageSupport):
    """Провайдер для Rust (язык: rust)."""

    def __init__(self, language: str = "rust"):
        super().__init__(language)

    # ------------------------------------------------------------------
    # Фабрики компонентов
    # ------------------------------------------------------------------

    def create_analyzer(self):
        from analyzers.rust_analyzer import RustAnalyzer
        return RustAnalyzer()

    def create_syntax_healer(self):
        from fixers.syntax_repair import CriticalSyntaxHealer
        return CriticalSyntaxHealer()

    def create_segmenter(self):
        from fixers.file_segmenter import FileSegmenter
        return FileSegmenter(language="rust")

    # ------------------------------------------------------------------
    # Синтаксический лекарь (новый метод)
    # ------------------------------------------------------------------

    def get_syntax_healer(self):
        from fixers.language_syntax.rust_healer import RustSyntaxHealer
        return RustSyntaxHealer()

    # ------------------------------------------------------------------
    # Данные для LLM
    # ------------------------------------------------------------------

    def get_error_examples(self) -> Dict[str, str]:
        return {
            "E0308": """\
--- a/src/main.rs
+++ b/src/main.rs
@@ -10,7 +10,7 @@
 fn main() {
-    let x: i32 = "hello";
+    let x: i32 = 42;
}
""",
            "E0382": """\
--- a/src/main.rs
+++ b/src/main.rs
@@ -5,7 +5,7 @@
     let s1 = String::from("hello");
     let s2 = s1;
-    println!("{}", s1);
+    println!("{}", s2);
}
""",
            "E0599": """\
--- a/src/main.rs
+++ b/src/main.rs
@@ -10,7 +10,7 @@
fn main() {
-    let num = 5;
-    num.to_string_unknown();
+    let num = 5;
+    num.to_string();
}
""",
            "E0425": """\
--- a/src/main.rs
+++ b/src/main.rs
@@ -10,7 +10,7 @@
fn main() {
-    undefined_function();
+    // undefined_function(); // removed, add appropriate call or stub
}
""",
            "E0596": """\
--- a/src/main.rs
+++ b/src/main.rs
@@ -8,7 +8,7 @@
fn main() {
-    let x = vec![1,2,3];
+    let mut x = vec![1,2,3];
     x.push(4);
}
""",
            "E0381": """\
--- a/src/main.rs
+++ b/src/main.rs
@@ -6,7 +6,7 @@
fn main() {
-    let x;
-    println!("{}", x);
+    let x = 42;
+    println!("{}", x);
}
""",
            "E0502": """\
--- a/src/main.rs
+++ b/src/main.rs
@@ -7,7 +7,7 @@
fn main() {
     let mut v = vec![1,2,3];
     let first = &v[0];
-    v.push(4);
-    println!("{}", first);
+    // v.push(4); // moved after the borrow is no longer used
+    println!("{}", first);
+    v.push(4); // now safe
}
""",
            "E0369": """\
--- a/src/main.rs
+++ b/src/main.rs
@@ -9,5 +9,5 @@
fn main() {
     let x: Option<i32> = Some(5);
-    let y = x + 10;
+    let y = x.unwrap_or(0) + 10;
}
""",
            "E0277": """\
--- a/src/main.rs
+++ b/src/main.rs
@@ -1,5 +1,6 @@
+#[derive(Debug)]
 struct Point {
     x: i32,
     y: i32,
 }
 fn main() {
     let p = Point { x: 1, y: 2 };
     println!("{:?}", p);
}
""",
        }

    def get_prompt_enhancements(self, error: Dict[str, Any]) -> str:
        code = error.get("code", "")
        msg = error.get("message", "").lower()
        hints = []
        if code == "E0765" or "unterminated" in msg:
            hints.append(
                "Note: E0765 often occurs because of an unclosed string somewhere in the file, "
                "or a macro with mismatched parentheses/arguments. Please check ALL string literals "
                "and macro invocations in the file, not only the reported line."
            )
        if "expected ';'" in msg or code == "E0001":
            hints.append("Add a semicolon at the end of the statement.")
        return "\n".join(hints) if hints else ""

    # ------------------------------------------------------------------
    # Классификация ошибок
    # ------------------------------------------------------------------

    def get_classification_rules(self) -> Dict[str, Dict[str, Any]]:
        return {
            "E0001": {"class": "CRITICAL_SYNTAX", "allowed_actions": ["fix_syntax_heuristic"]},
            "E0308": {"class": "BLOCKING", "allowed_actions": ["edit_line", "edit_signature", "add_clone"]},
            "E0382": {"class": "BLOCKING", "allowed_actions": ["edit_line", "add_clone", "restructure_ownership"]},
            "E0425": {"class": "BLOCKING", "allowed_actions": ["add_missing_function", "replace_call"]},
            "E0599": {"class": "BLOCKING", "allowed_actions": ["replace_method", "add_method_impl"]},
            "E0596": {"class": "STRUCTURAL", "allowed_actions": ["add_mut", "restructure_borrow"]},
            "E0594": {"class": "STRUCTURAL", "allowed_actions": ["add_mut"]},
            "E0499": {"class": "STRUCTURAL", "allowed_actions": ["restructure_borrow", "clone"]},
            "E0765": {"class": "CRITICAL_SYNTAX", "allowed_actions": ["fix_syntax_heuristic"]},
        }

    def get_class_weight(self, error_class: str) -> Optional[float]:
        return None

    # ------------------------------------------------------------------
    # Сигнатуры и аудит
    # ------------------------------------------------------------------

    def get_initial_signatures_provider(self):
        def collect(project_path: Path, context) -> Dict[str, Set]:
            try:
                from analyzers.rust_analyzer import RustAnalyzer
                analyzer = RustAnalyzer()
                errors = analyzer.analyze(project_path, clean_before_each=False)
                from core.contract import ErrorSignature
                sigs: Dict[str, Set] = {}
                for err in errors:
                    fname = err.get("file", "")
                    if fname:
                        sig_set = sigs.setdefault(fname, set())
                        sig_set.add(ErrorSignature.from_error(err))
                return sigs
            except Exception as e:
                logger.warning(f"Не удалось собрать исходные сигнатуры для Rust: {e}")
                return {}
        return collect

    # ------------------------------------------------------------------
    # Возможности языка
    # ------------------------------------------------------------------

    def supports_feature(self, feature: str) -> bool:
        if feature in ("tree_sitter", "cargo_clean", "audit_crate"):
            return True
        return False

    # ------------------------------------------------------------------
    # Конфигурация по умолчанию
    # ------------------------------------------------------------------

    @staticmethod
    def get_default_config() -> Dict[str, Any]:
        return {
            "pipeline": {
                "max_iterations": 10,
                "use_planning": True,
                "invariant_check": True,
            },
        }

    # ------------------------------------------------------------------
    # Дополнительные методы для SyntaxDisasterRecovery
    # ------------------------------------------------------------------

    def get_code_keywords(self) -> set:
        return {
            'fn ', 'let ', 'mut ', 'const ', 'static ', 'struct ', 'enum ', 'trait ',
            'impl ', 'mod ', 'use ', 'pub ', 'unsafe ', 'extern ', 'async ', 'if ',
            'else ', 'match ', 'loop ', 'while ', 'for ', 'return ', 'break ', 'continue ',
            'print', 'println', 'format', 'assert', 'debug', 'vec!', 'macro_rules!'
        }

    def is_cascade_candidate(self, error: Dict[str, Any], file_errors: list) -> bool:
        if error.get('code') == 'E0765':
            expected_found_count = sum(
                1 for e in file_errors if 'expected' in e.get('message', '').lower()
            )
            if expected_found_count >= 3:
                return True
        return False