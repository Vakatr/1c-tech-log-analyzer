import unittest

from ics_log_stats import (
    aggregate_by_event,
    aggregate_by_user,
    find_errors,
    top_slowest,
    timeline_by_hour,
)


class TestAggregateByEvent(unittest.TestCase):
    def test_empty_list(self):
        self.assertEqual(aggregate_by_event([]), {})

    def test_single_event(self):
        self.assertEqual(aggregate_by_event([{'name': 'A'}]), {'A': 1})

    def test_multiple_events(self):
        events = [
            {'name': 'A'}, {'name': 'B'}, {'name': 'A'},
            {'name': 'C'}, {'name': 'A'},
        ]
        self.assertEqual(aggregate_by_event(events), {'A': 3, 'B': 1, 'C': 1})

    def test_events_without_name(self):
        events = [{'name': 'A'}, {}, {'name': ''}, {'other': 'x'}]
        self.assertEqual(aggregate_by_event(events), {'A': 1})

    def test_name_none_is_skipped(self):
        events = [{'name': None}, {'name': 'A'}]
        self.assertEqual(aggregate_by_event(events), {'A': 1})

    def test_returns_plain_dict(self):
        self.assertIs(type(aggregate_by_event([{'name': 'A'}])), dict)


class TestAggregateByUser(unittest.TestCase):
    def test_empty_list(self):
        self.assertEqual(aggregate_by_user([]), {})

    def test_single_user(self):
        self.assertEqual(aggregate_by_user([{'user': 'admin'}]), {'admin': 1})

    def test_multiple_users(self):
        events = [
            {'user': 'admin'}, {'user': 'u1'}, {'user': 'admin'},
            {'user': 'u2'}, {'user': 'admin'},
        ]
        self.assertEqual(
            aggregate_by_user(events), {'admin': 3, 'u1': 1, 'u2': 1}
        )

    def test_missing_user(self):
        events = [{'user': 'admin'}, {}, {'user': ''}]
        self.assertEqual(aggregate_by_user(events), {'admin': 1})

    def test_user_none_is_skipped(self):
        events = [{'user': None}, {'user': 'admin'}]
        self.assertEqual(aggregate_by_user(events), {'admin': 1})


class TestFindErrors(unittest.TestCase):
    def test_empty_list(self):
        self.assertEqual(find_errors([]), [])

    def test_no_errors(self):
        events = [
            {'name': 'Info', 'descr': ''},
            {'name': 'Warning', 'descr': ''},
        ]
        self.assertEqual(find_errors(events), [])

    def test_exception_name(self):
        events = [{'name': 'EXCP', 'descr': ''}]
        self.assertEqual(find_errors(events), events)

    def test_exception_context_name(self):
        events = [{'name': 'EXCPCNTX', 'descr': ''}]
        self.assertEqual(find_errors(events), events)

    def test_non_empty_descr_alone_is_not_error(self):
        # Aligned with ClickHouse (BUG-005): only known error event names count.
        events = [{'name': 'Info', 'descr': 'boom'}]
        self.assertEqual(find_errors(events), [])

    def test_both_conditions(self):
        events = [
            {'name': 'Info', 'descr': ''},
            {'name': 'EXCP', 'descr': ''},
            {'name': 'Info', 'descr': 'boom'},
            {'name': 'EXCPCNTX', 'descr': 'boom'},
            {'name': 'Info', 'descr': ''},
        ]
        self.assertEqual(len(find_errors(events)), 2)

    def test_missing_fields(self):
        events = [{}, {'name': 'EXCP'}, {'descr': 'boom'}]
        self.assertEqual(len(find_errors(events)), 1)

    def test_returns_same_object_references(self):
        error = {'name': 'EXCP', 'id': 1}
        ok = {'name': 'Info', 'descr': '', 'id': 2}
        result = find_errors([error, ok])
        self.assertIs(result[0], error)

    def test_preserves_order(self):
        events = [
            {'name': 'EXCP', 'id': 1},
            {'name': 'Info', 'descr': 'boom', 'id': 2},
            {'name': 'EXCPCNTX', 'id': 3},
        ]
        ids = [e['id'] for e in find_errors(events)]
        self.assertEqual(ids, [1, 3])

    def test_lowercase_excp_not_error(self):
        self.assertEqual(find_errors([{'name': 'excp', 'descr': ''}]), [])

    def test_whitespace_around_name_is_tolerated(self):
        self.assertEqual(find_errors([{'name': ' EXCP '}]), [{'name': ' EXCP '}])


class TestTopSlowest(unittest.TestCase):
    def test_empty_list(self):
        self.assertEqual(top_slowest([]), [])

    def test_default_n_is_10(self):
        events = [{'duration': str(i)} for i in range(15)]
        self.assertEqual(len(top_slowest(events)), 10)

    def test_custom_n(self):
        events = [{'duration': str(i)} for i in range(10)]
        self.assertEqual(len(top_slowest(events, n=3)), 3)

    def test_sorted_descending(self):
        events = [
            {'duration': '1.5', 'id': 1},
            {'duration': '10.2', 'id': 2},
            {'duration': '3.7', 'id': 3},
        ]
        self.assertEqual([e['id'] for e in top_slowest(events)], [2, 3, 1])

    def test_n_larger_than_list(self):
        self.assertEqual(len(top_slowest([{'duration': '1'}], n=10)), 1)

    def test_n_zero(self):
        self.assertEqual(top_slowest([{'duration': '1'}], n=0), [])

    def test_n_negative(self):
        self.assertEqual(top_slowest([{'duration': '1'}], n=-1), [])

    def test_missing_duration_defaults_zero(self):
        events = [
            {'duration': '5.0', 'id': 1},
            {'id': 2},
            {'duration': '1.0', 'id': 3},
        ]
        self.assertEqual([e['id'] for e in top_slowest(events)], [1, 3, 2])

    def test_integer_duration(self):
        events = [{'duration': 3, 'id': 1}, {'duration': 7, 'id': 2}]
        self.assertEqual([e['id'] for e in top_slowest(events)], [2, 1])

    def test_none_duration_defaults_zero(self):
        events = [{'duration': None, 'id': 1}, {'duration': '2', 'id': 2}]
        self.assertEqual(top_slowest(events)[0]['id'], 2)

    def test_invalid_string_duration_defaults_zero(self):
        events = [
            {'duration': 'not-a-number', 'id': 1},
            {'duration': '2', 'id': 2},
        ]
        self.assertEqual([e['id'] for e in top_slowest(events)], [2, 1])

    def test_does_not_mutate_input(self):
        events = [
            {'duration': '1.0', 'id': 1},
            {'duration': '5.0', 'id': 2},
        ]
        top_slowest(events)
        self.assertEqual(events[0]['id'], 1)


class TestTimelineByHour(unittest.TestCase):
    def test_empty_list(self):
        self.assertEqual(timeline_by_hour([]), {})

    def test_single_event_with_seconds(self):
        events = [{'timestamp': '01.01.2024 10:30:00'}]
        self.assertEqual(timeline_by_hour(events), {'2024-01-01 10': 1})

    def test_single_event_without_seconds(self):
        events = [{'timestamp': '01.01.2024 10:30'}]
        self.assertEqual(timeline_by_hour(events), {'2024-01-01 10': 1})

    def test_with_millis(self):
        events = [{'timestamp': '15.06.2023 23:45:12.345'}]
        self.assertEqual(timeline_by_hour(events), {'2023-06-15 23': 1})

    def test_same_hour_aggregation(self):
        events = [
            {'timestamp': '01.01.2024 10:00:00'},
            {'timestamp': '01.01.2024 10:30:00'},
            {'timestamp': '01.01.2024 10:59:59'},
        ]
        self.assertEqual(timeline_by_hour(events), {'2024-01-01 10': 3})

    def test_multiple_hours(self):
        events = [
            {'timestamp': '01.01.2024 10:00:00'},
            {'timestamp': '01.01.2024 11:00:00'},
            {'timestamp': '01.01.2024 10:30:00'},
            {'timestamp': '02.01.2024 10:00:00'},
        ]
        result = timeline_by_hour(events)
        self.assertEqual(result['2024-01-01 10'], 2)
        self.assertEqual(result['2024-01-01 11'], 1)
        self.assertEqual(result['2024-01-02 10'], 1)

    def test_missing_timestamp(self):
        self.assertEqual(timeline_by_hour([{}, {'timestamp': ''}]), {})

    def test_none_timestamp(self):
        self.assertEqual(timeline_by_hour([{'timestamp': None}]), {})

    def test_invalid_timestamp(self):
        self.assertEqual(timeline_by_hour([{'timestamp': 'invalid'}]), {})

    def test_out_of_range_values(self):
        self.assertEqual(
            timeline_by_hour([{'timestamp': '32.13.2024 25:00:00'}]), {}
        )

    def test_mixed_valid_invalid(self):
        events = [
            {'timestamp': '01.01.2024 10:00:00'},
            {'timestamp': 'bad'},
            {'timestamp': '02.01.2024 12:00:00'},
        ]
        self.assertEqual(len(timeline_by_hour(events)), 2)

    def test_returns_plain_dict(self):
        result = timeline_by_hour([{'timestamp': '01.01.2024 10:30:00'}])
        self.assertIs(type(result), dict)

    def test_hour_zero_padded(self):
        result = timeline_by_hour([{'timestamp': '05.03.2024 07:15:00'}])
        self.assertEqual(result, {'2024-03-05 07': 1})

    def test_iso_timestamp(self):
        result = timeline_by_hour([{'timestamp': '2024-01-01T10:30:00.123'}])
        self.assertEqual(result, {'2024-01-01 10': 1})

    def test_iso_timestamp_without_millis(self):
        result = timeline_by_hour([{'timestamp': '2024-01-01 10:30'}])
        self.assertEqual(result, {'2024-01-01 10': 1})


if __name__ == '__main__':
    unittest.main()