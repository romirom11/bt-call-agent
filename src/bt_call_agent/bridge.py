"""Bridge a Bluetooth HFP phone call to Gemini Live.

Phone (HFP AG) --mSBC--> PipeWire bluez_input.*  --pw-record 16k--> Gemini Live
Gemini Live --24k PCM--> pw-play --> PipeWire bluez_output.* --mSBC--> Phone

Waits for a call (HFP nodes appear), runs one Live session per call, and goes
back to waiting when the call ends.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import pathlib
import subprocess
from dataclasses import dataclass
from typing import Callable

from . import events
from .events import hub

IN_RATE = 16000          # what we send to Gemini
OUT_RATE = 24000         # what Gemini Live returns
CHUNK_MS = 40
CHUNK_BYTES = IN_RATE * 2 * CHUNK_MS // 1000
PREBUF_BYTES = OUT_RATE * 2 * 250 // 1000   # 250 ms jitter buffer at the start of each reply
PREBUF_MAX_WAIT = 0.3
GRACE_S = 4              # HFP nodes may vanish briefly while Android re-routes audio

GREETING_CUE = ("(The other person has just picked up the phone. Speak first: greet them and "
                "briefly say who you are, following your instructions.)")


def log(*a):
    text = " ".join(str(x) for x in a)
    print(dt.datetime.now().strftime("%H:%M:%S"), text, flush=True)
    hub.log(text)


@dataclass
class Options:
    model: str
    prompt: str
    transcripts_dir: pathlib.Path
    voice: str | None = None
    language: str | None = None
    greet: bool = True
    greet_timeout: float = 6.0
    out_volume: float = 0.7
    prompt_source: str = ""


# --------------------------------------------------------------------------- PipeWire

def parse_call_nodes(dump: list) -> tuple[str, str] | None:
    """Find (input_node, output_node) of an active HFP call in `pw-dump` output."""
    src = sink = None
    for obj in dump:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        if props.get("api.bluez5.profile") != "headset-audio-gateway":
            continue
        name = props.get("node.name", "")
        if name.startswith("bluez_input."):
            src = name
        elif name.startswith("bluez_output."):
            sink = name
    return (src, sink) if src and sink else None


def find_call_nodes() -> tuple[str, str] | None:
    try:
        out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5).stdout
        return parse_call_nodes(json.loads(out))
    except Exception:
        return None


async def wait_for_nodes(timeout: float = GRACE_S):
    """HFP nodes get torn down and recreated when Android re-routes call audio
    (e.g. when the far end answers). Wait a bit for them to come back."""
    for _ in range(int(timeout * 4)):
        nodes = find_call_nodes()
        if nodes:
            return nodes
        await asyncio.sleep(0.25)
    return None


class Player:
    """Streams PCM into the call via pw-play; restarts when the call node is
    recreated, and can be flushed to drop buffered audio on barge-in."""

    def __init__(self, target: str, volume: float = 0.7):
        self.target = target
        self.volume = volume
        self.proc = None
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.task = None

    async def _spawn(self):
        nodes = await wait_for_nodes()
        if nodes:
            self.target = nodes[1]
        self.proc = await asyncio.create_subprocess_exec(
            "pw-play", "--target", self.target, "--raw", "--rate", str(OUT_RATE),
            "--channels", "1", "--format", "s16", "--latency", "100ms",
            "--volume", str(self.volume),
            "-P", "{ node.dont-reconnect = true node.name = bt-call-agent-voice }", "-",
            stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

    async def start(self):
        await self._spawn()
        self.task = asyncio.create_task(self._pump())

    async def _pump(self):
        loop = asyncio.get_running_loop()
        play_until = 0.0   # when everything written so far will have been played
        while True:
            data = await self.queue.get()
            # Idle -> speaking: build a small jitter buffer first so network
            # bursts from Gemini don't underrun the call stream (crackle).
            # Only when playback has drained, never mid-phrase.
            if loop.time() >= play_until:
                deadline = loop.time() + PREBUF_MAX_WAIT
                while len(data) < PREBUF_BYTES and (left := deadline - loop.time()) > 0:
                    try:
                        data += await asyncio.wait_for(self.queue.get(), left)
                    except asyncio.TimeoutError:   # == TimeoutError on 3.11+, not on 3.10
                        break
            for _ in range(2):
                if self.proc.returncode is not None:
                    log("(call output node changed, reconnecting voice)")
                    await self._spawn()
                try:
                    self.proc.stdin.write(data)
                    await self.proc.stdin.drain()
                    play_until = max(play_until, loop.time()) + len(data) / (OUT_RATE * 2)
                    break
                except (BrokenPipeError, ConnectionResetError):
                    await self.proc.wait()

    def feed(self, data: bytes):
        self.queue.put_nowait(data)

    async def flush(self):
        """Barge-in: drop everything not yet played."""
        await self.stop()
        self.queue = asyncio.Queue()
        await self.start()

    async def stop(self):
        if self.task:
            self.task.cancel()
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()


# --------------------------------------------------------------------------- session tasks

async def mic_reader(session, source, stop: asyncio.Event, rerouted: asyncio.Event):
    from google.genai import types
    started = asyncio.get_running_loop().time()
    while not stop.is_set():
        rec = await asyncio.create_subprocess_exec(
            "pw-record", "--target", source, "--raw", "--rate", str(IN_RATE),
            "--channels", "1", "--format", "s16", "--latency", f"{CHUNK_MS}ms",
            "-P", "{ node.dont-reconnect = true node.name = bt-call-agent-ears }", "-",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            while not stop.is_set():
                data = await rec.stdout.readexactly(CHUNK_BYTES)
                await session.send_realtime_input(
                    audio=types.Blob(data=data, mime_type=f"audio/pcm;rate={IN_RATE}"))
        except asyncio.IncompleteReadError:
            pass
        finally:
            if rec.returncode is None:
                rec.kill()
                await rec.wait()
        if stop.is_set():
            return
        nodes = await wait_for_nodes()
        if not nodes:
            log("call audio gone")
            stop.set()
            return
        source = nodes[0]
        log(f"(call audio re-routed after {asyncio.get_running_loop().time() - started:.1f}s,"
            f" reconnecting -> {source})")
        rerouted.set()


async def greeter(session, rerouted: asyncio.Event, caller_spoke: asyncio.Event, timeout: float):
    """Speak first: right after Android re-routes call audio (usually = far end
    answered), or after `timeout` seconds, unless the other side already spoke."""
    from google.genai import types
    waits = [asyncio.create_task(rerouted.wait()), asyncio.create_task(caller_spoke.wait())]
    await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    for w in waits:
        w.cancel()
    if caller_spoke.is_set():
        log("(other side spoke first, skipping greeting)")
        return
    await asyncio.sleep(0.5)   # let the new route settle
    log("(greeting)")
    await session.send_client_content(
        turns=types.Content(role="user", parts=[types.Part(text=GREETING_CUE)]),
        turn_complete=True)


async def call_watcher(stop: asyncio.Event):
    missing = 0.0
    while not stop.is_set():
        await asyncio.sleep(0.5)
        missing = 0.0 if find_call_nodes() else missing + 0.5
        if missing >= GRACE_S:
            log("call ended")
            stop.set()


async def receiver(session, player: Player, transcript, stop: asyncio.Event,
                   caller_spoke: asyncio.Event):
    heard, said = [], []

    def flush_lines():
        for who, label, buf in (("them", "👤 them", heard), ("agent", "🤖 agent", said)):
            if buf:
                text = "".join(buf).strip()
                buf.clear()
                if not text:
                    continue
                line = f"{label}: {text}"
                log(line)
                hub.line(who, text)
                transcript.write(line + "\n")
                transcript.flush()

    while not stop.is_set():
        async for msg in session.receive():
            sc = msg.server_content
            if msg.go_away:
                log("server go_away:", msg.go_away.time_left)
            if not sc:
                continue
            if sc.interrupted:
                log("(interrupted, dropping agent audio)")
                await player.flush()
            if sc.input_transcription and sc.input_transcription.text:
                if said:
                    flush_lines()
                heard.append(sc.input_transcription.text)
                hub.speaking("them", "".join(heard).strip())
                caller_spoke.set()
            if sc.output_transcription and sc.output_transcription.text:
                if heard:
                    flush_lines()
                said.append(sc.output_transcription.text)
                hub.speaking("agent", "".join(said).strip())
            if sc.model_turn:
                for part in sc.model_turn.parts or []:
                    if part.inline_data and part.inline_data.data:
                        player.feed(part.inline_data.data)
            if sc.turn_complete:
                flush_lines()


# --------------------------------------------------------------------------- orchestration

def live_config(opts: Options):
    from google.genai import types
    speech = None
    if opts.voice or opts.language:
        speech = types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=opts.voice))
            if opts.voice else None,
            language_code=opts.language or None)
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=opts.prompt,
        speech_config=speech,
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow()),
    )


async def run_call(client, opts: Options, nodes):
    source, sink = nodes
    log(f"call detected: {source} / {sink}")
    opts.transcripts_dir.mkdir(parents=True, exist_ok=True)
    tpath = opts.transcripts_dir / dt.datetime.now().strftime("call-%Y%m%d-%H%M%S.txt")
    stop = asyncio.Event()
    hub.call_started(opts.model, tpath.name, opts.prompt_source)
    hub.attach_call(stop)
    player = Player(sink, opts.out_volume)
    await player.start()
    try:
        with open(tpath, "w", encoding="utf-8") as transcript:
            async with client.aio.live.connect(model=opts.model, config=live_config(opts)) as session:
                log(f"Gemini Live connected ({opts.model}); transcript -> {tpath}")
                hub.set_status(events.IN_CALL)
                rerouted, caller_spoke = asyncio.Event(), asyncio.Event()
                tasks = [asyncio.create_task(c) for c in (
                    mic_reader(session, source, stop, rerouted),
                    receiver(session, player, transcript, stop, caller_spoke),
                    call_watcher(stop))]
                # kept out of `tasks`: the greeting finishing must not end the call
                extra = [asyncio.create_task(
                    greeter(session, rerouted, caller_spoke, opts.greet_timeout))] if opts.greet else []
                stopper = asyncio.create_task(stop.wait())
                done, _ = await asyncio.wait(tasks + [stopper], return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if t is not stopper and t.exception():
                        log("error:", repr(t.exception()))
                stop.set()
                for t in tasks + extra + [stopper]:
                    t.cancel()
                await asyncio.gather(*tasks, *extra, stopper, return_exceptions=True)
    finally:
        await player.stop()
        hub.call_ended()
    log("session closed")


async def serve(get_key: Callable[[], str | None], make_opts: Callable[[], Options],
                once: bool = False):
    """Wait for calls forever. The key and the options are read again before
    every call, so changes from the web UI apply from the next call on."""
    from google import genai
    clients: dict[str, object] = {}
    log("waiting for a Bluetooth call (HFP)...")
    while True:
        key = get_key()
        hub.set_status(events.WAITING if key else events.NO_KEY)
        nodes = find_call_nodes()
        if not nodes:
            await asyncio.sleep(1)
            continue
        if not key:
            log("call detected, but there is no Gemini API key: the agent stays out of it")
        else:
            try:
                opts = make_opts()
                if key not in clients:
                    clients.clear()
                    clients[key] = genai.Client(api_key=key)
                await run_call(clients[key], opts, nodes)
            except Exception as e:
                log("session failed:", repr(e))
            if once:
                return
        if key and find_call_nodes():
            hub.set_status(events.AGENT_STOPPED)
        while find_call_nodes():       # don't re-enter the same call
            await asyncio.sleep(1)
        log("waiting for the next call...")
