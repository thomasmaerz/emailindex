#!/usr/bin/env python3
"""
Unified Validation Runner for Email Intelligence System

Runs all validation tests and presents results in a clean, logical format:
1. Extraction Pipeline (email parsing + DB fidelity)
2. Attachment Pipeline (disk vs DB)
3. Vector/Embedding Pipeline (sqlite-vec + similarity search)

Usage:
    python tests/run_all_validations.py           # summary only
    python tests/run_all_validations.py --verbose # detailed output
    python tests/run_all_validations.py --json    # machine-readable
    python tests/run_all_validations.py --filter extraction  # run specific test
"""

import argparse
import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent.parent
LOG_DIR = BASE_DIR / "ingestion" / "logs"
MAILDIR = BASE_DIR / "maildir" / "cur"
DB_PATH = BASE_DIR / "db" / "emails.db"


def get_log_path() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return LOG_DIR / f"validation_{timestamp}.log"


def run_validation_script(script_name: str, verbose: bool = False) -> tuple[bool, str, dict]:
    script_path = BASE_DIR / "tests" / script_name
    
    if not script_path.exists():
        return False, f"Script not found: {script_name}", {}
    
    cmd = [sys.executable, str(script_path)]
    if verbose:
        cmd.append("--verbose")
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(BASE_DIR)
        )
        
        passed = result.returncode == 0
        
        output = result.stdout
        if result.stderr:
            output += "\nSTDERR:\n" + result.stderr
        
        return passed, output, {"returncode": result.returncode}
    
    except subprocess.TimeoutExpired:
        return False, "Validation timed out after 300s", {}
    except Exception as e:
        return False, f"Failed to run: {e}", {}


def run_extraction_validation(verbose: bool = False) -> tuple[bool, str, dict]:
    return run_validation_script("validate_extraction_pipeline.py", verbose)


def run_attachments_validation(verbose: bool = False) -> tuple[bool, str, dict]:
    return run_validation_script("validate_attachments.py", verbose)


def run_vector_validation(verbose: bool = False) -> tuple[bool, str, dict]:
    return run_validation_script("validate_issue4.py", verbose)


def format_summary(results: dict, verbose: bool = False) -> str:
    lines = []
    lines.append("")
    lines.append("Email Intelligence System - Validation Report")
    lines.append("=" * 60)
    lines.append(f"Run: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Database: {DB_PATH}")
    lines.append("")
    lines.append(f"{'Pipeline Phase':<25} {'Status':<8} {'Details'}")
    lines.append("-" * 60)
    
    status_icons = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭️", "ERROR": "⚠️"}
    
    for phase, data in results.items():
        status = data.get("status", "ERROR")
        icon = status_icons.get(status, "❓")
        details = data.get("details", "")
        
        lines.append(f"{phase:<25} {icon} {status:<6} {details}")
    
    lines.append("-" * 60)
    
    pass_count = sum(1 for r in results.values() if r.get("status") == "PASS")
    total = len(results)
    overall = "PASS" if pass_count == total else "FAIL"
    overall_icon = "✅" if overall == "PASS" else "❌"
    
    lines.append(f"{'Overall:':<25} {overall_icon} {overall} ({pass_count}/{total} checks)")
    lines.append("=" * 60)
    lines.append("")
    
    return "\n".join(lines)


def format_verbose_output(results: dict) -> str:
    lines = []
    lines.append("")
    lines.append("=" * 60)
    lines.append("DETAILED VALIDATION OUTPUT")
    lines.append("=" * 60)
    
    for phase, data in results.items():
        lines.append(f"\n### {phase} ###")
        lines.append("-" * 40)
        
        output = data.get("output", "")
        if output:
            lines.append(output)
        else:
            lines.append(f"Status: {data.get('status', 'UNKNOWN')}")
            lines.append(f"Details: {data.get('details', 'N/A')}")
    
    return "\n".join(lines)


def format_json_output(results: dict) -> str:
    output = {
        "timestamp": datetime.now().isoformat(),
        "database": str(DB_PATH),
        "overall": "PASS" if all(r.get("status") == "PASS" for r in results.values()) else "FAIL",
        "results": {}
    }
    
    for phase, data in results.items():
        output["results"][phase] = {
            "status": data.get("status", "ERROR"),
            "details": data.get("details", ""),
            "passed": data.get("status") == "PASS"
        }
    
    return json.dumps(output, indent=2)


def write_log_file(log_path: Path, results: dict, verbose: bool = False):
    with open(log_path, "w") as f:
        f.write(f"Validation Run Log\n")
        f.write(f"Generated: {datetime.now().isoformat()}\n")
        f.write(f"Database: {DB_PATH}\n")
        f.write(f"\n{'=' * 60}\n\n")
        
        f.write(format_summary(results, verbose=False))
        
        if verbose:
            f.write(format_verbose_output(results))


def check_prerequisites() -> Optional[str]:
    if not DB_PATH.exists():
        return f"Database not found: {DB_PATH}"
    if not MAILDIR.exists():
        return f"Maildir not found: {MAILDIR}"
    return None


def run_all_validations(
    filter_by: Optional[str] = None,
    verbose: bool = False,
    json_output: bool = False,
    log: bool = True
) -> dict:
    results = {}
    
    prereq_error = check_prerequisites()
    if prereq_error:
        return {
            "Extraction Pipeline": {"status": "ERROR", "details": prereq_error, "output": ""},
            "Attachment Pipeline": {"status": "ERROR", "details": prereq_error, "output": ""},
            "Vector Pipeline": {"status": "ERROR", "details": prereq_error, "output": ""}
        }
    
    phases = [
        ("Extraction Pipeline", lambda: run_extraction_validation(verbose)),
        ("Attachment Pipeline", lambda: run_attachments_validation(verbose)),
        ("Vector Pipeline", lambda: run_vector_validation(verbose))
    ]
    
    if filter_by:
        phases = [(name, func) for name, func in phases if filter_by.lower() in name.lower()]
    
    for phase_name, phase_func in phases:
        if verbose:
            print(f"\nRunning {phase_name}...", file=sys.stderr)
        
        passed, output, detail = phase_func()
        
        results[phase_name] = {
            "status": "PASS" if passed else "FAIL",
            "details": detail.get("returncode", 0) if not passed else "OK",
            "output": output
        }
    
    return results


def main():
    parser = argparse.ArgumentParser(description="Run all validation tests")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print detailed output")
    parser.add_argument("--json", "-j", action="store_true", help="Output as JSON")
    parser.add_argument("--no-log", action="store_true", help="Don't write log file")
    parser.add_argument("--filter", "-f", type=str, help="Run only specific test (extraction, attachments, vector)")
    args = parser.parse_args()
    
    results = run_all_validations(
        filter_by=args.filter,
        verbose=args.verbose,
        json_output=args.json,
        log=not args.no_log
    )
    
    if args.json:
        print(format_json_output(results))
    else:
        print(format_summary(results, verbose=args.verbose))
        
        if args.verbose:
            print(format_verbose_output(results))
    
    if not args.no_log:
        log_path = get_log_path()
        write_log_file(log_path, results, verbose=args.verbose)
        print(f"Log written to: {log_path}")
    
    all_passed = all(r.get("status") == "PASS" for r in results.values())
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
