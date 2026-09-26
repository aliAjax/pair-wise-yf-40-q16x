import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, InvalidTransition, PermissionDenied, ValidationError
from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class TraceChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.repo = SQLiteRepository(self.db_path)
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin-1", "admin")
        self.quarantine = Actor("qua-1", "quarantine")

    def tearDown(self):
        self.tmp.cleanup()

    def _consignment(self, code, parent_id=None):
        data = {"code": code, "origin": "O-" + code, "destination": "D-" + code}
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

    def _quarantine(self, entity_id):
        return self.service.transition(
            self.quarantine,
            entity_id,
            "quarantine",
            {"pest_found": True, "sample_id": "S-1"},
        )

    def test_chain_view_groups_by_source(self):
        root = self._consignment("ROOT")
        child = self._consignment("CHILD", root["id"])
        self._consignment("GRAND", child["id"])
        self._consignment("LEGACY")  # 没有历史来源的旧批次

        roots = self.service.list_chains()
        by_code = {item["code"]: item for item in roots}
        self.assertEqual(set(by_code), {"ROOT", "LEGACY"})
        self.assertEqual(by_code["ROOT"]["downstream_count"], 2)
        self.assertEqual(by_code["LEGACY"]["downstream_count"], 0)

        chain = self.service.trace_chain(child["id"])  # 从中间批次也能回溯到源头
        self.assertEqual(chain["root_id"], root["id"])
        self.assertEqual(
            [item["code"] for item in chain["items"]], ["ROOT", "CHILD", "GRAND"]
        )
        self.assertEqual(
            [item["downstream_count"] for item in chain["items"]], [2, 1, 0]
        )
        self.assertEqual([item["depth"] for item in chain["items"]], [0, 1, 2])

    def test_parent_must_exist_and_not_self_reference(self):
        with self.assertRaises(ValidationError):
            self._consignment("ORPHAN", parent_id="missing-parent")
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin,
                "consignment",
                {
                    "id": "self-ref",
                    "code": "SELF",
                    "origin": "A",
                    "destination": "B",
                    "parent_id": "self-ref",
                },
            )

    def test_bulk_quarantine_skips_destroyed_and_released(self):
        root = self._consignment("ROOT")
        inspected_child = self._consignment("INSPECTED", root["id"])
        declared_child = self._consignment("DECLARED", root["id"])
        released_child = self._consignment("RELEASED", root["id"])
        destroyed_child = self._consignment("DESTROYED", root["id"])
        grand = self._consignment("GRAND", inspected_child["id"])

        self._inspect(inspected_child["id"])
        self._inspect(released_child["id"])
        self.service.transition(
            self.quarantine,
            released_child["id"],
            "release",
            {"pest_found": False, "treatment": "certified"},
        )
        self._inspect(destroyed_child["id"], "suspected")
        self._quarantine(destroyed_child["id"])
        self.service.transition(
            self.quarantine,
            destroyed_child["id"],
            "destroy",
            {"method": "incineration", "witnessed_by": "W-1"},
        )

        self._inspect(root["id"], "suspected")
        self._quarantine(root["id"])

        result = self.service.quarantine_downstream(
            self.quarantine, root["id"], {"reason": "upstream positive", "sample_id": "S-1"}
        )

        self.assertEqual(result["quarantined_count"], 3)
        self.assertEqual(
            {item["code"] for item in result["quarantined"]},
            {"INSPECTED", "DECLARED", "GRAND"},
        )
        skipped = {item["code"]: item["reason"] for item in result["skipped"]}
        self.assertEqual(set(skipped), {"ROOT", "RELEASED", "DESTROYED"})
        self.assertEqual(skipped["DESTROYED"], "already destroyed")
        self.assertEqual(skipped["RELEASED"], "already released")

        self.assertEqual(self.service.get(inspected_child["id"])["status"], "quarantined")
        self.assertEqual(self.service.get(declared_child["id"])["status"], "quarantined")
        self.assertEqual(self.service.get(grand["id"])["status"], "quarantined")
        self.assertEqual(self.service.get(released_child["id"])["status"], "released")
        self.assertEqual(self.service.get(destroyed_child["id"])["status"], "destroyed")

        # 可安全重试：再次批量隔离不会重复处理
        again = self.service.quarantine_downstream(self.quarantine, root["id"], {})
        self.assertEqual(again["quarantined_count"], 0)

        audit = self.service.audit_log(entity_id=grand["id"])
        self.assertEqual(audit[-1]["action"], "quarantine_downstream")
        self.assertEqual(audit[-1]["detail"]["source_id"], root["id"])

    def test_bulk_quarantine_requires_positive_source_and_role(self):
        root = self._consignment("ROOT")
        self._consignment("CHILD", root["id"])
        with self.assertRaises(InvalidTransition):
            self.service.quarantine_downstream(self.quarantine, root["id"], {})
        self._inspect(root["id"], "suspected")
        self._quarantine(root["id"])
        with self.assertRaises(PermissionDenied):
            self.service.quarantine_downstream(Actor("view-1", "viewer"), root["id"], {})

    def test_chain_survives_restart(self):
        root = self._consignment("ROOT")
        self._consignment("CHILD", root["id"])
        self._inspect(root["id"], "suspected")
        self._quarantine(root["id"])
        self.service.quarantine_downstream(self.quarantine, root["id"], {})

        # 模拟服务重启：同一数据库文件重新组装依赖
        restarted = DomainService(SQLiteRepository(self.db_path), RuleEngine())
        chain = restarted.trace_chain(root["id"])
        self.assertEqual([item["code"] for item in chain["items"]], ["ROOT", "CHILD"])
        self.assertEqual(
            [item["status"] for item in chain["items"]], ["quarantined", "quarantined"]
        )
        roots = restarted.list_chains()
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0]["downstream_count"], 1)


class HttpTraceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        service = DomainService(repo, RuleEngine())
        self.server = create_server("127.0.0.1", 0, service, RuleEngine(), ".")
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _request(self, method, path, body=None, role="admin", user="tester"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"X-User-Id": user, "X-Role": role}
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, data

    def test_trace_and_bulk_quarantine_over_http(self):
        status, root = self._request(
            "POST",
            "/api/consignment",
            {"code": "R-1", "origin": "Port-A", "destination": "GH-1"},
        )
        self.assertEqual(status, 201)
        status, child = self._request(
            "POST",
            "/api/consignment",
            {
                "code": "C-1",
                "origin": "GH-1",
                "destination": "Nursery-2",
                "parent_id": root["id"],
            },
        )
        self.assertEqual(status, 201)

        status, chains = self._request("GET", "/api/trace")
        self.assertEqual(status, 200)
        self.assertEqual(len(chains["items"]), 1)
        self.assertEqual(chains["items"][0]["downstream_count"], 1)

        status, chain = self._request("GET", "/api/trace/" + child["id"])
        self.assertEqual(status, 200)
        self.assertEqual(chain["root_id"], root["id"])
        self.assertEqual(chain["total"], 2)

        self._request(
            "POST",
            "/api/entities/" + root["id"] + "/actions",
            {"action": "inspect", "data": {"inspector": "I-1", "inspection_result": "suspected"}},
        )
        self._request(
            "POST",
            "/api/entities/" + root["id"] + "/actions",
            {"action": "quarantine", "data": {"pest_found": True, "sample_id": "S-1"}},
            role="quarantine",
        )

        status, result = self._request(
            "POST",
            "/api/trace/" + root["id"] + "/quarantine",
            {"reason": "阳性追溯"},
            role="quarantine",
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["quarantined_count"], 1)
        self.assertEqual(result["quarantined"][0]["id"], child["id"])

        status, current = self._request("GET", "/api/entities/" + child["id"])
        self.assertEqual(status, 200)
        self.assertEqual(current["status"], "quarantined")

        status, _ = self._request("GET", "/api/trace/does-not-exist")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
