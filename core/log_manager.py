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
