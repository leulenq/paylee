//
//  APIClient.swift
//  SwipeBills
//

import Foundation

struct APIError: LocalizedError {
    let status: Int
    let code: String?
    let message: String
    /// For balance errors, how much the bill can still take.
    let availableCents: Int?
    /// For form validation (422), a message per field name, e.g. ["story": "Write at least 40 characters."].
    var fieldErrors: [String: String] = [:]

    var errorDescription: String? { message }

    static func local(_ message: String) -> APIError {
        APIError(status: 0, code: nil, message: message, availableCents: nil)
    }

    /// FastAPI errors look like {"detail": "text"} or {"detail": {"code", "message", "available_cents"}}.
    init(status: Int, body: Data) {
        let detail = (try? JSONSerialization.jsonObject(with: body) as? [String: Any])?["detail"]
        if let text = detail as? String {
            self.init(status: status, code: nil, message: text, availableCents: nil)
        } else if let object = detail as? [String: Any], let message = object["message"] as? String {
            self.init(
                status: status,
                code: object["code"] as? String,
                message: message,
                availableCents: object["available_cents"] as? Int
            )
        } else if let fields = detail as? [String: String], !fields.isEmpty {
            self.init(status: status, code: "invalid_fields", message: "Check the highlighted answers.", availableCents: nil)
            fieldErrors = fields
        } else {
            let fallback = status == 401 ? "Please sign in again." : "Something went wrong. Please try again."
            self.init(status: status, code: nil, message: fallback, availableCents: nil)
        }
    }

    init(status: Int, code: String?, message: String, availableCents: Int?) {
        self.status = status
        self.code = code
        self.message = message
        self.availableCents = availableCents
    }
}

final class APIClient {
    var token: String?
    /// Called when the server rejects the session, so the app can return to sign-in.
    var onUnauthorized: (() -> Void)?

    private let baseURL: URL
    private let session: URLSession = .shared

    let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        decoder.dateDecodingStrategy = .custom(APIDates.decode)
        return decoder
    }()

    let encoder: JSONEncoder = {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        return encoder
    }()

    init(baseURL: URL = AppConfig.apiBaseURL) {
        self.baseURL = baseURL
    }

    // MARK: Auth

    func appleLogin(identityToken: String) async throws -> SessionResponse {
        struct Body: Encodable { let identityToken: String }
        return try await post("api/auth/apple", body: Body(identityToken: identityToken))
    }

    func devLogin(deviceID: String) async throws -> SessionResponse {
        struct Body: Encodable { let deviceId: String }
        return try await post("api/auth/dev", body: Body(deviceId: deviceID))
    }

    // MARK: Me

    func me() async throws -> Me {
        try await get("api/me")
    }

    func myContributions() async throws -> [MyContribution] {
        let response: MyContributionsResponse = try await get("api/me/contributions")
        return response.contributions
    }

    func deleteAccount() async throws {
        _ = try await data("DELETE", "api/me")
    }

    func activity() async throws -> ActivityResponse {
        try await get("api/me/activity")
    }

    func markActivitySeen() async throws {
        _ = try await data("POST", "api/me/activity/seen")
    }

    // MARK: Bills

    func openBills() async throws -> [OpenBillRecord] {
        let response: OpenBillsResponse = try await get("api/me/open")
        return response.bills
    }

    func feed(limit: Int = 20, preferences: DiscoveryPreferences = .init(), excluding: [UUID] = []) async throws -> FeedResponse {
        var query = [URLQueryItem(name: "limit", value: String(limit))]
        query += preferences.categories.sorted { $0.rawValue < $1.rawValue }.map { URLQueryItem(name: "category", value: $0.rawValue) }
        if let state = preferences.state {
            query.append(URLQueryItem(name: "state", value: state))
        }
        if let maxRemaining = preferences.maxRemainingCents {
            query.append(URLQueryItem(name: "max_remaining_cents", value: String(maxRemaining)))
        }
        query += excluding.prefix(100).map { URLQueryItem(name: "exclude", value: $0.uuidString.lowercased()) }
        return try await get("api/feed", query: query)
    }

    func undoPass(billID: UUID) async throws {
        _ = try await data("DELETE", "api/bills/\(billID.uuidString.lowercased())/pass")
    }

    func registerDevice(token: String, sandbox: Bool) async throws {
        struct Body: Encodable { let token: String; let environment: String }
        _ = try await data("POST", "api/me/devices", body: encoder.encode(Body(token: token, environment: sandbox ? "sandbox" : "production")))
    }

    func unregisterDevice(token: String) async throws {
        _ = try await data("DELETE", "api/me/devices/\(token)")
    }

    func bill(id: UUID) async throws -> Bill {
        try await get("api/bills/\(id.uuidString.lowercased())")
    }

    func pass(billID: UUID) async throws {
        _ = try await data("POST", "api/bills/\(billID.uuidString.lowercased())/pass")
    }

    func report(billID: UUID, reason: ReportReason) async throws {
        struct Body: Encodable { let reason: ReportReason }
        _ = try await data("POST", "api/bills/\(billID.uuidString.lowercased())/report", body: encoder.encode(Body(reason: reason)))
    }

    // MARK: Contributions

    func contribute(billID: UUID, amountCents: Int) async throws -> ContributionResult {
        struct Body: Encodable { let amountCents: Int }
        return try await post("api/bills/\(billID.uuidString.lowercased())/contributions", body: Body(amountCents: amountCents))
    }

    func contribution(id: UUID) async throws -> ContributionStatus {
        try await get("api/contributions/\(id.uuidString.lowercased())")
    }

    func cancelContribution(id: UUID) async throws {
        _ = try await data("POST", "api/contributions/\(id.uuidString.lowercased())/cancel")
    }

    // MARK: Leaderboard

    func leaderboard(period: LeaderboardPeriod) async throws -> Leaderboard {
        try await get("api/leaderboard", query: [URLQueryItem(name: "period", value: period.rawValue)])
    }

    // MARK: Transport

    func get<Response: Decodable>(_ path: String, query: [URLQueryItem] = []) async throws -> Response {
        try decoder.decode(Response.self, from: await data("GET", path, query: query))
    }

    func post<Response: Decodable>(_ path: String, body: some Encodable) async throws -> Response {
        try decoder.decode(Response.self, from: await data("POST", path, body: encoder.encode(body)))
    }

    func data(_ method: String, _ path: String, query: [URLQueryItem] = [], body: Data? = nil, contentType: String = "application/json") async throws -> Data {
        var components = URLComponents(url: baseURL.appending(path: path), resolvingAgainstBaseURL: false)!
        if !query.isEmpty {
            components.queryItems = query
        }
        var request = URLRequest(url: components.url!)
        request.httpMethod = method
        request.timeoutInterval = 20
        if let token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            request.httpBody = body
            request.setValue(contentType, forHTTPHeaderField: "Content-Type")
        }

        let (data, response) = try await session.data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else {
            let error = APIError(status: status, body: data)
            if status == 401, token != nil {
                onUnauthorized?()
            }
            throw error
        }
        return data
    }
}

/// The API sends calendar dates ("2026-10-29") and timestamps with microseconds ("2026-10-24T19:10:11.123456Z").
nonisolated enum APIDates {
    /// "2026-10-29" in the device's time zone, the inverse of how calendar dates are decoded.
    static func calendarDay(_ date: Date) -> String {
        formatters[0].string(from: date)
    }

    static func decode(_ decoder: Decoder) throws -> Date {
        let text = try decoder.singleValueContainer().decode(String.self)
        for formatter in formatters {
            if let date = formatter.date(from: text) {
                return date
            }
        }
        throw DecodingError.dataCorrupted(
            DecodingError.Context(codingPath: decoder.codingPath, debugDescription: "Unrecognized date \(text)")
        )
    }

    private static let formatters: [DateFormatter] = [
        // Calendar dates are read in the device's time zone so "Oct 29" never displays as Oct 28.
        makeFormatter("yyyy-MM-dd", timeZone: .current),
        makeFormatter("yyyy-MM-dd'T'HH:mm:ss.SSSSSSXXXXX", timeZone: nil),
        makeFormatter("yyyy-MM-dd'T'HH:mm:ssXXXXX", timeZone: nil),
    ]

    private static func makeFormatter(_ format: String, timeZone: TimeZone?) -> DateFormatter {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = format
        if let timeZone {
            formatter.timeZone = timeZone
        }
        return formatter
    }
}
