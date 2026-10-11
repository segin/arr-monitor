"""Tests for arr-monitor.py

Run from the repository root with: python3 -m unittest discover tests
"""

import importlib.util
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "arr-monitor.py"
spec = importlib.util.spec_from_file_location("arr_monitor", SCRIPT)
am = importlib.util.module_from_spec(spec)
spec.loader.exec_module(am)

GIB = 1024 ** 3


class EpisodeInfoTests(unittest.TestCase):
    def test_common_patterns(self):
        self.assertEqual(am.extract_episode_info("Show.S01E05.mkv"), (1, 5))
        self.assertEqual(am.extract_episode_info("Show - 2x13 - Title.mkv"), (2, 13))
        self.assertEqual(am.extract_episode_info("Show Season 3 Episode 7.mkv"), (3, 7))

    def test_resolution_is_not_an_episode(self):
        self.assertIsNone(am.extract_episode_info("Movie.2019.1920x1080.mkv"))
        self.assertIsNone(am.extract_episode_info("Movie.1280x720.x264.mkv"))


class MatchingTests(unittest.TestCase):
    def test_exact_name_picks_the_right_file_among_equal_sizes(self):
        reads = [am.ReadFileInfo(100, "/dl/a/ep.mkv"), am.ReadFileInfo(100, "/dl/b/other.mkv")]
        self.assertEqual(am._match_by_exact_name("/tv/other.mkv", 10, reads).path, "/dl/b/other.mkv")

    def test_exact_name_ignores_the_destination_itself(self):
        reads = [am.ReadFileInfo(100, "/tv/ep.mkv")]
        self.assertIsNone(am._match_by_exact_name("/tv/ep.mkv", 10, reads))

    def test_episode_match_prefers_same_extension(self):
        reads = [am.ReadFileInfo(5, "/dl/Show.S01E05.srt"), am.ReadFileInfo(900, "/dl/Show.S01E05.mkv")]
        match = am._match_by_episode_pattern("/tv/Show - S01E05 - Title.mkv", reads, {})
        self.assertEqual(match.path, "/dl/Show.S01E05.mkv")

    def test_sole_candidate_needs_exactly_one(self):
        one = [am.ReadFileInfo(50 * GIB, "/dl/A.2019.mkv")]
        self.assertEqual(am._match_sole_candidate("/movies/A (2019).mkv", 0, one).path, "/dl/A.2019.mkv")
        two = one + [am.ReadFileInfo(2 * GIB, "/dl/B.2020.mkv")]
        self.assertIsNone(am._match_sole_candidate("/movies/B (2020).mkv", 0, two))

    def test_sole_candidate_needs_same_extension(self):
        reads = [am.ReadFileInfo(50 * GIB, "/dl/A.2019.mkv")]
        self.assertIsNone(am._match_sole_candidate("/movies/poster.jpg", 0, reads))


class FileTransferInfoTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        patcher = mock.patch.object(am.time, "monotonic", lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_speed_and_eta(self):
        info = am.FileTransferInfo("3", "/tv/ep.mkv", 0, 1000)
        self.now += 1
        info.update(100)
        self.assertEqual(info.speed, 100)
        self.assertEqual(info.eta_seconds, 9)
        self.assertEqual(info.percent, 10)

    def test_speed_drops_to_zero_when_stalled(self):
        info = am.FileTransferInfo("3", "/tv/ep.mkv", 0, 1000)
        self.now += 1
        info.update(100)
        self.now += 1
        info.update(100)
        self.assertGreater(info.speed, 0)  # A short pause is not a stall
        self.now += am.Config.STALL_SECONDS
        info.update(100)
        self.assertEqual(info.speed, 0)
        self.assertIsNone(info.eta_seconds)

    def test_unknown_target(self):
        info = am.FileTransferInfo("3", "/movies/a.mkv", 500)
        self.assertIsNone(info.percent)
        self.assertIsNone(info.eta_seconds)

    def test_source_found_later_is_adopted(self):
        info = am.FileTransferInfo("3", "/movies/a.mkv", 500)
        info.update(600, 2000, "/dl/a.mkv")
        self.assertEqual(info.target_size, 2000)
        self.assertEqual(info.source_filepath, "/dl/a.mkv")
        info.update(700)  # A later scan without a match keeps what is known
        self.assertEqual(info.target_size, 2000)

    def test_position_never_exceeds_target(self):
        info = am.FileTransferInfo("3", "/tv/ep.mkv", 0, 1000)
        info.update(1050)
        self.assertEqual(info.target_size, 1050)
        self.assertEqual(info.percent, 100)


class TextTests(unittest.TestCase):
    def test_sanitize_removes_control_characters(self):
        self.assertEqual(am.sanitize_text("a\x1b[2Jb\nc\td"), "a?[2Jb?c?d")

    def test_sanitize_handles_invalid_utf8(self):
        name = os.fsdecode(b"bad\xff.mkv")
        am.sanitize_text(name).encode("utf-8")  # Must not raise
        self.assertEqual(am.sanitize_text(name), "bad\\udcff.mkv")

    def test_clip_counts_columns_not_characters(self):
        self.assertEqual(am.display_width("日本語"), 6)
        self.assertEqual(am.clip_to_width("日本語", 4), "日本")
        self.assertEqual(am.clip_to_width("日本語", 5), "日本")
        self.assertEqual(am.clip_to_width("abc日本", 4, keep_end=True), "日本")

    def test_abbreviate_path_fits_width(self):
        path = "/data/テレビ/番組/シーズン1/エピソード.mkv"
        for width in range(1, 45):
            self.assertLessEqual(am.display_width(am.abbreviate_path(path, width)), width)
        self.assertTrue(am.abbreviate_path(path, 20).endswith(".mkv"))
        self.assertEqual(am.abbreviate_path("/a/b", 10), "/a/b")


class IdentifyManagerTests(unittest.TestCase):
    def test_native_and_hosted(self):
        self.assertEqual(am._identify_manager("Sonarr", []), "Sonarr")
        self.assertEqual(am._identify_manager("mono", ["mono", "--debug", "/opt/Radarr/Radarr.exe"]), "Radarr")
        self.assertEqual(am._identify_manager("python3", ["python3", "/opt/bazarr/bazarr.py"]), "Bazarr")
        self.assertIsNone(am._identify_manager("python3", ["python3", "script.py"]))

    def test_qbittorrent_only_on_request(self):
        self.assertIsNone(am._identify_manager("qbittorrent-nox", []))
        self.assertEqual(am._identify_manager("qbittorrent-nox", [], include_qbittorrent=True), "qbittorrent-nox")


class OpenFilesTests(unittest.TestCase):
    """Scans this process's own file descriptors"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.src = os.path.join(tmp.name, "src")
        self.dst = os.path.join(tmp.name, "dst")
        os.mkdir(self.src)
        os.mkdir(self.dst)
        self.fds = []
        self.addCleanup(lambda: [os.close(fd) for fd in self.fds])

    def open(self, path, flags, data=b""):
        fd = os.open(path, flags | os.O_CREAT, 0o600)
        self.fds.append(fd)
        if data:
            os.write(fd, data)
        return fd

    def make(self, path, size):
        with open(path, "wb") as f:
            f.write(b"x" * size)

    def scan(self, **kwargs):
        files = am.get_open_files(os.getpid(), **kwargs)
        return {info.filename: info for info in files.values() if info.filepath.startswith(self.dst)}

    def test_destination_opened_before_source(self):
        self.make(os.path.join(self.src, "Show.S01E01.mkv"), 5000)
        write_fd = self.open(os.path.join(self.dst, "Show - S01E01.mkv"), os.O_WRONLY, b"x" * 100)
        read_fd = self.open(os.path.join(self.src, "Show.S01E01.mkv"), os.O_RDONLY)
        self.assertLess(write_fd, read_fd)
        info = self.scan()["Show - S01E01.mkv"]
        self.assertEqual(info.target_size, 5000)
        self.assertEqual(info.source_filepath, os.path.join(self.src, "Show.S01E01.mkv"))
        self.assertEqual(info.percent, 2)

    def test_no_source_leaves_target_unknown(self):
        self.open(os.path.join(self.dst, "Movie (2020).mkv"), os.O_WRONLY, b"x" * 100)
        info = self.scan()["Movie (2020).mkv"]
        self.assertIsNone(info.target_size)
        self.assertIsNone(info.percent)

    def test_two_renamed_movies_are_not_given_the_largest_source(self):
        for name, size in (("A.2019.mkv", 9000), ("B.2020.mkv", 2000)):
            self.make(os.path.join(self.src, name), size)
            self.open(os.path.join(self.src, name), os.O_RDONLY)
        self.open(os.path.join(self.dst, "B (2020).mkv"), os.O_WRONLY, b"x" * 100)
        self.assertIsNone(self.scan()["B (2020).mkv"].target_size)

    def test_deleted_files_are_still_seen(self):
        source = os.path.join(self.src, "ep.mkv")
        dest = os.path.join(self.dst, "ep.mkv")
        self.make(source, 400)
        self.open(source, os.O_RDONLY)
        self.open(dest, os.O_WRONLY, b"x" * 100)
        os.unlink(source)
        os.unlink(dest)
        info = self.scan()["ep.mkv"]
        self.assertEqual(info.filepath, dest)
        self.assertEqual(info.target_size, 400)

    def test_qbittorrent_shows_only_moves(self):
        self.make(os.path.join(self.src, "Movie.nfo"), 5000)
        self.open(os.path.join(self.dst, "Movie.nfo"), os.O_WRONLY, b"x" * 1000)
        self.open(os.path.join(self.src, "Movie.nfo"), os.O_RDONLY)
        self.open(os.path.join(self.dst, "downloading.mkv"), os.O_RDWR, b"x" * 300)
        found = self.scan(qbittorrent=True)
        self.assertEqual(list(found), ["Movie.nfo"])
        self.assertEqual(found["Movie.nfo"].target_size, 5000)
        self.assertEqual(found["Movie.nfo"].percent, 20)

    def test_missing_process(self):
        self.assertEqual(am.get_open_files(2 ** 22 + 1), {})


class DebugLoggerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def test_log_is_private_and_takes_any_filename(self):
        path = os.path.join(self.dir, "debug.log")
        with am.DebugLogger(path) as logger:
            logger.log("file: " + os.fsdecode(b"bad\xff\nname.mkv"))
            self.assertTrue(logger.is_enabled)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertTrue(lines[-1].endswith("file: bad\\udcff?name.mkv"))

    def test_symlink_is_not_followed(self):
        target = os.path.join(self.dir, "victim")
        with open(target, "w") as f:
            f.write("keep me")
        link = os.path.join(self.dir, "debug.log")
        os.symlink(target, link)
        with mock.patch.object(am.sys, "stderr"):
            with am.DebugLogger(link) as logger:
                self.assertFalse(logger.is_enabled)
        with open(target) as f:
            self.assertEqual(f.read(), "keep me")

    def test_size_limit_stops_logging(self):
        path = os.path.join(self.dir, "debug.log")
        with mock.patch.object(am.Config, "LOG_MAX_BYTES", 200):
            with am.DebugLogger(path) as logger:
                for _ in range(50):
                    logger.log("x" * 40)
                self.assertFalse(logger.is_enabled)
                self.assertEqual(logger.error, "size limit reached")
        self.assertLess(os.path.getsize(path), 1000)


if __name__ == "__main__":
    unittest.main()
