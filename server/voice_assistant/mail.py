"""TLS mail delivery and cancellable IMAP IDLE reply matching."""
import email.policy
from email.parser import BytesParser
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
import re
import smtplib
import ssl
import time


def plain_reply(raw, message_id, limit):
    message = BytesParser(policy=email.policy.default).parsebytes(raw)
    ids = re.findall(r'<[^<>\s]+>', str(message.get('In-Reply-To', '')))
    if message_id not in ids:
        return None
    body = message.get_body(preferencelist=('plain',))
    if body is None or body.get_content_type() != 'text/plain':
        return None
    text = body.get_content().strip()
    return text[:limit] if text else None


class Mail:
    def __init__(self, config):
        self.config = config

    def new_id(self):
        return make_msgid(domain=self.config['username'].split('@')[-1])

    def send(self, text, message_id):
        c = self.config
        message = EmailMessage()
        message['From'] = c['username']
        message['To'] = c['recipient']
        message['Subject'] = 'Voice assistant request'
        message['Date'] = formatdate(localtime=False)
        message['Message-ID'] = message_id
        message.set_content(text)
        with smtplib.SMTP_SSL(c['smtp_host'], c['smtp_port'], timeout=20,
                              context=ssl.create_default_context()) as smtp:
            smtp.login(c['username'], c['app_password'].replace(' ', ''))
            smtp.send_message(message)

    def wait_reply(self, message_id, deadline, stop):
        from imapclient import IMAPClient
        c = self.config
        while time.time() < deadline and not stop.is_set():
            try:
                with IMAPClient(c['imap_host'], port=c['imap_port'], ssl=True,
                                ssl_context=ssl.create_default_context(), timeout=15) as imap:
                    imap.login(c['username'], c['app_password'].replace(' ', ''))
                    imap.select_folder(c['mailbox'], readonly=True)
                    if b'IDLE' not in imap.capabilities():
                        raise RuntimeError('IMAP server does not support IDLE')
                    while time.time() < deadline and not stop.is_set():
                        # Search first: catches replies arriving before login or during reconnect.
                        uids = imap.search(['HEADER', 'In-Reply-To', message_id])
                        for uid in reversed(uids):
                            # Bound downloads and never mark messages read.
                            size = imap.fetch([uid], ['RFC822.SIZE'])[uid][b'RFC822.SIZE']
                            if size > 2_000_000:
                                continue
                            raw = imap.fetch([uid], ['BODY.PEEK[]'])[uid][b'BODY[]']
                            text = plain_reply(raw, message_id, c['max_reply_chars'])
                            if text:
                                return text
                        imap.idle()
                        try:
                            # Exit IDLE every 30s to renew/search even if a notification is lost.
                            end = min(deadline, time.time() + 30)
                            while not stop.is_set() and time.time() < end:
                                if imap.idle_check(timeout=min(1, max(0, end - time.time()))):
                                    break
                        finally:
                            imap.idle_done()
            except (OSError, IMAPClient.Error):
                # Temporary network failures reconnect within the original request deadline.
                stop.wait(min(3, max(0, deadline - time.time())))
        if stop.is_set():
            raise InterruptedError('Reply watch cancelled')
        raise TimeoutError('No matching plain-text email reply before timeout')
