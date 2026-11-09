# V2 Bridge

Stream a Linux desktop to DJI FPV Goggles V2 over USB, with no Moonlight host or game-streaming server.

The stock V2 firmware can't take video from a PC, so this needs [WTFOS](https://fpv.wtf/) and [dji-moonlight-shim](https://github.com/fpv-wtf/dji-moonlight-shim) on the goggles. This project replaces the Windows PC side of that setup. It captures the desktop, encodes it with NVENC, and sends H.264 to the shim's USB bulk channel.

**Status:** experimental. It displayed video on one rooted FPV Goggles V2 at 1440x810 from KDE Plasma on Wayland, but the stream currently delivers only about 10 fps, even with a 60 fps target. Latency hasn't been measured, and the defaults below are starting points.

## Requirements

On the goggles:

- WTFOS and the `dji-moonlight-shim` package, both installed from [fpv.wtf](https://fpv.wtf/). Rooting is an unofficial modification, done at your own risk.

On the PC:

- Python 3.10 or later and libusb-1.0.
- A desktop session with xdg-desktop-portal and a ScreenCast backend. Tests used KDE Plasma on Wayland.
- An NVIDIA GPU and FFmpeg with `h264_nvenc`.
- GStreamer 1.22 or later with `gst-launch-1.0`, its base plugins, and its PipeWire plugin. On Arch, install `gstreamer gst-plugins-base gst-plugin-pipewire`.

If the bridge can't open the goggles, add this line to `/etc/udev/rules.d/60-dji-fpv-v2.rules`, then run `sudo udevadm control --reload && sudo udevadm trigger`:

```text
SUBSYSTEM=="usb", ATTR{idVendor}=="2ca3", ATTR{idProduct}=="001f", MODE="0660", TAG+="uaccess"
```

## Start streaming

1. Connect the goggles over USB, power them on, and select **Moonlight** from the goggles menu. Do this first: the bridge can't detect whether the shim is running.
2. Install the Python dependencies:

   ```bash
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   ```

3. Start the stream:

   ```bash
   .venv/bin/python -m v2_bridge
   ```

On the first run, the desktop asks which monitor to share. The grant is saved in `work/portal-restore-token`. To choose again, delete that file. Press **Ctrl+C** to stop, or **BACK** on the goggles to exit the shim.

| Option | Default | Notes |
| --- | --- | --- |
| `--size` | `1440x810` | Even width and height |
| `--fps` | `60` | Stream rate |
| `--bitrate-mbps` | `30` | Lower it if the picture breaks up |
| `--hide-cursor` | off | Hide the mouse pointer |

If the shim receives no frame for 3 seconds, it stops the stream and the bridge exits. Run the bridge again to reconnect.

## Run tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## License

Copyright (C) 2026 V2 Bridge contributors.

GPL-3.0-only. `v2_bridge/avi.py`, `v2_bridge/annexb.py`, and `v2_bridge/linux_capture.py` come from [G2 Bridge](https://github.com/Ammaar-Alam/g2-bridge), also GPL-3.0-only. The stream format is defined by [dji-moonlight-shim](https://github.com/fpv-wtf/dji-moonlight-shim).
