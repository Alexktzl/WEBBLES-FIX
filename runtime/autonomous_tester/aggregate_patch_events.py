"""
Агрегирует patch_events.jsonl из runtime/<project_id>/ в глобальный файл
runtime/autonomous_tester/patch_events.jsonl.

Использование:
    python aggregate_patch_events.py <project_id>

Запускается после каждого проекта, ДО удаления runtime/<project_id>/.
"""

import json
import pathlib
import sys

TESTER_DIR = pathlib.Path(__file__).parent
WEBBLES_ROOT = TESTER_DIR.parent.parent  # C:/dev/webbles_fix/runtime/../ = C:/dev/webbles_fix

def main(project_id: str) -> int:
    src = WEBBLES_ROOT / "runtime" / project_id / "patch_events.jsonl"
    dst = TESTER_DIR / "patch_events.jsonl"

    if not src.exists():
        print(f"[aggregate] patch_events.jsonl не найден для {project_id}: {src}")
        return 0

    lines_added = 0
    with open(src, encoding="utf-8") as f_in, \
         open(dst, "a", encoding="utf-8") as f_out:
        for line in f_in:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Для patch_event-записей убеждаемся, что project_id проставлен
            if obj.get("_type") == "patch_event":
                if not obj.get("project_id"):
                    obj["project_id"] = project_id
                f_out.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
                lines_added += 1

    print(f"[aggregate] {project_id}: добавлено {lines_added} patch-событий → {dst}")
    return lines_added


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: aggregate_patch_events.py <project_id>")
        sys.exit(1)
    main(sys.argv[1])
