//
//  Bill.swift
//  SwipeBills
//

import Foundation

enum BillCategory: String, Codable, CaseIterable {
    case rent, utilities, medical, phone, internet, other

    var label: String { rawValue.capitalized }

    var symbol: String {
        switch self {
        case .rent: "house.fill"
        case .utilities: "bolt.fill"
        case .medical: "cross.case.fill"
        case .phone: "iphone"
        case .internet: "wifi"
        case .other: "doc.text.fill"
        }
    }
}

struct Bill: Identifiable, Codable, Hashable {
    let id: UUID
    let billerName: String
    let category: BillCategory
    let requesterFirstName: String
    let city: String
    // Money is integer cents, never Double: binary floating point can't represent
    // most decimal amounts exactly (0.1 + 0.2 != 0.3), and the backend and Stripe use cents too.
    let amountDueCents: Int
    var amountRaisedCents: Int
    let story: String
    let dueDate: Date
    var status: String = "live"

    var remainingCents: Int { max(amountDueCents - amountRaisedCents, 0) }

    var progress: Double {
        guard amountDueCents > 0 else { return 1 }
        return min(Double(amountRaisedCents) / Double(amountDueCents), 1)
    }
}

enum Money {
    /// Formats integer cents as US dollars, e.g. 21250 -> "$212.50".
    static func format(_ cents: Int) -> String {
        (Decimal(cents) / 100).formatted(.currency(code: "USD"))
    }

    /// Whole-dollar amounts drop the cents, e.g. 2500 -> "$25", 2550 -> "$25.50". For buttons and chips.
    static func short(_ cents: Int) -> String {
        guard cents % 100 == 0 else { return format(cents) }
        return (Decimal(cents) / 100).formatted(.currency(code: "USD").precision(.fractionLength(0)))
    }

    /// Parses keypad dollars ("25", "12.50", always with a "." separator) into cents. Returns nil for anything
    /// but a positive amount with at most two decimal places.
    static func parseCents(_ text: String) -> Int? {
        let cleaned = text.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: "$", with: "")
        guard let dollars = Decimal(string: cleaned, locale: Locale(identifier: "en_US_POSIX")), dollars > 0 else { return nil }
        var scaled = dollars * 100
        var rounded = Decimal()
        NSDecimalRound(&rounded, &scaled, 0, .plain)
        guard rounded == scaled else { return nil }
        return NSDecimalNumber(decimal: rounded).intValue
    }
}

/// How a due date reads. The due date is the only clock Paylee shows, so it's stated plainly: never a
/// countdown timer, never invented urgency.
enum DuePhrase {
    static func text(for date: Date, now: Date = .now) -> String {
        let calendar = Calendar.current
        let days = calendar.dateComponents([.day], from: calendar.startOfDay(for: now), to: calendar.startOfDay(for: date)).day ?? 0
        switch days {
        case ..<0: return "Was due \(date.formatted(.dateTime.month(.abbreviated).day()))"
        case 0: return "Due today"
        case 1: return "Due tomorrow"
        case 2...6: return "Due \(date.formatted(.dateTime.weekday(.wide)))"
        default: return "Due \(date.formatted(.dateTime.month(.abbreviated).day()))"
        }
    }

    /// True within three days of the due date, when a small remainder is worth surfacing.
    static func isSoon(_ date: Date, now: Date = .now) -> Bool {
        let days = Calendar.current.dateComponents([.day], from: Calendar.current.startOfDay(for: now), to: date).day ?? 99
        return days <= 3
    }
}
