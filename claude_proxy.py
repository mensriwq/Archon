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

# Add scripts directory to path to import validate_progress
sys.path.append(os.path.join(os.path.dirname(__file__), "scripts"))
try:
    from validate_progress import validate_progress
except ImportError:
    validate_progress = None

# ==============================================================================
# Configuration & Logging
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("claude_proxy")

LITELLM_URL = "http://127.0.0.1:8001/v1/messages"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8000

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
        self.forward_count = 0  # Track total LLM forwards

    def reset(self):
        for req_id, req_data in self.pending_requests.items():
            if not req_data["future"].done():
                req_data["future"].cancel()
        self.pending_requests.clear()
        interceptor.reset()
        logger.info("Session state has been fully reset.")

session = ProxySession()

# ==============================================================================
# FastAPI App
# ==============================================================================
app = FastAPI(title="Claude Code HITL Proxy")

HTML_UI = """
<!DOCTYPE html>
<html>
<head>
    <title>Claude Code Web Proxy</title>
    <style>
        :root { --bg: #1e1e1e; --panel: #252526; --border: #333333; --text: #cccccc; --accent: #007acc; }
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 0; background: var(--bg); color: var(--text); display: flex; height: 100vh; overflow: hidden; }
        .sidebar { width: 400px; border-right: 1px solid var(--border); display: flex; flex-direction: column; background: var(--panel); }
        .main { flex: 1; display: flex; flex-direction: column; }
        .header { padding: 10px 20px; border-bottom: 1px solid var(--border); font-weight: bold; font-size: 14px; background: #2d2d2d; }
        .content { flex: 1; overflow-y: auto; padding: 20px; }
        .footer { padding: 20px; border-top: 1px solid var(--border); background: var(--panel); }
        
        pre { background: #1e1e1e; padding: 10px; border: 1px solid var(--border); border-radius: 4px; font-size: 12px; overflow-x: auto; white-space: pre-wrap; word-wrap: break-word; }
        .message { margin-bottom: 15px; border: 1px solid var(--border); border-radius: 4px; }
        .msg-header { padding: 5px 10px; font-weight: bold; font-size: 12px; border-bottom: 1px solid var(--border); }
        .msg-role-user { border-left: 4px solid #dcdcaa; }
        .msg-role-user .msg-header { background: rgba(220, 220, 170, 0.1); }
        .msg-role-assistant { border-left: 4px solid #4ec9b0; }
        .msg-role-assistant .msg-header { background: rgba(78, 201, 176, 0.1); }
        .msg-body { padding: 10px; font-size: 13px; font-family: monospace; }
        
        textarea { width: 100%; height: 200px; background: var(--bg); color: var(--text); border: 1px solid var(--border); padding: 10px; font-family: monospace; font-size: 13px; box-sizing: border-box; resize: vertical; }
        button { background: var(--accent); color: white; border: none; padding: 8px 16px; border-radius: 2px; cursor: pointer; font-size: 13px; font-weight: bold; margin-right: 10px; }
        button:hover { opacity: 0.9; }
        button.danger { background: #c53131; }
        
        .badge { padding: 3px 8px; border-radius: 10px; font-size: 11px; font-weight: bold; }
        .badge-idle { background: #555; color: white; }
        .badge-active { background: #c53131; color: white; }
        
        .req-item { display: block; width: 100%; text-align: left; padding: 8px; margin-bottom: 5px; background: #333; color: #ccc; border: 1px solid var(--border); cursor: pointer; border-radius: 4px; font-family: monospace; }
        .req-item:hover { background: #444; }
        .req-item.active { border-color: var(--accent); background: #2a2d3e; }
        .req-badge { font-size: 10px; padding: 2px 5px; background: #555; border-radius: 4px; float: right; }
    </style>
</head>
<body>
    <div class="sidebar">
        <div class="header">Session State (Multi-Agent)</div>
        <div class="content">
            <div style="margin-bottom:10px;">Status: <span id="status-badge" class="badge badge-idle">IDLE</span></div>
            <div style="margin-bottom:10px;">Forward Count: <span id="forward-count-badge" style="font-weight:bold; color:var(--accent);">0</span></div>
            <div style="margin-bottom:10px; display:flex; gap:10px;">
                <button id="btn-toggle-auto" onclick="toggleAutoMode()" style="flex:1; margin:0; background:#555;">Auto-Forward: OFF</button>
                <button class="danger" onclick="resetSession()" style="flex:1; margin:0;">Force Reset</button>
            </div>
            
            <div style="margin-top: 20px; margin-bottom: 5px; font-weight: bold; border-bottom: 1px solid var(--border); padding-bottom: 5px;">Pending Requests</div>
            <div id="request-list-view" style="max-height: 200px; overflow-y: auto; margin-bottom: 15px;">
                <div style="color: #666; font-size: 12px; font-style: italic;">No pending requests</div>
            </div>

            <h4>System Prompt</h4>
            <pre id="system-prompt-view" style="max-height:300px; overflow-y:auto;">None</pre>
            
            <details style="margin-top: 20px;">
                <summary style="cursor: pointer; font-weight: bold; margin-bottom: 5px;">ID Mapping (Claude -> Provider) ▾</summary>
                <pre id="id-map-view" style="max-height:200px; overflow-y:auto; margin-top: 5px;">{}</pre>
            </details>
        </div>
    </div>
    
    <div class="main">
        <div class="header">Conversation History (From Claude Code)</div>
        <div class="content" id="chat-history">
            <div style="color: #666; text-align: center; margin-top: 50px;">Waiting for Claude Code to send a request...</div>
        </div>
        
        <div class="footer">
            <div style="margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center;">
                <strong style="color: #4ec9b0;">LLM Response Override (JSON format)</strong>
                <span id="action-status" style="color: #dcdcaa; font-size: 12px;"></span>
            </div>
            <textarea id="llm-response-editor" placeholder="Click 'Fetch from LiteLLM' to fetch a response, or paste custom JSON here..."></textarea>
            <div style="margin-top: 10px;">
                <button id="btn-fetch" onclick="forwardRequest()">Fetch from LiteLLM (Provider)</button>
                <button id="btn-send" onclick="commitResponse()" style="background: #4ec9b0; color: #1e1e1e;">Send to Claude</button>
            </div>
        </div>
    </div>

    <script>
        let currentReqId = null;
        let lastReqsKeys = "";

        function updateAutoUIVisuals(isAuto) {
            const autoBtn = document.getElementById('btn-toggle-auto');
            const fetchBtn = document.getElementById('btn-fetch');
            const sendBtn = document.getElementById('btn-send');

            if (isAuto) {
                autoBtn.textContent = "Auto-Forward: ON";
                autoBtn.style.background = "#4ec9b0";
                fetchBtn.disabled = true;
                sendBtn.disabled = true;
                fetchBtn.style.opacity = "0.5";
                sendBtn.style.opacity = "0.5";
                fetchBtn.style.cursor = "not-allowed";
                sendBtn.style.cursor = "not-allowed";
            } else {
                autoBtn.textContent = "Auto-Forward: OFF";
                autoBtn.style.background = "#555";
                fetchBtn.disabled = false;
                sendBtn.disabled = false;
                fetchBtn.style.opacity = "1";
                sendBtn.style.opacity = "1";
                fetchBtn.style.cursor = "pointer";
                sendBtn.style.cursor = "pointer";
            }
        }

        async function pollState() {
            try {
                const res = await fetch('/api/state');
                const data = await res.json();
                
                document.getElementById('id-map-view').textContent = JSON.stringify(data.id_mapping, null, 2);
                document.getElementById('forward-count-badge').textContent = data.forward_count || 0;
                
                updateAutoUIVisuals(data.auto_mode);
                
                const pendingReqs = data.pending_requests || {};
                const keys = Object.keys(pendingReqs);
                const keysStr = keys.sort().join(',');
                
                const badge = document.getElementById('status-badge');
                if (keys.length > 0) {
                    badge.textContent = keys.length + " PENDING";
                    badge.className = "badge badge-active";
                } else {
                    badge.textContent = "IDLE";
                    badge.className = "badge badge-idle";
                }

                // Update request list UI if keys changed
                if (keysStr !== lastReqsKeys) {
                    lastReqsKeys = keysStr;
                    renderRequestList(keys);
                    
                    // Auto-select first request if none selected or selected is gone
                    if (!currentReqId || !keys.includes(currentReqId)) {
                        if (keys.length > 0) {
                            selectRequest(keys[0], pendingReqs[keys[0]]);
                        } else {
                            currentReqId = null;
                            document.getElementById('chat-history').innerHTML = '<div style="color: #666; text-align: center; margin-top: 50px;">Waiting for next request...</div>';
                            document.getElementById('system-prompt-view').textContent = "None";
                        }
                    }
                }
                
                // Keep chat updated if selected request content changes (unlikely but safe)
                if (currentReqId && pendingReqs[currentReqId]) {
                    // Update selection styling
                    document.querySelectorAll('.req-item').forEach(el => {
                        el.className = el.dataset.id === currentReqId ? 'req-item active' : 'req-item';
                    });
                }
                
            } catch (e) {
                console.error("Polling error:", e);
            }
            setTimeout(pollState, 1500);
        }
        
        function renderRequestList(keys) {
            const listDiv = document.getElementById('request-list-view');
            listDiv.innerHTML = '';
            if (keys.length === 0) {
                listDiv.innerHTML = '<div style="color: #666; font-size: 12px; font-style: italic;">No pending requests</div>';
                return;
            }
            keys.forEach(id => {
                const btn = document.createElement('button');
                btn.className = id === currentReqId ? 'req-item active' : 'req-item';
                btn.dataset.id = id;
                btn.innerHTML = `Req: ${id.substring(0,8)} <span class="req-badge">WAITING</span>`;
                btn.onclick = async () => {
                    const res = await fetch('/api/state');
                    const data = await res.json();
                    if (data.pending_requests && data.pending_requests[id]) {
                        selectRequest(id, data.pending_requests[id]);
                    }
                };
                listDiv.appendChild(btn);
            });
        }

        function selectRequest(id, reqBody) {
            currentReqId = id;
            document.querySelectorAll('.req-item').forEach(el => {
                el.className = el.dataset.id === currentReqId ? 'req-item active' : 'req-item';
            });
            renderChat(reqBody);
        }

        function renderChat(req) {
            document.getElementById('system-prompt-view').textContent = 
                typeof req.system === 'string' ? req.system : JSON.stringify(req.system, null, 2);
            
            const chatDiv = document.getElementById('chat-history');
            chatDiv.innerHTML = '';
            
            if (!req.messages) return;
            
            req.messages.forEach(m => {
                const wrapper = document.createElement('div');
                wrapper.className = `message msg-role-${m.role}`;
                
                const header = document.createElement('div');
                header.className = 'msg-header';
                header.textContent = m.role.toUpperCase();
                
                const body = document.createElement('div');
                body.className = 'msg-body';
                body.textContent = typeof m.content === 'string' ? m.content : JSON.stringify(m.content, null, 2);
                
                wrapper.appendChild(header);
                wrapper.appendChild(body);
                chatDiv.appendChild(wrapper);
            });
            
            chatDiv.scrollTop = chatDiv.scrollHeight;
        }

        async function forwardRequest() {
            if (!currentReqId) {
                alert("No active request selected.");
                return;
            }
            const statusNode = document.getElementById('action-status');
            statusNode.textContent = "Fetching from LLM...";
            try {
                const res = await fetch('/api/forward', { 
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({req_id: currentReqId})
                });
                const data = await res.json();
                if (data.error) {
                    alert("Error from LLM: " + data.error);
                    statusNode.textContent = "Failed.";
                } else {
                    document.getElementById('llm-response-editor').value = JSON.stringify(data.response, null, 2);
                    statusNode.textContent = "Ready to send.";
                }
            } catch (e) {
                alert(e);
                statusNode.textContent = "Error.";
            }
        }

        async function commitResponse() {
            if (!currentReqId) {
                alert("No active request selected.");
                return;
            }
            const statusNode = document.getElementById('action-status');
            const editor = document.getElementById('llm-response-editor');
            const content = editor.value.trim();
            
            if (!content) {
                alert("Response cannot be empty");
                return;
            }
            
            let parsed;
            try {
                parsed = JSON.parse(content);
            } catch (e) {
                // Auto-wrap plain text into Claude's expected format
                parsed = {
                    "role": "assistant",
                    "content": [{"type": "text", "text": content}],
                    "stop_reason": "end_turn"
                };
            }
            
            statusNode.textContent = "Sending to Claude...";
            try {
                await fetch('/api/commit', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        req_id: currentReqId,
                        response: parsed
                    })
                });
                editor.value = "";
                statusNode.textContent = "Sent successfully.";
                currentReqId = null; // Clear selection after commit
            } catch (e) {
                alert(e);
                statusNode.textContent = "Error sending.";
            }
        }

        async function resetSession() {
            if(confirm("Are you sure you want to reset the session and ID mappings?")) {
                await fetch('/api/reset', { method: 'POST' });
                document.getElementById('llm-response-editor').value = "";
                document.getElementById('action-status').textContent = "Session reset.";
            }
        }

        async function toggleAutoMode() {
            const res = await fetch('/api/toggle_auto', { method: 'POST' });
            const data = await res.json();
            updateAutoUIVisuals(data.auto_mode);
        }

        pollState();
    </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
@app.get("/v1", response_class=HTMLResponse)
async def serve_ui():
    return HTML_UI

@app.get("/api/state")
async def get_state():
    async with session.lock:
        return {
            "pending_requests": {
                req_id: req_data["body"] for req_id, req_data in session.pending_requests.items()
            },
            "id_mapping": interceptor.id_mapping,
            "auto_mode": session.auto_mode,
            "forward_count": session.forward_count
        }

@app.post("/api/toggle_auto")
async def toggle_auto():
    async with session.lock:
        session.auto_mode = not session.auto_mode
        logger.info(f"Auto-Forward mode set to: {session.auto_mode}")
    return {"auto_mode": session.auto_mode}

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
            "future": future
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
                    except asyncio.CancelledError:
                        break
                
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


def identify_agent_phase(payload):
    """
    Identifies the Archon agent phase using multiple fingerprints from the system prompt.
    Returns "BRAIN" for high-level reasoning (Plan/Review) or "HAND" for execution (Prover).
    """
    system_prompt = payload.get("system", "")
    if not isinstance(system_prompt, str):
        # Handle cases where system prompt might be a list of blocks
        if isinstance(system_prompt, list):
            system_prompt = " ".join([b.get("text", "") for b in system_prompt if b.get("type") == "text"])
        else:
            system_prompt = str(system_prompt)
    
    sp = system_prompt.lower()
    
    # 1. Plan Agent Fingerprints
    is_plan = "plan agent" in sp and ("prompts/plan.md" in sp or "prepare rich informal content" in sp)
    
    # 2. Review Agent Fingerprints
    is_review = "review agent" in sp and ("attempts_raw.jsonl" in sp or "structured proof journal" in sp)
    
    if is_plan or is_review:
        return "BRAIN"
    
    return "HAND"

# Internal helper for forwarding to LLM (used by both API and Auto mode)
async def _internal_forward(req_id: str, apply_interception: bool = True):
    async with session.lock:
        req_data = session.pending_requests.get(req_id)
        if not req_data:
            return {"error": "No pending request found"}
        payload = json.loads(json.dumps(req_data["body"]))
        session.forward_count += 1
    
    # Stream mode drops tool_use.input for Gemini models in LiteLLM 1.83.4
    # See https://github.com/BerriAI/litellm/issues/25390
    payload["stream"] = False

    # --- ARCHON SMART ROUTING ---
    if identify_agent_phase(payload) == "BRAIN":
        payload["model"] = "claude-opus-4-6"
    else:
        payload["model"] = "claude-haiku-4-5"
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
            headers = {"Content-Type": "application/json", "Authorization": "Bearer sk-none"}
            resp = await client.post(LITELLM_URL, json=payload, headers=headers, timeout=600.0)
            
            if resp.status_code != 200:
                # Log evidence for debugging (especially 400 WebSearch errors)
                try:
                    with open("last_error_payload.json", "w") as f:
                        json.dump({
                            "request_payload": payload,
                            "response_text": resp.text,
                            "status_code": resp.status_code
                        }, f, indent=2)
                    logger.error(f"LiteLLM Error {resp.status_code} logged to last_error_payload.json")
                except Exception as log_e:
                    logger.error(f"Failed to write error payload to disk: {log_e}")
                
                return {"error": f"HTTP {resp.status_code}: {resp.text}", "status_code": resp.status_code}
                
            data = resp.json()
            
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
    logger.info(f"Starting Clean Proxy Server on {LISTEN_HOST}:{LISTEN_PORT}")
    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT, log_level="warning")
