//
//  AppConfig.swift
//  SwipeBills
//

import Foundation

enum AppConfig {
    /// Override with the launch argument `-PayleeAPIBaseURL http://192.168.1.20:8000` to reach a dev server
    /// from a physical device.
    static var apiBaseURL: URL {
        if let override = UserDefaults.standard.string(forKey: "PayleeAPIBaseURL"), let url = URL(string: override) {
            return url
        }
        #if DEBUG
        return URL(string: "http://127.0.0.1:8000")!
        #else
        // Set to the production API host before shipping.
        return URL(string: "https://api.paylee.example")!
        #endif
    }

    /// Where shareable bill pages live (`/b/<id>`). The backend serves them, so in development it's the API host.
    static var publicWebURL: URL {
        #if DEBUG
        return apiBaseURL
        #else
        // Set to the production web host before shipping.
        return URL(string: "https://paylee.example")!
        #endif
    }

    static func shareURL(for billID: UUID) -> URL {
        publicWebURL.appending(path: "b/\(billID.uuidString.lowercased())")
    }

    /// The app's URL scheme: `paylee://bill/<id>` opens a bill, `paylee://checkout` returns from payment.
    /// Must match APP_CALLBACK_SCHEME in backend/app/web.py and CFBundleURLTypes in Paylee-Info.plist.
    static let checkoutCallbackScheme = "paylee"

    /// Mirrors the server's minimum. The server is the authority; this only shapes the amount picker.
    static let minimumContributionCents = 500
}
