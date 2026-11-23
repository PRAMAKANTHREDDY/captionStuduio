#!/usr/bin/env python3
"""
iOS recorder - one-command capture of a USB-connected iPhone.

Running this starts, in parallel:
  1. Screen video + iPhone audio  (AVFoundation, like QuickTime -> .mov)
  2. Device syslog                (idevicesyslog -> .log)

Everything is written to recordings/<timestamp>[_name]/.
Press Ctrl+C (or type 'stop' + Enter) to finish; files are finalized cleanly.

Usage:
    python3 ios_app.py               # record until stopped
    python3 ios_app.py --name mytest # label the session folder
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
SWIFT_SRC = os.path.join(ROOT, "ios_record.swift")
RECORDER_BIN = os.path.join(ROOT, ".build", "ios_record")


def check_prereqs():
    missing = [c for c in ("idevice_id", "idevicesyslog", "swiftc") if not shutil.which(c)]
    if missing:
        sys.exit(f"ERROR: missing required tool(s): {', '.join(missing)}\n"
                 "Install with: brew install libimobiledevice  (swiftc comes with Xcode CLT)")


def get_device():
    out = subprocess.run(["idevice_id", "-l"], capture_output=True, text=True).stdout.split()
    if not out:
        sys.exit("ERROR: no iPhone detected. Connect via USB, unlock it, and tap Trust.")
    return out[0]


def device_info(udid):
    def val(key):
        return subprocess.run(["ideviceinfo", "-u", udid, "-k", key],
                              capture_output=True, text=True).stdout.strip()
    if not shutil.which("ideviceinfo"):
        return ""
    return f"name={val('DeviceName')} model={val('ProductType')} ios={val('ProductVersion')}"


def build_recorder():
    """Compile the Swift recorder once (and again whenever the source changes)."""
    if (os.path.exists(RECORDER_BIN)
            and os.path.getmtime(RECORDER_BIN) >= os.path.getmtime(SWIFT_SRC)):
        return
    print("  compiling recorder (first run only) ...")
    os.makedirs(os.path.dirname(RECORDER_BIN), exist_ok=True)
    r = subprocess.run(["swiftc", "-O", SWIFT_SRC, "-o", RECORDER_BIN],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"ERROR: failed to compile recorder:\n{r.stderr}")


def session_dir(name):
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(REC_DIR, f"{ts}_ios_{name}" if name else f"{ts}_ios")
    os.makedirs(path, exist_ok=True)
    return path


def main():
    ap = argparse.ArgumentParser(description="Record connected iPhone: video+audio+logs")
    ap.add_argument("--name", default="", help="label for the session folder")
    ap.add_argument("--mute", action="store_true",
                    help="don't play the iPhone audio on the Mac (still recorded)")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # show status lines immediately, even when piped

    check_prereqs()
    udid = get_device()
    build_recorder()
    sess = session_dir(args.name)
    video = os.path.join(sess, "video.mov")
    log = os.path.join(sess, "syslog.log")

    print("=" * 60)
    print("iOS Recorder")
    print(f"  device  : {udid}")
    print(f"  info    : {device_info(udid)}")
    print(f"  session : {sess}")
    print("=" * 60)

    print("[1/2] syslog capture ...")
    log_f = open(log, "w")
    log_proc = subprocess.Popen(["idevicesyslog", "-u", udid],
                                stdout=log_f, stderr=subprocess.STDOUT)

    print("[2/2] screen + audio recording ...")
    rec_cmd = [RECORDER_BIN, video] + (["--mute"] if args.mute else [])
    rec_proc = subprocess.Popen(rec_cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                start_new_session=True)

    # wait until the recorder confirms recording has actually started
    started = False
    for line in rec_proc.stdout:
        line = line.strip()
        if line == "RECORDING":
            started = True
            break
        print(f"  {line}")
    if not started:
        log_proc.terminate()
        log_f.close()
        sys.exit("ERROR: recorder exited before recording started.")

    print("\n>>> RECORDING. Do your actions on the iPhone.")
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
        while not stop.is_set() and rec_proc.poll() is None:
            time.sleep(0.3)
    except KeyboardInterrupt:
        pass

    print("\nStopping ...")
    if rec_proc.poll() is None:
        print("  finalizing video (do not force-quit)...")
        rec_proc.send_signal(signal.SIGINT)
        try:
            rest, _ = rec_proc.communicate(timeout=30)
            for l in rest.splitlines():
                print(f"  {l}")
        except subprocess.TimeoutExpired:
            print("  WARNING: recorder did not finalize in 30s; terminating.")
            rec_proc.terminate()

    log_proc.terminate()
    try:
        log_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        log_proc.kill()
    log_f.close()

    print("=" * 60)
    print("DONE. Saved:")
    for p in (video, log):
        ok = "OK " if os.path.exists(p) and os.path.getsize(p) > 0 else "-- "
        size = f"{os.path.getsize(p):,} B" if os.path.exists(p) else "missing"
        print(f"  [{ok}] {p}  ({size})")
    print("=" * 60)


if __name__ == "__main__":
    main()
