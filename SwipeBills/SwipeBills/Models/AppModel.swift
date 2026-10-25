//
//  AppModel.swift
//  SwipeBills
//

import AuthenticationServices
import Foundation
import Observation
import UIKit
import UserNotifications

@Observable
final class AppModel {
    private(set) var isSignedIn = false
    private(set) var unreadActivity = 0
    var hasCompletedOnboarding = UserDefaults.standard.bool(forKey: AppModel.onboardingKey) {
        didSet { UserDefaults.standard.set(hasCompletedOnboarding, forKey: Self.onboardingKey) }
    }
    /// The APNs token for this device, once iOS hands it over.
    private var pushToken: String?
    /// A bill opened from a link (`paylee://bill/<id>`), waiting for the feed to present it.
    var linkedBillID: UUID?
    let api = APIClient()
    let requests: RequestsModel

    /// Which side of Paylee this account is using. Nil until chosen; changeable from Profile.
    private(set) var role: UserRole? = UserDefaults.standard.string(forKey: AppModel.roleKey).flatMap(UserRole.init(rawValue:))

    /// Privacy, notification, and practice settings. Kept locally and mirrored to the server when it can.
    var settings = ProfileSettings.load() {
        didSet {
            guard settings != oldValue else { return }
            settings.save()
            let api = api, settings = settings
            Task {
                try? await api.saveSettings(settings)
                await self.refreshPracticeProgress()
            }
        }
    }

    /// Where the current practice stands. Nil when they haven't set one, or the server hasn't answered.
    private(set) var practiceProgress: PracticeProgress?

    private static let roleKey = "PayleeRole"

    private static let tokenAccount = "session-token"
    private static let devDeviceIDKey = "PayleeDevDeviceID"
    private static let onboardingKey = "PayleeCompletedOnboarding"

    init() {
        requests = RequestsModel(api: api)
        #if DEBUG
        // `-PayleeRole payer|requestor|none` sets or clears the side, for screenshots.
        if let forced = UserDefaults.standard.string(forKey: "PayleeRole") {
            role = UserRole(rawValue: forced)
        }
        #endif
        if let token = KeychainStore.read(account: Self.tokenAccount) {
            api.token = token
            isSignedIn = true
        }
        api.onUnauthorized = { [weak self] in
            self?.signOut()
        }
    }

    func signInWithApple(_ authorization: ASAuthorization) async throws {
        guard let credential = authorization.credential as? ASAuthorizationAppleIDCredential,
              let tokenData = credential.identityToken,
              let identityToken = String(data: tokenData, encoding: .utf8) else {
            throw APIError.local("Apple didn't return a sign-in token. Please try again.")
        }
        finishSignIn(try await api.appleLogin(identityToken: identityToken))
    }

    /// Development builds can sign in without Apple, which needs a paid developer account and a signed build.
    func signInForDevelopment() async throws {
        let defaults = UserDefaults.standard
        let deviceID = defaults.string(forKey: Self.devDeviceIDKey) ?? "device-\(UUID().uuidString)"
        defaults.set(deviceID, forKey: Self.devDeviceIDKey)
        finishSignIn(try await api.devLogin(deviceID: deviceID))
    }

    func signOut() {
        requests.reset()
        // Stop this device getting the old account's notifications. Best effort: the server also drops
        // tokens Apple reports as dead.
        if let pushToken, api.token != nil {
            let api = api
            Task { try? await api.unregisterDevice(token: pushToken) }
        }
        KeychainStore.delete(account: Self.tokenAccount)
        api.token = nil
        unreadActivity = 0
        role = nil
        UserDefaults.standard.removeObject(forKey: Self.roleKey)
        isSignedIn = false
    }

    // MARK: Role and settings

    func choose(_ role: UserRole) {
        self.role = role
        UserDefaults.standard.set(role.rawValue, forKey: Self.roleKey)
        let api = api
        Task { try? await api.setRole(role) }
    }

    /// Adopts the server's copy of the role and settings once it serves them; until then local values stand.
    func syncProfile() async {
        if let me = try? await api.me(), let serverRole = me.role, serverRole != role {
            role = serverRole
            UserDefaults.standard.set(serverRole.rawValue, forKey: Self.roleKey)
        }
        await refreshPracticeProgress()
    }

    /// Pulls the practice counter. Settings are replaced only when the server copy actually differs,
    /// so this doesn't write them back.
    func refreshPracticeProgress() async {
        guard let loaded = try? await api.loadSettings() else { return }
        practiceProgress = loaded.progress
        if loaded.settings != settings {
            settings = loaded.settings
        }
    }

    /// A quiet note when a gift would pass the monthly amount they set aside. It never blocks the gift.
    func ceilingNote(adding cents: Int) -> String? {
        guard let limit = settings.practice?.monthlyLimitCents else { return nil }
        let spent = practiceProgress?.spentCentsThisMonth ?? 0
        guard spent + cents > limit else { return nil }
        let month = Date.now.formatted(.dateTime.month(.wide))
        return "This goes past the \(Money.short(limit)) you set aside for \(month). You can still pay it."
    }

    func deleteAccount() async throws {
        try await api.deleteAccount()
        UserDefaults.standard.removeObject(forKey: Self.devDeviceIDKey)
        signOut()
    }

    func refreshUnreadActivity() async {
        guard isSignedIn, let activity = try? await api.activity() else { return }
        unreadActivity = activity.unreadCount
    }

    // MARK: Tiers

    /// Set when the giver has just reached a higher member tier; the tab view celebrates it, then clears it.
    var tierUp: Me?

    private static let seenTierKey = "PayleeSeenTier"
    private static let tierOrder = ["Member", "Gold", "Platinum", "Black"]

    /// Compares the giver's tier with the last one they were shown, and queues a celebration if it rose.
    func checkForTierUp() async {
        guard isSignedIn, let me = try? await api.me() else { return }
        let defaults = UserDefaults.standard
        let seen = defaults.string(forKey: Self.seenTierKey) ?? "Member"
        let seenRank = Self.tierOrder.firstIndex(of: seen) ?? 0
        let currentRank = Self.tierOrder.firstIndex(of: me.tier.name) ?? 0
        defaults.set(me.tier.name, forKey: Self.seenTierKey)
        if currentRank > seenRank {
            tierUp = me
        }
    }

    func markActivitySeen() async {
        guard unreadActivity > 0 else { return }
        unreadActivity = 0
        try? await api.markActivitySeen()
    }

    // MARK: Push notifications

    /// Asks once for permission; on yes, iOS calls back with a token via `didRegister(pushToken:)`.
    func requestPushPermission() async {
        let granted = (try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge])) ?? false
        if granted {
            UIApplication.shared.registerForRemoteNotifications()
        }
    }

    /// Re-registers on launch when permission was already given; tokens can change between launches.
    func registerForPushIfAuthorized() async {
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        if settings.authorizationStatus == .authorized {
            UIApplication.shared.registerForRemoteNotifications()
        }
    }

    func didRegister(pushToken token: Data) {
        pushToken = token.map { String(format: "%02x", $0) }.joined()
        Task { await uploadPushToken() }
    }

    func uploadPushToken() async {
        guard isSignedIn, let pushToken else { return }
        #if DEBUG
        let sandbox = true
        #else
        let sandbox = false
        #endif
        try? await api.registerDevice(token: pushToken, sandbox: sandbox)
    }

    /// A notification about a bill was tapped.
    func openNotification(billID: String?) {
        guard let billID, let id = UUID(uuidString: billID) else { return }
        linkedBillID = id
    }

    func handle(_ url: URL) {
        guard url.scheme == AppConfig.checkoutCallbackScheme, url.host == "bill",
              let id = UUID(uuidString: url.lastPathComponent) else { return }
        linkedBillID = id
    }

    private func finishSignIn(_ session: SessionResponse) {
        requests.reset()
        KeychainStore.save(session.token, account: Self.tokenAccount)
        api.token = session.token
        isSignedIn = true
        Task { await uploadPushToken() }
    }
}
