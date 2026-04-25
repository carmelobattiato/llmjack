# Design: llmjack CLI Wizard + Session Logging

**Date:** 2026-04-25  
**Scope:** Interactive TUI wizard for providers.json configuration + structured per-session logging  
**Status:** Approved

---

## 1. Goals

1. `llmjack.py` diventa l'entry point unico dell'applicazione.
2. Senza parametri (o con `--wizard`) lancia un wizard TUI interattivo che configura `providers.json`.
3. Ogni sessione proxy scrive log strutturati in `logs/<timestamp>/` con livelli configurabili.
4. Tutti i parametri del wizard sono anche configurabili via CLI flag (per avvio non interattivo).

---

## 2. Struttura cartelle

```
llmjack/
├── llmjack.py              ← entry point (CLI parsing, wizard trigger, uvicorn start)
├── providers.json          ← config (aggiunta sezione "logging")
├── proxy.py                ← deprecato, thin wrapper → llmjack.py (backward compat)
├── backup.sh
├── CLAUDE.md
├── README.md
│
├── core/
│   ├── __init__.py
│   ├── server.py           ← FastAPI app + routes (contenuto attuale di proxy.py)
│   ├── config.py           ← load/save providers.json, build model index, CLI overrides
│   └── log_manager.py      ← session logging setup, rotation, livelli custom
│
├── clients/                ← invariato
│   ├── __init__.py
│   ├── qwen_client.py
│   ├── deepseek_client.py
│   ├── chatgpt_client.py
│   └── claude_client.py
│
├── wizard/
│   ├── __init__.py
│   └── setup.py            ← TUI interattivo (questionary + rich)
│
├── logs/                   ← gitignored
│   └── YYYY-MM-DD_HH-MM-SS/
│       ├── proxy.log
│       ├── errors.log
│       └── config.json
│
└── data/                   ← invariato (sessioni + profili Chrome, gitignored)
```

---

## 3. `llmjack.py` — Entry point

### CLI flags

| Flag | Tipo | Default | Descrizione |
|------|------|---------|-------------|
| *(nessun arg)* | — | — | avvia wizard |
| `--wizard` | flag | — | forza wizard; gli override CLI (`--providers`, `--models` ecc.) vengono ignorati perché il wizard ri-configura tutto |
| `--no-wizard` | flag | — | skip wizard, usa providers.json as-is |
| `--providers` | string | — | es. `qwen,claude` — abilita solo questi provider |
| `--models` | string | — | es. `claude:claude-haiku,qwen:qwen3.6-plus` |
| `--default-provider` | string | — | provider di default per richieste senza model |
| `--default-model` | string (ripetibile) | — | es. `--default-model claude:claude-haiku` |
| `--thinking` | string (ripetibile) | — | abilita thinking per il provider specificato |
| `--search` | string (ripetibile) | — | abilita search per il provider specificato |
| `--preempt` | string (ripetibile) | — | abilita preempt per il provider specificato |
| `--port` | int | 8080 | porta di esposizione |
| `--log-level` | string | da providers.json | `TRACE\|DEBUG\|INFO\|WARNING\|ERROR` |
| `--verbose` | flag | — | shorthand per `--log-level DEBUG` |
| `--log-dir` | path | `./logs` | cartella base dei log |

### Logica di avvio

```
1. parse_args()
2. cfg = load_config("providers.json")
3. if --wizard OR (no --no-wizard AND no --providers):
       cfg, port, log_level = wizard.run_setup(cfg)
   else:
       cfg = apply_cli_overrides(cfg, args)
       port = args.port
       log_level = args.log_level or cfg["logging"]["level"]
4. session_dir = log_manager.setup(cfg, log_level, base_dir=args.log_dir)
5. print_banner(cfg, port, session_dir)
6. uvicorn.run("core.server:app", host="0.0.0.0", port=port)
```

### Backward compatibility

`proxy.py` diventa un thin wrapper:
```python
import subprocess, sys
print("[!] proxy.py è deprecato — usa llmjack.py")
subprocess.run([sys.executable, "llmjack.py", "--no-wizard"] + sys.argv[1:])
```

---

## 4. `wizard/setup.py` — TUI interattivo

### Dipendenze nuove

```bash
pip install questionary rich
```

### Step del wizard

```
Step 1 · Provider       checkbox: seleziona provider abilitati (pre-checked da providers.json)
Step 2 · Modelli        checkbox per ciascun provider selezionato
Step 3 · Parametri      checkbox (thinking_enabled, search_enabled, preempt) per provider
Step 4 · Default        select: provider di default + modello di default per ciascuno
Step 5 · Avvio          text: porta [8080] · select: log_level [INFO]
Step 6 · Riepilogo      tabella rich + one-command string · confirm: salva su providers.json?
```

### Output del wizard

Restituisce a `llmjack.py`:
- `new_config: dict` — config modificata (già con tutte le scelte)
- `port: int`
- `log_level: str`

### Salvataggio condizionale

Alla fine del wizard: `"Salva questa configurazione su providers.json? [y/N]"`.
- Sì → sovrascrive `providers.json` (backup in `providers.json.bak` prima — sempre un singolo file, sovrascrive il .bak precedente)
- No → config usata solo per la sessione corrente, providers.json non toccato

### One-command generato (esempio)

```
python llmjack.py --no-wizard \
    --providers claude \
    --models claude:claude-haiku-4-5-20251001 \
    --default-provider claude \
    --default-model claude:claude-haiku-4-5-20251001 \
    --port 8080
```

Stampato in un `rich.Panel` verde a fine wizard, prima della conferma di avvio.

### Gestione assenza dipendenze

Se `questionary` o `rich` non sono installati:
```
[!] Dipendenze wizard mancanti: pip install questionary rich
[!] Avvio in modalità --no-wizard (providers.json as-is)
```
Il proxy parte comunque con la config corrente.

---

## 5. `core/config.py` — Config management

Estrae da `server.py` tutta la logica di config:

```python
def load_config(path: Path) -> dict: ...
def save_config(cfg: dict, path: Path) -> None: ...     # con backup .bak
def build_model_index(cfg: dict) -> tuple[dict, dict]: ...
def apply_cli_overrides(cfg: dict, args) -> dict: ...
def default_model(cfg: dict, model_map: dict) -> str: ...
def provider_for(model_id: str, model_map: dict, cfg: dict) -> str: ...
```

---

## 6. `core/log_manager.py` — Session logging

### Livelli di log

| Livello | Valore numerico | Uso |
|---------|----------------|-----|
| `TRACE` | 5 (custom) | browser automation detail, SSE chunk parsing |
| `DEBUG` | 10 | verbose Playwright, route intercept |
| `INFO` | 20 | request/response, timing, avvio/shutdown |
| `WARNING` | 30 | fallback silenzioso, session expired |
| `ERROR` | 40 | eccezioni, provider failure |
| `CRITICAL` | 50 | crash, config invalida |

### File per sessione

```
logs/2026-04-25_14-30-00/
    proxy.log       ← livello configurato + sopra (es. INFO → INFO, WARNING, ERROR, CRITICAL)
    errors.log      ← solo WARNING + ERROR + CRITICAL
    config.json     ← snapshot config esatta usata per la sessione
```

### `providers.json` — nuova sezione `logging`

```json
{
  "default_provider": "claude",
  "logging": {
    "level": "INFO",
    "log_keep_sessions": 10
  },
  "providers": { ... }
}
```

Valori di default se la sezione è assente: `level="INFO"`, `log_keep_sessions=10`.

### Rotation

All'avvio, dopo aver creato la nuova sessione: conta le cartelle in `logs/`, ordina per nome (timestamp), cancella le più vecchie se il totale supera `log_keep_sessions`.

### Cosa viene loggato (livello INFO)

**Ogni request:**
```
2026-04-25 14:30:01  [→] claude/claude-haiku-4-5-20251001 | q=47chars | stream=True
```

**Ogni response:**
```
2026-04-25 14:30:04  [←] claude | 312chars | 2.8s | "La capitale d'Italia è Roma…"
```

**Avvio sessione:**
```
2026-04-25 14:30:00  [START] port=8080 providers=[claude] log=logs/2026-04-25_14-30-00/
```

**DEBUG aggiunge:** messaggi `[v]` verbose, route intercept, SSE parse detail  
**TRACE aggiunge:** ogni chunk SSE, JS injector output, Playwright network events

### API pubblica

```python
def setup(cfg: dict, level: str, base_dir: Path) -> Path:
    """Crea session dir, configura logger, rotation. Ritorna session_dir."""

def get_logger() -> logging.Logger:
    """Ritorna il logger 'llmjack' da qualsiasi modulo."""

def log(msg: str) -> None:     # INFO + stdout
def vlog(msg: str) -> None:    # DEBUG + stdout (se level<=DEBUG)
def tlog(msg: str) -> None:    # TRACE → sempre su file; su stdout solo se log level corrente è TRACE
def elog(msg: str) -> None:    # ERROR + stdout + errors.log
```

I client esistenti (`qwen_client.py` etc.) usano `log_manager.get_logger()` invece di `print()` per i messaggi DEBUG.

---

## 7. `core/server.py` — FastAPI app

Contenuto attuale di `proxy.py` con queste modifiche:
- Rimuove `_load_config()`, `_build_model_index()`, `_parse_args()` → ora in `core/config.py`
- Rimuove `log()`, `vlog()` → ora in `core/log_manager.py`
- `lifespan()` non ricarica config (già impostata da `llmjack.py` prima di uvicorn.run)
- `_ask_sync()` e `_stream_sync()` aggiungono timing e log strutturato
- Aggiunge `_config_ready: bool` flag per evitare doppio load

---

## 8. Migrazione

### Ordine di implementazione

1. Creare struttura cartelle (`core/`, `wizard/`, `logs/`)
2. `core/config.py` — estrarre logica config da proxy.py
3. `core/log_manager.py` — nuovo modulo logging
4. `core/server.py` — proxy.py adattato (import da config e log_manager)
5. `wizard/setup.py` — TUI completo
6. `llmjack.py` — entry point che orchestra tutto
7. `proxy.py` → thin wrapper deprecato
8. Aggiornare `providers.json` con sezione `logging`
9. Aggiornare `CLAUDE.md` con nuovi comandi e struttura

### Test manuale per ciascun step

- Dopo step 4: `python -c "from core.server import app"` senza errori
- Dopo step 5: `python wizard/setup.py` lancia il wizard standalone
- Dopo step 6: `python llmjack.py` lancia wizard, `python llmjack.py --no-wizard` parte diretto
- Dopo step 7: `python proxy.py` mostra warning e parte lo stesso

---

## 9. Dipendenze aggiornate

```bash
pip install fastapi uvicorn playwright questionary rich
playwright install chromium
```

`questionary` e `rich` sono **opzionali per il wizard** — il proxy funziona senza.
