"""Capture a monitor through the ScreenCast portal and pipe raw frames into the encoder.

Runs as the engine's encoder process: the arguments after -- are the FFmpeg command
that reads NV12 on stdin and writes the encoded stream to this process's stdout.
"""
import argparse
import ctypes
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys

try:
    from jeepney import DBusAddress, MatchRule, message_bus, new_method_call
    from jeepney.io.blocking import open_dbus_connection
    from jeepney.wrappers import unwrap_msg
except ImportError:
    raise SystemExit('jeepney is required. Run "python -m pip install -r requirements.txt"')

SCREENCAST = DBusAddress('/org/freedesktop/portal/desktop', bus_name='org.freedesktop.portal.Desktop',
                         interface='org.freedesktop.portal.ScreenCast')
MONITOR, CURSOR_HIDDEN, CURSOR_EMBEDDED, PERSIST_UNTIL_REVOKED = 1, 1, 2, 2
PR_SET_PDEATHSIG = 1
# The compositor sends no frames while the screen is still, and the engine treats 5 s of silence as a lost display
KEEPALIVE_MS = 100


def source_options(token, show_cursor):
    """Return the SelectSources options; a saved token restores the earlier grant without a dialog."""
    options = dict(types=('u', MONITOR), multiple=('b', False), persist_mode=('u', PERSIST_UNTIL_REVOKED),
                   cursor_mode=('u', CURSOR_EMBEDDED if show_cursor else CURSOR_HIDDEN))
    return {**options, 'restore_token': ('s', token)} if token else options


def request(connection, method, signature, arguments, options):
    """Call a portal method and return the results of its Response signal."""
    token = f'g2bridge{secrets.token_hex(8)}'
    sender = connection.unique_name[1:].replace('.', '_')
    rule = MatchRule(type='signal', interface='org.freedesktop.portal.Request', member='Response',
                     path=f'/org/freedesktop/portal/desktop/request/{sender}/{token}')
    unwrap_msg(connection.send_and_get_reply(message_bus.AddMatch(rule)))
    # subscribe before calling so a fast reply is not missed
    with connection.filter(rule) as responses:
        unwrap_msg(connection.send_and_get_reply(new_method_call(
            SCREENCAST, method, signature, (*arguments, {**options, 'handle_token': ('s', token)}))))
        code, results = connection.recv_until_filtered(responses).body
    if code == 1:
        raise SystemExit('Screen sharing was cancelled in the portal dialog')
    if code != 0:
        raise SystemExit(f'ScreenCast portal {method} failed with response {code}')
    return {name: value for name, (_, value) in results.items()}


def open_stream(token_file, show_cursor):
    """Start a portal session; return its connection, which must stay open, the PipeWire fd, and the node id."""
    connection = open_dbus_connection(bus='SESSION', enable_fds=True)
    session = request(connection, 'CreateSession', 'a{sv}', (),
                      dict(session_handle_token=('s', f'g2bridge{secrets.token_hex(8)}')))['session_handle']
    token = token_file.read_text().strip() if token_file.is_file() else None
    request(connection, 'SelectSources', 'oa{sv}', (session,), source_options(token, show_cursor))
    started = request(connection, 'Start', 'osa{sv}', (session, ''), {})
    # the portal may replace the token on each use, and only the latest one is valid
    if 'restore_token' in started:
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(started['restore_token'])
    if not started.get('streams'):
        raise SystemExit('The ScreenCast portal returned no monitor')
    reply = connection.send_and_get_reply(new_method_call(SCREENCAST, 'OpenPipeWireRemote', 'oa{sv}', (session, {})))
    return connection, unwrap_msg(reply)[0].to_raw_fd(), started['streams'][0][0]


def gst_command(fd, node, fps, size):
    """Return the GStreamer pipeline that writes letterboxed NV12 frames to stdout."""
    width, height = size.split('x')
    return ['gst-launch-1.0', '-q', 'pipewiresrc', f'fd={fd}', f'path={node}', 'do-timestamp=true',
            # ponytail: the compositor paces frames evenly, but this fails if fps exceeds the monitor's maximum
            f'keepalive-time={KEEPALIVE_MS}', '!', f'video/x-raw,max-framerate={fps}/1',
            # keep only the newest frame so slow scaling drops frames instead of adding delay
            '!', 'queue', 'leaky=downstream', 'max-size-buffers=1', 'max-size-bytes=0', 'max-size-time=0',
            '!', 'videoconvertscale', 'method=bilinear', 'add-borders=true', f'n-threads={os.cpu_count() or 1}',
            '!', f'video/x-raw,format=NV12,width={width},height={height},pixel-aspect-ratio=1/1',
            '!', 'fdsink', 'fd=1', 'sync=false']


def die_with_parent():
    """Stop the capture if this helper is killed; the encoder then exits when its input ends."""
    ctypes.CDLL(None).prctl(PR_SET_PDEATHSIG, signal.SIGTERM)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--fps', type=int, required=True)
    parser.add_argument('--size', required=True)
    parser.add_argument('--hide-cursor', dest='show_cursor', action='store_false')
    parser.add_argument('encoder', nargs='+')
    args = parser.parse_args()
    if shutil.which('gst-launch-1.0') is None:
        raise SystemExit('gst-launch-1.0 not found. Install GStreamer with its PipeWire plugin')
    connection, fd, node = open_stream(args.token_file, args.show_cursor)
    with connection:
        capture = subprocess.Popen(gst_command(fd, node, args.fps, args.size), stdout=subprocess.PIPE, pass_fds=(fd,),
                                   preexec_fn=die_with_parent)
        encoder = subprocess.Popen(args.encoder, stdin=capture.stdout)
        capture.stdout.close()
        for stop in (signal.SIGTERM, signal.SIGINT):
            signal.signal(stop, lambda *_: encoder.terminate())
        code = encoder.wait()
        capture.terminate()
        capture.wait()
    return code


if __name__ == '__main__':
    raise SystemExit(main())
