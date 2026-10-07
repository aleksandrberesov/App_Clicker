"""Summarize what changed between two consecutive UI snapshots.

After an action the model used to get only "Clicked e175" plus a brand-new tree, with no
hint whether the click did anything. This compares the tree before and after and reports
new/removed elements and state changes ("panel appeared", "item now selected").

Element ids (``e12`` / ``12``) are reassigned on every observation, so a plain line diff
would flag everything below an inserted element as changed. Entries are compared by
content with the ids stripped; the *new* snapshot's ids are shown so the model can act on
what appeared. Works on the text of all three engines (UIA, web, recognizer outline).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import NamedTuple, Optional

_ID = re.compile(r"^\[e?\d+\]\s*")
# Recognizer outlines put several elements on one line: "[1] button "A" | [2] input"
_ROW_SPLIT = re.compile(r" \| (?=\[e?\d+\])")
_STATES = re.compile(r"\s*\(([^()]*)\)\s*$")
_VALUE = re.compile(r'\s+value="[^"]*"')
_OPTIONS = re.compile(r"\s+options=\[[^\]]*\]")
# Lines that change on every observation without telling the model anything new
_COUNT_HEADER = re.compile(r"^On-screen elements \(\d+\):$")  # recognizer
_EVENTS_HEADER = "Events since the last step:"                  # web (already shown in the tree)

MAX_LINE = 140


class _Entry(NamedTuple):
    shown: str            # the element as it appears in its snapshot, id included
    plain: str            # same without the id
    depth: int
    identity: str         # type + name + automation id, without value/state
    value: str
    states: frozenset

    @property
    def key(self) -> tuple:
        return (self.identity, self.value, self.states)


def _clip(text: str, limit: int = MAX_LINE) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _entry(part: str, depth: int) -> _Entry:
    plain = _ID.sub("", part, count=1)
    core, states = plain, frozenset()
    m = _STATES.search(core)
    if m:
        states = frozenset(s.strip() for s in m.group(1).split(",") if s.strip())
        core = core[: m.start()]
    value = ""
    vm = _VALUE.search(core)
    if vm:
        value = vm.group(0).strip()[len("value="):]
        core = _VALUE.sub("", core, count=1)
    core = _OPTIONS.sub("", core, count=1)
    return _Entry(part, plain, depth, core.strip(), value, states)


def _parse(snapshot: str) -> list[_Entry]:
    entries: list[_Entry] = []
    in_events = False
    for raw in (snapshot or "").splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped == _EVENTS_HEADER:
            in_events = True
            continue
        if in_events:
            if raw.startswith("  - "):
                continue
            in_events = False
        if _COUNT_HEADER.match(stripped):
            continue
        depth = (len(raw) - len(raw.lstrip())) // 2
        for part in _ROW_SPLIT.split(stripped):
            entries.append(_entry(part, depth))
    return entries


def _delta(old: _Entry, new: _Entry) -> str:
    parts = [f"+{s}" for s in sorted(new.states - old.states)]
    parts += [f"-{s}" for s in sorted(old.states - new.states)]
    if old.value != new.value:
        parts.append(f"value {old.value or 'empty'} -> {new.value or 'empty'}")
    return ", ".join(parts)


@dataclass
class UIChange:
    added: list = field(default_factory=list)       # [_Entry] in new-snapshot order
    removed: list = field(default_factory=list)     # [_Entry] in old-snapshot order
    changed: list = field(default_factory=list)     # [(old, new)]
    into_view: int = 0
    out_of_view: int = 0
    title: Optional[tuple] = None                   # (old, new) when the window title changed
    total: int = 0                                  # entries in the new snapshot

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed
                    or self.into_view or self.out_of_view or self.title)

    def headline(self) -> str:
        """One short line, for logs and reports."""
        if self.empty:
            return "UI tree unchanged"
        bits = []
        if self.added:
            bits.append(f"+{len(self.added)} added")
        if self.removed:
            bits.append(f"-{len(self.removed)} removed")
        if self.changed:
            bits.append(f"~{len(self.changed)} changed")
        if self.into_view or self.out_of_view:
            bits.append("scrolled")
        if self.title:
            bits.append("title changed")
        return "UI changes: " + ", ".join(bits)

    def render(self, max_added: int = 20, max_removed: int = 8, max_changed: int = 12) -> str:
        """The block shown to the model after an action."""
        if self.empty:
            return ("UI tree unchanged after this action (it may have had no effect, or it only "
                    "changed drawn content that has no element of its own).")

        out = [f"Changes since the previous screen ({self.headline()[len('UI changes: '):]}):"]
        if len(self.added) + len(self.removed) >= 8 and \
                len(self.added) + len(self.removed) >= self.total / 2:
            out.append("  (the screen content was largely replaced)")
        if self.title:
            out.append(f'  window title: "{self.title[0]}" -> "{self.title[1]}"')

        if self.added:
            shown = self.added[:max_added]
            base = min(e.depth for e in shown)
            out += ["  + " + "  " * (e.depth - base) + _clip(e.shown) for e in shown]
            if len(self.added) > len(shown):
                out.append(f"  + ... and {len(self.added) - len(shown)} more added")
        for e in self.removed[:max_removed]:
            out.append("  - " + _clip(e.plain))
        if len(self.removed) > max_removed:
            out.append(f"  - ... and {len(self.removed) - max_removed} more removed")
        for old, new in self.changed[:max_changed]:
            out.append(f"  ~ {_clip(new.shown)}: {_delta(old, new)}")
        if len(self.changed) > max_changed:
            out.append(f"  ~ ... and {len(self.changed) - max_changed} more changed")
        if self.into_view or self.out_of_view:
            out.append(f"  ({self.into_view} elements scrolled into view, "
                       f"{self.out_of_view} out of view)")
        return "\n".join(out)


def diff_snapshots(old: str, new: str, old_title: str = "", new_title: str = "") -> UIChange:
    old_e, new_e = _parse(old), _parse(new)

    # 1. Entries identical in content (ids ignored) are unchanged; duplicates pair up arbitrarily.
    pool: dict = defaultdict(list)
    for i, e in enumerate(old_e):
        pool[e.key].append(i)
    unmatched_new = []
    for j, e in enumerate(new_e):
        idxs = pool.get(e.key)
        if idxs:
            idxs.pop()
        else:
            unmatched_new.append(j)
    unmatched_old = sorted(i for idxs in pool.values() for i in idxs)

    # 2. Leftovers that share an identity are the same element in a different state.
    by_identity: dict = defaultdict(list)
    for i in unmatched_old:
        by_identity[old_e[i].identity].append(i)
    change = UIChange(total=len(new_e))
    for j in unmatched_new:
        cands = by_identity.get(new_e[j].identity)
        if cands:
            old_el, new_el = old_e[cands.pop(0)], new_e[j]
            toggled = old_el.states ^ new_el.states
            if old_el.value == new_el.value and toggled == {"offscreen"}:
                # scrolling flips dozens of these; counting them keeps the summary readable
                if "offscreen" in old_el.states:
                    change.into_view += 1
                else:
                    change.out_of_view += 1
            else:
                change.changed.append((old_el, new_el))
        else:
            change.added.append(new_e[j])
    change.removed = [old_e[i] for i in sorted(i for idxs in by_identity.values() for i in idxs)]

    if old_title != new_title:
        change.title = (old_title, new_title)
    return change
