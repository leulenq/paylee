//
//  Navigation.swift
//  SwipeBills
//
//  Pushed screens use NavigationStack for the behaviour (push, pop, edge swipe) but hide the stock bar and
//  draw their own back button, so they match the rest of Paylee.
//

import SwiftUI
import UIKit

extension View {
    /// Hides the system navigation bar and adds Paylee's back button and title row at the top of the content.
    func payleeNavigation() -> some View {
        toolbar(.hidden, for: .navigationBar)
            .navigationBarBackButtonHidden(true)
    }
}

struct BackButton: View {
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        CircleIconButton(symbol: "chevron.left", label: "Back", size: 40) { dismiss() }
    }
}

/// A row that pushes somewhere: title, optional value on the right, chevron.
struct NavigationRow<Destination: View>: View {
    let title: String
    var value: String?
    var detail: String?
    @ViewBuilder let destination: () -> Destination

    var body: some View {
        NavigationLink {
            destination()
        } label: {
            HStack(alignment: .center, spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(Typeface.ui(17, .medium, relativeTo: .body))
                        .foregroundStyle(Color.ink)
                    if let detail {
                        Text(detail)
                            .font(Typeface.ui(14, .regular, relativeTo: .footnote))
                            .foregroundStyle(Color.inkMuted)
                    }
                }
                Spacer(minLength: 8)
                if let value {
                    Text(value)
                        .font(Typeface.ui(16, .medium, relativeTo: .subheadline))
                        .foregroundStyle(Color.inkMuted)
                        .lineLimit(1)
                }
                Image(systemName: "chevron.right")
                    .font(.system(size: 13, weight: .bold))
                    .foregroundStyle(Color.inkFaint)
            }
            .padding(.vertical, 15)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

/// A labelled switch in Paylee's style: ink when on, raised when off. Lime is reserved for actions.
struct SettingToggle: View {
    let title: String
    var detail: String?
    @Binding var isOn: Bool

    var body: some View {
        Toggle(isOn: $isOn) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(Typeface.ui(17, .medium, relativeTo: .body))
                    .foregroundStyle(Color.ink)
                if let detail {
                    Text(detail)
                        .font(Typeface.ui(14, .regular, relativeTo: .footnote))
                        .foregroundStyle(Color.inkMuted)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .tint(Color.ink)
        .padding(.vertical, 12)
    }
}

/// With the bar hidden, UIKit disables the edge swipe back. This restores it on every stack.
extension UINavigationController: @retroactive UIGestureRecognizerDelegate {
    override open func viewDidLoad() {
        super.viewDidLoad()
        interactivePopGestureRecognizer?.delegate = self
    }

    public func gestureRecognizerShouldBegin(_ gestureRecognizer: UIGestureRecognizer) -> Bool {
        viewControllers.count > 1
    }
}
