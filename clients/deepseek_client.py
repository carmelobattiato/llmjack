#!/usr/bin/env python3
"""
DeepSeek client via Playwright browser automation.
Mirrors qwen_client.py — same session-persistence pattern, different URL/interceptor.

Direct API calls fail because DeepSeek computes x-ds-pow-response (PoW challenge)
and x-hif-leim per-request in browser JS. Playwright runs real Chrome to bypass this.

Response capture uses page.route() (Playwright network layer) instead of JS fetch
override, because DeepSeek's frontend may capture window.fetch before our override runs.
"""
import json
import queue as _stdlib_queue
import sys
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, Playwright, BrowserContext, Page

_DATA_DIR   = Path(__file__).parent.parent / "data"
PROFILE_DIR = _DATA_DIR / "deepseek_profile"
SESSION_FILE = _DATA_DIR / "deepseek_session"
DEEPSEEK_URL = "https://chat.deepseek.com"
RESPONSE_TIMEOUT = 120_000  # ms

DEBUG = False

# DeepSeek SSE format is NOT standard OpenAI. It uses fragment-based patches:
#
#   Full state:  {"v": {"response": {"fragments": [{"type":"THINK","content":"..."}]}}}
#   New frag:    {"p":"response/fragments","o":"APPEND","v":[{"type":"RESPONSE","content":"La"}]}
#   Content upd: {"p":"response/fragments/-1/content","o":"APPEND","v":" capitale"}
#   Short-form:  {"v":" della"}   (no p/o — appends to current fragment)
#   Status:      {"p":"response/status","o":"SET","v":"FINISHED"}
#
# THINK fragments = reasoning (skip). RESPONSE fragments = actual answer (capture).

_FILL_JS = """(text) => {
    const ta = document.querySelector(
        'textarea:not([aria-hidden="true"])'
    );
    if (!ta) return false;
    const setter = Object.getOwnPropertyDescriptor(
        window.HTMLTextAreaElement.prototype, 'value'
    ).set;
    setter.call(ta, text);
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    ta.focus();
    return true;
}"""


def dbg(msg: str):
    if DEBUG:
        try:
            from core import log_manager as _lm
            _lm.tlog(msg)
        except Exception:
            print(f"[DEEPSEEK-DEBUG {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


class DeepSeekClient:
    def __init__(self, session_id: str | None = None, echo: bool = True):
        self._pw: Playwright = sync_playwright().start()
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._session_id = session_id
        self._echo = echo
        self._ready = False

        # Per-request capture state (thread-safe)
        self._capture_chunks: list[str] = []
        self._capture_event = threading.Event()
        self._capture_ver = 0
        self._capture_lock = threading.Lock()

    # ------------------------------------------------------------------
    # SSE body parser
    # ------------------------------------------------------------------

    def _parse_ds_chunks(self, body: bytes) -> list[str]:
        """Parse raw DeepSeek SSE body → ordered list of RESPONSE content chunks."""
        text = body.decode("utf-8", errors="replace")
        chunks: list[str] = []
        frag_type: str | None = None

        for line in text.split("\n"):
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if not payload:
                continue
            try:
                msg = json.loads(payload)

                # Full state: {"v": {"response": {"fragments": [...]}}}
                if isinstance(msg.get("v"), dict) and isinstance(msg["v"].get("response"), dict):
                    frags = msg["v"]["response"].get("fragments", [])
                    if frags:
                        last = frags[-1]
                        frag_type = last.get("type")
                        if frag_type == "RESPONSE" and last.get("content"):
                            chunks.append(last["content"])
                    continue

                # New fragment appended: {"p":"response/fragments","o":"APPEND","v":[{...}]}
                if msg.get("p") == "response/fragments" and isinstance(msg.get("v"), list):
                    frag = msg["v"][0] if msg["v"] else None
                    if frag:
                        frag_type = frag.get("type")
                        if frag_type == "RESPONSE" and frag.get("content"):
                            chunks.append(frag["content"])
                    continue

                # Content update via path: {"p":"response/fragments/-1/content","v":"..."}
                if msg.get("p") and "content" in msg.get("p", "") and isinstance(msg.get("v"), str):
                    if frag_type == "RESPONSE":
                        chunks.append(msg["v"])
                    continue

                # Short-form: {"v":"..."} — no p, no o
                if isinstance(msg.get("v"), str) and not msg.get("p") and not msg.get("o"):
                    if frag_type == "RESPONSE":
                        chunks.append(msg["v"])
                    continue

            except Exception:
                pass

        return chunks

    # ------------------------------------------------------------------
    # Browser lifecycle
    # ------------------------------------------------------------------

    def _open_context(self, headless: bool):
        if self._context:
            self._context.close()
        dbg(f"launch_persistent_context headless={headless}")
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=headless,
            args=["--no-sandbox"],
            viewport={"width": 1280, "height": 800},
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

        # Network-level debug hooks (passive)
        self._page.on("request",  lambda r: dbg(f"[NET→] {r.method} {r.url}") if "/api/v" in r.url else None)
        self._page.on("response", lambda r: dbg(f"[NET←] {r.status} {r.url}") if "/api/v" in r.url else None)

        # Route interceptor — captures response body at Playwright network layer
        self._register_route()
        dbg("browser launched")

    def _register_route(self):
        def _handle(route):
            my_ver = self._capture_ver
            dbg(f"[ROUTE] intercepted completion ver={my_ver}")
            try:
                resp = route.fetch()
                body = resp.body()
                dbg(f"[ROUTE] body {len(body)} bytes")
                chunks = self._parse_ds_chunks(body)
                answer_len = sum(len(c) for c in chunks)
                dbg(f"[ROUTE] parsed {len(chunks)} chunks → {answer_len} chars")
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

        self._page.route("**/api/v0/chat/completion", _handle)
        dbg("route registered")

    def _ensure_ready(self):
        if self._ready:
            dbg("already ready")
            return

        self._open_context(headless=True)
        start_url = f"{DEEPSEEK_URL}/a/chat/s/{self._session_id}" if self._session_id else DEEPSEEK_URL
        dbg(f"navigating to {start_url}")
        self._page.goto(start_url, wait_until="load")
        dbg(f"load fired, url={self._page.url}")

        try:
            self._page.locator("textarea").first.wait_for(state="visible", timeout=8_000)
            needs_login = False
            dbg("textarea visible → logged in")
        except Exception:
            needs_login = True
            dbg("textarea not found → needs login")

        if needs_login:
            print("[!] DeepSeek: sessione scaduta. Apro il browser per il login...")
            self._open_context(headless=False)
            self._page.goto(DEEPSEEK_URL, wait_until="load")
            print("[!] Effettua il login in DeepSeek nel browser.")
            print("[!] Quando sei loggato, premi Invio qui per continuare...")
            try:
                input()
            except EOFError:
                pass
            print("[✓] Sessione DeepSeek salvata.")
            self._open_context(headless=True)
            self._page.goto(DEEPSEEK_URL, wait_until="load")
            self._page.locator("textarea").first.wait_for(state="visible", timeout=15_000)

        self._page.wait_for_timeout(300)
        dbg(f"ready, url={self._page.url}")
        self._ready = True

    # ------------------------------------------------------------------
    # Request helpers
    # ------------------------------------------------------------------

    def _fire(self, question: str) -> int:
        """Fill textarea and press Enter. Returns req_ver (0 on failure)."""
        req_ver = int(time.time() * 1000)
        with self._capture_lock:
            self._capture_ver = req_ver
            self._capture_chunks = []
            self._capture_event.clear()

        dbg(f"question: {question[:80]}")

        try:
            stop = self._page.locator(
                'button[aria-label*="stop" i], button[title*="stop" i], '
                'button[class*="stop"], [data-testid*="stop"]'
            ).first
            stop.wait_for(state="visible", timeout=500)
            stop.click()
            dbg("stop button clicked")
            self._page.wait_for_timeout(300)
        except Exception:
            pass

        self._page.wait_for_timeout(200)
        self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        self._page.wait_for_timeout(100)

        filled = self._page.evaluate(_FILL_JS, question)
        if not filled:
            dbg("textarea fill failed — aborting")
            return 0

        dbg(f"textarea filled ({len(question)} chars), pressing Enter")
        self._page.wait_for_timeout(100)
        self._page.keyboard.press("Enter")
        self._page.wait_for_timeout(200)
        dbg("Enter sent")
        return req_ver

    def _save_session(self):
        url = self._page.url
        if "/a/chat/s/" in url:
            self._session_id = url.split("/a/chat/s/")[-1].split("?")[0]
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            SESSION_FILE.write_text(self._session_id)
            dbg(f"session_id: {self._session_id}")

    def _wait_for_capture(self, req_ver: int) -> list[str]:
        """Poll until route handler sets capture event or timeout. Returns chunks."""
        deadline = time.time() + RESPONSE_TIMEOUT / 1000
        last_log_sec = -1
        while time.time() < deadline:
            # Keep Playwright event loop alive (routes fire during wait_for_timeout)
            self._page.wait_for_timeout(200)
            if self._capture_event.is_set():
                break
            elapsed_sec = int(RESPONSE_TIMEOUT / 1000 - (deadline - time.time()))
            if DEBUG and elapsed_sec > 1 and elapsed_sec % 5 == 0 and elapsed_sec != last_log_sec:
                last_log_sec = elapsed_sec
                dbg(f"waiting for route... elapsed={elapsed_sec}s")

        with self._capture_lock:
            chunks = list(self._capture_chunks)

        dbg(f"capture done: {len(chunks)} chunks, {sum(len(c) for c in chunks)} chars")
        return chunks

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ask(self, question: str) -> str:
        self._ensure_ready()
        req_ver = self._fire(question)
        if not req_ver:
            return ""

        chunks = self._wait_for_capture(req_ver)
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

        chunks = self._wait_for_capture(req_ver)

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

    session_id = SESSION_FILE.read_text().strip() if SESSION_FILE.exists() else None

    if args:
        with DeepSeekClient(session_id=session_id, echo=True) as client:
            client.ask(" ".join(args))
    else:
        with DeepSeekClient(session_id=session_id, echo=True) as client:
            print("DeepSeek pronto. Scrivi una domanda (Ctrl+C per uscire).\n")
            while True:
                try:
                    question = input("Tu: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if not question:
                    continue
                print("DeepSeek: ", end="", flush=True)
                client.ask(question)
                print()
