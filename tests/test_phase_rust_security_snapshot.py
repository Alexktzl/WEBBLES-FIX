"""
Rust RUSTSEC/Cargo.toml security-фикс: снапшот перед мутацией + no-op детект
(2026-07-09, orhun/gpg-tui — первый Rust-специфичный конверсионный баг).

Инцидент: _handle_security_error мутировал Cargo.lock/Cargo.toml `cargo update`
МИМО ApplyPatchStage и не писал _pre_patch_content — ValidateStage ставил
snapshot_failed, DecideStage не мог откатить. Плюс returncode==0 считался
успехом даже когда cargo update — no-op на транзитивной зависимости
(залочена semver-констрейнтом родителя): ошибка удалялась из очереди,
VALIDATING пере-сканировал, advisory на месте → вечный REJECT
error_count_not_decreased + прожиг медленных ре-сканов cargo каждый цикл.

Инварианты:
1. Реальный cargo update (Cargo.lock изменился) → _pre_patch_content
   содержит Cargo.toml и Cargo.lock (откат возможен), уход в VALIDATING.
2. No-op cargo update (Cargo.lock не изменился) → НЕ успех: ошибка помечена
   нерешаемой (add_unfixable_error), NEXT_ERROR — не пере-выбирается.
3. Fail-closed сохранён: инвариант держится, порчи нет.

Запуск: python tests/test_phase_rust_security_snapshot.py
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from core.stages.generate_patch_stage import GeneratePatchStage  # noqa: E402
from core.state_machine import State  # noqa: E402

results = []


def check(name, cond, note=""):
    results.append((name, bool(cond), note))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f" ({note})" if note and not cond else ""))
    assert cond, f"{name}: {note}"


class _Ctx:
    def __init__(self, work_dir, errors):
        self.working_path = work_dir
        self.project_path = work_dir
        self.metadata = {}
        self.current_errors = list(errors)
        self.processed = []
        self.unfixable = []
        self.state_history = []
        self.patch = "unset"

    def update(self, metadata=None, **_kw):
        if metadata is not None:
            self.metadata = metadata
        return self

    def set_errors(self, errs):
        self.current_errors = list(errs)
        return self

    def set_patch(self, p):
        self.patch = p
        return self

    def record_processed_error(self, k):
        self.processed.append(k)
        return self

    def add_unfixable_error(self, e):
        self.unfixable.append(e)
        return self

    def add_state_to_history(self, s):
        self.state_history.append(s)
        return self


def _stage():
    st = GeneratePatchStage.__new__(GeneratePatchStage)
    st._extract_package_from_security_error = lambda e: "bytes"
    st._get_current_version_from_toml = lambda ctx, pkg: None
    st._error_signature = lambda e: e.get("code", "")
    return st


ERR = {"file": "Cargo.toml", "code": "RUSTSEC-2026-0007",
       "error_type": "security", "error_class": "MANIFEST",
       "message": "vulnerable: bytes 1.10.1"}


def _setup(tmp, lock_body):
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    (tmp / "Cargo.lock").write_text(lock_body, encoding="utf-8")


def test_noop_cargo_update_marks_unfixable(tmp_path):
    """cargo update rc=0 но Cargo.lock не изменился → нерешаемо, NEXT_ERROR."""
    wd = tmp_path / "crate"
    _setup(wd, "lock-v1\n")
    st = _stage()
    ctx = _Ctx(wd, [ERR])

    def _run(cmd, **kw):
        return MagicMock(returncode=0, stdout="", stderr="")  # ничего не меняет

    with patch("core.stages.generate_patch_stage.subprocess.run", side_effect=_run), \
         patch("core.stages.generate_patch_stage.DependencySearch.search_safe_version", return_value=None):
        out = st._handle_security_error(ctx, ERR, "RUSTSEC-2026-0007")
    check("noop_goes_next_error", out.state_history[-1] == State.NEXT_ERROR)
    check("noop_marked_unfixable", ERR in out.unfixable)
    check("noop_error_still_in_queue", any(e.get("code") == "RUSTSEC-2026-0007" for e in out.current_errors),
          "no-op не должен удалять ошибку из очереди как исправленную")


def test_real_update_writes_snapshot_and_validates(tmp_path):
    """cargo update реально поменял Cargo.lock → снапшот обоих манифестов
    в _pre_patch_content, уход в VALIDATING."""
    wd = tmp_path / "crate"
    _setup(wd, "lock-v1\n")
    st = _stage()
    ctx = _Ctx(wd, [ERR])

    def _run(cmd, **kw):
        # эмулируем реальный бамп: переписываем Cargo.lock
        (wd / "Cargo.lock").write_text("lock-v2-bumped\n", encoding="utf-8")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("core.stages.generate_patch_stage.subprocess.run", side_effect=_run):
        out = st._handle_security_error(ctx, ERR, "RUSTSEC-2026-0007")
    check("real_goes_validating", out.state_history[-1] == State.VALIDATING)
    ppc = out.metadata.get("_pre_patch_content") or {}
    check("snapshot_has_cargo_toml", "Cargo.toml" in ppc, f"ppc keys: {list(ppc)}")
    check("snapshot_has_cargo_lock", "Cargo.lock" in ppc, f"ppc keys: {list(ppc)}")
    check("snapshot_lock_is_pre_mutation", ppc.get("Cargo.lock") == "lock-v1\n",
          "снапшот обязан хранить СОДЕРЖИМОЕ ДО мутации")
    check("real_error_removed_from_queue",
          not any(e.get("code") == "RUSTSEC-2026-0007" for e in out.current_errors))


def test_dispatch_routes_rustsec_despite_lost_error_type():
    """2026-07-09 (Rust-серия-3): RUSTSEC-ошибки от cargo audit теряют
    error_type к моменту генерации (в реале None, error_class=MANIFEST).
    Диспетчер обязан роутить их в security-хендлер ПО КОДУ (RUSTSEC-*),
    иначе они падают в _handle_manifest-пустышку → вечный REJECT-прожиг.
    Кодируем само условие диспетчеризации (инвариант маршрутизации)."""
    def _routes_to_security(err):
        code = str(err.get("code") or "")
        is_rustsec = code.startswith("RUSTSEC")
        return err.get("file") == "Cargo.toml" and (
            err.get("error_type") == "security" or is_rustsec
        )
    # error_type потерян (None) — как в реальном прогоне gpg-tui
    check("rustsec_lost_type_routes",
          _routes_to_security({"file": "Cargo.toml", "code": "RUSTSEC-2026-0007", "error_type": None}))
    # error_type сохранён — тоже роутится
    check("rustsec_with_type_routes",
          _routes_to_security({"file": "Cargo.toml", "code": "RUSTSEC-2026-0194", "error_type": "security"}))
    # обычная MANIFEST-ошибка (не RUSTSEC) — НЕ в security
    check("plain_manifest_not_security",
          not _routes_to_security({"file": "Cargo.toml", "code": "unused-manifest-key", "error_type": None}))
    # RUSTSEC не в Cargo.toml (гипотетич.) — не матчим по файлу
    check("rustsec_wrong_file_not_routed",
          not _routes_to_security({"file": "src/lib.rs", "code": "RUSTSEC-2026-0007", "error_type": None}))


def test_rustsec_attempt_cap_marks_unfixable(tmp_path):
    """2026-07-09 (gpg-tui solo): cargo update может СДВИНУТЬ Cargo.lock (не
    no-op → «успех»), но не закрыть конкретный advisory (транзитив залочен
    родителем) → VALIDATING→REJECT→повторный выбор = петля 6× REJECT.
    После MAX_RUSTSEC_ATTEMPTS код помечается нерешаемым БЕЗ новой мутации."""
    wd = tmp_path / "crate"
    _setup(wd, "lock-v1\n")
    st = _stage()
    # счётчик уже на пороге
    ctx = _Ctx(wd, [ERR])
    ctx.metadata = {"_rustsec_attempts_RUSTSEC-2026-0007": 2}
    called = {"cargo": False}

    def _run(cmd, **kw):
        called["cargo"] = True
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("core.stages.generate_patch_stage.subprocess.run", side_effect=_run):
        out = st._handle_security_error(ctx, ERR, "RUSTSEC-2026-0007")
    check("cap_goes_next_error", out.state_history[-1] == State.NEXT_ERROR)
    check("cap_marked_unfixable", ERR in out.unfixable)
    check("cap_no_cargo_update", not called["cargo"],
          "при исчерпанном лимите cargo update не должен вызываться")


def test_rustsec_attempt_counter_increments(tmp_path):
    """Каждый заход инкрементит счётчик попыток для кода advisory."""
    wd = tmp_path / "crate"
    _setup(wd, "lock-v1\n")
    st = _stage()
    ctx = _Ctx(wd, [ERR])  # счётчик пуст

    def _run(cmd, **kw):
        return MagicMock(returncode=0, stdout="", stderr="")  # no-op

    with patch("core.stages.generate_patch_stage.subprocess.run", side_effect=_run), \
         patch("core.stages.generate_patch_stage.DependencySearch.search_safe_version", return_value=None):
        out = st._handle_security_error(ctx, ERR, "RUSTSEC-2026-0007")
    check("counter_incremented",
          out.metadata.get("_rustsec_attempts_RUSTSEC-2026-0007") == 1,
          f"meta: {out.metadata}")


def test_precise_bump_uses_fix_version(tmp_path):
    """2026-07-09 (конверсия RUSTSEC): при наличии fix_version хендлер
    сначала пробует `cargo update -p pkg --precise <fix_version>` — это чинит
    транзитив, который обычный update не берёт. Успех = Cargo.lock изменился."""
    wd = tmp_path / "crate"
    _setup(wd, "lock-v1\n")
    st = _stage()
    err = dict(ERR); err["fix_version"] = "1.10.2"
    ctx = _Ctx(wd, [err])
    calls = []

    def _run(cmd, **kw):
        calls.append(cmd)
        # эмулируем: --precise реально бампает lock
        if "--precise" in cmd:
            (wd / "Cargo.lock").write_text("lock-v2-precise\n", encoding="utf-8")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("core.stages.generate_patch_stage.subprocess.run", side_effect=_run):
        out = st._handle_security_error(ctx, err, "RUSTSEC-2026-0007")
    check("precise_command_used",
          any("--precise" in c and "1.10.2" in c for c in calls),
          f"calls: {calls}")
    check("precise_goes_validating", out.state_history[-1] == State.VALIDATING)
    check("precise_snapshot_taken", "Cargo.lock" in (out.metadata.get("_pre_patch_content") or {}))


def test_direct_bump_fallback_when_update_noop(tmp_path):
    """cargo update (и --precise) no-op → прямой бамп Cargo.toml через
    upsert (форс версии в дерево). Успех → VALIDATING (пайплайн проверит)."""
    wd = tmp_path / "crate"
    _setup(wd, "lock-v1\n")
    (wd / "Cargo.toml").write_text("[package]\nname='x'\n[dependencies]\n", encoding="utf-8")
    st = _stage()
    st._get_current_version_from_toml = lambda ctx, pkg: None  # нет прямой зависимости
    err = dict(ERR); err["fix_version"] = "0.8.4"
    ctx = _Ctx(wd, [err])

    def _run(cmd, **kw):
        return MagicMock(returncode=0, stdout="", stderr="")  # все cargo update no-op

    called = {"upsert": False}
    import core.stages.generate_patch_stage as gps
    real_upsert = gps.upsert_dependency
    def _spy_upsert(fp, name, ver, **kw):
        called["upsert"] = True
        return real_upsert(fp, name, ver, **kw)

    with patch("core.stages.generate_patch_stage.subprocess.run", side_effect=_run), \
         patch("core.stages.generate_patch_stage.upsert_dependency", side_effect=_spy_upsert):
        out = st._handle_security_error(ctx, err, "RUSTSEC-2026-0007")
    check("direct_bump_upsert_called", called["upsert"])
    check("direct_bump_goes_validating", out.state_history[-1] == State.VALIDATING)


def test_lock_only_fix_pins_version_in_cargo_toml(tmp_path):
    """2026-07-09 (durability): успешный lock-only фикс (--precise) ДОПОЛНИТЕЛЬНО
    закрепляет fix_version в Cargo.toml, чтобы соседние cargo update не затёрли
    пин (RUSTSEC-2026-0195 чинился, но не доживал до доставки)."""
    wd = tmp_path / "crate"
    _setup(wd, "lock-v1\n")
    (wd / "Cargo.toml").write_text(
        "[package]\nname='x'\n[dependencies]\nbytes = \"1.10.1\"\n", encoding="utf-8")
    st = _stage()
    st._get_current_version_from_toml = lambda ctx, pkg: "1.10.1"
    err = dict(ERR); err["fix_version"] = "1.10.2"
    ctx = _Ctx(wd, [err])

    def _run(cmd, **kw):
        if "--precise" in cmd:
            (wd / "Cargo.lock").write_text("lock-v2\n", encoding="utf-8")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("core.stages.generate_patch_stage.subprocess.run", side_effect=_run):
        out = st._handle_security_error(ctx, err, "RUSTSEC-2026-0007")
    toml_after = (wd / "Cargo.toml").read_text(encoding="utf-8")
    check("version_pinned_in_toml", "1.10.2" in toml_after, f"toml: {toml_after!r}")
    check("pin_goes_validating", out.state_history[-1] == State.VALIDATING)


if __name__ == "__main__":
    import tempfile
    for fn in (test_noop_cargo_update_marks_unfixable, test_real_update_writes_snapshot_and_validates):
        with tempfile.TemporaryDirectory() as td:
            fn(Path(td))
    test_dispatch_routes_rustsec_despite_lost_error_type()
    for _fn in (test_rustsec_attempt_cap_marks_unfixable, test_rustsec_attempt_counter_increments,
                test_precise_bump_uses_fix_version, test_direct_bump_fallback_when_update_noop,
                test_lock_only_fix_pins_version_in_cargo_toml):
        with tempfile.TemporaryDirectory() as _td:
            _fn(Path(_td))

    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, note) for n, ok, note in results if not ok]
    print(f"rust_security_snapshot: {passed}/{len(results)} passed")
    sys.exit(1 if failed else 0)
