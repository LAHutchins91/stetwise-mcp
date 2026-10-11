"""Hand-built OOXML fixtures with known formulas, merges, comments, and revisions."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_OFF = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_X = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _zip(parts: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, text in parts.items():
            archive.writestr(name, text.encode("utf-8"))
    return buffer.getvalue()


def model_xlsx() -> bytes:
    content_types = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{NS_CT}">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>
"""
    rels = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{NS_REL}">
  <Relationship Id="rId1" Type="{NS_OFF}/officeDocument" Target="xl/workbook.xml"/>
</Relationships>
"""
    workbook_rels = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{NS_REL}">
  <Relationship Id="rId1" Type="{NS_OFF}/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="{NS_OFF}/worksheet" Target="worksheets/sheet2.xml"/>
  <Relationship Id="rId3" Type="{NS_OFF}/styles" Target="styles.xml"/>
</Relationships>
"""
    workbook = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="{NS_X}" xmlns:r="{NS_OFF}">
  <sheets>
    <sheet name="Inputs" sheetId="1" r:id="rId1"/>
    <sheet name="Model" sheetId="2" r:id="rId2"/>
  </sheets>
  <definedNames>
    <definedName name="Total">Inputs!$A$3</definedName>
  </definedNames>
  <calcPr calcId="191029"/>
</workbook>
"""
    styles = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="{NS_X}">
  <fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
  <fills count="1"><fill><patternFill patternType="none"/></fill></fills>
  <borders count="1"><border/></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="2">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="2" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
  </cellXfs>
</styleSheet>
"""
    inputs = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="{NS_X}">
  <dimension ref="A1:C6"/>
  <sheetData>
    <row r="1"><c r="A1" s="1"><v>100</v></c></row>
    <row r="2"><c r="A2" s="1"><v>200</v></c></row>
    <row r="3"><c r="A3" s="1"><f>A1+A2</f><v>300</v></c></row>
    <row r="5"><c r="B5" s="1" t="inlineStr"><is><t>Note</t></is></c></row>
  </sheetData>
  <mergeCells count="1"><mergeCell ref="B5:C6"/></mergeCells>
</worksheet>
"""
    model = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="{NS_X}">
  <dimension ref="A1:A2"/>
  <sheetData>
    <row r="1"><c r="A1"><f>Inputs!A1</f><v>100</v></c></row>
    <row r="2"><c r="A2"><f>Inputs!A3</f><v>300</v></c></row>
  </sheetData>
</worksheet>
"""
    return _zip(
        {
            "[Content_Types].xml": content_types,
            "_rels/.rels": rels,
            "xl/_rels/workbook.xml.rels": workbook_rels,
            "xl/workbook.xml": workbook,
            "xl/styles.xml": styles,
            "xl/worksheets/sheet1.xml": inputs,
            "xl/worksheets/sheet2.xml": model,
        }
    )


def contract_docx() -> bytes:
    content_types = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{NS_CT}">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>
</Types>
"""
    rels = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{NS_REL}">
  <Relationship Id="rId1" Type="{NS_OFF}/officeDocument" Target="word/document.xml"/>
</Relationships>
"""
    document_rels = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{NS_REL}">
  <Relationship Id="rId1" Type="{NS_OFF}/styles" Target="styles.xml"/>
  <Relationship Id="rId2" Type="{NS_OFF}/comments" Target="comments.xml"/>
</Relationships>
"""
    styles = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{NS_W}">
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Normal">
    <w:name w:val="Normal"/>
  </w:style>
  <w:style w:type="paragraph" w:styleId="ListParagraph">
    <w:name w:val="List Paragraph"/>
  </w:style>
  <w:style w:type="character" w:styleId="Emphasis">
    <w:name w:val="Emphasis"/>
  </w:style>
</w:styles>
"""
    comments = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:comments xmlns:w="{NS_W}">
  <w:comment w:id="0" w:author="Sam" w:date="2026-01-01T00:00:00Z">
    <w:p><w:r><w:t>Reviewer note stays</w:t></w:r></w:p>
  </w:comment>
</w:comments>
"""
    document = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{NS_W}">
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:rPr><w:b/></w:rPr><w:t>Service agreement</w:t></w:r>
    </w:p>
    <w:p>
      <w:r><w:t xml:space="preserve">The fee is </w:t></w:r>
      <w:r><w:rPr><w:b/></w:rPr><w:t>Acme</w:t></w:r>
      <w:r><w:t xml:space="preserve"> Corp</w:t></w:r>
      <w:r><w:t xml:space="preserve"> for the work.</w:t></w:r>
      <w:r><w:commentReference w:id="0"/></w:r>
    </w:p>
    <w:p>
      <w:r><w:t xml:space="preserve">Keep </w:t></w:r>
      <w:del w:id="1" w:author="Alex" w:date="2026-01-02T00:00:00Z">
        <w:r><w:delText>old fee</w:delText></w:r>
      </w:del>
      <w:ins w:id="2" w:author="Alex" w:date="2026-01-02T00:00:00Z">
        <w:r><w:t>new fee</w:t></w:r>
      </w:ins>
    </w:p>
    <w:tbl>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Fee</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Date</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>100</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>2026-01-01</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
  </w:body>
</w:document>
"""
    return _zip(
        {
            "[Content_Types].xml": content_types,
            "_rels/.rels": rels,
            "word/_rels/document.xml.rels": document_rels,
            "word/document.xml": document,
            "word/styles.xml": styles,
            "word/comments.xml": comments,
        }
    )


def table_xlsx() -> bytes:
    content_types = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{NS_CT}">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/xl/tables/table1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml"/>
</Types>
"""
    rels = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{NS_REL}">
  <Relationship Id="rId1" Type="{NS_OFF}/officeDocument" Target="xl/workbook.xml"/>
</Relationships>
"""
    workbook_rels = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{NS_REL}">
  <Relationship Id="rId1" Type="{NS_OFF}/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="{NS_OFF}/styles" Target="styles.xml"/>
</Relationships>
"""
    sheet_rels = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{NS_REL}">
  <Relationship Id="rId1" Type="{NS_OFF}/table" Target="../tables/table1.xml"/>
</Relationships>
"""
    workbook = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="{NS_X}" xmlns:r="{NS_OFF}">
  <sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets>
</workbook>
"""
    styles = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="{NS_X}">
  <fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
  <fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
  <borders count="1"><border/></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>
</styleSheet>
"""
    sheet = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="{NS_X}" xmlns:r="{NS_OFF}">
  <dimension ref="A1:B4"/>
  <sheetData>
    <row r="1"><c r="A1" t="inlineStr"><is><t>Item</t></is></c><c r="B1" t="inlineStr"><is><t>Qty</t></is></c></row>
    <row r="2"><c r="A2" t="inlineStr"><is><t>Nails</t></is></c><c r="B2"><v>4</v></c></row>
    <row r="3"><c r="A3" t="inlineStr"><is><t>Bolts</t></is></c><c r="B3"><v>9</v></c></row>
    <row r="4"><c r="A4" t="inlineStr"><is><t>Total</t></is></c><c r="B4"><f>SUM(B2:B3)</f><v>13</v></c></row>
  </sheetData>
  <tableParts count="1"><tablePart r:id="rId1"/></tableParts>
</worksheet>
"""
    table = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<table xmlns="{NS_X}" id="1" name="Stock" displayName="Stock" ref="A1:B4" headerRowCount="1" totalsRowCount="1">
  <autoFilter ref="A1:B3"/>
  <tableColumns count="2">
    <tableColumn id="1" name="Item"/>
    <tableColumn id="2" name="Qty" totalsRowLabel="Total"/>
  </tableColumns>
</table>
"""
    return _zip(
        {
            "[Content_Types].xml": content_types,
            "_rels/.rels": rels,
            "xl/_rels/workbook.xml.rels": workbook_rels,
            "xl/worksheets/_rels/sheet1.xml.rels": sheet_rels,
            "xl/workbook.xml": workbook,
            "xl/styles.xml": styles,
            "xl/worksheets/sheet1.xml": sheet,
            "xl/tables/table1.xml": table,
        }
    )


def stale_xlsx() -> bytes:
    content_types = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{NS_CT}">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>
"""
    rels = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{NS_REL}">
  <Relationship Id="rId1" Type="{NS_OFF}/officeDocument" Target="xl/workbook.xml"/>
</Relationships>
"""
    workbook_rels = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{NS_REL}">
  <Relationship Id="rId1" Type="{NS_OFF}/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="{NS_OFF}/styles" Target="styles.xml"/>
</Relationships>
"""
    workbook = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="{NS_X}" xmlns:r="{NS_OFF}">
  <sheets><sheet name="Calc" sheetId="1" r:id="rId1"/></sheets>
  <calcPr calcId="191029"/>
</workbook>
"""
    styles = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="{NS_X}">
  <fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
  <fills count="1"><fill><patternFill patternType="none"/></fill></fills>
  <borders count="1"><border/></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>
</styleSheet>
"""
    sheet = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="{NS_X}">
  <dimension ref="A1:B2"/>
  <sheetData>
    <row r="1"><c r="A1"><v>10</v></c><c r="B1"><v>5</v></c></row>
    <row r="2"><c r="A2"><f>A1+B1</f><v>0</v></c><c r="B2"><f>A1/0</f><v>1</v></c></row>
  </sheetData>
</worksheet>
"""
    return _zip(
        {
            "[Content_Types].xml": content_types,
            "_rels/.rels": rels,
            "xl/_rels/workbook.xml.rels": workbook_rels,
            "xl/workbook.xml": workbook,
            "xl/styles.xml": styles,
            "xl/worksheets/sheet1.xml": sheet,
        }
    )


def types_xlsx() -> bytes:
    """Formulas with wrong caches: date, currency, percent, boolean, error, and string."""
    content_types = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{NS_CT}">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>
"""
    rels = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{NS_REL}">
  <Relationship Id="rId1" Type="{NS_OFF}/officeDocument" Target="xl/workbook.xml"/>
</Relationships>
"""
    workbook_rels = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{NS_REL}">
  <Relationship Id="rId1" Type="{NS_OFF}/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="{NS_OFF}/styles" Target="styles.xml"/>
</Relationships>
"""
    workbook = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="{NS_X}" xmlns:r="{NS_OFF}">
  <sheets><sheet name="Types" sheetId="1" r:id="rId1"/></sheets>
  <calcPr calcId="191029"/>
</workbook>
"""
    styles = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="{NS_X}">
  <fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
  <fills count="1"><fill><patternFill patternType="none"/></fill></fills>
  <borders count="1"><border/></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="4">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="14" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
    <xf numFmtId="7" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
    <xf numFmtId="10" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
  </cellXfs>
</styleSheet>
"""
    sheet = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="{NS_X}">
  <dimension ref="A1:A6"/>
  <sheetData>
    <row r="1"><c r="A1" s="1"><f>DATE(2011,2,8)</f><v>0</v></c></row>
    <row r="2"><c r="A2" s="2"><f>1234.5</f><v>0</v></c></row>
    <row r="3"><c r="A3" s="3"><f>1/4</f><v>0</v></c></row>
    <row r="4"><c r="A4"><f>TRUE()</f><v>0</v></c></row>
    <row r="5"><c r="A5"><f>1/0</f><v>1</v></c></row>
    <row r="6"><c r="A6"><f>"Hello"</f><v>0</v></c></row>
  </sheetData>
</worksheet>
"""
    return _zip(
        {
            "[Content_Types].xml": content_types,
            "_rels/.rels": rels,
            "xl/_rels/workbook.xml.rels": workbook_rels,
            "xl/workbook.xml": workbook,
            "xl/styles.xml": styles,
            "xl/worksheets/sheet1.xml": sheet,
        }
    )


def strings_xlsx() -> bytes:
    content_types = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{NS_CT}">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>
"""
    rels = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{NS_REL}">
  <Relationship Id="rId1" Type="{NS_OFF}/officeDocument" Target="xl/workbook.xml"/>
</Relationships>
"""
    workbook_rels = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{NS_REL}">
  <Relationship Id="rId1" Type="{NS_OFF}/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="{NS_OFF}/styles" Target="styles.xml"/>
  <Relationship Id="rId3" Type="{NS_OFF}/sharedStrings" Target="sharedStrings.xml"/>
</Relationships>
"""
    workbook = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="{NS_X}" xmlns:r="{NS_OFF}">
  <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>
"""
    styles = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="{NS_X}">
  <fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
  <fills count="1"><fill><patternFill patternType="none"/></fill></fills>
  <borders count="1"><border/></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>
</styleSheet>
"""
    shared = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="{NS_X}" count="2" uniqueCount="1"><si><t>Hello</t></si></sst>
"""
    sheet = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="{NS_X}">
  <sheetData>
    <row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><f>"Hello"</f><v>0</v></c></row>
    <row r="2"><c r="A2" t="s"><v>0</v></c><c r="B2" t="inlineStr"><is><t>Hello world</t></is></c></row>
  </sheetData>
</worksheet>
"""
    return _zip(
        {
            "[Content_Types].xml": content_types,
            "_rels/.rels": rels,
            "xl/_rels/workbook.xml.rels": workbook_rels,
            "xl/workbook.xml": workbook,
            "xl/styles.xml": styles,
            "xl/sharedStrings.xml": shared,
            "xl/worksheets/sheet1.xml": sheet,
        }
    )


def settings_docx() -> bytes:
    content_types = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{NS_CT}">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>
</Types>
"""
    rels = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{NS_REL}">
  <Relationship Id="rId1" Type="{NS_OFF}/officeDocument" Target="word/document.xml"/>
</Relationships>
"""
    document_rels = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{NS_REL}">
  <Relationship Id="rId1" Type="{NS_OFF}/settings" Target="settings.xml"/>
</Relationships>
"""
    settings = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:settings xmlns:w="{NS_W}"><w:zoom w:percent="100"/></w:settings>
"""
    document = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{NS_W}">
  <w:body>
    <w:p><w:r><w:t>Alpha stay</w:t></w:r></w:p>
    <w:p><w:r><w:t>Beta move</w:t></w:r></w:p>
  </w:body>
</w:document>
"""
    return _zip(
        {
            "[Content_Types].xml": content_types,
            "_rels/.rels": rels,
            "word/_rels/document.xml.rels": document_rels,
            "word/document.xml": document,
            "word/settings.xml": settings,
        }
    )


def write_all(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "model.xlsx").write_bytes(model_xlsx())
    (directory / "contract.docx").write_bytes(contract_docx())
    (directory / "table.xlsx").write_bytes(table_xlsx())
    (directory / "stale.xlsx").write_bytes(stale_xlsx())
    (directory / "strings.xlsx").write_bytes(strings_xlsx())
    (directory / "settings.docx").write_bytes(settings_docx())


if __name__ == "__main__":
    import sys

    write_all(Path(sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/generated"))
