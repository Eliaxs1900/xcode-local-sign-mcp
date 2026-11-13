// Renders a small marked-up text file as a terminal-style PNG.
// usage: swift render.swift in.txt out.png
//   "# "  title      "## " section      "> " command (blue)
//   "+ "  good (green)   "- " bad (red)   "! " note (yellow)   other: plain
import AppKit

let args = CommandLine.arguments
let text = try! String(contentsOfFile: args[1], encoding: .utf8)
    .trimmingCharacters(in: .whitespacesAndNewlines)
let mono = NSFont.monospacedSystemFont(ofSize: 13, weight: .regular)
let monoBold = NSFont.monospacedSystemFont(ofSize: 13, weight: .bold)
let titleFont = NSFont.systemFont(ofSize: 18, weight: .semibold)
func rgb(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat) -> NSColor { NSColor(srgbRed: r, green: g, blue: b, alpha: 1) }
let plain = rgb(0.86, 0.87, 0.89), white = rgb(1, 1, 1)
let marks: [String: NSColor] = ["+ ": rgb(0.47, 0.86, 0.47), "- ": rgb(0.98, 0.47, 0.44),
                                "> ": rgb(0.55, 0.72, 0.98), "! ": rgb(0.96, 0.80, 0.36)]
let background = rgb(0.10, 0.11, 0.13)

let doc = NSMutableAttributedString()
let para = NSMutableParagraphStyle()
para.lineSpacing = 2
let rawLines = text.components(separatedBy: "\n")
for (i, raw) in rawLines.enumerated() {
    var line = raw, font = mono, color = plain
    if line.hasPrefix("# ") { line.removeFirst(2); font = titleFont; color = white }
    else if line.hasPrefix("## ") { line.removeFirst(3); font = monoBold; color = white }
    else if let m = marks.first(where: { line.hasPrefix($0.key) }) { color = m.value }
    doc.append(NSAttributedString(string: line + (i < rawLines.count - 1 ? "\n" : ""),
                                  attributes: [.font: font, .foregroundColor: color, .paragraphStyle: para]))
}

let pad: CGFloat = 28
let box = doc.boundingRect(with: NSSize(width: 1500, height: 100_000),
                           options: [.usesLineFragmentOrigin, .usesFontLeading])
let size = NSSize(width: ceil(box.width) + 2 * pad, height: ceil(box.height) + 2 * pad)
let scale: CGFloat = 2
let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width * scale),
                           pixelsHigh: Int(size.height * scale), bitsPerSample: 8, samplesPerPixel: 4,
                           hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                           bytesPerRow: 0, bitsPerPixel: 0)!
rep.size = size
NSGraphicsContext.saveGraphicsState()
let ctx = NSGraphicsContext(bitmapImageRep: rep)!
NSGraphicsContext.current = ctx
background.setFill()
NSBezierPath(roundedRect: NSRect(origin: .zero, size: size), xRadius: 10, yRadius: 10).fill()
doc.draw(with: NSRect(x: pad, y: pad, width: size.width - 2 * pad, height: size.height - 2 * pad),
         options: [.usesLineFragmentOrigin, .usesFontLeading])
NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: args[2]))
