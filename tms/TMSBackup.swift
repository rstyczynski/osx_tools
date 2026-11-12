import Foundation

// A stable, dedicated app identity for macOS privacy permissions.
guard let runner = Bundle.main.object(forInfoDictionaryKey: "TMSRunnerPath") as? String else {
    fputs("TMS runner path is missing from the app configuration.\n", stderr)
    exit(1)
}
let process = Process()
process.executableURL = URL(fileURLWithPath: "/bin/bash")
let arguments = Array(CommandLine.arguments.dropFirst())
process.arguments = [runner] + (arguments.isEmpty ? ["--all"] : arguments)
do {
    try process.run()
    process.waitUntilExit()
    exit(process.terminationStatus)
} catch {
    fputs("TMS launcher: \(error)\n", stderr)
    exit(1)
}
