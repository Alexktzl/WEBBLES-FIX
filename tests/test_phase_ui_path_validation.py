"""
Tech debt audit 2026-06-21, находка #6 (P2): eel-exposed методы (studio.py
start_fix, web_app.py run_pipeline) принимали путь к проекту от JS-стороны
без какой-либо проверки существования/типа. Низкий риск для loopback-only
desktop-приложения, но дешёвая и полезная проверка — отклоняем явно с
понятным сообщением вместо падения глубже в run_fix_agent/ctrl.run_pipeline.

НЕ сужаем до "разрешённого корня" — легитимный сценарий это починка ЛЮБОГО
проекта пользователя, поэтому единственная проверка — путь должен реально
резолвиться в существующую директорию.

eel — desktop-bridge библиотека с side-effect-ным `eel.init()` на уровне
модуля; мокается перед импортом, чтобы тест не зависел от наличия конкретных
HTML-папок и не пытался поднять реальный браузерный мост.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _make_fake_eel():
    """@eel.expose должен оставаться identity-декоратором — иначе он
    заменяет реальную функцию заглушкой-моком целиком."""
    fake = MagicMock()
    fake.expose = lambda f: f
    return fake


@pytest.fixture
def studio_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "eel", _make_fake_eel())
    import importlib
    if "studio" in sys.modules:
        importlib.reload(sys.modules["studio"])
        mod = sys.modules["studio"]
    else:
        mod = importlib.import_module("studio")
    return mod


@pytest.fixture
def web_app_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "eel", _make_fake_eel())
    import importlib
    if "web_app" in sys.modules:
        importlib.reload(sys.modules["web_app"])
        mod = sys.modules["web_app"]
    else:
        mod = importlib.import_module("web_app")
    return mod


def test_start_fix_rejects_nonexistent_path(studio_module, monkeypatch):
    pushed = []
    monkeypatch.setattr(studio_module, "_push", lambda e: pushed.append(e))
    result = studio_module.start_fix("/this/path/does/not/exist/at/all", "python")
    assert result is False
    assert pushed and pushed[0]["type"] == "run_error"
    assert "не найден" in pushed[0]["data"]["message"]


def test_start_fix_rejects_file_path_not_directory(studio_module, monkeypatch, tmp_path):
    pushed = []
    monkeypatch.setattr(studio_module, "_push", lambda e: pushed.append(e))
    f = tmp_path / "not_a_dir.txt"
    f.write_text("x", encoding="utf-8")
    result = studio_module.start_fix(str(f), "python")
    assert result is False
    assert pushed and "директорией" in pushed[0]["data"]["message"]


def test_start_fix_accepts_valid_existing_directory(studio_module, monkeypatch, tmp_path):
    """Валидный путь должен пройти проверку (не отклоняться на этапе валидации) —
    не дожидаемся реального запуска фонового потока/run_fix_agent."""
    pushed = []
    monkeypatch.setattr(studio_module, "_push", lambda e: pushed.append(e))
    monkeypatch.setattr(studio_module, "run_fix_agent", lambda *a, **kw: {"refused": False})
    monkeypatch.setattr(studio_module.threading, "Thread",
                        lambda target, daemon: MagicMock(start=lambda: target()))
    result = studio_module.start_fix(str(tmp_path), "python")
    assert result is True
    assert not any(p.get("type") == "run_error" for p in pushed)


def test_run_pipeline_rejects_nonexistent_path(web_app_module):
    result = web_app_module.run_pipeline("/this/path/does/not/exist/at/all", "python")
    assert result["status"] == "error"
    assert "не найден" in result["message"]


def test_run_pipeline_rejects_file_path_not_directory(web_app_module, tmp_path):
    f = tmp_path / "not_a_dir.txt"
    f.write_text("x", encoding="utf-8")
    result = web_app_module.run_pipeline(str(f), "python")
    assert result["status"] == "error"
    assert "директорией" in result["message"]


def test_run_pipeline_accepts_valid_existing_directory(web_app_module, tmp_path, monkeypatch):
    monkeypatch.setattr(
        web_app_module.ctrl, "run_pipeline",
        lambda *a, **kw: {"initial_error_count": 0, "final_error_count": 0, "accepted_patches": 0},
    )
    result = web_app_module.run_pipeline(str(tmp_path), "python")
    assert result["status"] == "ok"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
