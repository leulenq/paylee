//
//  RoleChooserView.swift
//  SwipeBills
//
//  The fork after sign-in. Two plain choices, no ranking between them: needing help is not the lesser side.
//

import SwiftUI

struct RoleChooserView: View {
    @Environment(AppModel.self) private var app
    @State private var appeared = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            PayleeMark(size: 40)
                .padding(.top, 20)

            Text("What brings you to Paylee?")
                .font(Typeface.display(40, .black, relativeTo: .largeTitle))
                .tracking(-1.6)
                .foregroundStyle(Color.ink)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 40)

            VStack(spacing: 12) {
                choice(
                    title: "I want to pay bills",
                    detail: "Swipe through bills a person has checked, and pay the company on the bill directly.",
                    symbol: "arrow.right",
                    role: .payer
                )
                choice(
                    title: "I need help with a bill",
                    detail: "Send one bill. If givers cover it, Paylee pays the company for you. You never handle the money.",
                    symbol: "doc.text",
                    role: .requestor
                )
            }
            .padding(.top, 32)
            .opacity(appeared ? 1 : 0)
            .offset(y: appeared ? 0 : 16)

            Spacer()

            Text("You can switch sides later in Profile.")
                .font(Typeface.ui(15, .regular, relativeTo: .footnote))
                .foregroundStyle(Color.inkMuted)
                .padding(.bottom, 16)
        }
        .padding(.horizontal, 24)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(CanvasBackground())
        .onAppear {
            withAnimation(.smooth(duration: 0.5).delay(0.1)) { appeared = true }
        }
    }

    private func choice(title: String, detail: String, symbol: String, role: UserRole) -> some View {
        Button {
            withAnimation(Motion.settle) { app.choose(role) }
        } label: {
            HStack(alignment: .top, spacing: 16) {
                VStack(alignment: .leading, spacing: 6) {
                    Text(title)
                        .font(Typeface.ui(21, .semibold, relativeTo: .title3))
                        .foregroundStyle(Color.ink)
                    Text(detail)
                        .font(Typeface.ui(16, .regular, relativeTo: .subheadline))
                        .foregroundStyle(Color.inkMuted)
                        .lineSpacing(2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
                Image(systemName: symbol)
                    .font(.system(size: 16, weight: .heavy))
                    .foregroundStyle(Color.lime)
                    .padding(.top, 4)
            }
            .padding(22)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(SurfaceBackground(cornerRadius: 22))
        }
        .buttonStyle(PressableStyle(scale: 0.98))
    }
}

#Preview {
    RoleChooserView().environment(AppModel())
}
