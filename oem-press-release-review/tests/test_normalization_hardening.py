import codecs
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from test_adapters import autohome_sample, run_script


REDACTED = "敏感内容已脱敏，待人工核对"
FACT_COLUMNS = [
    "事实编号", "字段类型", "官方原文或脱敏摘录", "来源类型", "证据定位",
    "来源名称与日期", "材料版本/有效期", "核验状态", "适用范围", "限定条件",
    "当前状态", "备注",
]


def find_param(payload, name, trim_index=0):
    titleid = next(
        item["titleid"]
        for group in payload["result"]["titlelist"]
        for item in group["items"]
        if item["itemname"] == name
    )
    return next(
        item
        for item in payload["result"]["datalist"][trim_index]["paramconflist"]
        if item["titleid"] == titleid
    )


def set_param(payload, name, itemname="", sublist=None, trim_index=0):
    param = find_param(payload, name, trim_index)
    param["itemname"] = itemname
    param["sublist"] = sublist or []
    return param


def table_cells(line):
    content = line.strip()[1:-1]
    cells = []
    current = []
    escaped = False
    for char in content:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            current.append(char)
            escaped = True
        elif char == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    cells.append("".join(current).strip())
    return cells


class NormalizationHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def normalize(self, payload, target_energy="range_extender", source_relative=None, bom=False,
                  source_url=None, fetched_at=None):
        source = self.root / (source_relative or Path("params.json"))
        source.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if bom:
            raw = codecs.BOM_UTF8 + raw
        source.write_bytes(raw)
        out = self.root / "out"
        args = [
            "normalize_autohome_config.py", source, "--out", out,
            "--target-energy", target_energy,
        ]
        if source_url is not None:
            args.extend(["--source-url", source_url])
        if fetched_at is not None:
            args.extend(["--fetched-at", fetched_at])
        result = run_script(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        normalized = json.loads((out / "normalized.json").read_text(encoding="utf-8"))
        facts = json.loads((out / "facts.json").read_text(encoding="utf-8"))
        gate = json.loads((out / "gate.json").read_text(encoding="utf-8"))
        matrix = (out / "matrix.md").read_text(encoding="utf-8")
        return source, raw, normalized, facts, gate, matrix

    def test_unknown_target_is_blocked_as_unclear_scope(self):
        payload = autohome_sample()
        for index in range(2):
            set_param(payload, "能源类型", "未知", trim_index=index)
        _, _, _, _, gate, _ = self.normalize(payload, target_energy="unknown")
        self.assertEqual(gate["status"], "blocked")
        self.assertEqual(gate["issue_type"], "适用范围不明")
        self.assertEqual(gate["risk_level"], "高")
        self.assertIn("适用范围不明", gate["reason"])

    def test_known_target_cannot_match_unknown_only_source(self):
        payload = autohome_sample()
        for index in range(2):
            set_param(payload, "能源类型", "未知", trim_index=index)
        _, _, _, _, gate, _ = self.normalize(payload, target_energy="pure_ev")
        self.assertEqual(gate["status"], "blocked")
        self.assertEqual(gate["issue_type"], "适用范围不明")
        self.assertEqual(gate["risk_level"], "高")
        self.assertIn("未知", gate["reason"])

    def test_mixed_source_allows_known_target_but_excludes_unknown_trim(self):
        payload = autohome_sample()
        set_param(payload, "能源类型", "纯电", trim_index=0)
        set_param(payload, "能源类型", "未知", trim_index=1)
        _, _, normalized, facts, gate, matrix = self.normalize(payload, target_energy="pure_ev")
        self.assertEqual(gate["status"], "allowed_with_limits")
        self.assertEqual(normalized["available_energy_types"], ["pure_ev", "unknown"])
        self.assertEqual(
            [trim["usable_for_target"] for trim in normalized["trims"]],
            [True, False],
        )
        known_name = payload["result"]["datalist"][0]["specname"]
        unknown_name = payload["result"]["datalist"][1]["specname"]
        self.assertTrue(any(known_name in fact["适用范围"] for fact in facts))
        self.assertFalse(any(unknown_name in fact["适用范围"] for fact in facts))
        self.assertIn(known_name, matrix)
        self.assertNotIn(unknown_name, matrix)
        self.assertTrue(any(
            "unknown" in limit and ("不可用" in limit or "不得" in limit)
            for limit in gate["limits"]
        ))

    def test_cross_energy_mismatch_marks_all_trims_unusable_and_emits_no_facts(self):
        payload = autohome_sample()
        _, _, normalized, facts, gate, matrix = self.normalize(payload, target_energy="pure_ev")
        self.assertEqual(gate["status"], "blocked")
        self.assertEqual(
            [trim["usable_for_target"] for trim in normalized["trims"]],
            [False, False],
        )
        self.assertEqual(facts, [])
        for trim in payload["result"]["datalist"]:
            self.assertNotIn(trim["specname"], matrix)

    def test_synthetic_sensitive_sentinels_are_redacted_before_all_outputs(self):
        payload = autohome_sample()
        sentinels = {
            "phone": "13800001111",
            "id_card": "110101199001011234",
            "email": "privacy-test@example.com",
            "employee_id": "TEST-0001",
            "internal_price": "18.88万",
        }
        payload["result"]["bread"]["brandname"] = f"品牌{sentinels['phone']}"
        payload["result"]["bread"]["seriesname"] = f"车系{sentinels['id_card']}"
        first = payload["result"]["datalist"][0]
        first["specname"] = f"2026款 {sentinels['email']}"
        first["minprice"] = f"内部预售价 {sentinels['internal_price']}"
        set_param(payload, "厂商指导价(元)", f"内部预售价 {sentinels['internal_price']}")
        set_param(payload, "座椅布局", sentinels["employee_id"])
        first["condition"].append("商业机密：未公开价格 99.99万")

        source, raw, normalized, facts, gate, matrix = self.normalize(payload)
        self.assertEqual(normalized["source"]["input_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(normalized["source"]["input_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        for output in (normalized, facts, gate, matrix):
            serialized = json.dumps(output, ensure_ascii=False) if not isinstance(output, str) else output
            for sentinel in sentinels.values():
                self.assertNotIn(sentinel, serialized)
        for output in (normalized, facts, matrix):
            serialized = json.dumps(output, ensure_ascii=False) if not isinstance(output, str) else output
            self.assertIn(REDACTED, serialized)

    def test_metadata_and_condition_keys_are_redacted_without_changing_raw_sha(self):
        payload = autohome_sample()
        file_sentinel = "13800002222"
        url_sentinel = "metadata-test@example.com"
        time_sentinel = "18.88万"
        key_sentinel = "99.99万"
        payload["result"]["datalist"][0]["condition"] = {
            "手机号": "ordinary-value",
            f"内部预售价-{time_sentinel}": key_sentinel,
            file_sentinel: "key-only-value",
        }
        source_url = f"https://example.invalid/{file_sentinel}?contact={url_sentinel}"
        fetched_at = f"2026-11-25T10:34:18Z 内部预售价 {time_sentinel}"
        source, raw, normalized, facts, gate, matrix = self.normalize(
            payload,
            source_relative=Path("private") / f"params-{file_sentinel}.json",
            source_url=source_url,
            fetched_at=fetched_at,
        )
        self.assertEqual(normalized["source"]["input_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(normalized["source"]["input_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        for output in (normalized, facts, gate, matrix):
            serialized = json.dumps(output, ensure_ascii=False) if not isinstance(output, str) else output
            for sentinel in (file_sentinel, url_sentinel, time_sentinel, key_sentinel, "手机号"):
                self.assertNotIn(sentinel, serialized)
        condition = normalized["trims"][0]["condition"]
        self.assertEqual(len(condition), 3)
        self.assertTrue(all(key.startswith(REDACTED) for key in condition))
        self.assertTrue(all(value == REDACTED for value in condition.values()))

    def test_normal_phone_related_configuration_is_not_over_redacted(self):
        payload = autohome_sample()
        value = "支持手机互联、smartphone、headphone"
        set_param(payload, "座椅布局", value)
        _, _, normalized, facts, _, _ = self.normalize(payload)
        self.assertEqual(normalized["trims"][0]["parameters"]["座椅布局"], value)
        fact = next(row for row in facts if row["官方原文或脱敏摘录"].startswith("座椅布局："))
        self.assertIn(value, fact["官方原文或脱敏摘录"])
        for output in (normalized, facts):
            serialized = json.dumps(output, ensure_ascii=False) if not isinstance(output, str) else output
            self.assertNotIn(REDACTED, serialized)

    def test_phone_label_is_kept_while_embedded_number_is_removed(self):
        payload = autohome_sample()
        phone = "13900003333"
        set_param(payload, "座椅布局", f"手机：{phone}")
        _, _, normalized, facts, _, matrix = self.normalize(payload)
        value = normalized["trims"][0]["parameters"]["座椅布局"]
        self.assertTrue(value.startswith("手机："))
        self.assertIn(REDACTED, value)
        self.assertNotIn(phone, value)
        for output in (normalized, facts, matrix):
            serialized = json.dumps(output, ensure_ascii=False) if not isinstance(output, str) else output
            self.assertNotIn(phone, serialized)

    def test_source_and_fact_locations_use_basename_only(self):
        relative = Path("Users") / "sentinel-user" / "private" / "params.json"
        source, _, normalized, facts, _, _ = self.normalize(
            autohome_sample(), source_relative=relative,
        )
        self.assertEqual(normalized["source"]["input_file"], source.name)
        self.assertNotIn("sentinel-user", json.dumps(normalized, ensure_ascii=False))
        for fact in facts:
            self.assertTrue(fact["证据定位"].startswith(source.name + " → "))
            self.assertNotIn("sentinel-user", fact["证据定位"])
            self.assertNotIn(str(source), fact["证据定位"])

    def test_single_selection_keeps_ordered_details_and_marker(self):
        payload = autohome_sample()
        set_param(payload, "座椅布局", sublist=[{"name": "A配置", "value": "●"}])
        _, _, normalized, facts, _, _ = self.normalize(payload)
        trim = normalized["trims"][0]
        self.assertEqual(trim["selection_details"]["座椅布局"], [{"name": "A配置", "value": "●"}])
        self.assertEqual(trim["parameters"]["座椅布局"], "A配置●")
        fact = next(row for row in facts if row["官方原文或脱敏摘录"].startswith("座椅布局："))
        self.assertIn("A配置●", fact["官方原文或脱敏摘录"])

    def test_mixed_selection_details_preserve_order_and_per_option_symbols(self):
        payload = autohome_sample()
        details = [
            {"name": "A配置", "value": "●"},
            {"name": "B配置", "value": "○"},
        ]
        set_param(payload, "座椅布局", sublist=details)
        _, _, normalized, _, _, _ = self.normalize(payload)
        trim = normalized["trims"][0]
        self.assertEqual(trim["selection_details"]["座椅布局"], details)
        self.assertEqual(trim["parameters"]["座椅布局"], "A配置● / B配置○")

    def test_unnamed_selection_keeps_marker_without_rendering_empty_item(self):
        payload = autohome_sample()
        details = [
            {"name": "", "value": "●"},
            {"name": "A配置", "value": "○"},
        ]
        set_param(payload, "座椅布局", sublist=details)
        _, _, normalized, facts, _, _ = self.normalize(payload)
        trim = normalized["trims"][0]
        self.assertEqual(trim["selection_details"]["座椅布局"], details)
        self.assertEqual(trim["selection_markers"]["座椅布局"], ["●", "○"])
        self.assertEqual(trim["parameters"]["座椅布局"], "A配置○")
        fact = next(row for row in facts if row["官方原文或脱敏摘录"].startswith("座椅布局："))
        self.assertIn("A配置○", fact["官方原文或脱敏摘录"])
        self.assertNotIn("● /", fact["官方原文或脱敏摘录"])
        self.assertIn("标配", fact["官方原文或脱敏摘录"])
        self.assertIn("选配", fact["官方原文或脱敏摘录"])

    def test_itemname_and_sublist_are_merged_without_losing_markers(self):
        payload = autohome_sample()
        details = [
            {"name": "A配置", "value": "●"},
            {"name": "B配置", "value": "○"},
        ]
        set_param(payload, "座椅布局", "座椅配置", details)
        _, _, normalized, _, _, _ = self.normalize(payload)
        trim = normalized["trims"][0]
        self.assertEqual(trim["selection_details"]["座椅布局"], details)
        self.assertEqual(trim["parameters"]["座椅布局"], "座椅配置 / A配置● / B配置○")

    def test_symbol_itemname_keeps_its_meaning_in_value_and_fact_note(self):
        payload = autohome_sample()
        set_param(payload, "座椅布局", "●", [{"name": "A配置", "value": "○"}])
        _, _, normalized, facts, _, _ = self.normalize(payload)
        trim = normalized["trims"][0]
        self.assertEqual(trim["selection_details"]["座椅布局"], [{"name": "A配置", "value": "○"}])
        self.assertEqual(trim["parameters"]["座椅布局"], "● / A配置○")
        fact = next(row for row in facts if row["官方原文或脱敏摘录"].startswith("座椅布局："))
        self.assertIn("标配", fact["官方原文或脱敏摘录"])
        self.assertIn("选配", fact["官方原文或脱敏摘录"])
        self.assertIn("待核", fact["官方原文或脱敏摘录"])

    def test_specname_injection_cannot_break_markdown_rows_or_headings(self):
        payload = autohome_sample()
        payload["result"]["bread"]["brandname"] = "品牌\n## 品牌注入"
        payload["result"]["bread"]["seriesname"] = "车系\r\n# 系列注入"
        payload["result"]["datalist"][0]["specname"] = "2026款 | 注入\r\n## FAKE HEADING\n第二行\t\x00\x1b"
        _, _, _, _, _, matrix = self.normalize(payload)

        lines = matrix.splitlines()
        headings = [line for line in lines if line.lstrip().startswith("#")]
        self.assertEqual(len(headings), 2)
        self.assertTrue(lines[0].startswith("# "))
        self.assertFalse(any(line.startswith("## FAKE") for line in lines))
        self.assertNotIn("\r", matrix)
        self.assertNotIn("\t", matrix)
        self.assertNotIn("\x00", matrix)
        self.assertNotIn("\x1b", matrix)
        table_lines = [line for line in lines if line.startswith("|")]
        self.assertGreaterEqual(len(table_lines), 4)
        column_counts = [len(table_cells(line)) for line in table_lines]
        self.assertEqual(len(set(column_counts)), 1)
        injected_row = next(line for line in table_lines if "2026款" in line)
        self.assertIn(r"2026款 \| 注入", injected_row)

    def test_inline_markdown_and_html_are_neutralized_in_headings_and_cells(self):
        payload = autohome_sample()
        payload["result"]["bread"]["brandname"] = "品牌 <b>加粗</b> [链接](https://example.invalid)"
        payload["result"]["bread"]["seriesname"] = "车系 **强调** _斜体_ ~~删除~~ `代码`"
        payload["result"]["datalist"][0]["specname"] = (
            "2026款 <img src=x onerror=alert(1)> | ![图片](https://example.invalid/a.png) "
            "[点击](https://example.invalid) **粗体** _斜体_ ~~删除~~ `代码` <https://example.invalid>"
        )
        _, _, _, _, _, matrix = self.normalize(payload)
        self.assertNotIn("<b>", matrix)
        self.assertNotIn("<img", matrix)
        self.assertNotIn("<https://", matrix)
        self.assertNotIn("![", matrix)
        self.assertNotIn("](", matrix)
        self.assertNotIn("**", matrix)
        self.assertNotIn("__", matrix)
        self.assertNotIn("~~", matrix)
        self.assertIn(r"\|", matrix)
        self.assertIn(r"\[链接\]", matrix)
        self.assertIn(r"\*\*强调\*\*", matrix)
        table_lines = [line for line in matrix.splitlines() if line.startswith("|")]
        self.assertEqual(len({len(table_cells(line)) for line in table_lines}), 1)

    def test_fact_field_types_use_template_chinese_enum(self):
        _, _, _, facts, _, _ = self.normalize(autohome_sample())
        expected = {
            "车型名称": "参数",
            "厂商指导价": "价格",
            "能源类型": "参数",
            "上市时间": "上市状态",
            "辅助驾驶系统": "智驾与安全",
            "辅助驾驶等级": "智驾与安全",
        }
        for label, field_type in expected.items():
            fact = next(row for row in facts if row["官方原文或脱敏摘录"].startswith(label + "："))
            self.assertEqual(fact["字段类型"], field_type)
        allowed = {"参数", "价格", "上市状态", "智驾与安全"}
        self.assertTrue(all(row["字段类型"] in allowed for row in facts))

    def test_fact_cards_keep_fixed_twelve_columns(self):
        _, _, _, facts, _, _ = self.normalize(autohome_sample())
        self.assertTrue(facts)
        for fact in facts:
            self.assertEqual(list(fact), FACT_COLUMNS)

    def test_utf8_bom_json_is_accepted_without_changing_raw_sha(self):
        source, raw, normalized, _, _, _ = self.normalize(autohome_sample(), bom=True)
        self.assertTrue(source.read_bytes().startswith(codecs.BOM_UTF8))
        self.assertEqual(normalized["source"]["input_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(normalized["trim_count"], 2)

    def test_cli_stdout_redacts_sensitive_values_in_absolute_output_path(self):
        source = self.root / "params.json"
        source.write_text(json.dumps(autohome_sample(), ensure_ascii=False), encoding="utf-8")
        sentinel = "13800004444"
        out = self.root / f"out-{sentinel}"
        result = run_script(
            "normalize_autohome_config.py", source, "--out", out,
            "--target-energy", "range_extender",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(sentinel, result.stdout)
        self.assertIn(REDACTED, result.stdout)
        self.assertEqual(json.loads(result.stdout)["trims"], 2)

    def test_cli_error_redacts_sensitive_values_before_stderr_serialization(self):
        sentinel = "13800005555"
        source = self.root / f"missing-{sentinel}.json"
        result = run_script("normalize_autohome_config.py", source, "--out", self.root / "out")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(sentinel, result.stderr)
        self.assertIn(REDACTED, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_malformed_json_error_is_short_and_has_no_traceback(self):
        source = self.root / "bad.json"
        source.write_bytes(b'{"returncode":')
        result = run_script("normalize_autohome_config.py", source, "--out", self.root / "out")
        self.assertEqual(result.returncode, 2)
        self.assertTrue(result.stderr.startswith("错误："))
        self.assertLess(len(result.stderr), 300)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
