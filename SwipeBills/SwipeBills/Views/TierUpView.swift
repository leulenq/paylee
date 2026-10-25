//
//  TierUpView.swift
//  SwipeBills
//
//  The moment a giver reaches a new member tier: their new card slides up in its new finish, and the
//  tier name is set as large as the screen allows.
//

import SwiftUI

struct TierUpView: View {
    let me: Me
    let onDone: () -> Void

    @State private var cardShown = false
    @State private var textShown = false

    private var style: TierStyle { TierStyle(name: me.tier.name) }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Spacer()

            Text("You're")
                .font(Typeface.ui(22, .medium, relativeTo: .title3))
                .foregroundStyle(Color.inkMuted)
                .opacity(textShown ? 1 : 0)
            Text("\(me.tier.name).")
                .font(Typeface.display(92, .black, relativeTo: .largeTitle))
                .tracking(-4)
                .foregroundStyle(Color.ink)
                .lineLimit(1)
                .minimumScaleFactor(0.5)
                .opacity(textShown ? 1 : 0)
                .offset(y: textShown ? 0 : 24)

            card
                .padding(.top, 28)
                .offset(y: cardShown ? 0 : 260)
                .opacity(cardShown ? 1 : 0)

            Text(detail)
                .font(Typeface.ui(18, .regular, relativeTo: .body))
                .foregroundStyle(Color.inkMuted)
                .lineSpacing(3)
                .padding(.top, 24)
                .opacity(textShown ? 1 : 0)

            Spacer()

            Button("Continue", action: onDone)
                .buttonStyle(.primary)
                .opacity(textShown ? 1 : 0)
                .padding(.bottom, 12)
        }
        .padding(.horizontal, 24)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
        .background(CanvasBackground())
        .sensoryFeedback(.success, trigger: cardShown) { _, shown in shown }
        .onAppear {
            withAnimation(.smooth(duration: 0.7).delay(0.15)) { cardShown = true }
            withAnimation(.smooth(duration: 0.5).delay(0.45)) { textShown = true }
        }
    }

    private var card: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("paylee")
                    .font(Typeface.display(16, .black, relativeTo: .headline))
                    .tracking(-0.5)
                Spacer()
                Text(me.tier.name)
                    .font(Typeface.ui(15, .semibold, relativeTo: .subheadline))
            }
            .foregroundStyle(style.ink)
            Spacer(minLength: 20)
            Text(me.handle)
                .font(Typeface.display(24, .extraBold, relativeTo: .title))
                .tracking(-0.6)
                .foregroundStyle(style.ink)
                .lineLimit(1)
                .minimumScaleFactor(0.6)
        }
        .padding(22)
        // A card's own proportions (ISO ID-1, 85.6 x 54 mm), so it never stretches to fill the screen.
        .aspectRatio(85.6 / 54, contentMode: .fit)
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .background(TierCardBackground(tier: style))
        .accessibilityElement(children: .combine)
    }

    private var detail: String {
        guard let next = me.tier.nextName, let nextAt = me.tier.nextAt else {
            return "The top tier. \(me.impact.billsCompleted) bills paid off with your help."
        }
        let remaining = max(nextAt - me.impact.billsCompleted, 0)
        return "\(me.impact.billsCompleted) bills paid off with your help. \(remaining) more to \(next)."
    }
}
