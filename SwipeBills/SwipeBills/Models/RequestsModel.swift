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
    /// Preview data is available only through the explicit debug launch argument.
    private(set) var isPreview = false

    private var service: RequestsService
    private let api: APIClient
    private var generation = 0

    init(api: APIClient) {
        self.api = api
        service = LiveRequestsService(api: api)
        configurePreview()
    }

    private func configurePreview() {
        #if DEBUG
        // `-PayleePreviewRequests empty|sample` skips the backend entirely.
        if let mode = UserDefaults.standard.string(forKey: "PayleePreviewRequests") {
            service = PreviewRequestsService(seeded: mode == "sample")
            isPreview = true
        }
        #endif
    }

    /// Clear private data and discard in-flight responses when the signed-in account changes.
    func reset() {
        generation += 1
        requests = []
        hasLoaded = false
        errorMessage = nil
        isPreview = false
        service = LiveRequestsService(api: api)
        configurePreview()
    }

    /// Bills still moving, newest first; then everything finished.
    var active: [BillRequest] { requests.filter { !$0.status.isClosed } }
    var past: [BillRequest] { requests.filter { $0.status.isClosed } }

    func load() async {
        let started = generation
        do {
            let loaded = try await service.list()
            guard started == generation else { return }
            requests = loaded
            errorMessage = nil
        } catch {
            guard started == generation else { return }
            errorMessage = error.localizedDescription
        }
        hasLoaded = true
    }

    func submit(_ draft: RequestDraft) async throws -> BillRequest {
        let started = generation
        let request = try await service.submit(draft)
        guard started == generation else { throw CancellationError() }
        upsert(request)
        return request
    }

    func withdraw(_ id: UUID) async throws {
        let started = generation
        let request = try await service.withdraw(id)
        guard started == generation else { throw CancellationError() }
        upsert(request)
    }

    func updateBalance(_ id: UUID, amountCents: Int, note: String) async throws {
        let started = generation
        let request = try await service.updateBalance(id, amountCents: amountCents, note: note)
        guard started == generation else { throw CancellationError() }
        upsert(request)
    }

    private func upsert(_ request: BillRequest) {
        if let index = requests.firstIndex(where: { $0.id == request.id }) {
            requests[index] = request
        } else {
            requests.insert(request, at: 0)
        }
    }

}
