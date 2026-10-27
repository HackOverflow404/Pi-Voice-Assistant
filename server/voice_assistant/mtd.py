"""MTD (Champaign-Urbana Mass Transit District) bus times, from MTD's API v3 (https://mtd.dev):
when the next bus leaves a stop, and which bus to take to a place.

API v3 has no trip planner, so `plan` finds direct trips itself: stops near the destination
(an MTD stop name, or a place found by OpenStreetMap's geocoder) and live departures from
the home stop and the stops a short walk from it, joined by trip. Each stop's timetable
(`/stops/{id}/schedule`, every trip with its time of day and position on the trip) says
whether a trip reaches the destination after it leaves, and how long the ride is; the
ride is added to the bus's live departure time. Transfers aren't attempted.

Shapes are the live API's, which differ from its published types: route groups and stops
are keyed `routeGroupId` and `stopId`, and a schedule asked for by date comes back empty.
MTD asks clients to cache: the stop list, route groups and timetables are kept for a day,
departures for 30 seconds."""
import datetime as dt
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request

WALK_METERS_PER_MINUTE = 80
JUST_WALK = 8  # minutes on foot below which a bus isn't worth suggesting
GEOCODER = 'https://nominatim.openstreetmap.org/search'
CU_VIEWBOX = '-88.36,40.17,-88.14,40.04'  # Champaign, Urbana and Savoy: left, top, right, bottom
ALL = '*'  # Skill.route for "all buses"
TIMEOUT = 20  # the API's first answer after a quiet spell can take over 10 seconds


class MtdError(RuntimeError):
    pass


def meters(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def point(location):
    return (float(location['latitude']), float(location['longitude'])) if location else None


def parse_time(text):
    """An API date-time as an aware datetime (local time when it has no offset)."""
    if not text:
        return None
    t = dt.datetime.fromisoformat(text.replace('Z', '+00:00'))
    return t if t.tzinfo else t.astimezone()


def day_minutes(stop_time, field):
    """A timetable time as minutes after the service day's midnight (past midnight -> 24:xx).
    Timetable dates are placeholders; only the time of day means anything."""
    t = dt.datetime.fromisoformat(stop_time[field])
    return t.hour * 60 + t.minute + t.second / 60 + (1440 if stop_time.get(field.replace('Time', 'PastMidnight')) else 0)


def say_clock(t):
    t = t.astimezone()
    return f'{t.hour % 12 or 12}{":" + format(t.minute, "02d") if t.minute else ""} {"AM" if t.hour < 12 else "PM"}'


def say_name(name):
    return name.replace('&', 'and')


def minutes_until(departure, now):
    value = departure.get('minutesTillDeparture')
    if value not in (None, ''):
        return int(value)
    t = parse_time(departure.get('estimatedDeparture') or departure.get('scheduledDeparture'))
    return max(0, round((t - now).total_seconds() / 60)) if t else 0


def in_minutes(m):
    return 'now' if m <= 0 else 'in 1 minute' if m == 1 else f'in {m} minutes'


def and_list(items):
    items = [str(i) for i in items]
    return ', '.join(items[:-1]) + ' and ' + items[-1] if len(items) > 1 else items[0]


def walk_minutes(meters_apart):
    """Minutes on foot between two points: streets add about a quarter to the straight line."""
    return math.ceil(meters_apart * 1.25 / WALK_METERS_PER_MINUTE)


def route_label(departure):
    route = departure.get('route') or {}
    return ' '.join(p for p in (route.get('shortName'), route.get('longName')) if p) or 'bus'


def heading(departure):
    return ((departure.get('trip') or {}).get('direction') or {}).get('name') or departure.get('headsign')


class Mtd:
    def __init__(self, config, opener=urllib.request.urlopen, clock=time.time):
        self.key, self.url = config['api_key'], config['url'].rstrip('/')
        self.home, self.default_route = config['default_stop'], config['default_route']
        self.nearby, self.near_destination = config['nearby_meters'], config['destination_meters']
        self.options = config['max_options']
        self.open, self.clock = opener, clock
        self.cache = {}

    # ------------------------------------------------------------ fetching

    def cached(self, key, seconds, fetch):
        hit = self.cache.get(key)
        if hit and self.clock() - hit[0] < seconds:
            return hit[1]
        value = fetch()
        self.cache[key] = (self.clock(), value)
        return value

    def fetch_json(self, url, headers):
        request = urllib.request.Request(url, headers={'Accept': 'application/json',
                                                       'User-Agent': 'pi-voice-assistant (Clippy)', **headers})
        try:
            with self.open(request, timeout=TIMEOUT) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # MTD's answer for "no departures / no stop times here"
                exc.close()
                return {'result': []}
            raise MtdError(f'{url.split("?")[0]}: HTTP {exc.code}') from exc
        except (OSError, ValueError) as exc:
            raise MtdError(f'{url.split("?")[0]}: {exc}') from exc

    def get(self, path, **params):
        query = '?' + urllib.parse.urlencode(params) if params else ''
        data = self.fetch_json(self.url + path + query, {'X-ApiKey': self.key})
        if data.get('error'):
            raise MtdError(data['error'].get('message') or 'MTD error')
        return data.get('result') or []

    def stops(self):
        return self.cached('stops', 86400, lambda: self.get('/stops', excludeBoardingPoints='true'))

    def groups(self):
        return self.cached('groups', 86400, lambda: self.get('/routes/groups'))

    def departures(self, stop_id):
        return self.cached(f'departures:{stop_id}', 30,
                           lambda: self.get(f'/stops/{urllib.parse.quote(stop_id)}/departures'))

    def timetable(self, stop_id):
        """{trip id: (minutes into the service day, position on the trip)} at a stop."""
        def fetch():
            times = {}
            for st in self.get(f'/stops/{urllib.parse.quote(stop_id)}/schedule'):
                if st.get('tripId') and st.get('departureTime'):
                    times[st['tripId']] = (day_minutes(st, 'departureTime'), int(st.get('stopSequence') or 0))
            return times
        return self.cached(f'timetable:{stop_id}', 86400, fetch)

    def warm(self):
        """Load what every answer needs (stops, routes, the home stop and its neighbours'
        timetables, several seconds the first time) before anyone asks."""
        self.groups()
        home = self.find_stop(self.home)
        if home and home[2]:
            for stop_id, _, _ in self.near(home[2], self.nearby):
                self.timetable(stop_id)

    # ------------------------------------------------------------ places and routes

    def find_stop(self, name):
        """(stop group id, name, (lat, lon), exact) for a spoken stop name, or None. `exact`:
        every word said is in the stop's name, so it's that stop and not just a search hit."""
        def fetch():
            for query in dict.fromkeys((name, name.replace(' and ', ' & '))):
                results = self.get('/stops/search', query=query)
                if results:
                    break
            else:
                return None
            said = set(re.sub(r'[^a-z0-9 ]', ' ', name.lower()).split()) - {'and', 'the'}

            def words(result):
                return set(re.sub(r'[^a-z0-9 ]', ' ', result['name'].lower()).split())
            best = max(results, key=lambda r: (said <= words(r), ':' not in r['stopId']))
            return (best['stopId'].split(':')[0], best['name'], point(best.get('location')),
                    said <= words(best))
        return self.cached(f'stop:{name.lower()}', 86400, fetch)

    def locate(self, place):
        """(name, (lat, lon)) of a destination: an MTD stop by that name, else OpenStreetMap."""
        stop = self.find_stop(place)
        if stop and stop[3] and stop[2]:
            return stop[1], stop[2]

        def geocode():
            query = urllib.parse.urlencode({'q': place, 'format': 'jsonv2', 'limit': 1,
                                            'viewbox': CU_VIEWBOX, 'bounded': 1})
            found = self.fetch_json(f'{GEOCODER}?{query}', {})
            if not found or not isinstance(found, list):
                return None
            return found[0].get('name') or place, (float(found[0]['lat']), float(found[0]['lon']))
        return self.cached(f'place:{place.lower()}', 86400, geocode)

    def near(self, where, radius):
        """Stop groups within `radius` meters of a point, nearest first: [(stop id, name, meters)]."""
        found = []
        for stop in self.stops():
            location = point(stop.get('location'))
            if location and (distance := meters(where, location)) <= radius:
                found.append((stop['stopId'], stop['name'], distance))
        return sorted(found, key=lambda s: s[2])

    def route_groups(self, route):
        """Route group ids for what was said: None for every route; the default route when
        nothing names one. A number matches every group running it (22 -> Illini and
        Illini Limited, which run the 220 too)."""
        if route == ALL:
            return None
        groups, text = self.groups(), (route or '').lower().strip()
        if re.fullmatch(r'\d{1,3}', text):
            ids = {g['routeGroupId'] for g in groups if any(str(r.get('number')) == text for r in g.get('routes') or [])}
            return ids or {f'#{text}'}  # not in the route list: match departures by number
        named = [g for g in groups if re.search(rf'\b{re.escape(g["routeGroupName"].lower())}\b', text)]
        if named:  # "silver limited" names Silver Limited, not Silver
            longest = max(len(g['routeGroupName']) for g in named)
            return {g['routeGroupId'] for g in named if len(g['routeGroupName']) == longest}
        if text == self.default_route.lower():
            return None
        return self.route_groups(self.default_route)

    def group_name(self, ids):
        """What to call the routes: 'Illini' for Illini plus Illini Limited; None for a mix."""
        names = sorted({g['routeGroupName'] for g in self.groups() if g['routeGroupId'] in (ids or ())}, key=len)
        return names[0] if names and all(n.startswith(names[0]) for n in names) else None

    @staticmethod
    def on_route(departure, ids):
        route = departure.get('route') or {}
        return ids is None or route.get('routeGroupId') in ids or f'#{route.get("shortName")}' in ids

    # ------------------------------------------------------------ answers

    def report(self, stop='', route='', now=None):
        """When the next buses leave a stop (the home stop unless one is named)."""
        now = now or dt.datetime.now().astimezone()
        found = self.find_stop(stop or self.home)
        if not found:
            return f"I couldn't find a stop called {stop}."
        stop_id, stop_name = found[0], say_name(found[1])
        ids = self.route_groups(route)
        name = self.group_name(ids)
        coming = sorted((d for d in self.departures(stop_id) if self.on_route(d, ids)),
                        key=lambda d: minutes_until(d, now))[:3]
        if not coming:
            what = f'{name} bus' if name else 'bus'
            return f'No {what} is due at {stop_name} in the next hour.'
        waits = [minutes_until(d, now) for d in coming]
        headings = {heading(d) for d in coming}
        if name and len(headings) <= 1:
            sentence = f'The next {name} leaves {stop_name} {in_minutes(waits[0])}'
            if len(waits) > 1:
                sentence += ', then in ' + ' and '.join(str(w) for w in waits[1:]) + ' minutes'
        else:  # several routes or directions: "the 220 Illini going south in 3 and 9 minutes"
            runs = {}
            for d, w in zip(coming, waits):
                way = heading(d)
                runs.setdefault(route_label(d) + (f' going {way.lower()}' if len(headings) > 1 and way else ''),
                                []).append(w)
            parts = [f'the {label} {in_minutes(ws[0]) if len(ws) == 1 else "in " + and_list(ws) + " minutes"}'
                     for label, ws in runs.items()]
            sentence = f'From {stop_name}: ' + and_list(parts)
        if not any(d.get('isRealTime') for d in coming):
            sentence += '. Those are scheduled times'
        return sentence + '.'

    def plan(self, destination, now=None):
        """Which direct bus to take from the home stop (or a stop a short walk from it) to a
        place, with when it leaves and gets there: the best few options."""
        now = now or dt.datetime.now().astimezone()
        target = self.locate(destination)
        if not target:
            return f"I couldn't find {destination} around Champaign-Urbana."
        place, where = say_name(target[0]), target[1]
        home = self.find_stop(self.home)
        if not home or not home[2]:
            return f"I couldn't find the {self.home} stop."
        on_foot = walk_minutes(meters(home[2], where))
        if on_foot <= JUST_WALK:
            return f"{place} is about a {on_foot} minute walk from {say_name(home[1])}; that's quicker than a bus."
        origins = self.near(home[2], self.nearby)
        starts = {s[0] for s in origins}
        targets = [s for s in self.near(where, self.near_destination) if s[0] not in starts][:6]
        if not targets:
            return f'There is no bus stop near {place}.'

        reaches = {}  # trip -> [(minutes into the day, position, meters to the place)] near the destination
        for stop_id, _, distance in targets:
            for trip, (minute, position) in self.timetable(stop_id).items():
                reaches.setdefault(trip, []).append((minute, position, distance))

        options = {}  # one per route group: the earliest arrival
        for stop_id, stop_name, distance in origins:
            walk = math.ceil(distance / WALK_METERS_PER_MINUTE)
            departures = [d for d in self.departures(stop_id) if (d.get('trip') or {}).get('tripId') in reaches]
            if not departures:
                continue
            here = self.timetable(stop_id)
            for d in departures:
                trip, wait = d['trip']['tripId'], minutes_until(d, now)
                if wait < walk or trip not in here:
                    continue
                leaves_at, position = here[trip]
                later = [(m, dist) for m, p, dist in reaches[trip] if p > position]
                if not later:
                    continue  # this trip reaches the destination before it gets here
                ride, dest_distance = min((m - leaves_at, dist) for m, dist in later)
                leaves = parse_time(d.get('estimatedDeparture') or d.get('scheduledDeparture')) or \
                    now + dt.timedelta(minutes=wait)
                arrive = leaves + dt.timedelta(minutes=ride + math.ceil(dest_distance / WALK_METERS_PER_MINUTE))
                key = (d.get('route') or {}).get('routeGroupId') or route_label(d)
                if key not in options or arrive < options[key]['arrive']:
                    options[key] = dict(label=route_label(d), wait=wait, walk=walk, origin=stop_name, arrive=arrive)
        best = sorted(options.values(), key=lambda o: o['arrive'])[:self.options]
        walking = f"It's about a {on_foot} minute walk"
        if not best:
            return (f"I couldn't find a direct bus from {say_name(home[1])} to {place} in the next hour. "
                    f'{walking}.')
        if now + dt.timedelta(minutes=on_foot) <= best[0]['arrive']:
            return (f"Walking is quickest: {place} is about {on_foot} minutes on foot. The best bus, the "
                    f"{best[0]['label']} {in_minutes(best[0]['wait'])}, gets there around {say_clock(best[0]['arrive'])}.")
        lines = []
        for i, o in enumerate(best):
            start = (f'walk {o["walk"]} minute{"s" * (o["walk"] != 1)} to {say_name(o["origin"])} and take'
                     if o['walk'] else 'take')
            line = (f'{start} the {o["label"]} {in_minutes(o["wait"])}, getting you to {place} '
                    f'around {say_clock(o["arrive"])}')
            lines.append(('For ' + place + ', ' if i == 0 else 'Or ') + line)
        return '. '.join(lines) + '.'
