#!/usr/bin/env python3
"""Standalone debug telnet server — no SillyTavern required.

Spins up just the telnet half of the bridge so you can connect with a vintage
client (NCSA Telnet on System 7, a VT100, etc.) and confirm the terminal path
works *before* wiring up the full program. It reuses the exact TelnetTerminal
and text-rendering code from bridge.py, so whatever you see here is what the
real bridge will produce.

There is no WebSocket and no AI. Instead a tiny loopback stands in for
SillyTavern: anything you type comes back as a "reply" rendered through the
same print_message() pipeline (wrapping, margins, italic remap, charset,
teletype). A few slash-commands let you exercise specific features.

Usage:
    python3 debug_telnet.py                 # listen on 0.0.0.0:2323
    python3 debug_telnet.py --telnet 23     # privileged port (needs sudo)
    python3 debug_telnet.py --vintage mac   # System 7 / NCSA Telnet preset
    python3 debug_telnet.py --vintage vt100 --cps 30

All the rendering flags from bridge.py work here too (--margin, --max-width,
--cps, --para-pause, --italic, --charset, --vintage).

Once connected, type anything to see it echoed back as a formatted reply, or:
    /help     list the debug commands
    /sample   print a multi-paragraph sample with *italics* and Unicode
    /ruler    print a column ruler to check width / wrapping
    /charset  show how the current charset renders tricky glyphs
    /width    report the negotiated client width (NAWS)
    /history  replay a fake chat backlog (tests print_history)
    /quit     disconnect
"""

import argparse
import asyncio
import sys
import threading

# Reuse everything from the real bridge so this stays a faithful mirror.
from bridge import (
    Settings,
    TelnetTerminal,
    VINTAGE_PRESETS,
    DEFAULT_MARGIN,
    DEFAULT_MAX_WIDTH,
    DEFAULT_TYPE_CPS,
    DEFAULT_PARA_PAUSE,
    log,
)


SAMPLE_TEXT = (
    "The terminal flickered to life. \x1b[3mIt actually works,\x1b[23m she "
    "thought, watching the cursor blink in the phosphor glow.\n\n"
    "This paragraph exists to test word wrapping across the negotiated "
    "width. It should break cleanly at spaces, keep the left and right "
    "margins, and never split an ANSI escape sequence in half.\n\n"
    "Unicode check: “smart quotes,” an em—dash, an ellipsis… "
    "and a bullet • list item. Under --charset macroman these survive; "
    "under ascii they transliterate; under utf8 they pass through."
)


def _ruler(width: int) -> str:
    """A column ruler `width` cells wide, e.g. ....5...10...15."""
    chars = []
    for i in range(1, width + 1):
        if i % 10 == 0:
            chars.append(str((i // 10) % 10))
        elif i % 5 == 0:
            chars.append("+")
        else:
            chars.append(".")
    return "".join(chars)


CHARSET_PROBE = (
    "dashes - – —   quotes ‘x’ “y”   "
    "ellipsis… bullet• degree° times× arrow→"
)

# A fake backlog mirroring what the extension sends as {type: "history"}.
SAMPLE_HISTORY = [
    {"is_user": False, "name": "Narrator",
     "text": "The cursor blinks in the green glow. \x1b[3mSomeone is already "
             "here,\x1b[23m waiting for you to speak."},
    {"is_user": True, "name": "You", "text": "> hello? is anyone there"},
    {"is_user": False, "name": "Narrator",
     "text": "A reply scrolls up from the dark, one line at a time. "
             "\x1b[1mYes.\x1b[22m I have been waiting a long time for a "
             "connection like this one."},
    {"is_user": True, "name": "You", "text": "> who are you"},
]


class LoopbackBridge:
    """Drop-in stand-in for bridge.Bridge with no WebSocket and no AI.

    Implements just the surface TelnetTerminal touches: a `terminal` slot and
    `send_user_line()`. User input is either a debug command or echoed back as
    a rendered "reply".
    """

    FAKE_CHARS = ["Narrator", "Ada Lovelace", "HAL 9000", "Detective Noir"]

    def __init__(self, reply_delay: float = 0.15):
        self.terminal = None
        self.reply_delay = reply_delay
        self.current_char = self.FAKE_CHARS[0]
        self.char_menu = []

    # TelnetTerminal calls these:
    async def send_user_line(self, line: str):
        stripped = line.strip()
        if stripped.startswith("/"):
            await self._command(stripped)
            return
        # Simulate an AI reply: small pause, then echo through the real
        # rendering pipeline so wrapping/italics/charset are all exercised.
        if self.reply_delay > 0:
            await asyncio.sleep(self.reply_delay)
        await self._say(f"You said: \x1b[3m{line}\x1b[23m")

    async def _say(self, text: str):
        if self.terminal:
            await self.terminal.print_message(text)

    async def _command(self, cmd: str):
        name = cmd[1:].split()[0].lower() if len(cmd) > 1 else ""
        term = self.terminal
        if term is None:
            return
        if name in ("help", "?", "h"):
            await self._say(
                "Debug commands:\n\n"
                "/sample  - multi-paragraph sample (italics + Unicode)\n"
                "/ruler   - column ruler at the negotiated width\n"
                "/charset - tricky glyphs under the active charset\n"
                "/width   - report negotiated client width (NAWS)\n"
                "/history - replay a fake chat backlog\n"
                "/chars   - list fake characters\n"
                "/char N  - switch to a fake character (by number or name)\n"
                "/new     - simulate starting a fresh chat\n"
                "/quit    - disconnect\n\n"
                "Anything else is echoed back as a formatted reply."
            )
        elif name == "sample":
            await self._say(SAMPLE_TEXT)
        elif name == "ruler":
            w = term.effective_width()
            await term.notify(f"effective text width = {w} cols "
                              f"(client {term.width}, margin {Settings.margin})")
            await self._say(_ruler(w))
        elif name == "charset":
            await term.notify(f"charset = {Settings.charset}")
            await self._say(CHARSET_PROBE)
        elif name == "history":
            await term.notify("replaying fake backlog (print_history)")
            await term.print_history(SAMPLE_HISTORY)
        elif name in ("chars", "list", "who"):
            self.char_menu = list(self.FAKE_CHARS)
            lines = ["Characters:", ""]
            for i, c in enumerate(self.FAKE_CHARS, 1):
                mark = "  *" if c == self.current_char else ""
                lines.append(f"  {i:>2}. {c}{mark}")
            lines.append("")
            lines.append("Type  /char <number or name>  to switch.")
            await term.print_block("\n".join(lines), instant=True)
        elif name in ("char", "select"):
            arg = cmd[1:].split(None, 1)
            arg = arg[1].strip() if len(arg) > 1 else ""
            if not arg:
                await term.notify("[usage: /char <number or name> — see /chars]")
                return
            target = arg
            if arg.isdigit() and self.char_menu:
                idx = int(arg) - 1
                if 0 <= idx < len(self.char_menu):
                    target = self.char_menu[idx]
                else:
                    await term.notify(f"[no character #{arg} — try /chars]")
                    return
            self.current_char = target
            await term.notify(f"[now chatting with {target}]")
            await term.print_history(SAMPLE_HISTORY)
        elif name in ("new", "newchat"):
            await term.notify("[new chat started — old one kept (simulated)]")
            await term.print_history(SAMPLE_HISTORY[:1])
        elif name == "width":
            await term.notify(
                f"client width = {term.width} cols (NAWS); "
                f"effective = {term.effective_width()} cols"
            )
        elif name in ("quit", "exit", "bye", "q"):
            await term.notify("[closing — debug telnet]")
            await term.close()
        else:
            await term.notify(f"[unknown command: /{name} — try /help]")

    # ── Terminal slot management (mirrors bridge.Bridge minimal surface) ────
    async def set_terminal(self, term):
        old = self.terminal
        self.terminal = term
        if old and old is not term:
            try:
                await old.close()
            except Exception:
                pass

    async def clear_terminal(self, term):
        if self.terminal is term:
            self.terminal = None

    async def handle_telnet(self, reader, writer):
        peer = writer.get_extra_info("peername")
        log(f"debug telnet client connected: {peer}")
        term = TelnetTerminal(self, reader, writer, peer)
        await self.set_terminal(term)
        try:
            await term.negotiate()
            # Mirror the real bridge: replay backlog on connect before the prompt.
            await term.print_history(SAMPLE_HISTORY)
            # Greet so the user immediately sees formatted output on connect.
            await self._say(
                "Debug telnet bridge — no SillyTavern attached.\n\n"
                "If you can read this, wrapped and padded, the terminal path "
                "works. Type /help for debug commands, or just type a line to "
                "see it echoed back as a formatted reply."
            )
            await term.run_input_loop()
        finally:
            await self.clear_terminal(term)
            await term.close()
            log(f"debug telnet client disconnected: {peer}")


async def main_async(args):
    lb = LoopbackBridge(reply_delay=args.reply_delay)

    host, port = parse_endpoint(args.telnet, "0.0.0.0")
    server = await asyncio.start_server(lb.handle_telnet, host, port)
    log(f"Debug telnet listening on {host}:{port} "
        f"(charset={Settings.charset}, italic={Settings.italic_mode}, "
        f"max_width={Settings.max_width})")
    log("Connect your vintage Mac to this host/port. Ctrl+C here to stop.")

    async with server:
        await server.serve_forever()


def parse_endpoint(spec: str, default_host: str):
    if ":" in spec:
        host, port_s = spec.rsplit(":", 1)
        return (host or default_host, int(port_s))
    return (default_host, int(spec))


def main():
    p = argparse.ArgumentParser(
        description="Standalone debug telnet server (no SillyTavern)")
    p.add_argument("--telnet", default="2323",
                   help="listen endpoint: PORT or HOST:PORT (default: %(default)s). "
                        "Use 23 for the standard telnet port (needs sudo).")
    p.add_argument("--reply-delay", type=float, default=0.15,
                   help="seconds to wait before echoing a reply (default: %(default)s)")
    # Same rendering flags as bridge.py:
    p.add_argument("--margin", type=int, default=DEFAULT_MARGIN)
    p.add_argument("--max-width", type=int, default=DEFAULT_MAX_WIDTH)
    p.add_argument("--cps", type=float, default=DEFAULT_TYPE_CPS)
    p.add_argument("--para-pause", type=float, default=DEFAULT_PARA_PAUSE)
    p.add_argument("--italic",
                   choices=("ansi", "reverse", "underline", "asterisk", "off"),
                   default=None)
    p.add_argument("--charset", choices=("utf8", "ascii", "macroman"),
                   default=None)
    p.add_argument("--vintage", choices=tuple(VINTAGE_PRESETS.keys()),
                   default=None)
    args = p.parse_args()

    if args.vintage:
        preset = VINTAGE_PRESETS[args.vintage]
        Settings.italic_mode = preset.get("italic_mode", Settings.italic_mode)
        Settings.charset = preset.get("charset", Settings.charset)
        Settings.max_width = preset.get("max_width", Settings.max_width)
    if args.italic is not None:
        Settings.italic_mode = args.italic
    if args.charset is not None:
        Settings.charset = args.charset

    Settings.margin = max(0, args.margin)
    if args.max_width != DEFAULT_MAX_WIDTH or not args.vintage:
        Settings.max_width = args.max_width
    Settings.cps = max(0.0, args.cps)
    Settings.para_pause = max(0.0, args.para_pause)

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        sys.stderr.write("\n")


if __name__ == "__main__":
    main()
