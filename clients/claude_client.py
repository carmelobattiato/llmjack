#!/usr/bin/env python3
"""
Claude.ai client via Playwright browser automation.

Strategy:
  1. Launch Chrome with persistent profile (handles Cloudflare + session cookies).
  2. Navigate to claude.ai for login check / manual login if needed.
  3. Use context.request (Playwright APIRequestContext) for all HTTP calls —
     it shares the browser's cookie jar, so sessionKey + cf_clearance are
     included automatically without any page.route() interception.

No page.route() needed: context.request.post() returns the full SSE body directly.

API flow:
  1. GET  /api/organizations                                           → org_id
  2. POST /api/organizations/{org_id}/chat_conversations               → conv_id (once)
  3. POST /api/organizations/{org_id}/chat_conversations/{conv_id}/completion (each turn)

SSE format (Anthropic Messages API):
  {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"delta"}}
"""
import json
import queue as _stdlib_queue
import sys
import time
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright, Playwright, BrowserContext, Page

_DATA_DIR   = Path(__file__).parent.parent / "data"
PROFILE_DIR = _DATA_DIR / "claude_profile"
SESSION_FILE = _DATA_DIR / "claude_session"
CLAUDE_URL = "https://claude.ai"
RESPONSE_TIMEOUT = 180_000  # ms — SSE stream can take a while for long responses

DEBUG = False


def dbg(msg: str):
    if DEBUG:
        try:
            from core import log_manager as _lm
            _lm.tlog(msg)
        except Exception:
            print(f"[CLAUDE-DEBUG {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


class ClaudeAIClient:
    def __init__(self, session_id: str | None = None, model: str = "claude-sonnet-4-6", echo: bool = True):
        self._pw: Playwright = sync_playwright().start()
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._model = model
        self._echo = echo
        self._ready = False
        self._headless_blocked = False

        self._org_id: str | None = None
        self._conv_id: str | None = None

        # session_id = "org_id/conv_id"
        if session_id and "/" in session_id:
            org, conv = session_id.split("/", 1)
            self._org_id = org
            self._conv_id = conv

    # ------------------------------------------------------------------
    # SSE parser
    # ------------------------------------------------------------------

    def _parse_claude_chunks(self, body: bytes) -> list[str]:
        """Parse Claude.ai SSE body → list of delta text chunks.

        Claude.ai uses Anthropic Messages API streaming format:
          {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"..."}}
        We skip thinking_delta, input_json_delta, and other non-text delta types.
        """
        text = body.decode("utf-8", errors="replace")
        chunks: list[str] = []

        if DEBUG:
            lines = [l for l in text.split("\n") if l.startswith("data: ")]
            dbg(f"[PARSE] data-lines={len(lines)}")
            for i, l in enumerate(lines[:5]):
                dbg(f"[PARSE] data[{i}]: {l[:300]}")

        for line in text.split("\n"):
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if not payload:
                continue
            try:
                msg = json.loads(payload)
                if msg.get("type") == "content_block_delta":
                    delta = msg.get("delta", {})
                    if delta.get("type") == "text_delta":
                        t = delta.get("text", "")
                        if t:
                            chunks.append(t)
            except Exception:
                pass

        dbg(f"[PARSE] extracted {len(chunks)} chunks, {sum(len(c) for c in chunks)} chars")
        return chunks

    # ------------------------------------------------------------------
    # Browser lifecycle
    # ------------------------------------------------------------------

    def _open_context(self, headless: bool):
        if self._context:
            self._context.close()
            time.sleep(2)

        for lock_name in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
            (PROFILE_DIR / lock_name).unlink(missing_ok=True)

        ignore_args = ["--enable-automation"]
        if not headless:
            ignore_args.append("--enable-unsafe-swiftshader")

        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",
            headless=headless,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            ignore_default_args=ignore_args,
            viewport={"width": 1280, "height": 800},
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        dbg(f"browser launched headless={headless}")

    # ------------------------------------------------------------------
    # Login / ready
    # ------------------------------------------------------------------

    def _check_logged_in(self) -> bool:
        """GET /api/organizations via context.request; returns True if session valid."""
        try:
            resp = self._context.request.get(
                f"{CLAUDE_URL}/api/organizations",
                headers={"anthropic-client-platform": "web_claude_ai"},
                timeout=10_000,
            )
            dbg(f"[CHECK] /api/organizations → {resp.status}")
            if resp.ok:
                d = resp.json()
                if d and isinstance(d, list):
                    org_id = (d[0].get("uuid") or d[0].get("id") or "").strip()
                    if org_id:
                        self._org_id = org_id
                        return True
        except Exception as e:
            dbg(f"org check failed: {e}")
        return False

    def _ensure_ready(self):
        if self._ready:
            return

        headless_candidates = (False,) if self._headless_blocked else (True, False)
        needs_login = True

        for headless in headless_candidates:
            self._open_context(headless=headless)
            dbg(f"navigating to {CLAUDE_URL} headless={headless}")
            self._page.goto(CLAUDE_URL, wait_until="domcontentloaded")
            self._page.wait_for_timeout(3_000 if headless else 1_500)

            if self._check_logged_in():
                needs_login = False
                dbg(f"logged in headless={headless}")
                break
            if headless:
                dbg("headless: auth failed — trying headed")
                self._headless_blocked = True

        if needs_login:
            print("[!] Claude.ai: sessione scaduta. Apro il browser per il login...")
            self._open_context(headless=False)
            self._page.goto(CLAUDE_URL, wait_until="load")
            print("[!] Effettua il login in Claude.ai nel browser.")
            print("[!] Quando sei loggato e vedi la chat, premi Invio qui per continuare...")
            try:
                input()
            except EOFError:
                pass
            print("[✓] Login confermato.")
            time.sleep(3)

            if not self._check_logged_in():
                raise RuntimeError("Login failed — cannot get org_id after login")

            # Try to switch to headless after login
            try:
                self._open_context(headless=True)
                self._page.goto(CLAUDE_URL, wait_until="domcontentloaded")
                self._page.wait_for_timeout(4_000)
                if not self._check_logged_in():
                    raise Exception("still blocked headless")
                print("[✓] Modalità headless attiva.")
                dbg("headless OK post-login")
            except Exception:
                dbg("headless blocked post-login — using headed")
                self._headless_blocked = True
                print("[!] Cloudflare blocca headless — Chrome rimane visibile.")
                self._open_context(headless=False)
                self._page.goto(CLAUDE_URL, wait_until="domcontentloaded")
                self._page.wait_for_timeout(3_000)
                if not self._check_logged_in():
                    raise RuntimeError("Cannot authenticate after headed fallback")

        dbg(f"org_id: {self._org_id}")

        if not self._conv_id:
            self._conv_id = self._create_conversation()
            self._save_session()
            dbg(f"conv_id: {self._conv_id}")

        self._ready = True

    # ------------------------------------------------------------------
    # API helpers — all via context.request (browser cookie jar)
    # ------------------------------------------------------------------

    def _create_conversation(self) -> str:
        resp = self._context.request.post(
            f"{CLAUDE_URL}/api/organizations/{self._org_id}/chat_conversations",
            headers={
                "Content-Type": "application/json",
                "anthropic-client-platform": "web_claude_ai",
            },
            data=json.dumps({
                "name": "",
                "model": self._model,
                "include_conversation_preferences": True,
                "paprika_mode": None,
                "compass_mode": None,
                "is_temporary": False,
                "enabled_imagine": True,
            }),
            timeout=15_000,
        )
        if not resp.ok:
            raise RuntimeError(f"Cannot create conversation: {resp.status} {resp.text()[:200]}")
        d = resp.json()
        conv_id = (d.get("uuid") or d.get("id") or "").strip()
        if not conv_id:
            raise RuntimeError(f"No uuid in conversation response: {d}")
        return conv_id

    def _complete(self, question: str) -> list[str]:
        """POST to /completion endpoint, parse SSE body, return delta chunks."""
        body = json.dumps({
            "prompt": question,
            "timezone": "Europe/Rome",
            "personalized_styles": [{"type": "default", "key": "Default", "name": "Normal",
                                     "nameKey": "normal_style_name", "prompt": "Normal\n",
                                     "summary": "Default responses from Claude",
                                     "summaryKey": "normal_style_summary", "isDefault": True}],
            "locale": "it-IT",
            "model": self._model,
            "tools": [],
            "turn_message_uuids": {
                "human_message_uuid": str(uuid.uuid4()),
                "assistant_message_uuid": str(uuid.uuid4()),
            },
            "attachments": [],
            "files": [],
            "sync_sources": [],
            "rendering_mode": "messages",
        })

        url = f"{CLAUDE_URL}/api/organizations/{self._org_id}/chat_conversations/{self._conv_id}/completion"
        dbg(f"question: {question[:80]}")
        dbg(f"POST {url}")

        resp = self._context.request.post(
            url,
            headers={
                "Content-Type": "application/json",
                "anthropic-client-platform": "web_claude_ai",
                "accept": "text/event-stream",
            },
            data=body,
            timeout=RESPONSE_TIMEOUT,
        )

        dbg(f"completion status: {resp.status}")
        if not resp.ok:
            dbg(f"error body: {resp.text()[:300]}")
            return []

        return self._parse_claude_chunks(resp.body())

    # ------------------------------------------------------------------
    # Session persistence
    # ------------------------------------------------------------------

    def _save_session(self):
        if self._org_id and self._conv_id:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            SESSION_FILE.write_text(f"{self._org_id}/{self._conv_id}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ask(self, question: str) -> str:
        self._ensure_ready()
        chunks = self._complete(question)
        answer = "".join(chunks)
        if self._echo or DEBUG:
            print(answer, flush=True)
        dbg("done")
        return answer

    def ask_stream(self, question: str, out: "_stdlib_queue.Queue[str | None]") -> None:
        """Send question, put incremental text chunks into `out`. Puts None sentinel when done."""
        self._ensure_ready()
        chunks = self._complete(question)
        for chunk in chunks:
            out.put(chunk)
        out.put(None)

    @property
    def session_id(self) -> str | None:
        if self._org_id and self._conv_id:
            return f"{self._org_id}/{self._conv_id}"
        return None

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

    model = "claude-sonnet-4-6"
    if "--model" in args:
        idx = args.index("--model")
        if idx + 1 < len(args):
            model = args[idx + 1]
            args = args[:idx] + args[idx + 2:]

    session_id = SESSION_FILE.read_text().strip() if SESSION_FILE.exists() else None

    if args:
        with ClaudeAIClient(session_id=session_id, model=model, echo=True) as client:
            client.ask(" ".join(args))
    else:
        with ClaudeAIClient(session_id=session_id, model=model, echo=True) as client:
            print(f"Claude.ai pronto (model={model}). Scrivi una domanda (Ctrl+C per uscire).\n")
            while True:
                try:
                    question = input("Tu: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if not question:
                    continue
                print("Claude: ", end="", flush=True)
                client.ask(question)
                print()
