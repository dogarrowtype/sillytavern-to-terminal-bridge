# SillyTavern ↔ Terminal Bridge

A minimal SillyTavern extension + companion script that pipes a SillyTavern
chat into a real terminal — for playing adventure games inside cool-retro-term
(or any terminal emulator) while SillyTavern handles the model, character
card, prompts, and chat history.

The extension does **not** modify any SillyTavern settings, characters,
presets, or prompts. It only listens for new AI messages and sends user input
through SillyTavern's normal "send + trigger" path.

## How it works

```
┌──────────────────────┐    WebSocket     ┌─────────────────────────┐
│ SillyTavern (browser)│◄────ws://───────►│ bridge.py (in terminal) │
│  - extension         │   localhost:5005 │  - WS server            │
│  - sends AI replies  │                  │  - prints to stdout     │
│  - forwards typed in │                  │  - reads stdin lines    │
└──────────────────────┘                  └─────────────────────────┘
```

Whatever you type in the terminal becomes a `> [your text]` user turn in
SillyTavern, generation runs, and the reply is printed back to the terminal
with markdown converted to ANSI (`*italic*`, `**bold**`, code fences). A handful
of `/` commands (see [Terminal commands](#terminal-commands)) let you list and
switch character cards without leaving the terminal.

## Install

### 1. Extension

Copy or symlink the `extension/` directory into SillyTavern's third-party
extensions folder, named `sillytavern-terminal-bridge`:

```
SillyTavern/data/<your-user-handle>/extensions/sillytavern-terminal-bridge/
```

Reload SillyTavern. The "Terminal Bridge" panel appears under Extensions.

### 2. Bridge

```
cd bridge
pip install -r requirements.txt
```

(Python 3.9+ recommended.)

## Run

Start the bridge inside cool-retro-term so it becomes the foreground "game":

```
cool-retro-term -e python3 /absolute/path/to/bridge/bridge.py --port 5005
```

Or in any terminal:

```
python3 bridge/bridge.py --port 5005
```

The extension auto-connects to `ws://127.0.0.1:5005` when SillyTavern loads
(or when you flip the Enabled toggle / change the port). The status indicator
in the Extensions panel shows `connected` / `connecting` / `disconnected`.

Type a line, press Enter — that becomes a user turn in SillyTavern, the AI
generates a reply, and the reply prints in the terminal.

## Telnet mode — play from a vintage machine

The bridge can also listen for incoming telnet connections, so you can play
from a Mac Plus, a Macintosh SE, an Amiga, or any other vintage box that has
a TCP/IP stack and a telnet client.

```
python3 bridge/bridge.py --telnet 2323 --vintage mac
```

Then on the vintage machine, run telnet to your modern host's IP on port
2323. The bridge handles the telnet protocol (IAC echo + suppress-go-ahead +
NAWS for window size), runs a built-in line editor, and serves you the same
chat that the SillyTavern extension is feeding it.

### Presets

`--vintage` picks sensible defaults for a target machine. You can override
any of them with `--charset`, `--italic`, or `--max-width`.

| Preset | charset | italic | max width | Notes |
|---|---|---|---|---|
| `mac` | macroman | reverse | 80 | System 6/7 / NCSA Telnet 2.7 / BetterTelnet |
| `vt100` | ascii | reverse | 80 | Real VT100/VT220 hardware |
| `tty` | ascii | off | 72 | Teletypes, line printers, no ANSI |

- **charset**: how outgoing bytes are encoded. `macroman` is what Apple's
  classic OS expects, so em-dashes, smart quotes, ellipses, and "fancy"
  glyphs the AI emits arrive on a Mac as the right MacRoman characters
  instead of UTF-8 mojibake. `ascii` transliterates them (`— → --`,
  `“…” → "..."`).
- **italic**: real VT100s and most period-correct emulators don't have
  italic. `reverse` swaps in reverse-video for `*emphasis*` so it shows up.
  `off` strips it entirely.

### Picking a telnet client on classic Mac OS

Two solid choices, both 68k-compatible (so they'll run on a Mac SE):

- **BetterTelnet 2.0fc1** (recommended) — Rolf Braun's improved fork of
  NCSA Telnet. Better VT100/VT220/xterm emulation, ANSI color support,
  scrollback buffer, proper backspace/delete handling. This is the one
  to use if your SE has the RAM (~1 MB free). Look for it on
  Macintosh Garden.
- **NCSA Telnet 2.7** — the original. Lighter on RAM, simpler UI, totally
  fine for our purposes. Pick this if BetterTelnet feels heavy on the SE.
  Also widely archived (Macintosh Garden, info-mac).

Either way, before connecting:

1. **Terminal Emulation: VT100** — both clients support it; usually the default.
2. **Backspace sends:** DEL (`0x7F`) is what the bridge expects, but it
   also accepts BS (`0x08`) so either is fine.
3. **Local echo: OFF** — the bridge does server-side echo (the IAC
   negotiation handles this, but if your client overrides, force it off).
4. **Window size**: 80 × 24 fits the SE's screen nicely. The bridge picks
   this up automatically via NAWS.

### Macintosh SE on System 7.5.3 walkthrough

1. On your modern host:
   ```
   python3 bridge/bridge.py --telnet 2323 --vintage mac
   ```
   The bridge logs `Telnet listening on 0.0.0.0:2323` and `WebSocket
   listening on ws://127.0.0.1:5005`. Make sure your firewall allows
   port 2323.
2. On the SE, open BetterTelnet 2.0fc1 (or NCSA Telnet 2.7).
   File → New Connection → enter your modern host's LAN IP, port `2323`,
   emulation `VT100`. Connect.
3. You should see a `[connected — macroman, 80c]` notice and a `> ` prompt.
4. Open SillyTavern on any modern browser on the same LAN, with the
   extension installed and pointed at `127.0.0.1:5005`. Pick your
   adventure character.
5. Type a line on the SE, press Return. It becomes a `> [your text]`
   user turn in SillyTavern, the model generates, and the reply prints
   on the SE — wrapped to the SE's window width (auto-detected via NAWS),
   with `*emphasis*` shown as reverse video.

The bridge's built-in line editor handles Backspace, Ctrl+U (kill line),
Ctrl+W (kill word), and Ctrl+C (disconnect). Use the SE's normal Delete
key — both BS (0x08) and DEL (0x7f) are treated as backspace.

### Security

Telnet is unencrypted. The default `--telnet 2323` binds to `0.0.0.0`
(any interface), so anyone on your LAN who can reach the port can play
the chat — and read whatever the AI says. Bind to a specific interface
or use a firewall if that matters to you:

```
python3 bridge/bridge.py --telnet 192.168.1.10:2323 --vintage mac
```

## Terminal commands

A few commands typed at the prompt are handled by the bridge itself instead of
being sent to the AI. Anything else — including other slash text like
`/me waves` — is passed through to SillyTavern as a normal user turn.

| Command | Does |
|---|---|
| `/chars` (`/list`, `/who`) | List character cards; the current one is marked `*` |
| `/char <n\|name>` (`/select`) | Switch to a character by menu number or name (uses `/go`) |
| `/new` (`/newchat`) | Start a fresh chat with the current character; the old chat is kept |
| `/history` | Replay the current chat from the top |
| `/help` | Show this command list |

Switching with `/char` runs SillyTavern's `/go` (exact name, then prefix, then
substring match), then replays the new character's backlog so you land in the
conversation with its context already on screen. `/new` runs
`/newchat delete=false`, so the chat you were in is preserved on disk and you
can return to it from SillyTavern. Like the rest of the bridge, these are
best-effort: with no SillyTavern connected they report that instead of hanging.

## Chat backlog on connect

When a telnet client connects, the bridge asks the extension for the current
chat log and replays it — wrapped, padded, and charset-converted just like live
replies — before showing the prompt. Prior turns appear between dim
`--- history ---` / `--- end of history ---` markers, so you join an ongoing
conversation with its context already on screen instead of a blank session.

This is best-effort: if SillyTavern isn't connected, the chat is empty, or no
reply arrives within a few seconds, the session just opens empty. The replay is
instant (it ignores `--cps`/`--para-pause`); only new replies teletype.

## Reading back history

The bridge runs in line-mode and doesn't intercept keys, so use the terminal
emulator's own scrollback:

- **cool-retro-term** (and most Konsole/QMLTermWidget-based terminals):
  - `Shift + PageUp` / `Shift + PageDown` — scroll one page
  - `Shift + ↑` / `Shift + ↓` — scroll one line
  - Mouse wheel — scroll
- If older AI replies aren't there, the terminal's scrollback buffer is too
  small. In cool-retro-term, open **Settings → Terminal** and increase the
  *History Size* (or set "Unlimited"). 1000 lines is the default.

## Settings

In the SillyTavern Extensions panel:

- **Enabled** — turn the bridge on/off.
- **Host / Port** — defaults `127.0.0.1` / `5005`. Use `127.0.0.1` rather than
  `localhost` to avoid browser-side IPv6/IPv4 ambiguity.
- **Reconnect** — force a reconnect attempt.

## Troubleshooting

- **Status stays "connecting"**: bridge isn't running, or wrong port. Check
  the terminal for a `[bridge listening on ws://…]` banner.
- **`Address already in use`**: another process is using the port. Pass
  `--port 5006` (and update the extension settings to match).
- **AI replies don't appear**: check the browser console for `[TerminalBridge]`
  errors. Confirm the message isn't a system message (those are skipped).
- **"no compatible SillyTavern API found"**: your SillyTavern version doesn't
  expose `executeSlashCommandsWithOptions`, `sendMessageAsUser`, or
  `generate` on the context. Update SillyTavern.

## Files

- `extension/manifest.json`, `index.js`, `settings.html`, `style.css` — the
  SillyTavern UI extension.
- `bridge/bridge.py` — the companion WebSocket server. Single file.
- `bridge/requirements.txt` — just `websockets`.
