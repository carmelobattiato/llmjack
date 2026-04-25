# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

OpenAI-compatible HTTP proxy that routes API calls to AI chat providers (Qwen, DeepSeek, ChatGPT, Claude) via Playwright browser automation — bypassing the need for paid API keys. Most providers compute per-request anti-bot tokens in browser JS, making direct API calls impossible. Playwright runs real headless Chrome instead.

Provider configuration lives in `providers.json` — enable/disable providers and models, set per-provider params (`thinking_enabled`, `search_enabled`, etc.).

## Run commands

```bash
# Start proxy — interactive wizard (default when no --providers arg)
python llmjack.py

# Non-interactive (skip wizard)
python llmjack.py --no-wizard
python llmjack.py --no-wizard --providers claude --models claude:claude-haiku-4-5-20251001 --port 8080

# Force wizard even with CLI flags
python llmjack.py --wizard

# Log control
python llmjack.py --no-wizard --log-level DEBUG
python llmjack.py --no-wizard --verbose          # shorthand for DEBUG

# proxy.py still works (shows deprecation warning, delegates to llmjack.py --no-wizard)
python proxy.py --verbose --port 9000

# Test clients standalone
python clients/qwen_client.py "Ciao"
python clients/qwen_client.py --debug "test"
python clients/qwen_client.py --new "test"          # force new Qwen session
python clients/deepseek_client.py "Ciao"
python clients/deepseek_client.py --debug "test"
python clients/chatgpt_client.py "Ciao"
python clients/chatgpt_client.py --debug --model gpt-4o "test"
python clients/claude_client.py "Ciao"
python clients/claude_client.py --debug --model claude-sonnet-4-6 "test"

# Reset sessions
curl -X DELETE http://localhost:8080/v1/session            # tutti i provider
curl -X DELETE http://localhost:8080/v1/session/qwen       # solo Qwen
curl -X DELETE http://localhost:8080/v1/session/deepseek   # solo DeepSeek
curl -X DELETE http://localhost:8080/v1/session/chatgpt    # solo ChatGPT
curl -X DELETE http://localhost:8080/v1/session/claude     # solo Claude

# Backup project (excludes data/ profiles and __pycache__)
./backup.sh
```

## Install dependencies (first time)

```bash
pip install fastapi uvicorn playwright questionary rich
playwright install chromium
# questionary and rich are optional — wizard falls back gracefully without them
```

## Architecture

```
Client (qwen-code CLI or any OpenAI SDK)
    │  POST /v1/chat/completions  (model="claude-sonnet-4-6" etc.)
    ▼
llmjack.py  (entry point: parse args, wizard or CLI overrides, setup logging)
    │
    ▼
core/server.py  (FastAPI, single ThreadPoolExecutor, single asyncio busy lock)
    │  routes by model_id → provider via _model_map from providers.json
    ├─► clients/qwen_client.QwenClient          → chat.qwen.ai
    ├─► clients/deepseek_client.DeepSeekClient  → chat.deepseek.com
    ├─► clients/chatgpt_client.ChatGPTClient    → chatgpt.com
    └─► clients/claude_client.ClaudeAIClient   → claude.ai
```

**`providers.json`** — Source of truth for enabled providers, models, and params. Loaded at startup. Also contains a `"logging"` section (after `"default_provider"`) with `"level"` (default `"INFO"`) and `"log_keep_sessions"` (default `10`). To add a new provider: add entry to `providers.json`, create `clients/<name>_client.py`, add `elif provider == "<name>"` in `core/server.py:_client_for()`.

**`data/`** — Runtime directory (gitignored). Contains session files (`qwen_session`, `deepseek_session`, `chatgpt_session`, `claude_session`) and Chrome profile directories (`qwen_profile/`, `deepseek_profile/`, `chatgpt_profile/`, `claude_profile/`). Each client creates this directory automatically on first write.

**`logs/`** — Per-session log directory (gitignored). Each run creates `logs/YYYY-MM-DD_HH-MM-SS/` containing:
- `proxy.log` — all messages at the configured level and above
- `errors.log` — WARNING+ only
- `config.json` — config snapshot for that session
Rotation keeps the last `log_keep_sessions` directories (default 10).

**`llmjack.py`** — Main entry point. Startup sequence:
1. `parse_args()`
2. `load_config()` (from `core/config.py`)
3. If `--wizard` OR (not `--no-wizard` AND not `--providers`): run interactive wizard (`wizard/setup.py`)
4. Else: `apply_cli_overrides()` with `--providers`, `--models`, `--default-provider`, `--default-model`, `--thinking`, `--search`, `--preempt`
5. `log_manager.setup()` — creates session log directory
6. Shares config into `core.server` globals (`_config`, `_model_map`, `_model_meta`, `_config_ready=True`)
7. `uvicorn.run("core.server:app")`

**`core/config.py`** — Config load/save/override:
- `load_config()`, `save_config()` (writes `.bak` before overwriting), `build_model_index()`, `apply_cli_overrides()`
- `apply_cli_overrides()` handles: `--providers`, `--models`, `--default-provider`, `--default-model`, `--thinking`, `--search`, `--preempt`

**`core/log_manager.py`** — Session logging:
- Custom TRACE level (value=5, below DEBUG=10). Level hierarchy: TRACE < DEBUG < INFO < WARNING < ERROR < CRITICAL.
- Public API: `log()` INFO, `vlog()` DEBUG, `tlog()` TRACE, `elog()` ERROR.
- Configurable via `--log-level` CLI flag or `providers.json "logging"."level"`.

**`core/server.py`** — FastAPI app (was `proxy.py`):
- Single `ThreadPoolExecutor(max_workers=1)` — Playwright is not thread-safe; only one browser interaction at a time.
- `asyncio.Lock` (`_busy_lock`) rejects concurrent requests with 503 instead of queuing them.
- `_config_ready` flag prevents `lifespan()` from re-loading config when `llmjack.py` already set it.
- **Streaming path** (`stream=True`, no tools): `ask_stream()` runs in executor thread, puts chunks in `Queue`, async generator reads with 2s timeout and yields SSE chunks immediately.
- **Tool-call path** (`tools` present): builds a full text prompt via `_build_qwen_prompt()` (compact function signatures + conversation history), waits for full response, parses JSON tool call with `_parse_tool_call_response()` (4 regex patterns), returns OpenAI `tool_calls` format.
- **Lifespan handler**: `lifespan()` must be defined **before** `app = FastAPI(lifespan=lifespan)` — Python resolves the name at parse time.

**`wizard/setup.py`** — Interactive TUI (questionary + rich). Guides provider/model selection before startup. If `questionary` or `rich` are not installed, the wizard falls back gracefully (skips TUI, proceeds with defaults or existing config).

**`proxy.py`** — Thin deprecated wrapper. Shows a deprecation warning and delegates to `llmjack.py --no-wizard` with the same CLI flags. Kept for backward compatibility.

**Session persistence**: Each provider has `data/<name>_session` (plain text) and `data/<name>_profile/` (Chrome profile with cookies). Both survive restarts. `data/claude_session` stores `org_id/conv_id`.

**`clients/qwen_client.py`** — Qwen browser automation:
- `_ensure_ready()`: opens browser, navigates to Qwen, detects login state. If session expired, opens visible browser for manual login.
- `_fire(question)`: fills React textarea via `nativeInputValueSetter` trick (not `keyboard.type()`), presses Enter, returns `req_ver` version counter.
- `ask(question)` → `str`: polls `window.__qwen_answer__` every 100ms until `window.__qwen_ready__` is True with matching version.
- **JS interceptor** (`_INTERCEPTOR_JS`): injected via `add_init_script`, overrides `window.fetch` to intercept `/api/v2/chat/completions` SSE stream. Looks for `delta.phase === 'answer'` chunks. Version counter (`__qwen_req_ver__`) prevents stale signals from previous requests.

**`clients/deepseek_client.py`** — DeepSeek browser automation:
- Uses `page.route("**/api/v0/chat/completion")` to intercept SSE body. Parses DeepSeek's JSON Patch fragment format (THINK vs RESPONSE fragments).
- Session URL pattern: `/a/chat/s/<uuid>`

**`clients/chatgpt_client.py`** — ChatGPT browser automation:
- Uses `channel="chrome"` (real Chrome) to bypass Cloudflare Turnstile. Removes `--enable-automation` + `--enable-unsafe-swiftshader` (crashes macOS headed Chrome).
- `page.route("**/backend-api/f/conversation")` intercepts SSE; injects model into request body before `route.fetch()`.
- SSE format is v1 JSON Patch: `{"o":"patch","v":[{"p":"/message/content/parts/0","o":"append","v":"text"}]}` or shorthand `{"v":[...]}`. Accumulated via patch appends.
- Entity markers (`word[...]`) span chunks — strip from joined text after parsing.

**`clients/claude_client.py`** — Claude.ai client (no UI interaction needed):
- `context.request.post()` (Playwright APIRequestContext) shares the browser's cookie jar (`sessionKey` + `cf_clearance`). No `page.route()` needed — response body returned directly.
- `_check_logged_in()`: GET `/api/organizations` to verify session and get `org_id`.
- Creates a new conversation via POST `/api/organizations/{org_id}/chat_conversations` once per session; stores `org_id/conv_id` in `data/claude_session`.
- SSE format is Anthropic Messages API: `{"type":"content_block_delta","delta":{"type":"text_delta","text":"..."}}`

## Streaming reality

Only Qwen has truly incremental streaming: the JS interceptor pushes SSE delta chunks into the queue in real time. ChatGPT, DeepSeek, and Claude use `page.route()` / `context.request.post()` which captures the **full response body** at once — `ask_stream()` for these providers batches all chunks into the queue after the response completes. The proxy's word-by-word simulate path (tool+non-stream fallback in `core/server.py:generate_words()`) emits words with `asyncio.sleep(0.008)`.

## Gemini (not yet implemented)

`providers.json` has a `gemini` provider stub but `clients/gemini_client.py` does not exist. To implement: create `clients/gemini_client.py` (following DeepSeek's route-intercept pattern) and add `elif provider == "gemini": return _get_gemini_client(model)` in `core/server.py:_client_for()`.

## `--model` CLI flag removed

The old `proxy.py --model <model>` banner-only flag no longer exists. All model control is via `--models` (e.g. `--models claude:claude-haiku-4-5-20251001`). To change the default model at runtime, edit `providers.json` or use `--default-model`.

## Key gotchas

**React textarea**: `locator("textarea").first` resolves to Monaco editor's hidden `<textarea class="ime-text-area" aria-hidden="true">` when code blocks are rendered. Always use JS querySelector: `'textarea:not(.ime-text-area):not([aria-hidden="true"])'`.

**Model selection is unreliable**: `_select_model()` tries CSS selectors against Qwen's dynamic class names. They often fail silently (which is fine — Qwen uses whatever model the UI last had selected). Timeouts are intentionally short (200ms per selector) to avoid the 36s startup delay this caused before.

**Tool call detection**: `_parse_tool_call_response()` tries 4 patterns in order: ` ```tool_call ` block, ` ```json ` block with `"name"` key, `<tool_call>` XML, raw JSON object. The first match wins.

**Version counter**: Each `ask()` call sets `window.__qwen_req_ver__` to `int(time.time() * 1000)`. The JS interceptor's async closure captures `myVer` and aborts if the counter changes — prevents a previous response from triggering `ready=True` for a new request.

**`_get_claude_client()` defensive fallback**: if `default_model` in providers.json doesn't exist in the enabled models list, it auto-selects the first enabled Claude model. Prevents 403 from stale config edits.

**Chrome vs Chromium**: ChatGPT and Claude use `channel="chrome"` (real system Chrome, not Playwright's bundled Chromium) to bypass Cloudflare Turnstile. Requires `brew install --cask google-chrome` on macOS. Qwen and DeepSeek use bundled Chromium (no `channel=` arg).

**DeepSeek headless**: Unlike ChatGPT and Claude, `DeepSeekClient` has no `_headless_blocked` flag. It always launches headless; it only opens a visible browser when login is needed, then re-launches headless. If Cloudflare blocks DeepSeek headless after login, the client will silently fail.

**`qwen-code` settings**: Configured in `~/.qwen/settings.json`. `selectedType` must be `"openai"` (not `"custom"`). The `model.name` must match the provider `id` field (`"qwen-proxy"`).
