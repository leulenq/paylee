//
//  HelpView.swift
//  SwipeBills
//
//  Plain answers for requestors. Every answer must match how the backend actually behaves.
//

import SwiftUI

struct HelpView: View {
    @State private var expanded: String?

    private let answers: [(String, String)] = [
        ("How does my bill get paid?", "Paylee pays the company on your bill directly, through the company's own billing system. The money never goes to you or your bank account."),
        ("Does it cost anything?", "No. Paylee never charges the person asking for help."),
        ("Who sees my bill?", "Givers see your first name, your city, the company, the amount, and your story after a reviewer edits out private details. They never see the bill itself, your last name, your email, or your account number."),
        ("How long does review take?", "Usually within two business days. We email you either way."),
        ("What if I pay part of it myself?", "Tell us the new balance so we never overpay the company."),
        ("What if it isn't covered by the due date?", "Anything givers raised still goes to the company on your bill."),
        ("Will I hear from the givers?", "No. Givers stay private, and there's nothing you owe anyone."),
    ]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                ScreenTitle("Help")
                VStack(spacing: 0) {
                    ForEach(answers, id: \.0) { question, answer in
                        Button {
                            withAnimation(Motion.settle) { expanded = expanded == question ? nil : question }
                        } label: {
                            VStack(alignment: .leading, spacing: 8) {
                                HStack(alignment: .firstTextBaseline) {
                                    Text(question)
                                        .font(Typeface.ui(17, .semibold, relativeTo: .headline))
                                        .foregroundStyle(Color.ink)
                                    Spacer()
                                    Image(systemName: expanded == question ? "minus" : "plus")
                                        .font(.system(size: 13, weight: .bold))
                                        .foregroundStyle(Color.inkMuted)
                                }
                                if expanded == question {
                                    Text(answer)
                                        .font(Typeface.ui(16, .regular, relativeTo: .body))
                                        .foregroundStyle(Color.inkMuted)
                                        .lineSpacing(3)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                            }
                            .padding(.vertical, 16)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        Hairline()
                    }
                }
                .padding(.top, 16)
            }
            .padding(.horizontal, 22)
            .padding(.top, 8)
            .padding(.bottom, 32)
        }
        .scrollIndicators(.hidden)
        .background(CanvasBackground())
        .statusBarBackdrop()
    }
}
