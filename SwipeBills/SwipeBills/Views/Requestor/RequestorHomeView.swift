//
//  RequestorHomeView.swift
//  SwipeBills
//
//  A requestor's home: where each of their bills stands, in calm, plain language. The submit flow and the
//  per-bill detail screen are still to come (see docs/api/requestor-and-profile.md for the data).
//

import SwiftUI

struct RequestorHomeView: View {
    let isActive: Bool

    @Environment(AppModel.self) private var app

    private var model: RequestsModel { app.requests }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                ScreenTitle("My bills")
                #if DEBUG
                if model.isPreview {
                    Text("Preview data enabled for this debug session.")
                        .font(Typeface.ui(13, .medium, relativeTo: .caption))
                        .foregroundStyle(Color.inkMuted)
                        .padding(.top, 6)
                }
                #endif

                if let error = model.errorMessage {
                    Text(error)
                        .font(Typeface.ui(15, .regular, relativeTo: .footnote))
                        .foregroundStyle(Color.danger)
                        .padding(.top, 12)
                }

                if !model.hasLoaded {
                    ProgressView().tint(Color.inkMuted).frame(maxWidth: .infinity, minHeight: 200)
                } else if model.requests.isEmpty {
                    empty
                } else {
                    VStack(spacing: 0) {
                        ForEach(Array(model.requests.enumerated()), id: \.element.id) { index, request in
                            if index > 0 { Hairline() }
                            row(request)
                        }
                    }
                    .padding(.top, 16)
                }
            }
            .padding(.horizontal, 22)
            .padding(.top, 8)
            .padding(.bottom, 32)
        }
        .scrollIndicators(.hidden)
        .background(CanvasBackground())
        .statusBarBackdrop()
        .refreshable { await model.load() }
        .task(id: isActive) {
            if isActive { await model.load() }
        }
    }

    private var empty: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("One bill. Paid at the source.")
                .font(Typeface.display(32, .black, relativeTo: .title))
                .tracking(-1)
                .foregroundStyle(Color.ink)
                .fixedSize(horizontal: false, vertical: true)
            Text("Send a photo of one bill in your name. A person checks it, givers cover it, and Paylee pays the company directly. It costs you nothing, and you never handle the money.")
                .font(Typeface.ui(17, .regular, relativeTo: .body))
                .foregroundStyle(Color.inkMuted)
                .lineSpacing(3)
        }
        .padding(.top, 20)
    }

    private func row(_ request: BillRequest) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                Text(request.billerName)
                    .font(Typeface.ui(18, .semibold, relativeTo: .headline))
                    .foregroundStyle(Color.ink)
                Spacer()
                Text(Money.format(request.targetCents))
                    .font(Typeface.ui(16, .semibold, relativeTo: .subheadline))
                    .foregroundStyle(Color.ink)
                    .monospacedDigit()
            }
            Text(statusLine(request))
                .font(Typeface.ui(15, .regular, relativeTo: .subheadline))
                .foregroundStyle(Color.inkMuted)
                .fixedSize(horizontal: false, vertical: true)
            if request.status == .live || request.status == .funded {
                ProgressBar(progress: request.progress)
            }
        }
        .padding(.vertical, 16)
        .accessibilityElement(children: .combine)
    }

    private func statusLine(_ request: BillRequest) -> String {
        switch request.status {
        case .submitted, .inReview: return request.statusNote ?? "A person is checking your bill. We'll email you either way."
        case .needsInfo: return request.statusNote ?? "We need a little more from you. Check your email."
        case .rejected: return request.statusNote ?? "We couldn't verify this bill."
        case .queued: return "Approved. It goes live as soon as a spot opens."
        case .live: return "Live. \(Money.format(request.amountRaisedCents)) of \(Money.format(request.targetCents)) covered. \(DuePhrase.text(for: request.dueDate))."
        case .funded: return "Fully funded. We'll confirm when payment is applied to your account. Tell us if the balance changes."
        case .paid: return "Paylee confirmed payment to \(request.billerName)."
        case .expired: return "This posting ended. Paylee is checking whether to pay the company or return the gifts."
        case .withdrawn: return "You withdrew this bill."
        case .removed: return "This bill was taken down after review."
        }
    }
}
