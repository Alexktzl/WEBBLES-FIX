"""
Stage J — External example search.

Когда система натыкается на ошибку, у людей-разработчиков первое действие —
поискать «как это чинят». Этот пакет добавляет в case-file (C.4) секцию
EXTERNAL EXAMPLES: прецеденты из открытых источников.

Каскад «дёшево → дорого», с агрессивным кэшем по error_code:
  1. rustc --explain  (бесплатно, локально, мгновенно)
  2. GitHub Code Search (нужен GITHUB_TOKEN, opt-in)
  3. StackOverflow API  (opt-in)

Публичный API:
    from analysis.external_examples import Example, ExampleSearchService
"""

from analysis.external_examples.provider import (
    Example,
    ExampleSearchProvider,
    ExampleSearchService,
)

__all__ = ["Example", "ExampleSearchProvider", "ExampleSearchService"]
