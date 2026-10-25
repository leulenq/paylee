//
//  APIModels.swift
//  SwipeBills
//
//  Response shapes from the backend. JSON keys are snake_case and converted automatically by APIClient.
//

import Foundation

struct UserSummary: Codable, Hashable {
    let id: UUID
    let handle: String
}

struct SessionResponse: Codable {
    let token: String
    let user: UserSummary
}

struct Impact: Codable, Hashable {
    let totalGivenCents: Int
    let billsHelped: Int
    let billsCompleted: Int
    let billsClosed: Int
}

struct Badge: Codable, Hashable, Identifiable {
    let id: String
    let title: String
    let detail: String
    let earned: Bool
}

struct Tier: Codable, Hashable {
    let name: String
    let nextName: String?
    let nextAt: Int?
    /// 0...1 toward the next tier.
    let progress: Double
}

struct Me: Codable {
    let id: UUID
    let handle: String
    let memberSince: Date
    let impact: Impact
    let tier: Tier
    let badges: [Badge]
    /// Nil until the backend serves roles (docs/api/requestor-and-profile.md), or before the user chooses.
    var role: UserRole?
}

struct PlaceNote: Codable, Equatable {
    let sentence: String
    let billsPaidOff: Int
}

struct SeasonNote: Codable, Equatable, Identifiable {
    let id: String
    let title: String
    let category: BillCategory
    let detail: String
}

struct FeedResponse: Codable {
    let bills: [Bill]
    var place: PlaceNote?
    var season: SeasonNote?
}

struct OpenBillRecord: Decodable {
    let bill: Bill
    let myTotalCents: Int

    private enum CodingKeys: String, CodingKey { case myTotalCents }

    init(from decoder: Decoder) throws {
        bill = try Bill(from: decoder)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        myTotalCents = try container.decode(Int.self, forKey: .myTotalCents)
    }
}

struct OpenBillsResponse: Decodable {
    let bills: [OpenBillRecord]
}

struct Contribution: Codable, Hashable, Identifiable {
    let id: UUID
    let billId: UUID
    let amountCents: Int
    let status: String
    let createdAt: Date
    let completedAt: Date?
}

struct ContributionResult: Codable {
    let contribution: Contribution
    /// Set when the giver must finish paying on a checkout page; nil when the payment already settled.
    let checkoutUrl: URL?
    let closedBill: Bool
    let bill: Bill?
}

struct ContributionStatus: Codable {
    let contribution: Contribution
    let bill: Bill?
    let closedBill: Bool
}

struct MyContribution: Codable, Hashable, Identifiable {
    let id: UUID
    let amountCents: Int
    let status: String
    let createdAt: Date
    let billId: UUID
    let billerName: String
    let category: BillCategory
    let requesterFirstName: String
    let billStatus: String
    let amountDueCents: Int
    let amountRaisedCents: Int
    let closedByMe: Bool
}

struct MyContributionsResponse: Codable {
    let contributions: [MyContribution]
}

/// Something that happened to a bill the giver helped: fully covered, paid to the biller, or refunded.
struct ActivityEvent: Codable, Hashable, Identifiable {
    let kind: String
    let at: Date
    let billId: UUID
    let billerName: String
    let category: BillCategory
    let requesterFirstName: String
    let closedByMe: Bool
    let amountCents: Int
    let unread: Bool

    var id: String { "\(kind)-\(billId)-\(at.timeIntervalSince1970)" }
}

struct ActivityResponse: Codable {
    let events: [ActivityEvent]
    let unreadCount: Int
}

struct LeaderboardEntry: Codable, Hashable, Identifiable {
    let rank: Int
    let handle: String
    let billsCompleted: Int
    let billsClosed: Int
    let isMe: Bool

    var id: String { handle }
}

struct Leaderboard: Codable {
    let period: String
    let entries: [LeaderboardEntry]
    let me: LeaderboardEntry?
}

enum LeaderboardPeriod: String, CaseIterable, Identifiable {
    case month, all

    var id: String { rawValue }
    var label: String { self == .month ? "This month" : "All time" }
}

enum ReportReason: String, CaseIterable, Identifiable, Codable {
    case fakeOrAltered = "fake_or_altered"
    case notTheirBill = "not_their_bill"
    case offensive
    case personalInfo = "personal_info"
    case other

    var id: String { rawValue }

    var label: String {
        switch self {
        case .fakeOrAltered: "The bill looks fake or edited"
        case .notTheirBill: "It isn't their bill"
        case .offensive: "Offensive or inappropriate"
        case .personalInfo: "It shows private information"
        case .other: "Something else"
        }
    }
}
