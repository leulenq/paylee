//
//  LeaderboardView.swift
//  SwipeBills
//

import SwiftUI

struct LeaderboardView: View {
    /// True while this tab is on screen, so data refreshes each time the giver comes back to it.
    let isActive: Bool

    @Environment(AppModel.self) private var app

    @State private var period: LeaderboardPeriod = .month
    @State private var board: Leaderboard?
    @State private var errorMessage: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                ScreenTitle("Leaders")

                SegmentedPicker(options: LeaderboardPeriod.allCases, selection: $period) { $0.label }
                    .padding(.top, 20)

                Group {
                    if let board {
                        if board.entries.isEmpty {
                            emptyState
                        } else {
                            ranking(board)
                        }
                    } else if let errorMessage {
                        Text(errorMessage)
                            .font(Typeface.ui(16, .regular))
                            .foregroundStyle(Color.inkMuted)
                    } else {
                        ProgressView().tint(Color.inkMuted).frame(maxWidth: .infinity, minHeight: 160)
                    }
                }
                .padding(.top, 18)

                Text("Ranked by bills helped all the way to paid off, then by bills closed. Dollar amounts never count, so a $5 gift and a $500 gift move you up the same.")
                    .font(Typeface.ui(14, .regular, relativeTo: .footnote))
                    .foregroundStyle(Color.inkMuted)
                    .lineSpacing(2)
                    .padding(.top, 28)
            }
            .padding(.horizontal, 22)
            .padding(.top, 8)
            .padding(.bottom, 32)
        }
        .scrollIndicators(.hidden)
        .background(CanvasBackground())
        .statusBarBackdrop()
        .refreshable { await load() }
        .task(id: TaskKey(isActive: isActive, period: period)) {
            if isActive { await load() }
        }
    }

    private struct TaskKey: Equatable {
        let isActive: Bool
        let period: LeaderboardPeriod
    }

    private func load() async {
        do {
            let loaded = try await app.api.leaderboard(period: period)
            withAnimation(Motion.settle) { board = loaded }
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func ranking(_ board: Leaderboard) -> some View {
        let meListed = board.me.map { mine in board.entries.contains(mine) } ?? true
        return VStack(spacing: 0) {
            ForEach(Array(board.entries.enumerated()), id: \.element.id) { index, entry in
                if index > 0 { Hairline() }
                row(entry, leader: entry.rank == 1)
            }
            if let me = board.me, !meListed {
                Rectangle().fill(Color.line).frame(height: 1).padding(.top, 8)
                row(me, leader: false)
            }
        }
    }

    private func row(_ entry: LeaderboardEntry, leader: Bool) -> some View {
        HStack(alignment: .center, spacing: 16) {
            Text("\(entry.rank)")
                .font(Typeface.display(leader ? 44 : 28, .black, relativeTo: .title))
                .tracking(-1)
                .foregroundStyle(leader ? Color.ink : Color.inkMuted)
                .monospacedDigit()
                .frame(width: 56, alignment: .leading)
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 8) {
                    Text(entry.handle)
                        .font(Typeface.ui(leader ? 19 : 17, .semibold, relativeTo: .headline))
                        .foregroundStyle(Color.ink)
                        .lineLimit(1)
                    if entry.isMe {
                        Text("You")
                            .font(Typeface.ui(12, .bold, relativeTo: .caption))
                            .foregroundStyle(Color.onLime)
                            .padding(.horizontal, 7)
                            .padding(.vertical, 2)
                            .background(RoundedRectangle(cornerRadius: 5).fill(Color.lime))
                    }
                }
                Text("\(entry.billsCompleted) paid off · \(entry.billsClosed) closed")
                    .font(Typeface.ui(14, .regular, relativeTo: .footnote))
                    .foregroundStyle(Color.inkMuted)
            }
            Spacer()
        }
        .padding(.vertical, leader ? 18 : 14)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Rank \(entry.rank), \(entry.handle)\(entry.isMe ? ", you" : ""), \(entry.billsCompleted) bills paid off, \(entry.billsClosed) closed")
    }

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("No one yet.")
                .font(Typeface.display(34, .black, relativeTo: .title))
                .tracking(-1)
                .foregroundStyle(Color.ink)
            Text(period == .month
                 ? "Help a bill get paid off this month and you're first."
                 : "Help a bill get paid off and you're first.")
                .font(Typeface.ui(17, .regular, relativeTo: .body))
                .foregroundStyle(Color.inkMuted)
        }
        .padding(.vertical, 24)
    }
}
