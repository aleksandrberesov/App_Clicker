"""Web perception: turn the live page into the same Observation the desktop engine produces.

A script runs in every visible frame and walks the rendered DOM (open shadow roots
included) in document order. It lists

  * interactive elements, each tagged with an id (``e1``, ``e2``, ...) written to a
    ``data-ac-id`` attribute so the executor can address it with a locator, and
  * context without ids — headings, paragraphs, table rows, alerts, dialogs, images —
    so the model can read and verify the page, not just click it.

Ids are reassigned on every observation, exactly like the UIA engine. States the
model needs to plan are computed in the page: checked/selected/expanded, field
values (passwords masked), whether an element is scrolled out of view, and whether
something such as a modal backdrop covers it.
"""

from __future__ import annotations

import io

from PIL import Image
from playwright.sync_api import Error as PlaywrightError

from ..perception import Observation

ATTR = "data-ac-id"

SNAPSHOT_JS = r"""
({ start, maxItems }) => {
  const ATTR = 'data-ac-id';
  const NODE_CAP = 20000;
  const vw = window.innerWidth, vh = window.innerHeight;
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const clip = (s, n) => (s.length > n ? s.slice(0, n - 1) + '…' : s);
  const q = (s) => '"' + s.replace(/"/g, "'") + '"';

  const clearTags = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.hasAttribute(ATTR)) el.removeAttribute(ATTR);
      if (el.shadowRoot) clearTags(el.shadowRoot);
    }
  };
  clearTags(document);

  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'HEAD', 'META', 'LINK', 'TITLE',
    'OPTION', 'OPTGROUP', 'DATALIST', 'IFRAME', 'FRAME', 'OBJECT', 'EMBED']);
  const INTERACTIVE_ROLES = new Set(['button', 'link', 'checkbox', 'radio', 'switch', 'tab', 'menuitem',
    'menuitemcheckbox', 'menuitemradio', 'option', 'combobox', 'textbox', 'searchbox', 'slider',
    'spinbutton', 'treeitem', 'listbox', 'file-input', 'date-input', 'time-input', 'color-input']);
  const FIELD_ROLES = new Set(['textbox', 'searchbox', 'combobox', 'listbox', 'spinbutton', 'slider',
    'file-input', 'date-input', 'time-input', 'color-input']);
  const INPUT_ROLES = { checkbox: 'checkbox', radio: 'radio', button: 'button', submit: 'button',
    reset: 'button', image: 'button', range: 'slider', number: 'spinbutton', search: 'searchbox',
    file: 'file-input', color: 'color-input', date: 'date-input', 'datetime-local': 'date-input',
    month: 'date-input', week: 'date-input', time: 'time-input' };
  const TYPED_TEXTBOXES = new Set(['email', 'password', 'tel', 'url']);

  const parentOf = (el) => el.parentElement ||
    (el.getRootNode() instanceof ShadowRoot ? el.getRootNode().host : null);

  let active = document.activeElement;
  while (active && active.shadowRoot && active.shadowRoot.activeElement) active = active.shadowRoot.activeElement;

  const roleOf = (el) => {
    const explicit = clean(el.getAttribute('role')).split(' ')[0];
    if (explicit && !['presentation', 'none', 'generic'].includes(explicit)) return explicit;
    switch (el.tagName) {
      case 'A': return el.hasAttribute('href') ? 'link' : null;
      case 'BUTTON': case 'SUMMARY': return 'button';
      case 'INPUT': return el.type === 'hidden' ? null : (INPUT_ROLES[el.type] || 'textbox');
      case 'SELECT': return el.multiple || el.size > 1 ? 'listbox' : 'combobox';
      case 'TEXTAREA': return 'textbox';
      case 'H1': case 'H2': case 'H3': case 'H4': case 'H5': case 'H6': return 'heading';
      case 'IMG': return 'img';
      case 'DIALOG': return 'dialog';
      case 'TR': return 'row';
    }
    const p = parentOf(el);
    if (el.isContentEditable && !(p && p.isContentEditable)) return 'textbox';
    return null;
  };

  const isInteractive = (el, role, style) => {
    if (el.tagName === 'INPUT' && el.type === 'hidden') return false;
    if (role && INTERACTIVE_ROLES.has(role)) return true;
    if (el.hasAttribute('onclick')) return true;
    const ti = el.getAttribute('tabindex');
    if (ti !== null && Number(ti) >= 0 && el !== document.body && el !== document.documentElement) return true;
    if (style.cursor === 'pointer') {
      // cursor is inherited: only the outermost element of a clickable region counts
      const p = parentOf(el);
      return !p || getComputedStyle(p).cursor !== 'pointer';
    }
    return false;
  };

  // A scrollable panel (not the page): gets an id so `scroll` can target it
  const isScrollArea = (el, style) => el !== document.body && el !== document.documentElement &&
    ((/(auto|scroll|overlay)/.test(style.overflowY) && el.scrollHeight > el.clientHeight + 1) ||
     (/(auto|scroll|overlay)/.test(style.overflowX) && el.scrollWidth > el.clientWidth + 1));

  // A label whose text already names a listed control
  const namesControl = (el) => el.tagName === 'LABEL' && el.control && el.control.getClientRects().length > 0;

  const rowText = (el) => [...el.children].map((c) => clip(clean(c.innerText), 40)).filter(Boolean).join(' | ');

  const nameOf = (el, role) => {
    let n = '';
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const root = el.getRootNode();
      n = clean(lb.split(/\s+/)
        .map((id) => (root.getElementById && root.getElementById(id)) || document.getElementById(id))
        .filter(Boolean).map((r) => r.innerText || r.textContent).join(' '));
    }
    n = n || clean(el.getAttribute('aria-label'));
    if (!n && el.labels && el.labels.length) n = clean([...el.labels].map((l) => l.innerText).join(' '));
    if (!n && el.tagName === 'INPUT' && ['submit', 'reset', 'button'].includes(el.type)) {
      n = clean(el.value) || (el.type === 'submit' ? 'Submit' : el.type === 'reset' ? 'Reset' : '');
    }
    if (!n && (el.tagName === 'IMG' || (el.tagName === 'INPUT' && el.type === 'image'))) n = clean(el.alt);
    if (!n && FIELD_ROLES.has(role)) n = clean(el.getAttribute('placeholder'));
    let fromContent = false;
    if (!n && !FIELD_ROLES.has(role)) {
      n = role === 'row' ? rowText(el) : clean(el.innerText);
      fromContent = !!n;
      if (!n) {
        const img = el.querySelector('img[alt]');
        n = img ? clean(img.alt) : '';
      }
    }
    return { name: n || clean(el.getAttribute('title')), fromContent };
  };

  const valueOf = (el, role) => {
    if (el.tagName === 'INPUT') {
      if (['checkbox', 'radio', 'button', 'submit', 'reset', 'image'].includes(el.type)) return null;
      if (el.type === 'file') return [...(el.files || [])].map((f) => f.name).join(', ');
      if (el.type === 'password') return el.value ? '•'.repeat(Math.min(el.value.length, 8)) : '';
      return el.value;
    }
    if (el.tagName === 'TEXTAREA') return el.value;
    if (el.tagName === 'SELECT') return [...el.selectedOptions].map((o) => clean(o.label)).join(', ');
    if (role === 'textbox' && el.isContentEditable) return el.innerText;
    return el.getAttribute('aria-valuetext') || el.getAttribute('aria-valuenow');
  };

  const describeEl = (n) => n.tagName.toLowerCase() + (n.id ? '#' + n.id
    : (typeof n.className === 'string' && n.className.trim() ? '.' + n.className.trim().split(/\s+/)[0] : ''));

  const coveredBy = (el, rect) => {
    // probe the middle of the element's visible part
    const left = Math.max(rect.left, 0), right = Math.min(rect.right, vw);
    const top = Math.max(rect.top, 0), bottom = Math.min(rect.bottom, vh);
    const root = el.getRootNode();
    const hit = (root.elementFromPoint ? root : document).elementFromPoint((left + right) / 2, (top + bottom) / 2);
    if (!hit || hit === el || el.contains(hit) || hit.contains(el)) return null;
    const label = hit.closest && hit.closest('label');
    if (label && label.control === el) return null;
    return describeEl(hit);
  };

  const statesOf = (el, role, rect, inView, scrollArea) => {
    const s = [];
    const aria = (a) => el.getAttribute(a);
    if (scrollArea) {
      const travel = el.scrollHeight - el.clientHeight;
      s.push(travel > 1 ? `scrolled ${Math.round((100 * el.scrollTop) / travel)}%` : 'scrolls sideways');
    }
    if (el.disabled || aria('aria-disabled') === 'true') s.push('disabled');
    if (el.readOnly || aria('aria-readonly') === 'true') s.push('readonly');
    if (['checkbox', 'radio', 'switch', 'menuitemcheckbox', 'menuitemradio'].includes(role)) {
      const c = el.tagName === 'INPUT' ? (el.indeterminate ? 'mixed' : el.checked) : aria('aria-checked');
      s.push(c === 'mixed' ? 'mixed' : (c === true || c === 'true') ? 'checked' : 'unchecked');
    }
    if (aria('aria-selected') === 'true') s.push('selected');
    if (aria('aria-pressed') === 'true') s.push('pressed');
    if (aria('aria-expanded') !== null) s.push(aria('aria-expanded') === 'true' ? 'expanded' : 'collapsed');
    if (el.tagName === 'SUMMARY' && el.parentElement && el.parentElement.tagName === 'DETAILS') {
      s.push(el.parentElement.open ? 'expanded' : 'collapsed');
    }
    if (aria('aria-invalid') === 'true') s.push('invalid');
    if (el.required || aria('aria-required') === 'true') s.push('required');
    if (el === active) s.push('focused');
    if (!inView) s.push('offscreen');
    else {
      const by = coveredBy(el, rect);
      if (by) s.push('covered by ' + by);
    }
    return s;
  };

  const lineFor = (el, role, rect, inView, scrollArea) => {
    const { name, fromContent } = nameOf(el, role);
    let label = role && INTERACTIVE_ROLES.has(role) ? role : scrollArea ? 'scroll-area' : 'clickable';
    if (label === 'textbox' && el.tagName === 'INPUT' && TYPED_TEXTBOXES.has(el.type)) label += '[' + el.type + ']';
    // A clickable card or scroll area: list its content below it instead of one long name
    const long = fromContent && (name.length > 60 || scrollArea);
    const parts = [label];
    if (name && !long) parts.push(q(clip(name, 80)));
    if (!name) {
      const hint = el.id ? '#' + el.id : el.getAttribute('data-testid') ? 'testid=' + el.getAttribute('data-testid')
        : el.tagName === 'A' ? 'href=' + q(clip(el.getAttribute('href') || '', 60)) : '';
      if (hint) parts.push(clip(hint, 60));
    }
    const v = valueOf(el, role);
    if (v) parts.push('value=' + q(clip(clean(v), 60)));
    if (el.tagName === 'SELECT') {
      const opts = [...el.options].map((o) => clip(clean(o.label), 30));
      parts.push('options=[' + opts.slice(0, 15).join(', ') + (opts.length > 15 ? `, +${opts.length - 15} more` : '') + ']');
    }
    const st = statesOf(el, role, rect, inView, scrollArea);
    if (st.length) parts.push('(' + st.join(', ') + ')');
    return { text: parts.join(' '), long };
  };

  // Text of an element plus its inline, non-interactive descendants (a paragraph's worth)
  const ownText = (el) => {
    let s = '';
    for (const n of el.childNodes) {
      if (n.nodeType === Node.TEXT_NODE) { s += n.nodeValue; continue; }
      if (n.nodeType !== Node.ELEMENT_NODE || SKIP.has(n.tagName) || namesControl(n)) continue;
      if (n.tagName === 'BR') { s += ' '; continue; }
      const st = getComputedStyle(n);
      if (st.display === 'none' || st.visibility === 'hidden') continue;
      if (st.display.startsWith('inline') && !isInteractive(n, roleOf(n), st)) {
        s += st.display === 'inline' ? ownText(n) : ' ' + ownText(n) + ' ';
      }
    }
    return s;
  };

  const childrenOf = (el) => {
    if (el.tagName === 'SLOT') {
      const assigned = el.assignedElements({ flatten: true });
      return assigned.length ? assigned : [...el.children];
    }
    return el.shadowRoot ? [...el.shadowRoot.children] : [...el.children];
  };

  const items = [];
  let visited = 0;

  // quiet: an ancestor's line already carries this subtree's text
  // box:   visible box left by ancestors that cut off overflow (null = nothing clips)
  // area:  the nearest scroll area, counting the text lines it currently hides
  const walk = (el, depth, quiet, box, area) => {
    if (++visited > NODE_CAP || SKIP.has(el.tagName)) return;
    const style = getComputedStyle(el);
    if (style.display === 'none') return;
    const rect = el.getBoundingClientRect();
    const shown = style.display !== 'contents' && rect.width > 0 && rect.height > 0 &&
      (el.checkVisibility ? el.checkVisibility({ visibilityProperty: true, checkVisibilityCSS: true })
                          : style.visibility === 'visible');
    const inClip = !box || (rect.bottom > box.t && rect.top < box.b && rect.right > box.l && rect.left < box.r);
    const inView = shown && inClip && rect.bottom > 0 && rect.right > 0 && rect.top < vh && rect.left < vw;
    const role = roleOf(el);
    const scrollArea = shown && isScrollArea(el, style);
    let childDepth = depth, childQuiet = quiet;

    let childBox = box;
    if ((style.overflowX !== 'visible' || style.overflowY !== 'visible') &&
        el !== document.body && el !== document.documentElement) {
      childBox = box
        ? { l: Math.max(box.l, rect.left), t: Math.max(box.t, rect.top), r: Math.min(box.r, rect.right), b: Math.min(box.b, rect.bottom) }
        : { l: rect.left, t: rect.top, r: rect.right, b: rect.bottom };
    }
    const childArea = scrollArea ? { hidden: 0 } : area;

    if (shown && (scrollArea || isInteractive(el, role, style))) {
      const { text, long } = lineFor(el, role, rect, inView, scrollArea);
      items.push({ el, text, depth, inView, act: true });
      if (long || scrollArea) {
        childDepth = depth + 1;
        const t = clean(ownText(el));
        if (t) items.push({ text: 'text ' + q(clip(t, 150)), depth: childDepth, inView, act: false });
      } else {
        childQuiet = true;
      }
    } else if (shown && !quiet && area && !inClip) {
      area.hidden += 1;  // scrolled out of its panel: summarized below the panel instead
    } else if (shown && !quiet && namesControl(el)) {
      childQuiet = true;
    } else if (shown && !quiet) {
      let ctx = null;
      if (role === 'heading') {
        const t = clean(el.innerText);
        if (t) { ctx = 'heading ' + q(clip(t, 100)); childQuiet = true; }
      } else if (role === 'dialog' || role === 'alertdialog') {
        const { name, fromContent } = nameOf(el, role);
        ctx = role + (name && !fromContent ? ' ' + q(clip(name, 60)) : '');
        childDepth = depth + 1;
      } else if (role === 'alert' || role === 'status') {
        const t = clean(el.innerText);
        if (t) { ctx = role + ' ' + q(clip(t, 150)); childQuiet = true; }
      } else if (role === 'row') {
        const t = rowText(el);
        if (t) { ctx = 'row ' + q(clip(t, 150)); childQuiet = true; }
      } else if (role === 'img') {
        const a = clean(el.getAttribute('alt') || el.getAttribute('aria-label'));
        if (a) ctx = 'img ' + q(clip(a, 60));
      } else if (!style.display.startsWith('inline')) {
        const t = clean(ownText(el));
        if (t) ctx = 'text ' + q(clip(t, 150));
      }
      if (ctx) items.push({ text: ctx, depth, inView, act: false });
    }

    if (el.namespaceURI === 'http://www.w3.org/2000/svg') return;  // an icon: its insides are noise
    for (const child of childrenOf(el)) walk(child, childDepth, childQuiet, childBox, childArea);
    if (scrollArea && childArea.hidden) {
      items.push({ text: `... ${childArea.hidden} more lines inside; scroll this area to see them`,
                   depth: childDepth, inView, act: false });
    }
  };
  walk(document.body || document.documentElement, 0, false, null, null);

  // Over budget: keep what is on screen, then off-screen controls, then off-screen text
  let kept = items;
  if (items.length > maxItems) {
    const rank = (it) => (it.inView ? 0 : it.act ? 1 : 2);
    const order = items.map((_, i) => i).sort((a, b) => rank(items[a]) - rank(items[b]) || a - b);
    const keep = new Set(order.slice(0, Math.max(maxItems, 0)));
    kept = items.filter((_, i) => keep.has(i));
  }

  let n = start;
  const ids = [], lines = [];
  for (const it of kept) {
    let text = it.text;
    if (it.act) {
      const id = 'e' + (++n);
      it.el.setAttribute(ATTR, id);
      ids.push(id);
      text = `[${id}] ` + text;
    }
    lines.push('  '.repeat(it.depth) + text);
  }
  const se = document.scrollingElement || document.documentElement;
  return {
    lines, ids, next: n, omitted: items.length - kept.length,
    scroll: { y: Math.round(se.scrollTop), height: se.scrollHeight, view: vh },
  };
}
"""


def _clip(text: str, limit: int) -> str:
    t = " ".join((text or "").split())
    return t[: limit - 1] + "…" if len(t) > limit else t


def _title(page) -> str:
    try:
        return page.title()
    except PlaywrightError:
        return ""


def page_label(page) -> str:
    title = _title(page)
    return f"{title} — {page.url}" if title else page.url


class WebPerceiver:
    def __init__(self, session, max_nodes: int = 300, screenshots: bool = True,
                 max_image_edge: int = 1568):
        self.session = session
        self.max_nodes = max_nodes
        self.screenshots = screenshots
        self.max_image_edge = max_image_edge

    # -- public API --------------------------------------------------------
    def observe(self) -> Observation:
        page = self.session.page
        if page is None or page.is_closed():
            head = self._events_block()
            return Observation(
                window_title="(no open tab)",
                tree_text="\n".join(head + ["(All tabs are closed. Use `navigate` to open a page.)"]),
                elements={},
            )

        # A click that opens a tab can be reported while we are reading the old one: follow it
        for _ in range(3):
            try:
                page.wait_for_load_state("domcontentloaded", timeout=self.session.nav_timeout * 1000)
            except PlaywrightError:
                pass
            elements, lines, omitted, scroll = self._snapshot(page)
            shot = self._capture(page) if self.screenshots else None
            if self.session.page is page or self.session.page is None:
                break
            page = self.session.page

        head = self._events_block()
        tabs = self.session.tabs()
        if len(tabs) > 1:
            head.append("Open tabs: " + " | ".join(
                f'[{i}] "{_clip(_title(p) or p.url, 40)}"' + (" (active)" if p is page else "")
                for i, p in enumerate(tabs)
            ))
        if scroll and scroll["height"] > scroll["view"] * 1.05:
            travel = scroll["height"] - scroll["view"]
            pct = round(100 * scroll["y"] / travel) if travel > 0 else 0
            head.append(f"Page scroll: {pct}% (the page is {scroll['height'] / scroll['view']:.1f} screens tall)")

        body = "\n".join(lines) if lines else "(nothing visible on the page)"
        if omitted:
            body += f"\n... ({omitted} more off-screen items not listed; scroll to reach them)"
        return Observation(
            window_title=page_label(page),
            tree_text="\n".join(head + ([""] if head else []) + [body]),
            elements=elements,
            screenshot=shot,
        )

    # -- helpers -----------------------------------------------------------
    def _events_block(self) -> list[str]:
        events = self.session.drain_events()
        return ["Events since the last step:"] + [f"  - {e}" for e in events] if events else []

    def _snapshot(self, page):
        elements: dict = {}
        lines: list[str] = []
        next_id, omitted, scroll = 0, 0, None
        budget = self.max_nodes

        for frame in page.frames:
            main = frame is page.main_frame
            if frame.is_detached():
                continue
            name = ""
            if not main:
                try:
                    owner = frame.frame_element()
                    if not owner.is_visible():
                        continue
                    name = owner.get_attribute("title") or owner.get_attribute("name") or ""
                except PlaywrightError:
                    continue
            result = self._eval(frame, next_id, budget)
            if isinstance(result, str):
                if main:
                    lines.append(f"(could not read the page: {result})")
                continue

            for eid in result["ids"]:
                elements[eid] = frame.locator(f'[{ATTR}="{eid}"]')
            next_id = result["next"]
            budget = max(budget - len(result["lines"]), 0)
            omitted += result["omitted"]
            if main:
                scroll = result["scroll"]
                lines.extend(result["lines"])
            elif result["lines"]:
                lines.append(f'frame "{_clip(name or frame.name or frame.url, 60)}":')
                lines.extend("  " + ln for ln in result["lines"])
        return elements, lines, omitted, scroll

    def _eval(self, frame, start: int, budget: int):
        """The frame's snapshot dict, or a one-line error message if it could not be read."""
        args = {"start": start, "maxItems": budget}
        for attempt in (1, 2):
            try:
                return frame.evaluate(SNAPSHOT_JS, args)
            except PlaywrightError as e:
                error = (str(e).splitlines() or [type(e).__name__])[0]
                # Usually a navigation replaced the document mid-read; let it land and retry once
                if attempt == 2 or frame is not frame.page.main_frame:
                    return error
                try:
                    frame.wait_for_load_state("domcontentloaded")
                except PlaywrightError:
                    return error
        return "unknown error"

    def _capture(self, page):
        try:
            png = page.screenshot(scale="css", timeout=10_000)
        except PlaywrightError:
            return None
        img = Image.open(io.BytesIO(png))
        w, h = img.size
        if max(w, h) <= self.max_image_edge:
            return png
        scale = self.max_image_edge / max(w, h)
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        return buf.getvalue()
