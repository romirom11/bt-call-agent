"""Bluetooth helpers: pairing a phone and checking the system setup."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time

from . import bridge, config

HF_UUID = "0000111e"   # Handsfree (HF role) — what we want
AG_UUID = "0000111f"   # Handsfree Audio Gateway — what a normal PC advertises
HEADSET_CLASS = 0x0404  # major Audio/Video, minor Wearable Headset


INSTALL_HINT = ("Arch: pacman -S bluez bluez-utils pipewire wireplumber pipewire-audio | "
                "Debian/Ubuntu: apt install bluez pipewire wireplumber libspa-0.2-bluetooth | "
                "Fedora: dnf install bluez pipewire wireplumber pipewire-utils")


def spa_bluez_plugin() -> str | None:
    """Path of PipeWire's BlueZ SPA plugin, if installed (Debian/Ubuntu ship it separately)."""
    import glob
    dirs = [os.environ.get("SPA_PLUGIN_DIR", "")] + glob.glob("/usr/lib*/spa-0.2") \
        + glob.glob("/usr/lib/*/spa-0.2")
    for d in dirs:
        p = os.path.join(d, "bluez5", "libspa-bluez5.so")
        if d and os.path.isfile(p):
            return p
    return None


def sh(*cmd, timeout=10) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def devices(flt: str | None = None) -> dict[str, str]:
    out = sh("bluetoothctl", "devices", *([flt] if flt else []))
    return {m[1]: m[2] for m in re.finditer(r"^Device ([0-9A-F:]{17}) (.*)$", out, re.M)}


# --------------------------------------------------------------------------- pair

def pair(timeout: int = 180) -> int:
    """Make this machine discoverable, auto-accept pairing, trust the phone."""
    before = set(devices("Paired"))
    ctl = subprocess.Popen(["bluetoothctl", "--agent", "NoInputNoOutput"],
                           stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, text=True)

    def cmd(c):
        ctl.stdin.write(c + "\n")
        ctl.stdin.flush()

    try:
        for c in ("power on", "default-agent", "pairable on",
                  f"discoverable-timeout {timeout}", "discoverable on"):
            cmd(c)
            time.sleep(0.3)
        name = sh("bluetoothctl", "show")
        alias = re.search(r"Alias: (.*)", name)
        print(f"Discoverable as '{alias[1] if alias else '?'}' for {timeout}s.")
        print("On the phone: Bluetooth -> pair new device -> pick it. Ctrl+C to stop.")
        deadline = time.time() + timeout
        while time.time() < deadline:
            new = {mac: n for mac, n in devices("Paired").items() if mac not in before}
            connected = devices("Connected")
            for mac, n in new.items():
                sh("bluetoothctl", "trust", mac)
                if mac in connected:
                    print(f"✓ paired, trusted and connected: {n} ({mac})")
                    print("  On the phone, open this device's settings and make sure "
                          "'Phone calls' is enabled.")
                    return 0
            time.sleep(2)
        print("✗ no phone paired before the timeout")
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        try:
            cmd("discoverable off")
            cmd("quit")
            ctl.wait(timeout=3)
        except Exception:
            ctl.kill()


# --------------------------------------------------------------------------- doctor

def doctor() -> int:
    problems = 0

    def check(ok: bool, good: str, bad: str, hint: str = ""):
        nonlocal problems
        if ok:
            print(f"✓ {good}")
        else:
            problems += 1
            print(f"✗ {bad}" + (f"\n    → {hint}" if hint else ""))

    tools = ["bluetoothctl", "pw-dump", "pw-record", "pw-play", "wpctl"]
    missing = [t for t in tools if not shutil.which(t)]
    check(not missing, "bluez + pipewire tools found",
          f"missing tools: {', '.join(missing)}", INSTALL_HINT)

    plugin = spa_bluez_plugin()
    check(bool(plugin), f"PipeWire Bluetooth plugin found ({plugin})",
          "PipeWire Bluetooth (bluez5 SPA) plugin not found: no HFP role, no call audio nodes",
          INSTALL_HINT)

    check(sh("systemctl", "is-active", "bluetooth").strip() == "active",
          "bluetooth.service is running", "bluetooth.service is not running",
          "sudo systemctl enable --now bluetooth")

    show = sh("bluetoothctl", "show")
    check("Powered: yes" in show, "adapter is powered on",
          "no adapter, or it is powered off", "bluetoothctl power on")

    check(HF_UUID in show, "adapter advertises the Hands-Free (headset) role",
          "adapter does not advertise the Hands-Free role"
          + (" (it advertises Audio Gateway instead)" if AG_UUID in show else ""),
          "bt-call-agent setup enable")

    m = re.search(r"Class: (0x[0-9a-fA-F]+)", show)
    cls = int(m[1], 16) if m else 0
    if "Powered: yes" in show:     # class reads 0x0 while powered off
        check(cls & 0x1FFC == HEADSET_CLASS, f"device class looks like a headset ({hex(cls)})",
              f"device class is {hex(cls)}, phones may treat this machine as a computer",
              "bt-call-agent setup enable (sets Class and disables bluetoothd's hostname plugin)")

    paired = devices("Paired")
    connected = devices("Connected")
    check(bool(paired), f"paired devices: {', '.join(paired.values())}",
          "no paired phone", "bt-call-agent pair")
    if paired:
        check(bool(connected), f"connected: {', '.join(connected.values())}",
              "no phone connected right now",
              "turn on Bluetooth on the phone or: bluetoothctl connect <MAC>")

    nodes = bridge.find_call_nodes()
    print(f"• call audio nodes: {nodes[0]} / {nodes[1]}" if nodes
          else "• no active call (call audio nodes appear only during a call)")

    key = config.resolve_api_key(None)
    check(bool(key), "Gemini API key found",
          "no Gemini API key",
          f"export GEMINI_API_KEY=... or put it in {config.key_path()} (chmod 600)")
    prompt_src = config.prompt_path(None)
    print(f"• prompt: {prompt_src}")

    print("\nAll good." if not problems else f"\n{problems} problem(s) found.")
    return 1 if problems else 0


# --------------------------------------------------------------------------- service

SERVICE = """[Unit]
Description=bt-call-agent: AI agent for Bluetooth phone calls (Gemini Live)
After=pipewire.service wireplumber.service
Wants=pipewire.service wireplumber.service

[Service]
ExecStart={exec_start}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def install_service(extra_args: list[str]) -> int:
    import shlex
    import sys
    unit_dir = config.xdg("XDG_CONFIG_HOME", ".config") / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / "bt-call-agent.service"
    exec_start = " ".join(shlex.quote(a) for a in
                          [sys.executable, "-m", "bt_call_agent", "run", *extra_args])
    unit.write_text(SERVICE.format(exec_start=exec_start))
    print(f"wrote {unit}")
    if not config.resolve_api_key(None, env=False):
        print(f"! the service can't see your shell's GEMINI_API_KEY: put the key in {config.key_path()}")
    subprocess.run(["systemctl", "--user", "daemon-reload"])
    print("start:  systemctl --user enable --now bt-call-agent")
    print("logs:   journalctl --user -u bt-call-agent -f")
    return 0


def setup_script(action: str) -> int:
    script = config.DATA / "headset-mode.sh"
    return subprocess.call(["bash", str(script), action], env=os.environ.copy())
