//
//  SampleBills.swift
//  SwipeBills
//
//  Local stand-in data until the feed comes from the backend.
//

import Foundation

extension Bill {
    static let samples: [Bill] = [
        Bill(
            id: UUID(),
            billerName: "Kentucky Utilities",
            category: .utilities,
            requesterFirstName: "Maria",
            city: "Lexington, KY",
            amountDueCents: 21_250,
            amountRaisedCents: 8_000,
            story: "My hours got cut this fall and I fell behind. I have two kids and a shutoff notice for next week. I'm back to full shifts in November.",
            dueDate: .now.addingTimeInterval(5 * 86_400)
        ),
        Bill(
            id: UUID(),
            billerName: "Baptist Health",
            category: .medical,
            requesterFirstName: "Dev",
            city: "Louisville, KY",
            amountDueCents: 64_000,
            amountRaisedCents: 51_500,
            story: "ER visit after a bike accident. Insurance covered most of it but this is what's left, and it just went to collections warning.",
            dueDate: .now.addingTimeInterval(12 * 86_400)
        ),
        Bill(
            id: UUID(),
            billerName: "Verizon",
            category: .phone,
            requesterFirstName: "Tasha",
            city: "Columbus, OH",
            amountDueCents: 8_900,
            amountRaisedCents: 0,
            story: "I'm job hunting and my phone is how employers reach me. One month would get me through to my first paycheck.",
            dueDate: .now.addingTimeInterval(3 * 86_400)
        ),
        Bill(
            id: UUID(),
            billerName: "Park Place Apartments",
            category: .rent,
            requesterFirstName: "James",
            city: "Cincinnati, OH",
            amountDueCents: 115_000,
            amountRaisedCents: 30_000,
            story: "My roommate moved out without notice and left me with the full rent. I've found a new roommate starting next month.",
            dueDate: .now.addingTimeInterval(8 * 86_400)
        ),
        Bill(
            id: UUID(),
            billerName: "Spectrum",
            category: .internet,
            requesterFirstName: "Lin",
            city: "Nashville, TN",
            amountDueCents: 7_499,
            amountRaisedCents: 2_000,
            story: "I'm finishing an online nursing program and need internet for my last two exams.",
            dueDate: .now.addingTimeInterval(6 * 86_400)
        ),
        Bill(
            id: UUID(),
            billerName: "Atmos Energy",
            category: .utilities,
            requesterFirstName: "Robert",
            city: "Bowling Green, KY",
            amountDueCents: 13_420,
            amountRaisedCents: 11_000,
            story: "Retired and on a fixed income. The gas bill doubled when it got cold and I'm a little short this month.",
            dueDate: .now.addingTimeInterval(4 * 86_400)
        ),
    ]
}
