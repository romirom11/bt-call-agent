"""Where things live: API key, prompt, settings, transcripts."""
from __future__ import annotations

import json
import os
import pathlib
import re
import secrets

APP = "bt-call-agent"
DATA = pathlib.Path(__file__).resolve().parent / "data"
DEFAULT_MODEL = "gemini-3.8-live"

# Call settings: command-line flags > ~/.config/bt-call-agent/config.json > these.
DEFAULTS: dict = {
    "model": DEFAULT_MODEL,
    "voice": None,          # prebuilt Gemini voice; None = model default
    "language": None,       # BCP-47 speech language hint
    "greet": True,
    "greet_timeout": 6.0,
    "out_volume": 0.7,
}
VOICES = ["Puck", "Charon", "Kore", "Fenrir", "Aoede", "Leda", "Orus", "Zephyr"]


def xdg(var: str, fallback: str) -> pathlib.Path:
    return pathlib.Path(os.environ.get(var) or pathlib.Path.home() / fallback)


def config_dir() -> pathlib.Path:
    return xdg("XDG_CONFIG_HOME", ".config") / APP


def key_path() -> pathlib.Path:
    return config_dir() / "key"


def settings_path() -> pathlib.Path:
    return config_dir() / "config.json"


def user_prompt_path() -> pathlib.Path:
    return config_dir() / "prompt.txt"


def presets_dir() -> pathlib.Path:
    return config_dir() / "prompts"


def state_dir() -> pathlib.Path:
    return xdg("XDG_STATE_HOME", ".local/state") / APP


def write_private(path: pathlib.Path, text: str) -> None:
    """Write a file only the user can read, atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- API key

def key_source(key_file: str | None = None, env: bool = True) -> str | None:
    """Where the key comes from: 'flag', 'env', 'file' or None."""
    if key_file:
        return "flag"
    if env and os.environ.get("GEMINI_API_KEY"):
        return "env"
    if key_path().is_file() and key_path().read_text().strip():
        return "file"
    return None


def resolve_api_key(key_file: str | None, env: bool = True) -> str | None:
    """--key-file > $GEMINI_API_KEY > ~/.config/bt-call-agent/key"""
    if key_file:
        return pathlib.Path(key_file).expanduser().read_text().strip() or None
    if env and os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"].strip()
    if key_path().is_file():
        return key_path().read_text().strip() or None
    return None


def save_api_key(key: str) -> None:
    key = key.strip()
    if not key or any(c.isspace() for c in key):
        raise ValueError("the API key must be one word with no spaces")
    write_private(key_path(), key + "\n")


# --------------------------------------------------------------------------- prompt

def prompt_path(cli_path: str | None) -> pathlib.Path:
    """--prompt > ~/.config/bt-call-agent/prompt.txt > bundled default"""
    if cli_path:
        return pathlib.Path(cli_path).expanduser()
    user = user_prompt_path()
    return user if user.is_file() else DATA / "default-prompt.txt"


def save_prompt(text: str) -> pathlib.Path:
    if not text.strip():
        raise ValueError("the prompt is empty")
    path = user_prompt_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
    return path


PRESET_NAME = re.compile(r"^[\w][\w .-]{0,63}$")


def _preset_file(name: str) -> pathlib.Path:
    name = name.strip()
    if not PRESET_NAME.match(name) or ".." in name:
        raise ValueError("use letters, digits, spaces, dots and dashes in the name (max 64)")
    return presets_dir() / f"{name}.txt"


def list_presets() -> list[dict]:
    """Bundled examples first, then the user's saved prompts."""
    out = [{"name": "default", "builtin": True,
             "text": (DATA / "default-prompt.txt").read_text(encoding="utf-8")}]
    for builtin, folder in ((True, DATA / "prompts"), (False, presets_dir())):
        if folder.is_dir():
            for f in sorted(folder.glob("*.txt")):
                out.append({"name": f.stem, "builtin": builtin,
                            "text": f.read_text(encoding="utf-8")})
    return out


def save_preset(name: str, text: str) -> None:
    if not text.strip():
        raise ValueError("the prompt is empty")
    f = _preset_file(name)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text.rstrip() + "\n", encoding="utf-8")


def delete_preset(name: str) -> bool:
    f = _preset_file(name)
    if f.is_file():
        f.unlink()
        return True
    return False


# --------------------------------------------------------------------------- settings

def validate_settings(raw: dict) -> dict:
    """Check and normalise a (partial) settings dict. Unknown keys are an error."""
    out = {}
    for k, v in raw.items():
        if k not in DEFAULTS:
            raise ValueError(f"unknown setting: {k}")
        if k == "model":
            if not isinstance(v, str) or not v.strip():
                raise ValueError("model must be a non-empty string")
            v = v.strip()
        elif k in ("voice", "language"):
            if v is not None and not isinstance(v, str):
                raise ValueError(f"{k} must be a string")
            v = (v or "").strip() or None
        elif k == "greet":
            if not isinstance(v, bool):
                raise ValueError("greet must be true or false")
        elif k == "greet_timeout":
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 60:
                raise ValueError("greet_timeout must be between 0 and 60 seconds")
            v = float(v)
        elif k == "out_volume":
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
                raise ValueError("out_volume must be between 0 and 1")
            v = float(v)
        out[k] = v
    return out


def load_settings() -> dict:
    """Settings saved in config.json (only the keys that are set there)."""
    try:
        raw = json.loads(settings_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise ValueError(f"{settings_path()}: {e}") from e
    if not isinstance(raw, dict):
        raise ValueError(f"{settings_path()}: expected a JSON object")
    return validate_settings({k: v for k, v in raw.items() if k in DEFAULTS})


def save_settings(changes: dict) -> dict:
    """Merge changes into config.json; a None value removes the key (back to the default)."""
    reset = [k for k, v in changes.items() if v is None]
    merged = {**load_settings(),
              **validate_settings({k: v for k, v in changes.items() if v is not None})}
    for k in reset:
        if k not in DEFAULTS:
            raise ValueError(f"unknown setting: {k}")
        merged.pop(k, None)
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".config.json.tmp")
    tmp.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return merged


def effective_settings(overrides: dict | None = None) -> dict:
    """Defaults, then config.json, then command-line overrides."""
    return {**DEFAULTS, **load_settings(), **(overrides or {})}


# --------------------------------------------------------------------------- web UI

def web_token_path() -> pathlib.Path:
    return config_dir() / "web-token"


def web_token() -> str:
    """The web UI's access token, created on first use."""
    p = web_token_path()
    if p.is_file() and (t := p.read_text().strip()):
        return t
    t = secrets.token_urlsafe(24)
    write_private(p, t + "\n")
    return t
