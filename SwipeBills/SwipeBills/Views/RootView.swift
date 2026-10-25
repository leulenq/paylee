//
//  RootView.swift
//  SwipeBills
//
//  Routing: sign in, choose a side, then either the payer app or the requestor app. The two sides share
//  sign-in, Profile, and the design system, and nothing else: different tabs, different home, different tone.
//

import SwiftUI

struct RootView: View {
    @Environment(AppModel.self) private var app

    private enum Stage: Equatable {
        case signedOut, choosingRole, payerOnboarding, payer, requestor
    }

    private var stage: Stage {
        guard app.isSignedIn else { return .signedOut }
        switch app.role {
        case nil: return .choosingRole
        case .payer: return app.hasCompletedOnboarding ? .payer : .payerOnboarding
        case .requestor: return .requestor
        }
    }

    var body: some View {
        Group {
            switch stage {
            case .signedOut: SignInView()
            case .choosingRole: RoleChooserView()
            case .payerOnboarding: OnboardingView()
            case .payer: PayerTabView()
            case .requestor: RequestorTabView()
            }
        }
        .transition(.opacity)
        .animation(Motion.settle, value: stage)
        #if DEBUG
        // `-PayleeDevAutoLogin YES` skips the sign-in screen, and `-PayleeShowOnboarding YES|NO` replays or
        // skips payer onboarding, for screenshots and quick iteration.
        .task {
            if UserDefaults.standard.object(forKey: "PayleeShowOnboarding") != nil {
                app.hasCompletedOnboarding = !UserDefaults.standard.bool(forKey: "PayleeShowOnboarding")
            }
            if !app.isSignedIn && UserDefaults.standard.bool(forKey: "PayleeDevAutoLogin") {
                try? await app.signInForDevelopment()
            }
        }
        #endif
        .task(id: app.isSignedIn) {
            if app.isSignedIn { await app.syncProfile() }
        }
    }
}

// MARK: - Payer

enum PayerTab: String, CaseIterable, TabItem {
    case deck, open, ledger, profile

    var title: String {
        switch self {
        case .deck: "Deck"
        case .open: "Open"
        case .ledger: "Ledger"
        case .profile: "Profile"
        }
    }

    var symbol: String {
        switch self {
        case .deck: "rectangle.stack"
        case .open: "clock"
        case .ledger: "book.closed"
        case .profile: "person"
        }
    }
}

/// Deck to give, Open for bills you've started, Ledger for where your money went, Profile for you.
/// Tabs stay alive so the deck keeps its place.
struct PayerTabView: View {
    @Environment(AppModel.self) private var app
    @Environment(\.scenePhase) private var scenePhase
    @State private var tab: PayerTab = DebugLaunch.initialTab(default: .deck)

    var body: some View {
        ZStack {
            TabScreen(isSelected: tab == .deck) { FeedView(api: app.api) }
            TabScreen(isSelected: tab == .open) { OpenBillsView(isActive: tab == .open) { tab = .deck } }
            TabScreen(isSelected: tab == .ledger) { LedgerView(isActive: tab == .ledger) }
            TabScreen(isSelected: tab == .profile) { ImpactView(isActive: tab == .profile) }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            BottomTabBar(selection: $tab, badged: app.unreadActivity > 0 && tab != .open ? [.open] : [])
        }
        .task {
            await app.refreshUnreadActivity()
            await app.checkForTierUp()
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                Task { await app.refreshUnreadActivity() }
            }
        }
        .overlay {
            if let me = app.tierUp {
                TierUpView(me: me) {
                    withAnimation(Motion.settle) { app.tierUp = nil }
                }
                .transition(.opacity)
            }
        }
        .animation(Motion.settle, value: app.tierUp?.tier.name)
        .onChange(of: app.linkedBillID) { _, id in
            if id != nil { tab = .deck }
        }
    }
}

// MARK: - Requestor

enum RequestorTab: String, CaseIterable, TabItem {
    case bills, help

    var title: String {
        switch self {
        case .bills: "My bills"
        case .help: "Help"
        }
    }

    var symbol: String {
        switch self {
        case .bills: "doc.text"
        case .help: "questionmark.circle"
        }
    }
}

/// Fewer tabs and no deck: a requestor's whole job is to submit a bill and know where it stands.
struct RequestorTabView: View {
    @State private var tab: RequestorTab = DebugLaunch.initialTab(default: .bills)

    var body: some View {
        ZStack {
            TabScreen(isSelected: tab == .bills) { RequestorHomeView(isActive: tab == .bills) }
            TabScreen(isSelected: tab == .help) { HelpView() }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            BottomTabBar(selection: $tab)
        }
    }
}

// MARK: - Shared tab plumbing

protocol TabItem: Hashable, CaseIterable where AllCases: RandomAccessCollection {
    var title: String { get }
    var symbol: String { get }
}

private struct TabScreen<Content: View>: View {
    let isSelected: Bool
    @ViewBuilder let content: () -> Content

    var body: some View {
        content()
            .opacity(isSelected ? 1 : 0)
            .allowsHitTesting(isSelected)
            .accessibilityHidden(!isSelected)
            .animation(Motion.quick, value: isSelected)
    }
}

enum DebugLaunch {
    /// `-PayleeInitialTab <name>` opens a tab directly, for screenshots.
    static func initialTab<Tab: RawRepresentable>(default fallback: Tab) -> Tab where Tab.RawValue == String {
        #if DEBUG
        if let name = UserDefaults.standard.string(forKey: "PayleeInitialTab"), let tab = Tab(rawValue: name) {
            return tab
        }
        #endif
        return fallback
    }
}

/// A flat bar along the bottom edge. The selected tab is ink, the rest faint; no pill, no glow.
struct BottomTabBar<Tab: TabItem>: View {
    @Binding var selection: Tab
    /// Tabs with something unread.
    var badged: Set<Tab> = []

    var body: some View {
        HStack(spacing: 0) {
            ForEach(Array(Tab.allCases), id: \.self) { tab in
                let isSelected = tab == selection
                Button {
                    withAnimation(Motion.quick) { selection = tab }
                } label: {
                    VStack(spacing: 5) {
                        Image(systemName: tab.symbol)
                            .symbolVariant(isSelected ? .fill : .none)
                            .font(.system(size: 19, weight: .semibold))
                            .frame(height: 24)
                            .overlay(alignment: .topTrailing) {
                                if badged.contains(tab) {
                                    Circle()
                                        .fill(Color.lime)
                                        .frame(width: 8, height: 8)
                                        .offset(x: 6, y: -1)
                                }
                            }
                        Text(tab.title)
                            .font(Typeface.ui(12, isSelected ? .bold : .medium, relativeTo: .caption))
                    }
                    .foregroundStyle(isSelected ? Color.ink : Color.inkFaint)
                    .frame(maxWidth: .infinity, minHeight: 52)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(badged.contains(tab) ? "\(tab.title), new updates" : tab.title)
                .accessibilityAddTraits(isSelected ? [.isSelected, .isButton] : .isButton)
            }
        }
        .padding(.top, 8)
        .background {
            Color.canvas
                .overlay(alignment: .top) { Hairline() }
                .ignoresSafeArea()
        }
        .sensoryFeedback(.selection, trigger: selection)
    }
}

#Preview {
    RootView().environment(AppModel())
}
