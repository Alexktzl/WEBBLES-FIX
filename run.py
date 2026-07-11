#!/usr/bin/env python3
"""
Webbles Fix — единая точка запуска.
Запуск: python run.py
"""

import logging
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.controller import Controller

LOGO = r"""
██╗    ██╗███████╗██████╗ ██████╗ ██╗     ███████╗███████╗    ███████╗██╗██╗  ██╗
██║    ██║██╔════╝██╔══██╗██╔══██╗██║     ██╔════╝██╔════╝    ██╔════╝██║╚██╗██╔╝
██║ █╗ ██║█████╗  ██████╔╝██████╔╝██║     █████╗  ███████╗    █████╗  ██║ ╚███╔╝ 
██║███╗██║██╔══╝  ██╔══██╗██╔══██╗██║     ██╔══╝  ╚════██║    ██╔══╝  ██║ ██╔██╗ 
╚███╔███╔╝███████╗██████╔╝██████╔╝███████╗███████╗███████║    ██║     ██║██╔╝ ██╗
 ╚══╝╚══╝ ╚══════╝╚═════╝ ╚═════╝ ╚══════╝╚══════╝╚══════╝    ╚═╝     ╚═╝╚═╝  ╚═╝
"""


def print_menu() -> None:
    print(LOGO)
    print("=" * 60)
    print("WEBLES FIX — РЕЖИМ ЗАПУСКА")
    print("=" * 60)
    print("1. Запустить (CLI + Telegram бот)")
    print("2. Выход")
    print("\nНажмите цифру (1-2)...")


def getch() -> str:
    try:
        import msvcrt
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
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            return sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def main() -> int:
    ctrl = Controller()

    while True:
        print_menu()
        key = getch()
        if key == '1':
            config = ctrl.load_config()
            token = config.get("telegram", {}).get("token")

            if token:
                # Настраиваем логирование бота в файл
                bot_log_file = Path("webbles_bot.log")
                bot_handler = logging.FileHandler(bot_log_file, mode='w', encoding='utf-8')
                bot_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

                for name in ("telegram", "httpx", "apscheduler", "telegram.ext", "telegram.vendor.ptb_urllib3.urllib3"):
                    logger_obj = logging.getLogger(name)
                    logger_obj.handlers.clear()
                    logger_obj.addHandler(bot_handler)
                    logger_obj.setLevel(logging.INFO)
                    logger_obj.propagate = False

                # Передаём тот же контроллер в бота
                from reporters.telegram_bot import set_controller, run_bot
                set_controller(ctrl)

                def run_bot_thread():
                    try:
                        run_bot(token)
                    except Exception as e:
                        logging.getLogger("telegram").error(f"Bot crashed: {e}", exc_info=True)

                bot_thread = threading.Thread(target=run_bot_thread, daemon=True)
                bot_thread.start()

                print("\n✅ Бот запущен в фоне. Логи пишутся в webbles_bot.log")
                print("Вы можете продолжать работу в CLI.\n")
            else:
                print("\n⚠️ Токен бота не настроен. Уведомления не будут отправляться.")
                print("Настройте токен через CLI (пункт 2 в главном меню).\n")

            # Запускаем CLI (блокирует выполнение, пока не выйдем из CLI)
            from cli import main as cli_main
            cli_main(ctrl)

        elif key == '2':
            print("\nВыход из программы.")
            sys.exit(0)


if __name__ == "__main__":
    main()