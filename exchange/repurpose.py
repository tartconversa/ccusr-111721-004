import json

from .audit import write
from .grants import Grants
from .policy import Policies
from .products import Products


def _gaps(assessment):
    reasons = []
    for entry in assessment["inputs"]:
        if not entry["active"]:
            reasons.append({"source": entry["source"], "reason": "inactive"})
        if entry["grant"] != "reuse":
            reasons.append({"source": entry["source"], "reason": "grant"})
        if entry["policy"] != "allow":
            reasons.append({"source": entry["source"], "reason": "policy"})
    return reasons


class Repurposing:
    """用途变更申请：不改写既有交付，为同一产品向同一合作方申请新用途。"""

    def __init__(self, connection):
        self.connection = connection
        self.grants = Grants(connection)
        self.policies = Policies(connection)
        self.products = Products(connection)

    def submit(self, request_id, delivery_id, purpose, at, actor):
        existing = self._row(request_id)
        if existing:
            if (existing["delivery_id"], existing["purpose"]) != (delivery_id, purpose):
                raise ValueError("申请编号已用于其他请求")
            return existing
        delivery = self.connection.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
        if delivery is None:
            raise KeyError(delivery_id)
        if delivery["purpose"] == purpose:
            raise ValueError("新用途与既有交付用途相同")
        pending = self.connection.execute(
            "SELECT id FROM purpose_requests WHERE product_id=? AND recipient=? AND purpose=? AND status='pending'",
            (delivery["product_id"], delivery["recipient"], purpose),
        ).fetchone()
        if pending:
            raise ValueError("已存在待审的用途变更申请")
        assessment = self._assess(delivery["product_id"], delivery["product_revision"], delivery["recipient"], purpose, at)
        with self.connection:
            self.connection.execute(
                "INSERT INTO purpose_requests(id,delivery_id,product_id,product_revision,recipient,purpose,assessment,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    delivery_id,
                    delivery["product_id"],
                    delivery["product_revision"],
                    delivery["recipient"],
                    purpose,
                    json.dumps(assessment, ensure_ascii=False, sort_keys=True),
                    at,
                ),
            )
            write(
                self.connection, at, actor, "purpose.request", request_id,
                {
                    "delivery": delivery_id,
                    "product": delivery["product_id"],
                    "revision": delivery["product_revision"],
                    "recipient": delivery["recipient"],
                    "from_purpose": delivery["purpose"],
                    "purpose": purpose,
                    "ready": assessment["ready"],
                    "inputs": assessment["inputs"],
                },
            )
        return self.get(request_id)

    def approve(self, request_id, at, actor):
        row = self.get(request_id)
        if row["status"] == "approved":
            return row
        if row["status"] == "rejected":
            raise ValueError("用途变更申请已驳回")
        assessment = self._assess(row["product_id"], row["product_revision"], row["recipient"], row["purpose"], at)
        if not assessment["ready"]:
            with self.connection:
                write(self.connection, at, actor, "purpose.assess", request_id, {"ready": False, "inputs": assessment["inputs"]})
            raise PermissionError(_gaps(assessment))
        with self.connection:
            self.connection.execute(
                "UPDATE purpose_requests SET status='approved', assessment=?, decided_at=? WHERE id=?",
                (json.dumps(assessment, ensure_ascii=False, sort_keys=True), at, request_id),
            )
            write(self.connection, at, actor, "purpose.approve", request_id, {"purpose": row["purpose"], "inputs": assessment["inputs"]})
        return self.get(request_id)

    def reject(self, request_id, reason, at, actor):
        row = self.get(request_id)
        if row["status"] != "pending":
            raise ValueError("用途变更申请已结案")
        with self.connection:
            self.connection.execute(
                "UPDATE purpose_requests SET status='rejected', reason=?, decided_at=? WHERE id=?",
                (reason, at, request_id),
            )
            write(self.connection, at, actor, "purpose.reject", request_id, {"reason": reason})
        return self.get(request_id)

    def review(self, request_id, at):
        row = self.get(request_id)
        current = self._assess(row["product_id"], row["product_revision"], row["recipient"], row["purpose"], at)
        stored = {entry["source"]: entry for entry in row["assessment"]["inputs"]}
        changes = []
        for entry in current["inputs"]:
            before = stored.get(entry["source"])
            if before is None:
                continue
            for aspect in ("grant", "policy", "active"):
                if before[aspect] != entry[aspect]:
                    changes.append({"source": entry["source"], "aspect": aspect, "was": before[aspect], "now": entry[aspect]})
        return {"request": row, "assessment": current, "changes": changes}

    def clearance(self, product_id, recipient, purpose):
        established = self.connection.execute(
            "SELECT 1 FROM deliveries WHERE product_id=? AND recipient=? AND purpose=? LIMIT 1",
            (product_id, recipient, purpose),
        ).fetchone()
        if established:
            return None
        delivered = self.connection.execute(
            "SELECT 1 FROM deliveries WHERE product_id=? AND recipient=? LIMIT 1",
            (product_id, recipient),
        ).fetchone()
        if not delivered:
            return None
        approved = self.connection.execute(
            "SELECT id FROM purpose_requests WHERE product_id=? AND recipient=? AND purpose=? AND status='approved' LIMIT 1",
            (product_id, recipient, purpose),
        ).fetchone()
        if approved:
            return None
        row = self.connection.execute(
            "SELECT id,status,reason FROM purpose_requests WHERE product_id=? AND recipient=? AND purpose=? ORDER BY rowid DESC LIMIT 1",
            (product_id, recipient, purpose),
        ).fetchone()
        if row:
            return {"request": row["id"], "status": row["status"], "reason": row["reason"]}
        return {"request": None, "status": "missing", "reason": None}

    def get(self, request_id):
        row = self._row(request_id)
        if row is None:
            raise KeyError(request_id)
        return row

    def _row(self, request_id):
        row = self.connection.execute("SELECT * FROM purpose_requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["assessment"] = json.loads(result["assessment"])
        return result

    def _assess(self, product_id, product_revision, recipient, purpose, at):
        sources = {}
        for asset in self.products.inputs(product_id, product_revision):
            entry = sources.setdefault(
                asset["source_id"],
                {"jurisdiction": asset["jurisdiction"], "classification": asset["classification"], "active": True},
            )
            entry["active"] = entry["active"] and asset["state"] == "active" and bool(asset["source_active"])
        inputs = []
        for source_id in sorted(sources):
            info = sources[source_id]
            grant_id = self.grants.permits(source_id, recipient, purpose, at)
            decision, policy_revision = self.policies.decision(info["jurisdiction"], info["classification"], purpose)
            inputs.append(
                {
                    "source": source_id,
                    "grant": "reuse" if grant_id else "required",
                    "grant_id": grant_id,
                    "policy": decision,
                    "policy_revision": policy_revision,
                    "active": info["active"],
                }
            )
        ready = bool(inputs) and all(entry["grant"] == "reuse" and entry["policy"] == "allow" and entry["active"] for entry in inputs)
        return {"inputs": inputs, "ready": ready}
