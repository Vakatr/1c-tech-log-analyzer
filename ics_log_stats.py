"""
1C Log Statistics Module
Provides aggregation and analysis functions for parsed log events
"""

from collections import Counter
from typing import Any, Dict, List
from datetime import datetime

from ics_log_constants import is_error_event


def aggregate_by_event(events: List[Dict[str, Any]]) -> Dict[str, int]:
    """
    Aggregate events by event name (N field).
    
    Args:
        events: List of parsed event dictionaries
        
    Returns:
        Dictionary mapping event names to counts
    """
    counter = Counter()
    for event in events:
        name = event.get('name', '')
        if name:
            counter[name] += 1
    return dict(counter)


def aggregate_by_user(events: List[Dict[str, Any]]) -> Dict[str, int]:
    """
    Aggregate events by user.
    
    Args:
        events: List of parsed event dictionaries
        
    Returns:
        Dictionary mapping usernames to counts
    """
    counter = Counter()
    for event in events:
        user = event.get('user', '')
        if user:
            counter[user] += 1
    return dict(counter)


def find_errors(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Find error events.

    An error is an event whose ``name`` is one of
    :data:`ics_log_constants.ERROR_EVENT_NAMES` (``EXCP`` / ``EXCPCNTX``).
    This is the same definition the ClickHouse adapter uses, so both data
    sources show the same rows.

    Args:
        events: List of parsed event dictionaries.

    Returns:
        List of error event dictionaries, in input order.
    """
    return [event for event in events if is_error_event(event)]



def top_slowest(events: List[Dict[str, Any]], n: int = 10) -> List[Dict[str, Any]]:
    """
    Find the slowest events by duration.
    
    Args:
        events: List of parsed event dictionaries
        n: Number of top events to return
        
    Returns:
        List of top N slowest events sorted by duration descending
    """
    # Sort by duration descending; treat None/invalid duration as 0
    def _dur(e):
        v = e.get('duration', 0)
        if v is None:
            return 0.0
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0
    sorted_events = sorted(events, key=_dur, reverse=True)
    
    return sorted_events[:n]


def timeline_by_hour(events: List[Dict[str, Any]]) -> Dict[str, int]:
    """
    Create a timeline of events by hour.

    Args:
        events: List of parsed event dictionaries

    Returns:
        Dictionary mapping hour strings (YYYY-MM-DD HH) to event counts
    """
    timeline = Counter()

    for event in events:
        timestamp = event.get('timestamp', '')
        if not timestamp:
            continue

        # Support both 1C (dd.mm.yyyy) and ISO (yyyy-mm-dd) timestamps.  Strip
        # only a trailing fractional-seconds component: a plain split('.')
        # would cut the date apart ("01.01.2024 ..." -> "01").
        normalized = str(timestamp).strip().replace('T', ' ')
        head, _, tail = normalized.rpartition('.')
        if head and tail.isdigit() and len(tail) <= 6:
            normalized = head
        dt = None
        for fmt in (
            '%Y-%m-%d %H:%M:%S',
            '%Y-%m-%d %H:%M',
            '%d.%m.%Y %H:%M:%S',
            '%d.%m.%Y %H:%M',
            '%Y-%m-%d',
        ):
            try:
                dt = datetime.strptime(normalized, fmt)
                break
            except ValueError:
                continue

        if dt is None:
            continue

        hour_key = dt.strftime('%Y-%m-%d %H')
        timeline[hour_key] += 1

    return dict(timeline)
