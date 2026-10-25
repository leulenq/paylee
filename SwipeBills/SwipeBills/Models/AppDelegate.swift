//
//  AppDelegate.swift
//  SwipeBills
//
//  Push notifications still arrive through UIKit's delegate callbacks, so the delegate owns the AppModel
//  and forwards them to it.
//

import UIKit
import UserNotifications

final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    let app = AppModel()

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        Task { await app.registerForPushIfAuthorized() }
        return true
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        app.didRegister(pushToken: deviceToken)
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        // Expected on simulators without APNs and in builds without the push entitlement; nothing to do.
    }

    /// Shows banners even while the app is open: "you closed it" is worth seeing in the moment.
    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        [.banner, .sound, .list]
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse
    ) async {
        let billID = response.notification.request.content.userInfo["bill_id"] as? String
        await MainActor.run {
            app.openNotification(billID: billID)
        }
    }
}
