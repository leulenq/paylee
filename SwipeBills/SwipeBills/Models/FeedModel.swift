//
//  FeedModel.swift
//  SwipeBills
//

import Foundation
import Observation

@Observable
final class FeedModel {
    private(set) var bills: [Bill] = []
    private(set) var isLoading = false
    private(set) var hasLoaded = false
    private(set) var errorMessage: String?
    /// The last bill passed, which Rewind can bring back.
    private(set) var lastPassed: Bill?
    private(set) var placeSentence: String?
    private(set) var season: SeasonNote?
    var preferences = DiscoveryPreferences.load()

    private let api: APIClient
    /// True once the server has nothing more for the current filters, so the deck stops asking.
    private var isExhausted = false
    private let pageSize = 20
    /// Fetch the next page while this many cards are still left, so the deck never visibly runs dry.
    private let refillThreshold = 3

    init(api: APIClient) {
        self.api = api
    }

    /// Replaces the deck, e.g. on first load or after the filters change.
    func load() async {
        guard !isLoading else { return }
        isLoading = true
        defer {
            isLoading = false
            hasLoaded = true
        }
        do {
            let page = try await api.feed(limit: pageSize, preferences: preferences)
            bills = page.bills
            placeSentence = page.place?.sentence
            season = page.season
            isExhausted = page.bills.count < pageSize
            lastPassed = nil
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func updatePreferences(_ new: DiscoveryPreferences) async {
        guard new != preferences else { return }
        preferences = new
        new.save()
        await load()
    }

    func remove(_ bill: Bill) {
        bills.removeAll { $0.id == bill.id }
        refillIfNeeded()
    }

    /// Returns a bill to the top of the deck, e.g. when the giver backs out of the contribute sheet.
    func putBack(_ bill: Bill) {
        bills.removeAll { $0.id == bill.id }
        bills.insert(bill, at: 0)
    }

    func pass(_ bill: Bill) {
        lastPassed = bill
        remove(bill)
        // A lost pass only means the bill may reappear sooner, so failures are ignored.
        Task { try? await api.pass(billID: bill.id) }
    }

    func rewind() {
        guard let bill = lastPassed else { return }
        lastPassed = nil
        putBack(bill)
        Task { try? await api.undoPass(billID: bill.id) }
    }

    private func refillIfNeeded() {
        guard bills.count <= refillThreshold, !isExhausted, !isLoading else { return }
        Task { await loadMore() }
    }

    private func loadMore() async {
        isLoading = true
        defer { isLoading = false }
        guard let page = try? await api.feed(limit: pageSize, preferences: preferences, excluding: bills.map(\.id)) else {
            return
        }
        isExhausted = page.bills.count < pageSize
        let known = Set(bills.map(\.id))
        bills += page.bills.filter { !known.contains($0.id) }
    }
}
