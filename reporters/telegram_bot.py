#!/usr/bin/env python3
"""
Минимальный Telegram-бот для мониторинга и управления Webbles Fix.
Использует единый контроллер, переданный из run.py.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

sys.path.insert(0, str(Path(__file__).parent.parent))

# Контроллер будет передан из run.py через set_controller()
controller = None

def set_controller(ctrl):
    global controller
    controller = ctrl

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📊 Статус", "🔄 Перезапуск"],
        ["🔔 Уведомления вкл", "🔕 Уведомления выкл"],
        ["❌ Стоп", "👋 Выход"]
    ],
    resize_keyboard=True,
    input_field_placeholder="Выберите действие..."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 *Webbles Fix Bot*\n\n"
        "Я мониторю автономный конвейер исправления кода.\n"
        "Используйте кнопки ниже.",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global controller
    if controller is None:
        await update.message.reply_text("⚠️ Контроллер не подключён.", reply_markup=MAIN_KEYBOARD)
        return
    if controller.pipeline_running:
        state = controller.get_pipeline_state()
        progress = controller.get_pipeline_progress()
        if state:
            msg = f"⏳ Конвейер выполняется: {state}\n{progress}"
        else:
            msg = "⏳ Конвейер выполняется..."
        await update.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)
    else:
        await update.message.reply_text("✅ Нет активных заданий.", reply_markup=MAIN_KEYBOARD)


async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🔄 *Перезапуск*\n\n"
        "Конвейер запускается через консоль. Чтобы перезапустить:\n"
        "1. Остановите текущий процесс (`Ctrl+C`).\n"
        "2. Запустите заново: `python run.py` -> выберите CLI.\n"
        "Или дождитесь автоматического завершения задачи.",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global controller
    if controller and controller.pipeline_running:
        await update.message.reply_text("⚠️ Остановка задачи запрошена. Дождитесь завершения.", reply_markup=MAIN_KEYBOARD)
    else:
        await update.message.reply_text("✅ Нет активных заданий.", reply_markup=MAIN_KEYBOARD)


async def toggle_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE, enable: bool) -> None:
    global controller
    if controller is None:
        return
    config = controller.load_config()
    config.setdefault("telegram", {})["notifications"] = enable
    controller.save_config(config)
    state = "включены" if enable else "выключены"
    await update.message.reply_text(f"🔔 Уведомления {state}.", reply_markup=MAIN_KEYBOARD)


async def enable_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await toggle_notifications(update, context, True)


async def disable_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await toggle_notifications(update, context, False)


async def exit_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("👋 До свидания! Бот продолжит работу в фоне.")
    await update.message.reply_text("Клавиатура скрыта.", reply_markup=ReplyKeyboardMarkup([[]]))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    if text == "📊 Статус":
        await status(update, context)
    elif text == "🔄 Перезапуск":
        await restart(update, context)
    elif text == "🔔 Уведомления вкл":
        await enable_notifications(update, context)
    elif text == "🔕 Уведомления выкл":
        await disable_notifications(update, context)
    elif text == "❌ Стоп":
        await stop_command(update, context)
    elif text == "👋 Выход":
        await exit_bot(update, context)


def run_bot(token: str) -> None:
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("restart", restart))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("notify_on", enable_notifications))
    application.add_handler(CommandHandler("notify_off", disable_notifications))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.run_polling()