import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class ChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.repo = SQLiteRepository(self.db_path)
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.officer = Actor("officer-1", "quarantine")

    def tearDown(self):
        self.tmp.cleanup()

    def _consignment(self, code, parent_id=None):
        data = {"code": code, "origin": "Port-A", "destination": "House-" + code}
        if parent_id:
            data["parent_id"] = parent_id
        return self.service.create(self.admin, "consignment", data)

    def _inspect(self, entity_id, result="clean"):
        return self.service.transition(
            self.admin,
            entity_id,
            "inspect",
            {"inspector": "I-1", "inspection_result": result},
        )

    def _quarantine(self, entity_id, sample="S-1"):
        return self.service.transition(
            self.officer,
            entity_id,
            "quarantine",
            {"pest_found": True, "sample_id": sample},
        )

    def test_chains_group_by_source_and_count_downstream(self):
        root = self._consignment("C-ROOT")
        child = self._consignment("C-CHILD", parent_id=root["id"])
        grand = self._consignment("C-GRAND", parent_id=child["id"])
        legacy = self._consignment("C-OLD")

        chains = {item["root_id"]: item for item in self.service.chains()}
        self.assertEqual(set(chains), {root["id"], legacy["id"]})

        chain = chains[root["id"]]
        self.assertEqual(chain["size"], 3)
        self.assertEqual(chain["downstream_count"], 2)
        self.assertEqual(
            [m["code"] for m in chain["members"]], ["C-ROOT", "C-CHILD", "C-GRAND"]
        )
        self.assertEqual(chain["members"][0]["downstream_count"], 2)
        self.assertEqual(chain["members"][1]["downstream_count"], 1)
        self.assertEqual(chain["members"][2]["downstream_count"], 0)
        self.assertEqual(chain["members"][1]["parent_id"], root["id"])
        self.assertEqual(chain["members"][1]["destination"], "House-C-CHILD")

        legacy_chain = chains[legacy["id"]]
        self.assertEqual(legacy_chain["size"], 1)
        self.assertEqual(legacy_chain["downstream_count"], 0)

    def test_parent_must_be_existing_consignment(self):
        with self.assertRaises(ValidationError):
            self._consignment("C-BAD", parent_id="missing-parent")
        facility = self.service.create(
            self.admin, "facility", {"name": "Nursery", "address": "County 1"}
        )
        with self.assertRaises(ValidationError):
            self._consignment("C-BAD2", parent_id=facility["id"])
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin,
                "consignment",
                {
                    "id": "self-parent",
                    "code": "C-SELF",
                    "origin": "A",
                    "destination": "B",
                    "parent_id": "self-parent",
                },
            )

    def test_quarantine_chain_skips_destroyed_and_released(self):
        root = self._consignment("C-ROOT")
        child_a = self._consignment("C-A", parent_id=root["id"])
        child_b = self._consignment("C-B", parent_id=root["id"])
        child_c = self._consignment("C-C", parent_id=root["id"])
        grand = self._consignment("C-G", parent_id=child_a["id"])

        # 源头检验阳性并隔离
        self._inspect(root["id"], "positive")
        self._quarantine(root["id"], "S-ROOT")
        # child_b 隔离后复检解除
        self._inspect(child_b["id"], "suspected")
        self._quarantine(child_b["id"], "S-B")
        self.service.transition(self.admin, child_b["id"], "recheck", {"sample_id": "S-B2"})
        self.service.transition(
            self.officer,
            child_b["id"],
            "release",
            {"pest_found": False, "treatment": "completed"},
        )
        # child_c 已检查未隔离
        self._inspect(child_c["id"], "clean")
        # grand 已销毁
        self._inspect(grand["id"], "positive")
        self._quarantine(grand["id"], "S-G")
        self.service.transition(
            self.officer,
            grand["id"],
            "destroy",
            {"method": "incineration", "witnessed_by": "W-1"},
        )

        result = self.service.quarantine_chain(self.officer, root["id"])

        isolated = {item["id"]: item for item in result["isolated"]}
        self.assertEqual(set(isolated), {child_a["id"], child_c["id"]})
        self.assertEqual(isolated[child_a["id"]]["from_status"], "declared")
        self.assertEqual(isolated[child_c["id"]]["from_status"], "inspected")

        skipped = {item["id"]: item["status"] for item in result["skipped"]}
        self.assertEqual(skipped[root["id"]], "quarantined")
        self.assertEqual(skipped[child_b["id"]], "released")
        self.assertEqual(skipped[grand["id"]], "destroyed")

        self.assertEqual(self.service.get(child_a["id"])["status"], "quarantined")
        self.assertEqual(self.service.get(child_c["id"])["status"], "quarantined")
        self.assertEqual(self.service.get(child_b["id"])["status"], "released")
        self.assertEqual(self.service.get(grand["id"])["status"], "destroyed")

        # 台账：批量隔离写入审计并记录来源链条
        audit = self.service.audit_log(child_a["id"])
        isolate_entries = [row for row in audit if row["action"] == "isolate"]
        self.assertEqual(len(isolate_entries), 1)
        self.assertEqual(
            isolate_entries[0]["detail"]["patch"]["chain_root"], root["id"]
        )

    def test_quarantine_chain_permissions(self):
        root = self._consignment("C-ROOT")
        with self.assertRaises(PermissionDenied):
            self.service.quarantine_chain(Actor("viewer", "viewer"), root["id"])
        with self.assertRaises(PermissionDenied):
            self.service.quarantine_chain(Actor("lab", "lab"), root["id"])
        result = self.service.quarantine_chain(self.officer, root["id"])
        self.assertEqual(result["isolated_count"], 1)
        self.assertEqual(self.service.get(root["id"])["status"], "quarantined")

    def test_isolate_transition_rules(self):
        batch = self._consignment("C-1")
        with self.assertRaises(ValidationError):
            self.service.transition(self.officer, batch["id"], "isolate", {})
        updated = self.service.transition(
            self.officer, batch["id"], "isolate", {"reason": "manual suspect"}
        )
        self.assertEqual(updated["status"], "quarantined")
        self.assertEqual(updated["data"]["isolated_by"], "officer-1")
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.officer, batch["id"], "isolate", {"reason": "again"}
            )

    def test_chain_endpoint_validation(self):
        with self.assertRaises(NotFoundError):
            self.service.chain("missing")
        facility = self.service.create(
            self.admin, "facility", {"name": "Nursery", "address": "County 1"}
        )
        with self.assertRaises(ValidationError):
            self.service.chain(facility["id"])
        with self.assertRaises(NotFoundError):
            self.service.quarantine_chain(self.officer, "missing")

    def test_restart_keeps_chain_view(self):
        root = self._consignment("C-ROOT")
        child = self._consignment("C-CHILD", parent_id=root["id"])
        self._inspect(child["id"], "clean")
        self.service.quarantine_chain(self.officer, root["id"])

        # 模拟服务重启：同一数据库文件重新装配仓储与服务
        restarted = DomainService(SQLiteRepository(self.db_path), RuleEngine())
        chains = {item["root_id"]: item for item in restarted.chains()}
        self.assertIn(root["id"], chains)
        chain = restarted.chain(root["id"])
        self.assertEqual(chain["downstream_count"], 1)
        self.assertEqual(
            [m["code"] for m in chain["members"]], ["C-ROOT", "C-CHILD"]
        )
        self.assertEqual(chain["members"][0]["status"], "quarantined")
        self.assertEqual(chain["members"][1]["status"], "quarantined")


if __name__ == "__main__":
    unittest.main()
