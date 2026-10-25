//
//  ProfileAPI.swift
//  SwipeBills
//
//  Roles, profile settings, and private rank. Specified in docs/api/requestor-and-profile.md; each call
//  is best effort until the backend serves it, and the app keeps a local copy of everything here.
//

import Foundation

enum UserRole: String, Codable {
    case payer
    case requestor
}

/// A giving rhythm the payer chose for themself. The app's job is to say when it's complete, never to
/// push past it, and a missed period changes nothing.
nonisolated struct GivingPractice: Codable, Equatable {
    enum Cadence: String, Codable, CaseIterable { case weekly, monthly }
    enum StartDay: String, Codable, CaseIterable { case sunday, payday, firstOfMonth = "first_of_month" }

    var cadence: Cadence = .weekly
    var billsPerPeriod = 1
    var monthlyLimitCents: Int?
    var startDay: StartDay = .sunday

    var summary: String {
        let bills = billsPerPeriod == 1 ? "One bill" : "\(billsPerPeriod) bills"
        return "\(bills) a \(cadence == .weekly ? "week" : "month")"
    }
}

/// How the current period stands. A missed period is simply absent: nothing here resets.
nonisolated struct PracticeProgress: Codable, Equatable {
    var finishedThisPeriod: Int
    var target: Int
    var spentCentsThisMonth: Int
    var complete: Bool
    var periodLabel: String
}

struct LoadedSettings {
    var settings: ProfileSettings
    var progress: PracticeProgress?
}

nonisolated struct ProfileSettings: Codable, Equatable {
    var showOnLeaderboards = true
    var showCityOnShareCards = true
    var notifyPaymentFacts = true
    var notifyOpenBills = true
    var practice: GivingPractice?

    private static let storageKey = "PayleeProfileSettings"

    static func load() -> ProfileSettings {
        guard let data = UserDefaults.standard.data(forKey: storageKey),
              let settings = try? JSONDecoder().decode(ProfileSettings.self, from: data) else {
            return ProfileSettings()
        }
        return settings
    }

    func save() {
        if let data = try? JSONEncoder().encode(self) {
            UserDefaults.standard.set(data, forKey: Self.storageKey)
        }
    }
}

struct PrivateRank: Codable, Equatable {
    /// Nil until this payer has helped a bill all the way to paid.
    let rank: Int?
    let of: Int
    let period: String
    let billsCompleted: Int
    let visible: Bool
}

/// What a contribution's money has done so far, from `GET /api/contributions/{id}/receipt`.
/// Each timestamp is set only once Paylee holds evidence for that step.
struct PaymentReceipt: Codable, Equatable {
    let contributionId: UUID
    let contributionCents: Int
    let fundingStatus: String
    let refundedAt: Date?
    let billerName: String
    let category: BillCategory
    let paymentId: UUID?
    let paymentStatus: String?
    let billPaymentCents: Int?
    let submittedAt: Date?
    let settledAt: Date?
    let appliedAt: Date?
    let returnedAt: Date?
    let accountEnding: String?
}

extension APIClient {
    func setRole(_ role: UserRole) async throws {
        struct Body: Encodable { let role: UserRole }
        _ = try await data("PUT", "api/me/role", body: encoder.encode(Body(role: role)))
    }

    func loadSettings() async throws -> LoadedSettings {
        let data = try await self.data("GET", "api/me/settings")
        let settings = try decoder.decode(ProfileSettings.self, from: data)
        struct Envelope: Decodable { var practiceProgress: PracticeProgress? }
        let progress = try decoder.decode(Envelope.self, from: data).practiceProgress
        return LoadedSettings(settings: settings, progress: progress)
    }

    func saveSettings(_ settings: ProfileSettings) async throws {
        _ = try await data("PUT", "api/me/settings", body: encoder.encode(settings))
    }

    func rank() async throws -> PrivateRank {
        try await get("api/me/rank")
    }

    func receipt(contributionID: UUID) async throws -> PaymentReceipt {
        try await get("api/contributions/\(contributionID.uuidString.lowercased())/receipt")
    }
}
