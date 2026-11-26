import hashlib
import importlib.util
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"


def run_script(name, *args):
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / name), *map(str, args)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def load_inspector_module():
    spec = importlib.util.spec_from_file_location("inspect_docx", SCRIPTS_DIR / "inspect_docx.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def valid_png(index):
    width, height = 320, 180
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            row.extend((20 + index * 20, 70 + (x // 8) % 120, 150 - (y // 6) % 100))
        rows.append(bytes(row))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b"")


def make_docx(path, image_count=1):
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">
<w:body>
<w:p><w:r><w:t>某车企纯电版上市</w:t></w:r></w:p>
<w:p><w:r><w:t>某车型全球代言人，联合设计，首发</w:t></w:r></w:p>
<w:p><w:r><w:t>车身尺寸 5299*2010*1815 mm</w:t></w:r><w:hyperlink r:id="rId2"><w:r><w:t>活动页面</w:t></w:r></w:hyperlink><w:fldSimple w:instr=' HYPERLINK "javascript:void(0)" '><w:r><w:t>链接</w:t></w:r></w:fldSimple></w:p>
__IMAGE_BLOCK__
<w:tbl><w:tr><w:tc><w:p><w:r><w:t>车型</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>能源类型</w:t></w:r></w:p></w:tc></w:tr><w:tr><w:tc><w:p><w:r><w:t>Anonymous-Series-001 Max</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>纯电</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
<w:p><w:ins w:id="1" w:author="作者" w:date="2026-11-25T00:00:00Z"><w:r><w:t>新增</w:t></w:r></w:ins><w:del w:id="2" w:author="作者" w:date="2026-11-25T00:00:00Z"><w:r><w:delText>删除</w:delText></w:r></w:del></w:p>
<w:p><w:r><w:rPr><w:vanish/></w:rPr><w:t>隐藏文本</w:t></w:r></w:p>
<w:sectPr><w:headerReference w:type="default" r:id="rId3"/><w:footerReference w:type="default" r:id="rId4"/></w:sectPr>
</w:body></w:document>"""
    image_blocks = []
    for image_index in range(image_count):
        rid = "rId1" if image_index == 0 else f"rId{100 + image_index}"
        description = "车辆海报：Anonymous-Series-001 纯电版，首发，全球代言人，联合设计" if image_index == 0 else f"Anonymous-Series-001 增程版示意图 {image_index}"
        caption = "" if image_count > 1 and image_index == 0 else "<w:p><w:r><w:t>增程版</w:t></w:r></w:p>"
        image_blocks.append(
            f'<w:p><w:r><w:drawing><wp:inline><wp:extent cx="914400" cy="914400"/><wp:docPr id="{image_index + 1}" name="图片 {image_index + 1}" descr="{description}"/><a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic><pic:nvPicPr><pic:cNvPr id="0" name="image{image_index + 1}.png"/><pic:cNvPicPr/></pic:nvPicPr><pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill><pic:spPr/></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>{caption}'
        )
    document = document.replace("__IMAGE_BLOCK__", "".join(image_blocks))
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://clickx.example.invalid/track?x=1" TargetMode="External"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/>
<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>
</Relationships>"""
    extra_image_rels = "".join(
        f'<Relationship Id="rId{100 + image_index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image{image_index + 1}.png"/>'
        for image_index in range(1, image_count)
    )
    rels = rels.replace("</Relationships>", extra_image_rels + "</Relationships>")
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/><Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/><Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/><Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/></Types>"""
    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/></Relationships>"""
    header = """<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>页眉：内部评审</w:t></w:r></w:p></w:hdr>"""
    footer = """<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>页脚：未发布</w:t></w:r></w:p></w:ftr>"""
    comments = """<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:comment w:id="0" w:author="内部" w:date="2026-11-25T00:00:00Z"><w:p><w:r><w:t>价格待确认</w:t></w:r></w:p></w:comment></w:comments>"""
    core = """<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:title>纯电版发布稿</dc:title><dc:creator>内部</dc:creator><cp:lastModifiedBy>内部</cp:lastModifiedBy><dcterms:created xsi:type="dcterms:W3CDTF">2026-11-25T00:00:00Z</dcterms:created></cp:coreProperties>"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/_rels/document.xml.rels", rels)
        zf.writestr("word/header1.xml", header)
        zf.writestr("word/footer1.xml", footer)
        zf.writestr("word/comments.xml", comments)
        zf.writestr("docProps/core.xml", core)
        for image_index in range(image_count):
            zf.writestr(f"word/media/image{image_index + 1}.png", valid_png(image_index))


def make_complex_docx(path):
    source = path.with_suffix(".source.docx")
    make_docx(source)
    with zipfile.ZipFile(source) as zin, zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "word/document.xml":
                text = data.decode("utf-8")
                text = text.replace(
                    "<w:tc><w:p><w:r><w:t>车型</w:t></w:r></w:p></w:tc>",
                    "<w:tc><w:p><w:r><w:t>车型</w:t></w:r><w:hyperlink r:id=\"rId2\"><w:r><w:t>表格跟踪</w:t></w:r></w:hyperlink><w:fldSimple w:instr=' HYPERLINK \"javascript:void(0)\" '><w:r><w:t>表格脚本</w:t></w:r></w:fldSimple><w:fldSimple w:instr=' HYPERLINK javascript:alert(1) '><w:r><w:t>无引号脚本</w:t></w:r></w:fldSimple></w:p></w:tc>",
                    1,
                )
                text = text.replace(
                    "<w:p><w:r><w:rPr><w:vanish/></w:rPr><w:t>隐藏文本</w:t></w:r></w:p>",
                    "<w:p><w:r><w:rPr><w:vanish/></w:rPr><w:t>隐藏文本</w:t></w:r></w:p><w:p><w:moveFrom w:id=\"3\" w:author=\"作者\"><w:r><w:t>移动前</w:t></w:r></w:moveFrom><w:moveTo w:id=\"4\" w:author=\"作者\"><w:r><w:t>移动后</w:t></w:r></w:moveTo></w:p><w:p><w:pPr><w:rPr><w:rPrChange w:id=\"5\" w:author=\"作者\"><w:rPr/></w:rPrChange></w:rPr></w:pPr><w:r><w:t>属性修订</w:t></w:r></w:p><w:p><w:r><w:drawing><a:blip r:link=\"rId5\"/></w:drawing><w:pict><v:imagedata r:id=\"rId6\"/></w:pict></w:r></w:p>",
                    1,
                )
                data = text.encode("utf-8")
            elif item.filename == "word/_rels/document.xml.rels":
                text = data.decode("utf-8")
                text = text.replace("</Relationships>", "<Relationship Id=\"rId5\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/image\" Target=\"https://img.example.invalid/a.png\" TargetMode=\"External\"/><Relationship Id=\"rId6\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/image\" Target=\"https://img.example.invalid/b.png\" TargetMode=\"External\"/></Relationships>")
                data = text.encode("utf-8")
            zout.writestr(item, data)
        zout.writestr("word/activeX/activeX1.xml", "<activeX/>")
        zout.writestr("word/embeddings/oleObject1.bin", b"OLE")
    source.unlink()


def make_shared_image_docx(path, missing_target=False):
    source = path.with_suffix(".source.docx")
    make_docx(source)
    target = "media/missing.png" if missing_target else "media/image1.png"
    header = """<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:p><w:r><w:drawing><a:blip r:embed="rId1"/></w:drawing></w:r></w:p><w:p><w:r><w:t>页眉图注</w:t></w:r></w:p></w:hdr>"""
    rels = f"""<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="{target}"/></Relationships>"""
    with zipfile.ZipFile(source) as zin, zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "word/header1.xml":
                data = header.encode("utf-8")
            zout.writestr(item, data)
        zout.writestr("word/_rels/header1.xml.rels", rels)
    source.unlink()


def auto_media_sample():
    names = {
        1: "车型名称", 3: "厂商指导价(元)", 6: "能源类型", 8: "上市时间",
        21: "长*宽*高(mm)", 34: "轴距(mm)", 79: "系统综合功率(kW)",
        86: "电池类型", 152: "空气悬挂类型", 153: "整体主动转向系统",
        164: "激光雷达数量", 165: "激光雷达线数", 167: "辅助驾驶系统",
        168: "辅助驾驶等级", 247: "后排液晶屏幕尺寸", 249: "后排投影系统",
        293: "座椅布局",
    }
    titlelist = [{"itemtype": "参数信息", "groupname": "参数信息", "items": [
        {"titleid": key, "itemname": value} for key, value in names.items()
    ]}]
    trims = []
    for specid, name, price, height, power, air, steer, lidar, screen, projection in [
        (1, "2026款 增程 Max 长续航版", "19.88万", "1815", "280", None, None, [{"name": "1个", "value": "●"}], [{"name": "20.8英寸", "value": "○"}], None),
        (2, "2026款 增程 四驱 Ultra+旗舰 长续航版", "22.88万", "1800", "420", [{"name": "双腔", "value": "●"}], "●", [{"name": "4个", "value": "●"}], [{"name": "20.8英寸", "value": "○"}], "●"),
    ]:
        values = {
            1: name, 3: price, 6: "增程式", 8: "2026-10-01",
            21: f"5299*2010*{height}", 34: "3110", 79: power,
            86: [{"name": "磷酸铁锂电池", "value": "●"}], 152: air,
            153: steer, 164: lidar, 165: [{"name": "896线", "value": "●"}],
            167: [{"name": "示例智驾", "value": "●"}], 168: [{"name": "L2", "value": "●"}],
            247: screen, 249: projection, 293: [{"name": "2-2-2", "value": "●"}],
        }
        params = []
        for titleid in names:
            value = values.get(titleid)
            item = {"titleid": titleid, "itemname": value if isinstance(value, str) else "", "itemvalue": None, "sublist": value if isinstance(value, list) else []}
            params.append(item)
        trims.append({"specid": specid, "specname": name, "specstatus": 20, "year": 0, "minprice": price, "dealerprice": 0, "condition": ["SUV", "增程式", "2026"], "paramconflist": params, "sort": specid})
    return {"returncode": 0, "message": "", "result": {"bread": {"brandid": 999, "brandname": "匿名品牌", "seriesid": 999, "seriesname": "Anonymous-Series-001"}, "conditionlist": [], "titlelist": titlelist, "datalist": trims}}


class DocxInspectionTests(unittest.TestCase):
    def test_inspect_docx_extracts_structural_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "press.docx"
            out = root / "inspection"
            make_docx(docx)
            result = run_script("inspect_docx.py", docx, "--out", out)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((out / "inspection.json").read_text(encoding="utf-8"))
            counts = data["counts"]
            self.assertEqual(counts["tables"], 1)
            self.assertEqual(counts["images"], 1)
            self.assertEqual(counts["external_links"], 1)
            self.assertEqual(counts["field_codes"], 1)
            self.assertEqual(counts["tracked_insertions"], 1)
            self.assertEqual(counts["tracked_deletions"], 1)
            self.assertEqual(counts["comments"], 1)
            self.assertEqual(data["tables"][0][0][0], "车型")
            self.assertEqual(data["images"][0]["next_paragraph_text"], "增程版")
            self.assertEqual(data["external_links"][0]["url"], "https://clickx.example.invalid/track?x=1")
            self.assertIn("javascript:void(0)", data["field_codes"][0]["instruction"])
            self.assertIn("页眉：内部评审", json.dumps(data["headers_footers"], ensure_ascii=False))
            self.assertIn("页脚：未发布", json.dumps(data["headers_footers"], ensure_ascii=False))
            self.assertEqual(data["comments"][0]["text"], "价格待确认")
            self.assertEqual(data["core_properties"]["title"], "纯电版发布稿")
            self.assertTrue(data["flags"]["has_tracked_changes"])
            self.assertTrue(data["flags"]["has_comments"])
            self.assertTrue(data["flags"]["has_script_fields"])
            self.assertTrue(data["flags"]["has_tracking_fields"])
            self.assertTrue(data["external_links"][0]["is_tracking"])
            self.assertTrue(data["flags"]["has_hidden_text"])
            self.assertTrue((out / "media" / "image1.png").exists())
            self.assertEqual((out / "media" / "image1.png").read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            self.assertIn("某车企纯电版上市", (out / "body.txt").read_text(encoding="utf-8"))
            self.assertEqual((out / "body.txt").read_text(encoding="utf-8").count("Anonymous-Series-001 Max"), 1)

    def test_inspect_docx_missing_ocr_is_nonfatal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "press.docx"
            make_docx(docx)
            old_path = os.environ.get("PATH")
            os.environ["PATH"] = ""
            try:
                result = run_script("inspect_docx.py", docx, "--out", root / "inspection", "--ocr")
            finally:
                if old_path is None:
                    os.environ.pop("PATH", None)
                else:
                    os.environ["PATH"] = old_path
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((root / "inspection" / "inspection.json").read_text(encoding="utf-8"))
            self.assertIsNone(data["images"][0]["ocr_text"])
            self.assertTrue(any("OCR_UNAVAILABLE" in item for item in data["warnings"]))

    def test_inspect_docx_rejects_doctype(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "press.docx"
            bad = root / "bad.docx"
            make_docx(source)
            with zipfile.ZipFile(source) as zin, zipfile.ZipFile(bad, "w", zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    data = zin.read(item.filename)
                    if item.filename == "word/document.xml":
                        text = data.decode("utf-8")
                        text = text.replace("?>", '?><!DOCTYPE w:document [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>', 1)
                        data = text.encode("utf-8")
                    zout.writestr(item, data)
            result = run_script("inspect_docx.py", bad, "--out", root / "inspection")
            self.assertEqual(result.returncode, 2)
            self.assertIn("DTD", result.stderr)

    def test_inspect_docx_rejects_utf16_doctype(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "press.docx"
            bad = root / "bad-utf16.docx"
            make_docx(source)
            with zipfile.ZipFile(source) as zin, zipfile.ZipFile(bad, "w", zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    data = zin.read(item.filename)
                    if item.filename == "word/document.xml":
                        text = data.decode("utf-8")
                        text = text.replace("?>", '?><!DOCTYPE w:document [<!ENTITY xxe "EXPANDED">]>', 1)
                        text = text.replace('encoding="UTF-8"', 'encoding="UTF-16"', 1)
                        text = text.replace("某车企纯电版上市", "&xxe;", 1)
                        data = text.encode("utf-16")
                    zout.writestr(item, data)
            result = run_script("inspect_docx.py", bad, "--out", root / "inspection")
            self.assertEqual(result.returncode, 2)
            self.assertIn("DTD", result.stderr)

    def test_oversized_media_is_hard_failure(self):
        module = load_inspector_module()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "press.docx"
            make_docx(docx)
            with zipfile.ZipFile(docx) as zf:
                rels = module.relationships(zf, "word/document.xml")
                with mock.patch.object(module, "MAX_MEMBER_BYTES", 8):
                    with self.assertRaisesRegex(ValueError, "媒体"):
                        module.extract_media(zf, rels, root / "inspection", [], False, "chi_sim+eng")

    def test_inspect_docx_covers_table_fields_and_change_types(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "complex.docx"
            out = root / "inspection"
            make_complex_docx(docx)
            result = run_script("inspect_docx.py", docx, "--out", out)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((out / "inspection.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(data["counts"]["external_links"], 2)
            self.assertTrue(any("javascript:void(0)" in item["instruction"] for item in data["field_codes"]))
            self.assertTrue(any("javascript:alert(1)" in item["instruction"] for item in data["field_codes"]))
            self.assertEqual(data["counts"]["external_images"], 2)
            self.assertTrue(data["flags"]["has_external_images"])
            self.assertEqual(data["counts"]["tracked_moves_from"], 1)
            self.assertEqual(data["counts"]["tracked_moves_to"], 1)
            self.assertGreaterEqual(data["counts"]["property_changes"], 1)
            self.assertTrue(data["flags"]["has_tracked_changes"])
            self.assertTrue(data["flags"]["has_activex"])
            self.assertTrue(data["flags"]["has_embedded_objects"])
            self.assertEqual(data["activex_parts"], ["word/activeX/activeX1.xml"])

    def test_missing_image_relationship_is_hard_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "press.docx"
            bad = root / "missing-image.docx"
            make_docx(source)
            with zipfile.ZipFile(source) as zin, zipfile.ZipFile(bad, "w", zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    data = zin.read(item.filename)
                    if item.filename == "word/document.xml":
                        data = data.replace(b'r:embed="rId1"', b'r:embed="rId7"', 1)
                    elif item.filename == "word/_rels/document.xml.rels":
                        data = data.replace(b"</Relationships>", b"<Relationship Id=\"rId7\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/image\" Target=\"media/missing.png\"/></Relationships>")
                    zout.writestr(item, data)
            result = run_script("inspect_docx.py", bad, "--out", root / "inspection")
            self.assertEqual(result.returncode, 2)
            self.assertIn("图片", result.stderr)

    def test_missing_image_relationship_id_is_hard_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "press.docx"
            bad = root / "missing-rel.docx"
            make_docx(source)
            with zipfile.ZipFile(source) as zin, zipfile.ZipFile(bad, "w", zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    data = zin.read(item.filename)
                    if item.filename == "word/document.xml":
                        data = data.replace(b'r:embed="rId1"', b'r:embed="rIdMissing"', 1)
                    zout.writestr(item, data)
            result = run_script("inspect_docx.py", bad, "--out", root / "inspection")
            self.assertEqual(result.returncode, 2)
            self.assertIn("图片关系", result.stderr)

    def test_missing_header_image_is_hard_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "header-missing.docx"
            make_shared_image_docx(docx, missing_target=True)
            result = run_script("inspect_docx.py", docx, "--out", root / "inspection")
            self.assertEqual(result.returncode, 2)
            self.assertIn("图片", result.stderr)

    def test_shared_image_keeps_each_reference_context(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "shared.docx"
            make_shared_image_docx(docx)
            result = run_script("inspect_docx.py", docx, "--out", root / "inspection")
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((root / "inspection" / "inspection.json").read_text(encoding="utf-8"))
            self.assertEqual(data["counts"]["images"], 1)
            self.assertEqual(data["counts"]["image_references"], 2)
            captions = {item["next_paragraph_text"] for item in data["image_references"]}
            self.assertIn("增程版", captions)
            self.assertIn("页眉图注", captions)

    def test_inspect_docx_rejects_excessive_zip_members(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "many.docx"
            with zipfile.ZipFile(docx, "w", zipfile.ZIP_DEFLATED) as zf:
                for index in range(10001):
                    zf.writestr(f"word/media/{index}.bin", b"")
            result = run_script("inspect_docx.py", docx, "--out", root / "inspection")
            self.assertEqual(result.returncode, 2)
            self.assertIn("成员", result.stderr)


class PublicPageCaptureTests(unittest.TestCase):
    def test_capture_public_page_saves_evidence(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/app.js":
                    body = b'fetch("/api/data");'
                    ctype = "application/javascript"
                elif self.path == "/api/data":
                    body = b'{"series":"Anonymous-Series-001","energy":"range_extender"}'
                    ctype = "application/json"
                else:
                    body = "<html><head><title>Anonymous-Series-001 参数页</title><script src='/app.js'></script></head><body><h1>纯电版参数</h1><p>参数仅供参考，以官方发布为准。</p></body></html>".encode("utf-8")
                    ctype = "text/html"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                out = Path(td) / "capture"
                base = f"http://127.0.0.1:{server.server_port}"
                result = run_script("capture_public_page.py", base, "--out", out, "--fetch-scripts", "--scan-api", "--api-url", base + "/api/data", "--allow-loopback")
                self.assertEqual(result.returncode, 0, result.stderr)
                metadata = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
                self.assertEqual(metadata["source_type"], "public_web_non_official")
                self.assertEqual(metadata["verification_status"], "user_provided_unverified")
                self.assertTrue(metadata["fetched_at"].endswith("Z"))
                self.assertIn("纯电版参数", (out / "visible_text.txt").read_text(encoding="utf-8"))
                self.assertIn("参数仅供参考", json.dumps(json.loads((out / "disclaimers.json").read_text(encoding="utf-8")), ensure_ascii=False))
                self.assertTrue((out / "scripts" / "app.js").exists())
                self.assertIn("/api/data", json.dumps(json.loads((out / "api_candidates.json").read_text(encoding="utf-8"))))
                self.assertEqual(json.loads((out / "api.json").read_text(encoding="utf-8"))["series"], "Anonymous-Series-001")
                self.assertEqual((out / "api.raw.json").read_bytes(), b'{"series":"Anonymous-Series-001","energy":"range_extender"}')
                self.assertTrue(metadata["api"]["fetched_at"].endswith("Z"))
        finally:
            server.shutdown()
            server.server_close()

    def test_loopback_is_rejected_by_default(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"<html><body>local</body></html>")

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as td:
                result = run_script("capture_public_page.py", f"http://127.0.0.1:{server.server_port}", "--out", Path(td) / "capture")
                self.assertEqual(result.returncode, 2)
                self.assertIn("回环", result.stderr)
        finally:
            server.shutdown()
            server.server_close()

    def test_unknown_charset_returns_clean_error(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"<html><body>page</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=no-such-charset")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as td:
                result = run_script("capture_public_page.py", f"http://127.0.0.1:{server.server_port}", "--out", Path(td) / "capture", "--allow-loopback")
                self.assertEqual(result.returncode, 2)
                self.assertIn("错误", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
        finally:
            server.shutdown()
            server.server_close()

    def test_non_global_address_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            result = run_script("capture_public_page.py", "http://100.64.0.1/", "--out", Path(td) / "capture")
            self.assertEqual(result.returncode, 2)
            self.assertIn("非全局", result.stderr)

    def test_api_must_share_page_origin(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"ok":true}' if self.path == "/api/data" else b"<html><body>page</body></html>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as td:
                result = run_script(
                    "capture_public_page.py",
                    f"http://127.0.0.1:{server.server_port}",
                    "--out", Path(td) / "capture",
                    "--api-url", f"http://localhost:{server.server_port}/api/data",
                    "--allow-loopback",
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("同源", result.stderr)
        finally:
            server.shutdown()
            server.server_close()


class AutoMediaNormalizationTests(unittest.TestCase):
    def test_cross_energy_type_is_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "params.json"
            source.write_text(json.dumps(auto_media_sample(), ensure_ascii=False), encoding="utf-8")
            out = root / "normalized"
            result = run_script("normalize_auto_media_config.py", source, "--out", out, "--target-energy", "pure_ev", "--source-url", "https://config.example.invalid/series/999.html")
            self.assertEqual(result.returncode, 0, result.stderr)
            gate = json.loads((out / "gate.json").read_text(encoding="utf-8"))
            self.assertEqual(gate["status"], "blocked")
            self.assertEqual(gate["target_energy"], "pure_ev")
            self.assertEqual(gate["available_energy_types"], ["range_extender"])
            self.assertIn("跨能源类型", gate["reason"])
            self.assertTrue((out / "matrix.md").exists())

    def test_normalizes_trim_parameters_and_source_status(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "params.json"
            source.write_text(json.dumps(auto_media_sample(), ensure_ascii=False), encoding="utf-8")
            out = root / "normalized"
            result = run_script("normalize_auto_media_config.py", source, "--out", out, "--target-energy", "range_extender")
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((out / "normalized.json").read_text(encoding="utf-8"))
            self.assertEqual(data["series"]["seriesname"], "Anonymous-Series-001")
            self.assertEqual(data["available_energy_types"], ["range_extender"])
            self.assertEqual(len(data["trims"]), 2)
            first, second = data["trims"]
            self.assertEqual(first["price"], "19.88万")
            self.assertEqual(first["dimensions"], "5299*2010*1815")
            self.assertEqual(second["system_power_kw"], "420")
            self.assertEqual(first["battery_type"], "磷酸铁锂电池")
            self.assertIsNone(first["air_suspension_type"])
            self.assertEqual(second["air_suspension_type"], "双腔")
            self.assertEqual(second["lidar_count"], "4个")
            self.assertEqual(first["driving_assistance_level"], "L2")
            self.assertIn("非官方", data["source"]["source_type"])
            self.assertEqual(data["source"]["verification_status"], "用户提供，未独立核验")
            self.assertEqual(data["source"]["source_date"], "页面发布日期未提供，待补")
            facts = json.loads((out / "facts.json").read_text(encoding="utf-8"))
            self.assertTrue(facts)
            self.assertEqual(facts[0]["来源类型"], data["source"]["source_type"])
            self.assertTrue(all("适用范围" in row for row in facts))
            self.assertIn("能源类型：增程", facts[0]["适用范围"])
            rear_screen = next(row for row in facts if "后排屏幕尺寸" in row["官方原文或脱敏摘录"])
            self.assertIn("○", rear_screen["官方原文或脱敏摘录"])
            self.assertIn("选配", rear_screen["官方原文或脱敏摘录"])
            gate = json.loads((out / "gate.json").read_text(encoding="utf-8"))
            self.assertEqual(gate["status"], "allowed_with_limits")
            self.assertIn("非官方", gate["limits"][0])

    def test_malformed_config_structure_returns_clear_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "bad.json"
            source.write_text(json.dumps({"returncode": 0, "result": {"titlelist": [None], "datalist": [None]}}), encoding="utf-8")
            result = run_script("normalize_auto_media_config.py", source, "--out", root / "out")
            self.assertEqual(result.returncode, 2)
            self.assertIn("错误", result.stderr)


class PackagedFixtureTests(unittest.TestCase):
    def test_packaged_fixtures_regress_real_case_structure(self):
        fixtures = SKILL_DIR / "tests" / "fixtures"
        docx = fixtures / "anonymized_press_release.docx"
        params = fixtures / "anonymized_auto_media_params.json"
        self.assertTrue(docx.exists())
        self.assertTrue(params.exists())
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = run_script("inspect_docx.py", docx, "--out", root / "docx")
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((root / "docx" / "inspection.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(data["counts"]["images"], 1)
            self.assertEqual(data["counts"]["images"], 8)
            self.assertEqual(data["counts"]["image_references"], 8)
            self.assertEqual(sum(image["next_paragraph_text"] == "增程版" for image in data["images"]), 7)
            for image in data["images"]:
                png = (root / "docx" / image["file"]).read_bytes()
                self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
                width, height = struct.unpack(">II", png[16:24])
                self.assertEqual((width, height), (320, 180))
                offset = 8
                compressed = []
                while offset < len(png):
                    length = struct.unpack(">I", png[offset:offset + 4])[0]
                    kind = png[offset + 4:offset + 8]
                    body = png[offset + 8:offset + 8 + length]
                    if kind == b"IDAT":
                        compressed.append(body)
                    offset += 12 + length
                self.assertEqual(len(zlib.decompress(b"".join(compressed))), height * (1 + width * 3))
            self.assertTrue(data["flags"]["has_tracking_fields"])
            self.assertTrue(data["flags"]["has_script_fields"])
            self.assertIn("Anonymous-Series-001", (root / "docx" / "body.txt").read_text(encoding="utf-8"))
            result = run_script("normalize_auto_media_config.py", params, "--out", root / "config", "--target-energy", "pure_ev")
            self.assertEqual(result.returncode, 0, result.stderr)
            gate = json.loads((root / "config" / "gate.json").read_text(encoding="utf-8"))
            self.assertEqual(gate["status"], "blocked")
            self.assertEqual(gate["issue_type"], "版型错配")
            normalized = json.loads((root / "config" / "normalized.json").read_text(encoding="utf-8"))
            self.assertEqual(normalized["series"]["seriesname"], "Anonymous-Series-001")


class TaxonomyTests(unittest.TestCase):
    def test_known_energy_type_is_not_marked_unknown(self):
        text = (SKILL_DIR / "examples" / "匿名示例.md").read_text(encoding="utf-8")
        row = next(line for line in text.splitlines() if line.startswith("| F02 |"))
        self.assertIn("| 纯电；Max 版 |", row)

    def test_examples_state_energy_type_in_fact_cards(self):
        root = SKILL_DIR
        for path in (root / "examples").glob("*.md"):
            lines = path.read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(lines):
                if not line.startswith("|") or "适用范围" not in line:
                    continue
                headers = [cell.strip() for cell in line.strip("|").split("|")]
                column = headers.index("适用范围")
                for row in lines[index + 2:]:
                    if not row.startswith("|"):
                        break
                    cells = [cell.strip() for cell in row.strip("|").split("|")]
                    if len(cells) > column:
                        self.assertRegex(cells[column], r"(?:(?:能源类型[:：]\s*)?(?:纯电|增程|插混)|能源类型未提供，待补)", f"{path.name}: {cells[column]}")

    def test_issue_taxonomy_is_single_source(self):
        root = SKILL_DIR
        types = json.loads((root / "references" / "issue_types.json").read_text(encoding="utf-8"))
        self.assertIn("点击跟踪字段", types)
        self.assertIn("适用范围不明", types)
        for label in types:
            self.assertIn(label, (root / "SKILL.md").read_text(encoding="utf-8"))
            self.assertIn(label, (root / "references" / "问题分类与分层输出.md").read_text(encoding="utf-8"))
        for path in (root / "examples").glob("*.md"):
            lines = path.read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(lines):
                if not line.startswith("|") or "问题类型" not in line:
                    continue
                headers = [cell.strip() for cell in line.strip("|").split("|")]
                column = headers.index("问题类型")
                for row in lines[index + 2:]:
                    if not row.startswith("|"):
                        break
                    cells = [cell.strip() for cell in row.strip("|").split("|")]
                    if len(cells) <= column:
                        continue
                    value = cells[column].split("（", 1)[0].strip()
                    for label in [item.strip() for item in re.split(r"[；;]", value) if item.strip()]:
                        self.assertIn(label, types, f"{path.name}: {label}")


if __name__ == "__main__":
    unittest.main()
