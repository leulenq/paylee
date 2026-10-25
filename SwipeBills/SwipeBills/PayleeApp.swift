//
//  PayleeApp.swift
//  SwipeBills
//
//  Created by Leul F. Enquanhone on 10/24/26.
//

import SwiftUI

@main
struct PayleeApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    init() {
        Typeface.registerBundledFonts()
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(delegate.app)
                .tint(Color.lime)
                .preferredColorScheme(.dark)
                .onOpenURL { delegate.app.handle($0) }
        }
    }
}
