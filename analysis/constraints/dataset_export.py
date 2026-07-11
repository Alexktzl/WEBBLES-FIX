"""
Экспорт накопленного датасета в ТРЕНИРОВОЧНЫЙ формат (2026-07-10, курс на fine-tune).

Из datasets/{fix_dataset,dpo_pairs}.jsonl → готовые файлы для дообучения:
  * datasets/train_sft.jsonl  — SFT: {"messages":[user→prompt, assistant→fix]}
      (chat-формат Qwen; таргет = патч/исправленный код из ПОДТВЕРЖДЁННЫХ побед).
  * datasets/train_dpo.jsonl  — DPO: {"prompt","chosen","rejected"}
      (chosen=принятый фикс, rejected=отклонённая попытка на ТУ ЖЕ находку).

ТАРГЕТ = ТОЛЬКО ЧИСТЫЙ КОД. Датасет хранит сырой unified-diff (learning) или
after-снимок (verify); при экспорте diff РЕКОНСТРУИРУЕТСЯ в чистый «после»-код
(_diff_to_after) — без путей файлов, ханк-маркеров, метаданных патча. Инцидент
2026-07-11: 77% таргетов были сырыми diff'ами с путями → дообученная модель
зазубрила формат diff и конкретные пути. Не-diff патчи (EditSet-JSON) в SFT не
берём (не чистый код).

Запуск: python analysis/constraints/dataset_export.py
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

SYSTEM = ("You are a precise code-fixing assistant. Given a code analyzer finding "
          "and the surrounding code, output the minimal fix. Do not change unrelated "
          "code. Preserve all existing symbols.")


def _prompt(row: Dict[str, Any]) -> str:
    code = row.get("code") or "?"
    lang = row.get("language") or "code"
    msg = row.get("message") or ""
    ctx = row.get("before") or row.get("context") or []
    ctx_txt = "\n".join(ctx) if isinstance(ctx, list) else str(ctx)
    return (f"Language: {lang}\nFinding: {code}\nMessage: {msg}\n\n"
            f"CODE:\n{ctx_txt}\n\nProduce the fix.")


def _looks_like_diff(s: str) -> bool:
    return ("@@" in s) or s.lstrip().startswith(("--- a/", "--- ", "diff --git")) \
        or ("+++ b/" in s)


def _diff_to_after(patch: str) -> str:
    """Реконструирует ЧИСТЫЙ «после»-код из unified-diff: выкидывает заголовки
    файлов (`--- a/...`, `+++ b/...`, `diff --git`, `index`), ханк-маркеры
    (`@@`), удалённые строки (`-`); оставляет контекст и добавленные (` `/`+`)
    без ведущего символа.

    ПОЧЕМУ (2026-07-11, инцидент дообучения): 77% обучающих таргетов были
    сырыми diff'ами С ПУТЯМИ файлов → модель зазубрила формат diff и конкретные
    пути (утечка `src/Newtonsoft.Json/JsonTextWriter.cs`). Таргет ОБЯЗАН быть
    только финальным кодом, без путей и метаданных патча.
    """
    out: List[str] = []
    in_hunk = False
    for ln in patch.splitlines():
        if ln.startswith(("--- ", "+++ ", "diff --git", "index ", "rename ",
                          "new file", "deleted file", "similarity ")):
            continue
        if ln.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if ln.startswith("-"):            # удалённая строка — не входит в «после»
            continue
        if ln.startswith("+"):
            out.append(ln[1:])
        elif ln.startswith(" "):
            out.append(ln[1:])
        elif ln == "":
            out.append("")
        else:                             # неожиданная строка вне diff-грамматики
            out.append(ln)
    return "\n".join(out).strip("\n")


def _target(row: Dict[str, Any]) -> str:
    """Таргет ВСЕГДА чистый код: after-снимок (verify) или реконструкция из
    diff-патча (learning). Сырой diff/путь/JSON-патч в таргет НЕ попадает."""
    after = row.get("after") or []
    if isinstance(after, list) and any(str(l).strip() for l in after):
        return "\n".join(after)
    patch = row.get("patch")
    if patch:
        ps = str(patch)
        if _looks_like_diff(ps):
            code = _diff_to_after(ps)
            # страховка: если после чистки остались следы путей/ханков — брак
            if code.strip() and not _looks_like_diff(code) and "a/" != code[:2]:
                return code
        # не-diff патч (EditSet-JSON и т.п.) — не чистый код, в SFT не берём
        return ""
    return ""


def export(root: Path) -> Dict[str, int]:
    root = Path(root)
    ds = root / "datasets" / "fix_dataset.jsonl"
    dpo_in = root / "datasets" / "dpo_pairs.jsonl"
    sft_out = root / "datasets" / "train_sft.jsonl"
    dpo_out = root / "datasets" / "train_dpo.jsonl"

    # SFT из позитивов с непустым таргетом
    n_sft = 0
    with sft_out.open("w", encoding="utf-8") as fh:
        if ds.is_file():
            for line in ds.open(encoding="utf-8", errors="replace"):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("label") != "positive":
                    continue
                tgt = _target(row)
                if not tgt.strip():
                    continue
                fh.write(json.dumps({"messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": _prompt(row)},
                    {"role": "assistant", "content": tgt},
                ]}, ensure_ascii=False) + "\n")
                n_sft += 1

    # DPO из пар
    n_dpo = 0
    with dpo_out.open("w", encoding="utf-8") as fh:
        if dpo_in.is_file():
            for line in dpo_in.open(encoding="utf-8", errors="replace"):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                chosen, rejected = row.get("chosen"), row.get("rejected")
                if not chosen or not rejected or chosen == rejected:
                    continue
                fh.write(json.dumps({
                    "prompt": _prompt(row),
                    "chosen": str(chosen),
                    "rejected": str(rejected),
                }, ensure_ascii=False) + "\n")
                n_dpo += 1

    return {"sft": n_sft, "dpo": n_dpo}


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    res = export(root)
    print(f"train_sft.jsonl: {res['sft']} примеров")
    print(f"train_dpo.jsonl: {res['dpo']} пар")
