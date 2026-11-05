"""中文促销资料 Word 报告。公开入口为 write_report(path, data)。

仅依赖 python-docx、Pillow 和标准库。不联网，不修改输入或图片。
scope/data_scope 可描述数据范围；sample=True 可标记整份输入或单条记录。
"""

import io
import math
import os
import re
import tempfile
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from docx.text.paragraph import Paragraph
from PIL import Image, ImageOps

__all__ = ["write_report"]

_MISSING = "未提供"
_KIND = {"campaign": "活动页面", "current": "当前页面", "offer": "优惠记录", "rule": "规则记录"}
_ROLE = {"own": "本品", "competitor": "竞品"}
_RULE_WORDS = ("规则", "叠加", "适用范围", "使用条件", "参与条件", "活动时间", "有效期", "门槛")
_SAMPLE_SCOPE = {"true", "1", "sample", "sample only", "sample data", "demo", "example",
                 "fixture", "mock", "synthetic", "示例", "样例", "演示", "模拟", "示例数据",
                 "样例数据", "演示数据", "模拟数据", "仅示例", "仅样例"}
_XML_UNSAFE = re.compile(r"[^\t\n\r\x20-\uD7FF\uE000-\uFFFD\U00010000-\U0010ffff]")
_WIDTH = 6.9  # Letter, 0.8 inch left and right margins


def _text(value):
    if value is None:
        return ""
    return _XML_UNSAFE.sub("", str(value)).replace("\r\n", "\n").replace("\r", "\n").strip()


def _values(value):
    if value is None:
        return ()
    return (value,) if isinstance(value, (str, os.PathLike)) else value


def _strings(values):
    return list(dict.fromkeys(s for v in _values(values) if (s := _text(v))))


def _get(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, Mapping) else getattr(obj, key, default)


def _scope(obj):
    return _text(_get(obj, "scope")) or _text(_get(obj, "data_scope"))


def _sample(obj):
    def marked(value):
        return re.sub(r"[-_\s]+", " ", _text(value).casefold()) in _SAMPLE_SCOPE

    return marked(_get(obj, "sample")) or marked(_scope(obj))


def _web_address(value):
    """Only create a network hyperlink for an absolute http(s) source supplied by the caller."""
    if not value or any(c.isspace() for c in value) or "\\" in value:
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() in ("http", "https") and parsed.hostname and parsed.username is None:
            _ = parsed.port  # Validate the port too.
            return value
    except ValueError:
        pass
    return ""


@dataclass
class _Entry:
    number: int
    sample: bool
    product: str
    role: str
    brand: str
    platform: str
    shop: str
    sku: str
    observed_at: str
    url: str
    kind: str
    evidence_id: str
    excerpt: str
    scope: str
    fields: tuple
    assets: tuple

    @classmethod
    def read(cls, number, item, sample):
        attrs = {key: _text(_get(item, key)) for key in (
            "product", "role", "brand", "platform", "shop", "sku", "observed_at", "url",
            "kind", "evidence_id", "excerpt")}
        attrs["role"] = attrs["role"].lower()
        attrs["kind"] = attrs["kind"].lower()
        fields = _get(item, "fields") or {}
        return cls(number, sample, **attrs, scope=_scope(item),
                   fields=tuple((_text(k) or "未命名字段", _text(v) or _MISSING)
                                for k, v in fields.items()),
                   assets=tuple(_values(_get(item, "assets"))))

    @property
    def ref(self):
        return f"{'示例' if self.sample else '记录'} {self.number:02d}"

    @property
    def anchor(self):
        return f"d11_{'sample' if self.sample else 'record'}_{self.number}"

    @property
    def rule_fields(self):
        return [(k, v) for k, v in self.fields if any(word in k for word in _RULE_WORDS)]


def _el(tag, **attrs):
    node = OxmlElement(tag)
    for key, value in attrs.items():
        node.set(qn(key), str(value))
    return node


def _style(doc, name, size, *, color="24292D", bold=False, before=0, after=3, keep=False):
    styles = doc.styles
    style = styles[name] if name in styles else styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
    if name != "Normal":
        style.base_style = styles["Normal"]
    style.font.name = "Arial"
    style.font.size = Pt(size)
    style.font.bold = bold
    style.font.italic = False
    style.font.underline = False
    style.font.color.rgb = RGBColor.from_string(color)
    rp = style.element.get_or_add_rPr()
    fonts = rp.rFonts
    for key in list(fonts.attrib):
        if "theme" in key.lower():
            del fonts.attrib[key]
    fonts.set(qn("w:eastAsia"), "PingFang SC")
    lang = rp.find(qn("w:lang"))
    if lang is None:
        lang = _el("w:lang")
        rp.append(lang)
    lang.set(qn("w:eastAsia"), "zh-CN")
    pf = style.paragraph_format
    pf.space_before, pf.space_after = Pt(before), Pt(after)
    # CJK font metrics vary widely between Word and headless Writer. Give editable
    # text a generous baseline; inline figures alone need an automatic line height.
    pf.line_spacing = 1.0 if name == "Report Figure" else Pt(round(size * 1.55, 1))
    pf.keep_with_next, pf.keep_together, pf.widow_control = keep, keep, True
    ppr = style.element.get_or_add_pPr()
    for border in ppr.findall(qn("w:pBdr")):
        ppr.remove(border)
    return style


def _document(title):
    doc = Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Inches(8.5), Inches(11)
    section.left_margin = section.right_margin = Inches(0.8)
    section.top_margin, section.bottom_margin = Inches(0.72), Inches(0.70)
    section.footer_distance = Inches(0.30)
    # The stock template has a document grid and theme fonts. Use explicit Chinese type.
    grid = section._sectPr.find(qn("w:docGrid"))
    if grid is not None:
        section._sectPr.remove(grid)
    _style(doc, "Normal", 11)
    _style(doc, "Title", 25, color="000000", bold=True, after=7, keep=True)
    _style(doc, "Heading 1", 17, color="000000", bold=True, before=13, after=5, keep=True)
    _style(doc, "Heading 2", 13.5, color="000000", bold=True, before=7, after=3, keep=True)
    _style(doc, "Heading 3", 12, color="000000", bold=True, before=6, after=3, keep=True)
    _style(doc, "Report Meta", 10, color="45535D", after=3)
    _style(doc, "Report Detail", 10.5, after=2)
    _style(doc, "Report Source", 9.5, color="41515C", after=4)
    _style(doc, "Report Quote", 10.5, color="34434D", before=2, after=3)
    doc.styles["Report Quote"].paragraph_format.left_indent = Inches(0.16)
    _style(doc, "Report Cell", 10, after=0)
    _style(doc, "Report Figure", 9, before=4, after=2, keep=True)
    _style(doc, "Report Caption", 9, color="45535D", after=5)
    _style(doc, "Report Footer", 9, color="596770", after=0)
    footer = section.footer.paragraphs[0]
    footer.style = "Report Footer"
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    footer.add_run("第 ")
    page = _el("w:fldSimple", **{"w:instr": " PAGE "})
    page.append(_el("w:r"))
    page[0].append(_el("w:t"))
    page[0][0].text = "1"
    footer._p.append(page)
    footer.add_run(" 页")
    props = doc.core_properties
    props.title, props.subject, props.language = title[:255], "商品促销资料与证据缺口", "zh-CN"
    props.author, props.comments = "", ""
    doc.add_paragraph(title, "Title")
    return doc


def _label(p, label, value):
    p.add_run(label + "：").bold = True
    p.add_run(value)


def _line(doc, items, style="Report Detail"):
    p = doc.add_paragraph(style=style)
    for i, (label, value) in enumerate(items):
        if i:
            p.add_run("　　")
        _label(p, label, value)
    return p


def _link(p, label, *, target=None, anchor=None):
    link = _el("w:hyperlink", **{"w:history": "1"})
    if target is not None:
        link.set(qn("r:id"), p.part.relate_to(target, RT.HYPERLINK, is_external=True))
    else:
        link.set(qn("w:anchor"), anchor)
    run = p.add_run(label)
    run.font.color.rgb = RGBColor.from_string("23566F")
    run.underline = True
    link.append(run._r)
    p._p.append(link)


def _anchor(p, e):
    # The numeric bookmark id and its name must both be unique, even for duplicate evidence IDs.
    ident = 2 * e.number + int(e.sample)
    start = _el("w:bookmarkStart", **{"w:id": ident, "w:name": e.anchor})
    end = _el("w:bookmarkEnd", **{"w:id": ident})
    p._p.insert(1, start)  # after the paragraph properties
    p._p.append(end)


def _coverage(doc, platforms, entries):
    kinds = list(_KIND)
    if any(e.kind not in _KIND for e in entries):
        kinds.append("other")
    counts = Counter((e.platform, e.kind if e.kind in _KIND else "other") for e in entries)
    labels = ["平台"] + [_KIND.get(k, "其他记录") for k in kinds]
    rows = [labels] + [[pl or "平台待核"] + [
        f"{n} 条" if (n := counts[pl, k]) else "待补" for k in kinds] for pl in platforms]
    table = doc.add_table(rows=0, cols=len(labels))
    table.alignment, table.autofit = WD_TABLE_ALIGNMENT.CENTER, False
    widths = [2.0] + [(_WIDTH - 2.0) / len(kinds)] * len(kinds)
    for col, width in zip(table.columns, widths):
        col.width = Inches(width)
    pr = table._tbl.tblPr
    borders = _el("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        borders.append(_el("w:" + edge, **{"w:val": "single", "w:sz": 5, "w:color": "D9D9D9"}))
    pr.append(borders)
    margins = _el("w:tblCellMar")
    for edge, val in (("top", 55), ("bottom", 55), ("left", 95), ("right", 95)):
        margins.append(_el("w:" + edge, **{"w:w": val, "w:type": "dxa"}))
    pr.append(margins)
    for i, values in enumerate(rows):
        row = table.add_row()
        trpr = row._tr.get_or_add_trPr()
        if i == 0:
            trpr.append(_el("w:tblHeader", **{"w:val": "true"}))
        for j, (cell, val, width) in enumerate(zip(row.cells, values, widths)):
            cell.width = Inches(width)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            fill = "344A59" if not i else ("FFFFFF" if i % 2 else "F1F5F6")
            cell._tc.get_or_add_tcPr().append(_el("w:shd", **{"w:fill": fill, "w:val": "clear"}))
            p = cell.paragraphs[0]
            p.style = "Report Cell"
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT if j == 0 else WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.keep_with_next = not i
            run = p.add_run(val)
            run.bold = not i
            if not i:
                run.font.color.rgb = RGBColor(255, 255, 255)
            elif j and val == "待补":
                run.font.color.rgb = RGBColor.from_string("596770")


def _panels(path):
    """Yield PNG screenshot previews (or JPEG for photographs), entirely in memory."""
    with Image.open(path) as source:
        if source.format not in {"PNG", "JPEG", "WEBP", "GIF", "BMP", "TIFF", "ICO", "AVIF",
                                 "JPEG2000", "HEIF", "MPO"}:
            raise ValueError("unsupported raster format")
        if Image.MAX_IMAGE_PIXELS and source.width * source.height > Image.MAX_IMAGE_PIXELS:
            raise Image.DecompressionBombError("image exceeds decoding limit")
        first_frame = getattr(source, "n_frames", 1) > 1
        oriented = ImageOps.exif_transpose(source)
        w, h = oriented.size
        # Divide a long phone capture in reading order. No pixels between slices are skipped.
        parts = min(8, math.ceil(h / max(1.55 * w, 450))) if h > 2.5 * w and w >= 200 else 1
        is_jpeg = source.format in ("JPEG", "MPO")
        for i in range(parts):
            panel = oriented.crop((0, i * h // parts, w, (i + 1) * h // parts))
            pw, ph = panel.size
            scale = min((_WIDTH - 0.1) / pw, 3.85 / ph, 1 / 105)
            # Preserve text/alpha on screenshots. Strip file metadata on embedded renditions.
            alpha = "A" in panel.getbands() or "transparency" in panel.info
            panel = panel.convert("RGBA" if alpha else "RGB")
            panel.info.clear()
            panel.thumbnail((2200, 2600), Image.Resampling.LANCZOS)
            stream = io.BytesIO()
            if is_jpeg and not alpha:
                panel.save(stream, format="JPEG", quality=93, subsampling=0)
            else:
                panel.save(stream, format="PNG")
            stream.seek(0)
            yield stream, pw * scale, ph * scale, i + 1, parts, first_frame


def _pictures(doc, e):
    for i, raw_path in enumerate(e.assets, 1):
        path = Path(os.fspath(raw_path)) if isinstance(raw_path, (str, os.PathLike)) else Path()
        name = _text(path.name) or "未命名附件"
        label = f"配图 {i}"
        reason = ""
        if not path.is_absolute():
            reason = "未提供绝对本地路径"
        else:
            try:
                if not path.is_file():
                    raise FileNotFoundError
                for stream, width, height, part, total, first in _panels(path):
                    p = doc.add_paragraph(style="Report Figure")
                    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    with stream:
                        shape = p.add_run().add_picture(stream, width=Inches(width), height=Inches(height))
                    suffix = (f"（自上而下第 {part}/{total} 段）" if total > 1 else "")
                    if first:
                        suffix += "（首帧）"
                    shape._inline.docPr.set("descr", f"{e.ref}，{label}：{name}{suffix}")
                    p = doc.add_paragraph(style="Report Caption")
                    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    _link(p, e.ref, anchor=e.anchor)
                    p.add_run(f"　{label}：{name}{suffix}")
            except FileNotFoundError:
                reason = "本地文件不存在或不是普通文件"
            except Image.DecompressionBombError:
                reason = "图片尺寸超出解码上限"
            except (OSError, ValueError, SyntaxError, EOFError):
                reason = "文件不可读或不是支持的图片"
        if reason:
            doc.add_paragraph(f"{label} 未嵌入：{name}（{reason}）。", "Report Source")


def _fields(doc, pairs):
    # Put short, comparable values on one line, leaving conditions and long text room to wrap.
    batch = []
    units = 0
    for key, value in pairs:
        size = len(key) + len(value) + 3
        if batch and (len(batch) >= 2 or size + units > 35 or "\n" in key + value):
            _line(doc, batch)
            batch, units = [], 0
        batch.append((key, value))
        units += size
        if "\n" in key + value:
            _line(doc, batch)
            batch, units = [], 0
    if batch:
        _line(doc, batch)


def _entry(doc, e, *, show_platform=False):
    name = e.product or ("规则资料" if e.kind == "rule" else "商品未注明")
    p = doc.add_heading(e.ref + "　" + name, level=3)
    _anchor(p, e)
    heading = p._p
    role = _ROLE.get(e.role, f"归属待核（{e.role}）" if e.role else "归属待核")
    kind = _KIND.get(e.kind, f"类型待核（{e.kind}）" if e.kind else "类型待核")
    p = doc.add_paragraph(style="Report Meta")
    p.add_run(" · ".join((role, kind))).bold = True
    for key, value in (("平台", (e.platform or "待核") if show_platform else ""),
                       ("品牌", e.brand), ("店铺", e.shop), ("SKU", e.sku)):
        if value:
            p.add_run(f"　{key}：{value}")
    if e.scope:
        _line(doc, [("资料范围", e.scope)], "Report Meta")
    if e.fields:
        _fields(doc, e.fields)
    elif not e.excerpt:
        doc.add_paragraph("未提供分项字段或原文摘录。", "Report Detail")
    if e.excerpt:
        _line(doc, [("原文摘录", e.excerpt)], "Report Quote")
    _line(doc, [("证据编号", e.evidence_id or _MISSING), ("采集时间", e.observed_at or _MISSING)],
          "Report Source")
    p = doc.add_paragraph(style="Report Source")
    p.add_run("来源：").bold = True
    url = _web_address(e.url)
    if url:
        label = url if len(url) <= 70 else f"打开原始页面（{urlsplit(url).hostname}）"
        _link(p, label, target=url)
    else:
        p.add_run("链接格式不可用" if e.url else _MISSING)
    if not e.assets:
        p.add_run("　　配图：未提供")

    # A short card and its provenance fit on one page. Long excerpts/field lists may
    # flow, but keep the identity with the first field and the two source lines together.
    text_block = [Paragraph(n, doc._body) for n in heading.itersiblings()
                  if n.tag == qn("w:p")]
    short = (len(text_block) <= 9 and
             sum(len(p.text) + 36 * p.text.count("\n") for p in text_block) <= 420)
    for paragraph in (text_block[:-1] if short else (text_block[0], text_block[-2])):
        paragraph.paragraph_format.keep_with_next = True
    if e.assets:
        _pictures(doc, e)


def _reference(doc, e, explanation):
    p = doc.add_paragraph(style="Report Detail")
    _link(p, e.ref, anchor=e.anchor)
    names = " / ".join(x for x in (e.product, e.shop or e.brand) if x)
    p.add_run(f"　{names}　{explanation}" if names else f"　{explanation}")


def _sections(doc, records, platforms):
    by_platform = defaultdict(list)
    for e in records:
        by_platform[e.platform].append(e)

    doc.add_heading("各平台活动规则", 1)
    if any(e.kind == "rule" or e.rule_fields for e in records):
        doc.add_paragraph("规则按来源平台整理；适用店铺、商品和使用条件以各条原文为准。")
        for platform in platforms:
            rules = [e for e in by_platform[platform] if e.kind == "rule"]
            mentions = [(e, e.rule_fields) for e in by_platform[platform] if e.kind != "rule" and e.rule_fields]
            if not (rules or mentions):
                continue
            doc.add_heading(platform or "平台待核", 2)
            for e in rules:
                _entry(doc, e)
            if mentions:
                p = doc.add_paragraph("商品或优惠页面另列的条件", "Report Detail")
                p.runs[0].bold = True
                p.paragraph_format.keep_with_next = True
                for e, fields in mentions:
                    _reference(doc, e, "；".join(k + "：" + v for k, v in fields))
    else:
        doc.add_paragraph("尚未收录独立的规则记录或单列的使用条件，平台规则待核。")

    doc.add_heading("商品价格与福利", 1)
    if any(e.role != "competitor" and e.kind != "rule" for e in records):
        doc.add_paragraph("当前页面的标价只对应采集时点。活动价、优惠门槛和赠品按原记录列示，不能据此推算成交价。")
        for platform in platforms:
            group = [e for e in by_platform[platform] if e.role != "competitor" and e.kind != "rule"]
            if group:
                doc.add_heading(platform or "平台待核", 2)
                for e in group:
                    _entry(doc, e)
    else:
        doc.add_paragraph("尚未收录可用于核对本品价格或福利的商品、优惠页面记录。")

    doc.add_heading("竞品资料", 1)
    if any(e.role == "competitor" for e in records):
        for platform in platforms:
            group = [e for e in by_platform[platform] if e.role == "competitor"]
            if group:
                doc.add_heading(platform or "平台待核", 2)
                for e in group:
                    if e.kind == "rule":
                        _reference(doc, e, "竞品规则的原文和来源见上方链接。")
                    else:
                        _entry(doc, e)
    else:
        doc.add_paragraph("尚未收录标为竞品的记录，无法比较竞品价格或福利。")


def _actions(doc, records, products, competitors, gaps, suggestions):
    doc.add_heading("证据缺口与后续动作", 1)
    doc.add_heading("证据缺口", 2)
    notes = list(gaps)
    missing_sources = defaultdict(list)
    if not records:
        notes.append("缺少可核对的商品或规则记录，活动价、福利和竞品差异均待核。")
    else:
        for e in records:
            absent = tuple(k for k, value in (("网页来源链接", _web_address(e.url)),
                           ("采集时间", e.observed_at), ("证据编号", e.evidence_id)) if not value)
            if absent:
                missing_sources[absent].append(e)
        own_names = {e.product for e in records if e.role == "own"}
        comp_names = {v for e in records if e.role == "competitor" for v in (e.product, e.brand, e.shop)}
        for label, targets, names in (("尚无同名本品记录", products, own_names),
                                     ("尚未匹配到同名竞品、品牌或店铺", competitors, comp_names)):
            absent = [n for n in targets if n not in names]
            if absent:
                notes.append(f"{label}：{'、'.join(absent)}。")
    for note in dict.fromkeys(notes or ([] if missing_sources else ["未另列待补事项。"])):
        doc.add_paragraph(note, "Report Detail")
    for keys, group in missing_sources.items():
        p = doc.add_paragraph(style="Report Detail")
        for i, e in enumerate(group[:6]):
            if i:
                p.add_run("、")
            _link(p, e.ref, anchor=e.anchor)
        if len(group) > 6:
            p.add_run(f" 等 {len(group)} 条记录")
        p.add_run("：缺少" + "、".join(keys) + "。")
    doc.add_heading("建议动作", 2)
    actions = suggestions or (["先补录目标平台的来源链接、采集时间和原文，再比较同规格商品。"]
                              if not records else ["未提供具体建议。"])
    for action in actions:
        doc.add_paragraph(action, "Report Detail")


def _candidate_gallery(doc, refs):
    if not refs:
        return
    doc.add_heading("商品认款参考", 1)
    doc.add_paragraph("下列图片用于辨认商品和规格。品牌或店铺参考图不证明本期活动、渠道价格或当前包装版本。", "Report Detail")
    cols = min(3, len(refs))
    for start in range(0, len(refs), cols):
        group = refs[start:start + cols]
        table = doc.add_table(rows=1, cols=cols)
        table.autofit = False
        for cell, c in zip(table.rows[0].cells, group):
            p = cell.paragraphs[0]
            p.style = "Report Figure"
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            pic = c.get("asset")
            if pic:
                with Image.open(pic) as im:
                    im = ImageOps.exif_transpose(im).convert("RGBA")
                    im.thumbnail((500, 430), Image.Resampling.LANCZOS)
                    stream = io.BytesIO()
                    im.save(stream, format="PNG"); stream.seek(0)
                    w, hh = im.size
                    scale = min(1.85 / w, 1.48 / hh)
                    p.add_run().add_picture(stream, width=Inches(scale*w), height=Inches(scale*hh))
            p = cell.add_paragraph(); p.add_run(c["title"]).bold = True
            p = cell.add_paragraph(c.get("identity", "候选") + "  ·  " + c["sku"], style="Report Detail")
            if _web_address(c.get("url")):
                _link(cell.add_paragraph(style="Report Source"), "查看" + c["platform"] + "来源", target=c["url"])


def write_report(path, data):
    """Write a Chinese .docx to path (str/Path), replacing it only after a successful save.

    data is a mapping; entries may be mappings or objects with the documented attributes.
    Missing data stays unknown, original field values/qualifiers are not parsed into prices,
    and sample entries never contribute to the main sections or coverage counts. No I/O is
    performed at import time. Only absolute local assets are opened; URLs are hyperlinks.
    """
    if not isinstance(data, Mapping):
        raise TypeError("data must be a mapping")
    campaign, scope = _text(data.get("campaign")), _scope(data)
    title = _text(data.get("title")) or (campaign + "促销资料报告" if campaign else "商品促销资料报告")
    products, channels, competitors, gaps, suggestions = (
        _strings(data.get(k)) for k in ("products", "channels", "competitors", "gaps", "suggestions"))
    sample_scope = _sample(data)
    records, samples = [], []
    for item in (data.get("entries") or ()):
        is_sample = sample_scope or _sample(item)
        group = samples if is_sample else records
        group.append(_Entry.read(len(group) + 1, item, is_sample))
    platforms = list(dict.fromkeys([*channels, *(e.platform for e in records)]))

    doc = _document(title)
    pilot = bool(data.get("pilot_batch"))
    if pilot:
        lead = (f"已整理 {len(data.get('candidate_refs', []))} 个候选，记录 {len(samples)} 条有来源的渠道资料。"
                + ("已取得的资料仅供本次试跑。" if samples else "暂未核实渠道优惠或到手价。"))
    elif sample_scope:
        lead = "整份资料标为示例。示例单列于文末，不作为商品、活动规则或竞品证据。"
    elif records:
        lead = f"本次收录 {len(records)} 条记录，可按平台核对规则、价格、福利和竞品资料。"
    else:
        lead = "尚无可用于核对商品或活动规则的记录。价格、福利和竞品差异均无法判断。"
    if samples and not sample_scope:
        lead += f"另有 {len(samples)} 条示例单列于文末。"
    doc.add_paragraph(lead)
    for pairs in ((("活动", campaign or _MISSING), ("数据范围", scope)),
                  (("关注商品", "、".join(products)), ("关注竞品", "、".join(competitors))),
                  (("关注平台", "、".join(channels)),)):
        items = [(k, v) for k, v in pairs if v]
        if sum(len(k) + len(v) + 3 for k, v in items) > 44:
            for pair in items:
                _line(doc, [pair], "Report Meta")
        elif items:
            _line(doc, items, "Report Meta")

    _candidate_gallery(doc, data.get("candidate_refs", []))
    if platforms:
        doc.add_heading("平台资料覆盖", 1)
        doc.add_paragraph("表内为各类记录数，含本品及竞品。“待补”表示尚未收录该类资料。", "Report Detail")
        _coverage(doc, platforms, samples if pilot else records)
    if pilot and not samples:
        doc.add_heading("待补资料", 1)
        key_gaps = _strings(data.get("manual_gaps")) or gaps
        for gap in dict.fromkeys(key_gaps):
            doc.add_paragraph(gap, "Report Detail")
        if set(key_gaps) != set(gaps):
            doc.add_paragraph("逐项缺少的图片、价格和平台规则见同一目录的「待补.txt」。", "Report Detail")
    else:
        _sections(doc, records, platforms)
        _actions(doc, records, products, competitors, [] if sample_scope else gaps,
                 [] if sample_scope else suggestions)
    if (samples or sample_scope) and not (pilot and not samples):
        doc.add_heading("试跑资料" if pilot else "示例资料", 1)
        p = doc.add_paragraph("下列试跑记录保留来源和条件，不能据此认定整个渠道可用。" if pilot else "以下内容仅作示例，不参与平台覆盖统计，也不用于推定商品价格、福利或竞品差异。")
        p.paragraph_format.keep_with_next = True
        for e in samples:
            _entry(doc, e, show_platform=True)
        if not samples:
            doc.add_paragraph("未提供示例记录。", "Report Detail")
        if sample_scope:
            for label, items in (("示例缺口", gaps), ("示例建议", suggestions)):
                for value in items:
                    _line(doc, [(label, value)])

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Avoid replacing a previous valid report with a partially written ZIP package.
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".d11-report-", suffix=".tmp",
                                     delete=False) as temp:
        temp_path = Path(temp.name)
    try:
        doc.save(temp_path)
        os.replace(temp_path, target)
    finally:
        temp_path.unlink(missing_ok=True)
