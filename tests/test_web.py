"""Web UI and settings tests: no Bluetooth, PipeWire or network needed."""
import asyncio
import json
import os
import pathlib
import stat
import tempfile
import unittest
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from bt_call_agent import cli, config, events, web


class TempHome(unittest.IsolatedAsyncioTestCase):
    """XDG dirs in a temp folder; a web client signed in with a bearer token."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = pathlib.Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(d / "cfg"),
                                                "XDG_STATE_HOME": str(d / "state")})
        self.env.start()
        os.environ.pop("GEMINI_API_KEY", None)
        self.transcripts = d / "transcripts"
        self.transcripts.mkdir()
        self.hub = events.Hub()
        self.hub_patch = [mock.patch.object(web, "hub", self.hub)]
        for p in self.hub_patch:
            p.start()
        self.ctx = web.Context(transcripts_dir=self.transcripts, token="secret-token")
        self.client = TestClient(TestServer(web.make_app(self.ctx)))
        await self.client.start_server()
        self.auth = {"Authorization": "Bearer secret-token"}

    async def asyncTearDown(self):
        await self.client.close()
        for p in self.hub_patch:
            p.stop()
        self.env.stop()
        self.tmp.cleanup()

    async def call(self, method, path, data=None, status=200, headers=None):
        r = await self.client.request(method, path, json=data, headers={**self.auth, **(headers or {})})
        body = await r.json()
        self.assertEqual(r.status, status, body)
        return body


class Auth(TempHome):
    async def test_api_needs_token(self):
        r = await self.client.get("/api/state")
        self.assertEqual(r.status, 401)
        r = await self.client.get("/api/state", headers={"Authorization": "Bearer nope"})
        self.assertEqual(r.status, 401)

    async def test_page_shows_signin_without_token(self):
        r = await self.client.get("/")
        self.assertEqual(r.status, 401)
        self.assertIn("web-url", await r.text())

    async def test_token_link_sets_cookie_and_redirects(self):
        r = await self.client.get("/?token=secret-token", allow_redirects=False)
        self.assertEqual(r.status, 302)
        self.assertEqual(r.headers["Location"], "/")
        cookie = r.cookies[web.COOKIE]
        self.assertEqual(cookie.value, "secret-token")
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Strict")
        r = await self.client.get("/api/state")       # the client keeps the cookie
        self.assertEqual(r.status, 200)

    async def test_wrong_token_link_sets_nothing(self):
        r = await self.client.get("/?token=wrong", allow_redirects=False)
        self.assertEqual(r.status, 302)
        self.assertNotIn(web.COOKIE, r.cookies)

    async def test_cross_site_writes_refused(self):
        await self.call("PUT", "/api/settings", {"greet": False}, status=403,
                        headers={"Origin": "http://evil.example"})

    async def test_assets_are_public(self):
        r = await self.client.get("/assets/app.css")
        self.assertEqual(r.status, 200)


class Settings(TempHome):
    async def test_save_and_reset(self):
        out = await self.call("PUT", "/api/settings", {"voice": "Kore", "out_volume": 0.5})
        self.assertEqual(out["data"]["effective"]["voice"], "Kore")
        self.assertEqual(json.loads(config.settings_path().read_text()),
                         {"voice": "Kore", "out_volume": 0.5})
        out = await self.call("PUT", "/api/settings", {"voice": None})
        self.assertIsNone(out["data"]["effective"]["voice"])
        self.assertEqual(config.load_settings(), {"out_volume": 0.5})

    async def test_rejects_bad_values_and_keeps_file(self):
        await self.call("PUT", "/api/settings", {"greet": False})
        for bad in ({"out_volume": 3}, {"greet": "yes"}, {"model": ""}, {"nope": 1},
                    {"greet_timeout": True}):
            await self.call("PUT", "/api/settings", bad, status=400)
        self.assertEqual(config.load_settings(), {"greet": False})

    async def test_cli_flags_win_and_are_reported(self):
        self.ctx.cli_overrides = {"voice": "Puck"}
        await self.call("PUT", "/api/settings", {"voice": "Kore"})
        s = (await self.call("GET", "/api/state"))["data"]["settings"]
        self.assertEqual(s["effective"]["voice"], "Puck")
        self.assertEqual(s["locked"], ["voice"])

    async def test_broken_settings_file_is_reported(self):
        config.settings_path().parent.mkdir(parents=True)
        config.settings_path().write_text("{not json")
        s = (await self.call("GET", "/api/state"))["data"]["settings"]
        self.assertTrue(s["error"])
        self.assertEqual(s["effective"], config.DEFAULTS)


class Prompt(TempHome):
    async def test_save_prompt(self):
        state = (await self.call("GET", "/api/state"))["data"]["prompt"]
        self.assertTrue(state["bundled"])
        out = await self.call("PUT", "/api/prompt", {"text": "Book a table."})
        self.assertEqual(config.user_prompt_path().read_text(), "Book a table.\n")
        self.assertFalse(out["data"]["bundled"])
        await self.call("PUT", "/api/prompt", {"text": "  "}, status=400)

    async def test_locked_by_cli(self):
        self.ctx.cli_prompt = "/somewhere/p.txt"
        await self.call("PUT", "/api/prompt", {"text": "x"}, status=409)

    async def test_presets(self):
        names = [p["name"] for p in (await self.call("GET", "/api/presets"))["data"]]
        self.assertIn("default", names)
        self.assertIn("restaurant-booking", names)
        await self.call("POST", "/api/presets", {"name": "dentist", "text": "Call the dentist."})
        mine = [p for p in (await self.call("GET", "/api/presets"))["data"] if not p["builtin"]]
        self.assertEqual([(p["name"], p["text"]) for p in mine], [("dentist", "Call the dentist.\n")])
        await self.call("POST", "/api/presets", {"name": "default", "text": "x"}, status=409)
        for bad in ("../evil", "a/b", "", ".hidden"):
            await self.call("POST", "/api/presets", {"name": bad, "text": "x"}, status=400)
        await self.call("DELETE", "/api/presets/dentist")
        await self.call("DELETE", "/api/presets/dentist", status=404)


class Key(TempHome):
    async def test_replace_key(self):
        out = await self.call("PUT", "/api/key", {"key": " AIzaTest \n"})
        self.assertEqual(config.key_path().read_text(), "AIzaTest\n")
        self.assertEqual(stat.S_IMODE(config.key_path().stat().st_mode), 0o600)
        self.assertEqual(out["data"]["source"], "file")
        self.assertIsNone(out["note"])
        self.assertNotIn("AIzaTest", json.dumps(await self.call("GET", "/api/state")))
        await self.call("PUT", "/api/key", {"key": "two words"}, status=400)

    async def test_env_key_still_wins(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "env"}):
            out = await self.call("PUT", "/api/key", {"key": "AIzaTest"})
        self.assertIn("GEMINI_API_KEY", out["note"])


class Transcripts(TempHome):
    async def test_list_read_delete(self):
        (self.transcripts / "call-20261127-101500.txt").write_text("👤 them: Hello\n🤖 agent: Hi!\n")
        (self.transcripts / "call-20261127-121500.txt").write_text("")
        (self.transcripts / "notes.txt").write_text("not a call")
        calls = (await self.call("GET", "/api/transcripts"))["data"]
        self.assertEqual([c["name"] for c in calls],
                         ["call-20261127-121500.txt", "call-20261127-101500.txt"])
        self.assertIn("Hello", calls[1]["preview"])
        t = (await self.call("GET", "/api/transcripts/call-20261127-101500.txt"))["data"]
        self.assertIn("agent: Hi!", t["text"])
        r = await self.client.get("/api/transcripts/call-20261127-101500.txt?download=1",
                                  headers=self.auth)
        self.assertIn("attachment", r.headers["Content-Disposition"])
        await self.call("GET", "/api/transcripts/notes.txt", status=404)
        await self.call("DELETE", "/api/transcripts/call-20261127-101500.txt")
        self.assertFalse((self.transcripts / "call-20261127-101500.txt").exists())

    async def test_current_call_cannot_be_deleted(self):
        (self.transcripts / "call-20261127-101500.txt").write_text("")
        self.hub.call_started("m", "call-20261127-101500.txt", "p")
        await self.call("DELETE", "/api/transcripts/call-20261127-101500.txt", status=409)


class Live(TempHome):
    async def test_stop_agent(self):
        await self.call("POST", "/api/agent/stop", status=409)
        stop = asyncio.Event()
        self.hub.attach_call(stop)
        await self.call("POST", "/api/agent/stop")
        self.assertTrue(stop.is_set())

    async def test_events_stream(self):
        self.hub.call_started("gemini-x", "call-20261127-101500.txt", "/p.txt")
        r = await self.client.get("/api/events", headers=self.auth)
        self.assertEqual(r.headers["Content-Type"], "text/event-stream")

        async def next_event():
            while True:
                line = (await r.content.readline()).decode()
                if line.startswith("data: "):
                    return json.loads(line[6:])

        first = await asyncio.wait_for(next_event(), 5)
        self.assertEqual(first["type"], "snapshot")
        self.assertEqual(first["status"], events.CONNECTING)
        self.hub.set_status(events.IN_CALL)
        self.hub.speaking("them", "Hel")
        self.hub.line("them", "Hello")
        got = [await asyncio.wait_for(next_event(), 5) for _ in range(3)]
        self.assertEqual([e["type"] for e in got], ["status", "partial", "line"])
        self.assertEqual(self.hub.call["lines"][0]["text"], "Hello")
        r.close()


class CliArgs(unittest.TestCase):
    def test_only_given_flags_override(self):
        ap_args = []
        with mock.patch.object(cli, "cmd_run", lambda a: ap_args.append(a) or 0):
            cli.main(["run"])
            cli.main(["run", "--voice", "Kore", "--no-greet", "--out-volume", "0.4"])
        self.assertEqual(cli.overrides(ap_args[0]), {})
        self.assertEqual(cli.overrides(ap_args[1]), {"voice": "Kore", "greet": False, "out_volume": 0.4})

    def test_bad_flag_value(self):
        with self.assertRaises(SystemExit):
            cli.main(["run", "--out-volume", "5"])

    def test_parse_addr(self):
        self.assertEqual(cli.parse_addr("8080"), ("127.0.0.1", 8080))
        self.assertEqual(cli.parse_addr("0.0.0.0:9000"), ("0.0.0.0", 9000))
        self.assertEqual(cli.parse_addr("[::1]:9000"), ("::1", 9000))
        with self.assertRaises(ValueError):
            cli.parse_addr("host:http")


if __name__ == "__main__":
    unittest.main()
