// Face detector for the talking-head check: prints one JSON line per image path given on the command line,
// {"path": ..., "faces": [h, ...]} where each h is a detected face's height as a fraction of the frame.
// Uses macOS's built-in Vision framework, so nothing extra is installed. Compiled on first use by faces.py.
import Foundation
import Vision
import ImageIO

for path in CommandLine.arguments.dropFirst() {
    var heights: [Double] = []
    let url = URL(fileURLWithPath: path)
    if let src = CGImageSourceCreateWithURL(url as CFURL, nil),
       let img = CGImageSourceCreateImageAtIndex(src, 0, nil) {
        let req = VNDetectFaceRectanglesRequest()
        let handler = VNImageRequestHandler(cgImage: img, options: [:])
        try? handler.perform([req])
        for face in req.results ?? [] {
            heights.append(Double(face.boundingBox.height))
        }
    }
    let obj: [String: Any] = ["path": path, "faces": heights]
    if let data = try? JSONSerialization.data(withJSONObject: obj),
       let line = String(data: data, encoding: .utf8) {
        print(line)
    }
}
