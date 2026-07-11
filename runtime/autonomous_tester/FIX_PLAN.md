# Webbles Fix — Plan фиксов (post-control-series)

**Дата:** 2026-06-14  
**Основание:** AUDIT_control_series.md — серия 20 проектов  
**Статус:** РЕАЛИЗОВАН

---

## Реализованные фиксы

### BUG-4: LLM TimeoutError — retry/backoff [ИСПРАВЛЕН]

**Файл:** `fixers/local_llm_provider.py`  
**Изменение:** В `_do_request` добавлен цикл retry (до 3 попыток) с паузами 10/30/60 сек для `TimeoutError` и `socket.timeout`.  
**Влияние:** P17, P18, P19, P20 — 4/6 PARTIAL были вызваны только этим.

### BUG-3: NET_DELTA regression loop — per-file cap [ИСПРАВЛЕН]

**Файл:** `core/stages/validate_stage.py`  
**Изменение:** `ValidateStage.MAX_NET_DELTA_ROLLBACKS_PER_FILE = 3`. После 3 rollbacks одного файла — bulk-skip всех ошибок файла (аналог TESP cap).  
**Влияние:** P03×5, P05×7, P12×5, P17×6, P19×5.

### BUG-2: `_o15_retries` глобальный → per-error-signature [ИСПРАВЛЕН]

**Файл:** `core/stages/apply_patch_stage.py`  
**Изменение:** Ключ изменён с `"_o15_retries"` на `f"_o15_retries_{error_signature}"`.  
**Влияние:** Разные ошибки больше не блокируют O.15 retry друг друга.

### BUG-6: cp1251 encoding в subprocess [ИСПРАВЛЕН]

**Файлы:** `analyzers/ruff_analyzer.py`, `analyzers/bandit_analyzer.py`, `analyzers/mypy_analyzer.py`, `analyzers/python_analyzer.py`  
**Изменение:** `text=True` → `text=True, encoding='utf-8', errors='replace'`.  
**Влияние:** P06 (maifeipin/lite_agent с CJK символами).

### BUG-5: FileAntiLoop counter grows past max [ИСПРАВЛЕН]

**Файл:** `core/anti_loop.py`  
**Изменение:** `self._attempts[file_path] = min(total, self.max_total_attempts)` и `self._sig_attempts[key] = min(sig_count, self.max_attempts_per_file)`.  
**Влияние:** P10 (счётчик рос до 34/20).

### BUG-7: EditSet.no_content — basename path fallback [ИСПРАВЛЕН]

**Файл:** `fixers/structured_edit.py`  
**Изменение:** После exact match и slash-normalized match — дополнительный fallback по basename файла.  
**Влияние:** P18 (tqdm_monitor.py — LLM возвращал имя без пути).

---

## Тесты

- `tests/test_phase_critical_bug_fixes.py` — 20/20 (обновлён для BUG-2)
- `tests/test_phase_net_delta_classify.py` — 69/69
- `tests/test_phase_local_llm.py` — 69/69
- `tests/test_phase_b_structured.py` + `test_phase_a_foundation.py` + `test_phase_logic_guard.py` + `test_phase_p_bandit_explain_autofix.py` — 188/188

---

## Следующий шаг

Запуск 5 реальных проектов для верификации фиксов.
