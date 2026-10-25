//
//  DiscoveryPreferences.swift
//  SwipeBills
//
//  What a giver wants to see: which kinds of bills, where, and how large. Chosen during onboarding and
//  editable from the deck. Kept on the device, since the server only needs them as feed filters.
//

import Foundation

nonisolated struct DiscoveryPreferences: Codable, Equatable {
    /// Empty means every category.
    var categories: Set<BillCategory> = []
    /// Two-letter state code, or nil for anywhere.
    var state: String?
    /// Only bills with at most this much left to close, or nil for any amount.
    var maxRemainingCents: Int?

    var activeFilterCount: Int {
        (categories.isEmpty ? 0 : 1) + (state == nil ? 0 : 1) + (maxRemainingCents == nil ? 0 : 1)
    }

    static let amountLimits: [Int?] = [nil, 10_000, 25_000, 50_000, 100_000]

    private static let storageKey = "PayleeDiscoveryPreferences"

    static func load() -> DiscoveryPreferences {
        guard let data = UserDefaults.standard.data(forKey: storageKey),
              let preferences = try? JSONDecoder().decode(DiscoveryPreferences.self, from: data) else {
            return DiscoveryPreferences()
        }
        return preferences
    }

    func save() {
        if let data = try? JSONEncoder().encode(self) {
            UserDefaults.standard.set(data, forKey: Self.storageKey)
        }
    }
}

extension BillCategory: Identifiable {
    var id: String { rawValue }
}

nonisolated enum USState {
    /// Code and name, sorted by name, matching the backend's US_STATES.
    static let all: [(code: String, name: String)] = [
        ("AL", "Alabama"), ("AK", "Alaska"), ("AZ", "Arizona"), ("AR", "Arkansas"), ("CA", "California"),
        ("CO", "Colorado"), ("CT", "Connecticut"), ("DE", "Delaware"), ("DC", "District of Columbia"),
        ("FL", "Florida"), ("GA", "Georgia"), ("HI", "Hawaii"), ("ID", "Idaho"), ("IL", "Illinois"),
        ("IN", "Indiana"), ("IA", "Iowa"), ("KS", "Kansas"), ("KY", "Kentucky"), ("LA", "Louisiana"),
        ("ME", "Maine"), ("MD", "Maryland"), ("MA", "Massachusetts"), ("MI", "Michigan"), ("MN", "Minnesota"),
        ("MS", "Mississippi"), ("MO", "Missouri"), ("MT", "Montana"), ("NE", "Nebraska"), ("NV", "Nevada"),
        ("NH", "New Hampshire"), ("NJ", "New Jersey"), ("NM", "New Mexico"), ("NY", "New York"),
        ("NC", "North Carolina"), ("ND", "North Dakota"), ("OH", "Ohio"), ("OK", "Oklahoma"), ("OR", "Oregon"),
        ("PA", "Pennsylvania"), ("RI", "Rhode Island"), ("SC", "South Carolina"), ("SD", "South Dakota"),
        ("TN", "Tennessee"), ("TX", "Texas"), ("UT", "Utah"), ("VT", "Vermont"), ("VA", "Virginia"),
        ("WA", "Washington"), ("WV", "West Virginia"), ("WI", "Wisconsin"), ("WY", "Wyoming"),
    ]

    static func name(for code: String) -> String {
        all.first { $0.code == code }?.name ?? code
    }
}
