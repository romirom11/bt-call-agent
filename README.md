# bt-call-agent

Let an AI agent talk in your phone calls — without root, without Twilio, without
hacking Android.

Your Linux machine pretends to be a **Bluetooth headset**. The phone happily
routes call audio to it, like it would to any earbuds, and `bt-call-agent`
bridges that audio to **Gemini Live**. The person on the other end talks to the
AI; you watch the transcript.

```
┌────────────────────────────────┐
│ Phone: GSM / Telegram call     │
└────────────────────────────────┘
      ▲                   │
 agent's voice       their voice      Bluetooth HFP, mSBC 16 kHz
      │                   ▼           (the phone thinks it's a headset)
┌────────────────────────────────┐
│ Linux: BlueZ + PipeWire        │
│ bluez_output.*   bluez_input.* │
└────────────────────────────────┘
      ▲                   │
   pw-play            pw-record       bt-call-agent
   24 kHz              16 kHz
      │                   ▼
┌────────────────────────────────┐
│ Gemini Live (cloud)            │
└────────────────────────────────┘
```

Should work with any call the phone routes to a Bluetooth headset: regular GSM
calls, Telegram, WhatsApp, Signal… (so far tested with Telegram).

## Status

Early prototype (v0.1). Tested on Arch Linux (BlueZ 5.87, PipeWire 1.6) with a
Pixel 7 Pro over Telegram calls. Expect rough edges.

What works:
- the phone sees the machine as a headset (HFP Hands-Free, wide-band mSBC)
- two-way audio, the agent speaks first, barge-in (interrupt the agent mid-sentence)
- live transcript of both sides, saved per call
- surviving Android's mid-call audio re-routing

Not yet:
- dialing a number from the agent (HFP supports `ATD<number>;` — on the roadmap)
- tools (calendar, booking APIs…) for the agent

## Requirements

- Linux with **BlueZ ≥ 5.6x**, **PipeWire ≥ 1.0** with **WirePlumber 0.5** and its
  BlueZ plugin (Arch: `bluez bluez-utils pipewire wireplumber pipewire-audio`;
  Debian/Ubuntu: `bluez pipewire wireplumber libspa-0.2-bluetooth`)
- a Bluetooth adapter whose SCO (call) audio goes over HCI. Tested with a Realtek
  USB adapter. Raspberry Pi's built-in one routes SCO to PCM pins by default, so
  use a USB dongle there.
- Python ≥ 3.10
- a [Gemini API key](https://aistudio.google.com/apikey)
- a phone, within Bluetooth range

## Install

```bash
pipx install git+https://github.com/romirom11/bt-call-agent
# or with uv:
uv tool install git+https://github.com/romirom11/bt-call-agent
```

On Debian/Ubuntu, pipx and `python3 -m venv` need the `python3-venv` package
(`sudo apt install pipx python3-venv`).

## Setup

**1. Put the machine into headset mode** (asks for sudo):

```bash
bt-call-agent setup enable
```

This changes three things, all reverted by `bt-call-agent setup disable`:

| What | Why |
|---|---|
| WirePlumber `bluez5.roles = [ hfp_hf a2dp_sink ]` | advertise the *headset* side of HFP instead of the *phone* side |
| `/etc/bluetooth/main.conf`: `Class = 0x240404` | look like a headset (Audio/Video → Wearable Headset), not a computer |
| `bluetoothd -P hostname` (systemd drop-in) | BlueZ's hostname plugin would override the class with "Computer" again |

> While headset mode is on, this machine can't play its own audio to Bluetooth
> headphones. That's why it's a toggle.

**2. Pair the phone:**

```bash
bt-call-agent pair
```

On the phone: Bluetooth → pair new device → pick this machine. Then open the
device's settings on the phone and make sure **Phone calls** is enabled.

**3. API key:**

```bash
mkdir -p ~/.config/bt-call-agent
install -m 600 /dev/stdin ~/.config/bt-call-agent/key <<< "AIza..."
# or: export GEMINI_API_KEY=AIza...
```

**4. Check everything:**

```bash
bt-call-agent doctor
```

## Run

```bash
bt-call-agent run
```

It waits for a call. Make or answer a call on the phone, with audio routed to
the Bluetooth device, and the agent takes over. It greets first once the other
side picks up (or after 6 s), and answers whenever the other side speaks first.

```
16:19:45 call detected: bluez_input.AA_BB_…0 / bluez_output.AA_BB_…1
16:19:46 Gemini Live connected (gemini-3.8-live)
16:19:51 (greeting)
16:19:57 🤖 agent: Hello, this is an AI assistant calling on behalf of Alex…
16:20:02 👤 them: Hi, how can I help?
```

Transcripts go to `~/.local/state/bt-call-agent/transcripts/`.

Useful options:

| Option | |
|---|---|
| `--prompt FILE` | what the agent should do (see [examples/](examples/)) |
| `--voice Kore` | a prebuilt Gemini voice |
| `--no-greet` | wait for the other side to speak first |
| `--out-volume 0.5` | agent loudness into the call (lower if it distorts) |
| `--model …` | default `gemini-3.8-live` |

### Prompts

The prompt decides the agent's task **and its language**. Default lookup order:
`--prompt` → `~/.config/bt-call-agent/prompt.txt` → the bundled test prompt.
Examples:

- [`examples/restaurant-booking.txt`](examples/restaurant-booking.txt) — book a table, in the restaurant's language
- [`examples/test-call-uk.txt`](examples/test-call-uk.txt) — Ukrainian test call

### As a service

```bash
bt-call-agent install-service -- --prompt ~/.config/bt-call-agent/prompt.txt
systemctl --user enable --now bt-call-agent
journalctl --user -u bt-call-agent -f
```

## Troubleshooting

**Phone shows the machine as a computer / no "Phone calls" toggle.**
Run `bt-call-agent doctor`. The class must be `0x…0404`. On the phone, *forget*
the device and pair again: phones cache the old class.

**Phone says "can't communicate with …".** Forget the device on the phone,
toggle the phone's Bluetooth, pair again. `sudo btmon` shows whether the phone
even reaches the machine.

**The call is detected but the agent goes silent after a second.** Android
re-routes call audio when the other side answers, and PipeWire recreates the
call nodes. The bridge reconnects automatically. If you see it give up
(`call audio gone`), raise `GRACE_S` in `bridge.py`.

**Crackling.** Usually buffer underruns or clipping: try `--out-volume 0.5`.
Check `journalctl -k | grep SCO` for radio-level errors: Wi-Fi on 2.4 GHz and
Bluetooth share the band, and a USB dongle on an extension cable helps.

**Revert everything:** `bt-call-agent setup disable`.

## Be a good citizen

- **Disclose that it's an AI.** The bundled prompts make the agent say so at the
  start of the call. In the EU, AI systems talking to people must make that clear
  (AI Act transparency rules), and it's simply fair.
- **Recording.** Transcripts are recordings of a conversation. Check the consent
  rules where you and the other side are.
- Don't use it for spam, robocalls or impersonation.

## How it works (short version)

- **HFP roles.** A phone is the *Audio Gateway*; headsets are the *Hands-Free*
  unit. BlueZ + PipeWire can play either; we pick Hands-Free.
- **Audio.** The phone sends call audio over SCO (mSBC, 16 kHz). PipeWire exposes
  it as `bluez_input.<mac>` / `bluez_output.<mac>` nodes, only while a call is up.
- **Bridge.** `pw-record` streams 16 kHz PCM to the Gemini Live websocket.
  Gemini's 24 kHz replies go through a small jitter buffer into `pw-play`.
  Gemini does voice activity detection, interruptions and transcription.

## License

MIT
