#!/usr/bin/env python3
"""Tall ASCII T/J rocking in opposite directions, without mirroring the J. Python 3; no dependencies.
Run: python3 tj-spin-ultra.py [--fps 16] [--no-color]
Ctrl-C exits and restores the terminal. Resizes to fit its tmux pane.
Pauses in hidden/detached tmux panes. Use --popup for a tmux popup.
Original green faces/darker green sides, no shimmer or gradient.
Caches 49 distinct poses and changed-row spans; no dependencies.
"""
import argparse
import os
import subprocess
from collections import OrderedDict
import math
import shutil
import signal
import sys
import time


def motion_angle(phase):
    # Continuous sinusoidal motion: slow smoothly at the edge-on reversals.
    return (math.pi / 2) * math.sin(phase)


def pose_index(frame):
    """The sine wave retraces its poses; cache each pose just once."""
    frame %= 96
    if frame <= 24:
        return frame
    if frame <= 72:
        return 48 - frame
    return frame - 96


def inside(x, y, letter):
    if letter == 'T':
        return (-3.5 <= y <= -2 and abs(x) <= 3.65) or (-2 < y <= 3.5 and abs(x) <= .85)
    # Extend the hook left by 3.0 units, preserving its stroke thickness.
    right = math.hypot(x + 1.3, y - 1.5)
    left = math.hypot(x + 4.3, y - 1.5)
    return ((abs(x) <= .85 and -3.5 <= y <= 1.5)
            or (y >= 1.5 and x >= -1.3 and .45 <= right <= 2.15)
            or (y >= 1.5 and x <= -4.3 and .45 <= left <= 2.15)
            or (-4.3 <= x <= -1.3 and 1.95 <= y <= 3.65)
            or (-6.45 <= x <= -4.75 and .75 <= y <= 1.5))



def surface(letter):
    points = []
    step = .16
    for iy in range(47):
        y = -3.5 + iy * step
        for ix in range(66):
            x = -6.56 + ix * step
            if not inside(x, y, letter):
                continue
            points.extend([(x, y, -.55, '@'), (x, y, .55, '@')])
            if any(not inside(x + dx, y + dy, letter)
                   for dx, dy in ((step, 0), (-step, 0), (0, step), (0, -step))):
                points.extend((x, y, z, '+') for z in (-.4, -.2, 0, .2, .4))
    # Sample the exact vertical edges, so both aligned strokes occupy the
    # intended terminal columns even when the pane is resized.
    edges = (-3.65, -.85, .85, 3.65) if letter == 'T' else (-6.45, -4.75, -.85, .85)
    for x in edges:
        for iy in range(47):
            y = -3.5 + iy * step
            if inside(x, y, letter):
                points.extend([(x, y, -.55, '@'), (x, y, .55, '@')])
    return points


def render(objects, phase, cols, rows):
    width, height = min(cols - 1, 67), min(rows - 1, 35)
    if width < 16 or height < 10:
        return ()
    scale = min((width - 2) / 31, (height - 2) / 16)
    grid = [[' '] * width for _ in range(height)]
    depth = [[-float('inf')] * width for _ in range(height)]
    for points, cx, cy, direction in objects:
        a = direction * motion_angle(phase)
        c, s = math.cos(a), math.sin(a)
        for x, y, z, ch in points:
            rx, rz = x * c + z * s, -x * s + z * c
            col = round(width / 2 + (rx + cx) * 2 * scale)
            row = round(height / 2 + (y + cy) * 1.3 * scale)
            if 0 <= col < width and 0 <= row < height and rz > depth[row][col]:
                depth[row][col] = rz
                grid[row][col] = ch
    # Colors are fixed by glyph: no per-cell color buffers or color math.
    return tuple(''.join(cells).encode('ascii') for cells in grid)


def encode_delta(previous, current, cols, rows, color=True):
    """Paint changed row spans, emitting a color code only when it changes."""
    if not current:
        return b'' if previous == current else b'\x1b[HResize pane to 17x11+'
    width, height = len(current[0]), len(current)
    left, top = max(0, (cols - width) // 2), max(0, (rows - height) // 2)
    output = bytearray()
    active = None
    for row, chars in enumerate(current):
        old = previous[row] if previous else b' ' * width
        if chars == old:
            continue
        start = 0
        while chars[start] == old[start]:
            start += 1
        end = width - 1
        while chars[end] == old[end]:
            end -= 1
        output.extend(f'\x1b[{top + row + 1};{left + start + 1}H'.encode())
        for ch in chars[start:end + 1]:
            if color and ch != 32 and ch != active:
                output.extend(b'\x1b[92m' if ch == 64 else b'\x1b[32m')
                active = ch
            output.append(ch)
    if output and color:
        output.extend(b'\x1b[0m')
    return bytes(output)


class Visibility:
    """Check tmux at most once every two seconds, without blocking frames.

    TTY matching avoids mistaking a popup for the pane that launched it.
    Errors fail open; no tmux hooks or configuration changes are installed.
    """
    def __init__(self, disabled=False):
        self.pane = os.environ.get('TMUX_PANE')
        self.enabled = bool(not disabled and self.pane and os.environ.get('TMUX') and shutil.which('tmux'))
        try:
            self.tty = os.ttyname(sys.stdout.fileno())
        except OSError:
            self.enabled = False
            self.tty = ''
        self.visible = True
        self.next_check = 0.0
        self.process = None
        self.started = 0.0

    def apply(self, text):
        parts = text.strip().split('|')
        if len(parts) != 7:
            self.visible = True
            return
        tty, clients, zoomed, active, in_mode, attached, window_active = parts
        if tty != self.tty:
            # Popup or nested PTY: the parent pane does not describe visibility.
            self.enabled = False
            self.visible = True
            return
        watching = int(clients) > 0 if clients else (int(attached) > 0 and window_active == '1')
        self.visible = watching and not (zoomed == '1' and active != '1') and in_mode == '0'

    def poll(self, now):
        if self.process is not None:
            if self.process.poll() is not None:
                try:
                    data = self.process.communicate()[0]
                    if self.process.returncode == 0:
                        self.apply(data)
                    else:
                        self.visible = True
                except (OSError, ValueError):
                    self.visible = True
                self.process = None
            elif now - self.started > .75:
                self.close()
                self.visible = True
        if self.enabled and self.process is None and now >= self.next_check:
            self.next_check = now + 2.0
            fmt = '#{pane_tty}|#{window_active_clients}|#{window_zoomed_flag}|#{pane_active}|#{pane_in_mode}|#{session_attached}|#{window_active}'
            try:
                self.process = subprocess.Popen(['tmux', 'display-message', '-p', '-t', self.pane, fmt],
                                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                                stderr=subprocess.DEVNULL, text=True)
                self.started = now
            except OSError:
                self.enabled = False
                self.visible = True
        return self.visible

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.kill()
            self.process.communicate()
            self.process = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fps', type=int, default=16, choices=range(1, 61), metavar='1-60')
    parser.add_argument('--no-color', action='store_true')
    parser.add_argument('--popup', action='store_true', help='disable parent-pane visibility checks for a popup')
    parser.add_argument('--no-auto-pause', action='store_true', help='keep playing when the tmux pane is hidden')
    args = parser.parse_args()
    if not sys.stdout.isatty():
        parser.error('Run in an interactive terminal or tmux pane.')
    objects = [(surface('T'), -1.8, -1.0, 1), (surface('J'), 1, 1.25, -1)]
    visibility = Visibility(args.popup or args.no_auto_pause)
    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    size = None
    frames, transitions = {}, OrderedDict()
    previous_frame = None
    elapsed = 0.0
    last_tick = time.monotonic()
    was_visible = True
    output = sys.stdout.buffer
    try:
        output.write(b'\x1b[?1049h\x1b[?25l\x1b[0m\x1b[2J')
        output.flush()
        while True:
            tick = time.monotonic()
            visible = visibility.poll(tick)
            if visible and was_visible:
                elapsed += tick - last_tick
            resumed = visible and not was_visible
            last_tick, was_visible = tick, visible
            if not visible:
                time.sleep(.5)
                continue
            current_size = os.get_terminal_size(sys.stdout.fileno())
            if current_size != size:
                size = current_size
                frames.clear()
                transitions.clear()
                previous_frame = None
                output.write(b'\x1b[0m\x1b[2J')
            elif resumed:
                previous_frame = None
                output.write(b'\x1b[0m\x1b[2J')
            frame = pose_index(int(elapsed / 5.76 * 96))
            if frame != previous_frame:
                if frame not in frames:
                    frames[frame] = render(objects, frame * math.tau / 96, *size)
                key = (previous_frame, frame)
                payload = transitions.get(key)
                if payload is None:
                    before = frames.get(previous_frame)
                    payload = encode_delta(before, frames[frame], *size, not args.no_color)
                    transitions[key] = payload
                    if len(transitions) > 192:
                        transitions.popitem(last=False)
                else:
                    transitions.move_to_end(key)
                output.write(payload)
                previous_frame = frame
            output.flush()
            time.sleep(max(0, 1 / args.fps - (time.monotonic() - tick)))
    except KeyboardInterrupt:
        pass
    finally:
        visibility.close()
        output.write(b'\x1b[0m\x1b[?25h\x1b[?1049l')
        output.flush()


if __name__ == '__main__':
    main()
