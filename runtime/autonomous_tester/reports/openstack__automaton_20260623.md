# openstack/automaton  [PASS, верифицировано]

- Дата: 2026-06-23T22:05:50
- Ошибок: 5  →  13 (current_total_errors, baseline_remaining; см. ниже)
- ACCEPT (заявлено): 4  |  NEEDS_REVIEW: 5  |  REJECT: 2
- **Верификация: 1 REAL_FIX (strong), 3 REAL_FIX (weak, by-design), 0 STILL_FLAGGED, 0 unsafe_accept**

## Находка инструмента верификации (исправлена по ходу)
Два ACCEPT в одном файле (`test_fsm.py`, строки 23 и 32) — маркер `# type: ignore` от строки 23 ложно приписывался strong-фиксу на строке 32 (`m.default_start_state = start_state` → `m.set_default_start_state(start_state)`, замена прямого присвоения read-only свойства на метод-сеттер — настоящий, содержательный фикс). Причина: `changed_lines()` сканировал ВЕСЬ файл, а не хунк вокруг целевой строки. Исправлено: добавлен `target_line`-scoping (окно ±3 строки вокруг хунка). Регрессионный тест добавлен.

## Детали ACCEPT
1. `machines.py:19` import-not-found (prettytable) — weak/by-design
2. `converters/pydot.py:21` import-not-found (pydot) — weak/by-design
3. `tests/test_fsm.py:23` import-not-found (testtools) — weak/by-design
4. `tests/test_fsm.py:32` misc (read-only property) — **strong**, после исправления verify_accepts.py
