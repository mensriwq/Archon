import sys
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

def main():
    parser = argparse.ArgumentParser(description="Validate PROGRESS.md format.")
    parser.add_argument("file", help="Path to PROGRESS.md (or '-' to read from stdin)")
    parser.add_argument("--get-stage", action="store_true", help="If valid, print the current stage and exit")
    parser.add_argument("--get-files", action="store_true", help="If valid, print the objective .lean files and exit")
    
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
        print(result, file=sys.stderr)
        sys.exit(1)
        
    stage = result
    
    if args.get_stage:
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
