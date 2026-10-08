// activity-capture helper (macOS)
//
// Thin native capture helper, started and supervised by Activity Tracker.app. Every ~1s it reads
// the foreground app + focused window title; on a cadence it captures the FOCUSED
// WINDOW (not the whole display), OCRs it with Apple's Vision framework, deletes
// the image immediately, and appends JSONL events to the spool dir. The Linux-side
// collector polls that spool. See capture/SPOOL-SCHEMA.md for the contract this
// file must honor.
//
// Capturing the focused window rather than the display is what makes attribution
// exact: with a chat app in a side pane, a full-display grab describes both and
// the reader cannot tell which one you were working in. It also makes change
// detection meaningful, since only the window you are in moves the signal.
//
// Build:   swiftc -O -o activity-capture capture-daemon.swift   (CI does this per arch)
// Perms:   needs Screen Recording (screenshots) + Accessibility (window titles),
//          granted once in System Settings > Privacy & Security. Because the app
//          launches this helper, macOS attributes both to Activity Tracker.app.
// Test:    activity-capture --ocr-file <image>  prints the OCR text (no permission needed)
//
// Env:
//   ACTIVITY_SPOOL_DIR   default ~/.activity-spool
//   ACTIVITY_BLOCKLIST   default $ACTIVITY_SPOOL_DIR/blocklist.json
//
// HARD INVARIANTS (see SPOOL-SCHEMA.md): blocklist match OR a private/incognito
// browser window => zero bytes written; screenshot deleted in a defer even if OCR
// throws; PAUSED flag halts all writes; no writes while idle >2min or screen locked.

import Foundation
import AppKit
import Vision
import CoreGraphics
import ImageIO
import ApplicationServices

// MARK: - Config

let home = FileManager.default.homeDirectoryForCurrentUser.path
let spoolDir = ProcessInfo.processInfo.environment["ACTIVITY_SPOOL_DIR"]
    ?? "\(home)/.activity-spool"
let blocklistPath = ProcessInfo.processInfo.environment["ACTIVITY_BLOCKLIST"]
    ?? "\(spoolDir)/blocklist.json"

// Cadence knobs — env-tunable so they can be changed via the LaunchAgent plist
// without recompiling. Lower = finer-grained tracking at higher CPU/battery cost.
func envDouble(_ key: String, _ def: Double) -> Double {
    if let v = ProcessInfo.processInfo.environment[key], let d = Double(v), d > 0 { return d }
    return def
}

let pollSeconds: TimeInterval = envDouble("ACTIVITY_POLL_SECONDS", 1)      // frontmost app/title sample rate
let ocrIntervalSeconds: TimeInterval = envDouble("ACTIVITY_OCR_INTERVAL", 2) // re-OCR the same window at most this often
let idleThresholdSeconds: Double = envDouble("ACTIVITY_IDLE_SECONDS", 120)
let spoolRetentionHours: Double = 48
let maxOcrChars = 2000

// Capture mode: "ocr" (local Vision OCR text, the default) or "frame" (save a
// downscaled, change-detected JPEG for a vision model to describe — see the app's vision worker). Frame mode reuses the daemon's Screen Recording grant + the
// blocklist/idle/pause gates, so blocked windows are never framed.
let captureMode = ProcessInfo.processInfo.environment["ACTIVITY_CAPTURE_MODE"] ?? "ocr"
let frameDir = ProcessInfo.processInfo.environment["ACTIVITY_FRAME_DIR"] ?? "\(spoolDir)/frames"
let frameIntervalSeconds: TimeInterval = envDouble("ACTIVITY_FRAME_INTERVAL", 30)
let frameMaxWidth = 1400

// Capture scope: "window" (the focused window only — the default and what makes
// attribution and change detection exact) or "display" (the whole primary screen,
// the pre-2026-08 behaviour). Falls back to the display whenever the window grab
// fails or returns something too small to be a real window.
let captureScope = ProcessInfo.processInfo.environment["ACTIVITY_CAPTURE_SCOPE"] ?? "window"

// Change detection. A 16x16 average hash (256 bits) replaces the old 8x8/64-bit
// one, but pixels alone are the WRONG signal for the thing we most want to track:
// typing a paragraph into a document flips almost no cells at any grid size. So
// OCR text is the authoritative signal when the screen has text, and the pixel
// hash only decides text-free screens (video, images, canvases).
let frameChangeThreshold = Int(envDouble("ACTIVITY_FRAME_CHANGE_THRESHOLD", 10))
let frameTextChangeRatio = envDouble("ACTIVITY_FRAME_TEXT_CHANGE_RATIO", 0.02)

// A window switch forces a capture (the first look at a new window is always worth
// recording), but no more often than this — otherwise rapid alt-tabbing floods the
// vision worker's backlog and pushes real work out of it.
let frameMinGapSeconds: TimeInterval = envDouble("ACTIVITY_FRAME_MIN_GAP", 10)

// MARK: - Parent watch
// Started by the app with ACTIVITY_PARENT_PID: if the app goes away (crash, force
// quit), the helper must not keep capturing on its own.
let parentPid: pid_t? = ProcessInfo.processInfo.environment["ACTIVITY_PARENT_PID"].flatMap { Int32($0) }
func parentGone() -> Bool {
    guard let pp = parentPid else { return false }
    return kill(pp, 0) != 0 && errno == ESRCH
}

// MARK: - Permission preflight (--check)
// Exits 0 if both TCC grants are present, 1 otherwise. These
// grants are per-binary-signature: they must be re-granted after every recompile.
if CommandLine.arguments.contains("--check") {
    let screenOk = CGPreflightScreenCaptureAccess()
    let axOk = AXIsProcessTrusted()
    print("Screen Recording: \(screenOk ? "GRANTED" : "MISSING")")
    print("Accessibility:    \(axOk ? "GRANTED" : "MISSING")")
    if !screenOk { _ = CGRequestScreenCaptureAccess() }   // triggers the system prompt
    exit(screenOk && axOk ? 0 : 1)
}

// MARK: - Blocklist

struct Blocklist {
    var procNames: [String] = []
    var titleRegexes: [NSRegularExpression] = []

    static func load(_ path: String) -> Blocklist {
        var bl = Blocklist()
        guard let data = FileManager.default.contents(atPath: path),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            FileHandle.standardError.write("[capture] WARNING: blocklist not loaded from \(path); capturing with NO blocklist\n".data(using: .utf8)!)
            return bl
        }
        bl.procNames = (json["procNames"] as? [String] ?? []).map { $0.lowercased() }
        for pat in (json["titlePatterns"] as? [String] ?? []) {
            if let re = try? NSRegularExpression(pattern: pat) {
                bl.titleRegexes.append(re)
            }
        }
        return bl
    }

    // Blocked if the app name/bundle id OR the window title matches.
    func blocks(proc: String, bundleId: String, title: String) -> Bool {
        let p = proc.lowercased()
        let b = bundleId.lowercased()
        for name in procNames where p.contains(name) || b.contains(name) {
            return true
        }
        let hay = title
        let range = NSRange(hay.startIndex..<hay.endIndex, in: hay)
        for re in titleRegexes where re.firstMatch(in: hay, range: range) != nil {
            return true
        }
        return false
    }
}

// MARK: - System state

func secondsSinceLastInput() -> Double {
    let anyEvent = CGEventType(rawValue: ~0)!   // kCGAnyInputEventType
    return CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: anyEvent)
}

func screenIsLocked() -> Bool {
    guard let dict = CGSessionCopyCurrentDictionary() as? [String: Any] else { return false }
    return (dict["CGSSessionScreenIsLocked"] as? Int ?? 0) == 1
}

func frontmostAppInfo() -> (name: String, bundleId: String, pid: pid_t, windowId: CGWindowID)? {
    // Read the frontmost on-screen window's owner LIVE from the window server.
    // NSWorkspace.frontmostApplication goes stale in a background daemon that does
    // not pump its run loop — it froze on a since-quit "Super Auto Pets" and
    // mislabeled every capture's proc. CGWindowList is always current.
    let opts: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
    guard let windows = CGWindowListCopyWindowInfo(opts, kCGNullWindowID) as? [[String: Any]] else {
        return nil
    }
    // Windows are front-to-back; the first layer-0 window is the frontmost app
    // (layer 0 = normal windows; skip the menu bar, Dock, overlays, panels).
    for w in windows {
        guard (w[kCGWindowLayer as String] as? Int) == 0,
              let pidNum = w[kCGWindowOwnerPID as String] as? Int else { continue }
        let pid = pid_t(pidNum)
        let name = (w[kCGWindowOwnerName as String] as? String) ?? "Unknown"
        let bundleId = NSRunningApplication(processIdentifier: pid)?.bundleIdentifier ?? ""
        // The window number is what `screencapture -l` needs to grab this window
        // alone. 0 means "unknown" and makes the capture fall back to the display.
        let windowId = CGWindowID(w[kCGWindowNumber as String] as? Int ?? 0)
        return (name, bundleId, pid, windowId)
    }
    return nil
}

// Focused window title via the Accessibility API. Empty string on failure.
func focusedWindowTitle(pid: pid_t) -> String {
    let appElem = AXUIElementCreateApplication(pid)
    // Bound AX calls so an unresponsive frontmost app can't hang the daemon loop.
    AXUIElementSetMessagingTimeout(appElem, 2.0)
    var windowRef: CFTypeRef?
    guard AXUIElementCopyAttributeValue(appElem, kAXFocusedWindowAttribute as CFString, &windowRef) == .success,
          let window = windowRef else {
        return ""
    }
    var titleRef: CFTypeRef?
    // swiftlint:disable:next force_cast
    guard AXUIElementCopyAttributeValue(window as! AXUIElement, kAXTitleAttribute as CFString, &titleRef) == .success,
          let title = titleRef as? String else {
        return ""
    }
    return title
}

// MARK: - Private / incognito browser detection
//
// Chromium browsers do NOT expose "Incognito" in the AX window title, so a title
// pattern can't catch them. We ask the browser directly via its AppleScript
// `mode` property. This needs a one-time Automation grant (activity-capture ->
// the browser). The call is timeout-bounded and fail-open: on any error/timeout
// it reports "not incognito" so the capture loop never hangs or over-blocks. With
// the grant in place it reliably flags incognito windows, which are then treated
// as a capture-time block (zero bytes), same as the blocklist.

let chromiumScriptNames: [String: String] = [
    "com.google.chrome": "Google Chrome",
    "com.brave.browser": "Brave Browser",
    "com.microsoft.edgemac": "Microsoft Edge",
    "org.chromium.chromium": "Chromium",
    "com.vivaldi.vivaldi": "Vivaldi",
]

// Run a subprocess with a hard wall-clock timeout. Returns trimmed stdout on a
// clean exit; nil on spawn failure, non-zero exit, or timeout (process killed).
func runWithTimeout(_ path: String, _ args: [String], seconds: Double) -> String? {
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: path)
    proc.arguments = args
    let outPipe = Pipe()
    proc.standardOutput = outPipe
    proc.standardError = FileHandle.nullDevice
    do { try proc.run() } catch { return nil }

    let sem = DispatchSemaphore(value: 0)
    DispatchQueue.global().async { proc.waitUntilExit(); sem.signal() }
    if sem.wait(timeout: .now() + seconds) == .timedOut {
        proc.terminate()
        if sem.wait(timeout: .now() + 0.5) == .timedOut { kill(proc.processIdentifier, SIGKILL) }
        return nil
    }
    guard proc.terminationStatus == 0 else { return nil }
    let data = outPipe.fileHandleForReading.readDataToEndOfFile()
    return String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
}

// nil = could not determine (error / Automation not granted); true = incognito.
func frontWindowIsIncognito(bundleId: String) -> Bool? {
    guard let appName = chromiumScriptNames[bundleId.lowercased()] else { return false }
    let script = "with timeout of 2 seconds\ntell application \"\(appName)\" to get mode of front window\nend timeout"
    guard let out = runWithTimeout("/usr/bin/osascript", ["-e", script], seconds: 3.0) else { return nil }
    return out == "incognito"
}

// MARK: - Screen capture

// Capture the target surface to a temp PNG, hand the decoded image to `body`, and
// delete the PNG. The image is ALWAYS deleted (defer) — invariant: images never
// persist. In "window" scope this grabs ONLY the focused window (`screencapture
// -l <windowid>`, `-o` to drop the drop-shadow); it falls back to a full-display
// grab if the window capture fails or comes back implausibly small (minimized or
// off-screen windows can yield a stub image).
func withCapturedImage<T>(windowId: CGWindowID, _ body: (CGImage) -> T) -> T? {
    let tmp = "\(NSTemporaryDirectory())actcap-\(UUID().uuidString).png"
    defer { try? FileManager.default.removeItem(atPath: tmp) }   // invariant: image never persists

    // Bounded via runWithTimeout so a hung screencapture can never freeze the loop
    // (this was the root cause of a multi-hour silent stall on 2026-07-28).
    func grab(_ args: [String]) -> CGImage? {
        guard runWithTimeout("/usr/sbin/screencapture", args + [tmp], seconds: 5) != nil,
              let img = NSImage(contentsOfFile: tmp),
              let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { return nil }
        return cg
    }

    var cg: CGImage?
    if captureScope == "window" && windowId != 0 {
        cg = grab(["-x", "-t", "png", "-o", "-l", String(windowId)])
        if let c = cg, c.width < 200 || c.height < 150 { cg = nil }   // stub image, not a real window
    }
    if cg == nil {
        cg = grab(["-x", "-t", "png"])
    }
    guard let image = cg else {
        FileHandle.standardError.write("[capture] screencapture failed or timed out\n".data(using: .utf8)!)
        return nil
    }
    return body(image)
}

// Recognize on-screen text with Apple's Vision framework. Capped at maxOcrChars.
func ocrText(_ cg: CGImage) -> String {
    var result = ""
    let request = VNRecognizeTextRequest { req, _ in
        guard let observations = req.results as? [VNRecognizedTextObservation] else { return }
        var lines: [String] = []
        for obs in observations {
            if let top = obs.topCandidates(1).first {
                lines.append(top.string)
            }
        }
        result = lines.joined(separator: "\n")
    }
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true

    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    try? handler.perform([request])

    if result.count > maxOcrChars {
        result = String(result.prefix(maxOcrChars))
    }
    return result
}

// Capture the focused window and OCR it. Returns the recognized text (may be empty).
func captureAndOcr(windowId: CGWindowID) -> String {
    return withCapturedImage(windowId: windowId) { ocrText($0) } ?? ""
}

// MARK: - Change detection

// 16x16 grayscale average hash (256 bits, four words) for "did the screen change".
// The old 8x8/64-bit version could not see a paragraph being typed; neither can
// this one, reliably — which is why textChangeRatio below is the primary signal
// and the hash only decides screens with no text on them.
func avgHash(_ cg: CGImage) -> [UInt64] {
    let side = 16
    let space = CGColorSpaceCreateDeviceGray()
    guard let ctx = CGContext(data: nil, width: side, height: side, bitsPerComponent: 8,
                              bytesPerRow: side, space: space,
                              bitmapInfo: CGImageAlphaInfo.none.rawValue) else { return [] }
    ctx.draw(cg, in: CGRect(x: 0, y: 0, width: side, height: side))
    guard let data = ctx.data else { return [] }
    let count = side * side
    let ptr = data.bindMemory(to: UInt8.self, capacity: count)
    var sum = 0
    for i in 0..<count { sum += Int(ptr[i]) }
    let avg = sum / count
    var hash = [UInt64](repeating: 0, count: count / 64)
    for i in 0..<count where Int(ptr[i]) > avg {
        hash[i / 64] |= (UInt64(1) << UInt64(i % 64))
    }
    return hash
}

func hamming(_ a: [UInt64], _ b: [UInt64]) -> Int {
    guard a.count == b.count, !a.isEmpty else { return Int.max }
    var d = 0
    for i in 0..<a.count { d += (a[i] ^ b[i]).nonzeroBitCount }
    return d
}

// Volatile UI text (clocks, unread counters, progress percentages) flips on every
// capture and would report "changed" forever, so it is dropped before comparing.
func isVolatileLine(_ s: String) -> Bool {
    if s.count < 3 { return true }
    return s.allSatisfy { $0.isNumber || ":./-% ".contains($0) }
}

func textLines(_ s: String) -> Set<String> {
    var out = Set<String>()
    for raw in s.split(separator: "\n") {
        let line = raw.trimmingCharacters(in: .whitespaces).lowercased()
        if !line.isEmpty && !isVolatileLine(line) { out.insert(line) }
    }
    return out
}

// Fraction of distinct text lines that differ between two screen reads
// (symmetric difference over union). 0 = identical text, 1 = nothing in common.
func textChangeRatio(_ a: String, _ b: String) -> Double {
    let sa = textLines(a), sb = textLines(b)
    let union = sa.union(sb).count
    if union == 0 { return 0 }
    return Double(union - sa.intersection(sb).count) / Double(union)
}

// MARK: - Frame capture (vision mode): focused-window JPEG + its OCR text

func downscaleAndSaveJPEG(_ cg: CGImage, to path: String, maxWidth: Int) -> Bool {
    let scale = min(1.0, Double(maxWidth) / Double(cg.width))
    let w = max(1, Int(Double(cg.width) * scale))
    let h = max(1, Int(Double(cg.height) * scale))
    guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                              space: CGColorSpaceCreateDeviceRGB(),
                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return false }
    ctx.interpolationQuality = .medium
    ctx.draw(cg, in: CGRect(x: 0, y: 0, width: w, height: h))
    guard let scaled = ctx.makeImage() else { return false }
    let rep = NSBitmapImageRep(cgImage: scaled)
    guard let jpeg = rep.representation(using: .jpeg, properties: [.compressionFactor: 0.6]) else { return false }
    do { try jpeg.write(to: URL(fileURLWithPath: path)); return true } catch { return false }
}

// Capture the focused window for the vision worker: a downscaled JPEG plus the
// window's OCR text, which the worker diffs against the previous capture of the
// same window to report what actually changed. Skips unchanged screens. The temp
// PNG never persists.
//
// `force` bypasses change detection (used on a window switch, where the first look
// at the new window is always worth recording).
func captureFrame(proc: String, title: String, windowId: CGWindowID, force: Bool) {
    _ = withCapturedImage(windowId: windowId) { cg -> Bool in
        let text = scrub(ocrText(cg))
        let h = avgHash(cg)

        // Text is authoritative when the window has text; the pixel hash only
        // decides text-free screens. Either signal alone is enough to capture.
        if !force {
            let pixelChanged = lastFrameHash.isEmpty || hamming(h, lastFrameHash) >= frameChangeThreshold
            let changed = text.isEmpty
                ? pixelChanged
                : (textChangeRatio(text, lastFrameText) >= frameTextChangeRatio || pixelChanged)
            if !changed { return false }
        }
        lastFrameHash = h
        lastFrameText = text

        try? FileManager.default.createDirectory(atPath: frameDir, withIntermediateDirectories: true)
        let base = "frame-\(Int(Date().timeIntervalSince1970 * 1000))"
        guard downscaleAndSaveJPEG(cg, to: "\(frameDir)/\(base).jpg", maxWidth: frameMaxWidth) else { return false }
        let ts = iso.string(from: Date())
        // The sidecar carries the OCR text so the vision worker can diff this
        // capture against the previous one WITHOUT a second screen grab. It stays
        // machine-local: only the derived diff is ever rendered downstream.
        let meta: [String: Any] = [
            "ts": ts, "proc": proc, "title": title, "frame": "\(base).jpg",
            "text": text, "scope": captureScope,
        ]
        if let d = try? JSONSerialization.data(withJSONObject: meta) {
            try? d.write(to: URL(fileURLWithPath: "\(frameDir)/\(base).json"))
        }
        writeEvent(["ts": ts, "source": "macos", "type": "frame", "proc": proc, "title": title, "frame": "\(base).jpg"])
        return true
    }
}

// MARK: - Secret scrub (defense in depth; the app scrubs again at ingest)

func scrub(_ text: String) -> String {
    var t = text
    let patterns: [(String, String)] = [
        ("(?i)(password|passwd|token|secret|api[_-]?key|access[_-]?key)\\s*[:=]\\s*\\S+", "$1=[REDACTED]"),
        ("(?i)bearer\\s+[A-Za-z0-9._-]{12,}", "Bearer [REDACTED]"),
        ("sk-[A-Za-z0-9_-]{16,}", "[REDACTED]"),
        ("(gh[pousr]|github_pat)_[A-Za-z0-9_]{20,}", "[REDACTED]"),
        ("AKIA[0-9A-Z]{16}", "[REDACTED]"),
        // Keep this list in sync with activitytracker/scrub.py: the helper writes
        // the spool to disk, so a pattern only the app knows leaves the raw secret
        // sitting in the spool until retention removes it.
        ("xox[baprs]-[A-Za-z0-9-]{10,}", "[REDACTED]"),
        ("AIza[0-9A-Za-z_-]{35}", "[REDACTED]"),
        ("-----BEGIN [A-Z ]*PRIVATE KEY-----", "[REDACTED PRIVATE KEY]"),
        ("(?i)([a-z][a-z0-9+.-]*://[^/\\s:@]+):[^/\\s@]+@", "$1:[REDACTED]@"),
        ("[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+:[^@\\s]+@", "[REDACTED]@")
    ]
    for (pat, rep) in patterns {
        if let re = try? NSRegularExpression(pattern: pat) {
            let range = NSRange(t.startIndex..<t.endIndex, in: t)
            t = re.stringByReplacingMatches(in: t, range: range, withTemplate: rep)
        }
    }
    return t
}

// MARK: - Spool writer

let iso: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime]
    f.timeZone = TimeZone.current
    return f
}()

func spoolFilePath(for date: Date) -> String {
    let df = DateFormatter()
    df.dateFormat = "yyyy-MM-dd"
    df.timeZone = TimeZone.current
    return "\(spoolDir)/events-\(df.string(from: date)).jsonl"
}

func writeEvent(_ event: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: event),
          var line = String(data: data, encoding: .utf8) else { return }
    line += "\n"
    let path = spoolFilePath(for: Date())
    if !FileManager.default.fileExists(atPath: path) {
        FileManager.default.createFile(atPath: path, contents: nil)
    }
    guard let fh = FileHandle(forWritingAtPath: path) else { return }
    defer { try? fh.close() }
    fh.seekToEndOfFile()
    fh.write(line.data(using: .utf8)!)
}

func pruneOldSpoolFiles() {
    let fm = FileManager.default
    guard let files = try? fm.contentsOfDirectory(atPath: spoolDir) else { return }
    let cutoff = Date().addingTimeInterval(-spoolRetentionHours * 3600)
    for f in files where f.hasPrefix("events-") && f.hasSuffix(".jsonl") {
        let full = "\(spoolDir)/\(f)"
        if let attrs = try? fm.attributesOfItem(atPath: full),
           let mod = attrs[.modificationDate] as? Date, mod < cutoff {
            try? fm.removeItem(atPath: full)
        }
    }
}

// MARK: - Focus sessions

// Close out the window currently held by emitting a `focus_end` carrying its real
// dwell time. Downstream this replaces duration *inferred* from event spacing,
// which cannot tell a two-second glance from an hour of work. Optional by
// contract (see SPOOL-SCHEMA.md) — the Windows daemon does not emit it.
func closeFocusSession(at now: Date) {
    guard !lastWindowKey.isEmpty, let since = lastFocusAt else { return }
    let dwell = Int(now.timeIntervalSince(since).rounded())
    lastFocusAt = nil
    guard dwell > 0 else { return }
    writeEvent([
        "ts": iso.string(from: now),
        "source": "macos",
        "type": "focus_end",
        "proc": lastFocusProc,
        "title": lastFocusTitle,
        "durationSec": dwell,
    ])
}

// MARK: - Main loop

func ensureSpoolDir() {
    try? FileManager.default.createDirectory(atPath: spoolDir, withIntermediateDirectories: true)
}

func isPaused() -> Bool {
    FileManager.default.fileExists(atPath: "\(spoolDir)/PAUSED")
}

// Write TCC grant status to a file the app reads to tell the user what is missing. Screen
// Recording / Accessibility loss is NOT a crash (the daemon keeps running, just
// with black screenshots / empty titles), so it needs explicit self-reporting.
func writeDaemonStatus() {
    let screenOk = CGPreflightScreenCaptureAccess()
    let axOk = AXIsProcessTrusted()
    let json = "{\"screenRecording\":\(screenOk),\"accessibility\":\(axOk),\"ts\":\"\(iso.string(from: Date()))\"}"
    try? json.write(toFile: "\(spoolDir)/.daemon-status", atomically: true, encoding: .utf8)
}

// --ocr-file <image>: OCR one image file and print the text. Lets the app's self-test
// prove Vision OCR works inside the packaged helper without any permission grant.
if let i = CommandLine.arguments.firstIndex(of: "--ocr-file"), i + 1 < CommandLine.arguments.count {
    let url = URL(fileURLWithPath: CommandLine.arguments[i + 1])
    guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
          let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
        print("could not read image"); exit(2)
    }
    print(ocrText(img))
    exit(0)
}

ensureSpoolDir()
var blocklist = Blocklist.load(blocklistPath)

FileHandle.standardError.write("[capture] started. spool=\(spoolDir) mode=\(captureMode) scope=\(captureScope) frameInterval=\(Int(frameIntervalSeconds))s blocklist=\(blocklist.procNames.count) procs / \(blocklist.titleRegexes.count) title patterns\n".data(using: .utf8)!)
writeDaemonStatus()

var lastWindowKey = ""          // "proc\u{1}title" of the last focus event written
var lastOcrAt = Date.distantPast
var lastOcrText = ""
var lastFocusProc = ""          // scrubbed identity of the window currently held...
var lastFocusTitle = ""         // ...so leaving it can be reported with its dwell
var lastFocusAt: Date? = nil
var lastPrune = Date.distantPast
var blocklistReloadedAt = Date()
var lastIncognitoKey = ""       // windowKey the incognito check last ran for
var lastIncognitoStatus: Bool? = false
var incognitoCheckWarned = false
var lastHeartbeat = Date.distantPast
var lastFrameHash: [UInt64] = []  // avg-hash of the last saved frame (change detection)
var lastFrameText = ""            // OCR text of the last saved frame (the primary signal)
var lastFrameAt = Date.distantPast

// Heartbeat watchdog: if the main loop stops making progress (any blocking call
// hangs despite the timeouts above), exit so the app restarts us fresh. Turns any future hang into a ~90s self-heal instead of a silent stall.
let tickLock = NSLock()
var lastLoopTick = Date()
func markTick() { tickLock.lock(); lastLoopTick = Date(); tickLock.unlock() }
Thread.detachNewThread {
    while true {
        Thread.sleep(forTimeInterval: 30)
        tickLock.lock(); let age = Date().timeIntervalSince(lastLoopTick); tickLock.unlock()
        if age > 90 {
            FileHandle.standardError.write("[capture] watchdog: loop stalled \(Int(age))s; exiting so the app restarts it\n".data(using: .utf8)!)
            exit(1)
        }
    }
}

while true {
    markTick()
    if parentGone() {
        closeFocusSession(at: Date())
        FileHandle.standardError.write("[capture] app is gone; exiting\n".data(using: .utf8)!)
        exit(0)
    }
    // Liveness heartbeat for the external health monitor (throttled ~15s). Written
    // every iteration regardless of idle/pause gating, so a fresh stamp means the
    // loop is alive and a stale one means stuck/dead — the monitor's key signal.
    if Date().timeIntervalSince(lastHeartbeat) > 15 {
        try? iso.string(from: Date()).write(toFile: "\(spoolDir)/.heartbeat", atomically: true, encoding: .utf8)
        lastHeartbeat = Date()
    }
    autoreleasepool {
        // Reload blocklist hourly so edits take effect without a restart.
        if Date().timeIntervalSince(blocklistReloadedAt) > 3600 {
            blocklist = Blocklist.load(blocklistPath)
            blocklistReloadedAt = Date()
            writeDaemonStatus()   // re-check TCC hourly in case a grant was lost
        }

        // Housekeeping: prune spool once an hour.
        if Date().timeIntervalSince(lastPrune) > 3600 {
            pruneOldSpoolFiles()
            lastPrune = Date()
        }

        // Gate: paused, idle, or locked => write nothing.
        if isPaused() || screenIsLocked() || secondsSinceLastInput() > idleThresholdSeconds {
            return
        }

        guard let app = frontmostAppInfo() else { return }
        let title = focusedWindowTitle(pid: app.pid)
        let windowKey = "\(app.name)\u{1}\(title)"

        // Invariant: blocklist match OR a private/incognito browser window =>
        // zero bytes, no screenshot.
        var blocked = blocklist.blocks(proc: app.name, bundleId: app.bundleId, title: title)
        if !blocked, chromiumScriptNames[app.bundleId.lowercased()] != nil {
            // Cache per window so osascript runs at most once per window change.
            if windowKey != lastIncognitoKey {
                lastIncognitoKey = windowKey
                lastIncognitoStatus = frontWindowIsIncognito(bundleId: app.bundleId)
            }
            if let inc = lastIncognitoStatus {
                blocked = inc
            } else if !incognitoCheckWarned {
                incognitoCheckWarned = true
                FileHandle.standardError.write("[capture] WARNING: could not read \(app.name) incognito state (allow Activity Tracker to control the browser under Privacy & Security > Automation). Private windows may be captured until then.\n".data(using: .utf8)!)
            }
        }
        if blocked {
            closeFocusSession(at: Date())   // the window we were in ended here
            lastWindowKey = ""   // force a fresh focus event when we leave the blocked window
            return
        }

        // Scrub the title before ANY write. Blocklist matching and the dedup key
        // above deliberately use the raw title (those patterns match plain words);
        // from here on only `safeTitle` may reach the spool. Titles are not
        // blocklisted, so a tab titled "Re: token=ghp_… — Gmail" would otherwise
        // put a live credential on disk and into everything downstream.
        let safeTitle = scrub(title)
        let windowChanged = windowKey != lastWindowKey
        let now = Date()

        if windowChanged {
            closeFocusSession(at: now)
            writeEvent([
                "ts": iso.string(from: now),
                "source": "macos",
                "type": "focus",
                "proc": app.name,
                "title": safeTitle
            ])
            lastWindowKey = windowKey
            lastFocusProc = app.name
            lastFocusTitle = safeTitle
            lastFocusAt = now
            // A new window is a new baseline: change detection compares like with
            // like, never this window's text against the previous window's.
            lastFrameHash = []
            lastFrameText = ""
            lastOcrText = ""
        }

        // Vision (frame) mode: save a downscaled, change-detected frame for the
        // app's vision worker instead of running local OCR.
        if captureMode == "frame" {
            // Steady cadence, plus a forced capture on a window switch (rate-limited
            // by frameMinGapSeconds) so switching to a window is never invisible.
            // Change detection still skips unchanged screens, so a static window
            // costs nothing.
            let sinceFrame = now.timeIntervalSince(lastFrameAt)
            if sinceFrame >= frameIntervalSeconds || (windowChanged && sinceFrame >= frameMinGapSeconds) {
                lastFrameAt = now
                captureFrame(proc: app.name, title: safeTitle, windowId: app.windowId, force: windowChanged)
            }
        } else if windowChanged || now.timeIntervalSince(lastOcrAt) >= ocrIntervalSeconds {
            // OCR on window change, or every ocrIntervalSeconds on the same window.
            lastOcrAt = now
            let text = scrub(captureAndOcr(windowId: app.windowId))
            // Skip the write if the screen text is unchanged (cheap dedup that
            // replaces image hashing: local Vision OCR is cheap, so we OCR then
            // compare text rather than hashing pixels).
            if !text.isEmpty && text != lastOcrText {
                lastOcrText = text
                writeEvent([
                    "ts": iso.string(from: now),
                    "source": "macos",
                    "type": "ocr",
                    "proc": app.name,
                    "title": safeTitle,
                    "text": text
                ])
            }
        }
    }
    Thread.sleep(forTimeInterval: pollSeconds)
}
