//
//  RequestsModel.swift
//  SwipeBills
//

import Foundation
import Observation

@Observable
final class RequestsModel {
    private(set) var requests: [BillRequest] = []
    private(set) var hasLoaded = false
    private(set) var errorMessage: String?
    /// True when showing in-memory preview data because the backend doesn't serve /api/requests yet.
    private(set) var isPreview = false

    private var service: RequestsService

    init(api: APIClient) {
        service = LiveRequestsService(api: api)
        #if DEBUG
        // `-PayleePreviewRequests empty|sample` skips the backend entirely.
        if let mode = UserDefaults.standard.string(forKey: "PayleePreviewRequests") {
            service = PreviewRequestsService(seeded: mode == "sample")
            isPreview = true
        }
        #endif
    }

    /// Bills still moving, newest first; then everything finished.
    var active: [BillRequest] { requests.filter { !$0.status.isClosed } }
    var past: [BillRequest] { requests.filter { $0.status.isClosed } }

    func load() async {
        do {
            requests = try await perform { try await $0.list() }
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
        hasLoaded = true
    }

    func submit(_ draft: RequestDraft) async throws -> BillRequest {
        let request = try await perform { try await $0.submit(draft) }
        upsert(request)
        return request
    }

    func withdraw(_ id: UUID) async throws {
        upsert(try await perform { try await $0.withdraw(id) })
    }

    func updateBalance(_ id: UUID, amountCents: Int, note: String) async throws {
        upsert(try await perform { try await $0.updateBalance(id, amountCents: amountCents, note: note) })
    }

    private func upsert(_ request: BillRequest) {
        if let index = requests.firstIndex(where: { $0.id == request.id }) {
            requests[index] = request
        } else {
            requests.insert(request, at: 0)
        }
    }

    /// Runs a call against the current service. In debug builds a 404 means the backend hasn't shipped the
    /// requestor endpoints yet, so the model switches to preview data and retries once.
    private func perform<T>(_ call: (RequestsService) async throws -> T) async throws -> T {
        do {
            return try await call(service)
        } catch let error as APIError where error.status == 404 && !isPreview {
            #if DEBUG
            service = PreviewRequestsService(seeded: false)
            isPreview = true
            return try await call(service)
            #else
            throw error
            #endif
        }
    }
}
