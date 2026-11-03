"""A viewer is not offered a control the server would refuse (owner ruling 2026-11-03).

The page hides what is marked `write`; these tests derive which actions are writes from the server's own role table and fail when a
button for one is not marked. The server still refuses a viewer's write with 403 whatever the page shows (tests/test_access.py).
"""

import re
import unittest

from tests.test_operator_ui import FILES
from volt import serve

JS = FILES["operator.js"]
# Handlers a button reaches by name that are not found by scanning for act( or call( (they go through a local wrapper).
EXTRA_HELPERS = {"detectClick", "saveName", "pacePress"}
# Functions that build a panel or a row: they hold handlers for buttons but are not themselves a handler.
NOT_HANDLERS = {"act", "fmtTime", "queryTerms", "libBackground", "setWorkspace", "findWire", "arcPanel", "tplPanel", "wireRow",
                "rundownRow", "archiveBits", "mosPanel"}


def balanced(src, i, o="(", c=")"):
    depth, quote = 0, None
    while i < len(src):
        ch = src[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif ch == o:
            depth += 1
        elif ch == c:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def write_kinds():
    """The page's POST action kinds whose route needs more than the viewer role, from serve.ROUTES and serve.ROUTE_ROLES."""
    kinds = set()
    for kind, method, tpl in re.findall(r"^\s+(\w+):\s+\['(GET|POST)',\s+'([^']+)'\]", JS, re.M):
        if method != "POST":
            continue
        path = tpl.replace("{id}", "abc123")
        funcs = [fn for pattern, fn in serve.ROUTES if pattern.match(path)]
        assert funcs, f"no server route for {kind} {tpl}"
        if serve.ROUTE_ROLES.get(funcs[0].__name__, "admin") != "viewer":
            kinds.add(kind)
    return kinds


class ViewerControls(unittest.TestCase):
    def test_the_write_set_is_what_the_server_says(self):
        kinds = write_kinds()
        for kind in ("go", "take", "hold", "recordStart", "recordStop", "cleared", "publish", "scriptSave", "storyStatus", "metaSet", "ingest"):
            self.assertIn(kind, kinds)
        for kind in ("commentAdd", "commentDelete", "notifRead", "presence"):
            self.assertNotIn(kind, kinds)       # a viewer may comment, delete their own comment, mark notifications read and say they are looking

    def test_every_button_that_writes_is_marked_write(self):
        kinds = write_kinds()
        direct = re.compile(r"\b(?:act|call|findAct|libAct)\(\s*'(%s)'" % "|".join(sorted(kinds)))
        helpers = set()
        for m in re.finditer(r"function (\w+)\s*\(", JS):
            start = JS.find("{", JS.find(")", m.end()))
            if direct.search(JS[start:balanced(JS, start, "{", "}")]):
                helpers.add(m.group(1))
        helpers = (helpers - NOT_HANDLERS - {h for h in helpers if h.startswith("render")}) | EXTRA_HELPERS
        ref = re.compile(r"\b(?:%s)\b" % "|".join(sorted(helpers)))
        missing = []
        for m in re.finditer(r"\bbtn\(", JS):
            body = JS[m.start():balanced(JS, m.end() - 1) + 1]
            if (direct.search(body) or ref.search(body)) and "write: true" not in body:
                missing.append(JS.count("\n", 0, m.start()) + 1)
        self.assertEqual(missing, [], "buttons for a write action without write: true, at operator.js lines %s" % missing)

    def test_the_page_hides_marked_controls_from_a_viewer_only(self):
        self.assertIn("else if (k === 'write') n.setAttribute('data-write', '')", JS)
        self.assertIn("document.documentElement.setAttribute('data-role', WHO.role)", JS)
        self.assertIn(':root[data-role="viewer"] [data-write]{display:none !important}', FILES["operator.css"])
        self.assertNotIn('data-role="operator"', FILES["operator.css"])
        for marked in ('id="build" data-write', 'id="cue-add" data-write', 'data-write class="panel lib-ingest"', 'id="lib-mark" data-write'):
            self.assertIn(marked, FILES["index.html"])

    def test_a_viewer_reads_stories_without_editing_them(self):
        self.assertIn("if (isViewer()) return 'Your role is viewer", JS)           # the editor is read-only with its reason; Save is hidden
        self.assertIn("if (!id || LS.busy || isViewer()) return;", JS)             # no edit lease is asked for
        self.assertIn("if (isViewer()) return;   // every action here is an operator's", JS)


if __name__ == "__main__":
    unittest.main()
