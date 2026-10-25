//
//  Components.swift
//  SwipeBills
//

import SwiftUI

// MARK: - Text

/// A quiet sentence-case label above a group. Not tracked caps: those turn every section into a template.
struct SectionLabel: View {
    let text: String

    init(_ text: String) {
        self.text = text
    }

    var body: some View {
        Text(text)
            .font(Typeface.ui(14, .semibold, relativeTo: .subheadline))
            .foregroundStyle(Color.inkMuted)
            .accessibilityAddTraits(.isHeader)
    }
}

/// Money, big. Dollars in full ink, cents dimmed, so the eye lands on the number that matters.
struct AmountText: View {
    let cents: Int
    var size: CGFloat = 64
    var color: Color = .ink
    var centsColor: Color = .inkMuted
    /// Hides ".00" on whole-dollar amounts.
    var compactWholeDollars = false

    var body: some View {
        let dollars = (cents / 100).formatted(.currency(code: "USD").precision(.fractionLength(0)))
        let remainder = cents % 100
        let showCents = !(compactWholeDollars && remainder == 0)
        Group {
            if showCents {
                Text("\(Text(dollars).foregroundStyle(color))\(Text(String(format: ".%02d", remainder)).foregroundStyle(centsColor))")
            } else {
                Text(dollars).foregroundStyle(color)
            }
        }
        .font(Typeface.display(size, .extraBold))
        .tracking(-size * 0.035)
        .monospacedDigit()
        .lineLimit(1)
        .minimumScaleFactor(0.5)
        .contentTransition(.numericText(value: Double(cents)))
        .accessibilityLabel(Money.format(cents))
    }
}

/// A screen title: wide, heavy, left-aligned.
struct ScreenTitle<Trailing: View>: View {
    let title: String
    @ViewBuilder var trailing: () -> Trailing

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(title)
                .font(Typeface.display(34, .black, relativeTo: .largeTitle))
                .tracking(-1)
                .foregroundStyle(Color.ink)
                .accessibilityAddTraits(.isHeader)
            Spacer()
            trailing()
        }
    }
}

extension ScreenTitle where Trailing == EmptyView {
    init(_ title: String) {
        self.init(title: title) { EmptyView() }
    }
}

// MARK: - Brand

/// A lime tile with a heavy lowercase "p".
struct PayleeMark: View {
    var size: CGFloat = 30

    var body: some View {
        Text("p")
            .font(.custom("ArchivoExpanded-Black", fixedSize: size * 0.82))
            .foregroundStyle(Color.onLime)
            .offset(y: -size * 0.06)
            .frame(width: size, height: size)
            .background(RoundedRectangle(cornerRadius: size * 0.24, style: .continuous).fill(Color.lime))
            .accessibilityHidden(true)
    }
}

struct PayleeWordmark: View {
    var body: some View {
        Text("paylee")
            .font(Typeface.display(24, .black, relativeTo: .title2))
            .tracking(-0.8)
            .foregroundStyle(Color.ink)
            .accessibilityLabel("Paylee")
    }
}

// MARK: - Buttons

/// The one action that matters on a screen: lime, full width, squared-off corners.
struct PrimaryButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(Typeface.ui(17, .bold, relativeTo: .headline))
            .foregroundStyle(isEnabled ? Color.onLime : Color.inkFaint)
            .frame(maxWidth: .infinity, minHeight: 58)
            .background(
                RoundedRectangle(cornerRadius: Radius.control, style: .continuous)
                    .fill(isEnabled ? Color.lime : Color.raised)
            )
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
            .animation(Motion.quick, value: configuration.isPressed)
            .animation(Motion.quick, value: isEnabled)
            .sensoryFeedback(.impact(weight: .light), trigger: configuration.isPressed) { _, pressed in pressed }
    }
}

/// Everything else that's a button: a raised neutral fill.
struct SecondaryButtonStyle: ButtonStyle {
    var foreground: Color = .ink
    var fullWidth = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(Typeface.ui(15, .semibold, relativeTo: .subheadline))
            .foregroundStyle(foreground)
            .padding(.horizontal, 18)
            .frame(maxWidth: fullWidth ? .infinity : nil, minHeight: 46)
            .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Color.raised))
            .opacity(configuration.isPressed ? 0.7 : 1)
            .animation(Motion.quick, value: configuration.isPressed)
    }
}

/// Shrinks slightly on press, for custom tiles and icon buttons.
struct PressableStyle: ButtonStyle {
    var scale: CGFloat = 0.95

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? scale : 1)
            .animation(Motion.quick, value: configuration.isPressed)
    }
}

extension ButtonStyle where Self == PrimaryButtonStyle {
    static var primary: PrimaryButtonStyle { PrimaryButtonStyle() }
}

extension ButtonStyle where Self == SecondaryButtonStyle {
    static var secondary: SecondaryButtonStyle { SecondaryButtonStyle() }
}

extension ButtonStyle where Self == PressableStyle {
    static var pressable: PressableStyle { PressableStyle() }
}

struct CircleIconButton: View {
    let symbol: String
    let label: String
    var size: CGFloat = 40
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: size * 0.36, weight: .bold))
                .foregroundStyle(Color.ink)
                .frame(width: size, height: size)
                .background(Circle().fill(Color.raised))
        }
        .buttonStyle(.pressable)
        .accessibilityLabel(label)
    }
}

// MARK: - Controls

/// A segmented control. The selected segment is ink, not lime: choosing a view isn't an action.
struct SegmentedPicker<Option: Hashable>: View {
    let options: [Option]
    @Binding var selection: Option
    let label: (Option) -> String

    @Namespace private var indicator

    var body: some View {
        HStack(spacing: 2) {
            ForEach(options, id: \.self) { option in
                let isSelected = option == selection
                Button {
                    withAnimation(Motion.settle) { selection = option }
                } label: {
                    Text(label(option))
                        .font(Typeface.ui(14, .semibold, relativeTo: .subheadline))
                        .foregroundStyle(isSelected ? Color.canvas : Color.inkMuted)
                        .lineLimit(1)
                        .minimumScaleFactor(0.8)
                        .frame(maxWidth: .infinity, minHeight: 38)
                        .background {
                            if isSelected {
                                RoundedRectangle(cornerRadius: 11, style: .continuous)
                                    .fill(Color.ink)
                                    .matchedGeometryEffect(id: "indicator", in: indicator)
                            }
                        }
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(isSelected ? .isSelected : [])
            }
        }
        .padding(3)
        .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Color.raised))
        .sensoryFeedback(.selection, trigger: selection)
    }
}

/// A flat bar: lime fill on a line-colored track.
struct ProgressBar: View {
    let progress: Double
    var track: Color = .line
    var height: CGFloat = 6

    @State private var shown: Double = 0

    var body: some View {
        GeometryReader { proxy in
            ZStack(alignment: .leading) {
                RoundedRectangle(cornerRadius: 2).fill(track)
                RoundedRectangle(cornerRadius: 2)
                    .fill(Color.lime)
                    .frame(width: proxy.size.width * shown)
            }
        }
        .frame(height: height)
        .onAppear {
            withAnimation(.smooth(duration: 0.7).delay(0.1)) { shown = progress }
        }
        .onChange(of: progress) { _, new in
            withAnimation(Motion.settle) { shown = new }
        }
        .accessibilityElement()
        .accessibilityLabel("\(Int((progress * 100).rounded())) percent funded")
    }
}

struct Hairline: View {
    var body: some View {
        Rectangle().fill(Color.line).frame(height: 1)
    }
}
