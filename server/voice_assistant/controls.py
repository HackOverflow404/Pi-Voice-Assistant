"""Spoken device commands: the Echo's Bluetooth pairing and system (media) and assistant
voice volumes, and the RF desk lamp (through its remote, wired to the Pi; see rf-lamp).

Every transcript is routed one of four ways: a device command (handled here, never sent),
a cancellation, an accidental trigger (both discarded), or a request for Instinct. A small
fixed grammar recognizes device commands instantly; anything it doesn't match goes to Jev
(TypeSafe's decision model) when an API key is configured, which answers the route and the
device action together in one request. Without Jev, or when Jev fails or is unsure, the
request goes to Instinct exactly as before."""
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.request

from . import skills

# The lamp's 12 remote buttons: action -> (rf-lamp button name, what it does, spoken reply).
# The lamp can't report its state, so on/off is a toggle and is confirmed as one.
LAMP = {
    'lamp_on_off': ('on/off', 'Turn the desk lamp on or off (one toggle button).', 'Toggled the lamp.'),
    'lamp_timer': ('1h', "Start the lamp's one-hour auto-off timer.", 'The lamp will turn off in an hour.'),
    'lamp_brighter': ('brightness up', 'Make the lamp brighter.', 'Lamp brighter.'),
    'lamp_dimmer': ('brightness down', 'Make the lamp dimmer.', 'Lamp dimmer.'),
    'lamp_warmer': ('warmth up', "Make the lamp's light warmer, more yellow.", 'Lamp warmer.'),
    'lamp_cooler': ('warmth down', "Make the lamp's light cooler, whiter or bluer.", 'Lamp cooler.'),
    'lamp_sun': ('sun', "Set the lamp to its bright daylight (sun) preset.", 'Lamp on sun mode.'),
    'lamp_k': ('k', "Press the lamp's K (colour temperature) button.", 'Lamp K mode.'),
    'lamp_night': ('night', "Set the lamp to its dim night light preset.", 'Night light on.'),
    'lamp_milk': ('milk', "Set the lamp to its soft milky white preset.", 'Lamp on milk mode.'),
    'lamp_book': ('book', "Set the lamp to its reading (book) preset.", 'Reading light on.'),
    'lamp_laptop': ('laptop', "Set the lamp to its laptop or screen-work preset.", 'Lamp on laptop mode.'),
}
ACTIONS = ('pairing_on', 'pairing_off', 'system_up', 'system_down', 'system_set',
           'ai_up', 'ai_down', 'ai_set', *LAMP)
STEP = 0.1  # "volume up/down" moves this fraction of the full range


@dataclass(frozen=True)
class Command:
    action: str
    level: float = None  # 0-1, for the *_set actions


# ---------------------------------------------------------------- grammar (fast path)

# Politeness and address that may pad a command: "hey Clippy, could you please turn it up".
FILLER = re.compile(r'\b(?:hey|ok(?:ay)?|clippy|please|can you|could you|would you|will you|'
                    r'i want you to|i want to|i\'d like to|just|now|a bit|a little(?: bit)?|some|'
                    r'for me|thanks|thank you)\b')
NUMBER_WORDS = {w: i for i, w in enumerate(
    'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen '
    'sixteen seventeen eighteen nineteen'.split())}
NUMBER_WORDS.update({w: 10 * i for i, w in enumerate('_ _ twenty thirty forty fifty sixty seventy eighty ninety'
                                                     .split()) if i >= 2})
NUMBER_WORDS['hundred'] = 100
# A number is digits or number words ("thirty five", "a hundred"), never ordinary words.
NUMBER = (r'(?:\d{1,3}|(?:a |one )?hundred|(?:' + '|'.join(sorted(NUMBER_WORDS, key=len, reverse=True)) +
          r')(?: (?:one|two|three|four|five|six|seven|eight|nine))?)')

AI = r'(?:your|you|voice|assistant|ai|a i|speech|talking|clippy\'?s?)'
SYSTEM = r'(?:system|media|music|speaker|device|echo|main)'
TARGET = rf'(?:(?P<target>{AI}|{SYSTEM}) )?'
UP = r'(?:up|louder|higher|increase|raise|boost)'
DOWN = r'(?:down|quieter|softer|lower|decrease|reduce)'
LEVEL = rf'(?P<level>{NUMBER})(?: (?P<percent>percent))?'

PAIRING_ON = re.compile(r'(?:(?:start|begin|enter|turn on|enable|open|activate|go into|put (?:yourself|it) in)'
                        r' (?:the )?(?:bluetooth )?pairing(?: mode)?|(?:bluetooth )?pairing(?: mode)? on|'
                        r'pair (?:a |with a )?(?:new )?(?:device|phone|laptop|computer|speaker)|'
                        r'make (?:yourself|it) (?:discoverable|visible)|be discoverable)')
PAIRING_OFF = re.compile(r'(?:(?:stop|end|exit|cancel|turn off|disable|close|leave|quit)'
                         r' (?:the )?(?:bluetooth )?pairing(?: mode)?|(?:bluetooth )?pairing(?: mode)? off|'
                         r'stop being (?:discoverable|visible))')
VOLUME_PATTERNS = [
    # "turn the volume up", "turn up your volume", "volume down", "increase the system volume"
    (re.compile(rf'(?:turn|crank|bring|move) (?:the )?{TARGET}volume (?P<dir>up|down)'), None),
    (re.compile(rf'(?:turn|crank|bring|move) (?P<dir>up|down) (?:the )?{TARGET}volume'), None),
    (re.compile(rf'(?:the )?{TARGET}volume (?P<dir>{UP}|{DOWN})'), None),
    (re.compile(rf'(?P<dir>{UP}|{DOWN}) (?:the )?{TARGET}volume'), None),
    # "turn it up", "louder", "speak louder", "talk softer"
    (re.compile(rf'turn (?:it|that|this) (?P<dir>up|down)'), 'system'),
    (re.compile(rf'(?:be |get |go )?(?P<dir>louder|quieter|softer)'), 'system'),
    (re.compile(rf'(?:speak|talk|say it) (?P<dir>louder|quieter|softer|up)'), 'ai'),
    # "set the volume to 50 percent", "volume 7", "your volume to thirty percent"
    (re.compile(rf'(?:set|change|put|make) (?:the )?{TARGET}volume (?:to |at )?{LEVEL}'), None),
    (re.compile(rf'(?:the )?{TARGET}volume (?:to |at )?{LEVEL}'), None),
    # "max volume", "mute the system volume"
    (re.compile(rf'(?P<max>max(?:imum)?|full) (?:the )?{TARGET}volume'), None),
    (re.compile(rf'(?:the )?{TARGET}volume (?:to )?(?P<max>max(?:imum)?|full)'), None),
    (re.compile(rf'(?P<mute>mute)(?: (?:the )?{TARGET}volume| (?:the )?(?:speaker|music|media))?'), 'system'),
]


def normalize(text):
    text = text.lower().replace('%', ' percent ')
    text = re.sub(r'[^a-z0-9\' ]', ' ', text)
    text = FILLER.sub(' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def parse_number(words):
    """'50', 'fifty', 'thirty five', 'a hundred' -> int, else None."""
    if words.isdigit():
        return int(words)
    words = re.sub(r'^(?:a|one) hundred$', 'hundred', words)
    if words in NUMBER_WORDS:
        return NUMBER_WORDS[words]
    tens, _, ones = words.partition(' ')
    if tens in NUMBER_WORDS and ones in NUMBER_WORDS and NUMBER_WORDS[tens] >= 20 and NUMBER_WORDS[ones] < 10:
        return NUMBER_WORDS[tens] + NUMBER_WORDS[ones]
    return None


def level_from(number, percent):
    """0-10 without "percent" is a ten-step scale ("volume 7" = 70%); otherwise a percentage."""
    if number is None or number < 0:
        return None
    level = number / 10 if number <= 10 and not percent else number / 100
    return level if level <= 1 else None


def spoken_level(text):
    """The level named anywhere in the transcript, for a Jev-routed *_set action."""
    for match in re.finditer(rf'\b({NUMBER})(?: (percent))?\b', normalize(text)):
        level = level_from(parse_number(match.group(1)), match.group(2))
        if level is not None:
            return level
    return None


def target_of(word):
    return 'ai' if word and re.fullmatch(AI, word) else 'system'


LAMP_WORD = r'(?:the |my |desk )*(?:lamp|light|lights)'
TURN = r'(?:turn|switch|flip|put)'
LAMP_PATTERNS = [
    (re.compile(rf'(?:{TURN} (?:on|off) {LAMP_WORD}|{TURN} {LAMP_WORD} (?:on|off)|{LAMP_WORD} (?:on|off)|'
                rf'toggle {LAMP_WORD})'), 'lamp_on_off'),
    # The lamp must be named: "set a one hour timer" alone is the timers skill's.
    (re.compile(rf'(?:(?:set|start) (?:an? |the |one )?(?:1 |one )?hour (?:lamp|light) timer|'
                rf'(?:set|start) (?:an? |the |one )?(?:1 |one )?hour timer (?:for|on) {LAMP_WORD}|'
                rf'{LAMP_WORD} (?:1 |one )?hour timer|{LAMP_WORD} timer|'
                rf'{TURN} (?:off )?{LAMP_WORD}(?: off)? in (?:an|one|1) hour)'), 'lamp_timer'),
    (re.compile(rf'(?:make {LAMP_WORD} brighter|brighten {LAMP_WORD}|{LAMP_WORD} brighter|'
                rf'{LAMP_WORD} brightness up|(?:turn|bring) (?:up {LAMP_WORD}|{LAMP_WORD} up))'), 'lamp_brighter'),
    (re.compile(rf'(?:make {LAMP_WORD} dimmer|dim {LAMP_WORD}|{LAMP_WORD} dimmer|'
                rf'{LAMP_WORD} brightness down|(?:turn|bring) (?:down {LAMP_WORD}|{LAMP_WORD} down))'), 'lamp_dimmer'),
    (re.compile(rf'(?:make {LAMP_WORD} warmer|{LAMP_WORD} warmer|{LAMP_WORD} warmth up|warmer {LAMP_WORD})'),
     'lamp_warmer'),
    (re.compile(rf'(?:make {LAMP_WORD} (?:cooler|colder)|{LAMP_WORD} (?:cooler|colder)|{LAMP_WORD} warmth down|'
                rf'(?:cooler|colder) {LAMP_WORD})'), 'lamp_cooler'),
]
# Presets by name: "lamp to sun mode", "book mode", "set the light to reading", "night light".
PRESETS = {'sun': 'lamp_sun', 'daylight': 'lamp_sun', 'k': 'lamp_k', 'kay': 'lamp_k', 'night': 'lamp_night',
           'night light': 'lamp_night', 'milk': 'lamp_milk', 'book': 'lamp_book', 'reading': 'lamp_book',
           'laptop': 'lamp_laptop', 'computer': 'lamp_laptop'}
PRESET = '|'.join(sorted(PRESETS, key=len, reverse=True))
PRESET_PATTERN = re.compile(
    rf'(?:(?:set|switch|put|change|turn) {LAMP_WORD} (?:to|on|onto) (?:the )?|{LAMP_WORD} (?:to |on )?(?:the )?|'
    rf'(?:the )?)(?P<preset>{PRESET})(?: (?P<mode>mode|preset|setting|light))?(?: on {LAMP_WORD})?')


def parse_lamp(said):
    for pattern, action in LAMP_PATTERNS:
        if pattern.fullmatch(said):
            return Command(action)
    match = PRESET_PATTERN.fullmatch(said)
    # A preset word alone ("book", "the sun") is too ambiguous: it counts only with the lamp
    # named, a mode word ("book mode", "reading light"), or as "night light" itself.
    if match and (re.search(rf'\b{LAMP_WORD}\b', said) or match.group('mode')
                  or match.group('preset') == 'night light'):
        return Command(PRESETS[match.group('preset')])
    return None


def parse(text):
    """The device command that the whole utterance is, or None. It must match in full, so
    a question that merely mentions volume or pairing still goes to Instinct."""
    said = normalize(text)
    lamp = parse_lamp(said)
    if lamp:
        return lamp
    if PAIRING_ON.fullmatch(said):
        return Command('pairing_on')
    if PAIRING_OFF.fullmatch(said):
        return Command('pairing_off')
    for pattern, fixed_target in VOLUME_PATTERNS:
        match = pattern.fullmatch(said)
        if not match:
            continue
        groups = match.groupdict()
        target = fixed_target or target_of(groups.get('target'))
        if groups.get('mute'):
            return Command('system_set', 0.0)
        if groups.get('max'):
            return Command(f'{target}_set', 1.0)
        if groups.get('dir'):
            return Command(f'{target}_{"up" if re.fullmatch(UP, groups["dir"]) else "down"}')
        if groups.get('level'):
            level = level_from(parse_number(groups['level']), groups.get('percent'))
            if level is not None:
                return Command(f'{target}_set', level)
    return None


# ---------------------------------------------------------------- Jev (fallback router)

CONTEXT = ('Speech-to-text of what a microphone on an Echo Show speaker picked up just after it heard '
           'something like its wake word "Hey Clippy". The microphone hears the whole room, and the wake '
           'word sometimes fires by mistake on similar-sounding speech, TV or music, so the transcript may '
           'be words that were never meant for the assistant at all.')

# Asked alongside the route: a confident "no" discards the transcript whatever the route says, so
# a remark or overheard sentence is never sent on just because it could be read as a message.
INSTRUCTION_VERBS = ('text, message, email, call, tell, let someone know, send, remind, note, remember, add '
                     'or schedule')
ADDRESSED = {
    'yes': 'Said to the assistant on purpose, expecting it to respond or act: a question for it, or an '
           'instruction or request to it. Anything that starts by asking it to ' + INSTRUCTION_VERBS +
           ' is meant for it, whatever the rest says, because the rest is the content to send or keep: '
           '"text mom I\'m running late", "text dad happy birthday", "note that my exam is on Friday" '
           'and "tell Instinct I finished the assignment" are all meant for the assistant.',
    'no': 'Not meant for the assistant as a command, question or message: a remark, reaction or '
          'exclamation ("ugh, I\'m so tired", "that was amazing"); thinking out loud or a question to '
          'oneself ("where did I put my keys"); a rhetorical question; talking to another person in the '
          'room or on a call; talking about the assistant rather than to it ("Clippy never hears me"); '
          'narrating or retelling what someone said; reading text aloud, singing or quoting; dialogue '
          'from a TV, video, game or song; or a fragment, filler or noise. A statement with no '
          'instruction to the assistant ("I\'m running late again") is not meant for it.',
}
ROUTES = {
    'instinct': 'A question, request or message spoken to the voice assistant on purpose, that the '
                'speaker cannot handle by itself: knowledge, assignments, email, reminders, notes, messages '
                'to send, and advanced math (algebra, calculus, statistics, proofs, unit conversions, word '
                'problems). An instruction to ' + INSTRUCTION_VERBS + ' something belongs here even when '
                'what follows is a plain statement, such as "text dad happy birthday".',
    'device': 'Something this speaker does itself, asked for or instructed rather than merely mentioned: '
              'Bluetooth pairing mode or volume; the desk lamp (on or '
              'off, brighter, dimmer, warmer, cooler, a one-hour timer, or presets like sun, night, milk, '
              'reading and laptop); setting, pausing, '
              'resuming, cancelling or checking a timer; starting, pausing, stopping or checking a '
              'stopwatch; saying the current time or date; simple arithmetic; or the local weather or the '
              'user\'s calendar events. Not advanced math, assignments, email or general knowledge.',
    'cancel': 'The speaker is cancelling or retracting their request, for example "never mind", '
              '"ignore that", "actually cancel" or "forget it".',
    'accidental': 'Not meant for the assistant: a remark or reaction, thinking out loud, talking to '
                  'someone else or about the assistant, background TV, video or music, a fragment, filler '
                  'sounds, or speech-recognition noise. Never an instruction to the assistant to ' +
                  INSTRUCTION_VERBS + ' something.',
}
DEVICE_ACTIONS = {
    'pairing_on': 'Start Bluetooth pairing mode so a new device can connect.',
    'pairing_off': 'Stop Bluetooth pairing mode.',
    'system_up': 'Raise the speaker\'s system or media volume (music, Bluetooth audio).',
    'system_down': 'Lower the speaker\'s system or media volume.',
    'system_set': 'Set the system or media volume to a specific level, or mute or maximize it.',
    'ai_up': 'Raise the assistant\'s own speaking voice volume.',
    'ai_down': 'Lower the assistant\'s own speaking voice volume.',
    'ai_set': 'Set the assistant\'s own speaking voice volume to a specific level.',
    'timer_start': 'Start a countdown timer for some duration.',
    'timer_pause': 'Pause a running timer.',
    'timer_resume': 'Resume a paused timer.',
    'timer_stop': 'Cancel or stop a timer.',
    'timer_query': 'Ask how much time is left on a timer.',
    'dismiss': 'Silence a timer alarm that is ringing ("stop", "okay").',
    'stopwatch_start': 'Start the stopwatch.',
    'stopwatch_pause': 'Pause the stopwatch.',
    'stopwatch_resume': 'Resume the stopwatch.',
    'stopwatch_stop': 'Stop or reset the stopwatch.',
    'stopwatch_query': 'Ask what the stopwatch shows.',
    'time_query': 'Ask the current time.',
    'date_query': 'Ask today\'s date or day of the week.',
    'math': 'Simple arithmetic: adding, subtracting, multiplying, dividing, percentages, powers or square roots of numbers.',
    'weather_query': 'Ask about the local weather or temperature, now or tomorrow.',
    'calendar_query': 'Ask what is on the user\'s calendar or schedule today or tomorrow, or what is next.',
    **{action: description for action, (_, description, _) in LAMP.items()},
    'none': 'Not a device command.',
}


class Jev:
    """One Jev call answers both questions, the route and (if it is a device command) which
    one; Jev evaluates all questions of a request in a single parallel pass."""
    def __init__(self, config, url='https://api.typesafe.ai/v1/systemone'):
        self.key, self.url = config['api_key'], config.get('url', url)
        self.model = config.get('model', 'jev-latest')
        self.min_probability = config.get('min_probability', 0.6)
        self.timeout = config.get('timeout_seconds', 3)

    def request(self, text):
        return {'model': self.model,
                'state': {'transcript': text, 'context': CONTEXT},
                'questions': {
                    'addressed': {'type': 'choice',
                                  'instructions': 'Was this said to the assistant on purpose, as a command, '
                                                  'a question for it, or a message for it to handle?',
                                  'criteria': ADDRESSED},
                    'route': {'type': 'choice', 'instructions': 'What should happen with this transcript?',
                              'criteria': ROUTES},
                    'device_action': {'type': 'choice',
                                      'instructions': 'If the speaker should do this itself, which action?',
                                      'criteria': DEVICE_ACTIONS}}}

    def call(self, text):
        request = urllib.request.Request(self.url, data=json.dumps(self.request(text)).encode(), headers={
            'Authorization': f'Bearer {self.key}', 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors='replace').strip()[:200]
            raise RuntimeError(f'Jev {exc.code}: {detail}') from exc

    def decide(self, text):
        """('instinct' | 'cancel' | 'accidental' | Command, route probability). The "addressed"
        answer settles whether it was meant for the assistant at all: a confident no discards it, and
        a confident yes stops an "accidental" route from discarding a real instruction such as "text
        mom I'm running late". Anything else unsure or incomplete falls back to Instinct, which is
        what happened before Jev."""
        answers = self.call(text)['answers']
        route = answers['route']
        probability = route['probabilities'].get(route['choice'], 0.0)
        addressed = answers.get('addressed') or {'choice': None, 'probabilities': {}}
        meant = addressed['probabilities'].get('yes', 0.0) if addressed['choice'] == 'yes' else 0.0
        not_meant = addressed['probabilities'].get('no', 0.0) if addressed['choice'] == 'no' else 0.0
        if not_meant >= self.min_probability and route['choice'] != 'cancel':
            return 'accidental', not_meant
        if route['choice'] == 'instinct' or probability < self.min_probability:
            return 'instinct', probability
        if route['choice'] == 'accidental' and meant >= self.min_probability:
            return 'instinct', meant
        if route['choice'] in ('cancel', 'accidental'):
            return route['choice'], probability
        action = answers['device_action']
        if action['probabilities'].get(action['choice'], 0.0) < self.min_probability:
            return 'instinct', probability
        if action['choice'] in skills.ACTIONS:
            skill = skills.from_action(action['choice'], text)
            return (skill or 'instinct'), probability
        if action['choice'] not in ACTIONS:
            return 'instinct', probability
        if action['choice'].endswith('_set'):
            level = spoken_level(text)
            if level is None:
                return 'instinct', probability
            return Command(action['choice'], level), probability
        return Command(action['choice']), probability


# ---------------------------------------------------------------- the desk lamp

class Lamp:
    """Presses the lamp's remote buttons by running rf-lamp's send.py, which drives the
    remote's data line from a GPIO. It runs under the system Python, which has pigpio."""
    def __init__(self, config, run=subprocess.run):
        self.command = list(config['command'])
        self.run_process = run

    def press(self, action):
        button = LAMP[action][0]
        result = self.run_process(self.command + [button], capture_output=True, text=True, timeout=15,
                                  stdin=subprocess.DEVNULL)
        if result.returncode != 0:
            raise RuntimeError(f'lamp {button} failed: {(result.stderr or result.stdout).strip()[:200]}')


# ---------------------------------------------------------------- the Echo, over the Pi's adb

SETTINGS = 'com.android.settings/.SubSettings'
PAIRING_FRAGMENT = 'com.android.settings.bluetooth.BluetoothPairingDetail'
DASHBOARD = 'com.instinct.voice/.MainActivity'


class Echo:
    """Pairing through the Echo's own "Pair new device" screen, which keeps it discoverable
    for as long as it is open. That screen isn't exported, so adb must run as root (the
    LineageOS userdebug build allows `adb root`); it is re-enabled after an Echo reboot."""
    def __init__(self, config, run=subprocess.run):
        self.serial = config.get('adb_serial')
        self.run_process = run

    def adb(self, *args, timeout=15):
        command = ['adb'] + (['-s', self.serial] if self.serial else []) + list(args)
        result = self.run_process(command, capture_output=True, text=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL)
        if result.returncode != 0:
            raise RuntimeError(f'adb {args[0]} failed: {(result.stderr or result.stdout).strip()[:200]}')
        return result.stdout.strip()

    def ensure_root(self):
        if self.adb('shell', 'id', '-u') != '0':
            self.adb('root', timeout=30)
            self.adb('wait-for-device', timeout=30)

    def start_pairing(self):
        self.ensure_root()
        self.adb('shell', 'input', 'keyevent', 'KEYCODE_WAKEUP')
        self.adb('shell', 'am', 'start', '-n', SETTINGS, '-e', ':settings:show_fragment', PAIRING_FRAGMENT)

    def bonded(self):
        """{address: name} of the Echo's paired devices."""
        devices, inside = {}, False
        for line in self.adb('shell', 'dumpsys', 'bluetooth_manager').splitlines():
            if line.strip() == 'Bonded devices:':
                inside = True
                continue
            match = re.match(r'\s+([0-9A-F]{2}(?::[0-9A-F]{2}){5}) \[[^]]*\] ?(.*)', line) if inside else None
            if not match:
                if inside:
                    break
                continue
            devices[match.group(1)] = match.group(2).strip()
        return devices

    def pairing_open(self):
        activities = self.adb('shell', 'dumpsys', 'activity', 'activities')
        resumed = next((line for line in activities.splitlines() if 'mResumedActivity' in line), '')
        return 'com.android.settings/' in resumed

    def stop_pairing(self):
        """Back to the dashboard, which ends discoverability; left alone if the user has
        already moved on from Settings to something else."""
        if self.pairing_open():
            self.adb('shell', 'am', 'start', '-n', DASHBOARD)


# ---------------------------------------------------------------- persisted levels

class Levels:
    """The assistant's voice volume, persisted in the state directory so a spoken change
    outlives restarts; config.yaml's tts.speech_volume is only the starting value."""
    def __init__(self, state_dir, default):
        self.path = Path(state_dir) / 'controls.json'
        self.values = {'speech_volume': default}
        try:
            self.values.update(json.loads(self.path.read_text()))
        except (OSError, ValueError):
            pass

    @property
    def speech_volume(self):
        return self.values['speech_volume']

    @speech_volume.setter
    def speech_volume(self, value):
        self.values['speech_volume'] = value
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.values))
        tmp.replace(self.path)


def clamp(level, lowest=0.0):
    return round(min(1.0, max(lowest, level)), 2)


def percent(level):
    return f'{round(level * 100)} percent'
