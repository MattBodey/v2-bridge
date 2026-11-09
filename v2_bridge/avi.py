"""Read complete H.264 access units, without their AUDs, from FFmpeg's nonseekable, single-stream AVI."""
import struct
from .annexb import strip_aud


class AviPacketStream:
    def __init__(self, max_frame_bytes=2*1024*1024, max_header_bytes=1024*1024):
        for limit in (max_frame_bytes, max_header_bytes):
            if type(limit) is not int or limit < 64:
                raise ValueError('Invalid AVI buffer limit')
        self.frame_limit = max_frame_bytes
        self.header_limit = max_header_bytes
        self.buffer = bytearray()
        self.phase = 'riff'
        self.header_seen = False
        self.pad_due = False

    def _check_header(self, data):
        found = {'avih': 0, 'strh': 0, 'strf': 0}

        def chunks(body, depth=0):
            if depth > 3:
                raise ValueError('AVI header nesting exceeds bound')
            offset = 0
            while offset < len(body):
                if len(body)-offset < 8:
                    raise ValueError('Truncated AVI header chunk')
                tag, size = struct.unpack_from('<4sI', body, offset)
                end = offset+8+size
                next_chunk = end+(size&1)
                if next_chunk > len(body):
                    raise ValueError('AVI header chunk exceeds parent')
                payload = body[offset+8:end]
                if tag == b'LIST':
                    if size < 4:
                        raise ValueError('Missing AVI list type')
                    if payload[:4] == b'strl':
                        chunks(payload[4:], depth+1)
                elif tag == b'avih':
                    found['avih'] += 1
                    if size < 56 or struct.unpack_from('<I', payload, 24)[0] != 1:
                        raise ValueError('Expected one AVI video stream')
                elif tag == b'strh':
                    found['strh'] += 1
                    if size < 56 or payload[:8] != b'vidsH264':
                        raise ValueError('Expected H264 AVI stream')
                elif tag == b'strf':
                    found['strf'] += 1
                    if size < 40 or payload[16:20] != b'H264':
                        raise ValueError('Expected H264 AVI bitmap format')
                offset = next_chunk

        chunks(data)
        if any(count != 1 for count in found.values()):
            raise ValueError('Missing or duplicate AVI stream headers')

    def feed(self, data):
        self.buffer.extend(data)
        packets = []
        while True:
            if self.phase == 'riff':
                if len(self.buffer) < 12:
                    break
                if self.buffer[:4] != b'RIFF' or self.buffer[8:12] != b'AVI ':
                    raise ValueError('Expected AVI RIFF header')
                if struct.unpack_from('<I', self.buffer, 4)[0] < 4:
                    raise ValueError('Invalid AVI RIFF length')
                del self.buffer[:12]
                self.phase = 'header'
            if self.pad_due:
                if not self.buffer:
                    break
                del self.buffer[:1]
                self.pad_due = False
            if len(self.buffer) < 8:
                break
            tag, size = struct.unpack_from('<4sI', self.buffer)
            if self.phase == 'header':
                if tag == b'LIST':
                    if size < 4:
                        raise ValueError('Missing AVI list type')
                    if len(self.buffer) < 12:
                        break
                    kind = self.buffer[8:12]
                    if kind == b'movi':
                        if not self.header_seen:
                            raise ValueError('AVI movie before stream header')
                        del self.buffer[:12]
                        self.phase = 'frames'
                        continue
                if size > self.header_limit:
                    raise ValueError('AVI header exceeds configured bound')
                if len(self.buffer) < 8+size:
                    break
                if tag == b'LIST' and kind == b'hdrl':
                    if self.header_seen:
                        raise ValueError('Duplicate AVI header list')
                    self._check_header(self.buffer[12:8+size])
                    self.header_seen = True
                del self.buffer[:8+size]
                self.pad_due = bool(size&1)
            else:
                if tag not in (b'00dc', b'JUNK', b'idx1'):
                    raise ValueError(f'Unexpected AVI movie chunk {tag!r}')
                limit = self.frame_limit if tag == b'00dc' else self.header_limit
                if size > limit:
                    raise ValueError('AVI chunk exceeds configured bound')
                if len(self.buffer) < 8+size:
                    break
                if tag == b'00dc' and size:
                    packets.append(strip_aud(self.buffer[8:8+size]))
                del self.buffer[:8+size]
                self.pad_due = bool(size&1)
        return packets

    def finish(self):
        if self.phase != 'frames' or self.buffer or self.pad_due:
            raise ValueError('Truncated AVI stream')
