"""Where things live: API key, prompt, transcripts."""
from __future__ import annotations

import os
import pathlib

APP = "bt-call-agent"
DATA = pathlib.Path(__file__).resolve().parent / "data"
DEFAULT_MODEL = "gemini-3.8-live"


def xdg(var: str, fallback: str) -> pathlib.Path:
    return pathlib.Path(os.environ.get(var) or pathlib.Path.home() / fallback)


def config_dir() -> pathlib.Path:
    return xdg("XDG_CONFIG_HOME", ".config") / APP


def key_path() -> pathlib.Path:
    return config_dir() / "key"


def state_dir() -> pathlib.Path:
    return xdg("XDG_STATE_HOME", ".local/state") / APP


def resolve_api_key(key_file: str | None, env: bool = True) -> str | None:
    """--key-file > $GEMINI_API_KEY > ~/.config/bt-call-agent/key"""
    if key_file:
        return pathlib.Path(key_file).expanduser().read_text().strip() or None
    if env and os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"].strip()
    if key_path().is_file():
        return key_path().read_text().strip() or None
    return None


def prompt_path(cli_path: str | None) -> pathlib.Path:
    """--prompt > ~/.config/bt-call-agent/prompt.txt > bundled default"""
    if cli_path:
        return pathlib.Path(cli_path).expanduser()
    user = config_dir() / "prompt.txt"
    return user if user.is_file() else DATA / "default-prompt.txt"
