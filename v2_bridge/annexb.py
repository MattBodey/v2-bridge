"""Split encoder output at H.264 access unit delimiters (AUDs).

Input must include an AUD before each picture. The splitter removes the AUD
and preserves all other NAL units, including codec configuration for keyframes.
"""
import re

_AUD = re.compile(rb'\x00\x00(?:\x00)?\x01\x09')
_START = re.compile(rb'\x00\x00(?:\x00)?\x01')


def strip_aud(data):
    """Remove the delimiter from exactly one already-framed access unit."""
    data = bytes(data)
    aud = _AUD.match(data)
    if not aud:
        raise ValueError('Encoder output must begin each access unit with an AUD')
    match = _START.search(data, aud.end())
    if match is None:
        raise ValueError('Empty H.264 access unit')
    if _AUD.search(data, aud.end()):
        raise ValueError('Multiple access units in one encoder packet')
    return data[match.start():]


def is_idr(data):
    """Return whether an access unit contains an IDR picture (NAL type 5)."""
    return any(data[m.end()] & 0x1f == 5 for m in _START.finditer(data) if m.end() < len(data))


class AccessUnitStream:
    def __init__(self, max_frame_bytes=2 * 1024 * 1024):
        if type(max_frame_bytes) is not int or max_frame_bytes < 16:
            raise ValueError('Invalid frame limit')
        self.limit = max_frame_bytes
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        boundaries = [match.start() for match in _AUD.finditer(self.buffer)]
        frames = []
        if boundaries and boundaries[0] != 0:
            raise ValueError('Unexpected bytes before initial AUD')
        for start, end in zip(boundaries, boundaries[1:]):
            if end - start > self.limit:
                raise ValueError('H.264 access unit exceeds configured bound')
            frames.append(strip_aud(self.buffer[start:end]))
        if len(boundaries) > 1:
            del self.buffer[:boundaries[-1]]
        if len(self.buffer) > self.limit:
            raise ValueError('H.264 access unit exceeds configured bound')
        return frames

    def finish(self):
        if not self.buffer:
            return []
        result = strip_aud(self.buffer)
        self.buffer.clear()
        return [result]
