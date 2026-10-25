//
//  Typeface.swift
//  SwipeBills
//
//  One family, two widths. Archivo Expanded (heavy, wide) is for money and headlines; Archivo (normal width)
//  is for everything you read. Bundled under the SIL Open Font License (Fonts/Archivo-OFL.txt). Every style
//  is tied to a text style so it scales with Dynamic Type.
//

import CoreText
import SwiftUI

enum Typeface {
    enum Weight: String {
        case regular = "Regular"
        case medium = "Medium"
        case semibold = "SemiBold"
        case bold = "Bold"
    }

    enum DisplayWeight: String {
        case semibold = "SemiBold"
        case bold = "Bold"
        case extraBold = "ExtraBold"
        case black = "Black"
    }

    /// Registers the bundled fonts for this process, so no Info.plist entry is needed. Call once at launch.
    static func registerBundledFonts() {
        for url in Bundle.main.urls(forResourcesWithExtension: "ttf", subdirectory: nil) ?? [] {
            CTFontManagerRegisterFontsForURL(url as CFURL, .process, nil)
        }
    }

    /// Wide and heavy: amounts, headlines, stamps.
    static func display(_ size: CGFloat, _ weight: DisplayWeight = .extraBold, relativeTo style: Font.TextStyle = .largeTitle) -> Font {
        .custom("ArchivoExpanded-\(weight.rawValue)", size: size, relativeTo: style)
    }

    /// Interface and reading text.
    static func ui(_ size: CGFloat, _ weight: Weight = .regular, relativeTo style: Font.TextStyle = .body) -> Font {
        .custom("Archivo-\(weight.rawValue)", size: size, relativeTo: style)
    }
}
