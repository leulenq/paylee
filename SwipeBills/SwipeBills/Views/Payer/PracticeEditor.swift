//
//  PracticeEditor.swift
//  SwipeBills
//
//  A rhythm the payer sets for themself. The app says when it's complete. A missed week changes nothing,
//  and money they don't use is never treated as owed.
//

import SwiftUI

struct PracticeEditor: View {
    @Binding var practice: GivingPractice?
    var progress: PracticeProgress?

    private enum Choice: String, CaseIterable, Hashable {
        case none, weekly, monthly

        var label: String {
            switch self {
            case .none: "None"
            case .weekly: "Weekly"
            case .monthly: "Monthly"
            }
        }

        var cadence: GivingPractice.Cadence? {
            switch self {
            case .none: nil
            case .weekly: .weekly
            case .monthly: .monthly
            }
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            SegmentedPicker(options: Array(Choice.allCases), selection: choice) { $0.label }

            if let practice {
                Text(practice.summary)
                    .font(Typeface.ui(20, .medium, relativeTo: .title3))
                    .foregroundStyle(Color.ink)

                countRow
                startRow
                limitRow

                if let progress {
                    Text(progressSentence(progress, practice: practice))
                        .font(Typeface.ui(16, .regular, relativeTo: .subheadline))
                        .foregroundStyle(Color.ink)
                        .fixedSize(horizontal: false, vertical: true)
                }

                Text("A missed week changes nothing. Money you don't use stays in your account.")
                    .font(Typeface.ui(15, .regular, relativeTo: .footnote))
                    .foregroundStyle(Color.inkMuted)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text("No rhythm set. The deck is here whenever you open it.")
                    .font(Typeface.ui(16, .regular, relativeTo: .body))
                    .foregroundStyle(Color.inkMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var choice: Binding<Choice> {
        Binding(
            get: {
                switch practice?.cadence {
                case .weekly: .weekly
                case .monthly: .monthly
                case nil: .none
                }
            },
            set: { next in
                guard let cadence = next.cadence else {
                    practice = nil
                    return
                }
                var updated = practice ?? GivingPractice()
                updated.cadence = cadence
                practice = updated
            }
        )
    }

    private var countRow: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text("Bills you finish")
                    .font(Typeface.ui(16, .medium, relativeTo: .body))
                    .foregroundStyle(Color.ink)
                Text("Counts when you pay the last of it.")
                    .font(Typeface.ui(13, .regular, relativeTo: .caption))
                    .foregroundStyle(Color.inkMuted)
            }
            Spacer()
            stepButton("minus") { edit { $0.billsPerPeriod = max(1, $0.billsPerPeriod - 1) } }
            Text("\(practice?.billsPerPeriod ?? 1)")
                .font(Typeface.display(22, .black, relativeTo: .title3))
                .foregroundStyle(Color.ink)
                .monospacedDigit()
                .frame(minWidth: 28)
            stepButton("plus") { edit { $0.billsPerPeriod = min(4, $0.billsPerPeriod + 1) } }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(practice?.billsPerPeriod ?? 1) bills you finish each period")
    }

    private var startRow: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Have a deck ready")
                .font(Typeface.ui(16, .medium, relativeTo: .body))
                .foregroundStyle(Color.ink)
            HStack(spacing: 8) {
                ForEach(GivingPractice.StartDay.allCases, id: \.self) { day in
                    let on = practice?.startDay == day
                    Button(label(for: day)) { edit { $0.startDay = day } }
                        .buttonStyle(.plain)
                        .font(Typeface.ui(15, .semibold, relativeTo: .subheadline))
                        .foregroundStyle(on ? Color.canvas : Color.ink)
                        .frame(maxWidth: .infinity, minHeight: 40)
                        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(on ? Color.ink : Color.raised))
                        .accessibilityAddTraits(on ? .isSelected : [])
                }
            }
        }
    }

    private var limitRow: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Set aside this month")
                .font(Typeface.ui(16, .medium, relativeTo: .body))
                .foregroundStyle(Color.ink)
            HStack(spacing: 8) {
                ForEach([nil, 5_000, 10_000, 20_000], id: \.self) { cents in
                    let on = practice?.monthlyLimitCents == cents
                    Button(cents.map { Money.short($0) } ?? "None") { edit { $0.monthlyLimitCents = cents } }
                        .buttonStyle(.plain)
                        .font(Typeface.ui(15, .semibold, relativeTo: .subheadline))
                        .foregroundStyle(on ? Color.canvas : Color.ink)
                        .frame(maxWidth: .infinity, minHeight: 40)
                        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(on ? Color.ink : Color.raised))
                        .accessibilityAddTraits(on ? .isSelected : [])
                }
            }
            if let limit = practice?.monthlyLimitCents, let spent = progress?.spentCentsThisMonth {
                let left = max(limit - spent, 0)
                Text("\(Money.format(left)) of \(Money.format(limit)) is still yours.")
                    .font(Typeface.ui(14, .regular, relativeTo: .footnote))
                    .foregroundStyle(Color.inkMuted)
            }
        }
    }

    private func stepButton(_ symbol: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 14, weight: .bold))
                .foregroundStyle(Color.ink)
                .frame(width: 36, height: 36)
                .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.raised))
        }
        .buttonStyle(.pressable)
    }

    private func edit(_ change: (inout GivingPractice) -> Void) {
        guard var current = practice else { return }
        change(&current)
        practice = current
    }

    private func label(for day: GivingPractice.StartDay) -> String {
        switch day {
        case .sunday: "Sunday"
        case .payday: "Friday"
        case .firstOfMonth: "The 1st"
        }
    }

    private func progressSentence(_ progress: PracticeProgress, practice: GivingPractice) -> String {
        if progress.complete {
            return practice.cadence == .weekly ? "That's your week." : "That's your month."
        }
        let left = max(progress.target - progress.finishedThisPeriod, 0)
        let noun = left == 1 ? "finish" : "finishes"
        return "\(progress.finishedThisPeriod) of \(progress.target) \(progress.periodLabel). \(left) \(noun) left, whenever you want."
    }
}
