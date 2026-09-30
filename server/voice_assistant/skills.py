"""Built-in skills answered on the Pi without Instinct: timers, a stopwatch, the time and
date, simple arithmetic, and the local weather and calendar.

`parse` recognizes the common wordings instantly (the fast path); Jev picks the skill for
other wordings and `from_action` then reads the parameters (a duration, an expression)
from the words, since Jev answers choices, not values. Anything that can't be parsed goes
to Instinct, which also handles advanced math."""
from dataclasses import dataclass
import datetime as dt
import json
import math
from pathlib import Path
import re
import threading
import time
import urllib.request
import uuid

ACTIONS = ('timer_start', 'timer_pause', 'timer_resume', 'timer_stop', 'timer_query', 'dismiss',
           'stopwatch_start', 'stopwatch_pause', 'stopwatch_resume', 'stopwatch_stop', 'stopwatch_query',
           'time_query', 'date_query', 'math', 'weather_query', 'calendar_query')
RING_SECONDS = 60  # a finished timer rings this long unless dismissed


@dataclass(frozen=True)
class Skill:
    action: str
    seconds: float = None   # timer_start
    label: str = ''         # which timer ("pasta"), when named
    all: bool = False       # "cancel all timers"
    expression: str = ''    # math: the expression as it will be read back
    value: float = None     # math: its result
    day: str = 'today'      # calendar_query / weather_query: today or tomorrow


# ---------------------------------------------------------------- words and numbers

UNITS = {w: i for i, w in enumerate(
    'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen '
    'sixteen seventeen eighteen nineteen'.split())}
TENS = {w: 10 * i for i, w in enumerate('_ _ twenty thirty forty fifty sixty seventy eighty ninety'.split()) if i >= 2}
SCALES = {'hundred': 100, 'thousand': 1000, 'million': 10 ** 6}
NUMBER_WORD = set(UNITS) | set(TENS) | set(SCALES) | {'a', 'an', 'point'}


def normalize(text):
    """Lower case with math symbols spelled out; keeps digits, decimal points and colons."""
    text = text.lower()
    for symbol, word in (('×', ' times '), ('*', ' times '), ('÷', ' divided by '), ('−', ' minus '),
                         ('+', ' plus '), ('^', ' to the power of '), ('√', ' square root of '),
                         ('%', ' percent '), ('=', ' ')):
        text = text.replace(symbol, word)
    text = re.sub(r'(?<=\d),(?=\d{3})', '', text)                         # 1,000 -> 1000
    text = re.sub(r'(?<=\d)\s*/\s*(?=\d)', ' divided by ', text)          # 10/2
    text = re.sub(r'(?<=\d)\s*-\s*(?=\d)', ' minus ', text)               # 10-2
    text = re.sub(r'(?<=\d)x(?=\d)|(?<=\d) x (?=\d)', ' times ', text)    # 3x4, 3 x 4
    text = re.sub(r"[^a-z0-9.:' ]", ' ', text).replace("'", ' ')
    text = re.sub(r'\b(?:hey|ok(?:ay)?|clippy|please|can you|could you|would you|will you|for me|'
                  r'thanks|thank you|um+|uh+)\b', ' ', text)
    text = re.sub(r'(?<!\d)\.|\.(?!\d)', ' ', text)                       # sentence dots, keep 2.5
    return re.sub(r'\s+', ' ', text).strip()


def read_number(words, i):
    """Parse a number starting at words[i]: digits ('2.5'), or words ('twenty five',
    'one hundred and ten', 'a'). Returns (value, next index) or (None, i)."""
    if i < len(words) and re.fullmatch(r'\d+(?:\.\d+)?', words[i]):
        return float(words[i]), i + 1
    if i < len(words) and words[i] in ('a', 'an'):
        return 1.0, i + 1
    total, current, j, seen = 0, 0, i, False
    while j < len(words):
        w = words[j]
        if w in UNITS:
            current += UNITS[w]
        elif w in TENS:
            current += TENS[w]
        elif w == 'hundred':
            current = (current or 1) * 100
        elif w in ('thousand', 'million'):
            total += (current or 1) * SCALES[w]
            current = 0
        elif w == 'and' and seen and j + 1 < len(words) and (words[j + 1] in UNITS or words[j + 1] in TENS):
            j += 1
            continue
        elif w == 'point' and seen and j + 1 < len(words) and words[j + 1] in UNITS:
            decimals = ''
            j += 1
            while j < len(words) and words[j] in UNITS and UNITS[words[j]] < 10:
                decimals += str(UNITS[words[j]])
                j += 1
            return total + current + float('0.' + decimals), j
        else:
            break
        seen = True
        j += 1
    return (float(total + current), j) if seen else (None, i)


def spoken(value):
    """A number as it should be read back: '84', '2.5', '0.3333'."""
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f'{value:.4g}' if abs(value) >= 1e-4 else f'{value:.3e}'


# ---------------------------------------------------------------- durations

DURATION_UNITS = {'hour': 3600, 'hours': 3600, 'hr': 3600, 'hrs': 3600, 'h': 3600,
                  'minute': 60, 'minutes': 60, 'min': 60, 'mins': 60, 'm': 60,
                  'second': 1, 'seconds': 1, 'sec': 1, 'secs': 1, 's': 1}


def parse_duration(text):
    """Seconds named in the text: '5 minutes', 'an hour and a half', 'two and a half
    minutes', '1 hour 30 minutes', 'half an hour', 'ninety seconds'. None if there is none."""
    words = normalize(text).split()
    total, found, i = 0.0, False, 0
    while i < len(words):
        if words[i] == 'half' and i + 2 < len(words) and words[i + 1] in ('a', 'an') and words[i + 2] in DURATION_UNITS:
            total += 0.5 * DURATION_UNITS[words[i + 2]]
            found, i = True, i + 3
            continue
        value, j = read_number(words, i)
        if value is None:
            i += 1
            continue
        if words[j:j + 3] in (['and', 'a', 'half'], ['and', 'one', 'half']):  # "two and a half minutes"
            value, j = value + 0.5, j + 3
        if j < len(words) and words[j] in DURATION_UNITS:
            unit = DURATION_UNITS[words[j]]
            total += value * unit
            found, j = True, j + 1
            if words[j:j + 3] == ['and', 'a', 'half']:                     # "an hour and a half"
                total += 0.5 * unit
                j += 3
        i = max(j, i + 1)
    return total if found and total > 0 else None


def say_duration(seconds):
    """'1 hour 5 minutes', '90 seconds' -> '1 minute 30 seconds'."""
    seconds = int(round(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    parts = [f'{n} {unit}{"" if n == 1 else "s"}' for n, unit in ((h, 'hour'), (m, 'minute'), (s, 'second')) if n]
    return ' '.join(parts) or '0 seconds'


# ---------------------------------------------------------------- arithmetic

class MathError(ValueError):
    pass


def tokenize_math(text):
    """Spoken arithmetic -> tokens: numbers and + - * / ^ sqrt % ( ). Raises MathError on
    anything it doesn't understand, so non-arithmetic never gets a made-up answer."""
    words = normalize(text).split()
    phrases = [(['multiplied', 'by'], '*'), (['divided', 'by'], '/'), (['to', 'the', 'power', 'of'], '^'),
               (['raised', 'to', 'the', 'power', 'of'], '^'), (['raised', 'to'], '^'), (['to', 'the'], '^'),
               (['square', 'root', 'of'], 'sqrt'), (['the', 'square', 'root', 'of'], 'sqrt'),
               (['root', 'of'], 'sqrt'), (['percent', 'of'], '%of'), (['plus'], '+'), (['add'], '+'),
               (['minus'], '-'), (['less'], '-'), (['negative'], 'neg'), (['times'], '*'), (['x'], '*'),
               (['over'], '/'), (['squared'], 'sq'), (['cubed'], 'cube'), (['percent'], '%'),
               (['mod'], 'mod'), (['modulo'], 'mod'), (['open', 'bracket'], '('), (['close', 'bracket'], ')')]
    tokens, i = [], 0
    while i < len(words):
        for phrase, op in phrases:
            if words[i:i + len(phrase)] == phrase:
                tokens.append(op)
                i += len(phrase)
                break
        else:
            value, j = read_number(words, i)
            if value is None or (words[i] in ('a', 'an') and not tokens):
                raise MathError(words[i])
            tokens.append(value)
            i = j
            # "to the 3rd": ordinal powers are rare; plain numbers only
    return tokens


def evaluate(tokens):
    """Recursive-descent evaluation with the usual precedence."""
    pos = 0

    def peek():
        return tokens[pos] if pos < len(tokens) else None

    def take():
        nonlocal pos
        pos += 1
        return tokens[pos - 1]

    def atom():
        token = take() if peek() is not None else None
        if token is None:
            raise MathError('incomplete')
        if token == 'neg' or token == '-':
            return -atom()
        if token == 'sqrt':
            value = atom()
            if value < 0:
                raise MathError('negative root')
            return math.sqrt(value)
        if token == '(':
            value = expression()
            if take() != ')':
                raise MathError('bracket')
            return value
        if isinstance(token, float):
            value = token
            while peek() in ('sq', 'cube', '%'):
                op = take()
                value = value ** 2 if op == 'sq' else value ** 3 if op == 'cube' else value / 100
            return value
        raise MathError(str(token))

    def power():
        base = atom()
        if peek() == '^':
            take()
            exponent = power()
            if abs(exponent) > 100:
                raise MathError('too big')
            return base ** exponent
        return base

    def term():
        value = power()
        while peek() in ('*', '/', 'mod', '%of'):
            op, right = take(), power()
            if op == '*':
                value *= right
            elif op == '%of':
                value = value / 100 * right
            elif right == 0:
                raise ZeroDivisionError
            else:
                value = value / right if op == '/' else value % right
        return value

    def expression():
        value = term()
        while peek() in ('+', '-'):
            op, right = take(), term()
            value = value + right if op == '+' else value - right
        return value

    result = expression()
    if pos != len(tokens):
        raise MathError('trailing')
    return result


MATH_LEAD = re.compile(r'^(?:what s|what is|whats|how much is|calculate|compute|work out|what do you get for|'
                       r'what does|solve)\s+')
MATH_TAIL = re.compile(r'\s+(?:equal|equals|make|come to|is)$')
MATH_OPS = re.compile(r'\b(?:plus|minus|times|multiplied|divided|over|power|squared|cubed|root|percent|mod|'
                      r'modulo|add|less|x)\b')


def parse_math(text):
    """(expression as read back, value) for simple arithmetic, else None."""
    said = MATH_TAIL.sub('', MATH_LEAD.sub('', normalize(text)))
    if not MATH_OPS.search(said):
        return None
    try:
        tokens = tokenize_math(said)
        if sum(isinstance(t, float) for t in tokens) < 1 or len(tokens) < 2:
            return None
        value = evaluate(tokens)
    except ZeroDivisionError:
        return said, math.nan
    except (MathError, OverflowError, ValueError):
        return None
    if isinstance(value, complex) or math.isinf(value):
        return None
    return said, value


# ---------------------------------------------------------------- the fast-path grammar

TIMER = r'(?:timer|timers|countdown|count down)'
STOPWATCH = r'(?:stop ?watch|stopwatch)'
THE = r'(?:(?:the|my|a|an|all|all the|all my|this|that)\s+)?'
DISMISS_WORDS = re.compile(r'(?:stop|ok|okay|dismiss|done|enough|quiet|silence|shut up|got it|turn it off|'
                           r'stop it|that s enough|stop (?:the )?(?:alarm|timer|ringing|beeping|sound|noise))')
TIME_QUERY = re.compile(r'(?:what s|what is|whats|tell me|say|give me|do you have|check)? ?(?:the )?(?:current )?time'
                        r'(?: is it)?(?: now| right now)?|what time is it(?: now| right now)?|what s the time')
DATE_QUERY = re.compile(r'(?:what s|what is|whats|tell me) (?:the |today s |todays )?date(?: today)?|'
                        r'what day is (?:it|today)(?: today)?|what s today|what is today|what s the day(?: today)?')
WEATHER_QUERY = re.compile(r'(?:what s|what is|whats|how s|how is|tell me) the (?:weather|temperature|forecast)'
                           r'(?: like)?(?: (?:outside|today|tomorrow|now|right now|tonight))?|weather|'
                           r'(?:is it|will it|is it going to|it is going to) (?:rain|snow|be (?:cold|hot|warm|sunny))'
                           r'(?: today| tomorrow| later| tonight)?|how (?:cold|hot|warm) is it(?: outside)?(?: today)?|'
                           r'do i need (?:an umbrella|a jacket|a coat)(?: today| tomorrow)?')
CALENDAR_QUERY = re.compile(r'(?:what s|what is|whats) (?:on )?(?:my )?(?:calendar|schedule|agenda)'
                            r'(?: (?:for )?(?:today|tomorrow))?|what(?: s| is) (?:next|coming up)(?: on my calendar)?|'
                            r'what(?: s| is) my next (?:event|meeting|class)|when is my next (?:event|meeting|class)|'
                            r'what(?: do i have| is| s) (?:on )?(?:my calendar )?(?:today|tomorrow)|'
                            r'what s on (?:today|tomorrow)')
LABEL_JUNK = re.compile(r'\b(?:a|an|the|my|set|start|create|make|put|on|up|new|for|of|called|named|'
                        r'\d+(?:\.\d+)?|' + '|'.join(sorted(NUMBER_WORD | set(DURATION_UNITS) | {'and', 'half'},
                                                           key=len, reverse=True)) + r')\b')


def timer_label(said):
    """'set a pasta timer for 10 minutes' -> 'pasta'; '10 minute timer for the eggs' -> 'eggs';
    'timer called laundry' -> 'laundry'."""
    for pattern in (r'(?:called|named|labeled|labelled) (?P<l>[a-z ]+?)$',
                    rf'{TIMER} for (?:the |my )?(?P<l>[a-z][a-z ]*?)$',
                    rf'(?P<l>[a-z ]+?) {TIMER}\b'):
        match = re.search(pattern, said)
        if match:
            label = re.sub(r'\s+', ' ', LABEL_JUNK.sub(' ', match.group('l'))).strip()
            if label and not re.search(r'\d', label):
                return label
    return ''


def which_timer(said):
    """The timer named in a pause/resume/stop/query ('pause the pasta timer' -> 'pasta')."""
    match = re.search(rf'(?:the |my )?(?P<l>[a-z ]+?) {TIMER}\b', said)
    if not match:
        return ''
    label = re.sub(r'\b(?:pause|resume|continue|unpause|restart|stop|cancel|delete|end|clear|remove|'
                   r'turn off|kill|hold|freeze|the|my|all|this|that|check|on|left|how much time is|'
                   r'how long is|what s|time)\b', ' ', match.group('l'))
    return re.sub(r'\s+', ' ', label).strip()


def parse(text, ringing=False):
    """The built-in skill the whole utterance asks for, or None. `ringing`: a timer is
    ringing, so a bare "stop" or "okay" dismisses it."""
    said = normalize(text)
    if ringing and (not said or DISMISS_WORDS.fullmatch(said)):
        return Skill('dismiss')
    everything = bool(re.search(r'\ball\b|\btimers\b', said))
    # Stopwatch first: "stop the stopwatch" is not a timer command.
    if re.search(rf'\b{STOPWATCH}\b', said):
        if re.fullmatch(rf'(?:start|begin|run|launch|set|turn on|open)(?: up)? {THE}{STOPWATCH}(?: now)?|{STOPWATCH} start', said):
            return Skill('stopwatch_start')
        if re.fullmatch(rf'(?:pause|hold|freeze) {THE}{STOPWATCH}', said):
            return Skill('stopwatch_pause')
        if re.fullmatch(rf'(?:resume|continue|unpause|restart|start again) {THE}{STOPWATCH}', said):
            return Skill('stopwatch_resume')
        if re.fullmatch(rf'(?:stop|end|cancel|reset|clear|finish|turn off|close) {THE}{STOPWATCH}', said):
            return Skill('stopwatch_stop')
        if re.fullmatch(rf'(?:what s|what is|whats|check|how long (?:has|is))? ?{THE}{STOPWATCH}'
                        r'(?: at| say| time| been running| running| show)?', said):
            return Skill('stopwatch_query')
        return None
    if re.search(rf'\b{TIMER}\b|\b(?:remind|wake) me in\b|\balarm (?:in|for)\b', said):
        if re.fullmatch(rf'(?:pause|hold|freeze) .*{TIMER}', said):
            return Skill('timer_pause', label=which_timer(said), all=everything)
        if re.fullmatch(rf'(?:resume|continue|unpause|restart|start again|keep going with) .*{TIMER}', said):
            return Skill('timer_resume', label=which_timer(said), all=everything)
        if re.fullmatch(rf'(?:stop|cancel|delete|end|clear|remove|turn off|kill|dismiss) .*{TIMER}', said):
            return Skill('timer_stop', label=which_timer(said), all=everything)
        if re.search(r'how (?:much time|long)|time left|what s left|remaining|how much longer|check|status', said):
            return Skill('timer_query', label=which_timer(said))
        seconds = parse_duration(said)
        if seconds and not re.search(r'\b(?:how|what|why|when|which|who)\b', said):
            return Skill('timer_start', seconds=seconds, label=timer_label(said))
        return None
    if re.fullmatch(r'(?:pause|hold)', said):
        return Skill('timer_pause')
    if re.fullmatch(r'(?:resume|continue|unpause)', said):
        return Skill('timer_resume')
    if TIME_QUERY.fullmatch(said):
        return Skill('time_query')
    if DATE_QUERY.fullmatch(said):
        return Skill('date_query')
    if WEATHER_QUERY.fullmatch(said):
        return Skill('weather_query', day='tomorrow' if 'tomorrow' in said else 'today')
    if CALENDAR_QUERY.fullmatch(said):
        return Skill('calendar_query', day='tomorrow' if 'tomorrow' in said else 'today')
    math_answer = parse_math(text)
    if math_answer:
        return Skill('math', expression=math_answer[0], value=math_answer[1])
    return None


def from_action(action, text):
    """The Skill for an action Jev chose, with its parameters read from the words; None
    when a needed parameter can't be read (the request then goes to Instinct)."""
    said = normalize(text)
    if action == 'timer_start':
        return Skill('timer_start', seconds=parse_duration(said), label=timer_label(said))  # no duration: ask
    if action in ('timer_pause', 'timer_resume', 'timer_stop', 'timer_query'):
        return Skill(action, label=which_timer(said), all=bool(re.search(r'\ball\b|\btimers\b', said)))
    if action == 'math':
        answer = parse_math(text)
        return Skill('math', expression=answer[0], value=answer[1]) if answer else None
    if action in ('weather_query', 'calendar_query'):
        return Skill(action, day='tomorrow' if 'tomorrow' in said else 'today')
    return Skill(action)


# ---------------------------------------------------------------- timers and the stopwatch

@dataclass
class Timer:
    id: str
    label: str
    duration: float
    ends_at: float = None      # wall clock, while running
    remaining: float = None    # while paused
    rang_at: float = None      # when it finished; it rings until dismissed or RING_SECONDS

    def left(self, now):
        return self.remaining if self.ends_at is None else max(0.0, self.ends_at - now)

    @property
    def state(self):
        return 'ringing' if self.rang_at else 'paused' if self.ends_at is None else 'running'


@dataclass
class Stopwatch:
    elapsed: float = 0.0       # before the current run
    started_at: float = None   # wall clock, while running

    def total(self, now):
        return self.elapsed + (now - self.started_at if self.started_at else 0.0)


class Clock:
    """Timers and the stopwatch, shared by every connection and saved to state/clock.json
    (wall-clock times) so they survive reconnects and restarts. `changed` callbacks run
    after every change; `tick` turns finished timers into ringing ones."""
    def __init__(self, state_dir, now=time.time):
        self.path = Path(state_dir) / 'clock.json'
        self.now = now
        self.timers, self.stopwatch = [], None
        self.listeners = []
        self.lock = threading.Lock()
        try:
            data = json.loads(self.path.read_text())
            self.timers = [Timer(**t) for t in data.get('timers', [])]
            self.stopwatch = Stopwatch(**data['stopwatch']) if data.get('stopwatch') else None
        except (OSError, ValueError, TypeError):
            pass
        self.tick()

    def save(self):
        data = {'timers': [t.__dict__ for t in self.timers],
                'stopwatch': self.stopwatch.__dict__ if self.stopwatch else None}
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data))
        tmp.replace(self.path)

    def changed(self):
        self.save()
        for listener in list(self.listeners):
            listener()

    def ringing(self):
        return any(t.rang_at for t in self.timers)

    def tick(self):
        """Finished timers start ringing; ones that have rung long enough are removed."""
        now, changed = self.now(), False
        for t in list(self.timers):
            if t.ends_at is not None and not t.rang_at and now >= t.ends_at:
                t.rang_at, changed = t.ends_at, True
            if t.rang_at and now - t.rang_at >= RING_SECONDS:
                self.timers.remove(t)
                changed = True
        if changed:
            self.changed()
        return changed

    def pick(self, label, states):
        """Timers a command applies to: the named one, else the most recently started one in
        a fitting state."""
        candidates = [t for t in self.timers if t.state in states]
        if label:
            named = [t for t in candidates if label in t.label or t.label in label and t.label]
            return named[-1:] if named else []
        return candidates[-1:]

    # Each command returns the sentence to say.
    def start_timer(self, seconds, label=''):
        now = self.now()
        self.timers.append(Timer(uuid.uuid4().hex[:8], label, seconds, ends_at=now + seconds))
        self.changed()
        name = f'{label.capitalize()} timer' if label else 'Timer'
        return f'{name} set for {say_duration(seconds)}.'

    def pause_timer(self, label='', everything=False):
        now = self.now()
        chosen = [t for t in self.timers if t.state == 'running'] if everything else self.pick(label, ('running',))
        if not chosen:
            return 'There is no running timer to pause.'
        for t in chosen:
            t.remaining, t.ends_at = t.left(now), None
        self.changed()
        return 'Timers paused.' if len(chosen) > 1 else f'Paused, with {say_duration(chosen[0].remaining)} left.'

    def resume_timer(self, label='', everything=False):
        now = self.now()
        chosen = [t for t in self.timers if t.state == 'paused'] if everything else self.pick(label, ('paused',))
        if not chosen:
            return 'There is no paused timer.'
        for t in chosen:
            t.ends_at, t.remaining = now + t.remaining, None
        self.changed()
        return 'Timers resumed.' if len(chosen) > 1 else f'Resumed, {say_duration(chosen[0].left(now))} to go.'

    def stop_timer(self, label='', everything=False):
        chosen = list(self.timers) if everything else self.pick(label, ('running', 'paused', 'ringing'))
        if not chosen:
            return 'There is no timer to cancel.'
        for t in chosen:
            self.timers.remove(t)
        self.changed()
        return 'All timers cancelled.' if len(chosen) > 1 else \
            (f'{chosen[0].label.capitalize()} timer cancelled.' if chosen[0].label else 'Timer cancelled.')

    def dismiss(self, timer_id=None):
        rang = [t for t in self.timers if t.rang_at and (timer_id is None or t.id == timer_id)]
        for t in rang:
            self.timers.remove(t)
        if rang:
            self.changed()
        return bool(rang)

    def timer_status(self, label=''):
        now = self.now()
        chosen = [t for t in self.timers if not label or label in t.label]
        if not chosen:
            return 'There are no timers.' if not label else f'There is no {label} timer.'
        parts = []
        for t in chosen[-3:]:
            name = f'The {t.label} timer' if t.label else 'The timer' if len(chosen) == 1 else 'One timer'
            if t.state == 'ringing':
                parts.append(f'{name} is done.')
            else:
                parts.append(f'{name} has {say_duration(t.left(now))} left' + (', paused.' if t.state == 'paused' else '.'))
        return ' '.join(parts)

    def start_stopwatch(self):
        if self.stopwatch and self.stopwatch.started_at:
            return f'The stopwatch is already running, at {say_duration(self.stopwatch.total(self.now()))}.'
        self.stopwatch = Stopwatch(started_at=self.now())
        self.changed()
        return 'Stopwatch started.'

    def pause_stopwatch(self):
        if not self.stopwatch or not self.stopwatch.started_at:
            return 'The stopwatch is not running.'
        now = self.now()
        self.stopwatch.elapsed, self.stopwatch.started_at = self.stopwatch.total(now), None
        self.changed()
        return f'Stopwatch paused at {say_duration(self.stopwatch.elapsed)}.'

    def resume_stopwatch(self):
        if not self.stopwatch:
            return self.start_stopwatch()
        if self.stopwatch.started_at:
            return 'The stopwatch is already running.'
        self.stopwatch.started_at = self.now()
        self.changed()
        return 'Stopwatch resumed.'

    def stop_stopwatch(self):
        if not self.stopwatch:
            return 'There is no stopwatch running.'
        total = self.stopwatch.total(self.now())
        self.stopwatch = None
        self.changed()
        return f'Stopwatch stopped at {say_duration(total)}.'

    def stopwatch_status(self):
        if not self.stopwatch:
            return 'The stopwatch is not running.'
        total = say_duration(self.stopwatch.total(self.now()))
        return f'The stopwatch is at {total}' + ('.' if self.stopwatch.started_at else ', paused.')

    def act(self, item_id, action):
        """A tap on the Echo's timer tile: pause, resume, stop or dismiss one item."""
        if item_id == 'stopwatch':
            handler = {'pause': self.pause_stopwatch, 'resume': self.resume_stopwatch,
                       'stop': self.stop_stopwatch}.get(action)
            return handler() if handler else None
        timer = next((t for t in self.timers if t.id == item_id), None)
        if not timer:
            return None
        if action == 'dismiss' or (action == 'stop' and timer.rang_at):
            return self.dismiss(item_id)
        if action == 'stop':
            self.timers.remove(timer)
            self.changed()
            return True
        if action == 'pause' and timer.state == 'running':
            timer.remaining, timer.ends_at = timer.left(self.now()), None
        elif action == 'resume' and timer.state == 'paused':
            timer.ends_at, timer.remaining = self.now() + timer.remaining, None
        else:
            return None
        self.changed()
        return True

    def snapshot(self):
        """For the Echo: remaining/elapsed milliseconds as of now; it counts from there."""
        now = self.now()
        items = [dict(id=t.id, kind='timer', label=t.label, state=t.state, duration_ms=int(t.duration * 1000),
                      remaining_ms=int(t.left(now) * 1000)) for t in self.timers]
        if self.stopwatch:
            items.append(dict(id='stopwatch', kind='stopwatch', label='', duration_ms=0,
                              state='running' if self.stopwatch.started_at else 'paused',
                              elapsed_ms=int(self.stopwatch.total(now) * 1000)))
        return items


# ---------------------------------------------------------------- time, date, weather, calendar

def say_time(now):
    """'It's 4:05 PM.' written so TTS reads it naturally."""
    hour = now.hour % 12 or 12
    minute = f'{now.minute:02d}' if now.minute else ''
    return f"It's {hour}{':' + minute if minute else ''} {'AM' if now.hour < 12 else 'PM'}."


def say_date(now):
    return f"It's {now.strftime('%A')}, {now.strftime('%B')} {now.day}."


WMO = {0: 'clear', 1: 'mainly clear', 2: 'partly cloudy', 3: 'overcast', 45: 'foggy', 48: 'foggy',
       51: 'drizzling', 53: 'drizzling', 55: 'drizzling', 61: 'raining lightly', 63: 'raining', 65: 'raining heavily',
       71: 'snowing lightly', 73: 'snowing', 75: 'snowing heavily', 80: 'showery', 81: 'showery', 82: 'stormy',
       95: 'thundery', 96: 'thundery', 99: 'thundery'}


class Weather:
    """Open-Meteo for the Pi's location (found once from its public IP, like the Echo's
    dashboard), cached for ten minutes."""
    def __init__(self):
        self.place, self.cached, self.cached_at = None, None, 0.0

    def get(self, url):
        request = urllib.request.Request(url, headers={'User-Agent': 'pi-voice-assistant'})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    def forecast(self):
        if self.cached and time.time() - self.cached_at < 600:
            return self.cached
        if not self.place:
            data = self.get('https://ipapi.co/json/')
            self.place = (data['latitude'], data['longitude'], data.get('city', ''))
        lat, lon, _ = self.place
        self.cached = self.get(
            f'https://api.open-meteo.com/v1/forecast?latitude={lat:.4f}&longitude={lon:.4f}'
            '&current=temperature_2m,weather_code&hourly=temperature_2m,precipitation_probability'
            '&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code'
            '&timezone=auto&forecast_days=2')
        self.cached_at = time.time()
        return self.cached

    def report(self, day='today'):
        data, city = self.forecast(), self.place[2]
        daily = data['daily']
        i = 1 if day == 'tomorrow' else 0
        high, low = round(daily['temperature_2m_max'][i]), round(daily['temperature_2m_min'][i])
        rain = daily['precipitation_probability_max'][i] or 0
        where = f' in {city}' if city else ''
        if day == 'tomorrow':
            sentence = (f'Tomorrow{where} will be {WMO.get(daily["weather_code"][1], "mixed")}, '
                        f'with a high of {high} and a low of {low} degrees.')
        else:
            now = data['current']
            sentence = (f"It's {round(now['temperature_2m'])} degrees and {WMO.get(now['weather_code'], 'mixed')}"
                        f'{where}, with a high of {high} and a low of {low}.')
        if rain >= 30:
            sentence += f' There is a {rain} percent chance of rain.'
        return sentence


def say_clock_time(ms, zone=None):
    t = dt.datetime.fromtimestamp(ms / 1000, zone)
    hour = t.hour % 12 or 12
    return f'{hour}{":" + format(t.minute, "02d") if t.minute else ""} {"AM" if t.hour < 12 else "PM"}'


def calendar_report(events, now, day='today'):
    """Upcoming events for today or tomorrow from the Pi's calendar feed."""
    if events is None:
        return 'I have no calendar connected.'
    now_ms = now.timestamp() * 1000
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(days=1 if day == 'tomorrow' else 0)
    end = start + dt.timedelta(days=1)
    todays = [e for e in events if e['end'] > now_ms and start.timestamp() * 1000 <= e['begin'] < end.timestamp() * 1000
              or (day == 'today' and e['begin'] <= now_ms < e['end'])]
    if not todays:
        return 'Nothing else is on your calendar today.' if day == 'today' else 'Nothing is on your calendar tomorrow.'
    items = [e['title'] if e['all_day'] else f'{e["title"]} at {say_clock_time(e["begin"], now.tzinfo)}' for e in todays[:4]]
    lead = ('You have' if day == 'today' else 'Tomorrow you have') + \
        (f' {len(todays)} events: ' if len(todays) > 1 else ' ')
    more = f', and {len(todays) - 4} more' if len(todays) > 4 else ''
    return lead + ', '.join(items[:-1]) + (' and ' if len(items) > 1 else '') + items[-1] + more + '.'


def math_reply(skill):
    if math.isnan(skill.value):
        return "That's undefined; you can't divide by zero."
    return f'{skill.expression} is {spoken(skill.value)}.'
