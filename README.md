# llmjack

**OpenAI-compatible HTTP proxy** that routes `/v1/chat/completions` requests to Qwen, DeepSeek, ChatGPT, and Claude — using Playwright browser automation instead of paid API keys.

Most AI providers compute per-request anti-bot tokens inside browser JavaScript, making direct API calls impossible without a valid browser session. llmjack runs a real headless Chrome instance that maintains a logged-in session and intercepts the provider's internal API calls, exposing a standard OpenAI-compatible interface that any client (LangChain, qwen-code, Continue, Cursor, custom scripts) can talk to.

---

> [!WARNING]
> **Use dedicated accounts — not your primary ones.**
>
> llmjack automates a real browser session against provider web interfaces. Providers may detect unusual usage patterns (high request frequency, automated behaviour, headless browser fingerprints) and **suspend or permanently ban the account**.
>
> - Create a separate, throwaway account on each provider you intend to use.
> - Do not use accounts tied to paid subscriptions, personal data, or other services you care about.
> - You are responsible for complying with each provider's terms of service. This tool is intended for personal experimentation and research only.

---

## How it works

```
Your app / IDE / CLI
    │  POST /v1/chat/completions
    │  {"model": "claude-haiku-4-5-20251001", "messages": [...]}
    ▼
llmjack  (FastAPI · port 8080)
    │  routes by model → provider
    ├─► Qwen client     → chat.qwen.ai       (JS fetch interceptor)
    ├─► DeepSeek client → chat.deepseek.com  (page.route SSE intercept)
    ├─► ChatGPT client  → chatgpt.com        (page.route + real Chrome)
    └─► Claude client   → claude.ai          (context.request, no route needed)
```

One request at a time — Playwright is not thread-safe. Concurrent requests get a `503` instead of queuing.

---

## Supported providers and models

| Provider   | Models                                                         | Strategy                       |
|------------|----------------------------------------------------------------|--------------------------------|
| `qwen`     | qwen3.6-plus, qwen3.6-flash, qwen3.6-max, qwen3.5-*, qwen3-max | JS fetch interceptor + SSE     |
| `deepseek` | deepseek-chat, deepseek-reasoner                               | page.route SSE intercept       |
| `chatgpt`  | gpt-5, gpt-5-mini, gpt-4o, o3, o4 (and variants)              | real Chrome, Cloudflare bypass |
| `claude`   | claude-haiku-4-5, claude-sonnet-4-6, claude-opus-4-7           | context.request (cookie jar)   |

Enable and configure providers and models in `providers.json`.

---

## Requirements

- Python 3.11+
- Google Chrome installed (`brew install --cask google-chrome` on macOS) — required for ChatGPT and Claude to bypass Cloudflare Turnstile
- A free account on each provider you want to use

```bash
pip install fastapi uvicorn playwright questionary rich
playwright install chromium
```

`questionary` and `rich` are optional — the interactive wizard falls back gracefully if they are not installed.

---

## Quick start

```bash
python llmjack.py
```

Launches an interactive TUI wizard to select providers, models, port, and log level. At the end it shows a one-command string to skip the wizard next time.

**Skip the wizard:**

```bash
python llmjack.py --no-wizard
python llmjack.py --no-wizard --providers claude --models claude:claude-haiku-4-5-20251001 --port 8080
```

**Re-open the wizard at any time:**

```bash
python llmjack.py --wizard
```

---

## CLI reference

| Flag | Default | Description |
|------|---------|-------------|
| *(no args)* | — | launches interactive wizard |
| `--wizard` | — | force wizard regardless of other flags |
| `--no-wizard` | — | skip wizard, use `providers.json` as-is |
| `--providers p1,p2` | — | enable only these providers |
| `--models p:model,...` | — | enable specific models per provider |
| `--default-provider p` | — | default provider for unrecognized models |
| `--default-model p:model` | — | default model per provider (repeatable) |
| `--thinking provider` | — | enable `thinking_enabled` for provider (repeatable) |
| `--search provider` | — | enable `search_enabled` for provider (repeatable) |
| `--port` / `-p` | `8080` | listening port |
| `--log-level` | from config | `TRACE` · `DEBUG` · `INFO` · `WARNING` · `ERROR` |
| `--verbose` / `-v` | — | shorthand for `--log-level DEBUG` |
| `--log-dir` | `./logs` | base directory for session logs |

---

## API endpoints

```bash
# Chat (non-streaming)
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3.6-plus","messages":[{"role":"user","content":"Hello"}]}'

# Streaming
curl -N -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-haiku-4-5-20251001","messages":[{"role":"user","content":"Hello"}],"stream":true}'

# List available models
curl http://localhost:8080/v1/models

# Reset browser session (re-triggers login if needed)
curl -X DELETE http://localhost:8080/v1/session           # all providers
curl -X DELETE http://localhost:8080/v1/session/claude    # single provider
```

---

## Configuration

`providers.json` is the single source of truth. Edit it directly or let the wizard rewrite it.

```jsonc
{
  "default_provider": "claude",
  "logging": {
    "level": "INFO",
    "log_keep_sessions": 10
  },
  "providers": {
    "claude": {
      "enabled": true,
      "default_model": "claude-haiku-4-5-20251001",
      "models": {
        "claude-haiku-4-5-20251001": { "enabled": true,  "label": "Claude Haiku 4.5" },
        "claude-sonnet-4-6":         { "enabled": false, "label": "Claude Sonnet 4.6" },
        "claude-opus-4-7":           { "enabled": false, "label": "Claude Opus 4.7" }
      },
      "params": { "thinking_enabled": false }
    }
  }
}
```

---

## Session logging

Each proxy run creates a timestamped directory under `logs/`:

```
logs/
└── 2026-04-25_14-30-00/
    ├── proxy.log     ← all messages at configured level and above
    ├── errors.log    ← WARNING, ERROR, CRITICAL only
    └── config.json   ← exact config snapshot used for the session
```

Log levels: `TRACE` (5) · `DEBUG` (10) · `INFO` (20) · `WARNING` (30) · `ERROR` (40) · `CRITICAL` (50)

Old sessions are pruned automatically — `log_keep_sessions` controls how many to keep (default: 10).

---

## First login

On first run, or when a session expires, llmjack opens a visible Chrome window and waits for you to log in manually. Once logged in, the session is saved to `data/<provider>_profile/` and reused across restarts — you will not be asked to log in again unless the session expires or you reset it.

---

## Use with IDE / CLI tools

Any tool that supports an OpenAI-compatible endpoint works. Set:

- **Base URL:** `http://localhost:8080/v1`
- **API key:** any non-empty string (llmjack ignores it)
- **Model:** any model ID from `providers.json` (e.g. `qwen3.6-plus`, `claude-haiku-4-5-20251001`)

**qwen-code** (`~/.qwen/settings.json`): set `selectedType` to `"openai"` and `model.name` to your proxy's provider id.

---

## Project structure

```
llmjack/
├── llmjack.py          entry point — wizard, CLI parsing, uvicorn launch
├── proxy.py            deprecated wrapper (delegates to llmjack.py --no-wizard)
├── providers.json      provider/model config
├── core/
│   ├── server.py       FastAPI app and routes
│   ├── config.py       load/save/override providers.json
│   └── log_manager.py  session logging, TRACE level, rotation
├── wizard/
│   └── setup.py        interactive TUI (questionary + rich)
├── clients/
│   ├── qwen_client.py
│   ├── deepseek_client.py
│   ├── chatgpt_client.py
│   └── claude_client.py
├── logs/               per-session logs (gitignored)
└── data/               browser sessions + Chrome profiles (gitignored)
```

---

## Streaming note

Only Qwen supports truly incremental streaming — its JS interceptor pushes SSE chunks in real time. ChatGPT, DeepSeek, and Claude capture the full response body first and then replay it as a stream. The latency you see is the provider's actual generation time; the chunking is post-processed.

---

## License

MIT
