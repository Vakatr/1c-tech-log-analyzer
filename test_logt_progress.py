"""Tests for :mod:`logt_progress`.

The tests pin the contract that P1-03/P1-04 rely on:

* every real LogT output format maps to the expected
  :class:`import_progress.ImportStage`, counts and percentage;
* the hour-weighted percentage matches the P1-02 expectation table
  (``1/3`` + 45 % -> 17.8, ``2/3`` without a total -> 33.3, ``2/3`` at
  110 % -> 61.7), because processing spans 5..90 %;
* broken/unknown output never raises and never resets the state;
* ``tqdm`` detection does not confuse an ``[INFO] Час ...: файлов N`` line
  with a progress bar.

All samples are inline strings on purpose: some of them contain ``\\r`` and a
file checkout may normalise line endings, which would silently weaken the
tests.
"""

from __future__ import annotations

import random
import unittest

from import_progress import ImportStage
from logt_progress import LogTProgressParser, is_tqdm_line

SAMPLES = {
    "files_found": "[INFO] Найдено файлов rphost: 12, rmngr: 3",
    "hours_total": "[INFO] Часовых групп: 3",
    "hour_start": "[INFO] Час 2024-01-15_10: файлов 7 (rphost+rmngr)",
    "tqdm_total": (
        "Час 1/3:  45%|████▌     | 45.0k/100.0k "
        "[00:12<00:14, 3.80kстрок/s, Операции=120]"
    ),
    "tqdm_ascii": (
        "Час 1/3:  45%|##########| 45.0k/100.0k "
        "[00:12<00:14, 3.80kстрок/s]"
    ),
    "tqdm_no_total": (
        "Час 2/3: 12.3kстрок [00:05<00:00, 2.40kстрок/s, Операции=120]"
    ),
    "tqdm_over": (
        "Час 2/3: 110%|██████████| 110.0k/100.0k "
        "[00:12<00:00, 3.80kстрок/s]"
    ),
    "tqdm_pct_only": (
        "Час 1/3:  45%|████▌     | [00:12<00:14, 3.80kстрок/s]"
    ),
    "tqdm_no_timing": "Час 1/3:  45%|████▌     | 45.0k/100.0k",
    "tqdm_unknown_rate": (
        "Час 1/3:  45%|████▌     | 45.0k/100.0k "
        "[?строк/s, Операции=120]"
    ),
    "tqdm_ansi": (
        "\x1b[32mЧас 1/3:\x1b[0m  45%|\x1b[42m████▌     \x1b[0m| "
        "45.0k/100.0k [00:12<00:14, 3.80kстрок/s, Операции=120]"
    ),
    "tqdm_chunked": (
        "\rЧас 1/3:  30%|███       | 30.0k/100.0k "
        "[00:10<00:20, 3.00kстрок/s]"
        "\rЧас 1/3:  45%|████▌     | 45.0k/100.0k "
        "[00:12<00:14, 3.80kстрок/s]\r\n"
    ),
    "multi_process": "[INFO] Процесс 1/2 rphost_1234: каталогов 2, файлов 14",
    "multi_progress": (
        "[INFO] rphost_1234/2024-01-15.log: объединено 1,234 событий "
        "из 3 файлов (обработано файлов 7/14)"
    ),
    "done": "[INFO] Готово: обработано 12345 операций",
    "finished": "[INFO] Скрипт успешно завершен",
}


def _bar(percent: int, hours: str = "1/1") -> str:
    """Build a minimal pct-only tqdm tick, without counters."""
    return (
        f"Час {hours}: {percent:3d}%|{'#' * 10}| "
        f"[00:01<00:01, 2.00kстрок/s]"
    )


class TestIsTqdmLine(unittest.TestCase):
    """``is_tqdm_line`` is used to keep stderr_tail free of bar redraws."""

    def test_unicode_bar_is_tqdm(self):
        """The real Unicode bar is recognised."""
        self.assertTrue(is_tqdm_line(SAMPLES["tqdm_total"]))

    def test_ascii_bar_is_tqdm(self):
        """An ASCII fallback bar is recognised too."""
        self.assertTrue(is_tqdm_line(SAMPLES["tqdm_ascii"]))

    def test_no_total_tick_is_tqdm(self):
        """A tick without ``total`` is still a bar (unit ``строк`` present)."""
        self.assertTrue(is_tqdm_line(SAMPLES["tqdm_no_total"]))

    def test_ansi_tick_is_tqdm(self):
        """ANSI colour codes do not hide a bar."""
        self.assertTrue(is_tqdm_line(SAMPLES["tqdm_ansi"]))

    def test_chunked_tick_is_tqdm(self):
        """Several ``\\r``-joined ticks are still one tqdm line."""
        self.assertTrue(is_tqdm_line(SAMPLES["tqdm_chunked"]))

    def test_info_hour_line_is_not_tqdm(self):
        """``[INFO] Час ...: файлов N`` is a log record, not a bar."""
        self.assertFalse(is_tqdm_line(SAMPLES["hour_start"]))
        self.assertFalse(
            is_tqdm_line("[INFO] Час 1/3: файлов 12 (rphost+rmngr)")
        )

    def test_other_log_lines_are_not_tqdm(self):
        """Ordinary LogT records and blank/garbage input are not bars."""
        self.assertFalse(is_tqdm_line(SAMPLES["files_found"]))
        self.assertFalse(is_tqdm_line(SAMPLES["multi_progress"]))
        self.assertFalse(is_tqdm_line(""))
        self.assertFalse(is_tqdm_line("   "))
        self.assertFalse(is_tqdm_line("совсем непонятная строка"))


class TestScanning(unittest.TestCase):
    """Scanning and multi-process lines."""

    def test_files_found_sets_scanning_total(self):
        """``rphost`` + ``rmngr`` counts become the scanning total."""
        progress = LogTProgressParser().feed_stdout(SAMPLES["files_found"])
        self.assertIsNotNone(progress)
        self.assertIs(progress.stage, ImportStage.SCANNING)
        self.assertEqual(progress.current, 0)
        self.assertEqual(progress.total, 15)
        self.assertEqual(progress.detail, "rphost: 12, rmngr: 3")
        self.assertNotIn("[INFO]", progress.message)

    def test_hours_total_is_remembered(self):
        """``Часовых групп`` is kept for the overall percentage."""
        parser = LogTProgressParser()
        progress = parser.feed_stdout(SAMPLES["hours_total"])
        self.assertIsNotNone(progress)
        self.assertIs(progress.stage, ImportStage.SCANNING)
        self.assertIn("3", progress.detail or "")
        # The hint is authoritative for the very first bar tick.
        tick = parser.feed_stderr(
            "Час 2/3: 0%|          | 0/50.0k [00:00<00:00, ?строк/s]"
        )
        self.assertEqual(tick.percent, 33.3)

    def test_hours_total_keeps_file_counts(self):
        """The hour count does not discard the file total already found."""
        parser = LogTProgressParser()
        parser.feed_stdout(SAMPLES["files_found"])
        progress = parser.feed_stdout(SAMPLES["hours_total"])
        self.assertEqual(progress.current, 0)
        self.assertEqual(progress.total, 15)

    def test_multi_process_sets_file_total(self):
        """A per-process line reports the files of that process."""
        progress = LogTProgressParser().feed_stdout(SAMPLES["multi_process"])
        self.assertIsNotNone(progress)
        self.assertIs(progress.stage, ImportStage.SCANNING)
        self.assertEqual(progress.current, 0)
        self.assertEqual(progress.total, 14)
        self.assertIn("процесс 1/2", progress.detail or "")
        self.assertIn("rphost_1234", progress.detail or "")

    def test_multi_processed_files_counter(self):
        """``(обработано файлов 7/14)`` updates current/total."""
        progress = LogTProgressParser().feed_stdout(SAMPLES["multi_progress"])
        self.assertIsNotNone(progress)
        self.assertIs(progress.stage, ImportStage.SCANNING)
        self.assertEqual(progress.current, 7)
        self.assertEqual(progress.total, 14)
        self.assertIn("7/14", progress.detail or "")


class TestProcessing(unittest.TestCase):
    """tqdm ticks and their hour-weighted percentage."""

    def test_first_hour_45_percent(self):
        """1/3 with 45 % of the hour is 17.8 % overall (5..90 % band)."""
        progress = LogTProgressParser().feed_stderr(SAMPLES["tqdm_total"])
        self.assertIsNotNone(progress)
        self.assertIs(progress.stage, ImportStage.PROCESSING)
        self.assertEqual(progress.current, 45_000)
        self.assertEqual(progress.total, 100_000)
        self.assertEqual(progress.percent, 17.8)
        self.assertIn("час 1/3", progress.detail or "")
        self.assertIn("Операции=120", progress.detail or "")
        self.assertEqual(progress.message, "Обработка логов")

    def test_second_hour_without_total(self):
        """Without ``total`` the hour fraction is 0: 2/3 -> 33.3 %."""
        progress = LogTProgressParser().feed_stdout(SAMPLES["tqdm_no_total"])
        self.assertIsNotNone(progress)
        self.assertIs(progress.stage, ImportStage.PROCESSING)
        self.assertEqual(progress.current, 12_300)
        self.assertIsNone(progress.total)
        self.assertEqual(progress.percent, 33.3)
        self.assertIn("час 2/3", progress.detail or "")

    def test_bar_over_100_percent_is_clamped_per_hour(self):
        """110 % of hour 2/3 is 61.7 % overall, not 100 %."""
        progress = LogTProgressParser().feed_stderr(SAMPLES["tqdm_over"])
        self.assertEqual(progress.current, 110_000)
        self.assertEqual(progress.total, 100_000)
        self.assertEqual(progress.percent, 61.7)

    def test_percentage_without_counters(self):
        """A pct-only tick uses ``pct / 100`` as the hour fraction."""
        progress = LogTProgressParser().feed_stderr(SAMPLES["tqdm_pct_only"])
        self.assertEqual(progress.percent, 17.8)
        self.assertIsNone(progress.current)
        self.assertIsNone(progress.total)

    def test_ascii_bar(self):
        """An ASCII bar yields the same numbers as the Unicode one."""
        progress = LogTProgressParser().feed_stderr(SAMPLES["tqdm_ascii"])
        self.assertEqual(progress.percent, 17.8)
        self.assertEqual(progress.current, 45_000)

    def test_ansi_sequences_are_stripped(self):
        """ANSI colour codes do not break the counters or the message."""
        progress = LogTProgressParser().feed_stderr(SAMPLES["tqdm_ansi"])
        self.assertEqual(progress.percent, 17.8)
        self.assertEqual(progress.current, 45_000)
        self.assertNotIn("\x1b", progress.message)

    def test_tick_without_timing(self):
        """Missing ``[elapsed<remaining, rate]`` is tolerated."""
        progress = LogTProgressParser().feed_stderr(SAMPLES["tqdm_no_timing"])
        self.assertEqual(progress.percent, 17.8)
        self.assertEqual(progress.current, 45_000)
        self.assertEqual(progress.total, 100_000)

    def test_unknown_rate_is_tolerated(self):
        """``?строк/s`` (no rate estimate yet) is ignored."""
        progress = LogTProgressParser().feed_stderr(SAMPLES["tqdm_unknown_rate"])
        self.assertEqual(progress.percent, 17.8)
        self.assertEqual(progress.current, 45_000)

    def test_scaled_numbers_with_mega_suffix(self):
        """``1.23M / 2.46M`` is parsed with its scale."""
        progress = LogTProgressParser().feed_stderr(
            "Час 1/3:  50%|█████     | 1.23M/2.46M "
            "[00:12<00:14, 3.80kстрок/s]"
        )
        self.assertEqual(progress.current, 1_230_000)
        self.assertEqual(progress.total, 2_460_000)
        self.assertEqual(progress.percent, 19.2)

    def test_comma_decimal_separator(self):
        """``1,5k/3,0k`` is 50 % of the hour."""
        progress = LogTProgressParser().feed_stderr(
            "Час 1/1:  50%|#####| 1,5k/3,0k [00:01<00:01, 1.00kстрок/s]"
        )
        self.assertEqual(progress.current, 1_500)
        self.assertEqual(progress.total, 3_000)
        self.assertEqual(progress.percent, 47.5)

    def test_comma_thousands_separator(self):
        """``1,234/4,936`` is treated as thousands, not as decimals."""
        progress = LogTProgressParser().feed_stderr(
            "Час 1/1:  25%|##        | 1,234/4,936 "
            "[00:01<00:03, 1.00kстрок/s]"
        )
        self.assertEqual(progress.current, 1_234)
        self.assertEqual(progress.total, 4_936)
        self.assertEqual(progress.percent, 26.2)

    def test_unknown_hours_total_is_indeterminate(self):
        """An hour start line without a known hour count has no percentage."""
        progress = LogTProgressParser().feed_stdout(SAMPLES["hour_start"])
        self.assertIsNotNone(progress)
        self.assertIs(progress.stage, ImportStage.PROCESSING)
        self.assertIsNone(progress.percent)
        self.assertIn("2024-01-15_10", progress.detail or "")

    def test_stderr_and_stdout_share_the_parser(self):
        """A tick fed through ``feed_stderr`` updates the same state."""
        parser = LogTProgressParser()
        parser.feed_stdout(SAMPLES["hours_total"])
        progress = parser.feed_stderr(SAMPLES["tqdm_total"])
        self.assertEqual(progress.percent, 17.8)


class TestInserting(unittest.TestCase):
    """The ``Готово`` line and its fixed 92 % milestone."""

    def test_done_reports_92_percent_and_operations(self):
        """``Готово: обработано N операций`` is the insert phase at 92 %."""
        progress = LogTProgressParser().feed_stdout(SAMPLES["done"])
        self.assertIsNotNone(progress)
        self.assertIs(progress.stage, ImportStage.INSERTING)
        self.assertEqual(progress.percent, 92.0)
        self.assertEqual(progress.current, 12_345)
        self.assertIn("12345", progress.detail or "")
        self.assertNotIn("[INFO]", progress.message)

    def test_done_after_processing_stays_at_92(self):
        """Processing progress below 92 % does not lower on insert."""
        parser = LogTProgressParser()
        parser.feed_stdout(SAMPLES["hours_total"])
        parser.feed_stderr(SAMPLES["tqdm_total"])  # 15 %
        progress = parser.feed_stdout(SAMPLES["done"])
        self.assertEqual(progress.percent, 92.0)


class TestMonotonicity(unittest.TestCase):
    """The last known percentage is never decreased."""

    def test_tqdm_ticks_are_monotonic(self):
        """Hour fractions 0 -> 10 -> 5 -> 99 never go back (5 % is ignored)."""
        parser = LogTProgressParser()
        percents = [
            parser.feed_stderr(_bar(value)).percent
            for value in (0, 10, 5, 99)
        ]
        self.assertEqual(percents, [5.0, 13.5, 13.5, 89.2])

    def test_hour_transition_does_not_decrease(self):
        """Starting the next hour never rewinds the bar."""
        parser = LogTProgressParser()
        parser.feed_stdout(SAMPLES["hours_total"])
        parser.feed_stdout(SAMPLES["hour_start"])
        first = parser.feed_stderr(
            "Час 1/3: 100%|██████████| 100.0k/100.0k "
            "[00:12<00:00, 3.80kстрок/s]"
        )
        boundary = parser.feed_stdout(
            "[INFO] Час 2024-01-15_11: файлов 4 (rphost+rmngr)"
        )
        second = parser.feed_stderr(
            "Час 2/3: 0%|          | 0/50.0k [00:00<00:00, ?строк/s]"
        )
        self.assertEqual(first.percent, 33.3)
        self.assertGreaterEqual(boundary.percent, first.percent)
        self.assertGreaterEqual(second.percent, boundary.percent)

    def test_insert_milestone_is_92_after_processing(self):
        """A full hour reaches 90 %, then ``Готово`` advances to 92 %."""
        parser = LogTProgressParser()
        parser.feed_stderr(_bar(99))
        progress = parser.feed_stdout(SAMPLES["done"])
        self.assertEqual(progress.percent, 92.0)


class TestRobustness(unittest.TestCase):
    """Unknown input, chunking and state preservation."""

    def test_unknown_lines_return_none(self):
        """Garbage, blank and empty input is ignored."""
        parser = LogTProgressParser()
        for line in (
            "",
            "   ",
            "\x1b[0m",
            "совсем непонятная строка",
            "[INFO] Найдено процессов: 2 - rphost_1, rphost_2",
        ):
            with self.subTest(line=line):
                self.assertIsNone(parser.feed_stdout(line))

    def test_unknown_line_does_not_reset_state(self):
        """A garbage line between two known ones keeps the counts."""
        parser = LogTProgressParser()
        parser.feed_stdout(SAMPLES["files_found"])
        self.assertIsNone(parser.feed_stdout("мусор"))
        progress = parser.feed_stdout(SAMPLES["hours_total"])
        self.assertEqual(progress.total, 15)

    def test_success_line_does_not_change_the_stage(self):
        """``Скрипт успешно завершен`` yields no event by itself."""
        parser = LogTProgressParser()
        parser.feed_stderr(SAMPLES["tqdm_total"])
        self.assertIsNone(parser.feed_stdout(SAMPLES["finished"]))
        # The state is intact: the same tick still reports the same percent.
        self.assertEqual(parser.feed_stderr(SAMPLES["tqdm_total"]).percent, 17.8)

    def test_chunked_ticks_use_only_the_last_segment(self):
        """Several ``\\r`` segments in one chunk resolve to the freshest one."""
        progress = LogTProgressParser().feed_stderr(SAMPLES["tqdm_chunked"])
        self.assertIsNotNone(progress)
        self.assertEqual(progress.percent, 17.8)
        self.assertEqual(progress.current, 45_000)

    def test_prefixed_line_without_bar_data_is_ignored(self):
        """``Час 1/3:`` without a bar is not treated as a progress tick."""
        parser = LogTProgressParser()
        self.assertIsNone(parser.feed_stdout("Час 1/3: файлов 12"))
        # The hours total from that malformed line was not learned.
        self.assertIsNone(parser.feed_stdout(SAMPLES["hour_start"]).percent)

    def test_pathological_number_does_not_crash(self):
        """A huge numeric field is dropped instead of raising ``OverflowError``."""
        progress = LogTProgressParser().feed_stderr(
            "Час 1/1:  50%|          | "
            + "9" * 400
            + "/1 [00:01<00:01, 1.00kстрок/s]"
        )
        self.assertIsNotNone(progress)
        self.assertIsNone(progress.current)
        self.assertEqual(progress.percent, 47.5)

    def test_reset_forgets_everything(self):
        """``reset`` clears hours, counts and the monotonic percentage."""
        parser = LogTProgressParser()
        parser.feed_stdout(SAMPLES["files_found"])
        parser.feed_stdout(SAMPLES["hours_total"])
        parser.feed_stderr(SAMPLES["tqdm_total"])
        parser.reset()
        progress = parser.feed_stdout(SAMPLES["hour_start"])
        self.assertIsNone(progress.percent)
        self.assertIsNone(progress.current)
        # A fresh scan starts from zero again.
        scanning = parser.feed_stdout(SAMPLES["files_found"])
        self.assertEqual(scanning.current, 0)
        self.assertEqual(scanning.total, 15)

    def test_full_sequence_scanning_processing_inserting(self):
        """A realistic stdout/stderr sequence changes stages in order."""
        parser = LogTProgressParser()
        stages = []
        for line in (
            SAMPLES["files_found"],
            SAMPLES["hours_total"],
            SAMPLES["hour_start"],
            SAMPLES["tqdm_total"],
            SAMPLES["done"],
        ):
            progress = parser.feed_stdout(line)
            self.assertIsNotNone(progress)
            self.assertNotIn("[INFO]", progress.message)
            self.assertNotIn("tqdm", progress.message)
            stages.append(progress.stage)
        self.assertEqual(
            stages,
            [
                ImportStage.SCANNING,
                ImportStage.SCANNING,
                ImportStage.PROCESSING,
                ImportStage.PROCESSING,
                ImportStage.INSERTING,
            ],
        )
        self.assertEqual(parser.feed_stdout(SAMPLES["finished"]), None)


class TestExoticInput(unittest.TestCase):
    """Garbage, control characters and non-finite numbers never break the bar."""

    def test_unknown_lines_return_none_and_keep_state(self):
        """Arbitrary lines are ignored even after a real tick was processed."""
        parser = LogTProgressParser()
        first = parser.feed_stderr(SAMPLES["tqdm_total"])
        self.assertEqual(first.percent, 17.8)
        for line in (
            "случайный текст",
            "[INFO] Найдено процессов: 2 - rphost_1, rphost_2",
            # A ``Час i/H:`` prefix without bar data must not emit an event.
            "Час 1/3: файлов 12",
            "\x1b[0m",
            "\x07\x07",
            "\r\r\r",
        ):
            with self.subTest(line=line):
                self.assertIsNone(parser.feed_stdout(line))
        again = parser.feed_stderr(SAMPLES["tqdm_total"])
        self.assertEqual(again.percent, 17.8)
        self.assertEqual(again.current, 45_000)
        self.assertEqual(again.total, 100_000)

    def test_multiple_carriage_returns_with_ansi_use_the_last_tick(self):
        """Several ``\\r`` segments plus ANSI codes resolve to the freshest bar."""
        line = (
            "\x1b[31m\r\rЧас 1/3:  10%|#         | 10/100 "
            "[00:01<00:09, 1.00kстрок/s]\r"
            "\x1b[0m\rЧас 1/3:  45%|####▌     | 45/100 "
            "[00:02<00:03, 2.00kстрок/s]\r\n"
        )
        progress = LogTProgressParser().feed_stderr(line)
        self.assertIsNotNone(progress)
        self.assertEqual(progress.percent, 17.8)
        self.assertEqual(progress.current, 45)
        self.assertEqual(progress.total, 100)
        self.assertNotIn("\x1b", progress.message)
        self.assertIn("час 1/3", progress.detail or "")

    def test_non_finite_and_negative_numbers_are_tolerated(self):
        """``nan``/``inf``/``1e309``/negative values stay bounded or are ignored."""
        tails = (
            "nan%|          | nan/nan [00:01<00:01, 1.00kстрок/s]",
            "inf%|          | inf/inf [00:01<00:01, 1.00kстрок/s]",
            "1e309%|        | 1/2 [00:01<00:01, 1.00kстрок/s]",
            "-5%|          | -1/-2 [00:01<00:01, 1.00kстрок/s]",
            "50%|          | 1e309/1e309 [00:01<00:01, 1.00kстрок/s]",
        )
        for tail in tails:
            with self.subTest(tail=tail):
                parser = LogTProgressParser()
                parser.feed_stdout("[INFO] Часовых групп: 1")
                progress = parser.feed_stderr(f"Час 1/1: {tail}")
                self.assertIsNotNone(progress)
                self.assertTrue(
                    progress.percent is None
                    or 0.0 <= progress.percent <= 100.0,
                    msg=f"percent out of range: {progress.percent!r}",
                )
                if progress.current is not None:
                    self.assertGreaterEqual(progress.current, 0)
                if progress.total is not None:
                    self.assertGreaterEqual(progress.total, 0)

    def test_seeded_fuzz_lines_never_raise_and_stay_bounded(self):
        """300 deterministic random lines keep percent in ``[0, 100]``/``None``."""
        rng = random.Random(12345)
        alphabet = (
            "0123456789.,%|/[]<>=:ЧасГотовоФайловфайловстрок"
            "\x1b\r\x00 ()-+"
        )
        prefixes = (
            "",
            "Час 1/3: ",
            "[INFO] ",
            "[INFO] Найдено файлов rphost: ",
            "[INFO] Часовых групп: ",
            "[INFO] Готово: обработано ",
        )
        parser = LogTProgressParser()
        last_percent = 0.0
        for _ in range(300):
            line = rng.choice(prefixes) + "".join(
                rng.choice(alphabet) for _ in range(rng.randint(0, 50))
            )
            feed = rng.choice((parser.feed_stdout, parser.feed_stderr))
            progress = feed(line)
            if progress is None:
                continue
            if progress.percent is not None:
                self.assertGreaterEqual(progress.percent, 0.0, msg=line)
                self.assertLessEqual(progress.percent, 100.0, msg=line)
                self.assertGreaterEqual(progress.percent, last_percent, msg=line)
                last_percent = progress.percent
            if progress.current is not None:
                self.assertGreaterEqual(progress.current, 0)
            if progress.total is not None:
                self.assertGreaterEqual(progress.total, 0)
        # The parser is still usable after the fuzz sequence.
        final = parser.feed_stderr(SAMPLES["tqdm_total"])
        self.assertIsNotNone(final)
        self.assertGreaterEqual(final.percent, last_percent)


if __name__ == "__main__":  # pragma: no cover - convenience entry point
    unittest.main()
