"""契约解析 / 兼容性判定 / CSV 批次校验 / 契约-批次存储 单元测试（仅标准库）：
python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.contract import (ContractError, check_compatible, parse_contract,
                          validate_csv)
from app.store import ContractStore


def make_contract(version="1.0.0", **columns):
    """columns: name -> 覆盖项（dict），缺省为可空字符串列。"""
    cols = []
    for name, spec in columns.items():
        cols.append({"name": name, "type": "string", "required": False, **spec})
    return parse_contract({"version": version, "columns": cols})


BASE_CONTRACT = {
    "version": "1.0.0",
    "columns": [
        {"name": "obs_id", "type": "string", "required": True, "unique": True,
         "pattern": "[A-Z]{2}-\\d{6}"},
        {"name": "station", "type": "string", "required": True,
         "allowed": ["ST-01", "ST-02"]},
        {"name": "utc_hour", "type": "utc_hour", "required": True},
        {"name": "reading", "type": "number", "required": True, "min": -40, "max": 50},
        {"name": "unit", "type": "string", "required": True, "allowed": ["°C"]},
    ],
}


def base_contract(**overrides):
    data = json.loads(json.dumps(BASE_CONTRACT))
    data.update(overrides)
    return data


class TestParseContract(unittest.TestCase):
    def test_valid(self):
        c = parse_contract(BASE_CONTRACT)
        self.assertEqual(c.version, "1.0.0")
        self.assertEqual([col.name for col in c.columns],
                         ["obs_id", "station", "utc_hour", "reading", "unit"])
        self.assertTrue(c.column_map()["obs_id"].unique)
        self.assertEqual(c.column_map()["reading"].max, 50)

    def test_bad_version(self):
        with self.assertRaises(ContractError):
            parse_contract({"columns": [{"name": "a", "type": "string"}]})
        with self.assertRaises(ContractError):
            parse_contract({"version": "", "columns": [{"name": "a", "type": "string"}]})
        with self.assertRaises(ContractError):
            parse_contract({"version": 1, "columns": [{"name": "a", "type": "string"}]})

    def test_bad_structure(self):
        with self.assertRaises(ContractError):
            parse_contract([1, 2])
        with self.assertRaises(ContractError):
            parse_contract({"version": "1", "columns": []})
        with self.assertRaises(ContractError):  # 列名重复
            parse_contract({"version": "1", "columns": [
                {"name": "a", "type": "string"}, {"name": "a", "type": "string"}]})
        with self.assertRaises(ContractError):  # 未知类型
            parse_contract({"version": "1", "columns": [{"name": "a", "type": "text"}]})
        with self.assertRaises(ContractError):  # 类型不支持的约束键
            parse_contract({"version": "1", "columns": [
                {"name": "a", "type": "number", "allowed": ["x"]}]})
        with self.assertRaises(ContractError):  # 未知键
            parse_contract({"version": "1", "columns": [
                {"name": "a", "type": "string", "min_length": 3}]})

    def test_bad_constraints(self):
        with self.assertRaises(ContractError):  # 非法正则
            parse_contract({"version": "1", "columns": [
                {"name": "a", "type": "string", "pattern": "(["}]})
        with self.assertRaises(ContractError):  # min > max
            parse_contract({"version": "1", "columns": [
                {"name": "a", "type": "number", "min": 5, "max": 1}]})
        with self.assertRaises(ContractError):  # allowed 为空数组
            parse_contract({"version": "1", "columns": [
                {"name": "a", "type": "string", "allowed": []}]})
        with self.assertRaises(ContractError):  # max_length 非整数
            parse_contract({"version": "1", "columns": [
                {"name": "a", "type": "string", "max_length": 1.5}]})
        with self.assertRaises(ContractError):  # integer 的 min 非整数
            parse_contract({"version": "1", "columns": [
                {"name": "a", "type": "integer", "min": 0.5}]})
        with self.assertRaises(ContractError):  # required 非布尔
            parse_contract({"version": "1", "columns": [
                {"name": "a", "type": "string", "required": "yes"}]})


class TestCompatibility(unittest.TestCase):
    def test_identical_rejected_version_unchanged(self):
        old = parse_contract(BASE_CONTRACT)
        new = parse_contract(BASE_CONTRACT)
        self.assertTrue(any("版本号" in p for p in check_compatible(old, new)))

    def test_version_regression_rejected(self):
        old = make_contract("2.0.0", a={})
        new = make_contract("1.9.9", a={})
        self.assertTrue(any("回退" in p for p in check_compatible(old, new)))
        # 数字点分按数值比较：2.0.0 < 10.0.0
        self.assertEqual(check_compatible(make_contract("2.0.0", a={}),
                                          make_contract("10.0.0", a={})), [])

    def test_add_optional_column_ok(self):
        old = parse_contract(BASE_CONTRACT)
        data = base_contract(version="2.0.0")
        data["columns"].append({"name": "qc_flag", "type": "string",
                                "required": False, "allowed": ["PASS"]})
        self.assertEqual(check_compatible(old, parse_contract(data)), [])

    def test_add_required_column_rejected(self):
        old = parse_contract(BASE_CONTRACT)
        data = base_contract(version="2.0.0")
        data["columns"].append({"name": "operator", "type": "string", "required": True})
        problems = check_compatible(old, parse_contract(data))
        self.assertTrue(any("新增必填列" in p for p in problems))

    def test_delete_column_rejected(self):
        old = parse_contract(BASE_CONTRACT)
        data = base_contract(version="2.0.0")
        data["columns"] = [c for c in data["columns"] if c["name"] != "unit"]
        problems = check_compatible(old, parse_contract(data))
        self.assertTrue(any("删除列 unit" in p for p in problems))

    def test_type_change_rejected(self):
        old = parse_contract(BASE_CONTRACT)
        data = base_contract(version="2.0.0")
        next(c for c in data["columns"] if c["name"] == "reading")["type"] = "integer"
        problems = check_compatible(old, parse_contract(data))
        self.assertTrue(any("类型变更" in p for p in problems))

    def test_required_relax_ok_tighten_rejected(self):
        old = make_contract("1.0.0", a={"required": True}, b={"required": False})
        new = make_contract("2.0.0", a={"required": False}, b={"required": True})
        problems = check_compatible(old, new)
        self.assertEqual(len(problems), 1)
        self.assertIn("可空转必填", problems[0])

    def test_range_relax_and_tighten(self):
        old = make_contract("1.0.0", r={"type": "number", "min": 0, "max": 10})
        # 放宽：下限下调 + 上限上调 + 删除上限
        ok = make_contract("2.0.0", r={"type": "number", "min": -5, "max": 99})
        self.assertEqual(check_compatible(old, ok), [])
        ok2 = make_contract("3.0.0", r={"type": "number", "min": -5})
        self.assertEqual(check_compatible(ok, ok2), [])
        # 收紧：下限上调 / 上限下调 / 新增约束
        bad1 = make_contract("2.0.0", r={"type": "number", "min": 1, "max": 10})
        bad2 = make_contract("2.0.0", r={"type": "number", "min": 0, "max": 9})
        bad3 = make_contract("2.0.0", r={"type": "number", "min": 0, "max": 10})
        free = make_contract("1.0.0", r={"type": "number"})
        self.assertTrue(any("下限上调" in p for p in check_compatible(old, bad1)))
        self.assertTrue(any("上限下调" in p for p in check_compatible(old, bad2)))
        self.assertTrue(any("新增下限" in p or "新增上限" in p
                            for p in check_compatible(free, bad3)))

    def test_allowed_relax_and_tighten(self):
        old = make_contract("1.0.0", u={"allowed": ["a", "b"]})
        self.assertEqual(check_compatible(old, make_contract("2.0.0", u={"allowed": ["a", "b", "c"]})), [])
        self.assertEqual(check_compatible(old, make_contract("2.0.0", u={})), [])  # 删除枚举
        problems = check_compatible(old, make_contract("2.0.0", u={"allowed": ["a"]}))
        self.assertTrue(any("枚举取值减少" in p for p in problems))
        free = make_contract("1.0.0", u={})
        problems = check_compatible(free, make_contract("2.0.0", u={"allowed": ["a"]}))
        self.assertTrue(any("新增枚举约束" in p for p in problems))

    def test_pattern_and_max_length(self):
        old = make_contract("1.0.0", s={"pattern": "a+", "max_length": 5})
        self.assertEqual(check_compatible(old, make_contract("2.0.0", s={"max_length": 9})), [])
        problems = check_compatible(old, make_contract("2.0.0", s={"pattern": "b+", "max_length": 5}))
        self.assertTrue(any("模式变更" in p for p in problems))
        problems = check_compatible(old, make_contract("2.0.0", s={"pattern": "a+", "max_length": 4}))
        self.assertTrue(any("长度上限收紧" in p for p in problems))
        free = make_contract("1.0.0", s={})
        problems = check_compatible(free, make_contract("2.0.0", s={"pattern": "a+"}))
        self.assertTrue(any("新增模式约束" in p for p in problems))

    def test_unique_relax_ok_tighten_rejected(self):
        old = make_contract("1.0.0", a={"unique": True}, b={})
        new = make_contract("2.0.0", a={}, b={"unique": True})
        problems = check_compatible(old, new)
        self.assertEqual(len(problems), 1)
        self.assertIn("唯一性约束", problems[0])


class TestValidateCsv(unittest.TestCase):
    def setUp(self):
        self.contract = parse_contract(BASE_CONTRACT)

    def test_all_pass(self):
        text = ("obs_id,station,utc_hour,reading,unit\n"
                "OB-000001,ST-01,2026-11-15T00:00:00Z,21.5,°C\n"
                "OB-000002,ST-02,2026-11-15 01:00:00,-3,°C\n")
        r = validate_csv(self.contract, text)
        self.assertTrue(r.ok)
        self.assertEqual(r.total_rows, 2)
        self.assertEqual(r.rows[0]["reading"], 21.5)
        self.assertEqual(r.rows[1]["utc_hour"], "2026-11-15T01:00:00Z")

    def test_utc_hour_offset_normalized(self):
        text = ("obs_id,station,utc_hour,reading,unit\n"
                "OB-000001,ST-01,2026-11-15T10:00:00+08:00,21.5,°C\n")
        r = validate_csv(self.contract, text)
        self.assertTrue(r.ok)
        self.assertEqual(r.rows[0]["utc_hour"], "2026-11-15T02:00:00Z")

    def test_row_errors_collected_with_line_numbers(self):
        text = ("obs_id,station,utc_hour,reading,unit\n"
                "OB-000001,ST-01,2026-11-15T00:00:00Z,21.5,°C\n"
                "bad-id,ST-99,2026-11-15T00:30:00Z,abc,mg\n"
                "OB-000003,ST-02,2026-11-15T01:00:00Z,58.0,°C\n")
        r = validate_csv(self.contract, text)
        self.assertFalse(r.ok)
        self.assertEqual(r.total_rows, 3)
        self.assertEqual(r.error_rows, 2)
        q1, q2 = r.quarantine
        self.assertEqual(q1.line, 3)
        cols = {e.column for e in q1.errors}
        self.assertEqual(cols, {"obs_id", "station", "utc_hour", "reading", "unit"})
        self.assertEqual(q2.line, 4)
        self.assertEqual([e.column for e in q2.errors], ["reading"])
        self.assertIn("超出上限", q2.errors[0].message)
        self.assertEqual(q1.record["obs_id"], "bad-id")

    def test_required_empty_and_unique(self):
        text = ("obs_id,station,utc_hour,reading,unit\n"
                "OB-000001,ST-01,2026-11-15T00:00:00Z,21.5,°C\n"
                ",ST-01,2026-11-15T01:00:00Z,21.5,°C\n"
                "OB-000001,ST-02,2026-11-15T02:00:00Z,20.0,°C\n")
        r = validate_csv(self.contract, text)
        self.assertEqual(r.error_rows, 2)
        self.assertIn("必填字段为空", r.quarantine[0].errors[0].message)
        self.assertIn("唯一", r.quarantine[1].errors[0].message)

    def test_header_errors(self):
        # 缺必填列 + 未知列
        text = "obs_id,reading,extra\nOB-000001,21.5,x\n"
        r = validate_csv(self.contract, text)
        self.assertFalse(r.ok)
        self.assertEqual(len(r.quarantine), 0)  # 表头不合格时不再逐行判定
        joined = "；".join(r.header_errors)
        self.assertIn("缺少契约必填列", joined)
        self.assertIn("未定义", joined)
        self.assertEqual(r.total_rows, 1)

    def test_column_count_mismatch(self):
        text = ("obs_id,station,utc_hour,reading,unit\n"
                "OB-000001,ST-01,2026-11-15T00:00:00Z,21.5\n")
        r = validate_csv(self.contract, text)
        self.assertFalse(r.ok)
        msgs = [e.message for e in r.quarantine[0].errors]
        self.assertTrue(any("列数" in m for m in msgs))
        self.assertTrue(any("必填字段为空" in m for m in msgs))  # unit 缺失

    def test_empty_and_header_only(self):
        r = validate_csv(self.contract, "")
        self.assertFalse(r.ok)
        self.assertIn("为空", r.header_errors[0])
        r = validate_csv(self.contract, "obs_id,station,utc_hour,reading,unit\n")
        self.assertFalse(r.ok)
        self.assertIn("无数据行", r.header_errors[0])

    def test_type_parsing(self):
        c = make_contract("1.0.0",
                          i={"type": "integer", "required": True},
                          n={"type": "number", "required": True},
                          b={"type": "boolean", "required": True})
        text = "i,n,b\n5,1.25,true\n-3,1e2,0\n"
        r = validate_csv(c, text)
        self.assertTrue(r.ok)
        self.assertEqual(r.rows[0], {"i": 5, "n": 1.25, "b": True})
        self.assertEqual(r.rows[1]["n"], 100.0)
        self.assertEqual(r.rows[1]["b"], False)
        for bad in ("1.0", "abc", "", "1e"):
            r = validate_csv(c, f"i,n,b\n{bad},1,true\n")
            self.assertFalse(r.ok, bad)
        r = validate_csv(c, "i,n,b\n1,nan,true\n")
        self.assertFalse(r.ok)  # 非有限数值
        r = validate_csv(c, "i,n,b\n1,1,yes\n")
        self.assertFalse(r.ok)  # 非法布尔

    def test_optional_column_absent(self):
        c = make_contract("1.0.0",
                          a={"required": True},
                          opt={"required": False, "allowed": ["x", "y"]})
        r = validate_csv(c, "a\nhello\n")
        self.assertTrue(r.ok)
        self.assertIsNone(r.rows[0]["opt"])
        # 提供了可选列但取值越界 => 隔离
        r = validate_csv(c, "a,opt\nhello,z\n")
        self.assertFalse(r.ok)

    def test_string_constraints(self):
        c = make_contract("1.0.0",
                          s={"required": True, "max_length": 3, "pattern": "[a-z]+"})
        self.assertTrue(validate_csv(c, "s\nabc\n").ok)
        self.assertFalse(validate_csv(c, "s\nabcd\n").ok)   # 超长
        self.assertFalse(validate_csv(c, "s\nABC\n").ok)     # 模式不符（整体匹配）
        self.assertFalse(validate_csv(c, "s\nab1\n").ok)


class TestContractStore(unittest.TestCase):
    """契约更新与批次上传交错：批次只读自己绑定的契约快照，失败不影响上次可用批次。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ContractStore(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def _publish_contract(self, data):
        c = parse_contract(data)
        cid = self.store.publish_contract(c.version, c.raw)
        return cid, c

    def test_interleaved_contract_and_batches(self):
        # 1) 契约 v1 上线，批次 A 全量通过并发布
        v1 = base_contract()
        cid1, c1 = self._publish_contract(v1)
        active = self.store.active_contract()
        self.assertEqual((active["id"], active["version"]), (cid1, "1.0.0"))
        r_ok = validate_csv(c1, "obs_id,station,utc_hour,reading,unit\n"
                                "OB-000001,ST-01,2026-11-15T00:00:00Z,21.5,°C\n")
        self.assertTrue(r_ok.ok)
        bid_a = self.store.publish_batch("批次A", active, r_ok.total_rows, r_ok.rows)

        # 2) 契约兼容演进到 v2（放宽：上限 50→60、单位 +°F、新增可选列）
        v2 = base_contract(version="2.0.0")
        next(c for c in v2["columns"] if c["name"] == "reading")["max"] = 60
        next(c for c in v2["columns"] if c["name"] == "unit")["allowed"] = ["°C", "°F"]
        v2["columns"].append({"name": "qc_flag", "type": "string", "required": False})
        old = parse_contract(self.store.active_contract()["payload"])
        self.assertEqual(check_compatible(old, parse_contract(v2)), [])
        cid2, c2 = self._publish_contract(v2)
        self.assertEqual(self.store.active_contract()["version"], "2.0.0")

        # 3) 批次 B 使用 v2 才合法的取值（°F、55 度），发布成功并绑定 v2
        r2 = validate_csv(c2, "obs_id,station,utc_hour,reading,unit,qc_flag\n"
                              "OB-000002,ST-01,2026-11-15T01:00:00Z,55,°F,PASS\n")
        self.assertTrue(r2.ok)
        bid_b = self.store.publish_batch("批次B", self.store.active_contract(),
                                         r2.total_rows, r2.rows)

        # 4) 交错后查询不得混用：A 仍绑定 v1 快照，B 绑定 v2 快照
        a = self.store.get_batch(bid_a)
        b = self.store.get_batch(bid_b)
        self.assertEqual(a["contract"]["version"], "1.0.0")
        self.assertEqual(a["contract"]["id"], cid1)
        self.assertEqual(a["contract"]["payload"]["columns"][4]["allowed"], ["°C"])
        self.assertEqual(b["contract"]["version"], "2.0.0")
        self.assertEqual(b["contract"]["id"], cid2)
        self.assertEqual(len(b["contract"]["payload"]["columns"]), 6)

        # 5) 失败批次隔离：上次可用批次不变
        r_bad = validate_csv(c2, "obs_id,station,utc_hour,reading,unit\n"
                                 "OB-000003,ST-01,2026-11-15T02:00:00Z,999,°C\n")
        self.assertFalse(r_bad.ok)
        self.store.quarantine_batch("批次C", self.store.active_contract(),
                                    r_bad.total_rows, r_bad.error_rows,
                                    r_bad.header_errors, [])
        cur = self.store.current_batch()
        self.assertEqual(cur["id"], bid_b)
        self.assertEqual(cur["contract"]["version"], "2.0.0")

        # 6) 不兼容契约被拒绝：现役契约不变，历史留痕
        v3 = base_contract(version="3.0.0")
        v3["columns"] = [c for c in v3["columns"] if c["name"] != "unit"]
        problems = check_compatible(parse_contract(self.store.active_contract()["payload"]),
                                    parse_contract(v3))
        self.assertTrue(problems)
        self.store.reject_contract("3.0.0", "；".join(problems))
        self.assertEqual(self.store.active_contract()["version"], "2.0.0")
        hist = self.store.contract_history()
        states = {h["version"]: h["state"] for h in hist}
        self.assertEqual(states["1.0.0"], "archived")
        self.assertEqual(states["2.0.0"], "active")
        self.assertEqual(states["3.0.0"], "rejected")
        # 批次历史包含三种状态来源
        bh = self.store.batch_history()
        self.assertEqual([x["state"] for x in bh],
                         ["quarantined", "published", "published"])

    def test_current_batch_empty_initially(self):
        self.assertIsNone(self.store.current_batch())
        self.assertIsNone(self.store.active_contract())
        self.assertEqual(self.store.batch_history(), [])


class TestExamples(unittest.TestCase):
    def test_example_files(self):
        base = Path(__file__).resolve().parent.parent / "examples"
        v1 = parse_contract(json.loads((base / "contract_v1.json").read_text(encoding="utf-8")))
        v2 = parse_contract(json.loads((base / "contract_v2_relaxed.json").read_text(encoding="utf-8")))
        v3 = parse_contract(json.loads((base / "contract_v3_invalid.json").read_text(encoding="utf-8")))
        # v1 -> v2 是兼容演进；v2 -> v3 必须被拒
        self.assertEqual(check_compatible(v1, v2), [])
        problems = check_compatible(v2, v3)
        self.assertTrue(any("删除列 unit" in p for p in problems))
        self.assertTrue(any("类型变更" in p for p in problems))
        self.assertTrue(any("下限上调" in p for p in problems))
        self.assertTrue(any("新增必填列" in p for p in problems))
        # 合格 CSV 在 v1/v2 下都通过
        ok_text = (base / "observations_ok.csv").read_text(encoding="utf-8-sig")
        self.assertTrue(validate_csv(v1, ok_text).ok)
        self.assertTrue(validate_csv(v2, ok_text).ok)
        # 含错 CSV：9 行出错、1 行合格
        bad = validate_csv(v1, (base / "observations_bad.csv").read_text(encoding="utf-8-sig"))
        self.assertFalse(bad.ok)
        self.assertEqual(bad.total_rows, 10)
        self.assertEqual(bad.error_rows, 9)
        self.assertEqual([q.line for q in bad.quarantine], [3, 4, 5, 6, 7, 8, 9, 10, 11])
        # v2 批次：v2 下通过，v1 下被拒（未知列 qc_flag + 枚举越界）
        v2_text = (base / "observations_v2_ok.csv").read_text(encoding="utf-8-sig")
        self.assertTrue(validate_csv(v2, v2_text).ok)
        self.assertFalse(validate_csv(v1, v2_text).ok)


if __name__ == "__main__":
    unittest.main()
