"""Today's and tomorrow's events from private iCal addresses, for the Echo dashboard.

The Echo can't sync Google Calendar without Play Services, so the Pi reads the calendars'
secret iCal links and sends the events over the existing WebSocket."""
import datetime as dt
import logging
import urllib.request

LOG = logging.getLogger('voice')

# ARGB colours assigned to calendars in config order (the iCal feed carries none).
COLORS = [0xFF7EE6D3, 0xFF8AB4F8, 0xFFF6AE2D, 0xFFE57373, 0xFFBA68C8, 0xFF81C784]


def fetch(url, timeout=20):
    request = urllib.request.Request(url, headers={'User-Agent': 'pi-voice-assistant'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def epoch_ms(value, zone):
    """Event time as epoch milliseconds; all-day dates start at local midnight."""
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=zone)
        return int(value.timestamp() * 1000)
    return int(dt.datetime.combine(value, dt.time(), zone).timestamp() * 1000)


def events_between(ics, start, end, color):
    """Expanded occurrences in [start, end), cancelled ones dropped."""
    import icalendar
    import recurring_ical_events
    zone = start.tzinfo
    found = []
    for event in recurring_ical_events.of(icalendar.Calendar.from_ical(ics)).between(start, end):
        if str(event.get('STATUS', '')).upper() == 'CANCELLED':
            continue
        begin = event.decoded('DTSTART')
        finish = event.decoded('DTEND') if 'DTEND' in event else None
        all_day = not isinstance(begin, dt.datetime)
        if finish is None:
            finish = begin + (dt.timedelta(days=1) if all_day else dt.timedelta(0))
        found.append(dict(title=str(event.get('SUMMARY', '(No title)')), begin=epoch_ms(begin, zone),
                          end=epoch_ms(finish, zone), all_day=all_day,
                          location=str(event.get('LOCATION', '')), color=color))
    return found


class Shared:
    """Latest events, shared by all sessions; `version` increments when they change."""
    def __init__(self):
        self.events, self.version = None, 0

    def update(self, events):
        if events is not None and events != self.events:
            self.events, self.version = events, self.version + 1


def load(urls, now=None):
    """Events from today's local midnight for two days, sorted all-day first then by start.
    A calendar that fails to load is skipped (and logged); if all fail, returns None so the
    dashboard keeps what it has instead of showing an empty day."""
    now = now or dt.datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=2)
    events, loaded = [], 0
    for index, url in enumerate(urls):
        try:
            events += events_between(fetch(url), start, end, COLORS[index % len(COLORS)])
            loaded += 1
        except Exception as exc:
            LOG.warning('Calendar %d failed: %s', index + 1, type(exc).__name__)
    if urls and not loaded:
        return None
    return sorted(events, key=lambda e: (not e['all_day'], e['begin']))
