"""A small driver for a real headless Chrome over the DevTools protocol: load a page, wait for a condition, click.

The other browser tests only dump a page's DOM; a test that answers an item has to click. This uses the `websockets`
package (installed with uvicorn) and Chrome's own remote-debugging port on the loopback address, so no extra browser
tooling is needed. Clicks are real mouse events at the element's position, not `element.click()`.
"""
import json
import subprocess
import time
from pathlib import Path

from websockets.sync.client import connect


class Browser:
    def __init__(self, chrome: str, profile: Path):
        self.proc = subprocess.Popen(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars", "--no-first-run",
             "--no-default-browser-check", "--window-size=1280,2000", f"--user-data-dir={profile}",
             "--remote-debugging-port=0", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port_file, deadline = Path(profile) / "DevToolsActivePort", time.monotonic() + 60
        while not (port_file.exists() and len(port_file.read_text().split()) >= 2):
            if time.monotonic() > deadline or self.proc.poll() is not None:
                self.proc.kill()
                raise RuntimeError("Chrome did not open its debugging port")
            time.sleep(0.1)
        port, path = port_file.read_text().split()[:2]
        self.conn = connect(f"ws://127.0.0.1:{port}{path}", max_size=None, proxy=None, open_timeout=30)
        self.ws = self.conn.__enter__()
        self.n, self.session = 0, None
        target = self.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        self.session = self.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        self.call("Page.enable")

    def call(self, method: str, params: dict | None = None) -> dict:
        self.n += 1
        msg = {"id": self.n, "method": method, "params": params or {}}
        if self.session:
            msg["sessionId"] = self.session
        self.ws.send(json.dumps(msg))
        while True:
            reply = json.loads(self.ws.recv(timeout=60))
            if reply.get("id") == self.n:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return reply["result"]

    def js(self, expression: str):
        """The value of a JavaScript expression in the page (promises are awaited)."""
        r = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
        if "exceptionDetails" in r:
            raise RuntimeError(f"page script failed: {r['exceptionDetails'].get('text')} in {expression[:120]}")
        return r["result"].get("value")

    def goto(self, url: str) -> None:
        self.call("Page.navigate", {"url": url})

    def until(self, expression: str, timeout: float = 30, what: str = ""):
        """Wait until a JavaScript expression is truthy in the page; returns its value."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                value = self.js(expression)
            except RuntimeError:
                value = None  # the page is between documents
            if value:
                return value
            if time.monotonic() > deadline:
                raise AssertionError(f"the page never showed: {what or expression}")
            time.sleep(0.1)

    def click(self, selector: str) -> None:
        """A real mouse click in the middle of the first element matching `selector`."""
        sel = json.dumps(selector)
        box = self.until(f"(() => {{ const e = document.querySelector({sel}); if (!e) return null; "
                         "e.scrollIntoView({block: 'center'}); const r = e.getBoundingClientRect(); "
                         "return r.width && r.height ? {x: r.left + r.width / 2, y: r.top + r.height / 2} : null; })()",
                         what=f"something to click at {selector}")
        hit = self.js(f"(() => {{ const e = document.elementFromPoint({box['x']}, {box['y']}); "
                      f"return !!e && !!e.closest({sel}); }})()")
        assert hit, f"{selector} is covered by another element"
        for kind in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", {"type": kind, "x": box["x"], "y": box["y"], "button": "left",
                                                   "clickCount": 1})

    def text(self, selector: str) -> str:
        return self.js(f"(document.querySelector({json.dumps(selector)}) || {{}}).textContent || ''")

    def close(self) -> None:
        try:
            self.session = None
            self.call("Browser.close")
            self.conn.__exit__(None, None, None)
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
