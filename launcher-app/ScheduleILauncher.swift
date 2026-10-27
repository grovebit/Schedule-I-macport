// Schedule I Launcher: plays, sets up, updates and configures the native Mac port of Schedule I
// (github.com/grovebit/Schedule-I-macport).
//
// - Locations: the Windows game files, the port's folder and the built game app; checked on start, changeable in
//   Settings. Anything missing turns the main window into a setup checklist.
// - SteamCMD: downloaded from Valve into ~/Library/Application Support/Schedule I Launcher (no Homebrew, and no
//   quarantine flag, so macOS doesn't flag Valve's crash reporter on every run). Update checks are anonymous; the
//   download signs in as the user in a pseudo terminal, so password and Steam Guard prompts can be answered here.
// - Settings: the port's options in the game's preferences (com.TVGS.ScheduleI) and its build option.
import AppKit
import SwiftUI

let appID = "3164500"
let betaBranch = "alternate"
let portRepository = "https://github.com/grovebit/Schedule-I-macport"
let steamCMDDownload = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_osx.tar.gz"
let gameDomain = "com.TVGS.ScheduleI"
let steamBundleID = "com.valvesoftware.steam"
let home = FileManager.default.homeDirectoryForCurrentUser.path
let support = home + "/Library/Application Support/Schedule I Launcher"

func firstMatch(_ pattern: String, in text: String) -> String? {
    guard let re = try? NSRegularExpression(pattern: pattern, options: [.dotMatchesLineSeparators]),
          let m = re.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)), m.numberOfRanges > 1,
          let r = Range(m.range(at: 1), in: text) else { return nil }
    return String(text[r])
}

func exists(_ path: String) -> Bool { FileManager.default.fileExists(atPath: path) }

func shortPath(_ path: String) -> String {
    path.hasPrefix(home) ? "~" + path.dropFirst(home.count) : path
}

// MARK: - Running tools

// Its mutable state (pending) is only touched on the main queue.
final class ToolRun: @unchecked Sendable {
    let process = Process()
    private let input = Pipe()
    private let output = Pipe()
    private var pending = ""

    // onText gets every chunk as it arrives (prompts don't end with a newline), onLine every complete line
    init(_ exe: String, _ args: [String], terminal: Bool = false, directory: String? = nil) {
        if terminal {  // SteamCMD asks for the password on a terminal: give it one with script(1)
            process.executableURL = URL(fileURLWithPath: "/usr/bin/script")
            process.arguments = ["-q", "/dev/null", exe] + args
        } else {
            process.executableURL = URL(fileURLWithPath: exe)
            process.arguments = args
        }
        if let directory { process.currentDirectoryURL = URL(fileURLWithPath: directory) }
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
        process.environment = env
        process.standardInput = input
        process.standardOutput = output
        process.standardError = output
    }

    func start(onText: @escaping (String) -> Void = { _ in }, onLine: @escaping (String) -> Void = { _ in }) async -> Int32 {
        output.fileHandleForReading.readabilityHandler = { [weak self] h in
            let data = h.availableData
            guard let self, !data.isEmpty, let s = String(data: data, encoding: .utf8) else { return }
            DispatchQueue.main.async {
                onText(s)
                self.pending += s
                let parts = self.pending.components(separatedBy: CharacterSet(charactersIn: "\r\n"))
                self.pending = parts.last ?? ""
                parts.dropLast().filter { !$0.isEmpty }.forEach(onLine)
            }
        }
        return await withCheckedContinuation { cont in
            process.terminationHandler = { p in
                self.output.fileHandleForReading.readabilityHandler = nil
                cont.resume(returning: p.terminationStatus)
            }
            do { try process.run() } catch {
                let name = process.executableURL?.lastPathComponent ?? ""
                DispatchQueue.main.async { onLine("Couldn't start \(name): \(error.localizedDescription)") }
                cont.resume(returning: -1)
            }
        }
    }

    func send(_ text: String) {
        input.fileHandleForWriting.write((text + "\n").data(using: .utf8)!)
    }

    func stop() {
        if process.isRunning { process.terminate() }
    }
}

// MARK: - Model

enum UpdateState: Equatable {
    case unknown, checking, upToDate, available, offline
}

@MainActor
final class Launcher: ObservableObject {
    // locations
    @AppStorage("gamePath") var gamePath = home + "/Games/ScheduleI-Windows"
    // the repository the launcher was built from (launcher-app/build.sh), else a new clone in ~/Games
    @AppStorage("portPath") var portPath = Bundle.main.object(forInfoDictionaryKey: "MacPortRepository") as? String
        ?? home + "/Games/Schedule-I-macport"
    @AppStorage("appPath") var appPath = "/Applications/Schedule I.app"
    @AppStorage("steamUser") var steamUser = ""
    // status
    @Published var version = ""
    @Published var localBuild: String?
    @Published var latestBuild: String?
    @Published var update = UpdateState.unknown
    @Published var needsRebuild = false
    @Published var gameRunning = false
    @Published var steamStarting = false
    // current task
    @Published var task: String?
    @Published var step = ""
    @Published var progress: Double?
    @Published var log = ""
    @Published var prompt: String?  // SteamCMD waits for this (password, Steam Guard code)
    @Published var askAccount = false  // the download needs the Steam account name
    @Published var notice: String?
    // settings
    @Published var renderScale = 1.0 { didSet { if loaded { savePrefs() } } }
    @Published var lighterEffects = true { didSet { if loaded { savePrefs() } } }
    @Published var frameLimit = -1 { didSet { if loaded { savePrefs() } } }
    @Published var matchRegularVersion = false

    private var loaded = false
    private var running: ToolRun?
    private var timer: Timer?
    private var afterAccount: (() -> Void)?

    init() {
        if Snapshot.directory == nil, steamUser.isEmpty, let vdf = try? String(contentsOfFile: home + "/Library/Application Support/Steam/config/loginusers.vdf", encoding: .utf8) {
            steamUser = firstMatch(#""AccountName"\s+"([^"]+)""#, in: vdf) ?? ""
        }
        refresh()
        loadPrefs()
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                let running = Launcher.isRunning(gameDomain)
                if running != self.gameRunning {
                    self.gameRunning = running
                    if !running { self.loadPrefs() }  // the game saves its preferences when it quits
                }
            }
        }
        if gameFound && portFound && Snapshot.directory == nil { Task { await checkForUpdate(quiet: true) } }
    }

    // MARK: locations

    var gameFound: Bool { exists(gamePath + "/Schedule I_Data") }
    var portFound: Bool { exists(portPath + "/build.sh") }
    var appFound: Bool { exists(appPath + "/Contents/Info.plist") }

    // Options newer porting tools have (grovebit/Schedule-I-macport pull requests #1 and #2)
    private func portSource(_ rel: String) -> String { (try? String(contentsOfFile: portPath + "/" + rel, encoding: .utf8)) ?? "" }
    var portHasVersionMatch: Bool { portSource("tools/py/build.py").contains("--main-branch-version") }
    var portHasFasterEffects: Bool { portSource("tools/csharp/MacPortRuntime/Runtime.cs").contains("MacPortFeatures") }
    var ready: Bool { gameFound && portFound && appFound }
    @Published var toolsInstalled = true

    // Where new downloads go: the port and the game files side by side
    var installFolder: String { (gamePath as NSString).deletingLastPathComponent }

    func chooseInstallFolder() {
        let panel = NSOpenPanel()
        panel.message = "Choose where to install Schedule I. It needs about 20 GB."
        panel.prompt = "Install Here"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.directoryURL = URL(fileURLWithPath: installFolder)
        guard panel.runModal() == .OK, let url = panel.url else { return }
        if !portFound { portPath = url.path + "/Schedule-I-macport" }
        if !gameFound { gamePath = url.path + "/ScheduleI-Windows" }
        refresh()
    }

    // Free space on the install folder's disk (the folder may not exist yet)
    var freeSpace: Int64? {
        var path = installFolder
        while !exists(path) && path != "/" { path = (path as NSString).deletingLastPathComponent }
        let values = try? URL(fileURLWithPath: path).resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
        return values?.volumeAvailableCapacityForImportantUsage
    }
    static let neededSpace: Int64 = 20_000_000_000

    // Apple's command line developer tools (git, compilers): needed to get and build the port. xcode-select -p
    // doesn't trigger the system's install prompt, unlike running git or xcrun without them.
    func checkTools() async {
        var path = ""
        let status = await ToolRun("/usr/bin/xcode-select", ["-p"]).start(onLine: { path = $0 })
        toolsInstalled = status == 0 && exists(path)
    }

    func installTools() {
        Task {
            _ = await ToolRun("/usr/bin/xcode-select", ["--install"]).start()
            notice = "Follow Apple’s installer. When it’s done, come back here."
        }
    }

    func choose(_ title: String, current: String, files: Bool = false, set: (String) -> Void) {
        let panel = NSOpenPanel()
        panel.message = title
        panel.canChooseDirectories = !files
        panel.canChooseFiles = files
        panel.allowedContentTypes = files ? [.application] : []
        panel.treatsFilePackagesAsDirectories = false
        panel.directoryURL = URL(fileURLWithPath: (current as NSString).deletingLastPathComponent)
        if panel.runModal() == .OK, let url = panel.url {
            set(url.path)
            refresh()
        }
    }

    // MARK: status

    static func isRunning(_ bundleID: String) -> Bool {
        !NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).isEmpty
    }

    func refresh() {
        objectWillChange.send()
        let info = NSDictionary(contentsOfFile: appPath + "/Contents/Info.plist")
        version = info?["CFBundleShortVersionString"] as? String ?? ""
        let args = (info?["MacPortUpdate"] as? [String: Any])?["BuildArguments"] as? [String] ?? []
        matchRegularVersion = args.contains("--main-branch-version")
        localBuild = (try? String(contentsOfFile: gamePath + "/steamapps/appmanifest_\(appID).acf", encoding: .utf8))
            .flatMap { firstMatch(#""buildid"\s+"(\d+)""#, in: $0) }
        if let l = localBuild, let r = latestBuild { update = l == r ? .upToDate : .available }
        gameRunning = Launcher.isRunning(gameDomain)
        if !ready { Task { await checkTools() } }
        guard appFound else { needsRebuild = false; return }
        Task {
            let status = await ToolRun(appPath + "/Contents/MacOS/Schedule I", ["--check"]).start()
            needsRebuild = status == 1
        }
    }

    var statusLine: String {
        if task != nil { return step.isEmpty ? "Working…" : step }
        if steamStarting { return "Starting Steam…" }
        if gameRunning { return "The game is running" }
        if !ready { return "Let’s get you set up" }
        if needsRebuild { return "One more step to finish the update" }
        switch update {
        case .available: return "A game update is available"
        case .checking: return "Checking for updates…"
        default: return "Ready to play"
        }
    }

    // MARK: settings (the game's preferences; it writes them when it quits, so only while it's closed)

    private var prefs: UserDefaults { UserDefaults(suiteName: gameDomain)! }

    func loadPrefs() {
        loaded = false
        let p = prefs
        renderScale = p.object(forKey: "MacPortRenderScale") == nil ? 1.0 : p.double(forKey: "MacPortRenderScale")
        lighterEffects = (p.object(forKey: "MacPortFeatures") as? String) != ""
        frameLimit = p.object(forKey: "TargetFPS") == nil ? 90 : p.integer(forKey: "TargetFPS")
        loaded = true
    }

    private func savePrefs() {
        guard !gameRunning else { return }
        let p = prefs
        if renderScale >= 0.999 { p.removeObject(forKey: "MacPortRenderScale") } else { p.set(Float(renderScale), forKey: "MacPortRenderScale") }
        if lighterEffects { p.removeObject(forKey: "MacPortFeatures") } else { p.set("", forKey: "MacPortFeatures") }
        p.set(frameLimit, forKey: "TargetFPS")
    }

    func recommended() {
        renderScale = 0.75
        lighterEffects = true
        frameLimit = -1
    }

    // MARK: actions

    func play() {
        Task {
            if !Launcher.isRunning(steamBundleID) {
                steamStarting = true
                if let steam = NSWorkspace.shared.urlForApplication(withBundleIdentifier: steamBundleID) {
                    NSWorkspace.shared.open(steam)
                }
                for _ in 0..<30 where !Launcher.isRunning(steamBundleID) {
                    try? await Task.sleep(for: .seconds(1))
                }
                try? await Task.sleep(for: .seconds(8))  // let it sign in
                steamStarting = false
            }
            NSWorkspace.shared.open(URL(fileURLWithPath: appPath))
        }
    }

    // SteamCMD from Valve, in the launcher's own folder
    private var steamCMD: String { support + "/steamcmd/steamcmd.sh" }

    private func ensureSteamCMD() async -> Bool {
        if exists(steamCMD) { return true }
        step = "Getting ready to talk to Steam…"
        try? FileManager.default.createDirectory(atPath: support + "/steamcmd", withIntermediateDirectories: true)
        let status = await ToolRun("/bin/sh", ["-c", "curl -fsSL '\(steamCMDDownload)' | tar -xz -C '\(support)/steamcmd'"])
            .start(onLine: { [self] in append($0) })
        return status == 0 && exists(steamCMD)
    }

    func checkForUpdate(quiet: Bool = false) async {
        guard task == nil, gameFound else { return }
        if !quiet { begin("Checking for updates") }
        update = .checking
        guard await ensureSteamCMD() else { update = .offline; if !quiet { end("Couldn’t reach Steam. Check your internet connection.") }; return }
        var text = ""
        let r = ToolRun(steamCMD, ["+login", "anonymous", "+app_info_update", "1", "+app_info_print", appID, "+quit"])
        if !quiet { running = r }
        _ = await r.start(onText: { text += $0 }, onLine: { [self] l in if !quiet { append(l) } })
        let branches = text.components(separatedBy: "\"branches\"").last ?? ""
        latestBuild = firstMatch(#""\#(betaBranch)"\s*\{\s*"buildid"\s*"(\d+)""#, in: branches)
        if latestBuild == nil { update = .offline }
        refresh()
        if !quiet {
            end(update == .offline ? "Couldn’t reach Steam. Check your internet connection." :
                update == .available ? nil : "You’re on the latest version.")
        }
    }

    // Downloads (or repairs) the Windows game files, then rebuilds the app
    func downloadGame() {
        guard !gameRunning else { notice = "Close the game first."; return }
        guard !steamUser.isEmpty else { afterAccount = { [self] in downloadGame() }; askAccount = true; return }
        Task {
            begin(gameFound ? "Updating the game" : "Downloading the game")
            guard await ensureSteamCMD() else { end("Couldn’t reach Steam. Check your internet connection."); return }
            step = "Signing in to Steam…"
            let r = ToolRun(steamCMD, ["+@sSteamCmdForcePlatformType", "windows", "+force_install_dir", gamePath,
                                       "+login", steamUser, "+app_update", appID, "-beta", betaBranch, "validate", "+quit"],
                            terminal: true)
            running = r
            var tail = ""
            let status = await r.start(onText: { [self] s in
                tail = String((tail + s).suffix(300))
                let lower = tail.lowercased().trimmingCharacters(in: .whitespaces)
                if lower.hasSuffix("password:") {
                    prompt = "Enter your Steam password."
                } else if lower.hasSuffix("steam guard code:") || lower.hasSuffix("two-factor code:") {
                    prompt = "Enter the Steam Guard code from the Steam app or your email."
                } else if lower.contains("confirm the login") {
                    step = "Approve the sign-in in the Steam app on your phone…"
                }
            }, onLine: { [self] l in
                append(l)
                if let p = firstMatch(#"progress: ([0-9.]+)"#, in: l), let v = Double(p) {
                    progress = v / 100
                    step = l.contains("verifying") ? "Checking the game files…" : "Downloading the game…"
                }
            })
            prompt = nil
            let ok = status == 0 && log.contains("fully installed")
            refresh()
            guard ok else {
                end(log.contains("Invalid Password") ? "Steam didn’t accept that password. Please try again." :
                    "The download stopped. To see why, choose … → Show Details.")
                return
            }
            await build(title: "Setting up the game for your Mac")
        }
    }

    // The porting tool (grovebit's Schedule-I-macport), fetched by the setup step when it's missing
    private func getPortingTool() async -> Bool {
        step = "Getting the porting tool…"
        try? FileManager.default.createDirectory(atPath: (portPath as NSString).deletingLastPathComponent, withIntermediateDirectories: true)
        let status = await ToolRun("/usr/bin/git", ["clone", portRepository, portPath]).start(onLine: { [self] in append($0) })
        refresh()
        return status == 0 && portFound
    }

    func setMatchRegularVersion(_ on: Bool) {
        guard !gameRunning else { notice = "Close the game first."; return }
        matchRegularVersion = on
        Task { await build(title: "Updating the multiplayer setting") }
    }

    func build(title: String) async {
        if task == nil { begin(title) } else { task = title }
        progress = nil
        if !portFound {
            guard await getPortingTool() else {
                end("Couldn’t download the porting tool. To see why, choose … → Show Details.")
                return
            }
        }
        step = "Starting…"
        var args = ["--game", gamePath, "--out", appPath]
        if matchRegularVersion && portHasVersionMatch { args.append("--main-branch-version") }
        let r = ToolRun(portPath + "/build.sh", args, directory: portPath)
        running = r
        let status = await r.start(onLine: { [self] l in
            append(l)
            if l.hasPrefix("==> ") { step = Launcher.stepName(String(l.dropFirst(4))) }
            if let a = firstMatch(#"([0-9.]+) of [0-9.]+ GB"#, in: l).flatMap(Double.init),
               let b = firstMatch(#"of ([0-9.]+) GB"#, in: l).flatMap(Double.init), b > 0 {
                progress = a / b
            }
        })
        refresh()
        end(status == 0 ? "All set. Have fun!" : "Setup didn’t finish. To see why, choose … → Show Details.")
    }

    static func stepName(_ s: String) -> String {
        switch s {
        case "game": return "Checking the game files…"
        case "deps": return "Downloading what’s needed (only the first time, about 6 GB)…"
        case "app": return "Almost done…"
        case "icon": return "Almost done…"
        default: return "Adapting the game for your Mac…"
        }
    }

    func setAccount(_ name: String) {
        steamUser = name.trimmingCharacters(in: .whitespaces)
        askAccount = false
        afterAccount?()
        afterAccount = nil
    }

    func answer(_ text: String) {
        running?.send(text)
        prompt = nil
        step = "Signing in to Steam…"
    }

    func cancel() {
        running?.stop()
    }

    private func begin(_ title: String) {
        task = title
        step = ""
        progress = nil
        log = ""
        notice = nil
    }

    private func end(_ text: String?) {
        task = nil
        step = ""
        progress = nil
        running = nil
        notice = text
    }

    private func append(_ line: String) {
        log += line + "\n"
        if log.count > 200_000 { log = String(log.suffix(150_000)) }
    }
}

// MARK: - Main window

struct MainView: View {
    @EnvironmentObject var model: Launcher
    @Environment(\.openSettings) private var openSettings
    @State private var showDetails = false
    @State private var secret = ""
    @State private var account = ""

    var body: some View {
        VStack(spacing: 0) {
            header
            Group {
                if model.task != nil { working } else if model.ready { readyArea } else { setup }
            }
            .padding(.horizontal, 24)
            .padding(.bottom, 20)
        }
        .frame(width: 400)
        .background(.background)
        .sheet(isPresented: Binding(get: { model.prompt != nil }, set: { if !$0 { model.prompt = nil } })) { promptSheet }
        .sheet(isPresented: $model.askAccount) { accountSheet }
        .sheet(isPresented: $showDetails) { detailsSheet }
        .task { if let dir = Snapshot.directory { await Snapshot.take(into: dir) { openSettings() } } }
    }

    var header: some View {
        VStack(spacing: 10) {
            Image(nsImage: model.appFound ? NSWorkspace.shared.icon(forFile: model.appPath) : NSApp.applicationIconImage)
                .resizable().frame(width: 96, height: 96)
                .shadow(color: .black.opacity(0.25), radius: 8, y: 4)
            Text("Schedule I").font(.system(size: 26, weight: .bold, design: .rounded))
            HStack(spacing: 6) {
                if model.update == .available && model.task == nil {
                    Circle().fill(.orange).frame(width: 7, height: 7)
                }
                Text(model.statusLine)
                if !model.version.isEmpty && model.task == nil && model.ready { Text("·"); Text(model.version) }
            }
            .font(.callout).foregroundStyle(.secondary)
        }
        .padding(.top, 28)
        .padding(.bottom, 22)
        .frame(maxWidth: .infinity)
        .overlay(alignment: .topTrailing) { toolbar.padding(10) }
    }

    var toolbar: some View {
        HStack(spacing: 2) {
            Menu {
                Button("Check for Updates") { Task { await model.checkForUpdate() } }
                    .disabled(!model.gameFound)
                Button("Fix Game Files") { model.downloadGame() }.disabled(!model.gameFound || model.gameRunning)
                Button("Set Up Again") { Task { await model.build(title: "Setting up the game for your Mac") } }
                    .disabled(!model.gameFound || !model.portFound || model.gameRunning)
                Divider()
                Button("Show Details") { showDetails = true }
            } label: {
                Image(systemName: "ellipsis.circle")
            }
            .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
            .disabled(model.task != nil)
            SettingsLink { Image(systemName: "gearshape") }.buttonStyle(.borderless)
        }
        .font(.title3).foregroundStyle(.secondary)
    }

    var readyArea: some View {
        VStack(spacing: 12) {
            if model.update == .available || model.needsRebuild {
                Button {
                    if model.update == .available { model.downloadGame() } else { Task { await model.build(title: "Finishing the update") } }
                } label: {
                    Label(model.update == .available ? "Update Game" : "Finish Update", systemImage: "arrow.down.circle.fill")
                        .frame(maxWidth: .infinity)
                }
                .controlSize(.extraLarge).buttonStyle(.borderedProminent).tint(.orange)
                .disabled(model.gameRunning)
                Button("Play Now, Update Later", action: model.play).buttonStyle(.link).disabled(model.gameRunning || model.steamStarting)
            } else {
                Button(action: model.play) {
                    Label("Play", systemImage: "play.fill").frame(maxWidth: .infinity)
                }
                .controlSize(.extraLarge).buttonStyle(.borderedProminent)
                .disabled(model.gameRunning || model.steamStarting)
                .keyboardShortcut(.defaultAction)
            }
            note
        }
    }

    var working: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(model.task ?? "").font(.headline)
            if let p = model.progress { ProgressView(value: p) } else { ProgressView().progressViewStyle(.linear) }
            HStack {
                Button("Details") { showDetails = true }.buttonStyle(.link)
                Spacer()
                Button("Stop", action: model.cancel)
            }
            .font(.callout)
        }
    }

    var setup: some View {
        let first = model.toolsInstalled ? 1 : 2  // number of the first step after the tools
        let setupDone = model.appFound && model.portFound
        return VStack(alignment: .leading, spacing: 14) {
            if !(model.portFound && model.gameFound) { installLocation }
            if !model.toolsInstalled {
                SetupStep(done: false, number: 1, title: "Install Apple’s developer tools",
                          detail: "Free from Apple, about 5 minutes. Needed to set up the game.") {
                    Button("Install", action: model.installTools)
                    Button("Done? Check Again") { Task { await model.checkTools() } }
                }
            }
            SetupStep(done: model.gameFound, number: first, title: "Download the game",
                      detail: model.gameFound ? shortPath(model.gamePath) : "Your copy of Schedule I from Steam, about 7 GB.") {
                Button("Download", action: model.downloadGame)
                Button("Choose…") { model.choose("Choose the folder with “Schedule I_Data” in it", current: model.gamePath) { model.gamePath = $0 } }
            }
            SetupStep(done: setupDone, number: first + 1, title: "Set it up for your Mac",
                      detail: setupDone ? shortPath(model.appPath) : "Turns your copy into a Mac app. Takes about 10 minutes, only the first time.") {
                Button("Start") { Task { await model.build(title: "Setting up the game for your Mac") } }
                    .disabled(!model.gameFound || !model.toolsInstalled)
            }
            note
        }
    }

    var installLocation: some View {
        let free = model.freeSpace
        let short = free.map { $0 < Launcher.neededSpace } ?? false
        return HStack(spacing: 10) {
            Image(systemName: "externaldrive").font(.title3).foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text("Installs to \(shortPath(model.installFolder))").font(.callout)
                if let free {
                    Text(short ? "Only \(ByteCountFormatter.string(fromByteCount: free, countStyle: .file)) free. It needs about 20 GB."
                               : "\(ByteCountFormatter.string(fromByteCount: free, countStyle: .file)) free. It needs about 20 GB.")
                        .font(.caption).foregroundStyle(short ? .orange : .secondary)
                }
            }
            Spacer()
            Button("Change…", action: model.chooseInstallFolder).controlSize(.small)
        }
        .padding(10)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
    }

    @ViewBuilder var note: some View {
        if let n = model.notice {
            Text(n).font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center)
                .frame(maxWidth: .infinity).fixedSize(horizontal: false, vertical: true)
        }
    }

    var promptSheet: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("Sign in to Steam", systemImage: "lock.fill").font(.headline)
            Text(model.prompt ?? "").foregroundStyle(.secondary)
            SecureField("", text: $secret).textFieldStyle(.roundedBorder).onSubmit(send)
            Text("Sent straight to Steam and never saved.").font(.caption).foregroundStyle(.secondary)
            HStack {
                Button("Cancel") { model.cancel(); model.prompt = nil }
                Spacer()
                Button("Continue", action: send).keyboardShortcut(.defaultAction).disabled(secret.isEmpty)
            }
        }
        .padding(20).frame(width: 340)
    }

    private func send() {
        model.answer(secret)
        secret = ""
    }

    var accountSheet: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("Your Steam account", systemImage: "person.crop.circle").font(.headline)
            Text("The name you sign in to Steam with, not your email. It’s needed to download the game.")
                .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            TextField("account name", text: $account).textFieldStyle(.roundedBorder).onSubmit { model.setAccount(account) }
            HStack {
                Button("Cancel") { model.askAccount = false }
                Spacer()
                Button("Continue") { model.setAccount(account) }.keyboardShortcut(.defaultAction).disabled(account.isEmpty)
            }
        }
        .padding(20).frame(width: 340)
    }

    var detailsSheet: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("What happened").font(.headline)
            ScrollView {
                Text(model.log.isEmpty ? "Nothing here yet." : model.log)
                    .font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(height: 320)
            HStack {
                Button("Copy") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(model.log, forType: .string)
                }
                Spacer()
                Button("Close") { showDetails = false }.keyboardShortcut(.defaultAction)
            }
        }
        .padding(20).frame(width: 560)
    }
}

struct SetupStep<Actions: View>: View {
    let done: Bool
    let number: Int
    let title: String
    let detail: String
    @ViewBuilder let actions: () -> Actions

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            ZStack {
                Circle().fill(done ? Color.green : Color.secondary.opacity(0.2)).frame(width: 26, height: 26)
                if done { Image(systemName: "checkmark").font(.caption.bold()).foregroundStyle(.white) }
                else { Text("\(number)").font(.callout.bold()) }
            }
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.headline)
                Text(detail).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                if !done { HStack { actions() }.controlSize(.small).padding(.top, 2) }
            }
            Spacer(minLength: 0)
        }
    }
}

// MARK: - Settings window

// Row titles and their explanations, a bit larger than the defaults
extension Text {
    func rowTitle() -> some View { self.font(.system(size: 14)) }
    func explanation() -> some View {
        self.font(.system(size: 12)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
    }
}

struct SettingsView: View {
    @EnvironmentObject var model: Launcher

    @AppStorage("settingsTab") private var tab = 0

    var body: some View {
        TabView(selection: $tab) {
            graphics.tabItem { Label("Graphics", systemImage: "sparkles") }.tag(0)
            multiplayer.tabItem { Label("Multiplayer", systemImage: "person.2") }.tag(1)
            downloads.tabItem { Label("Downloads", systemImage: "arrow.down.circle") }.tag(2)
        }
        .frame(width: 500)
    }

    @ViewBuilder var gameRunningNote: some View {
        if model.gameRunning {
            Section {
                Label("Close the game to change these settings.", systemImage: "exclamationmark.circle")
                    .font(.system(size: 13)).foregroundStyle(.orange)
            }
        }
    }

    var graphics: some View {
        Form {
            gameRunningNote
            Section {
                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Text("Resolution").rowTitle()
                        Spacer()
                        Text("\(Int((model.renderScale * 100).rounded()))%").rowTitle().monospacedDigit().foregroundStyle(.secondary)
                    }
                    Slider(value: $model.renderScale, in: 0.5...1, step: 0.05).labelsHidden()
                    HStack {
                        Text("Faster")
                        Spacer()
                        Text("Sharper")
                    }
                    .font(.system(size: 12)).foregroundStyle(.secondary)
                    Text("Lower runs faster but looks a little softer. Menus and text always stay sharp.")
                        .explanation().padding(.top, 4)
                }
                Toggle(isOn: $model.lighterEffects) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Faster sun rays and shadows").rowTitle()
                        Text(model.portHasFasterEffects ? "Looks the same, runs faster." : "Needs a newer porting tool.").explanation()
                    }
                }
                .disabled(!model.portHasFasterEffects)
                Picker(selection: $model.frameLimit) {
                    Text("Unlimited").tag(-1)
                    Text("120 fps").tag(120)
                    Text("90 fps").tag(90)
                    Text("60 fps").tag(60)
                } label: {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Frame rate limit").rowTitle()
                        Text("Unlimited plays best. A limit saves battery but can cause slight stutter.").explanation()
                    }
                }
            } footer: {
                HStack {
                    Text("Changes take effect the next time you start the game.").explanation()
                    Spacer()
                    Button("Use Recommended", action: model.recommended)
                }
            }
            .disabled(model.gameRunning)
        }
        .formStyle(.grouped)
        .fixedSize(horizontal: false, vertical: true)
    }

    var multiplayer: some View {
        Form {
            gameRunningNote
            Section {
                Toggle(isOn: Binding(get: { model.matchRegularVersion }, set: { model.setMatchRegularVersion($0) })) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Play with friends on the regular version").rowTitle()
                        Text(model.portHasVersionMatch || !model.portFound
                             ? "Turn this on if joining a friend shows “Version Mismatch”. Takes about a minute."
                             : "Needs a newer porting tool.").explanation()
                    }
                }
                .disabled(model.gameRunning || model.task != nil || !model.portFound || !model.gameFound || !model.portHasVersionMatch)
            }
        }
        .formStyle(.grouped)
        .fixedSize(horizontal: false, vertical: true)
    }

    var downloads: some View {
        Form {
            Section {
                VStack(alignment: .leading, spacing: 6) {
                    TextField(text: $model.steamUser) { Text("Steam account name").rowTitle() }
                    Text("The name you sign in to Steam with, not your email. Used to download the game and its updates.").explanation()
                }
            }
            Section {
                LocationRow(title: "Game download", path: model.gamePath, found: model.gameFound) {
                    model.choose("Choose the folder with “Schedule I_Data” in it", current: model.gamePath) { model.gamePath = $0 }
                }
                LocationRow(title: "Porting tool", note: "by grovebit, from GitHub", path: model.portPath, found: model.portFound) {
                    model.choose("Choose the Schedule-I-macport folder", current: model.portPath) { model.portPath = $0 }
                }
                LocationRow(title: "Game app", path: model.appPath, found: model.appFound) {
                    model.choose("Choose Schedule I.app", current: model.appPath, files: true) { model.appPath = $0 }
                }
            } header: {
                Text("Where everything is")
            } footer: {
                Text("Change these only if you moved something.").explanation()
            }
            .disabled(model.task != nil)
        }
        .formStyle(.grouped)
        .fixedSize(horizontal: false, vertical: true)
    }
}

struct LocationRow: View {
    let title: String
    var note: String? = nil
    let path: String
    let found: Bool
    let change: () -> Void

    var body: some View {
        HStack {
            Image(systemName: found ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                .font(.system(size: 16))
                .foregroundStyle(found ? .green : .orange)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(title).rowTitle()
                    if let note { Text(note).font(.system(size: 12)).foregroundStyle(.secondary) }
                }
                Text(found ? shortPath(path) : "Not found")
                    .font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                    .help(shortPath(path))
            }
            Spacer()
            Button("Show") { NSWorkspace.shared.selectFile(path, inFileViewerRootedAtPath: "") }
                .disabled(!found)
            Button("Change…", action: change)
        }
    }
}

// MARK: - Screenshots

// Capture mode for the README: `-snapshot <dir> -snapshotName <name> [-snapshotSettings YES] [-settingsTab N]`, plus
// any stored setting as an argument (e.g. -gamePath, -steamUser) to show a particular state. Brings the app to the
// front so the controls show the accent color, writes <dir>/<name>.png at the screen's resolution and quits.
enum Snapshot {
    static var directory: String? { UserDefaults.standard.string(forKey: "snapshot") }

    @MainActor
    static func take(into dir: String, openSettings: () -> Void) async {
        let defaults = UserDefaults.standard
        let name = defaults.string(forKey: "snapshotName") ?? "snapshot"
        NSApp.activate(ignoringOtherApps: true)
        if defaults.bool(forKey: "snapshotSettings") {
            openSettings()
        }
        try? await Task.sleep(for: .seconds(2))
        let window = defaults.bool(forKey: "snapshotSettings")
            ? NSApp.windows.first { $0.isVisible && $0.title != "Schedule I" && $0.contentView != nil }
            : NSApp.windows.first { $0.isVisible && $0.title == "Schedule I" }
        window?.makeFirstResponder(nil)  // no focused text field
        try? await Task.sleep(for: .milliseconds(300))
        // -snapshotHold N: keep the window up for N seconds for an outside screen capture instead (the toolbar's
        // glass doesn't render into cacheDisplay)
        let hold = defaults.double(forKey: "snapshotHold")
        if hold > 0 {
            try? await Task.sleep(for: .seconds(hold))
        } else if let window, let view = window.contentView?.superview,
           let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) {
            view.cacheDisplay(in: view.bounds, to: rep)
            try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: dir + "/" + name + ".png"))
        }
        NSApp.terminate(nil)
    }
}

// MARK: - App

@main
struct LauncherApp: App {
    @StateObject private var model = Launcher()

    var body: some Scene {
        Window("Schedule I", id: "main") {
            MainView().environmentObject(model)
        }
        .windowResizability(.contentSize)
        Settings {
            SettingsView().environmentObject(model)
        }
    }
}
