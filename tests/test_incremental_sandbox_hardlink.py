"""
Regression-тест: IncrementalSandbox.apply_patch не должен мутировать исходный
проект через расшаренный hardlink-inode.

planning/incremental_sandbox.py строит снапшоты состояний через os.link()
(hardlink), включая самый первый снапшот — прямо из source_project. Если
apply_patch() записывает пропатченный файл через write_text() без
предварительного unlink(), запись идёт "на месте" в тот же inode, и правится
содержимое ВСЕХ путей, которые на него ссылаются — включая настоящий файл
исходного проекта, а не только файл в изолированном state-каталоге песочницы.
"""

from pathlib import Path

from planning.incremental_sandbox import IncrementalSandbox


def test_apply_patch_does_not_corrupt_source_via_hardlink(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.py").write_text("x = 1\n", encoding="utf-8", newline="")

    sandbox = IncrementalSandbox(base_path=tmp_path / ".webles_sandbox", source_project=source)
    base_hash = next(iter(sandbox.states))

    new_hash, new_dir = sandbox.apply_patch(base_hash, "a.py", "x = 999\n")

    assert (source / "a.py").read_text(encoding="utf-8") == "x = 1\n", (
        "apply_patch() corrupted the original source project file via a shared hardlink inode"
    )
    assert (new_dir / "a.py").read_text(encoding="utf-8") == "x = 999\n"


def test_apply_patch_does_not_corrupt_parent_state(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.py").write_text("x = 1\n", encoding="utf-8", newline="")

    sandbox = IncrementalSandbox(base_path=tmp_path / ".webles_sandbox", source_project=source)
    base_hash = next(iter(sandbox.states))
    base_dir = sandbox.states[base_hash]

    sandbox.apply_patch(base_hash, "a.py", "x = 999\n")

    assert (base_dir / "a.py").read_text(encoding="utf-8") == "x = 1\n", (
        "apply_patch() corrupted the parent state's file via a shared hardlink inode"
    )
