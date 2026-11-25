#!/usr/bin/env python3
import argparse
import hashlib
import json
import posixpath
import re
import shutil
import subprocess
import sys
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
MAX_MEMBER_BYTES = 200 * 1024 * 1024
MAX_MEMBERS = 10000
MAX_TOTAL_BYTES = 500 * 1024 * 1024
STORY_PART_RE = re.compile(r"^word/(?:(?:header|footer|footnotes|endnotes)[^/]*\.xml|comments\.xml)$")


def local(tag):
    return tag.rsplit("}", 1)[-1]


def iter_paragraph_content(element):
    yield element
    for child in list(element):
        if local(child.tag) == "p":
            continue
        yield from iter_paragraph_content(child)


def text_of(element, include_deleted=True):
    if element is None:
        return ""
    values = []
    for node in element.iter():
        name = local(node.tag)
        if name == "t" or (include_deleted and name == "delText"):
            values.append(node.text or "")
        elif name == "tab":
            values.append("\t")
        elif name in {"br", "cr"}:
            values.append("\n")
    return "".join(values).strip()


def text_without_deletions(element):
    values = []

    def walk(node):
        name = local(node.tag)
        if name in {"del", "moveFrom"} or (node is not element and name == "p" and local(element.tag) == "p"):
            return
        if name == "t":
            values.append(node.text or "")
        elif name == "tab":
            values.append("\t")
        elif name in {"br", "cr"}:
            values.append("\n")
        for child in list(node):
            walk(child)

    walk(element)
    return "".join(values).strip()


def text_of_paragraph(element):
    values = []
    for node in iter_paragraph_content(element):
        name = local(node.tag)
        if name == "t":
            values.append(node.text or "")
        elif name == "tab":
            values.append("\t")
        elif name in {"br", "cr"}:
            values.append("\n")
    return "".join(values).strip()


def load_xml(zf, name):
    try:
        data = zf.read(name)
    except KeyError:
        return None
    if len(data) > MAX_MEMBER_BYTES:
        raise ValueError(f"XML 文件过大：{name}")
    try:
        if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
            encoding = "utf-32"
        elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
            encoding = "utf-16"
        else:
            encoding = "utf-8-sig"
        text = data.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        text = data.decode("utf-8", errors="ignore")
    upper = text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise ValueError(f"拒绝解析含 DTD/实体声明的 XML：{name}")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"XML 解析失败：{name}: {exc}") from exc


def relationships(zf, part_name):
    rel_name = posixpath.join(posixpath.dirname(part_name), "_rels", posixpath.basename(part_name) + ".rels")
    root = load_xml(zf, rel_name)
    result = {}
    if root is None:
        return result
    for rel in root:
        rid = rel.get("Id")
        if rid:
            result[rid] = {
                "type": rel.get("Type", ""),
                "target": rel.get("Target", ""),
                "external": rel.get("TargetMode") == "External",
            }
    return result


def image_content_types(zf):
    root = load_xml(zf, "[Content_Types].xml")
    defaults = {}
    overrides = {}
    if root is None:
        return defaults, overrides
    for node in root:
        name = local(node.tag)
        if name == "Default":
            extension = (node.get("Extension") or "").lower()
            if extension:
                defaults[extension] = node.get("ContentType") or ""
        elif name == "Override":
            value = node.get("PartName") or ""
            if not value:
                continue
            part = normalize_part_path(decode_uri_path(value).lstrip("/"))
            overrides[part] = node.get("ContentType") or ""
    return defaults, overrides


def is_image_part(part_name, defaults, overrides):
    content_type = overrides.get(part_name)
    if content_type is None:
        extension = posixpath.splitext(part_name)[1].lstrip(".").lower()
        content_type = defaults.get(extension, "")
    return content_type.lower().startswith("image/")


def decode_uri_path(value):
    try:
        decoded = urllib.parse.unquote(value, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(f"URI 路径编码无效：{value}") from exc
    if not decoded or "\x00" in decoded or "\\" in decoded:
        raise ValueError(f"URI 路径无效：{value}")
    return decoded


def normalize_part_path(value):
    path = posixpath.normpath(value)
    if path in {"", "."} or path == ".." or path.startswith("../") or path.startswith("/"):
        raise ValueError(f"部件路径越界：{value}")
    return path


def resolve_part(part_name, target):
    decoded = decode_uri_path(target)
    if decoded.startswith("/"):
        joined = decoded.lstrip("/")
    else:
        joined = posixpath.join(posixpath.dirname(part_name), decoded)
    try:
        return normalize_part_path(joined)
    except ValueError as exc:
        raise ValueError(f"关系目标路径越界：{target}") from exc


def collect_image_parts(zf, story_rels):
    targets = []
    for part_name, rels in story_rels.items():
        for rid, rel in rels.items():
            if rel.get("external") or not rel.get("type", "").endswith("/image"):
                continue
            target = resolve_part(part_name, rel.get("target", ""))
            try:
                info = zf.getinfo(target)
            except KeyError as exc:
                raise ValueError(f"图片关系引用媒体缺失：{part_name} {rid} {target}") from exc
            if info.is_dir():
                raise ValueError(f"图片关系引用媒体缺失：{part_name} {rid} {target}")
            if info.file_size > MAX_MEMBER_BYTES:
                raise ValueError(f"引用媒体超过大小上限：{target}")
            targets.append((target, part_name, rid))
    defaults, overrides = image_content_types(zf)
    for target, part_name, rid in targets:
        if not is_image_part(target, defaults, overrides):
            raise ValueError(f"图片关系目标内容类型不是图片：{part_name} {rid} {target}")
    image_parts = {
        info.filename
        for info in zf.infolist()
        if not info.is_dir() and is_image_part(info.filename, defaults, overrides)
    }
    image_parts.update(target for target, _, _ in targets)
    member_order = {name: index for index, name in enumerate(zf.namelist())}
    return sorted(image_parts, key=member_order.__getitem__)


def external_url(value):
    value = value.strip().strip('"')
    return re.match(r"^(?:https?|mailto):", value, re.I) is not None


def field_target(instruction):
    match = re.search(r"HYPERLINK\s+(?:\"([^\"]+)\"|'([^']+)'|([^\s\\]+))", instruction, re.I)
    if match:
        return next((group for group in match.groups() if group is not None), "")
    return instruction.strip().strip('"')


def is_tracking_url(value):
    parsed = urllib.parse.urlsplit(field_target(value))
    host = (parsed.hostname or "").lower()
    return any(token in host for token in ("clickx", "dspmnt", "track", "monitor", "analytics"))


def script_url(value):
    value = field_target(value)
    return re.match(r"^(?:javascript|vbscript|data):", value, re.I) is not None


def field_instructions(paragraph):
    instructions = []
    current = []
    for node in iter_paragraph_content(paragraph):
        name = local(node.tag)
        if name == "fldChar":
            field_type = node.get(f"{{{W}}}fldCharType")
            if field_type == "begin":
                current = []
            elif field_type == "separate":
                continue
            elif field_type == "end":
                instruction = "".join(current).strip()
                if instruction:
                    instructions.append(instruction)
                current = []
        elif name == "instrText":
            current.append(node.text or "")
        elif name == "fldSimple":
            instruction = (node.get(f"{{{W}}}instr") or "").strip()
            if instruction:
                instructions.append(instruction)
    for node in iter_paragraph_content(paragraph):
        if local(node.tag) == "fldSimple":
            instruction = (node.get(f"{{{W}}}instr") or "").strip()
            if instruction and instruction not in instructions:
                instructions.append(instruction)
    return instructions


def paragraph_record(paragraph, index, part_name, rels, location):
    text = text_of_paragraph(paragraph)
    content = list(iter_paragraph_content(paragraph))
    hidden_runs = []
    for run in content:
        if local(run.tag) != "r":
            continue
        props = run.find(f"{{{W}}}rPr")
        if props is not None and any(props.find(f"{{{W}}}{tag}") is not None for tag in ("vanish", "webHidden")):
            hidden_runs.append(text_of(run))
    image_ids = []
    for node in content:
        if local(node.tag) not in {"blip", "imagedata"}:
            continue
        for attribute in ("embed", "link", "id"):
            value = node.get(f"{{{R}}}{attribute}")
            if value and value not in image_ids:
                image_ids.append(value)
    hyperlink_ids = [node.get(f"{{{R}}}id") for node in content if local(node.tag) == "hyperlink" and node.get(f"{{{R}}}id")]
    return {
        "index": index,
        "part": part_name,
        "location": location,
        "style": (paragraph.find(f"{{{W}}}pPr/{{{W}}}pStyle").get(f"{{{W}}}val") if paragraph.find(f"{{{W}}}pPr/{{{W}}}pStyle") is not None else None),
        "text": text,
        "text_without_deletions": text_without_deletions(paragraph),
        "hidden_text": hidden_runs,
        "image_relationship_ids": image_ids,
        "hyperlink_relationship_ids": hyperlink_ids,
        "field_instructions": field_instructions(paragraph),
        "relationships": {rid: rels.get(rid) for rid in hyperlink_ids if rid in rels},
    }


def table_record(table, index):
    rows = []
    for row in table.findall(f"{{{W}}}tr"):
        rows.append([text_without_deletions(cell) for cell in row.findall(f"{{{W}}}tc")])
    return rows


def iter_body_blocks(body):
    def walk(element):
        name = local(element.tag)
        if name in {"p", "tbl"}:
            yield element
        elif name not in {"del", "moveFrom"}:
            for child in list(element):
                yield from walk(child)

    yield from walk(body)


def add_field_links(field_codes, external_links, paragraph, part_name):
    for instruction in field_instructions(paragraph):
        item = {
            "instruction": instruction,
            "part": part_name,
            "paragraph_index": paragraph_record_index.get((part_name, id(paragraph))),
            "is_script_scheme": script_url(instruction),
        }
        field_codes.append(item)
        if re.match(r"\s*HYPERLINK\b", instruction, re.I):
            target = field_target(instruction)
            if external_url(target):
                external_links.append({"url": target, "part": part_name, "source": "field", "instruction": instruction})


paragraph_record_index = {}


def extract_media(zf, rels, out_dir, warnings, ocr_enabled, ocr_lang, image_parts=None):
    media_dir = out_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    tesseract = shutil.which("tesseract") if ocr_enabled else None
    if ocr_enabled and not tesseract:
        warnings.append("OCR_UNAVAILABLE: 未找到 tesseract；图片仍需人工查看")
    if image_parts is None:
        image_parts = collect_image_parts(zf, {"word/document.xml": rels})
    images = []
    for name in image_parts:
        try:
            info = zf.getinfo(name)
        except KeyError as exc:
            raise ValueError(f"图片关系引用媒体缺失：{name}") from exc
        if info.is_dir() or info.file_size > MAX_MEMBER_BYTES:
            if info.is_dir():
                raise ValueError(f"图片关系引用媒体缺失：{name}")
            raise ValueError(f"引用媒体超过大小上限：{name}")
        data = zf.read(name)
        digest = hashlib.sha256(data).hexdigest()
        filename = re.sub(r"[^A-Za-z0-9._-]", "_", posixpath.basename(name)) or "image.bin"
        destination = media_dir / filename
        if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
            destination = media_dir / f"{digest[:12]}_{filename}"
        destination.write_bytes(data)
        images.append({
            "part": name,
            "file": destination.relative_to(out_dir).as_posix(),
            "bytes": len(data),
            "sha256": digest,
            "next_paragraph_text": None,
            "alt_text": None,
            "ocr_text": None,
            "ocr_error": None,
        })
    if tesseract:
        for image in images:
            try:
                result = subprocess.run(
                    [tesseract, str(out_dir / image["file"]), "stdout", "-l", ocr_lang],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=45,
                )
            except subprocess.TimeoutExpired:
                image["ocr_error"] = "OCR_TIMEOUT"
                warnings.append(f"OCR_TIMEOUT: {image['file']}")
                continue
            if result.returncode == 0:
                image["ocr_text"] = result.stdout.strip()
            else:
                image["ocr_error"] = result.stderr.strip()[:500]
                warnings.append(f"OCR_FAILED: {image['file']}")
    return images


def associate_story_images(elements, paragraphs, part_name, rels, images, external_images, image_references):
    for index, element in enumerate(elements):
        record = paragraphs[index]
        for rid in record["image_relationship_ids"]:
            rel = rels.get(rid)
            if not rel:
                raise ValueError(f"图片关系 ID 缺失：{part_name} {rid}")
            if not rel.get("type", "").endswith("/image"):
                raise ValueError(f"图片引用关系类型无效：{part_name} {rid}")
            if rel.get("external"):
                external_images.append({"url": rel.get("target"), "part": part_name, "paragraph_index": record["index"], "source": "image_relationship"})
                continue
            part = resolve_part(part_name, rel.get("target", ""))
            image = next((item for item in images if item["part"] == part), None)
            if image is None:
                raise ValueError(f"图片关系无法解析或引用媒体缺失：{part}")
            next_text = paragraphs[index + 1]["text"] if index + 1 < len(paragraphs) else None
            alt_text = next((node.get("descr") or node.get("title") for node in iter_paragraph_content(element) if local(node.tag) == "docPr" and (node.get("descr") or node.get("title"))), None)
            image_references.append({"part": part_name, "media_part": part, "file": image["file"], "paragraph_index": record["index"], "next_paragraph_text": next_text, "alt_text": alt_text})
            if image["next_paragraph_text"] is None:
                image["next_paragraph_text"] = next_text
            if image["alt_text"] is None:
                image["alt_text"] = alt_text


def inspect_docx(docx_path, out_dir, ocr_enabled=False, ocr_lang="chi_sim+eng"):
    warnings = []
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(docx_path) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_MEMBERS:
            raise ValueError(f"ZIP 成员数超过上限：{len(infos)} > {MAX_MEMBERS}")
        if sum(info.file_size for info in infos) > MAX_TOTAL_BYTES:
            raise ValueError("ZIP 解压总大小超过上限")
        document = load_xml(zf, "word/document.xml")
        if document is None:
            raise ValueError("缺少 word/document.xml，不是可读取的 DOCX")
        rels = relationships(zf, "word/document.xml")
        story_roots = {"word/document.xml": document}
        story_rels = {"word/document.xml": rels}
        for name in sorted(zf.namelist()):
            if name == "word/document.xml" or STORY_PART_RE.match(name) is None:
                continue
            root = load_xml(zf, name)
            if root is None:
                continue
            story_roots[name] = root
            story_rels[name] = relationships(zf, name)
        image_parts = collect_image_parts(zf, story_rels)
        images = extract_media(zf, rels, out_dir, warnings, ocr_enabled, ocr_lang, image_parts=image_parts)
        body = document.find(f"{{{W}}}body")
        if body is None:
            raise ValueError("word/document.xml 缺少 body")

        paragraph_elements = body.findall(f".//{{{W}}}p")
        paragraphs = [
            paragraph_record(element, index, "word/document.xml", rels, f"正文段落 {index}")
            for index, element in enumerate(paragraph_elements, 1)
        ]
        paragraph_index_by_id = {id(element): index for index, element in enumerate(paragraph_elements, 1)}
        for element, index in paragraph_index_by_id.items():
            paragraph_record_index[("word/document.xml", element)] = index
        tables = []
        blocks = []
        for child in iter_body_blocks(body):
            name = local(child.tag)
            if name == "p":
                index = paragraph_index_by_id[id(child)]
                blocks.append({"type": "paragraph", "index": index, "text": paragraphs[index - 1]["text_without_deletions"]})
            elif name == "tbl":
                rows = table_record(child, len(tables) + 1)
                tables.append(rows)
                blocks.append({"type": "table", "index": len(tables), "rows": rows})

        external_images = []
        image_references = []
        associate_story_images(paragraph_elements, paragraphs, "word/document.xml", rels, images, external_images, image_references)

        external_links = []
        field_codes = []
        for element, paragraph in zip(paragraph_elements, paragraphs):
            for rid in paragraph["hyperlink_relationship_ids"]:
                rel = rels.get(rid, {})
                if rel.get("external") and external_url(rel["target"]):
                    external_links.append({"url": rel["target"], "part": "word/document.xml", "source": "hyperlink", "paragraph_index": paragraph["index"]})
            add_field_links(field_codes, external_links, element, "word/document.xml")

        headers_footers = {}
        other_story_parts = {}
        for name, root in story_roots.items():
            if name == "word/document.xml":
                continue
            rels_for_part = story_rels[name]
            part_elements = root.findall(f".//{{{W}}}p")
            part_paragraphs = [paragraph_record(p, i + 1, name, rels_for_part, f"{name} 段落 {i + 1}") for i, p in enumerate(part_elements)]
            for element, index in {id(element): index for index, element in enumerate(part_elements, 1)}.items():
                paragraph_record_index[(name, element)] = index
            associate_story_images(part_elements, part_paragraphs, name, rels_for_part, images, external_images, image_references)
            for element, paragraph in zip(part_elements, part_paragraphs):
                for rid in paragraph["hyperlink_relationship_ids"]:
                    rel = rels_for_part.get(rid, {})
                    if rel.get("external") and external_url(rel["target"]):
                        external_links.append({"url": rel["target"], "part": name, "source": "hyperlink", "paragraph_index": paragraph["index"]})
                add_field_links(field_codes, external_links, element, name)
            if name == "word/comments.xml":
                continue
            if re.match(r"^word/(?:header|footer)", name):
                headers_footers[name] = [item["text"] for item in part_paragraphs]
            else:
                other_story_parts[name] = [item["text"] for item in part_paragraphs]

        tracked = {"insertions": [], "deletions": [], "moves_from": [], "moves_to": [], "property_changes": []}
        hidden = []
        for name in sorted(zf.namelist()):
            if not name.startswith("word/") or not name.endswith(".xml"):
                continue
            root = load_xml(zf, name)
            if root is None:
                continue
            for tag, key in (("ins", "insertions"), ("del", "deletions"), ("moveFrom", "moves_from"), ("moveTo", "moves_to")):
                for node in root.iter(f"{{{W}}}{tag}"):
                    tracked[key].append({"part": name, "author": node.get(f"{{{W}}}author"), "date": node.get(f"{{{W}}}date"), "text": text_of(node)})
            for node in root.iter():
                if local(node.tag).endswith("PrChange") or local(node.tag) in {"numberingChange", "cellIns", "cellDel", "cellMerge"}:
                    tracked["property_changes"].append({"part": name, "tag": local(node.tag), "author": node.get(f"{{{W}}}author"), "date": node.get(f"{{{W}}}date")})
            for run in root.iter(f"{{{W}}}r"):
                props = run.find(f"{{{W}}}rPr")
                if props is not None and any(props.find(f"{{{W}}}{tag}") is not None for tag in ("vanish", "webHidden")):
                    hidden.append({"part": name, "text": text_of(run)})

        comments = []
        comments_root = story_roots.get("word/comments.xml")
        if comments_root is not None:
            for comment in comments_root.findall(f"{{{W}}}comment"):
                comments.append({"id": comment.get(f"{{{W}}}id"), "author": comment.get(f"{{{W}}}author"), "date": comment.get(f"{{{W}}}date"), "text": text_of(comment)})

        properties = {}
        for name in sorted(zf.namelist()):
            if name.startswith("docProps/") and name.endswith(".xml"):
                root = load_xml(zf, name)
                if root is not None:
                    properties[name] = {local(node.tag): (node.text or "") for node in root}
        core_properties = properties.get("docProps/core.xml", {})

        embedded = [name for name in zf.namelist() if name.startswith("word/embeddings/")]
        activex_parts = [name for name in zf.namelist() if name.startswith("word/activeX/")]
        ole_relationships = []
        for name in zf.namelist():
            if not name.endswith(".rels"):
                continue
            root = load_xml(zf, name)
            if root is None:
                continue
            for rel in root:
                if (rel.get("Type") or "").endswith("/oleObject"):
                    ole_relationships.append({"part": name, "target": rel.get("Target"), "external": rel.get("TargetMode") == "External"})
        has_macros = "word/vbaProject.bin" in zf.namelist()
        for link in external_links:
            parsed = urllib.parse.urlsplit(link["url"])
            link["host"] = parsed.hostname
            link["is_tracking"] = is_tracking_url(link["url"])
            link["display_url"] = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        for field in field_codes:
            target = field_target(field["instruction"])
            field["target_url"] = target
            field["is_tracking"] = is_tracking_url(target)
            field["is_script_scheme"] = script_url(target)
        flags = {
            "has_tracked_changes": any(tracked.values()),
            "has_comments": bool(comments),
            "has_external_links": bool(external_links),
            "has_tracking_fields": any(item["is_tracking"] for item in external_links) or any(item["is_tracking"] for item in field_codes),
            "has_script_fields": any(item["is_script_scheme"] for item in field_codes),
            "has_hidden_text": bool(hidden),
            "has_images": bool(images),
            "has_external_images": bool(external_images),
            "has_embedded_objects": bool(embedded),
            "has_activex": bool(activex_parts),
            "has_ole": bool(embedded or ole_relationships),
            "has_macros": has_macros,
        }
        data = {
            "document": str(docx_path),
            "counts": {
                "paragraphs": len(paragraphs),
                "tables": len(tables),
                "images": len(images),
                "image_references": len(image_references),
                "external_images": len(external_images),
                "external_links": len(external_links),
                "field_codes": len(field_codes),
                "tracked_insertions": len(tracked["insertions"]),
                "tracked_deletions": len(tracked["deletions"]),
                "tracked_moves_from": len(tracked["moves_from"]),
                "tracked_moves_to": len(tracked["moves_to"]),
                "property_changes": len(tracked["property_changes"]),
                "comments": len(comments),
                "headers_footers": len(headers_footers),
                "other_story_parts": len(other_story_parts),
            },
            "paragraphs": paragraphs,
            "blocks": blocks,
            "tables": tables,
            "images": images,
            "image_references": image_references,
            "external_images": external_images,
            "external_links": external_links,
            "field_codes": field_codes,
            "tracked_changes": tracked,
            "hidden_text": hidden,
            "comments": comments,
            "headers_footers": headers_footers,
            "other_story_parts": other_story_parts,
            "document_properties": properties,
            "core_properties": core_properties,
            "embedded_objects": embedded,
            "activex_parts": activex_parts,
            "ole_relationships": ole_relationships,
            "flags": flags,
            "warnings": warnings,
        }
        (out_dir / "inspection.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = []
        for block in blocks:
            if block["type"] == "paragraph":
                lines.append(block["text"])
            else:
                lines.extend(" | ".join(row) for row in block["rows"])
        (out_dir / "body.txt").write_text("\n".join(lines), encoding="utf-8")
        (out_dir / "tables.json").write_text(json.dumps(tables, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def main():
    parser = argparse.ArgumentParser(description="只读解析 DOCX 的正文、表格、图片、链接、字段、修订、批注、页眉页脚和属性")
    parser.add_argument("docx", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--ocr", action="store_true", help="如本机存在 tesseract，则对图片执行可选 OCR")
    parser.add_argument("--ocr-lang", default="chi_sim+eng")
    args = parser.parse_args()
    try:
        data = inspect_docx(args.docx, args.out, args.ocr, args.ocr_lang)
    except (OSError, ValueError, RuntimeError, NotImplementedError, EOFError, zipfile.BadZipFile) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    print(json.dumps({"out": str(args.out), "counts": data["counts"], "flags": data["flags"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
