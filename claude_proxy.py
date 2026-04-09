import json
import uuid
import logging
import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
import httpx
import uvicorn
import re
import sys
import os
import subprocess
from urllib.parse import urlparse

# Add scripts directory to path to import validate_progress
sys.path.append(os.path.join(os.path.dirname(__file__), "scripts"))
try:
    from validate_progress import validate_progress
except ImportError:
    validate_progress = None

try:
    from prompt_cleaner import clean_payload_system
except ImportError:
    logger.warning("prompt_cleaner module not found. Auto-cleaning prompts will be disabled.")
    clean_payload_system = lambda x: x

# ==============================================================================
# Configuration & Logging
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("claude_proxy")

MY_ADDR = os.getenv("ANTHROPIC_BASE_URL", "").rstrip("/")
if not MY_ADDR:
    logger.warning("ANTHROPIC_BASE_URL is not set. Claude may not send request here. Please set it, e.g.: export ANTHROPIC_BASE_URL=http://127.0.0.1:8000")
    MY_ADDR = "http://127.0.0.1:8000"

_parsed = urlparse(MY_ADDR)
LISTEN_PORT = _parsed.port
if LISTEN_PORT is None:
    logger.error("No port found in ANTHROPIC_BASE_URL (e.g., :8000)")
    sys.exit(1)

ANTHROPIC_TARGET_URL = os.getenv("ANTHROPIC_TARGET_URL", "").rstrip("/")

# Cleanup port before starting
def cleanup_port(port):
    try:
        pids = subprocess.check_output(["lsof", "-t", f"-i:{port}", "-sTCP:LISTEN"], stderr=subprocess.DEVNULL).decode().strip().split()
        for pid in pids:
            if pid:
                logger.info(f"Cleaning up existing process {pid} on port {port}...")
                subprocess.run(["kill", "-9", pid], check=False)
    except Exception:
        pass

cleanup_port(LISTEN_PORT)

def get_backend_url():
    """
    Determines the target LLM endpoint based on environment variables.
    Priority:
    1. ANTHROPIC_TARGET_URL (Explicit direct shoot - use this to bypass LiteLLM)
    2. litellm_backend_url (Default fallback for Archon smart routing)
    """
    if ANTHROPIC_TARGET_URL:
        target = f"{ANTHROPIC_TARGET_URL}/v1/messages"
        logger.info(f"Targeting explicit ANTHROPIC_TARGET_URL: {target} (Direct mode, Smart Routing model rewrite disabled)")
        return target, False # Not LiteLLM mode

    def get_litellm_port():
        try:
            pids = subprocess.check_output(["pgrep", "-f", "litellm"], stderr=subprocess.DEVNULL).decode().strip().split()
            if pids:
                pids_str = ",".join(pids)
                lsof_out = subprocess.check_output(["lsof", "-Pan", "-p", pids_str, "-iTCP", "-sTCP:LISTEN"], stderr=subprocess.DEVNULL).decode().strip()
                for line in lsof_out.splitlines()[1:]:
                    parts = line.split()
                    if len(parts) >= 9:
                        addr = parts[8]
                        if ':' in addr:
                            return int(addr.split(':')[-1])
        except Exception:
            pass
        return None
        
    litellm_port = get_litellm_port()
    if litellm_port is None:
        logger.warning("Neither env ANTHROPIC_TARGET_URL nor running litellm detected. I'm confused. Defaulting to litellm with port 4000.")
        litellm_port = 4000
    litellm_backend_url = f"http://127.0.0.1:{litellm_port}/v1/messages"
    
    logger.info(f"Using backend endpoint: {litellm_backend_url} (LiteLLM mode, Smart Routing model rewrite enabled)")
    return litellm_backend_url, True # LiteLLM mode

# TODO: allow user set backend url
backend_url, is_litellm_mode = get_backend_url()

try:
    from interceptor_logic import InterceptorLogic
except ImportError:
    from scripts.interceptor_logic import InterceptorLogic

interceptor = InterceptorLogic()

# ==============================================================================
# State Management
# ==============================================================================
class ProxySession:
    def __init__(self):
        self.pending_requests = {}  # req_id -> {"body": dict, "future": asyncio.Future()}
        self.lock = asyncio.Lock()
        self.auto_mode = True  # Track if auto-forward is enabled
        self.auto_clean_prompt = True  # Track if auto-clean prompt is enabled
        self.force_brain = False  # Track if force-brain is enabled
        self.forward_count = 0  # Track total LLM forwards
        self.token_usage = {
            "hand": {"input": 0, "output": 0},
            "brain": {"input": 0, "output": 0}
        }

    def reset(self):
        for req_id, req_data in self.pending_requests.items():
            if not req_data["future"].done():
                req_data["future"].cancel()
        self.pending_requests.clear()
        interceptor.reset()
        self.token_usage = {
            "hand": {"input": 0, "output": 0},
            "brain": {"input": 0, "output": 0}
        }
        logger.info("Session state has been fully reset.")

session = ProxySession()

# ==============================================================================
# FastAPI App
# ==============================================================================
app = FastAPI(title="Claude Code HITL Proxy")

def load_ui():
    ui_path = os.path.join(os.path.dirname(__file__), "proxy.html")
    if os.path.exists(ui_path):
        with open(ui_path, "r") as f:
            return f.read()
    return "<h1>proxy.html not found</h1>"

@app.get("/", response_class=HTMLResponse)
@app.get("/v1", response_class=HTMLResponse)
async def serve_ui():
    return load_ui()

@app.get("/api/state")
async def get_state():
    async with session.lock:
        return {
            "pending_requests": {
                req_id: req_data["body"] for req_id, req_data in session.pending_requests.items()
            },
            "id_mapping": interceptor.id_mapping,
            "auto_mode": session.auto_mode,
            "auto_clean_prompt": session.auto_clean_prompt,
            "force_brain": session.force_brain,
            "forward_count": session.forward_count,
            "token_usage": session.token_usage
        }

@app.post("/api/toggle_auto")
async def toggle_auto():
    async with session.lock:
        session.auto_mode = not session.auto_mode
        logger.info(f"Auto-Forward mode set to: {session.auto_mode}")
    return {"auto_mode": session.auto_mode}

@app.post("/api/toggle_auto_clean")
async def toggle_auto_clean():
    async with session.lock:
        session.auto_clean_prompt = not session.auto_clean_prompt
        logger.info(f"Auto-Clean Prompt mode set to: {session.auto_clean_prompt}")
    return {"auto_clean_prompt": session.auto_clean_prompt}

@app.post("/api/toggle_force_brain")
async def toggle_force_brain():
    async with session.lock:
        session.force_brain = not session.force_brain
        logger.info(f"Force-Brain mode set to: {session.force_brain}")
    return {"force_brain": session.force_brain}

@app.post("/api/reset")
async def api_reset():
    async with session.lock:
        session.reset()
    return {"status": "ok"}

@app.post("/api/forward")
async def api_forward(request: Request):
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return {"error": "Invalid JSON body"}
        
    req_id = data.get("req_id")
    if not req_id:
        return {"error": "Missing req_id"}
        
    res = await _internal_forward(req_id, apply_interception=False)
    return res

@app.post("/api/commit")
async def api_commit(request: Request):
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return {"error": "Invalid JSON body"}
        
    req_id = data.get("req_id")
    response_data = data.get("response")
    
    if not req_id or not response_data:
        return {"error": "Missing req_id or response"}
        
    # Apply validation interception to human commits too
    response_data = interceptor.intercept_and_validate_response(response_data)
        
    async with session.lock:
        req_data = session.pending_requests.get(req_id)
        if not req_data or req_data["future"].done():
            return {"error": "Request not found or already processed"}
            
        # Ensure it has a valid top-level ID (Claude expects this)
        if "id" not in response_data:
            response_data["id"] = f"msg_{uuid.uuid4().hex}"
            
        req_data["future"].set_result({"response": response_data})
        logger.info(f"Human has committed a response for Claude (req_id: {req_id}).")
        return {"status": "ok"}

async def background_auto_fetch(req_id: str, future: asyncio.Future):
    try:
        res = await _internal_forward(req_id, apply_interception=True)
        if not future.done():
            future.set_result(res)
    except Exception as e:
        if not future.done():
            future.set_result({"error": str(e), "status_code": 500})

# ==============================================================================
# Claude Code Interception Endpoints
# ==============================================================================
@app.post("/v1/messages")
@app.post("/messages")
async def intercept_claude_messages(request: Request):
    try:
        body = await request.json()
    except json.JSONDecodeError:
        logger.error("Claude sent invalid JSON")
        return JSONResponse(status_code=400, content={"error": {"message": "Invalid JSON"}})

    # Extract original headers to pass through
    original_headers = dict(request.headers)
    # Remove Host header as it will be set by httpx
    original_headers.pop("host", None)
    original_headers.pop("content-length", None)

    req_id = str(uuid.uuid4())
    logger.info(f"=== Intercepted Request from Claude Code (ID: {req_id}) ===")
    
    try:
        msg_count = len(body.get('messages', []))
        is_stream = body.get('stream', False)
        logger.info(f"Request Info: stream={is_stream}, msg_count={msg_count}, last_msg_role={body.get('messages', [{}])[-1].get('role')}")
    except Exception as e:
        logger.error(f"Error logging request info: {e}")
    
    is_auto = False
    async with session.lock:
        is_auto = session.auto_mode
        future = asyncio.Future()
        session.pending_requests[req_id] = {
            "body": body,
            "future": future,
            "headers": original_headers
        }

    if is_auto:
        logger.info(f"Auto-Forward mode is ON. Starting background fetch task for {req_id}...")
        asyncio.create_task(background_auto_fetch(req_id, future))
    else:
        logger.info(f"Waiting for human interaction for {req_id}...")

    is_stream_req = body.get('stream', False)

    if is_stream_req:
        # Give it a short time to fail fast (e.g. 400 Bad Request from LiteLLM)
        try:
            await asyncio.wait_for(asyncio.shield(future), timeout=0.5)
        except asyncio.TimeoutError:
            pass

        if future.done():
            res = future.result()
            if "error" in res:
                async with session.lock:
                    session.pending_requests.pop(req_id, None)
                return JSONResponse(
                    status_code=res.get("status_code", 500),
                    content={"type": "error", "error": {"type": "api_error", "message": res["error"]}}
                )

        async def stream_generator():
            try:
                while not future.done():
                    yield ": keep-alive\n\n"
                    try:
                        await asyncio.wait_for(asyncio.shield(future), timeout=2.0)
                    except asyncio.TimeoutError:
                        continue
                
                res = future.result()
                if "error" in res:
                    error_msg = res["error"]
                    logger.error(f"Error during stream generation for {req_id}: {error_msg}")
                    yield f'event: error\ndata: {json.dumps({"type": "error", "error": {"type": "api_error", "message": error_msg}})}\n\n'
                else:
                    response_data = res["response"]
                    logger.info(f"Returning response to Claude (stream) for {req_id}.")
                    for chunk in generate_sse_from_json(response_data):
                        yield chunk
            except asyncio.CancelledError:
                logger.info(f"Stream for {req_id} cancelled.")
            except Exception as e:
                error_msg = str(e)
                logger.error(f"Error during stream generation for {req_id}: {error_msg}")
                yield f'event: error\ndata: {json.dumps({"type": "error", "error": {"type": "api_error", "message": error_msg}})}\n\n'
            finally:
                async with session.lock:
                    session.pending_requests.pop(req_id, None)
                
        return StreamingResponse(stream_generator(), media_type="text/event-stream")
    else:
        try:
            res = await future
            if "error" in res:
                status_code = res.get("status_code", 500)
                return JSONResponse(
                    status_code=status_code, 
                    content={"type": "error", "error": {"type": "api_error", "message": res["error"]}}
                )
            else:
                response_data = res["response"]
                logger.info(f"Returning response to Claude (JSON) for {req_id}.")
                return JSONResponse(content=response_data)
        except asyncio.CancelledError:
            logger.info(f"Request {req_id} was cancelled.")
            return JSONResponse(status_code=500, content={"type": "error", "error": {"type": "api_error", "message": "Request cancelled by proxy"}})
        except Exception as e:
            return JSONResponse(status_code=500, content={"type": "error", "error": {"type": "api_error", "message": str(e)}})
        finally:
            async with session.lock:
                session.pending_requests.pop(req_id, None)

def generate_sse_from_json(json_obj):
    # Emit message_start
    yield f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {'id': json_obj.get('id', f'msg_{uuid.uuid4().hex[:16]}'), 'type': 'message', 'role': 'assistant', 'model': json_obj.get('model', 'claude-3-5-sonnet-20241022'), 'content': []}})}\n\n"
    
    for i, block in enumerate(json_obj.get('content', [])):
        if block['type'] == 'text':
            yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': i, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
            yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': i, 'delta': {'type': 'text_delta', 'text': block['text']}})}\n\n"
            yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': i})}\n\n"
        elif block['type'] == 'tool_use':
            yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': i, 'content_block': {'type': 'tool_use', 'id': block['id'], 'name': block['name'], 'input': {}}})}\n\n"
            yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': i, 'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])}})}\n\n"
            yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': i})}\n\n"
        else:
            logger.warning(f"Unknown content block type ignored during SSE generation: {block.get('type')}. Block data: {json.dumps(block)}")
            
    # message_delta and usage
    yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': json_obj.get('stop_reason', 'end_turn'), 'stop_sequence': None}, 'usage': json_obj.get('usage', {'output_tokens': 10})})}\n\n"
    yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"


def get_working_dir(payload):
    """Extracts 'Primary working directory' from the system prompt."""
    system_prompt = payload.get("system", "")
    if isinstance(system_prompt, list):
        system_prompt = " ".join([b.get("text", "") for b in system_prompt if b.get("type") == "text"])
    else:
        system_prompt = str(system_prompt)

    pwd_match = re.search(r"Primary working directory:\s*([^\n]+)", system_prompt)
    if pwd_match:
        return pwd_match.group(1).strip()
    return None

def is_brain_needed(payload):
    """
    Determines if 'BRAIN' mode (high-reasoning model) is needed based on current project state.
    Checks micro stage (plan/review) and macro stage (init).
    """
    project_dir = get_working_dir(payload)
    if not project_dir:
        return False
        
    state_dir = os.path.join(project_dir, ".archon")
    try:
        from validate_progress import get_micro_stage, get_progress
        
        # 1. Check micro stage from latest iter meta.json
        micro_stage = get_micro_stage(state_dir)
        if micro_stage in ["plan", "review"]:
            return True
        elif micro_stage == "prover":
            return False
            
        # 2. If no active micro stage, check macro stage from PROGRESS.md
        is_valid, large_stage = get_progress(state_dir)
        if is_valid and large_stage == "init":
            return True
            
    except Exception as e:
        logger.warning(f"Failed to determine brain need for {state_dir}: {e}")
        
    return False

# Internal helper for forwarding to LLM (used by both API and Auto mode)
async def _internal_forward(req_id: str, apply_interception: bool = True):
    async with session.lock:
        req_data = session.pending_requests.get(req_id)
        if not req_data:
            return {"error": "No pending request found"}
        payload = json.loads(json.dumps(req_data["body"]))
        original_headers = req_data.get("headers")
        session.forward_count += 1
        auto_clean = session.auto_clean_prompt
    
    # --- PROMPT CLEANING ---
    if "system" in payload and auto_clean:
        payload["system"] = clean_payload_system(payload["system"])
    
    # Stream mode drops tool_use.input for Gemini models in LiteLLM 1.83.4
    # See https://github.com/BerriAI/litellm/issues/25390
    payload["stream"] = False

    # --- ARCHON SMART ROUTING ---
    async with session.lock:
        force_brain = session.force_brain
    
    need_brain = is_brain_needed(payload)
    use_high_reasoning = force_brain or need_brain

    if is_litellm_mode:
        if use_high_reasoning:
            payload["model"] = "claude-opus-4-6"
        else:
            payload["model"] = "claude-haiku-4-5"
        
        logger.info(f"Smart Routing (LiteLLM): force_brain={force_brain}, need_brain={need_brain}, use_high_reasoning={use_high_reasoning}, selected_model={payload['model']}")
    else:
        logger.info(f"Direct/Relay mode: Brain needed={use_high_reasoning}, but skipping name rewrite. Using client model: {payload.get('model')}")
    # ----------------------------

    if "messages" in payload:
        for msg in payload["messages"]:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
                
            for block in content:
                block_type = block.get("type")
                if block_type == "tool_use":
                    c_id = block.get("id")
                    if c_id:
                        p_id = interceptor.claude_id_to_provider(c_id)
                        block["id"] = p_id
                elif block_type == "tool_result":
                    c_id = block.get("tool_use_id")
                    if c_id:
                        p_id = interceptor.claude_id_to_provider(c_id)
                        block["tool_use_id"] = p_id

    async with httpx.AsyncClient() as client:
        try:
            # Prepare headers for forwarding
            forward_headers = {"Content-Type": "application/json"}
            
            # Pass through original headers (API keys, custom headers, etc.)
            if original_headers:
                # Merge original headers, but we might want to sanitize some
                for k, v in original_headers.items():
                    if k.lower() not in ["content-length", "host", "content-type"]:
                        forward_headers[k] = v
            
            # Ensure an Authorization header exists for backend if not present
            if "authorization" not in {k.lower() for k in forward_headers.keys()}:
                forward_headers["Authorization"] = "Bearer sk-none"

            resp = await client.post(backend_url, json=payload, headers=forward_headers, timeout=600.0)
            
            if resp.status_code != 200:
                # Log evidence for debugging (especially 400 WebSearch errors)
                try:
                    with open("last_error_payload.json", "w") as f:
                        json.dump({
                            "request_payload": payload,
                            "response_text": resp.text,
                            "status_code": resp.status_code
                        }, f, indent=2)
                    logger.error(f"Backend Error {resp.status_code} logged to last_error_payload.json")
                except Exception as log_e:
                    logger.error(f"Failed to write error payload to disk: {log_e}")
                
                return {"error": f"HTTP {resp.status_code}: {resp.text}", "status_code": resp.status_code}
                
            data = resp.json()
            
            if "usage" in data:
                usage = data["usage"]
                # Gemini/Anthropic use input_tokens, OpenAI uses prompt_tokens
                prompt_tokens = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
                completion_tokens = usage.get("output_tokens") or usage.get("completion_tokens") or 0
                
                phase_key = "brain" if use_high_reasoning else "hand"
                
                async with session.lock:
                    session.token_usage[phase_key]["input"] += prompt_tokens
                    session.token_usage[phase_key]["output"] += completion_tokens
            
            if "content" in data:
                for block in data["content"]:
                    if block.get("type") == "tool_use":
                        p_id = block.get("id")
                        if p_id:
                            c_id = interceptor.provider_id_to_claude(p_id)
                            block["id"] = c_id

            # Apply validation interception if requested
            if apply_interception:
                data = interceptor.intercept_and_validate_response(data)

            return {"response": data}
            
        except Exception as e:
            return {"error": str(e)}


if __name__ == "__main__":
    logger.info(f"Starting Proxy Server on 127.0.0.1:{LISTEN_PORT}")
    uvicorn.run(app, host="127.0.0.1", port=LISTEN_PORT, log_level="warning")
