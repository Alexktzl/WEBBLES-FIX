"""
Helper: record plan/execute/verify in decisions.jsonl + update summary.json.
Called after each project run.

Usage:
    python record_project.py <output_file> <project_id> <repo> <size_kb> \
        <exit_code> <initial> <accepted> <nr> <rejected> <final> <status>
"""
import sys, json, datetime, pathlib
from parse_agent_output import parse_output

BASE = pathlib.Path(__file__).parent

def record(output_file, project_id, repo, size_kb,
           exit_code, initial, accepted, nr, rejected, final, status):
    ts = datetime.datetime.now().isoformat()

    # Parse stdout for per-code stats
    try:
        text = pathlib.Path(output_file).read_text(encoding="utf-8", errors="replace")
        code_stats = parse_output(text)
    except Exception as e:
        code_stats = {"error": str(e)}

    # Classification
    pipeline_healthy = (exit_code == 0 and status != "UNKNOWN")
    if not pipeline_healthy or (initial > 0 and accepted == 0 and nr == 0 and rejected == 0):
        classification = "FAIL"
    elif pipeline_healthy and initial == 0:
        classification = "EMPTY"
    elif pipeline_healthy and initial > 0 and accepted == 0 and nr > 0:
        classification = "PARTIAL"
    else:
        classification = "PASS"

    score = accepted * 2.0 + nr * 0.5 + (1 if pipeline_healthy else -3)

    recs = [
        {"phase": "plan", "ts": ts, "project_id": project_id, "repo": repo,
         "source": "github", "size_kb": size_kb, "confidence": 0.90,
         "filter_result": "accepted"},
        {"phase": "execute", "ts": ts, "project_id": project_id,
         "exit_code": exit_code, "initial_error_count": initial,
         "accepted_patches": accepted, "needs_review_count": nr,
         "rejected_patches": rejected, "final_error_count": final,
         "status": status, "code_stats": code_stats},
        {"phase": "verify", "ts": ts, "project_id": project_id,
         "classification": classification, "score": score},
    ]

    decisions = BASE / "decisions.jsonl"
    with decisions.open("a", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # Update summary.json
    p = BASE / "summary.json"
    s = json.loads(p.read_text(encoding="utf-8"))
    s["total_projects"] += 1
    s["clones_deleted"]  += 1
    s[classification.lower()] += 1
    s["consecutive_fail"] = s["consecutive_fail"] + 1 if classification == "FAIL" else 0
    s["total_errors_found"]  += initial
    s["total_accepted"]      += accepted
    s["total_needs_review"]  += nr
    s["total_rejected"]      += rejected
    s["last_updated"] = ts
    denom = s["pass"] + s["partial"] + s["fail"]
    s["effective_pass_rate"] = round(s["pass"] / denom, 3) if denom else None
    p.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"[{s['total_projects']}/{s['series_n']}] {classification} "
          f"pass={s['pass']} partial={s['partial']} fail={s['fail']} "
          f"rate={s['effective_pass_rate']}")
    print(f"  code_stats keys: {list(code_stats.get('code_stats', {}).keys())}")
    return classification


if __name__ == "__main__":
    args = sys.argv[1:]
    output_file, project_id, repo = args[0], args[1], args[2]
    size_kb  = int(args[3])
    exit_code = int(args[4])
    initial, accepted, nr, rejected, final = [int(x) for x in args[5:10]]
    status = args[10]
    record(output_file, project_id, repo, size_kb,
           exit_code, initial, accepted, nr, rejected, final, status)
