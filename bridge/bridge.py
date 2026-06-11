#!/usr/bin/env python3
"""SillyTavern Terminal Bridge — companion process.

Listens on a local WebSocket port for the SillyTavern extension. Prints AI
replies to the active terminal session (stdio by default, or a telnet client
when --telnet is given) and forwards user input back as user turns.
"""

import argparse
import asyncio
import json
import re
import shutil
import sys
import threading
from typing import Optional

import websockets

# ── Styling ────────────────────────────────────────────────────────────────

PROMPT = "\x1b[36;2m> \x1b[0m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"
CLEAR_LINE = "\r\x1b[2K"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

DEFAULT_MARGIN = 2
DEFAULT_MAX_WIDTH = 78
DEFAULT_TYPE_CPS = 0.0
DEFAULT_PARA_PAUSE = 0.0


class Settings:
    margin = DEFAULT_MARGIN
    max_width = DEFAULT_MAX_WIDTH
    cps = DEFAULT_TYPE_CPS
    para_pause = DEFAULT_PARA_PAUSE
    italic_mode = "ansi"     # ansi | reverse | underline | off
    charset = "utf8"          # utf8 | ascii | macroman


# ── ASCII transliteration table for --charset ascii ────────────────────────

_ASCII_MAP = {
    "‐": "-", "‑": "-", "‒": "-", "–": "-",
    "—": "--", "―": "--",
    "‘": "'", "’": "'", "‚": ",", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "…": "...", "•": "*", "‣": ">", "·": ".",
    " ": " ", " ": " ", " ": " ", " ": " ",
    " ": " ", " ": " ", " ": " ",
    "«": "<<", "»": ">>",
    "½": "1/2", "¼": "1/4", "¾": "3/4",
    "°": "deg", "×": "x", "÷": "/",
    "±": "+/-", "→": "->", "←": "<-",
    "↑": "^", "↓": "v",
}


def _to_ascii(text: str) -> str:
    out = []
    for ch in text:
        if ord(ch) < 128:
            out.append(ch)
        elif ch in _ASCII_MAP:
            out.append(_ASCII_MAP[ch])
        else:
            out.append("?")
    return "".join(out)


def encode_for_wire(text: str) -> bytes:
    """Encode an outgoing string per the active charset setting."""
    if Settings.charset == "ascii":
        return _to_ascii(text).encode("ascii", errors="replace")
    if Settings.charset == "macroman":
        # Most extra Latin glyphs (em-dash, smart quotes, ellipsis, …) round-trip
        # cleanly to MacRoman. Anything else becomes '?'.
        return text.encode("mac_roman", errors="replace")
    return text.encode("utf-8", errors="replace")


def decode_from_wire(data: bytes) -> str:
    if Settings.charset == "macroman":
        return data.decode("mac_roman", errors="replace")
    if Settings.charset == "ascii":
        return data.decode("ascii", errors="replace")
    return data.decode("utf-8", errors="replace")


# ── Italic remap (VT100 has no italic; map to reverse / underline / nothing) ──

_ITALIC_ON_RE = re.compile(r"\x1b\[3m")
_ITALIC_OFF_RE = re.compile(r"\x1b\[23m")


def remap_italic(text: str) -> str:
    mode = Settings.italic_mode
    if mode == "ansi":
        return text
    if mode == "reverse":
        text = _ITALIC_ON_RE.sub("\x1b[7m", text)
        text = _ITALIC_OFF_RE.sub("\x1b[27m", text)
        return text
    if mode == "underline":
        text = _ITALIC_ON_RE.sub("\x1b[4m", text)
        text = _ITALIC_OFF_RE.sub("\x1b[24m", text)
        return text
    if mode == "asterisk":
        # No attribute change — re-mark italics with literal *asterisks*.
        # Best for displays where reverse/underline look bad (e.g. vintage Mac).
        text = _ITALIC_ON_RE.sub("*", text)
        text = _ITALIC_OFF_RE.sub("*", text)
        return text
    # off
    text = _ITALIC_ON_RE.sub("", text)
    text = _ITALIC_OFF_RE.sub("", text)
    return text


# ── Width-aware wrapping (ANSI-safe) ──────────────────────────────────────

def visible_width(s: str) -> int:
    return len(ANSI_RE.sub("", s))


def tokenize(text: str):
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


# ── Logging ────────────────────────────────────────────────────────────────

_log_lock = threading.Lock()


def log(msg: str) -> None:
    """Write a bridge-operator log line to stderr (never to the active terminal)."""
    with _log_lock:
        sys.stderr.write(f"{DIM}[bridge]{RESET} {msg}\n")
        sys.stderr.flush()


# ── Terminal base ──────────────────────────────────────────────────────────

class Terminal:
    """One active user session. Subclasses provide the transport."""

    def __init__(self, bridge: "Bridge", width: int = 80):
        self.bridge = bridge
        self.width = width
        self.write_lock = asyncio.Lock()
        self.input_buf = ""

    def effective_width(self) -> int:
        usable = self.width - 2 * Settings.margin
        if Settings.max_width > 0:
            usable = min(usable, Settings.max_width)
        return max(20, usable)

    async def _emit_bytes(self, data: bytes) -> None:
        raise NotImplementedError

    async def _emit(self, s: str) -> None:
        await self._emit_bytes(encode_for_wire(s))

    async def close(self) -> None:
        pass

    # ── Output ────────────────────────────────────────────────────────────

    async def write_prompt(self) -> None:
        await self._emit(PROMPT)

    async def redraw_prompt_with_buffer(self) -> None:
        await self._emit(CLEAR_LINE + PROMPT + self.input_buf)

    async def _teletype(self, s: str) -> None:
        if Settings.cps <= 0:
            await self._emit(s)
            return
        delay = 1.0 / Settings.cps
        i = 0
        n = len(s)
        while i < n:
            m = ANSI_RE.match(s, i)
            if m:
                await self._emit(m.group(0))
                i = m.end()
                continue
            await self._emit(s[i])
            if not s[i].isspace() or s[i] == " ":
                await asyncio.sleep(delay)
            i += 1

    async def _emit_block(self, raw_text: str, instant: bool = False) -> None:
        """Render one wrapped, padded message body (no leading/trailing blank,
        no prompt redraw). With instant=True, skip teletype and para pauses."""
        text = remap_italic(raw_text)
        width = self.effective_width()
        pad = " " * Settings.margin
        lines = wrap_text(text.rstrip("\n"), width)
        blank_run = 0
        for line in lines:
            if line.strip() == "":
                blank_run += 1
                if blank_run > 1:
                    continue
                await self._emit("\n")
                if not instant and Settings.para_pause > 0 and Settings.cps > 0:
                    await asyncio.sleep(Settings.para_pause)
                continue
            blank_run = 0
            if instant:
                await self._emit(pad + line + RESET + "\n")
            else:
                await self._teletype(pad + line + RESET + "\n")

    async def print_block(self, raw_text: str, instant: bool = False) -> None:
        """Print one framed message (blank line, body, blank line, prompt).
        instant=True renders immediately for menus/system output."""
        async with self.write_lock:
            await self._emit(CLEAR_LINE + "\n")
            await self._emit_block(raw_text, instant=instant)
            await self._emit("\n")
            await self.redraw_prompt_with_buffer()

    async def print_message(self, raw_text: str) -> None:
        await self.print_block(raw_text, instant=False)

    async def print_history(self, messages: list) -> None:
        """Replay prior chat turns (instantly) before the live prompt.

        Each entry is {"is_user": bool, "name": str, "text": str}. User turns
        are shown with the prompt marker to mirror the live echo; AI turns are
        rendered through the same wrapping/charset pipeline as live replies.
        """
        rendered = [m for m in messages if (m.get("text") or "").strip()]
        if not rendered:
            return
        async with self.write_lock:
            await self._emit(CLEAR_LINE)
            await self._emit(f"{DIM}--- history ---{RESET}\n")
            width = self.effective_width()
            for m in rendered:
                text = m.get("text") or ""
                await self._emit("\n")
                if m.get("is_user"):
                    body = text[2:] if text.startswith("> ") else text
                    lines = wrap_text(remap_italic(body), width)
                    for idx, line in enumerate(lines):
                        prefix = PROMPT if idx == 0 else "  "
                        await self._emit(prefix + line + RESET + "\n")
                else:
                    await self._emit_block(text, instant=True)
            await self._emit("\n")
            await self._emit(f"{DIM}--- end of history ---{RESET}\n\n")
            await self.redraw_prompt_with_buffer()

    async def notify(self, msg: str) -> None:
        async with self.write_lock:
            await self._emit(CLEAR_LINE + f"{DIM}{msg}{RESET}\n")
            await self.redraw_prompt_with_buffer()


# ── Stdio terminal (cool-retro-term, plain shell) ──────────────────────────

class StdioTerminal(Terminal):
    def __init__(self, bridge, loop):
        cols = shutil.get_terminal_size(fallback=(80, 24)).columns
        super().__init__(bridge, cols)
        self.loop = loop

    def effective_width(self) -> int:
        # Re-read each call so terminal resizes are picked up.
        self.width = shutil.get_terminal_size(fallback=(80, 24)).columns
        return super().effective_width()

    async def _emit_bytes(self, data: bytes) -> None:
        sys.stdout.buffer.write(data)
        sys.stdout.flush()

    def start_input_thread(self, stop_event: threading.Event) -> threading.Thread:
        def reader():
            while not stop_event.is_set():
                try:
                    line = input()
                except (EOFError, KeyboardInterrupt):
                    stop_event.set()
                    return
                if not line.strip():
                    asyncio.run_coroutine_threadsafe(self.write_prompt(), self.loop)
                    continue
                asyncio.run_coroutine_threadsafe(
                    self.bridge.send_user_line(line), self.loop
                )
        t = threading.Thread(target=reader, daemon=True)
        t.start()
        return t


# ── Telnet protocol ────────────────────────────────────────────────────────

IAC = 0xff
DONT = 0xfe
DO = 0xfd
WONT = 0xfc
WILL = 0xfb
SB = 0xfa
SE = 0xf0

OPT_ECHO = 1
OPT_SGA = 3        # Suppress Go-Ahead
OPT_TTYPE = 24
OPT_NAWS = 31


class TelnetTerminal(Terminal):
    # Parser states for the IAC tokenizer.
    _S_DATA = 0
    _S_IAC = 1
    _S_NEG = 2          # after WILL/WONT/DO/DONT, expect option byte
    _S_SB = 3           # collecting subnegotiation bytes
    _S_SB_IAC = 4       # IAC seen inside SB (could be IAC SE or IAC IAC)

    def __init__(self, bridge, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter, peer):
        super().__init__(bridge, 80)
        self.reader = reader
        self.writer = writer
        self.peer = peer
        self._state = self._S_DATA
        self._iac_verb: Optional[int] = None
        self._sb_buf = bytearray()
        self._last_cr = False
        self._closed = False

    async def _emit_bytes(self, data: bytes) -> None:
        if self._closed:
            return
        # 1. Escape any 0xff data bytes (RFC 854).
        out = data.replace(b"\xff", b"\xff\xff")
        # 2. Normalize \n to CRLF for NVT.
        out = out.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        try:
            self.writer.write(out)
            await self.writer.drain()
        except (ConnectionResetError, BrokenPipeError, OSError):
            self._closed = True

    async def _send_raw(self, data: bytes) -> None:
        """Send telnet protocol bytes (IAC negotiations) without escaping."""
        if self._closed:
            return
        try:
            self.writer.write(data)
            await self.writer.drain()
        except (ConnectionResetError, BrokenPipeError, OSError):
            self._closed = True

    async def negotiate(self) -> None:
        # Server-side echo + char-at-a-time. Ask the client for NAWS.
        await self._send_raw(bytes([
            IAC, WILL, OPT_ECHO,
            IAC, WILL, OPT_SGA,
            IAC, DO, OPT_SGA,
            IAC, DO, OPT_NAWS,
        ]))
        await self.notify(
            f"[connected — {Settings.charset}, {self.width}c — "
            f"/help for commands]"
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass

    # ── Input loop ────────────────────────────────────────────────────────

    async def run_input_loop(self) -> None:
        try:
            while not self.reader.at_eof():
                chunk = await self.reader.read(256)
                if not chunk:
                    break
                await self._feed(chunk)
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass

    async def _feed(self, data: bytes) -> None:
        for b in data:
            await self._step(b)

    async def _step(self, b: int) -> None:
        s = self._state
        if s == self._S_DATA:
            if b == IAC:
                self._state = self._S_IAC
            else:
                await self._on_data_byte(b)
        elif s == self._S_IAC:
            if b == IAC:
                await self._on_data_byte(0xff)
                self._state = self._S_DATA
            elif b in (WILL, WONT, DO, DONT):
                self._iac_verb = b
                self._state = self._S_NEG
            elif b == SB:
                self._sb_buf = bytearray()
                self._state = self._S_SB
            else:
                # NOP / GA / DM / BREAK / etc — ignore.
                self._state = self._S_DATA
        elif s == self._S_NEG:
            await self._on_negotiate(self._iac_verb, b)
            self._state = self._S_DATA
        elif s == self._S_SB:
            if b == IAC:
                self._state = self._S_SB_IAC
            else:
                self._sb_buf.append(b)
        elif s == self._S_SB_IAC:
            if b == SE:
                await self._on_subneg(bytes(self._sb_buf))
                self._sb_buf = bytearray()
                self._state = self._S_DATA
            elif b == IAC:
                self._sb_buf.append(0xff)
                self._state = self._S_SB
            else:
                self._state = self._S_DATA

    async def _on_negotiate(self, verb: int, opt: int) -> None:
        # We don't need much. Just reply politely so the client doesn't hang.
        if verb == WILL:
            if opt in (OPT_NAWS, OPT_TTYPE):
                # We did say DO NAWS; nothing more to do.
                pass
            else:
                await self._send_raw(bytes([IAC, DONT, opt]))
        elif verb == DO:
            if opt in (OPT_ECHO, OPT_SGA):
                pass  # already said WILL
            else:
                await self._send_raw(bytes([IAC, WONT, opt]))
        # WONT/DONT: ignore (silent accept)

    async def _on_subneg(self, buf: bytes) -> None:
        if len(buf) >= 5 and buf[0] == OPT_NAWS:
            cols = (buf[1] << 8) | buf[2]
            if 20 <= cols <= 500:
                self.width = cols
                log(f"NAWS: client width = {cols}")

    # ── Line editor (runs on each data byte) ──────────────────────────────

    async def _on_data_byte(self, b: int) -> None:
        # CR-LF / CR-NUL handling: CR submits, swallow the following LF or NUL.
        if self._last_cr:
            self._last_cr = False
            if b in (0x00, 0x0a):
                return
        if b == 0x0d:  # CR
            self._last_cr = True
            await self._submit_line()
            return
        if b == 0x0a:  # bare LF
            await self._submit_line()
            return
        if b == 0x03:  # Ctrl+C — close
            await self._emit("^C\r\n")
            await self.close()
            return
        if b == 0x04:  # Ctrl+D — close on empty buffer
            if not self.input_buf:
                await self._emit("\r\n")
                await self.close()
            return
        if b in (0x08, 0x7f):  # BS or DEL — both treated as backspace
            if self.input_buf:
                self.input_buf = self.input_buf[:-1]
                await self._emit("\b \b")
            return
        if b == 0x15:  # Ctrl+U — kill whole line
            n = len(self.input_buf)
            self.input_buf = ""
            if n:
                await self._emit("\b \b" * n)
            return
        if b == 0x17:  # Ctrl+W — kill word
            i = len(self.input_buf) - 1
            while i >= 0 and self.input_buf[i] == " ":
                i -= 1
            while i >= 0 and self.input_buf[i] != " ":
                i -= 1
            kill = len(self.input_buf) - (i + 1)
            self.input_buf = self.input_buf[: i + 1]
            if kill:
                await self._emit("\b \b" * kill)
            return
        if b < 0x20:
            # Other control chars: ignore.
            return
        # Printable byte — decode per active charset, append, echo.
        ch = decode_from_wire(bytes([b]))
        self.input_buf += ch
        await self._emit(ch)

    async def _submit_line(self) -> None:
        line = self.input_buf
        self.input_buf = ""
        await self._emit("\r\n")
        if not line.strip():
            await self.write_prompt()
            return
        await self.bridge.send_user_line(line)


# ── Bridge: WS to SillyTavern + active Terminal ────────────────────────────

class Bridge:
    def __init__(self):
        self.ws_client = None
        self.terminal: Optional[Terminal] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.lock = asyncio.Lock()
        # Pending request/reply waiters, keyed by reply kind ("history",
        # "chars", "selected"). Each is a list of Futures.
        self._waiters: dict = {}
        # Names from the last /chars listing, so "/char <n>" can resolve by index.
        self.char_menu: list = []

    async def set_ws_client(self, ws):
        async with self.lock:
            old = self.ws_client
            self.ws_client = ws
        if old is not None and old is not ws:
            try:
                await old.close()
            except Exception:
                pass

    async def clear_ws_client(self, ws):
        async with self.lock:
            if self.ws_client is ws:
                self.ws_client = None

    async def set_terminal(self, term: Terminal):
        async with self.lock:
            old = self.terminal
            self.terminal = term
        if old and old is not term:
            try:
                await old.notify("[replaced by another session]")
            except Exception:
                pass
            try:
                await old.close()
            except Exception:
                pass

    async def clear_terminal(self, term: Terminal):
        async with self.lock:
            if self.terminal is term:
                self.terminal = None

    # ── WS handling ───────────────────────────────────────────────────────

    async def handle_ws(self, ws):
        await self.set_ws_client(ws)
        log("SillyTavern extension connected")
        try:
            if self.terminal:
                await self.terminal.write_prompt()
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                mtype = data.get("type")
                if mtype == "out" and isinstance(data.get("text"), str):
                    if self.terminal:
                        await self.terminal.print_message(data["text"])
                    else:
                        log("dropped AI message (no terminal connected)")
                elif mtype == "history" and isinstance(data.get("messages"), list):
                    self._resolve("history", data["messages"])
                elif mtype == "chars" and isinstance(data.get("characters"), list):
                    self._resolve("chars", data)
                elif mtype == "selected":
                    self._resolve("selected", data)
                elif mtype == "new_chat_result":
                    self._resolve("new_chat_result", data)
        except websockets.ConnectionClosed:
            pass
        finally:
            await self.clear_ws_client(ws)
            self._resolve_all(None)
            log("SillyTavern extension disconnected")

    # ── Request / reply plumbing (extension round-trips) ──────────────────

    def _resolve(self, kind: str, payload) -> None:
        """Deliver a reply (or None on failure) to everyone awaiting `kind`."""
        for fut in self._waiters.pop(kind, []):
            if not fut.done():
                fut.set_result(payload)

    def _resolve_all(self, payload=None) -> None:
        for kind in list(self._waiters):
            self._resolve(kind, payload)

    def _discard_waiter(self, kind: str, fut) -> None:
        lst = self._waiters.get(kind)
        if lst and fut in lst:
            lst.remove(fut)

    async def _request(self, kind: str, payload: dict, timeout: float = 5.0):
        """Send `payload` to the extension and await a reply of `kind`.

        Best-effort: returns None if no extension is connected, the send fails,
        or no reply arrives in time — callers degrade gracefully instead of
        stalling the terminal session.
        """
        ws = self.ws_client
        if ws is None or self.loop is None:
            return None
        fut = self.loop.create_future()
        self._waiters.setdefault(kind, []).append(fut)
        try:
            await ws.send(json.dumps(payload))
        except Exception:
            self._discard_waiter(kind, fut)
            return None
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._discard_waiter(kind, fut)
            return None

    async def request_history(self, term: Terminal) -> None:
        """Ask the extension for the chat log and replay it on `term`."""
        messages = await self._request("history", {"type": "history_request"})
        if messages and self.terminal is term:
            await term.print_history(messages)

    # ── Terminal-local commands ───────────────────────────────────────────

    _COMMANDS = ("/help", "/chars", "/list", "/who", "/char", "/select",
                 "/new", "/newchat", "/history")

    async def maybe_handle_command(self, line: str) -> bool:
        """Intercept bridge-local slash commands. Returns True if handled.

        Anything not in `_COMMANDS` (including other "/..." text) is left for
        SillyTavern, so roleplay slash usage still passes through untouched.
        """
        s = line.strip()
        if not s.startswith("/"):
            return False
        parts = s.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd not in self._COMMANDS:
            return False
        term = self.terminal
        if term is None:
            return True
        if cmd == "/help":
            await term.print_block(
                "Bridge commands:\n\n"
                "/chars            list characters (current marked *)\n"
                "/char <n|name>    switch to a character\n"
                "/new              start a fresh chat (keeps the old one)\n"
                "/history          replay this chat from the top\n"
                "/help             this message\n\n"
                "Anything else you type is sent to the AI.",
                instant=True,
            )
        elif cmd in ("/chars", "/list", "/who"):
            await self._cmd_list_chars(term)
        elif cmd in ("/char", "/select"):
            await self._cmd_select_char(term, arg)
        elif cmd in ("/new", "/newchat"):
            await self._cmd_new_chat(term)
        elif cmd == "/history":
            await self.request_history(term)
        return True

    async def _cmd_list_chars(self, term: Terminal) -> None:
        data = await self._request("chars", {"type": "chars_request"})
        if not data:
            await term.notify("[no character list — is SillyTavern connected?]")
            return
        chars = data.get("characters") or []
        self.char_menu = [c.get("name", "") for c in chars]
        if not chars:
            await term.notify("[no characters found]")
            return
        current = data.get("current_name") or ""
        lines = ["Characters:", ""]
        for i, c in enumerate(chars, 1):
            name = c.get("name", "?")
            mark = "  *" if name == current and current else ""
            lines.append(f"  {i:>2}. {name}{mark}")
        lines.append("")
        lines.append("Type  /char <number or name>  to switch.")
        await term.print_block("\n".join(lines), instant=True)

    async def _cmd_select_char(self, term: Terminal, arg: str) -> None:
        if not arg:
            await term.notify("[usage: /char <number or name> — see /chars]")
            return
        name = arg
        if arg.isdigit() and self.char_menu:
            idx = int(arg) - 1
            if 0 <= idx < len(self.char_menu):
                name = self.char_menu[idx]
            else:
                await term.notify(f"[no character #{arg} — try /chars]")
                return
        await term.notify(f"[switching to {name}…]")
        result = await self._request(
            "selected", {"type": "select", "name": name}, timeout=20.0
        )
        if not result:
            await term.notify("[switch timed out — is SillyTavern connected?]")
            return
        if not result.get("ok"):
            err = result.get("error") or "unknown error"
            await term.notify(f"[switch failed: {err}]")
            return
        await term.notify(f"[now chatting with {result.get('name', name)}]")
        if self.terminal is term:
            await self.request_history(term)

    async def _cmd_new_chat(self, term: Terminal) -> None:
        await term.notify("[starting a new chat — the old one is kept…]")
        result = await self._request(
            "new_chat_result", {"type": "new_chat"}, timeout=20.0
        )
        if not result:
            await term.notify("[new chat timed out — is SillyTavern connected?]")
            return
        if not result.get("ok"):
            err = result.get("error") or "unknown error"
            await term.notify(f"[new chat failed: {err}]")
            return
        await term.notify("[new chat started]")
        if self.terminal is term:
            await self.request_history(term)

    async def send_user_line(self, line: str):
        if await self.maybe_handle_command(line):
            return
        ws = self.ws_client
        if ws is None:
            if self.terminal:
                await self.terminal.notify("[no SillyTavern connected]")
            return
        try:
            await ws.send(json.dumps({"type": "in", "text": line}))
        except Exception as exc:
            if self.terminal:
                await self.terminal.notify(f"[send failed: {exc}]")

    # ── Telnet handling ───────────────────────────────────────────────────

    async def handle_telnet(self, reader, writer):
        peer = writer.get_extra_info("peername")
        log(f"telnet client connected: {peer}")
        term = TelnetTerminal(self, reader, writer, peer)
        await self.set_terminal(term)
        try:
            await term.negotiate()
            await self.request_history(term)
            await term.run_input_loop()
        finally:
            await self.clear_terminal(term)
            await term.close()
            log(f"telnet client disconnected: {peer}")


# ── Server orchestration ───────────────────────────────────────────────────

def parse_endpoint(spec: str, default_host: str) -> tuple:
    """Parse 'PORT' or 'HOST:PORT' into (host, port)."""
    if ":" in spec:
        host, port_s = spec.rsplit(":", 1)
        return (host or default_host, int(port_s))
    return (default_host, int(spec))


async def main_async(args):
    bridge = Bridge()
    bridge.loop = asyncio.get_running_loop()

    stop_event = threading.Event()
    stdio_term = None
    if not args.telnet:
        stdio_term = StdioTerminal(bridge, bridge.loop)
        await bridge.set_terminal(stdio_term)
        stdio_term.start_input_thread(stop_event)

    ws_host = args.host
    ws_port = args.port
    ws_server = await websockets.serve(bridge.handle_ws, ws_host, ws_port)
    log(f"WebSocket listening on ws://{ws_host}:{ws_port}")

    telnet_server = None
    if args.telnet:
        thost, tport = parse_endpoint(args.telnet, "0.0.0.0")
        telnet_server = await asyncio.start_server(
            bridge.handle_telnet, thost, tport
        )
        log(f"Telnet listening on {thost}:{tport}")

    if stdio_term:
        await stdio_term.write_prompt()

    try:
        if telnet_server:
            async with telnet_server:
                while not stop_event.is_set():
                    await asyncio.sleep(0.2)
        else:
            while not stop_event.is_set():
                await asyncio.sleep(0.2)
    except asyncio.CancelledError:
        pass
    finally:
        ws_server.close()
        await ws_server.wait_closed()


# ── CLI ────────────────────────────────────────────────────────────────────

VINTAGE_PRESETS = {
    "mac": {
        "italic_mode": "asterisk",
        "charset": "macroman",
        "max_width": 80,
    },
    "vt100": {
        "italic_mode": "reverse",
        "charset": "ascii",
        "max_width": 80,
    },
    "tty": {
        "italic_mode": "off",
        "charset": "ascii",
        "max_width": 72,
    },
}


def main():
    p = argparse.ArgumentParser(description="SillyTavern Terminal Bridge")
    p.add_argument("--host", default="127.0.0.1",
                   help="WebSocket bind host (default: %(default)s)")
    p.add_argument("--port", type=int, default=5005,
                   help="WebSocket bind port (default: %(default)s)")
    p.add_argument("--telnet", default=None,
                   help="Enable telnet server: PORT or HOST:PORT. "
                        "If set, stdio is disabled and the bridge logs to stderr only.")
    p.add_argument("--margin", type=int, default=DEFAULT_MARGIN,
                   help="left/right padding columns (default: %(default)s)")
    p.add_argument("--max-width", type=int, default=DEFAULT_MAX_WIDTH,
                   help="cap line width; 0 = no cap (default: %(default)s)")
    p.add_argument("--cps", type=float, default=DEFAULT_TYPE_CPS,
                   help="teletype chars/sec; 0 = instant (default: %(default)s)")
    p.add_argument("--para-pause", type=float, default=DEFAULT_PARA_PAUSE,
                   help="extra pause between paragraphs when --cps > 0")
    p.add_argument("--italic",
                   choices=("ansi", "reverse", "underline", "asterisk", "off"),
                   default=None,
                   help="how to render italic: ansi (default), reverse, underline, "
                        "asterisk (literal *stars*, default for --vintage mac), or off")
    p.add_argument("--charset",
                   choices=("utf8", "ascii", "macroman"),
                   default=None,
                   help="wire encoding; default utf8, or set by --vintage")
    p.add_argument("--vintage",
                   choices=tuple(VINTAGE_PRESETS.keys()),
                   default=None,
                   help="preset for vintage clients: 'mac' (System 7 / NCSA Telnet), "
                        "'vt100', 'tty'")
    args = p.parse_args()

    # Apply vintage preset first; explicit flags override.
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
