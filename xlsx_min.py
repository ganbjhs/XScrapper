"""
A .xlsx writer in one file, with no dependencies.

The dashboard hands the desk one workbook holding every category and its
posts. That could have been openpyxl, and openpyxl would have been three lines
— but web.py's zero-new-dependencies rule is not decoration: this server is
deployed by copying a directory onto a box and running it, and a missing wheel
there is a download button that 500s in front of whoever needed the file.

An .xlsx is a zip of XML, so writing one honestly costs about a hundred lines.
Deliberately narrow: text and numbers, a bold frozen header, column widths.
No formulas, no merged cells, no charts, no dates-as-dates (timestamps go in
as the strings the rest of the project already renders). If this file ever
needs a fourth feature, that is the moment to reach for the library instead.

    build([("Summary", ["Category", "Posts"], [["BJP Pro", 51]])]) -> bytes
"""

from __future__ import annotations

import io
import re
import zipfile

# Excel's own ceilings. Crossing either silently produces a file Excel offers
# to "repair", which reads to the operator as data loss — so we clip and say so
# in the only place we can: the cell itself.
MAX_ROWS = 1_048_575          # minus the header row
MAX_CELL = 32_767

# Characters XML 1.0 cannot carry at all. Scraped post text contains them more
# often than you would hope (stray \x00 and \x1a out of mangled encodings), and
# one of them anywhere in the sheet corrupts the whole workbook.
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# Excel forbids these in a sheet name, plus a 31-character ceiling.
_BAD_SHEET = re.compile(r"[\[\]\:\*\?\/\\]")

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"


def col_name(i: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA. Excel's own column lettering."""
    out = ""
    i = int(i)
    while True:
        out = chr(ord("A") + i % 26) + out
        i = i // 26 - 1
        if i < 0:
            return out


def esc(v) -> str:
    """XML-escape a value and drop the characters XML cannot hold."""
    s = "" if v is None else str(v)
    s = _ILLEGAL.sub("", s)
    if len(s) > MAX_CELL:
        # Say what happened in the cell rather than handing over a silently
        # shortened one: a truncation nobody can see is a lie.
        s = s[:MAX_CELL - 12] + "…[truncated]"
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def sheet_name(raw: str, taken: set) -> str:
    """A legal, unique sheet name. Collisions get a numeric suffix."""
    name = _BAD_SHEET.sub(" ", str(raw or "Sheet"))
    # Collapse the gap the substitution leaves behind: "Hate Speech / Hate
    # Content" should read as one space, not three.
    name = " ".join(name.split()) or "Sheet"
    name = name[:31]
    if name.lower() not in taken:
        taken.add(name.lower())
        return name
    for n in range(2, 1000):
        suffix = f" ({n})"
        cand = name[:31 - len(suffix)] + suffix
        if cand.lower() not in taken:
            taken.add(cand.lower())
            return cand
    raise ValueError("too many sheets with the same name")


def _cell(ref: str, val, style: int = 0) -> str:
    st = f' s="{style}"' if style else ""
    # A bool is an int in Python and would land as 1/0 in a column of words, so
    # it is checked first and written as the text the CSV export also writes.
    if isinstance(val, bool):
        return f'<c r="{ref}"{st} t="inlineStr"><is><t>{esc(val)}</t></is></c>'
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return f'<c r="{ref}"{st}><v>{val}</v></c>'
    text = esc(val)
    if not text:
        return f'<c r="{ref}"{st}/>'
    return (f'<c r="{ref}"{st} t="inlineStr">'
            f'<is><t xml:space="preserve">{text}</t></is></c>')


def _widths(headers: list, rows: list) -> str:
    """Column widths from the first few hundred rows. Enough to be readable."""
    if not headers:
        return ""
    wide = []
    for i, h in enumerate(headers):
        n = len(str(h))
        for r in rows[:300]:
            if i < len(r):
                n = max(n, len(str(r[i] if r[i] is not None else "")))
        wide.append(min(60, max(9, n + 2)))
    cols = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>'
                   for i, w in enumerate(wide))
    return f"<cols>{cols}</cols>"


def _sheet_xml(headers: list, rows: list) -> str:
    out = [f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           f'<worksheet xmlns="{_NS}">',
           '<sheetViews><sheetView workbookViewId="0">'
           '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" '
           'state="frozen"/></sheetView></sheetViews>',
           _widths(headers, rows),
           "<sheetData>"]
    out.append("<row r=\"1\">" + "".join(
        _cell(f"{col_name(i)}1", h, 1) for i, h in enumerate(headers)) + "</row>")
    for n, row in enumerate(rows[:MAX_ROWS], start=2):
        out.append(f'<row r="{n}">' + "".join(
            _cell(f"{col_name(i)}{n}", v) for i, v in enumerate(row)) + "</row>")
    out.append("</sheetData></worksheet>")
    return "".join(out)


_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<styleSheet xmlns="{_NS}">'
    '<fonts count="2">'
    '<font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font>'
    '</fonts>'
    '<fills count="2">'
    '<fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill>'
    '</fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/>'
    '</border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" '
    'borderId="0"/></cellStyleXfs>'
    '<cellXfs count="2">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" '
    'applyFont="1"/>'
    '</cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/>'
    '</cellStyles></styleSheet>'
)


def build(sheets: list) -> bytes:
    """
    Render [(name, headers, rows), …] as an .xlsx file.

    `rows` is a list of lists; a value that is an int or a float lands as a
    number and everything else as text. A workbook with no sheets is not a
    thing Excel will open, so one empty sheet is written instead.
    """
    sheets = list(sheets) or [("Sheet1", ["(nothing to export)"], [])]
    taken: set = set()
    named = [(sheet_name(n, taken), h, r) for n, h, r in sheets]

    types = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
             'content-types">'
             '<Default Extension="rels" ContentType="application/vnd.'
             'openxmlformats-package.relationships+xml"/>'
             '<Default Extension="xml" ContentType="application/xml"/>'
             '<Override PartName="/xl/workbook.xml" ContentType="application/'
             'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
             '<Override PartName="/xl/styles.xml" ContentType="application/'
             'vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
    wb_sheets, wb_rels = [], []
    for i, (name, _h, _r) in enumerate(named, start=1):
        types.append(f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
                     f'ContentType="application/vnd.openxmlformats-'
                     f'officedocument.spreadsheetml.worksheet+xml"/>')
        wb_sheets.append(f'<sheet name="{esc(name)}" sheetId="{i}" '
                         f'r:id="rId{i}"/>')
        wb_rels.append(f'<Relationship Id="rId{i}" Type="{_NS_R}/worksheet" '
                       f'Target="worksheets/sheet{i}.xml"/>')
    types.append("</Types>")
    style_rid = len(named) + 1
    wb_rels.append(f'<Relationship Id="rId{style_rid}" Type="{_NS_R}/styles" '
                   f'Target="styles.xml"/>')

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "".join(types))
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   f'<Relationships xmlns="{_NS_PKG}">'
                   f'<Relationship Id="rId1" Type="{_NS_R}/officeDocument" '
                   f'Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   f'<workbook xmlns="{_NS}" xmlns:r="{_NS_R}">'
                   f'<sheets>{"".join(wb_sheets)}</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   f'<Relationships xmlns="{_NS_PKG}">'
                   f'{"".join(wb_rels)}</Relationships>')
        z.writestr("xl/styles.xml", _STYLES)
        for i, (_n, headers, rows) in enumerate(named, start=1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(headers, rows))
    return buf.getvalue()
