import AppKit
import WebKit

private struct Installation: Decodable {
    let vault: String
    let version: String?
    let steps: [String]?
}

private final class HamzabanApp: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    private var window: NSWindow?
    private var webView: WKWebView?
    private var vault: URL?
    private let fileManager = FileManager.default

    private var home: URL {
        fileManager.homeDirectoryForCurrentUser
    }

    private var payload: URL {
        Bundle.main.resourceURL!.appendingPathComponent("payload", isDirectory: true)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        if let installed = installedVault() {
            vault = installed
            offerUpgradeIfNeeded(for: installed)
        } else {
            showSetup()
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        for url in urls where url.scheme?.lowercased() == "hamzaban" {
            handleLink(url)
        }
    }

    private func installedVault() -> URL? {
        guard let installation = installation() else {
            return nil
        }
        let candidate = URL(fileURLWithPath: installation.vault, isDirectory: true)
        let engine = candidate.appendingPathComponent(".ai-memory", isDirectory: true)
        return fileManager.fileExists(atPath: engine.appendingPathComponent("control_room.py").path)
            ? candidate : nil
    }

    private func installation() -> Installation? {
        let record = home.appendingPathComponent(".hamzaban/install.json")
        guard let data = try? Data(contentsOf: record) else { return nil }
        return try? JSONDecoder().decode(Installation.self, from: data)
    }

    private func offerUpgradeIfNeeded(for installed: URL) {
        let bundledVersion = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String
        guard let existing = installation(), existing.version != bundledVersion else {
            showDashboard()
            return
        }
        let alert = NSAlert()
        alert.messageText = "به‌روزرسانی همزبان"
        alert.informativeText = "نسخهٔ جدید فایل‌های اجرایی همزبان را در Vault به‌روزرسانی می‌کند و از نسخهٔ قبلی پشتیبان می‌گیرد."
        alert.addButton(withTitle: "به‌روزرسانی")
        alert.addButton(withTitle: "بعداً")
        if alert.runModal() == .alertFirstButtonReturn {
            let selected = Set(existing.steps ?? [])
            let skipped = ["skills", "mcp", "hooks", "schedule"].filter { !selected.contains($0) }
            install(into: installed, skipping: skipped)
        } else {
            showDashboard()
        }
    }

    private func chooseVault() -> URL? {
        let suggestion = home.appendingPathComponent("Documents/Hamzaban-Vault", isDirectory: true)
        let alert = NSAlert()
        alert.messageText = "راه‌اندازی همزبان"
        alert.informativeText = "حافظهٔ مشترک در پوشهٔ Vault ذخیره می‌شود. می‌توانی پوشهٔ پیشنهادی را به کار ببری یا یک پوشهٔ دیگر انتخاب کنی."
        alert.addButton(withTitle: "پوشهٔ پیشنهادی")
        alert.addButton(withTitle: "انتخاب پوشه…")
        alert.addButton(withTitle: "بعداً")
        switch alert.runModal() {
        case .alertFirstButtonReturn:
            return suggestion
        case .alertSecondButtonReturn:
            let panel = NSOpenPanel()
            panel.canChooseFiles = false
            panel.canChooseDirectories = true
            panel.canCreateDirectories = true
            panel.allowsMultipleSelection = false
            panel.prompt = "انتخاب Vault"
            return panel.runModal() == .OK ? panel.url : nil
        default:
            return nil
        }
    }

    private func showSetup() {
        guard let selected = chooseVault() else {
            NSApp.terminate(nil)
            return
        }
        let choices: [(String, String)] = [
            ("هماهنگ‌کردن مهارت‌های عامل‌ها", "skills"),
            ("ثبت سرور حافظه در عامل‌ها", "mcp"),
            ("ثبت هوک‌های Claude Code", "hooks"),
            ("اجرای همگام‌سازی هر دقیقه", "schedule")
        ]
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8
        stack.frame = NSRect(x: 0, y: 0, width: 370, height: 130)
        let buttons = choices.map { choice -> NSButton in
            let button = NSButton(checkboxWithTitle: choice.0, target: nil, action: nil)
            button.state = .on
            stack.addArrangedSubview(button)
            return button
        }
        let alert = NSAlert()
        alert.messageText = "اتصال‌های همزبان"
        alert.informativeText = "پوشهٔ انتخابی: \(selected.path)\nهر تغییر در تنظیمات عامل‌ها پیش از اجرا پشتیبان‌گیری می‌شود. موارد دلخواه را انتخاب کن."
        alert.accessoryView = stack
        alert.addButton(withTitle: "نصب")
        alert.addButton(withTitle: "انصراف")
        guard alert.runModal() == .alertFirstButtonReturn else {
            NSApp.terminate(nil)
            return
        }
        let skipped = choices.enumerated().compactMap { index, choice in
            buttons[index].state == .on ? nil : choice.1
        }
        install(into: selected, skipping: skipped)
    }

    private func python() -> URL? {
        let paths = [
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/Library/Frameworks/Python.framework/Versions/Current/bin/python3",
            "/usr/bin/python3"
        ]
        for path in paths where fileManager.isExecutableFile(atPath: path) {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: path)
            process.arguments = ["-c", "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"]
            process.standardOutput = FileHandle.nullDevice
            process.standardError = FileHandle.nullDevice
            do {
                try process.run()
                process.waitUntilExit()
                if process.terminationStatus == 0 { return process.executableURL }
            } catch {
                continue
            }
        }
        return nil
    }

    private func run(_ python: URL, script: URL, arguments: [String], vault: URL? = nil) -> (Int32, String) {
        let process = Process()
        process.executableURL = python
        process.arguments = [script.path] + arguments
        process.currentDirectoryURL = script.deletingLastPathComponent()
        var environment = ProcessInfo.processInfo.environment
        if let vault = vault { environment["INFINITE_CTX_VAULT"] = vault.path }
        process.environment = environment
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        do {
            try process.run()
            let output = pipe.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            return (process.terminationStatus, String(data: output, encoding: .utf8) ?? "")
        } catch {
            return (1, error.localizedDescription)
        }
    }

    private func install(into selected: URL, skipping: [String]) {
        guard let interpreter = python() else {
            message("Python پیدا نشد", "همزبان به Python 3.10 یا جدیدتر نیاز دارد. آن را نصب کن و دوباره اپ را باز کن.")
            NSApp.terminate(nil)
            return
        }
        let progress = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 430, height: 110),
                                styleMask: [.titled], backing: .buffered, defer: false)
        progress.title = "نصب همزبان"
        progress.center()
        let label = NSTextField(labelWithString: "در حال آماده‌کردن Vault و اتصال‌های انتخابی…")
        label.frame = NSRect(x: 25, y: 58, width: 380, height: 24)
        let spinner = NSProgressIndicator(frame: NSRect(x: 25, y: 20, width: 380, height: 20))
        spinner.style = .bar
        spinner.isIndeterminate = true
        spinner.startAnimation(nil)
        progress.contentView?.addSubview(label)
        progress.contentView?.addSubview(spinner)
        progress.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        let installer = payload.appendingPathComponent("install.py")
        var arguments = ["--vault", selected.path, "--yes", "--no-open"]
        arguments += ["--skip", (skipping + ["shortcuts", "open"]).joined(separator: ",")]
        DispatchQueue.global(qos: .userInitiated).async {
            let result = self.run(interpreter, script: installer, arguments: arguments)
            DispatchQueue.main.async {
                progress.close()
                if result.0 == 0, let installed = self.installedVault() {
                    self.vault = installed
                    self.showDashboard()
                    if result.1.contains("failed:") {
                        self.message("نصب با هشدار پایان یافت", result.1.suffix(1800).description)
                    }
                } else {
                    self.message("نصب کامل نشد", String(result.1.suffix(1800)))
                    NSApp.terminate(nil)
                }
            }
        }
    }

    private func showDashboard() {
        guard let vault = vault else { return }
        let page = vault.appendingPathComponent("00-Dashboard/control-room.html")
        guard fileManager.fileExists(atPath: page.path) else {
            message("صفحهٔ همزبان پیدا نشد", page.path)
            return
        }
        let view = WKWebView(frame: NSRect(x: 0, y: 0, width: 1280, height: 880))
        view.navigationDelegate = self
        let frame = NSRect(x: 0, y: 0, width: 1280, height: 880)
        let window = NSWindow(contentRect: frame,
                              styleMask: [.titled, .closable, .miniaturizable, .resizable],
                              backing: .buffered, defer: false)
        window.title = "Hamzaban همزبان"
        window.center()
        window.contentView = view
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        view.loadFileURL(page, allowingReadAccessTo: page.deletingLastPathComponent())
        self.window = window
        self.webView = view
    }

    private func handleLink(_ url: URL) {
        guard let vault = vault, let interpreter = python() else {
            message("همزبان آماده نیست", "نصب همزبان یا Python 3.10 را بررسی کن.")
            return
        }
        let script = vault.appendingPathComponent(".ai-memory/app_links.py")
        DispatchQueue.global(qos: .userInitiated).async {
            let result = self.run(interpreter, script: script, arguments: [url.absoluteString], vault: vault)
            if result.0 != 0 {
                DispatchQueue.main.async {
                    self.message("لینک باز نشد", String(result.1.suffix(600)))
                }
            }
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.cancel)
            return
        }
        if url.scheme?.lowercased() == "hamzaban" {
            decisionHandler(.cancel)
            handleLink(url)
        } else if url.isFileURL || url.scheme == "about" {
            decisionHandler(.allow)
        } else {
            decisionHandler(.cancel)
            NSWorkspace.shared.open(url)
        }
    }

    private func message(_ title: String, _ detail: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = detail
        alert.runModal()
    }
}

let app = NSApplication.shared
let delegate = HamzabanApp()
app.delegate = delegate
app.run()
