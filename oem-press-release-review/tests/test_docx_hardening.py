import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


SKILL_DIR = Path(__file__).resolve().parents[1]
INSPECTOR = SKILL_DIR / "scripts" / "inspect_docx.py"
IMAGE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
HYPERLINK_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def document_xml(body):
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>{body}</w:body></w:document>'''


def relationships_xml(items=""):
    return f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{items}</Relationships>'


def image_relationship(rid="rId1", target="media/image1.png", external=False):
    mode = ' TargetMode="External"' if external else ""
    return f'<Relationship Id="{rid}" Type="{IMAGE_REL}" Target="{target}"{mode}/>'


def hyperlink_relationship(rid, target):
    return f'<Relationship Id="{rid}" Type="{HYPERLINK_REL}" Target="{target}" TargetMode="External"/>'


def image_paragraph(rid="rId1", attribute="embed"):
    return f'<w:p><w:r><w:drawing><a:blip r:{attribute}="{rid}"/></w:drawing></w:r></w:p>'


STORY_ROOTS = {
    "word/header1.xml": '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>页眉</w:t></w:r></w:p></w:hdr>',
    "word/footer1.xml": '<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>页脚</w:t></w:r></w:p></w:ftr>',
    "word/footnotes.xml": '<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:footnote w:id="1"><w:p><w:r><w:t>脚注</w:t></w:r></w:p></w:footnote></w:footnotes>',
    "word/endnotes.xml": '<w:endnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:endnote w:id="1"><w:p><w:r><w:t>尾注</w:t></w:r></w:p></w:endnote></w:endnotes>',
    "word/comments.xml": '<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:comment w:id="0"><w:p><w:r><w:t>批注</w:t></w:r></w:p></w:comment></w:comments>',
}


def comments_with_image(rid="rId1", attribute="embed"):
    return f'''<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:comment w:id="0" w:author="审阅者"><w:p><w:r><w:t>批注</w:t></w:r><w:r><w:drawing><a:blip r:{attribute}="{rid}"/></w:drawing></w:r></w:p></w:comment></w:comments>'''


def comments_with_link_and_script_field():
    return '''<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:comment w:id="0" w:author="审阅者"><w:p><w:hyperlink r:id="rIdLink"><w:r><w:t>外部链接</w:t></w:r></w:hyperlink><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> HYPERLINK "javascript:alert(1)" </w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>脚本字段</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p></w:comment></w:comments>'''


def base_members(body):
    return {
        "[Content_Types].xml": '''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>''',
        "_rels/.rels": relationships_xml(f'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'),
        "word/document.xml": document_xml(body),
        "word/_rels/document.xml.rels": relationships_xml(),
    }


def make_docx(path, body=None, members=None):
    if body is None:
        body = '<w:p><w:r><w:t>正文</w:t></w:r></w:p>'
    parts = base_members(body)
    if members:
        parts.update(members)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts.items():
            zf.writestr(name, data)


def run_inspector(docx, out):
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, str(INSPECTOR), str(docx), "--out", str(out)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def load_inspector_module():
    spec = importlib.util.spec_from_file_location("inspect_docx_hardening_target", INSPECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DocxImageHardeningTests(unittest.TestCase):
    def assert_clean_failure(self, result, message):
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(message, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertLess(len(result.stderr), 1000)

    def test_unreferenced_missing_header_image_relationship_is_hard_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "header-unreferenced.docx"
            make_docx(docx, members={
                "word/header1.xml": STORY_ROOTS["word/header1.xml"],
                "word/_rels/header1.xml.rels": relationships_xml(image_relationship(target="media/missing.png")),
            })
            result = run_inspector(docx, root / "inspection")
            self.assert_clean_failure(result, "媒体缺失")

    def test_unreferenced_missing_image_relationship_is_hard_failure_in_other_stories(self):
        for story in ("word/footer1.xml", "word/footnotes.xml", "word/endnotes.xml", "word/comments.xml"):
            with self.subTest(story=story), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                docx = root / "story-unreferenced.docx"
                rel_part = f"word/_rels/{Path(story).name}.rels"
                make_docx(docx, members={
                    story: STORY_ROOTS[story],
                    rel_part: relationships_xml(image_relationship(target="media/missing.png")),
                })
                result = run_inspector(docx, root / "inspection")
                self.assert_clean_failure(result, "媒体缺失")

    def test_comment_missing_image_relationship_id_is_hard_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "comment-missing-id.docx"
            make_docx(docx, members={
                "word/comments.xml": comments_with_image(rid="rIdMissing"),
                "word/_rels/comments.xml.rels": relationships_xml(),
            })
            result = run_inspector(docx, root / "inspection")
            self.assert_clean_failure(result, "图片关系 ID 缺失")

    def test_comment_missing_internal_image_media_is_hard_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "comment-missing-media.docx"
            make_docx(docx, members={
                "word/comments.xml": comments_with_image(),
                "word/_rels/comments.xml.rels": relationships_xml(image_relationship(target="media/missing.png")),
            })
            result = run_inspector(docx, root / "inspection")
            self.assert_clean_failure(result, "媒体缺失")

    def test_comment_internal_image_reference_is_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "comment-internal.docx"
            out = root / "inspection"
            make_docx(docx, members={
                "word/comments.xml": comments_with_image(),
                "word/_rels/comments.xml.rels": relationships_xml(image_relationship()),
                "word/media/image1.png": PNG,
            })
            result = run_inspector(docx, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((out / "inspection.json").read_text(encoding="utf-8"))
            self.assertEqual(data["counts"]["images"], 1)
            self.assertEqual(data["counts"]["image_references"], 1)
            reference = data["image_references"][0]
            self.assertEqual(reference["part"], "word/comments.xml")
            self.assertEqual(reference["media_part"], "word/media/image1.png")
            self.assertEqual(reference["paragraph_index"], 1)
            self.assertEqual(data["comments"][0]["text"], "批注")

    def test_comment_external_image_reference_is_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "comment-external.docx"
            out = root / "inspection"
            target = "https://images.example.invalid/comment.png"
            make_docx(docx, members={
                "word/comments.xml": comments_with_image(attribute="link"),
                "word/_rels/comments.xml.rels": relationships_xml(image_relationship(target=target, external=True)),
            })
            result = run_inspector(docx, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((out / "inspection.json").read_text(encoding="utf-8"))
            self.assertEqual(data["counts"]["images"], 0)
            self.assertEqual(data["counts"]["image_references"], 0)
            self.assertEqual(data["counts"]["external_images"], 1)
            self.assertEqual(data["external_images"][0]["url"], target)
            self.assertEqual(data["external_images"][0]["part"], "word/comments.xml")

    def test_comment_percent_encoded_image_target_resolves(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "comment-encoded-image.docx"
            out = root / "inspection"
            make_docx(docx, members={
                "word/comments.xml": comments_with_image(),
                "word/_rels/comments.xml.rels": relationships_xml(image_relationship(target="media/image%201.png")),
                "word/media/image 1.png": PNG,
            })
            result = run_inspector(docx, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((out / "inspection.json").read_text(encoding="utf-8"))
            self.assertEqual(data["counts"]["images"], 1)
            self.assertEqual(data["counts"]["image_references"], 1)
            reference = data["image_references"][0]
            self.assertEqual(reference["part"], "word/comments.xml")
            self.assertEqual(reference["media_part"], "word/media/image 1.png")
            self.assertEqual((out / "media" / "image_1.png").read_bytes(), PNG)

    def test_comment_percent_encoded_parent_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "comment-traversal.docx"
            make_docx(docx, members={
                "word/comments.xml": comments_with_image(),
                "word/_rels/comments.xml.rels": relationships_xml(image_relationship(target="%2e%2e/%2e%2e/outside.png")),
                "outside.png": PNG,
            })
            result = run_inspector(docx, root / "inspection")
            self.assert_clean_failure(result, "越界")

    def test_percent_encoded_space_in_image_target_resolves(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "encoded-space.docx"
            out = root / "inspection"
            make_docx(docx, body=image_paragraph(), members={
                "word/_rels/document.xml.rels": relationships_xml(image_relationship(target="media/image%201.png")),
                "word/media/image 1.png": PNG,
            })
            result = run_inspector(docx, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((out / "inspection.json").read_text(encoding="utf-8"))
            self.assertEqual(data["images"][0]["part"], "word/media/image 1.png")
            self.assertEqual(data["image_references"][0]["media_part"], "word/media/image 1.png")
            self.assertEqual((out / "media" / "image_1.png").read_bytes(), PNG)

    def test_image_relationship_outside_word_media_is_extracted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "custom-image-part.docx"
            out = root / "inspection"
            content_types = base_members("")["[Content_Types].xml"].replace(
                "</Types>",
                '<Override PartName="/custom/press.bin" ContentType="image/png"/></Types>',
            )
            make_docx(docx, body=image_paragraph(), members={
                "[Content_Types].xml": content_types,
                "word/_rels/document.xml.rels": relationships_xml(image_relationship(target="../custom/press.bin")),
                "custom/press.bin": PNG,
            })
            result = run_inspector(docx, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((out / "inspection.json").read_text(encoding="utf-8"))
            self.assertEqual(data["images"][0]["part"], "custom/press.bin")
            self.assertEqual(data["image_references"][0]["media_part"], "custom/press.bin")
            self.assertEqual((out / "media" / "press.bin").read_bytes(), PNG)

    def test_image_relationship_requires_image_content_type(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "not-an-image.docx"
            make_docx(docx, body=image_paragraph(), members={
                "word/_rels/document.xml.rels": relationships_xml(image_relationship(target="media/not-image.xml")),
                "word/media/not-image.xml": "<not-image/>",
            })
            result = run_inspector(docx, root / "inspection")
            self.assert_clean_failure(result, "内容类型")

    def test_unreferenced_images_declared_by_content_types_are_collected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "unreferenced-images.docx"
            out = root / "inspection"
            jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 32
            content_types = base_members("")["[Content_Types].xml"].replace(
                "</Types>",
                '<Override PartName="/custom/unreferenced.jpg" ContentType="image/jpeg"/></Types>',
            )
            make_docx(docx, members={
                "[Content_Types].xml": content_types,
                "word/media/unused.png": PNG,
                "custom/unreferenced.jpg": jpeg,
            })
            result = run_inspector(docx, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((out / "inspection.json").read_text(encoding="utf-8"))
            self.assertEqual(data["counts"]["images"], 2)
            self.assertEqual(data["counts"]["image_references"], 0)
            self.assertTrue(data["flags"]["has_images"])
            self.assertCountEqual(
                [item["part"] for item in data["images"]],
                ["word/media/unused.png", "custom/unreferenced.jpg"],
            )
            self.assertEqual((out / "media" / "unused.png").read_bytes(), PNG)
            self.assertEqual((out / "media" / "unreferenced.jpg").read_bytes(), jpeg)

    def test_unreferenced_non_images_are_not_counted_as_images(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "unreferenced-non-images.docx"
            out = root / "inspection"
            content_types = base_members("")["[Content_Types].xml"].replace(
                "</Types>",
                '<Override PartName="/word/media/not-image.png" ContentType="application/octet-stream"/></Types>',
            )
            make_docx(docx, members={
                "[Content_Types].xml": content_types,
                "word/media/not-image.png": PNG,
                "word/media/undeclared.bin": PNG,
            })
            result = run_inspector(docx, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((out / "inspection.json").read_text(encoding="utf-8"))
            self.assertEqual(data["counts"]["images"], 0)
            self.assertEqual(data["images"], [])
            self.assertFalse(data["flags"]["has_images"])
            self.assertEqual(list((out / "media").iterdir()), [])

    def test_image_relationship_target_without_image_content_type_is_hard_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "undeclared-image-target.docx"
            make_docx(docx, body=image_paragraph(), members={
                "word/_rels/document.xml.rels": relationships_xml(image_relationship(target="media/image.bin")),
                "word/media/image.bin": PNG,
            })
            result = run_inspector(docx, root / "inspection")
            self.assert_clean_failure(result, "内容类型不是图片")

    def test_percent_encoded_parent_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "traversal.docx"
            content_types = base_members("")["[Content_Types].xml"].replace(
                "</Types>",
                '<Override PartName="/outside.png" ContentType="image/png"/></Types>',
            )
            make_docx(docx, body=image_paragraph(), members={
                "[Content_Types].xml": content_types,
                "word/_rels/document.xml.rels": relationships_xml(image_relationship(target="%2e%2e/%2e%2e/outside.png")),
                "outside.png": PNG,
            })
            result = run_inspector(docx, root / "inspection")
            self.assert_clean_failure(result, "越界")

    def test_non_media_image_respects_member_size_limit(self):
        module = load_inspector_module()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "oversized-custom-image.docx"
            content_types = '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="bin" ContentType="image/png"/></Types>'
            with zipfile.ZipFile(docx, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("[Content_Types].xml", content_types)
                zf.writestr("custom/press.bin", b"x" * 512)
            rels = {"rId1": {"type": IMAGE_REL, "target": "../custom/press.bin", "external": False}}
            with zipfile.ZipFile(docx) as zf, mock.patch.object(module, "MAX_MEMBER_BYTES", 256):
                with self.assertRaisesRegex(ValueError, "大小上限"):
                    module.extract_media(zf, rels, root / "inspection", [], False, "chi_sim+eng")


class DocxBodyHardeningTests(unittest.TestCase):
    def test_body_text_preserves_paragraph_table_interleaving(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "interleaved.docx"
            out = root / "inspection"
            body = ''.join([
                '<w:p><w:r><w:t>段落A</w:t></w:r></w:p>',
                '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>单元格</w:t></w:r></w:p></w:tc></w:tr></w:tbl>',
                '<w:p><w:r><w:t>段落B</w:t></w:r></w:p>',
            ])
            make_docx(docx, body=body)
            result = run_inspector(docx, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((out / "inspection.json").read_text(encoding="utf-8"))
            self.assertEqual([block["type"] for block in data["blocks"]], ["paragraph", "table", "paragraph"])
            self.assertEqual((out / "body.txt").read_text(encoding="utf-8"), "段落A\n单元格\n段落B")

    def test_sdt_paragraphs_and_table_blocks_stay_in_document_order(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "sdt-blocks.docx"
            out = root / "inspection"
            table = '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>表格保留</w:t></w:r><w:del w:id="1"><w:r><w:delText>表格删除</w:delText></w:r></w:del></w:p></w:tc></w:tr></w:tbl>'
            body = ''.join([
                '<w:p><w:r><w:t>开始</w:t></w:r></w:p>',
                '<w:sdt><w:sdtPr/><w:sdtContent><w:sdt><w:sdtPr/><w:sdtContent>',
                '<w:p><w:r><w:t>控件前</w:t></w:r><w:del w:id="2"><w:r><w:delText>控件删除</w:delText></w:r></w:del><w:r><w:t>控件后</w:t></w:r></w:p>',
                table,
                '</w:sdtContent></w:sdt></w:sdtContent></w:sdt>',
                '<w:p><w:r><w:t>结束</w:t></w:r></w:p>',
            ])
            make_docx(docx, body=body)
            result = run_inspector(docx, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((out / "inspection.json").read_text(encoding="utf-8"))
            self.assertEqual([block["type"] for block in data["blocks"]], ["paragraph", "paragraph", "table", "paragraph"])
            self.assertEqual([paragraph["text"] for paragraph in data["paragraphs"]], ["开始", "控件前控件后", "表格保留", "结束"])
            self.assertEqual([paragraph["text_without_deletions"] for paragraph in data["paragraphs"]], ["开始", "控件前控件后", "表格保留", "结束"])
            self.assertEqual(data["tables"], [[["表格保留"]]])
            self.assertEqual(data["blocks"][2]["rows"], [["表格保留"]])
            self.assertEqual((out / "body.txt").read_text(encoding="utf-8"), "开始\n控件前控件后\n表格保留\n结束")
            self.assertEqual([item["text"] for item in data["tracked_changes"]["deletions"]], ["控件删除", "表格删除"])

    def test_custom_xml_paragraphs_and_table_blocks_stay_in_document_order(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "custom-xml-blocks.docx"
            out = root / "inspection"
            body = ''.join([
                '<w:p><w:r><w:t>开始</w:t></w:r></w:p>',
                '<w:customXml><w:customXmlPr/><w:sdt><w:sdtPr/><w:sdtContent>',
                '<w:p><w:r><w:t>自定义段落</w:t></w:r></w:p>',
                '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>自定义表格</w:t></w:r></w:p></w:tc></w:tr></w:tbl>',
                '</w:sdtContent></w:sdt></w:customXml>',
                '<w:p><w:r><w:t>结束</w:t></w:r></w:p>',
            ])
            make_docx(docx, body=body)
            result = run_inspector(docx, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((out / "inspection.json").read_text(encoding="utf-8"))
            self.assertEqual([block["type"] for block in data["blocks"]], ["paragraph", "paragraph", "table", "paragraph"])
            self.assertEqual([paragraph["text"] for paragraph in data["paragraphs"]], ["开始", "自定义段落", "自定义表格", "结束"])
            self.assertEqual(data["tables"], [[["自定义表格"]]])
            self.assertEqual((out / "body.txt").read_text(encoding="utf-8"), "开始\n自定义段落\n自定义表格\n结束")

    def test_table_text_excludes_deletions_but_tracked_changes_keep_them(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "table-deletions.docx"
            out = root / "inspection"
            body = ''.join([
                '<w:p><w:r><w:t>正文</w:t></w:r></w:p>',
                '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>保留</w:t></w:r><w:del w:id="1" w:author="审阅者"><w:r><w:delText>删除一</w:delText></w:r><w:del><w:r><w:t>删除二</w:t></w:r></w:del></w:p></w:tc></w:tr></w:tbl>',
            ])
            make_docx(docx, body=body)
            result = run_inspector(docx, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((out / "inspection.json").read_text(encoding="utf-8"))
            self.assertEqual(data["tables"], [[["保留"]]])
            self.assertEqual(data["blocks"][1]["rows"], [["保留"]])
            self.assertEqual((out / "body.txt").read_text(encoding="utf-8"), "正文\n保留")
            self.assertEqual(len(data["tracked_changes"]["deletions"]), 1)
            self.assertEqual(data["tracked_changes"]["deletions"][0]["text"], "删除一删除二")

    def test_simple_hyperlink_javascript_field_is_flagged_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "simple-script-field.docx"
            out = root / "inspection"
            body = '<w:p><w:fldSimple w:instr=\' HYPERLINK "javascript:alert(1)" \'><w:r><w:t>脚本链接</w:t></w:r></w:fldSimple></w:p>'
            make_docx(docx, body=body)
            result = run_inspector(docx, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((out / "inspection.json").read_text(encoding="utf-8"))
            self.assertEqual(data["counts"]["field_codes"], 1)
            self.assertTrue(data["flags"]["has_script_fields"])
            field = data["field_codes"][0]
            self.assertEqual(field["instruction"], 'HYPERLINK "javascript:alert(1)"')
            self.assertEqual(field["target_url"], "javascript:alert(1)")
            self.assertEqual(field["paragraph_index"], 1)
            self.assertTrue(field["is_script_scheme"])

    def test_comment_links_and_fields_are_checked_without_becoming_other_story_parts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "comment-link-field.docx"
            out = root / "inspection"
            target = "https://comments.example.invalid/review"
            make_docx(docx, members={
                "word/comments.xml": comments_with_link_and_script_field(),
                "word/_rels/comments.xml.rels": relationships_xml(hyperlink_relationship("rIdLink", target)),
            })
            result = run_inspector(docx, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((out / "inspection.json").read_text(encoding="utf-8"))
            self.assertEqual(data["counts"]["comments"], 1)
            self.assertEqual(data["counts"]["external_links"], 1)
            self.assertEqual(data["counts"]["field_codes"], 1)
            self.assertEqual(data["counts"]["other_story_parts"], 0)
            self.assertNotIn("word/comments.xml", data["other_story_parts"])
            self.assertEqual(data["comments"][0]["text"], "外部链接脚本字段")
            self.assertEqual(data["external_links"][0]["url"], target)
            self.assertEqual(data["external_links"][0]["part"], "word/comments.xml")
            self.assertEqual(data["external_links"][0]["source"], "hyperlink")
            self.assertEqual(data["external_links"][0]["paragraph_index"], 1)
            field = data["field_codes"][0]
            self.assertEqual(field["part"], "word/comments.xml")
            self.assertEqual(field["paragraph_index"], 1)
            self.assertEqual(field["instruction"], 'HYPERLINK "javascript:alert(1)"')
            self.assertEqual(field["target_url"], "javascript:alert(1)")
            self.assertTrue(field["is_script_scheme"])
            self.assertTrue(data["flags"]["has_external_links"])
            self.assertTrue(data["flags"]["has_script_fields"])


if __name__ == "__main__":
    unittest.main()
