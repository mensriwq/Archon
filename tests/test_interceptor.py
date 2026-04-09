import json
import asyncio
import os
import sys

# Ensure we can import from scripts/
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "scripts"))
try:
    from interceptor_logic import InterceptorLogic
except ImportError:
    # Fallback if scripts isn't in path
    sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
    from scripts.interceptor_logic import InterceptorLogic

CUR_DIR = os.path.dirname(os.path.abspath(__file__))
SAMPLE_JSON = os.path.join(CUR_DIR, "sample_response.json")

def test_interceptor_with_valid_progress():
    interceptor = InterceptorLogic()
    
    with open(SAMPLE_JSON, "r") as f:
        data = json.load(f)
        
    # Inject valid PROGRESS.md format
    data["content"][0]["input"]["file_path"] = "PROGRESS.md"
    data["content"][0]["input"]["content"] = "## Current Stage\ninit\n## Current Objectives\n1. TestObjective.lean\n"
    
    result = interceptor.intercept_and_validate_response(data)
    
    block = result["content"][0]
    assert block["name"] == "Write", f"Expected Write, got {block['name']}"
    print("test_interceptor_with_valid_progress: PASS")

def test_interceptor_with_invalid_progress():
    interceptor = InterceptorLogic()
    
    with open(SAMPLE_JSON, "r") as f:
        data = json.load(f)
        
    # Inject invalid PROGRESS.md format
    data["content"][0]["input"]["file_path"] = "PROGRESS.md"
    data["content"][0]["input"]["content"] = "## Current Stage: init\n## Current Objectives\n1. TestObjective.lean\n"
    
    result = interceptor.intercept_and_validate_response(data)
    
    block = result["content"][0]
    assert block["name"] == "Bash", f"Expected Bash, got {block['name']}"
    assert "VALIDATION ERROR" in block["input"]["command"], "Expected VALIDATION ERROR in bash command"
    assert "## Current Stage: init" in block["input"]["command"], "Expected original content in bash command"
    print("test_interceptor_with_invalid_progress: PASS")

def test_interceptor_ignores_other_files():
    interceptor = InterceptorLogic()
    
    with open(SAMPLE_JSON, "r") as f:
        data = json.load(f)
        
    # Write to a normal file
    data["content"][0]["input"]["file_path"] = "test.txt"
    data["content"][0]["input"]["content"] = "some random content"
    
    result = interceptor.intercept_and_validate_response(data)
    
    block = result["content"][0]
    assert block["name"] == "Write", f"Expected Write, got {block['name']}"
    print("test_interceptor_ignores_other_files: PASS")

if __name__ == "__main__":
    test_interceptor_with_valid_progress()
    test_interceptor_with_invalid_progress()
    test_interceptor_ignores_other_files()
    print("ALL TESTS PASSED")
