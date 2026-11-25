from __future__ import annotations

from html.parser import HTMLParser
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest

from familiekilder import setup_page
from familiekilder.setup_page import (
    PAGES_URL,
    SETUP_PORT,
    STATUS_APP,
    STATUS_PATH,
    STATUS_PROTOCOL,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PAGES_DIR = PROJECT_ROOT / "pages"
ROOT_INDEX = PAGES_DIR / "index.html"
SETUP_DIR = PAGES_DIR / "oppsett"
SETUP_HTML = SETUP_DIR / "index.html"
SETUP_CSS = SETUP_DIR / "oppsett.css"
SETUP_JS = SETUP_DIR / "oppsett.js"

LOCAL_ORIGIN = f"http://127.0.0.1:{SETUP_PORT}"
LOCAL_URL = f"{LOCAL_ORIGIN}/"
ALLOWED_TEXT_EXTENSIONS = {".html", ".css", ".js"}


class _HtmlInspector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.inline_styles: list[str] = []
        self.inline_scripts: list[str] = []
        self.event_handlers: list[tuple[str, str]] = []
        self.links_by_id: dict[str, dict[str, str | None]] = {}
        self.text_by_id: dict[str, str] = {}
        self.noscript_chunks: list[str] = []
        self._stack: list[tuple[str, str | None]] = []
        self._in_script = False
        self._script_has_src = False
        self._in_noscript = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        self.elements.append((tag, attr_map))
        element_id = attr_map.get("id")
        self._stack.append((tag, element_id))

        if tag == "style":
            self.inline_styles.append("<style>")
        if "style" in attr_map:
            self.inline_styles.append(f"<{tag} style=...>")
        for name in attr_map:
            if name.lower().startswith("on"):
                self.event_handlers.append((tag, name))

        if tag == "script":
            self._in_script = True
            self._script_has_src = bool(attr_map.get("src"))
            if not self._script_has_src:
                self.inline_scripts.append("<script> without src")
        if tag == "noscript":
            self._in_noscript = True
        if tag == "a" and element_id:
            self.links_by_id[element_id] = attr_map

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False
            self._script_has_src = False
        if tag == "noscript":
            self._in_noscript = False
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self._in_script and data.strip():
            self.inline_scripts.append(data.strip())
        if self._in_noscript:
            self.noscript_chunks.append(data)
        for _, element_id in self._stack:
            if element_id:
                self.text_by_id[element_id] = self.text_by_id.get(element_id, "") + data


def _parse_html(path: Path) -> _HtmlInspector:
    parser = _HtmlInspector()
    parser.feed(path.read_text(encoding="utf-8"))
    parser.close()
    return parser


def _extract_csp(inspector: _HtmlInspector) -> dict[str, str]:
    csp_values = [
        attrs.get("content") or ""
        for tag, attrs in inspector.elements
        if tag == "meta" and (attrs.get("http-equiv") or "").lower() == "content-security-policy"
    ]
    if len(csp_values) != 1:
        raise AssertionError(f"Expected one CSP meta tag, found {len(csp_values)}")
    directives: dict[str, str] = {}
    for raw_part in csp_values[0].split(";"):
        part = raw_part.strip()
        if not part:
            continue
        name, _, value = part.partition(" ")
        directives[name] = value.strip()
    return directives


class SetupPageStaticTests(unittest.TestCase):
    def test_pages_contains_only_utf8_text_files(self) -> None:
        files = sorted(path for path in PAGES_DIR.rglob("*") if path.is_file())
        self.assertEqual(
            [path.relative_to(PAGES_DIR).as_posix() for path in files],
            [
                "index.html",
                "oppsett/index.html",
                "oppsett/oppsett.css",
                "oppsett/oppsett.js",
            ],
        )
        for path in files:
            self.assertIn(path.suffix, ALLOWED_TEXT_EXTENSIONS, path)
            raw = path.read_bytes()
            self.assertNotIn(b"\x00", raw, path)
            raw.decode("utf-8")

    def test_root_index_redirects_and_links_to_oppsett(self) -> None:
        inspector = _parse_html(ROOT_INDEX)
        refreshes = [
            attrs.get("content") or ""
            for tag, attrs in inspector.elements
            if tag == "meta" and (attrs.get("http-equiv") or "").lower() == "refresh"
        ]
        self.assertEqual(len(refreshes), 1)
        self.assertIn("url=oppsett/", refreshes[0].lower())
        hrefs = [attrs.get("href") for tag, attrs in inspector.elements if tag == "a"]
        self.assertIn("oppsett/", hrefs)

    def test_constants_match_across_python_html_js_and_csp(self) -> None:
        inspector = _parse_html(SETUP_HTML)
        csp = _extract_csp(inspector)
        self.assertEqual(csp.get("default-src"), "'none'")
        self.assertEqual(csp.get("script-src"), "'self'")
        self.assertEqual(csp.get("style-src"), "'self'")
        self.assertEqual(csp.get("base-uri"), "'none'")
        self.assertEqual(csp.get("form-action"), "'none'")
        self.assertEqual(csp.get("connect-src"), LOCAL_ORIGIN)

        for link_id, label in (
            ("continue-link", "Fortsett oppsettet"),
            ("direct-link", "Åpne veiviseren direkte"),
        ):
            self.assertIn(link_id, inspector.links_by_id)
            attrs = inspector.links_by_id[link_id]
            self.assertEqual(attrs.get("href"), LOCAL_URL)
            self.assertEqual(attrs.get("target"), "_blank")
            self.assertEqual(attrs.get("rel"), "noopener noreferrer")
            self.assertEqual(inspector.text_by_id[link_id].strip(), label)

        js_text = SETUP_JS.read_text(encoding="utf-8")
        self.assertRegex(js_text, rf"\bconst SETUP_PORT = {SETUP_PORT};")
        self.assertEqual(len(re.findall(rf"\b{SETUP_PORT}\b", js_text)), 1)
        self.assertIn(f"const STATUS_PATH = '{STATUS_PATH}';", js_text)
        self.assertIn(f"const STATUS_APP = '{STATUS_APP}';", js_text)
        protocol_match = re.search(r"const SUPPORTED_PROTOCOLS = \[([^\]]+)\];", js_text)
        self.assertIsNotNone(protocol_match)
        protocols = [
            int(item.strip()) for item in protocol_match.group(1).split(",") if item.strip()
        ]
        self.assertIn(STATUS_PROTOCOL, protocols)

        wizard_js = (Path(setup_page.__file__).parent / "data/setup/setup.js").read_text(
            encoding="utf-8"
        )
        self.assertRegex(wizard_js, rf"(?m)^const SETUP_PORT = {SETUP_PORT};$")
        self.assertIn(f"const PAGES_URL = '{PAGES_URL}';", wizard_js)

    def test_no_external_resources_and_no_inline_script_or_style(self) -> None:
        for html_path in (ROOT_INDEX, SETUP_HTML):
            inspector = _parse_html(html_path)
            self.assertEqual(inspector.inline_styles, [], html_path)
            self.assertEqual(inspector.inline_scripts, [], html_path)
            self.assertEqual(inspector.event_handlers, [], html_path)
            for tag, attrs in inspector.elements:
                for attr_name in ("src", "href", "action", "poster", "data"):
                    value = attrs.get(attr_name)
                    if not value:
                        continue
                    lowered = value.strip().lower()
                    if tag == "a" and attr_name == "href" and lowered == LOCAL_URL:
                        continue
                    self.assertFalse(
                        lowered.startswith(("http://", "https://", "//", "data:", "javascript:")),
                        f"{html_path} has external or disallowed {tag}[{attr_name}]={value!r}",
                    )

        css_text = SETUP_CSS.read_text(encoding="utf-8")
        self.assertNotIn("url(", css_text.lower())
        self.assertNotIn("@import", css_text.lower())

        js_text = SETUP_JS.read_text(encoding="utf-8")
        for forbidden in (
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "document.write",
            "localStorage",
            "sessionStorage",
            "document.cookie",
            "indexedDB",
            "window.open",
            "https://",
        ):
            self.assertNotIn(forbidden, js_text)

    def test_noscript_agent_message_and_safari_note_exist_in_static_html(self) -> None:
        inspector = _parse_html(SETUP_HTML)
        noscript_text = " ".join("".join(inspector.noscript_chunks).split())
        self.assertIn("JavaScript", noscript_text)
        self.assertIn("direkte", noscript_text)

        message = inspector.text_by_id["agent-message"]
        self.assertNotIn("`", message)
        lines = message.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertIn(
            "Åpne det eksisterende Familiekilder-oppsettet på denne maskinen igjen.",
            lines[0],
        )
        self.assertEqual(
            lines[1],
            "macOS/Linux: ~/familiekilder-mcp/.private/venv/bin/python -I -m familiekilder setup --no-open",
        )
        self.assertEqual(
            lines[2],
            'Windows (PowerShell): & "$HOME\\familiekilder-mcp\\.private\\venv\\Scripts\\python.exe" -I -m familiekilder setup --no-open',
        )
        self.assertIn(LOCAL_ORIGIN, lines[3])
        self.assertIn("--root", lines[3])
        self.assertIn("--profile", lines[3])

        direct_section_text = inspector.text_by_id["direct-section"]
        self.assertIn("Safari", direct_section_text)
        self.assertIn("Åpne veiviseren direkte", direct_section_text)

        roles = [attrs.get("role") for _, attrs in inspector.elements if attrs.get("role")]
        self.assertIn("status", roles)


NODE_HARNESS = r"""
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const htmlPath = process.argv[2];
const jsPath = process.argv[3];
const htmlSource = fs.readFileSync(htmlPath, 'utf8');
const jsSource = fs.readFileSync(jsPath, 'utf8');

function extractAgentMessage(html) {
  const match = html.match(/<pre id="agent-message"[^>]*>([\s\S]*?)<\/pre>/);
  assert.ok(match, 'agent-message pre block not found in HTML');
  return match[1].replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>');
}

const AGENT_MESSAGE = extractAgentMessage(htmlSource);

function createFakeElement(id, initial = {}) {
  const listeners = new Map();
  const attributes = new Map();
  return {
    id,
    hidden: Boolean(initial.hidden),
    textContent: initial.textContent || '',
    className: initial.className || '',
    dataset: Object.assign({}, initial.dataset || {}),
    href: initial.href || '',
    target: initial.target || '',
    rel: initial.rel || '',
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(fn);
    },
    removeEventListener(type, fn) {
      if (listeners.has(type)) listeners.get(type).delete(fn);
    },
    click() {
      const set = listeners.get('click');
      if (set) {
        for (const fn of Array.from(set)) fn({ target: this });
      }
    },
    setAttribute(name, value) {
      attributes.set(name, String(value));
    },
    removeAttribute(name) {
      attributes.delete(name);
    },
    getAttribute(name) {
      return attributes.has(name) ? attributes.get(name) : null;
    },
  };
}

function createFakePermissionStatus(initialState) {
  const listeners = new Set();
  const status = {
    state: initialState,
    onchange: null,
    addEventListener(type, fn) {
      if (type === 'change') listeners.add(fn);
    },
    removeEventListener(type, fn) {
      if (type === 'change') listeners.delete(fn);
    },
    listenerCount() {
      return listeners.size + (this.onchange ? 1 : 0);
    },
    setState(nextState) {
      this.state = nextState;
      for (const fn of Array.from(listeners)) fn({ target: this });
      if (typeof this.onchange === 'function') this.onchange({ target: this });
    },
  };
  return status;
}

function createDeferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function createFakeAbortControllerClass() {
  return class FakeAbortController {
    constructor() {
      this._listeners = new Set();
      this.signal = {
        aborted: false,
        addEventListener: (type, fn) => {
          if (type === 'abort') this._listeners.add(fn);
        },
        removeEventListener: (type, fn) => {
          if (type === 'abort') this._listeners.delete(fn);
        },
      };
    }
    abort() {
      if (this.signal.aborted) return;
      this.signal.aborted = true;
      for (const fn of Array.from(this._listeners)) fn();
    }
  };
}

async function flushMicrotasks(rounds = 8) {
  for (let i = 0; i < rounds; i += 1) {
    await Promise.resolve();
  }
}

function createHarness(options = {}) {
  let now = 0;
  let nextTimerId = 1;
  const timers = new Map();
  const events = [];
  const fetchCalls = [];
  const permissionQueries = [];
  const selectionLog = [];
  const clipboardWrites = [];

  const elements = {
    'check-panel': createFakeElement('check-panel', { hidden: true, dataset: { state: 'idle' } }),
    'status-region': createFakeElement('status-region'),
    'status-title': createFakeElement('status-title'),
    'status-detail': createFakeElement('status-detail'),
    'status-prompt-help': createFakeElement('status-prompt-help', { hidden: true }),
    'status-denied': createFakeElement('status-denied', { hidden: true }),
    'continue-link': createFakeElement('continue-link', {
      hidden: true,
      href: 'http://127.0.0.1:28417/',
      target: '_blank',
      rel: 'noopener noreferrer',
      textContent: 'Fortsett oppsettet',
    }),
    'check-button': createFakeElement('check-button', { textContent: 'Sjekk tilkoblingen' }),
    'other-address-note': createFakeElement('other-address-note', { hidden: true }),
    'agent-message': createFakeElement('agent-message', { textContent: AGENT_MESSAGE }),
    'copy-message': createFakeElement('copy-message', { hidden: true }),
    'copy-feedback': createFakeElement('copy-feedback', { hidden: true }),
  };

  const selection = {
    ranges: [],
    removeAllRanges() {
      this.ranges = [];
      selectionLog.push('removeAllRanges');
    },
    addRange(range) {
      this.ranges.push(range);
      selectionLog.push({ added: range.node.id });
    },
  };

  const document = {
    readyState: 'complete',
    getElementById(id) {
      assert.ok(Object.prototype.hasOwnProperty.call(elements, id), `Unknown element id: ${id}`);
      return elements[id];
    },
    createRange() {
      return {
        node: null,
        selectNodeContents(node) {
          this.node = node;
          selectionLog.push({ selected: node.id });
        },
      };
    },
    addEventListener() {},
  };

  const fakeSetTimeout = (fn, delay) => {
    const id = nextTimerId++;
    timers.set(id, { id, fn, runAt: now + Number(delay || 0) });
    return id;
  };
  const fakeClearTimeout = (id) => {
    timers.delete(id);
  };

  async function advance(ms) {
    const target = now + ms;
    while (true) {
      let next = null;
      for (const entry of timers.values()) {
        if (entry.runAt <= target && (!next || entry.runAt < next.runAt || (entry.runAt === next.runAt && entry.id < next.id))) {
          next = entry;
        }
      }
      if (!next) break;
      timers.delete(next.id);
      now = next.runAt;
      next.fn();
      await flushMicrotasks();
    }
    now = target;
    await flushMicrotasks();
  }

  const fetchImpl = options.fetch || ((url, opts) => {
    const deferred = createDeferred();
    if (opts && opts.signal) {
      opts.signal.addEventListener('abort', () => {
        const err = new Error('Aborted');
        err.name = 'AbortError';
        deferred.reject(err);
      });
    }
    return deferred.promise;
  });

  const wrappedFetch = (url, opts) => {
    events.push('fetch');
    const record = { url, opts };
    fetchCalls.push(record);
    return fetchImpl(url, opts, record);
  };

  let permissionsObj;
  if (options.permissions === null) {
    permissionsObj = undefined;
  } else if (typeof options.permissions === 'function') {
    permissionsObj = {
      query(descriptor) {
        events.push(`permissions:${descriptor.name}`);
        permissionQueries.push(descriptor.name);
        return options.permissions(descriptor);
      },
    };
  } else {
    permissionsObj = {
      query(descriptor) {
        events.push(`permissions:${descriptor.name}`);
        permissionQueries.push(descriptor.name);
        return Promise.resolve(createFakePermissionStatus('granted'));
      },
    };
  }

  let clipboardObj;
  if (options.clipboard === null) {
    clipboardObj = undefined;
  } else if (typeof options.clipboard === 'function') {
    clipboardObj = {
      writeText(text) {
        clipboardWrites.push(text);
        return options.clipboard(text);
      },
    };
  } else {
    clipboardObj = {
      writeText(text) {
        clipboardWrites.push(text);
        return Promise.resolve();
      },
    };
  }

  const navigator = {};
  if (permissionsObj !== undefined) navigator.permissions = permissionsObj;
  if (clipboardObj !== undefined) navigator.clipboard = clipboardObj;

  const sandbox = {
    document,
    navigator,
    fetch: wrappedFetch,
    setTimeout: fakeSetTimeout,
    clearTimeout: fakeClearTimeout,
    AbortController: createFakeAbortControllerClass(),
    getSelection: () => selection,
    Promise,
    Error,
    TypeError,
    Object,
    Array,
    Boolean,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;

  vm.runInNewContext(jsSource, sandbox, { filename: 'oppsett.js' });

  return {
    elements,
    events,
    fetchCalls,
    permissionQueries,
    selection,
    selectionLog,
    clipboardWrites,
    timers,
    advance,
    flushMicrotasks,
  };
}

function okJsonResponse(payload) {
  return {
    ok: true,
    status: 200,
    json: () => Promise.resolve(payload),
  };
}

async function testIdleAndGrantedSuccess() {
  const pendingFetch = createDeferred();
  const pendingPerm = createDeferred();
  const h = createHarness({
    fetch: () => pendingFetch.promise,
    permissions: () => pendingPerm.promise,
  });

  // Initial idle state after load.
  assert.equal(h.elements['check-panel'].hidden, false);
  assert.equal(h.elements['check-panel'].dataset.state, 'idle');
  assert.equal(h.elements['check-button'].textContent, 'Sjekk tilkoblingen');
  assert.equal(h.elements['continue-link'].hidden, true);
  assert.equal(h.elements['other-address-note'].hidden, true);
  assert.equal(h.elements['copy-message'].hidden, false);

  h.elements['check-button'].click();

  // Fetch starts synchronously inside click handler before permissions.query resolves.
  assert.equal(h.fetchCalls.length, 1);
  assert.equal(h.events[0], 'fetch');
  assert.equal(h.events[1], 'permissions:loopback-network');
  assert.equal(h.fetchCalls[0].url, 'http://127.0.0.1:28417/status');
  assert.equal(h.fetchCalls[0].opts.cache, 'no-store');
  assert.equal(h.fetchCalls[0].opts.credentials, 'omit');
  assert.equal(h.fetchCalls[0].opts.redirect, 'error');
  assert.equal(h.fetchCalls[0].opts.headers, undefined);
  assert.equal(h.elements['check-panel'].dataset.state, 'checking');
  assert.equal(h.elements['check-panel'].getAttribute('aria-busy'), 'true');
  assert.equal(h.elements['check-button'].textContent, 'Prøv igjen');

  pendingPerm.resolve(createFakePermissionStatus('granted'));
  await flushMicrotasks();

  pendingFetch.resolve(okJsonResponse({ app: 'familiekilder-mcp', protocol: 1 }));
  await flushMicrotasks();

  assert.equal(h.elements['check-panel'].dataset.state, 'contact');
  assert.equal(h.elements['check-panel'].getAttribute('aria-busy'), null);
  assert.equal(h.elements['status-title'].textContent, 'Familiekilder kjører på denne maskinen');
  assert.equal(h.elements['continue-link'].hidden, false);
  assert.equal(h.elements['other-address-note'].hidden, false);
  assert.equal(h.elements['status-denied'].hidden, true);
  assert.equal(h.elements['check-button'].hidden, false);
  assert.equal(h.timers.size, 0);
}

async function testDeniedPermission() {
  const permStatus = createFakePermissionStatus('denied');
  const h = createHarness({
    fetch: () => Promise.reject(new TypeError('Failed to fetch')),
    permissions: () => Promise.resolve(permStatus),
  });

  h.elements['check-button'].click();
  await flushMicrotasks();

  assert.equal(h.elements['check-panel'].dataset.state, 'no-contact');
  assert.equal(
    h.elements['status-title'].textContent,
    'Vi får ikke kontakt med Familiekilder på denne maskinen',
  );
  assert.equal(h.elements['status-denied'].hidden, false);
  assert.equal(h.elements['continue-link'].hidden, true);
  assert.ok(h.permissionQueries.length >= 2, 'Permission should be re-read after failure');
}

async function testPromptToGrantedToHang() {
  const permStatus = createFakePermissionStatus('prompt');
  const h = createHarness({
    fetch: (_url, opts) =>
      new Promise((_res, rej) => {
        opts.signal.addEventListener('abort', () => rej(new Error('Aborted')));
      }),
    permissions: () => Promise.resolve(permStatus),
  });

  h.elements['check-button'].click();
  await flushMicrotasks();

  // After 30 s in prompt: still checking, not aborted, prompt help still hidden.
  await h.advance(30000);
  assert.equal(h.elements['check-panel'].dataset.state, 'checking');
  assert.equal(h.fetchCalls[0].opts.signal.aborted, false);
  assert.equal(h.elements['status-prompt-help'].hidden, true);

  // After 60 s total in prompt: neutral help appears WITHOUT aborting.
  await h.advance(30000);
  assert.equal(h.elements['check-panel'].dataset.state, 'checking');
  assert.equal(h.fetchCalls[0].opts.signal.aborted, false);
  assert.equal(h.elements['status-prompt-help'].hidden, false);

  // Even at 90 s still in prompt, do not abort.
  await h.advance(30000);
  assert.equal(h.fetchCalls[0].opts.signal.aborted, false);

  // Permission changes to granted: prompt help clears and 5 s timer starts now.
  permStatus.setState('granted');
  await flushMicrotasks();
  assert.equal(h.elements['status-prompt-help'].hidden, true);
  assert.equal(permStatus.listenerCount(), 0);

  await h.advance(4999);
  assert.equal(h.elements['check-panel'].dataset.state, 'checking');
  assert.equal(h.fetchCalls[0].opts.signal.aborted, false);

  await h.advance(1);
  assert.equal(h.fetchCalls[0].opts.signal.aborted, true);
  assert.equal(h.elements['check-panel'].dataset.state, 'no-contact');
  assert.equal(h.elements['status-denied'].hidden, true);
}

async function testPromptToDenied() {
  const permStatus = createFakePermissionStatus('prompt');
  const pendingFetch = createDeferred();
  const h = createHarness({
    fetch: () => pendingFetch.promise,
    permissions: () => Promise.resolve(permStatus),
  });

  h.elements['check-button'].click();
  await flushMicrotasks();
  assert.equal(h.elements['check-panel'].dataset.state, 'checking');

  permStatus.setState('denied');
  pendingFetch.reject(new TypeError('Failed to fetch'));
  await flushMicrotasks();

  assert.equal(h.elements['check-panel'].dataset.state, 'no-contact');
  assert.equal(h.elements['status-denied'].hidden, false);
  assert.equal(permStatus.listenerCount(), 0);
  assert.equal(h.timers.size, 0);
}

async function testUnsupportedPermissionsAndFallback() {
  // Fallback from loopback-network to local-network-access uses 5 s timeout when granted.
  const hFallback = createHarness({
    fetch: (_url, opts) =>
      new Promise((_res, rej) => {
        opts.signal.addEventListener('abort', () => rej(new Error('Aborted')));
      }),
    permissions: (descriptor) => {
      if (descriptor.name === 'loopback-network') {
        return Promise.reject(new TypeError('Unsupported permission'));
      }
      return Promise.resolve(createFakePermissionStatus('granted'));
    },
  });
  hFallback.elements['check-button'].click();
  await flushMicrotasks();
  assert.deepEqual(hFallback.permissionQueries.slice(0, 2), [
    'loopback-network',
    'local-network-access',
  ]);
  await hFallback.advance(4999);
  assert.equal(hFallback.elements['check-panel'].dataset.state, 'checking');
  await hFallback.advance(1);
  assert.equal(hFallback.elements['check-panel'].dataset.state, 'no-contact');
  assert.equal(hFallback.elements['status-denied'].hidden, true);

  // Neither permission name supported: 60 s timeout.
  const hUnknown = createHarness({
    fetch: (_url, opts) =>
      new Promise((_res, rej) => {
        opts.signal.addEventListener('abort', () => rej(new Error('Aborted')));
      }),
    permissions: () => {
      throw new TypeError('Unsupported permission');
    },
  });
  hUnknown.elements['check-button'].click();
  await flushMicrotasks();
  await hUnknown.advance(59999);
  assert.equal(hUnknown.elements['check-panel'].dataset.state, 'checking');
  assert.equal(hUnknown.fetchCalls[0].opts.signal.aborted, false);
  await hUnknown.advance(1);
  assert.equal(hUnknown.fetchCalls[0].opts.signal.aborted, true);
  assert.equal(hUnknown.elements['check-panel'].dataset.state, 'no-contact');
  assert.equal(hUnknown.elements['status-denied'].hidden, true);

  // Missing navigator.permissions still works for both success and timeout.
  const hMissing = createHarness({
    fetch: () => Promise.resolve(okJsonResponse({ app: 'familiekilder-mcp', protocol: 1 })),
    permissions: null,
  });
  hMissing.elements['check-button'].click();
  await flushMicrotasks();
  assert.equal(hMissing.elements['check-panel'].dataset.state, 'contact');
}

async function testTimeoutWhenGranted() {
  const h = createHarness({
    fetch: (_url, opts) =>
      new Promise((_res, rej) => {
        opts.signal.addEventListener('abort', () => rej(new Error('Aborted')));
      }),
    permissions: () => Promise.resolve(createFakePermissionStatus('granted')),
  });

  h.elements['check-button'].click();
  await flushMicrotasks();
  await h.advance(4999);
  assert.equal(h.elements['check-panel'].dataset.state, 'checking');
  await h.advance(1);
  assert.equal(h.fetchCalls[0].opts.signal.aborted, true);
  assert.equal(h.elements['check-panel'].dataset.state, 'no-contact');
  assert.equal(h.elements['status-denied'].hidden, true);
}

async function testForeignAndInvalidResponses() {
  const invalidResponses = [
    // Foreign HTML / non-JSON body.
    { ok: true, status: 200, json: () => Promise.reject(new SyntaxError('Unexpected token <')) },
    // Non-OK status even with valid payload.
    {
      ok: false,
      status: 503,
      json: () => Promise.resolve({ app: 'familiekilder-mcp', protocol: 1 }),
    },
    // Foreign JSON shape.
    okJsonResponse({ status: 'ok' }),
    // Wrong app.
    okJsonResponse({ app: 'other-app', protocol: 1 }),
    // Wrong protocol version.
    okJsonResponse({ app: 'familiekilder-mcp', protocol: 2 }),
    // Wrong protocol type.
    okJsonResponse({ app: 'familiekilder-mcp', protocol: '1' }),
    // Null payload.
    okJsonResponse(null),
  ];

  for (const response of invalidResponses) {
    const h = createHarness({
      fetch: () => Promise.resolve(response),
      permissions: () => Promise.resolve(createFakePermissionStatus('granted')),
    });
    h.elements['check-button'].click();
    await flushMicrotasks();
    assert.equal(h.elements['check-panel'].dataset.state, 'no-contact');
    assert.equal(h.elements['continue-link'].hidden, true);
    assert.equal(h.elements['status-denied'].hidden, true);
  }
}

async function testTwoQuickClicks() {
  const first = createDeferred();
  const second = createDeferred();
  let callIndex = 0;
  const h = createHarness({
    fetch: () => {
      callIndex += 1;
      return callIndex === 1 ? first.promise : second.promise;
    },
    permissions: () => Promise.resolve(createFakePermissionStatus('granted')),
  });

  h.elements['check-button'].click();
  assert.equal(h.fetchCalls.length, 1);
  assert.equal(h.fetchCalls[0].opts.signal.aborted, false);

  h.elements['check-button'].click();
  assert.equal(h.fetchCalls.length, 2);
  assert.equal(h.fetchCalls[0].opts.signal.aborted, true);
  assert.equal(h.fetchCalls[1].opts.signal.aborted, false);

  // Stale first attempt resolving with valid data must NOT update the page.
  first.resolve(okJsonResponse({ app: 'familiekilder-mcp', protocol: 1 }));
  await flushMicrotasks();
  assert.equal(h.elements['check-panel'].dataset.state, 'checking');

  // Second attempt fails -> page updates to no-contact.
  second.reject(new TypeError('Failed to fetch'));
  await flushMicrotasks();
  assert.equal(h.elements['check-panel'].dataset.state, 'no-contact');

  // Reverse: second attempt wins with contact, stale first attempt later rejects.
  const third = createDeferred();
  const fourth = createDeferred();
  let nextIndex = 0;
  const h2 = createHarness({
    fetch: () => {
      nextIndex += 1;
      return nextIndex === 1 ? third.promise : fourth.promise;
    },
    permissions: () => Promise.resolve(createFakePermissionStatus('granted')),
  });
  h2.elements['check-button'].click();
  h2.elements['check-button'].click();
  fourth.resolve(okJsonResponse({ app: 'familiekilder-mcp', protocol: 1 }));
  await flushMicrotasks();
  assert.equal(h2.elements['check-panel'].dataset.state, 'contact');
  third.reject(new TypeError('Late stale failure'));
  await flushMicrotasks();
  assert.equal(h2.elements['check-panel'].dataset.state, 'contact');
}

async function testRecoveryInSamePage() {
  const permStatus = createFakePermissionStatus('denied');
  let mode = 'fail';
  const h = createHarness({
    fetch: () =>
      mode === 'ok'
        ? Promise.resolve(okJsonResponse({ app: 'familiekilder-mcp', protocol: 1 }))
        : Promise.reject(new TypeError('Failed to fetch')),
    permissions: () => Promise.resolve(permStatus),
  });

  // First click: no contact + denied note.
  h.elements['check-button'].click();
  await flushMicrotasks();
  assert.equal(h.elements['check-panel'].dataset.state, 'no-contact');
  assert.equal(h.elements['status-denied'].hidden, false);

  // User allows permission and starts local wizard, then retries in the same tab.
  permStatus.setState('granted');
  mode = 'ok';
  h.elements['check-button'].click();
  assert.equal(h.elements['check-panel'].dataset.state, 'checking');
  assert.equal(h.elements['status-denied'].hidden, true);
  await flushMicrotasks();
  assert.equal(h.elements['check-panel'].dataset.state, 'contact');
  assert.equal(h.elements['continue-link'].hidden, false);
  assert.equal(h.elements['other-address-note'].hidden, false);

  // Wizard stops again and user rechecks from contact state.
  mode = 'fail';
  h.elements['check-button'].click();
  await flushMicrotasks();
  assert.equal(h.elements['check-panel'].dataset.state, 'no-contact');
  assert.equal(h.elements['status-denied'].hidden, true);
  assert.equal(h.elements['continue-link'].hidden, true);
  assert.equal(h.elements['other-address-note'].hidden, true);
}

async function testClipboardSuccessAndFailure() {
  // Clipboard success: plain text without backticks, one command per line.
  const hOk = createHarness({
    clipboard: () => Promise.resolve(),
  });
  hOk.elements['copy-message'].click();
  await flushMicrotasks();
  assert.equal(hOk.clipboardWrites.length, 1);
  assert.equal(hOk.clipboardWrites[0], AGENT_MESSAGE);
  assert.ok(!hOk.clipboardWrites[0].includes('`'));
  assert.equal(hOk.elements['copy-feedback'].hidden, false);
  assert.equal(hOk.elements['copy-feedback'].textContent, 'Beskjeden er kopiert.');

  // Clipboard rejects: falls back to selecting the text node contents.
  const hFail = createHarness({
    clipboard: () => Promise.reject(new Error('NotAllowedError')),
  });
  hFail.elements['copy-message'].click();
  await flushMicrotasks();
  assert.equal(hFail.selection.ranges.length, 1);
  assert.equal(hFail.selection.ranges[0].node.id, 'agent-message');
  assert.equal(hFail.elements['copy-feedback'].hidden, false);
  assert.equal(
    hFail.elements['copy-feedback'].textContent,
    'Beskjeden er merket. Kopier den med Ctrl+C eller Cmd+C.',
  );

  // Missing clipboard API: also selects the text cleanly.
  const hMissing = createHarness({ clipboard: null });
  hMissing.elements['copy-message'].click();
  await flushMicrotasks();
  assert.equal(hMissing.selection.ranges.length, 1);
  assert.equal(hMissing.selection.ranges[0].node.id, 'agent-message');
}

async function main() {
  const cases = [
    testIdleAndGrantedSuccess,
    testDeniedPermission,
    testPromptToGrantedToHang,
    testPromptToDenied,
    testUnsupportedPermissionsAndFallback,
    testTimeoutWhenGranted,
    testForeignAndInvalidResponses,
    testTwoQuickClicks,
    testRecoveryInSamePage,
    testClipboardSuccessAndFailure,
  ];
  for (const testCase of cases) {
    await testCase();
  }
  process.stdout.write(`ok ${cases.length}\n`);
}

main().catch((error) => {
  process.stderr.write(`${error && error.stack ? error.stack : String(error)}\n`);
  process.exit(1);
});
"""


class SetupPageStateMachineTests(unittest.TestCase):
    def test_state_machine_in_node_vm(self) -> None:
        node = shutil.which("node")
        if node is None:
            if os.environ.get("FAMILIEKILDER_REQUIRE_NODE") == "1":
                self.fail("node is required when FAMILIEKILDER_REQUIRE_NODE=1")
            self.skipTest("node is not installed")

        result = subprocess.run(
            [node, "-", str(SETUP_HTML), str(SETUP_JS)],
            input=NODE_HARNESS,
            capture_output=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"Node state-machine harness failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )
        self.assertIn("ok 10", result.stdout)


if __name__ == "__main__":
    unittest.main()
