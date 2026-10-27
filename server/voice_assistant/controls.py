"""Spoken device commands: the Echo's Bluetooth pairing and system (media) and assistant
voice volumes, Luxo Jr. the RF floor lamp (through its remote, wired to the Pi; see rf-lamp), and
finding the user's phone (a Home Assistant critical alert to the Companion app).

Every transcript is routed one of four ways: a device command (handled here, never sent),
a cancellation, an accidental trigger (both discarded), or a request for Instinct. A small
fixed grammar recognizes device commands instantly; anything it doesn't match goes to Jev
(TypeSafe's decision model) when an API key is configured, which answers the route and the
device action together in one request. Without Jev, or when Jev fails or is unsure, the
request goes to Instinct exactly as before."""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.request

from . import skills

# The lamp's 12 remote buttons: action -> (rf-lamp button name, what it does, spoken reply).
# The lamp can't report its state, so on/off is one toggle button; lamp_reply confirms it in
# the words the user asked with.
LAMP = {
    'lamp_on_off': ('on/off', 'Turn Luxo Jr., the floor lamp, on or off (one toggle button).', 'Toggled Luxo Junior.'),
    'lamp_timer': ('1h', "Start the lamp's one-hour auto-off timer.", 'Luxo Junior will turn off in an hour.'),
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
    'lamp_goodnight': ('on/off', 'Saying goodnight or going to sleep: turn the lamp off for the night.',
                       'Goodnight! Turned off Luxo Junior.'),
}
ACTIONS = ('pairing_on', 'pairing_off', 'system_up', 'system_down', 'system_set',
           'ai_up', 'ai_down', 'ai_set', 'phone_ping', *LAMP)
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


# Luxo Jr. is the lamp's name ("turn on Luxo", "Luxo Junior brighter").
LAMP_WORD = r'(?:the |my |floor |desk )*(?:lamp|light|lights|lux ?o(?: jr| junior)?)'
# What speech recognition makes of "lamp" on the Echo's narrowband microphone (seen in the logs:
# "turn on my lap", "turn on my load", "turn off my laptop"). Only accepted after "turn on/off my",
# where nothing else this speaker controls would fit.
LAMP_MISHEARD = r'(?:the |my |desk )+(?:lap|laps|lab|lamb|lump|load|loud|land|lamps|laptop|lam)'
TURN = r'(?:turn|switch|flip|put)'
LAMP_PATTERNS = [
    (re.compile(rf'{TURN} (?:on|off) {LAMP_MISHEARD}|{TURN} {LAMP_MISHEARD} (?:on|off)'), 'lamp_on_off'),
    (re.compile(r'(?:good ?night|nighty? night|night night)(?: everyone| all| buddy)?'), 'lamp_goodnight'),
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


def lamp_reply(action, text):
    """What to say after pressing a lamp button. On and off are the same button, so the reply
    echoes what was asked for ("turn on my lamp" -> "Turned on the lamp."), and only says
    "toggled" when the request named neither."""
    if action == 'lamp_on_off':
        said = normalize(text)
        if re.search(r'\b(?:off|out)\b', said):
            return 'Turned off Luxo Junior.'
        if re.search(r'\bon\b', said):
            return 'Turned on Luxo Junior.'
    return LAMP[action][2]


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


# "Find my phone", "ping my iPhone", "where's my phone", "I can't find my phone".
PHONE_WORD = r'(?:my |the )?(?:i ?phone|phone|cell(?: phone)?|mobile)'
PHONE_PATTERN = re.compile(
    rf'(?:help me )?(?:find|ping|ring|locate|buzz|beep) {PHONE_WORD}|'
    rf'(?:where\'?s|where is|where did i (?:put|leave)) {PHONE_WORD}|'
    rf'i (?:can\'?t|cannot|can not) find {PHONE_WORD}|'
    rf'(?:make|have) {PHONE_WORD} (?:ring|beep|make a (?:sound|noise))|play a sound on {PHONE_WORD}')


def parse(text):
    """The device command that the whole utterance is, or None. It must match in full, so
    a question that merely mentions volume or pairing still goes to Instinct."""
    said = normalize(text)
    if PHONE_PATTERN.fullmatch(said):
        return Command('phone_ping')
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
           'something like its wake word "Hey Clippy". The speaker is a university student\'s desk '
           'assistant: it handles a few things itself (Luxo Jr. the floor lamp, volume, timers, finding the phone, the '
           'time and weather, and MTD bus times and which bus to take) and '
           'passes everything else to Instinct, the student\'s personal AI assistant. The microphone hears '
           'the whole room, and the wake word sometimes fires by mistake on similar-sounding speech, TV or '
           'music, so the transcript may be words that were never meant for the assistant at all. The '
           'microphone is quiet and muffled, so speech recognition garbles short lamp commands: "lamp" comes '
           'out as "lap", "load", "lab" or "laptop", and "turn off" as "turn over" or "turn off now". When "recent" is given, it is the user\'s last exchange with '
           'Instinct, and this transcript may continue it.')

# What Instinct can do, so Jev knows which requests are its (jev.instinct in config.yaml replaces it).
INSTINCT = ('Instinct is the user\'s personal AI assistant, reached by message, which knows their life and '
            'acts for them: their calendar and schedule (adding, moving or changing events, planning or '
            'replanning the day around what is left to do, and anything about events beyond today and '
            'tomorrow); coursework (assignments, homework and what is due, '
            'exams, PrairieLearn and PrairieTest, booking CBTF exam slots, open lab sign-ups, class meetings '
            'and rooms); student organizations and campus events (MicroTech general meetings, EOH); their to-do list and time tracker; job applications and online assessments; email; '
            'texts to other people; reminders and notes; groceries, orders and what to cook; and anything '
            'that needs knowledge or a web search: facts, explanations, news, TV releases, travel times '
            '(but not local bus times or which local bus to take: the speaker checks those itself), '
            'opening hours and advanced math.')

# Asked alongside the route: a confident "no" discards the transcript whatever the route says, so
# a remark or overheard sentence is never sent on just because it could be read as a message.
INSTRUCTION_VERBS = ('text, message, email, call, tell, let someone know, send, remind, note, remember, add '
                     'or schedule')
ADDRESSED = {
    'yes': 'Said to the assistant on purpose, expecting it to respond or act: a question for it, or an '
           'instruction or request to it. Anything that starts by asking it to ' + INSTRUCTION_VERBS +
           ' is meant for it, whatever the rest says, because the rest is the content to send or keep: '
           '"text mom I\'m running late", "text dad happy birthday", "note that my exam is on Friday" '
           'and "tell Instinct I finished the assignment" are all meant for the assistant. So is a statement '
           'that asks it to act or corrects something it did: "I just finished eating, replan my day", '
           '"I can\'t make class Monday, so find me an open lab", "change my calendar, the event actually '
           'runs from seven to eight". So is information given to Instinct even with no request in it, '
           'when it answers a question Instinct just asked or adds details or corrections to a recent '
           'request (see "recent"): after Instinct asks for a birthday, "the birthday is October 25, and '
           'it\'s not the same person" is meant for it.',
    'no': 'Not meant for the assistant as a command, question or message: a remark, reaction or '
          'exclamation ("ugh, I\'m so tired", "that was amazing"); thinking out loud or a question to '
          'oneself ("where did I put my keys"); a rhetorical question; talking to another person in the '
          'room or on a call, including suggestions and invitations to them ("come on, let\'s go '
          'shopping", "let\'s grab food"); talking about the assistant rather than to it ("Clippy never hears me"); '
          'narrating or retelling what someone said; reading text aloud, singing or quoting; dialogue '
          'from a TV, video, game or song; or a fragment, filler or noise. A statement about the speaker\'s '
          'own plans or day that asks nothing of the assistant ("I can\'t make my 9 AM class Monday", '
          '"I also need to make food for dinner", "ugh, I\'m running late again") is usually conversation '
          'in the room, so it is not meant for it, unless it answers or follows up the recent exchange with '
          'Instinct.',
}
ROUTES = {
    'instinct': 'A question, request, message or update spoken to the assistant on purpose that the '
                'speaker cannot handle by itself, so it goes to Instinct. ' + INSTINCT + ' Statements '
                'that ask it to act on the user\'s plans, and corrections to what Instinct did, belong here; '
                'a bare statement about the user\'s day with no request in it does not. An '
                'instruction to ' + INSTRUCTION_VERBS + ' something belongs here even when what follows is a '
                'plain statement, such as "text dad happy birthday". So does a question to the assistant '
                'about how the user\'s work, applications or plans are going. So does an answer to a question '
                'Instinct just asked, or more details, context or a correction for a recent request, even '
                'with no request of its own.',
    'device': 'Something this speaker does itself, asked for or instructed rather than merely mentioned: '
              'Bluetooth pairing mode or volume; Luxo Jr., the floor lamp (on or '
              'off, brighter, dimmer, warmer, cooler, a one-hour timer, or presets like sun, night, milk, '
              'reading and laptop); ringing the user\'s phone to find it; setting, pausing, '
              'resuming, cancelling or checking a timer; starting, pausing, stopping or checking a '
              'stopwatch; saying the current time or date; simple arithmetic; the local weather; when the '
              'next MTD bus leaves a stop, or which bus to take to a place in Champaign-Urbana; or '
              'reading out the events already on the user\'s calendar today or tomorrow ("what\'s on my '
              'calendar tomorrow", "what\'s my next meeting"). Not assignments, '
              'homework, what is due, exams, to-do lists, planning, scheduling, advanced math, email or '
              'general knowledge: those are Instinct\'s.',
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
    'calendar_query': 'Ask which events are on the user\'s calendar today or tomorrow, or what the next '
                      'event is. Not assignments, homework, what is due, exams, deadlines, to-do items or '
                      'planning the day.',
    'bus_query': 'Ask when the next MTD bus leaves a stop, optionally for a route by number or name '
                 '("when\'s the next bus", "when does the 22 come", "next Illini at the Union", "what buses '
                 'are coming"). Not a destination: that is bus_trip.',
    'bus_trip': 'Ask which bus to take to get to a place in Champaign-Urbana, or when the next bus to a '
                'place leaves ("which bus should I take to Target", "how do I get to the Union by bus", '
                '"next bus to Siebel"). Not travel to other cities.',
    'phone_ping': 'Ring the user\'s phone so they can find it, or say where it is ("find my phone", '
                  '"where\'s my phone", "I can\'t find my phone").',
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
        self.routes = dict(ROUTES)
        if config.get('instinct'):
            self.routes['instinct'] = ROUTES['instinct'].replace(INSTINCT, config['instinct'].strip())

    def request(self, text, recent=None):
        state = {'transcript': text, 'context': CONTEXT}
        if recent:
            state['recent'] = recent
        return {'model': self.model,
                'state': state,
                'questions': {
                    'addressed': {'type': 'choice',
                                  'instructions': 'Was this said to the assistant on purpose, as a command, '
                                                  'a question for it, or a message for it to handle?',
                                  'criteria': ADDRESSED},
                    'route': {'type': 'choice', 'instructions': 'What should happen with this transcript?',
                              'criteria': self.routes},
                    'device_action': {'type': 'choice',
                                      'instructions': 'If the speaker should do this itself, which action?',
                                      'criteria': DEVICE_ACTIONS}}}

    def call(self, text, recent=None):
        request = urllib.request.Request(self.url, data=json.dumps(self.request(text, recent)).encode(), headers={
            'Authorization': f'Bearer {self.key}', 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors='replace').strip()[:200]
            raise RuntimeError(f'Jev {exc.code}: {detail}') from exc

    def decide(self, text, recent=None):
        """('instinct' | 'cancel' | 'accidental' | Command, route probability). The "addressed"
        answer settles whether it was meant for the assistant at all: a confident no discards it, and
        a confident yes stops an "accidental" route from discarding a real instruction such as "text
        mom I'm running late". Anything else unsure or incomplete falls back to Instinct, which is
        what happened before Jev."""
        answers = self.call(text, recent)['answers']
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


RECENT_SECONDS = 600  # an exchange with Instinct this recent is context for the next request


def recent_exchange(request, now):
    """The last request to Instinct and its reply, as context for Jev, while it is recent."""
    if not request or now - request['created'] > RECENT_SECONDS:
        return None
    minutes = round((now - request['created']) / 60)
    when = 'Just now' if minutes < 1 else f'{minutes} minute{"s" * (minutes != 1)} ago'
    text = f'{when} the user asked Instinct: "{request["transcript"]}".'
    reply = (request.get('reply') or '').strip()
    if reply and reply != 'Voice message':
        text += f' Instinct replied: "{reply[:800]}"'
    elif reply:
        text += ' Instinct replied with a voice message.'
    return text


# ---------------------------------------------------------------- Luxo Jr. (the floor lamp)

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


# ---------------------------------------------------------------- the user's phone

def ago(seconds):
    if seconds < 90:
        return 'a moment ago'
    if seconds < 5400:
        return f'{round(seconds / 60)} minutes ago'
    if seconds < 172800:
        hours = round(seconds / 3600)
        return 'an hour ago' if hours == 1 else f'{hours} hours ago'
    return f'{round(seconds / 86400)} days ago'


class Phone:
    """Rings the phone through Home Assistant: a Companion-app critical alert, which sounds
    even on silent or in a Focus. Then says where HA last saw it (its device tracker)."""
    def __init__(self, config, opener=urllib.request.urlopen):
        self.url, self.token = config['ha_url'].rstrip('/'), config['token']
        self.notify, self.tracker = config['notify'], config['tracker']
        self.open = opener

    def api(self, method, path, body=None):
        request = urllib.request.Request(
            f'{self.url}/api/{path}', method=method, data=None if body is None else json.dumps(body).encode(),
            headers={'Authorization': f'Bearer {self.token}', 'Content-Type': 'application/json'})
        try:
            with self.open(request, timeout=8) as response:
                return json.loads(response.read() or b'null')
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f'Home Assistant {exc.code} for {path}') from exc

    def ping(self, now=None):
        """Ring it and return what to say."""
        self.api('POST', f'services/notify/{self.notify}', {
            'title': 'Clippy', 'message': '📍 Clippy is looking for your phone',
            'data': {'push': {'sound': {'name': 'default', 'critical': 1, 'volume': 1.0}}}})
        try:
            where = self.where(self.api('GET', f'states/{self.tracker}'), now)
        except Exception:  # the ring went out; the location is a bonus
            where = ''
        return f'Ringing your phone.{where}'

    @staticmethod
    def where(tracker, now=None):
        zone = tracker.get('state')
        if zone in (None, 'unknown', 'unavailable'):
            return ''
        place = {'home': 'at home', 'not_home': 'away from home'}.get(zone, f'at {zone}')
        seen = datetime.fromisoformat(tracker['last_updated'])
        seconds = ((now or datetime.now(timezone.utc)) - seen).total_seconds()
        return f' It was last {place} {ago(max(seconds, 0))}.'


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
