"""Post-action UI diff: what the model is told changed after each action."""

from types import SimpleNamespace as NS

from app_clicker.agent import TesterAgent
from app_clicker.llm import AssistantTurn, ToolCall
from app_clicker.uidiff import diff_snapshots

BEFORE = """\
[e1] Window "ECG Constructor"
  [e2] Button "Tips"
  [e3] ListItem "Rhythm 35" (selected)
  [e4] Button "OK"
"""


def test_unchanged_tree_says_so():
    change = diff_snapshots(BEFORE, BEFORE)
    assert change.empty
    assert "unchanged" in change.render()
    assert "no effect" in change.render()  # the model must not read this as proof of failure


def test_inserted_panel_is_added_and_does_not_shift_the_rest():
    after = """\
[e1] Window "ECG Constructor"
  [e2] Button "Tips"
  [e3] Pane "Tips"
    [e4] RadioButton "Вся область отведения"
    [e5] RadioButton "Выбранное отведение"
  [e6] ListItem "Rhythm 35" (selected)
  [e7] Button "OK"
"""
    change = diff_snapshots(BEFORE, after)
    # "ListItem" and "OK" got new ids but did not change: only the panel is reported
    assert [e.plain for e in change.added] == [
        'Pane "Tips"', 'RadioButton "Вся область отведения"', 'RadioButton "Выбранное отведение"']
    assert not change.removed and not change.changed
    text = change.render()
    assert '+ [e3] Pane "Tips"' in text          # new ids, so the model can act on them
    assert '  +   [e4] RadioButton "Вся область отведения"' in text  # indented under the panel
    assert "Button \"OK\"" not in text


def test_selection_change_is_reported_as_state_change():
    after = BEFORE.replace('ListItem "Rhythm 35" (selected)', 'ListItem "Rhythm 35"') \
                  .replace('Button "OK"', 'Button "OK" (disabled)')
    change = diff_snapshots(BEFORE, after)
    deltas = {new.plain: (old.states, new.states) for old, new in change.changed}
    assert deltas['ListItem "Rhythm 35"'] == (frozenset({"selected"}), frozenset())
    assert not change.added and not change.removed
    text = change.render()
    assert '~ [e3] ListItem "Rhythm 35": -selected' in text
    assert '[e4] Button "OK" (disabled): +disabled' in text


def test_value_change_and_removed_dialog():
    old = '[e1] Window "w"\n  [e2] Edit "Name" value="abc"\n  [e3] Window "Confirm"\n    [e4] Button "Yes"\n'
    new = '[e1] Window "w"\n  [e2] Edit "Name" value="abcd"\n'
    change = diff_snapshots(old, new)
    assert [e.plain for e in change.removed] == ['Window "Confirm"', 'Button "Yes"']
    text = change.render()
    assert 'value "abc" -> "abcd"' in text
    assert '- Window "Confirm"' in text


def test_scrolling_is_counted_not_listed():
    old = "\n".join(f'[e{i}] ListItem "Row {i}"' + (" (offscreen)" if i > 3 else "") for i in range(1, 9))
    new = "\n".join(f'[e{i}] ListItem "Row {i}"' + (" (offscreen)" if i <= 4 else "") for i in range(1, 9))
    change = diff_snapshots(old, new)  # rows 5-8 scrolled in, rows 1-3 scrolled out, row 4 stays hidden
    assert (change.into_view, change.out_of_view) == (4, 3)
    assert not change.changed
    assert "4 elements scrolled into view, 3 out of view" in change.render()


def test_window_title_change():
    change = diff_snapshots(BEFORE, BEFORE, "Old", "New")
    assert not change.empty and 'window title: "Old" -> "New"' in change.render()


def test_large_changes_are_capped_and_flagged():
    old = '[e1] Window "w"\n' + "\n".join(f'  [e{i}] Button "Old {i}"' for i in range(2, 42))
    new = '[e1] Window "w"\n' + "\n".join(f'  [e{i}] Button "New {i}"' for i in range(2, 42))
    text = diff_snapshots(old, new).render()
    assert "largely replaced" in text
    assert "and 20 more added" in text and "and 32 more removed" in text
    assert len(text.splitlines()) < 40


def test_recognizer_rows_and_header_count_are_handled():
    old = 'On-screen elements (3):\n[1] button "Save" | [2] button "Cancel"\n[3] input "Name"'
    new = 'On-screen elements (4):\n[1] button "Save" | [2] button "Cancel"\n[3] input "Name"\n[4] label "Saved"'
    change = diff_snapshots(old, new)  # ids and the count header do not register as changes
    assert [e.plain for e in change.added] == ['label "Saved"']
    assert not change.removed and not change.changed


def test_web_events_block_and_context_lines():
    old = 'heading "Login"\n[e1] button "Sign in"'
    new = ('Events since the last step:\n  - dialog "Really?" was accepted\n\n'
           'heading "Login"\nalert "Wrong password"\n[e1] button "Sign in"')
    change = diff_snapshots(old, new)
    assert [e.plain for e in change.added] == ['alert "Wrong password"']  # events are not re-reported
    assert not change.removed


# -- the agent hands the change to the model ---------------------------------
class Perceiver:
    def __init__(self, trees):
        self.trees = list(trees)

    def observe(self):
        return NS(window_title="w", tree_text=self.trees.pop(0), screenshot=None, elements={"e2": 1})


class Executor:
    def dispatch(self, name, inp, elements):
        return "Clicked e2."


class Chain:
    def __init__(self):
        self.tool_texts = []
        self.turns = [AssistantTurn("", ToolCall("1", "click", {"element_id": "e2"}), "tool_use", {}),
                      AssistantTurn("", ToolCall("2", "click", {"element_id": "e2"}), "tool_use", {}),
                      AssistantTurn("", ToolCall("3", "finish", {"status": "passed", "summary": "ok"}),
                                    "tool_use", {})]

    def complete(self, system, messages, tools, log=None):
        if messages[-1]["role"] == "tool":
            self.tool_texts.append(messages[-1]["text"])
        return self.turns.pop(0)


def test_agent_reports_what_changed_after_each_action():
    opened = BEFORE + '  [e5] Pane "Tips"\n'
    chain = Chain()
    agent = TesterAgent(chain, perceiver=Perceiver([BEFORE, opened, opened]), executor=Executor(),
                        verbose=False)
    assert agent.run("task").status == "passed"
    first, second = chain.tool_texts
    assert 'Changes since the previous screen (+1 added)' in first and '+ [e5] Pane "Tips"' in first
    assert "UI tree unchanged" in second  # a repeated click is visibly a no-op
    assert "Updated window" in first and "UI elements:" in first
