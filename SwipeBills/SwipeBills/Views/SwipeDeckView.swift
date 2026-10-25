//
//  SwipeDeckView.swift
//  SwipeBills
//
//  Owns only the drag interaction. The parent owns the bills and decides what a swipe means:
//  right gives, left passes, up closes the whole bill.
//

import SwiftUI

enum SwipeDirection {
    case left, right, up
}

struct SwipeDeckView<Empty: View>: View {
    let bills: [Bill]
    let canRewind: Bool
    /// Shared with the detail screen so the card zooms open into it.
    let transitionNamespace: Namespace.ID
    let onSwipe: (Bill, SwipeDirection) -> Void
    let onTap: (Bill) -> Void
    let onRewind: () -> Void
    @ViewBuilder let empty: () -> Empty

    @State private var dragOffset: CGSize = .zero
    @State private var isSwiping = false
    /// The direction the current drag would commit to if released now; drives stamps and haptics.
    @State private var armedDirection: SwipeDirection?
    @State private var swipeCount = 0

    private let horizontalThreshold: CGFloat = 110
    private let upThreshold: CGFloat = 140
    private let visibleCardCount = 3

    var body: some View {
        VStack(spacing: 20) {
            ZStack {
                if bills.isEmpty {
                    empty()
                }
                // Reversed so the first bill is drawn last, on top of the stack.
                ForEach(Array(bills.prefix(visibleCardCount).enumerated()).reversed(), id: \.element.id) { index, bill in
                    card(for: bill, depth: index)
                }
            }
            .frame(maxHeight: .infinity)
            .padding(.bottom, 20)

            actionButtons
        }
        .sensoryFeedback(.selection, trigger: armedDirection) { _, armed in armed != nil }
        .sensoryFeedback(.impact(weight: .medium), trigger: swipeCount)
    }

    // MARK: Drag state

    private func progress(toward direction: SwipeDirection) -> CGFloat {
        switch direction {
        case .right: min(max(dragOffset.width, 0) / horizontalThreshold, 1)
        case .left: min(max(-dragOffset.width, 0) / horizontalThreshold, 1)
        case .up: abs(dragOffset.width) < -dragOffset.height ? min(max(-dragOffset.height, 0) / upThreshold, 1) : 0
        }
    }

    private var dragProgress: CGFloat {
        max(progress(toward: .left), progress(toward: .right), progress(toward: .up))
    }

    private func direction(for translation: CGSize) -> SwipeDirection? {
        if -translation.height > upThreshold && abs(translation.width) < -translation.height { return .up }
        if translation.width > horizontalThreshold { return .right }
        if translation.width < -horizontalThreshold { return .left }
        return nil
    }

    // MARK: Cards

    private func card(for bill: Bill, depth: Int) -> some View {
        let isTop = depth == 0
        // Cards underneath rise toward the top position as the top card is dragged away.
        let effectiveDepth = CGFloat(depth) - (isTop ? 0 : dragProgress)
        return BillCardView(bill: bill)
            .matchedTransitionSource(id: bill.id, in: transitionNamespace) { source in
                source.clipShape(RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
            }
            .overlay {
                // Cards further back sink into the canvas instead of casting shadows.
                if !isTop {
                    RoundedRectangle(cornerRadius: Radius.card, style: .continuous)
                        .fill(Color.canvas.opacity(0.45 * effectiveDepth))
                }
            }
            .overlay(alignment: .topLeading) {
                if isTop { stamp("Give", filled: true, angle: -10).opacity(progress(toward: .right)) }
            }
            .overlay(alignment: .topTrailing) {
                if isTop { stamp("Pass", filled: false, angle: 10).opacity(progress(toward: .left)) }
            }
            .overlay(alignment: .center) {
                if isTop { stamp("Close it", filled: true, angle: 0).opacity(progress(toward: .up)) }
            }
            // Scaling from the bottom edge keeps each card's bottom in place, so the offset alone sets how far it peeks.
            .scaleEffect(1 - effectiveDepth * 0.05, anchor: .bottom)
            .offset(y: effectiveDepth * 12)
            .offset(isTop ? dragOffset : .zero)
            .rotationEffect(.degrees(isTop ? Double(dragOffset.width / 22) : 0), anchor: .bottom)
            .allowsHitTesting(isTop)
            .onTapGesture { onTap(bill) }
            .gesture(drag)
            .accessibilityHidden(!isTop)
            .accessibilityAction { onTap(bill) }
            .accessibilityAction(named: "Chip in") { swipe(.right) }
            .accessibilityAction(named: "Close it") { swipe(.up) }
            .accessibilityAction(named: "Pass") { swipe(.left) }
    }

    /// A rubber-stamp word that appears as the card is dragged, so the meaning of each direction is visible.
    private func stamp(_ text: String, filled: Bool, angle: Double) -> some View {
        Text(text.uppercased())
            .font(Typeface.display(30, .black, relativeTo: .title))
            .tracking(-0.5)
            .foregroundStyle(filled ? Color.onLime : Color.ink)
            .padding(.horizontal, 16)
            .padding(.vertical, 6)
            .background {
                RoundedRectangle(cornerRadius: 6)
                    .fill(filled ? Color.lime : Color.canvas)
            }
            .overlay {
                if !filled {
                    RoundedRectangle(cornerRadius: 6).strokeBorder(Color.ink, lineWidth: 3)
                }
            }
            .rotationEffect(.degrees(angle))
            .padding(26)
            .accessibilityHidden(true)
    }

    // MARK: Gesture

    private var drag: some Gesture {
        DragGesture()
            .onChanged { value in
                guard !isSwiping else { return }
                dragOffset = value.translation
                armedDirection = direction(for: value.translation)
            }
            .onEnded { value in
                guard !isSwiping else { return }
                armedDirection = nil
                // Predicted end lets a quick flick count as a swipe, not just a long drag.
                if let direction = direction(for: value.predictedEndTranslation) {
                    swipe(direction, momentum: value.predictedEndTranslation)
                } else {
                    withAnimation(Motion.settle) { dragOffset = .zero }
                }
            }
    }

    /// Throws the top card off screen along the way it was moving, then reports the swipe.
    private func swipe(_ direction: SwipeDirection, momentum: CGSize? = nil) {
        guard let top = bills.first, !isSwiping else { return }
        isSwiping = true
        swipeCount += 1
        let exit: CGSize = switch direction {
        case .right, .left:
            CGSize(
                width: direction == .right ? 700 : -700,
                height: momentum.map { max(min($0.height, 400), -400) } ?? dragOffset.height + 60
            )
        case .up:
            CGSize(width: (momentum?.width ?? dragOffset.width) * 0.5, height: -1100)
        }
        withAnimation(.easeOut(duration: 0.26)) {
            dragOffset = exit
        } completion: {
            withAnimation(Motion.settle) {
                onSwipe(top, direction)
                dragOffset = .zero
            }
            isSwiping = false
        }
    }

    // MARK: Buttons

    // Buttons mirror the gestures, and grow as a drag heads their way, the way the card's stamps do.
    private var actionButtons: some View {
        let pass = progress(toward: .left)
        let give = progress(toward: .right)
        let close = progress(toward: .up)
        return HStack(spacing: 16) {
            Button(action: onRewind) {
                Image(systemName: "arrow.uturn.backward")
                    .font(.system(size: 15, weight: .bold))
                    .foregroundStyle(canRewind ? Color.ink : Color.inkFaint)
                    .frame(width: 46, height: 46)
                    .background(Circle().fill(Color.surface))
            }
            .buttonStyle(.pressable)
            .disabled(!canRewind)
            .accessibilityLabel("Rewind last pass")

            Button { swipe(.left) } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 22, weight: .bold))
                    .foregroundStyle(pass > 0.95 ? Color.canvas : Color.ink)
                    .frame(width: 64, height: 64)
                    .background(Circle().fill(pass > 0.95 ? Color.ink : Color.raised))
                    .scaleEffect(1 + 0.12 * pass)
            }
            .buttonStyle(.pressable)
            .accessibilityLabel("Pass")

            Button { swipe(.up) } label: {
                Image(systemName: "arrow.up")
                    .font(.system(size: 19, weight: .heavy))
                    .foregroundStyle(close > 0.95 ? Color.onLime : Color.lime)
                    .frame(width: 54, height: 54)
                    .background(Circle().fill(close > 0.95 ? Color.lime : Color.raised))
                    .scaleEffect(1 + 0.14 * close)
            }
            .buttonStyle(.pressable)
            .accessibilityLabel("Close it: pay off the rest of this bill")

            Button { swipe(.right) } label: {
                Text("$")
                    .font(.custom("ArchivoExpanded-Black", fixedSize: 34))
                    .foregroundStyle(Color.onLime)
                    .frame(width: 78, height: 78)
                    .background(Circle().fill(Color.lime))
                    .scaleEffect(1 + 0.1 * give)
            }
            .buttonStyle(.pressable)
            .accessibilityLabel("Chip in")
        }
        .disabled(bills.isEmpty && !canRewind)
        .opacity(bills.isEmpty ? 0.3 : 1)
        .animation(Motion.quick, value: dragOffset == .zero)
        .animation(Motion.settle, value: bills.isEmpty)
    }
}

#Preview {
    @Previewable @Namespace var namespace
    SwipeDeckView(
        bills: Bill.samples, canRewind: true, transitionNamespace: namespace,
        onSwipe: { _, _ in }, onTap: { _ in }, onRewind: {}
    ) {
        Text("Empty")
    }
    .padding()
    .background(CanvasBackground())
}
