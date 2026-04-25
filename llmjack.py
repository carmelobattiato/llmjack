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


def _print_quick_help() -> None:
    rows = [
        ("--no-wizard",              "avvia senza wizard, usa providers.json as-is"),
        ("--providers qwen,claude",  "abilita solo questi provider"),
        ("--models p:model,...",     "specifica modelli (es. claude:claude-haiku-4-5-20251001)"),
        ("--default-provider p",     "provider di default per richieste senza model"),
        ("--port 8080",              "porta di esposizione (default 8080)"),
        ("--log-level INFO",         "livello log: TRACE · DEBUG · INFO · WARNING · ERROR"),
        ("--verbose / -v",           "shorthand per --log-level DEBUG"),
        ("--wizard",                 "rilancia il wizard in qualsiasi momento"),
    ]
    try:
        from rich.console import Console
        from rich.table import Table
        from rich import box
        c = Console()
        t = Table(box=box.ROUNDED, border_style="dim", show_header=False,
                  padding=(0, 1), title="Parametri principali",
                  title_style="bold dim", title_justify="left")
        t.add_column(style="bold yellow", no_wrap=True)
        t.add_column(style="dim")
        for flag, desc in rows:
            t.add_row(flag, desc)
        c.print(t)
        c.print()
    except ImportError:
        print("\nParametri principali:")
        for flag, desc in rows:
            print(f"  {flag:<36} {desc}")
        print()


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
        if not args.wizard:
            _print_quick_help()
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
