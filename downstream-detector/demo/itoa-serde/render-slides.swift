import AppKit
import CoreText
import Foundation

let width = 1280
let height = 720
let output = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : ".local/walkthrough"
try FileManager.default.createDirectory(atPath: output, withIntermediateDirectories: true)

let ink = CGColor(red: 0.94, green: 0.96, blue: 1.0, alpha: 1)
let muted = CGColor(red: 0.58, green: 0.64, blue: 0.75, alpha: 1)
let mint = CGColor(red: 0.27, green: 0.91, blue: 0.72, alpha: 1)
let coral = CGColor(red: 1.0, green: 0.40, blue: 0.43, alpha: 1)
let blue = CGColor(red: 0.42, green: 0.63, blue: 1.0, alpha: 1)
let panel = CGColor(red: 0.08, green: 0.12, blue: 0.20, alpha: 1)
let border = CGColor(red: 0.18, green: 0.24, blue: 0.34, alpha: 1)

struct Card {
    let kicker: String
    let title: String
    let subtitle: String
    let kind: Int
}

let cards = [
    Card(kicker: "DOWNSTREAM DETECTOR  /  01", title: "Same API.\nDifferent behavior.", subtitle: "Catch the break before it reaches your users.", kind: 0),
    Card(kicker: "REAL PROJECTS  /  02", title: "Follow the\ndependency chain.", subtitle: "Pinned Rust crates. A real downstream test suite.", kind: 1),
    Card(kicker: "THE CHANGE  /  03", title: "One line changes\none result.", subtitle: "The API still compiles. The answer is now wrong.", kind: 2),
    Card(kicker: "AGENT SUBSTRATE  /  04", title: "Compare in an\nisolated actor.", subtitle: "Baseline and candidate run offline in the sandbox.", kind: 3),
    Card(kicker: "THE SIGNAL  /  05", title: "The consumer\ncatches the break.", subtitle: "3 downstream assertions fail. The baseline passes.", kind: 4),
    Card(kicker: "USE THE RESULT  /  06", title: "Inspect. Reproduce.\nThen decide.", subtitle: "Full backtraces and a local Docker command are included.", kind: 5),
]

func rect(_ c: CGContext, _ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat, _ color: CGColor) {
    c.setFillColor(color)
    c.fill(CGRect(x: x, y: CGFloat(height) - y - h, width: w, height: h))
}

func roundRect(_ c: CGContext, _ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat, _ radius: CGFloat, _ color: CGColor, stroke: CGColor? = nil) {
    let r = CGRect(x: x, y: CGFloat(height) - y - h, width: w, height: h)
    let path = CGPath(roundedRect: r, cornerWidth: radius, cornerHeight: radius, transform: nil)
    c.addPath(path)
    c.setFillColor(color)
    c.fillPath()
    if let stroke {
        c.addPath(path)
        c.setStrokeColor(stroke)
        c.setLineWidth(1.2)
        c.strokePath()
    }
}

func line(_ c: CGContext, _ x1: CGFloat, _ y1: CGFloat, _ x2: CGFloat, _ y2: CGFloat, _ color: CGColor, _ width: CGFloat = 2) {
    c.beginPath()
    c.move(to: CGPoint(x: x1, y: CGFloat(height) - y1))
    c.addLine(to: CGPoint(x: x2, y: CGFloat(height) - y2))
    c.setStrokeColor(color)
    c.setLineWidth(width)
    c.strokePath()
}

func label(_ c: CGContext, _ value: String, _ x: CGFloat, _ y: CGFloat, _ size: CGFloat, _ color: CGColor, bold: Bool = false) {
    let fontName = (bold ? "HelveticaNeue-Bold" : "HelveticaNeue") as CFString
    let font = CTFontCreateWithName(fontName, size, nil)
    let attrs: [NSAttributedString.Key: Any] = [
        NSAttributedString.Key(kCTFontAttributeName as String): font,
        NSAttributedString.Key(kCTForegroundColorAttributeName as String): color,
    ]
    for (i, row) in value.components(separatedBy: "\n").enumerated() {
        let line = CTLineCreateWithAttributedString(NSAttributedString(string: row, attributes: attrs))
        c.textPosition = CGPoint(x: x, y: CGFloat(height) - y - size - CGFloat(i) * (size * 1.22))
        CTLineDraw(line, c)
    }
}

func pill(_ c: CGContext, _ text: String, _ x: CGFloat, _ y: CGFloat, _ color: CGColor, width: CGFloat) {
    roundRect(c, x, y, width, 34, 17, CGColor(red: color.components![0], green: color.components![1], blue: color.components![2], alpha: 0.12), stroke: color)
    label(c, text, x + 14, y + 8, 13, color, bold: true)
}

func backdrop(_ c: CGContext, _ index: Int) {
    let colors = [
        CGColor(red: 0.035, green: 0.055, blue: 0.10, alpha: 1),
        CGColor(red: 0.055, green: 0.085, blue: 0.15, alpha: 1),
    ] as CFArray
    let gradient = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(), colors: colors, locations: [0, 1])!
    c.drawLinearGradient(gradient, start: CGPoint(x: 0, y: 0), end: CGPoint(x: CGFloat(width), y: CGFloat(height)), options: [])
    c.setStrokeColor(CGColor(red: 0.18, green: 0.24, blue: 0.34, alpha: 0.24))
    c.setLineWidth(1)
    for x in stride(from: 40, through: width, by: 64) {
        for y in stride(from: 24, through: height, by: 64) {
            c.strokeEllipse(in: CGRect(x: x, y: height - y, width: 2, height: 2))
        }
    }
    // Slim progress marker gives every slide a shared visual timeline.
    roundRect(c, 64, 42, 1152, 3, 2, CGColor(red: 0.18, green: 0.24, blue: 0.34, alpha: 1))
    roundRect(c, 64, 42, CGFloat(index + 1) * 192, 3, 2, mint)
}

func diagram(_ c: CGContext, kind: Int) {
    let x: CGFloat = 700
    if kind == 0 {
        roundRect(c, x, 170, 490, 410, 24, panel, stroke: border)
        label(c, "THE COMPATIBILITY CHECK", x + 30, 198, 14, muted, bold: true)
        roundRect(c, x + 30, 254, 430, 106, 16, CGColor(red: 0.08, green: 0.21, blue: 0.19, alpha: 1), stroke: mint)
        label(c, "BASELINE", x + 52, 272, 13, mint, bold: true)
        label(c, "consumer tests  ✓", x + 52, 306, 24, ink, bold: true)
        roundRect(c, x + 30, 382, 430, 106, 16, CGColor(red: 0.22, green: 0.10, blue: 0.15, alpha: 1), stroke: coral)
        label(c, "CANDIDATE", x + 52, 400, 13, coral, bold: true)
        label(c, "consumer tests  ×", x + 52, 434, 24, ink, bold: true)
        label(c, "Same downstream project. Different upstream revision.", x + 31, 525, 14, muted)
    } else if kind == 1 {
        roundRect(c, x, 190, 490, 330, 22, panel, stroke: border)
        label(c, "UPSTREAM", x + 32, 225, 13, muted, bold: true)
        label(c, "itoa", x + 32, 254, 38, ink, bold: true)
        label(c, "small, fast integer formatting", x + 32, 302, 16, muted)
        line(c, x + 32, 345, x + 458, 345, border, 1)
        label(c, "DOWNSTREAM CONSUMER", x + 32, 373, 13, muted, bold: true)
        label(c, "serde_json", x + 32, 402, 34, ink, bold: true)
        label(c, "JSON serialization and debug tests", x + 32, 450, 16, muted)
        pill(c, "PINNED COMMITS", x + 32, 474, blue, width: 154)
    } else if kind == 2 {
        roundRect(c, x, 190, 490, 330, 22, panel, stroke: border)
        label(c, "BEFORE  ·  INTEGER FORMATTER", x + 30, 225, 13, muted, bold: true)
        roundRect(c, x + 28, 258, 434, 82, 12, CGColor(red: 0.06, green: 0.09, blue: 0.15, alpha: 1), stroke: border)
        label(c, "format(1)  →  \"1\"", x + 50, 281, 24, mint, bold: true)
        label(c, "AFTER  ·  SAME SIGNATURE", x + 30, 368, 13, muted, bold: true)
        roundRect(c, x + 28, 400, 434, 82, 12, CGColor(red: 0.15, green: 0.07, blue: 0.12, alpha: 1), stroke: coral)
        label(c, "format(1)  →  \"2\"", x + 50, 423, 24, coral, bold: true)
        pill(c, "SOURCE API UNCHANGED", x + 29, 497, mint, width: 198)
    } else if kind == 3 {
        roundRect(c, x, 190, 490, 330, 22, panel, stroke: border)
        pill(c, "AGENT SUBSTRATE ACTOR", x + 28, 216, blue, width: 210)
        roundRect(c, x + 28, 270, 206, 130, 16, CGColor(red: 0.08, green: 0.21, blue: 0.19, alpha: 1), stroke: mint)
        label(c, "BASELINE", x + 48, 291, 13, mint, bold: true)
        label(c, "cargo test", x + 48, 324, 20, ink, bold: true)
        label(c, "PASS", x + 48, 359, 17, mint, bold: true)
        roundRect(c, x + 256, 270, 206, 130, 16, CGColor(red: 0.22, green: 0.10, blue: 0.15, alpha: 1), stroke: coral)
        label(c, "CANDIDATE", x + 276, 291, 13, coral, bold: true)
        label(c, "cargo test", x + 276, 324, 20, ink, bold: true)
        label(c, "FAIL", x + 276, 359, 17, coral, bold: true)
        line(c, x + 234, 334, x + 256, 334, muted, 2)
        label(c, "NETWORK OFF  ·  RESOURCE LIMITED  ·  FULL OUTPUT SAVED", x + 29, 449, 13, muted, bold: true)
    } else if kind == 4 {
        roundRect(c, x, 190, 490, 330, 22, panel, stroke: border)
        pill(c, "POTENTIAL REGRESSION", x + 28, 216, coral, width: 220)
        let names = ["number", "indented", "value_number"]
        for (i, name) in names.enumerated() {
            let yy = CGFloat(280 + i * 62)
            roundRect(c, x + 28, yy, 434, 48, 11, CGColor(red: 0.14, green: 0.075, blue: 0.12, alpha: 1))
            label(c, "×", x + 45, yy + 12, 20, coral, bold: true)
            label(c, name, x + 80, yy + 14, 18, ink, bold: true)
            label(c, "expected 1 · got 2", x + 275, yy + 15, 14, muted)
        }
        label(c, "Full Rust backtraces captured for every failure.", x + 30, 481, 14, muted)
    } else {
        roundRect(c, x, 190, 490, 330, 22, panel, stroke: border)
        label(c, "ONE COMMAND TO REPLAY", x + 28, 220, 13, muted, bold: true)
        roundRect(c, x + 26, 254, 438, 142, 14, CGColor(red: 0.035, green: 0.05, blue: 0.085, alpha: 1), stroke: border)
        label(c, "docker run --rm --network none\n  downstream-detector:itoa-serde-demo compare\n  /opt/real-demo/itoa demo-baseline HEAD itoa\n  /opt/real-demo/serde-json HEAD", x + 48, 274, 14, mint, bold: true)
        line(c, x + 28, 420, x + 462, 420, border, 1)
        label(c, "PINNED INPUTS", x + 30, 444, 12, muted, bold: true)
        label(c, "FULL REPORT   ·   STACK TRACES   ·   SOURCE PATCH", x + 30, 472, 13, ink, bold: true)
    }
}

func draw(_ card: Card, index: Int) throws {
    let space = CGColorSpaceCreateDeviceRGB()
    let context = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width * 4, space: space, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    backdrop(context, index)
    label(context, card.kicker, 64, 86, 14, mint, bold: true)
    label(context, card.title, 64, 162, 58, ink, bold: true)
    label(context, card.subtitle, 66, 334, 20, muted)
    diagram(context, kind: card.kind)
    label(context, "DOWNSTREAM DETECTOR", 64, 635, 13, muted, bold: true)
    label(context, "RUST  /  AGENT SUBSTRATE", 980, 635, 13, muted, bold: true)
    let image = context.makeImage()!
    let bitmap = NSBitmapImageRep(cgImage: image)
    let data = bitmap.representation(using: .png, properties: [:])!
    let url = URL(fileURLWithPath: output).appendingPathComponent(String(format: "scene-%02d.png", index + 1))
    try data.write(to: url)
}

for (i, card) in cards.enumerated() {
    try draw(card, index: i)
}
