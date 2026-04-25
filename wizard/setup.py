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
                title=f"{_icon(p)} {p:<12}  → {pcfg.get('default_model','')}",
                value=p,
                checked=False,
            )
            for p, pcfg in providers.items()
        ]
        console.print("  [dim]↑↓ naviga · [bold]SPAZIO[/bold] seleziona/deseleziona · [bold]INVIO[/bold] conferma[/dim]")
        return _q(lambda: questionary.checkbox(
            "Provider da abilitare",
            choices=choices,
            style=QSTYLE,
            instruction="",
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
        console.print("  [dim]↑↓ naviga · [bold]SPAZIO[/bold] seleziona/deseleziona · [bold]INVIO[/bold] conferma[/dim]")
        result = _q(lambda: questionary.checkbox(
            f"Modelli  [{_icon(pname)} {pname}]",
            choices=choices,
            style=QSTYLE,
            instruction="",
        ).ask())
        if not result:
            default = pcfg.get("default_model")
            if default and default in models:
                console.print(f"  nessuna selezione, uso default: {default}")
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
        console.print("  [dim]↑↓ naviga · [bold]SPAZIO[/bold] attiva/disattiva · [bold]INVIO[/bold] conferma[/dim]")
        selected = _q(lambda: questionary.checkbox(
            f"Parametri  [{_icon(pname)} {pname}]",
            choices=choices,
            style=QSTYLE,
            instruction="",
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
            param_lines = []
            for k, v in pp.items():
                if isinstance(v, bool):
                    icon = "✓" if v else "✗"
                    param_lines.append(f"{icon} {k}")
            plist = "\n".join(param_lines) if param_lines else "—"
            star = "★ default" if p == default_provider else ""
            t.add_row(f"{_icon(p)} {p}", mlist, plist, star)

        console.print(t)
        console.print(f"\n  Porta {port}   Log level {log_level}\n")
        console.print(Panel(
            f"Lancia senza wizard:\n\n{cmd}",
            title="▶  One-command",
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
        console.print(
            "  ✓ Selezionati: "
            + "  ".join(f"{_icon(p)} [bold]{p}[/bold]" for p in selected)
            + "\n"
        )

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
            console.print(f"  Provider di default: {default_provider} (unico attivo)")
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
            validate=lambda v: (v.isdigit() and 1024 <= int(v) <= 65535) or "Valore 1024-65535",
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
            console.print("✓ providers.json aggiornato (backup in providers.json.bak)")

        console.print()
        return new_cfg, port, log_level
