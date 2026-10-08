#!/usr/bin/env python3
"""Book a Hillstone (Wisely) reservation by driving the public booking page.

Wisely's booking POST isn't documented, so instead of guessing its payload we
drive the real widget with Playwright and let its own JS make the request.
Selectors are deliberately loose (labels/placeholders/text) so small UI
changes don't break it.

To make date selection robust, every inventory request the widget makes is
rewritten to our target date + party size, so the time buttons it renders are
the target day's slots no matter what its calendar defaults to.

Guest details come from env (GitHub secrets): GUEST_FIRST_NAME,
GUEST_LAST_NAME, GUEST_PHONE, GUEST_EMAIL, optional GUEST_NOTES.

Debug output goes to ARTIFACT_DIR (default ./artifacts): step screenshots with
all form inputs masked, plus wisely-api.log (request URLs/status only, no
bodies), so nothing personal ends up in the public run artifacts.

Standalone: python book.py 2026-10-09T17:30 [--dry-run]
"""
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

TZ = ZoneInfo("America/Phoenix")
BOOK_URL = "https://reservations.getwisely.com/hillstone-phoenix?g=hillstone-phoenix"
ART = Path(os.getenv("ARTIFACT_DIR") or "artifacts")

SUCCESS_RE = re.compile(
    r"(reservation (is )?(confirmed|booked)|you'?re (all )?set|you'?re booked|"
    r"see you|confirmation (number|#|code)|booking confirmed|thank you)",
    re.I,
)
SUBMIT_RE = re.compile(r"^\s*(reserve|book|confirm|complete|submit|finish)", re.I)
CARD_RE = re.compile(r"(card number|credit card|cvv|cvc)", re.I)


class PageLoadError(RuntimeError):
    """The booking page itself didn't load; retrying other slots won't help."""


def guest():
    g = {k: os.getenv(f"GUEST_{k.upper()}", "").strip()
         for k in ("first_name", "last_name", "phone", "email", "notes")}
    missing = [k for k in ("first_name", "last_name", "phone", "email") if not g[k]]
    if missing:
        raise RuntimeError(f"missing guest secrets: {', '.join('GUEST_' + m.upper() for m in missing)}")
    return g


def time_regex(when: datetime):
    h = when.strftime("%-I")
    return re.compile(rf"(^|\D){h}:{when:%M}\s*{when:%p}", re.I)


DUMP_JS = """() => {
  const out = [];
  const walk = (root) => {
    for (const el of root.querySelectorAll('button,a,select,input,textarea,[role=button],[role=option],[role=radio],[aria-label]')) {
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      out.push([el.tagName.toLowerCase(), el.getAttribute('role') || el.type || '',
                (el.innerText || el.value || '').trim().replace(/\\s+/g, ' ').slice(0, 60),
                el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('name') || ''].join(' | '));
      if (el.shadowRoot) walk(el.shadowRoot);
    }
    for (const el of root.querySelectorAll('*')) if (el.shadowRoot) walk(el.shadowRoot);
  };
  walk(document);
  return out.slice(0, 120);
}"""


def dump_page(page, why):
    """Print the page's visible controls to the log (dry runs only; no guest data there)."""
    print(f"  --- page dump ({why}) url={page.url}")
    for fr in page.frames:
        print(f"  frame: {fr.url[:120]}")
        try:
            for line in fr.evaluate(DUMP_JS):
                print(f"    {line}")
            text = fr.evaluate("() => document.body ? document.body.innerText : ''")
            print("    text: " + " / ".join(t.strip() for t in text.splitlines() if t.strip())[:800])
        except Exception as e:
            print(f"    (unreadable: {e})")


class Flow:
    def __init__(self, page, when, party):
        self.page, self.when, self.party = page, when, party
        self.n = 0
        self.api_log = []
        self.booking_responses = []

    def shot(self, name):
        self.n += 1
        try:
            self.page.screenshot(
                path=str(ART / f"{self.n:02d}-{name}.png"),
                full_page=True,
                mask=[self.page.locator("input, textarea")],
            )
        except Exception as e:
            print(f"  screenshot {name} failed: {e}")

    # -- network -----------------------------------------------------------
    def install_hooks(self):
        start_ms = str(int(self.when.timestamp() * 1000))

        def rewrite(route):
            parts = urlsplit(route.request.url)
            q = dict(parse_qsl(parts.query))
            if "search_ts" in q:
                q["search_ts"] = start_ms
            if "party_size" in q:
                q["party_size"] = str(self.party)
            route.continue_(url=urlunsplit(parts._replace(query=urlencode(q))))

        self.page.route(re.compile(r"wisely\.io/.*/inventory"), rewrite)

        def on_response(resp):
            req = resp.request
            if "wisely" not in req.url:
                return
            line = f"{req.method} {resp.status} {req.url.split('?')[0]}"
            self.api_log.append(line)
            if req.method in ("POST", "PUT") and "inventory" not in req.url:
                self.booking_responses.append((resp.status, req.url))

        self.page.on("response", on_response)

    # -- steps -------------------------------------------------------------
    def _select_option(self, pattern, name_hint):
        """Pick the first <select> option whose text matches pattern. Returns the label or None."""
        rx = re.compile(pattern, re.I)
        for sel in self.page.locator("select").all():
            try:
                opts = sel.evaluate("s => [...s.options].map(o => o.text.trim())")
            except Exception:
                continue
            label = next((o for o in opts if rx.search(o)), None)
            if label:
                sel.select_option(label=label)
                print(f"  {name_hint}: select -> {label}")
                return label
        return None

    def set_party(self):
        if self._select_option(rf"^\s*{self.party}\s*(guests?|people)?\s*$", "party"):
            return
        btn = self.page.get_by_role("button", name=re.compile(rf"^\s*{self.party}\s*(guests?|people)?\s*$", re.I))
        if btn.count():
            btn.first.click()
            print("  party: button")
            return
        print("  party: no control found (relying on request rewrite)")

    def set_time(self):
        w = self.when
        # Wisely's Time dropdown uses zero-padded labels ("05:00 PM").
        pat = rf"^\s*0?{w:%-I}:{w:%M}\s*{w:%p}\s*$"
        if not self._select_option(pat, "time"):
            print("  time: no dropdown option (relying on request rewrite)")

    CLICK_DAY_JS = """([month, day]) => {
      // Find the calendar block headed "October 2026" and click its day cell.
      const heads = [...document.querySelectorAll('body *')].filter(
        e => e.children.length === 0 && e.textContent.trim() === month);
      for (const h of heads) {
        let box = h.parentElement;
        for (let i = 0; i < 6 && box; i++, box = box.parentElement) {
          const cells = [...box.querySelectorAll('*')].filter(
            e => e.children.length === 0 && e.textContent.trim() === day && e.offsetParent !== null);
          if (cells.length === 1) {
            const t = cells[0].closest('button,a,td,[role=button],[role=gridcell]') || cells[0];
            t.scrollIntoView({block: 'center'});
            t.click();
            return true;
          }
        }
      }
      return false;
    }"""

    def set_date(self):
        p, d = self.page, self.when
        for _ in range(3):
            if p.evaluate(self.CLICK_DAY_JS, [d.strftime("%B %Y"), str(d.day)]):
                print(f"  date: calendar {d:%B %Y} day {d.day}")
                return
            nxt = p.locator('[aria-label="Next Month"]')
            if not nxt.count():
                break
            nxt.first.click()
            p.wait_for_timeout(500)
        date_input = p.locator("input[type=date]")
        if date_input.count():
            date_input.first.fill(d.strftime("%Y-%m-%d"))
            print("  date: input[type=date]")
            return
        labels = [
            d.strftime("%A, %B %-d, %Y"), d.strftime("%B %-d, %Y"),
            d.strftime("%A, %B %-d"), d.strftime("%a %b %-d"), d.strftime("%b %-d"),
            d.strftime("%m/%d/%Y"), d.strftime("%Y-%m-%d"),
        ]
        for _ in range(3):  # current month view, then try opening the picker / next month
            for lab in labels:
                loc = p.locator(f'[aria-label*="{lab}" i]')
                if loc.count():
                    loc.first.click()
                    print(f"  date: aria-label {lab!r}")
                    return
            opener = p.get_by_role("button", name=re.compile(r"(date|calendar|today|tomorrow|\w{3},? \w{3} \d)", re.I))
            if opener.count():
                try:
                    opener.first.click(timeout=3000)
                except Exception:
                    pass
            nxt = p.get_by_role("button", name=re.compile(r"next( month)?|›|>", re.I))
            if nxt.count() and d.strftime("%B") not in p.content():
                nxt.first.click()
        print("  date: no picker match (relying on request rewrite)")

    def click_slot(self):
        p = self.page
        rx = time_regex(self.when)
        for attempt in range(3):
            for role in ("button", "link", "radio", "option"):
                loc = p.get_by_role(role, name=rx)
                if loc.count():
                    loc.first.click()
                    print(f"  slot: {role} matching {rx.pattern}")
                    return True
            loc = p.get_by_text(rx)
            if loc.count():
                loc.first.click()
                print("  slot: text match")
                return True
            # Nudge the widget to (re)search, then wait for results.
            search = p.get_by_role("button", name=re.compile(r"(search|find|check availability|view times)", re.I))
            if search.count():
                search.first.click()
            p.wait_for_timeout(2500)
        return False

    def fill_form(self, g):
        p = self.page
        fields = [  # (label/placeholder regex, input-name hint, value)
            (r"first\s*name", "first", g["first_name"]),
            (r"last\s*name|surname", "last", g["last_name"]),
            (r"phone|mobile|cell", "phone", g["phone"]),
            (r"e-?mail", "email", g["email"]),
        ]
        filled = 0
        full_name = p.get_by_label(re.compile(r"^\s*(full\s*)?name\s*\*?$", re.I))
        if full_name.count() and not p.get_by_label(re.compile(r"first\s*name", re.I)).count():
            full_name.first.fill(f"{g['first_name']} {g['last_name']}")
            filled += 2
            fields = fields[2:]
        for pat, hint, val in fields:
            rx = re.compile(pat, re.I)
            for loc in (p.get_by_label(rx), p.get_by_placeholder(rx), p.locator(f'input[name*="{hint}" i]')):
                if loc.count():
                    loc.first.fill(val)
                    filled += 1
                    break
            else:
                print(f"  form: no field for /{pat}/")
        if g["notes"]:
            notes = p.locator("textarea")
            if notes.count():
                notes.first.fill(g["notes"])
        # Tick only required checkboxes (terms etc.); leave marketing opt-ins alone.
        for cb in p.locator("input[type=checkbox][required]").all():
            try:
                if not cb.is_checked():
                    cb.check()
            except Exception:
                pass
        print(f"  form: filled {filled}/4 fields")
        return filled >= 4

    def submit(self):
        p = self.page
        btn = p.get_by_role("button", name=SUBMIT_RE)
        if not btn.count():
            btn = p.locator("button[type=submit], input[type=submit]")
        if not btn.count():
            return False, "no submit button"
        btn.last.click()
        try:
            p.wait_for_function(
                "re => new RegExp(re, 'i').test(document.body.innerText)",
                arg=SUCCESS_RE.pattern, timeout=20000,
            )
            return True, "confirmation page shown"
        except PWTimeout:
            ok_posts = [s for s, _ in self.booking_responses if 200 <= s < 300]
            if ok_posts:
                return True, f"booking POST returned {ok_posts[-1]} (no confirmation text seen)"
            err = p.locator("[role=alert], .error, .alert, [class*=error]").all_inner_texts()
            return False, "no confirmation; " + ("; ".join(e.strip() for e in err if e.strip())[:300] or "unknown")


def book(when: datetime, party: int, dry_run=False):
    """Returns (ok, detail). In dry_run, ok means 'reached submit with form filled'."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=TZ)
    g = guest() if not dry_run else {
        "first_name": "Test", "last_name": "Dryrun", "phone": "6025550100",
        "email": "dryrun@example.com", "notes": "",
    }
    ART.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=os.getenv("CHROMIUM_PATH") or None)
        ctx = browser.new_context(
            timezone_id="America/Phoenix", locale="en-US",
            viewport={"width": 1280, "height": 1000},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"),
        )
        page = ctx.new_page()
        page.set_default_timeout(15000)
        f = Flow(page, when, party)
        f.install_hooks()
        try:
            # The widget polls in the background, so "networkidle" never fires.
            page.goto(os.getenv("BOOK_URL") or BOOK_URL, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_selector("button, select, input", timeout=30000)
            except PWTimeout:
                raise PageLoadError("booking page loaded but no controls appeared")
            page.wait_for_timeout(2000)
            f.shot("loaded")
            f.set_party()
            f.set_date()
            page.wait_for_timeout(1000)
            f.set_time()
            page.wait_for_timeout(2500)
            f.shot("date-selected")
            if not f.click_slot():
                f.shot("slot-not-found")
                if dry_run:
                    dump_page(page, "slot not found")
                    print("  api calls:\n    " + "\n    ".join(f.api_log[:40]))
                return False, f"slot {when:%-I:%M %p} not found on page"
            page.wait_for_timeout(1500)
            # Some flows have an intermediate "Continue"/"Next" (e.g. seating type).
            cont = page.get_by_role("button", name=re.compile(r"^\s*(continue|next)\b", re.I))
            if cont.count() and not page.locator("input[type=email], input[type=tel]").count():
                cont.first.click()
                page.wait_for_timeout(1500)
            f.shot("form")
            if CARD_RE.search(page.inner_text("body")) or page.frame_locator(
                    "iframe[src*=stripe], iframe[name*=card]").locator("input").count():
                return False, "booking requires a credit card; not auto-submitting"
            filled = f.fill_form(g)
            if dry_run:
                dump_page(page, "booking form")
            if not filled:
                f.shot("form-incomplete")
                return False, "could not find all guest fields"
            if dry_run:
                return True, "dry run: form filled, stopped before submit"
            ok, detail = f.submit()
            f.shot("after-submit")
            return ok, detail
        finally:
            (ART / "wisely-api.log").write_text("\n".join(f.api_log) + "\n")
            browser.close()


if __name__ == "__main__":
    w = datetime.fromisoformat(sys.argv[1]).replace(tzinfo=TZ)
    print(book(w, int(os.getenv("PARTY_SIZE") or "2"), dry_run="--dry-run" in sys.argv))
