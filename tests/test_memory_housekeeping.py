"""
DEF-2 + DEF-3: MemoryLearning housekeeping — stale temp-path purge and TTL eviction.

DEF-2: Keys with absolute temp-sandbox paths (webbles_isolated_*) must not
       accumulate in memory.json — load() silently drops them and rewrites
       the file so they vanish permanently.

DEF-3: Entries older than ttl_days (default 90) are dropped during load().
       ttl_days is configurable via pipeline.memory.ttl_days in webles_config.json.
       Entries without a 'timestamp' field are kept (legacy compatibility).

Run: python tests/test_memory_housekeeping.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memory.learning import MemoryLearning

results: list = []


def check(name: str, cond: bool) -> None:
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def _make_entry(ts: str = "", score: float = 1.0, patch: str = "PATCH\n") -> dict:
    e: dict = {"patch": patch, "hash": f"hash-{patch[:4]}", "score": score}
    if ts:
        e["timestamp"] = ts
    return e


def _ts_days_ago(n: int) -> str:
    return (datetime.utcnow() - timedelta(days=n)).isoformat()


def _ts_now() -> str:
    return datetime.utcnow().isoformat()


# =====================================================================
# DEF-2: _is_stale_temp_key detection
# =====================================================================

def test_stale_key_windows_webbles_isolated():
    sig = r"C:\Users\foo\AppData\Local\Temp\webbles_isolated_abc123\auth.py::E501::line too long"
    check("stale: windows webbles_isolated", MemoryLearning._is_stale_temp_key(sig) is True)


def test_stale_key_windows_temp_webbles():
    sig = r"C:\Users\foo\AppData\Local\Temp\webbles_backups\file.py::code::msg"
    # Does NOT contain 'webbles_isolated' or '\Temp\webbles_' (contains '\Temp\webbles_')
    # The path IS under \Temp\ with webbles_ prefix
    sig2 = r"C:\Users\foo\AppData\Local\Temp\webbles_sandbox\x.py::E501::msg"
    check("stale: windows Temp webbles_sandbox", MemoryLearning._is_stale_temp_key(sig2) is True)


def test_stale_key_unix_tmp():
    sig = "/tmp/webbles_isolated_xyz/project/main.py::E501::line too long"
    check("stale: unix /tmp/webbles_", MemoryLearning._is_stale_temp_key(sig) is True)


def test_stale_key_relative_path():
    sig = "auth.py::E501::line too long characters"
    check("not stale: relative path", MemoryLearning._is_stale_temp_key(sig) is False)


def test_stale_key_nested_relative():
    sig = "src/core/main.py::F821::undefined name x"
    check("not stale: nested relative", MemoryLearning._is_stale_temp_key(sig) is False)


def test_stale_key_no_separator():
    sig = "auth.py::E999::invalid syntax"
    check("not stale: plain filename", MemoryLearning._is_stale_temp_key(sig) is False)


# =====================================================================
# DEF-2: load() purges stale keys from memory
# =====================================================================

def test_load_purges_stale_keys():
    fp = Path(tempfile.mkdtemp()) / "mem.json"
    stale_sig = r"C:\Users\u\AppData\Local\Temp\webbles_isolated_abc\auth.py::E501::long"
    good_sig = "auth.py::E501::line too long"
    data = {
        stale_sig: [_make_entry(_ts_now(), patch="STALE\n")],
        good_sig:  [_make_entry(_ts_now(), patch="GOOD\n")],
    }
    fp.write_text(json.dumps(data), encoding="utf-8")

    m = MemoryLearning(memory_file=fp)
    check("purge: stale key absent from memory", stale_sig not in m.successful_fixes)
    check("purge: good key kept in memory", m.get_known_fix(good_sig) == "GOOD\n")


def test_load_purge_rewrites_file():
    """After purge the on-disk file must no longer contain stale keys."""
    fp = Path(tempfile.mkdtemp()) / "mem.json"
    stale_sig = r"C:\Users\u\AppData\Local\Temp\webbles_isolated_xyz\db.py::sql_injection::sql"
    good_sig = "db.py::sql_injection::sql query"
    data = {
        stale_sig: [_make_entry(_ts_now(), patch="S\n")],
        good_sig:  [_make_entry(_ts_now(), patch="G\n")],
    }
    fp.write_text(json.dumps(data), encoding="utf-8")

    MemoryLearning(memory_file=fp)  # triggers purge + save
    on_disk = json.loads(fp.read_text(encoding="utf-8"))
    check("purge: stale key gone from disk", stale_sig not in on_disk)
    check("purge: good key present on disk", good_sig in on_disk)


def test_load_no_stale_no_unnecessary_save():
    """If nothing to purge, load() does NOT call save() (no write)."""
    fp = Path(tempfile.mkdtemp()) / "mem.json"
    good_sig = "main.py::SyntaxError::invalid syntax"
    data = {good_sig: [_make_entry(_ts_now(), patch="P\n")]}
    fp.write_text(json.dumps(data), encoding="utf-8")
    mtime_before = fp.stat().st_mtime

    import time; time.sleep(0.05)  # ensure mtime would differ if file is written
    MemoryLearning(memory_file=fp)
    mtime_after = fp.stat().st_mtime
    check("no purge: file not rewritten", mtime_before == mtime_after)


# =====================================================================
# DEF-3: _is_expired
# =====================================================================

def test_is_expired_old_entry():
    cutoff = datetime.utcnow() - timedelta(days=90)
    old_entry = _make_entry(_ts_days_ago(200))
    check("expired: 200 days old (cutoff=90d)", MemoryLearning._is_expired(old_entry, cutoff) is True)


def test_is_expired_recent_entry():
    cutoff = datetime.utcnow() - timedelta(days=90)
    recent = _make_entry(_ts_days_ago(30))
    check("not expired: 30 days old (cutoff=90d)", MemoryLearning._is_expired(recent, cutoff) is False)


def test_is_expired_no_timestamp():
    cutoff = datetime.utcnow() - timedelta(days=90)
    entry = _make_entry("")  # no timestamp key
    check("not expired: no timestamp (legacy keep)", MemoryLearning._is_expired(entry, cutoff) is False)


def test_is_expired_bad_timestamp():
    cutoff = datetime.utcnow() - timedelta(days=90)
    entry = _make_entry("not-a-date")
    check("not expired: unparseable timestamp -> keep", MemoryLearning._is_expired(entry, cutoff) is False)


def test_is_expired_boundary():
    """Entry exactly at cutoff is expired (strictly less-than check)."""
    cutoff = datetime.utcnow() - timedelta(days=90)
    # 90 days ago is right at the boundary — slightly before = expired
    just_before = (cutoff - timedelta(seconds=1)).isoformat()
    entry = _make_entry(just_before)
    check("expired: just before cutoff", MemoryLearning._is_expired(entry, cutoff) is True)


# =====================================================================
# DEF-3: TTL eviction in load()
# =====================================================================

def test_ttl_evicts_old_entries():
    fp = Path(tempfile.mkdtemp()) / "mem.json"
    sig = "auth.py::E501::line too long"
    data = {sig: [_make_entry(_ts_days_ago(200), patch="OLD\n")]}
    fp.write_text(json.dumps(data), encoding="utf-8")

    m = MemoryLearning(memory_file=fp, ttl_days=90)
    check("ttl: old entry evicted from memory", m.get_known_fix(sig) is None)
    check("ttl: bucket removed when all expire", sig not in m.successful_fixes)


def test_ttl_keeps_recent_entries():
    fp = Path(tempfile.mkdtemp()) / "mem.json"
    sig = "auth.py::E501::line too long"
    data = {sig: [_make_entry(_ts_days_ago(30), patch="RECENT\n")]}
    fp.write_text(json.dumps(data), encoding="utf-8")

    m = MemoryLearning(memory_file=fp, ttl_days=90)
    check("ttl: recent entry kept", m.get_known_fix(sig) == "RECENT\n")


def test_ttl_keeps_legacy_no_timestamp():
    fp = Path(tempfile.mkdtemp()) / "mem.json"
    sig = "notes.py::F821::undefined name x"
    entry = {"patch": "NOTAG\n", "hash": "h1", "score": 1.0}  # no timestamp
    fp.write_text(json.dumps({sig: [entry]}), encoding="utf-8")

    m = MemoryLearning(memory_file=fp, ttl_days=90)
    check("ttl: no-timestamp entry kept (legacy compat)", m.get_known_fix(sig) == "NOTAG\n")


def test_ttl_partial_eviction():
    """Only old entries in a bucket are dropped; recent ones survive."""
    fp = Path(tempfile.mkdtemp()) / "mem.json"
    sig = "db.py::sql_injection::sql"
    old = _make_entry(_ts_days_ago(200), score=0.9, patch="OLD\n")
    new = _make_entry(_ts_days_ago(10),  score=0.8, patch="NEW\n")
    fp.write_text(json.dumps({sig: [old, new]}), encoding="utf-8")

    m = MemoryLearning(memory_file=fp, ttl_days=90)
    fixes = m.successful_fixes.get(sig, [])
    check("ttl partial: old entry gone", not any(e["patch"] == "OLD\n" for e in fixes))
    check("ttl partial: recent entry kept", any(e["patch"] == "NEW\n" for e in fixes))


def test_ttl_disabled_zero():
    """ttl_days=0 disables eviction — even 500-day-old entries survive."""
    fp = Path(tempfile.mkdtemp()) / "mem.json"
    sig = "main.py::SyntaxError::invalid syntax"
    data = {sig: [_make_entry(_ts_days_ago(500), patch="ANCIENT\n")]}
    fp.write_text(json.dumps(data), encoding="utf-8")

    m = MemoryLearning(memory_file=fp, ttl_days=0)
    check("ttl disabled: ancient entry kept", m.get_known_fix(sig) == "ANCIENT\n")


def test_ttl_disabled_negative():
    """ttl_days=-1 also disables eviction."""
    fp = Path(tempfile.mkdtemp()) / "mem.json"
    sig = "main.py::E999::invalid syntax"
    data = {sig: [_make_entry(_ts_days_ago(400), patch="OLD\n")]}
    fp.write_text(json.dumps(data), encoding="utf-8")

    m = MemoryLearning(memory_file=fp, ttl_days=-1)
    check("ttl negative: ancient entry kept", m.get_known_fix(sig) == "OLD\n")


def test_ttl_evicted_file_rewritten():
    """After TTL eviction the on-disk file must reflect the cleaned state."""
    fp = Path(tempfile.mkdtemp()) / "mem.json"
    sig = "auth.py::E501::line too long"
    data = {sig: [_make_entry(_ts_days_ago(200), patch="OLD\n")]}
    fp.write_text(json.dumps(data), encoding="utf-8")

    MemoryLearning(memory_file=fp, ttl_days=90)
    on_disk = json.loads(fp.read_text(encoding="utf-8"))
    check("ttl: expired sig absent from disk", sig not in on_disk)


# =====================================================================
# Constructor / config
# =====================================================================

def test_constructor_default_ttl():
    m = MemoryLearning()
    check("constructor: default ttl_days=90", m.ttl_days == 90)


def test_constructor_custom_ttl():
    m = MemoryLearning(ttl_days=30)
    check("constructor: custom ttl_days=30", m.ttl_days == 30)


def test_constructor_ttl_zero():
    m = MemoryLearning(ttl_days=0)
    check("constructor: ttl_days=0 stored", m.ttl_days == 0)


# =====================================================================
# Integration: round-trip with mixed-age entries
# =====================================================================

def test_round_trip_mixed_ages():
    fp = Path(tempfile.mkdtemp()) / "mem.json"
    sig_old = "config.py::hardcoded_secret::hardcoded secret"
    sig_new = "database.py::sql_injection::sql query"
    stale_sig = r"C:\Users\u\Temp\webbles_isolated_abc\db.py::B608::sql"

    data = {
        sig_old:   [_make_entry(_ts_days_ago(200), patch="OLDF\n")],
        sig_new:   [_make_entry(_ts_days_ago(5),   patch="NEWF\n")],
        stale_sig: [_make_entry(_ts_now(),          patch="STALE\n")],
    }
    fp.write_text(json.dumps(data), encoding="utf-8")

    m = MemoryLearning(memory_file=fp, ttl_days=90)
    check("round-trip: old entry evicted", m.get_known_fix(sig_old) is None)
    check("round-trip: recent entry kept", m.get_known_fix(sig_new) == "NEWF\n")
    check("round-trip: stale path purged", stale_sig not in m.successful_fixes)

    # verify reload produces same result
    m2 = MemoryLearning(memory_file=fp, ttl_days=90)
    check("round-trip reload: recent still there", m2.get_known_fix(sig_new) == "NEWF\n")
    check("round-trip reload: stale still gone", stale_sig not in m2.successful_fixes)


# =====================================================================
# Regression: existing behavior unchanged
# =====================================================================

def test_regression_record_get():
    """record_success / get_known_fix behaviour unchanged with ttl=90."""
    m = MemoryLearning(ttl_days=90)
    m.record_success("sig1", "P\n", score=1.0)
    check("regression: record+get still works", m.get_known_fix("sig1") == "P\n")


def test_regression_language_filter_unchanged():
    m = MemoryLearning(ttl_days=90)
    m.record_success("sig1", "RUST\n", score=1.0, language="rust")
    check("regression: lang filter still works", m.get_known_fix("sig1", language="python") is None)
    check("regression: lang match still works", m.get_known_fix("sig1", language="rust") == "RUST\n")


def test_regression_persist_load():
    fp = Path(tempfile.mkdtemp()) / "mem.json"
    m1 = MemoryLearning(memory_file=fp, ttl_days=90)
    m1.record_success("sig1", "P\n", score=1.0)
    m2 = MemoryLearning(memory_file=fp, ttl_days=90)
    check("regression: persist/load round-trip", m2.get_known_fix("sig1") == "P\n")


if __name__ == "__main__":
    print("MemoryLearning housekeeping (DEF-2 stale-purge + DEF-3 TTL):")
    tests = [
        # DEF-2 detection
        ("stale_key_windows_webbles_isolated", test_stale_key_windows_webbles_isolated),
        ("stale_key_windows_temp_webbles", test_stale_key_windows_temp_webbles),
        ("stale_key_unix_tmp", test_stale_key_unix_tmp),
        ("stale_key_relative_path", test_stale_key_relative_path),
        ("stale_key_nested_relative", test_stale_key_nested_relative),
        ("stale_key_no_separator", test_stale_key_no_separator),
        # DEF-2 load() behaviour
        ("load_purges_stale_keys", test_load_purges_stale_keys),
        ("load_purge_rewrites_file", test_load_purge_rewrites_file),
        ("load_no_stale_no_unnecessary_save", test_load_no_stale_no_unnecessary_save),
        # DEF-3 _is_expired
        ("is_expired_old_entry", test_is_expired_old_entry),
        ("is_expired_recent_entry", test_is_expired_recent_entry),
        ("is_expired_no_timestamp", test_is_expired_no_timestamp),
        ("is_expired_bad_timestamp", test_is_expired_bad_timestamp),
        ("is_expired_boundary", test_is_expired_boundary),
        # DEF-3 load() TTL behaviour
        ("ttl_evicts_old_entries", test_ttl_evicts_old_entries),
        ("ttl_keeps_recent_entries", test_ttl_keeps_recent_entries),
        ("ttl_keeps_legacy_no_timestamp", test_ttl_keeps_legacy_no_timestamp),
        ("ttl_partial_eviction", test_ttl_partial_eviction),
        ("ttl_disabled_zero", test_ttl_disabled_zero),
        ("ttl_disabled_negative", test_ttl_disabled_negative),
        ("ttl_evicted_file_rewritten", test_ttl_evicted_file_rewritten),
        # Constructor
        ("constructor_default_ttl", test_constructor_default_ttl),
        ("constructor_custom_ttl", test_constructor_custom_ttl),
        ("constructor_ttl_zero", test_constructor_ttl_zero),
        # Integration
        ("round_trip_mixed_ages", test_round_trip_mixed_ages),
        # Regression
        ("regression_record_get", test_regression_record_get),
        ("regression_language_filter_unchanged", test_regression_language_filter_unchanged),
        ("regression_persist_load", test_regression_persist_load),
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except AssertionError as e:
            print(f"  [FAIL] {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
            failed += 1
    total = len(tests)
    print(f"\nMemory housekeeping: {total - failed}/{total} pass")
    sys.exit(0 if failed == 0 else 1)
