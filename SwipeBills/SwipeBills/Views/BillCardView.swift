//
//  BillCardView.swift
//  SwipeBills
//
//  A bill reads top to bottom the way a giver decides: who gets paid, how much it takes to close, how far
//  along it is, then the person's own words. Never a photo.
//

import SwiftUI

struct BillCardView: View {
    let bill: Bill

    /// At or under this remainder, a single ordinary gift ends the bill.
    static let oneGiftCents = 6_000

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Label(bill.category.label, systemImage: bill.category.symbol)
                    .labelStyle(.titleAndIcon)
                Spacer()
                Text(DuePhrase.text(for: bill.dueDate))
                    .foregroundStyle(DuePhrase.isSoon(bill.dueDate) ? Color.ink : Color.inkMuted)
            }
            .font(Typeface.ui(14, .medium, relativeTo: .subheadline))
            .foregroundStyle(Color.inkMuted)

            Text(bill.billerName)
                .font(Typeface.ui(20, .semibold, relativeTo: .headline))
                .foregroundStyle(Color.ink)
                .lineLimit(1)
                .padding(.top, 28)

            AmountText(cents: bill.remainingCents, size: 66)
                .padding(.top, 2)
            HStack(spacing: 8) {
                Text("left to close it")
                    .foregroundStyle(Color.inkMuted)
                if bill.remainingCents > 0 && bill.remainingCents <= Self.oneGiftCents {
                    Text("One gift finishes it")
                        .foregroundStyle(Color.onLime)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 3)
                        .background(RoundedRectangle(cornerRadius: 6).fill(Color.lime))
                }
            }
            .font(Typeface.ui(15, .medium, relativeTo: .subheadline))

            ProgressBar(progress: bill.progress)
                .padding(.top, 20)
            Text("\(Money.format(bill.amountRaisedCents)) of \(Money.format(bill.amountDueCents)) raised")
                .font(Typeface.ui(13, .medium, relativeTo: .caption))
                .foregroundStyle(Color.inkMuted)
                .monospacedDigit()
                .padding(.top, 8)

            Text(bill.story)
                .font(Typeface.ui(19, .regular, relativeTo: .body))
                .foregroundStyle(Color.ink)
                .lineSpacing(4)
                .lineLimit(5)
                .padding(.top, 28)

            Spacer(minLength: 16)

            HStack(alignment: .center) {
                VStack(alignment: .leading, spacing: 1) {
                    Text(bill.requesterFirstName)
                        .font(Typeface.ui(16, .semibold, relativeTo: .subheadline))
                        .foregroundStyle(Color.ink)
                    Text(bill.city)
                        .font(Typeface.ui(14, .regular, relativeTo: .footnote))
                        .foregroundStyle(Color.inkMuted)
                }
                Spacer()
                Image(systemName: "arrow.up.left.and.arrow.down.right")
                    .font(.system(size: 13, weight: .bold))
                    .foregroundStyle(Color.inkMuted)
                    .frame(width: 36, height: 36)
                    .background(Circle().fill(Color.raised))
                    .accessibilityHidden(true)
            }
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(SurfaceBackground())
        .accessibilityElement(children: .combine)
        .accessibilityHint("Double tap for the full story")
    }
}

#Preview {
    BillCardView(bill: Bill.samples[0])
        .padding()
        .background(CanvasBackground())
}
