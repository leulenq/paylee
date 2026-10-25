//
//  ImpactView.swift
//  SwipeBills
//

import SwiftUI

struct ImpactView: View {
    /// True while this tab is on screen, so data refreshes each time the giver comes back to it.
    let isActive: Bool

    @Environment(AppModel.self) private var app

    @State private var me: Me?
    @State private var history: [MyContribution] = []
    @State private var updates: [ActivityEvent] = []
    @State private var errorMessage: String?
    @State private var confirmingDelete = false
    @State private var rank: PrivateRank?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                ScreenTitle("Impact")

                if let me {
                    memberCard(me)
                        .padding(.top, 22)
                    summary(me.impact)
                        .padding(.top, 26)
                    tierProgress(me)
                        .padding(.top, 22)
                    if let rank, let place = rank.rank, rank.billsCompleted > 0 {
                        Text(rankSentence(place, of: rank.of, visible: rank.visible))
                            .font(Typeface.ui(15, .regular, relativeTo: .subheadline))
                            .foregroundStyle(Color.inkMuted)
                            .padding(.top, 14)
                    }
                    if !updates.isEmpty {
                        section("Updates") { updatesList }
                    }
                    section("Distinctions") { badgeList(me.badges) }
                } else if let errorMessage {
                    Text(errorMessage)
                        .font(Typeface.ui(16, .regular))
                        .foregroundStyle(Color.inkMuted)
                        .padding(.top, 24)
                } else {
                    RoundedRectangle(cornerRadius: 24, style: .continuous)
                        .fill(Color.surface)
                        .frame(height: 200)
                        .padding(.top, 22)
                }

                section("Practice") {
                    PracticeEditor(
                        practice: Binding(
                            get: { app.settings.practice },
                            set: { app.settings.practice = $0 }
                        ),
                        progress: app.practiceProgress
                    )
                }

                section("Your gifts") { giftList }

                if let me {
                    privateTotal(me.impact)
                        .padding(.top, 20)
                }

                account
                    .padding(.top, 44)
            }
            .padding(.horizontal, 22)
            .padding(.top, 8)
            .padding(.bottom, 32)
        }
        .scrollIndicators(.hidden)
        .background(CanvasBackground())
        .statusBarBackdrop()
        .refreshable { await load() }
        .task(id: isActive) {
            if isActive { await load() }
        }
        .confirmationDialog("Delete your account?", isPresented: $confirmingDelete, titleVisibility: .visible) {
            Button("Delete account", role: .destructive) {
                Task {
                    do {
                        try await app.deleteAccount()
                    } catch {
                        errorMessage = error.localizedDescription
                    }
                }
            }
        } message: {
            Text("Your giving name and badges are removed. Records of payments already made are kept, without your name, because they're financial records.")
        }
    }

    private func load() async {
        do {
            async let meRequest = app.api.me()
            async let historyRequest = app.api.myContributions()
            async let activityRequest = app.api.activity()
            let (loadedMe, loadedHistory, activity) = try await (meRequest, historyRequest, activityRequest)
            let loadedRank = try? await app.api.rank()
            withAnimation(Motion.settle) {
                me = loadedMe
                history = loadedHistory
                updates = Array(activity.events.prefix(5))
                rank = loadedRank
            }
            errorMessage = nil
            // Unread markers stay visible for this visit; the server forgets them now.
            if isActive {
                await app.markActivitySeen()
            }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func section(_ title: String, @ViewBuilder content: () -> some View) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            SectionLabel(title)
            content()
        }
        .padding(.top, 40)
    }

    // MARK: Member card

    private func memberCard(_ me: Me) -> some View {
        let style = TierStyle(name: me.tier.name)
        return VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .top) {
                Text("paylee")
                    .font(Typeface.display(17, .black, relativeTo: .headline))
                    .tracking(-0.5)
                Spacer()
                Text(me.tier.name)
                    .font(Typeface.ui(15, .semibold, relativeTo: .subheadline))
            }
            .foregroundStyle(style.ink)
            Spacer(minLength: 28)
            Text(me.handle)
                .font(Typeface.display(26, .extraBold, relativeTo: .title))
                .tracking(-0.8)
                .foregroundStyle(style.ink)
                .lineLimit(1)
                .minimumScaleFactor(0.6)
            Text("Since \(me.memberSince.formatted(.dateTime.month(.abbreviated).year()))")
                .font(Typeface.ui(14, .medium, relativeTo: .footnote))
                .foregroundStyle(style.inkMuted)
                .padding(.top, 2)
        }
        .padding(22)
        .frame(maxWidth: .infinity, minHeight: 200, alignment: .topLeading)
        .background(TierCardBackground(tier: style))
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(me.tier.name) member card, \(me.handle)")
    }

    /// The giver's record as a sentence, with the numbers set large inside it.
    private func summary(_ impact: Impact) -> some View {
        let helped = impact.billsHelped
        let paid = impact.billsCompleted
        let closed = impact.billsClosed
        let text: Text = if helped == 0 {
            Text("Your first bill is one swipe away.")
        } else {
            Text("You've helped with \(number(helped)) \(helped == 1 ? "bill" : "bills"). \(number(paid)) \(paid == 1 ? "is" : "are") paid off, and you closed \(number(closed)) yourself.")
        }
        return text
            .font(Typeface.ui(22, .medium, relativeTo: .title3))
            .foregroundStyle(Color.inkMuted)
            .lineSpacing(4)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func rankSentence(_ place: Int, of total: Int, visible: Bool) -> String {
        let spot = "\(ordinal(place)) of \(total) this month, by bills finished."
        return visible ? "You're \(spot)" : "You're \(spot) Hidden from everyone else."
    }

    private func ordinal(_ value: Int) -> String {
        let teen = value % 100
        if (11...13).contains(teen) { return "\(value)th" }
        switch value % 10 {
        case 1: return "\(value)st"
        case 2: return "\(value)nd"
        case 3: return "\(value)rd"
        default: return "\(value)th"
        }
    }

    private func number(_ value: Int) -> Text {
        Text("\(value)")
            .font(Typeface.display(24, .black, relativeTo: .title3))
            .foregroundStyle(Color.ink)
    }

    @ViewBuilder
    private func tierProgress(_ me: Me) -> some View {
        if let nextName = me.tier.nextName, let nextAt = me.tier.nextAt {
            let remaining = max(nextAt - me.impact.billsCompleted, 0)
            VStack(alignment: .leading, spacing: 10) {
                Text("\(remaining) more paid-off \(remaining == 1 ? "bill" : "bills") to \(nextName)")
                    .font(Typeface.ui(15, .medium, relativeTo: .subheadline))
                    .foregroundStyle(Color.ink)
                ProgressBar(progress: max(me.tier.progress, 0.02))
            }
            .accessibilityElement(children: .combine)
        }
    }

    private func privateTotal(_ impact: Impact) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text("Total given, visible only to you")
                .font(Typeface.ui(15, .regular, relativeTo: .subheadline))
                .foregroundStyle(Color.inkMuted)
            Spacer()
            Text(Money.format(impact.totalGivenCents))
                .font(Typeface.ui(16, .semibold, relativeTo: .subheadline))
                .foregroundStyle(Color.ink)
                .monospacedDigit()
        }
        .accessibilityElement(children: .combine)
    }

    // MARK: Updates

    private var updatesList: some View {
        VStack(spacing: 0) {
            ForEach(Array(updates.enumerated()), id: \.element.id) { index, event in
                if index > 0 { Hairline() }
                HStack(alignment: .firstTextBaseline, spacing: 12) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(updateTitle(event))
                            .font(Typeface.ui(16, .medium, relativeTo: .subheadline))
                            .foregroundStyle(Color.ink)
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
                }
                .padding(.vertical, 14)
                .accessibilityElement(children: .combine)
            }
        }
    }

    private func updateTitle(_ event: ActivityEvent) -> String {
        let name = event.requesterFirstName
        switch event.kind {
        case "paid":
            return "Paylee paid \(event.billerName) \(Money.format(event.amountCents)) for \(name)."
        case "refunded":
            return "\(Money.format(event.amountCents)) refunded to your original payment method."
        default:
            return event.closedByMe
                ? "You closed \(name)'s \(event.billerName) bill."
                : "\(name)'s \(event.billerName) bill is fully covered."
        }
    }

    // MARK: Distinctions

    private func badgeList(_ badges: [Badge]) -> some View {
        VStack(spacing: 0) {
            ForEach(Array(badges.enumerated()), id: \.element.id) { index, badge in
                if index > 0 { Hairline() }
                HStack(spacing: 14) {
                    Image(systemName: symbol(for: badge.id))
                        .font(.system(size: 16, weight: .bold))
                        .foregroundStyle(badge.earned ? Color.onLime : Color.inkFaint)
                        .frame(width: 40, height: 40)
                        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(badge.earned ? Color.lime : Color.surface))
                    VStack(alignment: .leading, spacing: 2) {
                        Text(badge.title)
                            .font(Typeface.ui(16, .semibold, relativeTo: .subheadline))
                            .foregroundStyle(badge.earned ? Color.ink : Color.inkMuted)
                        Text(badge.detail)
                            .font(Typeface.ui(14, .regular, relativeTo: .footnote))
                            .foregroundStyle(Color.inkMuted)
                    }
                    Spacer()
                }
                .padding(.vertical, 12)
                .accessibilityElement(children: .combine)
                .accessibilityLabel("\(badge.title), \(badge.earned ? "earned" : "not earned yet"). \(badge.detail)")
            }
        }
    }

    private func symbol(for badgeID: String) -> String {
        switch badgeID {
        case "first_bill": "1.circle"
        case "closer": "arrow.up"
        case "five_complete": "5.circle"
        case "finisher": "flag.checkered"
        default: "star"
        }
    }

    // MARK: Gifts

    @ViewBuilder
    private var giftList: some View {
        if history.isEmpty {
            Text("Swipe right on a bill to chip in. Each gift shows up here, with updates until the bill is paid.")
                .font(Typeface.ui(16, .regular, relativeTo: .body))
                .foregroundStyle(Color.inkMuted)
                .padding(.top, 8)
        } else {
            VStack(spacing: 0) {
                ForEach(Array(history.enumerated()), id: \.element.id) { index, item in
                    if index > 0 { Hairline() }
                    HStack(alignment: .firstTextBaseline, spacing: 12) {
                        VStack(alignment: .leading, spacing: 3) {
                            Text("\(item.billerName) for \(item.requesterFirstName)")
                                .font(Typeface.ui(16, .semibold, relativeTo: .subheadline))
                                .foregroundStyle(Color.ink)
                                .lineLimit(1)
                            Text(statusText(item))
                                .font(Typeface.ui(14, .regular, relativeTo: .footnote))
                                .foregroundStyle(item.closedByMe && item.status == "succeeded" ? Color.lime : Color.inkMuted)
                                .lineLimit(2)
                        }
                        Spacer(minLength: 8)
                        Text(Money.format(item.amountCents))
                            .font(Typeface.ui(16, .semibold, relativeTo: .subheadline))
                            .foregroundStyle(item.status == "succeeded" ? Color.ink : Color.inkMuted)
                            .strikethrough(item.status == "refunded")
                            .monospacedDigit()
                    }
                    .padding(.vertical, 14)
                    .accessibilityElement(children: .combine)
                }
            }
        }
    }

    private func statusText(_ item: MyContribution) -> String {
        if item.status == "refunded" { return "Refunded to you" }
        if item.status == "refund_pending" { return "Refund pending confirmation" }
        switch item.billStatus {
        case "paid":
            return item.closedByMe ? "You closed it. Paid to \(item.billerName)." : "Paid to \(item.billerName)."
        case "funded":
            return item.closedByMe ? "You closed it. Awaiting payment confirmation." : "Fully funded. Awaiting payment confirmation."
        case "expired":
            return "Posting ended. Payment or refund is being verified."
        case "removed":
            return "Removed after review"
        default:
            return "\(Money.format(max(item.amountDueCents - item.amountRaisedCents, 0))) left to go"
        }
    }

    // MARK: Account

    private var account: some View {
        HStack(spacing: 10) {
            Button("Sign out") { app.signOut() }
                .buttonStyle(.secondary)
            Button("Delete account") { confirmingDelete = true }
                .buttonStyle(SecondaryButtonStyle(foreground: .danger))
            Spacer()
        }
    }
}
