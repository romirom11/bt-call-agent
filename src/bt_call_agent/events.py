"""What the agent is doing right now, for anyone listening (the web UI).

The bridge reports into the module-level `hub`; subscribers get every event
through their own queue. Without subscribers this is just a few dict updates.
"""
from __future__ import annotations

import asyncio
import collections
import time

# status values
NO_KEY = "no_key"                  # waiting, but there is no Gemini API key
WAITING = "waiting"                # no call
CONNECTING = "connecting"          # call audio found, opening the Gemini session
IN_CALL = "in_call"                # the agent is talking
AGENT_STOPPED = "agent_stopped"    # the call is still up, but the agent left it


class Hub:
    def __init__(self, log_size: int = 300):
        self.status = WAITING
        self.call: dict | None = None          # current call: started, model, transcript, lines
        self.partial: dict[str, str] = {}      # speaker -> text not yet flushed to a line
        self.logs: collections.deque = collections.deque(maxlen=log_size)
        self.pairing: dict | None = None
        self._subs: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None

    # ------------------------------------------------------------------ plumbing

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the event loop, so other threads can publish safely."""
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, event: dict) -> None:
        loop = self._loop
        if loop and loop.is_running() and not _in_loop(loop):   # e.g. from the pairing thread
            loop.call_soon_threadsafe(self._fanout, event)
        else:
            self._fanout(event)

    def _fanout(self, event: dict) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:          # a stuck client: drop it, it will reconnect
                self._subs.discard(q)

    def snapshot(self) -> dict:
        return {"status": self.status, "call": self.call, "partial": self.partial,
                "logs": list(self.logs), "pairing": self.pairing}

    # ------------------------------------------------------------------ reports

    def log(self, text: str) -> None:
        entry = {"t": time.time(), "text": text}
        self.logs.append(entry)
        self.publish({"type": "log", **entry})

    def set_status(self, status: str) -> None:
        if status != self.status:
            self.status = status
            self.publish({"type": "status", "status": status, "call": self.call})

    def call_started(self, model: str, transcript: str, prompt_source: str) -> None:
        self.call = {"started": time.time(), "model": model, "transcript": transcript,
                     "prompt": prompt_source, "lines": []}
        self.partial = {}
        self.set_status(CONNECTING)

    def call_ended(self) -> None:
        if self.call:
            self.call["ended"] = time.time()
        self.partial = {}
        self._stop = None
        self.publish({"type": "call_ended", "call": self.call})

    def speaking(self, who: str, text: str) -> None:
        """Text of a turn still in progress."""
        self.partial[who] = text
        self.publish({"type": "partial", "who": who, "text": text})

    def line(self, who: str, text: str) -> None:
        """A finished turn."""
        self.partial.pop(who, None)
        entry = {"t": time.time(), "who": who, "text": text}
        if self.call is not None:
            self.call["lines"].append(entry)
        self.publish({"type": "line", **entry})

    def set_pairing(self, pairing: dict | None) -> None:
        self.pairing = pairing
        self.publish({"type": "pairing", "pairing": pairing})

    # ------------------------------------------------------------------ control

    def attach_call(self, stop: asyncio.Event) -> None:
        self._stop = stop

    def stop_agent(self) -> bool:
        """Take the agent out of the current call. The call itself stays up."""
        if self._stop is None or self._stop.is_set():
            return False
        self._stop.set()
        return True


def _in_loop(loop: asyncio.AbstractEventLoop) -> bool:
    try:
        return asyncio.get_running_loop() is loop
    except RuntimeError:
        return False


hub = Hub()
