//
//  ContributeSheet.swift
//  SwipeBills
//
//  Where money moves. The amount is the whole screen; it's entered on a built-in keypad, not the
//  system keyboard. A completed gift ends on a full-bleed lime screen built from type alone.
//

import AuthenticationServices
import SwiftUI

struct ContributeSheet: View {
    let bill: Bill
    let api: APIClient
    /// Called once money has been counted toward the bill, with whether this gift closed it.
    let onContributed: (_ closedBill: Bool) -> Void
    /// The monthly amount they set aside, and what they've already given this month. Nil limit means no note.
    let monthlyLimitCents: Int?
    let spentThisMonthCents: Int
    let showsCity: Bool

    @Environment(\.dismiss) private var dismiss
    @Environment(\.webAuthenticationSession) private var webAuthenticationSession

    @State private var availableCents: Int
    /// What the giver has typed, in dollars, e.g. "25" or "12.5".
    @State private var entry = ""
    @State private var phase: Phase = .choosing
    @State private var errorMessage: String?

    private enum Phase: Equatable {
        case choosing
        case paying
        case done(amountCents: Int, closedBill: Bool, stillProcessing: Bool)
    }

    /// `startsWithFullAmount` pre-fills the remaining balance, for a swipe up ("Close it").
    init(
        bill: Bill,
        api: APIClient,
        startsWithFullAmount: Bool = false,
        monthlyLimitCents: Int? = nil,
        spentThisMonthCents: Int = 0,
        showsCity: Bool = true,
        onContributed: @escaping (_ closedBill: Bool) -> Void
    ) {
        self.bill = bill
        self.api = api
        self.onContributed = onContributed
        self.monthlyLimitCents = monthlyLimitCents
        self.spentThisMonthCents = spentThisMonthCents
        self.showsCity = showsCity
        _availableCents = State(initialValue: bill.remainingCents)
        _entry = State(initialValue: startsWithFullAmount ? Self.entryText(for: bill.remainingCents) : "")
    }

    var body: some View {
        ZStack {
            switch phase {
            case .choosing, .paying:
                chooser
                    .transition(.opacity)
            case let .done(amountCents, closedBill, stillProcessing):
                SuccessView(
                    bill: bill,
                    amountCents: amountCents,
                    closedBill: closedBill,
                    stillProcessing: stillProcessing
                ) { dismiss() }
                .transition(.opacity)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background((isDone ? Color.lime : Color.surface).ignoresSafeArea())
        .presentationBackground(isDone ? Color.lime : Color.surface)
        .presentationCornerRadius(Radius.sheet)
        .presentationDragIndicator(isDone ? .hidden : .visible)
        .interactiveDismissDisabled(phase == .paying)
        .animation(Motion.settle, value: isDone)
    }

    private var isDone: Bool {
        if case .done = phase { return true }
        return false
    }

    // MARK: Choosing an amount

    private var amountCents: Int? { Money.parseCents(entry) }

    private var validationMessage: String? {
        guard let cents = amountCents else { return nil }
        if cents > availableCents { return "Only \(Money.format(availableCents)) is left on this bill." }
        if cents < AppConfig.minimumContributionCents && cents != availableCents {
            return "The minimum is \(Money.short(AppConfig.minimumContributionCents))."
        }
        return nil
    }

    private var canGive: Bool {
        amountCents != nil && validationMessage == nil && phase == .choosing && availableCents > 0
    }

    private var presets: [Int] {
        [1_000, 2_500, 5_000].filter { $0 < availableCents && $0 >= AppConfig.minimumContributionCents }
    }

    private var chooser: some View {
        VStack(spacing: 0) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(bill.billerName)
                        .font(Typeface.ui(19, .semibold, relativeTo: .headline))
                        .foregroundStyle(Color.ink)
                    Text("For \(bill.requesterFirstName) · \(Money.format(availableCents)) left")
                        .font(Typeface.ui(15, .regular, relativeTo: .subheadline))
                        .foregroundStyle(Color.inkMuted)
                        .monospacedDigit()
                }
                Spacer()
                CircleIconButton(symbol: "xmark", label: "Cancel", size: 36) { dismiss() }
                    .disabled(phase == .paying)
            }
            .padding(.top, 26)

            Spacer(minLength: 8)

            amountDisplay

            Spacer(minLength: 8)

            presetRow
                .padding(.bottom, 14)

            Keypad(entry: $entry)
                .disabled(phase == .paying)

            Button {
                guard let amountCents else { return }
                Task { await give(amountCents) }
            } label: {
                if phase == .paying {
                    ProgressView().tint(Color.onLime)
                } else {
                    Text(amountCents.map { "Give \(Money.short($0))" } ?? "Enter an amount")
                }
            }
            .buttonStyle(.primary)
            .disabled(!canGive)
            .padding(.top, 12)

            Text("All of it goes to \(bill.billerName). Gifts to a person aren't tax-deductible.")
                .font(Typeface.ui(12, .regular, relativeTo: .caption))
                .foregroundStyle(Color.inkMuted)
                .multilineTextAlignment(.center)
                .padding(.top, 12)
                .padding(.bottom, 4)
        }
        .padding(.horizontal, 22)
    }

    private var amountDisplay: some View {
        VStack(spacing: 12) {
            Text(entry.isEmpty ? "$0" : "$\(entry)")
                .font(Typeface.display(88, .extraBold))
                .tracking(-3)
                .monospacedDigit()
                .foregroundStyle(entry.isEmpty ? Color.inkFaint : Color.ink)
                .contentTransition(.numericText())
                .animation(Motion.quick, value: entry)
                .minimumScaleFactor(0.4)
                .lineLimit(1)
                .accessibilityLabel(amountCents.map { Money.format($0) } ?? "No amount entered")
            Group {
                if let message = validationMessage ?? errorMessage {
                    Text(message).foregroundStyle(Color.danger)
                } else {
                    VStack(spacing: 4) {
                        Text("Paid straight to the company").foregroundStyle(Color.inkMuted)
                        if let note = ceilingNote {
                            Text(note).foregroundStyle(Color.ink)
                        }
                    }
                }
            }
            .font(Typeface.ui(15, .medium, relativeTo: .footnote))
            .multilineTextAlignment(.center)
            .frame(minHeight: 40, alignment: .top)
        }
    }

    private var presetRow: some View {
        HStack(spacing: 8) {
            ForEach(presets, id: \.self) { cents in
                presetButton(Money.short(cents), cents: cents, isClose: false)
            }
            if availableCents > 0 {
                presetButton("Close it", cents: availableCents, isClose: true)
            }
        }
    }

    private func presetButton(_ title: String, cents: Int, isClose: Bool) -> some View {
        let isSelected = amountCents == cents
        return Button {
            entry = Self.entryText(for: cents)
            errorMessage = nil
        } label: {
            Text(title)
                .font(Typeface.ui(15, .semibold, relativeTo: .subheadline))
                .lineLimit(1)
                .fixedSize(horizontal: isClose, vertical: false)
                .foregroundStyle(isSelected ? Color.canvas : (isClose ? Color.lime : Color.ink))
                .padding(.horizontal, isClose ? 16 : 0)
                .frame(maxWidth: isClose ? nil : .infinity, minHeight: 42)
                .background(RoundedRectangle(cornerRadius: Radius.chip + 2, style: .continuous).fill(isSelected ? Color.ink : Color.raised))
        }
        .buttonStyle(.pressable)
        .disabled(phase == .paying)
        .accessibilityLabel(isClose ? "Close it, \(Money.format(cents))" : Money.format(cents))
        .accessibilityAddTraits(isSelected ? .isSelected : [])
    }

    /// Shown when this gift would pass the amount they set aside. The button still pays.
    private var ceilingNote: String? {
        guard let limit = monthlyLimitCents, let cents = amountCents, spentThisMonthCents + cents > limit else { return nil }
        let month = Date.now.formatted(.dateTime.month(.wide))
        return "This goes past the \(Money.short(limit)) you set aside for \(month). You can still pay it."
    }

    private static func entryText(for cents: Int) -> String {
        cents % 100 == 0 ? "\(cents / 100)" : String(format: "%d.%02d", cents / 100, cents % 100)
    }

    // MARK: Paying

    private func give(_ cents: Int) async {
        phase = .paying
        errorMessage = nil
        do {
            let result = try await api.contribute(billID: bill.id, amountCents: cents)
            guard let checkoutURL = result.checkoutUrl else {
                finish(amountCents: cents, closedBill: result.closedBill, stillProcessing: false)
                return
            }
            let callback: URL
            do {
                callback = try await webAuthenticationSession.authenticate(
                    using: checkoutURL, callbackURLScheme: AppConfig.checkoutCallbackScheme
                )
            } catch {
                await releaseHold(result.contribution.id)
                throw error
            }
            let outcome = URLComponents(url: callback, resolvingAgainstBaseURL: false)?
                .queryItems?.first { $0.name == "result" }?.value
            guard outcome == "success" else {
                await releaseHold(result.contribution.id)
                showCanceled()
                return
            }
            try await waitForSettlement(contributionID: result.contribution.id, amountCents: cents)
        } catch let error as APIError {
            phase = .choosing
            errorMessage = error.message
            if let available = error.availableCents {
                availableCents = available
            }
        } catch ASWebAuthenticationSessionError.canceledLogin {
            showCanceled()
        } catch {
            phase = .choosing
            errorMessage = error.localizedDescription
        }
    }

    /// Stripe tells the server about the payment by webhook, which can land a moment after the browser returns.
    private func waitForSettlement(contributionID: UUID, amountCents: Int) async throws {
        for _ in 0..<10 {
            let status = try await api.contribution(id: contributionID)
            switch status.contribution.status {
            case "succeeded":
                finish(amountCents: amountCents, closedBill: status.closedBill, stillProcessing: false)
                return
            case "refund_pending", "refunded":
                throw APIError.local("This bill filled up while you were paying, so your payment is being refunded.")
            case "canceled":
                throw APIError.local("The payment didn't go through. Nothing was charged.")
            default:
                try await Task.sleep(for: .seconds(1))
            }
        }
        finish(amountCents: amountCents, closedBill: false, stillProcessing: true)
    }

    private func releaseHold(_ contributionID: UUID) async {
        try? await api.cancelContribution(id: contributionID)
    }

    private func showCanceled() {
        phase = .choosing
        errorMessage = "Payment canceled. Nothing was charged."
    }

    private func finish(amountCents: Int, closedBill: Bool, stillProcessing: Bool) {
        withAnimation(Motion.settle) {
            phase = .done(amountCents: amountCents, closedBill: closedBill, stillProcessing: stillProcessing)
        }
        onContributed(closedBill)
    }
}

// MARK: - Keypad

private struct Keypad: View {
    @Binding var entry: String

    private enum Key: Hashable {
        case digit(Character), dot, delete
    }

    private let rows: [[Key]] = [
        [.digit("1"), .digit("2"), .digit("3")],
        [.digit("4"), .digit("5"), .digit("6")],
        [.digit("7"), .digit("8"), .digit("9")],
        [.dot, .digit("0"), .delete],
    ]

    var body: some View {
        Grid(horizontalSpacing: 6, verticalSpacing: 4) {
            ForEach(rows, id: \.self) { row in
                GridRow {
                    ForEach(row, id: \.self) { key in
                        Button { press(key) } label: { label(for: key) }
                            .buttonStyle(KeyStyle())
                            .accessibilityLabel(accessibilityLabel(for: key))
                    }
                }
            }
        }
        .sensoryFeedback(.selection, trigger: entry)
    }

    @ViewBuilder
    private func label(for key: Key) -> some View {
        switch key {
        case let .digit(character):
            Text(String(character)).font(Typeface.ui(28, .medium, relativeTo: .title2))
        case .dot:
            Text(".").font(Typeface.ui(28, .bold, relativeTo: .title2))
        case .delete:
            Image(systemName: "delete.left").font(.system(size: 22, weight: .regular))
        }
    }

    private func accessibilityLabel(for key: Key) -> String {
        switch key {
        case let .digit(character): String(character)
        case .dot: "Decimal point"
        case .delete: "Delete"
        }
    }

    private func press(_ key: Key) {
        switch key {
        case let .digit(character):
            if let dot = entry.firstIndex(of: ".") {
                // At most two digits after the decimal point.
                guard entry.distance(from: dot, to: entry.endIndex) <= 2 else { return }
            } else if entry == "0" {
                entry = ""
            }
            guard entry.count < 8 else { return }
            entry.append(character)
        case .dot:
            guard !entry.contains(".") else { return }
            entry = entry.isEmpty ? "0." : entry + "."
        case .delete:
            if !entry.isEmpty { entry.removeLast() }
        }
    }

    private struct KeyStyle: ButtonStyle {
        func makeBody(configuration: Configuration) -> some View {
            configuration.label
                .foregroundStyle(Color.ink)
                .frame(maxWidth: .infinity, minHeight: 54)
                .background {
                    RoundedRectangle(cornerRadius: 14, style: .continuous)
                        .fill(Color.raised.opacity(configuration.isPressed ? 1 : 0))
                }
                .contentShape(Rectangle())
                .animation(Motion.quick, value: configuration.isPressed)
        }
    }
}

// MARK: - Success

/// Full-bleed lime. The amount comes up huge, then one line on what happens next. No confetti: the
/// colour change and the type carry it.
private struct SuccessView: View {
    let bill: Bill
    let amountCents: Int
    let closedBill: Bool
    let stillProcessing: Bool
    let onDone: () -> Void

    @State private var shown = false
    @State private var shareImage: Image?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Spacer()

            Text(closedBill ? "Closed." : "Sent.")
                .font(Typeface.display(28, .black, relativeTo: .title))
                .foregroundStyle(Color.onLime)
                .opacity(shown ? 1 : 0)
                .offset(y: shown ? 0 : 20)

            AmountText(
                cents: closedBill ? bill.amountDueCents : amountCents,
                size: 96, color: .onLime, centsColor: Color.onLime.opacity(0.45), compactWholeDollars: true
            )
            .opacity(shown ? 1 : 0)
            .offset(y: shown ? 0 : 40)

            Text(message)
                .font(Typeface.ui(19, .medium, relativeTo: .body))
                .foregroundStyle(Color.onLime)
                .lineSpacing(3)
                .padding(.top, 18)
                .opacity(shown ? 1 : 0)

            Spacer()
            Spacer()

            VStack(spacing: 10) {
                Button(action: onDone) {
                    Text("Back to bills")
                        .font(Typeface.ui(17, .bold, relativeTo: .headline))
                        .foregroundStyle(Color.lime)
                        .frame(maxWidth: .infinity, minHeight: 58)
                        .background(RoundedRectangle(cornerRadius: Radius.control, style: .continuous).fill(Color.onLime))
                }
                .buttonStyle(.pressable)
                if let shareImage {
                    ShareLink(
                        item: shareImage,
                        preview: SharePreview(closedBill ? "I helped fully fund a stranger's bill" : "I chipped in on a stranger's bill", image: shareImage)
                    ) {
                        Label("Share it", systemImage: "square.and.arrow.up")
                            .font(Typeface.ui(16, .semibold, relativeTo: .subheadline))
                            .foregroundStyle(Color.onLime)
                            .frame(maxWidth: .infinity, minHeight: 48)
                    }
                }
            }
            .opacity(shown ? 1 : 0)
            .padding(.bottom, 8)
        }
        .padding(.horizontal, 26)
        .frame(maxWidth: .infinity, alignment: .leading)
        .sensoryFeedback(.success, trigger: shown) { _, now in now }
        .onAppear {
            shareImage = ShareCard(
                category: bill.category,
                city: bill.city,
                amountCents: closedBill ? bill.amountDueCents : amountCents,
                closedBill: closedBill,
                showsCity: showsCity
            ).renderImage()
            withAnimation(.smooth(duration: 0.55).delay(0.1)) { shown = true }
        }
    }

    private var message: String {
        if stillProcessing {
            return "Your payment is processing. It'll show on the bill in a moment."
        }
        if closedBill {
            return "\(bill.requesterFirstName)'s \(bill.billerName) bill is paid in full. We pay \(bill.billerName) directly and tell you when it's done."
        }
        return "Straight to \(bill.billerName) for \(bill.requesterFirstName). We'll tell you when the bill is paid off."
    }
}

#Preview {
    Color.clear.sheet(isPresented: .constant(true)) {
        ContributeSheet(bill: Bill.samples[0], api: APIClient()) { _ in }
    }
}
