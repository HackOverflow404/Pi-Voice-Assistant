"""A durable outbox; uncertain SMTP sends are never automatically repeated."""
import sqlite3
import time


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('''CREATE TABLE IF NOT EXISTS requests (
            message_id TEXT PRIMARY KEY, transcript TEXT NOT NULL,
            reply TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, created REAL NOT NULL)''')
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

    def latest(self):
        self.db.row_factory = sqlite3.Row
        row = self.db.execute('SELECT * FROM requests ORDER BY created DESC LIMIT 1').fetchone()
        return dict(row) if row else None
