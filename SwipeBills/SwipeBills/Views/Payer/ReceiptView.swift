//
//  ReceiptView.swift
//  SwipeBills
//
//  Where one gift's money went, step by step. Each step is marked done only when Paylee holds evidence for
//  it (the receipt endpoint sets a timestamp per step), so the screen never claims more than happened.
//

import SwiftUI

struct ReceiptView: View {
    let contribution: MyContribution

    @Environment(AppModel.self) private var app
    @State private var receipt: PaymentReceipt?
    @State private var errorMessage: String?

    private struct Step: Identifiable {
        let title: String
        let detail: String?
        let date: Date?
        var id: String { title }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                BackButton()

                Text(contribution.billerName)
                    .font(Typeface.ui(17, .semibold, relativeTo: .headline))
                    .foregroundStyle(Color.inkMuted)
                    .padding(.top, 24)
                AmountText(cents: contribution.amountCents, size: 64, compactWholeDollars: true)
                Text("Your gift, \(contribution.createdAt.formatted(.dateTime.month(.wide).day()))")
                    .font(Typeface.ui(16, .medium, relativeTo: .subheadline))
                    .foregroundStyle(Color.inkMuted)

                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(steps.enumerated()), id: \.element.id) { index, step in
                        stepRow(step, isLast: index == steps.count - 1)
                    }
                }
                .padding(.top, 36)

                if let errorMessage {
                    Text(errorMessage)
                        .font(Typeface.ui(15, .regular, relativeTo: .footnote))
                        .foregroundStyle(Color.inkMuted)
                        .padding(.top, 12)
                }

                Text("Paylee pays the company through its own billing system and records proof at each step. The person who posted the bill never handles the money, and you won't hear from them. You'll hear from the payment.")
                    .font(Typeface.ui(15, .regular, relativeTo: .footnote))
                    .foregroundStyle(Color.inkMuted)
                    .lineSpacing(2)
                    .padding(.top, 36)
            }
            .padding(.horizontal, 22)
            .padding(.top, 8)
            .padding(.bottom, 32)
        }
        .scrollIndicators(.hidden)
        .background(CanvasBackground())
        .statusBarBackdrop()
        .payleeNavigation()
        .task {
            do {
                receipt = try await app.api.receipt(contributionID: contribution.id)
            } catch {
                errorMessage = "Detailed payment steps aren't available right now."
            }
        }
    }

    private var steps: [Step] {
        let biller = contribution.billerName
        var steps = [Step(title: "You gave \(Money.format(contribution.amountCents))", detail: nil, date: contribution.createdAt)]
        if let refunded = receipt?.refundedAt ?? (contribution.status == "refunded" ? contribution.createdAt : nil) {
            steps.append(Step(title: "Refunded to you", detail: "The bill was finished before your payment landed.", date: refunded))
            return steps
        }
        let covered = ["funded", "paid"].contains(contribution.billStatus)
        steps.append(Step(title: "Bill fully covered", detail: covered ? nil : "\(Money.format(max(contribution.amountDueCents - contribution.amountRaisedCents, 0))) still to go", date: covered ? .distantPast : nil))
        steps.append(Step(title: "Payment sent to \(biller)", detail: nil, date: receipt?.submittedAt))
        steps.append(Step(title: "\(biller) received it", detail: nil, date: receipt?.settledAt))
        let ending = receipt?.accountEnding.map { " ending \($0)" } ?? ""
        steps.append(Step(title: "Applied to the account\(ending)", detail: nil, date: receipt?.appliedAt))
        if let returned = receipt?.returnedAt {
            steps.append(Step(title: "Returned by \(biller)", detail: "Paylee will retry or refund it.", date: returned))
        }
        return steps
    }

    private func stepRow(_ step: Step, isLast: Bool) -> some View {
        let done = step.date != nil
        return HStack(alignment: .top, spacing: 16) {
            VStack(spacing: 0) {
                ZStack {
                    Circle()
                        .fill(done ? Color.lime : Color.canvas)
                        .frame(width: 22, height: 22)
                    if done {
                        Image(systemName: "checkmark")
                            .font(.system(size: 10, weight: .heavy))
                            .foregroundStyle(Color.onLime)
                    } else {
                        Circle().strokeBorder(Color.line, lineWidth: 2).frame(width: 22, height: 22)
                    }
                }
                if !isLast {
                    Rectangle().fill(done ? Color.lime : Color.line).frame(width: 2).frame(minHeight: 36)
                }
            }
            VStack(alignment: .leading, spacing: 3) {
                Text(step.title)
                    .font(Typeface.ui(17, done ? .semibold : .regular, relativeTo: .body))
                    .foregroundStyle(done ? Color.ink : Color.inkMuted)
                if let date = step.date, date != .distantPast {
                    Text(date.formatted(.dateTime.month(.abbreviated).day().hour().minute()))
                        .font(Typeface.ui(14, .regular, relativeTo: .footnote))
                        .foregroundStyle(Color.inkMuted)
                } else if let detail = step.detail {
                    Text(detail)
                        .font(Typeface.ui(14, .regular, relativeTo: .footnote))
                        .foregroundStyle(Color.inkMuted)
                }
            }
            .padding(.bottom, isLast ? 0 : 20)
            Spacer()
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(step.title), \(done ? "done" : "not yet")")
    }
}
