"""
SyntaxRepairStage — pre-pipeline batch repair for E999/invalid-syntax.

Runs ONCE at the start of _global_fix_loop BEFORE the first _single_run().
At that point context.current_errors is empty (AnalyzeStage hasn't run yet),
so this stage scans working_path directly via ast.parse.

Repair chain per broken file:
  1. PythonSyntaxHealer  (deterministic patterns)
  2. tokenize            (unclosed brackets)
  3. parso + LLM         (localized region, fallback)

On success  → write file to disk; AnalyzeStage won't find E999 there anymore.
On failure  → do nothing; generate_patch_stage.py step 2.6 collapses the
              resulting E999/invalid-syntax cascade to 1 NR per file
              (via "_syntax_nr_files_done" metadata key).
"""

from __future__ import annotations

import ast
import io
import logging
import re
import tokenize
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_MAX_REGION_LINES = 40      # lines of context sent to LLM
_SCAN_EXCLUDE = frozenset({".git", "__pycache__", ".venv", "venv", "node_modules", ".tox"})


# ---------------------------------------------------------------------------
# ast / tokenize helpers
# ---------------------------------------------------------------------------

def _ast_error(content: str) -> Optional[Tuple[int, int, str]]:
    """Returns (line, col, msg) of first SyntaxError, or None if clean."""
    try:
        ast.parse(content)
        return None
    except SyntaxError as e:
        return (e.lineno or 1, e.offset or 0, e.msg or "")


def _try_healer(content: str) -> Optional[str]:
    """Run PythonSyntaxHealer on whole file. Returns fixed content or None."""
    try:
        from fixers.language_syntax.python_healer import PythonSyntaxHealer
        result = PythonSyntaxHealer().heal(content)
        if result and result != content and _ast_error(result) is None:
            return result
    except Exception as e:
        logger.debug("SyntaxRepair healer error: %s", e)
    return None


def _try_tokenize_repair(content: str) -> Optional[str]:
    """Detect unclosed brackets via tokenize and append missing closers."""
    stack: List[str] = []
    pairs = {"(": ")", "[": "]", "{": "}"}
    closing = set(pairs.values())
    try:
        for tok in tokenize.generate_tokens(io.StringIO(content).readline):
            if tok.type == tokenize.OP:
                if tok.string in pairs:
                    stack.append(pairs[tok.string])
                elif tok.string in closing and stack and stack[-1] == tok.string:
                    stack.pop()
    except tokenize.TokenError:
        pass
    except Exception as e:
        logger.debug("SyntaxRepair tokenize error: %s", e)
        return None

    if not stack:
        return None

    suffix = "\n" + "".join(reversed(stack)) + "\n"
    fixed = content.rstrip("\n") + suffix
    if _ast_error(fixed) is None:
        return fixed
    return None


def _parso_region(content: str, err_line: int) -> Tuple[int, int]:
    """Use parso to refine error line, return (start, end) 0-based line indices."""
    try:
        import parso  # type: ignore
        module = parso.parse(content)
        if module.errors:
            err_line = module.errors[0].start_pos[0]
    except ImportError:
        pass
    except Exception:
        pass

    lines = content.splitlines()
    half = _MAX_REGION_LINES // 2
    start = max(0, err_line - half)
    end = min(len(lines), err_line + half)
    return start, end


def _extract_code_block(text: str) -> str:
    m = re.search(r"```(?:python)?\n(.*?)```", text, re.DOTALL)
    return m.group(1) if m else text.strip()


def _try_llm_repair(
    content: str,
    err_line: int,
    err_msg: str,
    llm_client,
    file_rel: str,
) -> Optional[str]:
    """Extract localized region, call LLM once, reconstruct & validate."""
    if llm_client is None:
        return None
    if not (hasattr(llm_client, "complete_text") or hasattr(llm_client, "chat")):
        return None
    try:
        all_lines = content.splitlines(keepends=True)
        start, end = _parso_region(content, err_line)
        region = "".join(all_lines[start:end])
        local_line = err_line - start

        system = (
            "You are a Python syntax repair tool. "
            "Return ONLY the fixed code region — no explanations, no markdown, "
            "no extra text. Preserve indentation exactly."
        )
        user = (
            f"Fix the Python syntax error in the region below.\n"
            f"Error: {err_msg} (at region line {local_line}).\n\n"
            f"{region}"
        )

        if hasattr(llm_client, "complete_text"):
            response = llm_client.complete_text(system, user)
        else:
            msgs = [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
            response = llm_client.chat(msgs)

        if not response:
            return None

        fixed_region = _extract_code_block(response)
        fixed_lines = fixed_region.splitlines(keepends=True)
        if fixed_lines and not fixed_lines[-1].endswith("\n"):
            fixed_lines[-1] += "\n"

        reconstructed = "".join(all_lines[:start] + fixed_lines + all_lines[end:])
        if _ast_error(reconstructed) is None:
            logger.info("SyntaxRepair: LLM fixed %s (region %d-%d)", file_rel, start, end)
            return reconstructed
    except Exception as e:
        logger.debug("SyntaxRepair LLM error for %s: %s", file_rel, e)
    return None


# ---------------------------------------------------------------------------
# main class
# ---------------------------------------------------------------------------

class SyntaxRepairStage:
    """Pre-pipeline E999 repair — call once before the main fix loop.

    Scans working_path directly (context.current_errors is empty at this point).
    Fixed files will have no E999 when AnalyzeStage scans next.
    Unfixed files are left for the normal pipeline; generate_patch_stage step
    2.6 collapses their E999 cascade to 1 NR per file.
    """

    def execute(
        self,
        context,
        working_path: Path,
        llm_client=None,
    ):
        """
        Returns (context, accepted_patch_list, n_failed).

        accepted_patch_list items: {"file", "message", "code"}
        n_failed: number of files that could not be repaired
        """
        broken_files = self._find_broken_files(working_path)
        if not broken_files:
            return context, [], 0

        logger.info("SyntaxRepairStage: found %d Python files with syntax errors", len(broken_files))

        accepted_list: List[Dict[str, Any]] = []
        n_failed = 0

        for file_path, file_rel, err_line, err_msg in broken_files:
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
            except Exception as exc:
                logger.warning("SyntaxRepair: cannot read %s: %s", file_rel, exc)
                n_failed += 1
                continue

            # Python version-aware analysis (2026-06-22): эта pre-pipeline
            # стадия сканирует файлы НАПРЯМУЮ, до AnalyzeStage — где такая же
            # проверка уже исключает version-incompatible "ошибки" из
            # current_errors. Без проверки здесь тоже SyntaxRepairStage
            # потратил бы попытки healer/tokenize/LLM на PEP 695 и подобный
            # код, который не баг (см. core/python_version_detector.py,
            # CONVERSION_ANALYSIS_2026-06-22_FULL_ARCHIVE.md).
            if (getattr(context, "config", {}) or {}).get("pipeline", {}).get(
                "python_version_detection", True
            ):
                try:
                    from core.python_version_detector import (
                        classify_version_incompatibility,
                        detect_project_python_version,
                    )
                    _project_required = detect_project_python_version(working_path)
                    _ver_result = classify_version_incompatibility(
                        working_path, content, project_required_version=_project_required,
                    )
                except Exception as e:
                    logger.debug("python_version_detector (SyntaxRepairStage) failed: %s", e)
                    _ver_result = None
                if _ver_result:
                    logger.info(
                        "  SyntaxRepairStage: %s требует Python %s, окружение %s "
                        "(%s) — не E999, пропускаем без попытки репарации",
                        file_rel, _ver_result["required_version"],
                        _ver_result["current_version"], _ver_result["detected_via"],
                    )
                    _vvm = dict(context.metadata)
                    _vvm["unsupported_python_version_items"] = list(
                        _vvm.get("unsupported_python_version_items", []) or []
                    ) + [{
                        "file": file_rel, "line": err_line, "code": "E999",
                        "message": err_msg, **_ver_result,
                    }]
                    _vvm["unsupported_python_version_count"] = int(
                        _vvm.get("unsupported_python_version_count", 0)
                    ) + 1
                    context = context.update(metadata=_vvm)
                    continue

            # Attempt 1 — PythonSyntaxHealer
            fixed = _try_healer(content)
            method = "healer"

            # Attempt 2 — tokenize unclosed brackets
            if fixed is None:
                fixed = _try_tokenize_repair(content)
                method = "tokenize_bracket"

            # Attempt 3 — LLM on localized region
            if fixed is None:
                fixed = _try_llm_repair(content, err_line, err_msg, llm_client, file_rel)
                method = "llm_region"

            # H1 (аудит 2026-07-01): эта стадия пишет на диск и регистрирует
            # ACCEPT в обход ApplyPatch/Validate/Review/Decide — раньше
            # единственной проверкой был ast.parse результата. LLM-«ремонт»
            # региона (и в принципе любой ремонт) мог стереть функции/классы/
            # импорты, и это уходило в accepted без единого guard-а: before
            # с E999 не парсится, поэтому и per-patch, и финальная символьная
            # проверки были слепы (см. C2 в PROJECT_AUDIT_REPORT; фолбэк в
            # symbol_regression теперь сравнивает regex-vs-regex).
            if fixed is not None:
                try:
                    from analysis.symbol_regression import check_symbol_regression
                    _reg = check_symbol_regression(content, fixed, "python")
                except Exception as _sr_e:
                    logger.warning(
                        "SyntaxRepair: символьная проверка %s упала (%s) — "
                        "fail-closed, ремонт не применяем", file_rel, _sr_e,
                    )
                    _reg = {"ok": False, "reason": f"check_failed: {_sr_e}"}
                if not _reg.get("ok", False):
                    logger.warning(
                        "SyntaxRepair: ремонт %s (%s) ОТКЛОНЁН — потеря символов "
                        "(missing_defs=%s, missing_classes=%s, missing_imports=%s) — "
                        "оставляем файл пайплайну/NR",
                        file_rel, method, _reg.get("missing_defs"),
                        _reg.get("missing_classes"), _reg.get("missing_imports"),
                    )
                    n_failed += 1
                    continue

            if fixed is not None:
                try:
                    file_path.write_text(fixed, encoding="utf-8")
                    context = context.add_accepted_patch({
                        "error": {
                            "file": file_rel,
                            "line": err_line,
                            "code": "E999",
                            "message": (
                                f"syntax_repair({method}): fixed syntax error — {err_msg}"
                            ),
                            "error_class": "CRITICAL_SYNTAX",
                        },
                        "reason": f"syntax_repair_stage:{method}",
                    })
                    accepted_list.append({
                        "file": file_rel,
                        "message": f"syntax_repair({method}): {err_msg}",
                        "code": "E999",
                    })
                    logger.info("SyntaxRepair FIXED: %s via %s", file_rel, method)
                except Exception as exc:
                    logger.warning("SyntaxRepair: write failed for %s: %s", file_rel, exc)
                    n_failed += 1
            else:
                logger.info("SyntaxRepair could not fix %s — leaving for pipeline NR collapse", file_rel)
                n_failed += 1

        if accepted_list or n_failed:
            logger.info(
                "SyntaxRepairStage: %d fixed, %d unfixed (pipeline step 2.6 will collapse their NR)",
                len(accepted_list), n_failed,
            )

        return context, accepted_list, n_failed

    @staticmethod
    def _find_broken_files(working_path: Path) -> List[Tuple[Path, str, int, str]]:
        """Scan working_path for Python files with syntax errors.

        Returns list of (file_path, file_rel_str, err_line, err_msg).
        Only the first syntax error per file is returned (ast.parse stops at first).
        """
        result: List[Tuple[Path, str, int, str]] = []
        try:
            for py_file in sorted(working_path.rglob("*.py")):
                if any(part in _SCAN_EXCLUDE for part in py_file.parts):
                    continue
                try:
                    content = py_file.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                err = _ast_error(content)
                if err is not None:
                    err_line, _col, err_msg = err
                    try:
                        rel = py_file.relative_to(working_path)
                        rel_str = str(rel)
                    except ValueError:
                        rel_str = py_file.name
                    result.append((py_file, rel_str, err_line, err_msg))
        except Exception as e:
            logger.warning("SyntaxRepair scan error: %s", e)
        return result
