import AppKit
import ApplicationServices
import Foundation

final class BannerObserver {
    private struct BannerSnapshot: Hashable {
        let app: String
        let title: String
        let body: String
    }

    private struct CandidateDebugSnapshot: Hashable {
        let role: String
        let subrole: String
        let roleDescription: String
        let rawTexts: [String]
        let filteredTexts: [String]
        let reason: String
    }

    private var observers: [pid_t: AXObserver] = [:]
    private var deliveredSnapshots: [BannerSnapshot: Date] = [:]
    private var recentRejects: [CandidateDebugSnapshot: Date] = [:]
    private var loggedAttributeNamesForPID = Set<pid_t>()
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
    private let maxTextCount = 20
    private let maxTextLength = 300
    private let maxTraversalDepth = 8
    private let traversableAttributes = [
        kAXChildrenAttribute as CFString,
        kAXVisibleChildrenAttribute as CFString,
        kAXContentsAttribute as CFString,
        kAXWindowsAttribute as CFString,
        kAXRowsAttribute as CFString,
        kAXColumnsAttribute as CFString,
        kAXToolbarButtonAttribute as CFString,
        kAXServesAsTitleForUIElementsAttribute as CFString,
    ]
    private let ignoredRoles = Set([
        kAXMenuBarRole as String,
        kAXMenuRole as String,
        kAXMenuItemRole as String,
        kAXMenuBarItemRole as String,
    ])

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
        pruneRejects()

        let runningApps = findBannerProcesses()
        guard !runningApps.isEmpty else { return }

        for runningApp in runningApps {
            let appElement = AXUIElementCreateApplication(runningApp.processIdentifier)
            if debugEnabled && !loggedAttributeNamesForPID.contains(runningApp.processIdentifier) {
                loggedAttributeNamesForPID.insert(runningApp.processIdentifier)
                debug("app pid=\(runningApp.processIdentifier) attrs=\(copyAttributeNames(appElement))")
            }

            var visited = Set<Int>()
            let candidates = collectCandidates(
                from: appElement,
                pid: runningApp.processIdentifier,
                depth: 0,
                path: "AXApplication",
                visited: &visited
            )

            if debugEnabled {
                debug("scan pid=\(runningApp.processIdentifier), candidates=\(candidates.count)")
            }

            for candidate in candidates {
                let payload = candidate.payload
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

    private struct CandidateResult {
        let payload: [String: Any]
        let path: String
    }

    private func collectCandidates(
        from element: AXUIElement,
        pid: pid_t,
        depth: Int,
        path: String,
        visited: inout Set<Int>
    ) -> [CandidateResult] {
        if depth > maxTraversalDepth {
            return []
        }

        let elementHash = Int(CFHash(element))
        if visited.contains(elementHash) {
            return []
        }
        visited.insert(elementHash)

        var results: [CandidateResult] = []
        if let payload = parseBanner(element: element, pid: pid, path: path) {
            results.append(CandidateResult(payload: payload, path: path))
        }

        for attribute in traversableAttributes {
            guard let attrValue = copyAttribute(element, attribute: attribute) else {
                continue
            }
            let nextPathBase = "\(path).\(attribute)"
            let childElements = unwrapAXElements(attrValue)
            for (idx, child) in childElements.enumerated() {
                let childPath = childElements.count == 1 ? nextPathBase : "\(nextPathBase)[\(idx)]"
                results.append(
                    contentsOf: collectCandidates(
                        from: child,
                        pid: pid,
                        depth: depth + 1,
                        path: childPath,
                        visited: &visited
                    )
                )
            }
        }
        return results
    }

    private func parseBanner(element: AXUIElement, pid: pid_t, path: String) -> [String: Any]? {
        let role = (copyAttribute(element, attribute: kAXRoleAttribute as CFString) as? String) ?? ""
        let subrole = (copyAttribute(element, attribute: kAXSubroleAttribute as CFString) as? String) ?? ""
        let roleDescription = (copyAttribute(element, attribute: kAXRoleDescriptionAttribute as CFString) as? String) ?? ""
        if ignoredRoles.contains(role) {
            emitReject(
                pid: pid,
                role: role,
                subrole: subrole,
                roleDescription: roleDescription,
                rawTexts: [],
                filteredTexts: [],
                reason: "ignored_role",
                path: path
            )
            return nil
        }

        let rawTexts = extractTexts(from: element)
        if rawTexts.isEmpty {
            emitReject(
                pid: pid,
                role: role,
                subrole: subrole,
                roleDescription: roleDescription,
                rawTexts: rawTexts,
                filteredTexts: [],
                reason: "no_raw_texts",
                path: path
            )
            return nil
        }

        let texts = rawTexts.filter { !ignoredLabels.contains($0) }
        if texts.isEmpty {
            emitReject(
                pid: pid,
                role: role,
                subrole: subrole,
                roleDescription: roleDescription,
                rawTexts: rawTexts,
                filteredTexts: texts,
                reason: "all_texts_filtered",
                path: path
            )
            return nil
        }
        if texts.count > maxTextCount {
            emitReject(
                pid: pid,
                role: role,
                subrole: subrole,
                roleDescription: roleDescription,
                rawTexts: rawTexts,
                filteredTexts: texts,
                reason: "too_many_texts",
                path: path
            )
            return nil
        }

        if debugEnabled {
            debug("window accepted pid=\(pid) path=\(path) role=\(role) subrole=\(subrole) desc=\(roleDescription) texts=\(texts)")
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
        if depth > 10 {
            return []
        }

        var values: [String] = []
        for attribute in [kAXValueAttribute, kAXTitleAttribute, kAXDescriptionAttribute] {
            if let text = copyAttribute(element, attribute: attribute as CFString) as? String {
                let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
                if !trimmed.isEmpty && trimmed.count < maxTextLength && !seen.contains(trimmed) {
                    seen.insert(trimmed)
                    values.append(trimmed)
                }
            }
        }

        if let help = copyAttribute(element, attribute: kAXHelpAttribute as CFString) as? String {
            let trimmed = help.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty && trimmed.count < maxTextLength && !seen.contains(trimmed) {
                seen.insert(trimmed)
                values.append(trimmed)
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

    private func copyAttributeNames(_ element: AXUIElement) -> [String] {
        var namesCF: CFArray?
        let error = AXUIElementCopyAttributeNames(element, &namesCF)
        guard error == .success, let namesCF else {
            return []
        }
        return namesCF as? [String] ?? []
    }

    private func unwrapAXElements(_ value: AnyObject) -> [AXUIElement] {
        if CFGetTypeID(value) == AXUIElementGetTypeID() {
            return [unsafeBitCast(value, to: AXUIElement.self)]
        }
        if let array = value as? [AnyObject] {
            return array.compactMap { item in
                guard CFGetTypeID(item) == AXUIElementGetTypeID() else {
                    return nil
                }
                return unsafeBitCast(item, to: AXUIElement.self)
            }
        }
        return []
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

    private func pruneRejects() {
        let now = Date()
        recentRejects = recentRejects.filter { now.timeIntervalSince($0.value) < dedupeTTL }
    }

    private func emitReject(
        pid: pid_t,
        role: String,
        subrole: String,
        roleDescription: String,
        rawTexts: [String],
        filteredTexts: [String],
        reason: String,
        path: String
    ) {
        guard debugEnabled else { return }
        let snapshot = CandidateDebugSnapshot(
            role: role,
            subrole: subrole,
            roleDescription: roleDescription,
            rawTexts: rawTexts,
            filteredTexts: filteredTexts,
            reason: "\(reason)@\(path)"
        )
        guard recentRejects[snapshot] == nil else { return }
        recentRejects[snapshot] = Date()
        debug(
            "window rejected pid=\(pid) path=\(path) reason=\(reason) role=\(role) subrole=\(subrole) desc=\(roleDescription) raw=\(rawTexts) filtered=\(filteredTexts)"
        )
    }
}

BannerObserver().run()
