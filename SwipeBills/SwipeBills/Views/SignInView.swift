//
//  SignInView.swift
//  SwipeBills
//

import AuthenticationServices
import SwiftUI

struct SignInView: View {
    @Environment(AppModel.self) private var app
    @State private var errorMessage: String?
    @State private var isWorking = false
    @State private var appeared = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            PayleeMark(size: 44)
                .padding(.top, 20)

            Spacer()

            Text("Pay a stranger's bill.")
                .font(Typeface.display(50, .black, relativeTo: .largeTitle))
                .tracking(-2)
                .foregroundStyle(Color.ink)
                .fixedSize(horizontal: false, vertical: true)
                .opacity(appeared ? 1 : 0)
                .offset(y: appeared ? 0 : 24)
            Text("Straight to the company on the bill. Never to a bank account.")
                .font(Typeface.ui(20, .regular, relativeTo: .title3))
                .foregroundStyle(Color.inkMuted)
                .lineSpacing(3)
                .padding(.top, 16)
                .opacity(appeared ? 1 : 0)

            VStack(alignment: .leading, spacing: 14) {
                promise("A person checks every bill before it goes live.")
                promise("You give under a private name.")
                promise("Leaderboards count bills paid off, not dollars.")
            }
            .padding(.top, 34)
            .opacity(appeared ? 1 : 0)

            Spacer()

            VStack(spacing: 14) {
                SignInWithAppleButton(.continue) { request in
                    // Paylee never needs a giver's name or email, so it asks Apple for neither.
                    request.requestedScopes = []
                } onCompletion: { result in
                    Task { await handleApple(result) }
                }
                .signInWithAppleButtonStyle(.white)
                .frame(height: 58)
                .clipShape(RoundedRectangle(cornerRadius: Radius.control, style: .continuous))

                #if DEBUG
                Button {
                    Task { await signInForDevelopment() }
                } label: {
                    Text("Continue in development mode")
                        .font(Typeface.ui(15, .medium, relativeTo: .footnote))
                        .foregroundStyle(Color.inkMuted)
                        .underline()
                }
                .buttonStyle(.plain)
                #endif

                if let errorMessage {
                    Text(errorMessage)
                        .font(Typeface.ui(14, .medium, relativeTo: .footnote))
                        .foregroundStyle(Color.danger)
                        .multilineTextAlignment(.center)
                }
            }
            .frame(maxWidth: .infinity)
            .disabled(isWorking)
            .padding(.bottom, 12)
        }
        .padding(.horizontal, 24)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(CanvasBackground())
        .onAppear {
            withAnimation(.smooth(duration: 0.7).delay(0.1)) { appeared = true }
        }
    }

    private func promise(_ text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Image(systemName: "checkmark")
                .font(.system(size: 13, weight: .heavy))
                .foregroundStyle(Color.lime)
                .accessibilityHidden(true)
            Text(text)
                .font(Typeface.ui(17, .medium, relativeTo: .body))
                .foregroundStyle(Color.ink)
        }
    }

    private func handleApple(_ result: Result<ASAuthorization, Error>) async {
        isWorking = true
        defer { isWorking = false }
        do {
            try await app.signInWithApple(result.get())
            errorMessage = nil
        } catch ASAuthorizationError.canceled {
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func signInForDevelopment() async {
        isWorking = true
        defer { isWorking = false }
        do {
            try await app.signInForDevelopment()
            errorMessage = nil
        } catch {
            errorMessage = "\(error.localizedDescription)\nIs the backend running at \(AppConfig.apiBaseURL.absoluteString)?"
        }
    }
}

#Preview {
    SignInView().environment(AppModel())
}
