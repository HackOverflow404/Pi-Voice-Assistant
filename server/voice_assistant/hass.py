"""Clippy as a Home Assistant device, over MQTT discovery.

Publishes the assistant's state (status, last request and reply, whether the Echo is
connected, the voice and speaker volumes) and takes a few commands from Home Assistant:
set either volume, start or stop Bluetooth pairing. MQTT runs in paho's own thread;
commands are handed to the server's event loop, where the session lives.

Optional: enabled by `mqtt.host` in config.yaml.
"""
import asyncio
import json
import logging

LOG = logging.getLogger('voice')
DISCOVERY = 'homeassistant'
BASE = 'clippy'
STATE = f'{BASE}/state'
AVAILABILITY = f'{BASE}/availability'
DEVICE = {'identifiers': ['clippy'], 'name': 'Clippy', 'manufacturer': 'Amazon (LineageOS)',
          'model': 'Echo Show 5', 'via_device': 'mainframe'}
TEXT_LIMIT = 250  # HA states max out at 255 characters; full text is in the attributes


def entities():
    """(component, key, discovery fields) for each entity; states come from the one JSON topic."""
    state = {'state_topic': STATE, 'json_attributes_topic': STATE}
    yield 'sensor', 'status', {**state, 'name': 'Status', 'icon': 'mdi:account-voice',
                               'value_template': '{{ value_json.status }}'}
    yield 'sensor', 'last_request', {**state, 'name': 'Last request', 'icon': 'mdi:account-voice',
                                     'value_template': '{{ value_json.transcript[:%d] }}' % TEXT_LIMIT}
    yield 'sensor', 'last_reply', {**state, 'name': 'Last reply', 'icon': 'mdi:message-reply-text',
                                   'value_template': '{{ value_json.reply[:%d] }}' % TEXT_LIMIT}
    yield 'binary_sensor', 'echo_connected', {
        'state_topic': STATE, 'name': 'Echo connected', 'device_class': 'connectivity',
        'value_template': "{{ 'ON' if value_json.connected else 'OFF' }}"}
    slider = {'state_topic': STATE, 'min': 0, 'max': 100, 'step': 5, 'unit_of_measurement': '%', 'mode': 'slider'}
    yield 'number', 'voice_volume', {**slider, 'name': 'Voice volume', 'icon': 'mdi:account-voice',
                                     'command_topic': f'{BASE}/voice_volume/set',
                                     'value_template': '{{ (value_json.voice_volume * 100) | round }}'}
    yield 'number', 'speaker_volume', {
        **slider, 'name': 'Speaker volume', 'icon': 'mdi:speaker', 'command_topic': f'{BASE}/speaker_volume/set',
        'value_template': "{{ (value_json.speaker_volume * 100) | round if value_json.speaker_volume is not none "
                          "else 'None' }}"}
    for action, name, icon in (('pairing_on', 'Start pairing', 'mdi:bluetooth-connect'),
                               ('pairing_off', 'Stop pairing', 'mdi:bluetooth-off')):
        yield 'button', action, {'name': name, 'icon': icon, 'command_topic': f'{BASE}/command', 'payload_press': action}


class Hub:
    """Owns the MQTT connection and the state HA sees; the current Session is attached while
    the Echo is connected. `on_voice_volume(level)` applies a voice volume change anywhere;
    speaker volume and pairing need the attached session."""
    def __init__(self, config, loop, voice_volume, on_voice_volume):
        import paho.mqtt.client as mqtt
        self.loop, self.on_voice_volume, self.session = loop, on_voice_volume, None
        self.state = {'status': 'offline', 'transcript': '', 'reply': '', 'error': None, 'connected': False,
                      'voice_volume': voice_volume, 'speaker_volume': None}
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='clippy-voice-assistant')
        self.client.will_set(AVAILABILITY, 'offline', retain=True)
        self.client.on_connect, self.client.on_message = self._on_connect, self._on_message
        self.client.connect_async(config['host'], config.get('port', 1883), keepalive=30)
        self.client.loop_start()

    def _on_connect(self, client, userdata, flags, reason, properties):
        if reason.is_failure:
            LOG.warning('Home Assistant MQTT connect failed: %s', reason)
            return
        for component, key, fields in entities():
            uid = f'clippy_{key}'
            config = {**fields, 'unique_id': uid, 'object_id': uid, 'availability_topic': AVAILABILITY,
                      'device': DEVICE}
            client.publish(f'{DISCOVERY}/{component}/{uid}/config', json.dumps(config), retain=True)
        client.publish(AVAILABILITY, 'online', retain=True)
        client.subscribe([(f'{BASE}/voice_volume/set', 0), (f'{BASE}/speaker_volume/set', 0), (f'{BASE}/command', 0)])
        self._publish()
        LOG.info('Home Assistant: Clippy published over MQTT')

    def _on_message(self, client, userdata, message):
        payload = message.payload.decode(errors='replace')
        asyncio.run_coroutine_threadsafe(self.handle(message.topic, payload), self.loop)

    async def handle(self, topic, payload):
        try:
            if topic.endswith('voice_volume/set'):
                self.on_voice_volume(min(1.0, max(0.05, float(payload) / 100)))
            elif topic.endswith('speaker_volume/set'):
                if not self.session:
                    raise RuntimeError('the Echo is not connected')
                await self.session.media_volume('set', min(1.0, max(0.0, float(payload) / 100)))  # reports itself
            elif topic.endswith('command') and payload in ('pairing_on', 'pairing_off'):
                if not self.session:
                    raise RuntimeError('the Echo is not connected')
                from .controls import Command
                await self.session.control(Command(payload))
        except Exception as exc:
            LOG.warning('Home Assistant command %s %s failed: %s', topic, payload, exc)

    def attach(self, session):
        self.session = session
        self.update(connected=True)

    def detach(self, session):
        if self.session is session:
            self.session = None
            self.update(connected=False, status='offline')

    def update(self, **changes):
        self.state.update(changes)
        self._publish()

    def _publish(self):
        self.client.publish(STATE, json.dumps(self.state), retain=True)

    def close(self):
        self.client.publish(AVAILABILITY, 'offline', retain=True).wait_for_publish(timeout=5)
        self.client.loop_stop()
        self.client.disconnect()
