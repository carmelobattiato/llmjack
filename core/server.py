#!/usr/bin/env python3
"""
FastAPI server — OpenAI-compatible proxy.
Avviato da llmjack.py che pre-imposta _config e _config_ready prima di uvicorn.run().
"""
import asyncio
import concurrent.futures
import json
import queue as _stdlib_queue
import re
import threading
import time
import uuid
from pathlib import Path

import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

import clients.qwen_client as qwen_module
from clients.qwen_client import QwenClient, SESSION_FILE as QWEN_SESSION_FILE
import clients.deepseek_client as ds_module
from clients.deepseek_client import DeepSeekClient, SESSION_FILE as DS_SESSION_FILE
import clients.chatgpt_client as gpt_module
from clients.chatgpt_client import ChatGPTClient, SESSION_FILE as GPT_SESSION_FILE
import clients.claude_client as claude_module
from clients.claude_client import ClaudeAIClient, SESSION_FILE as CLAUDE_SESSION_FILE

from core.config import (
    load_config, build_model_index,
    default_model as _cfg_default_model,
    provider_for as _cfg_provider_for,
)
from core.log_manager import log, vlog, elog, tlog

# ── runtime state ─────────────────────────────────────────────────────────────

VERBOSE = False
_config: dict = {}
_model_map: dict[str, str] = {}
_model_meta: dict[str, dict] = {}

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

_qwen_client: QwenClient | None = None
_qwen_lock = threading.Lock()
_ds_client: DeepSeekClient | None = None
_ds_lock = threading.Lock()
_gpt_client: ChatGPTClient | None = None
_gpt_lock = threading.Lock()
_claude_client: ClaudeAIClient | None = None
_claude_lock = threading.Lock()

_busy_lock: asyncio.Lock | None = None
_config_ready: bool = False   # True quando llmjack.py ha già impostato _config


# ── client getters ────────────────────────────────────────────────────────────

def _get_qwen_client(model: str | None = None) -> QwenClient:
    global _qwen_client
    with _qwen_lock:
        if _qwen_client is None:
            sid = QWEN_SESSION_FILE.read_text().strip() if QWEN_SESSION_FILE.exists() else None
            qwen_model = model or _config.get("providers", {}).get("qwen", {}).get("default_model")
            _qwen_client = QwenClient(session_id=sid, model=qwen_model, echo=VERBOSE)
        return _qwen_client


def _get_ds_client() -> DeepSeekClient:
    global _ds_client
    with _ds_lock:
        if _ds_client is None:
            sid = DS_SESSION_FILE.read_text().strip() if DS_SESSION_FILE.exists() else None
            _ds_client = DeepSeekClient(session_id=sid, echo=VERBOSE)
        return _ds_client


def _get_gpt_client(model: str | None = None) -> ChatGPTClient:
    global _gpt_client
    with _gpt_lock:
        if _gpt_client is None:
            sid = GPT_SESSION_FILE.read_text().strip() if GPT_SESSION_FILE.exists() else None
            pcfg = _config.get("providers", {}).get("chatgpt", {})
            enabled_models = [mid for mid, mcfg in pcfg.get("models", {}).items() if mcfg.get("enabled")]
            default = pcfg.get("default_model", "auto")
            if default not in enabled_models and enabled_models:
                default = enabled_models[0]
            gpt_model = model if model in enabled_models else default
            _gpt_client = ChatGPTClient(session_id=sid, model=gpt_model, echo=VERBOSE)
        elif model and _gpt_client._model != model:
            _gpt_client._model = model
        return _gpt_client


def _get_claude_client(model: str | None = None) -> ClaudeAIClient:
    global _claude_client
    with _claude_lock:
        if _claude_client is None:
            sid = CLAUDE_SESSION_FILE.read_text().strip() if CLAUDE_SESSION_FILE.exists() else None
            pcfg = _config.get("providers", {}).get("claude", {})
            enabled_models = [mid for mid, mcfg in pcfg.get("models", {}).items() if mcfg.get("enabled")]
            default = pcfg.get("default_model", "claude-sonnet-4-6")
            if default not in enabled_models and enabled_models:
                default = enabled_models[0]
            claude_model = model if model in enabled_models else default
            _claude_client = ClaudeAIClient(session_id=sid, model=claude_model, echo=VERBOSE)
        elif model and _claude_client._model != model:
            _claude_client._model = model
        return _claude_client


def _client_for(provider: str, model: str | None = None):
    if provider == "deepseek":
        return _get_ds_client()
    if provider == "chatgpt":
        chatgpt_models = set(_config.get("providers", {}).get("chatgpt", {}).get("models", {}).keys())
        gpt_model = model if model in chatgpt_models else None
        return _get_gpt_client(gpt_model)
    if provider == "claude":
        claude_models = set(_config.get("providers", {}).get("claude", {}).get("models", {}).keys())
        claude_model = model if model in claude_models else None
        return _get_claude_client(claude_model)
    return _get_qwen_client(model)


# ── sync ask helpers ──────────────────────────────────────────────────────────

def _ask_sync(question: str, provider: str, model: str) -> str:
    t0 = time.time()
    log(f"[→] {provider}/{model}")
    vlog(f"question: {question[:120]}")
    answer = _client_for(provider, model).ask(question)
    elapsed = time.time() - t0
    preview = answer[:100].replace("\n", " ")
    if len(answer) > 100:
        preview += "…"
    log(f"[←] {provider}: \"{preview}\"  [{elapsed:.1f}s, {len(answer)}chars]")
    tlog(f"full answer: {answer[:500]}")
    return answer


def _stream_sync(question: str, provider: str, model: str, q: "_stdlib_queue.Queue[str | None]") -> None:
    t0 = time.time()
    log(f"[→] {provider}/{model} (stream)")
    vlog(f"question: {question[:120]}")
    _client_for(provider, model).ask_stream(question, q)
    elapsed = time.time() - t0
    log(f"[←] {provider}: stream completato [{elapsed:.1f}s]")


# ── init ──────────────────────────────────────────────────────────────────────

def _init_sync():
    enabled = [p for p, cfg in _config.get("providers", {}).items() if cfg.get("enabled")]
    for provider in enabled:
        if provider == "qwen":
            log(f"[*] Test login Qwen...")
            _get_qwen_client()._ensure_ready()
            log(f"[✓] Qwen: login attivo")
        elif provider == "deepseek":
            log(f"[*] Test login DeepSeek...")
            _get_ds_client()._ensure_ready()
            log(f"[✓] DeepSeek: login attivo")
        elif provider == "chatgpt":
            log(f"[*] Test login ChatGPT...")
            _get_gpt_client()._ensure_ready()
            log(f"[✓] ChatGPT: login attivo")
        elif provider == "claude":
            log(f"[*] Test login Claude.ai...")
            _get_claude_client()._ensure_ready()
            log(f"[✓] Claude.ai: login attivo")


# ── tool calling helpers ──────────────────────────────────────────────────────

def _format_tool_def(tool: dict) -> str:
    fn = tool.get("function", tool)
    name = fn.get("name", "")
    desc = fn.get("description", "")[:150]
    params = fn.get("parameters", {})
    props = params.get("properties", {})
    required = set(params.get("required", []))

    param_strs = []
    for pname, pdef in props.items():
        ptype = pdef.get("type", "any")
        opt = "" if pname in required else "?"
        param_strs.append(f"{pname}:{ptype}{opt}")

    return f"{name}({', '.join(param_strs)})\n  {desc}"


def _format_message_for_qwen(msg: dict) -> str:
    role = msg.get("role", "")
    content = msg.get("content") or ""
    if isinstance(content, list):
        content = " ".join(p.get("text", "") for p in content if p.get("type") == "text")

    if role == "system":
        return f"[System]\n{content}"
    elif role == "user":
        return f"[User]\n{content}"
    elif role == "assistant":
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            parts = []
            for tc in tool_calls:
                fn = tc.get("function", {})
                parts.append(
                    f"```tool_call\n{{\"name\": \"{fn.get('name')}\", "
                    f"\"arguments\": {fn.get('arguments', '{}')} }}\n```"
                )
            return "[Assistant]\n" + "\n".join(parts)
        return f"[Assistant]\n{content}"
    elif role == "tool":
        return f"[Tool result for {msg.get('tool_call_id', '')}]\n{content}"
    return f"[{role}]\n{content}"


def _build_qwen_prompt(messages: list[dict], tools: list[dict]) -> str:
    parts: list[str] = []
    if tools:
        tool_lines = [_format_tool_def(t) for t in tools]
        parts.append(
            "Tools available:\n" + "\n".join(tool_lines) + "\n\n"
            "To call a tool output ONLY:\n"
            "```tool_call\n{\"name\":\"NAME\",\"arguments\":{...}}\n```\n"
            "No other text before or after the block."
        )
    for msg in messages:
        parts.append(_format_message_for_qwen(msg))
    parts.append("[Assistant]")
    return "\n\n".join(parts)


_TOOL_CALL_PATTERNS = [
    re.compile(r"```tool_call\s*\n(.*?)\n```", re.DOTALL),
    re.compile(r"```json\s*\n(\{.*?\"name\".*?\})\s*\n```", re.DOTALL),
    re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL),
    re.compile(r"(\{\s*\"name\"\s*:.*?\"arguments\"\s*:.*?\})\s*$", re.DOTALL),
]


def _parse_tool_call_response(text: str) -> dict | None:
    for pat in _TOOL_CALL_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        raw = m.group(1).strip()
        try:
            obj = json.loads(raw)
            name = obj.get("name")
            args = obj.get("arguments", obj.get("parameters", {}))
            if not name:
                continue
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    pass
            return {"name": name, "arguments": args if isinstance(args, dict) else {}}
        except Exception:
            continue
    return None


# ── SSE builders ──────────────────────────────────────────────────────────────

def _sse_chunk(cid: str, model: str, created: int, content: str = "", finish: str | None = None) -> str:
    delta = {"content": content} if content else ({} if finish else {"role": "assistant", "content": ""})
    return f"data: {json.dumps({'id': cid, 'object': 'chat.completion.chunk', 'created': created, 'model': model, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]})}\n\n"


def _sse_tool_chunk(cid: str, model: str, created: int, tc_id: str, name: str, args_str: str, finish: bool = False) -> str:
    if not finish:
        delta = {"tool_calls": [{"index": 0, "id": tc_id, "type": "function", "function": {"name": name, "arguments": args_str}}]}
        finish_reason = None
    else:
        delta = {}
        finish_reason = "tool_calls"
    return f"data: {json.dumps({'id': cid, 'object': 'chat.completion.chunk', 'created': created, 'model': model, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish_reason}]})}\n\n"


def _tool_call_response(cid: str, model: str, created: int, name: str, args: dict) -> dict:
    tc_id = f"call_{uuid.uuid4().hex[:16]}"
    return {
        "id": cid, "object": "chat.completion", "created": created, "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": None,
            "tool_calls": [{"id": tc_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]},
            "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


# ── helpers ───────────────────────────────────────────────────────────────────

def _extract_question(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                return " ".join(p.get("text", "") for p in content if p.get("type") == "text")
            return str(content)
    return ""


# ── lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _busy_lock
    if not _config_ready:
        # Fallback: avviato standalone senza llmjack.py
        cfg = load_config()
        _config.update(cfg)
        mm, me = build_model_index(cfg)
        _model_map.update(mm)
        _model_meta.update(me)

    qwen_module.DEBUG = VERBOSE
    ds_module.DEBUG = VERBOSE
    gpt_module.DEBUG = VERBOSE
    claude_module.DEBUG = VERBOSE
    _busy_lock = asyncio.Lock()
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(_executor, _init_sync)
    log("")
    yield
    log("[!] Shutdown proxy...")


# ── app ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Multi-Provider Proxy", version="2.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── routes ────────────────────────────────────────────────────────────────────

@app.get("/v1/models")
async def list_models():
    data = []
    for mid, pname in _model_map.items():
        label = _model_meta.get(mid, {}).get("label", mid)
        data.append({"id": mid, "object": "model", "created": 0, "owned_by": pname, "description": label})
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    messages: list[dict] = body.get("messages", [])
    tools: list[dict] = body.get("tools", [])
    stream: bool = body.get("stream", False)
    model: str = body.get("model", _cfg_default_model(_config, _model_map))

    # resolve provider (fall back to default if model unknown, e.g. "qwen-proxy")
    provider = _cfg_provider_for(model, _model_map, _config)

    if tools:
        question = _build_qwen_prompt(messages, tools)
        vlog(f"agentic prompt ({len(question)} chars, {len(tools)} tools)")
    else:
        question = _extract_question(messages)

    if not question:
        raise HTTPException(status_code=400, detail="no user message found")

    if _busy_lock and _busy_lock.locked():
        raise HTTPException(status_code=503, detail="proxy busy, retry in a moment")

    cid = f"chatcmpl-{uuid.uuid4().hex[:16]}"
    created = int(time.time())
    loop = asyncio.get_running_loop()

    # ── true streaming (no tools) ─────────────────────────────────────────────
    if stream and not tools:
        q: _stdlib_queue.Queue[str | None] = _stdlib_queue.Queue()

        async def generate_stream():
            async with _busy_lock:
                fut = loop.run_in_executor(_executor, _stream_sync, question, provider, model, q)
                yield _sse_chunk(cid, model, created)
                while True:
                    try:
                        chunk = await loop.run_in_executor(None, lambda: q.get(timeout=2))
                    except _stdlib_queue.Empty:
                        continue
                    if chunk is None:
                        break
                    yield _sse_chunk(cid, model, created, content=chunk)
                await fut
            yield _sse_chunk(cid, model, created, finish="stop")
            yield "data: [DONE]\n\n"

        return StreamingResponse(generate_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ── wait-for-full (tools or non-streaming) ────────────────────────────────
    async with _busy_lock:
        try:
            answer: str = await loop.run_in_executor(_executor, _ask_sync, question, provider, model)
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))

    if not answer:
        raise HTTPException(status_code=504, detail="provider returned empty answer (timeout?)")

    tool_call = _parse_tool_call_response(answer) if tools else None

    if tool_call:
        name, args = tool_call["name"], tool_call["arguments"]
        args_str = json.dumps(args)
        tc_id = f"call_{uuid.uuid4().hex[:16]}"
        log(f"[⚙] Tool call: {name}({args_str[:80]}{'…' if len(args_str) > 80 else ''})")

        if stream:
            async def generate_tool():
                yield _sse_chunk(cid, model, created)
                yield _sse_tool_chunk(cid, model, created, tc_id, name, args_str)
                yield _sse_tool_chunk(cid, model, created, tc_id, name, "", finish=True)
                yield "data: [DONE]\n\n"
            return StreamingResponse(generate_tool(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        return JSONResponse(_tool_call_response(cid, model, created, name, args))

    if stream:
        async def generate_words():
            yield _sse_chunk(cid, model, created)
            words = answer.split(" ")
            for i, word in enumerate(words):
                yield _sse_chunk(cid, model, created, content=word if i == 0 else f" {word}")
                await asyncio.sleep(0.008)
            yield _sse_chunk(cid, model, created, finish="stop")
            yield "data: [DONE]\n\n"
        return StreamingResponse(generate_words(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return JSONResponse({
        "id": cid, "object": "chat.completion", "created": created, "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": len(question.split()), "completion_tokens": len(answer.split()),
                  "total_tokens": len(question.split()) + len(answer.split())},
    })


@app.delete("/v1/session")
@app.delete("/v1/session/{provider}")
async def reset_session(provider: str = "all"):
    global _qwen_client, _ds_client, _gpt_client, _claude_client
    loop = asyncio.get_running_loop()
    targets = ["qwen", "deepseek", "chatgpt", "claude"] if provider == "all" else [provider]
    for p in targets:
        if p == "qwen":
            with _qwen_lock:
                if _qwen_client:
                    await loop.run_in_executor(_executor, _qwen_client.close)
                    _qwen_client = None
            QWEN_SESSION_FILE.unlink(missing_ok=True)
        elif p == "deepseek":
            with _ds_lock:
                if _ds_client:
                    await loop.run_in_executor(_executor, _ds_client.close)
                    _ds_client = None
            DS_SESSION_FILE.unlink(missing_ok=True)
        elif p == "chatgpt":
            with _gpt_lock:
                if _gpt_client:
                    await loop.run_in_executor(_executor, _gpt_client.close)
                    _gpt_client = None
            GPT_SESSION_FILE.unlink(missing_ok=True)
        elif p == "claude":
            with _claude_lock:
                if _claude_client:
                    await loop.run_in_executor(_executor, _claude_client.close)
                    _claude_client = None
            CLAUDE_SESSION_FILE.unlink(missing_ok=True)
    return {"status": "ok", "reset": targets}
