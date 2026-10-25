//
//  BillDetailView.swift
//  SwipeBills
//
//  The full bill as one page, grouped by spacing and hairlines rather than boxes inside boxes.
//

import SwiftUI

struct BillDetailView: View {
    let bill: Bill
    let api: APIClient
    let onChipIn: () -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var choosingReportReason = false
    @State private var reportSent = false
    @State private var reportError: String?

    private var canChipIn: Bool { bill.status == "live" && bill.remainingCents > 0 }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                topBar

                Text("\(bill.category.label) · Due \(bill.dueDate.formatted(.dateTime.month(.wide).day()))")
                    .font(Typeface.ui(15, .medium, relativeTo: .subheadline))
                    .foregroundStyle(Color.inkMuted)
                    .padding(.top, 28)
                Text(bill.billerName)
                    .font(Typeface.ui(24, .semibold, relativeTo: .title2))
                    .foregroundStyle(Color.ink)
                    .padding(.top, 4)

                if bill.remainingCents > 0 {
                    AmountText(cents: bill.remainingCents, size: 76)
                        .padding(.top, 12)
                    Text("left to close it")
                        .font(Typeface.ui(16, .medium, relativeTo: .subheadline))
                        .foregroundStyle(Color.inkMuted)
                } else {
                    Text(bill.status == "paid" ? "Payment applied." : "Fully funded.")
                        .font(Typeface.display(52, .black, relativeTo: .largeTitle))
                        .tracking(-2)
                        .foregroundStyle(Color.ink)
                        .padding(.top, 12)
                    Text(bill.status == "paid" ? "Paylee confirmed payment to \(bill.billerName)." : "Paylee will confirm when payment reaches \(bill.billerName).")
                        .font(Typeface.ui(16, .medium, relativeTo: .subheadline))
                        .foregroundStyle(Color.inkMuted)
                }
                ProgressBar(progress: bill.progress, height: 8)
                    .padding(.top, 20)

                facts
                    .padding(.top, 28)

                VStack(alignment: .leading, spacing: 12) {
                    SectionLabel("In \(bill.requesterFirstName)'s words")
                    Text(bill.story)
                        .font(Typeface.ui(21, .regular, relativeTo: .title3))
                        .foregroundStyle(Color.ink)
                        .lineSpacing(5)
                    Text("\(bill.requesterFirstName), \(bill.city)")
                        .font(Typeface.ui(15, .semibold, relativeTo: .subheadline))
                        .foregroundStyle(Color.inkMuted)
                }
                .padding(.top, 40)

                VStack(alignment: .leading, spacing: 18) {
                    SectionLabel("Where your money goes")
                    promise("checkmark", "A person checked this bill, the name on it, and the balance before it went live.")
                    promise("building.columns", "It's paid to \(bill.billerName) directly. It never touches a personal bank account.")
                    promise("arrow.uturn.backward", "Paylee never collects more than the balance. Anything that arrives late is refunded.")
                }
                .padding(.top, 40)

                Button {
                    choosingReportReason = true
                } label: {
                    Text("Report this bill")
                        .font(Typeface.ui(15, .medium, relativeTo: .footnote))
                        .foregroundStyle(Color.inkMuted)
                        .underline()
                }
                .buttonStyle(.plain)
                .padding(.top, 36)
                if let reportError {
                    Text(reportError)
                        .font(Typeface.ui(14, .medium, relativeTo: .footnote))
                        .foregroundStyle(Color.danger)
                        .padding(.top, 6)
                }
            }
            .padding(.horizontal, 22)
            .padding(.top, 12)
            .padding(.bottom, 40)
        }
        .scrollIndicators(.hidden)
        .background(CanvasBackground())
        .statusBarBackdrop()
        .safeAreaInset(edge: .bottom) {
            if canChipIn {
                Button("Chip in") { onChipIn() }
                    .buttonStyle(.primary)
                    .padding(.horizontal, 22)
                    .padding(.top, 12)
                    .background(Color.canvas.ignoresSafeArea())
            }
        }
        .confirmationDialog("Why are you reporting this bill?", isPresented: $choosingReportReason, titleVisibility: .visible) {
            ForEach(ReportReason.allCases) { reason in
                Button(reason.label) {
                    Task { await report(reason) }
                }
            }
        }
        .alert("Thanks for telling us", isPresented: $reportSent) {
            Button("OK") {}
        } message: {
            Text("A reviewer will look at this bill. The person who posted it won't know who reported it.")
        }
    }

    private var topBar: some View {
        HStack(spacing: 10) {
            CircleIconButton(symbol: "chevron.down", label: "Close", size: 40) { dismiss() }
            Spacer()
            ShareLink(
                item: AppConfig.shareURL(for: bill.id),
                subject: Text("Help close a bill on Paylee"),
                message: Text("\(bill.requesterFirstName) has \(Money.format(bill.remainingCents)) left on a \(bill.billerName) bill. Every dollar goes straight to the company.")
            ) {
                Image(systemName: "square.and.arrow.up")
                    .font(.system(size: 14, weight: .bold))
                    .foregroundStyle(Color.ink)
                    .frame(width: 40, height: 40)
                    .background(Circle().fill(Color.raised))
            }
            .buttonStyle(.pressable)
            .accessibilityLabel("Share this bill")
        }
    }

    private var facts: some View {
        VStack(spacing: 0) {
            fact("Amount due", Money.format(bill.amountDueCents))
            Hairline()
            fact("Raised", Money.format(bill.amountRaisedCents))
            Hairline()
            fact("Due", bill.dueDate.formatted(.dateTime.month(.wide).day().year()))
        }
    }

    private func fact(_ label: String, _ value: String) -> some View {
        HStack {
            Text(label)
                .foregroundStyle(Color.inkMuted)
            Spacer()
            Text(value)
                .foregroundStyle(Color.ink)
                .monospacedDigit()
        }
        .font(Typeface.ui(16, .medium, relativeTo: .subheadline))
        .padding(.vertical, 14)
        .accessibilityElement(children: .combine)
    }

    private func promise(_ symbol: String, _ text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 14) {
            Image(systemName: symbol)
                .font(.system(size: 14, weight: .bold))
                .foregroundStyle(Color.lime)
                .frame(width: 20)
                .accessibilityHidden(true)
            Text(text)
                .font(Typeface.ui(16, .regular, relativeTo: .body))
                .foregroundStyle(Color.ink)
                .lineSpacing(3)
        }
    }

    private func report(_ reason: ReportReason) async {
        do {
            try await api.report(billID: bill.id, reason: reason)
            reportError = nil
            reportSent = true
        } catch {
            reportError = error.localizedDescription
        }
    }
}

#Preview {
    Color.clear.sheet(isPresented: .constant(true)) {
        BillDetailView(bill: Bill.samples[1], api: APIClient()) {}
    }
}
