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
with markdown converted to ANSI (`*italic*`, `**bold**`, code fences).

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
