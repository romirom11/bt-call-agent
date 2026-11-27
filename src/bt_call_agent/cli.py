"""bt-call-agent command line."""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

from . import __version__, config, system


def overrides(a) -> dict:
    """Call settings given on the command line; they win over config.json."""
    return {k: getattr(a, k) for k in config.DEFAULTS if getattr(a, k, None) is not None}


def parse_addr(addr: str) -> tuple[str, int]:
    """'8080' | 'host:8080' | '[::1]:8080' -> (host, port)"""
    host, _, port = addr.rpartition(":")
    host = host.strip("[]") or "127.0.0.1"
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"bad --web address {addr!r}: expected [HOST:]PORT")
    return host, int(port)


def cmd_run(a) -> int:
    from . import bridge
    web_addr = None
    if a.web:
        try:
            web_addr = parse_addr(a.web)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
    key_error = []

    def get_key():
        try:
            key = config.resolve_api_key(a.key_file)
            key_error.clear()
            return key
        except OSError as e:           # read before every call: don't die on a moved --key-file
            if not key_error:
                bridge.log(f"can't read the API key: {e}")
            key_error.append(e)
            return None
    if not get_key() and not web_addr:
        print(f"No Gemini API key: set GEMINI_API_KEY, pass --key-file, put it in {config.key_path()}, "
              "or start with --web and set it there", file=sys.stderr)
        return 2
    try:
        config.load_settings()
    except ValueError as e:
        print(f"bad settings file: {e}", file=sys.stderr)
        return 2
    cli = overrides(a)
    transcripts_dir = (pathlib.Path(a.transcripts).expanduser() if a.transcripts
                       else config.state_dir() / "transcripts")

    def make_opts() -> bridge.Options:
        try:
            s = config.effective_settings(cli)
        except ValueError as e:        # a broken config.json: keep going on defaults + flags
            bridge.log(f"ignoring settings file: {e}")
            s = {**config.DEFAULTS, **cli}
        prompt_file = config.prompt_path(a.prompt)
        bridge.log(f"prompt: {prompt_file}")
        return bridge.Options(
            model=s["model"], prompt=prompt_file.read_text(encoding="utf-8"),
            transcripts_dir=transcripts_dir, voice=s["voice"], language=s["language"],
            greet=s["greet"], greet_timeout=s["greet_timeout"], out_volume=s["out_volume"],
            prompt_source=str(prompt_file))

    async def main():
        runner = None
        if web_addr:
            from . import web
            runner = await web.start(*web_addr, web.Context(
                transcripts_dir=transcripts_dir, cli_overrides=cli,
                cli_prompt=a.prompt, key_file=a.key_file))
        try:
            await bridge.serve(get_key, make_opts, once=a.once)
        finally:
            if runner:
                await runner.cleanup()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except OSError as e:               # e.g. the web port is taken
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="bt-call-agent",
        description="Let an AI agent talk in your phone calls: this Linux machine pretends to be "
                    "a Bluetooth headset, and call audio is bridged to Gemini Live.")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="wait for calls and let the agent talk")
    r.add_argument("--model", help=f"Gemini Live model (default: {config.DEFAULT_MODEL})")
    r.add_argument("--prompt", help="system prompt file (default: ~/.config/bt-call-agent/prompt.txt "
                                    "or the bundled one)")
    r.add_argument("--voice", help="prebuilt Gemini voice, e.g. Kore, Puck, Aoede")
    r.add_argument("--language", help="speech language hint (BCP-47, e.g. el-GR); "
                                      "the prompt is what actually decides the language")
    r.add_argument("--no-greet", dest="greet", action="store_const", const=False,
                   help="don't speak first; wait for the other side")
    r.add_argument("--greet-timeout", type=float,
                   help="greet after this many seconds if no audio re-route (answer) was seen "
                        "(default: 6)")
    r.add_argument("--out-volume", type=float, help="agent voice level into the call, 0-1 (default: 0.7)")
    r.add_argument("--key-file", help="file with the Gemini API key")
    r.add_argument("--transcripts", help="directory for call transcripts "
                                         "(default: ~/.local/state/bt-call-agent/transcripts)")
    r.add_argument("--once", action="store_true", help="exit after the first call")
    r.add_argument("--web", nargs="?", const="127.0.0.1:8080", metavar="[HOST:]PORT",
                   help="also serve the web UI (default 127.0.0.1:8080; 0.0.0.0:8080 for the LAN)")
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

    w = sub.add_parser("web-url", help="print the web UI's address with its access token")
    w.add_argument("addr", nargs="?", default="127.0.0.1:8080", metavar="[HOST:]PORT")
    w.set_defaults(func=cmd_web_url)

    a = ap.parse_args(argv)
    if a.cmd == "run":
        try:
            config.validate_settings(overrides(a))
        except ValueError as e:
            ap.error(str(e))
    return a.func(a)


def cmd_web_url(a) -> int:
    from .web import urls
    try:
        host, port = parse_addr(a.addr)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    for u in urls(host, port, config.web_token()):
        print(u)
    return 0
