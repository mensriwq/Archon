import logging
import uuid
import sys
import os

sys.path.append(os.path.dirname(__file__))
try:
    from validate_progress import validate_progress
except ImportError:
    validate_progress = None

logger = logging.getLogger("claude_proxy.interceptor")

class InterceptorLogic:
    def __init__(self):
        self.id_mapping = {}

    def reset(self):
        self.id_mapping.clear()

    def claude_id_to_provider(self, claude_id: str) -> str:
        return self.id_mapping.get(claude_id, claude_id)

    def provider_id_to_claude(self, provider_id: str) -> str:
        for c_id, p_id in self.id_mapping.items():
            if p_id == provider_id:
                return c_id
        new_claude_id = f"toolu_{uuid.uuid4().hex[:20]}"
        self.id_mapping[new_claude_id] = provider_id
        logger.info(f"Created new ID mapping: Claude[{new_claude_id}] -> Provider[{provider_id}]")
        return new_claude_id

    def intercept_and_validate_response(self, response_data: dict) -> dict:
        if "content" not in response_data:
            return response_data
            
        for block in response_data["content"]:
            if block.get("type") == "tool_use":
                tool_name = block.get("name")
                if tool_name in ["Write", "Edit"]:
                    input_args = block.get("input", {})
                    file_path = input_args.get("file_path", "")
                    
                    if file_path.endswith("PROGRESS.md"):
                        logger.info(f"Interceptor: Inspecting tool {tool_name} modifying {file_path}")
                        new_content = None
                        validation_error = None

                        if tool_name == "Write":
                            new_content = input_args.get("content", "")
                        elif tool_name == "Edit":
                            old_string = input_args.get("old_string", "")
                            new_string = input_args.get("new_string", "")
                            try:
                                with open(file_path, "r", encoding="utf-8") as f:
                                    current_content = f.read()
                                if old_string in current_content:
                                    new_content = current_content.replace(old_string, new_string, 1)
                            except Exception as e:
                                logger.warning(f"Could not read {file_path} for Edit prediction: {e}")
                        
                        if new_content is not None and validate_progress:
                            is_valid, result = validate_progress(new_content)
                            if not is_valid:
                                validation_error = result
                        
                        if validation_error:
                            logger.warning(f"Interceptor: Blocked invalid PROGRESS.md update. Substituting with Bash error. Reason: {validation_error}")
                            block["name"] = "Bash"
                            
                            safe_content = new_content if new_content else "(empty content)"
                            safe_content = safe_content.replace("\nEOF\n", "\nE_O_F\n")
                            
                            bash_err = validation_error.replace("'", "'\\''")
                            
                            command = (
                                f": <<'EOF'\n"
                                f"[SYSTEM INTERCEPTOR]: Your Write/Edit tool call was invalid and has been blocked.\n"
                                f"To deliver this feedback safely, we intercepted your request and wrapped it into THIS diagnostic Bash command.\n\n"
                                f"Here is the exact content you attempted to write/edit:\n"
                                f"---\n"
                                f"{safe_content}\n"
                                f"---\n"
                                f"EOF\n\n"
                                f"echo 'VALIDATION FAILED FOR PROGRESS.md:' >&2\n"
                                f"echo '{bash_err}' >&2\n"
                                f"echo 'Please fix the markdown formatting according to the instructions and try again.' >&2\n"
                                f"exit 1"
                            )
                            
                            block["input"] = {
                                "command": command
                            }
        return response_data
