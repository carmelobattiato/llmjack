#!/usr/bin/env python3
"""
Qwen client via Playwright browser automation.
Browser opens once, stays open for all questions in the session.
Use --new flag to start a fresh conversation.
Use --debug for verbose logging.
"""
import json
import queue as _stdlib_queue
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, Playwright, BrowserContext, Page

_DATA_DIR  = Path(__file__).parent.parent / "data"
PROFILE_DIR = _DATA_DIR / "qwen_profile"
SESSION_FILE = _DATA_DIR / "qwen_session"
QWEN_URL = "https://chat.qwen.ai"
RESPONSE_TIMEOUT = 120_000  # ms

DEBUG = False

_INTERCEPTOR_JS = """
(function() {
    if (window.__qwen_intercepted__) return;
    window.__qwen_intercepted__ = true;
    window.__qwen_answer__  = null;
    window.__qwen_ready__   = false;
    window.__qwen_req_ver__ = 0;

    const _orig = window.fetch;
    window.fetch = async function(...args) {
        const url = typeof args[0] === 'string' ? args[0]
                  : (args[0] instanceof Request ? args[0].url : '');
        const resp = await _orig.apply(this, args);
        if (url.includes('/api/v2/chat/completions')) {
            const clone  = resp.clone();
            const myVer  = window.__qwen_req_ver__;
            (async () => {
                try {
                    const reader = clone.body.getReader();
                    const dec    = new TextDecoder();
                    let answer   = '';
                    while (true) {
                        const { done, value } = await reader.read();
                        if (done) break;
                        if (window.__qwen_req_ver__ !== myVer) return;
                        const text = dec.decode(value, { stream: true });
                        for (const line of text.split('\\n')) {
                            if (!line.startsWith('data: ')) continue;
                            const payload = line.slice(6).trim();
                            if (payload === '[DONE]') break;
                            try {
                                const chunk = JSON.parse(payload);
                                const delta = chunk?.choices?.[0]?.delta;
                                if (delta?.phase === 'answer' && delta?.content) {
                                    answer += delta.content;
                                    window.__qwen_answer__ = answer;
                                }
                            } catch (_) {}
                        }
                    }
                } catch (_) {}
                if (window.__qwen_req_ver__ === myVer) {
                    window.__qwen_ready__ = true;
                }
            })();
        }
        return resp;
    };
})();
"""

_FILL_JS = """(text) => {
    const ta = document.querySelector(
        'textarea:not(.ime-text-area):not([aria-hidden="true"])'
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
            print(f"[QWEN-DEBUG {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


class QwenClient:
    def __init__(self, session_id: str | None = None, model: str | None = None, echo: bool = True):
        self._pw: Playwright = sync_playwright().start()
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._session_id = session_id
        self._model = model
        self._echo = echo
        self._ready = False

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
        self._context.add_init_script(_INTERCEPTOR_JS)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        dbg("browser launched")

    def _ensure_ready(self):
        if self._ready:
            dbg("already ready")
            return

        self._open_context(headless=True)
        start_url = f"{QWEN_URL}/c/{self._session_id}" if self._session_id else QWEN_URL
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
            print("[!] Sessione scaduta. Apro il browser per il login...")
            self._open_context(headless=False)
            self._page.goto(QWEN_URL, wait_until="load")
            print("[!] Effettua il login nel browser.")
            print("[!] Quando sei loggato, premi Invio qui per continuare...")
            try:
                input()
            except EOFError:
                pass
            print("[✓] Sessione salvata.")
            self._open_context(headless=True)
            self._page.goto(QWEN_URL, wait_until="load")
            self._page.locator("textarea").first.wait_for(state="visible", timeout=15_000)

        self._page.wait_for_timeout(300)

        if self._model:
            self._select_model()

        dbg(f"ready, url={self._page.url}")
        self._ready = True

    def _select_model(self):
        if not self._model:
            return
        dbg(f"trying to select model: {self._model}")
        try:
            selectors = [
                '[class*="model-select"]', '[class*="ModelSelect"]',
                '[class*="model-switch"]', '[data-testid*="model"]',
                'button[class*="model"]',
            ]
            for sel in selectors:
                try:
                    btn = self._page.locator(sel).first
                    btn.wait_for(state="visible", timeout=200)
                    btn.click()
                    dbg(f"model selector opened via: {sel}")
                    break
                except Exception:
                    continue
            else:
                dbg("model selector not found, skipping")
                return

            self._page.wait_for_timeout(300)
            short = self._model.replace("qwen", "").strip("-").lower()
            for sel in [
                f'[role="option"]:has-text("{self._model}")',
                f'li:has-text("{self._model}")',
                f'[role="option"]:has-text("{short}")',
                f'li:has-text("{short}")',
            ]:
                try:
                    opt = self._page.locator(sel).first
                    opt.wait_for(state="visible", timeout=300)
                    opt.click()
                    dbg(f"model option clicked: {sel}")
                    self._page.wait_for_timeout(150)
                    return
                except Exception:
                    continue
            dbg("model option not found, closing dropdown")
            self._page.keyboard.press("Escape")
        except Exception as e:
            dbg(f"_select_model failed: {e}")

    def _fire(self, question: str) -> int:
        """Fill textarea and press Enter. Returns req_ver (0 on failure)."""
        req_ver = int(time.time() * 1000)
        self._page.evaluate(f"""
            window.__qwen_req_ver__ = {req_ver};
            window.__qwen_answer__  = null;
            window.__qwen_ready__   = false;
        """)
        dbg(f"typing question: {question[:80]}")

        try:
            stop = self._page.locator(
                'button[aria-label*="stop" i], button[title*="stop" i], '
                'button[class*="stop"], [data-testid*="stop"]'
            ).first
            stop.wait_for(state="visible", timeout=500)
            stop.click()
            dbg("stop button clicked (was generating)")
            self._page.wait_for_timeout(300)
        except Exception:
            pass

        self._page.wait_for_timeout(200)
        self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        self._page.wait_for_timeout(100)

        filled = self._page.evaluate(_FILL_JS, question)
        if not filled:
            dbg("textarea fill via JS failed — aborting")
            return 0

        dbg(f"textarea filled ({len(question)} chars), pressing Enter")
        self._page.wait_for_timeout(100)
        self._page.keyboard.press("Enter")
        self._page.wait_for_timeout(200)
        dbg("Enter sent, starting poll")
        return req_ver

    def _save_session(self):
        url = self._page.url
        if "/c/" in url:
            self._session_id = url.split("/c/")[-1].split("?")[0]
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            SESSION_FILE.write_text(self._session_id)
            dbg(f"session_id: {self._session_id}")

    def ask(self, question: str) -> str:
        self._ensure_ready()
        req_ver = self._fire(question)
        if not req_ver:
            return ""

        deadline = time.time() + RESPONSE_TIMEOUT / 1000
        answer = ""
        while time.time() < deadline:
            self._page.wait_for_timeout(100)
            state = self._page.evaluate(f"""({{
                answer: window.__qwen_answer__ || '',
                ready:  window.__qwen_ready__,
                ver:    window.__qwen_req_ver__
            }})""")
            ans   = state.get("answer", "")
            ready = state.get("ready", False) and state.get("ver") == req_ver
            elapsed = RESPONSE_TIMEOUT / 1000 - (deadline - time.time())
            if ans:
                dbg(f"answer growing: {len(ans)} chars (ready={ready})")
            if ready and ans:
                answer = ans
                break
            if ready and not ans:
                dbg("ready=True but answer empty — stale signal, ignoring")
                self._page.evaluate("window.__qwen_ready__ = false;")
            if DEBUG and int(elapsed) % 5 == 0 and elapsed > 1:
                dbg(f"waiting... elapsed={elapsed:.0f}s answer_len={len(ans)}")

        dbg(f"loop ended: answer={len(answer)} chars")
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

        prev_len = 0
        deadline = time.time() + RESPONSE_TIMEOUT / 1000
        while time.time() < deadline:
            self._page.wait_for_timeout(100)
            state = self._page.evaluate(f"""({{
                answer: window.__qwen_answer__ || '',
                ready:  window.__qwen_ready__,
                ver:    window.__qwen_req_ver__
            }})""")
            ans   = state.get("answer", "")
            ready = state.get("ready", False) and state.get("ver") == req_ver

            if len(ans) > prev_len:
                chunk = ans[prev_len:]
                dbg(f"stream +{len(chunk)} chars")
                out.put(chunk)
                prev_len = len(ans)

            if ready:
                if not ans:
                    self._page.evaluate("window.__qwen_ready__ = false;")
                    continue
                break

        dbg(f"stream done: {prev_len} total chars")
        self._save_session()
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


def interactive_session(session_id: str | None = None):
    with QwenClient(session_id=session_id, echo=True) as client:
        if session_id:
            print(f"Sessione ripresa: {session_id}")
        print("Qwen pronto. Scrivi una domanda (Ctrl+C per uscire).\n")
        while True:
            try:
                question = input("Tu: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not question:
                continue
            print("Qwen: ", end="", flush=True)
            client.ask(question)
            print()
        if client.session_id:
            print(f"\nSession ID: {client.session_id}")


if __name__ == "__main__":
    args = sys.argv[1:]
    DEBUG = "--debug" in args
    args = [a for a in args if a != "--debug"]

    new_chat = "--new" in args
    args = [a for a in args if a != "--new"]

    session_id = None
    if "--session" in args:
        idx = args.index("--session")
        if idx + 1 < len(args):
            session_id = args[idx + 1]
            args = args[:idx] + args[idx + 2:]
    elif new_chat:
        SESSION_FILE.unlink(missing_ok=True)
        dbg("session reset")
    elif SESSION_FILE.exists():
        session_id = SESSION_FILE.read_text().strip()
        dbg(f"resuming saved session: {session_id}")

    if args:
        with QwenClient(session_id=session_id, echo=True) as client:
            client.ask(" ".join(args))
            if client.session_id:
                print(f"\nSession ID: {client.session_id}")
    else:
        interactive_session(session_id=session_id)
