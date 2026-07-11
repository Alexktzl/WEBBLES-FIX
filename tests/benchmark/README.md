# Webbles Fix — Benchmark (Stage F)

Курированный набор кейсов для измерения, насколько хорошо отрабатывают
**детерминистические** компоненты системы. LLM-вызовы здесь НЕ делаются —
бенчмарк должен быть воспроизводим до символа.

## Структура каталога

```
tests/benchmark/
├── README.md           ← этот файл
└── cases/
    ├── rust/
    │   ├── e0609_unknown_field/
    │   │   ├── before/
    │   │   │   ├── Cargo.toml
    │   │   │   └── src/main.rs           ← сломанный код
    │   │   ├── after/
    │   │   │   ├── Cargo.toml
    │   │   │   └── src/main.rs           ← как должно быть
    │   │   └── expected.json             ← метаданные кейса
    │   └── …
    ├── python/
    └── javascript/
```

## Формат `expected.json`

```jsonc
{
  "description": "E0609: access to non-existent field 'player_name' on struct Player",
  "language": "rust",                  // rust / python / javascript
  "category": "BLOCKING",              // BLOCKING / CLEANUP / CRITICAL_SYNTAX / WARNING / SECURITY
  "primary_error": {
    "code": "E0609",                   // ID ошибки (rustc / ruff / eslint / …)
    "file": "src/main.rs",             // путь относительно before/
    "line": 4,
    "error_class": "STRUCTURAL",
    "message": "no field `player_name` on type `Player`"
  },
  "acceptable_fixes": [                // человеко-читаемое описание правильных решений
    "rename access to existing field name (.player_name → .name)"
  ],

  // ОПЦИОНАЛЬНО: точечные проверки конкретных компонентов
  "constraints_test": {
    "expected_dont_contains": "add a new field",
    "expected_do_contains":   "Use the actual field names"
  },
  "symbol_graph_test": {
    "must_find_definition_of": "Player"
  }
}
```

`acceptable_fixes` пока используется только для документации, но в Stage F (вторая
итерация) на этом будет строиться более содержательная проверка корректности
сгенерированного патча.

## Что проверяет runner

Запуск:

```bash
python3 tests/run_benchmark.py
python3 tests/run_benchmark.py --language rust
python3 tests/run_benchmark.py --verbose
```

Для каждого кейса проверяет:

1. **structure** — `expected.json` корректно загружается, обязательные поля
   на месте, `before/` и `after/` существуют, `before != after`.
2. **constraints** — если в кейсе указан `constraints_test`, проверяет, что
   `analysis.constraints.error_constraints.get_constraints(code)` возвращает
   ожидаемые DO/DON'T-фрагменты (Stage C.2).
3. **symbol_graph** — если в кейсе указан `symbol_graph_test`, проверяет, что
   `SymbolGraph(before).find_related_definitions(error)` находит определение
   нужного символа (Stage C.1).
4. **ai_authored** — `before`-файл должен иметь AI-score < 0.5
   (`analysis.ai_authored_detector.score`). Если score высокий — наш минимальный
   кейс заподозрен в AI-style, нужна ручная проверка (Stage D.6).

## Что НЕ проверяет (пока)

- Применение реального LLM-патча к `before` и сравнение с `after`.
- ReviewStage (D.3) — требует LLM.
- Конец-в-конец прогона PipelineEngine.

Это — Stage F вторая итерация. Сейчас runner покрывает **детерминистическую
часть** системы. Этого достаточно, чтобы:

- ловить регрессии в C.1 / C.2 / D.6 (уже ловит — см. историю PROGRESS.md);
- расширять набор кейсов с минимальными затратами;
- использовать как фундамент для LLM-driven варианта (через stored fixtures).

## Как добавить кейс

1. Скопируйте любой существующий каталог: `cp -r cases/rust/e0609_unknown_field cases/rust/<new_name>`.
2. Замените `before/*` на новый сломанный код, `after/*` на правильный.
3. Обновите `expected.json`: код, файл, строку, message; категорию; при желании
   добавьте `constraints_test` / `symbol_graph_test`.
4. Прогоните: `python3 tests/run_benchmark.py --language <lang>`.
5. Если что-то падает — либо ваш кейс кривой, либо нашли регрессию.
   Оба исхода ценны.

## Генерированные кейсы (`gen_*`)

Помимо ручных кейсов есть генератор `tests/benchmark/generate_cases.py`,
который из небольшого числа РЕАЛЬНЫХ шаблонов ошибок производит много
вариантов, подставляя имена/типы/модули из пулов. Цель — проверить, что
`SymbolGraph` (C.1), `error_constraints` (C.2) и `ai_authored_detector`
(D.6) работают на разных идентификаторах (`Account`, `Widget`, `Session`,
…), а не запоминают один захардкоженный `Player`.

```bash
python3 tests/benchmark/generate_cases.py          # (пере)создать gen_*
python3 tests/benchmark/generate_cases.py --clean  # удалить все gen_*
```

Генерированные кейсы лежат в `cases/<lang>/gen_<code>_<NNN>/`, помечены
`"generated": true` в `expected.json`, и пересоздаются идемпотентно
(ручные кейсы при этом не трогаются).

## Текущий объём

**263 кейса** (rust 87 + python 86 + js 90): 120 ручных + 143 генерированных.
Категории: BLOCKING 111, WARNING 92, CLEANUP 55, CRITICAL_SYNTAX 5.
Цель плана `WORK_PLAN.md` (≥60) перевыполнена. 100% pass.
