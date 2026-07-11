#!/usr/bin/env python3
"""
Webbles Fix — автономная система исправления кода.
Запуск: python run.py
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# Для мгновенного ввода без Enter
try:
    import msvcrt
    def getch() -> str:
        ch = msvcrt.getch()
        if ch in (b'\x00', b'\xe0'):
            msvcrt.getch()
            return ''
        try:
            return ch.decode('utf-8', errors='ignore')
        except UnicodeDecodeError:
            return ''
except ImportError:
    import termios
    import tty
    def getch() -> str:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            return sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

from logging.handlers import RotatingFileHandler

sys.path.insert(0, str(Path(__file__).parent))

from core.controller import Controller, PROMPT_FILE

LOGO = r"""
██╗    ██╗███████╗██████╗ ██████╗ ██╗     ███████╗███████╗    ███████╗██╗██╗  ██╗
██║    ██║██╔════╝██╔══██╗██╔══██╗██║     ██╔════╝██╔════╝    ██╔════╝██║╚██╗██╔╝
██║ █╗ ██║█████╗  ██████╔╝██████╔╝██║     █████╗  ███████╗    █████╗  ██║ ╚███╔╝ 
██║███╗██║██╔══╝  ██╔══██╗██╔══██╗██║     ██╔══╝  ╚════██║    ██╔══╝  ██║ ██╔██╗ 
╚███╔███╔╝███████╗██████╔╝██████╔╝███████╗███████╗███████║    ██║     ██║██╔╝ ██╗
 ╚══╝╚══╝ ╚══════╝╚═════╝ ╚═════╝ ╚══════╝╚══════╝╚══════╝    ╚═╝     ╚═╝╚═╝  ╚═╝
"""


def setup_logging(log_file: Optional[Path] = None, level=logging.INFO) -> None:
    logger = logging.getLogger()
    logger.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)
    if log_file:
        handler = RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)


def clear_screen() -> None:
    os.system('cls' if os.name == 'nt' else 'clear')


def print_header() -> None:
    print(LOGO)
    print("=" * 60)
    print("ГЛАВНОЕ МЕНЮ")
    print("=" * 60)
    print("1. Запуск")
    print("2. Настройки")
    print("3. Выход")
    print("\nНажмите цифру (1-3) без Enter...")


def detect_language(project_path: Path) -> Optional[str]:
    indicators = {
        "rust": ["Cargo.toml", "Cargo.lock"],
        "python": ["requirements.txt", "setup.py", "pyproject.toml", "Pipfile"],
        "javascript": ["package.json", "package-lock.json", "yarn.lock"],
        "typescript": ["tsconfig.json", "package.json"],
    }
    scores = {lang: 0 for lang in indicators}
    for lang, files in indicators.items():
        for file in files:
            if (project_path / file).exists():
                scores[lang] += 1
    py_files = list(project_path.rglob("*.py"))
    rs_files = list(project_path.rglob("*.rs"))
    js_files = list(project_path.rglob("*.js"))
    ts_files = list(project_path.rglob("*.ts"))
    if rs_files:
        scores["rust"] += len(rs_files)
    if py_files:
        scores["python"] += len(py_files)
    if ts_files:
        scores["typescript"] += len(ts_files) * 2
    elif js_files:
        scores["javascript"] += len(js_files)
    best = max(scores, key=scores.get)
    if scores[best] > 0:
        return best
    return None


def get_project_path() -> Optional[Path]:
    """Запрашивает путь к проекту."""
    print("\n📁 Введите путь к проекту (или Enter для отмены):")
    path_str = input().strip()
    if not path_str:
        return None
    path = Path(path_str).resolve()
    if not path.exists():
        print(f"❌ Путь '{path}' не существует.")
        return None
    return path


def run_pipeline(ctrl: Controller, project_path: Path, language: str) -> None:
    """Запускает конвейер с текущими настройками."""
    config = ctrl.load_config()
    if not config:
        print("⚠️ Нет сохранённой конфигурации. Сначала настройте систему (пункт 2).")
        return

    logger = logging.getLogger("webbles_fix")
    logger.info("=" * 60)
    logger.info("Webbles Fix — автономная система исправления кода")
    logger.info("=" * 60)
    logger.info(f"Проект: {project_path}")
    logger.info(f"Язык: {language}")

    # Явно устанавливаем флаг, чтобы бот сразу увидел статус
    ctrl.pipeline_running = True
    try:
        result = ctrl.run_pipeline(project_path, language, config)
        print("\n" + "=" * 60)
        print("РЕЗУЛЬТАТ РАБОТЫ")
        print("=" * 60)
        print(f"Статус: {result['status']}")
        print(f"Итераций: {result['iterations']}")
        print(f"Откатов: {result['rollbacks']}")
        print(f"Ошибок до: {result['initial_error_count']}")
        print(f"Осталось из baseline (baseline_remaining): {result.get('baseline_remaining', result['final_error_count'])}")
        print(f"Всего ошибок сейчас (current_total_errors): {result.get('current_total_errors')}")
        print(f"Принято патчей: {result['accepted_patches']}")
        print(f"Отклонено патчей: {result['rejected_patches']}")
        print(f"Время выполнения: {result['total_runtime_seconds']:.2f} сек")
        if result.get("system_health"):
            health = result["system_health"]
            print(f"\nЗдоровье системы: {health.get('overall', 0):.2f}")
        if result.get("error"):
            print(f"\nОшибка: {result['error']}")

        # Вывод оставшихся ошибок
        if result.get("remaining_errors"):
            print("\n⚠️ ОСТАВШИЕСЯ ОШИБКИ (требуют ручного вмешательства):")
            for i, err in enumerate(result["remaining_errors"], 1):
                print(f"  {i}. {err['file']}:{err['line']} [{err['code']}] {err['message']}")

        if result.get("unfixable_errors"):
            print("\n🔒 НЕИСПРАВИМЫЕ ОШИБКИ (помечены как нерешаемые):")
            for i, err in enumerate(result["unfixable_errors"], 1):
                print(f"  {i}. {err['file']}:{err['line']} [{err['code']}] {err['message']}")

    except Exception as e:
        logger.exception("Ошибка при выполнении конвейера")
        print(f"Критическая ошибка: {e}")
    finally:
        ctrl.pipeline_running = False


def settings_menu(ctrl: Controller) -> None:
    """Меню настроек."""
    while True:
        clear_screen()
        print("=" * 60)
        print("НАСТРОЙКИ")
        print("=" * 60)
        print("1. LLM (провайдер, модель, ключ)")
        print("2. Telegram (токен, chat ID)")
        print("3. Параметры конвейера (итерации, глубина, строгость)")
        print("4. Эталон здоровья (baseline)")
        print("5. Промпт (редактировать)")
        print("6. Сбросить всё к значениям по умолчанию")
        print("7. Назад")
        print("\nНажмите цифру (1-7) без Enter...")
        key = getch()
        if key == '1':
            llm_settings(ctrl)
        elif key == '2':
            telegram_settings(ctrl)
        elif key == '3':
            pipeline_settings(ctrl)
        elif key == '4':
            baseline_settings(ctrl)
        elif key == '5':
            prompt_settings(ctrl)
        elif key == '6':
            confirm = input("Сбросить ВСЕ настройки к значениям по умолчанию? (y/N): ").strip().lower()
            if confirm == 'y':
                ctrl.reset_to_defaults()
                print("✅ Настройки сброшены.")
                input("Нажмите Enter для продолжения...")
        elif key == '7':
            break


def llm_settings(ctrl: Controller) -> None:
    config = ctrl.load_config()
    print("\n--- LLM ---")
    provider = input(f"Провайдер (openai/anthropic/deepseek/ollama) [{config.get('llm', {}).get('provider', 'deepseek')}]: ").strip()
    if provider:
        ctrl.set_llm_config(provider=provider)
    model = input(f"Модель [{config.get('llm', {}).get('model', 'deepseek-v4-flash')}]: ").strip()
    if model:
        ctrl.set_llm_config(model=model)
    api_key = input("API ключ (оставьте пустым, чтобы не менять): ").strip()
    if api_key:
        ctrl.set_llm_config(api_key=api_key)
    base_url = input(f"Base URL [{config.get('llm', {}).get('base_url', 'https://api.deepseek.com/v1')}]: ").strip()
    if base_url:
        ctrl.set_llm_config(base_url=base_url)
    max_tokens = input(f"Max tokens [{config.get('llm', {}).get('max_tokens', 8000)}]: ").strip()
    if max_tokens:
        try:
            ctrl.set_llm_config(max_tokens=int(max_tokens))
        except ValueError:
            pass
    print("✅ Настройки LLM сохранены.")
    input("Нажмите Enter для продолжения...")


def telegram_settings(ctrl: Controller) -> None:
    config = ctrl.load_config()
    print("\n--- Telegram ---")
    token = input("Токен бота (оставьте пустым, чтобы не менять): ").strip()
    if token:
        ctrl.set_telegram_config(token=token)
    chat_id = input("Chat ID: ").strip()
    if chat_id:
        ctrl.set_telegram_config(chat_id=chat_id)
    print("✅ Настройки Telegram сохранены.")
    input("Нажмите Enter для продолжения...")


def pipeline_settings(ctrl: Controller) -> None:
    config = ctrl.load_config()
    print("\n--- Параметры конвейера ---")
    max_iter = input(f"Макс. итераций [{config.get('pipeline', {}).get('max_iterations', 10)}]: ").strip()
    if max_iter:
        try:
            ctrl.set_pipeline_config(max_iterations=int(max_iter))
        except ValueError:
            pass
    depth = input(f"Глубина планирования [{config.get('pipeline', {}).get('planning_depth', 2)}]: ").strip()
    if depth:
        try:
            ctrl.set_pipeline_config(planning_depth=int(depth))
        except ValueError:
            pass
    beam = input(f"Ширина луча [{config.get('pipeline', {}).get('beam_width', 3)}]: ").strip()
    if beam:
        try:
            ctrl.set_pipeline_config(beam_width=int(beam))
        except ValueError:
            pass
    strict = input(f"Строгость (0.1-2.0) [{config.get('pipeline', {}).get('strictness', 1.0)}]: ").strip()
    if strict:
        try:
            ctrl.set_pipeline_config(strictness=float(strict))
        except ValueError:
            pass
    use_planning = input(f"Использовать планирование? (y/n) [{'y' if config.get('pipeline', {}).get('use_planning', True) else 'n'}]: ").strip().lower()
    if use_planning in ('y', 'n'):
        ctrl.set_pipeline_config(use_planning=(use_planning == 'y'))
    dry_run = input(f"Режим симуляции? (y/n) [{'y' if config.get('dry_run', False) else 'n'}]: ").strip().lower()
    if dry_run in ('y', 'n'):
        ctrl.set_pipeline_config(dry_run=(dry_run == 'y'))
    print("✅ Параметры конвейера сохранены.")
    input("Нажмите Enter для продолжения...")


def baseline_settings(ctrl: Controller) -> None:
    config = ctrl.load_config()
    print("\n--- Эталон здоровья ---")
    baseline_file = input(f"Путь к файлу эталона (JSON) [{config.get('pipeline', {}).get('baseline_file', '')}]: ").strip()
    if baseline_file:
        ctrl.set_baseline_config(baseline_file=baseline_file)
    reset = input("Сбросить эталон при следующем запуске? (y/n) [n]: ").strip().lower()
    ctrl.set_baseline_config(reset_baseline=(reset == 'y'))
    print("✅ Настройки эталона сохранены.")
    input("Нажмите Enter для продолжения...")


def prompt_settings(ctrl: Controller) -> None:
    print("\n--- Промпт ---")
    if ctrl.prompt_exists():
        print("Текущий промпт загружен из файла prompts/fix_prompt.txt")
        edit = input("Открыть для редактирования? (y/N): ").strip().lower()
        if edit == 'y':
            if os.name == 'nt':
                os.system(f'notepad "{PROMPT_FILE}"')
            else:
                print("Редактируйте файл вручную:", PROMPT_FILE)
    else:
        print("Файл промпта не найден.")
        ctrl.ensure_prompt_file()
    input("Нажмите Enter для продолжения...")


def main(ctrl: Controller) -> None:
    setup_logging(Path("webbles_fix.log"), level=logging.INFO)

    ctrl.ensure_prompt_file()

    if not ctrl.config_exists():
        ctrl.reset_to_defaults()
        print("✅ Создана конфигурация по умолчанию.")

    while True:
        clear_screen()
        print_header()
        key = getch()
        if key == '1':
            project_path = get_project_path()
            if project_path is None:
                continue
            language = detect_language(project_path)
            if language:
                lang_names = {"rust": "Rust", "python": "Python", "javascript": "JavaScript", "typescript": "TypeScript"}
                print(f"🔍 Определён язык: {lang_names.get(language, language)}")
                confirm = input("Подтвердить? (Y/n): ").strip().lower()
                if confirm in ('n', 'no', 'нет'):
                    language = None
            if not language:
                print("Выберите язык: 1. Rust, 2. Python, 3. JavaScript, 4. TypeScript")
                lang_choice = input("Ваш выбор (1-4): ").strip()
                langs = {"1": "rust", "2": "python", "3": "javascript", "4": "typescript"}
                language = langs.get(lang_choice, "python")
            run_pipeline(ctrl, project_path, language)
            input("\nНажмите Enter, чтобы вернуться в меню...")
        elif key == '2':
            settings_menu(ctrl)
        elif key == '3':
            print("\nВыход из программы.")
            sys.exit(0)


if __name__ == "__main__":
    main(Controller())