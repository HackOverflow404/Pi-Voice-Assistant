"""Reply text cleanup for speech, and chunking so playback can start after one sentence."""
import re

EMOJI = re.compile('[\U0001F000-\U0001FAFF☀-➿️‍]')
BULLET = re.compile(r'^\s*(?:[-•*]|\d+[.)])\s+')


def strip_signoff(text):
    """Drop a trailing signature: a "-- " block, or a short unpunctuated last paragraph
    such as the assistant's name."""
    text = re.split(r'\n-- ?\n', text, maxsplit=1)[0]
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text.strip()) if p.strip()]
    if len(paragraphs) > 1 and len(paragraphs[-1]) <= 30 and not paragraphs[-1].endswith(('.', '!', '?', ':')):
        paragraphs.pop()
    return '\n\n'.join(paragraphs)


def speakable(text):
    """Plain sentences for TTS: no sign-off, WhatsApp formatting marks, emoji or URLs;
    list items and lines become sentences."""
    text = strip_signoff(text.replace('\r\n', '\n'))
    text = text.replace('```', ' ')
    text = re.sub(r'(^|\s)[*_~`]+(?=\S)', r'\1', text)
    text = re.sub(r'(?<=\S)[*_~`]+(?=\s|$|[.,!?;:])', '', text)
    text = EMOJI.sub('', text)
    text = re.sub(r'https?://\S+', 'a link', text)
    lines = []
    for line in text.split('\n'):
        line = BULLET.sub('', line).strip()
        if line:
            lines.append(line if line.endswith(('.', '!', '?', ':', ';', ',')) else line + '.')
    return re.sub(r'\s+', ' ', ' '.join(lines)).strip()


def chunks(text, first_max=60):
    """Sentences to synthesize one at a time. Very short sentences join the next one, and a
    long first sentence is split at its first clause break so speech starts sooner."""
    parts = []
    for sentence in (s.strip() for s in re.split(r'(?<=[.!?])\s+', text)):
        if not sentence:
            continue
        if parts and len(parts[-1]) < 25:
            parts[-1] += ' ' + sentence
        else:
            parts.append(sentence)
    if parts and len(parts[0]) > first_max:
        brk = re.search(r'[,;:]\s', parts[0][20:])
        if brk:
            cut = 20 + brk.end()
            parts[:1] = [parts[0][:cut].strip(), parts[0][cut:].strip()]
    return parts
