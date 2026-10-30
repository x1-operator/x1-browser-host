/**
 * X1 browser host — a self-contained Chromium runtime.
 * Own it end to end: sessions, live frames, saved contexts, usage. No third party.
 *
 * Auth: Authorization: Bearer <HOST_TOKEN>
 * Live frame also accepts ?exp=<unix>&sig=<hmac(sha256, HOST_TOKEN, "<sessionId>.<exp>")>
 * so a browser can show the frame without ever seeing the token.
 */
import express from 'express';
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';

const PORT = Number(process.env.PORT || 8080);
const TOKEN = process.env.HOST_TOKEN || '';
const DATA_DIR = process.env.DATA_DIR || '/data';
const MAX_SESSIONS = Number(process.env.MAX_SESSIONS || 5);
const HARD_CAP = Number(process.env.HARD_CAP_SECONDS || 1800);
const DEFAULT_TIMEOUT = Number(process.env.DEFAULT_TIMEOUT_SECONDS || 600);
const IDLE_KILL_MS = 1000 * 60 * 30;
const FRAME_TTL_MS = 400;

if (!TOKEN) {
  console.error('HOST_TOKEN is required. Set it to a long random string.');
  process.exit(1);
}

const CONTEXT_DIR = path.join(DATA_DIR, 'contexts');
const CONFIG_FILE = path.join(DATA_DIR, 'config.json');
const DEFAULT_CONFIG = { name: 'x1-browser-host', default_timeout: DEFAULT_TIMEOUT, allowed_domains: [] };

const sessions = new Map();
const counters = { total_seconds: 0, sessions_opened: 0, actions: 0 };

let browserPromise = null;

async function getBrowser() {
  if (!browserPromise) {
    browserPromise = chromium.launch({
      headless: true,
      args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
    });
  }
  return browserPromise;
}

async function readConfig() {
  try {
    const raw = await fs.readFile(CONFIG_FILE, 'utf8');
    return { ...DEFAULT_CONFIG, ...JSON.parse(raw) };
  } catch {
    return { ...DEFAULT_CONFIG };
  }
}

async function writeConfig(next) {
  await fs.mkdir(DATA_DIR, { recursive: true });
  await fs.writeFile(CONFIG_FILE, JSON.stringify(next, null, 2));
  return next;
}

function contextPath(id) {
  return path.join(CONTEXT_DIR, `${id}.json`);
}

function nowIso() {
  return new Date().toISOString();
}

function limitOf(value, fallback) {
  const raw = Number(value);
  if (!Number.isFinite(raw)) return fallback;
  return Math.min(200, Math.max(1, Math.round(raw)));
}

function clip(value, size = 4000) {
  if (value === null || value === undefined) return null;
  const text = typeof value === 'string' ? value : JSON.stringify(value);
  return text.length <= size ? value : { truncated: true, preview: text.slice(0, size) };
}

function sign(id, exp) {
  return crypto.createHmac('sha256', TOKEN).update(`${id}.${exp}`).digest('hex');
}

function viewToken(id, seconds = 900) {
  const exp = Math.floor(Date.now() / 1000) + seconds;
  return { exp, sig: sign(id, exp) };
}

function allowedByDomain(url, allowed) {
  if (!Array.isArray(allowed) || !allowed.length) return true;
  try {
    const host = new URL(url).hostname;
    return allowed.some((entry) => host === entry || host.endsWith(`.${entry}`));
  } catch {
    return false;
  }
}

function sessionView(session) {
  return {
    id: session.id,
    status: session.status,
    created_at: session.created_at,
    started_at: session.started_at,
    ended_at: session.ended_at,
    expires_at: session.expires_at,
    keep_alive: session.keep_alive,
    timeout_seconds: session.timeout_seconds,
    context_id: session.context_id,
    viewport: session.viewport,
    url: session.url || null,
    title: session.title || null,
    action_count: session.action_count,
    last_action: session.last_action,
    last_status: session.last_status,
    actions: session.logs.slice(-40),
  };
}

async function persistContext(session) {
  if (!session.context_id || !session.context) return;
  try {
    const state = await session.context.storageState();
    await fs.mkdir(CONTEXT_DIR, { recursive: true });
    await fs.writeFile(contextPath(session.context_id), JSON.stringify(state));
  } catch (error) {
    session.logs.push({ at: nowIso(), action: 'context.persist', ok: false, error: error.message });
  }
}

async function closeSession(session, status = 'COMPLETED') {
  if (!session || session.status !== 'RUNNING') return session;
  const seconds = Math.max(0, Math.round((Date.now() - session.started_ms) / 1000));
  counters.total_seconds += seconds;
  session.ended_at = nowIso();
  session.status = status;
  session.duration_seconds = seconds;
  await persistContext(session);
  try {
    await session.context?.close();
  } catch {
    /* already gone */
  }
  sessions.set(session.id, session);
  return session;
}

async function reap() {
  const now = Date.now();
  for (const session of sessions.values()) {
    if (session.status !== 'RUNNING') continue;
    const expired = now > session.expires_ms;
    const idle = now - session.last_used_ms > IDLE_KILL_MS;
    if (expired || (idle && !session.keep_alive)) {
      await closeSession(session, expired ? 'TIMED_OUT' : 'IDLE');
    }
  }
}

setInterval(() => {
  reap().catch(() => {});
}, 15000);

async function openSession(body) {
  const running = [...sessions.values()].filter((item) => item.status === 'RUNNING');
  if (running.length >= MAX_SESSIONS) {
    const error = new Error(`The host allows ${MAX_SESSIONS} concurrent sessions and ${running.length} are running.`);
    error.status = 429;
    throw error;
  }

  const config = await readConfig();
  const timeout = Math.min(HARD_CAP, Math.max(60, Number(body.timeout_seconds) || config.default_timeout || DEFAULT_TIMEOUT));
  const width = Number(body.viewport?.width) || 1280;
  const height = Number(body.viewport?.height) || 800;
  const browser = await getBrowser();

  let storageState;
  if (body.context_id) {
    try {
      storageState = JSON.parse(await fs.readFile(contextPath(String(body.context_id)), 'utf8'));
    } catch {
      storageState = undefined;
    }
  }

  const context = await browser.newContext({
    viewport: { width, height },
    storageState,
    userAgent:
      'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
  });
  const page = await context.newPage();
  const id = crypto.randomUUID();
  const session = {
    id,
    status: 'RUNNING',
    created_at: nowIso(),
    started_at: nowIso(),
    started_ms: Date.now(),
    last_used_ms: Date.now(),
    expires_ms: Date.now() + timeout * 1000,
    expires_at: new Date(Date.now() + timeout * 1000).toISOString(),
    keep_alive: Boolean(body.keep_alive),
    timeout_seconds: timeout,
    context_id: body.context_id ? String(body.context_id) : null,
    context,
    page,
    viewport: { width, height },
    allowed_domains: Array.isArray(body.allowed_domains) ? body.allowed_domains : config.allowed_domains,
    action_count: 0,
    last_action: null,
    last_status: null,
    logs: [{ at: nowIso(), action: 'session.create', ok: true }],
    url: null,
    title: null,
  };

  page.on('framenavigated', (frame) => {
    if (frame === page.mainFrame()) session.url = frame.url();
  });
  page.on('close', () => {
    if (session.status === 'RUNNING') closeSession(session, 'COMPLETED').catch(() => {});
  });

  sessions.set(id, session);
  counters.sessions_opened += 1;
  return session;
}

async function snapshot(page) {
  const data = await page.evaluate(() => {
    const text = (document.body?.innerText || '').replace(/\n{3,}/g, '\n\n').slice(0, 4500);
    const pick = (node) => (node?.innerText || node?.value || node?.getAttribute?.('aria-label') || '').trim().slice(0, 80);
    const fields = [...document.querySelectorAll('input, textarea, select')]
      .slice(0, 40)
      .map((node) => ({
        selector: node.id ? `#${node.id}` : node.getAttribute('name') ? `[name="${node.getAttribute('name')}"]` : node.tagName.toLowerCase(),
        type: node.tagName.toLowerCase() === 'input' ? node.type : node.tagName.toLowerCase(),
        name: node.getAttribute('name') || node.id || '',
        placeholder: node.getAttribute('placeholder') || '',
        value: node.type === 'password' ? '' : String(node.value || '').slice(0, 40),
      }));
    const buttons = [...document.querySelectorAll('button, [role="button"], input[type="submit"], a')]
      .filter((node) => pick(node))
      .slice(0, 60)
      .map((node) => ({ text: pick(node), selector: node.id ? `#${node.id}` : null, href: node.getAttribute('href') || null }));
    return { text, fields, buttons };
  });
  return { url: page.url(), title: await page.title(), ...data };
}

async function runAction(page, session, action, params) {
  switch (action) {
    case 'goto': {
      const url = String(params.url || '');
      if (!/^https?:\/\//i.test(url)) throw new Error('url must start with http:// or https://');
      if (!allowedByDomain(url, session.allowed_domains)) {
        throw new Error(`${url} is outside this host's allowed domains.`);
      }
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
      return { url: page.url(), title: await page.title() };
    }
    case 'click': {
      const selector = String(params.selector || '');
      if (!selector) throw new Error('selector is required');
      await page.locator(selector).first().click({ timeout: 20000 });
      return { clicked: selector, url: page.url() };
    }
    case 'click_text': {
      const text = String(params.text || '');
      if (!text) throw new Error('text is required');
      await page.getByText(text, { exact: false }).first().click({ timeout: 20000 });
      return { clicked: text, url: page.url() };
    }
    case 'fill': {
      const selector = String(params.selector || '');
      if (!selector) throw new Error('selector is required');
      await page.locator(selector).first().fill(String(params.value ?? params.text ?? ''), { timeout: 20000 });
      return { filled: selector };
    }
    case 'type': {
      const selector = String(params.selector || '');
      if (!selector) throw new Error('selector is required');
      const target = page.locator(selector).first();
      await target.fill('');
      await target.type(String(params.text ?? params.value ?? ''), { delay: 20 });
      if (params.submit) await target.press('Enter');
      return { typed: selector, submitted: Boolean(params.submit), url: page.url() };
    }
    case 'press': {
      await page.keyboard.press(String(params.key || 'Enter'));
      return { pressed: String(params.key || 'Enter') };
    }
    case 'select': {
      const selector = String(params.selector || '');
      if (!selector) throw new Error('selector is required');
      await page.locator(selector).first().selectOption(String(params.value ?? ''));
      return { selected: selector };
    }
    case 'scroll': {
      const delta = Number(params.delta) || 800;
      await page.mouse.wheel(0, delta);
      await page.waitForTimeout(300);
      return { scrolled: delta };
    }
    case 'wait_for': {
      const selector = String(params.selector || '');
      if (!selector) throw new Error('selector is required');
      await page.locator(selector).first().waitFor({ timeout: Number(params.timeout_ms) || 20000 });
      return { appeared: selector };
    }
    case 'wait': {
      const ms = Math.min(30000, Math.max(0, Number(params.ms) || 1000));
      await page.waitForTimeout(ms);
      return { waited_ms: ms };
    }
    case 'extract_text': {
      const selector = params.selector ? String(params.selector) : 'body';
      const text = await page.locator(selector).first().innerText({ timeout: 20000 });
      return { text: text.replace(/\n{3,}/g, '\n\n').slice(0, 8000) };
    }
    case 'extract_links': {
      const limit = Math.min(80, Number(params.limit) || 30);
      const links = await page.evaluate(
        (max) =>
          [...document.querySelectorAll('a[href]')]
            .slice(0, max)
            .map((node) => ({ text: (node.innerText || '').trim().slice(0, 80), href: node.href })),
        limit
      );
      return { links };
    }
    case 'back': {
      await page.goBack({ timeout: 20000 });
      return { url: page.url() };
    }
    case 'forward': {
      await page.goForward({ timeout: 20000 });
      return { url: page.url() };
    }
    case 'reload': {
      await page.reload({ waitUntil: 'domcontentloaded', timeout: 30000 });
      return { url: page.url() };
    }
    case 'url': {
      return { url: page.url(), title: await page.title() };
    }
    case 'snapshot': {
      return snapshot(page);
    }
    case 'screenshot': {
      const buffer = await page.screenshot({ type: 'jpeg', quality: 72, fullPage: Boolean(params.full_page) });
      return { image: `data:image/jpeg;base64,${buffer.toString('base64')}`, bytes: buffer.length };
    }
    default:
      throw new Error(`Unknown page action: ${action}`);
  }
}

const app = express();
app.use(express.json({ limit: '2mb' }));

function bearer(req) {
  const header = req.headers.authorization || '';
  const token = header.startsWith('Bearer ') ? header.slice(7) : '';
  return token && token === TOKEN;
}

const auth = (req, res, next) => {
  if (bearer(req)) return next();
  return res.status(401).json({ error: 'Unauthorized' });
};

app.get('/health', (req, res) => {
  res.json({ ok: true, engine: 'x1-browser-host', sessions: sessions.size, max_sessions: MAX_SESSIONS, uptime_s: Math.round(process.uptime()) });
});

app.get('/sessions', auth, (req, res) => {
  const limit = limitOf(req.query.limit, 20);
  const status = req.query.status ? String(req.query.status).toUpperCase() : null;
  const items = [...sessions.values()]
    .filter((item) => (status ? item.status === status : true))
    .sort((a, b) => (a.created_at < b.created_at ? 1 : -1))
    .slice(0, limit)
    .map(sessionView);
  res.json(items);
});

app.post('/sessions', auth, async (req, res) => {
  try {
    const session = await openSession(req.body || {});
    res.status(201).json(sessionView(session));
  } catch (error) {
    res.status(error.status || 500).json({ error: error.message });
  }
});

function withSession(handler) {
  return async (req, res) => {
    const session = sessions.get(String(req.params.id));
    if (!session) return res.status(404).json({ error: 'No session with that id' });
    try {
      return await handler(session, req, res);
    } catch (error) {
      if (/Target (page|closed)|has been closed/i.test(error.message)) {
        await closeSession(session, 'FAILED');
        return res.status(410).json({ error: 'The session browser was closed. Open a new session.' });
      }
      return res.status(400).json({ error: error.message });
    }
  };
}

app.get('/sessions/:id', auth, withSession(async (session, req, res) => res.json(sessionView(session))));

app.post('/sessions/:id/release', auth, withSession(async (session, req, res) => {
  const closed = await closeSession(session, 'COMPLETED');
  res.json(sessionView(closed));
}));

app.delete('/sessions/:id', auth, withSession(async (session, req, res) => {
  const closed = await closeSession(session, 'COMPLETED');
  res.json(sessionView(closed));
}));

app.get('/sessions/:id/logs', auth, withSession(async (session, req, res) => {
  res.json(session.logs.slice(-limitOf(req.query.limit, 50)));
}));

app.get('/sessions/:id/downloads', auth, withSession(async (session, req, res) => {
  res.json(session.downloads || []);
}));

app.get('/sessions/:id/view', auth, withSession(async (session, req, res) => {
  const { exp, sig } = viewToken(session.id, 900);
  res.json({ session_id: session.id, status: session.status, frame_url: `/sessions/${session.id}/frame.jpg?exp=${exp}&sig=${sig}`, expires_at: new Date(exp * 1000).toISOString() });
}));

app.post('/sessions/:id/actions', auth, withSession(async (session, req, res) => {
  const body = req.body || {};
  const action = String(body.action || '');
  if (!action) return res.status(400).json({ error: 'action is required' });
  if (session.status !== 'RUNNING') return res.status(410).json({ error: `Session is ${session.status}. Open a new session.` });
  const started = Date.now();
  try {
    const result = await runAction(session.page, session, action, body.params || {});
    session.action_count += 1;
    counters.actions += 1;
    session.last_action = action;
    session.last_status = 200;
    session.last_used_ms = Date.now();
    session.title = await session.page.title().catch(() => session.title);
    session.logs.push({ at: nowIso(), action, ok: true, ms: Date.now() - started });
    res.json({ ok: true, action, result: clip(result, 9000), ms: Date.now() - started, url: session.url });
  } catch (error) {
    session.last_action = action;
    session.last_status = 400;
    session.logs.push({ at: nowIso(), action, ok: false, ms: Date.now() - started, error: error.message });
    res.status(400).json({ ok: false, action, error: error.message, ms: Date.now() - started });
  }
}));

// Live frame: bearer token, or a short-lived signature handed out by the app's own backend.
app.get('/sessions/:id/frame.jpg', async (req, res) => {
  const session = sessions.get(String(req.params.id));
  if (!session) return res.status(404).send('No session');
  const exp = Number(req.query.exp || 0);
  const sig = String(req.query.sig || '');
  const signatureOk = exp > Math.floor(Date.now() / 1000) && sig === sign(session.id, exp);
  if (!bearer(req) && !signatureOk) return res.status(401).send('Unauthorized');
  if (session.status !== 'RUNNING') return res.status(410).send('Session closed');
  const now = Date.now();
  try {
    if (!session.frame_cache || now - session.frame_cache.at > FRAME_TTL_MS) {
      const buffer = await session.page.screenshot({ type: 'jpeg', quality: 55 });
      session.frame_cache = { at: now, buffer };
    }
    res.set('Content-Type', 'image/jpeg');
    res.set('Cache-Control', 'no-store');
    res.send(session.frame_cache.buffer);
  } catch (error) {
    res.status(410).send(error.message);
  }
});

app.post('/contexts', auth, async (req, res) => {
  const id = crypto.randomUUID();
  await fs.mkdir(CONTEXT_DIR, { recursive: true });
  await fs.writeFile(contextPath(id), JSON.stringify({ cookies: [], origins: [] }));
  res.status(201).json({ id, created_at: nowIso() });
});

app.get('/contexts/:id', auth, async (req, res) => {
  try {
    const raw = await fs.readFile(contextPath(String(req.params.id)), 'utf8');
    const state = JSON.parse(raw);
    res.json({ id: String(req.params.id), cookies: (state.cookies || []).length, origins: (state.origins || []).length });
  } catch {
    res.status(404).json({ error: 'No context with that id' });
  }
});

app.delete('/contexts/:id', auth, async (req, res) => {
  try {
    await fs.unlink(contextPath(String(req.params.id)));
    res.status(204).end();
  } catch {
    res.status(404).json({ error: 'No context with that id' });
  }
});

app.get('/project', auth, async (req, res) => {
  const config = await readConfig();
  res.json({ ...config, max_sessions: MAX_SESSIONS, hard_cap_seconds: HARD_CAP });
});

app.post('/project', auth, async (req, res) => {
  const current = await readConfig();
  const body = req.body || {};
  const next = {
    name: typeof body.name === 'string' && body.name.trim() ? body.name.trim() : current.name,
    default_timeout: body.default_timeout ? Math.min(HARD_CAP, Math.max(60, Number(body.default_timeout))) : current.default_timeout,
    allowed_domains: Array.isArray(body.allowed_domains) ? body.allowed_domains.slice(0, 50).map(String) : current.allowed_domains,
  };
  res.json(await writeConfig(next));
});

app.get('/usage', auth, (req, res) => {
  const running = [...sessions.values()].filter((item) => item.status === 'RUNNING');
  const liveSeconds = running.reduce((total, item) => total + Math.round((Date.now() - item.started_ms) / 1000), 0);
  const seconds = counters.total_seconds + liveSeconds;
  res.json({
    browserMinutes: Math.round((seconds / 60) * 10) / 10,
    browserSeconds: seconds,
    sessions_opened: counters.sessions_opened,
    sessions_running: running.length,
    actions: counters.actions,
    max_sessions: MAX_SESSIONS,
  });
});

app.use((req, res) => res.status(404).json({ error: 'Not found' }));

app.listen(PORT, () => {
  console.log(`x1-browser-host listening on ${PORT}`);
});