#!/usr/bin/env python3
"""
ChatGPT client via Playwright browser automation.
Same pattern as deepseek_client.py — page.route() for capture + model injection.

ChatGPT computes openai-sentinel-* tokens (PoW + turnstile) in browser JS
per-request, making direct API calls impossible. Playwright runs real Chrome.

SSE format (v1 encoding): JSON Patch operations, not OpenAI delta format.
  Full patch:  {"o":"patch","v":[{"p":"/message/content/parts/0","o":"append","v":"text"},...]}
  Short patch: {"v":[{"p":"/message/content/parts/0","o":"append","v":"text"},...]}
  Initial add: {"p":"","o":"add","v":{"message":{...,"parts":[""]}}} — empty, skip
Entity markers (\ue200...\ue201) are stripped from captured text.
"""
import json
import queue as _stdlib_queue
import re
import sys
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, Playwright, BrowserContext, Page

_DATA_DIR   = Path(__file__).parent.parent / "data"
PROFILE_DIR = _DATA_DIR / "chatgpt_profile"
SESSION_FILE = _DATA_DIR / "chatgpt_session"
CHATGPT_URL = "https://chatgpt.com"
RESPONSE_TIMEOUT = 120_000  # ms

DEBUG = False

_FILL_JS = """(text) => {
    // ChatGPT uses ProseMirror contenteditable div (#prompt-textarea)
    const el = document.getElementById('prompt-textarea')
             || document.querySelector('[contenteditable="true"][data-id]')
             || document.querySelector('[contenteditable="true"]');
    if (el) {
        el.focus();
        document.execCommand('selectAll', false, null);
        const ok = document.execCommand('insertText', false, text);
        if (!ok) {
            el.innerHTML = '';
            el.dispatchEvent(new InputEvent('input', { bubbles: true, data: text, inputType: 'insertText' }));
            el.textContent = text;
            el.dispatchEvent(new InputEvent('input', { bubbles: true }));
        }
        return true;
    }
    // Fallback: textarea (older ChatGPT)
    const ta = document.querySelector('textarea');
    if (ta) {
        const setter = Object.getOwnPropertyDescriptor(
            window.HTMLTextAreaElement.prototype, 'value'
        ).set;
        setter.call(ta, text);
        ta.dispatchEvent(new Event('input', { bubbles: true }));
        ta.focus();
        return true;
    }
    return false;
}"""


def dbg(msg: str):
    if DEBUG:
        try:
            from core import log_manager as _lm
            _lm.tlog(msg)
        except Exception:
            print(f"[CHATGPT-DEBUG {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


class ChatGPTClient:
    def __init__(self, session_id: str | None = None, model: str = "auto", echo: bool = True):
        self._pw: Playwright = sync_playwright().start()
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._session_id = session_id
        self._model = model
        self._echo = echo
        self._ready = False
        self._headless_blocked = False  # set True if Cloudflare blocks headless

        # Per-request capture state
        self._capture_chunks: list[str] = []
        self._capture_event = threading.Event()
        self._capture_ver = 0
        self._capture_lock = threading.Lock()

    # ------------------------------------------------------------------
    # SSE body parser
    # ------------------------------------------------------------------

    def _parse_chatgpt_chunks(self, body: bytes) -> list[str]:
        """Parse ChatGPT v1 SSE body → list of text delta chunks.

        ChatGPT uses JSON Patch format (not OpenAI delta):
          {"o":"patch","v":[{"p":"/message/content/parts/0","o":"append","v":"text"},...]}
        Initial message add has empty parts[0]; content arrives via patch appends.
        """
        text = body.decode("utf-8", errors="replace")

        if DEBUG:
            lines_with_data = [l for l in text.split("\n") if l.startswith("data: ")]
            dbg(f"[PARSE] data-lines={len(lines_with_data)}")

        chunks: list[str] = []

        for line in text.split("\n"):
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if not payload or payload in ("[DONE]", '"v1"'):
                continue
            try:
                msg = json.loads(payload)

                # Full patch: {"o":"patch","v":[ops]}
                # Short patch: {"v":[ops]} (no top-level "o" or "p" or "type")
                ops = None
                if msg.get("o") == "patch" and isinstance(msg.get("v"), list):
                    ops = msg["v"]
                elif isinstance(msg.get("v"), list) and not msg.get("o") and not msg.get("p") and not msg.get("type"):
                    ops = msg["v"]

                if ops:
                    for op in ops:
                        if (op.get("p") == "/message/content/parts/0"
                                and op.get("o") == "append"
                                and isinstance(op.get("v"), str)
                                and op["v"]):
                            chunks.append(op["v"])

            except Exception:
                pass

        # Entity markers may span chunk boundaries — strip after joining
        full = "".join(chunks)
        full = re.sub(r"\ue200\w+\ue202[^\ue201]*\ue201", "", full)

        dbg(f"[PARSE] extracted {len(chunks)} raw chunks → {len(full)} chars after strip")
        return [full] if full else []

    # ------------------------------------------------------------------
    # Browser lifecycle
    # ------------------------------------------------------------------

    def _open_context(self, headless: bool):
        if self._context:
            self._context.close()
            time.sleep(2)  # Chrome needs time to release profile file locks

        # Clear stale lock files from crashed Chrome instances
        for lock_name in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
            (PROFILE_DIR / lock_name).unlink(missing_ok=True)

        dbg(f"launch_persistent_context headless={headless} channel=chrome")
        # In headed mode drop --enable-unsafe-swiftshader (causes GPU crash on macOS)
        ignore_args = ["--enable-automation"]
        if not headless:
            ignore_args.append("--enable-unsafe-swiftshader")

        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",   # usa Chrome installato, non Chromium bundled
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
            ignore_default_args=ignore_args,
            viewport={"width": 1280, "height": 800},
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

        self._page.on("request",  lambda r: dbg(f"[NET→] {r.method} {r.url}") if "conversation" in r.url else None)
        self._page.on("response", lambda r: dbg(f"[NET←] {r.status} {r.url}") if "conversation" in r.url else None)

        self._register_route()
        dbg("browser launched")

    def _register_route(self):
        def _handle(route):
            my_ver = self._capture_ver
            dbg(f"[ROUTE] intercepted conversation ver={my_ver}")
            try:
                # Inject desired model into request body
                post_data = route.request.post_data
                modified = False
                if post_data and self._model:
                    try:
                        body_dict = json.loads(post_data)
                        body_dict["model"] = self._model
                        post_data = json.dumps(body_dict)
                        modified = True
                        dbg(f"[ROUTE] model injected: {self._model}")
                    except Exception:
                        pass

                resp = route.fetch(post_data=post_data) if modified else route.fetch()
                body = resp.body()
                dbg(f"[ROUTE] body {len(body)} bytes")
                chunks = self._parse_chatgpt_chunks(body)
                dbg(f"[ROUTE] parsed {len(chunks)} chunks → {sum(len(c) for c in chunks)} chars")
                with self._capture_lock:
                    if self._capture_ver == my_ver:
                        self._capture_chunks = chunks
                        self._capture_event.set()
                route.fulfill(response=resp)
            except Exception as e:
                dbg(f"[ROUTE] error: {e}")
                try:
                    route.continue_()
                except Exception:
                    pass

        self._page.route("**/backend-api/f/conversation", _handle)
        dbg("route registered")

    def _ensure_ready(self):
        if self._ready:
            dbg("already ready")
            return

        # If headless was blocked before, skip straight to headed
        headless_candidates = (False,) if self._headless_blocked else (True, False)
        for _headless in headless_candidates:
            self._open_context(headless=_headless)
            start_url = f"{CHATGPT_URL}/c/{self._session_id}" if self._session_id else CHATGPT_URL
            dbg(f"navigating to {start_url} (headless={_headless})")
            self._page.goto(start_url, wait_until="domcontentloaded")
            self._page.wait_for_timeout(3_000 if _headless else 1_000)
            dbg(f"load fired, url={self._page.url}")
            try:
                self._page.locator(
                    '#prompt-textarea, [contenteditable="true"], textarea'
                ).first.wait_for(state="attached", timeout=5_000)
                needs_login = False
                dbg(f"input visible → logged in (headless={_headless})")
                break
            except Exception:
                if _headless:
                    dbg("headless: input not found — trying headed for login check")
                    self._headless_blocked = True
                else:
                    needs_login = True
                    dbg("input not found → needs login")

        if needs_login:
            print("[!] ChatGPT: sessione scaduta. Apro il browser per il login...")
            self._open_context(headless=False)
            self._page.goto(CHATGPT_URL, wait_until="load")
            print("[!] Effettua il login in ChatGPT nel browser.")
            print("[!] Quando sei loggato e vedi la chat, premi Invio qui per continuare...")
            try:
                input()
            except EOFError:
                pass
            print("[✓] Login confermato. Salvataggio sessione in corso...")
            time.sleep(3)  # let Chrome flush cookies to disk

            # Try headless — Cloudflare may block it; fall back to headed if so
            try:
                self._open_context(headless=True)
                self._page.goto(CHATGPT_URL, wait_until="domcontentloaded")
                self._page.wait_for_timeout(4_000)
                self._page.locator(
                    '#prompt-textarea, [contenteditable="true"], textarea'
                ).first.wait_for(state="attached", timeout=6_000)
                dbg("headless OK")
                print("[✓] Modalità headless attiva.")
            except Exception:
                dbg("headless bloccato da Cloudflare — resto in headed")
                self._headless_blocked = True
                print("[!] Cloudflare blocca headless — Chrome rimane visibile in background.")
                self._open_context(headless=False)
                self._page.goto(CHATGPT_URL, wait_until="domcontentloaded")
                self._page.wait_for_timeout(3_000)
                self._page.locator(
                    '#prompt-textarea, [contenteditable="true"], textarea'
                ).first.wait_for(state="attached", timeout=30_000)

        self._page.wait_for_timeout(500)
        dbg(f"ready, url={self._page.url}")
        self._ready = True

    # ------------------------------------------------------------------
    # Request helpers
    # ------------------------------------------------------------------

    def _fire(self, question: str) -> int:
        """Fill input and submit. Returns req_ver (0 on failure)."""
        req_ver = int(time.time() * 1000)
        with self._capture_lock:
            self._capture_ver = req_ver
            self._capture_chunks = []
            self._capture_event.clear()

        dbg(f"question: {question[:80]}")

        try:
            stop = self._page.locator(
                'button[aria-label*="stop" i], button[data-testid*="stop" i]'
            ).first
            stop.wait_for(state="attached", timeout=500)
            stop.click()
            dbg("stop clicked")
            self._page.wait_for_timeout(300)
        except Exception:
            pass

        self._page.wait_for_timeout(200)
        self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        self._page.wait_for_timeout(100)

        filled = self._page.evaluate(_FILL_JS, question)
        if not filled:
            dbg("JS fill failed — trying keyboard fallback")
            try:
                self._page.locator('#prompt-textarea, textarea').first.click()
                self._page.keyboard.press("Control+a")
                self._page.keyboard.type(question[:500])  # cap to avoid slow type
                filled = True
            except Exception as e:
                dbg(f"keyboard fallback failed: {e}")
                return 0

        dbg(f"filled ({len(question)} chars), submitting")
        self._page.wait_for_timeout(150)

        submitted = False
        for selector in [
            'button[data-testid="send-button"]',
            'button[aria-label*="send" i]',
            'button[class*="send"]:not([disabled])',
        ]:
            try:
                btn = self._page.locator(selector).first
                btn.wait_for(state="attached", timeout=400)
                btn.click()
                submitted = True
                dbg(f"submitted via {selector}")
                break
            except Exception:
                continue

        if not submitted:
            self._page.keyboard.press("Enter")
            dbg("submitted via Enter")

        self._page.wait_for_timeout(200)
        return req_ver

    def _save_session(self):
        url = self._page.url
        if "/c/" in url:
            self._session_id = url.split("/c/")[-1].split("?")[0]
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            SESSION_FILE.write_text(self._session_id)
            dbg(f"session_id: {self._session_id}")

    def _wait_for_capture(self) -> list[str]:
        deadline = time.time() + RESPONSE_TIMEOUT / 1000
        last_log_sec = -1
        while time.time() < deadline:
            self._page.wait_for_timeout(200)
            if self._capture_event.is_set():
                break
            elapsed_sec = int(RESPONSE_TIMEOUT / 1000 - (deadline - time.time()))
            if DEBUG and elapsed_sec > 1 and elapsed_sec % 5 == 0 and elapsed_sec != last_log_sec:
                last_log_sec = elapsed_sec
                dbg(f"waiting for route... elapsed={elapsed_sec}s")
        with self._capture_lock:
            return list(self._capture_chunks)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ask(self, question: str) -> str:
        self._ensure_ready()
        req_ver = self._fire(question)
        if not req_ver:
            return ""

        chunks = self._wait_for_capture()
        answer = "".join(chunks)

        self._save_session()
        if self._echo or DEBUG:
            print(answer, flush=True)
        dbg("done")
        return answer

    def ask_stream(self, question: str, out: "_stdlib_queue.Queue[str | None]") -> None:
        """Send question, put incremental text chunks into `out`. Puts None sentinel when done."""
        self._ensure_ready()
        req_ver = self._fire(question)
        if not req_ver:
            out.put(None)
            return

        chunks = self._wait_for_capture()
        self._save_session()
        for chunk in chunks:
            out.put(chunk)
        out.put(None)

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def close(self):
        dbg("closing browser")
        if self._context:
            self._context.close()
        self._pw.stop()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    DEBUG = "--debug" in args
    args = [a for a in args if a != "--debug"]

    model = "auto"
    if "--model" in args:
        idx = args.index("--model")
        if idx + 1 < len(args):
            model = args[idx + 1]
            args = args[:idx] + args[idx + 2:]

    session_id = SESSION_FILE.read_text().strip() if SESSION_FILE.exists() else None

    if args:
        with ChatGPTClient(session_id=session_id, model=model, echo=True) as client:
            client.ask(" ".join(args))
    else:
        with ChatGPTClient(session_id=session_id, model=model, echo=True) as client:
            print(f"ChatGPT pronto (model={model}). Scrivi una domanda (Ctrl+C per uscire).\n")
            while True:
                try:
                    question = input("Tu: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if not question:
                    continue
                print("ChatGPT: ", end="", flush=True)
                client.ask(question)
                print()
