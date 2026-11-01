"""A recording the recorder was killed in the middle of carries on after it.

The kill itself is simulated the only way a test can: the sinks are closed and
the note's heartbeat stopped, but the session is never stopped, so
recording.json still says "complete": false and the note stays on disk --
which is exactly what a watchdog SIGABRT leaves behind.  Uses the real ffmpeg,
since the point is that the files of both parts exist side by side.
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from pupilrec import recording
from pupilrec.capture import CameraStats
from pupilrec.recording import RecordingSession, find_resumable
from pupilrec.server import AppState


def tiny_jpeg() -> bytes:
    return subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=red:s=16x16", "-frames:v", "1", "-f", "mjpeg", "pipe:1"],
        check=True, capture_output=True).stdout


class FakeWorker:
    def __init__(self, cam_id, usb_path, role="world"):
        self.cam_id, self.usb_path, self.role = cam_id, usb_path, role
        self.side = cam_id.split("_")[0]
        self.product, self.uid = "Pupil Cam1 ID2", "3:10"
        self.width, self.height, self.fps = 16, 16, 30
        self.stats = CameraStats()
        self.sink = None

    def attach_sink(self, sink):
        self.sink = sink

    def detach_sink(self):
        sink, self.sink = self.sink, None
        return sink


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class ResumeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.jpeg = tiny_jpeg()

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        # ffmpeg complains, rightly, about the parts no frame was fed into.
        quiet = mock.patch.object(recording.logger, "disabled", True)
        quiet.start()
        self.addCleanup(quiet.stop)
        self.note = os.path.join(self.root, "var", "active_recording.json")
        self.recordings = os.path.join(self.root, "recordings")
        os.makedirs(self.recordings)
        self.workers = [FakeWorker("left_world", "3-1.1"),
                        FakeWorker("left_eye", "3-1.3", "eye")]

    def feed(self, n, t0=None):
        t0 = time.time() if t0 is None else t0
        for i in range(n):
            for w in self.workers:
                if w.sink is not None:
                    w.sink.write(recording_frame(self.jpeg, t0 + i / 30, i))

    def killed(self, session):
        """Everything a SIGABRT leaves: data on disk, no stop, the note."""
        for w in self.workers:
            sink = w.detach_sink()
            sink.close()
        session._note._stop.set()
        session._note._thread.join()

    def started_and_killed(self, frames=5):
        session = RecordingSession(self.recordings, self.workers, "teszt",
                                   note_path=self.note, started_by="ipad")
        self.feed(frames)
        self.killed(session)
        return session

    def test_a_killed_recording_is_continued_in_the_same_directory(self):
        first = self.started_and_killed(frames=5)
        resume = find_resumable(self.recordings, self.note)
        self.assertIsNotNone(resume)
        self.assertEqual(resume["directory"], first.directory)
        self.assertEqual(resume["started_by"], "ipad")

        second = RecordingSession(self.recordings, self.workers,
                                  note_path=self.note, resume=resume)
        self.assertEqual(second.segment, 2)
        self.assertEqual(second.name, first.name)
        self.assertGreater(second.elapsed, 0)
        self.feed(3)
        result = second.stop()

        files = set(os.listdir(first.directory))
        for cam in ("left_world", "left_eye"):
            self.assertIn(f"{cam}.mkv", files, "the first part is kept")
            self.assertIn(f"{cam}.part2.mkv", files)
            self.assertIn(f"{cam}.part2.csv", files)
        with open(os.path.join(first.directory, "recording.json")) as fh:
            meta = json.load(fh)
        self.assertTrue(meta["complete"])
        self.assertEqual(meta["interruptions"], 1)
        self.assertEqual([s["ended"] for s in meta["segments"]],
                         ["interrupted", "stopped"])
        self.assertEqual(meta["segments"][0]["results"][0]["frames"], 5)
        frames = {r["camera"]: r["frames"] for r in meta["results"]}
        self.assertEqual(frames, {"left_eye": 8, "left_world": 8})
        self.assertEqual({c["camera"]: c["frames"] for c in result["cameras"]},
                         {"left_eye": 8, "left_world": 8})
        self.assertFalse(os.path.exists(self.note), "a stopped recording clears it")

    def test_it_can_be_interrupted_more_than_once(self):
        self.started_and_killed()
        second = RecordingSession(self.recordings, self.workers, note_path=self.note,
                                  resume=find_resumable(self.recordings, self.note))
        self.feed(2)
        self.killed(second)
        third = RecordingSession(self.recordings, self.workers, note_path=self.note,
                                 resume=find_resumable(self.recordings, self.note))
        self.assertEqual(third.segment, 3)
        third.stop()
        with open(os.path.join(third.directory, "recording.json")) as fh:
            meta = json.load(fh)
        self.assertEqual(meta["interruptions"], 2)
        self.assertEqual(meta["segments"][1]["results"][0]["frames"], 2)

    def test_a_stopped_recording_is_not_resumed(self):
        session = RecordingSession(self.recordings, self.workers, "x",
                                   note_path=self.note)
        session.stop()
        self.assertIsNone(find_resumable(self.recordings, self.note))

    def test_a_note_from_long_ago_is_ignored_and_removed(self):
        self.started_and_killed()
        with mock.patch.object(recording.time, "time",
                               return_value=time.time() + recording.RESUME_WINDOW + 60):
            self.assertIsNone(find_resumable(self.recordings, self.note))
        self.assertFalse(os.path.exists(self.note))

    def test_a_note_pointing_outside_the_recordings_is_ignored(self):
        os.makedirs(os.path.dirname(self.note))
        with open(self.note, "w") as fh:
            json.dump({"directory": "/etc", "alive_unix": time.time()}, fh)
        self.assertIsNone(find_resumable(self.recordings, self.note))

    def test_a_table_cut_off_mid_line_counts_only_whole_rows(self):
        path = os.path.join(self.root, "t.csv")
        with open(path, "w") as fh:
            fh.write(recording.CSV_HEADER)
            fh.write("0,100.5,x,1,0,1,10\n1,100.6,x,1,0,2,10\n2,100.7,x")
        self.assertEqual(recording._table_summary(path), (2, 100.6))

    def test_the_server_resumes_before_anyone_sees_it_stopped(self):
        self.started_and_killed()
        cfg = mock.Mock(recordings_dir=self.recordings, gps_dir=self.root,
                        tiles_dir=self.root)
        with mock.patch("pupilrec.server.TrackStore"), \
             mock.patch("pupilrec.server.TileCache"), \
             mock.patch("pupilrec.server.PreviewScaler"):
            state = AppState(cfg, self.workers, note_path=self.note)
        state.resume_recording(find_resumable(self.recordings, self.note))
        status = state.status()
        self.assertTrue(status["recording"])
        self.assertEqual(status["segment"], 2)
        self.assertEqual(status["started_by"], "ipad")
        state.stop_recording()


def recording_frame(jpeg, unix_time, index):
    from pupilrec.capture import Frame
    return Frame(jpeg=jpeg, unix_time=unix_time, monotonic=time.monotonic(),
                 device_time=index / 30, uvc_index=index, width=16, height=16)


if __name__ == "__main__":
    unittest.main()
