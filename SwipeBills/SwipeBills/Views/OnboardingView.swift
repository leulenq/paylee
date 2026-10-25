//
//  OnboardingView.swift
//  SwipeBills
//
//  First run after sign-in: how Paylee works, what the giver cares about, where, and whether to be told
//  when bills they help get paid. Everything here can be changed later from the deck's filters.
//

import SwiftUI

struct OnboardingView: View {
    @Environment(AppModel.self) private var app
    @State private var step = 0
    @State private var preferences = DiscoveryPreferences.load()

    private let stepCount = 5

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                progress
                Spacer()
                if step < stepCount - 1 {
                    Button("Skip") { finish() }
                        .font(Typeface.ui(16, .medium, relativeTo: .subheadline))
                        .foregroundStyle(Color.inkMuted)
                }
            }
            .padding(.top, 16)

            Group {
                switch step {
                case 0: howItWorks
                case 1: causes
                case 2: location
                case 3: practiceStep
                default: notifications
                }
            }
            .id(step)
            .transition(.asymmetric(
                insertion: .offset(x: 40).combined(with: .opacity),
                removal: .offset(x: -40).combined(with: .opacity)
            ))
            .frame(maxHeight: .infinity, alignment: .top)
            .padding(.top, 40)

            primaryButton
                .padding(.bottom, 12)
        }
        .padding(.horizontal, 24)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(CanvasBackground())
        .sensoryFeedback(.selection, trigger: step)
    }

    /// "1 / 4": counting, not dots.
    private var progress: some View {
        Text("\(step + 1) / \(stepCount)")
            .font(Typeface.ui(16, .semibold, relativeTo: .subheadline))
            .foregroundStyle(Color.inkMuted)
            .monospacedDigit()
            .contentTransition(.numericText(value: Double(step)))
            .accessibilityLabel("Step \(step + 1) of \(stepCount)")
    }

    private func title(_ text: String, _ detail: String) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(text)
                .font(Typeface.display(42, .black, relativeTo: .largeTitle))
                .tracking(-1.6)
                .foregroundStyle(Color.ink)
                .fixedSize(horizontal: false, vertical: true)
            Text(detail)
                .font(Typeface.ui(18, .regular, relativeTo: .body))
                .foregroundStyle(Color.inkMuted)
                .lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: Steps

    private var howItWorks: some View {
        VStack(alignment: .leading, spacing: 36) {
            title("Real bills. Paid at the source.", "A person checks every bill before you see it.")
            VStack(alignment: .leading, spacing: 0) {
                gesture("arrow.right", "Swipe right", "Chip in any amount.")
                Hairline()
                gesture("arrow.up", "Swipe up", "Close it. Pay off everything that's left.")
                Hairline()
                gesture("arrow.left", "Swipe left", "Pass for now. It only tunes your deck. You can rewind.")
            }
        }
    }

    private func gesture(_ symbol: String, _ title: String, _ detail: String) -> some View {
        HStack(spacing: 16) {
            Image(systemName: symbol)
                .font(.system(size: 17, weight: .heavy))
                .foregroundStyle(Color.lime)
                .frame(width: 28)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(Typeface.ui(18, .semibold, relativeTo: .headline)).foregroundStyle(Color.ink)
                Text(detail).font(Typeface.ui(16, .regular, relativeTo: .subheadline)).foregroundStyle(Color.inkMuted)
            }
        }
        .padding(.vertical, 16)
        .accessibilityElement(children: .combine)
    }

    private var causes: some View {
        VStack(alignment: .leading, spacing: 32) {
            title("What do you want to cover?", "Pick any, or none to see every kind of bill.")
            CategoryPicker(selection: $preferences.categories)
        }
    }

    private var location: some View {
        VStack(alignment: .leading, spacing: 32) {
            title("Where?", "Giving close to home hits different. You can widen it any time.")
            StatePicker(state: $preferences.state)
        }
    }

    private var practiceStep: some View {
        VStack(alignment: .leading, spacing: 28) {
            title(
                "A rhythm, if you want one.",
                "One finish on a morning you choose. Miss it and nothing resets."
            )
            PracticeEditor(
                practice: Binding(
                    get: { app.settings.practice },
                    set: { app.settings.practice = $0 }
                )
            )
        }
    }

    private var notifications: some View {
        VStack(alignment: .leading, spacing: 32) {
            title("See it get paid.", "We'll tell you when a bill you helped is covered, and when the company has been paid. Nothing else.")
            VStack(spacing: 8) {
                notificationPreview("You closed it", "Maria's Kentucky Utilities bill is paid in full.")
                notificationPreview("Kentucky Utilities has been paid", "Paylee paid $212.50 for Maria.")
                    .opacity(0.5)
            }
            .accessibilityHidden(true)
        }
    }

    private func notificationPreview(_ title: String, _ body: String) -> some View {
        HStack(alignment: .top, spacing: 12) {
            PayleeMark(size: 34)
            VStack(alignment: .leading, spacing: 2) {
                HStack {
                    Text(title).font(Typeface.ui(15, .semibold)).foregroundStyle(Color.ink)
                    Spacer()
                    Text("now").font(Typeface.ui(13, .regular)).foregroundStyle(Color.inkMuted)
                }
                Text(body).font(Typeface.ui(14, .regular)).foregroundStyle(Color.inkMuted)
            }
        }
        .padding(14)
        .background(RoundedRectangle(cornerRadius: 20, style: .continuous).fill(Color.surface))
    }

    // MARK: Navigation

    @ViewBuilder
    private var primaryButton: some View {
        if step == stepCount - 1 {
            VStack(spacing: 12) {
                Button("Turn on notifications") {
                    Task {
                        await app.requestPushPermission()
                        finish()
                    }
                }
                .buttonStyle(.primary)
                Button("Not now") { finish() }
                    .font(Typeface.ui(16, .medium, relativeTo: .subheadline))
                    .foregroundStyle(Color.inkMuted)
                    .frame(minHeight: 44)
            }
        } else {
            Button(step == 0 ? "Get started" : "Continue") {
                withAnimation(Motion.settle) { step += 1 }
            }
            .buttonStyle(.primary)
        }
    }

    private func finish() {
        preferences.save()
        withAnimation(Motion.settle) {
            app.hasCompletedOnboarding = true
        }
    }
}

#Preview {
    OnboardingView().environment(AppModel())
}
