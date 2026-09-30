import datetime as dt
import math
import tempfile
import unittest
from zoneinfo import ZoneInfo

from voice_assistant import skills
from voice_assistant.skills import Clock, Skill, parse, parse_duration, parse_math


class DurationTests(unittest.TestCase):
    def test_spoken_and_written_durations(self):
        for text, seconds in [('5 minutes', 300), ('five minutes', 300), ('an hour and a half', 5400),
                              ('two and a half minutes', 150), ('1 hour 30 minutes', 5400),
                              ('half an hour', 1800), ('ninety seconds', 90), ('a minute', 60),
                              ('twenty five minutes', 1500), ('1.5 hours', 5400), ('3 min', 180),
                              ('one hour and fifteen minutes', 4500)]:
            self.assertEqual(parse_duration(text), seconds, text)
        self.assertIsNone(parse_duration('set a timer'))

    def test_say_duration(self):
        self.assertEqual(skills.say_duration(90), '1 minute 30 seconds')
        self.assertEqual(skills.say_duration(3600), '1 hour')


class MathTests(unittest.TestCase):
    def check(self, text, value):
        answer = parse_math(text)
        self.assertIsNotNone(answer, text)
        self.assertAlmostEqual(answer[1], value, msg=text)

    def test_spoken_and_symbol_arithmetic(self):
        self.check("What's 12 times 7?", 84)
        self.check('what is twelve times seven', 84)
        self.check('12 × 7', 84)
        self.check('100 divided by 8', 12.5)
        self.check('15 percent of 80', 12)
        self.check('square root of 144', 12)
        self.check('2 to the power of 10', 1024)
        self.check('3 plus 4 times 2', 11)          # precedence
        self.check('5 squared', 25)
        self.check('negative 3 plus 10', 7)
        self.check('two point five times four', 10)
        self.check('1,000 minus 1', 999)
        self.check('10/4', 2.5)

    def test_not_arithmetic_or_advanced(self):
        for text in ('what is the derivative of x squared', 'integrate sine x', 'what is love',
                     'solve for x in 2x plus 3 equals 7', 'what times is it', 'how many minutes in a fortnight'):
            self.assertIsNone(parse_math(text), text)

    def test_divide_by_zero(self):
        answer = parse_math('5 divided by 0')
        self.assertTrue(math.isnan(answer[1]))


class GrammarTests(unittest.TestCase):
    def test_timers(self):
        self.assertEqual(parse('Set a timer for 5 minutes.'), Skill('timer_start', seconds=300))
        self.assertEqual(parse('set a pasta timer for 10 minutes'), Skill('timer_start', seconds=600, label='pasta'))
        self.assertEqual(parse('10 minute timer for the eggs'), Skill('timer_start', seconds=600, label='eggs'))
        self.assertEqual(parse('timer for an hour and a half'), Skill('timer_start', seconds=5400))
        self.assertEqual(parse('remind me in 20 minutes'), Skill('timer_start', seconds=1200))
        self.assertEqual(parse('pause the timer'), Skill('timer_pause'))
        self.assertEqual(parse('pause the pasta timer').label, 'pasta')
        self.assertEqual(parse('resume the timer'), Skill('timer_resume'))
        self.assertEqual(parse('cancel all timers'), Skill('timer_stop', all=True))
        self.assertEqual(parse('stop the timer'), Skill('timer_stop'))
        self.assertEqual(parse('how much time is left on the timer').action, 'timer_query')
        self.assertIsNone(parse('how do timers work'))

    def test_stopwatch(self):
        self.assertEqual(parse('start the stopwatch'), Skill('stopwatch_start'))
        self.assertEqual(parse('start a stop watch'), Skill('stopwatch_start'))
        self.assertEqual(parse('pause the stopwatch'), Skill('stopwatch_pause'))
        self.assertEqual(parse('resume the stopwatch'), Skill('stopwatch_resume'))
        self.assertEqual(parse('stop the stopwatch'), Skill('stopwatch_stop'))
        self.assertEqual(parse("what's the stopwatch at"), Skill('stopwatch_query'))

    def test_time_date_weather_calendar_math(self):
        self.assertEqual(parse('What time is it?'), Skill('time_query'))
        self.assertEqual(parse("what's the time"), Skill('time_query'))
        self.assertEqual(parse("What's the date today?"), Skill('date_query'))
        self.assertEqual(parse('what day is it'), Skill('date_query'))
        self.assertEqual(parse("What's the weather like?"), Skill('weather_query'))
        self.assertEqual(parse('is it going to rain tomorrow'), Skill('weather_query', day='tomorrow'))
        self.assertEqual(parse("what's on my calendar tomorrow"), Skill('calendar_query', day='tomorrow'))
        self.assertEqual(parse("What's 12 times 7?").action, 'math')

    def test_requests_for_instinct_are_left_alone(self):
        for text in ('What assignments do I have left tonight?', 'Remind me to do the laundry tomorrow.',
                     'what time does the library close', 'what is the time complexity of quicksort',
                     'text mom that I will be late'):
            self.assertIsNone(parse(text), text)

    def test_dismiss_only_while_ringing(self):
        self.assertEqual(parse('Stop.', ringing=True), Skill('dismiss'))
        self.assertEqual(parse('Okay, thanks.', ringing=True), Skill('dismiss'))
        self.assertIsNone(parse('Stop.'))

    def test_from_action_reads_parameters(self):
        self.assertEqual(skills.from_action('timer_start', 'could you count down eight minutes').seconds, 480)
        self.assertIsNone(skills.from_action('timer_start', 'start a timer').seconds)   # asks how long
        self.assertIsNone(skills.from_action('math', 'what is the integral of x'))


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.t = 1000.0
        self.clock = Clock(self.temp.name, now=lambda: self.t)

    def tearDown(self):
        self.temp.cleanup()

    def test_timer_lifecycle_rings_and_persists(self):
        self.assertEqual(self.clock.start_timer(300, 'pasta'), 'Pasta timer set for 5 minutes.')
        self.t += 60
        self.assertEqual(self.clock.pause_timer(), 'Paused, with 4 minutes left.')
        self.t += 500                       # paused: no time passes
        self.assertEqual(self.clock.resume_timer(), 'Resumed, 4 minutes to go.')
        self.t += 240
        self.assertTrue(self.clock.tick())
        self.assertTrue(self.clock.ringing())
        again = Clock(self.temp.name, now=lambda: self.t)        # survives a restart
        self.assertEqual(again.snapshot()[0]['state'], 'ringing')
        self.assertTrue(self.clock.dismiss())
        self.assertFalse(self.clock.timers)

    def test_ringing_ends_by_itself(self):
        self.clock.start_timer(10)
        self.t += 10 + skills.RING_SECONDS
        self.clock.tick()
        self.assertFalse(self.clock.timers)

    def test_named_and_all(self):
        self.clock.start_timer(60, 'eggs')
        self.clock.start_timer(120, 'pasta')
        self.assertEqual(self.clock.stop_timer('eggs'), 'Eggs timer cancelled.')
        self.assertEqual([t.label for t in self.clock.timers], ['pasta'])
        self.clock.start_timer(30)
        self.assertEqual(self.clock.stop_timer(everything=True), 'All timers cancelled.')

    def test_stopwatch(self):
        self.assertEqual(self.clock.start_stopwatch(), 'Stopwatch started.')
        self.t += 75
        self.assertEqual(self.clock.pause_stopwatch(), 'Stopwatch paused at 1 minute 15 seconds.')
        self.t += 100
        self.clock.resume_stopwatch()
        self.t += 5
        self.assertEqual(self.clock.snapshot()[0]['elapsed_ms'], 80000)
        self.assertEqual(self.clock.stop_stopwatch(), 'Stopwatch stopped at 1 minute 20 seconds.')

    def test_listeners_hear_changes(self):
        heard = []
        self.clock.listeners.append(lambda: heard.append(1))
        self.clock.start_timer(5)
        self.assertEqual(heard, [1])


class SayingTests(unittest.TestCase):
    def test_time_and_date(self):
        self.assertEqual(skills.say_time(dt.datetime(2026, 9, 29, 16, 5)), "It's 4:05 PM.")
        self.assertEqual(skills.say_time(dt.datetime(2026, 9, 29, 9, 0)), "It's 9 AM.")
        self.assertEqual(skills.say_date(dt.datetime(2026, 9, 29)), "It's Tuesday, September 29.")

    def test_calendar_report(self):
        zone = ZoneInfo('America/Chicago')
        now = dt.datetime(2026, 9, 29, 10, 0, tzinfo=zone)
        ms = lambda h, m=0, d=29: int(dt.datetime(2026, 9, d, h, m, tzinfo=zone).timestamp() * 1000)
        events = [dict(title='ECE 374', begin=ms(9, 30), end=ms(10, 45), all_day=False),
                  dict(title='ECE 428', begin=ms(14), end=ms(15, 15), all_day=False),
                  dict(title='Demo', begin=ms(13, 20, 30), end=ms(13, 40, 30), all_day=False)]
        self.assertEqual(skills.calendar_report(events, now), 'You have 2 events: ECE 374 at 9:30 AM and ECE 428 at 2 PM.')
        self.assertEqual(skills.calendar_report(events, now, 'tomorrow'), 'Tomorrow you have Demo at 1:20 PM.')
        self.assertEqual(skills.math_reply(Skill('math', expression='12 times 7', value=84.0)), '12 times 7 is 84.')
