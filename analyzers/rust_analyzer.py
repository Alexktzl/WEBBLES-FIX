""" 
Анализатор Rust для Webbles Fix.
Извлекает ошибки из кода Rust с помощью cargo check, build, clippy и audit.
Добавлено извлечение предложений компилятора (rustc suggestions) для семантического ремонта.
"""

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class RustAnalyzer:
    """
    Анализирует код Rust, запуская несколько команд cargo и собирая все ошибки.
    """

    def analyze(self, project_path: Path, clean_before_each: bool = False) -> List[Dict[str, Any]]:
        """
        Находит ошибки в проекте Rust.
        Если clean_before_each=True, выполняет cargo clean перед каждой командой.
        По умолчанию (False) очистка не производится — ответственность за чистоту на вызывающей стороне.
        """
        if not (project_path / "Cargo.toml").exists():
            logger.warning(f"Cargo.toml не найден в {project_path}")
            return []

        all_errors: List[Dict[str, Any]] = []

        # ---------------------------------------------------------------------
        # 1. cargo clean – только если явно запрошено
        # ---------------------------------------------------------------------
        if clean_before_each:
            self._cargo_clean(project_path)

        # ---------------------------------------------------------------------
        # 2. cargo check (JSON)
        # ---------------------------------------------------------------------
        check_errors = self._run_cargo_check(project_path)
        if check_errors:
            logger.info(f"cargo check нашёл {len(check_errors)} ошибок")
            all_errors.extend(check_errors)

        # ---------------------------------------------------------------------
        # 3. cargo build (текстовый вывод)
        # ---------------------------------------------------------------------
        build_errors = self._run_cargo_build(project_path)
        if build_errors:
            logger.info(f"cargo build нашёл {len(build_errors)} ошибок")
            all_errors.extend(build_errors)

        # ---------------------------------------------------------------------
        # 4. cargo clippy (линтер)
        # ---------------------------------------------------------------------
        clippy_errors = self._run_cargo_clippy(project_path)
        if clippy_errors:
            logger.info(f"cargo clippy нашёл {len(clippy_errors)} предупреждений")
            all_errors.extend(clippy_errors)

        # ---------------------------------------------------------------------
        # 5. cargo audit (безопасность)
        # ---------------------------------------------------------------------
        audit_errors = self._run_cargo_audit(project_path)
        if audit_errors:
            logger.info(f"cargo audit нашёл {len(audit_errors)} уязвимостей")
            all_errors.extend(audit_errors)

        # Дедупликация и фильтрация
        unique_errors = self._deduplicate_and_filter(all_errors, project_path)
        logger.info(f"Всего уникальных ошибок: {len(unique_errors)}")
        return unique_errors

    # -------------------------------------------------------------------------
    # Вспомогательный метод для очистки (больше не вызывается автоматически)
    # -------------------------------------------------------------------------
    def _cargo_clean(self, project_path: Path) -> None:
        """Выполняет cargo clean с таймаутом."""
        logger.info("Выполняется cargo clean...")
        try:
            subprocess.run(
                ["cargo", "clean"],
                cwd=project_path,
                capture_output=True,
                timeout=120,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            logger.warning("cargo clean превысил таймаут в 120с")
        except Exception as e:
            logger.warning(f"Ошибка при cargo clean: {e}")

    # -------------------------------------------------------------------------
    # Методы запуска cargo (без промежуточных cargo clean)
    # -------------------------------------------------------------------------
    def _run_cargo_check(self, project_path: Path) -> List[Dict[str, Any]]:
        try:
            result = subprocess.run(
                ["cargo", "check", "--message-format=json"],
                cwd=project_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
            )
            errors = []
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("reason") != "compiler-message":
                    continue
                msg = data.get("message", {})
                spans = msg.get("spans", [])
                if not spans:
                    children = msg.get("children", [])
                    for child in children:
                        if child.get("spans"):
                            spans.extend(child.get("spans", []))
                if not spans:
                    continue
                primary = spans[0]
                file_name = primary.get("file_name", "")
                if not isinstance(file_name, str) or not file_name:
                    continue
                try:
                    file_path = Path(file_name).relative_to(project_path)
                except ValueError:
                    file_path = Path(file_name)

                code = msg.get("code")
                if isinstance(code, dict):
                    code = code.get("code", "")
                elif not isinstance(code, str):
                    code = ""

                # Извлечение предложений компилятора (rustc suggestions)
                rustc_suggestions = []
                children = msg.get("children", [])
                for child in children:
                    if child.get("level") == "help":
                        help_spans = child.get("spans", [])
                        help_msg = child.get("message", "")
                        for span in help_spans:
                            suggestion = {
                                "line": span.get("line_start"),
                                "column": span.get("column_start"),
                                "end_line": span.get("line_end"),
                                "end_column": span.get("column_end"),
                                "message": help_msg,
                                "replacement": span.get("suggested_replacement"),
                                "file": span.get("file_name"),
                            }
                            rustc_suggestions.append(suggestion)

                error_entry = {
                    "file": str(file_path),
                    "line": primary.get("line_start", 0),
                    "column": primary.get("column_start", 0),
                    "message": msg.get("message", ""),
                    "code": code,
                    "severity": msg.get("level", "error"),
                    "error_type": self._classify_rust_error(code, msg.get("message", "")),
                }
                
                if rustc_suggestions:
                    error_entry["rustc_suggestions"] = rustc_suggestions
                    
                errors.append(error_entry)
            return errors
        except subprocess.TimeoutExpired:
            logger.warning("cargo check timed out")
            return []
        except Exception as e:
            logger.warning(f"cargo check failed: {e}")
            return []

    def _run_cargo_build(self, project_path: Path) -> List[Dict[str, Any]]:
        try:
            result = subprocess.run(
                ["cargo", "build"],
                cwd=project_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
            )
            output = result.stderr + result.stdout
            return self._parse_rustc_output(output, project_path)
        except subprocess.TimeoutExpired:
            logger.warning("cargo build timed out")
            return []
        except Exception as e:
            logger.warning(f"cargo build failed: {e}")
            return []

    def _run_cargo_clippy(self, project_path: Path) -> List[Dict[str, Any]]:
        try:
            result = subprocess.run(
                ["cargo", "clippy", "--message-format=json"],
                cwd=project_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
            )
            errors = []
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("reason") != "compiler-message":
                    continue
                msg = data.get("message", {})
                if msg.get("level") not in ("warning", "error"):
                    continue
                spans = msg.get("spans", [])
                if not spans:
                    continue
                primary = spans[0]
                file_name = primary.get("file_name", "")
                if not isinstance(file_name, str) or not file_name:
                    continue
                try:
                    file_path = Path(file_name).relative_to(project_path)
                except ValueError:
                    file_path = Path(file_name)

                code = msg.get("code")
                if isinstance(code, dict):
                    code = code.get("code", "")
                elif not isinstance(code, str):
                    code = ""

                errors.append({
                    "file": str(file_path),
                    "line": primary.get("line_start", 0),
                    "column": primary.get("column_start", 0),
                    "message": msg.get("message", ""),
                    "code": code,
                    "severity": "warning",
                    "error_type": "lint",
                })
            return errors
        except subprocess.TimeoutExpired:
            logger.warning("cargo clippy timed out")
            return []
        except Exception as e:
            logger.warning(f"cargo clippy failed: {e}")
            return []

    def _run_cargo_audit(self, project_path: Path) -> List[Dict[str, Any]]:
        try:
            result = subprocess.run(
                ["cargo", "audit", "--json"],
                cwd=project_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
            )
            if not result.stdout.strip():
                return []
            data = json.loads(result.stdout)
            errors = []

            cargo_toml_path = project_path / "Cargo.toml"
            cargo_lines = None
            if cargo_toml_path.exists():
                try:
                    with open(cargo_toml_path, "r", encoding="utf-8") as f:
                        cargo_lines = f.readlines()
                except Exception:
                    pass

            for vuln in data.get("vulnerabilities", {}).get("list", []):
                advisory = vuln.get("advisory", {})
                package = vuln.get("package", {})
                package_name = package.get("name", "")

                line_num = 0
                if cargo_lines and package_name:
                    for idx, line in enumerate(cargo_lines, start=1):
                        if re.search(r'\b' + re.escape(package_name) + r'\b', line):
                            line_num = idx
                            break

                fix_version = None
                # cargo audit --json: исправленные версии лежат в
                # vuln.versions.patched (список requirement-строк, напр. [">=0.8.4"]).
                # Поддерживаем и старый формат advisory.patched на всякий случай.
                patched_list = vuln.get("versions", {}).get("patched", [])
                if not patched_list:
                    legacy = advisory.get("patched", {})
                    if isinstance(legacy, list):
                        patched_list = legacy
                    elif isinstance(legacy, dict):
                        patched_list = [v for v in legacy.values() if v]
                for constraint in patched_list:
                    match = re.search(r'(\d+\.\d+\.\d+)', str(constraint))
                    if match:
                        fix_version = match.group(1)
                        break

                errors.append({
                    "file": "Cargo.toml",
                    "line": line_num,
                    "column": 0,
                    "message": f"{advisory.get('title', '')}: {package_name} {package.get('version', '')}",
                    "code": advisory.get("id", ""),
                    "severity": advisory.get("severity", "unknown"),
                    "error_type": "security",
                    "fix_version": fix_version,
                })
            return errors
        except subprocess.TimeoutExpired:
            logger.warning("cargo audit timed out")
            return []
        except Exception as e:
            logger.warning(f"cargo audit failed: {e}")
            return []

    # -------------------------------------------------------------------------
    # Парсинг текстового вывода rustc (для cargo build)
    # -------------------------------------------------------------------------
    def _parse_rustc_output(self, output: str, project_path: Path) -> List[Dict[str, Any]]:
        errors = []
        pattern = re.compile(r"\s*-->\s*([^:]+):(\d+):(\d+)")
        lines = output.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            match = pattern.match(line)
            if match:
                file_path = match.group(1)
                line_num = int(match.group(2))
                col_num = int(match.group(3))
                i += 1
                message_parts = []
                while i < len(lines) and lines[i].strip() and not pattern.match(lines[i]):
                    msg_line = lines[i].strip()
                    if msg_line:
                        message_parts.append(msg_line)
                    i += 1
                message = " ".join(message_parts)
                severity = "error" if "error" in message.lower() else "warning"
                code_match = re.search(r"\[(E\d+)\]", message)
                code = code_match.group(1) if code_match else None
                if not isinstance(code, str):
                    code = ""
                try:
                    rel_path = Path(file_path).relative_to(project_path)
                except ValueError:
                    rel_path = Path(file_path)
                errors.append({
                    "file": str(rel_path),
                    "line": line_num,
                    "column": col_num,
                    "message": message,
                    "code": code,
                    "severity": severity,
                    "error_type": self._classify_rust_error(code, message),
                })
                continue
            i += 1
        return errors

    # -------------------------------------------------------------------------
    # Дедупликация и фильтрация ошибок
    # -------------------------------------------------------------------------
    def _deduplicate_and_filter(self, errors: List[Dict], project_path: Path) -> List[Dict]:
        unique_errors = []
        seen = set()
        resolved_project = project_path.resolve()
        for err in errors:
            key = (err.get("file"), err.get("line"), err.get("message"))
            if key in seen:
                continue
            fname = err.get("file", "")
            if fname:
                fname_norm = fname.replace('\\', '/').lower()
                if any(needle in fname_norm for needle in [
                    'rustc/', 'library/core/', 'library/std/', 'library/alloc/',
                    'library/panic_', 'library/backtrace/', '/rustlib/',
                    'internal_macros.rs', 'macros.rs'
                ]):
                    continue
                try:
                    fpath = Path(fname)
                    if fpath.is_absolute() and not str(fpath.resolve()).startswith(str(resolved_project)):
                        continue
                except Exception:
                    pass
                if fname != "Cargo.toml":
                    if not (resolved_project / fname).exists():
                        try:
                            candidate = resolved_project / fname.replace('\\', '/')
                            if not candidate.exists():
                                continue
                        except Exception:
                            continue
            seen.add(key)
            unique_errors.append(err)
        return unique_errors

    # -------------------------------------------------------------------------
    # Классификация ошибок
    # -------------------------------------------------------------------------
    @staticmethod
    def _classify_rust_error(code: Optional[str], message: str) -> str:
        if code and isinstance(code, str):
            if code == "E0308":
                msg_lower = message.lower()
                if "argument" in msg_lower or "parameter" in msg_lower:
                    return "type_mismatch_argument"
                return "type_mismatch"
            if code == "E0277":
                return "trait_not_satisfied"
            if code == "E0425":
                return "unresolved_function"
            if code == "E0599":
                return "unresolved_method"
            if code == "E0382":
                return "ownership_error"
            if code == "E0596":
                return "mutability_error"
            if code == "E0594":
                return "mutability_error"
            if code.startswith("E04"):
                return "dependency"
            if code.startswith("E03"):
                return "type"
            if code.startswith("E05"):
                return "compile"
            if code.startswith("E00"):
                return "syntax"
            return "compile"
        msg_lower = message.lower()
        if "cannot find" in msg_lower or "unresolved" in msg_lower:
            return "dependency"
        if "mismatched types" in msg_lower or "expected" in msg_lower:
            return "type"
        if "borrow" in msg_lower and "mut" in msg_lower:
            return "mutability_error"
        if "move" in msg_lower and "value" in msg_lower:
            return "ownership_error"
        return "unknown"