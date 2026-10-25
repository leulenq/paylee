//
//  FeedView.swift
//  SwipeBills
//

import SwiftUI

struct FeedView: View {
    let api: APIClient

    @Environment(AppModel.self) private var app
    @AppStorage("PayleeSeenSwipeHint") private var seenSwipeHint = false
    @Namespace private var cardTransition
    @State private var feed: FeedModel
    @State private var detailBill: Bill?
    @State private var showingPreferences = false
    @State private var contribution: ContributionRequest?
    /// The bill taken off the deck for the contribute sheet. Kept separately because SwiftUI clears
    /// `contribution` before the sheet's onDismiss runs.
    @State private var billInContribution: Bill?
    @State private var didContribute = false
    /// Set when a gift closes a bill. One finish is the point of a session, so the deck steps aside.
    @State private var sessionComplete = false
    @State private var closedThisGift = false
    /// Set when the giver taps "Chip in" on the detail screen; the contribute sheet opens once it closes.
    @State private var chipInAfterDetail: Bill?

    /// A contribute sheet to show: which bill, and whether to start at the full remaining amount (swipe up).
    private struct ContributionRequest: Identifiable {
        let bill: Bill
        let closeIt: Bool
        var id: UUID { bill.id }
    }

    init(api: APIClient) {
        self.api = api
        _feed = State(initialValue: FeedModel(api: api))
    }

    var body: some View {
        VStack(spacing: 18) {
            header
            content
        }
        .padding(.horizontal, 20)
        .padding(.top, 8)
        .padding(.bottom, 12)
        .background(CanvasBackground())
        .task {
            if !feed.hasLoaded {
                await feed.load()
                #if DEBUG
                await openDebugScreen()
                #endif
            }
        }
        // Read the link once and clear it before any await. Clearing it inside a `.task(id:)` keyed on it
        // would cancel that very task, which dropped links for bills that had to be fetched.
        .onChange(of: app.linkedBillID, initial: true) { _, id in
            guard let id else { return }
            app.linkedBillID = nil
            Task { await openLinkedBill(id) }
        }
        .fullScreenCover(item: $detailBill, onDismiss: openPendingContribution) { bill in
            BillDetailView(bill: bill, api: api) {
                chipInAfterDetail = bill
                detailBill = nil
            }
            .navigationTransition(.zoom(sourceID: bill.id, in: cardTransition))
        }
        .sheet(item: $contribution, onDismiss: contributionSheetClosed) { request in
            ContributeSheet(
                bill: request.bill,
                api: api,
                startsWithFullAmount: request.closeIt,
                monthlyLimitCents: app.settings.practice?.monthlyLimitCents,
                spentThisMonthCents: app.practiceProgress?.spentCentsThisMonth ?? 0,
                showsCity: app.settings.showCityOnShareCards
            ) { closed in
                didContribute = true
                closedThisGift = closed
            }
        }
        .sheet(isPresented: $showingPreferences) {
            PreferencesSheet(initial: feed.preferences) { preferences in
                Task { await feed.updatePreferences(preferences) }
            }
        }
        #if DEBUG
        .overlay {
            if let debugShareCard {
                debugShareCard
                    .resizable()
                    .scaledToFit()
                    .padding(24)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .background(Color.black.opacity(0.6))
                    .onTapGesture { self.debugShareCard = nil }
            }
        }
        #endif
    }

    #if DEBUG
    @State private var debugShareCard: Image?

    /// `-PayleeOpenSheet detail|contribute|closeit|filters|sharecard` opens a screen for the top bill, for screenshots.
    private func openDebugScreen() async {
        guard let sheet = UserDefaults.standard.string(forKey: "PayleeOpenSheet"), sheet != "none" else { return }
        // Presenting before the first frame is on screen is silently ignored.
        try? await Task.sleep(for: .milliseconds(700))
        if sheet == "filters" {
            showingPreferences = true
            return
        }
        guard let top = feed.bills.first else { return }
        switch sheet {
        case "detail": detailBill = top
        case "contribute": startContribution(top, closeIt: false)
        case "closeit": startContribution(top, closeIt: true)
        case "sharecard": debugShareCard = ShareCard(category: top.category, city: top.city, amountCents: top.amountDueCents, closedBill: true, showsCity: app.settings.showCityOnShareCards).renderImage()
        default: break
        }
    }
    #endif

    private var header: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                PayleeWordmark()
                Spacer()
                filterButton
            }
            if let sentence = feed.placeSentence {
                Text(sentence)
                    .font(Typeface.ui(15, .regular, relativeTo: .subheadline))
                    .foregroundStyle(Color.inkMuted)
            }
            if let appointment = appointmentLine {
                Text(appointment)
                    .font(Typeface.ui(15, .semibold, relativeTo: .subheadline))
                    .foregroundStyle(Color.ink)
            } else if let season = feed.season {
                Button(action: toggleSeason) {
                    Text(showingSeason ? "Showing \(season.title)'s \(season.detail)" : "\(season.title) · \(season.detail)")
                        .font(Typeface.ui(15, .semibold, relativeTo: .subheadline))
                        .foregroundStyle(Color.ink)
                        .multilineTextAlignment(.leading)
                }
                .buttonStyle(.plain)
                .accessibilityHint(showingSeason ? "Clears the month filter" : "Shows this month's bills")
            }
        }
    }

    private var filterButton: some View {
        Button {
                showingPreferences = true
            } label: {
                HStack(spacing: 8) {
                    Text(filterSummary)
                        .font(Typeface.ui(14, .semibold, relativeTo: .footnote))
                        .lineLimit(1)
                    Image(systemName: "chevron.down")
                        .font(.system(size: 11, weight: .bold))
                }
                .foregroundStyle(Color.ink)
                .padding(.horizontal, 14)
                .frame(height: 38)
                .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color.surface))
            }
            .buttonStyle(.pressable)
            .accessibilityLabel("Filters, \(filterSummary)")
    }

    private var showingSeason: Bool {
        guard let season = feed.season else { return false }
        return feed.preferences.categories == [season.category]
    }

    private var appointmentLine: String? {
        guard let practice = app.settings.practice else { return nil }
        let calendar = Calendar.current
        switch practice.startDay {
        case .sunday:
            return calendar.component(.weekday, from: .now) == 1 ? "It's Sunday. Your deck is here." : nil
        case .payday:
            return calendar.component(.weekday, from: .now) == 6 ? "It's Friday. Your deck is here." : nil
        case .firstOfMonth:
            return calendar.component(.day, from: .now) == 1 ? "It's the first. Your deck is here." : nil
        }
    }

    private func toggleSeason() {
        guard let season = feed.season else { return }
        var preferences = feed.preferences
        preferences.categories = showingSeason ? [] : [season.category]
        Task { await feed.updatePreferences(preferences) }
    }

    private var filterSummary: String {
        let preferences = feed.preferences
        switch preferences.activeFilterCount {
        case 0: return "All bills"
        case 1:
            if let state = preferences.state { return USState.name(for: state) }
            if let max = preferences.maxRemainingCents { return "Under \(Money.short(max))" }
            return preferences.categories.count == 1 ? preferences.categories.first!.label : "\(preferences.categories.count) kinds"
        default: return "\(preferences.activeFilterCount) filters"
        }
    }

    @ViewBuilder
    private var content: some View {
        if !feed.hasLoaded {
            LoadingCard()
        } else if sessionComplete {
            SessionComplete(title: sessionTitle) {
                withAnimation(Motion.settle) { sessionComplete = false }
            }
        } else if let error = feed.errorMessage, feed.bills.isEmpty {
            DeckMessage(
                title: "Can't reach Paylee.",
                message: error,
                action: ("Try again", { Task { await feed.load() } })
            )
        } else {
            SwipeDeckView(
                bills: feed.bills,
                canRewind: feed.lastPassed != nil,
                transitionNamespace: cardTransition,
                onSwipe: handleSwipe,
                onTap: { detailBill = $0 },
                onRewind: { withAnimation(Motion.settle) { feed.rewind() } }
            ) {
                if feed.preferences.activeFilterCount > 0 {
                    DeckMessage(
                        title: "Nothing matches.",
                        message: "No live bills fit your filters right now. Widen them, or check back as new bills go live.",
                        action: ("Change filters", { showingPreferences = true })
                    )
                } else {
                    DeckMessage(
                        title: "That's every bill.",
                        message: "New bills go live as others get paid off.",
                        action: ("Check again", { Task { await feed.load() } })
                    )
                }
            }
            .overlay(alignment: .bottom) {
                if !seenSwipeHint && !feed.bills.isEmpty {
                    SwipeHint { withAnimation(Motion.settle) { seenSwipeHint = true } }
                        .padding(.bottom, 132)
                        .transition(.opacity)
                }
            }
        }
    }

    /// Presents a bill opened from a shared link or notification, whether or not it's in this giver's deck.
    private func openLinkedBill(_ id: UUID) async {
        if let bill = feed.bills.first(where: { $0.id == id }) {
            detailBill = bill
        } else if let bill = try? await api.bill(id: id) {
            detailBill = bill
        }
    }

    private func handleSwipe(_ bill: Bill, _ direction: SwipeDirection) {
        if !seenSwipeHint {
            withAnimation { seenSwipeHint = true }
        }
        switch direction {
        case .left:
            feed.pass(bill)
        case .right:
            startContribution(bill, closeIt: false)
        case .up:
            startContribution(bill, closeIt: true)
        }
    }

    private func startContribution(_ bill: Bill, closeIt: Bool) {
        feed.remove(bill)
        didContribute = false
        closedThisGift = false
        billInContribution = bill
        contribution = ContributionRequest(bill: bill, closeIt: closeIt)
    }

    private func openPendingContribution() {
        guard let bill = chipInAfterDetail else { return }
        chipInAfterDetail = nil
        startContribution(bill, closeIt: false)
    }

    private func contributionSheetClosed() {
        // A giver who backs out gets the bill back on top of the deck.
        if let bill = billInContribution, !didContribute {
            feed.putBack(bill)
        }
        billInContribution = nil
        let closed = didContribute && closedThisGift
        if didContribute {
            Task {
                await app.refreshPracticeProgress()
                if closed {
                    withAnimation(Motion.settle) { sessionComplete = true }
                }
                await app.checkForTierUp()
            }
        }
    }

    private var sessionTitle: String {
        guard let practice = app.settings.practice, let progress = app.practiceProgress else {
            return "That's the deck for tonight."
        }
        if progress.complete {
            return practice.cadence == .weekly ? "That's your bill for the week." : "That's your bill for the month."
        }
        return "\(progress.finishedThisPeriod) of \(progress.target) \(progress.periodLabel)."
    }
}

/// The end of a session. One finished bill is a complete visit; the app says so and steps aside, and
/// "keep looking" stays available without being pushed.
private struct SessionComplete: View {
    let title: String
    let onContinue: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Spacer()
            Text(title)
                .font(Typeface.display(40, .black, relativeTo: .largeTitle))
                .tracking(-1.4)
                .foregroundStyle(Color.ink)
                .fixedSize(horizontal: false, vertical: true)
            Text("You finished a bill. We'll tell you when the company confirms the payment.")
                .font(Typeface.ui(17, .regular, relativeTo: .body))
                .foregroundStyle(Color.inkMuted)
                .lineSpacing(3)
            Button("Keep looking", action: onContinue)
                .buttonStyle(.secondary)
                .padding(.top, 8)
            Spacer()
            Spacer()
        }
        .padding(.horizontal, 8)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
        .padding(.bottom, 100)
    }

}

/// First-run coaching: a hand sways over the deck until the giver swipes once.
private struct SwipeHint: View {
    let onDismiss: () -> Void
    @State private var sway = false

    var body: some View {
        Button(action: onDismiss) {
            HStack(spacing: 12) {
                Image(systemName: "hand.point.up.left.fill")
                    .font(.system(size: 18))
                    .foregroundStyle(Color.lime)
                    .offset(x: sway ? 8 : -8)
                Text("Swipe right to give. Left just tunes your deck.")
                    .font(Typeface.ui(15, .semibold, relativeTo: .subheadline))
                    .foregroundStyle(Color.ink)
            }
            .padding(.horizontal, 18)
            .padding(.vertical, 13)
            .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Color.raised))
        }
        .buttonStyle(.pressable)
        .onAppear {
            withAnimation(.easeInOut(duration: 0.9).repeatForever()) { sway = true }
        }
        .accessibilityLabel("Swipe right on a bill to give, left to pass. Double tap to dismiss.")
    }
}

/// Placeholder card shaped like a bill while the feed loads.
private struct LoadingCard: View {
    @State private var dim = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            block(width: 120, height: 14)
            block(width: 180, height: 20).padding(.top, 20)
            block(width: 230, height: 58)
            block(width: nil, height: 6).padding(.top, 14)
            Spacer()
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(SurfaceBackground())
        .opacity(dim ? 0.55 : 1)
        .animation(.easeInOut(duration: 0.9).repeatForever(), value: dim)
        .onAppear { dim = true }
        .padding(.bottom, 104)
        .accessibilityLabel("Loading bills")
    }

    private func block(width: CGFloat?, height: CGFloat) -> some View {
        RoundedRectangle(cornerRadius: 4)
            .fill(Color.raised)
            .frame(width: width, height: height)
            .frame(maxWidth: width == nil ? .infinity : nil, alignment: .leading)
    }
}

/// Empty and error states, set where the deck would be. Big type, left-aligned, one action.
private struct DeckMessage: View {
    let title: String
    let message: String
    let action: (String, () -> Void)

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Spacer()
            Text(title)
                .font(Typeface.display(40, .black, relativeTo: .largeTitle))
                .tracking(-1.2)
                .foregroundStyle(Color.ink)
            Text(message)
                .font(Typeface.ui(17, .regular, relativeTo: .body))
                .foregroundStyle(Color.inkMuted)
                .lineSpacing(3)
            Button(action.0, action: action.1)
                .buttonStyle(.secondary)
                .padding(.top, 8)
            Spacer()
            Spacer()
        }
        .padding(.horizontal, 8)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
    }
}

#Preview {
    FeedView(api: APIClient())
        .environment(AppModel())
}
