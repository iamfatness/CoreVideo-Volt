"""Race 2: kill racing go (volt/rundown.py kill, volt/playout.py advance, serve act_kill)."""

import unittest
from unittest import mock

from volt import playout, serve
from volt.playout import advance, go
from volt.rundown import add_item, attach, items, kill, open_show

from tests.racekit import Bg, PgBase, SqliteBase, _cleared_span


# ---- race 2: kill racing go ---------------------------------------------------------------------

class KillRaceTests:
    def _show(self):
        self.a, self.b = self.new_store(), self.new_store()
        asset, span = _cleared_span(self.a)
        self.show = open_show(self.a, "Final Four")
        self.first = add_item(self.a, self.show, "first")
        self.second = add_item(self.a, self.show, "second")
        for item in (self.first, self.second):
            attach(self.a, item, span)
        go(self.a, self.show)                       # first is on air; second is next

    def _status(self, item):
        return self.a.conn.execute("SELECT status FROM item WHERE id = ?", (item,)).fetchone()["status"]

    def test_the_operators_kill_loses_when_a_go_took_the_item_after_the_check(self):
        """act_kill checks the item is ready, then kills it. A go on another connection lands in between."""
        self._show()
        real = serve.kill

        def go_lands_first(store, item_id):
            go(self.b, self.show)                   # second goes on air after act_kill looked, before it wrote
            return real(store, item_id)

        with mock.patch.object(serve, "kill", go_lands_first):
            with self.assertRaises(serve.ApiError) as raised:
                serve.act_kill(self.a, self.second, {})
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self._status(self.second), "on air", "an item that went on air was killed")

    def test_a_go_that_read_the_item_ready_cannot_put_a_killed_item_on_air(self):
        """go reads second as the next ready item; a kill on another connection arrives before go writes."""
        self._show()
        outcome = {"bg": self.in_thread(lambda st: kill(st, self.second))}
        real_take = playout.take

        def kill_arrives(store, item_id):
            outcome["bg"].wait(1.0)               # blocked behind go's lock, or already committed
            return real_take(store, item_id)

        with mock.patch.object(playout, "take", kill_arrives):
            adv = advance(self.a, self.show)
        killed = outcome["bg"].finish()
        status = self._status(self.second)
        if adv.target == self.second:               # go won: the kill has to say so, and the item stays on air
            self.assertIs(killed, False, "kill reported success for an item that went on air")
            self.assertEqual(status, "on air")
        else:                                       # kill won: go must have passed the item over
            self.assertTrue(killed)
            self.assertEqual(status, "skipped")

    def test_kill_refuses_an_item_that_is_already_on_air(self):
        self._show()
        self.assertIs(kill(self.b, self.first), False)
        self.assertEqual(self._status(self.first), "on air")
        self.assertIs(kill(self.b, self.second), True)
        self.assertIs(kill(self.b, self.second), False)   # already killed: not a second success
        self.assertEqual([i.status for i in items(self.a, self.show)], ["on air", "killed"])


class KillRaceSqlite(KillRaceTests, SqliteBase):
    pass


class KillRacePg(KillRaceTests, PgBase):
    pass



if __name__ == "__main__":
    unittest.main()
