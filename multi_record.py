#!/usr/bin/env python3
"""
multi_record - record ALL connected Android devices at once.

For every device adb currently sees, this starts in parallel:
  1. Video recording (NO audio)      -> video.mp4
  2. Full logcat capture             -> logcat.log
  3. A UI accessibility-tree dump     -> ui_dump_start.xml

Each device gets its own folder named:  <MonthName>_<Date>_<DSN>
e.g.  recordings/November_17_R5CR127SRZD/

Press Ctrl+C (or type 'stop' + Enter) to finish; all recordings are
finalized cleanly for every device.

Usage:
    python3 multi_record.py
"""

import datetime as dt
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
REC_DIR = os.path.join(ROOT, "recordings")


def have(cmd):
    return shutil.which(cmd) is not None


def check_prereqs():
    missing = [c for c in ("adb", "scrcpy") if not have(c)]
    if missing:
        sys.exit(f"ERROR: missing required tool(s): {', '.join(missing)}\n"
                 f"Install with: brew install {' '.join(missing)}")


def get_devices():
    out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
    devices = [l.split("\t")[0] for l in out.splitlines()[1:]
               if l.strip() and l.strip().endswith("device")]
    if not devices:
        sys.exit("ERROR: no Android devices detected by adb.")
    return devices


def device_model(serial):
    return subprocess.run(["adb", "-s", serial, "shell", "getprop", "ro.product.model"],
                          capture_output=True, text=True).stdout.strip()


def session_dir(serial):
    now = dt.datetime.now()
    # MonthName_Date_DSN  e.g. November_17_R5CR127SRZD
    label = f"{now.strftime('%B')}_{now.strftime('%d')}_{serial}"
    path = os.path.join(REC_DIR, label)
    os.makedirs(path, exist_ok=True)
    return path


def dump_ui(serial, out_path):
    try:
        remote = f"/sdcard/_ar_dump_{serial}.xml"
        subprocess.run(["adb", "-s", serial, "shell", "uiautomator", "dump", remote],
                       capture_output=True, timeout=20)
        subprocess.run(["adb", "-s", serial, "pull", remote, out_path],
                       capture_output=True, timeout=20)
    except Exception as e:
        print(f"  [{serial}] ui dump skipped: {e}")


class DeviceRecorder:
    def __init__(self, serial):
        self.serial = serial
        self.dir = session_dir(serial)
        self.video = os.path.join(self.dir, "video.mp4")
        self.log = os.path.join(self.dir, "logcat.log")
        self.ui = os.path.join(self.dir, "ui_dump_start.xml")
        self.log_f = None
        self.log_proc = None
        self.scrcpy_proc = None

    def start(self):
        model = device_model(self.serial)
        print(f"[{self.serial}] model={model}  ->  {self.dir}")

        # UI dump snapshot
        dump_ui(self.serial, self.ui)

        # logcat
        subprocess.run(["adb", "-s", self.serial, "logcat", "-c"])
        self.log_f = open(self.log, "w")
        self.log_proc = subprocess.Popen(
            ["adb", "-s", self.serial, "logcat", "-v", "time"],
            stdout=self.log_f, stderr=subprocess.STDOUT)

        # video only, no audio, no window; own session for clean SIGINT finalize
        scrcpy_cmd = ["scrcpy", "-s", self.serial, "--record", self.video,
                      "--no-playback", "--no-audio"]
        self.scrcpy_proc = subprocess.Popen(scrcpy_cmd, start_new_session=True)

    def stop(self):
        # finalize video with one SIGINT to scrcpy's process group
        if self.scrcpy_proc and self.scrcpy_proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.scrcpy_proc.pid), signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                self.scrcpy_proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.scrcpy_proc.terminate()
        # stop logcat
        if self.log_proc:
            self.log_proc.terminate()
            try:
                self.log_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.log_proc.kill()
        if self.log_f:
            self.log_f.close()

    def report(self):
        for p in (self.video, self.log, self.ui):
            ok = os.path.exists(p) and os.path.getsize(p) > 0
            size = f"{os.path.getsize(p):,} B" if os.path.exists(p) else "missing"
            print(f"  [{'OK ' if ok else '-- '}] {p}  ({size})")


def main():
    check_prereqs()
    serials = get_devices()

    print("=" * 60)
    print(f"multi_record - {len(serials)} device(s) connected")
    print("=" * 60)

    recorders = [DeviceRecorder(s) for s in serials]
    for r in recorders:
        r.start()

    # let every scrcpy initialize before a stop is possible (mp4 trailer safety)
    time.sleep(3)

    print("\n>>> RECORDING all devices. Do your actions.")
    print(">>> Press Ctrl+C (or type 'stop' then Enter) to finish.\n")

    stop = threading.Event()

    def wait_for_stop_word():
        try:
            for line in sys.stdin:
                if line.strip().lower() in ("stop", "q", "quit"):
                    stop.set()
                    return
        except Exception:
            return

    threading.Thread(target=wait_for_stop_word, daemon=True).start()

    try:
        while not stop.is_set():
            if all(r.scrcpy_proc.poll() is not None for r in recorders):
                break
            time.sleep(0.3)
    except KeyboardInterrupt:
        pass

    print("\nStopping all devices (finalizing videos, do not force-quit)...")
    # stop in parallel so finalization overlaps
    threads = [threading.Thread(target=r.stop) for r in recorders]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print("=" * 60)
    print("DONE. Saved:")
    for r in recorders:
        print(f"\nDevice {r.serial}:")
        r.report()
    print("=" * 60)


if __name__ == "__main__":
    main()
