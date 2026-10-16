"""A durable outbox; uncertain SMTP sends are never automatically repeated."""
import sqlite3
import time


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('''CREATE TABLE IF NOT EXISTS requests (
            message_id TEXT PRIMARY KEY, transcript TEXT NOT NULL,
            reply TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, created REAL NOT NULL)''')
        # Voice notes already played, so a reconnect or restart never plays one twice.
        self.db.execute('CREATE TABLE IF NOT EXISTS voice (message_id TEXT PRIMARY KEY, played REAL NOT NULL)')
        self.db.commit()

    def create(self, message_id, transcript):
        self.db.execute('INSERT INTO requests VALUES (?, ?, ?, ?, ?)',
                        (message_id, transcript, '', 'sending', time.time()))
        self.db.commit()

    def update(self, message_id, status, reply=None):
        if reply is None:
            self.db.execute('UPDATE requests SET status=? WHERE message_id=?', (status, message_id))
        else:
            self.db.execute('UPDATE requests SET status=?, reply=? WHERE message_id=?',
                            (status, reply, message_id))
        self.db.commit()

    def rename(self, old_id, new_id):
        """Replace the local placeholder ID with the ID the message was sent under."""
        self.db.execute('UPDATE requests SET message_id=? WHERE message_id=?', (new_id, old_id))
        self.db.commit()

    def voice_played(self, message_id):
        return self.db.execute('SELECT 1 FROM voice WHERE message_id=?', (message_id,)).fetchone() is not None

    def mark_voice(self, message_id):
        self.db.execute('INSERT OR IGNORE INTO voice VALUES (?, ?)', (message_id, time.time()))
        self.db.commit()

    def latest(self):
        self.db.row_factory = sqlite3.Row
        row = self.db.execute('SELECT * FROM requests ORDER BY created DESC LIMIT 1').fetchone()
        return dict(row) if row else None
