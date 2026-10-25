//
//  Theme.swift
//  SwipeBills
//
//  Paylee is always dark: a tinted near-black canvas, off-white ink, and one accent (lime) reserved for
//  the things you act on and the progress you make. No gradients, glows, or drop shadows; depth comes
//  from three surface steps (canvas, surface, raised) and from type.
//
//  Colors live in Assets.xcassets and are used through generated symbols: Color.canvas, .surface,
//  .raised, .line, .ink, .inkMuted, .inkFaint, .lime, .onLime, .danger, .tierGold, .tierPlatinum.
//

import SwiftUI

/// Corner radii differ by role on purpose, so cards, controls, and chips don't all look the same.
enum Radius {
    static let card: CGFloat = 28
    static let sheet: CGFloat = 32
    static let control: CGFloat = 16
    static let chip: CGFloat = 8
}

enum Motion {
    /// Decelerates without overshoot. The default for anything that moves.
    static let settle = Animation.smooth(duration: 0.35)
    static let quick = Animation.smooth(duration: 0.2)
}

struct CanvasBackground: View {
    var body: some View {
        Color.canvas.ignoresSafeArea()
    }
}

extension View {
    /// Paints the status-bar strip so scrolled content never runs under the clock. A zero-height view
    /// that ignores the top safe area extends up into exactly that strip.
    func statusBarBackdrop(_ color: Color = .canvas) -> some View {
        overlay(alignment: .top) {
            color.frame(height: 0).ignoresSafeArea(edges: .top)
        }
    }
}

/// A flat surface panel. No border and no shadow: the step in tone is the separation.
struct SurfaceBackground: View {
    var cornerRadius: CGFloat = Radius.card
    var color: Color = .surface

    var body: some View {
        RoundedRectangle(cornerRadius: cornerRadius, style: .continuous).fill(color)
    }
}

/// The member card's finish, like a physical card that changes as a giver moves up.
enum TierStyle {
    case member, gold, platinum, black

    init(name: String) {
        switch name {
        case "Gold": self = .gold
        case "Platinum": self = .platinum
        case "Black": self = .black
        default: self = .member
        }
    }

    var fill: Color {
        switch self {
        case .member: .surface
        case .gold: .tierGold
        case .platinum: .tierPlatinum
        case .black: .canvas
        }
    }

    /// Text on the card. Gold and platinum are light metals, so they take dark ink.
    var ink: Color {
        switch self {
        case .gold, .platinum: .onLime
        case .member, .black: .ink
        }
    }

    var inkMuted: Color {
        switch self {
        case .gold, .platinum: Color.onLime.opacity(0.62)
        case .member, .black: .inkMuted
        }
    }

    /// Only the black card is outlined, in lime: on the canvas it would otherwise disappear.
    var outline: Color? {
        self == .black ? .lime : nil
    }
}

struct TierCardBackground: View {
    let tier: TierStyle

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: 24, style: .continuous)
        shape
            .fill(tier.fill)
            .overlay {
                if let outline = tier.outline {
                    shape.strokeBorder(outline, lineWidth: 1.5)
                }
            }
    }
}
