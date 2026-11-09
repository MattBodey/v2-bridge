"""Stream the Linux desktop to rooted DJI FPV Goggles V2 running dji-moonlight-shim."""
import argparse
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import time
import tomllib

import usb.core
import usb.util

from .avi import AviPacketStream

VENDOR_ID, PRODUCT_ID = 0x2CA3, 0x001F
INTERFACE, OUT_ENDPOINT = 3, 0x03
MAGIC = 0x42069
# The shim closes the stream if a frame is larger than its buffer
MAX_FRAME_BYTES = 1_000_000
WRITE_TIMEOUT_MS = 1000
# The shim's watchdog returns it to waiting for a new connect header after this long without a frame
SHIM_IDLE_SECONDS = 3
HELPER = Path(__file__).resolve().with_name('linux_capture.py')
TOKEN_FILE = HELPER.parents[1] / 'work' / 'portal-restore-token'
CONFIG_FILE = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'v2-bridge' / 'config.toml'
VALUE_SETTINGS = ('size', 'fps', 'bitrate-mbps')


def config_arguments(path):
    """Return the config file as command-line arguments, so flags given after them win and both get the same checks."""
    try:
        with path.open('rb') as file:
            config = tomllib.load(file)
    except FileNotFoundError:
        return []
    # ValueError covers both invalid TOML and non-UTF-8 bytes
    except (OSError, ValueError) as exc:
        raise SystemExit(f'Cannot read config file {path}: {exc}')
    arguments = []
    for name, value in config.items():
        if name in VALUE_SETTINGS and type(value) in (int, str):
            arguments += [f'--{name}', str(value)]
        elif name == 'hide-cursor' and type(value) is bool:
            arguments += ['--hide-cursor'] if value else []
        else:
            raise SystemExit(f'Invalid setting in {path}: {name} = {value!r}')
    return arguments


def parse_size(text):
    match = re.fullmatch(r'(\d+)x(\d+)', text)
    if not match or not all(int(side) > 0 and int(side) % 2 == 0 for side in match.groups()):
        raise argparse.ArgumentTypeError('Use WIDTHxHEIGHT with even, nonzero sides, such as 1440x810')
    return text


def encoder_command(ffmpeg, size, fps, bitrate_mbps, show_cursor):
    """Return the capture helper command, which runs FFmpeg with raw frames on its stdin."""
    bitrate = bitrate_mbps * 1_000_000
    return [sys.executable, str(HELPER), '--token-file', str(TOKEN_FILE), '--fps', str(fps), '--size', size,
            *([] if show_cursor else ['--hide-cursor']), '--',
            ffmpeg, '-hide_banner', '-nostdin', '-loglevel', 'warning',
            '-f', 'rawvideo', '-pixel_format', 'nv12', '-video_size', size, '-framerate', str(fps), '-i', 'pipe:0',
            '-an', '-c:v', 'h264_nvenc', '-preset', 'p1', '-tune', 'ull', '-rc', 'cbr', '-zerolatency', '1',
            '-delay', '0', '-profile:v', 'high', '-b:v', str(bitrate), '-maxrate', str(bitrate),
            '-bufsize', str(bitrate * 2 // fps), '-bf', '0', '-g', str(fps * 3), '-aud', '1', '-forced-idr', '1',
            # AVI releases each frame as soon as it is encoded; raw H.264 would wait for the next frame's delimiter
            '-bsf:v', 'filter_units=remove_types=12', '-flush_packets', '1', '-f', 'avi', 'pipe:1']


def stream(read, write, size, fps, report=print):
    """Send the connect header before the first frame, then length-prefixed frames; return the frame count."""
    width, height = map(int, size.split('x'))
    splitter = AviPacketStream(max_frame_bytes=MAX_FRAME_BYTES)
    frames = reported = 0
    last_report = time.perf_counter()
    last_frame = None
    while data := read(16384):
        for frame in splitter.feed(data):
            now = time.perf_counter()
            # A header sent before the shim has reset would be read as a frame length, so stop instead
            if frames and now - last_frame > SHIM_IDLE_SECONDS:
                raise RuntimeError(f'No frame for {now - last_frame:.1f} s, so the shim dropped the stream. Restart the bridge')
            if frames == 0:
                write(struct.pack('<4I', MAGIC, width, height, fps))
                report('Streaming. Ctrl+C stops.')
            write(struct.pack('<I', len(frame)) + frame)
            frames += 1
            last_frame = now
        now = time.perf_counter()
        if now - last_report >= 5:
            report(f'Sent {(frames - reported) / (now - last_report):.1f} fps')
            reported, last_report = frames, now
    return frames


def open_goggles():
    device = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
    if device is None:
        raise SystemExit('FPV Goggles V2 (2ca3:001f) not found. Connect them over USB and power them on')
    if device.is_kernel_driver_active(INTERFACE):
        device.detach_kernel_driver(INTERFACE)
    usb.util.claim_interface(device, INTERFACE)
    return device


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--size', type=parse_size, default='1440x810', help='Stream size (default: %(default)s)')
    parser.add_argument('--fps', type=int, default=60,
                        help="Stream rate, at most the shared display's refresh rate (default: %(default)s)")
    parser.add_argument('--bitrate-mbps', type=int, default=30, help='Video bitrate (default: %(default)s)')
    parser.add_argument('--hide-cursor', dest='show_cursor', action='store_false')
    parser.add_argument('--show-cursor', dest='show_cursor', action='store_true', default=True,
                        help='Undo hide-cursor from the config file')
    args = parser.parse_args(config_arguments(CONFIG_FILE) + sys.argv[1:])
    if args.fps <= 0 or args.bitrate_mbps <= 0:
        parser.error('--fps and --bitrate-mbps must be positive')
    ffmpeg = shutil.which('ffmpeg')
    if ffmpeg is None:
        parser.error('FFmpeg with h264_nvenc not found. Install it with your package manager')
    try:
        goggles = open_goggles()
    except usb.core.USBError as exc:
        raise SystemExit(f'Cannot open the goggles: {exc}. See the udev rule in the README')
    print('Starting capture. On the first run, choose a monitor in the screen-share dialog.', flush=True)
    process = subprocess.Popen(encoder_command(ffmpeg, args.size, args.fps, args.bitrate_mbps, args.show_cursor),
                               stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, bufsize=0)
    try:
        frames = stream(process.stdout.read, lambda data: goggles.write(OUT_ENDPOINT, data, timeout=WRITE_TIMEOUT_MS),
                        args.size, args.fps, lambda line: print(line, flush=True))
    except KeyboardInterrupt:
        return 0
    except usb.core.USBError as exc:
        print(f'USB write failed: {exc}. Is Moonlight open on the goggles?', file=sys.stderr)
        return 1
    except (RuntimeError, ValueError) as exc:
        print(f'Stream stopped: {exc}', file=sys.stderr)
        return 1
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        usb.util.dispose_resources(goggles)
    print(f'Capture ended after {frames} frames. Was screen sharing stopped?', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
