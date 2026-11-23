"use strict";
// Magertron for VS Code — hand a server you have built to the team that governs it.
//
// ⚠ THIS EXTENSION IS A BUTTON. It does not parse manifests, does not scrub
// credentials, does not decide what can be deployed. It shells out to the
// bundled `mcpctl` binary, which does all of that.
//
// That is deliberate and it is the whole architecture. The submission contract
// — which manifest shapes are valid, which values must never leave the
// developer's machine, how a server is classified — lives in ONE
// implementation. An extension that reimplemented any of it in TypeScript
// would drift from the CLI the moment the MCP spec moved, and the drift would
// show up as a developer being told two different things by two Magertron
// tools.
//
// ⚠ SUBMITTING IS NOT DEPLOYING. This records that your server EXISTS so your
// platform team can review it. It creates no route and deploys nothing. Every
// message below says so, because a developer who believes they have deployed
// something will not chase the approval that actually matters.
Object.defineProperty(exports, "__esModule", { value: true });
exports.activate = activate;
exports.deactivate = deactivate;
const vscode = require("vscode");
const path = require("path");
const fs = require("fs");
const child_process_1 = require("child_process");
const os = require("os");
const https = require("https");
const http = require("http");
const util_1 = require("util");
const execFileAsync = (0, util_1.promisify)(child_process_1.execFile);
// ─── the bundled binary ─────────────────────────────────────────────────────
/**
 * Resolve the mcpctl binary shipped inside this extension.
 *
 * ⚠ BUNDLED, NOT REQUIRED. An extension whose point is "you should not need a
 * terminal" cannot start by asking the developer to install a CLI. The binary
 * ships in bin/<platform>/ and is versioned with the extension, so the
 * submission contract the plugin speaks is always the one its CLI implements.
 */
function bundledMcpctl(ctx) {
    const plat = process.platform; // 'darwin' | 'linux' | 'win32'
    const arch = process.arch; // 'arm64' | 'x64'
    const dir = `${plat}-${arch}`;
    const exe = plat === 'win32' ? 'mcpctl.exe' : 'mcpctl';
    const p = path.join(ctx.extensionPath, 'bin', dir, exe);
    if (!fs.existsSync(p)) {
        return undefined;
    }
    // ⚠ npm and the marketplace do not reliably preserve the executable bit.
    // Set it rather than failing with EACCES, which reads as a broken install.
    if (plat !== 'win32') {
        try {
            fs.chmodSync(p, 0o755);
        }
        catch { /* best effort */ }
    }
    return p;
}
/**
 * Build the environment mcpctl runs with.
 *
 * ⚠ ENV, NOT THE CONFIG FILE. Writing to ~/.config/mcpctl would clobber an
 * interactive `mcpctl login` the developer may already rely on — a plugin has
 * no business overwriting the state of a tool it did not install.
 *
 * ⚠ AND PARTIAL OVERRIDES ARE THE POINT. A developer who has already run
 * `mcpctl login` can leave the token setting empty: mcpctl takes the server URL
 * from here and the credential from their own file. Nothing is duplicated and
 * nothing is stored twice.
 */
function envFor() {
    const cfg = vscode.workspace.getConfiguration('magertron');
    const env = { ...process.env };
    const url = (cfg.get('serverUrl') ?? '').trim();
    const tok = (cfg.get('token') ?? '').trim();
    if (url) {
        env.MCPCTL_SERVER = url;
    }
    // ⚠ Diagnostic 2026-08-17. status reaches the configured server and submit
    // reaches the one in the config FILE, from the same process and the same
    // envFor(). One of those two statements must be false; this says which.
    console.log(`[magertron] envFor: MCPCTL_SERVER=${env.MCPCTL_SERVER ?? "<unset>"}`);
    if (tok) {
        env.MCPCTL_TOKEN = tok;
    }
    if (cfg.get('insecureTls')) {
        env.MCPCTL_INSECURE = '1';
    }
    // ⚠ Identify the tool, so an operator reviewing a submission can see it came
    // from an editor rather than a terminal. Harmless if wrong — it is a
    // convenience for deciding where to ask a question, not an identity.
    // The editor's own name, not a constant. This extension runs unchanged
    // in Cursor and other VS Code forks, and hardcoding 'vscode' made a
    // Cursor submission claim it came from somewhere it did not.
    env.MCPCTL_CLIENT = vscode.env.appName
        ? vscode.env.appName.toLowerCase().replace(/\s+/g, '-')
        : 'vscode';
    return env;
}
// ─── output ─────────────────────────────────────────────────────────────────
let channel;
function log(line) {
    channel.appendLine(line);
}
// ─── commands ───────────────────────────────────────────────────────────────
async function pickManifest(uri) {
    if (uri) {
        return uri;
    }
    // The active editor is the likeliest intent — a developer with their
    // manifest open and the command palette down means that file.
    const active = vscode.window.activeTextEditor?.document;
    if (active && /\.(mcpb|json)$/i.test(active.fileName)) {
        // ⚠ SAVE FIRST. mcpctl reads from DISK. Submitting the saved version of a
        // file the developer has since edited would send something they never
        // wrote and cannot see — a silent wrong answer, which is worse than a
        // refusal.
        if (active.isDirty) {
            const save = await vscode.window.showWarningMessage(`${path.basename(active.fileName)} has unsaved changes. ` +
                `Magertron submits what is on disk.`, 'Save and submit', 'Cancel');
            if (save !== 'Save and submit') {
                return undefined;
            }
            await active.save();
        }
        return active.uri;
    }
    const picked = await vscode.window.showOpenDialog({
        canSelectMany: false,
        openLabel: 'Submit',
        filters: { 'MCP manifests': ['mcpb', 'json'] },
    });
    return picked?.[0];
}
async function submit(ctx, uri) {
    const bin = bundledMcpctl(ctx);
    if (!bin) {
        vscode.window.showErrorMessage(`Magertron: no mcpctl binary bundled for ${process.platform}-${process.arch}. ` +
            `Please report this — the extension ships binaries per platform and yours is missing.`);
        return;
    }
    const cfg = vscode.workspace.getConfiguration('magertron');
    if (!(cfg.get('serverUrl') ?? '').trim()) {
        const act = await vscode.window.showErrorMessage('Magertron: set your platform URL before submitting.', 'Open settings');
        if (act === 'Open settings') {
            vscode.commands.executeCommand('workbench.action.openSettings', 'magertron.serverUrl');
        }
        return;
    }
    const file = await pickManifest(uri);
    if (!file) {
        return;
    }
    log(`\n── submit ${file.fsPath}`);
    await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: 'Magertron: submitting…' }, async () => {
        try {
            const { stdout } = await execFileAsync(bin, ['submit', file.fsPath], {
                env: envFor(),
                timeout: 60_000,
            });
            log(stdout.trim());
            // ⚠ Say what did NOT happen. A developer who believes they have
            // deployed something will not chase the approval that actually
            // matters, and their server will sit ungoverned while they assume
            // otherwise.
            const act = await vscode.window.showInformationMessage('Submitted for review. This does not deploy it — your platform team decides.', 'Show details');
            if (act === 'Show details') {
                channel.show();
            }
        }
        catch (e) {
            // ⚠ mcpctl's stderr is written to be READ BY A DEVELOPER — it names the
            // manifest problem, or says the token lacks a scope. Surfacing "command
            // failed with exit code 1" instead would throw away the only useful
            // part.
            const msg = (e?.stderr || e?.message || String(e)).trim();
            log(msg);
            const act = await vscode.window.showErrorMessage(`Magertron: ${msg.split('\n')[0]}`, 'Show details');
            if (act === 'Show details') {
                channel.show();
            }
        }
    });
}
async function status(ctx) {
    const bin = bundledMcpctl(ctx);
    if (!bin) {
        vscode.window.showErrorMessage(`Magertron: no mcpctl bundled for ${process.platform}-${process.arch}.`);
        return;
    }
    try {
        const { stdout } = await execFileAsync(bin, ['status'], { env: envFor(), timeout: 20_000 });
        log(`\n── status\n${stdout.trim()}`);
        channel.show();
    }
    catch (e) {
        log((e?.stderr || e?.message || String(e)).trim());
        channel.show();
    }
}
// ─── push from Czartup ─────────────────────────────────────────────────────
/**
 * Czartup's "Push to IDE" opens cursor://magertron.magertron-submit/build?czarter=…&kit=…
 * (vscode://… for VS Code). The kit URL carries a one-time, 15-minute ticket: it is the
 * only credential, so nothing has to be configured here and no token is stored.
 *
 * ⚠ THE KIT IS DATA, NOT CODE. It is written to disk and opened; nothing in it is run.
 * Every path is checked to stay inside the folder the developer chose.
 */
const OPEN_KEY = 'magertron.openOnStart';
const PROMPT = (open) => `Build what ${open} says, following AGENTS.md. Stop wherever it says the Magertron admin takes over.`;
function getJson(url) {
    const insecure = !!vscode.workspace.getConfiguration('magertron').get('insecureTls');
    return new Promise((resolve, reject) => {
        const mod = url.startsWith('https:') ? https : http;
        const req = mod.get(url, { rejectUnauthorized: !insecure, headers: { Accept: 'application/json' } }, res => {
            const chunks = [];
            res.on('data', (c) => chunks.push(c));
            res.on('end', () => {
                const body = Buffer.concat(chunks).toString('utf8');
                let j;
                try {
                    j = JSON.parse(body);
                }
                catch {
                    j = undefined;
                }
                if ((res.statusCode ?? 0) >= 400 || !j) {
                    reject(new Error(j?.error || `the build kit could not be downloaded (HTTP ${res.statusCode})`));
                }
                else {
                    resolve(j);
                }
            });
        });
        req.on('error', reject);
        req.setTimeout(60_000, () => req.destroy(new Error('timed out downloading the build kit')));
    });
}
async function pullBuild(ctx, uri) {
    const q = new URLSearchParams(uri.query);
    const czarter = (q.get('czarter') || '').replace(/[^A-Za-z0-9._-]/g, '');
    const kit = q.get('kit') || '';
    if (uri.path !== '/build' || !czarter || !/^https?:\/\//.test(kit)) {
        vscode.window.showErrorMessage('Magertron: this link is not a Czartup build link.');
        return;
    }
    const here = vscode.workspace.workspaceFolders?.[0]?.uri;
    const picked = await vscode.window.showOpenDialog({
        canSelectFiles: false, canSelectFolders: true, canSelectMany: false,
        openLabel: `Put ${czarter} here`,
        title: `Czartup: where should the ${czarter} build kit go?`,
        defaultUri: here ? vscode.Uri.file(path.dirname(here.fsPath)) : vscode.Uri.file(os.homedir()),
    });
    if (!picked?.[0]) {
        return;
    }
    let target = path.join(picked[0].fsPath, czarter);
    if (fs.existsSync(target) && fs.readdirSync(target).length) {
        const act = await vscode.window.showWarningMessage(`${target} already has files. The build kit's files replace theirs; your other files stay.`, { modal: true }, 'Update it');
        if (act !== 'Update it') {
            return;
        }
    }
    let j;
    try {
        j = await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: `Magertron: getting the ${czarter} build kit…` }, () => getJson(kit));
    }
    catch (e) {
        vscode.window.showErrorMessage(`Magertron: ${e?.message || e}`);
        return;
    }
    const root = path.resolve(target);
    let n = 0;
    for (const f of (j.files || [])) {
        const out = path.resolve(root, f.path);
        if (out !== root && !out.startsWith(root + path.sep)) {
            log(`skipped ${f.path}: outside the folder`);
            continue;
        }
        fs.mkdirSync(path.dirname(out), { recursive: true });
        fs.writeFileSync(out, Buffer.from(f.base64, 'base64'));
        n++;
    }
    log(`\n── build kit ${czarter}: ${n} files in ${root}`);
    const open = String(j.open || 'AGENTS.md');
    await ctx.globalState.update(OPEN_KEY, { folder: root, open, czarter });
    const same = here && path.resolve(here.fsPath) === root;
    if (same) {
        await openKit(ctx);
    }
    else {
        // A new window starts a fresh copy of this extension; it opens the kit on start (activate).
        await vscode.commands.executeCommand('vscode.openFolder', vscode.Uri.file(root), { forceNewWindow: !!vscode.workspace.workspaceFolders?.length });
    }
}
async function openKit(ctx) {
    const s = ctx.globalState.get(OPEN_KEY);
    const ws = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    if (!s || !ws || path.resolve(ws) !== s.folder) {
        return;
    }
    await ctx.globalState.update(OPEN_KEY, undefined);
    const file = path.join(s.folder, s.open);
    if (fs.existsSync(file)) {
        await vscode.window.showTextDocument(vscode.Uri.file(file), { preview: false });
    }
    const prompt = PROMPT(s.open);
    await vscode.env.clipboard.writeText(prompt);
    // Put the build prompt in the agent's chat, ready to send. Cursor: its documented prompt deeplink
    // (pre-filled, the developer confirms - it never runs by itself). VS Code: the chat view's query.
    let filled = false;
    if (/cursor/i.test(vscode.env.appName || '')) {
        // ⚠ Cursor drops a prompt after a "&" and refuses ".env" in it - PROMPT has neither.
        const link = 'cursor://anysphere.cursor-deeplink/prompt?text=' + encodeURIComponent(prompt);
        filled = await vscode.env.openExternal(vscode.Uri.parse(link));
    }
    else {
        try {
            await vscode.commands.executeCommand('workbench.action.chat.open', { query: prompt, isPartialQuery: true });
            filled = true;
        }
        catch { /* no chat view in this editor */ }
    }
    vscode.window.showInformationMessage(filled
        ? `Czartup build kit for ${s.czarter} is here. The build prompt is in the agent chat - send it to start. ` +
            `When the server is done, submit it with Magertron: Submit.`
        : `Czartup build kit for ${s.czarter} is here. Paste the build prompt (on your clipboard) into the agent chat. ` +
            `When the server is done, submit it with Magertron: Submit.`);
}
function activate(ctx) {
    channel = vscode.window.createOutputChannel('Magertron');
    ctx.subscriptions.push(channel);
    ctx.subscriptions.push(vscode.commands.registerCommand('magertron.submit', (uri) => submit(ctx, uri)), vscode.commands.registerCommand('magertron.status', () => status(ctx)), vscode.window.registerUriHandler({ handleUri: (uri) => pullBuild(ctx, uri) }));
    void openKit(ctx);
}
function deactivate() { }
//# sourceMappingURL=extension.js.map