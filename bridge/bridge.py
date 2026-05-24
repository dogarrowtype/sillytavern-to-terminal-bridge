#!/usr/bin/env python3
"""SillyTavern Terminal Bridge — companion process.

Listens on a local WebSocket port for the SillyTavern extension. Prints AI
replies received from the extension to stdout, and forwards lines typed on
stdin back to the extension as user turns.
"""

import argparse
import asyncio
import json
import re
import shutil
import sys
import threading
import time

import websockets

PROMPT = "\x1b[36;2m> \x1b[0m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"
CLEAR_LINE = "\r\x1b[2K"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
DEFAULT_MARGIN = 2
DEFAULT_MAX_WIDTH = 78
DEFAULT_TYPE_CPS = 0
DEFAULT_PARA_PAUSE = 0.0


class Settings:
    margin = DEFAULT_MARGIN
    max_width = DEFAULT_MAX_WIDTH
    cps = DEFAULT_TYPE_CPS
    para_pause = DEFAULT_PARA_PAUSE


def visible_width(s: str) -> int:
    return len(ANSI_RE.sub("", s))


def tokenize(text: str):
    """Split a paragraph into tokens of (kind, value).

    kinds: 'ansi' (zero-width), 'space' (run of whitespace), 'word' (visible).
    """
    tokens = []
    i = 0
    n = len(text)
    while i < n:
        m = ANSI_RE.match(text, i)
        if m:
            tokens.append(("ansi", m.group(0)))
            i = m.end()
            continue
        if text[i].isspace():
            j = i
            while j < n and text[j].isspace() and not ANSI_RE.match(text, j):
                j += 1
            tokens.append(("space", text[i:j]))
            i = j
            continue
        j = i
        while j < n:
            if text[j].isspace():
                break
            if ANSI_RE.match(text, j):
                break
            j += 1
        tokens.append(("word", text[i:j]))
        i = j
    return tokens


def hard_break(word: str, width: int):
    """Split an over-long word into width-sized chunks (preserving any leading ANSI)."""
    chunks = []
    cur = ""
    cur_vis = 0
    i = 0
    while i < len(word):
        m = ANSI_RE.match(word, i)
        if m:
            cur += m.group(0)
            i = m.end()
            continue
        if cur_vis >= width:
            chunks.append(cur)
            cur = ""
            cur_vis = 0
        cur += word[i]
        cur_vis += 1
        i += 1
    if cur:
        chunks.append(cur)
    return chunks


def wrap_paragraph(text: str, width: int):
    """Wrap one paragraph (no embedded newlines) to a list of lines."""
    if width <= 0:
        return [text]
    tokens = tokenize(text)
    lines = []
    line = ""
    line_vis = 0
    pending_space = ""

    for kind, val in tokens:
        if kind == "ansi":
            line += val
            continue
        if kind == "space":
            if line_vis == 0:
                continue
            pending_space = " "
            continue
        w = visible_width(val)
        if w > width and line_vis == 0:
            for chunk in hard_break(val, width):
                lines.append(chunk)
            line = ""
            line_vis = 0
            pending_space = ""
            continue
        space_cost = 1 if pending_space and line_vis > 0 else 0
        if line_vis + space_cost + w > width:
            lines.append(line)
            line = val
            line_vis = w
            pending_space = ""
        else:
            if pending_space and line_vis > 0:
                line += pending_space
                line_vis += 1
            line += val
            line_vis += w
            pending_space = ""

    if line:
        lines.append(line)
    if not lines:
        lines.append("")
    return lines


def wrap_text(text: str, width: int):
    out_lines = []
    for paragraph in text.split("\n"):
        out_lines.extend(wrap_paragraph(paragraph, width))
    return out_lines


def effective_width() -> int:
    cols = shutil.get_terminal_size(fallback=(80, 24)).columns
    usable = cols - 2 * Settings.margin
    if Settings.max_width > 0:
        usable = min(usable, Settings.max_width)
    return max(20, usable)


class Bridge:
    def __init__(self):
        self.client = None
        self.loop = None
        self.lock = asyncio.Lock()

    async def set_client(self, ws):
        async with self.lock:
            old = self.client
            self.client = ws
        if old is not None and old is not ws:
            try:
                await old.close()
            except Exception:
                pass

    async def clear_client(self, ws):
        async with self.lock:
            if self.client is ws:
                self.client = None

    async def handle_connection(self, ws):
        await self.set_client(ws)
        write_prompt()
        try:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "out" and isinstance(data.get("text"), str):
                    print_message(data["text"])
        except websockets.ConnectionClosed:
            pass
        finally:
            await self.clear_client(ws)

    async def send_user_line(self, line: str):
        async with self.lock:
            ws = self.client
        if ws is None:
            sys.stdout.write(f"{DIM}[no client connected]{RESET}\n")
            write_prompt()
            return
        try:
            await ws.send(json.dumps({"type": "in", "text": line}))
        except Exception as exc:
            sys.stdout.write(f"{DIM}[send failed: {exc}]{RESET}\n")
            write_prompt()


def write_prompt():
    sys.stdout.write(PROMPT)
    sys.stdout.flush()


def teletype_write(s: str):
    """Write a string with optional per-character pacing.

    ANSI escapes are emitted instantly so styling switches don't show as
    visible delay between letters.
    """
    cps = Settings.cps
    if cps <= 0:
        sys.stdout.write(s)
        sys.stdout.flush()
        return
    delay = 1.0 / cps
    i = 0
    n = len(s)
    while i < n:
        m = ANSI_RE.match(s, i)
        if m:
            sys.stdout.write(m.group(0))
            i = m.end()
            continue
        sys.stdout.write(s[i])
        sys.stdout.flush()
        if not s[i].isspace() or s[i] == " ":
            time.sleep(delay)
        i += 1


def print_message(text: str):
    sys.stdout.write(CLEAR_LINE)
    width = effective_width()
    pad = " " * Settings.margin
    lines = wrap_text(text.rstrip("\n"), width)

    sys.stdout.write("\n")
    blank_run = 0
    for line in lines:
        if line.strip() == "":
            blank_run += 1
            if blank_run > 1:
                continue
            teletype_write("\n")
            if Settings.para_pause > 0 and Settings.cps > 0:
                time.sleep(Settings.para_pause)
            continue
        blank_run = 0
        teletype_write(pad + line + RESET + "\n")
    sys.stdout.write("\n")
    write_prompt()


def stdin_reader(bridge: Bridge, stop_event: threading.Event):
    while not stop_event.is_set():
        try:
            line = input()
        except EOFError:
            stop_event.set()
            return
        except KeyboardInterrupt:
            stop_event.set()
            return
        if not line.strip():
            write_prompt()
            continue
        asyncio.run_coroutine_threadsafe(bridge.send_user_line(line), bridge.loop)


async def main_async(host: str, port: int):
    bridge = Bridge()
    bridge.loop = asyncio.get_running_loop()

    stop_event = threading.Event()
    reader_thread = threading.Thread(
        target=stdin_reader, args=(bridge, stop_event), daemon=True
    )
    reader_thread.start()

    async with websockets.serve(bridge.handle_connection, host, port):
        sys.stdout.write(f"{DIM}[bridge listening on ws://{host}:{port}]{RESET}\n")
        write_prompt()
        try:
            while not stop_event.is_set():
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            pass


def main():
    parser = argparse.ArgumentParser(description="SillyTavern Terminal Bridge")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5005)
    parser.add_argument(
        "--margin",
        type=int,
        default=DEFAULT_MARGIN,
        help="left/right padding columns (default: %(default)s)",
    )
    parser.add_argument(
        "--max-width",
        type=int,
        default=DEFAULT_MAX_WIDTH,
        help="cap line width regardless of terminal size; 0 = no cap (default: %(default)s)",
    )
    parser.add_argument(
        "--cps",
        type=float,
        default=DEFAULT_TYPE_CPS,
        help="teletype effect: characters per second; 0 = instant (default: %(default)s)",
    )
    parser.add_argument(
        "--para-pause",
        type=float,
        default=DEFAULT_PARA_PAUSE,
        help="extra pause in seconds between paragraphs when --cps is on (default: %(default)s)",
    )
    args = parser.parse_args()

    Settings.margin = max(0, args.margin)
    Settings.max_width = args.max_width
    Settings.cps = max(0.0, args.cps)
    Settings.para_pause = max(0.0, args.para_pause)

    try:
        asyncio.run(main_async(args.host, args.port))
    except KeyboardInterrupt:
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
