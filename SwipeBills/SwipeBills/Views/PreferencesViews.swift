//
//  PreferencesViews.swift
//  SwipeBills
//
//  Controls for discovery preferences, shared by onboarding and the deck's filter sheet.
//

import SwiftUI

/// Tappable category chips. Nothing selected means every kind of bill. Selected chips are ink, not lime:
/// filtering is a choice, not an action.
struct CategoryPicker: View {
    @Binding var selection: Set<BillCategory>

    var body: some View {
        FlowLayout(spacing: 8) {
            ForEach(BillCategory.allCases) { category in
                let isOn = selection.contains(category)
                Button {
                    withAnimation(Motion.quick) {
                        if isOn { selection.remove(category) } else { selection.insert(category) }
                    }
                } label: {
                    Label(category.label, systemImage: category.symbol)
                        .font(Typeface.ui(16, .semibold, relativeTo: .subheadline))
                        .foregroundStyle(isOn ? Color.canvas : Color.ink)
                        .padding(.horizontal, 16)
                        .frame(height: 46)
                        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(isOn ? Color.ink : Color.raised))
                }
                .buttonStyle(.pressable)
                .accessibilityAddTraits(isOn ? .isSelected : [])
            }
        }
        .sensoryFeedback(.selection, trigger: selection)
    }
}

/// "Anywhere" or one state, in a menu so the long list stays out of the way.
struct StatePicker: View {
    @Binding var state: String?

    var body: some View {
        Menu {
            Button("Anywhere in the US") { state = nil }
            Divider()
            ForEach(USState.all, id: \.code) { entry in
                Button(entry.name) { state = entry.code }
            }
        } label: {
            HStack {
                Text(state.map(USState.name(for:)) ?? "Anywhere in the US")
                    .font(Typeface.ui(18, .semibold, relativeTo: .body))
                    .foregroundStyle(Color.ink)
                Spacer()
                Image(systemName: "chevron.up.chevron.down")
                    .font(.system(size: 13, weight: .bold))
                    .foregroundStyle(Color.inkMuted)
            }
            .padding(.horizontal, 18)
            .frame(height: 58)
            .background(RoundedRectangle(cornerRadius: Radius.control, style: .continuous).fill(Color.raised))
        }
        .accessibilityLabel("Location, \(state.map(USState.name(for:)) ?? "anywhere")")
    }
}

/// The deck's filter sheet.
struct PreferencesSheet: View {
    let initial: DiscoveryPreferences
    let onApply: (DiscoveryPreferences) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var draft: DiscoveryPreferences

    init(initial: DiscoveryPreferences, onApply: @escaping (DiscoveryPreferences) -> Void) {
        self.initial = initial
        self.onApply = onApply
        _draft = State(initialValue: initial)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 32) {
                HStack(alignment: .firstTextBaseline) {
                    Text("Filters")
                        .font(Typeface.display(30, .black, relativeTo: .largeTitle))
                        .tracking(-1)
                        .foregroundStyle(Color.ink)
                    Spacer()
                    if draft != DiscoveryPreferences() {
                        Button("Reset") {
                            withAnimation(Motion.quick) { draft = DiscoveryPreferences() }
                        }
                        .font(Typeface.ui(16, .semibold, relativeTo: .subheadline))
                        .foregroundStyle(Color.inkMuted)
                    }
                }

                group("Kinds of bills", hint: draft.categories.isEmpty ? "Showing every kind." : nil) {
                    CategoryPicker(selection: $draft.categories)
                }

                group("Where", hint: nil) {
                    StatePicker(state: $draft.state)
                }

                group("Most left to close", hint: "Bills with this much or less still to raise.") {
                    SegmentedPicker(options: DiscoveryPreferences.amountLimits.map { $0 ?? 0 }, selection: Binding(
                        get: { draft.maxRemainingCents ?? 0 },
                        set: { draft.maxRemainingCents = $0 == 0 ? nil : $0 }
                    )) { cents in cents == 0 ? "Any" : Money.short(cents) }
                }
            }
            .padding(.horizontal, 22)
            .padding(.top, 28)
        }
        .scrollIndicators(.hidden)
        .background(Color.surface.ignoresSafeArea())
        .safeAreaInset(edge: .bottom) {
            Button("Show bills") {
                onApply(draft)
                dismiss()
            }
            .buttonStyle(.primary)
            .padding(.horizontal, 22)
            .padding(.top, 8)
            .background(Color.surface.ignoresSafeArea())
        }
        .presentationBackground(Color.surface)
        .presentationCornerRadius(Radius.sheet)
        .presentationDragIndicator(.visible)
    }

    private func group(_ title: String, hint: String?, @ViewBuilder content: () -> some View) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(Typeface.ui(17, .semibold, relativeTo: .headline))
                    .foregroundStyle(Color.ink)
                if let hint {
                    Text(hint)
                        .font(Typeface.ui(14, .regular, relativeTo: .footnote))
                        .foregroundStyle(Color.inkMuted)
                }
            }
            content()
        }
    }
}

/// Lays children out left to right, wrapping onto new lines, for chip groups.
struct FlowLayout: Layout {
    var spacing: CGFloat = 8

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let rows = arrange(subviews: subviews, width: proposal.width ?? .infinity)
        let height = rows.last.map { $0.y + $0.height } ?? 0
        let width = rows.map(\.width).max() ?? 0
        return CGSize(width: proposal.width ?? width, height: height)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let rows = arrange(subviews: subviews, width: bounds.width)
        for row in rows {
            var x = bounds.minX
            for index in row.indices {
                let size = subviews[index].sizeThatFits(.unspecified)
                subviews[index].place(at: CGPoint(x: x, y: bounds.minY + row.y), proposal: ProposedViewSize(size))
                x += size.width + spacing
            }
        }
    }

    private struct Row {
        var indices: [Int] = []
        var y: CGFloat = 0
        var width: CGFloat = 0
        var height: CGFloat = 0
    }

    private func arrange(subviews: Subviews, width: CGFloat) -> [Row] {
        var rows: [Row] = [Row()]
        for index in subviews.indices {
            let size = subviews[index].sizeThatFits(.unspecified)
            var row = rows[rows.count - 1]
            let needed = row.indices.isEmpty ? size.width : row.width + spacing + size.width
            if needed > width && !row.indices.isEmpty {
                let y = row.y + row.height + spacing
                rows.append(Row(indices: [index], y: y, width: size.width, height: size.height))
                continue
            }
            row.indices.append(index)
            row.width = needed
            row.height = max(row.height, size.height)
            rows[rows.count - 1] = row
        }
        return rows
    }
}
