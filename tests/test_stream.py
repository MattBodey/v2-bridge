import argparse
import struct
import unittest
from unittest.mock import patch

from v2_bridge import __main__ as bridge


class StreamTests(unittest.TestCase):
    def test_header_precedes_length_prefixed_frames(self):
        writes = []
        chunks = iter([b'a', b'b', b''])
        with patch.object(bridge, 'AviPacketStream') as splitter:
            splitter.return_value.feed.side_effect = [[b'\x00\x00\x01\x65key'], [b'\x00\x00\x01\x41p', b'\x00\x00\x01\x41q']]
            frames = bridge.stream(lambda _: next(chunks), writes.append, '1440x810', 60, report=lambda _: None)
        self.assertEqual(frames, 3)
        self.assertEqual(splitter.call_args.kwargs['max_frame_bytes'], 1_000_000)
        self.assertEqual(writes[0], bytes.fromhex('69200400') + struct.pack('<3I', 1440, 810, 60))
        self.assertEqual(writes[1], struct.pack('<I', 7) + b'\x00\x00\x01\x65key')
        self.assertEqual(len(writes), 4)

    def test_no_header_without_a_frame(self):
        writes = []
        self.assertEqual(bridge.stream(lambda _: b'', writes.append, '1440x810', 60, report=lambda _: None), 0)
        self.assertEqual(writes, [])

    def test_stops_instead_of_writing_after_the_shim_timed_out(self):
        writes = []
        with patch.object(bridge, 'AviPacketStream') as splitter, \
                patch.object(bridge.time, 'perf_counter', side_effect=[0, 0, 3.5]), \
                self.assertRaisesRegex(RuntimeError, 'Restart the bridge'):
            splitter.return_value.feed.return_value = [b'first', b'late']
            bridge.stream(lambda _: b'data', writes.append, '1440x810', 60, report=lambda _: None)
        self.assertEqual(len(writes), 2)

    def test_size_must_suit_nv12(self):
        self.assertEqual(bridge.parse_size('1280x720'), '1280x720')
        for bad in ('1441x810', '0x810', '1440', 'axb'):
            with self.subTest(bad=bad), self.assertRaises(argparse.ArgumentTypeError):
                bridge.parse_size(bad)

    def test_encoder_settings_reach_helper_and_ffmpeg(self):
        command = bridge.encoder_command('/usr/bin/ffmpeg', '1280x720', 120, 20, False)
        helper = command[:command.index('--')]
        encoder = command[command.index('--')+1:]
        self.assertIn('--hide-cursor', helper)
        self.assertEqual(helper[helper.index('--size')+1], '1280x720')
        self.assertEqual(encoder[0], '/usr/bin/ffmpeg')
        for key, value in (('-video_size', '1280x720'), ('-framerate', '120'), ('-c:v', 'h264_nvenc'),
                           ('-b:v', '20000000'), ('-bufsize', str(40_000_000//120)), ('-aud', '1'), ('-g', '360')):
            self.assertEqual(encoder[encoder.index(key)+1], value)
        self.assertEqual(encoder[-3:], ['-f', 'avi', 'pipe:1'])


if __name__ == '__main__':
    unittest.main()
