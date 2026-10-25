//
//  LedgerView.swift
//  SwipeBills
//
//  A private record, not a score. It names the institutions that actually received money, reflects back
//  what the giver tends to finish, and leaves passed bills out of the story entirely.
//

import SwiftUI

struct LedgerView: View {
    let isActive: Bool

    @Environment(AppModel.self) private var app
    @State private var gifts: [MyContribution] = []
    /// City per bill, fetched once; the contributions list doesn't carry it.
    @State private var cities: [UUID: String] = [:]
    @State private var hasLoaded = false
    @State private var errorMessage: String?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    ScreenTitle("Ledger")

                    if !hasLoaded {
                        ProgressView().tint(Color.inkMuted).frame(maxWidth: .infinity, minHeight: 200)
                    } else if let errorMessage, gifts.isEmpty {
                        Text(errorMessage)
                            .font(Typeface.ui(16, .regular))
                            .foregroundStyle(Color.inkMuted)
                            .padding(.top, 20)
                    } else if gifts.isEmpty {
                        empty
                    } else {
                        portrait
                            .padding(.top, 14)
                        yearLine
                            .padding(.top, 18)
                        institutions
                        allGifts
                    }
                }
                .padding(.horizontal, 22)
                .padding(.top, 8)
                .padding(.bottom, 32)
            }
            .scrollIndicators(.hidden)
            .background(CanvasBackground())
            .statusBarBackdrop()
            .refreshable { await load() }
            .payleeNavigation()
        }
        .task(id: isActive) {
            if isActive { await load() }
        }
    }

    // MARK: Derived

    private var counted: [MyContribution] { gifts.filter { $0.status == "succeeded" } }

    /// One entry per bill that reached covered or paid, newest first.
    private var finishedBills: [MyContribution] {
        var seen = Set<UUID>()
        return counted
            .filter { ["funded", "paid"].contains($0.billStatus) }
            .filter { seen.insert($0.billId).inserted }
    }

    // MARK: Sections

    private var empty: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("An empty page.")
                .font(Typeface.display(30, .black, relativeTo: .title))
                .tracking(-1)
                .foregroundStyle(Color.ink)
            Text("Every bill you help finish is written here: the company that was paid, the city, the date. It's yours alone.")
                .font(Typeface.ui(17, .regular, relativeTo: .body))
                .foregroundStyle(Color.inkMuted)
                .lineSpacing(3)
        }
        .padding(.top, 20)
    }

    /// The pattern of what they choose, said back as a sentence. Sharper as the record grows.
    private var portrait: some View {
        Text(portraitSentence)
            .font(Typeface.ui(24, .medium, relativeTo: .title2))
            .foregroundStyle(Color.ink)
            .lineSpacing(4)
            .fixedSize(horizontal: false, vertical: true)
    }

    private var portraitSentence: String {
        let finished = finishedBills
        guard finished.count >= 2 else {
            return finished.isEmpty
                ? "You've chipped in. Your ledger starts with the first bill that's finished."
                : "One bill finished with your help, paid to \(finished[0].billerName)."
        }
        let category = mostCommon(finished.map(\.category))
        let city = mostCommon(finished.compactMap { cities[$0.billId] }.map { $0.components(separatedBy: ",").first ?? $0 })
        let kind = category.map { "\($0.label.lowercased()) bills" } ?? "bills"
        if let city {
            return "You keep finishing \(kind) in \(city)."
        }
        return "You keep finishing \(kind)."
    }

    private var yearLine: some View {
        let year = Calendar.current.component(.year, from: .now)
        let thisYear = finishedBills.filter { Calendar.current.component(.year, from: $0.createdAt) == year }
        let companies = Set(thisYear.map(\.billerName)).count
        let places = Set(thisYear.compactMap { cities[$0.billId] }).count
        return Text("\(String(year)): \(thisYear.count) \(thisYear.count == 1 ? "bill" : "bills") finished, \(companies) \(companies == 1 ? "company" : "companies"), \(places) \(places == 1 ? "city" : "cities").")
            .font(Typeface.ui(16, .regular, relativeTo: .subheadline))
            .foregroundStyle(Color.inkMuted)
    }

    /// The collection worth keeping: the actual institutions that received money.
    private var institutions: some View {
        VStack(alignment: .leading, spacing: 6) {
            SectionLabel("Paid to")
            if finishedBills.isEmpty {
                Text("No bill you've helped is finished yet.")
                    .font(Typeface.ui(16, .regular, relativeTo: .body))
                    .foregroundStyle(Color.inkMuted)
                    .padding(.top, 6)
            }
            VStack(spacing: 0) {
                ForEach(Array(finishedBills.enumerated()), id: \.element.id) { index, item in
                    if index > 0 { Hairline() }
                    NavigationLink {
                        ReceiptView(contribution: item)
                    } label: {
                        HStack(alignment: .firstTextBaseline, spacing: 12) {
                            VStack(alignment: .leading, spacing: 3) {
                                Text(item.billerName)
                                    .font(Typeface.ui(18, .semibold, relativeTo: .headline))
                                    .foregroundStyle(Color.ink)
                                Text(line(for: item))
                                    .font(Typeface.ui(14, .regular, relativeTo: .footnote))
                                    .foregroundStyle(Color.inkMuted)
                            }
                            Spacer()
                            Text(item.billStatus == "paid" ? "Confirmed" : "Paying")
                                .font(Typeface.ui(13, .bold, relativeTo: .caption))
                                .foregroundStyle(item.billStatus == "paid" ? Color.lime : Color.inkMuted)
                            Image(systemName: "chevron.right")
                                .font(.system(size: 13, weight: .bold))
                                .foregroundStyle(Color.inkFaint)
                        }
                        .padding(.vertical, 14)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        .padding(.top, 36)
    }

    private func line(for item: MyContribution) -> String {
        var parts = [item.category.label]
        if let city = cities[item.billId] { parts.append(city) }
        parts.append(item.createdAt.formatted(.dateTime.month(.abbreviated).year()))
        if item.closedByMe { parts.append("you finished it") }
        return parts.joined(separator: " · ")
    }

    private var allGifts: some View {
        VStack(alignment: .leading, spacing: 6) {
            SectionLabel("Every gift")
            VStack(spacing: 0) {
                ForEach(Array(gifts.enumerated()), id: \.element.id) { index, item in
                    if index > 0 { Hairline() }
                    NavigationLink {
                        ReceiptView(contribution: item)
                    } label: {
                        HStack(alignment: .firstTextBaseline, spacing: 12) {
                            VStack(alignment: .leading, spacing: 3) {
                                Text(item.billerName)
                                    .font(Typeface.ui(16, .medium, relativeTo: .subheadline))
                                    .foregroundStyle(Color.ink)
                                Text(item.createdAt.formatted(.dateTime.month(.abbreviated).day()))
                                    .font(Typeface.ui(14, .regular, relativeTo: .footnote))
                                    .foregroundStyle(Color.inkMuted)
                            }
                            Spacer()
                            Text(Money.format(item.amountCents))
                                .font(Typeface.ui(16, .semibold, relativeTo: .subheadline))
                                .foregroundStyle(item.status == "succeeded" ? Color.ink : Color.inkMuted)
                                .strikethrough(item.status == "refunded")
                                .monospacedDigit()
                        }
                        .padding(.vertical, 13)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
            Text("Only you can see this page.")
                .font(Typeface.ui(14, .regular, relativeTo: .footnote))
                .foregroundStyle(Color.inkMuted)
                .padding(.top, 14)
        }
        .padding(.top, 40)
    }

    // MARK: Loading

    private func load() async {
        do {
            let loaded = try await app.api.myContributions()
            let missing = Set(loaded.map(\.billId)).subtracting(cities.keys)
            let fetched = await withTaskGroup(of: (UUID, String)?.self) { group in
                for id in missing.prefix(40) {
                    group.addTask { (try? await app.api.bill(id: id)).map { (id, $0.city) } }
                }
                var result: [UUID: String] = [:]
                for await pair in group {
                    if let pair { result[pair.0] = pair.1 }
                }
                return result
            }
            withAnimation(Motion.settle) {
                gifts = loaded
                cities.merge(fetched) { $1 }
            }
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
        hasLoaded = true
    }

    private func mostCommon<T: Hashable>(_ values: [T]) -> T? {
        Dictionary(grouping: values, by: { $0 }).max { $0.value.count < $1.value.count }?.key
    }
}
