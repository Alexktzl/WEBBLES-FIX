"""
PythonDependencyInference — детерминированный сканер импортов + requirements.txt.

Все тесты — без сети/pip. recover() гоняется с `run_pip_check=False`.
Запуск: python3 tests/test_phase_python_dep_inference.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from analysis.python_dependency_inference import (
    PythonDependencyInference, collect_imports, infer_missing,
    apply_to_requirements, KNOWN_PACKAGES,
)

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def _mkproj(d, files):
    root = Path(d)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


# ----------------------------------------------------------------------
# 1. collect_imports — корректно вытаскивает top-level имена
# ----------------------------------------------------------------------
def test_collect_imports_basic():
    with tempfile.TemporaryDirectory() as d:
        proj = _mkproj(d, {
            "a.py": (
                "import os\n"
                "import sys\n"
                "from collections import defaultdict\n"
                "import requests\n"
                "from yaml import safe_load\n"
                "import numpy as np\n"
                "from urllib.parse import urlparse\n"
            ),
            "sub/b.py": "import pandas as pd\nfrom bs4 import BeautifulSoup\n",
            "__pycache__/x.py": "import does_not_exist\n",  # skip
            ".webbles_backups/old.py": "import dontknowme\n",  # skip
        })
        imports = collect_imports(proj)
        for need in ("os", "sys", "collections", "requests", "yaml", "numpy",
                     "urllib", "pandas", "bs4"):
            check(f"collect_has_{need}", need in imports)
        check("collect_skips_pycache", "does_not_exist" not in imports)
        check("collect_skips_backups", "dontknowme" not in imports)


# ----------------------------------------------------------------------
# 2. infer_missing — stdlib и локальные не попадают
# ----------------------------------------------------------------------
def test_infer_skips_stdlib_and_local():
    with tempfile.TemporaryDirectory() as d:
        proj = _mkproj(d, {
            "main.py": (
                "import os\n"           # stdlib → skip
                "import requests\n"     # KNOWN → add
                "import sub_a\n"        # локальный → skip
                "import yaml\n"         # KNOWN → add  (dist: pyyaml)
                "import does_not_exist\n"  # unknown → skip
            ),
            "sub_a/__init__.py": "",
        })
        missing = infer_missing(proj)
        check("inf_has_requests", "requests" in missing)
        check("inf_has_pyyaml", "pyyaml" in missing)
        check("inf_no_stdlib_os", "os" not in missing)
        check("inf_no_local_subA", "sub_a" not in missing)
        check("inf_no_unknown", "does_not_exist" not in missing)


def test_infer_skips_already_in_requirements():
    with tempfile.TemporaryDirectory() as d:
        proj = _mkproj(d, {
            "main.py": "import requests\nimport numpy\n",
            "requirements.txt": "requests==2.30.0\nflask>=3.0\n",
        })
        missing = infer_missing(proj)
        check("already_listed_requests_skipped", "requests" not in missing)
        check("new_numpy_added", "numpy" in missing)


# ----------------------------------------------------------------------
# 3. apply_to_requirements — корректно дописывает, делает backup
# ----------------------------------------------------------------------
def test_apply_appends_to_existing():
    with tempfile.TemporaryDirectory() as d:
        proj = _mkproj(d, {
            "main.py": "import requests\n",
            "requirements.txt": "flask>=3.0\n",
        })
        missing = infer_missing(proj)
        changed, backup, path = apply_to_requirements(proj, missing)
        check("apply_changed", changed is True)
        check("apply_backup_text", backup == "flask>=3.0\n")
        out = (proj / "requirements.txt").read_text(encoding="utf-8")
        check("apply_keeps_flask", "flask>=3.0" in out)
        check("apply_adds_requests", "requests" in out)
        check("apply_marker", "auto-added" in out)


def test_apply_creates_new_requirements():
    with tempfile.TemporaryDirectory() as d:
        proj = _mkproj(d, {"main.py": "import requests\n"})
        missing = infer_missing(proj)
        changed, backup, _ = apply_to_requirements(proj, missing)
        check("create_changed", changed is True)
        check("create_backup_none", backup is None)
        out = (proj / "requirements.txt").read_text(encoding="utf-8")
        check("create_has_requests", "requests" in out)


def test_apply_empty_missing_no_op():
    with tempfile.TemporaryDirectory() as d:
        proj = _mkproj(d, {
            "main.py": "import os\n",  # stdlib
            "requirements.txt": "flask\n",
        })
        missing = infer_missing(proj)
        changed, _, _ = apply_to_requirements(proj, missing)
        check("empty_missing_no_change", changed is False)
        # файл не тронут
        check("empty_missing_file_intact",
              (proj / "requirements.txt").read_text(encoding="utf-8") == "flask\n")


# ----------------------------------------------------------------------
# 4. recover — оркестрация (без pip-check)
# ----------------------------------------------------------------------
def test_recover_pipeline():
    with tempfile.TemporaryDirectory() as d:
        proj = _mkproj(d, {
            "main.py": "import requests\nfrom bs4 import X\n",
        })
        res = PythonDependencyInference().recover(proj, run_pip_check=False)
        check("recover_has_changes", res.has_changes is True)
        check("recover_lists_missing", "requests" in res.missing)
        check("recover_lists_bs", "beautifulsoup4" in res.missing)
        check("recover_explains", any("python" in e for e in res.explanations))
        out = (proj / "requirements.txt").read_text(encoding="utf-8")
        check("recover_file_written", "requests" in out and "beautifulsoup4" in out)


def test_recover_no_op_when_clean():
    with tempfile.TemporaryDirectory() as d:
        proj = _mkproj(d, {"main.py": "import os\nimport sys\n"})
        res = PythonDependencyInference().recover(proj, run_pip_check=False)
        check("clean_no_changes", res.has_changes is False)
        check("clean_missing_empty", res.missing == {})


# ----------------------------------------------------------------------
# 5. KNOWN_PACKAGES — критические маппинги корректны
# ----------------------------------------------------------------------
def test_critical_mappings():
    cases = [("yaml", "pyyaml"), ("bs4", "beautifulsoup4"),
             ("PIL", "pillow"), ("sklearn", "scikit-learn"),
             ("dotenv", "python-dotenv"), ("cv2", "opencv-python"),
             ("psycopg2", "psycopg2-binary"), ("jose", "python-jose")]
    for imp, dist in cases:
        check(f"map_{imp}_to_{dist}",
              KNOWN_PACKAGES.get(imp, (None, None))[0] == dist)


if __name__ == "__main__":
    print("python dep-inference smoke:")
    test_collect_imports_basic()
    test_infer_skips_stdlib_and_local()
    test_infer_skips_already_in_requirements()
    test_apply_appends_to_existing()
    test_apply_creates_new_requirements()
    test_apply_empty_missing_no_op()
    test_recover_pipeline()
    test_recover_no_op_when_clean()
    test_critical_mappings()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\npython dep-inference: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
