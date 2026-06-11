const MODULE_NAME = 'terminal_bridge';
const LOG_PREFIX = '[TerminalBridge]';

const defaultSettings = Object.freeze({
    enabled: true,
    host: '127.0.0.1',
    port: 5005,
});

const state = {
    ws: null,
    reconnectTimer: null,
    backoffMs: 1000,
    manualDisconnect: false,
    loggedContextKeys: false,
};

function getSettings() {
    const { extensionSettings } = SillyTavern.getContext();
    if (!extensionSettings[MODULE_NAME]) {
        extensionSettings[MODULE_NAME] = structuredClone(defaultSettings);
    }
    for (const key of Object.keys(defaultSettings)) {
        if (!Object.hasOwn(extensionSettings[MODULE_NAME], key)) {
            extensionSettings[MODULE_NAME][key] = defaultSettings[key];
        }
    }
    return extensionSettings[MODULE_NAME];
}

function setStatus(label) {
    const el = document.getElementById('tb_status');
    if (!el) return;
    el.textContent = label;
    el.classList.remove('connected', 'connecting', 'disconnected');
    el.classList.add(label);
}

function stripHtmlToText(html) {
    const doc = new DOMParser().parseFromString(html ?? '', 'text/html');
    return doc.body?.textContent ?? '';
}

function markdownToAnsi(text) {
    let out = text;

    out = out.replace(/```[a-zA-Z0-9_-]*\n?([\s\S]*?)```/g, (_, body) => body.replace(/^\n|\n$/g, ''));

    out = out.replace(/(\*\*|__)(?=\S)([\s\S]*?\S)\1/g, '\x1b[1m$2\x1b[22m');

    out = out.replace(/(?<![*\w])\*(?!\s)([\s\S]*?\S)\*(?!\w)/g, '\x1b[3m$1\x1b[23m');
    out = out.replace(/(?<![_\w])_(?!\s)([\s\S]*?\S)_(?!\w)/g, '\x1b[3m$1\x1b[23m');

    out = out.replace(/`([^`\n]+)`/g, '$1');

    out = out.replace(/\n{3,}/g, '\n\n');

    return out;
}

function formatForTerminal(rawMessage) {
    const plain = stripHtmlToText(rawMessage ?? '');
    return markdownToAnsi(plain);
}

function escapeForStscript(text) {
    let out = text.replace(/\\/g, '\\\\');
    out = out.replace(/\|/g, '\\|');
    out = out.replace(/\{\{/g, '\\{\\{');
    out = out.replace(/\}\}/g, '\\}\\}');
    return out;
}

async function sendUserTurn(text) {
    const ctx = SillyTavern.getContext();
    const trimmed = text.trim();
    if (!trimmed) return;
    const userText = `> ${trimmed}`;

    if (!state.loggedContextKeys) {
        state.loggedContextKeys = true;
        console.debug(LOG_PREFIX, 'context keys:', Object.keys(ctx));
    }

    if (typeof ctx.executeSlashCommandsWithOptions === 'function') {
        const escaped = escapeForStscript(userText);
        try {
            await ctx.executeSlashCommandsWithOptions(`/send ${escaped} | /trigger`);
            return;
        } catch (err) {
            console.error(LOG_PREFIX, 'slash command path failed, falling back:', err);
        }
    }

    if (typeof ctx.sendMessageAsUser === 'function' && typeof ctx.generate === 'function') {
        await ctx.sendMessageAsUser(userText, false);
        await ctx.generate();
        return;
    }

    console.error(LOG_PREFIX, 'no available API to send + trigger; user turn dropped:', userText);
    toastr.error('Terminal Bridge: cannot send message — no compatible SillyTavern API found.');
}

function sendHistory() {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
    const ctx = SillyTavern.getContext();
    const chat = Array.isArray(ctx.chat) ? ctx.chat : [];
    const messages = [];
    for (const msg of chat) {
        if (!msg) continue;
        if (msg.is_system) continue;
        const text = formatForTerminal(msg.mes ?? '');
        if (!text) continue;
        messages.push({ is_user: !!msg.is_user, name: msg.name ?? '', text });
    }
    try {
        state.ws.send(JSON.stringify({ type: 'history', messages }));
    } catch (err) {
        console.error(LOG_PREFIX, 'failed to send history to bridge:', err);
    }
}

function sendCharacters() {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
    const ctx = SillyTavern.getContext();
    const chars = Array.isArray(ctx.characters) ? ctx.characters : [];
    const characters = chars
        .filter((c) => c && typeof c.name === 'string' && c.name)
        .map((c) => ({ name: c.name }));
    let currentName = '';
    const id = ctx.characterId;
    if (id !== undefined && id !== null && chars[id]) {
        currentName = chars[id].name ?? '';
    }
    try {
        state.ws.send(JSON.stringify({ type: 'chars', characters, current_name: currentName }));
    } catch (err) {
        console.error(LOG_PREFIX, 'failed to send character list:', err);
    }
}

async function selectCharacter(name) {
    const ctx = SillyTavern.getContext();
    let ok = false;
    let error = '';
    let resolvedName = name;
    if (typeof ctx.executeSlashCommandsWithOptions === 'function') {
        try {
            const escaped = escapeForStscript(name);
            await ctx.executeSlashCommandsWithOptions(`/go ${escaped}`);
            const after = SillyTavern.getContext();
            const id = after.characterId;
            if (id !== undefined && id !== null && after.characters?.[id]) {
                resolvedName = after.characters[id].name ?? name;
            }
            ok = true;
        } catch (err) {
            error = String(err?.message ?? err);
            console.error(LOG_PREFIX, 'character switch failed:', err);
        }
    } else {
        error = 'no slash command API available';
    }
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        try {
            state.ws.send(JSON.stringify({ type: 'selected', ok, name: resolvedName, error }));
        } catch (err) {
            console.error(LOG_PREFIX, 'failed to send select result:', err);
        }
    }
}

async function startNewChat() {
    const ctx = SillyTavern.getContext();
    let ok = false;
    let error = '';
    if (typeof ctx.executeSlashCommandsWithOptions === 'function') {
        try {
            // delete=false keeps the current chat on disk; this just opens a fresh one.
            await ctx.executeSlashCommandsWithOptions('/newchat delete=false');
            ok = true;
        } catch (err) {
            error = String(err?.message ?? err);
            console.error(LOG_PREFIX, 'new chat failed:', err);
        }
    } else {
        error = 'no slash command API available';
    }
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        try {
            state.ws.send(JSON.stringify({ type: 'new_chat_result', ok, error }));
        } catch (err) {
            console.error(LOG_PREFIX, 'failed to send new chat result:', err);
        }
    }
}

function handleIncomingChatMessage(messageId) {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
    const ctx = SillyTavern.getContext();
    const msg = ctx.chat?.[messageId];
    if (!msg) return;
    if (msg.is_user) return;
    if (msg.is_system) return;
    const text = formatForTerminal(msg.mes ?? '');
    if (!text) return;
    try {
        state.ws.send(JSON.stringify({ type: 'out', text }));
    } catch (err) {
        console.error(LOG_PREFIX, 'failed to send message to bridge:', err);
    }
}

function clearReconnect() {
    if (state.reconnectTimer) {
        clearTimeout(state.reconnectTimer);
        state.reconnectTimer = null;
    }
}

function scheduleReconnect() {
    if (state.manualDisconnect) return;
    clearReconnect();
    setStatus('connecting');
    state.reconnectTimer = setTimeout(connect, state.backoffMs);
    state.backoffMs = Math.min(5000, state.backoffMs === 1000 ? 2000 : 5000);
}

function connect() {
    const settings = getSettings();
    if (!settings.enabled) {
        setStatus('disconnected');
        return;
    }
    disconnect(true);
    state.manualDisconnect = false;
    setStatus('connecting');

    const url = `ws://${settings.host}:${settings.port}`;
    let ws;
    try {
        ws = new WebSocket(url);
    } catch (err) {
        console.error(LOG_PREFIX, 'WebSocket construction failed:', err);
        scheduleReconnect();
        return;
    }
    state.ws = ws;

    ws.addEventListener('open', () => {
        console.log(LOG_PREFIX, `connected to ${url}`);
        setStatus('connected');
        state.backoffMs = 1000;
    });

    ws.addEventListener('message', async (event) => {
        let data;
        try {
            data = JSON.parse(event.data);
        } catch {
            return;
        }
        if (data?.type === 'in' && typeof data.text === 'string') {
            await sendUserTurn(data.text);
        } else if (data?.type === 'history_request') {
            sendHistory();
        } else if (data?.type === 'chars_request') {
            sendCharacters();
        } else if (data?.type === 'select' && typeof data.name === 'string') {
            await selectCharacter(data.name);
        } else if (data?.type === 'new_chat') {
            await startNewChat();
        }
    });

    ws.addEventListener('close', () => {
        if (state.ws === ws) state.ws = null;
        if (!state.manualDisconnect) {
            scheduleReconnect();
        } else {
            setStatus('disconnected');
        }
    });

    ws.addEventListener('error', () => {
        try { ws.close(); } catch { /* ignore */ }
    });
}

function disconnect(silent = false) {
    state.manualDisconnect = !silent;
    clearReconnect();
    if (state.ws) {
        try { state.ws.close(); } catch { /* ignore */ }
        state.ws = null;
    }
    if (!silent) setStatus('disconnected');
}

const SETTINGS_HTML = `
<div class="terminal-bridge-settings">
    <div class="inline-drawer">
        <div class="inline-drawer-toggle inline-drawer-header">
            <b>Terminal Bridge</b>
            <div class="inline-drawer-icon fa-solid fa-circle-chevron-down down"></div>
        </div>
        <div class="inline-drawer-content">
            <label for="tb_enabled" class="checkbox_label">
                <input id="tb_enabled" type="checkbox" />
                <span>Enabled</span>
            </label>
            <label for="tb_host">
                <span>Host</span>
                <input id="tb_host" type="text" class="text_pole" placeholder="127.0.0.1" />
            </label>
            <label for="tb_port">
                <span>Port</span>
                <input id="tb_port" type="number" class="text_pole" min="1" max="65535" placeholder="5005" />
            </label>
            <div class="terminal-bridge-status-row">
                <span>Status:</span>
                <span id="tb_status" class="terminal-bridge-status">disconnected</span>
                <button id="tb_reconnect" class="menu_button">Reconnect</button>
            </div>
        </div>
    </div>
</div>
`;

async function setupUi() {
    const ctx = SillyTavern.getContext();
    const settings = getSettings();

    $('#extensions_settings2').append(SETTINGS_HTML);

    const $enabled = $('#tb_enabled');
    const $host = $('#tb_host');
    const $port = $('#tb_port');
    const $reconnect = $('#tb_reconnect');

    $enabled.prop('checked', settings.enabled);
    $host.val(settings.host);
    $port.val(settings.port);

    $enabled.on('change', () => {
        settings.enabled = $enabled.prop('checked');
        ctx.saveSettingsDebounced();
        if (settings.enabled) {
            state.backoffMs = 1000;
            connect();
        } else {
            disconnect();
        }
    });

    const onEndpointChange = () => {
        const host = ($host.val() || '127.0.0.1').toString().trim();
        const port = parseInt($port.val(), 10);
        settings.host = host;
        settings.port = Number.isFinite(port) ? port : defaultSettings.port;
        ctx.saveSettingsDebounced();
        if (settings.enabled) {
            state.backoffMs = 1000;
            connect();
        }
    };
    $host.on('change', onEndpointChange);
    $port.on('change', onEndpointChange);

    $reconnect.on('click', () => {
        state.backoffMs = 1000;
        connect();
    });

    setStatus('disconnected');
}

(function init() {
    const ctx = SillyTavern.getContext();
    const { eventSource, event_types } = ctx;

    eventSource.on(event_types.APP_READY, async () => {
        try {
            await setupUi();
        } catch (err) {
            console.error(LOG_PREFIX, 'UI setup failed:', err);
        }
        eventSource.on(event_types.MESSAGE_RECEIVED, handleIncomingChatMessage);
        if (getSettings().enabled) {
            connect();
        }
    });
})();
