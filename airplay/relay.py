"""AirPlay on the Echo: runs shairport-sync with its audio on stdout (44.1 kHz, stereo, s16le)
and streams that PCM to the Echo's voice app, which connects over USB (adb reverse tcp:8767).

One listener at a time; a new connection replaces the old one (the app reconnects after the
Echo reboots or USB re-enumerates). shairport-sync writes in real time while something is
playing and nothing while idle. If the Echo falls behind, buffered audio past MAX_PENDING is
dropped, so the delay never grows."""
import argparse
import logging
import os
import select
import signal
import socket
import subprocess
import sys
import time

LOG = logging.getLogger('airplay')
FRAME = 4  # bytes per stereo s16 frame
RATE = 44100
MAX_PENDING = RATE * FRAME // 2  # 0.5 s


def serve(port, command):
    child = subprocess.Popen(command, stdout=subprocess.PIPE, bufsize=0)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    server = socket.create_server(('127.0.0.1', port))
    client, pending, sent = None, bytearray(), 0
    streamed = dropped = 0
    report = time.monotonic()

    def drop_client(reason):
        nonlocal client
        LOG.info('Echo disconnected (%s)', reason)
        client.close()
        client = None

    try:
        while True:
            readers = [child.stdout, server] + ([client] if client else [])
            writers = [client] if client and pending else []
            readable, writable, _ = select.select(readers, writers, [], 5)
            if server in readable:
                new, address = server.accept()
                if client:
                    drop_client('replaced')
                new.setblocking(False)
                new.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                client, pending, sent = new, bytearray(), 0
                LOG.info('Echo connected from %s:%d', *address)
            if client and client in readable:
                try:
                    gone = not client.recv(4096)
                except OSError:
                    gone = True
                if gone:
                    drop_client('closed')
            if child.stdout in readable:
                data = os.read(child.stdout.fileno(), 65536)
                if not data:
                    raise SystemExit(f'shairport-sync exited ({child.wait()})')
                if client:
                    pending += data
                    streamed += len(data)
                    if len(pending) > MAX_PENDING:
                        # Keep the rest of a frame already partly sent, so samples stay aligned.
                        keep = (FRAME - sent % FRAME) % FRAME
                        dropped += len(pending) - keep
                        del pending[keep:]
            if client and pending and (client in writable or child.stdout in readable):
                try:
                    count = client.send(pending)
                    del pending[:count]
                    sent += count
                except BlockingIOError:
                    pass
                except OSError as exc:
                    drop_client(exc)
            now = time.monotonic()
            if now - report >= 60:
                if streamed:
                    LOG.info('Streamed %.1f s of audio in the last %.0f s; dropped %.2f s',
                             streamed / FRAME / RATE, now - report, dropped / FRAME / RATE)
                streamed = dropped = 0
                report = now
    finally:
        child.terminate()
        try:
            child.wait(5)
        except subprocess.TimeoutExpired:
            child.kill()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--config', default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                         'shairport-sync.conf'))
    parser.add_argument('--port', type=int, default=8767)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    serve(args.port, ['shairport-sync', '-c', args.config])


if __name__ == '__main__':
    main()
