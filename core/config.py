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
