import unittest
from fractions import Fraction

from volt.timecode import (TimecodeError, frames_to_seconds, frames_to_tc, is_drop, nominal_fps, parse_rate,
                           seconds_to_frames, seconds_to_tc, tc_to_frames, tc_to_seconds, tc_to_seconds_exact)

NTSC = Fraction(30000, 1001)
NTSC60 = Fraction(60000, 1001)


class Rates(unittest.TestCase):
    def test_strings_and_decimals_become_exact_ratios(self):
        self.assertEqual(parse_rate("30000/1001"), NTSC)
        self.assertEqual(parse_rate(29.97), NTSC)
        self.assertEqual(parse_rate("59.94"), NTSC60)
        self.assertEqual(parse_rate(23.976), Fraction(24000, 1001))
        self.assertEqual(parse_rate(25), 25)
        self.assertEqual(parse_rate(25.0), 25)
        self.assertEqual(parse_rate("25"), 25)

    def test_bad_rates(self):
        for bad in ("abc", 0, -25, "1/0", 12.5, None):
            with self.assertRaises(TimecodeError, msg=repr(bad)):
                parse_rate(bad)

    def test_nominal(self):
        self.assertEqual([nominal_fps(r) for r in ("25", "30000/1001", "60000/1001", "24000/1001")], [25, 30, 60, 24])


class NonDrop(unittest.TestCase):
    def test_known_values(self):
        self.assertEqual(tc_to_frames("00:00:01:12", 25), 37)
        self.assertEqual(tc_to_frames("01:00:00:00", 25), 90000)
        self.assertEqual(frames_to_tc(90000 + 37, 25), "01:00:01:12")
        self.assertAlmostEqual(tc_to_seconds("00:00:01:12", 25), 1.48)

    def test_ntsc_non_drop_counts_30_but_runs_at_30000_1001(self):
        self.assertEqual(tc_to_frames("00:00:01:00", NTSC), 30)
        self.assertEqual(frames_to_seconds(30, NTSC), Fraction(1001, 1000))
        self.assertEqual(tc_to_frames("00:01:00:00", NTSC, drop=False), 1800)

    def test_either_separator_on_a_non_drop_clip(self):
        self.assertEqual(tc_to_frames("00:01:00;02", NTSC, drop=False), 1802)

    def test_bad_labels(self):
        for bad in ("nope", "1:00:00:00", "00:60:00:00", "00:00:60:00", "00:00:00:25", "00:00:00:-1", ""):
            with self.assertRaises(TimecodeError, msg=bad):
                tc_to_frames(bad, 25)

    def test_negative_frames(self):
        with self.assertRaises(TimecodeError):
            frames_to_tc(-1, 25)

    def test_seconds_round_trip(self):
        for f in (0, 1, 24, 25, 12345, 90000):
            self.assertEqual(seconds_to_frames(frames_to_seconds(f, 25), 25), f)
            self.assertEqual(tc_to_frames(seconds_to_tc(float(frames_to_seconds(f, 25)), 25), 25), f)


class DropFrame(unittest.TestCase):
    def test_29_97_known_boundaries(self):
        cases = {0: "00:00:00;00", 1799: "00:00:59;29", 1800: "00:01:00;02", 3597: "00:01:59;29",
                 3598: "00:02:00;02", 17981: "00:09:59;29", 17982: "00:10:00;00", 17983: "00:10:00;01",
                 107892: "01:00:00;00", 107892 * 24: "24:00:00;00"}
        for frames, label in cases.items():
            self.assertEqual(frames_to_tc(frames, NTSC, drop=True), label, frames)
            self.assertEqual(tc_to_frames(label, NTSC), frames, label)

    def test_59_94_known_boundaries(self):
        cases = {0: "00:00:00;00", 3599: "00:00:59;59", 3600: "00:01:00;04", 35963: "00:09:59;59",
                 35964: "00:10:00;00", 215784: "01:00:00;00"}
        for frames, label in cases.items():
            self.assertEqual(frames_to_tc(frames, NTSC60, drop=True), label, frames)
            self.assertEqual(tc_to_frames(label, NTSC60), frames, label)

    def test_illegal_labels_raise(self):
        for bad in ("00:01:00;00", "00:01:00;01", "00:02:00;01", "00:59:00;00", "01:09:00;01"):
            with self.assertRaises(TimecodeError, msg=bad):
                tc_to_frames(bad, NTSC)
        for bad in ("00:01:00;00", "00:01:00;03"):
            with self.assertRaises(TimecodeError, msg=bad):
                tc_to_frames(bad, NTSC60)

    def test_legal_neighbours(self):
        for good in ("00:00:00;00", "00:00:00;01", "00:10:00;00", "00:20:00;01", "00:01:01;00", "00:01:00;02"):
            tc_to_frames(good, NTSC)

    def test_drop_frame_has_no_form_at_other_rates(self):
        for rate in (25, Fraction(24000, 1001), 50, 24):
            with self.assertRaises(TimecodeError, msg=str(rate)):
                frames_to_tc(100, rate, drop=True)
            with self.assertRaises(TimecodeError, msg=str(rate)):
                tc_to_frames("00:00:01;00", rate)

    def test_separator_marks_drop(self):
        self.assertTrue(is_drop("01:00:00;00"))
        self.assertFalse(is_drop("01:00:00:00"))
        self.assertFalse(is_drop(None))
        self.assertFalse(is_drop("garbage"))

    def test_one_drop_hour_is_3600_seconds_of_wall_clock_to_within_a_frame(self):
        secs = tc_to_seconds_exact("01:00:00;00", NTSC)
        self.assertLess(abs(secs - 3600), Fraction(1, 30))  # non-drop would be 3603.6
        self.assertLess(abs(tc_to_seconds_exact("01:00:00:00", NTSC, drop=False) - 3600), Fraction(4))

    def test_seconds_are_exact_not_float(self):
        self.assertEqual(tc_to_seconds_exact("00:00:01;00", NTSC), Fraction(30 * 1001, 30000))
        self.assertEqual(frames_to_seconds(30000, NTSC), 1001)  # 30000 frames = exactly 1001 s, no drift

    def test_round_trip_over_a_full_day_29_97(self):
        # every frame of 24 h: frames -> label -> frames, labels strictly increasing and never illegal
        day = 107892 * 24
        prev = ""
        for n in range(day):
            label = frames_to_tc(n, NTSC, drop=True)
            if tc_to_frames(label, NTSC) != n or label <= prev:
                self.fail(f"frame {n} -> {label} (prev {prev})")
            prev = label

    def test_round_trip_over_a_full_day_59_94(self):
        day = 215784 * 24
        prev = ""
        for n in range(0, day):
            label = frames_to_tc(n, NTSC60, drop=True)
            if tc_to_frames(label, NTSC60) != n or label <= prev:
                self.fail(f"frame {n} -> {label} (prev {prev})")
            prev = label

    def test_round_trip_over_a_full_day_non_drop_25(self):
        for n in range(0, 25 * 86400, 7):
            self.assertEqual(tc_to_frames(frames_to_tc(n, 25), 25), n)

    def test_every_legal_label_in_a_day_maps_once(self):
        # label -> frames -> label over the label space of one hour, skipping the labels that do not exist
        seen = set()
        for m in range(60):
            for s in range(60):
                for f in range(30):
                    label = f"01:{m:02d}:{s:02d};{f:02d}"
                    if s == 0 and f < 2 and m % 10:
                        with self.assertRaises(TimecodeError):
                            tc_to_frames(label, NTSC)
                        continue
                    n = tc_to_frames(label, NTSC)
                    self.assertEqual(frames_to_tc(n, NTSC, drop=True), label)
                    seen.add(n)
        self.assertEqual(len(seen), 3600 * 30 - 2 * 54)  # 108000 labels less 54 dropped minutes x 2
        self.assertEqual(max(seen) - min(seen) + 1, len(seen))  # contiguous: no gaps, no repeats


if __name__ == "__main__":
    unittest.main()
