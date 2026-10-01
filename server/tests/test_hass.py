import asyncio
import json
import unittest

from voice_assistant.controls import Command
from voice_assistant.hass import STATE, Hub, entities


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, retain=False):
        self.published.append((topic, payload, retain))


def hub(voice_volume=0.3):
    h = Hub.__new__(Hub)  # skip the real MQTT connection
    h.client, h.session, h.loop = FakeClient(), None, None
    h.applied = []
    h.on_voice_volume = h.applied.append
    h.state = {'status': 'offline', 'transcript': '', 'reply': '', 'error': None, 'connected': False,
               'voice_volume': voice_volume, 'speaker_volume': None}
    return h


class EntityTests(unittest.TestCase):
    def test_entities(self):
        found = {(component, key) for component, key, _ in entities()}
        self.assertTrue({('sensor', 'status'), ('sensor', 'last_request'), ('sensor', 'last_reply'),
                         ('binary_sensor', 'echo_connected'), ('number', 'voice_volume'),
                         ('number', 'speaker_volume'), ('button', 'pairing_on'), ('button', 'pairing_off')} <= found)
        for component, key, fields in entities():
            self.assertTrue('state_topic' in fields or 'command_topic' in fields, key)
            if 'state_topic' in fields:
                self.assertEqual(fields['state_topic'], STATE)


class HubTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_volume_works_without_the_echo(self):
        h = hub()
        await h.handle('clippy/voice_volume/set', '55')
        self.assertEqual(h.applied, [0.55])
        await h.handle('clippy/voice_volume/set', '0')
        self.assertEqual(h.applied[-1], 0.05)  # never fully silent

    async def test_speaker_volume_and_pairing_need_the_echo(self):
        h = hub()
        with self.assertLogs('voice', 'WARNING'):
            await h.handle('clippy/speaker_volume/set', '40')
        with self.assertLogs('voice', 'WARNING'):
            await h.handle('clippy/command', 'pairing_on')

    async def test_commands_reach_the_attached_session(self):
        calls = []

        class Session:
            async def media_volume(self, change, level=None): calls.append((change, level))
            async def control(self, command): calls.append(command)
        h = hub()
        h.attach(Session())
        await h.handle('clippy/speaker_volume/set', '40')
        await h.handle('clippy/command', 'pairing_off')
        await h.handle('clippy/command', 'rm -rf')  # anything else is ignored
        self.assertEqual(calls, [('set', 0.4), Command('pairing_off')])

    def test_attach_and_detach_report_the_connection(self):
        h = hub()
        session = object()
        h.attach(session)
        self.assertTrue(json.loads(h.client.published[-1][1])['connected'])
        h.detach(object())  # a stale session can't detach the current one
        self.assertTrue(h.state['connected'])
        h.detach(session)
        last = json.loads(h.client.published[-1][1])
        self.assertEqual((last['connected'], last['status']), (False, 'offline'))
        self.assertTrue(all(retain for _, _, retain in h.client.published))


if __name__ == '__main__':
    unittest.main()
