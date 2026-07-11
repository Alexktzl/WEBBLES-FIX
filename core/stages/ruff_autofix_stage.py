"""
IMP-2: RuffAutoFixStage — pre-pipeline batch fix of safe, deterministic codes.

Runs `ruff check --fix` on a curated set of codes where:
  - ruff has a built-in auto-fix implementation
  - the fix is deterministic and does not change semantics
  - net-delta is validated after each batch (rollback if errors grow)

Injected in _global_fix_loop BEFORE SyntaxRepairStage and the main loop.
Returns (context, accepted_list) — accepted_list entries mirror the format
used by SyntaxRepairStage so stats stay consistent.

Safe codes (deterministic, structural risk low):
  WHITESPACE   — W291 W292 W293 W391
  COMPARISONS  — E711 E712
  IMPORTS      — I001 (isort sort order)
  REDUNDANT    — UP006 UP007 UP032 UP034 UP035 (pyupgrade safe subset)
  BLANK_LINES  — E301 E302 E303 E304 E305 E306 (blank line between defs)
  COMMENTS     — E261 E262 E265 E266 (comment spacing/style)
  F-STRINGS    — F541 (f-string без placeholder'ов — снимает лишний `f`;
                 2026-06-25, learning_cases.jsonl: 19 попыток через LLM,
                 100% успеха, чисто механический фикс — ruff сам корректно
                 пропускает f-строки С placeholder'ами, проверено вручную)

Separate pass with --unsafe-fixes (risky=False but needs extra flag):
  UNUSED_VARS  — F841 (remove assignment to unused var)

Net-delta guard: .py files are saved before each pass and restored on
rollback (net_delta > 0 means new errors were introduced).

Deliberately excluded:
  E501  — line wrapping changes context, too noisy
  F401  — removing imports can break re-exports / __all__ / type stubs
  W503  — removed from ruff (flake8-compat alias, no autofix)
  E1xx  — indentation changes are the main source of net-delta regressions
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from core.pipeline_context import PipelineContext

logger = logging.getLogger(__name__)

# Codes for the safe pass (ruff --fix, no --unsafe-fixes)
_SAFE_CODES = (
    "W291,W292,W293,W391,"
    "E711,E712,"
    "I001,"
    "UP006,UP007,UP032,UP034,UP035,"
    "E301,E302,E303,E304,E305,E306,"
    "E261,E262,E265,E266,"
    "COM812,"
    "B007,"
    "RET504,RET505,"
    "F541"
)

# Codes for the unsafe pass (ruff --fix --unsafe-fixes)
_UNSAFE_CODES = "F841"

_SCAN_EXCLUDE = {".git", "__pycache__", ".venv", "venv", "node_modules", ".tox", ".mypy_cache"}


def _ruff_error_count(path: Path) -> Optional[int]:
    """Count total ruff violations in path (project-wide).

    H5 (аудит 2026-07-01): возвращает None при ЛЮБОМ сбое (timeout,
    не-JSON вывод, ruff отсутствует) — раньше возвращался 0, и сбой
    ПОСЛЕ-пересчёта давал net_delta = 0 - before < 0, т.е. массовая
    непроверенная правка засчитывалась как успех. None → вызывающий код
    обязан откатить пасс (fail-closed), а не принять его.
    """
    try:
        r = subprocess.run(
            ["ruff", "check", "--output-format=json", str(path)],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
        )
        data = json.loads(r.stdout or "[]")
        return len(data) if isinstance(data, list) else None
    except Exception:
        return None


def _broken_py_files(saved: dict, base: Path) -> List[str]:
    """H5: файлы из снапшота, ИЗМЕНЁННЫЕ ruff-ом и переставшие парситься.
    ruff не должен ломать синтаксис — но пишем на диск мы, проверять нам."""
    import ast as _ast
    broken: List[str] = []
    for rel, old in saved.items():
        p = base / rel
        try:
            new = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            broken.append(rel)
            continue
        if new == old:
            continue
        try:
            _ast.parse(new)
        except SyntaxError:
            broken.append(rel)
    return broken


def _ruff_fix(path: Path, select: str, unsafe: bool = False, timeout: int = 120) -> Tuple[int, int]:
    """Run ruff --fix on path for given select codes.

    Returns (files_changed, violations_fixed) — approximate counts from ruff output.
    """
    cmd = ["ruff", "check", "--fix", f"--select={select}", str(path)]
    if unsafe:
        cmd.insert(3, "--unsafe-fixes")
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        # ruff prints "Fixed N errors." to stderr
        fixed = 0
        for line in (r.stderr or "").splitlines():
            if "Fixed" in line:
                parts = line.split()
                for i, p in enumerate(parts):
                    if p == "Fixed" and i + 1 < len(parts):
                        try:
                            fixed = int(parts[i + 1])
                        except ValueError:
                            pass
        return 0, fixed
    except subprocess.TimeoutExpired:
        logger.warning("RuffAutoFixStage: ruff --fix timeout (%ds)", timeout)
        return 0, 0
    except FileNotFoundError:
        logger.warning("RuffAutoFixStage: ruff not found in PATH")
        return 0, 0
    except Exception as e:
        logger.warning("RuffAutoFixStage: ruff --fix error: %s", e)
        return 0, 0


def _save_py_files(path: Path) -> dict:
    """Snapshot all .py files under path. Returns {rel_str: content}."""
    saved: dict = {}
    for py_file in path.rglob("*.py"):
        if _SCAN_EXCLUDE & set(py_file.parts):
            continue
        try:
            rel = str(py_file.relative_to(path))
            saved[rel] = py_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass
    return saved


def _restore_py_files(saved: dict, base: Path) -> None:
    """Restore .py files from a snapshot created by _save_py_files."""
    for rel, content in saved.items():
        try:
            (base / rel).write_text(content, encoding="utf-8")
        except Exception as e:
            logger.warning("RuffAutoFixStage restore %s: %s", rel, e)


class RuffAutoFixStage:
    """Pre-pipeline stage: batch ruff --fix for safe deterministic codes."""

    def execute(
        self, context: PipelineContext, working_path: Path
    ) -> Tuple[PipelineContext, List[dict]]:
        lang = (getattr(context, "language", None) or "").lower()
        if lang not in ("python", "py"):
            return context, []

        # Count errors before any fixes
        before_total = _ruff_error_count(working_path)
        if before_total is None:
            # H5: baseline не установлен (ruff недоступен/timeout) — без
            # точки сравнения массовая правка непроверяема, пропускаем стадию.
            logger.warning("RuffAutoFixStage: baseline-пересчёт не удался — стадия пропущена")
            return context, []
        if before_total == 0:
            return context, []

        logger.info("RuffAutoFixStage: %d violations found, running safe fixes...", before_total)

        accepted: List[dict] = []

        def _run_pass(select: str, unsafe: bool, source: str, baseline: int):
            """Один пасс ruff --fix с fail-closed валидацией (H5, аудит
            2026-07-01): сбой после-пересчёта или сломанный синтаксис
            изменённого файла → полный откат снапшота, НЕ успех."""
            saved = _save_py_files(working_path)
            _, fixed_count = _ruff_fix(working_path, select, unsafe=unsafe)
            if fixed_count <= 0:
                return baseline, None
            broken = _broken_py_files(saved, working_path)
            if broken:
                logger.warning(
                    "RuffAutoFixStage %s: ruff сломал синтаксис %s — откат (%d файлов)",
                    source, broken, len(saved),
                )
                _restore_py_files(saved, working_path)
                return baseline, None
            after = _ruff_error_count(working_path)
            if after is None:
                logger.warning(
                    "RuffAutoFixStage %s: после-пересчёт не удался — fail-closed откат "
                    "(%d файлов); раньше это засчитывалось как успех (after=0)",
                    source, len(saved),
                )
                _restore_py_files(saved, working_path)
                return baseline, None
            net = after - baseline
            if net > 0:
                logger.warning(
                    "RuffAutoFixStage %s net_delta=+%d — откат (%d файлов)",
                    source, net, len(saved),
                )
                _restore_py_files(saved, working_path)
                return baseline, None
            logger.info(
                "RuffAutoFixStage %s: fixed=%d before=%d after=%d net=%d",
                source, fixed_count, baseline, after, net,
            )
            return after, {
                "source": source,
                "codes": select,
                "fixed_count": fixed_count,
                "before_total": baseline,
                "after_total": after,
                "net_delta": net,
            }

        # --- Pass 1: safe codes ---
        before_total, entry = _run_pass(_SAFE_CODES, False, "ruff_autofix_safe", before_total)
        if entry:
            accepted.append(entry)

        # --- Pass 2: unsafe codes ---
        _, entry = _run_pass(_UNSAFE_CODES, True, "ruff_autofix_unsafe", before_total)
        if entry:
            accepted.append(entry)

        if accepted:
            total_fixed = sum(a["fixed_count"] for a in accepted)
            logger.info(
                "RuffAutoFixStage: total fixed=%d in %d pass(es)",
                total_fixed, len(accepted),
            )
            # Record in context metadata for reporting
            _m = dict(context.metadata)
            _m["ruff_autofix_stats"] = accepted
            _m["ruff_autofix_total_fixed"] = total_fixed
            context = context.update(metadata=_m)
        else:
            logger.debug("RuffAutoFixStage: no fixes applied")

        return context, accepted
