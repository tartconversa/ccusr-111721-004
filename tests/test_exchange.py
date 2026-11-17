import json
import tempfile
import unittest
from pathlib import Path

from exchange import Exchange


class ExchangeFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Exchange(Path(self.temp.name) / "exchange.db")
        self.service.catalog.register("flow", "机构甲", "甲地", "restricted", [("count", "通行计数", "integer", False)], "2026-11-01")
        self.service.catalog.ingest("item-1", "flow", {"count": 8}, "2026-11-02")
        self.service.grants.issue("g-1", "flow", "机构乙", "运行协调", "2026-12-31", "2026-11-03")
        self.service.policies.set("甲地", "restricted", "运行协调", "allow", "2026-11-03", "机构甲")
        self.revision = self.service.products.assemble("report", "通行观察", ["item-1"], "2026-11-04", "机构甲")

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_delivery_keeps_revision_and_request_identity(self):
        desk = self.service.delivery
        receipt = desk.send("d-1", "report", self.revision, "机构乙", "运行协调", "2026-11-05")
        self.assertEqual(receipt["product_revision"], self.revision)
        self.assertEqual(receipt, desk.send("d-1", "report", self.revision, "机构乙", "运行协调", "2026-11-05"))
        with self.assertRaises(ValueError):
            desk.send("d-1", "report", self.revision, "其他机构", "运行协调", "2026-11-05")

    def test_grant_and_policy_gate_delivery(self):
        with self.assertRaises(PermissionError):
            self.service.delivery.send("d-2", "report", self.revision, "陌生机构", "运行协调", "2026-11-05")
        self.service.policies.set("甲地", "restricted", "运行协调", "deny", "2026-11-06", "机构甲")
        with self.assertRaises(PermissionError):
            self.service.delivery.send("d-3", "report", self.revision, "机构乙", "运行协调", "2026-11-07")
        self.assertTrue(self.service.history("report"))

    def test_unknown_fields_rejected(self):
        with self.assertRaises(ValueError):
            self.service.catalog.ingest("item-2", "flow", {"unknown": 9}, "2026-11-03")


class PurposeChangeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Exchange(Path(self.temp.name) / "exchange.db")
        self.service.catalog.register("flow", "机构甲", "甲地", "restricted", [("count", "通行计数", "integer", False)], "2026-11-01")
        self.service.catalog.register("geo", "机构丙", "乙地", "business", [("zone", "片区编码", "text", False)], "2026-11-01")
        self.service.catalog.ingest("item-1", "flow", {"count": 8}, "2026-11-02")
        self.service.catalog.ingest("item-2", "geo", {"zone": "A9"}, "2026-11-02")
        self.service.grants.issue("g-1", "flow", "机构乙", "运力协调", "2026-12-31", "2026-11-03")
        self.service.grants.issue("g-2", "geo", "机构乙", "运力协调", "2026-12-31", "2026-11-03")
        self.service.policies.set("甲地", "restricted", "运力协调", "allow", "2026-11-03", "机构甲")
        self.service.policies.set("乙地", "business", "运力协调", "allow", "2026-11-03", "机构丙")
        self.revision = self.service.products.assemble("report", "跨境物流观察", ["item-1", "item-2"], "2026-11-04", "机构甲")
        self.service.delivery.send("d-1", "report", self.revision, "机构乙", "运力协调", "2026-11-05")
        # geo 来源已具备新用途许可与法域规则（可延用），flow 尚未（需新授权）
        self.service.grants.issue("g-3", "geo", "机构乙", "商业选址", "2026-12-31", "2026-11-06")
        self.service.policies.set("乙地", "business", "商业选址", "allow", "2026-11-06", "机构丙")

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def authorize_flow_for_new_purpose(self):
        self.service.grants.issue("g-4", "flow", "机构乙", "商业选址", "2026-12-31", "2026-11-09")
        self.service.policies.set("甲地", "restricted", "商业选址", "allow", "2026-11-09", "机构甲")

    def test_assessment_traces_each_source(self):
        request = self.service.repurposing.submit("r-1", "d-1", "商业选址", "2026-11-07", "机构乙")
        self.assertEqual(request["status"], "pending")
        self.assertEqual(request["delivery_id"], "d-1")
        inputs = {entry["source"]: entry for entry in request["assessment"]["inputs"]}
        self.assertEqual(inputs["geo"]["grant"], "reuse")
        self.assertEqual(inputs["geo"]["grant_id"], "g-3")
        self.assertEqual(inputs["geo"]["policy"], "allow")
        self.assertEqual(inputs["flow"]["grant"], "required")
        self.assertEqual(inputs["flow"]["policy"], "deny")
        self.assertFalse(request["assessment"]["ready"])
        trail = self.service.history("r-1")
        self.assertEqual(trail[0]["action"], "purpose.request")
        details = json.loads(trail[0]["details"])
        self.assertEqual(details["from_purpose"], "运力协调")
        self.assertEqual({entry["source"] for entry in details["inputs"]}, {"flow", "geo"})

    def test_claim_blocked_until_approved(self):
        self.service.repurposing.submit("r-1", "d-1", "商业选址", "2026-11-07", "机构乙")
        with self.assertRaises(PermissionError) as caught:
            self.service.delivery.send("d-2", "report", self.revision, "机构乙", "商业选址", "2026-11-08")
        reason = caught.exception.args[0][0]
        self.assertEqual(reason["reason"], "purpose")
        self.assertEqual(reason["request"], "r-1")
        self.assertEqual(reason["request_status"], "pending")
        with self.assertRaises(PermissionError) as caught:
            self.service.delivery.send("d-3", "report", self.revision, "机构乙", "市场分析", "2026-11-08")
        self.assertEqual(caught.exception.args[0][0]["request_status"], "missing")
        rejects = [event for event in self.service.history("report") if event["action"] == "delivery.reject"]
        self.assertEqual(len(rejects), 2)
        self.assertEqual(json.loads(rejects[0]["details"])["reasons"][0]["request"], "r-1")

    def test_approval_requires_all_sources_cleared(self):
        self.service.repurposing.submit("r-1", "d-1", "商业选址", "2026-11-07", "机构乙")
        with self.assertRaises(PermissionError) as caught:
            self.service.repurposing.approve("r-1", "2026-11-08", "审批员")
        gaps = caught.exception.args[0]
        self.assertIn({"source": "flow", "reason": "grant"}, gaps)
        self.assertIn({"source": "flow", "reason": "policy"}, gaps)
        self.assertEqual(self.service.repurposing.get("r-1")["status"], "pending")
        self.authorize_flow_for_new_purpose()
        approved = self.service.repurposing.approve("r-1", "2026-11-10", "审批员")
        self.assertEqual(approved["status"], "approved")
        self.assertTrue(approved["assessment"]["ready"])
        self.assertEqual(approved, self.service.repurposing.approve("r-1", "2026-11-10", "审批员"))

    def test_approved_claim_keeps_original_delivery_intact(self):
        self.service.repurposing.submit("r-1", "d-1", "商业选址", "2026-11-07", "机构乙")
        self.authorize_flow_for_new_purpose()
        self.service.repurposing.approve("r-1", "2026-11-10", "审批员")
        receipt = self.service.delivery.send("d-2", "report", self.revision, "机构乙", "商业选址", "2026-11-11")
        self.assertEqual(receipt["purpose"], "商业选址")
        original = self.service.delivery.send("d-1", "report", self.revision, "机构乙", "运力协调", "2026-11-05")
        self.assertEqual(original["purpose"], "运力协调")
        self.assertEqual(original["created_at"], "2026-11-05")
        self.assertEqual(json.loads(original["policy_revisions"]), {"flow": 1, "geo": 1})
        self.assertNotEqual(receipt["input_digest"], "")
        self.assertEqual(original["input_digest"], receipt["input_digest"])

    def test_rejection_blocks_and_explains(self):
        self.service.repurposing.submit("r-1", "d-1", "商业选址", "2026-11-07", "机构乙")
        rejected = self.service.repurposing.reject("r-1", "法域评估未通过", "2026-11-08", "审批员")
        self.assertEqual(rejected["status"], "rejected")
        with self.assertRaises(ValueError):
            self.service.repurposing.approve("r-1", "2026-11-09", "审批员")
        with self.assertRaises(ValueError):
            self.service.repurposing.reject("r-1", "重复驳回", "2026-11-09", "审批员")
        with self.assertRaises(PermissionError) as caught:
            self.service.delivery.send("d-2", "report", self.revision, "机构乙", "商业选址", "2026-11-09")
        reason = caught.exception.args[0][0]
        self.assertEqual(reason["request_status"], "rejected")
        self.assertEqual(reason["request_reason"], "法域评估未通过")
        resubmitted = self.service.repurposing.submit("r-2", "d-1", "商业选址", "2026-11-10", "机构乙")
        self.assertEqual(resubmitted["status"], "pending")
        with self.assertRaises(PermissionError) as caught:
            self.service.delivery.send("d-2", "report", self.revision, "机构乙", "商业选址", "2026-11-10")
        self.assertEqual(caught.exception.args[0][0]["request"], "r-2")

    def test_midway_source_change_blocks_and_is_reviewable(self):
        self.service.repurposing.submit("r-1", "d-1", "商业选址", "2026-11-07", "机构乙")
        self.authorize_flow_for_new_purpose()
        self.service.repurposing.approve("r-1", "2026-11-10", "审批员")
        self.service.policies.set("甲地", "restricted", "商业选址", "deny", "2026-11-12", "机构甲")
        with self.assertRaises(PermissionError) as caught:
            self.service.delivery.send("d-2", "report", self.revision, "机构乙", "商业选址", "2026-11-13")
        self.assertIn({"source": "flow", "reason": "policy"}, caught.exception.args[0])
        review = self.service.repurposing.review("r-1", "2026-11-13")
        self.assertFalse(review["assessment"]["ready"])
        self.assertIn({"source": "flow", "aspect": "policy", "was": "allow", "now": "deny"}, review["changes"])
        rejects = [event for event in self.service.history("report") if event["action"] == "delivery.reject"]
        self.assertEqual(json.loads(rejects[-1]["details"])["reasons"], [{"reason": "policy", "source": "flow"}])

    def test_submit_validation(self):
        with self.assertRaises(KeyError):
            self.service.repurposing.submit("r-9", "d-x", "商业选址", "2026-11-07", "机构乙")
        with self.assertRaises(ValueError):
            self.service.repurposing.submit("r-9", "d-1", "运力协调", "2026-11-07", "机构乙")
        first = self.service.repurposing.submit("r-1", "d-1", "商业选址", "2026-11-07", "机构乙")
        self.assertEqual(first, self.service.repurposing.submit("r-1", "d-1", "商业选址", "2026-11-07", "机构乙"))
        with self.assertRaises(ValueError):
            self.service.repurposing.submit("r-1", "d-1", "市场分析", "2026-11-07", "机构乙")
        with self.assertRaises(ValueError):
            self.service.repurposing.submit("r-2", "d-1", "商业选址", "2026-11-07", "机构乙")


if __name__ == "__main__":
    unittest.main()