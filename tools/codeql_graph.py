import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Dict, Any
from tools.base_tool import BaseTool

class CodeQLGraph(BaseTool):
    """Строит граф зависимостей через CodeQL с автоустановкой."""

    def is_available(self) -> bool:
        try:
            subprocess.run(["codeql", "--version"], capture_output=True, timeout=5)
            return True
        except FileNotFoundError:
            return False
        except Exception:
            return False

    def ensure_installed(self) -> bool:
        if self.is_available():
            return True

        # Проверяем, есть ли gh CLI
        has_gh = False
        try:
            subprocess.run(["gh", "--version"], capture_output=True, timeout=5)
            has_gh = True
        except FileNotFoundError:
            pass

        if has_gh:
            cmd = "gh extensions install github/gh-codeql"
            return self._prompt_install(cmd, "CodeQL (через gh)")
        else:
            print("\n⚠️  CodeQL не найден, и GitHub CLI (gh) не установлен.")
            print("    Установите CodeQL CLI вручную:")
            print("    https://github.com/github/codeql-cli-binaries/releases")
            print("    Или установите GitHub CLI: https://cli.github.com/")
            return False

    def run(self, project_path: Path) -> Optional[Dict[str, Any]]:
        if not self.ensure_installed():
            return None

        # Создаём временную базу данных для анализа
        with tempfile.TemporaryDirectory(prefix="webbles_codeql_") as tmpdir:
            db_path = Path(tmpdir) / "db"
            try:
                # Создаём БД
                subprocess.run(
                    ["codeql", "database", "create", str(db_path), "--language=rust", f"--source-root={project_path}"],
                    check=True, capture_output=True, text=True, timeout=300
                )
                return {"status": "ok", "db_path": str(db_path)}
            except subprocess.CalledProcessError as e:
                print(f"    ⚠️ Не удалось создать базу CodeQL: {e.stderr}")
                return None