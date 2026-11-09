import argparse
import contextlib
import io
from pathlib import Path
import struct
import tempfile
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

    def test_compositor_paces_capture_at_the_stream_rate(self):
        from v2_bridge import linux_capture
        command = linux_capture.gst_command(7, 42, 90, '1440x810')
        self.assertIn('video/x-raw,max-framerate=90/1', command)
        self.assertNotIn('videorate', command)

    def run_main(self, config, flags):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.toml'
            if config is not None:
                path.write_text(config)
            with patch.object(bridge, 'CONFIG_FILE', path), patch.object(bridge.sys, 'argv', ['v2_bridge', *flags]), \
                    patch.object(bridge.shutil, 'which', return_value='/usr/bin/ffmpeg'), \
                    patch.object(bridge, 'open_goggles'), patch.object(bridge.subprocess, 'Popen'), \
                    patch.object(bridge, 'stream', return_value=1), patch.object(bridge.usb.util, 'dispose_resources'), \
                    patch.object(bridge, 'encoder_command') as command, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                bridge.main()
        return command.call_args.args

    def test_flags_override_config_which_overrides_defaults(self):
        self.assertEqual(self.run_main(None, [])[1:4], ('1440x810', 60, 30))
        self.assertEqual(self.run_main('fps = 120\nbitrate-mbps = 20\nsize = "1280x720"\n', [])[1:4], ('1280x720', 120, 20))
        self.assertEqual(self.run_main('fps = 120\n', ['--fps', '90'])[2], 90)
        self.assertIs(self.run_main('hide-cursor = true\n', [])[4], False)
        self.assertIs(self.run_main('hide-cursor = false\n', [])[4], True)
        self.assertIs(self.run_main('hide-cursor = true\n', ['--show-cursor'])[4], True)
        self.assertIs(self.run_main(None, [])[4], True)

    def test_unreadable_config_reports_a_message(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / 'binary.toml'
            binary.write_bytes(b'fps = "\xff"\n')
            for path in (Path(folder), binary):
                with self.subTest(path=path.name), self.assertRaisesRegex(SystemExit, 'Cannot read config file'):
                    bridge.config_arguments(path)

    def test_bad_config_is_rejected(self):
        for bad in ('fps = true\n', 'typo = 1\n', 'fps = \n', 'fps = "fast"\n', 'size = "1441x810"\n', 'hide-cursor = 1\n'):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                self.run_main(bad, [])

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
