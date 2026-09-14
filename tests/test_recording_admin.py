"""Renaming and deleting stored recordings.

These are the two operations in the whole program that destroy data, and both
take a name straight from a browser.  So what is checked here is mostly what
must *not* happen: a name that escapes the recordings directory, a recording
that is still being written, a rename that quietly eats another recording.
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from pupilrec.recording import join_name, safe_label, split_name
from pupilrec import recording
from pupilrec.server import AppState


class NameRuleTest(unittest.TestCase):
    """One rule for what a recording may be called, used when it is created
    and when it is renamed, so the two can never disagree."""

    def test_a_label_keeps_letters_digits_dash_and_underscore(self):
        self.assertEqual(safe_label("teszt-2_A"), "teszt-2_A")

    def test_accented_letters_survive(self):
        self.assertEqual(safe_label("kétkamerás"), "kétkamerás")

    def test_spaces_become_underscores_rather_than_vanishing(self):
        self.assertEqual(safe_label("két kamera"), "két_kamera")
        self.assertEqual(safe_label("  sok   szóköz "), "sok_szóköz")

    def test_anything_that_could_change_the_path_is_dropped(self):
        self.assertEqual(safe_label("../../etc/passwd"), "etcpasswd")
        self.assertEqual(safe_label("a/b"), "ab")
        self.assertEqual(safe_label("."), "")

    def test_a_label_is_bounded(self):
        self.assertEqual(len(safe_label("x" * 200)), 40)

    def test_a_name_splits_into_timestamp_and_label(self):
        self.assertEqual(split_name("2026-09-09_00-17-41_szenzor"),
                         ("2026-09-09_00-17-41", "szenzor"))

    def test_a_name_with_no_label_splits_cleanly(self):
        self.assertEqual(split_name("2026-09-08_20-55-28"),
                         ("2026-09-08_20-55-28", ""))

    def test_a_directory_that_is_not_ours_keeps_its_whole_name(self):
        self.assertEqual(split_name("valami_random"), ("", "valami_random"))

    def test_dropping_the_label_leaves_the_timestamp_alone(self):
        self.assertEqual(join_name("2026-09-09_00-17-41", ""),
                         "2026-09-09_00-17-41")


class AdminTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        cfg = mock.Mock(recordings_dir=self.root, gps_dir=self.root,
                        tiles_dir=self.root)
        with mock.patch("pupilrec.server.TrackStore"), \
             mock.patch("pupilrec.server.TileCache"):
            self.state = AppState(cfg, [])

    def make(self, name, size=3, meta=True):
        path = os.path.join(self.root, name)
        os.makedirs(path)
        with open(os.path.join(path, "left_world.mkv"), "wb") as fh:
            fh.write(b"x" * size)
        if meta:
            with open(os.path.join(path, "recording.json"), "w") as fh:
                json.dump({"name": name, "complete": True}, fh)
        return path

    def exists(self, name):
        return os.path.isdir(os.path.join(self.root, name))

    # -- renaming --------------------------------------------------------

    def test_renaming_keeps_the_timestamp(self):
        self.make("2026-09-09_00-17-41_regi")
        out = self.state.rename_recording("2026-09-09_00-17-41_regi", "új név")
        self.assertEqual(out["name"], "2026-09-09_00-17-41_új_név")
        self.assertTrue(self.exists("2026-09-09_00-17-41_új_név"))
        self.assertFalse(self.exists("2026-09-09_00-17-41_regi"))

    def test_the_metadata_is_kept_in_step_with_the_directory(self):
        self.make("2026-09-09_00-17-41_regi")
        out = self.state.rename_recording("2026-09-09_00-17-41_regi", "uj")
        self.assertTrue(out["metadata_updated"])
        with open(os.path.join(self.root, out["name"], "recording.json")) as fh:
            self.assertEqual(json.load(fh)["name"], out["name"])

    def test_a_missing_metadata_file_is_reported_not_fatal(self):
        """The directory move is the real change; the copy inside is a nicety."""
        self.make("2026-09-09_00-17-41_regi", meta=False)
        with self.assertLogs("pupilrec.server", "WARNING"):
            out = self.state.rename_recording("2026-09-09_00-17-41_regi", "uj")
        self.assertFalse(out["metadata_updated"])
        self.assertTrue(self.exists(out["name"]))

    def test_an_empty_label_leaves_just_the_timestamp(self):
        self.make("2026-09-09_00-17-41_regi")
        out = self.state.rename_recording("2026-09-09_00-17-41_regi", "")
        self.assertEqual(out["name"], "2026-09-09_00-17-41")

    def test_renaming_to_the_same_name_is_not_an_error(self):
        self.make("2026-09-09_00-17-41_regi")
        out = self.state.rename_recording("2026-09-09_00-17-41_regi", "regi")
        self.assertEqual(out["name"], out["was"])
        self.assertTrue(self.exists("2026-09-09_00-17-41_regi"))

    def test_a_rename_never_swallows_another_recording(self):
        self.make("2026-09-09_00-17-41_egy")
        self.make("2026-09-09_00-17-41_ketto")
        with self.assertRaises(ValueError):
            self.state.rename_recording("2026-09-09_00-17-41_egy", "ketto")
        self.assertTrue(self.exists("2026-09-09_00-17-41_egy"))
        self.assertTrue(self.exists("2026-09-09_00-17-41_ketto"))

    def test_a_label_of_only_punctuation_on_a_nameless_directory_is_refused(self):
        """Nothing may end up with no name at all."""
        self.make("kezimappa")
        with self.assertRaises(ValueError):
            self.state.rename_recording("kezimappa", "///")

    # -- deleting --------------------------------------------------------

    def delete(self, name):
        """Deleting leaves an audit line; that is part of the behaviour."""
        with self.assertLogs("pupilrec.server", "WARNING") as logs:
            out = self.state.delete_recording(name)
        self.assertIn(name, "".join(logs.output))
        return out

    def test_deleting_removes_the_directory_and_reports_the_space(self):
        self.make("2026-09-09_00-17-41_regi", size=1000)
        out = self.delete("2026-09-09_00-17-41_regi")
        self.assertFalse(self.exists("2026-09-09_00-17-41_regi"))
        self.assertGreaterEqual(out["freed_bytes"], 1000)

    def test_deleting_forgets_a_result_that_pointed_at_it(self):
        """The UI must not go on reporting a recording that is gone."""
        self.make("2026-09-09_00-17-41_regi")
        self.state.last_result = {"name": "2026-09-09_00-17-41_regi"}
        self.delete("2026-09-09_00-17-41_regi")
        self.assertIsNone(self.state.last_result)

    def test_an_unrelated_result_is_left_alone(self):
        self.make("2026-09-09_00-17-41_regi")
        self.state.last_result = {"name": "masik"}
        self.delete("2026-09-09_00-17-41_regi")
        self.assertEqual(self.state.last_result, {"name": "masik"})

    # -- what must not happen --------------------------------------------

    def test_a_running_recording_cannot_be_deleted(self):
        self.make("2026-09-09_00-17-41_most")
        self.state.session = mock.Mock(name_="x")
        self.state.session.name = "2026-09-09_00-17-41_most"
        with self.assertRaises(ValueError):
            self.state.delete_recording("2026-09-09_00-17-41_most")
        self.assertTrue(self.exists("2026-09-09_00-17-41_most"))

    def test_a_running_recording_cannot_be_renamed(self):
        self.make("2026-09-09_00-17-41_most")
        self.state.session = mock.Mock()
        self.state.session.name = "2026-09-09_00-17-41_most"
        with self.assertRaises(ValueError):
            self.state.rename_recording("2026-09-09_00-17-41_most", "uj")
        self.assertTrue(self.exists("2026-09-09_00-17-41_most"))

    def test_a_name_cannot_climb_out_of_the_recordings_directory(self):
        outside = os.path.join(self.root, "..", "outside")
        os.makedirs(outside, exist_ok=True)
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        for attempt in ("../outside", "..", ".", "/etc", "a/b", ""):
            with self.subTest(attempt=attempt), self.assertRaises(LookupError):
                self.state.delete_recording(attempt)
        self.assertTrue(os.path.isdir(outside))

    def test_a_symlink_is_not_a_way_out(self):
        """A link planted in the recordings directory must not be followed."""
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        os.symlink(outside, os.path.join(self.root, "escape"))
        with self.assertRaises(LookupError):
            self.state.delete_recording("escape")
        self.assertTrue(os.path.isdir(outside))

    def test_a_recording_that_is_not_there_is_reported_as_missing(self):
        with self.assertRaises(LookupError):
            self.state.rename_recording("nincs-ilyen", "uj")

    def test_a_file_is_not_a_recording(self):
        with open(os.path.join(self.root, "notes.txt"), "w") as fh:
            fh.write("hello")
        with self.assertRaises(LookupError):
            self.state.delete_recording("notes.txt")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class FfmpegMissingTest(unittest.TestCase):
    """A recorder that cannot record must say so before someone presses record."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        # Deliberately a path that does not exist yet: a refused start must not
        # be the thing that creates it.
        self.recordings = os.path.join(self.root, "recordings")
        cfg = mock.Mock(recordings_dir=self.recordings, gps_dir=self.root,
                        tiles_dir=self.root)
        with mock.patch("pupilrec.server.TrackStore"), \
             mock.patch("pupilrec.server.TileCache"):
            self.state = AppState(cfg, [])

    def test_the_path_is_empty_when_ffmpeg_is_not_installed(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(recording.ffmpeg_path(), "")

    def test_starting_is_refused_with_a_reason(self):
        with mock.patch("pupilrec.server.ffmpeg_path", return_value=""):
            with self.assertRaises(RuntimeError) as caught:
                self.state.start_recording("proba")
        self.assertIn("ffmpeg", str(caught.exception))

    def test_a_refused_start_leaves_no_directory_behind(self):
        with mock.patch("pupilrec.server.ffmpeg_path", return_value=""):
            with self.assertRaises(RuntimeError):
                self.state.start_recording("proba")
        self.assertFalse(os.path.exists(self.recordings))

    def test_a_refused_start_is_not_mistaken_for_a_running_one(self):
        with mock.patch("pupilrec.server.ffmpeg_path", return_value=""):
            with self.assertRaises(RuntimeError):
                self.state.start_recording("proba")
        self.assertIsNone(self.state.session)
        self.assertEqual(self.state.status()["recording"], False)
