from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_consignment(actor, data, lookup):
    if data.get("origin") == data.get("destination"):
        raise ValidationError("origin and destination must differ")
    parent_id = data.get("parent_id")
    if parent_id in (None, ""):
        return
    if not isinstance(parent_id, str):
        raise ValidationError("parent_id must be a string")
    if parent_id == data.get("id"):
        raise ValidationError("parent_id cannot reference the consignment itself")
    parent = _find_one(lookup, "consignment", "id", parent_id)
    if parent is None:
        raise ValidationError("parent consignment not found: " + parent_id)
    new_id = data.get("id")
    if new_id:
        seen = {new_id, parent_id}
        node = parent
        while node:
            ancestor = (node.get("data") or {}).get("parent_id")
            if not ancestor:
                break
            if ancestor in seen:
                raise ValidationError("parent_id would create a cycle")
            seen.add(ancestor)
            node = _find_one(lookup, "consignment", "id", ancestor)


def _validate_quarantine(actor, entity, data, lookup):
    if not data.get("pest_found"):
        raise ValidationError("pest_found must be true for quarantine")
    return {"quarantined_by": actor.user_id}


def _validate_release(actor, entity, data, lookup):
    if data.get("pest_found"):
        raise ValidationError("pest-positive consignment cannot be released")
    if data.get("treatment") not in ("none", "completed", "certified"):
        raise ValidationError("release requires a valid treatment state")
    return {"released_by": actor.user_id}


def trace_downstream(consignments, start_id):
    pending = [start_id]
    visited = set()
    result = []
    while pending:
        current = pending.pop(0)
        if current in visited:
            continue
        visited.add(current)
        result.append(current)
        for item in consignments:
            if item.get("parent_id") == current:
                pending.append(item.get("id"))
    return result


def find_root_id(links, start_id):
    """Walk parent links upward to the ultimate source id (cycle-safe)."""
    by_id = {link.get("id"): link for link in links}
    current = start_id
    visited = set()
    while current and current not in visited:
        visited.add(current)
        link = by_id.get(current)
        if not link:
            break
        parent = link.get("parent_id")
        if not parent or parent not in by_id:
            break
        current = parent
    return current


def _children_map(consignments):
    children = {}
    for entity in consignments:
        parent = (entity.get("data") or {}).get("parent_id")
        children.setdefault(parent, []).append(entity)
    for group in children.values():
        group.sort(key=lambda item: (item.get("created_at") or "", item.get("id") or ""))
    return children


def _descendant_counts(consignments):
    """Total transitive downstream count for every consignment."""
    children = _children_map(consignments)
    counts = {}

    def measure(node_id, stack):
        if node_id in counts:
            return counts[node_id]
        if node_id in stack:
            return 0
        stack.add(node_id)
        total = 0
        for child in children.get(node_id, []):
            total += 1 + measure(child.get("id"), stack)
        stack.discard(node_id)
        counts[node_id] = total
        return total

    for entity in consignments:
        measure(entity.get("id"), set())
    return counts


def _chain_summary(entity, counts):
    data = entity.get("data") or {}
    return {
        "id": entity.get("id"),
        "code": data.get("code"),
        "status": entity.get("status"),
        "parent_id": data.get("parent_id"),
        "origin": data.get("origin"),
        "destination": data.get("destination"),
        "pest_found": bool(data.get("pest_found")),
        "downstream_count": counts.get(entity.get("id"), 0),
        "updated_at": entity.get("updated_at"),
    }


def list_chain_roots(consignments):
    """Roots are consignments without a known parent — legacy ones included."""
    known = {entity.get("id") for entity in consignments}
    counts = _descendant_counts(consignments)
    roots = []
    for entity in consignments:
        parent = (entity.get("data") or {}).get("parent_id")
        if parent and parent in known:
            continue
        roots.append(_chain_summary(entity, counts))
    return roots


def build_chain(consignments, start_id):
    """Resolve the source of start_id and return the whole downstream chain."""
    by_id = {entity.get("id"): entity for entity in consignments}
    links = [
        {"id": entity.get("id"), "parent_id": (entity.get("data") or {}).get("parent_id")}
        for entity in consignments
    ]
    root_id = find_root_id(links, start_id)
    children = _children_map(consignments)
    counts = _descendant_counts(consignments)
    items = []
    visited = set()
    queue = [(root_id, 0)]
    while queue:
        current, depth = queue.pop(0)
        if current in visited or current not in by_id:
            continue
        visited.add(current)
        summary = _chain_summary(by_id[current], counts)
        summary["depth"] = depth
        items.append(summary)
        for child in children.get(current, []):
            queue.append((child.get("id"), depth + 1))
    return {
        "root_id": root_id,
        "queried_id": start_id,
        "total": len(items),
        "items": items,
    }


BULK_QUARANTINE_ROLES = ("admin", "quarantine")
BULK_QUARANTINE_SKIP = {
    "quarantined": "already quarantined",
    "released": "already released",
    "destroyed": "already destroyed",
}


def plan_bulk_quarantine(actor, source, consignments):
    """Split the source's downstream chain into quarantine targets and skips.

    Destroyed, released or already quarantined batches must not be touched.
    """
    if source.get("kind") != "consignment":
        raise ValidationError("bulk quarantine source must be a consignment")
    RuleEngine._ensure_role(actor, BULK_QUARANTINE_ROLES)
    if source.get("status") != "quarantined":
        raise InvalidTransition(
            "source must be quarantined before downstream quarantine, current status: %s"
            % source.get("status")
        )
    by_id = {entity.get("id"): entity for entity in consignments}
    links = [
        {"id": entity.get("id"), "parent_id": (entity.get("data") or {}).get("parent_id")}
        for entity in consignments
    ]
    order = trace_downstream(links, source.get("id"))
    to_quarantine = []
    skipped = []
    for consignment_id in order:
        entity = by_id.get(consignment_id)
        if entity is None:
            continue
        entry = {
            "id": consignment_id,
            "code": (entity.get("data") or {}).get("code"),
            "status": entity.get("status"),
        }
        reason = BULK_QUARANTINE_SKIP.get(entity.get("status"))
        if reason:
            skipped.append(dict(entry, reason=reason))
        else:
            to_quarantine.append(entry)
    return {"order": order, "quarantine": to_quarantine, "skipped": skipped}


CUSTOM_CREATE = {'consignment': _validate_consignment}
CUSTOM_TRANSITIONS = {('consignment', 'quarantine'): _validate_quarantine, ('consignment', 'release'): _validate_release}


class RuleEngine:
    ALIASES = {'consignments': 'consignment', 'facilities': 'facility'}
    INITIAL_STATUS = {'consignment': 'declared', 'facility': 'registered'}
    TRANSITIONS = {'consignment': {'inspect': (('declared',), 'inspected'), 'quarantine': (('inspected',), 'quarantined'), 'release': (('inspected',), 'released'), 'destroy': (('quarantined',), 'destroyed'), 'recheck': (('quarantined',), 'inspected')}, 'facility': {'trace': (('registered',), 'traced')}}
    CREATE_REQUIRED = {'consignment': ('code', 'origin', 'destination'), 'facility': ('name', 'address')}
    ACTION_REQUIRED = {('consignment', 'inspect'): ('inspector', 'inspection_result'), ('consignment', 'quarantine'): ('pest_found', 'sample_id'), ('consignment', 'release'): ('pest_found', 'treatment'), ('consignment', 'destroy'): ('method', 'witnessed_by'), ('consignment', 'recheck'): ('sample_id',), ('facility', 'trace'): ('consignment_ids',)}
    CREATE_ROLES = {'consignment': ('admin', 'inspector'), 'facility': ('admin', 'quarantine')}
    ROLE_ACTIONS = {'inspect': ('admin', 'inspector'), 'quarantine': ('admin', 'quarantine'), 'release': ('admin', 'quarantine'), 'destroy': ('admin', 'quarantine'), 'recheck': ('admin', 'inspector'), 'trace': ('admin', 'quarantine')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
