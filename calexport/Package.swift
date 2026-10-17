// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "calexport",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "calexport",
            path: "Sources/calexport",
            exclude: ["Info.plist"],
            linkerSettings: [
                // Embed Info.plist (NSCalendarsFullAccessUsageDescription) so
                // TCC treats this CLI like an app and the grant persists.
                .unsafeFlags([
                    "-Xlinker", "-sectcreate",
                    "-Xlinker", "__TEXT",
                    "-Xlinker", "__info_plist",
                    "-Xlinker", "Sources/calexport/Info.plist",
                ])
            ]
        )
    ]
)
