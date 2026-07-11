"""
Stage L.3 — режимы агента и app-level gating.

Три режима с разными правами. Ограничения enforce'ятся **на уровне приложения**
(какие операции/инструменты доступны агенту в данном режиме), а НЕ на уровне
LLM-API — DeepSeek не enforce'ит запреты. Поэтому источник правды о
дозволенном — таблица `MODE_CAPS` здесь, а не текст системного промпта.

    Режим   | Можно                                   | Нельзя
    --------|-----------------------------------------|------------------------
    chat    | обсуждать, объяснять, советовать         | трогать файлы/пайплайн
    fix     | читать файлы очереди, гонять пайплайн     | обсуждать архитектуру,
            | (генерация→валидация→применение)         | трогать файлы вне очереди
    create  | обсуждать архитектуру, создавать файлы    | пропускать шаги,
            | (через apply-путь)                       | применять без проверки
"""

from __future__ import annotations

from enum import Enum
from typing import Set


class Mode(str, Enum):
    CHAT = "chat"
    FIX = "fix"
    CREATE = "create"


class Capability(str, Enum):
    DISCUSS = "discuss"            # обсуждать/объяснять/советовать
    DISCUSS_ARCH = "discuss_arch"  # обсуждать архитектуру/новые фичи
    READ_FILES = "read_files"      # читать файлы проекта
    RUN_PIPELINE = "run_pipeline"  # запускать цикл починки (Webbles Fix)
    CREATE_FILES = "create_files"  # создавать файлы (через apply-путь)


# Единственный источник правды о дозволенном в каждом режиме.
MODE_CAPS: dict[Mode, Set[Capability]] = {
    Mode.CHAT: {
        Capability.DISCUSS,
        Capability.DISCUSS_ARCH,
    },
    Mode.FIX: {
        Capability.READ_FILES,
        Capability.RUN_PIPELINE,
    },
    Mode.CREATE: {
        Capability.DISCUSS,
        Capability.DISCUSS_ARCH,
        Capability.READ_FILES,
        Capability.CREATE_FILES,
    },
}


_SYSTEM_PROMPTS: dict[Mode, str] = {
    Mode.CHAT: (
        "Ты — ассистент Webbles в режиме ЧАТ. Ты обсуждаешь код, объясняешь и "
        "советуешь, но НЕ вносишь изменения в файлы и не запускаешь починку. "
        "Если просят что-то починить или создать — предложи переключиться в "
        "режим Fix или Create."
    ),
    Mode.FIX: (
        "Ты — агент Webbles в режиме FIX. Ты чинишь ошибки строго по одной из "
        "очереди, через пайплайн (генерация→валидация→применение с проверкой). "
        "Ты НЕ обсуждаешь архитектуру и новые фичи и НЕ трогаешь файлы вне "
        "текущей задачи. На каждое действие даёшь объяснение Что/Почему/Как."
    ),
    Mode.CREATE: (
        "Ты — агент Webbles в режиме CREATE. Ты обсуждаешь архитектуру и "
        "создаёшь/изменяешь файлы — но только через проверяемый apply-путь, не "
        "пропуская шаги и не применяя изменения без валидации."
    ),
}


def can(mode: Mode, capability: Capability) -> bool:
    """Разрешена ли способность в данном режиме (app-level gating)."""
    return capability in MODE_CAPS.get(mode, set())


def system_prompt(mode: Mode) -> str:
    return _SYSTEM_PROMPTS.get(mode, "")


def refusal(mode: Mode, capability: Capability) -> str:
    """Сообщение-отказ с подсказкой, в какой режим переключиться."""
    suggest = [m for m, caps in MODE_CAPS.items() if capability in caps and m != mode]
    if suggest:
        names = " или ".join(m.value for m in suggest)
        return (f"В режиме «{mode.value}» это недоступно. "
                f"Переключись в режим «{names}».")
    return f"В режиме «{mode.value}» это недоступно."
