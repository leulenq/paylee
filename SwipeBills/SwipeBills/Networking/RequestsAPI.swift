//
//  RequestsAPI.swift
//  SwipeBills
//
//  The requestor side: a person's own bills, seen from their side. The endpoints are specified in
//  docs/api/requestor-and-profile.md. Preview data requires an explicit debug launch argument.
//

import Foundation

enum RequestStatus: String, Codable {
    case submitted
    case inReview = "in_review"
    case needsInfo = "needs_info"
    case rejected
    case queued
    case live
    case funded
    case paid
    case expired
    case withdrawn
    case removed

    var isClosed: Bool {
        [.rejected, .paid, .expired, .withdrawn, .removed].contains(self)
    }
}

struct BillRequest: Codable, Hashable, Identifiable {
    let id: UUID
    var status: RequestStatus
    let category: BillCategory
    let billerName: String
    let claimedAmountCents: Int
    var verifiedAmountCents: Int?
    var amountRaisedCents: Int
    let dueDate: Date
    let story: String
    let firstName: String
    let city: String
    let state: String
    let submittedAt: Date
    var liveAt: Date?
    var fundedAt: Date?
    var paidAt: Date?
    var accountEnding: String?
    var statusNote: String?
    var canWithdraw: Bool
    var canUpdateBalance: Bool

    /// The balance givers are working toward: the verified amount once a reviewer confirms it.
    var targetCents: Int { verifiedAmountCents ?? claimedAmountCents }
    var remainingCents: Int { max(targetCents - amountRaisedCents, 0) }
    var progress: Double {
        targetCents > 0 ? min(Double(amountRaisedCents) / Double(targetCents), 1) : 0
    }
}

struct RequestDraft {
    var firstName = ""
    var email = ""
    var city = ""
    var state: String?
    var category: BillCategory?
    var billerName = ""
    var amountText = ""
    var dueDate = Calendar.current.date(byAdding: .day, value: 14, to: .now) ?? .now
    var story = ""
    var consent = false
    var file: Attachment?

    struct Attachment: Equatable {
        let data: Data
        let filename: String
        let mimeType: String
    }

    var amountCents: Int? { Money.parseCents(amountText) }
}

protocol RequestsService {
    func list() async throws -> [BillRequest]
    func submit(_ draft: RequestDraft) async throws -> BillRequest
    func withdraw(_ id: UUID) async throws -> BillRequest
    func updateBalance(_ id: UUID, amountCents: Int, note: String) async throws -> BillRequest
}

// MARK: - Live

struct LiveRequestsService: RequestsService {
    let api: APIClient

    private struct ListResponse: Decodable { let requests: [BillRequest] }

    func list() async throws -> [BillRequest] {
        let response: ListResponse = try await api.get("api/requests")
        return response.requests
    }

    func submit(_ draft: RequestDraft) async throws -> BillRequest {
        guard let file = draft.file, let amount = draft.amountCents, let category = draft.category,
              let state = draft.state else {
            throw APIError.local("Some answers are missing.")
        }
        var form = MultipartForm()
        form.add("first_name", draft.firstName)
        form.add("email", draft.email)
        form.add("city", draft.city)
        form.add("state", state)
        form.add("category", category.rawValue)
        form.add("biller_name", draft.billerName)
        form.add("amount_cents", String(amount))
        form.add("due_date", APIDates.calendarDay(draft.dueDate))
        form.add("story", draft.story)
        form.add("consent", draft.consent ? "true" : "false")
        form.addFile("bill_file", filename: file.filename, mimeType: file.mimeType, data: file.data)
        let data = try await api.data("POST", "api/requests", body: form.finish(), contentType: form.contentType)
        return try api.decoder.decode(BillRequest.self, from: data)
    }

    func withdraw(_ id: UUID) async throws -> BillRequest {
        let data = try await api.data("POST", "api/requests/\(id.uuidString.lowercased())/withdraw")
        return try api.decoder.decode(BillRequest.self, from: data)
    }

    func updateBalance(_ id: UUID, amountCents: Int, note: String) async throws -> BillRequest {
        struct Body: Encodable { let amountCents: Int; let note: String }
        return try await api.post("api/requests/\(id.uuidString.lowercased())/balance", body: Body(amountCents: amountCents, note: note))
    }
}

struct MultipartForm {
    private let boundary = "paylee-\(UUID().uuidString)"
    private var body = Data()

    var contentType: String { "multipart/form-data; boundary=\(boundary)" }

    mutating func add(_ name: String, _ value: String) {
        body.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(name)\"\r\n\r\n\(value)\r\n".utf8))
    }

    mutating func addFile(_ name: String, filename: String, mimeType: String, data: Data) {
        body.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(name)\"; filename=\"\(filename)\"\r\nContent-Type: \(mimeType)\r\n\r\n".utf8))
        body.append(data)
        body.append(Data("\r\n".utf8))
    }

    func finish() -> Data {
        var result = body
        result.append(Data("--\(boundary)--\r\n".utf8))
        return result
    }
}

// MARK: - Preview

/// In-memory stand-in used by debug builds until the backend serves /api/requests. Never used in release.
final class PreviewRequestsService: RequestsService {
    private var requests: [BillRequest]

    init(seeded: Bool) {
        requests = seeded ? Self.samples : []
    }

    func list() async throws -> [BillRequest] {
        try await Task.sleep(for: .milliseconds(250))
        return requests.sorted { $0.submittedAt > $1.submittedAt }
    }

    func submit(_ draft: RequestDraft) async throws -> BillRequest {
        try await Task.sleep(for: .milliseconds(700))
        let request = BillRequest(
            id: UUID(), status: .inReview, category: draft.category ?? .other, billerName: draft.billerName,
            claimedAmountCents: draft.amountCents ?? 0, verifiedAmountCents: nil, amountRaisedCents: 0,
            dueDate: draft.dueDate, story: draft.story, firstName: draft.firstName, city: draft.city,
            state: draft.state ?? "", submittedAt: .now, canWithdraw: true, canUpdateBalance: true
        )
        requests.append(request)
        return request
    }

    func withdraw(_ id: UUID) async throws -> BillRequest {
        try await Task.sleep(for: .milliseconds(400))
        guard let index = requests.firstIndex(where: { $0.id == id }) else { throw APIError.local("Not found") }
        requests[index].status = .withdrawn
        requests[index].canWithdraw = false
        requests[index].canUpdateBalance = false
        return requests[index]
    }

    func updateBalance(_ id: UUID, amountCents: Int, note: String) async throws -> BillRequest {
        try await Task.sleep(for: .milliseconds(400))
        guard let index = requests.firstIndex(where: { $0.id == id }) else { throw APIError.local("Not found") }
        requests[index].statusNote = "We'll confirm the new balance of \(Money.format(amountCents)) with the company."
        return requests[index]
    }

    private static var samples: [BillRequest] {
        let day: TimeInterval = 86_400
        return [
            BillRequest(
                id: UUID(), status: .live, category: .utilities, billerName: "Kentucky Utilities",
                claimedAmountCents: 21_250, verifiedAmountCents: 21_250, amountRaisedCents: 13_500,
                dueDate: .now.addingTimeInterval(5 * day),
                story: "My hours got cut this fall and I fell behind. I have two kids and a shutoff notice for next week.",
                firstName: "Maria", city: "Lexington", state: "KY", submittedAt: .now.addingTimeInterval(-3 * day),
                liveAt: .now.addingTimeInterval(-2 * day), accountEnding: "4821", canWithdraw: true, canUpdateBalance: true
            ),
            BillRequest(
                id: UUID(), status: .paid, category: .phone, billerName: "Verizon",
                claimedAmountCents: 8_900, verifiedAmountCents: 8_900, amountRaisedCents: 8_900,
                dueDate: .now.addingTimeInterval(-40 * day),
                story: "I'm job hunting and my phone is how employers reach me.",
                firstName: "Maria", city: "Lexington", state: "KY", submittedAt: .now.addingTimeInterval(-60 * day),
                liveAt: .now.addingTimeInterval(-58 * day), fundedAt: .now.addingTimeInterval(-55 * day),
                paidAt: .now.addingTimeInterval(-53 * day), accountEnding: "0917", canWithdraw: false, canUpdateBalance: false
            ),
        ]
    }
}
