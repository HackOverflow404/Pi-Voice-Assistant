"""Texts the assistant contact over SMS through a Google Voice number, using only the Gmail
account that owns it: no phone, modem or paid gateway.

Google Voice forwards each text it receives to Gmail (voice.google.com → Settings → Messages
→ Forward messages to email). Every forwarded email comes from a per-conversation address,
<voice number>.<contact number>.<token>@txt.voice.google.com, and an email sent to that
address is delivered to the contact as a text from the Voice number. So sending is SMTP to
that address and receiving is polling Gmail over IMAP for mail from it.

Google Voice cannot start a conversation by email: text the contact once from
voice.google.com, and its answer creates the address, which is then found in the inbox and
remembered (or set sms.reply_address). Unlike WhatsApp, every incoming text is kept here,
whether or not it answers a request, so the server can read unsolicited ones aloud."""
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr, parsedate_to_datetime
import html
import imaplib
import json
import logging
from pathlib import Path
import re
import smtplib
import threading
import time

LOG = logging.getLogger('voice')
DOMAIN = 'txt.voice.google.com'
# Google's footer under the text in a forwarded message, and the start of a quoted reply.
FOOTER = re.compile(r'^\s*(YOUR ACCOUNT\b|To respond to this text message|Google LLC\b|You received this '
                    r'(email|message)|On .* wrote:$)', re.I)


def digits(number):
    """+1 (555) 555-0123 → 15555550123; ten-digit numbers are taken as North American."""
    d = re.sub(r'\D', '', str(number))
    return '1' + d if len(d) == 10 else d


def message_text(mail):
    """The text a forwarded message carries, without Google's header link and footer."""
    part = mail.get_body(preferencelist=('plain', 'html'))
    if part is None:
        return ''
    body = part.get_content()
    if part.get_content_type() == 'text/html':
        body = re.sub(r'(?i)<br\s*/?>|</p>|</div>|</tr>', '\n', body)
        body = html.unescape(re.sub(r'<[^>]+>', '', body))
    lines = []
    for line in body.replace('\r\n', '\n').split('\n'):
        if FOOTER.match(line):
            break
        if not lines and re.fullmatch(r'\s*<?https?://\S+>?\s*', line):
            continue  # Google's logo link above the message
        lines.append(line.rstrip())
    return '\n'.join(lines).strip()


class GoogleVoice:
    def __init__(self, config, state_dir):
        self.config = config
        self.contact = digits(config['contact'])
        self.state_path = Path(state_dir) / 'sms.json'
        state = {}
        try:
            state = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            pass
        self.validity, self.uid = state.get('uidvalidity'), state.get('uid')
        self.address = config.get('reply_address') or state.get('address')
        self.cond = threading.Condition()
        self.inbox = []  # {'time', 'text', 'taken'}: texts from the contact, oldest first
        self.closed = threading.Event()
        self.thread = None

    # Sending

    def send(self, text):
        """Send the spoken request, followed by the configured instruction that the reply
        will be read aloud; returns {"id", "timestamp"} for the message."""
        if not self.address:
            raise RuntimeError(f'No Google Voice address for {self.contact} yet: text it once from '
                               'voice.google.com and wait for its answer, or set sms.reply_address')
        instruction = (self.config.get('instruction') or '').strip()
        mail = EmailMessage()
        mail['From'] = self.config['gmail_address']
        mail['To'] = self.address
        mail['Message-ID'] = make_msgid(domain='pi-voice')
        mail.set_content(f'{text}\n\n{instruction}' if instruction else text)
        sent = time.time()
        with smtplib.SMTP_SSL(self.config['smtp_host'], 465, timeout=30) as smtp:
            smtp.login(self.config['gmail_address'], self.config['app_password'])
            smtp.send_message(mail)
        return {'id': mail['Message-ID'], 'timestamp': sent - 2}  # 2 s for clock skew with Google

    can_unsend = False

    def revoke(self, message_id):
        raise RuntimeError("A text can't be unsent")

    # Receiving

    def start(self):
        self.thread = threading.Thread(target=self.poll_forever, name='sms-inbox', daemon=True)
        self.thread.start()

    def close(self):
        self.closed.set()

    def poll_forever(self):
        backoff = 5
        while not self.closed.is_set():
            imap = None
            try:
                imap = imaplib.IMAP4_SSL(self.config['imap_host'], timeout=30)
                imap.login(self.config['gmail_address'], self.config['app_password'])
                self.open_mailbox(imap)
                LOG.info('Watching Gmail for texts from %s%s', self.contact,
                         '' if self.address else ' (no reply address yet)')
                backoff = 5
                while not self.closed.is_set():
                    self.fetch(imap)
                    self.closed.wait(self.config['poll_seconds'])
                    imap.noop()  # Gmail reports newly arrived mail on NOOP
            except (OSError, imaplib.IMAP4.error, ValueError) as exc:
                LOG.warning('Gmail inbox unavailable (%s: %s); retrying in %d s', type(exc).__name__, exc, backoff)
                self.closed.wait(backoff)
                backoff = min(backoff * 2, 300)
            finally:
                if imap:
                    try:
                        imap.logout()
                    except Exception:
                        pass

    def open_mailbox(self, imap):
        """Select the mailbox read-only (forwarded texts stay unread in Gmail). On first use,
        or if Gmail renumbered the mailbox, start after the newest message instead of reading
        the whole history aloud, and look back for the contact's reply address."""
        mailbox = self.config['mailbox']
        typ, _ = imap.select(f'"{mailbox}"', readonly=True)
        if typ != 'OK':
            raise ValueError(f'No Gmail mailbox {mailbox}')
        validity = int(imap.untagged_responses.get('UIDVALIDITY', [b'0'])[-1])
        if validity == self.validity and self.uid is not None:
            return
        _, data = imap.uid('search', None, 'ALL')
        uids = [int(u) for u in (data[0] or b'').split()]
        self.validity, self.uid = validity, max(uids, default=0)
        if not self.address:
            _, data = imap.uid('search', None, 'FROM', f'"{DOMAIN}"')
            for uid in reversed([int(u) for u in (data[0] or b'').split()][-200:]):
                _, parts = imap.uid('fetch', str(uid), '(BODY.PEEK[HEADER.FIELDS (FROM)])')
                mail = message_from_bytes(parts[0][1], policy=policy.default)
                if self.is_contact(mail):
                    self.address = parseaddr(mail['From'])[1]
                    LOG.info('Found the Google Voice address for %s', self.contact)
                    break
        self.save()

    def is_contact(self, mail):
        address = parseaddr(str(mail.get('From', '')))[1].lower()
        local, _, domain = address.partition('@')
        return domain == DOMAIN and self.contact in local.split('.')

    def fetch(self, imap):
        _, data = imap.uid('search', None, f'UID {self.uid + 1}:*', 'FROM', f'"{DOMAIN}"')
        new = sorted(u for u in (int(x) for x in (data[0] or b'').split()) if u > self.uid)
        for uid in new:
            _, parts = imap.uid('fetch', str(uid), '(BODY.PEEK[])')
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if raw is not None:
                mail = message_from_bytes(raw, policy=policy.default)
                if self.is_contact(mail):
                    if not self.address:
                        self.address = parseaddr(mail['From'])[1]
                    self.receive(message_text(mail), self.sent_at(mail))
            self.uid = uid
            self.save()

    @staticmethod
    def sent_at(mail):
        try:
            return min(parsedate_to_datetime(mail['Date']).timestamp(), time.time())
        except (TypeError, ValueError):
            return time.time()

    def receive(self, text, when):
        if not text:
            return  # a picture or other attachment without text
        LOG.info('Text from %s: %d characters', self.contact, len(text))
        with self.cond:
            self.inbox = [m for m in self.inbox if not m['taken']]
            self.inbox.append({'time': when, 'arrived': time.time(), 'text': text, 'taken': False})
            self.cond.notify_all()

    def save(self):
        state = {'uidvalidity': self.validity, 'uid': self.uid, 'address': self.address}
        tmp = self.state_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(state))
        tmp.replace(self.state_path)

    def take(self, since):
        """Mark and return the unread texts sent at or after `since`."""
        found = [m for m in self.inbox if not m['taken'] and m['time'] >= since]
        for m in found:
            m['taken'] = True
        return [m['text'] for m in found]

    def wait_reply(self, request, since, deadline, stop, on_reaction=None):
        """The contact's answer to the request sent at `since`: the first texts after it,
        joined while each follows within reply_settle_seconds of the last. A text that
        arrived earlier is left for unread() to read out on its own."""
        texts, settle_until = [], None
        while not stop.is_set():
            end = settle_until or deadline
            remaining = end - time.time()
            if remaining <= 0:
                break
            with self.cond:
                found = self.take(since)
                if not found:
                    self.cond.wait(min(0.5, remaining))  # short, so a cancel is noticed quickly
            if found:
                texts += found
                settle_until = time.time() + self.config['reply_settle_seconds']
        if texts:
            return '\n\n'.join(texts)
        if stop.is_set():
            raise InterruptedError('Reply watch cancelled')
        raise TimeoutError('No SMS reply before timeout')

    def unread(self):
        """Texts not claimed as a reply, joined, once the newest has settled for
        reply_settle_seconds; None if there are none. Texts older than stale_minutes (the
        Echo was away) are dropped rather than read out of context."""
        now = time.time()
        with self.cond:
            fresh = [m for m in self.inbox if not m['taken']]
            if not fresh or now - fresh[-1]['arrived'] < self.config['reply_settle_seconds']:
                return None
            for m in fresh:
                m['taken'] = True
        texts = [m['text'] for m in fresh if now - m['time'] <= self.config['stale_minutes'] * 60]
        if len(texts) < len(fresh):
            LOG.info('Dropped %d stale texts', len(fresh) - len(texts))
        return '\n\n'.join(texts) or None
