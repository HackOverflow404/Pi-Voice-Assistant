import subprocess
import tempfile
import unittest

from voice_assistant import controls
from voice_assistant.controls import Command, Echo, Jev, Levels, parse, spoken_level


class GrammarTests(unittest.TestCase):
    def test_pairing(self):
        for said in ('Start pairing.', 'Hey Clippy, start pairing mode.', 'Turn on Bluetooth pairing',
                     'Pair a new device.', 'Can you make yourself discoverable?', 'Pairing mode on'):
            self.assertEqual(parse(said), Command('pairing_on'), said)
        for said in ('Stop pairing.', 'Exit pairing mode, please.', 'Turn off Bluetooth pairing.'):
            self.assertEqual(parse(said), Command('pairing_off'), said)

    def test_relative_volume(self):
        cases = {'Turn the volume up.': 'system_up', 'Volume down': 'system_down',
                 'Turn it up a bit.': 'system_up', 'Louder!': 'system_up',
                 'Increase the system volume.': 'system_up', 'Lower the music volume': 'system_down',
                 'Turn up your volume.': 'ai_up', 'Speak louder.': 'ai_up', 'Talk softer please': 'ai_down',
                 'Decrease the AI volume.': 'ai_down', 'Voice volume up': 'ai_up'}
        for said, action in cases.items():
            self.assertEqual(parse(said), Command(action), said)

    def test_absolute_volume(self):
        cases = {'Set the volume to 50%.': Command('system_set', 0.5),
                 'Set volume to fifty percent': Command('system_set', 0.5),
                 'Volume 7.': Command('system_set', 0.7),
                 'Set your volume to thirty five percent.': Command('ai_set', 0.35),
                 'Max volume.': Command('system_set', 1.0),
                 'Mute.': Command('system_set', 0.0),
                 'Set the assistant volume to 100 percent': Command('ai_set', 1.0)}
        for said, command in cases.items():
            self.assertEqual(parse(said), command, said)

    def test_questions_are_not_commands(self):
        for said in ("What's the volume of a sphere?", 'How do I pair my AirPods with my phone?',
                     'Remind me to turn the volume down on the TV later.', 'Set a timer for 5 minutes.',
                     'Play something louder than this.', 'Volume 250.'):
            self.assertIsNone(parse(said), said)

    def test_spoken_level(self):
        self.assertEqual(spoken_level('could you make the music about 40 percent'), 0.4)
        self.assertEqual(spoken_level('put it on eight'), 0.8)
        self.assertIsNone(spoken_level('make it quieter'))


def answer(choice, probability, options):
    rest = (1 - probability) / (len(options) - 1)
    return {'type': 'choice', 'choice': choice, 'confidence': probability,
            'probabilities': {o: probability if o == choice else rest for o in options}}


class FakeJev(Jev):
    def __init__(self, route, route_p, action='none', action_p=0.9):
        super().__init__({'api_key': 'test'})
        self.response = {'model': 'jev-latest', 'answers': {
            'route': answer(route, route_p, controls.ROUTES),
            'device_action': answer(action, action_p, controls.DEVICE_ACTIONS)}}
        self.sent = None

    def call(self, text):
        self.sent = self.request(text)
        return self.response


class JevTests(unittest.TestCase):
    def test_request_asks_both_questions_in_one_call(self):
        jev = FakeJev('instinct', 0.9)
        jev.decide('what is on my calendar')
        self.assertEqual(set(jev.sent['questions']), {'route', 'device_action'})
        self.assertEqual(jev.sent['state']['transcript'], 'what is on my calendar')
        self.assertTrue(all(q['type'] == 'choice' for q in jev.sent['questions'].values()))

    def test_routes(self):
        self.assertEqual(FakeJev('instinct', 0.9).decide('x')[0], 'instinct')
        self.assertEqual(FakeJev('cancel', 0.8).decide('x')[0], 'cancel')
        self.assertEqual(FakeJev('accidental', 0.8).decide('x')[0], 'accidental')
        self.assertEqual(FakeJev('device', 0.9, 'pairing_on').decide('x')[0], Command('pairing_on'))

    def test_unsure_goes_to_instinct(self):
        self.assertEqual(FakeJev('accidental', 0.5).decide('x')[0], 'instinct')
        self.assertEqual(FakeJev('device', 0.9, 'system_up', 0.4).decide('x')[0], 'instinct')
        self.assertEqual(FakeJev('device', 0.9, 'none').decide('x')[0], 'instinct')

    def test_set_needs_a_level(self):
        self.assertEqual(FakeJev('device', 0.9, 'system_set').decide('music at forty percent please')[0],
                         Command('system_set', 0.4))
        self.assertEqual(FakeJev('device', 0.9, 'system_set').decide('change the volume')[0], 'instinct')


class Recorder:
    def __init__(self, outputs):
        self.outputs, self.commands = outputs, []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        args = command[3:] if command[1:2] == ['-s'] else command[1:]
        return subprocess.CompletedProcess(command, 0, stdout=self.outputs.get(tuple(args), ''), stderr='')


class EchoTests(unittest.TestCase):
    def test_start_pairing_gets_root_then_opens_pair_new_device(self):
        run = Recorder({('shell', 'id', '-u'): '2000'})
        Echo({'adb_serial': 'SERIAL'}, run).start_pairing()
        commands = [c[3:] for c in run.commands]  # after adb -s SERIAL
        self.assertTrue(all(c[:3] == ['adb', '-s', 'SERIAL'] for c in run.commands))
        self.assertEqual(commands[1], ['root'])
        self.assertIn(controls.PAIRING_FRAGMENT, commands[-1])

    def test_stop_pairing_only_leaves_settings(self):
        activities = ('shell', 'dumpsys', 'activity', 'activities')
        run = Recorder({activities: 'mResumedActivity: ActivityRecord{1 u0 com.android.settings/.SubSettings t1}'})
        Echo({}, run).stop_pairing()
        self.assertEqual(run.commands[-1][-2:], ['-n', controls.DASHBOARD])
        run = Recorder({activities: 'mResumedActivity: ActivityRecord{1 u0 com.spotify.music/.Main t2}'})
        Echo({}, run).stop_pairing()
        self.assertEqual(len(run.commands), 1)

    def test_adb_failure_raises(self):
        def fail(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, stdout='', stderr='device offline')
        with self.assertRaises(RuntimeError):
            Echo({}, fail).stop_pairing()


class LevelsTests(unittest.TestCase):
    def test_voice_volume_persists(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(Levels(folder, 0.3).speech_volume, 0.3)
            Levels(folder, 0.3).speech_volume = 0.6
            self.assertEqual(Levels(folder, 0.3).speech_volume, 0.6)


if __name__ == '__main__':
    unittest.main()
