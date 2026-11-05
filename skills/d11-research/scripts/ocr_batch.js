/*
 * macOS Vision 框架批量 OCR。
 * 用法: osascript -l JavaScript ocr_batch.js <输出json路径> <图片1> <图片2> ...
 * 输出: [{"path": "...", "text": "..."}]
 */
ObjC.import('Vision');
ObjC.import('Foundation');
ObjC.import('AppKit');

function recognize(p) {
  const url = $.NSURL.fileURLWithPath(p);
  const handler = $.VNImageRequestHandler.alloc.initWithURLOptions(url, $());
  const req = $.VNRecognizeTextRequest.alloc.init;
  req.recognitionLevel = 0;              // 0 = accurate
  req.usesLanguageCorrection = true;
  req.recognitionLanguages = ['zh-Hans', 'en-US'];
  const ok = handler.performRequestsError($([req]), $());
  if (!ok) return '';
  const rs = req.results;
  const parts = [];
  for (let i = 0; i < rs.count; i++) {
    const cands = rs.objectAtIndex(i).topCandidates(1);
    if (cands.count > 0) parts.push(String(cands.objectAtIndex(0).string.js));
  }
  return parts.join(' ');
}

function run(argv) {
  if (argv.length < 2) return 'ERR: need output path + at least one image';
  const outPath = argv[0];
  const results = [];
  for (let i = 1; i < argv.length; i++) {
    const p = argv[i];
    let text = '';
    try { text = recognize(p); } catch (e) { text = ''; }
    results.push({ path: p, text: text });
  }
  const json = JSON.stringify(results);
  const ns = $(json);
  ns.writeToFileAtomicallyEncodingError(outPath, true, $.NSUTF8StringEncoding, $());
  return 'OK:' + results.length;
}
