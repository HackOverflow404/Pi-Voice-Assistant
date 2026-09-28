"""Texts the assistant contact through the local WhatsApp bridge (bridge/, whatsmeow) and
waits for its reply. The bridge owns the WhatsApp session; this side only speaks HTTP to it."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request


class WhatsApp:
    def __init__(self, config):
        self.config = config
        self.url = config['bridge_url'].rstrip('/')

    def call(self, method, path, body=None, timeout=10):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors='replace').strip()[:200]
            raise RuntimeError(f'WhatsApp bridge {exc.code}: {detail}') from exc

    def send(self, text):
        """Send the spoken request, followed by the configured instruction that the reply
        will be read aloud; returns WhatsApp's {"id", "timestamp"} for the message."""
        instruction = (self.config.get('instruction') or '').strip()
        body = f'{text}\n\n{instruction}' if instruction else text
        return self.call('POST', '/send', {'contact': self.config['contact'], 'text': body}, timeout=30)

    def wait_reply(self, request, since, deadline, stop):
        """The contact's answer to message `request`. The chat also carries unrelated
        conversation, so the bridge decides which messages answer it (see repliesTo in
        bridge/main.go); `since` is the fallback if the bridge no longer knows the request.
        Follow-ups within reply_settle_seconds of the previous one are joined, since chat
        assistants often answer in several bubbles."""
        texts, after, settle_until = [], 0, None
        while not stop.is_set():
            end = settle_until or deadline
            remaining = end - time.time()
            if remaining <= 0:
                break
            wait = max(1, min(20, int(remaining)))
            query = urllib.parse.urlencode({'contact': self.config['contact'], 'request': request,
                                            'since': int(since), 'after': after, 'wait': wait})
            try:
                found = self.call('GET', f'/replies?{query}', timeout=wait + 10)['messages']
            except (OSError, RuntimeError):
                # Bridge restarting or WhatsApp reconnecting: retry within the deadline.
                stop.wait(min(3, max(0, end - time.time())))
                continue
            for message in found:
                texts.append(message['text'])
                after = max(after, message['seq'])
            if found:
                settle_until = time.time() + self.config['reply_settle_seconds']
        if texts:
            return '\n\n'.join(texts)
        if stop.is_set():
            raise InterruptedError('Reply watch cancelled')
        raise TimeoutError('No WhatsApp reply before timeout')
