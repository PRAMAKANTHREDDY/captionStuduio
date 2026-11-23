// Records a USB-connected iPhone's screen + iPhone audio from the Mac (like QuickTime).
// Records until Ctrl+C / SIGINT / SIGTERM, then finalizes the file cleanly.
// Usage: ios_record <output.mov> [--mute]   (--mute: don't play audio on the Mac)
import AVFoundation
import CoreMediaIO

setvbuf(stdout, nil, _IOLBF, 0)
guard CommandLine.arguments.count > 1 else { print("usage: ios_record <output.mov>"); exit(2) }
let url = URL(fileURLWithPath: CommandLine.arguments[1])

// Expose iOS devices as capture devices (off by default).
var prop = CMIOObjectPropertyAddress(
    mSelector: CMIOObjectPropertySelector(kCMIOHardwarePropertyAllowScreenCaptureDevices),
    mScope: CMIOObjectPropertyScope(kCMIOObjectPropertyScopeGlobal),
    mElement: CMIOObjectPropertyElement(kCMIOObjectPropertyElementMain))
var allow: UInt32 = 1
CMIOObjectSetPropertyData(CMIOObjectID(kCMIOObjectSystemObject), &prop, 0, nil, UInt32(MemoryLayout<UInt32>.size), &allow)

func findDevice() -> AVCaptureDevice? {
    AVCaptureDevice.DiscoverySession(deviceTypes: [.external], mediaType: .muxed, position: .unspecified)
        .devices.first
}

var device: AVCaptureDevice?
for _ in 0..<20 {  // device appears a few seconds after enabling
    device = findDevice()
    if device != nil { break }
    RunLoop.current.run(until: Date().addingTimeInterval(0.5))
}
guard let device else { print("ERROR: no iPhone capture device found. Unlock the phone and trust this Mac."); exit(1) }
print("Found: \(device.localizedName)")

// The muxed iPhone device carries both screen video and the iPhone's audio output.
let session = AVCaptureSession()
do { session.addInput(try AVCaptureDeviceInput(device: device)) } catch { print("ERROR: input: \(error)"); exit(1) }
let output = AVCaptureMovieFileOutput()
session.addOutput(output)

// Play the iPhone audio live on the Mac's speakers while recording
// (iOS routes audio to the Mac during capture, so the phone itself goes quiet).
if !CommandLine.arguments.contains("--mute") {
    let monitor = AVCaptureAudioPreviewOutput()
    monitor.volume = 1.0
    if session.canAddOutput(monitor) { session.addOutput(monitor) }
}

class Delegate: NSObject, AVCaptureFileOutputRecordingDelegate {
    func fileOutput(_ o: AVCaptureFileOutput, didStartRecordingTo url: URL, from c: [AVCaptureConnection]) {
        print("RECORDING")
    }
    func fileOutput(_ o: AVCaptureFileOutput, didFinishRecordingTo url: URL, from c: [AVCaptureConnection], error: Error?) {
        if let error { print("Finished with: \(error.localizedDescription)") }
        print("Saved: \(url.path)")
        exit(0)
    }
}
let delegate = Delegate()
try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
try? FileManager.default.removeItem(at: url)

// Stop cleanly on Ctrl+C / kill.
var sources: [DispatchSourceSignal] = []
for sig in [SIGINT, SIGTERM] {
    signal(sig, SIG_IGN)
    let s = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    s.setEventHandler {
        if output.isRecording { output.stopRecording() } else { exit(1) }
    }
    s.resume()
    sources.append(s)
}

session.startRunning()
// Let the device stream warm up before recording so nothing at the start is lost.
DispatchQueue.main.asyncAfter(deadline: .now() + 2) { output.startRecording(to: url, recordingDelegate: delegate) }
RunLoop.main.run()
