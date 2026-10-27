"""MTD bus times. The fake API answers in the live API's shapes (checked against api.mtd.dev
on 2026-10-26), which differ from its published types: `routeGroupId` and `stopId` keys, and
timetables without real dates."""
import datetime as dt
import io
import json
import unittest
import urllib.error
import urllib.parse

from voice_assistant import mtd
from voice_assistant.skills import Skill, parse

CONFIG = {'api_key': 'k', 'url': 'https://api.mtd.dev', 'default_stop': 'Campus Circle',
          'default_route': '22', 'nearby_meters': 400, 'destination_meters': 400, 'max_options': 2}
TZ = dt.timezone(dt.timedelta(hours=-5))
NOW = dt.datetime(2026, 10, 27, 15, 0, tzinfo=TZ)
CC = (40.1000, -88.2000)        # Campus Circle
NEAR_CC = (40.1027, -88.2000)   # ~300 m north of it
UNION = (40.1092, -88.2272)     # Illini Union
TARGET = (40.1400, -88.2500)    # somewhere far


def at(minutes):
    return (NOW + dt.timedelta(minutes=minutes)).isoformat()


def tod(minutes):
    """A timetable time: a placeholder date and the time of day."""
    t = dt.datetime(2026, 9, 8, 15, 0) + dt.timedelta(minutes=minutes)
    return t.isoformat()


def loc(p):
    return {'latitude': p[0], 'longitude': p[1]}


GROUPS = [{'routeGroupId': 'g-illini', 'routeGroupName': 'Illini', 'routes': [{'number': '22'}, {'number': '220'}]},
          {'routeGroupId': 'g-illini-ltd', 'routeGroupName': 'Illini Limited',
           'routes': [{'number': '22'}, {'number': '220'}]},
          {'routeGroupId': 'g-green', 'routeGroupName': 'Green', 'routes': [{'number': '5'}, {'number': '50'}]}]
STOPS = [{'stopId': 'CAMPUSCIR', 'name': 'Campus Circle', 'type': 0, 'location': loc(CC)},
         {'stopId': 'FO', 'name': 'Florida & Orchard', 'type': 0, 'location': loc(NEAR_CC)},
         {'stopId': 'IU', 'name': 'Illini Union', 'type': 0, 'location': loc(UNION)},
         {'stopId': 'TG', 'name': 'Target', 'type': 0, 'location': loc(TARGET)}]


def departure(route, group, minutes, trip, late=0, realtime=True, stop='CAMPUSCIR:2', way='South'):
    return {'stopId': stop, 'headsign': f'{route[0]}{way[0]} {route[1].upper()}',
            'route': {'shortName': route[0], 'longName': route[1], 'routeGroupId': group},
            'trip': {'tripId': trip, 'direction': {'id': 1, 'name': way, 'shortName': way[0]}},
            'minutesTillDeparture': minutes + late, 'scheduledDeparture': at(minutes),
            'estimatedDeparture': at(minutes + late), 'isRealTime': realtime}


def stop_time(trip, minutes, position):
    return {'tripId': trip, 'departureTime': tod(minutes), 'departurePastMidnight': False, 'stopSequence': position}


class FakeApi:
    def __init__(self, departures, timetables=None, geocode=None, stops=STOPS):
        self.departures, self.timetables, self.geocode, self.calls = departures, timetables or {}, geocode, []
        self.stops = stops

    def __call__(self, request, timeout):
        url = urllib.parse.urlparse(request.full_url)
        query = dict(urllib.parse.parse_qsl(url.query))
        self.calls.append(url.path)
        if url.netloc == 'nominatim.openstreetmap.org':
            body = self.geocode or []
        else:
            assert request.get_header('X-apikey') == 'k'
            path = urllib.parse.unquote(url.path)
            stop = path.split('/')[2] if path.count('/') >= 3 else None
            if path == '/routes/groups':
                result = GROUPS
            elif path == '/stops':
                result = self.stops
            elif path == '/stops/search':
                q = query['query'].lower()
                result = [{'stopId': s['stopId'], 'name': s['name'], 'type': 1, 'location': s['location']}
                          for s in self.stops if q in s['name'].lower()]
            elif path.endswith('/departures'):
                result = self.departures.get(stop)
            elif path.endswith('/schedule'):
                assert 'date' not in query  # with a date the live API finds nothing
                result = self.timetables.get(stop)
            else:
                raise AssertionError(path)
            if result is None:  # the live API's "nothing here"
                raise urllib.error.HTTPError(request.full_url, 404, 'Not Found', {}, io.BytesIO(b'{}'))
            body = {'result': result}

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        return Response(json.dumps(body).encode())


def make(api):
    return mtd.Mtd(CONFIG, opener=api, clock=lambda: 0)


class GrammarTests(unittest.TestCase):
    def test_next_bus(self):
        self.assertEqual(parse('When is the next bus?').action, 'bus_query')
        self.assertEqual(parse('when does the next 22 come').route, '22')
        self.assertEqual(parse('when is the next twenty two').route, '22')
        self.assertEqual(parse('next bus at Illini Union').stop, 'illini union')
        self.assertEqual(parse('what buses are coming').route, '*')
        self.assertEqual(parse('any buses at campus circle'), Skill('bus_query', stop='campus circle', route='*'))

    def test_which_bus(self):
        self.assertEqual(parse('Which bus should I take to Target?'), Skill('bus_trip', to='target'))
        self.assertEqual(parse('What bus do I take to get to the Illini Union?').to, 'illini union')
        self.assertEqual(parse('how do I get to Walmart by bus').to, 'walmart')
        self.assertEqual(parse('when is the next bus to Siebel Center').to, 'siebel center')

    def test_not_bus_requests(self):
        for said in ('I missed the bus', 'the bus is late again', 'how do I get to Chicago',
                     'text mom I am on the bus', 'tell me a joke about a bus'):
            self.assertIsNone(parse(said), said)


class ReportTests(unittest.TestCase):
    def test_defaults_to_the_22_at_campus_circle(self):
        api = FakeApi({'CAMPUSCIR': [departure(('5', 'Green'), 'g-green', 2, 't1'),
                                     departure(('22', 'Illini'), 'g-illini', 4, 't2'),
                                     departure(('220', 'Illini Limited'), 'g-illini-ltd', 12, 't3'),
                                     departure(('220', 'Illini'), 'g-illini', 26, 't4')]})
        self.assertEqual(make(api).report(route='when is the next bus', now=NOW),
                         'The next Illini leaves Campus Circle in 4 minutes, then in 12 and 26 minutes.')

    def test_a_route_name(self):
        api = FakeApi({'CAMPUSCIR': [departure(('22', 'Illini'), 'g-illini', 4, 't2'),
                                     departure(('50', 'Green'), 'g-green', 9, 't1')]})
        self.assertEqual(make(api).report(route='when is the next green', now=NOW),
                         'The next Green leaves Campus Circle in 9 minutes.')

    def test_all_routes_both_ways_at_a_named_stop(self):
        api = FakeApi({'IU': [departure(('50', 'Green'), 'g-green', 0, 't1', stop='IU:1', way='East'),
                              departure(('22', 'Illini'), 'g-illini', 1, 't2', stop='IU:2', way='North')]})
        self.assertEqual(make(api).report(stop='illini union', route='*', now=NOW),
                         'From Illini Union: the 50 Green going east now and the 22 Illini going north in 1 minute.')

    def test_nothing_due_and_scheduled_only(self):
        self.assertEqual(make(FakeApi({})).report(now=NOW),
                         'No Illini bus is due at Campus Circle in the next hour.')
        api = FakeApi({'CAMPUSCIR': [departure(('22', 'Illini'), 'g-illini', 9, 't1', realtime=False)]})
        self.assertTrue(make(api).report(now=NOW).endswith('Those are scheduled times.'))

    def test_unknown_stop(self):
        self.assertEqual(make(FakeApi({})).report(stop='narnia', now=NOW), "I couldn't find a stop called narnia.")


class PlanTests(unittest.TestCase):
    def test_offers_the_earliest_arrivals_with_delay_and_walking(self):
        departures = {'CAMPUSCIR': [departure(('22', 'Illini'), 'g-illini', 4, 'trip-a', late=2),
                                    departure(('22', 'Illini'), 'g-illini', 20, 'trip-b'),
                                    departure(('9', 'Brown'), 'g-brown', 6, 'trip-x'),       # never reaches the Union
                                    departure(('22', 'Illini'), 'g-illini', 8, 'trip-e')],   # passed the Union already
                      'FO': [departure(('5', 'Green'), 'g-green', 5, 'trip-c', stop='FO:1'),
                             departure(('5', 'Green'), 'g-green', 1, 'trip-d', stop='FO:1')]}  # can't walk there in time
        timetables = {'CAMPUSCIR': [stop_time('trip-a', 4, 10), stop_time('trip-b', 20, 10),
                                    stop_time('trip-x', 6, 3), stop_time('trip-e', 8, 12)],
                      'FO': [stop_time('trip-c', 5, 4), stop_time('trip-d', 1, 4)],
                      'IU': [stop_time('trip-a', 14, 18), stop_time('trip-b', 30, 18), stop_time('trip-c', 18, 9),
                             stop_time('trip-d', 10, 9), stop_time('trip-e', 2, 7)]}
        answer = make(FakeApi(departures, timetables)).plan('illini union', now=NOW)
        # trip-a: leaves 2 late at 3:06, 10-minute ride -> 3:16; trip-c: walk 4 minutes, 13-minute ride -> 3:18
        self.assertEqual(answer, 'For Illini Union, take the 22 Illini in 6 minutes, getting you to Illini Union '
                                 'around 3:16 PM. Or walk 4 minutes to Florida and Orchard and take the 5 Green in '
                                 '5 minutes, getting you to Illini Union around 3:18 PM.')

    def test_geocodes_places_that_are_not_stops(self):
        api = FakeApi({'CAMPUSCIR': [departure(('22', 'Illini'), 'g-illini', 3, 'trip-a')]},
                      {'CAMPUSCIR': [stop_time('trip-a', 3, 5)], 'TG': [stop_time('trip-a', 25, 20)]},
                      geocode=[{'name': 'Target', 'lat': str(TARGET[0]), 'lon': str(TARGET[1] + 0.001)}])
        answer = make(api).plan('the target store', now=NOW)
        self.assertTrue(answer.startswith('For Target, take the 22 Illini in 3 minutes'), answer)
        self.assertIn('/search', api.calls)

    def test_no_direct_bus_and_walkable(self):
        api = FakeApi({'CAMPUSCIR': [departure(('22', 'Illini'), 'g-illini', 3, 'trip-a')]},
                      {'CAMPUSCIR': [stop_time('trip-a', 3, 5)], 'IU': []})
        m = make(api)
        self.assertEqual(m.plan('illini union', now=NOW),
                         "I couldn't find a direct bus from Campus Circle to Illini Union in the next hour. "
                         "It's about a 40 minute walk.")
        self.assertEqual(m.plan('florida and orchard', now=NOW),
                         "Florida and Orchard is about a 5 minute walk from Campus Circle; that's quicker than a bus.")

    def test_walking_beats_a_slow_bus(self):
        near = (40.1090, -88.2000)  # ~1 km: a 16 minute walk; the bus gets there in 25
        stops = STOPS + [{'stopId': 'NR', 'name': 'Nearby', 'type': 0, 'location': loc(near)}]
        api = FakeApi({'CAMPUSCIR': [departure(('22', 'Illini'), 'g-illini', 15, 'trip-a')]},
                      {'CAMPUSCIR': [stop_time('trip-a', 15, 5)], 'NR': [stop_time('trip-a', 25, 9)]}, stops=stops)
        answer = make(api).plan('nearby', now=NOW)
        self.assertTrue(answer.startswith('Walking is quickest: Nearby is about 16 minutes on foot.'), answer)

    def test_repeated_buses_are_grouped(self):
        api = FakeApi({'IU': [departure(('220', 'Illini'), 'g-illini', 3, 't1', stop='IU:1'),
                              departure(('220', 'Illini'), 'g-illini', 9, 't2', stop='IU:1'),
                              departure(('220', 'Illini'), 'g-illini', 15, 't3', stop='IU:2', way='North')]})
        self.assertEqual(make(api).report(stop='illini union', route='*', now=NOW),
                         'From Illini Union: the 220 Illini going south in 3 and 9 minutes '
                         'and the 220 Illini going north in 15 minutes.')


if __name__ == '__main__':
    unittest.main()
