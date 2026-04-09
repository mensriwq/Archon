import sys
import os
import re

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "scripts"))

try:
    from prompt_cleaner import clean_system_prompt
except ImportError:
    sys.path.append(os.path.join(os.getcwd(), "scripts"))
    from prompt_cleaner import clean_system_prompt

def run_test():
    fixture_path = os.path.join(os.path.dirname(__file__), "sample_system_prompt.txt")
    
    if not os.path.exists(fixture_path):
        fixture_path = "tests/sample_system_prompt.txt"

    if not os.path.exists(fixture_path):
        print("FAIL: Fixture not found")
        sys.exit(1)

    with open(fixture_path, "r") as f:
        content = f.read()

    cleaned = clean_system_prompt(content)

    marker = "You are powered by the model named"
    if marker in cleaned:
        print("FAIL: Identity marker still present")
        sys.exit(1)

    if "# Environment" not in cleaned:
        print("FAIL: Oversalting - removed important sections")
        sys.exit(1)

    print("PASS")

if __name__ == "__main__":
    run_test()
