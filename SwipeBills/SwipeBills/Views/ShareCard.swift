//
//  ShareCard.swift
//  SwipeBills
//
//  A 4:5 image a giver can post after chipping in. It names the kind of bill and the city, never the
//  person or the company, so sharing can't expose who was helped.
//

import SwiftUI

struct ShareCard: View {
    let category: BillCategory
    let city: String
    let amountCents: Int
    let closedBill: Bool
    var showsCity = true

    static let size = CGSize(width: 360, height: 450)

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("paylee")
                    .font(.custom("ArchivoExpanded-Black", fixedSize: 22))
                    .tracking(-0.6)
                Spacer()
                Text(closedBill ? "Closed" : "Chipped in")
                    .font(.custom("Archivo-SemiBold", fixedSize: 14))
            }
            .foregroundStyle(Color.onLime)

            Spacer()

            Text(closedBill
                 ? "I helped fully fund a stranger's \(category.label.lowercased()) bill."
                 : "I chipped in on a stranger's \(category.label.lowercased()) bill.")
                .font(.custom("ArchivoExpanded-Black", fixedSize: 33))
                .tracking(-1.2)
                .foregroundStyle(Color.onLime)
                .fixedSize(horizontal: false, vertical: true)

            Spacer()

            HStack(alignment: .lastTextBaseline) {
                AmountText(cents: amountCents, size: 40, color: .onLime, centsColor: Color.onLime.opacity(0.45), compactWholeDollars: true)
                Spacer()
                VStack(alignment: .trailing, spacing: 2) {
                    if showsCity {
                        Text(city)
                    }
                    Text("Paid to the company")
                }
                .font(.custom("Archivo-Medium", fixedSize: 12))
                .foregroundStyle(Color.onLime.opacity(0.7))
            }
        }
        .padding(30)
        .frame(width: Self.size.width, height: Self.size.height)
        .background(Color.lime)
    }

    /// Renders the card at 3x (1080 x 1350 pixels), the size social apps expect for portrait posts.
    func renderImage() -> Image? {
        let renderer = ImageRenderer(content: self)
        renderer.scale = 3
        return renderer.uiImage.map { Image(uiImage: $0) }
    }
}

#Preview {
    ShareCard(category: .utilities, city: "Lexington, KY", amountCents: 21_250, closedBill: true)
}
