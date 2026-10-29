# -*- coding: utf-8 -*-
"""Tests de mission_stats.py : python -m unittest tests.test_mission_stats -v"""

import csv
import json
import os
import tempfile
import unittest

import cv2
import numpy as np

from mission_stats import (DigitReader, EndScreenDetector, EndScreenReader, IconLibrary, Session,
                           append_session_csv, format_amount, format_duration, hud_lines, load_layout, load_template,
                           read_history, recover_current, save_current, save_image, save_layout)


class SessionTest(unittest.TestCase):
    def test_duration_excludes_pauses(self):
        s = Session()
        s.start(1000)
        s.pause(1100)
        s.resume(1500)
        self.assertEqual(s.duration(1600), 200)
        self.assertFalse(Session().running)

    def test_record_counts_and_totals(self):
        s = Session()
        s.start(0)
        s.record({"outcome": "completed", "unstable": False, "items": [
            {"key": "gems", "name": "Gemmes", "amount": 199},
            {"key": "exp", "name": "EXP", "amount": None}]})
        s.record({"outcome": "failed", "unstable": False, "items": [
            {"key": "gems", "name": "Gemmes", "amount": 1}]})
        self.assertEqual((s.completed, s.failed), (1, 1))
        self.assertEqual(s.totals, {"Gemmes": 200})

    def test_rename_resource_merges(self):
        s = Session()
        s.totals = {"inconnue_1": 5, "Gemmes": 10}
        s.rename_resource("inconnue_1", "Gemmes")
        self.assertEqual(s.totals, {"Gemmes": 15})

    def test_set_activity_tracks_previous_main(self):
        s = Session()
        s.set_activity("farm", "macro farm")
        s.set_activity("", "rien")
        s.set_activity("heal and retry", "Scenario heal and retry 1/1 - etape 3/8 : ...")
        self.assertEqual(s.previous, "farm")
        self.assertTrue(s.current.startswith("Scenario heal and retry"))
        s.set_activity("heal and retry", "... etape 4/8")
        self.assertEqual(s.previous, "farm")

    def test_should_write(self):
        s = Session()
        s.start(0)
        self.assertFalse(s.should_write(30))
        self.assertTrue(s.should_write(60))
        s2 = Session()
        s2.start(0)
        s2.completed = 1
        self.assertTrue(s2.should_write(5))

    def test_dict_roundtrip(self):
        s = Session()
        s.start(0)
        s.completed = 3
        s.totals = {"EXP": 7}
        t = Session.from_dict(s.to_dict(50))
        self.assertEqual((t.completed, t.totals, t.duration(50)), (3, {"EXP": 7}, 50))


class CsvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.csv = os.path.join(self.dir, "sessions.csv")

    def tearDown(self):
        self.tmp.cleanup()

    def _row(self, start, totals, completed=0):
        s = Session()
        s.start(start)
        s.completed = completed
        s.totals = dict(totals)
        return s.csv_row(start + 120)

    def test_format(self):
        self.assertEqual(format_duration(5025), "1:23:45")
        self.assertEqual(format_duration(5), "0:00:05")
        self.assertEqual(format_amount(2001684), "2 001 684")
        self.assertEqual(format_amount(1), "1")

    def test_append_extends_header(self):
        append_session_csv(self.csv, self._row(0, {"EXP": 10}))
        append_session_csv(self.csv, self._row(1000, {"Gemmes": 5}))
        with open(self.csv, "rb") as f:
            self.assertTrue(f.read().startswith(b"\xef\xbb\xbf"))
        with open(self.csv, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f, delimiter=";"))
        self.assertEqual(rows[0], ["debut", "fin", "duree", "reussies", "ratees", "EXP", "Gemmes"])
        self.assertEqual(rows[1][6], "0")
        self.assertEqual(rows[2][5], "0")
        self.assertEqual(rows[2][6], "5")

    def test_read_history_latest_first_limited(self):
        for i in range(12):
            append_session_csv(self.csv, self._row(i * 1000, {}, completed=i))
        hist = read_history(self.csv, 10)
        self.assertEqual(len(hist), 10)
        self.assertEqual(hist[0]["reussies"], "11")
        self.assertEqual(read_history(os.path.join(self.dir, "absent.csv")), [])

    def test_recover_current_appends_and_deletes(self):
        cur = os.path.join(self.dir, "session_en_cours.json")
        s = Session()
        s.start(0)
        s.completed = 2
        save_current(cur, s, 100)
        self.assertTrue(recover_current(cur, self.csv))
        self.assertFalse(os.path.exists(cur))
        hist = read_history(self.csv)
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["reussies"], "2")
        self.assertFalse(recover_current(cur, self.csv))

    def test_recover_current_keeps_file_if_csv_locked(self):
        cur = os.path.join(self.dir, "session_en_cours.json")
        s = Session()
        s.start(0)
        s.completed = 1
        save_current(cur, s, 100)
        os.mkdir(self.csv)  # ecriture impossible (comme un fichier ouvert dans Excel)
        self.assertFalse(recover_current(cur, self.csv))
        self.assertTrue(os.path.exists(cur))

    def test_recover_current_bad_json(self):
        cur = os.path.join(self.dir, "session_en_cours.json")
        with open(cur, "w") as f:
            f.write("{pas du json")
        self.assertFalse(recover_current(cur, self.csv))
        self.assertFalse(os.path.exists(cur))


A = {"outcome": "completed", "items": [{"key": "g", "name": "G", "amount": 5}]}
B = {"outcome": "completed", "items": [{"key": "g", "name": "G", "amount": 9}]}


def run_detector(frames, step=0.3):
    """Chaque frame est la lecture renvoyee par read_fn (None = pas de titre)."""
    seq = iter(frames)
    det = EndScreenDetector(lambda screen: next(seq))
    out = []
    for i in range(len(frames)):
        r = det.feed(None, i * step)
        if r is not None:
            out.append(r)
    return out


class DetectorTest(unittest.TestCase):
    def test_detector_counts_once(self):
        out = run_detector([None, A, A, A, A, A, None, None, None, None, None, None])
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0]["unstable"])

    def test_detector_waits_for_stable_reading(self):
        out = run_detector([B, A, A, A, A, A])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["items"][0]["amount"], 5)

    def test_detector_lets_end_screen_settle(self):
        # Le titre apparait avant que les cases aient fini de s'afficher : pas de comptage avant 1 s.
        empty = {"outcome": "completed", "items": []}
        out = run_detector([empty, empty, empty, A, A, A, A])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["items"][0]["amount"], 5)

    def test_detector_unstable_after_timeout(self):
        out = run_detector([A, B] * 6)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["unstable"])

    def test_detector_title_gone_after_single_read(self):
        out = run_detector([None, A, None, None])
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["unstable"])

    def test_detector_counts_two_identical_missions(self):
        out = run_detector([A] * 5 + [None] * 6 + [A] * 5)
        self.assertEqual(len(out), 2)
        self.assertFalse(out[1]["unstable"])

    def test_detector_short_gap_does_not_rearm(self):
        out = run_detector([A] * 5 + [None, None] + [A] * 5)
        self.assertEqual(len(out), 1)


def render(text, scale=0.5):
    """Texte blanc sur fond noir, 30 px de haut (pour imiter la zone de nombre d'une case)."""
    (w, h), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
    img = np.zeros((30, w + 8, 3), np.uint8)
    cv2.putText(img, text, (4, 30 - 8), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 1, cv2.LINE_8)
    return img


class DigitReaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def trained(self):
        r = DigitReader(self.dir)
        r.learn(render("1234567890"), 1234567890)
        return r

    def test_learn_then_read(self):
        r = self.trained()
        self.assertEqual(r.missing(), [])
        self.assertEqual(r.read(render("907")), 907)
        self.assertEqual(r.read(render("32054")), 32054)

    def test_read_ignores_commas(self):
        self.assertEqual(self.trained().read(render("166,807")), 166807)

    def test_read_single_digit(self):
        self.assertEqual(self.trained().read(render("1")), 1)

    def test_missing_digits_reported(self):
        r = DigitReader(self.dir)
        r.learn(render("123"), 123)
        self.assertEqual(r.missing(), list("0456789"))

    def test_partial_calibration_reads_nothing(self):
        r = DigitReader(self.dir)
        r.learn(render("123456780"), 123456780)
        self.assertIsNone(r.read(render("12")))

    def test_unknown_glyph_is_unreadable(self):
        r = self.trained()
        self.assertIsNone(r.read(render("12X4")))
        self.assertIsNone(r.read(np.zeros((30, 40, 3), np.uint8)))

    def test_learn_rejects_count_mismatch(self):
        with self.assertRaises(ValueError):
            DigitReader(self.dir).learn(render("12"), 123)

    def test_variant_only_if_different(self):
        r = DigitReader(self.dir)
        r.learn(render("11"), 11)
        r.learn(render("11"), 11)
        self.assertEqual([f for f in os.listdir(self.dir) if f.startswith("1")], ["1.png"])

    def test_persisted_between_instances(self):
        self.trained()
        self.assertEqual(DigitReader(self.dir).read(render("4096")), 4096)


def icon(seed, size=40):
    """Icone factice : blocs de couleurs aleatoires (assez de structure pour la correlation)."""
    rng = np.random.default_rng(seed)
    small = rng.integers(0, 256, (8, 8, 3), dtype=np.uint8)
    return cv2.resize(small, (size, size), interpolation=cv2.INTER_NEAREST)


class IconLibraryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.icons = os.path.join(self.tmp.name, "icons")
        self.names = os.path.join(self.tmp.name, "icons.json")

    def tearDown(self):
        self.tmp.cleanup()

    def lib(self):
        return IconLibrary(self.icons, self.names)

    def test_identify_known_after_rename(self):
        lib = self.lib()
        k = lib.add_unknown(icon(1))
        self.assertEqual(lib.rename(k, "Gemmes"), k)
        key, name, score = lib.identify(icon(1))
        self.assertEqual((key, name), (k, "Gemmes"))
        self.assertGreater(score, 0.99)

    def test_identify_unknown(self):
        lib = self.lib()
        lib.add_unknown(icon(1))
        self.assertEqual(lib.identify(icon(2))[:2], (None, None))
        self.assertEqual(self.lib().identify(icon(2))[:2], (None, None))

    def test_identify_tolerates_size_difference(self):
        lib = self.lib()
        k = lib.add_unknown(icon(1))
        self.assertEqual(lib.identify(cv2.resize(icon(1), (42, 41)))[0], k)

    def test_add_unknown_not_duplicated(self):
        lib = self.lib()
        self.assertEqual(lib.add_unknown(icon(1)), "inconnue_1")
        self.assertEqual(lib.identify(icon(1))[0], "inconnue_1")
        self.assertEqual(lib.add_unknown(icon(2)), "inconnue_2")

    def test_names_persisted_and_delete(self):
        lib = self.lib()
        k = lib.add_unknown(icon(1))
        lib.rename(k, "EXP")
        self.assertEqual(self.lib().identify(icon(1))[1], "EXP")
        lib.delete(k)
        self.assertFalse(os.path.exists(os.path.join(self.icons, k + ".png")))
        self.assertEqual(self.lib().items(), [])


FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
SLOT = 80


def label(text, color=(80, 200, 240), scale=0.9):
    (w, h), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_DUPLEX, scale, 2)
    img = np.full((h + base + 12, w + 12, 3), 25, np.uint8)
    cv2.putText(img, text, (6, h + 6), cv2.FONT_HERSHEY_DUPLEX, scale, color, 2, cv2.LINE_AA)
    return img


TITLE_OK = label("MISSION COMPLETED")
TITLE_FAIL = label("MISSION FAILED", color=(60, 60, 220))
OBTAINED = label("OBTAINED", scale=0.7)


def paste(screen, img, x, y):
    screen[y:y + img.shape[0], x:x + img.shape[1]] = img


def end_screen(title=TITLE_OK, slots=((1, "32054"), (2, "199"), (3, "1")), extra_big_slot=False):
    """Ecran 1280x720 : titre, en-tete OBTAINED, cases SLOT x SLOT (icone en haut, nombre en bas a droite)."""
    screen = np.full((720, 1280, 3), 30, np.uint8)
    if title is not None:
        paste(screen, title, 420, 40)
    paste(screen, OBTAINED, 560, 200)
    y = 200 + OBTAINED.shape[0] + 20
    colors = [(40, 180, 230), (60, 200, 60), (200, 60, 160), (220, 120, 40)]
    for n, (seed, number) in enumerate(slots):
        x = 480 + n * (SLOT + 16)
        cv2.rectangle(screen, (x, y), (x + SLOT - 1, y + SLOT - 1), colors[n % 4], 2)
        paste(screen, icon(seed, 36), x + 22, y + 6)
        (tw, th), _ = cv2.getTextSize(number, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.putText(screen, number, (x + SLOT - 6 - tw, y + SLOT - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (255, 255, 255), 1, cv2.LINE_8)
    if extra_big_slot:
        cv2.rectangle(screen, (900, y - 10), (900 + 119, y + 109), (40, 180, 230), 2)
    return screen


class EndScreenReaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stats = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def reader(self, titles=True, slot=True, digits=True):
        os.makedirs(os.path.join(self.stats, "titles"), exist_ok=True)
        if titles:
            for name, img in (("completed.png", TITLE_OK), ("failed.png", TITLE_FAIL), ("obtained.png", OBTAINED)):
                save_image(os.path.join(self.stats, "titles", name), img)
        if slot:
            layout = load_layout(self.stats)
            layout["slot_w"] = layout["slot_h"] = SLOT
            save_layout(self.stats, layout)
        d = DigitReader(os.path.join(self.stats, "digits"))
        if digits:
            d.learn(render("1234567890"), 1234567890)
        icons = IconLibrary(os.path.join(self.stats, "icons"), os.path.join(self.stats, "icons.json"))
        return EndScreenReader(self.stats, icons, d)

    def test_outcome(self):
        r = self.reader()
        self.assertEqual(r.outcome(end_screen()), "completed")
        self.assertEqual(r.outcome(end_screen(title=TITLE_FAIL)), "failed")
        self.assertIsNone(r.outcome(np.full((720, 1280, 3), 30, np.uint8)))

    def test_find_slots(self):
        r = self.reader()
        slots = r.find_slots(end_screen(extra_big_slot=True))
        self.assertEqual(len(slots), 3)
        self.assertEqual([s[0] for s in slots], sorted(s[0] for s in slots))
        for x, y, w, h in slots:
            self.assertLessEqual(abs(w - SLOT), SLOT * 0.1)

    def test_read_full(self):
        r = self.reader()
        first = r.read(end_screen())
        self.assertEqual([it["key"] for it in first["items"]], [None, None, None])
        for n, name in ((0, "EXP"), (1, "Gemmes")):
            r.icons.rename(r.icons.add_unknown(first["items"][n]["icon"]), name)
        res = r.read(end_screen())
        self.assertEqual(res["outcome"], "completed")
        self.assertEqual([it["amount"] for it in res["items"]], [32054, 199, 1])
        self.assertEqual([it["name"] for it in res["items"]], ["EXP", "Gemmes", None])

    def test_read_without_title(self):
        self.assertIsNone(self.reader().read(end_screen(title=None)))

    def test_read_without_obtained(self):
        r = self.reader()
        screen = end_screen()
        screen[190:, :] = 30
        self.assertEqual(r.read(screen)["items"], [])

    def test_read_without_digits_gives_none_amounts(self):
        res = self.reader(digits=False).read(end_screen())
        self.assertEqual([it["amount"] for it in res["items"]], [None, None, None])

    def test_missing(self):
        r = self.reader(titles=False, slot=False, digits=False)
        self.assertEqual(r.missing(), ["titre reussi", "titre rate", "en-tete OBTAINED", "taille de case", "chiffres"])
        self.assertEqual(self.reader().missing(), [])

    @unittest.skipUnless(os.path.exists(os.path.join(FIXTURES, "expected.json")), "captures reelles absentes")
    def test_real_screens(self):
        stats = os.path.join(FIXTURES, "stats")
        r = EndScreenReader(stats, IconLibrary(os.path.join(stats, "icons"), os.path.join(stats, "icons.json")),
                            DigitReader(os.path.join(stats, "digits")))
        with open(os.path.join(FIXTURES, "expected.json"), encoding="utf-8") as f:
            cases = json.load(f)
        for case in cases:
            res = r.read(load_template(os.path.join(FIXTURES, case["file"])))
            self.assertEqual(res["outcome"], case["outcome"], case["file"])
            self.assertEqual([it["amount"] for it in res["items"]], case["amounts"], case["file"])


class HudLinesTest(unittest.TestCase):
    def test_hud_lines(self):
        s = Session()
        s.start(0)
        s.completed, s.failed = 12, 2
        s.totals = {"EXP": 384648, "Gemmes": 2388}
        s.set_activity("farm", "macro farm")
        s.set_activity("heal and retry", "Scenario heal and retry - etape 3/8")
        self.assertEqual(hud_lines(s, 5025), [
            "⏱ 1:23:45",
            "En cours : Scenario heal and retry - etape 3/8",
            "Avant    : farm",
            "Missions : ✔ 12   ✖ 2",
            "EXP 384 648 · Gemmes 2 388"])
        s.pause(5025)
        self.assertEqual(hud_lines(s, 9999)[0], "⏱ 1:23:45  (en pause)")

    def test_hud_lines_wraps_and_defaults(self):
        s = Session()
        s.start(0)
        self.assertEqual(hud_lines(s, 0)[2], "Avant    : -")
        self.assertEqual(len(hud_lines(s, 0)), 4)
        s.totals = {"A": 1, "B": 2, "C": 3}
        self.assertEqual(hud_lines(s, 0)[4:], ["A 1 · B 2", "C 3"])


if __name__ == "__main__":
    unittest.main()
