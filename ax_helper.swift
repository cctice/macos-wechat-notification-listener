import AppKit
import ApplicationServices
import Foundation

final class BannerObserver {
    private var observer: AXObserver?
    private var observedWindows = Set<AXUIElement>()
    private let processNames = ["UserNotificationCenter", "NotificationCenter"]
    private let ignoredLabels: Set<String> = [
        "Close", "关闭", "Options", "选项", "Reply", "回复", "View", "查看",
    ]

    func run() {
        guard AXIsProcessTrustedWithOptions(["AXTrustedCheckOptionPrompt": false] as CFDictionary) else {
            fputs("{\"type\":\"error\",\"message\":\"Accessibility permission not granted\"}\n", stderr)
            fflush(stderr)
            exit(2)
        }

        guard let runningApp = findBannerProcess() else {
            fputs("{\"type\":\"error\",\"message\":\"Notification center process not found\"}\n", stderr)
            fflush(stderr)
            exit(3)
        }

        let pid = runningApp.processIdentifier
        let appElement = AXUIElementCreateApplication(pid)

        var createdObserver: AXObserver?
        let callback: AXObserverCallback = { _, _, _, refcon in
            guard let refcon else { return }
            let instance = Unmanaged<BannerObserver>.fromOpaque(refcon).takeUnretainedValue()
            instance.scheduleWindowScan()
        }

        let createResult = AXObserverCreate(pid, callback, &createdObserver)
        guard createResult == .success, let createdObserver else {
            fputs("{\"type\":\"error\",\"message\":\"AXObserverCreate failed: \(createResult.rawValue)\"}\n", stderr)
            fflush(stderr)
            exit(4)
        }

        self.observer = createdObserver
        let refcon = UnsafeMutableRawPointer(Unmanaged.passUnretained(self).toOpaque())
        let addResult = AXObserverAddNotification(
            createdObserver,
            appElement,
            kAXWindowCreatedNotification as CFString,
            refcon
        )

        guard addResult == .success else {
            fputs("{\"type\":\"error\",\"message\":\"AXObserverAddNotification failed: \(addResult.rawValue)\"}\n", stderr)
            fflush(stderr)
            exit(5)
        }

        let source = AXObserverGetRunLoopSource(createdObserver)
        CFRunLoopAddSource(CFRunLoopGetCurrent(), source, .defaultMode)
        scheduleWindowScan()
        printJSON(["type": "ready", "pid": pid])
        RunLoop.current.run()
    }

    private func findBannerProcess() -> NSRunningApplication? {
        let apps = NSWorkspace.shared.runningApplications
        for expectedName in processNames {
            if let match = apps.first(where: { ($0.localizedName ?? "").contains(expectedName) }) {
                return match
            }
        }
        return nil
    }

    private func scheduleWindowScan() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) { [weak self] in
            self?.scanWindows()
        }
    }

    private func scanWindows() {
        guard let runningApp = findBannerProcess() else { return }
        let appElement = AXUIElementCreateApplication(runningApp.processIdentifier)
        guard let windows = copyAttribute(appElement, attribute: kAXWindowsAttribute as CFString) as? [AXUIElement] else {
            return
        }

        for window in windows {
            if observedWindows.contains(window) {
                continue
            }
            observedWindows.insert(window)
            guard let payload = parseBanner(window: window) else {
                continue
            }
            printJSON(payload)
        }
    }

    private func parseBanner(window: AXUIElement) -> [String: Any]? {
        let rawTexts = extractTexts(from: window)
        let texts = rawTexts.filter { !ignoredLabels.contains($0) }
        guard !texts.isEmpty else { return nil }

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
}

BannerObserver().run()
