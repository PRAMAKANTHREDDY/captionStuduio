#!/usr/bin/env python3
"""
AndroidRecorder - one-command capture of a connected Android device.

Running this starts, in parallel:
  1. Video + device audio recording  (scrcpy -> .mp4)   [captures TalkBack TTS]
  2. Full logcat capture              (adb logcat -> .log)
  3. A UI accessibility-tree dump taken at start         (.xml)

Everything is written to recordings/<timestamp>/.
Press Ctrl+C (or type 'stop' + Enter) to finish; files are finalized cleanly.

Usage:
    python3 app.py                 # record until stopped
    python3 app.py --no-audio      # video only (if audio capture misbehaves)
    python3 app.py --name mytest   # label the session folder
"""

import argparse
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


def get_device():
    out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
    devices = [l.split("\t")[0] for l in out.splitlines()[1:]
               if l.strip() and l.strip().endswith("device")]
    if not devices:
        sys.exit("ERROR: no Android device detected by adb. "
                 "Connect the device and enable USB debugging.")
    return devices[0]


def device_info(serial):
    def prop(p):
        return subprocess.run(["adb", "-s", serial, "shell", "getprop", p],
                              capture_output=True, text=True).stdout.strip()
    return (f"model={prop('ro.product.model')} "
            f"android={prop('ro.build.version.release')} "
            f"sdk={prop('ro.build.version.sdk')}")


def session_dir(name):
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    label = f"{ts}_{name}" if name else ts
    path = os.path.join(REC_DIR, label)
    os.makedirs(path, exist_ok=True)
    return path


def dump_ui(serial, out_path):
    try:
        subprocess.run(["adb", "-s", serial, "shell", "uiautomator", "dump",
                        "/sdcard/_ar_dump.xml"], capture_output=True, timeout=20)
        subprocess.run(["adb", "-s", serial, "pull", "/sdcard/_ar_dump.xml",
                        out_path], capture_output=True, timeout=20)
    except Exception as e:
        print(f"  (ui dump skipped: {e})")


def main():
    ap = argparse.ArgumentParser(description="Record connected Android device: video+audio+logs")
    ap.add_argument("--no-audio", action="store_true", help="record video only")
    ap.add_argument("--name", default="", help="label for the session folder")
    args = ap.parse_args()

    check_prereqs()
    serial = get_device()
    sess = session_dir(args.name)

    video = os.path.join(sess, "video.mp4")
    log = os.path.join(sess, "logcat.log")
    ui = os.path.join(sess, "ui_dump_start.xml")

    print("=" * 60)
    print("AndroidRecorder")
    print(f"  device  : {serial}")
    print(f"  info    : {device_info(serial)}")
    print(f"  session : {sess}")
    print("=" * 60)

    # start UI dump (snapshot of the starting screen's accessibility tree)
    print("[1/3] UI accessibility-tree dump ...")
    dump_ui(serial, ui)

    # start logcat capture
    print("[2/3] logcat capture ...")
    subprocess.run(["adb", "-s", serial, "logcat", "-c"])  # clear buffer
    log_f = open(log, "w")
    log_proc = subprocess.Popen(["adb", "-s", serial, "logcat", "-v", "time"],
                                stdout=log_f, stderr=subprocess.STDOUT)

    # start scrcpy video(+audio) recording
    print("[3/3] screen + audio recording ...")
    # --no-video-playback: don't open a video window / play video on the Mac,
    #   but DO let audio play through the Mac speakers so you hear the TTS live.
    # --audio-source=playback: capture the device playback (incl. TalkBack TTS)
    #   into the recording. The same stream is played on the Mac AND written to
    #   the mp4, so you get audio live while testing and in the recording.
    scrcpy_cmd = ["scrcpy", "-s", serial, "--record", video, "--no-video-playback"]
    if not args.no_audio:
        scrcpy_cmd += ["--audio-source=playback"]
    else:
        scrcpy_cmd += ["--no-audio", "--no-playback"]
    # launch scrcpy as a group leader so we can deliver SIGINT to the whole group,
    # exactly like an interactive Ctrl+C (the only thing scrcpy finalizes the mp4 on).
    scrcpy_proc = subprocess.Popen(scrcpy_cmd, start_new_session=True)

    # give scrcpy time to initialize the recording before a stop is allowed,
    # otherwise the mp4 trailer (moov atom) is never written and the file is unplayable
    time.sleep(3)

    print("\n>>> RECORDING. Do your actions on the device.")
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
            if scrcpy_proc.poll() is not None:  # scrcpy window closed
                break
            time.sleep(0.3)
    except KeyboardInterrupt:
        pass

    print("\nStopping ...")

    # scrcpy finalizes the mp4 (writes the moov trailer) only on an interactive-style
    # SIGINT. Deliver it to scrcpy's process group, then wait for it to drain and exit.
    if scrcpy_proc.poll() is None:
        print("  finalizing video (writing mp4 trailer, do not force-quit)...")
        try:
            os.killpg(os.getpgid(scrcpy_proc.pid), signal.SIGINT)
        except ProcessLookupError:
            pass
        try:
            scrcpy_proc.wait(timeout=30)
            print("  video finalized.")
        except subprocess.TimeoutExpired:
            print("  WARNING: scrcpy did not finalize in 30s; terminating.")
            scrcpy_proc.terminate()

    log_proc.terminate()
    try:
        log_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        log_proc.kill()
    log_f.close()

    print("=" * 60)
    print("DONE. Saved:")
    for p in (video, log, ui):
        exists = "OK " if os.path.exists(p) and os.path.getsize(p) > 0 else "-- "
        size = f"{os.path.getsize(p):,} B" if os.path.exists(p) else "missing"
        print(f"  [{exists}] {p}  ({size})")
    print("=" * 60)


if __name__ == "__main__":
    main()
