"""bt-call-agent command line."""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

from . import __version__, config, system


def cmd_run(a) -> int:
    from . import bridge
    key = config.resolve_api_key(a.key_file)
    if not key:
        print(f"No Gemini API key: set GEMINI_API_KEY, pass --key-file, or put it in {config.key_path()}",
              file=sys.stderr)
        return 2
    prompt_file = config.prompt_path(a.prompt)
    opts = bridge.Options(
        model=a.model,
        prompt=prompt_file.read_text(encoding="utf-8"),
        transcripts_dir=pathlib.Path(a.transcripts).expanduser() if a.transcripts
        else config.state_dir() / "transcripts",
        voice=a.voice, language=a.language,
        greet=not a.no_greet, greet_timeout=a.greet_timeout,
        out_volume=a.out_volume, once=a.once,
    )
    bridge.log(f"prompt: {prompt_file}")
    try:
        asyncio.run(bridge.serve(key, opts))
    except KeyboardInterrupt:
        pass
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="bt-call-agent",
        description="Let an AI agent talk in your phone calls: this Linux machine pretends to be "
                    "a Bluetooth headset, and call audio is bridged to Gemini Live.")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="wait for calls and let the agent talk")
    r.add_argument("--model", default=config.DEFAULT_MODEL)
    r.add_argument("--prompt", help="system prompt file (default: ~/.config/bt-call-agent/prompt.txt "
                                    "or the bundled one)")
    r.add_argument("--voice", help="prebuilt Gemini voice, e.g. Kore, Puck, Aoede")
    r.add_argument("--language", help="speech language hint (BCP-47, e.g. el-GR); "
                                      "the prompt is what actually decides the language")
    r.add_argument("--no-greet", action="store_true", help="don't speak first; wait for the other side")
    r.add_argument("--greet-timeout", type=float, default=6.0,
                   help="greet after this many seconds if no audio re-route (answer) was seen")
    r.add_argument("--out-volume", type=float, default=0.7, help="agent voice level into the call, 0-1")
    r.add_argument("--key-file", help="file with the Gemini API key")
    r.add_argument("--transcripts", help="directory for call transcripts "
                                         "(default: ~/.local/state/bt-call-agent/transcripts)")
    r.add_argument("--once", action="store_true", help="exit after the first call")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("setup", help="switch this machine's Bluetooth into headset mode (uses sudo)")
    s.add_argument("action", choices=["enable", "disable", "status"])
    s.set_defaults(func=lambda a: system.setup_script(a.action))

    p = sub.add_parser("pair", help="make this machine discoverable and pair a phone")
    p.add_argument("--timeout", type=int, default=180)
    p.set_defaults(func=lambda a: system.pair(a.timeout))

    d = sub.add_parser("doctor", help="check Bluetooth, PipeWire, pairing and API key")
    d.set_defaults(func=lambda a: system.doctor())

    i = sub.add_parser("install-service", help="install a systemd --user service running `run`",
                       description="Extra arguments after -- are passed to `run`, "
                                   "e.g. install-service -- --voice Kore")
    i.add_argument("run_args", nargs=argparse.REMAINDER)
    i.set_defaults(func=lambda a: system.install_service(
        [x for x in a.run_args if x != "--"]))

    a = ap.parse_args(argv)
    return a.func(a)
