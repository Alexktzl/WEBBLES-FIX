"""
Parse run_agent.py stdout → per-code and per-source stats for audit.

Usage:
    python parse_agent_output.py <output_file>
    python parse_agent_output.py < output.txt
"""
import re, json, sys
from collections import defaultdict
from pathlib import Path

PATCH_RE  = re.compile(r"\[patch_proposed\].*?'code':\s*'([^']+)'.*?'patch_source':\s*'([^']+)'")
VERDICT_RE = re.compile(r"вердикт:\s*(ACCEPT|NEEDS_REVIEW)")
REJECT_RE  = re.compile(r"Патч ОТКЛОНЁН.*?\(причина:\s*([^)]+)\)")
TOOK_RE    = re.compile(r"взял\s+(\S+)\s+@")


def parse_output(text: str) -> dict:
    code_stats   = defaultdict(lambda: {"accept": 0, "nr": 0, "reject": 0})
    source_stats = defaultdict(lambda: {"accept": 0, "nr": 0, "reject": 0})
    reject_reasons = defaultdict(int)
    codes_seen   = defaultdict(int)

    current_code   = None
    current_source = None

    for line in text.splitlines():
        # Track every code the pipeline picked up
        m = TOOK_RE.search(line)
        if m:
            codes_seen[m.group(1)] += 1

        # New patch proposed
        m = PATCH_RE.search(line)
        if m:
            current_code   = m.group(1)
            current_source = m.group(2)
            continue

        # Verdict for current patch
        m = VERDICT_RE.search(line)
        if m and current_code:
            verdict = m.group(1)
            if verdict == "ACCEPT":
                code_stats[current_code]["accept"]   += 1
                source_stats[current_source]["accept"] += 1
            else:
                code_stats[current_code]["nr"]       += 1
                source_stats[current_source]["nr"]   += 1
            current_code = current_source = None
            continue

        # Rejection line (reason available, code not always)
        m = REJECT_RE.search(line)
        if m:
            reason = m.group(1).strip()
            reject_reasons[reason] += 1
            if current_source:
                source_stats[current_source]["reject"] += 1
            current_code = current_source = None

    return {
        "code_stats":     dict(code_stats),
        "source_stats":   dict(source_stats),
        "reject_reasons": dict(reject_reasons),
        "codes_seen":     dict(codes_seen),
    }


if __name__ == "__main__":
    if len(sys.argv) > 1:
        text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
    else:
        text = sys.stdin.read()
    print(json.dumps(parse_output(text), ensure_ascii=False, indent=2))
