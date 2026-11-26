#!/usr/bin/env python3
import argparse
import hashlib
import html
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path


ENERGY_LABELS = {
    "pure_ev": "纯电",
    "range_extender": "增程",
    "hybrid": "插混",
    "unknown": "未知",
}

SENSITIVE_REDACTED = "敏感内容已脱敏，待人工核对"
SENSITIVE_CONTEXT = re.compile(
    r"(?:手机(?:号|号码)|联系(?:电话|方式)|身份证(?:号)?|证件(?:号|号码)|邮箱|电子邮箱|"
    r"(?:员工|工作|内部)?工号|内部(?:预售价?|指导价|价格)|未公开价格|非公开价格|"
    r"(?:商业)?机密|保密|敏感(?:内容|信息|数据)|待脱敏|"
    r"(?<![A-Za-z])(?:confidential|sensitive|internal[_\s-]?price)(?![A-Za-z]))",
    re.IGNORECASE,
)
SENSITIVE_FIELD_KEY = re.compile(
    r"(?<![A-Za-z0-9])(?:手机(?:号|号码)|联系(?:电话|方式)|身份证(?:号)?|证件(?:号|号码)|"
    r"邮箱|电子邮箱|(?:员工|工作|内部)?工号|phone(?:[_\s-]?(?:number|no))?|"
    r"mobile(?:[_\s-]?phone)?|e-?mail|email|id[_\s-]?card|employee[_\s-]?id|staff[_\s-]?id)"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)
SENSITIVE_PATTERNS = (
    re.compile(r"(?<!\d)1[3-9]\d(?:[ -]?\d{4}){2}(?!\d)"),
    re.compile(r"(?<!\d)(?:\d{17}[\dXx]|\d{15})(?!\d)"),
    re.compile(r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])", re.IGNORECASE),
    re.compile(r"(?<![A-Z0-9])(?:TEST|EMP|STAFF|WORKER|INTERNAL|INT)[-_]?\d{3,}(?![A-Z0-9])", re.IGNORECASE),
)


def redact_text(value, context=""):
    text = str(value)
    if (
        SENSITIVE_CONTEXT.search(context)
        or SENSITIVE_CONTEXT.search(text)
        or SENSITIVE_FIELD_KEY.search(context)
    ):
        return SENSITIVE_REDACTED
    for pattern in SENSITIVE_PATTERNS:
        text = pattern.sub(SENSITIVE_REDACTED, text)
    return text


def redact_untrusted(value, context=""):
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            original_key = str(key)
            safe_key = redact_text(original_key, original_key)
            base_key = safe_key
            suffix = 2
            while safe_key in redacted:
                safe_key = f"{base_key} [{suffix}]"
                suffix += 1
            child_context = f"{context}.{original_key}" if context else original_key
            redacted[safe_key] = SENSITIVE_REDACTED if safe_key != original_key else redact_untrusted(item, child_context)
        return redacted
    if isinstance(value, list):
        return [redact_untrusted(item, context) for item in value]
    if isinstance(value, str):
        return redact_text(value, context)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        redacted = redact_text(value, context)
        return value if redacted == str(value) else redacted
    return value


def energy_type(raw):
    text = raw or ""
    if "纯电" in text:
        return "pure_ev"
    if "增程" in text:
        return "range_extender"
    if "插电" in text or "插混" in text:
        return "hybrid"
    return "unknown"


def param_text(value):
    return "" if value is None else str(value).strip()


def selection_details(param):
    details = []
    for sub in param.get("sublist") or []:
        name = param_text(sub.get("name"))
        value = param_text(sub.get("value"))
        if name or value:
            details.append({"name": name, "value": value})
    return details


def parameter_value(param, include_markers=True):
    parts = []
    itemname = param_text(param.get("itemname"))
    if itemname:
        parts.append(itemname)
    for detail in selection_details(param):
        if not detail["name"]:
            continue
        value = detail["name"] + (detail["value"] if include_markers else "")
        if value:
            parts.append(value)
    return " / ".join(parts) if parts else None


def parameter_markers(param):
    markers = []
    itemname = param_text(param.get("itemname"))
    if itemname in {"●", "○"}:
        markers.append(itemname)
    markers.extend(
        detail["value"]
        for detail in selection_details(param)
        if detail["value"]
    )
    return markers


def marker_note(markers):
    unique = list(dict.fromkeys(marker for marker in markers if marker))
    if not unique:
        return ""
    if unique == ["●"]:
        return "（页面标配标记：●）"
    if unique == ["○"]:
        return "（页面选配/可选标记：○，待核）"
    if set(unique) <= {"●", "○"}:
        return "（页面含标配/选配混合标记：" + "、".join(unique) + "，待核）"
    return "（页面标记含义待核：" + "、".join(unique) + "）"


def markdown_text(value):
    text = "" if value is None else str(value)
    text = "".join(
        " " if char.isspace() else "" if unicodedata.category(char).startswith("C") else char
        for char in text
    )
    text = re.sub(r"\s+", " ", text).strip()
    text = html.escape(text, quote=False)
    text = text.replace("\\", "\\\\").replace("|", "\\|")
    for char in "[]()*_~`#":
        text = text.replace(char, "\\" + char)
    return text


def normalize_auto_media(source_path, out_dir, target_energy=None, source_url=None, fetched_at=None):
    raw_bytes = source_path.read_bytes()
    payload = redact_untrusted(json.loads(raw_bytes.decode("utf-8-sig")))
    if payload.get("returncode") not in {0, "0", None}:
        raise ValueError(f"汽车垂媒接口返回错误：{payload.get('message') or payload.get('returncode')}")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ValueError("JSON 缺少 result 对象")

    id_to_name = {}
    name_to_id = {}
    for group in result.get("titlelist", []):
        for item in group.get("items", []):
            titleid = item.get("titleid")
            name = item.get("itemname")
            if titleid is not None and name:
                id_to_name[titleid] = name
                name_to_id[name] = titleid

    def param_by_name(params, name):
        titleid = name_to_id.get(name)
        if titleid is None:
            return {}
        return next((item for item in params if item.get("titleid") == titleid), {})

    def value_by_name(params, name):
        return parameter_value(param_by_name(params, name), include_markers=False)

    bread = result.get("bread") or {}
    input_file = redact_text(Path(str(source_path).replace("\\", "/")).name)
    normalized_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    source = {
        "source_type": "其他（汽车垂媒公开参数页，非官方）",
        "source_type_code": "public_media_config_page",
        "source_name": "汽车垂媒公开参数页/同源参数接口",
        "source_url": redact_text(source_url or "未提供，待补"),
        "source_date": "页面发布日期未提供，待补",
        "capture_time": redact_text(fetched_at or "未提供，待补"),
        "verification_status": "用户提供，未独立核验",
        "material_version": "页面当前快照；版本号未提供，待补",
        "disclaimer": "页面参数为公开媒体信息，不能替代厂商正式配置表；未列出不等于没有。",
        "input_file": input_file,
        "input_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "normalized_at": normalized_at,
    }

    fields = [
        ("车型名称", "参数", "车型名称"),
        ("厂商指导价(元)", "价格", "厂商指导价"),
        ("能源类型", "参数", "能源类型"),
        ("上市时间", "上市状态", "上市时间"),
        ("长*宽*高(mm)", "参数", "长×宽×高"),
        ("轴距(mm)", "参数", "轴距"),
        ("系统综合功率(kW)", "参数", "系统综合功率"),
        ("电动机总功率(kW)", "参数", "电动机总功率"),
        ("电池类型", "参数", "电池类型"),
        ("电池能量(kWh)", "参数", "电池能量"),
        ("CLTC纯电续航里程(km)", "参数", "CLTC 纯电续航"),
        ("WLTC纯电续航里程(km)", "参数", "WLTC 纯电续航"),
        ("电池快充时间(分钟)", "参数", "电池快充时间"),
        ("空气悬架", "参数", "空气悬架"),
        ("空气悬挂类型", "参数", "空气悬挂类型"),
        ("整体主动转向系统", "参数", "整体主动转向"),
        ("激光雷达品牌", "参数", "激光雷达品牌"),
        ("激光雷达数量", "参数", "激光雷达数量"),
        ("激光雷达线数", "参数", "激光雷达线数"),
        ("辅助驾驶系统", "智驾与安全", "辅助驾驶系统"),
        ("辅助驾驶等级", "智驾与安全", "辅助驾驶等级"),
        ("辅助驾驶路段", "智驾与安全", "辅助驾驶路段"),
        ("车载智能系统", "参数", "车载智能系统"),
        ("中控屏幕尺寸", "参数", "中控屏幕尺寸"),
        ("中控屏幕分辨率", "参数", "中控屏幕分辨率"),
        ("AR-HUD增强现实抬头显示", "参数", "AR-HUD"),
        ("HUD抬头尺寸", "参数", "HUD 尺寸"),
        ("后排液晶屏幕", "参数", "后排液晶屏幕"),
        ("后排液晶屏幕尺寸", "参数", "后排屏幕尺寸"),
        ("后排投影系统", "参数", "后排投影系统"),
        ("座椅布局", "参数", "座椅布局"),
        ("零重力座椅", "参数", "零重力座椅"),
        ("扬声器品牌名称", "参数", "扬声器品牌"),
        ("扬声器数量", "参数", "扬声器数量"),
    ]

    trims = []
    facts = []
    fact_energies = []
    for index, item in enumerate(result.get("datalist", []), 1):
        params = item.get("paramconflist") or []
        parameter_map = {name: value_by_name(params, name) for name, _, _ in fields}
        display_parameter_map = {
            name: parameter_value(param_by_name(params, name))
            for name, _, _ in fields
        }
        details_map = {
            name: selection_details(param_by_name(params, name))
            for name, _, _ in fields
        }
        marker_map = {name: parameter_markers(param_by_name(params, name)) for name, _, _ in fields}
        raw_energy = parameter_map.get("能源类型") or next((value for value in item.get("condition", []) if "增程" in value or "纯电" in value or "插电" in value), "")
        trim_energy = energy_type(raw_energy)
        trim = {
            "specid": item.get("specid"),
            "specname": item.get("specname"),
            "year": item.get("year"),
            "price": item.get("minprice") or parameter_map.get("厂商指导价(元)"),
            "energy_type": trim_energy,
            "energy_type_raw": raw_energy,
            "launch_date": parameter_map.get("上市时间"),
            "dimensions": parameter_map.get("长*宽*高(mm)"),
            "wheelbase": parameter_map.get("轴距(mm)"),
            "system_power_kw": parameter_map.get("系统综合功率(kW)"),
            "motor_power_kw": parameter_map.get("电动机总功率(kW)"),
            "battery_type": parameter_map.get("电池类型"),
            "battery_capacity_kwh": parameter_map.get("电池能量(kWh)"),
            "cltc_range_km": parameter_map.get("CLTC纯电续航里程(km)"),
            "wltc_range_km": parameter_map.get("WLTC纯电续航里程(km)"),
            "fast_charge_time_min": parameter_map.get("电池快充时间(分钟)"),
            "air_suspension": parameter_map.get("空气悬架"),
            "air_suspension_type": parameter_map.get("空气悬挂类型"),
            "active_steering": parameter_map.get("整体主动转向系统"),
            "lidar_brand": parameter_map.get("激光雷达品牌"),
            "lidar_count": parameter_map.get("激光雷达数量"),
            "lidar_lines": parameter_map.get("激光雷达线数"),
            "driving_assistance_system": parameter_map.get("辅助驾驶系统"),
            "driving_assistance_level": parameter_map.get("辅助驾驶等级"),
            "driving_assistance_roads": parameter_map.get("辅助驾驶路段"),
            "cockpit_system": parameter_map.get("车载智能系统"),
            "center_screen": parameter_map.get("中控屏幕尺寸"),
            "center_screen_resolution": parameter_map.get("中控屏幕分辨率"),
            "ar_hud": parameter_map.get("AR-HUD增强现实抬头显示"),
            "hud_size": parameter_map.get("HUD抬头尺寸"),
            "rear_screen": parameter_map.get("后排液晶屏幕"),
            "rear_screen_size": parameter_map.get("后排液晶屏幕尺寸"),
            "rear_projection": parameter_map.get("后排投影系统"),
            "seat_layout": parameter_map.get("座椅布局"),
            "zero_gravity_seats": parameter_map.get("零重力座椅"),
            "speaker_brand": parameter_map.get("扬声器品牌名称"),
            "speaker_count": parameter_map.get("扬声器数量"),
            "specstatus_raw": item.get("specstatus"),
            "condition": item.get("condition") or [],
            "parameters": display_parameter_map,
            "selection_markers": marker_map,
            "selection_details": details_map,
        }
        trims.append(trim)
        for name, field_type, label in fields:
            value = display_parameter_map.get(name)
            if value is None:
                continue
            titleid = name_to_id.get(name)
            facts.append({
                "事实编号": f"F{len(facts) + 1:03d}",
                "字段类型": field_type,
                "官方原文或脱敏摘录": f"{label}：{value}{marker_note(marker_map.get(name, []))}",
                "来源类型": source["source_type"],
                "证据定位": f"{source['input_file']} → result.datalist[{index - 1}].paramconflist[titleid={titleid}]",
                "来源名称与日期": f"{source['source_name']}；页面发布日期未提供，待补；读取时间：{source['capture_time']}",
                "材料版本/有效期": source["material_version"],
                "核验状态": source["verification_status"],
                "适用范围": f"{bread.get('brandname', '')} {bread.get('seriesname', '')} {item.get('specname', '')}；能源类型：{ENERGY_LABELS.get(trim_energy, trim_energy)}；市场未提供，待补",
                "限定条件": "仅限该车型、该能源类型、该版型；页面为公开媒体来源；标记含义与选装范围待厂商配置表复核",
                "当前状态": f"页面状态码 {item.get('specstatus')}，含义待核；上市时间字段为 {parameter_map.get('上市时间') or '未列出'}",
                "备注": "不得跨能源类型或跨版型外推；未列出不等于没有",
            })
            fact_energies.append(trim_energy)

    available = []
    for trim in trims:
        if trim["energy_type"] not in available:
            available.append(trim["energy_type"])
    known_available = [value for value in available if value != "unknown"]
    if target_energy == "unknown":
        gate = {
            "status": "blocked",
            "target_energy": target_energy,
            "target_energy_label": ENERGY_LABELS[target_energy],
            "available_energy_types": available,
            "issue_type": "适用范围不明",
            "risk_level": "高",
            "reason": "适用范围不明：待检目标能源类型为 unknown，不得判定能源类型匹配。",
            "limits": ["先明确目标版型的能源类型", "unknown 版型不可用于取证", "页面未列出不等于该配置不存在"],
        }
    elif target_energy:
        target_label = ENERGY_LABELS.get(target_energy, target_energy)
        if target_energy not in known_available:
            if known_available:
                gate = {
                    "status": "blocked",
                    "target_energy": target_energy,
                    "target_energy_label": target_label,
                    "available_energy_types": available,
                    "issue_type": "版型错配",
                    "risk_level": "高",
                    "reason": f"跨能源类型：待检目标为 {target_label}，来源仅含 {'、'.join(ENERGY_LABELS.get(value, value) for value in available)}；不得跨能源类型取证。",
                    "limits": ["增程版参数不得证明纯电版，反之亦然", "高配参数不得外推全系", "页面未列出不等于该配置不存在"],
                }
            else:
                gate = {
                    "status": "blocked",
                    "target_energy": target_energy,
                    "target_energy_label": target_label,
                    "available_energy_types": available,
                    "issue_type": "适用范围不明",
                    "risk_level": "高",
                    "reason": f"适用范围不明：来源仅含未知能源类型（unknown），不能判定目标为 {target_label}；不得跨能源类型取证。",
                    "limits": ["先核实来源版型的能源类型", "unknown 版型不可用于取证", "页面未列出不等于该配置不存在"],
                }
        else:
            limits = ["非官方来源不能替代厂商正式配置表", "必须继续核对具体版型、市场和日期", "未列出不等于没有"]
            reason = "能源类型匹配，但仍须核对具体版型、市场、日期和官方配置表。"
            if "unknown" in available:
                limits.insert(0, f"unknown 版型能源类型不明，不可用于 {target_label} 取证")
                reason = f"仅能源类型已知的 {target_label} 版型可用于限定核查；unknown 版型不可用，仍须核对具体版型、市场、日期和官方配置表。"
            gate = {
                "status": "allowed_with_limits",
                "target_energy": target_energy,
                "target_energy_label": target_label,
                "available_energy_types": available,
                "issue_type": "证据待补",
                "risk_level": "高",
                "reason": reason,
                "limits": limits,
            }
    else:
        gate = {
            "status": "not_checked",
            "target_energy": None,
            "available_energy_types": available,
            "issue_type": "待明确目标版型",
            "risk_level": "高",
            "reason": "未提供待检稿能源类型，不能执行版型硬门。",
            "limits": ["先明确车系、能源类型、版型、市场和日期"],
        }

    if target_energy is None:
        for trim in trims:
            trim["usable_for_target"] = None
        usable_trims = trims
    else:
        for trim in trims:
            trim["usable_for_target"] = target_energy != "unknown" and trim["energy_type"] == target_energy
        usable_trims = [trim for trim in trims if trim["usable_for_target"]]
        facts = [fact for fact, energy in zip(facts, fact_energies) if target_energy != "unknown" and energy == target_energy]

    issue_types_path = Path(__file__).resolve().parents[1] / "references" / "issue_types.json"
    issue_types = set(json.loads(issue_types_path.read_text(encoding="utf-8")))
    if gate["issue_type"] not in issue_types:
        raise ValueError(f"问题类型不在白名单：{gate['issue_type']}")

    normalized = {
        "series": {
            "brandid": bread.get("brandid"),
            "brandname": bread.get("brandname"),
            "seriesid": bread.get("seriesid"),
            "seriesname": bread.get("seriesname"),
        },
        "source": source,
        "available_energy_types": available,
        "trim_count": len(trims),
        "trims": trims,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "normalized.json").write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "facts.json").write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "gate.json").write_text(json.dumps(gate, ensure_ascii=False, indent=2), encoding="utf-8")
    issues = [{
        "问题类型": gate["issue_type"],
        "内容风险": gate["risk_level"],
        "证据充分度": "不足",
        "说明": gate["reason"],
        "处置": "补厂商官方配置表；明确车系、能源类型、版型、市场、日期和选装/标配关系。",
    }]
    (out_dir / "issues.json").write_text(json.dumps(issues, ensure_ascii=False, indent=2), encoding="utf-8")

    columns = [
        ("车型", lambda trim: trim["specname"]),
        ("能源类型", lambda trim: ENERGY_LABELS.get(trim["energy_type"], trim["energy_type"])),
        ("厂商指导价", lambda trim: trim["price"]),
        ("上市时间", lambda trim: trim["launch_date"]),
        ("长×宽×高(mm)", lambda trim: trim["dimensions"]),
        ("轴距(mm)", lambda trim: trim["wheelbase"]),
        ("系统综合功率(kW)", lambda trim: trim["system_power_kw"]),
        ("电池类型", lambda trim: trim["battery_type"]),
        ("空气悬架", lambda trim: trim["air_suspension_type"] or trim["air_suspension"]),
        ("整体主动转向", lambda trim: trim["active_steering"]),
        ("激光雷达", lambda trim: trim["lidar_count"]),
        ("激光雷达线数", lambda trim: trim["lidar_lines"]),
        ("辅助驾驶", lambda trim: " / ".join(value for value in [trim["driving_assistance_system"], trim["driving_assistance_level"]] if value)),
        ("座舱", lambda trim: trim["cockpit_system"]),
        ("后排屏", lambda trim: trim["rear_screen_size"]),
        ("后排投影", lambda trim: trim["rear_projection"]),
    ]
    lines = [
        f"# {markdown_text(bread.get('brandname', ''))} {markdown_text(bread.get('seriesname', ''))} 版型 × 能源类型矩阵",
        "",
        f"来源类型：{markdown_text(source['source_type'])}；核验状态：{markdown_text(source['verification_status'])}；读取时间：{markdown_text(source['capture_time'])}。",
        "",
        "| " + " | ".join(name for name, _ in columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for trim in usable_trims:
        lines.append("| " + " | ".join(
            markdown_text(getter(trim) if getter(trim) is not None else "未列出")
            for _, getter in columns
        ) + " |")
    lines.extend([
        "",
        "## 硬门结论",
        "",
        f"- 状态：{markdown_text(gate['status'])}",
        f"- 目标能源类型：{markdown_text(gate.get('target_energy_label') or '未提供')}",
        f"- 来源能源类型：{markdown_text('、'.join(ENERGY_LABELS.get(value, value) for value in available))}",
        f"- 说明：{markdown_text(gate['reason'])}",
        "",
    ])
    (out_dir / "matrix.md").write_text("\n".join(lines), encoding="utf-8")
    return normalized, facts, gate


def main():
    parser = argparse.ArgumentParser(description="规范化汽车垂媒 getParamConf JSON，输出版型矩阵、事实卡与能源类型硬门")
    parser.add_argument("json_path", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--target-energy", choices=list(ENERGY_LABELS))
    parser.add_argument("--source-url")
    parser.add_argument("--fetched-at")
    args = parser.parse_args()
    try:
        normalized, facts, gate = normalize_auto_media(args.json_path, args.out, args.target_energy, args.source_url, args.fetched_at)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, json.JSONDecodeError) as exc:
        print(f"错误：{redact_text(str(exc))}", file=sys.stderr)
        return 2
    print(json.dumps({"out": redact_text(str(args.out)), "series": normalized["series"], "trims": normalized["trim_count"], "available_energy_types": normalized["available_energy_types"], "gate": gate["status"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
