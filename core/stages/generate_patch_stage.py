"""
Стадия генерации патча (single-writer architecture).
Эвристики НЕ изменяют файл. Только PatchApplier может менять код.
Использует отдельный PromptBuilder для формирования промпта.
Интегрированы внешние инструменты: Correctr, Repomix, Aider repo‑map, SocraticRefiner.
Добавлена прямая эвристика для E0765 (незакрытая кавычка) без LLM.
Добавлена очистка дубликатов через huniq (стабильный внешний инструмент) перед отправкой в LLM.
Добавлены структурные анкоры (structural anchors) в промпт для сохранения семантики.
"""

import difflib
import hashlib
import logging
import re
import subprocess
import time
from pathlib import Path, PurePath
from types import MappingProxyType
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from core.anti_loop import SegmentAntiLoop, FileAntiLoop
from core.contract import MetadataKeys
from core.error_context_validator import ErrorContextValidator
from core.pipeline_context import PipelineContext
from core.pipeline_stage import PipelineStage
from core.state_machine import State
from core.utils import process_key as _process_key
from fixers.file_segmenter import FileSegmenter
from fixers.patch_engine import PatchEngine
from fixers.segment_context_builder import SegmentContextBuilder
from fixers.prompt_builder import PromptBuilder
from fixers.rule_based_fixer import RuleBasedFixer
from memory.learning import MemoryLearning
from analysis.error_classifier import ErrorClassifier
from analysis.symbol_graph import SymbolGraph
from analysis.constraints.error_constraints import get_constraints, get_security_example, get_fix_recipe
from tools.dependency_search import DependencySearch
from fixers.toml_patcher import update_dependency_version, upsert_dependency
from fixers.brace_utils import count_braces_safe
from fixers.language_syntax.rust_healer import RustSyntaxHealer
from fixers.semantic_repair import SemanticRepair
from fixers.patch_normalizer import normalize_patch

# ---------- Адаптеры внешних инструментов ----------
from tools.correctr_analyzer import CorrectrAnalyzer
from tools.repomix_context import RepomixContextProvider
from tools.aider_repomap import AiderRepoMap
from tools.socratic_refiner import SocraticRefiner

if TYPE_CHECKING:
    from fixers.llm_client import LLMClient
    from fixers.syntax_disaster_recovery import SyntaxDisasterRecovery

logger = logging.getLogger(__name__)

# IMP-1: mypy codes that require installing stub packages — cannot be patched.
# Skipping them avoids wasted LLM iterations and spurious REJECT counts.
# F6: "untyped-decorator" добавлен — без кода в выводе mypy эти ошибки
# раньше проваливались мимо этой проверки прямо в LLM (см. _synthesize_mypy_code
# в analyzers/mypy_analyzer.py). _make_type_ignore_patch для неё всегда
# вернёт None (не import-строка) — код упадёт в "пропускаем без LLM".
# control series 12 analysis (2026-06-20): "import-not-found" добавлен —
# это ОТДЕЛЬНЫЙ от "import-untyped" mypy-код (пакет вообще не установлен/
# stub-ов нет вообще, а не "установлен, но без типов"), но с тем же
# детерминированным фиксом — # type: ignore[<code>] на строке импорта.
# На mbk-dev/okama-dash это 337/457 (73.7%) всех ошибок проекта — самый
# частый код во всей серии, раньше уходил в LLM целиком.
_UNFIXABLE_MYPY_CODES: frozenset = frozenset({
    "import-untyped", "import-not-found", "untyped-decorator",
})

# 2026-07-03 (pyca/bcrypt, замер №7): mypy-коды, для которых существует
# честный zero-collateral rule_based-фиксер (_py_list_item_tuple_repair,
# vector→tuple ремонт гетерогенных list-of-lists тест-векторов). В обычной
# диспетчеризации rule_based стоит ПОСЛЕ memory/golden; на первой же ошибке
# блока векторов memory-реплей (bracket-flatten — литералы целы, O.19 молчит)
# структурно мутировал блок, после чего строгий precondition фиксера (все
# inner — list-литералы) падал для остальных ошибок блока, и они уходили в
# LLM whack-a-mole; файл целиком откатывался финальным аудитом. Для ЭТИХ
# кодов даём детерминированному фиксеру приоритет над memory/golden — он
# чинит весь блок за один replace_file, остальные ошибки блока исчезают.
# Приоритет узкий: если фиксер не сработал (None), управление возвращается в
# обычную цепочку golden→memory→...→LLM без изменений.
_VECTOR_REPAIR_PREEMPT_CODES: frozenset = frozenset({"list-item", "arg-type"})


def _wrap_import_with_ignore_comment(
    stripped_line: str, indent: str, eol: str, ignore_code: str,
) -> Optional[str]:
    """2026-06-24 (malinkang/toggl2notion line-shift расследование):
    `from X import a, b, c  # type: ignore[code]` иногда пересекает лимит
    длины строки ровно из-за добавленного комментария — превращает
    безопасный детерминированный фикс в новую E501, которую NET_DELTA
    откатывает как регрессию. Если у импорта несколько имён через запятую
    (и он ещё не обёрнут в скобки) — переписываем в многострочную форму:

        from X import (  # type: ignore[code]
            a,
            b,
            c,
        )

    mypy привязывает `# type: ignore` к строке, где начинается оператор
    импорта (`from X import (`), это валидно независимо от того, сколько
    строк занимает сам импорт. Однострочный `import x` (без `from`/без
    запятых) так обернуть нельзя — возвращаем None, вызывающий код
    оставляет старое поведение (однострочный комментарий, даже если он
    превысит лимit — это уже существующее, не новое ограничение)."""
    if not stripped_line.startswith("from ") or " import " not in stripped_line:
        return None
    module_part, names_part = stripped_line.split(" import ", 1)
    names_part = names_part.strip()
    if "(" in names_part or names_part.endswith("\\"):
        return None  # уже многострочный/с переносом — не трогаем
    names = [n.strip() for n in names_part.split(",") if n.strip()]
    if len(names) < 2:
        return None  # одно имя — оборачивать в скобки нет смысла/выигрыша
    out = [f"{indent}{module_part} import (  # type: ignore[{ignore_code}]{eol}"]
    for name in names:
        out.append(f"{indent}    {name},{eol}")
    out.append(f"{indent}){eol}")
    return "".join(out)


class GeneratePatchStage(PipelineStage):
    MAX_ATTEMPTS_BEFORE_SEGMENTATION = 2
    FULL_FILE_LINE_LIMIT = 600
    MAX_EMPTY_RETRIES = 3
    # Perf-2 (2026-07-07, цель Алекса ≤600s/проект): потолки LLM-петель
    # снижены по perf-профилю — продуктивные решения выходят из петель за
    # 1-4 итерации (learning_cases июль), хвост 5..20 наблюдался ТОЛЬКО у
    # обречённых файлов, которые теперь отсекают circuit-breaker perf-1 и
    # FileAntiLoop; сегментная петля 10→5, blocking-retry 20→8. Один
    # обрезанный оборот ≈ 40s LLM + скан.
    MAX_SEGMENT_ATTEMPTS = 5
    MAX_SEGMENTED_STRATEGY_ATTEMPTS = 3
    # 2026-07-08 (loguru №6, разбор deep-reasoner): blocking-петля — главный
    # сток вызовов (~150 из 206 за прогон), при этом 8 ретраев дали 1 успех
    # на ~48 входов. Гранулярность FileAntiLoop (раз на вход в файл) грубее
    # вызовов — до 9 вызовов до следующей проверки. 8 → 3: по данным успех
    # приходит на 1-3-й попытке либо не приходит вовсе.
    MAX_BLOCKING_LLM_RETRIES = 3
    MAX_CONTEXT_TOKENS = 12000

    # empty_response диагностика (2026-06-22) — категории, для которых
    # LLM не вернул НИЧЕГО оцениваемого (нет ответа вовсе, не распарсился
    # формат) — это инфраструктурный/транспортный сбой, НЕ решение о
    # качестве патча, поэтому НЕ должен попадать в rejected_patches/
    # decisions[] REJECT (тот же принцип, что unsupported_python_version —
    # см. core/python_version_detector.py — исключение категории сбоев,
    # которые портят воронку конверсии без реальной пользы).
    _LLM_INFRA_CATEGORIES = frozenset({
        "timeout", "transport_error", "empty", "bad_format", "parser_fail",
    })
    # Категории, где LLM ДЕЙСТВИТЕЛЬНО предложил что-то конкретное (валидный
    # EditSet с непустыми правками), но это не превратилось в применимую
    # правку — это легитимный REJECT (есть что оценивать и отклонять).
    _LLM_REJECT_CATEGORIES = frozenset({
        "anchor_empty", "anchor_mismatch", "diff_fail",
        # Focused-fix режим (2026-06-22): после фильтрации edits вне
        # target-окна ничего применимого к целевой строке не осталось.
        "focused_fix_rejected",
    })

    # 2026-07-04 (экзамен-проба pytest-homeassistant): категории, означающие
    # «structured-diff не смог заанкорить правку в файле». Их серия по одному
    # файлу → файл структурно неанкорим (см. fast-fail в execute).
    _ANCHOR_FAIL_CATEGORIES = frozenset({"anchor_empty", "anchor_mismatch"})
    MAX_ANCHOR_FAILS_PER_FILE = 3
    # 2026-07-08 (series5_loguru3): «мягкий» dir-карантин по ширине —
    # сколько РАЗНЫХ файлов каталога (с подкаталогами) должны иметь хотя бы
    # один форматный откат, чтобы каталог считался корпусом. Верхнеуровневые
    # каталоги — двойной порог (не глушить корневой пакет проекта).
    DIR_WIDESPREAD_ANCHORFAIL_FILES = 5

    @classmethod
    def _dirs_with_widespread_anchor_fails(cls, anchor_fail_counts: dict) -> set:
        """Каталоги, где >= DIR_WIDESPREAD_ANCHORFAIL_FILES разных файлов
        имеют форматные откаты (см. комментарий у гейта в execute). Файл
        засчитывается всем каталогам-предкам, кроме корня проекта."""
        by_dir: Dict[str, set] = {}
        for f, n in (anchor_fail_counts or {}).items():
            if not n:
                continue
            norm = str(f).replace("\\", "/")
            parent = PurePath(norm).parent
            while str(parent) not in ("", "."):
                by_dir.setdefault(str(parent).replace("\\", "/"), set()).add(norm)
                parent = parent.parent
        out = set()
        for d, files in by_dir.items():
            th = cls.DIR_WIDESPREAD_ANCHORFAIL_FILES * (1 if d.count("/") else 2)
            if len(files) >= th:
                out.add(d)
        return out

    def _dir_quarantine_gate(self, context: PipelineContext, error: Dict) -> Optional[PipelineContext]:
        """Карантин каталога (fixture-корпус). None = каталог чист, иначе —
        готовый NR-контекст.

        История (Delgan/loguru, 5 прогонов 07-08.07): tests/exceptions/source —
        99 НАМЕРЕННО сломанных файлов-фикстур, прогон сжигал ~1900s при
        cycles_run=0. Слои решения:
        - hard-источник: FileAntiLoop заблокировал >=3 файлов каталога
          (агрегация по всем предкам, верхний уровень ×2);
        - «мягкий» источник (series5_loguru3): >=5 РАЗНЫХ файлов каталога с
          хотя бы одним форматным откатом (_anchor_fail_counts кормится
          rollback-ветками Apply; ACCEPT сбрасывает файл — чинящийся каталог
          порог не копит) — паттерн размазанного корпуса, где per-file пороги
          не набираются никому;
        - гейт стоит в НАЧАЛЕ execute (series5_loguru5): ниже он обходился
          rule_based/memory-патчами, каждый из которых жёг полный цикл
          Apply→full-scan→rollback (~15s × 115 откатов ≈ весь бюджет).
        Синтаксические ошибки НЕ исключаются: карантин = «сломанные ДАННЫЕ,
        не чинить ничем» (осознанное отличие от per-file гейтов)."""
        file_raw = str(error.get("file") or "")
        if not file_raw:
            return None
        q_dirs = set(self.file_anti_loop.quarantined_dirs())
        q_dirs |= self._dirs_with_widespread_anchor_fails(
            context.metadata.get("_anchor_fail_counts") or {},
        )
        if not q_dirs:
            return None
        file_norm = file_raw.replace("\\", "/")
        err_dir = str(PurePath(file_norm).parent).replace("\\", "/")
        # сравнение по границе сегмента: "source_extra" при карантине
        # "source" НЕ матчится.
        q_hit = next(
            (q for q in q_dirs if err_dir == q or err_dir.startswith(q + "/")),
            None,
        )
        if q_hit is None:
            return None
        logger.warning(
            "  %s: каталог %s в карантине (fixture-корпус) — "
            "NR без генерации, включая синтаксические",
            file_raw, q_hit,
        )
        dq_meta = dict(context.metadata)
        dq_meta["_needs_review_pending_reason"] = "dir_quarantined_fixture_corpus"
        dq_skipped = dict(dq_meta.get("_dir_quarantine_skipped") or {})
        dq_skipped[q_hit] = dq_skipped.get(q_hit, 0) + 1
        dq_meta["_dir_quarantine_skipped"] = dq_skipped
        dq_proc = dict(context.processed_errors)
        dq_key = _process_key(error)
        if dq_proc.get(dq_key, 0) < 3:
            dq_proc[dq_key] = 3
        context = context.update(metadata=dq_meta, processed_errors=MappingProxyType(dq_proc))
        return context.add_state_to_history(State.NEEDS_REVIEW)

    def __init__(self, llm_client: "LLMClient", memory: MemoryLearning, patch_engine: PatchEngine,
                 invariant_guard=None, language_provider=None,
                 disaster_recovery: Optional["SyntaxDisasterRecovery"] = None,
                 syntax_orchestrator=None):
        self.llm_client = llm_client
        self.memory = memory
        self.patch_engine = patch_engine
        self.invariant_guard = invariant_guard
        self.language_provider = language_provider
        self.disaster_recovery = disaster_recovery
        self.healer = RustSyntaxHealer()
        self.segmenter = FileSegmenter()
        self._segments_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._cache_key = None
        self.segment_anti_loop = SegmentAntiLoop()
        self.file_anti_loop = FileAntiLoop()
        self.classifier = ErrorClassifier()
        self.prompt_builder = PromptBuilder()
        self.rule_based_fixer = RuleBasedFixer()

        # Инструменты‑советчики
        self.correctr = CorrectrAnalyzer()
        self.repomix = RepomixContextProvider()
        self.aider_map = AiderRepoMap()
        self.socratic = SocraticRefiner(llm_client=self.llm_client)

    # ----------------------------------------------------------------
    # Pre-apply валидация memory-патча (dry-run в памяти)
    # ----------------------------------------------------------------
    def _memory_patch_safe(self, patch: str, file_path: Path,
                           error_code: str = "", error_sig: str = "") -> bool:
        """Проверяет применимость memory-патча к ТЕКУЩЕМУ состоянию файла.

        Memory мог сохранить патч в одном состоянии файла, а сейчас файл
        отличается — apply бы прошёл «технически», но получил бы кашу,
        которую потом откатывали бы постфактум через гарды.

        Эта проверка делает dry-run В ПАМЯТИ (реальный файл не трогаем):
          1) патч парсится в hunks, не пуст;
          2) контекст-линии (' ') и удаляемые ('-') в каждом ханке должны
             БУКВАЛЬНО совпадать с реальным содержимым файла на позиции
             ханка — иначе патч из другого состояния файла;
          3) после dry-run apply баланс { } не должен ухудшаться по
             сравнению с оригиналом (та же логика, что в apply_patch_stage,
             но превентивно — не пускаем заведомо плохой патч).

        Возвращает True если безопасно. Любая ошибка → False (страховка).
        """
        try:
            from fixers.patch_lines import body_kind, strip_body_prefix, is_llm_placeholder
            if not patch or not file_path.exists():
                return False
            original = file_path.read_text(encoding="utf-8")
            hunks = self.patch_engine._parse_patch(patch, target_file=str(file_path))
            if not hunks:
                return False
            new_lines = original.splitlines(keepends=True)
            # ханки в порядке убывания — чтобы индексы не сдвигались
            for old_start, old_count, body_lines in sorted(hunks, key=lambda h: h[0], reverse=True):
                # 1) контекст-линии должны совпасть с файлом на старой позиции
                expected = []
                for line in body_lines:
                    k = body_kind(line)
                    if k in (' ', '-'):
                        expected.append(strip_body_prefix(line))
                segment = new_lines[old_start - 1: old_start - 1 + old_count]
                if len(segment) != len(expected):
                    return False
                for a, b in zip(segment, expected):
                    if a.rstrip("\r\n") != b.rstrip("\r\n"):
                        return False
                # 2) применяем в локальной копии
                cleaned: List[str] = []
                for line in body_lines:
                    k = body_kind(line)
                    if k is None:
                        continue
                    content = strip_body_prefix(line)
                    if k == '+':
                        if is_llm_placeholder(content):
                            continue
                        cleaned.append(content)
                    elif k == ' ':
                        cleaned.append(content)
                new_lines[old_start - 1: old_start - 1 + old_count] = cleaned

            # 3) баланс скобок: не делаем хуже, чем было
            new_text = "".join(new_lines)
            o_o, o_c = count_braces_safe(original)
            orig_balance = abs(o_o - o_c)
            n_o, n_c = count_braces_safe(new_text)
            new_balance = abs(n_o - n_c)
            if orig_balance == 0 and new_balance > 0:
                return False
            if orig_balance > 0 and new_balance > orig_balance:
                return False

            # 4) O.19-гейт реплея (2026-07-03, диагностика 88 REJECT): memory/
            # golden хранят патчи, записанные ДО появления guard-ов — в
            # архиве живут патчи с порчей данных (bcrypt: b"salt"→"salt",
            # приняты в июне, реплеились в каждом прогоне и глушили честный
            # rule_based vector→tuple фиксер, который в диспетчеризации
            # стоит ПОСЛЕ memory). Реплей обязан проходить те же
            # детерминированные проверки, что и свежая генерация. Отравленная
            # memory-запись при этом карантинится (удаляется конкретный
            # патч по hash) — иначе она реплеилась бы вечно.
            if error_code:
                from analysis.data_literal_guard import check_data_literal_mangling
                _mangling = check_data_literal_mangling(original, new_text, error_code)
                if not _mangling.get("ok", True):
                    logger.warning(
                        "  реплей-патч отравлен (O.19 %s, code=%s) — отбраковано, "
                        "fallback на свежую генерацию",
                        _mangling.get("markers"), error_code,
                    )
                    if error_sig and self.memory is not None and hasattr(self.memory, "forget"):
                        try:
                            self.memory.forget(error_sig, patch=patch)
                        except Exception as _fe:
                            logger.debug("  memory-карантин не удался: %s", _fe)
                    return False
            return True
        except Exception as e:
            logger.debug("  memory pre-apply check упал: %s", e)
            return False

    # ----------------------------------------------------------------
    # Приоритетный vector→tuple ремонт (до memory/golden)
    # ----------------------------------------------------------------
    def _try_vector_repair_preempt(
        self, context: PipelineContext, error: Dict[str, Any], error_sig: str,
    ) -> Optional[PipelineContext]:
        """Пробует детерминированный vector→tuple фиксер ДО memory/golden.

        Возвращает PipelineContext(→APPLYING_PATCH) если фиксер выдал
        непустой патч, иначе None (управление возвращается в обычную цепочку).
        Только для кодов _VECTOR_REPAIR_PREEMPT_CODES. Фиксер zero-collateral
        по построению (мультимножество литералов + fail-closed), поэтому его
        приоритет над memory/golden безопасен: если он не уверен — None.

        Anti-loop: если rule_based уже вызвал NET_DELTA-регрессию для этой же
        сигнатуры, не повторяем детерминированный патч (он идемпотентен) —
        уходим в обычную цепочку (IMP-W503, зеркало логики ниже по execute).
        """
        _prev_src = context.metadata.get(MetadataKeys.PATCH_SOURCE, "") or ""
        _nd_retries = context.metadata.get("_net_delta_error_retries") or {}
        if "rule_based" in _prev_src and _nd_retries.get(error_sig, 0) > 0:
            return None

        file_rel = error.get("file", "")
        if not file_rel:
            return None
        try:
            work_dir = context.working_path if hasattr(context, "working_path") else context.project_path
            file_path = work_dir / file_rel
            if not file_path.exists():
                return None
            file_content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.debug("  vector-preempt: чтение файла упало: %s", e)
            return None

        try:
            rb_edit_set = self.rule_based_fixer.try_fix(error, file_content, context.language)
        except Exception as e:
            logger.debug("  vector-preempt: try_fix упал: %s", e)
            return None
        if rb_edit_set is None:
            return None

        rb_diff = rb_edit_set.to_unified_diff({file_rel: file_content})
        if not rb_diff or PatchEngine.is_empty_patch(rb_diff):
            return None

        logger.info(
            "  RuleBasedFixer (vector→tuple, приоритет над memory/golden): intent=%r",
            rb_edit_set.intent,
        )
        new_metadata = dict(context.metadata)
        new_metadata[MetadataKeys.PATCH_SOURCE] = "rule_based"
        new_metadata["confidence"] = float(rb_edit_set.confidence)
        new_metadata["intent"] = rb_edit_set.intent
        new_metadata["structured_edit"] = rb_edit_set.to_dict()
        context = context.set_patch(rb_diff)
        context = context.update(metadata=new_metadata)
        context = context.record_processed_error(_process_key(error))
        return context.add_state_to_history(State.APPLYING_PATCH)

    # ----------------------------------------------------------------
    # execute (главный метод)
    # ----------------------------------------------------------------
    def execute(self, context: PipelineContext) -> PipelineContext:
        logger.info("Стадия GENERATE_PATCH: генерация патча...")
        error = context.selected_error
        if not error:
            logger.warning("  Нет выбранной ошибки – переход к FAILED")
            return context.add_state_to_history(State.FAILED)

        # 2026-06-24: раньше PROJECT_DEADLINE проверялся только МЕЖДУ
        # вызовами llm_client (см. _deadline_exceeded ниже, перед каждой
        # итерацией СВОИХ retry-петель этой стадии), но не ВНУТРИ одного
        # вызова llm_client._call_with_retries/_call_for_json_with_retries —
        # их собственная exponential-backoff петля (до max_retries попыток
        # по hard_limit секунд + 2/4/8с пауз) могла перерасходовать бюджет
        # на десятки-сотни секунд за один вызов. Выставляем дедлайн на
        # клиенте здесь — единственная точка входа для всех вызовов LLM из
        # этой стадии.
        if self.llm_client is not None:
            self.llm_client.project_deadline = context.metadata.get(MetadataKeys.PROJECT_DEADLINE)

        # Сброс кэша сегментов
        self._segments_cache.clear()
        self._cache_key = None

        if not error.get("error_class"):
            classification = self.classifier.classify(error)
            error["error_class"] = classification["class"]
            logger.debug("  Fallback-классификация: присвоен класс %s", error["error_class"])

        error_sig = self._error_signature(error)
        error_class = error.get("error_class", "")
        logger.info("  Целевая ошибка: %s:%s [%s] %s",
                    error.get("file", ""), error.get("line", 0),
                    error.get("code", ""), error.get("message", ""))

        if context.is_unfixable(error_sig):
            logger.info("  Сигнатура %s помечена как нерешаемая – пропускаем", error_sig)
            return context.add_state_to_history(State.NEXT_ERROR)

        # ---- Карантин каталога — В САМОМ НАЧАЛЕ стадии (2026-07-08) ----
        # series5_loguru5: гейт стоял ниже детерминированных путей, и
        # rule_based/memory-патчи по корпусным F841/E712 обходили карантин —
        # каждый жёг полный цикл Apply→full-scan→rollback (~15s × 115
        # откатов ≈ весь бюджет). Карантин означает «это сломанные ДАННЫЕ,
        # не чинить НИЧЕМ» — ни LLM, ни rule-based, ни синтаксис-ремонтом.
        _early_q = self._dir_quarantine_gate(context, error)
        if _early_q is not None:
            return _early_q

        if error.get("code") in _UNFIXABLE_MYPY_CODES:
            _ti_patch = self._make_type_ignore_patch(error, context)
            if _ti_patch:
                logger.info("  %s: добавляем # type: ignore[%s] к импорту", error.get("code"), error.get("code"))
                _ti_meta = dict(context.metadata)
                _ti_meta[MetadataKeys.PATCH_SOURCE] = "type_ignore"
                context = context.set_patch(_ti_patch)
                context = context.update(metadata=_ti_meta)
                return context.add_state_to_history(State.APPLYING_PATCH)
            logger.info("  Код %s не патчится детерминированно (stub-пакеты/декоратор) — пропускаем без LLM", error.get("code"))
            return context.add_state_to_history(State.NEXT_ERROR)

        # --- Специальные обработчики (security, build_script, manifest) ---
        # 2026-07-09 (Rust-серия-3, gpg-tui): диспетчер требовал
        # error_type=="security", но у RUSTSEC-ошибок от cargo audit
        # error_type теряется к моменту генерации (в записи решения None,
        # error_class=MANIFEST) → RUSTSEC падал в _handle_manifest (пустышка)
        # или LLM-путь с пустым патчем → вечный REJECT error_count_not_decreased
        # + прожиг медленного cargo audit каждый цикл. Роутим RUSTSEC в
        # security-хендлер ПО КОДУ, независимо от error_type, и ДО MANIFEST-
        # ветки. Так фикс снапшота/no-op/unfixable (см. _handle_security_error)
        # реально применяется.
        _code = str(error.get("code") or "")
        _is_rustsec = _code.startswith("RUSTSEC")
        if (error.get("file") == "Cargo.toml"
                and (error.get("error_type") == "security" or _is_rustsec)):
            return self._handle_security_error(context, error, error_sig)

        if error_class == "BUILD_SCRIPT":
            return self._handle_build_script(context, error, error_sig)

        if error_class == "MANIFEST":
            return self._handle_manifest(context, error, error_sig)

        # --- Приоритет детерминированного vector→tuple фиксера над memory/golden ---
        # (2026-07-03, bcrypt замер №7 — см. _VECTOR_REPAIR_PREEMPT_CODES.)
        if error_class != "CRITICAL_SYNTAX" and error.get("code") in _VECTOR_REPAIR_PREEMPT_CODES:
            _preempt = self._try_vector_repair_preempt(context, error, error_sig)
            if _preempt is not None:
                return _preempt

        # --- Проверка golden‑патча и памяти ---
        if error_class != "CRITICAL_SYNTAX":
            golden = context.get_golden_patch(error_sig)
            if golden:
                # 2026-07-03 (диагностика 88 REJECT): golden-реплей раньше шёл
                # ВООБЩЕ без pre-apply dry-run — тот же класс риска, что у
                # memory (реплей патча из другого состояния файла / патча,
                # записанного до появления guard-ов). Прогоняем через тот же
                # гейт, что и memory (контекст-линии + скобки + O.19).
                _g_work_dir = context.working_path if hasattr(context, "working_path") else context.project_path
                _g_fp = _g_work_dir / error.get("file", "") if error.get("file") else None
                if _g_fp is None or not self._memory_patch_safe(
                    golden["patch"], _g_fp,
                    error_code=error.get("code", ""), error_sig=error_sig,
                ):
                    logger.info(
                        "  golden-патч не прошёл pre-apply/O.19 гейт → fallback на свежую генерацию"
                    )
                    golden = None
            if golden:
                logger.info("  Используем эталонный патч из GoldenStore")
                new_metadata = dict(context.metadata)
                new_metadata[MetadataKeys.PATCH_SOURCE] = "golden"
                context = context.set_patch(golden["patch"])
                context = context.update(metadata=new_metadata)
                return context.add_state_to_history(State.APPLYING_PATCH)

            # language/file_ext-фильтр: не реплеить rust-патч в python-файл и т.п.;
            # legacy-записи без тэга пропускаются — это безопасный default,
            # избавляющий от накопленных в прошлых прогонах «безродных» патчей.
            _ext = ""
            try:
                _ext = Path(error.get("file", "")).suffix.lower()
            except Exception:
                _ext = ""
            known_patch = self.memory.get_known_fix(
                error_sig, language=context.language, file_ext=_ext or None,
            )
            if known_patch:
                # Pre-apply dry-run: memory мог сохранить патч в ДРУГОМ состоянии
                # файла; если контекст-линии не совпадают / баланс скобок ломается —
                # не пускаем такой патч в pipeline, а возвращаемся к свежей генерации
                # (это безопаснее, чем откатывать постфактум через гарды).
                work_dir = context.working_path if hasattr(context, "working_path") else context.project_path
                target_fp = work_dir / error.get("file", "") if error.get("file") else None
                if target_fp is None or not self._memory_patch_safe(
                    known_patch, target_fp,
                    error_code=error.get("code", ""), error_sig=error_sig,
                ):
                    logger.info(
                        "  memory-патч НЕ применим/отравлен → fallback на свежую генерацию"
                    )
                    known_patch = None
            if known_patch:
                logger.info("  Используем известный патч из памяти (language=%s, ext=%s)",
                            context.language, _ext or "?")
                new_metadata = dict(context.metadata)
                new_metadata[MetadataKeys.PATCH_SOURCE] = "memory"
                context = context.set_patch(known_patch)
                context = context.update(metadata=new_metadata)
                return context.add_state_to_history(State.APPLYING_PATCH)

        if not ErrorContextValidator.is_valid(error, context.project_path):
            logger.warning("  Контекст ошибки не соответствует файлу – пропускаем")
            context = context.record_processed_error(_process_key(error))
            context = context.add_rejected_patch({"error": error, "reason": "invalid_context"})
            context = context.add_unfixable_error(error)
            return context.add_state_to_history(State.NEXT_ERROR)

        self.segmenter.language = context.language

        attempt = context.metadata.get(MetadataKeys.PATCH_ATTEMPT, 0) + 1
        empty_retries = context.metadata.get(MetadataKeys.EMPTY_RETRIES, 0)
        new_metadata = dict(context.metadata)
        new_metadata[MetadataKeys.PATCH_ATTEMPT] = attempt
        context = context.update(metadata=new_metadata)

        failure_reason = context.metadata.get(MetadataKeys.LAST_PATCH_FAILURE, "") or ""
        compiler_feedback = context.metadata.get(MetadataKeys.COMPILER_FEEDBACK, "") or ""

        if "empty patch" in failure_reason.lower() or empty_retries > 0:
            failure_reason = f"Previous patch was empty or ineffective. {failure_reason}"

        try:
            file_path_str = error.get("file", "")
            if not isinstance(file_path_str, str):
                file_path_str = str(file_path_str)
            work_dir = context.working_path if hasattr(context, 'working_path') else context.project_path
            file_path = work_dir / file_path_str
            if not file_path.exists():
                logger.warning("  Файл %s не найден", file_path)
                return context.add_state_to_history(State.FAILED)

            file_content = file_path.read_text(encoding="utf-8", errors="ignore")
            total_lines = file_content.count('\n') + 1

            # --- 1. Быстрый фикс через Correctr (до LLM) ---
            correctr_patch = self._try_correctr_fix(file_path, error, context)
            if correctr_patch:
                logger.info("  Correctr предложил патч, переходим к применению")
                new_metadata = dict(context.metadata)
                new_metadata[MetadataKeys.PATCH_SOURCE] = "correctr"
                context = context.set_patch(correctr_patch)
                context = context.update(metadata=new_metadata)
                context = context.record_processed_error(_process_key(error))
                return context.add_state_to_history(State.APPLYING_PATCH)

            estimated_tokens = len(file_content) // 4
            use_segmentation = attempt >= self.MAX_ATTEMPTS_BEFORE_SEGMENTATION or total_lines > self.FULL_FILE_LINE_LIMIT
            if empty_retries >= 2 and not use_segmentation:
                use_segmentation = True
            if context.metadata.get("use_full_file"):
                use_segmentation = False

            segments = self._get_segments(file_path_str, file_content)
            file_hash = hash(file_content)

            # stale detection: сохраняем стабильный hash снапшота для ApplyPatchStage
            _snap_hash = hashlib.sha256(file_content.encode("utf-8", errors="ignore")).hexdigest()
            _snap_meta = dict(context.metadata)
            _snap_meta[MetadataKeys.FILE_CONTENT_HASH] = _snap_hash
            context = context.update(metadata=_snap_meta)

            # ---- Семантический ремонт (без LLM) ----
            if error_class != "CRITICAL_SYNTAX":
                semantic_patch = SemanticRepair.try_fix(error, file_path)
                if semantic_patch and not PatchEngine.is_empty_patch(semantic_patch):
                    logger.info("  SemanticRepair сгенерировал детерминированный патч")
                    new_metadata[MetadataKeys.PATCH_SOURCE] = "semantic_repair"
                    context = context.set_patch(semantic_patch)
                    context = context.update(metadata=new_metadata)
                    context = context.record_processed_error(_process_key(error))
                    return context.add_state_to_history(State.APPLYING_PATCH)

            # ---- Rule-based детерминированные правила (Stage E, без LLM) ----
            # Для простых CLEANUP/WARNING кодов (unused_import, eqeqeq, …) есть
            # точные правила. Они возвращают EditSet, мы собираем diff сами.
            # Любой неуверенный случай → None и управление уходит к LLM.
            if error_class != "CRITICAL_SYNTAX":
                # IMP-W503: пропускаем rule_based если он уже вызвал NET_DELTA
                # регрессию для этой же ошибки. Детерминированный патч каждый раз
                # даёт тот же результат — повтор бессмыслен, лучше → NEEDS_REVIEW.
                _prev_src = context.metadata.get(MetadataKeys.PATCH_SOURCE, "") or ""
                _nd_retries = context.metadata.get("_net_delta_error_retries") or {}
                _rule_based_nd_failed = "rule_based" in _prev_src and _nd_retries.get(error_sig, 0) > 0
                _rb_max_len = self._detect_project_line_length(work_dir)
                rb_edit_set = None if _rule_based_nd_failed else self.rule_based_fixer.try_fix(error, file_content, context.language, max_line_length=_rb_max_len)
                if rb_edit_set is not None:
                    rb_file = error.get("file", "")
                    rb_diff = (rb_edit_set.to_unified_diff({rb_file: file_content})
                               if rb_file else None)
                    if rb_diff and not PatchEngine.is_empty_patch(rb_diff):
                        logger.info("  RuleBasedFixer: детерминированный патч intent=%r",
                                    rb_edit_set.intent)
                        new_metadata[MetadataKeys.PATCH_SOURCE] = "rule_based"
                        new_metadata["confidence"] = float(rb_edit_set.confidence)
                        new_metadata["intent"] = rb_edit_set.intent
                        new_metadata["structured_edit"] = rb_edit_set.to_dict()
                        context = context.set_patch(rb_diff)
                        context = context.update(metadata=new_metadata)
                        context = context.record_processed_error(_process_key(error))
                        return context.add_state_to_history(State.APPLYING_PATCH)

            # ---- Семантический контекст + инструменты ----
            semantic_context = self._gather_context(file_content, error, work_dir)

            # ---- Критический синтаксис ----
            if error_class == "CRITICAL_SYNTAX":
                return self._handle_critical_syntax(
                    context, error, error_sig, file_content, file_path,
                    failure_reason, compiler_feedback, segments, file_hash,
                    estimated_tokens, semantic_context, use_segmentation,
                    new_metadata, attempt, empty_retries
                )

            # ---- ESCALATION MODE: повторная попытка для безнадёжной ошибки ----
            # Идёт ДО W503/W504 guard, чтобы в режиме эскалации LLM получила
            # полный файл и явный контракт вместо немедленного NR.
            if context.metadata.get("_escalation"):
                logger.info(
                    "  ESCALATION MODE: полный файл + расширенный контекст для %s:%s",
                    error.get('file'), error.get('line'),
                )
                self.file_anti_loop.reset_file(file_path_str)
                return self._single_llm_attempt(
                    context, error, error_sig, file_content,
                    failure_reason or "ESCALATION: all previous strategies failed for this error",
                    compiler_feedback, segments, semantic_context,
                    force_full_file=True,
                )

            # W503/W504: если rule-based не смог исправить (строка слишком длинная
            # или формат не распознан) — не идём к LLM. LLM стабильно даёт O.15
            # на многострочных выражениях и создаёт E501. Сразу → NEEDS_REVIEW.
            #
            # no_llm_codes (конфигурируемо, default F821): для этих кодов LLM
            # либо угадывает наугад (F821 — undefined name без контекста всего
            # проекта), либо уходит в петлю повторов. Rule-based уже попытался
            # выше (_py_f821_typo_fix и т.п.) — если не справился, сразу NR.
            _no_llm_codes = set(
                context.config.get("pipeline", {}).get("no_llm_codes", ["F821"])
            ) | {"W503", "W504"}
            if error.get("code") in _no_llm_codes:
                logger.info(
                    "  %s: rule-based fix невозможен → NR без LLM (no_llm_codes)",
                    error.get("code"),
                )
                _nlc_meta = dict(context.metadata)
                _nlc_meta["_needs_review_pending_reason"] = "no_llm_codes"
                # Конверсия-5 (2026-07-02): исход детерминированный — retry
                # той же ошибки даст ровно тот же NR (rule-based уже не смог,
                # LLM запрещён политикой). Раньше processed инкрементился по
                # +1 за проход, и одна и та же сигнатура прогонялась/NR-илась
                # до трёх раз (httpx: F821 encoding/elapsed — 7 NR-записей
                # на 2 root-cause). Ставим отсечку сразу.
                from types import MappingProxyType as _MPT
                _nlc_proc = dict(context.processed_errors)

                # Каскадное схлопывание F821-групп (2026-07-02, вариант A,
                # утверждённый дизайн). _py_f821_typo_fix — функция от
                # (file_content, undefined_name), НЕ от строки: если
                # rule-based не смог подобрать замену для ОДНОГО экземпляра
                # root-cause ошибки (тот же error_sig — файл+код+нормализо-
                # ванное сообщение), он гарантированно не сможет и для
                # остальных экземпляров той же root-cause на других строках —
                # это то же самое неопределённое имя. Раньше каждый sibling
                # доходил до этой ветки отдельным проходом пайплайна и
                # создавал СВОЙ NR-item на одну и ту же root-cause ошибку
                # (httpx: F821 encoding/elapsed — 7 NR-записей на 2
                # root-cause). Теперь при первом прохождении сразу баним ВСЕХ
                # siblings через _process_key (тот же ключ, что читает отбор
                # кандидатов в root_cause_stage.py:45-46) и передаём
                # NeedsReviewStage список их строк — одна root-cause ошибка
                # снова даёт ровно один decision и один NR-item.
                _collapse_codes = set(
                    context.config.get("pipeline", {}).get(
                        "cascade_collapse_codes", ["F821"]
                    )
                )
                if error.get("code") in _collapse_codes:
                    _cascade_siblings = [
                        e for e in context.current_errors
                        if self._error_signature(e) == error_sig
                    ]
                    _cascade_occurrences = sorted({
                        int(e.get("line") or 0) for e in _cascade_siblings
                    })
                    for _sibling in _cascade_siblings:
                        _nlc_proc[_process_key(_sibling)] = 3
                    _nlc_meta["_nr_group_occurrences"] = _cascade_occurrences
                    logger.info(
                        "  %s: каскадно забанено %d siblings той же root-cause"
                        " (occurrences=%s)",
                        error.get("code"), len(_cascade_siblings), _cascade_occurrences,
                    )
                else:
                    _nlc_key = _process_key(error)
                    if _nlc_proc.get(_nlc_key, 0) < 3:
                        _nlc_proc[_nlc_key] = 3

                context = context.update(metadata=_nlc_meta, processed_errors=_MPT(_nlc_proc))
                return context.add_state_to_history(State.NEEDS_REVIEW)

            # ---- Structured-unanchorable fast-fail (2026-07-04, экзамен-проба
            # pytest-homeassistant) ----
            # Дешёвые пути (rule_based/vector-preempt/memory/golden/manifest)
            # уже отработали ВЫШЕ и не подошли. Осталась дорогая LLM-генерация
            # (~35s/попытка). Если для ЭТОГО файла structured-diff уже
            # MAX_ANCHOR_FAILS_PER_FILE раз подряд вернул anchor_empty/
            # anchor_mismatch (см. установку счётчика ниже, после генерации) —
            # его формат структурно не анкорится (типичный кейс: semgrep-
            # находки в .github/workflows/*.yml, 26× «anchor не найден» →
            # fallback → CRITICAL_SYNTAX → откат, жёгший весь бюджет). Не
            # тратим LLM на новые ошибки того же файла — сразу NR. Счётчик
            # per-file и СБРАСЫВАЕТСЯ при первом успешном structured-патче
            # (см. ниже), поэтому анкоримые файлы (YAML чинится на 88% —
            # learning_cases) не блокируются: streak копит только реально
            # неанкоримый файл. Синтаксические ошибки самого файла (E999/
            # invalid-syntax) НЕ глушим — их надо чинить, чтобы файл парсился.
            _unanchorable = set(context.metadata.get("_structured_unanchorable_files") or [])
            _err_code = str(error.get("code") or "")
            _is_syntax = (
                error_class == "CRITICAL_SYNTAX"
                or _err_code in ("E999", "E902", "invalid-syntax")
            )
            if file_path_str in _unanchorable and not _is_syntax:
                logger.info(
                    "  %s: файл structured-unanchorable (%d anchor-fail) — "
                    "NR без дорогой LLM-генерации",
                    file_path_str,
                    context.metadata.get("_anchor_fail_counts", {}).get(file_path_str, 0),
                )
                _uf_meta = dict(context.metadata)
                _uf_meta["_needs_review_pending_reason"] = "structured_unanchorable_format"
                from types import MappingProxyType as _MPT2
                _uf_proc = dict(context.processed_errors)
                _uf_key = _process_key(error)
                if _uf_proc.get(_uf_key, 0) < 3:
                    _uf_proc[_uf_key] = 3
                context = context.update(metadata=_uf_meta, processed_errors=_MPT2(_uf_proc))
                return context.add_state_to_history(State.NEEDS_REVIEW)

            # ---- LLM-бюджет исчерпан для класса (file, code) (Perf-1,
            # 2026-07-07, дизайн deep-reasoner) ----
            # Дешёвые детерминированные пути (vector-preempt/golden/memory/
            # correctr/semantic_repair/rule_based/no_llm_codes) и structured-
            # unanchorable fast-fail уже отработали ВЫШЕ и не подошли —
            # осталась дорогая LLM-генерация. Если DecideStage
            # (_track_llm_noeffect) уже видела MAX_LLM_NOEFFECT_PER_CLASS
            # безрезультатных LLM-REJECT этого (file, code) — новая попытка
            # почти наверняка повторит тот же исход (httpx: стрик 5×
            # structured_llm REJECT по test_auth.py ~226с, _models.py дошёл
            # до 11/10 попыток). Не тратим LLM — сразу NR. Синтаксические
            # ошибки (E999/invalid-syntax) не глушим — их обязательно чинить,
            # чтобы файл вообще парсился (тот же принцип, что и в гейте выше).
            _llm_exhausted = set(context.metadata.get("_llm_budget_exhausted") or [])
            _noeff_file_norm = file_path_str.replace("\\", "/")
            _noeff_class_key = f"{_noeff_file_norm}::{_err_code}"
            if _noeff_class_key in _llm_exhausted and not _is_syntax:
                logger.info(
                    "  %s: LLM-бюджет исчерпан (%d безрезультатных REJECT) — "
                    "NR без дорогой LLM-генерации",
                    _noeff_class_key,
                    context.metadata.get("_llm_noeffect_counts", {}).get(_noeff_class_key, 0),
                )
                _lb_meta = dict(context.metadata)
                _lb_meta["_needs_review_pending_reason"] = "llm_budget_exhausted"
                from types import MappingProxyType as _MPT3
                _lb_proc = dict(context.processed_errors)
                _lb_key = _process_key(error)
                if _lb_proc.get(_lb_key, 0) < 3:
                    _lb_proc[_lb_key] = 3
                context = context.update(metadata=_lb_meta, processed_errors=_MPT3(_lb_proc))
                return context.add_state_to_history(State.NEEDS_REVIEW)

            # Карантин каталога проверяется в НАЧАЛЕ execute() (2026-07-08,
            # series5_loguru5: гейт ниже детерминированных путей обходился
            # rule_based/memory-патчами) — см. _dir_quarantine_gate.

            # ---- Антипетля файла ----
            if not self.file_anti_loop.record_attempt(
                file_path_str, file_hash, failure_reason=failure_reason,
            ):
                logger.error("  Файл %s заблокирован антипетлёй", file_path_str)
                context = self._mark_data_toxic_if_o19(context, file_path_str, failure_reason)
                context = context.record_processed_error(_process_key(error))
                context = context.add_unfixable_error(error)
                return context.add_state_to_history(State.NEXT_ERROR)

            # ---- BLOCKING стратегия ----
            if error_class == "BLOCKING":
                return self._blocking_adaptive_strategy_no_sandbox(
                    context, error, error_sig, file_content,
                    failure_reason, compiler_feedback, segments, file_hash, semantic_context
                )

            # ---- Обычная стратегия (LLM) ----
            if use_segmentation:
                return self._segmented_strategy(context, error, error_sig, file_content,
                                                failure_reason, compiler_feedback,
                                                segments, file_hash)
            else:
                return self._single_llm_attempt(context, error, error_sig, file_content,
                                                failure_reason, compiler_feedback, segments, semantic_context)

        except TimeoutError:
            logger.error("  Таймаут генерации LLM", exc_info=True)
            return context.add_state_to_history(State.FAILED)
        except Exception as e:
            logger.error(f"  Ошибка LLM: {e}", exc_info=True)
            return context.add_state_to_history(State.FAILED)

    @staticmethod
    def _mark_data_toxic_if_o19(
        context: PipelineContext, file_path_str: str, failure_reason: str,
    ) -> PipelineContext:
        """Q1 containment (2026-07-02, спека Алекса): файл, заблокированный
        FileAntiLoop из-за повторной порчи тестовых данных guard-ом O.19
        (metadata["data_literal_mangling"], см. decide_stage.py:700), больше
        не должен жечь циклы на СТИЛЕВЫХ ошибках того же файла — блок
        FileAntiLoop сбрасывается при смене hash файла, а O.19 воспроизводится
        детерминированно на malformed test-данных (pyca/bcrypt tests/test_bcrypt.py).
        Помечаем файл в metadata["_data_toxic_files"]; PrioritizeStage вычищает
        его нестроктурные ошибки из очереди до конца прогона (см. containment
        в prioritize_stage.py).
        """
        if "O.19 data mangling" not in (failure_reason or ""):
            return context
        new_metadata = dict(context.metadata)
        toxic = list(new_metadata.get("_data_toxic_files") or [])
        if file_path_str not in toxic:
            toxic.append(file_path_str)
            new_metadata["_data_toxic_files"] = toxic
            logger.warning(
                "  файл %s помечен data-toxic — его нестроктурные ошибки"
                " исключаются из очереди до конца прогона",
                file_path_str,
            )
            context = context.update(metadata=new_metadata)
        return context

    @staticmethod
    def _deadline_exceeded(context) -> bool:
        """project_timeout, прокинутый через context.metadata (см. PipelineEngine._global_fix_loop).

        Внешний цикл `_single_run` проверяет дедлайн только МЕЖДУ переходами
        состояний — если одна стадия (эта, GeneratePatchStage) сама крутит
        долгую retry-петлю (blocking adaptive / segmented strategy, много
        LLM-вызовов), внешняя проверка не успевает прервать её до завершения
        всей петли. control series 12 (2026-06-20): investdaytip застрял на
        html_export.py на ~40+ минут при бюджете 720s — внешний таймер ни
        разу не сработал, потому что весь блокирующий цикл шёл внутри ОДНОГО
        вызова execute(). Вызывать в начале каждой итерации retry-петель.
        """
        deadline = context.metadata.get(MetadataKeys.PROJECT_DEADLINE)
        if deadline is None:
            return False
        return time.monotonic() >= float(deadline)

    # -----------------------------------------------------------------
    # empty_response диагностика (2026-06-22)
    # -----------------------------------------------------------------
    @staticmethod
    def _save_llm_debug(
        context: PipelineContext, error: Dict[str, Any],
        category: str, stage: str, raw_response: Optional[str],
    ) -> None:
        """Сохраняет сырой ответ модели для отладки —
        `runtime/<project>/llm_debug/llm_debug.jsonl` (системная папка,
        не внутри ремонтируемого проекта — см. _project_needs_review_dir).
        Любой сбой записи — не критичен, не ломает основной поток."""
        try:
            import json as _json
            from datetime import datetime as _dt
            from core.pipeline_engine import PipelineEngine
            debug_dir = PipelineEngine._project_llm_debug_dir(context.project_path)
            debug_dir.mkdir(parents=True, exist_ok=True)
            rec = {
                "ts": _dt.utcnow().isoformat(timespec="seconds") + "Z",
                "file": error.get("file", ""),
                "line": error.get("line", 0),
                "code": error.get("code", ""),
                "category": category,
                "stage": stage,
                "raw_response": (raw_response or "")[:4000],
            }
            with open(debug_dir / "llm_debug.jsonl", "a", encoding="utf-8") as f:
                f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug("llm_debug log write failed: %s", e)

    @classmethod
    def _record_llm_failure(
        cls, context: PipelineContext, error: Dict[str, Any], category: str,
    ) -> PipelineContext:
        """Маршрутизирует категоризированный empty_response-сбой:
        _LLM_REJECT_CATEGORIES → настоящий REJECT (LLM предложил что-то
        конкретное, мы оценили и отклонили); _LLM_INFRA_CATEGORIES →
        отдельный счётчик llm_infra_failure_count/_items, НЕ rejected_patches
        — не портит decisions[]/конверсию (нет патча, который можно было бы
        оценить)."""
        if category in cls._LLM_REJECT_CATEGORIES:
            return context.add_rejected_patch(
                {"error": error, "reason": f"empty_response_{category}"}
            )
        new_meta = dict(context.metadata)
        items = list(new_meta.get("llm_infra_failure_items", []) or [])
        items.append({
            "file": error.get("file", ""),
            "line": error.get("line", 0),
            "code": error.get("code", ""),
            "category": category,
        })
        new_meta["llm_infra_failure_items"] = items
        new_meta["llm_infra_failure_count"] = int(
            new_meta.get("llm_infra_failure_count", 0) or 0
        ) + 1
        return context.update(metadata=new_meta)

    # -----------------------------------------------------------------
    # Быстрый Correctr
    # -----------------------------------------------------------------
    @staticmethod
    def _detect_project_line_length(work_dir: Path) -> int:
        """Читает line-length из конфига проекта (pyproject.toml / setup.cfg / .flake8)."""
        import re as _re
        has_ruff = False
        for cfg in ("pyproject.toml", "setup.cfg", ".flake8"):
            p = work_dir / cfg
            if not p.exists():
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
                m = _re.search(r"line[_-]length\s*=\s*(\d+)", text)
                if m:
                    return int(m.group(1))
                if "[tool.ruff" in text:
                    has_ruff = True
            except Exception:
                pass
        return 88 if has_ruff else 79

    def _try_correctr_fix(self, file_path: Path, error: Dict[str, Any],
                          context: PipelineContext) -> Optional[str]:
        """Пытается исправить ошибку через Correctr (без LLM)."""
        if not self.correctr.enabled:
            return None
        if not self.correctr.ensure_installed():
            self.correctr.enabled = False
            return None

        try:
            success = self.correctr.fix(file_path, auto_yes=True)
            if success:
                new_content = file_path.read_text(encoding="utf-8")
                original = context.working_path / error["file"]
                if original.exists():
                    original_content = original.read_text(encoding="utf-8")
                    patch = difflib.unified_diff(
                        original_content.splitlines(keepends=True),
                        new_content.splitlines(keepends=True),
                        fromfile=f"a/{error['file']}",
                        tofile=f"b/{error['file']}"
                    )
                    unified = ''.join(patch)
                    if unified and not PatchEngine.is_empty_patch(unified):
                        return normalize_patch(unified, error.get("file", "unknown"), original_content)
            return None
        except Exception as e:
            logger.warning(f"  Ошибка Correctr: {e}")
            return None

    # -----------------------------------------------------------------
    # Контекст с Repomix и Aider
    # -----------------------------------------------------------------
    def _gather_context(self, file_content: str, error: Dict[str, Any],
                        project_dir: Path) -> str:
        """Собирает расширенный контекст с помощью Repomix и Aider."""
        parts = []

        try:
            ctx_builder = SegmentContextBuilder(file_content)
            sem_ctx = ctx_builder.build_full_file_context(target_line=error.get("line", 0))
            if sem_ctx:
                parts.append(sem_ctx)
        except Exception as e:
            logger.debug("  Не удалось собрать семантический контекст: %s", e)

        repo_ctx = self.repomix.safe_run(project_path=project_dir)
        if repo_ctx:
            parts.append("\n\n--- REPOMIX FULL REPO CONTEXT ---\n" + repo_ctx[:4000])

        aider_map = self.aider_map.safe_run(project_path=project_dir)
        if aider_map:
            parts.append("\n\n--- AIDER REPO MAP ---\n" + aider_map[:3000])

        return "\n".join(parts)

    # -----------------------------------------------------------------
    # Structural Anchors – извлечение сигнатур для промпта
    # -----------------------------------------------------------------
    @staticmethod
    def _extract_structural_anchors(code: str) -> str:
        """Извлекает сигнатуры ключевых структурных элементов для промпта."""
        import re
        anchors = []
        # impl блоки
        for m in re.finditer(r'(?:pub\s+)?impl\s+(?:\w+::)?(\w+)(?:\s+for\s+\w+)?\s*\{', code):
            anchors.append(f"impl {m.group(1)}")
        # сигнатуры функций (только верхнеуровневые)
        for m in re.finditer(r'^\s*(pub\s+)?fn\s+(\w+)\s*\(([^)]*)\)', code, re.MULTILINE):
            name = m.group(2)
            params = m.group(3).strip()
            anchors.append(f"fn {name}({params})")
        # структуры
        for m in re.finditer(r'struct\s+(\w+)', code):
            anchors.append(f"struct {m.group(1)}")
        return "\n".join(f"- {a}" for a in sorted(anchors))

    # -----------------------------------------------------------------
    # Case-file для structured-fix (Stage C.5)
    # -----------------------------------------------------------------
    def _get_example_search(self, context, work_dir):
        """Ленивая инициализация ExampleSearchService (Stage J).

        Строится один раз из `context.config` и кэшируется на стейдже.
        Любая проблема (нет пакета, кривой конфиг) → None: каскад
        case-file просто обойдётся без секции EXTERNAL EXAMPLES.
        """
        if getattr(self, "_example_search", "unset") != "unset":
            return self._example_search
        service = None
        try:
            from analysis.external_examples import ExampleSearchService
            config = getattr(context, "config", {}) if context is not None else {}
            working_path = getattr(context, "working_path", None) if context is not None else None
            service = ExampleSearchService.from_config(config, working_path or work_dir)
        except Exception as e:
            logger.debug("  case-file: ExampleSearchService init упал: %s", e)
            service = None
        self._example_search = service
        return service

    def _get_symbol_index(self, work_dir, language):
        """Ленивый ProjectSymbolIndex (Stage I), закэшированный по work_dir.

        Индекс строится один раз на каталог за прогон (regex-скан всех
        исходников, с лимитами). Любая проблема → None: case-file просто
        обойдётся без секции PROJECT USAGE.
        """
        cache = getattr(self, "_symbol_index_cache", None)
        if cache is None:
            cache = {}
            self._symbol_index_cache = cache
        key = str(work_dir)
        if key in cache:
            return cache[key]
        index = None
        try:
            from analysis.project_symbol_index import ProjectSymbolIndex
            index = ProjectSymbolIndex.build(work_dir, language)
        except Exception as e:
            logger.debug("  case-file: ProjectSymbolIndex build упал: %s", e)
            index = None
        cache[key] = index
        return index

    def _get_macro_scanner(self, work_dir):
        """Ленивый MacroScanner (Stage N.5), закэшированный по work_dir.

        Сканер `#define`-макросов строится один раз на каталог за прогон
        (regex-скан всех .cpp/.h файлов, с лимитами). Любая проблема → None:
        case-file просто обойдётся без секции MACRO CONTEXT.
        """
        cache = getattr(self, "_macro_scanner_cache", None)
        if cache is None:
            cache = {}
            self._macro_scanner_cache = cache
        key = str(work_dir)
        if key in cache:
            return cache[key]
        scanner = None
        try:
            from analysis.cpp_macro_context import MacroScanner
            scanner = MacroScanner.build(work_dir)
        except Exception as e:
            logger.debug("  case-file: MacroScanner build упал: %s", e)
            scanner = None
        cache[key] = scanner
        return scanner

    @staticmethod
    def _lsp_enabled(context) -> bool:
        """Stage I.2 флаг: config["pipeline"]["use_lsp"] (default False)."""
        try:
            cfg = getattr(context, "config", {}) or {}
            return bool(cfg.get("pipeline", {}).get("use_lsp", False))
        except Exception:
            return False

    @staticmethod
    def _contract_hint_enabled(context, language: str) -> bool:
        """Q.4 флаг: contract hint для Python (default True)."""
        lang = (language or "").strip().lower()
        if lang not in ("python", "py"):
            return False
        try:
            cfg = getattr(context, "config", {}) or {}
            return bool(cfg.get("pipeline", {}).get("contract_hint", True))
        except Exception:
            return True

    def _get_lsp_resolver(self, context, work_dir, language):
        """Ленивый LspSymbolResolver (Stage I.2), кэш по (work_dir, language).

        Возвращает None, если флаг use_lsp выключен ИЛИ language-server не
        установлен — тогда case-file обходится только regex cross-file (I.1).
        Любая проблема изолирована: точный слой никогда не ломает пайплайн.
        """
        if not self._lsp_enabled(context):
            return None
        cache = getattr(self, "_lsp_resolver_cache", None)
        if cache is None:
            cache = {}
            self._lsp_resolver_cache = cache
        key = (str(work_dir), (language or "").lower())
        if key in cache:
            return cache[key]
        resolver = None
        try:
            from analysis.lsp_client import LspSymbolResolver
            resolver = LspSymbolResolver.for_language(language, work_dir)
        except Exception as e:
            logger.debug("  case-file: LspSymbolResolver init упал: %s", e)
            resolver = None
        cache[key] = resolver
        return resolver

    def _build_case_file_for_error(self, error: Dict[str, Any], error_sig: str,
                                   file_content: str, work_dir, language: str,
                                   context=None) -> str:
        """Собирает структурированное «досье» по ошибке для передачи в
        `LLMClient.generate_structured_fix(..., extra_hint=...)`.

        Любой из источников (SymbolGraph, Memory) может упасть — на это
        отвечаем `debug`-логом и пустым вкладом этого источника. Сам
        case-file продолжает собираться из того, что есть; если в итоге
        ВСЁ пусто — вернётся "" и вызывающий код не будет добавлять
        бесполезный заголовок.
        """
        # 1) related definitions — C.1
        related = ""
        try:
            sg = SymbolGraph(work_dir, language)
            related = sg.find_related_definitions(error, working_path=work_dir, max_symbols=5)
        except Exception as e:
            logger.debug("  case-file: symbol_graph упал: %s", e)

        # 2) constraints — C.2
        code = (error.get("code") or "").strip()
        do_list, dont_list = get_constraints(code)

        # 3) similar fixes — C.3 / B.5
        # language/file_ext-фильтр: few-shot прецеденты только из того же языка,
        # иначе LLM может перенять rust-стиль фикса в python-промпт и т.п.
        similar: List[Dict[str, Any]] = []
        if self.memory is not None:
            try:
                _ext = ""
                try:
                    _ext = Path(error.get("file", "")).suffix.lower()
                except Exception:
                    _ext = ""
                similar = self.memory.get_similar_fixes(
                    error_sig, n=2,
                    language=language, file_ext=_ext or None,
                ) or []
            except Exception as e:
                logger.debug("  case-file: memory.get_similar_fixes упал: %s", e)

        # 4) external examples — J.7 (rustc --explain / GitHub / SO).
        #    Любой провайдер может упасть/таймаутить — изолировано try/except,
        #    как symbol_graph/memory. Передаём dict'ы (не Example) — декаплинг.
        external: List[Dict[str, Any]] = []
        try:
            service = self._get_example_search(context, work_dir)
            if service is not None:
                external = [ex.to_dict() for ex in service.search(error, language)]
        except Exception as e:
            logger.debug("  case-file: example_search упал: %s", e)

        # 4b) cross-file context — I.3 (project-wide определения + использования).
        #     Дополняет одно-файловый related (C.1) проектным масштабом.
        cross_file = ""
        try:
            index = self._get_symbol_index(work_dir, language)
            if index is not None:
                symbols = SymbolGraph._extract_mentioned_symbols(error) or []
                blocks = []
                for sym in symbols[:5]:
                    ctx = index.cross_file_context(sym)
                    if ctx:
                        blocks.append(ctx)
                cross_file = "\n\n".join(blocks)
        except Exception as e:
            logger.debug("  case-file: cross-file index упал: %s", e)

        # 4c) LSP precision — I.2 (опциональный точный слой поверх regex 4b).
        #     Под флагом pipeline.use_lsp и при наличии language-server'а
        #     (rust-analyzer / pyright / tsserver). Точный def/refs по позиции
        #     ошибки префиксуем к regex-блоку. Нет сервера / любой сбой → пропуск.
        try:
            resolver = self._get_lsp_resolver(context, work_dir, language)
            if resolver is not None and resolver.available:
                lsp_block = resolver.cross_file_context_at(
                    error.get("file", ""),
                    int(error.get("line", 0) or 0),
                    int(error.get("column", 0) or 0),
                )
                if lsp_block:
                    cross_file = lsp_block + (("\n\n" + cross_file) if cross_file else "")
        except Exception as e:
            logger.debug("  case-file: LSP cross-file упал: %s", e)

        # 4d) MACRO CONTEXT — C/C++ препроцессор-context (мини).
        # Если ошибка на строке N упоминает макрос FOO (по соглашению — uppercase),
        # подкладываем определение FOO из проекта. Это закрывает «g++ ругается
        # на строку 14, но смысл ошибки в теле макроса». Не запускаем `g++ -E`,
        # только ловим `#define` сканером (regex-уровень). См. Stage N.5.
        macro_block = ""
        try:
            lang = (language or "").lower()
            if lang in ("cpp", "c++", "cxx", "c"):
                from analysis.cpp_macro_context import MacroScanner, render_macro_context
                scanner = self._get_macro_scanner(work_dir)
                if scanner is not None:
                    # извлекаем строку исходника, на которой произошла ошибка
                    err_line_text = ""
                    err_ln = int(error.get("line") or 0)
                    if file_content and err_ln > 0:
                        src_lines = file_content.splitlines()
                        if 0 < err_ln <= len(src_lines):
                            err_line_text = src_lines[err_ln - 1]
                    res = scanner.find_relevant_macros(
                        error_line_text=err_line_text,
                        error_message=error.get("message", "") or "",
                        limit=5,
                    )
                    macro_block = render_macro_context(res.matched)
        except Exception as e:
            logger.debug("  case-file: macro context упал: %s", e)

        # 5) сборка через CaseFileBuilder — C.4 + J.6 + I.3
        case_file = ""
        try:
            case_file = self.prompt_builder.build_case_file(
                error=error,
                file_content=file_content,
                related_defs=related,
                constraints=(do_list, dont_list),
                similar_fixes=similar,
                external_examples=external,
                cross_file=cross_file,
                security_example=get_security_example(code, language),
                fix_recipe=get_fix_recipe(code, language),
            )
        except Exception as e:
            logger.warning("  case-file: build_case_file упал: %s", e)
            return ""

        # Дописываем секцию MACRO CONTEXT в конец case-file (если есть что показать).
        # Делаем после build_case_file, чтобы не менять контракт PromptBuilder
        # (back-compat для других стадий, которые им пользуются).
        if case_file and macro_block:
            case_file = case_file + "\n\n" + macro_block

        # 4e) Q.4: contract hint — контракт функции, охватывающей строку ошибки.
        # Только Python; любой сбой изолирован и не ломает case-file.
        if case_file and self._contract_hint_enabled(context, language):
            try:
                from analysis.contract_hint import build_contract_hint
                contract_hint = build_contract_hint(file_content, error, language)
                if contract_hint:
                    case_file = case_file + "\n\n" + contract_hint
            except Exception as e:
                logger.debug("  case-file: contract_hint упал: %s", e)

        if case_file:
            # Лог достаточен для acceptance: «для E0609 в логе виден
            # enriched case-file prompt, не голый file_content».
            logger.info(
                "  enriched case-file prompt: code=%s, %d chars, sections=%d",
                code or "<none>", len(case_file), case_file.count("\n## "),
            )
        return case_file

    # -----------------------------------------------------------------
    # Одиночная LLM‑попытка с SocraticRefiner и structural anchors
    # -----------------------------------------------------------------
    def _collect_related_contents(self, error, work_dir, language, primary_rel):
        """Кросс-файловый контекст: помимо файла ошибки собираем содержимое
        связанных файлов — манифест зависимостей (Cargo.toml / package.json /
        pyproject) и файлы, где определены упомянутые в ошибке символы (индекс
        I.1). Это позволяет EditSet'у править, например, Cargo.toml для
        E0432/E0433 или соседний модуль, а не падать с «нет контента для ...».

        Возвращает {rel_path: text} БЕЗ primary_rel (его кладёт вызывающий —
        там augmented-контент). Всё в try/except и с лимитами — никогда не
        роняет генерацию патча.
        """
        from pathlib import Path as _P
        extra: Dict[str, str] = {}
        MAX_EXTRA = 4
        MAX_BYTES = 60_000
        try:
            root = _P(work_dir)
            prim_variants = {primary_rel, (primary_rel or "").replace("\\", "/"),
                             (primary_rel or "").replace("/", "\\")}

            def _try_add(rel: str) -> None:
                if not rel or len(extra) >= MAX_EXTRA:
                    return
                if rel in prim_variants or rel in extra:
                    return
                try:
                    p = root / rel
                    if p.is_file():
                        data = p.read_text(encoding="utf-8", errors="ignore")
                        if 0 < len(data) <= MAX_BYTES:
                            extra[rel] = data
                except Exception:
                    pass

            # 1) Манифест зависимостей — главный кросс-файловый адресат.
            manifests = {
                "rust": ["Cargo.toml"], "rs": ["Cargo.toml"],
                "javascript": ["package.json"], "js": ["package.json"],
                "typescript": ["package.json"], "ts": ["package.json"],
                "python": ["pyproject.toml", "requirements.txt", "setup.py"],
            }.get((language or "").lower(), [])
            for m in manifests:
                _try_add(m)

            # 2) Файлы, где определены упомянутые в ошибке символы (индекс I.1).
            try:
                index = self._get_symbol_index(work_dir, language)
                if index is not None:
                    for sym in (SymbolGraph._extract_mentioned_symbols(error) or [])[:5]:
                        for d in index.definitions(sym):
                            _try_add(getattr(d, "file", "") or "")
            except Exception as e:
                logger.debug("  cross-file: индекс определений упал: %s", e)
        except Exception as e:
            logger.debug("  cross-file: сбор контекста упал: %s", e)
        return extra

    # -----------------------------------------------------------------
    # Task Brief helpers
    # -----------------------------------------------------------------
    def _build_tool_contract(self, context) -> str:
        """Собирает контракт системы проверок из конфига — что проверяет, критерий принятия."""
        try:
            cfg = getattr(context, "config", {}).get("pipeline", {})
            checkers = []
            if cfg.get("use_ruff", False):
                checkers.append("ruff (PEP8 style, import order, complexity)")
            if cfg.get("use_mypy", False):
                checkers.append("mypy (static type checking)")
            if cfg.get("use_bandit", False):
                checkers.append("bandit (security vulnerabilities)")
            checker_str = ", ".join(checkers) if checkers else "linter"
            base = (
                f"Active checkers: {checker_str}.\n"
                "Acceptance rule: total error count must DECREASE after the patch is applied.\n"
                "Rejection rule: if modified lines introduce ANY new errors → patch is rolled back automatically.\n"
                "Do NOT 'fix' one error by creating another (e.g. splitting a line beyond column 79, "
                "removing needed imports, or breaking indentation)."
            )
            if context.metadata.get("_escalation"):
                base = (
                    "!!! ESCALATION MODE: all standard strategies have already failed for this error.\n"
                    "You are receiving the FULL FILE content. Previous attempts are listed in ATTEMPT HISTORY.\n"
                    "Try a completely different approach: consider restructuring the offending block,\n"
                    "renaming the symbol, or suppressing the error with an inline comment if truly unfixable.\n\n"
                ) + base
            return base
        except Exception:
            return ""

    def _build_dependency_context(self, error: Dict[str, Any], file_content: str,
                                  work_dir, language: str) -> str:
        """Ищет символы в зоне ошибки и где они используются в других файлах."""
        try:
            import re as _re
            error_line = int(error.get("line", 0))
            if error_line <= 0:
                return ""
            lines = file_content.splitlines()
            start = max(0, error_line - 9)
            end = min(len(lines), error_line + 8)
            zone = "\n".join(lines[start:end])

            # символы определённые в зоне ошибки
            symbols = []
            for m in _re.finditer(r'(?:def|class|fn|function)\s+(\w+)', zone):
                name = m.group(1)
                if name not in ("self", "cls") and name not in symbols:
                    symbols.append(name)

            if not symbols:
                return ""

            index = self._get_symbol_index(work_dir, language)
            if index is None:
                return ""

            parts = []
            for sym in symbols[:3]:
                refs = index.references(sym)
                if not refs:
                    continue
                # убираем ссылки из того же файла — они и так видны LLM
                file_str = error.get("file", "")
                ext_refs = [(f, ln) for f, ln in refs if f != file_str][:5]
                if ext_refs:
                    ref_list = ", ".join(f"{f}:{ln}" for f, ln in ext_refs)
                    parts.append(f"  {sym}() → used in: {ref_list}")

            if not parts:
                return ""
            return "Symbols defined near error line, referenced in other files:\n" + "\n".join(parts)
        except Exception as e:
            logger.debug("  dependency_context упал: %s", e)
            return ""

    def _single_llm_attempt(self, context, error, error_sig, file_content,
                            failure_reason, compiler_feedback, segments,
                            semantic_context: str = "", force_full_file: bool = False):
        all_file_errors = [e for e in context.current_errors if e.get("file") == error.get("file")]
        if all_file_errors:
            error_list = "\n".join(
                f"Line {fe.get('line')}: [{fe.get('code')}] {fe.get('message')}"
                for fe in all_file_errors[:50]
            )
            file_content += f"\n\n--- ALL ERRORS IN THIS FILE (fix all of them) ---\n{error_list}"

        file_content = self._inject_compiler_suggestions(file_content, error)
        if semantic_context:
            file_content += f"\n\n{semantic_context}"

        # Извлекаем структурные анкоры и добавляем в контекст
        anchors = self._extract_structural_anchors(file_content)

        work_dir = context.working_path if hasattr(context, 'working_path') else context.project_path

        # --- Task Brief: attempt history ---
        attempt_history = list(context.metadata.get(MetadataKeys.ATTEMPT_HISTORY, []) or [])
        last_intent = context.metadata.get(MetadataKeys.LAST_PATCH_INTENT, "") or ""
        if failure_reason and last_intent:
            attempt_num = context.metadata.get(MetadataKeys.PATCH_ATTEMPT, 1) - 1
            _attempt_data = context.metadata.get(MetadataKeys.LAST_ATTEMPT_DATA) or {}
            _entry: dict = {
                "attempt": attempt_num,
                "intent": last_intent[:120],
                "failure": failure_reason[:200],
                "result": _attempt_data.get("result", "REJECT"),
            }
            if _attempt_data.get("net_delta") is not None:
                _entry["net_delta"] = _attempt_data["net_delta"]
            if _attempt_data.get("new_errors"):
                _entry["new_errors"] = _attempt_data["new_errors"]
            attempt_history.append(_entry)

        # --- Task Brief: tool contract ---
        tool_contract = self._build_tool_contract(context)

        # --- Task Brief: dependency context ---
        dep_ctx = self._build_dependency_context(
            error, file_content, work_dir, context.language
        )

        # 2026-06-25: socratic.safe_run раньше вызывался БЕЗУСЛОВНО на КАЖДОЙ
        # попытке (включая самую первую) — это 3 доп. LLM-вызова
        # (explanation/hypothesis/verification) сверх основной генерации
        # патча, без конфиг-флага для отключения. По решению пользователя
        # (обсуждение архитектуры "правильный инструмент для правильной
        # задачи", 2026-06-25) сужаем до retry-попыток: на первой попытке
        # (failure_reason пуст) ошибка обычно шаблонная и не требует
        # доп. рассуждения — экономим 3x LLM-вызовов там, где это не нужно.
        socratic_prompt = (
            self.socratic.safe_run(
                error=error, context=file_content, language=context.language
            )
            if failure_reason else None
        )

        base_prompt = self.prompt_builder.build(
            error=error,
            context=file_content,
            language=context.language,
            failure_reason=failure_reason,
            attempt=context.metadata.get(MetadataKeys.PATCH_ATTEMPT, 1),
            compiler_feedback=compiler_feedback,
            structural_anchors=anchors,
            attempt_history=attempt_history if attempt_history else None,
            tool_contract=tool_contract,
            dependency_context=dep_ctx,
        )

        final_prompt = socratic_prompt if socratic_prompt else base_prompt

        file_path_str = error.get("file", "")
        file_path_obj = work_dir / file_path_str if file_path_str else None

        # === Case-file (Stage C.5): «вести LLM за ручку». ===
        # Дополнительно к base_prompt передаём LLM «досье» по ошибке:
        # related definitions (C.1), DO/DON'T constraints (C.2),
        # прецеденты из памяти (C.3/B.5), anchor hints. Каждый источник
        # обёрнут в try, чтобы любой фейл вырождал case-file, но НЕ ронял
        # structured-fix целиком.
        case_file = self._build_case_file_for_error(error, error_sig, file_content, work_dir,
                                                    context.language, context)
        structured_hint = (final_prompt + "\n\n" + case_file) if case_file else final_prompt

        # E501: добавляем явное ограничение длины строки в подсказку LLM.
        # Частая проблема: LLM дробит строку, но части всё равно > 79 символов.
        if error.get("code") == "E501":
            _e501_constraint = (
                "\n\nCRITICAL CONSTRAINT for E501: after your fix, EVERY new line "
                "must be ≤ 79 characters (PEP 8). Count characters carefully. "
                "If the original line contains a long string literal, use implicit "
                "string concatenation across multiple lines, each ≤ 79 chars. "
                "Do NOT simply split at column 79 — ensure the split is syntactically valid. "
                "IMPORTANT: place binary operators (and, or, +, |, etc.) at the END of the "
                "line before the newline, NOT at the start of the next line — a continuation "
                "line starting with a binary operator creates a W504 violation."
            )
            if failure_reason and "exceed 79" in failure_reason:
                _e501_constraint += (
                    f"\n\nPREVIOUS ATTEMPT FAILED: {failure_reason}. "
                    "Use shorter fragments this time."
                )
            # Focused-fix режим (2026-06-22): раньше здесь стояла ОБРАТНАЯ
            # инструкция — "fix ALL E501 violations in this file, partial fix
            # WILL BE REJECTED", со списком ВСЕХ нарушений в файле. Найдено
            # живьём (control series, geopython/pygeofilter): эта инструкция
            # провоцировала модель придумывать несуществующий "# noqa: E501"
            # в anchor.match для соседних строк, которые она "доисправляла"
            # по своей инициативе — единственная такая лишняя правка топила
            # ВЕСЬ файл через file-atomicity в to_unified_diff, теряя
            # ВАЛИДНЫЙ фикс целевой строки. DecideStage уже умеет принимать
            # частичный фикс при множественных вхождениях одного кода
            # (см. _cnt_before > 1 and _cnt_after < _cnt_before override) —
            # инструкция "фиксить всё" была избыточна и вредна. Теперь явно
            # просим модель ограничиться целевой строкой; GeneratePatchStage
            # ДОПОЛНИТЕЛЬНО гарантированно отбрасывает любые edits вне окна
            # (EditSet.filter_to_target_window) — этот промпт — best-effort,
            # не единственная защита.
            _e501_constraint += (
                f"\n\nIMPORTANT: fix ONLY line {error.get('line')} (the target error "
                "above). Do NOT modify, annotate, or add '# noqa' to any OTHER line in "
                "this file, even if it also exceeds 79 characters — other violations "
                "are handled separately. Edits outside the target line will be "
                "discarded automatically."
            )
            structured_hint = structured_hint + _e501_constraint

        # === Гибрид diff+JSON: сначала пробуем структурированный путь. ===
        # LLM возвращает EditSet, мы собираем diff сами. Все известные
        # классы багов «кривой LLM diff» (`++ b/file`, `// existing code`,
        # дубли блоков) физически невозможны на этом пути.
        patch = None
        structured_meta: Dict[str, Any] = {}
        # Кросс-файловый контекст: файл ошибки + связанные файлы (манифест,
        # модули с определениями упомянутых символов). Передаём один и тот же
        # набор и в генерацию, и в сборку диффа, чтобы правки к Cargo.toml/
        # соседним модулям проходили, а не падали с «нет контента».
        fc = {file_path_str: file_content} if file_path_str else {}
        fc.update(self._collect_related_contents(error, work_dir, context.language, file_path_str))

        # empty_response диагностика (2026-06-22, расследование REJECT-
        # аномалии для локальной LLM): раньше ЛЮБОЙ провал structured+legacy
        # путей одной строкой логировался как REJECT "empty_response",
        # смешивая транспортные/форматные сбои LLM (нет ответа вовсе) с
        # реальными случаями "LLM что-то предложил, но это не собралось в
        # применимую правку". См. _LLM_INFRA_CATEGORIES/_LLM_REJECT_CATEGORIES
        # ниже и _record_llm_failure.
        _llm_fail_category: Optional[str] = None
        _llm_fail_raw: Optional[str] = None
        _llm_fail_stage: str = ""

        try:
            edit_set = self.llm_client.generate_structured_fix(
                error=error,
                file_contents=fc,
                language=context.language,
                extra_hint=structured_hint,
            )
        except Exception as e:
            logger.warning("  structured_fix упал: %s — fallback к legacy generate_fix", e)
            edit_set = None
            self.llm_client.last_failure_category = "transport_error"
            self.llm_client.last_raw_response = None

        if edit_set is None:
            _llm_fail_category = self.llm_client.last_failure_category
            _llm_fail_raw = self.llm_client.last_raw_response
            _llm_fail_stage = "structured_call"

        if edit_set is not None and file_path_str:
            # Focused-fix режим (2026-06-22): отбрасываем edits вне окна
            # вокруг target_line ДО сборки диффа — гарантированная защита,
            # не зависящая от того, послушалась ли модель промпт-инструкции
            # выше. См. EditSet.filter_to_target_window docstring.
            _focus_radius = int(
                (context.config.get("pipeline", {}) or {}).get(
                    "focused_fix_window_radius", 3
                )
            )
            edit_set, _dropped_edits = edit_set.filter_to_target_window(
                target_file=file_path_str,
                target_line=int(error.get("line") or 0),
                error_code=error.get("code", ""),
                window_radius=_focus_radius,
            )
            if _dropped_edits:
                logger.info(
                    "  Focused-fix: отброшено %d edit(ов) вне target-окна "
                    "(target=%s:%s, radius=%d): %s",
                    len(_dropped_edits), file_path_str, error.get("line"),
                    _focus_radius, _dropped_edits,
                )

            if not edit_set.edits:
                # Все правки были вне окна (или все — noqa-галлюцинация на
                # соседних строках) — ничего реального для целевой строки не
                # осталось. Это содержательный сбой модели (не транспортный),
                # настоящий REJECT — см. _LLM_REJECT_CATEGORIES.
                _llm_fail_category = "focused_fix_rejected"
                _llm_fail_raw = self.llm_client.last_raw_response
                _llm_fail_stage = "structured_diff"
            else:
                diag: Dict[str, Any] = {}
                candidate = edit_set.to_unified_diff(fc, diag=diag)
                if candidate and not PatchEngine.is_empty_patch(candidate):
                    patch = candidate
                    structured_meta = {
                        "patch_source": "structured_llm",
                        "structured_edit": edit_set.to_dict(),
                        "confidence": float(edit_set.confidence),
                        "intent": edit_set.intent,
                        "risks": list(edit_set.risks),
                    }
                    if _dropped_edits:
                        structured_meta["focused_fix_dropped_edits"] = _dropped_edits
                    logger.info(
                        "  Structured-fix принят: intent=%r, edits=%d, confidence=%.2f",
                        edit_set.intent[:60], len(edit_set.edits), edit_set.confidence,
                    )
                    # 2026-07-04: успешный structured-патч → файл анкорится,
                    # сбрасываем anchor-fail streak (см. fast-fail в execute).
                    # Ключ к zero-collateral: анкоримый файл (YAML 88% ACCEPT)
                    # никогда не накопит порог, streak копит только реально
                    # неанкоримый.
                    _afc = context.metadata.get("_anchor_fail_counts") or {}
                    if file_path_str in _afc:
                        _reset_meta = dict(context.metadata)
                        _reset_afc = dict(_afc)
                        _reset_afc.pop(file_path_str, None)
                        _reset_meta["_anchor_fail_counts"] = _reset_afc
                        context = context.update(metadata=_reset_meta)
                else:
                    _llm_fail_category = diag.get("reason", "diff_fail")
                    _llm_fail_raw = self.llm_client.last_raw_response
                    _llm_fail_stage = "structured_diff"

        # Fallback на legacy generate_fix если структурный путь не дал результат.
        if patch is None:
            legacy_patch = self.llm_client.generate_fix(
                error=error,
                context=file_content,
                language=context.language,
                enriched_prompt=final_prompt,
                project_dir=work_dir,
                file_path_obj=file_path_obj
            )
            if legacy_patch and not PatchEngine.is_empty_patch(legacy_patch):
                patch = legacy_patch
                _llm_fail_category = None  # успех легаси перекрывает раннюю неудачу
            elif _llm_fail_category is None:
                # structured вообще не запускался (нет file_path_str) —
                # категория берётся из легаси-вызова.
                _llm_fail_category = self.llm_client.last_failure_category
                _llm_fail_raw = self.llm_client.last_raw_response
                _llm_fail_stage = "legacy_call"

        if not patch or PatchEngine.is_empty_patch(patch):
            _cat = _llm_fail_category or "empty"
            logger.warning("  LLM вернул пустой ответ (категория: %s, этап: %s)", _cat, _llm_fail_stage)
            self._save_llm_debug(context, error, _cat, _llm_fail_stage, _llm_fail_raw)
            context = context.record_processed_error(_process_key(error))
            context = self._record_llm_failure(context, error, _cat)
            context = context.add_unfixable_error(error)
            # 2026-07-04: копим anchor-fail per-file. Серия anchor_empty/
            # anchor_mismatch по одному файлу → он структурно неанкорим,
            # fast-fail в execute перестанет тратить дорогую LLM-генерацию на
            # его НОВЫЕ ошибки (см. _ANCHOR_FAIL_CATEGORIES / execute).
            if _cat in self._ANCHOR_FAIL_CATEGORIES:
                _err_file = error.get("file", "") or ""
                if _err_file:
                    _af_meta = dict(context.metadata)
                    _af_counts = dict(_af_meta.get("_anchor_fail_counts") or {})
                    _af_counts[_err_file] = _af_counts.get(_err_file, 0) + 1
                    _af_meta["_anchor_fail_counts"] = _af_counts
                    if _af_counts[_err_file] >= self.MAX_ANCHOR_FAILS_PER_FILE:
                        _unf = list(_af_meta.get("_structured_unanchorable_files") or [])
                        if _err_file not in _unf:
                            _unf.append(_err_file)
                            _af_meta["_structured_unanchorable_files"] = _unf
                            logger.warning(
                                "  файл %s помечен structured-unanchorable после "
                                "%d anchor-fail — его новые ошибки идут в NR без LLM",
                                _err_file, _af_counts[_err_file],
                            )
                    context = context.update(metadata=_af_meta)
            return context.add_state_to_history(State.NEXT_ERROR)

        # E501 pre-check: убеждаемся что ни одна добавленная строка не превышает лимит.
        # Это позволяет отловить "неправильное" дробление ДО применения патча и
        # запросить повтор с явным указанием нарушителей — без NET_DELTA отката.
        if error.get("code") == "E501" and patch:
            _max_len = 79
            _long_new_lines = []
            for _pl in patch.splitlines():
                if _pl.startswith("+") and not _pl.startswith("+++"):
                    _new_text = _pl[1:]  # убираем ведущий '+'
                    # Строки с # noqa: E501 намеренно длиннее — пропускаем
                    if "# noqa: E501" in _new_text or "# noqa:E501" in _new_text:
                        continue
                    if len(_new_text) > _max_len:
                        _long_new_lines.append((_new_text.rstrip(), len(_new_text)))
            if _long_new_lines:
                _violations_str = "; ".join(
                    f"{repr(t[:60])}... ({n} chars)" for t, n in _long_new_lines[:3]
                )
                logger.warning(
                    "  E501 pre-check: %d новых строк > %d символов — запрашиваем повтор",
                    len(_long_new_lines), _max_len,
                )
                empty_retries = context.metadata.get(MetadataKeys.EMPTY_RETRIES, 0) + 1
                new_metadata = dict(context.metadata)
                new_metadata[MetadataKeys.EMPTY_RETRIES] = empty_retries
                new_metadata[MetadataKeys.LAST_PATCH_FAILURE] = (
                    f"new lines exceed 79 chars: {_violations_str}"
                )
                context = context.update(metadata=new_metadata)
                if empty_retries >= self.MAX_EMPTY_RETRIES:
                    context = context.record_processed_error(_process_key(error))
                    context = context.add_unfixable_error(error)
                    return context.add_state_to_history(State.NEXT_ERROR)
                return context.add_state_to_history(State.GENERATING_PATCH)

        if not PatchEngine.is_patch_relevant(patch, error, file_content, segments):
            logger.warning("  Патч не затрагивает указанную ошибку – требуем повтор")
            empty_retries = context.metadata.get(MetadataKeys.EMPTY_RETRIES, 0) + 1
            new_metadata = dict(context.metadata)
            new_metadata[MetadataKeys.EMPTY_RETRIES] = empty_retries
            new_metadata[MetadataKeys.LAST_PATCH_FAILURE] = "Patch does not modify the error line or its block"
            context = context.update(metadata=new_metadata)
            if empty_retries >= self.MAX_EMPTY_RETRIES:
                context = context.record_processed_error(_process_key(error))
                context = context.add_unfixable_error(error)
                return context.add_state_to_history(State.NEXT_ERROR)
            return context.add_state_to_history(State.GENERATING_PATCH)

        logger.info("=== ПОЛУЧЕННЫЙ ПАТЧ ===\n%s", patch[:2000])
        new_metadata = dict(context.metadata)
        # PATCH_SOURCE может быть либо "llm" (legacy текстовый diff), либо
        # "structured_llm" (EditSet → собранный нами diff). Reviewer / Decide
        # затем читают confidence из metadata.
        if structured_meta:
            new_metadata[MetadataKeys.PATCH_SOURCE] = structured_meta["patch_source"]
            new_metadata["structured_edit"] = structured_meta["structured_edit"]
            new_metadata["confidence"] = structured_meta["confidence"]
            new_metadata["intent"] = structured_meta["intent"]
            new_metadata["risks"] = structured_meta["risks"]
            # Сохраняем intent для attempt history на случай rejection
            new_metadata[MetadataKeys.LAST_PATCH_INTENT] = structured_meta["intent"]
            if "focused_fix_dropped_edits" in structured_meta:
                new_metadata["focused_fix_dropped_edits"] = structured_meta["focused_fix_dropped_edits"]
        else:
            new_metadata[MetadataKeys.PATCH_SOURCE] = "llm"
            # legacy LLM diff: дефолтная средняя уверенность.
            new_metadata.setdefault("confidence", 0.6)
        # Сохраняем актуальную attempt_history чтобы retry её подхватил
        new_metadata[MetadataKeys.ATTEMPT_HISTORY] = attempt_history
        new_metadata.pop(MetadataKeys.LAST_PATCH_FAILURE, None)
        new_metadata.pop(MetadataKeys.EMPTY_RETRIES, None)
        context = context.set_patch(patch)
        context = context.update(metadata=new_metadata)
        return context.add_state_to_history(State.APPLYING_PATCH)

    # -----------------------------------------------------------------
    # Обработчик критического синтаксиса (с эвристикой E0765 и huniq)
    # -----------------------------------------------------------------
    def _handle_critical_syntax(self, context, error, error_sig, file_content, file_path,
                                failure_reason, compiler_feedback, segments, file_hash,
                                estimated_tokens, semantic_context, use_segmentation,
                                new_metadata, attempt, empty_retries):
        logger.info("  Обнаружена критическая синтаксическая ошибка, собираем подсказки")

        # --- Очистка дубликатов через huniq (стабильный внешний инструмент) ---
        try:
            from tools.deduplicator import HuniqDeduplicator
            dedup = HuniqDeduplicator()
            cleaned = dedup.safe_run(file_path=file_path)
            if cleaned and cleaned.strip() and cleaned != file_content:
                logger.info("  huniq удалил дубликаты строк – используем очищенный файл")
                file_content = cleaned
                file_hash = hash(file_content)
        except Exception as e:
            logger.debug("  Очистка дубликатов перед LLM не сработала: %s", e)

        # 0. Прямые подсказки компилятора (без LLM)
        rustc_suggestions = error.get("rustc_suggestions")
        if rustc_suggestions:
            for sugg in rustc_suggestions:
                replacement = sugg.get("replacement")
                if replacement is not None:
                    line = sugg.get("line", error.get("line"))
                    col = sugg.get("column", 0)
                    logger.info(f"  Применяю подсказку компилятора: вставить '{replacement}' в {line}:{col}")
                    try:
                        lines = file_content.splitlines(keepends=True)
                        if 1 <= line <= len(lines):
                            target_line = lines[line - 1]
                            if col <= len(target_line):
                                # Защита от слипания токенов: если вставка
                                # склеит идентификатор/оператор с соседним
                                # символом, отказываемся применять подсказку
                                # (иначе rustc-подсказки уродуют файл вида
                                # `match io::stdin()` -> `match iifo::stdin()`).
                                if not self._is_safe_insertion_point(target_line, col, replacement):
                                    logger.warning(
                                        "  Подсказка компилятора склеит токены в %d:%d "
                                        "(left=%r right=%r repl=%r) — пропускаем",
                                        line, col,
                                        target_line[col - 1:col] if col >= 1 else "",
                                        target_line[col:col + 1] if col < len(target_line) else "",
                                        replacement,
                                    )
                                    continue
                                new_line = target_line[:col] + replacement + target_line[col:]
                                lines[line - 1] = new_line
                                patched_content = "".join(lines)
                                patch = difflib.unified_diff(
                                    file_content.splitlines(keepends=True),
                                    patched_content.splitlines(keepends=True),
                                    fromfile=f"a/{error['file']}",
                                    tofile=f"b/{error['file']}"
                                )
                                patch_str = "".join(patch)
                                if patch_str and not PatchEngine.is_empty_patch(patch_str):
                                    new_metadata[MetadataKeys.PATCH_SOURCE] = "rustc_suggestion"
                                    context = context.set_patch(patch_str)
                                    context = context.update(metadata=new_metadata)
                                    context = context.record_processed_error(_process_key(error))
                                    return context.add_state_to_history(State.APPLYING_PATCH)
                    except Exception as e:
                        logger.warning(f"  Не удалось применить подсказку компилятора: {e}")

        # 0.5. Эвристика для E0765 — незакрытая кавычка (без LLM)
        if error.get("code") == "E0765" or "unterminated double quote" in error.get("message", "").lower():
            quote_suggestions = self.healer._unclosed_quote_suggestions(error, file_path)
            for sugg in quote_suggestions:
                patch = sugg.get("patch")
                if patch and not PatchEngine.is_empty_patch(patch):
                    logger.info("  Найдена строка с незакрытой кавычкой – применяем патч без LLM")
                    new_metadata[MetadataKeys.PATCH_SOURCE] = "quote_heuristic"
                    context = context.set_patch(patch)
                    context = context.update(metadata=new_metadata)
                    context = context.record_processed_error(_process_key(error))
                    return context.add_state_to_history(State.APPLYING_PATCH)

        # 1a. Языковой syntax-healer (Python/Go/Java/Kotlin/JS/Rust).
        #     Каждый ЯП поставляет тонкий детерминированный целитель типовых
        #     синтаксических ошибок (missing colon, отступ, незакрытая скобка).
        #     Возвращает unified diff — применяем БЕЗ LLM и без сети. Если
        #     ничего не вернул — идём дальше по каскаду.
        #
        #     Исключение: если в файле несколько "unterminated string literal" —
        #     healer починит только одно из них, остальные дадут target_still_present.
        #     Пропускаем healer и отдаём всё на откуп LLM (у него весь файл).
        _healer_msg_lower = (error.get("message") or "").lower()
        _skip_healer_multi_uts = False
        def _is_uts_message(m: str) -> bool:
            ml = (m or "").lower()
            return "unterminated string literal" in ml or "missing closing quote" in ml
        if _is_uts_message(_healer_msg_lower):
            _uts_same_file = sum(
                1 for e in context.current_errors
                if e.get("file") == error.get("file")
                and _is_uts_message(e.get("message", ""))
            )
            if _uts_same_file > 1:
                _skip_healer_multi_uts = True
                logger.info(
                    "  Healer пропущен: %d UTS-ошибок в файле — "
                    "отдаём multi-UTS healer или LLM", _uts_same_file
                )
        lang_healer = None
        try:
            if self.language_provider and hasattr(self.language_provider, "get_syntax_healer"):
                lang_healer = self.language_provider.get_syntax_healer()
        except Exception as _e:
            lang_healer = None
            logger.debug("get_syntax_healer недоступен: %s", _e)
        if lang_healer is not None and not _skip_healer_multi_uts:
            try:
                lang_patch = lang_healer.heal(
                    error, file_path,
                    llm_client=self.llm_client,
                    invariant_guard=self.invariant_guard,
                    segmenter=self.segmenter,
                )
                if isinstance(lang_patch, str) and lang_patch and lang_patch != "HEALED":
                    norm = normalize_patch(lang_patch, error.get("file", "unknown"),
                                           error, file_content)
                    if norm and not PatchEngine.is_empty_patch(norm):
                        logger.info("  Языковой syntax-healer нашёл патч — применяем без LLM")
                        new_metadata[MetadataKeys.PATCH_SOURCE] = "language_syntax_healer"
                        context = context.set_patch(norm)
                        context = context.update(metadata=new_metadata)
                        context = context.record_processed_error(_process_key(error))
                        return context.add_state_to_history(State.APPLYING_PATCH)
                if lang_patch == "HEALED":
                    # Healer уже изменил файл на диске — снимем как FULL_FILE_REPLACEMENT.
                    # C4 (аудит 2026-07-01): ОБЯЗАТЕЛЬНО возвращаем файл к
                    # pre-heal содержимому перед передачей в ApplyPatchStage —
                    # иначе его backup (read_text уже похиленного файла)
                    # становится «оригиналом» снапшота, original==patched, и
                    # symbol/erosion guard-ы вместе с rollback-ом для
                    # language_syntax_healer слепнут. Заодно соблюдаем контракт
                    # границ стадий: Generate только генерирует, пишет — Apply.
                    try:
                        new_content = file_path.read_text(encoding="utf-8")
                        if new_content and new_content != file_content:
                            file_path.write_text(file_content, encoding="utf-8")
                            new_metadata[MetadataKeys.FULL_FILE_REPLACEMENT] = new_content
                            new_metadata[MetadataKeys.PATCH_SOURCE] = "language_syntax_healer"
                            context = context.set_patch(None)
                            context = context.update(metadata=new_metadata)
                            context = context.record_processed_error(_process_key(error))
                            return context.add_state_to_history(State.APPLYING_PATCH)
                    except Exception as _e:
                        logger.debug("HEALED-снапшот не сделан: %s", _e)
            except Exception as _e:
                logger.debug("Языковой syntax-healer упал: %s", _e)

        # 1a-multi. Multi-UTS: если в файле несколько UTS и обычный healer пропущен,
        # пробуем heal_multi_uts — обрабатывает все bare-quote+unclosed пары за один проход.
        if lang_healer is not None and _skip_healer_multi_uts and hasattr(lang_healer, "heal_multi_uts"):
            _all_uts_errors = [
                e for e in context.current_errors
                if e.get("file") == error.get("file")
                and _is_uts_message(e.get("message", ""))
            ]
            try:
                lang_patch = lang_healer.heal_multi_uts(_all_uts_errors, file_path)
                if isinstance(lang_patch, str) and lang_patch:
                    norm = normalize_patch(
                        lang_patch, error.get("file", "unknown"), error, file_content
                    )
                    if norm and not PatchEngine.is_empty_patch(norm):
                        logger.info(
                            "  Multi-UTS healer нашёл патч (%d ошибок) — применяем без LLM",
                            len(_all_uts_errors),
                        )
                        new_metadata[MetadataKeys.PATCH_SOURCE] = "language_syntax_healer"
                        context = context.set_patch(norm)
                        context = context.update(metadata=new_metadata)
                        context = context.record_processed_error(_process_key(error))
                        return context.add_state_to_history(State.APPLYING_PATCH)
            except Exception as _e:
                logger.debug("heal_multi_uts упал: %s", _e)

        # 1b. Python AST-репэр — для развалившихся файлов на Python пытаемся
        #     каскадные структурные правки (удаление дублей, добавление `:`,
        #     отступы). Если получаем валидный `ast.parse` — применяем как
        #     FULL_FILE_REPLACEMENT без LLM. Только для Python.
        try:
            lang = (getattr(context, "language", "") or "").lower()
        except Exception:
            lang = ""
        if lang in ("python", "py"):
            try:
                from fixers.python_ast_repair import try_repair as _py_ast_repair
                repaired = _py_ast_repair(file_content)
                if repaired and repaired != file_content:
                    logger.info("  Python AST-репэр восстановил файл — полная замена")
                    new_metadata = dict(context.metadata)
                    new_metadata[MetadataKeys.FULL_FILE_REPLACEMENT] = repaired
                    new_metadata[MetadataKeys.PATCH_SOURCE] = "python_ast_repair"
                    context = context.set_patch(None)
                    context = context.update(metadata=new_metadata)
                    context = context.record_processed_error(_process_key(error))
                    return context.add_state_to_history(State.APPLYING_PATCH)
            except Exception as _e:
                logger.debug("Python AST-репэр пропущен: %s", _e)

        # 1. Эвристики (read‑only)
        healer_suggestions = self.healer.analyze_and_suggest(
            error, file_path,
            llm_client=self.llm_client,
            invariant_guard=self.invariant_guard,
            segmenter=self.segmenter
        )

        # 2. Сбалансированный код от Tree‑sitter — применяем сразу как замену файла
        if healer_suggestions:
            for sugg in healer_suggestions:
                balanced = sugg.get("balanced_content")
                if balanced:
                    logger.info("  Tree-sitter сгенерировал сбалансированный код – применяем полную замену файла")
                    new_metadata = dict(context.metadata)
                    new_metadata[MetadataKeys.FULL_FILE_REPLACEMENT] = balanced
                    new_metadata[MetadataKeys.PATCH_SOURCE] = "brace_heuristic"
                    context = context.set_patch(None)
                    context = context.update(metadata=new_metadata)
                    context = context.record_processed_error(_process_key(error))
                    return context.add_state_to_history(State.APPLYING_PATCH)

        # 2.5. Восстановление через Splice (быстрая замена DisasterRecovery)
        error_class = error.get("error_class", "")
        if error_class == "CRITICAL_SYNTAX":
            try:
                from tools.splice_recovery import SpliceRecovery
                splice = SpliceRecovery()
                recovered = splice.safe_run(file_path=file_path)
                if recovered:
                    logger.info("  Splice успешно восстановил файл до рабочего состояния")
                    new_metadata = dict(context.metadata)
                    new_metadata[MetadataKeys.FULL_FILE_REPLACEMENT] = recovered
                    new_metadata[MetadataKeys.PATCH_SOURCE] = "splice_recovery"
                    context = context.set_patch(None)
                    context = context.update(metadata=new_metadata)
                    context = context.record_processed_error(_process_key(error))
                    return context.add_state_to_history(State.APPLYING_PATCH)
            except Exception as e:
                logger.debug("  Splice не сработал: %s", e)

        # 2.6. E999 / invalid-syntax: если все автоматические методы не справились,
        #      LLM тоже не справится (видит фрагмент, root-cause в другом месте) —
        #      это даёт почти 100% REJECT и тратит время впустую.
        #      Collapse cascade: ruff reports N invalid-syntax per broken file.
        #      We emit only 1 NR per file (first occurrence); the rest are skipped
        #      silently via "_syntax_nr_files_done" metadata key.
        #
        #      Исключение: "unterminated string literal" — ошибка видна на той же
        #      строке, LLM с ней справляется (видит контекст до и после незакрытой
        #      кавычки и корректно закрывает строку с учётом продолжения).
        _SKIP_LLM_CODES = ("E999", "invalid-syntax", "E902")
        _msg_lower = (error.get("message") or "").lower()
        _is_unterminated_string = _is_uts_message(_msg_lower)
        # invalid-syntax caused by cascading E999 UTS → redirect to UTS mode
        if not _is_unterminated_string and error.get("code") == "invalid-syntax":
            _inv_syn_uts = [
                e for e in context.current_errors
                if e.get("file") == error.get("file")
                and _is_uts_message(e.get("message", ""))
            ]
            if _inv_syn_uts:
                # Before redirecting: if the file already parses OK, the E999 that
                # triggered these cascades was already fixed → skip the stale cascade.
                try:
                    import ast as _ast
                    _ast.parse(file_content)
                    logger.info(
                        "  invalid-syntax @ %s: файл валиден (E999 уже исправлен)"
                        " → пропускаем устаревший cascade",
                        error.get("file"),
                    )
                    context = context.record_processed_error(_process_key(error))
                    return context.add_state_to_history(State.NEXT_ERROR)
                except SyntaxError:
                    pass  # File still broken → redirect to UTS mode
                _is_unterminated_string = True
                logger.info(
                    "  invalid-syntax @ %s → UTS-режим (%d UTS в файле)",
                    error.get("file"), len(_inv_syn_uts),
                )
        if error_class == "CRITICAL_SYNTAX" and error.get("code", "") in _SKIP_LLM_CODES and not _is_unterminated_string:
            # Stale cascade check: if the file now parses OK, this error is a cascade
            # from an E999 that was already fixed — skip instead of sending to NR.
            try:
                import ast as _ast
                _ast.parse(file_content)
                logger.info(
                    "  CRITICAL_SYNTAX(%s) @ %s: файл валиден — "
                    "пропускаем устаревшую cascade-ошибку",
                    error.get("code"), error.get("file"),
                )
                context = context.record_processed_error(_process_key(error))
                return context.add_state_to_history(State.NEXT_ERROR)
            except SyntaxError:
                pass  # File still broken → proceed to NR
            file_key = error.get("file", "") or ""
            _NR_FILES_KEY = "_syntax_nr_files_done"
            nr_files_done: set = set(context.metadata.get(_NR_FILES_KEY) or [])
            if file_key and file_key in nr_files_done:
                # Cascading E999 for a file we already sent to NR — silently skip.
                logger.debug(
                    "  CRITICAL_SYNTAX(%s) cascade skip for %s (NR already written)",
                    error.get("code"), file_key,
                )
                context = context.record_processed_error(_process_key(error))
                return context.add_state_to_history(State.NEXT_ERROR)
            # E999 Semantic Recovery (опционально, опыт.: pipeline.
            # e999_semantic_recovery, default False) — 4-й уровень эскалации
            # ПЕРЕД сдачей в NEEDS_REVIEW. Реконструирует файл ЦЕЛИКОМ по его
            # логике (сохраняя имена/сигнатуры классов и функций), вместо
            # точечного патчинга. Результат — обычный full_file_replacement
            # патч с patch_source="syntax_reconstruction", который дальше
            # проходит ЧЕРЕЗ ОБЫЧНЫЙ пайплайн (ApplyPatchStage → ValidateStage
            # → ReviewStage с усиленной AST-диф + socraticode-проверкой →
            # DecideStage) — НЕ отдельная decision-система.
            if (context.config.get("pipeline", {}).get("e999_semantic_recovery", False)
                    and context.language == "python"):
                context, _sr_success = self._try_semantic_recovery(context, error, file_content)
                if _sr_success:
                    return context
                # success=False: ПРОДОЛЖАЕМ обычным путём ниже (dedup-пометка
                # файла + NEEDS_REVIEW) — НЕ возвращаем context напрямую,
                # иначе стейт-машина застрянет на месте без перехода (см.
                # docstring _try_semantic_recovery — было реальным багом).

            logger.info(
                "  CRITICAL_SYNTAX(%s): все эвристики не сработали — "
                "пропускаем LLM, отправляем на ручной просмотр",
                error.get("code"),
            )
            new_metadata = dict(context.metadata)
            new_metadata[MetadataKeys.PATCH_SOURCE] = "skipped_llm_critical_syntax"
            if file_key:
                new_metadata[_NR_FILES_KEY] = list(nr_files_done | {file_key})
                # IMP-F: bulk-skip ALL other pending errors in this broken file.
                # Saves LLM budget — LLM cannot fix anything while E999 persists.
                _e999_blocked: set = set(new_metadata.get("_e999_cascade_blocked_files") or [])
                _e999_blocked.add(file_key)
                new_metadata["_e999_cascade_blocked_files"] = list(_e999_blocked)
                _new_proc = dict(context.processed_errors)
                _blocked_sigs: list = []
                for _pending in context.current_errors:
                    if _pending.get("file", "") == file_key:
                        _psig = self._error_signature(_pending)
                        if _new_proc.get(_psig, 0) < 3:
                            _new_proc[_psig] = 3
                            _blocked_sigs.append(_psig)
                if _blocked_sigs:
                    logger.info(
                        "  IMP-F: заблокировано %d ошибок файла %s (E999 NR)",
                        len(_blocked_sigs), file_key,
                    )
                    context = context.update(processed_errors=MappingProxyType(_new_proc))
                    _e999_blocked_sigs: dict = dict(
                        new_metadata.get("_e999_cascade_blocked_sigs") or {}
                    )
                    _e999_blocked_sigs[file_key] = _blocked_sigs
                    new_metadata["_e999_cascade_blocked_sigs"] = _e999_blocked_sigs
            new_metadata["_needs_review_pending_reason"] = "e999_cascade_unrepaired"
            context = context.update(metadata=new_metadata)
            context = context.record_processed_error(_process_key(error))
            return context.add_state_to_history(State.NEEDS_REVIEW)

        # 3. Pre‑cleanup подсказки НЕ передаём для критических ошибок
        pre_cleanup = []

        # 4. Enrichment‑контекст (без pre_cleanup)
        enrichment = self._build_enrichment_context(file_content, error, healer_suggestions, pre_cleanup)

        # 5. Все ошибки файла для промпта
        all_file_errors = [e for e in context.current_errors if e.get("file") == error.get("file")]
        error_hints = "\n".join(
            f"Line {fe.get('line')}: [{fe.get('code')}] {fe.get('message')}"
            for fe in all_file_errors[:50]
        )

        problem_range = self._extract_error_range(error.get("message", ""))
        range_hint = ""
        if problem_range:
            range_hint = f"\n\nCOMPILER INDICATES PROBLEM BETWEEN LINES {problem_range[0]} AND {problem_range[1]}. Focus your fix on these lines."

        current_braces_open, current_braces_close = count_braces_safe(file_content)
        braces_info = (
            f"\n\nCURRENT BRACE BALANCE: {current_braces_open} opening '{{' and "
            f"{current_braces_close} closing '}}' in the file. "
            f"Your fix must result in balanced braces (open == close)."
        )

        # 6. Выбор контекста для LLM
        broken_file_mode = context.metadata.get("broken_file_mode", False)
        if broken_file_mode and attempt >= 2:
            context_for_llm = file_content
            logger.info("  CRITICAL_SYNTAX (повтор): даём полный файл для LLM")
        elif estimated_tokens <= self.MAX_CONTEXT_TOKENS and not broken_file_mode:
            context_for_llm = file_content
            logger.info("  CRITICAL_SYNTAX: файл умещается в токены, даём полный контент")
        else:
            start_line = max(1, error.get("line", 1))
            lines = file_content.splitlines(keepends=True)
            if start_line > len(lines):
                start_line = len(lines)
            block_start = start_line
            for i in range(min(start_line - 1, len(lines) - 1), -1, -1):
                stripped = lines[i].strip()
                if any(stripped.startswith(prefix) for prefix in ('fn ', 'pub fn ', 'impl ', 'pub impl ', 'struct ', 'pub struct ', 'trait ', 'mod ', 'pub trait ')):
                    block_start = i + 1
                    break
            balance = 0
            block_end = start_line
            for i in range(block_start - 1, len(lines)):
                balance += lines[i].count('{') - lines[i].count('}')
                if balance == 0 and i >= start_line - 1:
                    block_end = i + 1
                    break
            begin_idx = max(0, block_start - 1)
            end_idx = min(len(lines), block_end + 1)
            context_lines = lines[begin_idx:end_idx] if begin_idx < len(lines) else []
            context_for_llm = "".join(context_lines)
            logger.info(f"  CRITICAL_SYNTAX: расширенный логический блок, строки {block_start}-{block_end}")

        # 7. Добавляем подсказки компилятора и семантический контекст
        context_for_llm = self._inject_compiler_suggestions(context_for_llm, error)
        if semantic_context:
            context_for_llm += f"\n\n{semantic_context}"

        # 8. Собираем итоговый промпт
        if error_hints:
            context_for_llm += f"\n\n--- ALL ERRORS IN THIS FILE (fix all of them if possible) ---\n{error_hints}"
        context_for_llm += braces_info
        if range_hint:
            context_for_llm += range_hint
        if enrichment:
            context_for_llm += enrichment
        if _is_unterminated_string:
            _target_file_uts = error.get("file", "")
            _uts_in_file = [
                e for e in context.current_errors
                if e.get("file") == _target_file_uts
                and _is_uts_message(e.get("message", ""))
            ]
            _uts_lines_str = ", ".join(
                str(e.get("line", "?"))
                for e in sorted(_uts_in_file, key=lambda x: x.get("line", 0))
            )
            context_for_llm += (
                f"\n\nCRITICAL: This file has {len(_uts_in_file)} unterminated string "
                f"literal(s) at line(s): {_uts_lines_str}. "
                "Fix ALL of them in your patch — do not fix only the reported line. "
                "Each string must have matching open and close quotes on the SAME line "
                "or span correctly across lines. "
                "A patch that fixes only SOME unterminated strings WILL BE REJECTED."
            )
        else:
            context_for_llm += "\n\nCRITICAL: The file has unmatched braces or delimiters. Find and fix the structural issue. Preserve all existing logic and signatures."

        if not context_for_llm or len(context_for_llm.strip()) < 100:
            logger.warning("  Контекст для LLM слишком мал, использую полный файл")
            context_for_llm = file_content

        logger.info("=== PROMPT SENT TO LLM (CRITICAL_SYNTAX) ===\n%s\n=== END OF PROMPT ===", context_for_llm[:4000])

        # 9a. P.S.1: СНАЧАЛА пробуем structured EditSet — это закрывает
        # класс багов «LLM выдал полный новый файл как pure-insert» (O.16).
        # Только если structured path не дал результата — идём в raw-LLM
        # (legacy path) с прежней логикой.
        work_dir = context.working_path if hasattr(context, 'working_path') else context.project_path
        file_path_str = error.get("file") or ""
        fc = {file_path_str: file_content} if file_path_str else {}
        try:
            fc.update(self._collect_related_contents(error, work_dir, context.language, file_path_str))
        except Exception as e:
            logger.debug("  CRITICAL_SYNTAX: _collect_related_contents skipped: %s", e)
        structured_critical_meta = None
        structured_patch = None
        try:
            edit_set = self.llm_client.generate_structured_fix(
                error=error,
                file_contents=fc,
                language=context.language,
                extra_hint=context_for_llm,
            )
        except Exception as e:
            logger.warning("  CRITICAL_SYNTAX: structured_fix упал: %s — fallback к legacy", e)
            edit_set = None
        if edit_set is not None and file_path_str:
            try:
                candidate = edit_set.to_unified_diff(fc)
            except Exception as e:
                logger.debug("  CRITICAL_SYNTAX: to_unified_diff упал: %s", e)
                candidate = None
            if candidate and not PatchEngine.is_empty_patch(candidate):
                structured_patch = candidate
                structured_critical_meta = {
                    "patch_source": "structured_llm_critical",
                    "structured_edit": edit_set.to_dict(),
                    "confidence": float(edit_set.confidence),
                    "intent": edit_set.intent,
                    "risks": list(edit_set.risks),
                }
                logger.info(
                    "  CRITICAL_SYNTAX: structured EditSet принят (intent=%r, "
                    "edits=%d, conf=%.2f)",
                    edit_set.intent[:60], len(edit_set.edits), edit_set.confidence,
                )

        if structured_patch is not None:
            structured_patch = normalize_patch(
                structured_patch, error.get("file", "unknown"),
                original_content=file_content,
            )
            if structured_patch and not self._is_diff_too_large(structured_patch, file_content):
                for k, v in structured_critical_meta.items():
                    new_metadata[k] = v
                # Совместимость: даже structured-путь помечаем критичным,
                # чтобы downstream-логика (validate fast-path и др.) не ломалась.
                new_metadata[MetadataKeys.PATCH_SOURCE] = "structured_llm_critical"
                context = context.set_patch(structured_patch)
                context = context.update(metadata=new_metadata)
                context = context.record_processed_error(_process_key(error))
                return context.add_state_to_history(State.APPLYING_PATCH)
            else:
                logger.info("  CRITICAL_SYNTAX structured-патч отфильтрован — fallback к legacy")

        # 9b. Legacy raw-LLM путь (когда structured не сработал).
        llm_patch = self.llm_client.generate_fix(
            error=error,
            context=context_for_llm,
            language=context.language,
            failure_reason=failure_reason,
            attempt=context.metadata.get(MetadataKeys.PATCH_ATTEMPT, 1),
            compiler_feedback=compiler_feedback,
            project_dir=work_dir,
            file_path_obj=file_path
        )

        if llm_patch and not PatchEngine.is_empty_patch(llm_patch):
            llm_patch = normalize_patch(
                llm_patch,
                error.get("file", "unknown"),
                original_content=file_content
            )
            if llm_patch and not self._is_diff_too_large(llm_patch, file_content):
                logger.info("  LLM сгенерировал патч для CRITICAL_SYNTAX (legacy raw), применяем")
                new_metadata[MetadataKeys.PATCH_SOURCE] = "llm_critical"
                context = context.set_patch(llm_patch)
                context = context.update(metadata=new_metadata)
                context = context.record_processed_error(_process_key(error))
                return context.add_state_to_history(State.APPLYING_PATCH)
            else:
                logger.info("  Патч от LLM отфильтрован как деструктивный или слишком большой")

        # 10. Запасные стратегии
        if use_segmentation:
            return self._segmented_strategy(context, error, error_sig, file_content,
                                            failure_reason, compiler_feedback,
                                            segments, file_hash)
        else:
            return self._single_llm_attempt(context, error, error_sig, file_content,
                                            failure_reason, compiler_feedback, segments)

    # -----------------------------------------------------------------
    # Специальные обработчики
    # -----------------------------------------------------------------
    # Rust-этап (2026-07-09, gpg-tui solo): сколько раз пробуем один RUSTSEC-код
    # прежде чем признать нерешаемым. Нужен, т.к. `cargo update` может СДВИНУТЬ
    # Cargo.lock (не no-op → «успех»), но конкретный advisory не уйти
    # (транзитив, залоченный родителем): тогда VALIDATING→REJECT→повторный
    # выбор следующего цикла давал 6× REJECT на один код. Ограничиваем.
    MAX_RUSTSEC_ATTEMPTS = 2

    def _handle_security_error(self, context, error, error_sig):
        logger.info("  Обнаружена уязвимость в Cargo.toml, попытка cargo update")
        # Кап попыток на КОД advisory: cargo update, сдвинувший lock, но не
        # закрывший advisory, иначе крутится бесконечно (VALIDATING→REJECT→
        # re-select). После MAX_RUSTSEC_ATTEMPTS — нерешаемо, без новой мутации.
        _code = str(error.get("code") or "")
        _rs_key = f"_rustsec_attempts_{_code}"
        _rs_n = int(context.metadata.get(_rs_key, 0))
        if _code.startswith("RUSTSEC") and _rs_n >= self.MAX_RUSTSEC_ATTEMPTS:
            logger.warning(
                "  %s: %d попыток cargo update не закрыли advisory (вероятно "
                "транзитив, требующий бампа констрейнта родителя) — нерешаемо",
                _code, _rs_n,
            )
            context = context.record_processed_error(_process_key(error))
            context = context.add_unfixable_error(error)
            return context.add_state_to_history(State.NEXT_ERROR)
        if _code.startswith("RUSTSEC"):
            _rs_meta = dict(context.metadata)
            _rs_meta[_rs_key] = _rs_n + 1
            context = context.update(metadata=_rs_meta)
        pkg_name = self._extract_package_from_security_error(error)
        if pkg_name:
            work_dir = context.working_path if hasattr(context, 'working_path') else context.project_path

            # Rust-этап (2026-07-09, orhun/gpg-tui): раньше этот путь мутировал
            # Cargo.lock/Cargo.toml `cargo update`-ом МИМО ApplyPatchStage и
            # НЕ писал _pre_patch_content — ValidateStage ставил snapshot_failed,
            # DecideStage не мог откатить («откат НЕВОЗМОЖЕН»). Снимаем снапшот
            # обоих манифест-файлов ДО мутации (единый источник истины
            # _pre_patch_content, §3 fail-closed), по образцу _manifest_snapshot.
            _pre_patch: Dict[str, str] = {}
            _lock_before = None
            for _rel in ("Cargo.toml", "Cargo.lock"):
                _fp = work_dir / _rel
                if _fp.exists():
                    try:
                        _txt = _fp.read_text(encoding="utf-8", errors="replace")
                        _pre_patch[_rel] = _txt
                        if _rel == "Cargo.lock":
                            _lock_before = _txt
                    except Exception as _e:
                        logger.debug("  security snapshot %s: %s", _rel, _e)

            def _lock_changed() -> bool:
                """no-op детект: Cargo.lock реально изменился после команды."""
                _lf = work_dir / "Cargo.lock"
                if not _lf.exists() or _lock_before is None:
                    return True  # нет базы для сравнения — не блокируем
                try:
                    return _lf.read_text(encoding="utf-8", errors="replace") != _lock_before
                except Exception:
                    return True

            # 2026-07-09 (конверсия RUSTSEC): cargo audit кладёт fix_version
            # (исправленная версия из advisory.versions.patched). Обычный
            # `cargo update -p pkg` двигает транзитив ТОЛЬКО в пределах
            # констрейнта родителя — для транзитивов это часто no-op.
            # `--precise <fix_version>` форсит конкретную исправленную версию,
            # если граф зависимостей её допускает — это и чинит транзитивы,
            # которые прежде «безопасно откладывались». Валидацию делает сам
            # пайплайн (VALIDATING пере-запускает cargo audit); нам нужно лишь
            # сделать фикс ЭФФЕКТИВНЫМ. Fail-closed цел: снапшот снят выше,
            # неэффективный фикс → REJECT/rollback.
            _fix_version = str(error.get("fix_version") or "").strip() or None

            cargo_update_success = False
            # 1) точечный бамп до исправленной версии (лучший путь для транзитива)
            if _fix_version:
                try:
                    _pr = subprocess.run(
                        ["cargo", "update", "-p", pkg_name, "--precise", _fix_version],
                        cwd=work_dir, capture_output=True, text=True, timeout=120,
                    )
                    if _pr.returncode == 0 and _lock_changed():
                        logger.info("  cargo update -p %s --precise %s — уязвимость закрыта точечно",
                                    pkg_name, _fix_version)
                        cargo_update_success = True
                    else:
                        logger.info("  --precise %s не прошёл/no-op (%s) — пробуем обычный update",
                                    _fix_version, _pr.stderr.strip()[:120])
                except Exception as e:
                    logger.warning("  Ошибка cargo update --precise: %s", e)

            # 2) обычный cargo update (в пределах констрейнта)
            if not cargo_update_success:
                try:
                    result = subprocess.run(
                        ["cargo", "update", "-p", pkg_name],
                        cwd=work_dir, capture_output=True, text=True, timeout=120,
                    )
                    if result.returncode == 0:
                        if _lock_changed():
                            logger.info("  cargo update успешно обновил %s", pkg_name)
                            cargo_update_success = True
                        else:
                            logger.info(
                                "  cargo update -p %s — no-op (транзитив залочен "
                                "констрейнтом) — не считаем успехом", pkg_name,
                            )
                    else:
                        logger.warning("  cargo update не удался: %s", result.stderr.strip()[:200])
                except Exception as e:
                    logger.warning("  Ошибка cargo update: %s", e)

            # 3) прямой бамп зависимости в Cargo.toml (форсим версию в дерево,
            #    когда транзитив нельзя поднять только через lock). Если пакета
            #    нет в [dependencies] — upsert добавит прямую зависимость на
            #    fix_version (стандартная ремедиация транзитивного advisory).
            #    Пайплайн затем сам проверит cargo audit-ом.
            if not cargo_update_success:
                file_path = work_dir / "Cargo.toml"
                current_ver = self._get_current_version_from_toml(context, pkg_name)
                _target = _fix_version
                if not _target and current_ver:
                    _target = DependencySearch.search_safe_version(
                        pkg_name, current_ver, major_lock=True)
                if _target and _target != current_ver:
                    ok = False
                    if current_ver:
                        ok = update_dependency_version(file_path, pkg_name, _target)
                    if not ok:
                        # пакета нет прямой зависимостью — добавляем (транзитив)
                        ok = upsert_dependency(file_path, pkg_name, _target)
                    if ok:
                        logger.info("  Cargo.toml: %s -> %s (прямой бамп для закрытия advisory)",
                                    pkg_name, _target)
                        cargo_update_success = True

            # 2026-07-09 (durability, gpg-tui): lock-only фикс (--precise/обычный
            # update) НЕ переживает обработку соседних advisory — их cargo update
            # пере-резолвит Cargo.lock и затирает точечный пин (RUSTSEC-2026-0195
            # чинился на этапе решения, но не доживал до доставки). Закрепляем
            # исправленную версию ЯВНО в Cargo.toml (upsert), чтобы последующие
            # cargo update не откатили её — тогда фикс durable и доставляется.
            if cargo_update_success and _fix_version:
                _ctoml = work_dir / "Cargo.toml"
                if _ctoml.exists() and _fix_version not in _pre_patch.get("Cargo.toml", ""):
                    try:
                        if not update_dependency_version(_ctoml, pkg_name, _fix_version):
                            upsert_dependency(_ctoml, pkg_name, _fix_version)
                        logger.info("  Cargo.toml: %s закреплён на %s (durability-пин)",
                                    pkg_name, _fix_version)
                    except Exception as _e:
                        logger.debug("  durability-пин %s не удался: %s", pkg_name, _e)

            if cargo_update_success:
                logger.info("  Уязвимость исправлена, удаляем ошибку из списка текущих")
                # Пишем снапшот в metadata ДО перехода в VALIDATING — теперь
                # DecideStage сможет откатить, если аудит всё же зафлагает.
                if _pre_patch:
                    _sec_meta = dict(context.metadata)
                    _existing = dict(_sec_meta.get("_pre_patch_content") or {})
                    _existing.update(_pre_patch)
                    _sec_meta["_pre_patch_content"] = _existing
                    context = context.update(metadata=_sec_meta)
                new_errors = [
                    e for e in context.current_errors
                    if self._error_signature(e) != error_sig
                ]
                context = context.set_errors(list(new_errors))
                context = context.set_patch(None)
                context = context.record_processed_error(_process_key(error))
                return context.add_state_to_history(State.VALIDATING)

        # 2026-07-09: no-op/неразрешимый RUSTSEC (транзитив, требующий бампа
        # констрейнта родителя) — помечаем нерешаемым, чтобы не пере-выбирать
        # каждый макро-цикл и не жечь медленный ре-скан cargo. Направление
        # ошибки безопасное: недоставленный security-фикс уходит в очередь
        # ручного разбора, а не молча REJECT-петляет (§4).
        logger.warning("  Уязвимость не исправлена автоматически (вероятно транзитив) — помечаем нерешаемой")
        context = context.record_processed_error(_process_key(error))
        context = context.add_unfixable_error(error)
        return context.add_state_to_history(State.NEXT_ERROR)

    def _handle_build_script(self, context, error, error_sig):
        logger.info("  Файл build.rs – мета-логика, применяем только безопасные эвристики")
        work_dir = context.working_path if hasattr(context, 'working_path') else context.project_path
        file_path = work_dir / error.get("file", "build.rs")
        if file_path.exists():
            semantic_patch = SemanticRepair.try_fix(error, file_path)
            if semantic_patch and not PatchEngine.is_empty_patch(semantic_patch):
                logger.info("  SemanticRepair сгенерировал патч для build.rs")
                new_metadata = dict(context.metadata)
                new_metadata[MetadataKeys.PATCH_SOURCE] = "semantic_repair"
                context = context.set_patch(semantic_patch)
                context = context.update(metadata=new_metadata)
                context = context.record_processed_error(_process_key(error))
                return context.add_state_to_history(State.APPLYING_PATCH)

            suggestions = self.healer.analyze_and_suggest(error, file_path)
            if suggestions:
                new_metadata = dict(context.metadata)
                new_metadata["healer_suggestions"] = suggestions
                context = context.update(metadata=new_metadata)
        logger.info("  Безопасные эвристики не помогли, пропускаем build.rs")
        context = context.record_processed_error(_process_key(error))
        return context.add_state_to_history(State.NEXT_ERROR)

    def _handle_manifest(self, context, error, error_sig):
        logger.info("  Файл Cargo.toml – манифест, специальная обработка уже выполнена")
        context = context.record_processed_error(_process_key(error))
        return context.add_state_to_history(State.NEXT_ERROR)

    def _try_semantic_recovery(
        self, context: PipelineContext, error: Dict[str, Any], file_content: str,
    ) -> Tuple[PipelineContext, bool]:
        """E999 Semantic Recovery (опционально). Возвращает (context, success).

        ВАЖНО (найдено живой проверкой на реальном кейсе,
        lucaswerkmeister/tool-lexeme-forms, 2026-06-21): успех/неуспех
        ОБЯЗАН возвращаться явным булевым флагом, а не "не-None контекст =
        успех" — на провале реконструкции возврат context без перехода
        состояния (без add_state_to_history) заставлял execute() вернуть
        этот context КАК ЕСТЬ, не переходя ни в какой новый стейт — стейт-
        машина застревала на месте и повторяла попытку реконструкции СНОВА
        для той же ошибки на следующей итерации, без сработавшего dedup
        (_NR_FILES_KEY помечается только в коде НИЖЕ, который при таком
        баге никогда не достигался). На реальном проекте это съело весь
        project_timeout (111 неудачных попыток, 0 успешных, 0 решений за
        весь прогон) вместо одной попытки + честной сдачи в NEEDS_REVIEW.

        success=False → вызывающий код ОБЯЗАН продолжить СВОЙ obычный путь
        (отправку в NEEDS_REVIEW с корректной пометкой dedup-файла), используя
        возвращённый context (содержит обновлённый счётчик неудач).

        Реконструкция не принимает решение сама — она лишь ГЕНЕРИРУЕТ
        кандидат-патч с patch_source="syntax_reconstruction"; решение
        (ACCEPT/REJECT/NEEDS_REVIEW) принимает обычный пайплайн ниже по
        потоку (ReviewStage с усиленной проверкой + DecideStage)."""
        from fixers.semantic_recovery import extract_logic_snapshot, reconstruct_file

        snapshot = extract_logic_snapshot(file_content)
        if snapshot.is_empty():
            logger.info(
                "  E999 Semantic Recovery: не удалось извлечь логический слепок "
                "(нет class/def/import) — пропускаем, обычный путь NEEDS_REVIEW"
            )
            return context, False

        logger.info(
            "  E999 Semantic Recovery: попытка реконструкции %s "
            "(classes=%d, functions=%d)",
            error.get("file", ""), len(snapshot.classes), len(snapshot.functions),
        )
        new_content = reconstruct_file(
            self.llm_client, file_content, snapshot, error, language=context.language,
        )
        if new_content is None:
            logger.info("  E999 Semantic Recovery: реконструкция не удалась — обычный путь NEEDS_REVIEW")
            _sr_meta = dict(context.metadata)
            _sr_meta["syntax_reconstruction_failed_count"] = int(
                _sr_meta.get("syntax_reconstruction_failed_count", 0)
            ) + 1
            return context.update(metadata=_sr_meta), False

        new_metadata = dict(context.metadata)
        new_metadata[MetadataKeys.FULL_FILE_REPLACEMENT] = new_content
        new_metadata[MetadataKeys.PATCH_SOURCE] = "syntax_reconstruction"
        # Слепок сохраняем в metadata — ReviewStage сравнит его с РЕЗУЛЬТАТОМ
        # (через AST, т.к. результат уже валиден) без повторного regex-парсинга.
        new_metadata["_syntax_reconstruction_snapshot"] = {
            "classes": sorted(snapshot.classes),
            "functions": {k: list(v) for k, v in snapshot.functions.items()},
            "imports": sorted(snapshot.imports),
        }
        new_metadata["_syntax_reconstruction_old_content"] = file_content
        new_metadata["syntax_reconstruction_attempts"] = int(
            new_metadata.get("syntax_reconstruction_attempts", 0)
        ) + 1
        context = context.update(metadata=new_metadata)
        logger.info(
            "  E999 Semantic Recovery: реконструкция дала валидный синтаксис, "
            "передаём через обычный пайплайн (Apply → Validate → Review → Decide)"
        )
        return context.add_state_to_history(State.APPLYING_PATCH), True

    @staticmethod
    def _make_type_ignore_patch(error: Dict[str, Any], context) -> Optional[str]:
        """Добавляет # type: ignore[<code>] к строке импорта.

        Безопасный детерминированный фикс: не меняет поведение, просто
        подавляет mypy-предупреждение об отсутствии stub-пакетов.

        control series 12 (2026-06-20): код в ignore-комментарии раньше был
        ХАРДКОДНУТ на "import-untyped" независимо от реального error["code"] —
        для "import-not-found" это сгенерировало бы НЕВАЛИДНЫЙ
        `# type: ignore[import-untyped]` на строке с другой ошибкой (mypy не
        подавит её, т.к. код в скобках не совпадает с реальным). Теперь код
        берётся из error напрямую.
        2026-06-24 (toggl2notion line-shift расследование, malinkang/toggl2notion):
        раньше комментарий слепо дописывался в конец строки независимо от
        итоговой длины — на `from x import a, b, c` ровно на границе E501
        (например 75 символов из 79) дописывание `  # type: ignore[...]`
        (30-40 символов) надёжно создавало НОВУЮ ошибку E501, которую
        NET_DELTA correctly ловил как регрессию и откатывал — безопасный по
        смыслу фикс терялся. Теперь если итоговая строка превысила бы лимит
        длины — multi-name `from X import a, b, c` переписывается в
        многострочную скобочную форму (mypy привязывает `# type: ignore` к
        строке `from X import (`, это валидно). Однострочный `import x` без
        запятых так обернуть нельзя — для него поведение не меняется (см.
        `_wrap_import_with_ignore_comment`).
        """
        import difflib as _difflib
        import re as _re
        _MAX_LINE_LEN = 79  # pycodestyle/flake8 default (см. реальные E501 в логах)
        try:
            work_dir = context.working_path if hasattr(context, "working_path") else context.project_path
            file_str = error.get("file", "")
            if not file_str:
                return None
            ignore_code = error.get("code", "") or "import-untyped"
            file_path = work_dir / file_str
            if not file_path.exists():
                return None
            line_no = int(error.get("line", 0) or 0)
            if line_no < 1:
                return None
            original = file_path.read_text(encoding="utf-8")
            lines = original.splitlines(keepends=True)
            if line_no > len(lines):
                return None
            target = lines[line_no - 1]
            stripped = target.strip()
            if not (stripped.startswith("import ") or stripped.startswith("from ")):
                return None
            if "# type: ignore" in target:
                return None
            eol = "\r\n" if target.endswith("\r\n") else "\n"
            new_line = target.rstrip("\r\n") + f"  # type: ignore[{ignore_code}]" + eol
            if len(new_line.rstrip("\r\n")) > _MAX_LINE_LEN:
                indent = _re.match(r"^(\s*)", target).group(1)
                wrapped_block = _wrap_import_with_ignore_comment(
                    stripped, indent, eol, ignore_code,
                )
                if wrapped_block is not None:
                    new_line = wrapped_block
            lines[line_no - 1] = new_line
            new_content = "".join(lines)
            patch = list(_difflib.unified_diff(
                original.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{file_str}",
                tofile=f"b/{file_str}",
            ))
            return "".join(patch) if patch else None
        except Exception as e:
            logger.debug("  _make_type_ignore_patch упал: %s", e)
            return None

    # -----------------------------------------------------------------
    # Вспомогательные методы
    # -----------------------------------------------------------------
    @staticmethod
    def _generate_unified_diff(original: str, modified: str, filename: str) -> str:
        diff = difflib.unified_diff(
            original.splitlines(keepends=True),
            modified.splitlines(keepends=True),
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
        )
        return ''.join(diff)

    @staticmethod
    def _extract_error_range(message: str) -> Optional[Tuple[int, int]]:
        numbers = re.findall(r'(?<!\d)(\d+)\s*(?:\||\.\.\.|\,)', message)
        if len(numbers) >= 2:
            try:
                start = int(numbers[0])
                end = int(numbers[-1])
                if start < end:
                    return (start, end)
            except (ValueError, IndexError):
                pass
        return None

    @staticmethod
    def _is_diff_too_large(patch: str, file_content: str, max_ratio: float = 0.3) -> bool:
        if not patch:
            return True
        total_lines = file_content.count('\n') + 1
        changed = 0
        for line in patch.splitlines():
            if line.startswith(('+', '-')) and not line.startswith(('+++', '---')):
                changed += 1
        ratio = changed / total_lines if total_lines > 0 else 1.0
        if ratio > max_ratio:
            logger.info("Патч отклонён: слишком большой (%d изменённых строк из %d, ratio=%.2f)", 
                       changed, total_lines, ratio)
            return True
        return False

    @staticmethod
    def _extract_package_from_security_error(error: Dict[str, Any]) -> Optional[str]:
        msg = error.get("message", "")
        match = re.search(r'advisory for ([a-zA-Z0-9_-]+)', msg)
        if match:
            return match.group(1)
        match = re.search(r':\s*([a-zA-Z0-9_-]+)\s+\d+\.\d+\.\d+', msg)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _get_current_version_from_toml(context: PipelineContext, package_name: str) -> Optional[str]:
        try:
            work_dir = context.working_path if hasattr(context, 'working_path') else context.project_path
            toml_path = work_dir / "Cargo.toml"
            if not toml_path.exists():
                return None
            import tomlkit
            with open(toml_path, "rb") as f:
                doc = tomlkit.load(f)
            for section in ("dependencies", "dev-dependencies", "build-dependencies"):
                if section in doc and package_name in doc[section]:
                    dep = doc[section][package_name]
                    if isinstance(dep, str):
                        return dep
                    elif isinstance(dep, dict) and "version" in dep:
                        return dep["version"]
        except Exception as e:
            logger.warning("Ошибка чтения Cargo.toml: %s", e)
        return None

    def _inject_compiler_suggestions(self, context_text: str, error: Dict[str, Any]) -> str:
        suggestions = error.get("rustc_suggestions")
        if not suggestions:
            return context_text
        parts = ["\n\nCOMPILER SUGGESTIONS (from rustc):"]
        for sugg in suggestions[:5]:
            parts.append(f"- Line {sugg.get('line', '?')}: {sugg.get('message', '')}")
            if sugg.get('replacement'):
                parts.append(f"  Suggested replacement: `{sugg['replacement']}`")
        return context_text + "\n".join(parts)

    @staticmethod
    def _is_safe_insertion_point(target_line: str, col: int, replacement: str) -> bool:
        """Безопасна ли вставка `replacement` в `target_line` на позиции `col`.

        Возвращает False, если вставка склеит токены — то есть либо первый
        символ replacement продолжит существующий идентификатор/составной
        оператор слева, либо последний символ replacement сольётся с
        идентификатором/оператором справа. Это защищает от случаев типа
        вставки `if` внутрь `io` (получалось `iifo`) или `>=` рядом с `=>`
        (получалось `=>=>`).

        Пустой replacement или вставка в пустую строку — безопасны.
        """
        if not replacement:
            return True

        def is_id(c: str) -> bool:
            return bool(c) and (c.isalnum() or c == "_")

        # Набор символов, образующих составные операторы Rust.
        op_chars = set("=<>!|&+-*/%.:?")

        def is_op(c: str) -> bool:
            return bool(c) and c in op_chars

        left = target_line[col - 1] if col >= 1 and col - 1 < len(target_line) else ""
        right = target_line[col] if 0 <= col < len(target_line) else ""
        first = replacement[0]
        last = replacement[-1]

        # Слияние идентификаторов
        if is_id(left) and is_id(first):
            return False
        if is_id(last) and is_id(right):
            return False
        # Слияние операторов
        if is_op(left) and is_op(first):
            return False
        if is_op(last) and is_op(right):
            return False
        return True

    def _build_enrichment_context(self, file_content: str,
                                  error: Dict[str, Any],
                                  healer_suggestions: List[Dict],
                                  pre_cleanup_suggestions: List[Dict]) -> str:
        parts = []
        try:
            from fixers.rust_tree_sitter import RustTreeSitter
            ts = RustTreeSitter()
            unclosed = ts.find_unclosed_blocks(file_content)
            if unclosed:
                lines = ["TREE-SITTER STRUCTURAL ANALYSIS:"]
                for block in unclosed:
                    lines.append(
                        f"- {block['block_type']} opened at line {block['open_line']} "
                        f"(missing '}}' at indent {block['expected_indent']})"
                    )
                lines.append("Fix by inserting '}' where they belong, preserving the block hierarchy.\n")
                parts.append("\n".join(lines))
        except Exception:
            pass

        if healer_suggestions:
            lines = ["HEURISTIC SUGGESTIONS:"]
            for s in healer_suggestions:
                lines.append(f"- Line {s.get('line', '?')}: {s.get('content', '')}")
            parts.append("\n".join(lines))

        if pre_cleanup_suggestions:
            lines = ["PRE-CLEANUP ARTIFACTS:"]
            for s in pre_cleanup_suggestions:
                lines.append(f"- {s.get('file', '?')}: {s.get('message', '')}")
            parts.append("\n".join(lines))

        return "\n\n" + "\n\n".join(parts) if parts else ""

    def _blocking_adaptive_strategy_no_sandbox(self, context, error, error_sig, file_content,
                                               failure_reason, compiler_feedback, segments, file_hash,
                                               semantic_context: str = ""):
        logger.info("  Запуск адаптивной стратегии для BLOCKING ошибки")
        attempt = 0
        phase = "single"

        # case-file строится один раз до цикла: external examples + cross-file context.
        # Только для фазы "single", попытка 1 — последующие итерации используют более
        # узкий контекст и добавлять весь case-file туда нецелесообразно.
        work_dir = context.working_path if hasattr(context, "working_path") else context.project_path
        _blocking_case_file = ""
        try:
            _blocking_case_file = self._build_case_file_for_error(
                error, error_sig, file_content, work_dir, context.language, context
            )
        except Exception as _e:
            logger.debug("  BLOCKING: case-file не удался: %s", _e)

        # P2.6: структурный пре-пасс (аналог P.S.1 для BLOCKING).
        # Одна попытка generate_structured_fix до адаптивного цикла.
        # При успехе — немедленный возврат в APPLYING_PATCH.
        # При любой неудаче — цикл продолжается без изменений.
        _ps_file_str = error.get("file") or ""
        _ps_fc: Dict[str, Any] = {_ps_file_str: file_content} if _ps_file_str else {}
        try:
            _ps_fc.update(self._collect_related_contents(
                error, work_dir, context.language, _ps_file_str
            ))
        except Exception as _e:
            logger.debug("  BLOCKING pre-pass: _collect_related_contents пропущен: %s", _e)
        _ps_hint = _blocking_case_file or failure_reason or ""
        _ps_edit_set = None
        try:
            _ps_edit_set = self.llm_client.generate_structured_fix(
                error=error,
                file_contents=_ps_fc,
                language=context.language,
                extra_hint=_ps_hint,
            )
        except Exception as _e:
            logger.warning(
                "  BLOCKING pre-pass: generate_structured_fix упал: %s — переходим к циклу", _e
            )
        if _ps_edit_set is not None and _ps_file_str:
            _ps_candidate = None
            try:
                _ps_candidate = _ps_edit_set.to_unified_diff(_ps_fc)
            except Exception as _e:
                logger.debug("  BLOCKING pre-pass: to_unified_diff упал: %s", _e)
            if _ps_candidate and not PatchEngine.is_empty_patch(_ps_candidate):
                _ps_candidate = normalize_patch(
                    _ps_candidate, _ps_file_str, original_content=file_content
                )
                if _ps_candidate and not self._is_diff_too_large(_ps_candidate, file_content):
                    if PatchEngine.is_patch_relevant(_ps_candidate, error, file_content, segments):
                        logger.info(
                            "  BLOCKING pre-pass: EditSet принят "
                            "(intent=%r, edits=%d, conf=%.2f)",
                            _ps_edit_set.intent[:60],
                            len(_ps_edit_set.edits),
                            _ps_edit_set.confidence,
                        )
                        _ps_meta = dict(context.metadata)
                        _ps_meta[MetadataKeys.PATCH_SOURCE] = "structured_llm_blocking"
                        _ps_meta["structured_edit"] = _ps_edit_set.to_dict()
                        _ps_meta["confidence"] = float(_ps_edit_set.confidence)
                        _ps_meta["intent"] = _ps_edit_set.intent
                        _ps_meta["risks"] = list(_ps_edit_set.risks)
                        context = context.set_patch(_ps_candidate)
                        context = context.update(metadata=_ps_meta)
                        context = context.record_processed_error(_process_key(error))
                        return context.add_state_to_history(State.APPLYING_PATCH)
                    else:
                        logger.info(
                            "  BLOCKING pre-pass: патч нерелевантен — переходим к циклу"
                        )
                else:
                    logger.info(
                        "  BLOCKING pre-pass: патч отфильтрован (_is_diff_too_large "
                        "или normalize) — переходим к циклу"
                    )

        irrelevant_streak = 0
        while attempt < self.MAX_BLOCKING_LLM_RETRIES:
            if self._deadline_exceeded(context):
                logger.warning(
                    "  PROJECT_TIMEOUT: внутри blocking-петли (итерация %d/%d) — досрочный выход",
                    attempt, self.MAX_BLOCKING_LLM_RETRIES,
                )
                break
            attempt += 1
            logger.info("  Итерация %d/%d (фаза: %s)", attempt, self.MAX_BLOCKING_LLM_RETRIES, phase)
            new_metadata = dict(context.metadata)
            new_metadata["blocking_attempt"] = attempt
            context = context.update(metadata=new_metadata)

            fb = ""
            if compiler_feedback:
                fb_lines = compiler_feedback.strip().splitlines()
                if fb_lines:
                    fb = f"Previous fix caused: {fb_lines[0][:200]}"
            elif failure_reason:
                fb = failure_reason

            if phase == "single":
                prompt_context = file_content
                if attempt == 1 and _blocking_case_file:
                    prompt_context = prompt_context + "\n\n" + _blocking_case_file
            elif phase == "multi":
                error_line = error.get("line", 0)
                lines = file_content.splitlines(keepends=True)
                start = max(0, error_line - 4)
                end = min(len(lines), error_line + 3)
                prompt_context = "".join(lines[start:end])
                fb = (fb or "") + " You may modify up to 3 lines around the error to fix it."
            else:
                return self._segmented_strategy(context, error, error_sig, file_content,
                                                fb or failure_reason, compiler_feedback,
                                                segments, file_hash)

            prompt_context = self._inject_compiler_suggestions(prompt_context, error)
            if semantic_context:
                prompt_context += f"\n\n{semantic_context}"

            patch = self.llm_client.generate_fix(
                error=error,
                context=prompt_context,
                language=context.language,
                failure_reason=fb,
                attempt=attempt,
                compiler_feedback=fb,
            )

            if patch and not PatchEngine.is_empty_patch(patch):
                if not PatchEngine.is_patch_relevant(patch, error, file_content, segments):
                    irrelevant_streak += 1
                    logger.warning(
                        "  Патч нерелевантен, требуем повтор (streak=%d)", irrelevant_streak
                    )
                    if irrelevant_streak >= 5:
                        logger.warning(
                            "  %d нерелевантных патчей подряд — ошибка неисправима в этой фазе",
                            irrelevant_streak,
                        )
                        break
                    if attempt >= 3:
                        phase = "multi"
                    continue
                irrelevant_streak = 0

                logger.info("  Итерация %d: получен валидный патч", attempt)
                new_metadata = dict(context.metadata)
                new_metadata[MetadataKeys.PATCH_SOURCE] = f"llm_blocking_{phase}"
                new_metadata.pop("last_patch_failure", None)
                new_metadata.pop("blocking_attempt", None)
                context = context.set_patch(patch)
                context = context.update(metadata=new_metadata)
                context = context.record_processed_error(_process_key(error))
                return context.add_state_to_history(State.APPLYING_PATCH)

            if not patch or PatchEngine.is_empty_patch(patch):
                logger.warning("  Итерация %d: пустой патч", attempt)
                if attempt >= 2:
                    phase = "multi"
                if attempt >= 4:
                    phase = "segment"
                continue

        context = context.record_processed_error(_process_key(error))
        context = context.add_unfixable_error(error)
        return context.add_state_to_history(State.NEXT_ERROR)

    def _segmented_strategy(self, context, error, error_sig, file_content,
                            failure_reason, compiler_feedback, segments, file_hash):
        logger.info("  Переход к сегментной стратегии (оркестрация отключена)")
        segmented_attempts = context.metadata.get(MetadataKeys.SEGMENTED_ATTEMPTS, 0) + 1
        new_metadata = dict(context.metadata)
        new_metadata[MetadataKeys.SEGMENTED_ATTEMPTS] = segmented_attempts
        context = context.update(metadata=new_metadata)

        try:
            segmented_patches = self._generate_segmented_patches(
                error=error,
                file_content=file_content,
                context=context,
                failure_reason=failure_reason,
                compiler_feedback=compiler_feedback,
                segments=segments,
                file_hash=file_hash,
            )
            if segmented_patches:
                merged_patch = self._merge_patches_smart(segmented_patches, context, error, file_content, segments)
                if not merged_patch:
                    if segmented_attempts >= self.MAX_SEGMENTED_STRATEGY_ATTEMPTS:
                        logger.warning("  Пустой объединённый патч (попытка %d/%d) – unfixable",
                                       segmented_attempts, self.MAX_SEGMENTED_STRATEGY_ATTEMPTS)
                        context = context.record_processed_error(_process_key(error))
                        context = context.add_unfixable_error(error)
                        return context.add_state_to_history(State.NEXT_ERROR)
                    logger.warning("  Пустой объединённый патч (попытка %d) – retry с full-file",
                                   segmented_attempts)
                    new_metadata[MetadataKeys.LAST_PATCH_FAILURE] = (
                        "Your patch produced no actual changes — the modified content was identical "
                        "to the original. Carefully modify the exact line(s) responsible for the error."
                    )
                    new_metadata[MetadataKeys.LAST_PATCH_INTENT] = (
                        new_metadata.get(MetadataKeys.LAST_PATCH_INTENT)
                        or "segmented patch (produced no diff)"
                    )
                    new_metadata["use_full_file"] = True
                    context = context.update(metadata=new_metadata)
                    return context.add_state_to_history(State.GENERATING_PATCH)

                new_metadata[MetadataKeys.SEGMENTED_PATCHES] = [merged_patch]
                new_metadata[MetadataKeys.PATCH_SOURCE] = "llm_segmented"
                new_metadata.pop(MetadataKeys.LAST_PATCH_FAILURE, None)
                new_metadata.pop(MetadataKeys.EMPTY_RETRIES, None)
                new_metadata.pop(MetadataKeys.SEGMENTED_ATTEMPTS, None)
                context = context.set_patch(None)
                context = context.update(metadata=new_metadata)
                return context.add_state_to_history(State.APPLYING_PATCH)
            else:
                if segmented_attempts >= self.MAX_SEGMENTED_STRATEGY_ATTEMPTS:
                    logger.error("  Нет патчей (попытка %d/%d) – unfixable",
                                 segmented_attempts, self.MAX_SEGMENTED_STRATEGY_ATTEMPTS)
                    context = context.record_processed_error(_process_key(error))
                    context = context.add_unfixable_error(error)
                    return context.add_state_to_history(State.NEXT_ERROR)
                logger.warning("  Нет патчей (попытка %d) – retry", segmented_attempts)
                new_metadata[MetadataKeys.LAST_PATCH_FAILURE] = (
                    "LLM did not produce a valid patch. Review the error, identify the offending "
                    "line, and provide a precise unified diff that fixes the issue."
                )
                context = context.update(metadata=new_metadata)
                return context.add_state_to_history(State.GENERATING_PATCH)
        except Exception as e:
            logger.error(f"  Ошибка сегментной обработки: {e}", exc_info=True)
            context = context.record_processed_error(_process_key(error))
            context = context.add_unfixable_error(error)
            return context.add_state_to_history(State.NEXT_ERROR)

    def _generate_segmented_patches(self, error, file_content, context, failure_reason,
                                    compiler_feedback, segments, file_hash):
        self.segmenter.language = context.language
        global_ctx = self.segmenter.extract_global_context(file_content)

        work_dir = context.working_path if hasattr(context, 'working_path') else context.project_path
        file_path = work_dir / error["file"]

        target_line = error.get("line", 0)
        if target_line > 0:
            start = max(1, target_line - 20)
            end = min(len(file_content.splitlines()), target_line + 20)
            segments_list = [{
                "start_line": start,
                "end_line": end,
                "text": "\n".join(file_content.splitlines()[start-1:end])
            }]
        else:
            segments_list = self.segmenter.segment_file(file_content, max_segment_lines=400)[:5]

        all_errors = [e for e in context.current_errors if e.get("file") == error.get("file")]
        context_builder = SegmentContextBuilder(file_content)

        patched_segments = []
        for idx, seg in enumerate(segments_list, 1):
            if self._deadline_exceeded(context):
                logger.warning(
                    "  PROJECT_TIMEOUT: внутри segmented-петли (сегмент %d/%d) — досрочный выход",
                    idx, len(segments_list),
                )
                break
            seg_start = seg["start_line"]
            seg_end = seg["end_line"]

            if not self.segment_anti_loop.record_attempt(str(error.get("file", "")), seg_start, file_hash):
                continue

            segment_content = seg.get("text", "")
            if not segment_content:
                segment_lines = file_content.splitlines()[seg_start-1:seg_end]
                segment_content = "\n".join(segment_lines)

            # FIXME-маркер убран из содержимого строки:
            # если он попадает в «-» строки diff, anchor matching не найдёт
            # его в оригинальном файле → 10 попыток вхолостую.
            # Информация об ошибке передаётся через build_segment_prompt.

            compiler_help = ""
            help_match = re.search(r'help:\s*(.*)', error.get("message", ""))
            if help_match:
                compiler_help = help_match.group(1).strip()

            segment_context = context_builder.build_context_for(
                segment_content.splitlines(True),
                compiler_help=compiler_help
            )
            enriched_prompt = segment_context + "\n" + self.segmenter.build_segment_prompt(
                {"start_line": seg_start, "end_line": seg_end, "text": segment_content},
                global_ctx,
                [e for e in all_errors if seg_start <= e.get("line", 0) <= seg_end],
                target_error=error,
            )

            segment_patch = None
            _anchor_hint = ""
            for attempt_no in range(1, self.MAX_SEGMENT_ATTEMPTS + 1):
                if self._deadline_exceeded(context):
                    logger.warning(
                        "  PROJECT_TIMEOUT: внутри segment-attempt-петли (сегмент %d, попытка %d/%d) — досрочный выход",
                        idx, attempt_no, self.MAX_SEGMENT_ATTEMPTS,
                    )
                    break
                _seg_ctx = enriched_prompt
                if _anchor_hint:
                    _seg_ctx = enriched_prompt + "\n\n" + _anchor_hint
                patch = self.llm_client.generate_fix(
                    error=error,
                    context=_seg_ctx,
                    language=context.language,
                    attempt=attempt_no,
                    project_dir=work_dir,
                    file_path_obj=file_path
                )
                if patch and not PatchEngine.is_empty_patch(patch):
                    if not PatchEngine.is_patch_relevant(patch, error, file_content, segments):
                        continue
                    if not PatchEngine.is_patch_anchorable(patch, file_content):
                        logger.info(
                            "  Сегмент-попытка %d: патч релевантен, но анкер не найден — повтор",
                            attempt_no,
                        )
                        _anchor_hint = (
                            "ANCHOR ERROR: your previous diff contained lines that don't match "
                            "the actual file content. Copy the EXACT lines from the SEGMENT above "
                            "as context (' ') lines in your diff — do not paraphrase or reformat them."
                        )
                        continue
                    self.segment_anti_loop.reset_segment(str(error.get("file", "")), seg_start)
                    segment_patch = patch
                    break
            if segment_patch:
                patched_segments.append(segment_patch)

        return patched_segments

    def _merge_patches_smart(self, patches, context, error, file_content, segments):
        work_dir = context.working_path if hasattr(context, 'working_path') else context.project_path
        file_path = work_dir / error["file"]
        file_name = file_path.name

        merged = PatchEngine.apply_patches_and_diff(file_content, patches, file_name)
        if not merged:
            return None

        if not PatchEngine.is_patch_relevant(merged, error, file_content, segments):
            return None

        if self._validate_merged_patch(merged, context, error, file_content, segments):
            return merged
        return None

    def _validate_merged_patch(self, patch, context, error, file_content, segments):
        if not PatchEngine.is_patch_relevant(patch, error, file_content, segments):
            return False

        file_path_str = error.get("file", "")
        if not file_path_str:
            return False
        work_dir = context.working_path if hasattr(context, 'working_path') else context.project_path
        file_path = work_dir / file_path_str
        if not file_path.exists():
            return False

        def mod_fn(tmp_file: Path):
            if not self.patch_engine.apply_patch(tmp_file, patch):
                raise RuntimeError("Merge patch application failed")

        from validation.syntax_validator import validate_in_sandbox
        success, _, _ = validate_in_sandbox(work_dir, file_path, mod_fn, context.language)
        return success

    def _get_segments(self, file_path_str, file_content) -> List[Dict[str, Any]]:
        cache_key = (file_path_str, hash(file_content))
        if getattr(self, '_cache_key', None) != cache_key:
            self._segments_cache[file_path_str] = self.segmenter.segment_file(file_content, max_segment_lines=10000)
            self._cache_key = cache_key
            if len(self._segments_cache) > 10:
                oldest = next(iter(self._segments_cache))
                del self._segments_cache[oldest]
        return self._segments_cache.get(file_path_str, [])

    @staticmethod
    def _error_signature(error):
        return PipelineStage._static_signature(error)
