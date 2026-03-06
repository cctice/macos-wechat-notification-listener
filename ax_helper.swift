import AppKit
import ApplicationServices
import Foundation

final class BannerObserver {
    private struct BannerSnapshot: Hashable {
        let app: String
        let title: String
        let body: String
    }

    private var observers: [pid_t: AXObserver] = [:]
    private var deliveredSnapshots: [BannerSnapshot: Date] = [:]
    private var isPrimed = false
    private let processNames = ["UserNotificationCenter", "NotificationCenter"]
    private let ignoredLabels: Set<String> = [
        "Close", "关闭", "Options", "选项", "Reply", "回复", "View", "查看",
    ]
    private let observedNotifications: [CFString] = [
        kAXWindowCreatedNotification as CFString,
        kAXMainWindowChangedNotification as CFString,
        kAXFocusedWindowChangedNotification as CFString,
        kAXFocusedUIElementChangedNotification as CFString,
    ]
    private let scanInterval: TimeInterval = 0.5
    private let dedupeTTL: TimeInterval = 8
    private let debugEnabled = CommandLine.arguments.contains("--debug")

    func run() {
        guard AXIsProcessTrustedWithOptions(["AXTrustedCheckOptionPrompt": false] as CFDictionary) else {
            fputs("{\"type\":\"error\",\"message\":\"Accessibility permission not granted\"}\n", stderr)
            fflush(stderr)
            exit(2)
        }

        let runningApps = findBannerProcesses()
        guard !runningApps.isEmpty else {
            fputs("{\"type\":\"error\",\"message\":\"Notification center process not found\"}\n", stderr)
            fflush(stderr)
            exit(3)
        }

        let callback: AXObserverCallback = { _, _, _, refcon in
            guard let refcon else { return }
            let instance = Unmanaged<BannerObserver>.fromOpaque(refcon).takeUnretainedValue()
            instance.scheduleWindowScan()
        }
        let refcon = UnsafeMutableRawPointer(Unmanaged.passUnretained(self).toOpaque())

        var readyPids: [pid_t] = []
        for runningApp in runningApps {
            let pid = runningApp.processIdentifier
            let appElement = AXUIElementCreateApplication(pid)

            var createdObserver: AXObserver?
            let createResult = AXObserverCreate(pid, callback, &createdObserver)
            guard createResult == .success, let createdObserver else {
                logError("AXObserverCreate failed: \(createResult.rawValue)")
                continue
            }

            var added = 0
            for notification in observedNotifications {
                let addResult = AXObserverAddNotification(
                    createdObserver,
                    appElement,
                    notification,
                    refcon
                )
                if addResult == .success || addResult == .notificationAlreadyRegistered {
                    added += 1
                    debug("registered \(notification) for pid=\(pid)")
                } else {
                    debug("failed registering \(notification) for pid=\(pid), code=\(addResult.rawValue)")
                }
            }

            if added > 0 {
                observers[pid] = createdObserver
                let source = AXObserverGetRunLoopSource(createdObserver)
                CFRunLoopAddSource(CFRunLoopGetCurrent(), source, .defaultMode)
                readyPids.append(pid)
            }
        }

        guard !readyPids.isEmpty else {
            logError("No AX notifications could be registered")
            exit(4)
        }

        Timer.scheduledTimer(withTimeInterval: scanInterval, repeats: true) { [weak self] _ in
            self?.scanWindows()
        }
        scheduleWindowScan()
        printJSON(["type": "ready", "pids": readyPids])
        RunLoop.current.run()
    }

    private func findBannerProcesses() -> [NSRunningApplication] {
        let apps = NSWorkspace.shared.runningApplications
        var matches: [NSRunningApplication] = []
        for app in apps {
            let name = app.localizedName ?? ""
            if processNames.contains(where: { name.contains($0) }) {
                matches.append(app)
            }
        }
        return matches
    }

    private func scheduleWindowScan() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) { [weak self] in
            self?.scanWindows()
        }
    }

    private func scanWindows() {
        pruneDeliveredSnapshots()

        let runningApps = findBannerProcesses()
        guard !runningApps.isEmpty else { return }

        for runningApp in runningApps {
            let appElement = AXUIElementCreateApplication(runningApp.processIdentifier)
            var candidates: [AXUIElement] = []

            if let windows = copyAttribute(appElement, attribute: kAXWindowsAttribute as CFString) as? [AXUIElement] {
                candidates.append(contentsOf: windows)
            }
            if let children = copyAttribute(appElement, attribute: kAXChildrenAttribute as CFString) as? [AXUIElement] {
                candidates.append(contentsOf: children)
            }

            if debugEnabled {
                debug("scan pid=\(runningApp.processIdentifier), candidates=\(candidates.count)")
            }

            for window in candidates {
                guard let payload = parseBanner(window: window) else {
                    continue
                }
                let snapshot = BannerSnapshot(
                    app: payload["app"] as? String ?? "",
                    title: payload["title"] as? String ?? "",
                    body: payload["body"] as? String ?? ""
                )
                if deliveredSnapshots[snapshot] != nil {
                    continue
                }
                deliveredSnapshots[snapshot] = Date()
                if isPrimed {
                    printJSON(payload)
                } else {
                    debug("priming snapshot=\(snapshot)")
                }
            }
        }

        if !isPrimed {
            isPrimed = true
            debug("initial snapshot finished")
        }
    }

    private func parseBanner(window: AXUIElement) -> [String: Any]? {
        let rawTexts = extractTexts(from: window)
        let texts = rawTexts.filter { !ignoredLabels.contains($0) }
        guard !texts.isEmpty else { return nil }
        guard texts.count <= 8 else { return nil }

        if debugEnabled {
            debug("window texts=\(texts)")
        }

        return [
            "type": "notification",
            "app": texts.indices.contains(0) ? texts[0] : "",
            "title": texts.indices.contains(1) ? texts[1] : texts[0],
            "body": texts.indices.contains(2) ? texts[2] : "",
            "subtitle": "",
        ]
    }

    private func extractTexts(from element: AXUIElement, depth: Int = 0, seen: inout Set<String>) -> [String] {
        if depth > 6 {
            return []
        }

        var values: [String] = []
        for attribute in [kAXValueAttribute, kAXTitleAttribute, kAXDescriptionAttribute] {
            if let text = copyAttribute(element, attribute: attribute as CFString) as? String {
                let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
                if !trimmed.isEmpty && trimmed.count < 300 && !seen.contains(trimmed) {
                    seen.insert(trimmed)
                    values.append(trimmed)
                }
            }
        }

        if let children = copyAttribute(element, attribute: kAXChildrenAttribute as CFString) as? [AXUIElement] {
            for child in children {
                values.append(contentsOf: extractTexts(from: child, depth: depth + 1, seen: &seen))
            }
        }

        return values
    }

    private func extractTexts(from element: AXUIElement) -> [String] {
        var seen = Set<String>()
        return extractTexts(from: element, seen: &seen)
    }

    private func copyAttribute(_ element: AXUIElement, attribute: CFString) -> AnyObject? {
        var value: CFTypeRef?
        let error = AXUIElementCopyAttributeValue(element, attribute, &value)
        guard error == .success else {
            return nil
        }
        return value
    }

    private func printJSON(_ payload: [String: Any]) {
        guard JSONSerialization.isValidJSONObject(payload),
              let data = try? JSONSerialization.data(withJSONObject: payload),
              let text = String(data: data, encoding: .utf8) else {
            return
        }
        FileHandle.standardOutput.write((text + "\n").data(using: .utf8)!)
    }

    private func debug(_ message: String) {
        guard debugEnabled else { return }
        fputs("[ax_helper] \(message)\n", stderr)
        fflush(stderr)
    }

    private func logError(_ message: String) {
        fputs("{\"type\":\"error\",\"message\":\"\(message.replacingOccurrences(of: "\"", with: "\\\""))\"}\n", stderr)
        fflush(stderr)
    }

    private func pruneDeliveredSnapshots() {
        let now = Date()
        deliveredSnapshots = deliveredSnapshots.filter { now.timeIntervalSince($0.value) < dedupeTTL }
    }
}

BannerObserver().run()
