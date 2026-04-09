import sys
import os
import json
import re
import argparse

def validate_progress(content):
    # 1. Check for ## Current Stage
    if not re.search(r"^## Current Stage\s*$", content, re.MULTILINE):
        return False, "VALIDATION ERROR: Missing exact header '## Current Stage'. The header must be precisely '## Current Stage'."
    
    # Extract the stage
    # Look for the first non-empty line after ## Current Stage, before the next ##
    stage_match = re.search(r"^## Current Stage\s*\n+([^#\n][^\n]*)", content, re.MULTILINE)
    if not stage_match:
        return False, "VALIDATION ERROR: Missing stage value under '## Current Stage'."
    
    stage = stage_match.group(1).strip()
    valid_stages = {"init", "autoformalize", "prover", "polish", "COMPLETE"}
    if stage not in valid_stages:
        return False, f"VALIDATION ERROR: Invalid stage '{stage}'. Must be one of: {', '.join(valid_stages)} on its own line."

    # 2. Check for ## Current Objectives
    if not re.search(r"^## Current Objectives\s*$", content, re.MULTILINE):
        return False, "VALIDATION ERROR: Missing exact header '## Current Objectives'."

    # Extract objectives section
    objectives_match = re.search(r"^## Current Objectives\s*\n(.*?)(?=\n## |$)", content, re.MULTILINE | re.DOTALL)
    if objectives_match:
        objectives_text = objectives_match.group(1)
        # Extract lean files
        lean_files = re.findall(r"[a-zA-Z0-9_/-]+\.lean", objectives_text)
        
        # If the stage requires files but none are found, report an error
        if stage in {"autoformalize", "prover", "polish"} and not lean_files:
            return False, f"VALIDATION ERROR: Stage is '{stage}' but no .lean files were found under '## Current Objectives'. You must explicitly list target files (e.g. `Foo/Bar.lean`). If the project is fully complete, set the stage to 'COMPLETE'."
    
    return True, stage

def get_lean_files(content):
    objectives_match = re.search(r"^## Current Objectives\s*\n(.*?)(?=\n## |$)", content, re.MULTILINE | re.DOTALL)
    if objectives_match:
        objectives_text = objectives_match.group(1)
        lean_files = re.findall(r"[a-zA-Z0-9_/-]+\.lean", objectives_text)
        # Unique list preserving order
        seen = set()
        unique_files = [x for x in lean_files if not (x in seen or seen.add(x))]
        return unique_files
    return []

def get_progress(state_dir):
    progress_file = os.path.join(state_dir, "PROGRESS.md")
    if not os.path.exists(progress_file):
        return False, f"VALIDATION ERROR: File not found: {progress_file}"
    try:
        with open(progress_file, "r", encoding="utf-8") as f:
            content = f.read()
        return validate_progress(content)
    except Exception as e:
        return False, f"VALIDATION ERROR: Could not read {progress_file}: {e}"

def get_micro_stage(state_dir):
    logs_dir = os.path.join(state_dir, "logs")
    if not os.path.isdir(logs_dir):
        return None
    
    iter_dirs = [d for d in os.listdir(logs_dir) if d.startswith("iter-") and os.path.isdir(os.path.join(logs_dir, d))]
    if not iter_dirs:
        return None
        
    latest_iter = sorted(iter_dirs)[-1]
    meta_file = os.path.join(logs_dir, latest_iter, "meta.json")
    if not os.path.isfile(meta_file):
        return None
        
    try:
        with open(meta_file, 'r', encoding='utf-8') as f:
            meta = json.load(f)
            
        if meta.get('review', {}).get('status') == 'running':
            return 'review'
        elif meta.get('prover', {}).get('status') == 'running':
            return 'prover'
        elif meta.get('plan', {}).get('status') == 'running':
            return 'plan'
        return None
    except Exception:
        return None

def main():
    parser = argparse.ArgumentParser(description="Validate PROGRESS.md format.")
    parser.add_argument("file", help="Path to PROGRESS.md (or '-' to read from stdin)")
    parser.add_argument("--get-stage", action="store_true", help="If valid, print the current stage and exit")
    parser.add_argument("--get-files", action="store_true", help="If valid, print the objective .lean files and exit")
    parser.add_argument("--get-progress", action="store_true", help="Print macro and micro stages as JSON and exit")
    
    args = parser.parse_args()
    
    if args.file == '-':
        content = sys.stdin.read()
    else:
        try:
            with open(args.file, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            print(f"Error reading {args.file}: {e}", file=sys.stderr)
            sys.exit(1)
            
    is_valid, result = validate_progress(content)
    
    if not is_valid:
        if args.get_progress:
            state_dir = os.path.dirname(os.path.abspath(args.file)) if args.file != '-' else None
            micro_stage = get_micro_stage(state_dir) if state_dir else None
            print(json.dumps({
                "macro_stage": None,
                "micro_stage": micro_stage,
                "error": result
            }))
            sys.exit(1)
        else:
            print(result, file=sys.stderr)
            sys.exit(1)
        
    stage = result
    
    if args.get_progress:
        state_dir = os.path.dirname(os.path.abspath(args.file)) if args.file != '-' else None
        micro_stage = get_micro_stage(state_dir) if state_dir else None
        print(json.dumps({
            "macro_stage": stage,
            "micro_stage": micro_stage
        }))
    elif args.get_stage:
        print(stage)
    elif args.get_files:
        files = get_lean_files(content)
        for f in files:
            print(f)
    else:
        # Default behavior: silent success (exit 0)
        pass

if __name__ == "__main__":
    main()
