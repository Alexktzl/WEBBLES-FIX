# ModOrganizer2/modorganizer-basic_games  [FAIL]

- Дата: 2026-06-22T22:01:24
- Ошибок: 102  →  102  (delta=0)
- ACCEPT: 0  |  NEEDS_REVIEW: 0  |  REJECT: 5
- Причина: no_fixes_applied — project_timeout (2460s) исчерпан. Массовый PreCleanup-баг сделал ~30 файлов синтаксически невалидными (в основном "( was never closed") — все эти записи пропущены защитой, но это значит правило PreCleanup систематически некорректно работает на этом проекте. Также 2x LLM hard timeout (180s), 1 файл с патчем, ломающим синтаксис (lslib_retriever.py, откат x3). version-detection сработал: 5 файлов исключены как несовместимые по версии Python (не E999).
