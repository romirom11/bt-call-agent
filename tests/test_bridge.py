"""Offline tests: no Bluetooth, PipeWire or network needed.  python -m unittest"""
import asyncio
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from bt_call_agent import bridge, config, events


def node(name, profile="headset-audio-gateway"):
    return {"type": "PipeWire:Interface:Node",
            "info": {"props": {"node.name": name, "api.bluez5.profile": profile}}}


class ParseNodes(unittest.TestCase):
    def test_finds_hfp_pair(self):
        dump = [node("alsa_output.pci"), node("bluez_input.AA_BB.0"), node("bluez_output.AA_BB.1")]
        self.assertEqual(bridge.parse_call_nodes(dump), ("bluez_input.AA_BB.0", "bluez_output.AA_BB.1"))

    def test_ignores_a2dp_and_incomplete(self):
        self.assertIsNone(bridge.parse_call_nodes([node("bluez_input.AA_BB.0", "a2dp-sink"),
                                                   node("bluez_output.AA_BB.1", "a2dp-sink")]))
        self.assertIsNone(bridge.parse_call_nodes([node("bluez_input.AA_BB.0")]))


class FakeProc:
    def __init__(self, log):
        self.returncode = None
        self.log = log
        outer = self

        class Stdin:
            def write(self, d):
                outer.log.append((asyncio.get_running_loop().time(), len(d)))

            async def drain(self):
                pass
        self.stdin = Stdin()

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return 0


class JitterBuffer(unittest.TestCase):
    def test_prebuffers_only_when_idle(self):
        writes = []

        async def fake_exec(*a, **k):
            return FakeProc(writes)

        async def no_nodes(timeout=0):
            return None

        async def scenario():
            with mock.patch.object(bridge.asyncio, "create_subprocess_exec", fake_exec), \
                    mock.patch.object(bridge, "wait_for_nodes", no_nodes):
                p = bridge.Player("x")
                await p.start()
                t0 = asyncio.get_running_loop().time()
                for _ in range(10):             # 40 ms chunks every 30 ms
                    p.feed(b"\0" * 1920)
                    await asyncio.sleep(0.03)
                await asyncio.sleep(1.0)        # reply finished and drained
                for _ in range(3):
                    p.feed(b"\0" * 1920)
                    await asyncio.sleep(0.03)
                await asyncio.sleep(0.5)
                await p.stop()
                return [(t - t0, n) for t, n in writes]

        w = asyncio.run(scenario())
        self.assertGreaterEqual(w[0][1], bridge.PREBUF_BYTES)          # first write is buffered
        self.assertTrue(all(n == 1920 for _, n in w[1:-1]))            # then streams with no waits
        self.assertGreater(w[-1][0] - w[-2][0], 0.9)                   # new reply after a pause
        self.assertEqual(w[-1][1], 3 * 1920)                           # ...is prebuffered again


class Serve(unittest.TestCase):
    """The wait-for-calls loop reports what it's doing to the event hub."""

    def run_serve(self, key, nodes_seq, run_call=None):
        seq = iter(nodes_seq)
        hub = events.Hub()
        statuses = []
        orig = hub.set_status

        def record(status):
            if status != hub.status or not statuses:
                statuses.append(status)
            orig(status)
        hub.set_status = record
        calls = []

        async def fake_run_call(client, opts, nodes):
            calls.append(opts)

        async def no_sleep(_):
            await _real_sleep(0)      # yield, but don't wait

        async def scenario():
            with mock.patch.object(bridge, "hub", hub), \
                    mock.patch.object(bridge, "find_call_nodes", lambda: next(seq, None)), \
                    mock.patch.object(bridge, "run_call", run_call or fake_run_call), \
                    mock.patch.object(bridge.asyncio, "sleep", no_sleep), \
                    mock.patch("google.genai.Client"):
                task = asyncio.create_task(bridge.serve(lambda: key, lambda: "opts"))
                for _ in range(50):
                    await _real_sleep(0)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        asyncio.run(scenario())
        return statuses, calls, hub

    def test_no_key_keeps_out_of_calls(self):
        statuses, calls, hub = self.run_serve(None, [None, ("in", "out"), ("in", "out"), None])
        self.assertEqual(calls, [])
        self.assertNotIn(events.AGENT_STOPPED, statuses)
        self.assertEqual(statuses[0], events.NO_KEY)
        self.assertTrue(any("no Gemini API key" in l["text"] for l in hub.logs))

    def test_call_then_agent_stopped_while_call_is_up(self):
        statuses, calls, _ = self.run_serve("k", [None, ("in", "out"), ("in", "out"), None])
        self.assertEqual(calls, ["opts"])           # options are made for the call
        self.assertEqual(statuses, [events.WAITING, events.AGENT_STOPPED, events.WAITING])


_real_sleep = asyncio.sleep


class Config(unittest.TestCase):
    def test_key_resolution_order(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": d, "GEMINI_API_KEY": "env"}):
            self.assertEqual(config.resolve_api_key(None), "env")
            (pathlib.Path(d) / "bt-call-agent").mkdir()
            config.key_path().write_text("file\n")
            self.assertEqual(config.resolve_api_key(None, env=False), "file")
            kf = pathlib.Path(d) / "k"
            kf.write_text("cli")
            self.assertEqual(config.resolve_api_key(str(kf)), "cli")

    def test_bundled_prompt_exists(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": d}):
            self.assertTrue(config.prompt_path(None).is_file())


if __name__ == "__main__":
    unittest.main()
