# llmjack CLI Wizard + Session Logging — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Aggiungere `llmjack.py` come entry point unico con wizard TUI interattivo per configurare `providers.json` e logging strutturato per sessione con livelli configurabili.

**Architecture:** `llmjack.py` orchestra tre moduli — `core/config.py` (config pura), `core/log_manager.py` (logging per sessione), `wizard/setup.py` (TUI questionary+rich) — e avvia `core/server.py` (FastAPI, migrato da proxy.py). I client esistenti restano invariati tranne l'aggiornamento del debug output verso il logger condiviso.

**Tech Stack:** Python 3.11+, FastAPI, uvicorn, Playwright, questionary, rich, logging stdlib

---

## File map

| Operazione | File | Responsabilità |
|-----------|------|----------------|
| Crea | `llmjack.py` | Entry point: CLI parsing, wizard trigger, uvicorn start |
| Crea | `core/__init__.py` | Package marker |
| Crea | `core/config.py` | load/save providers.json, model index, CLI overrides |
| Crea | `core/log_manager.py` | Session logging, TRACE level, rotation |
| Crea | `core/server.py` | FastAPI app (migrato da proxy.py) |
| Crea | `wizard/__init__.py` | Package marker |
| Crea | `wizard/setup.py` | TUI interattivo questionary+rich |
| Crea | `logs/.gitkeep` | Placeholder cartella log (gitignored) |
| Modifica | `proxy.py` | Thin wrapper deprecato → llmjack.py |
| Modifica | `providers.json` | Aggiungi sezione "logging" |
| Modifica | `clients/qwen_client.py` | dbg() → log_manager |
| Modifica | `clients/deepseek_client.py` | dbg() → log_manager |
| Modifica | `clients/chatgpt_client.py` | dbg() → log_manager |
| Modifica | `clients/claude_client.py` | dbg() → log_manager |
| Modifica | `CLAUDE.md` | Nuovi comandi e struttura |

---

## Task 1: Struttura cartelle + providers.json

**Files:**
- Create: `core/__init__.py`
- Create: `wizard/__init__.py`
- Create: `logs/.gitkeep`
- Modify: `providers.json`

- [ ] **Step 1: Crea cartelle e package markers**

```bash
mkdir -p core wizard logs
touch core/__init__.py wizard/__init__.py logs/.gitkeep
```

- [ ] **Step 2: Aggiungi sezione `logging` a providers.json**

Apri `providers.json` e aggiungi il campo `"logging"` subito dopo `"default_provider"`:

```json
{
  "default_provider": "qwen",

  "logging": {
    "level": "INFO",
    "log_keep_sessions": 10
  },

  "providers": {
    ...
  }
}
```

- [ ] **Step 3: Verifica struttura**

```bash
python -c "import json; cfg = json.load(open('providers.json')); print(cfg.get('logging'))"
```

Output atteso: `{'level': 'INFO', 'log_keep_sessions': 10}`

---

## Task 2: `core/config.py`

**Files:**
- Create: `core/config.py`

- [ ] **Step 1: Crea `core/config.py`**

```python
#!/usr/bin/env python3
"""Config loading, model index, CLI overrides — estratto da proxy.py."""
import argparse
import json
import shutil
from pathlib import Path

PROVIDERS_FILE = Path(__file__).parent.parent / "providers.json"

_DEFAULT_CFG: dict = {
    "default_provider": "qwen",
    "logging": {"level": "INFO", "log_keep_sessions": 10},
    "providers": {
        "qwen": {
            "enabled": True,
            "default_model": "qwen3.6-plus",
            "models": {"qwen3.6-plus": {"enabled": True, "label": "Qwen 3.6 Plus"}},
            "params": {},
        }
    },
}


def load_config(path: Path = PROVIDERS_FILE) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception as e:
        print(f"[!] {path.name} non trovato o invalido: {e}. Uso defaults Qwen.")
        return json.loads(json.dumps(_DEFAULT_CFG))


def save_config(cfg: dict, path: Path = PROVIDERS_FILE) -> None:
    """Salva providers.json, backup in .bak (sovrascrive il precedente)."""
    bak = path.with_suffix(".json.bak")
    if path.exists():
        shutil.copy2(path, bak)
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


def get_logging_cfg(cfg: dict) -> dict:
    return {"level": "INFO", "log_keep_sessions": 10, **cfg.get("logging", {})}


def build_model_index(cfg: dict) -> tuple[dict[str, str], dict[str, dict]]:
    """Returns (model_map: model_id→provider, model_meta: model_id→cfg)."""
    model_map: dict[str, str] = {}
    model_meta: dict[str, dict] = {}
    for pname, pcfg in cfg.get("providers", {}).items():
        if not pcfg.get("enabled", False):
            continue
        provider_params = pcfg.get("params", {})
        for mid, mcfg in pcfg.get("models", {}).items():
            if mcfg.get("enabled", True):
                model_map[mid] = pname
                model_meta[mid] = {**provider_params, **mcfg}
    return model_map, model_meta


def default_model(cfg: dict, model_map: dict) -> str:
    providers = cfg.get("providers", {})
    for pname in [cfg.get("default_provider", ""), *providers.keys()]:
        pcfg = providers.get(pname, {})
        if pcfg.get("enabled") and pcfg.get("default_model"):
            return pcfg["default_model"]
    return next(iter(model_map), "deepseek-chat")


def provider_for(model_id: str, model_map: dict, cfg: dict) -> str:
    if model_id in model_map:
        return model_map[model_id]
    for pname, pcfg in cfg.get("providers", {}).items():
        if pcfg.get("enabled"):
            return pname
    return "deepseek"


def apply_cli_overrides(cfg: dict, args: argparse.Namespace) -> dict:
    """Applica --providers / --models / --thinking / ecc. al config dict."""
    if not getattr(args, "providers", None):
        return cfg

    enabled_ps = {p.strip() for p in args.providers.split(",")}
    for pname in cfg.get("providers", {}):
        cfg["providers"][pname]["enabled"] = (pname in enabled_ps)

    if getattr(args, "models", None):
        mmap: dict[str, set] = {}
        for pm in args.models.split(","):
            pm = pm.strip()
            if ":" in pm:
                p, m = pm.split(":", 1)
                mmap.setdefault(p, set()).add(m)
        for pname, mset in mmap.items():
            if pname in cfg.get("providers", {}):
                for mid in cfg["providers"][pname].get("models", {}):
                    cfg["providers"][pname]["models"][mid]["enabled"] = (mid in mset)

    if getattr(args, "default_provider", None):
        cfg["default_provider"] = args.default_provider

    for dm_str in (getattr(args, "default_model", None) or []):
        if ":" in dm_str:
            p, m = dm_str.split(":", 1)
            if p in cfg.get("providers", {}):
                cfg["providers"][p]["default_model"] = m

    for attr, param in [("thinking", "thinking_enabled"), ("search", "search_enabled"), ("preempt", "preempt")]:
        for p in (getattr(args, attr, None) or []):
            if p in cfg.get("providers", {}):
                cfg["providers"][p].setdefault("params", {})[param] = True

    return cfg
```

- [ ] **Step 2: Verifica import e funzioni base**

```bash
python -c "
from core.config import load_config, build_model_index, get_logging_cfg
cfg = load_config()
mm, me = build_model_index(cfg)
print('model_map:', list(mm.keys())[:3])
print('logging:', get_logging_cfg(cfg))
"
```

Output atteso: lista modelli abilitati + `{'level': 'INFO', 'log_keep_sessions': 10}`

- [ ] **Step 3: Verifica apply_cli_overrides**

```bash
python -c "
import argparse
from core.config import load_config, apply_cli_overrides
cfg = load_config()
args = argparse.Namespace(providers='claude', models=None, default_provider=None,
                          default_model=None, thinking=None, search=None, preempt=None)
cfg2 = apply_cli_overrides(cfg, args)
enabled = [p for p,c in cfg2['providers'].items() if c.get('enabled')]
print('enabled:', enabled)
assert enabled == ['claude'], f'expected [claude], got {enabled}'
print('OK')
"
```

---

## Task 3: `core/log_manager.py`

**Files:**
- Create: `core/log_manager.py`

- [ ] **Step 1: Crea `core/log_manager.py`**

```python
#!/usr/bin/env python3
"""Session logging: TRACE/DEBUG/INFO/WARNING/ERROR/CRITICAL + rotation."""
import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Livello TRACE custom (sotto DEBUG=10)
TRACE = 5
logging.addLevelName(TRACE, "TRACE")

_logger: logging.Logger | None = None
_level: int = logging.INFO
_session_log_dir: Path | None = None


def _name_to_level(name: str) -> int:
    name = name.upper()
    return TRACE if name == "TRACE" else getattr(logging, name, logging.INFO)


def _rotate(base_dir: Path, keep: int) -> None:
    if not base_dir.exists():
        return
    sessions = sorted(d for d in base_dir.iterdir() if d.is_dir())
    for old in sessions[: max(0, len(sessions) - keep)]:
        shutil.rmtree(old, ignore_errors=True)


def setup(cfg: dict, level_name: str | None, base_dir: Path) -> Path:
    """Crea session dir, configura logger, esegue rotation. Ritorna session_dir."""
    global _logger, _level, _session_log_dir

    _level = _name_to_level(level_name or "INFO")
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    session_dir = base_dir / ts
    session_dir.mkdir(parents=True, exist_ok=True)
    _session_log_dir = session_dir

    (session_dir / "config.json").write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False)
    )

    _logger = logging.getLogger("llmjack")
    _logger.setLevel(_level)
    _logger.handlers.clear()
    _logger.propagate = False

    main_fmt = logging.Formatter(
        "%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    err_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    fh = logging.FileHandler(session_dir / "proxy.log", encoding="utf-8")
    fh.setLevel(_level)
    fh.setFormatter(main_fmt)
    _logger.addHandler(fh)

    eh = logging.FileHandler(session_dir / "errors.log", encoding="utf-8")
    eh.setLevel(logging.WARNING)
    eh.setFormatter(err_fmt)
    _logger.addHandler(eh)

    keep = cfg.get("logging", {}).get("log_keep_sessions", 10)
    _rotate(base_dir, keep)

    return session_dir


def get_logger() -> logging.Logger:
    return _logger if _logger is not None else logging.getLogger("llmjack")


def log(msg: str) -> None:
    """INFO: stdout + proxy.log."""
    print(msg, flush=True)
    if _logger:
        _logger.info(msg)


def vlog(msg: str) -> None:
    """DEBUG: stdout (solo se level<=DEBUG) + proxy.log."""
    full = f"  [v] {msg}"
    if _logger and _level <= logging.DEBUG:
        print(full, flush=True)
    if _logger:
        _logger.debug(full)


def tlog(msg: str) -> None:
    """TRACE: stdout (solo se level<=TRACE) + proxy.log."""
    if _logger and _level <= TRACE:
        print(f"  [t] {msg}", flush=True)
    if _logger:
        _logger.log(TRACE, msg)


def elog(msg: str) -> None:
    """ERROR: stderr + proxy.log + errors.log."""
    print(msg, flush=True, file=sys.stderr)
    if _logger:
        _logger.error(msg)
```

- [ ] **Step 2: Verifica creazione sessione**

```bash
python -c "
from pathlib import Path
from core.log_manager import setup
cfg = {'logging': {'level': 'DEBUG', 'log_keep_sessions': 5}}
d = setup(cfg, 'DEBUG', Path('logs'))
print('session dir:', d)
print('files:', [f.name for f in d.iterdir()])
"
```

Output atteso: path sessione + `['config.json', 'errors.log', 'proxy.log']`

- [ ] **Step 3: Verifica rotation**

```bash
python -c "
from pathlib import Path
from core.log_manager import setup, _rotate

# Crea 3 sessioni di test
base = Path('logs/_test_rotation')
base.mkdir(parents=True, exist_ok=True)
for i in range(3):
    (base / f'2026-01-0{i+1}_00-00-00').mkdir(exist_ok=True)

cfg = {'logging': {'log_keep_sessions': 2}}
_rotate(base, 2)
remaining = list(base.iterdir())
assert len(remaining) == 2, f'expected 2, got {len(remaining)}'
print('rotation OK, rimaste:', [d.name for d in remaining])

# Pulizia
import shutil; shutil.rmtree(base)
"
```

---

## Task 4: `core/server.py`

**Files:**
- Create: `core/server.py` (migrato da `proxy.py`)

- [ ] **Step 1: Copia proxy.py come base**

```bash
cp proxy.py core/server.py
```

- [ ] **Step 2: Sostituisci blocco imports iniziale**

Sostituisci i primi ~45 righe (fino a `PROVIDERS_FILE = ...`) con:

```python
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
```

- [ ] **Step 3: Sostituisci blocco globals**

Dopo gli import, sostituisci la sezione `# ── runtime state` con:

```python
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
```

- [ ] **Step 4: Rimuovi funzioni ora in core/config.py**

Elimina completamente queste funzioni da `core/server.py` (sono ora in `core/config.py`):
- `_load_config()`
- `_build_model_index()`
- `_default_model()`
- `_provider_for()`

Sostituisci le chiamate residue:
- `_default_model()` → `_cfg_default_model(_config, _model_map)`
- `_provider_for(model)` → `_cfg_provider_for(model, _model_map, _config)`

- [ ] **Step 5: Rimuovi le funzioni log (ora in log_manager)**

Elimina `log()`, `vlog()` da `core/server.py` — sono importate da `core.log_manager`.

- [ ] **Step 6: Aggiorna `_ask_sync` con timing**

```python
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
```

- [ ] **Step 7: Aggiorna `_stream_sync` con timing**

```python
def _stream_sync(question: str, provider: str, model: str, q: "_stdlib_queue.Queue[str | None]") -> None:
    t0 = time.time()
    log(f"[→] {provider}/{model} (stream)")
    vlog(f"question: {question[:120]}")
    _client_for(provider, model).ask_stream(question, q)
    elapsed = time.time() - t0
    log(f"[←] {provider}: stream completato [{elapsed:.1f}s]")
```

- [ ] **Step 8: Aggiorna `lifespan()`**

```python
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
```

- [ ] **Step 9: Aggiorna `chat_completions` per usare helper di config**

Nel route `POST /v1/chat/completions`, cambia:
```python
model: str = body.get("model", _default_model())
provider = _provider_for(model)
```
in:
```python
model: str = body.get("model", _cfg_default_model(_config, _model_map))
provider = _cfg_provider_for(model, _model_map, _config)
```

- [ ] **Step 10: Rimuovi `_parse_args()` e blocco `if __name__ == "__main__"`**

Elimina `_parse_args()` e tutto il blocco `if __name__ == "__main__": ...` — sono ora in `llmjack.py`.

- [ ] **Step 11: Verifica import**

```bash
python -c "from core.server import app; print('server OK')"
```

Output atteso: `server OK` senza errori.

---

## Task 5: `wizard/setup.py`

**Files:**
- Create: `wizard/setup.py`

- [ ] **Step 1: Crea `wizard/setup.py`**

```python
#!/usr/bin/env python3
"""
Wizard TUI interattivo per configurare providers.json.
Dipendenze: questionary, rich  (pip install questionary rich)
Se non installate, fallback silenzioso a no-wizard.
"""
import sys

_DEPS_OK = False
_DEPS_ERR = ""

try:
    import questionary
    from questionary import Style as QStyle
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich import box
    _DEPS_OK = True
except ImportError as _e:
    _DEPS_ERR = str(_e)


# ── fallback se deps mancanti ─────────────────────────────────────────────────

def _fallback_setup(config: dict) -> tuple[dict, int, str]:
    print(f"[!] Dipendenze wizard mancanti ({_DEPS_ERR}): pip install questionary rich")
    print("[!] Avvio in modalità --no-wizard (providers.json as-is)")
    log_cfg = config.get("logging", {})
    return config, 8080, log_cfg.get("level", "INFO")


if not _DEPS_OK:
    def run_setup(config: dict) -> tuple[dict, int, str]:
        return _fallback_setup(config)
else:
    # ── stile questionary ──────────────────────────────────────────────────────

    QSTYLE = QStyle([
        ("qmark",       "fg:#00b4d8 bold"),
        ("question",    "bold"),
        ("answer",      "fg:#00e676 bold"),
        ("pointer",     "fg:#ff9d00 bold"),
        ("highlighted", "fg:#00b4d8 bold"),
        ("selected",    "fg:#00e676"),
        ("separator",   "fg:#555555"),
        ("instruction", "fg:#888888 italic"),
        ("text",        ""),
        ("disabled",    "fg:#666666 italic"),
    ])

    PROVIDER_ICONS = {
        "qwen": "🟠", "deepseek": "🔵",
        "chatgpt": "🟢", "claude": "🟣", "gemini": "🔴",
    }

    console = Console()

    def _icon(p: str) -> str:
        return PROVIDER_ICONS.get(p, "⚪")

    def _q(fn):
        """Esegue fn() di questionary, esce su Ctrl-C / None."""
        r = fn()
        if r is None:
            console.print("\n[yellow]Annullato (Ctrl-C).[/yellow]")
            sys.exit(0)
        return r

    # ── banner ─────────────────────────────────────────────────────────────────

    def _print_banner():
        console.print(Panel.fit(
            "[bold cyan]llmjack[/bold cyan]  [dim]·[/dim]  "
            "[white]Multi-Provider OpenAI-Compatible Proxy[/white]\n"
            "[dim]Playwright browser automation  ·  No paid API keys required[/dim]",
            border_style="cyan",
            padding=(0, 4),
        ))
        console.print()

    # ── step helpers ───────────────────────────────────────────────────────────

    def _step_providers(providers: dict) -> list[str]:
        choices = [
            questionary.Choice(
                title=f"{_icon(p)} {p:<12}  [dim]→ {pcfg.get('default_model','')}[/dim]",
                value=p,
                checked=pcfg.get("enabled", False),
            )
            for p, pcfg in providers.items()
        ]
        return _q(lambda: questionary.checkbox(
            "Provider da abilitare",
            choices=choices,
            style=QSTYLE,
            instruction="spazio=seleziona  ↑↓=naviga  a=tutti  invio=conferma",
        ).ask())

    def _step_models(pname: str, pcfg: dict) -> list[str]:
        models = pcfg.get("models", {})
        choices = [
            questionary.Choice(
                title=f"{mid:<48}  {mcfg.get('label','')}",
                value=mid,
                checked=mcfg.get("enabled", False),
            )
            for mid, mcfg in models.items()
        ]
        result = _q(lambda: questionary.checkbox(
            f"Modelli  [{_icon(pname)} {pname}]",
            choices=choices,
            style=QSTYLE,
            instruction="spazio=seleziona  invio=conferma",
        ).ask())
        if not result:
            default = pcfg.get("default_model")
            if default and default in models:
                console.print(f"  [dim]→ nessuna selezione, uso default: [yellow]{default}[/yellow][/dim]")
                return [default]
        return result or []

    def _step_params(pname: str, params: dict) -> dict:
        bools = {k: v for k, v in params.items() if isinstance(v, bool)}
        if not bools:
            return dict(params)
        LABELS = {
            "thinking_enabled": "thinking_enabled   chain-of-thought / ragionamento esteso",
            "search_enabled":   "search_enabled     ricerca web in tempo reale",
            "preempt":          "preempt            modalità preempt",
        }
        choices = [
            questionary.Choice(title=LABELS.get(k, k), value=k, checked=v)
            for k, v in bools.items()
        ]
        selected = _q(lambda: questionary.checkbox(
            f"Parametri  [{_icon(pname)} {pname}]",
            choices=choices,
            style=QSTYLE,
            instruction="spazio=attiva/disattiva  invio=conferma",
        ).ask()) or []
        return {**params, **{k: (k in selected) for k in bools}}

    def _step_default_model(pname: str, models: list[str], current: str | None) -> str:
        if len(models) == 1:
            return models[0]
        return _q(lambda: questionary.select(
            f"Modello di default  [{_icon(pname)} {pname}]",
            choices=models,
            default=current if current in models else models[0],
            style=QSTYLE,
        ).ask())

    # ── one-command builder ────────────────────────────────────────────────────

    def _build_one_command(
        selected: list[str],
        sel_models: dict[str, list[str]],
        default_provider: str,
        default_models: dict[str, str],
        sel_params: dict[str, dict],
        port: int,
        log_level: str,
    ) -> str:
        parts = ["python llmjack.py --no-wizard"]
        parts.append(f"--providers {','.join(selected)}")
        margs = [f"{p}:{m}" for p in selected for m in sel_models.get(p, [])]
        if margs:
            parts.append(f"--models {','.join(margs)}")
        parts.append(f"--default-provider {default_provider}")
        for p in selected:
            dm = default_models.get(p)
            if dm:
                parts.append(f"--default-model {p}:{dm}")
        for p, pp in sel_params.items():
            if pp.get("thinking_enabled"):
                parts.append(f"--thinking {p}")
            if pp.get("search_enabled"):
                parts.append(f"--search {p}")
            if pp.get("preempt"):
                parts.append(f"--preempt {p}")
        if port != 8080:
            parts.append(f"--port {port}")
        if log_level != "INFO":
            parts.append(f"--log-level {log_level}")
        return " \\\n    ".join(parts)

    # ── summary ────────────────────────────────────────────────────────────────

    def _print_summary(
        selected: list[str],
        sel_models: dict[str, list[str]],
        default_provider: str,
        sel_params: dict[str, dict],
        port: int,
        log_level: str,
        cmd: str,
    ):
        console.print()
        t = Table(
            title="Configurazione selezionata",
            box=box.ROUNDED,
            border_style="cyan",
            show_lines=True,
            title_style="bold cyan",
            padding=(0, 1),
        )
        t.add_column("Provider", style="bold", no_wrap=True)
        t.add_column("Modelli abilitati", style="yellow")
        t.add_column("Parametri")
        t.add_column("", style="cyan", no_wrap=True)

        for p in selected:
            mlist = "\n".join(sel_models.get(p, []))
            pp = sel_params.get(p, {})
            plist = "\n".join(
                f"{'[green]✓[/green]' if v else '[red]✗[/red]'} {k}"
                for k, v in pp.items() if isinstance(v, bool)
            ) or "[dim]—[/dim]"
            star = "[bold cyan]★ default[/bold cyan]" if p == default_provider else ""
            t.add_row(f"{_icon(p)} {p}", mlist, plist, star)

        console.print(t)
        console.print(
            f"\n  Porta [bold]{port}[/bold]"
            f"   Log level [bold]{log_level}[/bold]\n"
        )
        console.print(Panel(
            f"[dim]Lancia senza wizard:[/dim]\n\n[bold cyan]{cmd}[/bold cyan]",
            title="[bold green]▶  One-command[/bold green]",
            border_style="green",
            padding=(1, 2),
        ))

    # ── main wizard ────────────────────────────────────────────────────────────

    def run_setup(config: dict) -> tuple[dict, int, str]:
        """
        Wizard interattivo a 6 step.
        Ritorna (new_config, port, log_level).
        """
        _print_banner()
        providers = config.get("providers", {})

        # Step 1 — provider
        console.rule("[bold cyan]1 · Provider[/bold cyan]")
        selected = _step_providers(providers)
        if not selected:
            console.print("[yellow]Nessun provider selezionato — uscita.[/yellow]")
            sys.exit(0)

        # Step 2 — modelli per provider
        sel_models: dict[str, list[str]] = {}
        for p in selected:
            console.rule(f"[bold cyan]2 · Modelli  [{_icon(p)} {p}][/bold cyan]")
            sel_models[p] = _step_models(p, providers[p])

        # Step 3 — parametri per provider
        sel_params: dict[str, dict] = {}
        for p in selected:
            raw = dict(providers[p].get("params", {}))
            if any(isinstance(v, bool) for v in raw.values()):
                console.rule(f"[bold cyan]3 · Parametri  [{_icon(p)} {p}][/bold cyan]")
                sel_params[p] = _step_params(p, raw)
            else:
                sel_params[p] = raw

        # Step 4 — provider di default
        console.rule("[bold cyan]4 · Provider di default[/bold cyan]")
        if len(selected) == 1:
            default_provider = selected[0]
            console.print(f"  [dim]Provider di default: [bold]{default_provider}[/bold] (unico attivo)[/dim]")
        else:
            cur_dp = config.get("default_provider", selected[0])
            default_provider = _q(lambda: questionary.select(
                "Provider usato quando il modello non è riconosciuto",
                choices=selected,
                default=cur_dp if cur_dp in selected else selected[0],
                style=QSTYLE,
            ).ask())

        # Step 5 — modello di default per provider
        console.rule("[bold cyan]5 · Modello di default[/bold cyan]")
        default_models: dict[str, str] = {}
        for p in selected:
            cur_dm = providers[p].get("default_model")
            default_models[p] = _step_default_model(p, sel_models.get(p, []), cur_dm)

        # Step 6 — porta + log level
        console.rule("[bold cyan]6 · Opzioni di avvio[/bold cyan]")
        port = int(_q(lambda: questionary.text(
            "Porta  [invio = 8080]",
            default="8080",
            style=QSTYLE,
            validate=lambda v: (v.isdigit() and 1024 <= int(v) <= 65535) or "Valore 1024–65535",
        ).ask()))

        log_level = _q(lambda: questionary.select(
            "Log level",
            choices=["TRACE", "DEBUG", "INFO", "WARNING", "ERROR"],
            default="INFO",
            style=QSTYLE,
        ).ask())

        # Riepilogo
        cmd = _build_one_command(
            selected, sel_models, default_provider, default_models, sel_params, port, log_level
        )
        console.rule("[bold green]Riepilogo[/bold green]")
        _print_summary(selected, sel_models, default_provider, sel_params, port, log_level, cmd)

        # Salvataggio
        save = _q(lambda: questionary.confirm(
            "Salva questa configurazione su providers.json?",
            default=False,
            style=QSTYLE,
        ).ask())

        # Avvio
        if not _q(lambda: questionary.confirm(
            "\nAvvia il proxy?", default=True, style=QSTYLE
        ).ask()):
            console.print("[yellow]Avvio annullato.[/yellow]")
            sys.exit(0)

        # Build new config
        new_cfg: dict = {"default_provider": default_provider, "providers": {}}
        if "logging" in config:
            new_cfg["logging"] = {**config["logging"], "level": log_level}
        else:
            new_cfg["logging"] = {"level": log_level, "log_keep_sessions": 10}

        for pname, pcfg in providers.items():
            is_on = pname in selected
            new_cfg["providers"][pname] = {
                "enabled": is_on,
                "default_model": default_models.get(pname, pcfg.get("default_model")),
                "models": {
                    mid: {**mcfg, "enabled": (mid in sel_models.get(pname, []))}
                    for mid, mcfg in pcfg.get("models", {}).items()
                },
                "params": sel_params.get(pname, pcfg.get("params", {})),
            }

        if save:
            from core.config import save_config
            save_config(new_cfg)
            console.print("[green]✓ providers.json aggiornato (backup in providers.json.bak)[/green]")

        console.print()
        return new_cfg, port, log_level
```

- [ ] **Step 2: Testa import wizard (senza dipendenze)**

```bash
python -c "from wizard.setup import run_setup; print('wizard import OK')"
```

Output atteso: `wizard import OK` (con o senza questionary/rich installati)

- [ ] **Step 3: Installa dipendenze wizard**

```bash
pip install questionary rich
```

---

## Task 6: `llmjack.py`

**Files:**
- Create: `llmjack.py`

- [ ] **Step 1: Crea `llmjack.py`**

```python
#!/usr/bin/env python3
"""
llmjack — entry point del proxy multi-provider.

Usage:
    python llmjack.py                         # wizard interattivo
    python llmjack.py --wizard                # forza wizard
    python llmjack.py --no-wizard             # usa providers.json as-is
    python llmjack.py --providers claude \
        --models claude:claude-haiku-4-5-20251001 \
        --default-provider claude
"""
import argparse
import sys
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="llmjack",
        description="Multi-Provider OpenAI-Compatible Proxy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--wizard", action="store_true",
                   help="Forza wizard (ignora altri flag di config)")
    p.add_argument("--no-wizard", dest="no_wizard", action="store_true",
                   help="Skip wizard, usa providers.json as-is")
    p.add_argument("--providers", metavar="P1,P2",
                   help="Provider da abilitare, es. qwen,claude")
    p.add_argument("--models", metavar="P:M,...",
                   help="Modelli, es. claude:claude-haiku,qwen:qwen3.6-plus")
    p.add_argument("--default-provider", dest="default_provider", metavar="PROVIDER")
    p.add_argument("--default-model", dest="default_model", action="append", metavar="P:M")
    p.add_argument("--thinking", action="append", metavar="PROVIDER",
                   help="Abilita thinking_enabled per PROVIDER (ripetibile)")
    p.add_argument("--search", action="append", metavar="PROVIDER",
                   help="Abilita search_enabled per PROVIDER (ripetibile)")
    p.add_argument("--preempt", action="append", metavar="PROVIDER",
                   help="Abilita preempt per PROVIDER (ripetibile)")
    p.add_argument("--port", "-p", type=int, default=8080, metavar="PORT")
    p.add_argument("--log-level", dest="log_level",
                   choices=["TRACE", "DEBUG", "INFO", "WARNING", "ERROR"],
                   help="Override log level da providers.json")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Shorthand per --log-level DEBUG")
    p.add_argument("--log-dir", dest="log_dir", default="logs", metavar="DIR",
                   help="Cartella base dei log (default: logs/)")
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    from core.config import (
        load_config, apply_cli_overrides, get_logging_cfg, build_model_index,
    )
    from core import log_manager
    import core.server as server_module

    cfg = load_config()

    # Decide se lanciare il wizard
    run_wizard = args.wizard or (not args.no_wizard and not args.providers)

    if run_wizard:
        from wizard.setup import run_setup
        cfg, port, log_level = run_setup(cfg)
    else:
        cfg = apply_cli_overrides(cfg, args)
        port = args.port
        log_cfg = get_logging_cfg(cfg)
        if args.verbose:
            log_level = "DEBUG"
        else:
            log_level = args.log_level or log_cfg.get("level", "INFO")

    # Setup logging sessione
    log_dir = Path(args.log_dir)
    session_dir = log_manager.setup(cfg, log_level, log_dir)

    # Condividi config con server module (stesso processo)
    server_module._config.update(cfg)
    mm, me = build_model_index(cfg)
    server_module._model_map.update(mm)
    server_module._model_meta.update(me)
    server_module._config_ready = True
    server_module.VERBOSE = log_level in ("TRACE", "DEBUG")

    # Propaga DEBUG ai client
    import clients.qwen_client as qc
    import clients.deepseek_client as dc
    import clients.chatgpt_client as gc
    import clients.claude_client as cc
    dbg_on = log_level in ("TRACE", "DEBUG")
    qc.DEBUG = dc.DEBUG = gc.DEBUG = cc.DEBUG = dbg_on

    # Banner
    enabled = [p for p, c in cfg.get("providers", {}).items() if c.get("enabled")]
    log_manager.log("━" * 48)
    log_manager.log("  llmjack — Multi-Provider OpenAI Proxy")
    log_manager.log("━" * 48)
    log_manager.log(f"  Provider attivi : {', '.join(enabled)}")
    log_manager.log(f"  Base URL        : http://localhost:{port}/v1")
    log_manager.log(f"  Log sessione    : {session_dir}/")
    log_manager.log(f"  Log level       : {log_level}")
    log_manager.log(f"  Reset sessione  : DELETE http://localhost:{port}/v1/session")
    log_manager.log("━" * 48)
    log_manager.log("")

    import uvicorn
    uvicorn.run(server_module.app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Testa help**

```bash
python llmjack.py --help
```

Output atteso: usage con tutti i flag descritti.

- [ ] **Step 3: Testa avvio no-wizard (non avvia il server, solo config)**

```bash
python -c "
import sys
sys.argv = ['llmjack.py', '--no-wizard', '--providers', 'claude']
# Importa solo il parse per non avviare uvicorn
from llmjack import _parse_args
from core.config import load_config, apply_cli_overrides
args = _parse_args()
cfg = load_config()
cfg = apply_cli_overrides(cfg, args)
enabled = [p for p,c in cfg['providers'].items() if c.get('enabled')]
print('enabled:', enabled)
assert 'claude' in enabled
print('OK')
"
```

---

## Task 7: `proxy.py` thin wrapper

**Files:**
- Modify: `proxy.py`

- [ ] **Step 1: Sostituisci il contenuto di proxy.py**

```python
#!/usr/bin/env python3
"""
DEPRECATO — usa llmjack.py

Questo wrapper esiste solo per backward compatibility.
Avvia llmjack.py --no-wizard passando tutti gli argomenti ricevuti.
"""
import subprocess
import sys

print("[!] proxy.py è deprecato — usa: python llmjack.py", flush=True)
result = subprocess.run(
    [sys.executable, "llmjack.py", "--no-wizard"] + sys.argv[1:]
)
sys.exit(result.returncode)
```

- [ ] **Step 2: Verifica che il wrapper stampi il warning**

```bash
python proxy.py --help 2>&1 | head -3
```

Output atteso: prima riga `[!] proxy.py è deprecato — usa: python llmjack.py`

---

## Task 8: Aggiorna client debug output

I quattro client hanno tutti la stessa funzione `dbg()` che stampa su stderr. La rendiamo compatibile con `log_manager` con un import opzionale (così i client funzionano anche se chiamati standalone).

**Files:**
- Modify: `clients/qwen_client.py`
- Modify: `clients/deepseek_client.py`
- Modify: `clients/chatgpt_client.py`
- Modify: `clients/claude_client.py`

- [ ] **Step 1: Aggiorna `dbg()` in `clients/qwen_client.py`**

Trova la funzione `dbg()` e sostituiscila:

```python
def dbg(msg: str):
    if not DEBUG:
        return
    try:
        from core.log_manager import tlog
        tlog(f"[QWEN] {msg}")
    except ImportError:
        print(f"[QWEN-DEBUG {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)
```

- [ ] **Step 2: Stessa modifica in `clients/deepseek_client.py`**

```python
def dbg(msg: str):
    if not DEBUG:
        return
    try:
        from core.log_manager import tlog
        tlog(f"[DS] {msg}")
    except ImportError:
        print(f"[DS-DEBUG {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)
```

- [ ] **Step 3: Stessa modifica in `clients/chatgpt_client.py`**

```python
def dbg(msg: str):
    if not DEBUG:
        return
    try:
        from core.log_manager import tlog
        tlog(f"[GPT] {msg}")
    except ImportError:
        print(f"[GPT-DEBUG {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)
```

- [ ] **Step 4: Stessa modifica in `clients/claude_client.py`**

```python
def dbg(msg: str):
    if not DEBUG:
        return
    try:
        from core.log_manager import tlog
        tlog(f"[CLAUDE] {msg}")
    except ImportError:
        print(f"[CLAUDE-DEBUG {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)
```

- [ ] **Step 5: Verifica standalone client funziona ancora**

```bash
python -c "from clients.claude_client import ClaudeAIClient; print('client import OK')"
```

---

## Task 9: Aggiorna CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Aggiorna sezione Run commands**

Sostituisci la sezione `## Run commands` con:

```markdown
## Run commands

```bash
# Avvio interattivo (wizard)
python llmjack.py

# Avvio non interattivo (usa providers.json as-is)
python llmjack.py --no-wizard

# Avvio con override da CLI (skip wizard)
python llmjack.py --providers claude \
    --models claude:claude-haiku-4-5-20251001 \
    --default-provider claude \
    --port 8080

# Forza wizard anche se ci sono altri arg
python llmjack.py --wizard

# Avvio verbose
python llmjack.py --no-wizard --verbose          # shorthand DEBUG
python llmjack.py --no-wizard --log-level TRACE  # massimo dettaglio

# Test client standalone (invariati)
python clients/qwen_client.py "Ciao"
python clients/claude_client.py "Ciao"

# Reset sessioni (invariato)
curl -X DELETE http://localhost:8080/v1/session
curl -X DELETE http://localhost:8080/v1/session/claude

# Backward compat (deprecato, mostra warning)
python proxy.py --no-wizard
```
```

- [ ] **Step 2: Aggiorna sezione Architecture**

Aggiungi sotto il diagramma esistente:

```markdown
**Entry point:** `llmjack.py` — orchestra config, wizard, logging, uvicorn. `proxy.py` è un thin wrapper deprecato.

**`core/config.py`** — funzioni pure per load/save `providers.json`, build model index, apply CLI overrides.

**`core/log_manager.py`** — logging per sessione. Livelli: TRACE(5) · DEBUG · INFO · WARNING · ERROR · CRITICAL. Ogni sessione in `logs/YYYY-MM-DD_HH-MM-SS/` con `proxy.log`, `errors.log`, `config.json`. Rotation automatica (mantiene ultimi N, da `providers.json["logging"]["log_keep_sessions"]`).

**`wizard/setup.py`** — TUI a 6 step con `questionary`+`rich`. Legge providers.json, chiede selezioni, genera one-command string. Opzionalmente sovrascrive providers.json al termine (con backup `.bak`). Graceful fallback se deps non installate.
```

- [ ] **Step 3: Aggiorna sezione Install dependencies**

```markdown
## Install dependencies

```bash
pip install fastapi uvicorn playwright questionary rich
playwright install chromium
```

`questionary` e `rich` sono opzionali — senza di esse il proxy parte in modalità `--no-wizard`.
```

- [ ] **Step 4: Aggiorna sezione Key gotchas con `--model` CLI bug fix**

Rimuovi la nota `**\`--model\` CLI flag is banner-only**` — non esiste più in llmjack.py.

---

## Verifica finale integrazione

- [ ] **Test 1: Import completo della catena**

```bash
python -c "
from core.config import load_config, build_model_index
from core.log_manager import setup, log
from wizard.setup import run_setup
print('all imports OK')
"
```

- [ ] **Test 2: Avvio no-wizard reale**

```bash
python llmjack.py --no-wizard --port 8081 &
sleep 3
curl -s http://localhost:8081/v1/models | python -m json.tool | head -5
kill %1
```

Output atteso: JSON con lista modelli da providers.json.

- [ ] **Test 3: Verifica log sessione**

Dopo il Test 2, controlla:

```bash
ls logs/
ls logs/$(ls logs/ | tail -1)/
```

Output atteso: cartella timestamp + `config.json errors.log proxy.log`

- [ ] **Test 4: Verifica proxy.py backward compat**

```bash
python proxy.py --help 2>&1 | head -1
```

Output atteso: `[!] proxy.py è deprecato — usa: python llmjack.py`
