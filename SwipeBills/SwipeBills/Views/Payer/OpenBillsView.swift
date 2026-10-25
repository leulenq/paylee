//
//  OpenBillsView.swift
//  SwipeBills
//
//  The reason to come back: bills you already put money on that aren't finished, ordered by their real due
//  dates. Below them, the second beat of every gift: what the payment has actually done since.
//

import SwiftUI

struct OpenBill: Identifiable, Hashable {
    let bill: Bill
    let myTotalCents: Int
    var id: UUID { bill.id }
}

struct OpenBillsView: View {
    let isActive: Bool
    let onGoToDeck: () -> Void

    @Environment(AppModel.self) private var app
    @State private var open: [OpenBill] = []
    @State private var updates: [ActivityEvent] = []
    @State private var contributions: [MyContribution] = []
    @State private var hasLoaded = false
    @State private var errorMessage: String?
    @State private var detailBill: Bill?
    @State private var finishing: Bill?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    ScreenTitle("Open")

                    if !hasLoaded {
                        ProgressView().tint(Color.inkMuted).frame(maxWidth: .infinity, minHeight: 200)
                    } else if let errorMessage, open.isEmpty && updates.isEmpty {
                        Text(errorMessage)
                            .font(Typeface.ui(16, .regular))
                            .foregroundStyle(Color.inkMuted)
                            .padding(.top, 20)
                    } else {
                        openSection
                        if !updates.isEmpty {
                            updatesSection
                        }
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
        .sheet(item: $detailBill) { bill in
            BillDetailView(bill: bill, api: app.api) {
                detailBill = nil
                Task {
                    try? await Task.sleep(for: .milliseconds(450))
                    finishing = bill
                }
            }
        }
        .sheet(item: $finishing, onDismiss: { Task { await load() } }) { bill in
            ContributeSheet(
                bill: bill,
                api: app.api,
                startsWithFullAmount: true,
                monthlyLimitCents: app.settings.practice?.monthlyLimitCents,
                spentThisMonthCents: app.practiceProgress?.spentCentsThisMonth ?? 0,
                showsCity: app.settings.showCityOnShareCards
            ) { _ in }
        }
    }

    // MARK: Open bills

    @ViewBuilder
    private var openSection: some View {
        if open.isEmpty {
            VStack(alignment: .leading, spacing: 12) {
                Text("Nothing open.")
                    .font(Typeface.display(30, .black, relativeTo: .title))
                    .tracking(-1)
                    .foregroundStyle(Color.ink)
                Text("When you chip into a bill that isn't finished yet, it waits here, ordered by when it's due.")
                    .font(Typeface.ui(17, .regular, relativeTo: .body))
                    .foregroundStyle(Color.inkMuted)
                    .lineSpacing(3)
                Button("Go to the deck", action: onGoToDeck)
                    .buttonStyle(.secondary)
                    .padding(.top, 6)
            }
            .padding(.top, 20)
        } else {
            Text(open.count == 1 ? "One bill you started isn't finished." : "\(open.count) bills you started aren't finished.")
                .font(Typeface.ui(17, .regular, relativeTo: .body))
                .foregroundStyle(Color.inkMuted)
                .padding(.top, 8)
            VStack(spacing: 0) {
                ForEach(Array(open.enumerated()), id: \.element.id) { index, item in
                    if index > 0 { Hairline() }
                    openRow(item)
                }
            }
            .padding(.top, 10)
        }
    }

    private func openRow(_ item: OpenBill) -> some View {
        let bill = item.bill
        let soon = DuePhrase.isSoon(bill.dueDate)
        return VStack(alignment: .leading, spacing: 12) {
            Button {
                detailBill = bill
            } label: {
                HStack(alignment: .firstTextBaseline) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(bill.billerName)
                            .font(Typeface.ui(18, .semibold, relativeTo: .headline))
                            .foregroundStyle(Color.ink)
                        Text("\(bill.city) · \(DuePhrase.text(for: bill.dueDate))")
                            .font(Typeface.ui(15, soon ? .semibold : .regular, relativeTo: .subheadline))
                            .foregroundStyle(soon ? Color.ink : Color.inkMuted)
                    }
                    Spacer()
                    VStack(alignment: .trailing, spacing: 2) {
                        Text(Money.format(bill.remainingCents))
                            .font(Typeface.display(20, .extraBold, relativeTo: .headline))
                            .foregroundStyle(Color.ink)
                            .monospacedDigit()
                        Text("left")
                            .font(Typeface.ui(13, .regular, relativeTo: .caption))
                            .foregroundStyle(Color.inkMuted)
                    }
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            ProgressBar(progress: bill.progress)

            HStack {
                Text("You put in \(Money.format(item.myTotalCents))")
                    .font(Typeface.ui(14, .regular, relativeTo: .footnote))
                    .foregroundStyle(Color.inkMuted)
                Spacer()
                Button {
                    finishing = bill
                } label: {
                    Text("Finish it · \(Money.short(bill.remainingCents))")
                        .font(Typeface.ui(15, .bold, relativeTo: .subheadline))
                        .foregroundStyle(Color.onLime)
                        .padding(.horizontal, 14)
                        .frame(height: 38)
                        .background(RoundedRectangle(cornerRadius: 11, style: .continuous).fill(Color.lime))
                }
                .buttonStyle(.pressable)
                .accessibilityLabel("Finish the \(bill.billerName) bill, \(Money.format(bill.remainingCents))")
            }
        }
        .padding(.vertical, 18)
    }

    // MARK: Updates (the second beat)

    private var updatesSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            SectionLabel("What your money did")
            VStack(spacing: 0) {
                ForEach(Array(updates.enumerated()), id: \.element.id) { index, event in
                    if index > 0 { Hairline() }
                    if let contribution = latestContribution(on: event.billId) {
                        NavigationLink {
                            ReceiptView(contribution: contribution)
                        } label: {
                            updateRow(event, linked: true)
                        }
                        .buttonStyle(.plain)
                    } else {
                        updateRow(event, linked: false)
                    }
                }
            }
        }
        .padding(.top, 40)
    }

    private func updateRow(_ event: ActivityEvent, linked: Bool) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(updateTitle(event))
                    .font(Typeface.ui(16, .medium, relativeTo: .subheadline))
                    .foregroundStyle(Color.ink)
                    .fixedSize(horizontal: false, vertical: true)
                Text(event.at.formatted(.relative(presentation: .named)))
                    .font(Typeface.ui(14, .regular, relativeTo: .footnote))
                    .foregroundStyle(Color.inkMuted)
            }
            Spacer(minLength: 8)
            if event.unread {
                Text("New")
                    .font(Typeface.ui(13, .bold, relativeTo: .caption))
                    .foregroundStyle(Color.lime)
            }
            if linked {
                Image(systemName: "chevron.right")
                    .font(.system(size: 13, weight: .bold))
                    .foregroundStyle(Color.inkFaint)
            }
        }
        .padding(.vertical, 14)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }

    /// Facts only: what happened to the money, and to which company. Never the person's feelings.
    private func updateTitle(_ event: ActivityEvent) -> String {
        switch event.kind {
        case "paid":
            return "\(event.billerName) confirmed the \(Money.format(event.amountCents)) payment."
        case "refunded":
            return "\(Money.format(event.amountCents)) came back to you. The \(event.billerName) bill was finished before your payment landed."
        default:
            return event.closedByMe
                ? "You finished the \(event.billerName) bill. Payment to the company is next."
                : "The \(event.billerName) bill you started was finished. Payment to the company is next."
        }
    }

    private func latestContribution(on billID: UUID) -> MyContribution? {
        contributions.first { $0.billId == billID }
    }

    /// Used when `/api/me/open` isn't available yet. One request per live bill.
    private func derivedOpenBills(from contributions: [MyContribution]) async -> [OpenBill] {
        let mine = contributions.filter { $0.status == "succeeded" }
        let liveIDs = Array(Set(mine.filter { $0.billStatus == "live" }.map(\.billId)))
        let bills = await withTaskGroup(of: Bill?.self) { group in
            for id in liveIDs {
                group.addTask { try? await app.api.bill(id: id) }
            }
            var result: [Bill] = []
            for await bill in group {
                if let bill { result.append(bill) }
            }
            return result
        }
        return bills
            .filter { $0.status == "live" && $0.remainingCents > 0 }
            .map { bill in
                OpenBill(bill: bill, myTotalCents: mine.filter { $0.billId == bill.id }.reduce(0) { $0 + $1.amountCents })
            }
            .sorted { $0.bill.dueDate < $1.bill.dueDate }
    }

    // MARK: Loading

    private func load() async {
        do {
            async let contributionsRequest = app.api.myContributions()
            async let activityRequest = app.api.activity()
            let (loadedContributions, activity) = try await (contributionsRequest, activityRequest)
            let openBills: [OpenBill]
            if let remote = try? await app.api.openBills() {
                openBills = remote
                    .map { OpenBill(bill: $0.bill, myTotalCents: $0.myTotalCents) }
                    .sorted { $0.bill.dueDate < $1.bill.dueDate }
            } else {
                openBills = await derivedOpenBills(from: loadedContributions)
            }

            withAnimation(Motion.settle) {
                contributions = loadedContributions
                open = openBills
                updates = Array(activity.events.prefix(8))
            }
            errorMessage = nil
            if isActive {
                await app.markActivitySeen()
            }
        } catch {
            errorMessage = error.localizedDescription
        }
        hasLoaded = true
    }
}
