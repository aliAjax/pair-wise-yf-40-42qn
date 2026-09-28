from datetime import datetime, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

ZONE_OBSERVATION_DAYS = 21


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def _parse_date(value, field):
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (ValueError, TypeError):
        raise ValidationError("invalid date for %s: %s" % (field, value))


def _validate_consignment(actor, data, lookup):
    if data.get("origin") == data.get("destination"):
        raise ValidationError("origin and destination must differ")


def zone_member_ids(zone_data):
    ids = {zone_data.get("center_facility_id")}
    for item in zone_data.get("facilities", []):
        ids.add(item.get("facility_id"))
    ids.discard(None)
    return ids


def zone_last_positive_at(zone_data):
    dates = [event.get("at") for event in zone_data.get("positive_events", []) if event.get("at")]
    return max(dates) if dates else None


def zone_release_status(zone_data, as_of=None):
    """Evaluate the conditions for lifting a quarantine zone as of a date."""
    as_of_date = _parse_date(as_of or _today(), "as_of")
    events = list(zone_data.get("positive_events", []))
    last_raw = zone_last_positive_at(zone_data)
    last_date = _parse_date(last_raw, "positive_events.at") if last_raw else None
    elapsed = (as_of_date - last_date).days if last_date else None
    declared_raw = zone_data.get("declared_at")
    declared_date = _parse_date(declared_raw, "declared_at") if declared_raw else None

    new_facilities = []
    seen = set()
    for event in events:
        event_date = _parse_date(event.get("at"), "positive_events.at")
        is_new = declared_date is not None and event_date > declared_date
        if is_new and event.get("facility_id") not in seen:
            seen.add(event.get("facility_id"))
            new_facilities.append({"facility_id": event.get("facility_id"), "at": event.get("at")})

    in_window = [
        event
        for event in events
        if (as_of_date - _parse_date(event.get("at"), "positive_events.at")).days
        < ZONE_OBSERVATION_DAYS
    ]

    elapsed_met = elapsed is not None and elapsed >= ZONE_OBSERVATION_DAYS
    no_new_met = elapsed_met and not in_window
    return {
        "as_of": as_of_date.isoformat(),
        "observation_days": ZONE_OBSERVATION_DAYS,
        "declared_at": declared_raw,
        "last_positive_at": last_raw,
        "days_since_last_positive": elapsed,
        "new_positive_facilities": new_facilities,
        "positive_facilities_in_window": in_window,
        "conditions": [
            {
                "key": "observation_elapsed",
                "met": elapsed_met,
                "detail": "at least %s days since the last positive (found %s)"
                % (ZONE_OBSERVATION_DAYS, elapsed),
            },
            {
                "key": "no_new_positive_facility",
                "met": no_new_met,
                "detail": "no positive facility recorded during the %s-day window (found %s)"
                % (ZONE_OBSERVATION_DAYS, len(in_window)),
            },
        ],
        "eligible": elapsed_met and no_new_met,
    }


def _validate_zone(actor, data, lookup):
    code = data.get("code")
    center = data.get("center_facility_id")
    if not _find_one(lookup, "facility", "id", center):
        raise ValidationError("center_facility_id must reference an existing facility")
    if _find_one(lookup, "zone", "code", code):
        raise ConflictError("zone code already exists: " + str(code))

    members = []
    raw_members = data.get("facilities")
    if raw_members is None and data.get("facility_ids"):
        reason = data.get("reason") or "included when zone was declared"
        raw_members = [{"facility_id": fid, "reason": reason} for fid in data.get("facility_ids")]
    if not isinstance(raw_members, list) or not raw_members:
        raise ValidationError("facilities must be a non-empty list of inclusions")

    seen = {center}
    for entry in raw_members:
        if not isinstance(entry, dict):
            raise ValidationError("each facility entry needs facility_id and reason")
        fid = entry.get("facility_id")
        reason = entry.get("reason")
        if not fid or not reason:
            raise ValidationError("each facility entry needs facility_id and reason")
        if not _find_one(lookup, "facility", "id", fid):
            raise ValidationError("unknown included facility: " + str(fid))
        if fid in seen:
            continue
        seen.add(fid)
        members.append({"facility_id": fid, "reason": reason})

    declared_at = data.get("declared_at") or _today()
    _parse_date(declared_at, "declared_at")
    data["facilities"] = members
    data["declared_at"] = declared_at
    data["positive_events"] = [{"facility_id": center, "at": declared_at}]
    data["registered_by"] = actor.user_id


def _validate_zone_report_positive(actor, entity, data, lookup):
    facility_id = data["facility_id"]
    if not _find_one(lookup, "facility", "id", facility_id):
        raise ValidationError("facility_id must reference an existing facility")
    positive_at = data.get("positive_at") or _today()
    _parse_date(positive_at, "positive_at")

    zone_data = entity["data"]
    facilities = [dict(item) for item in zone_data.get("facilities", [])]
    if facility_id not in zone_member_ids(zone_data):
        facilities.append(
            {"facility_id": facility_id, "reason": data.get("reason") or "newly positive facility"}
        )
    events = [dict(event) for event in zone_data.get("positive_events", [])]
    events.append({"facility_id": facility_id, "at": positive_at})
    return {
        "facilities": facilities,
        "positive_events": events,
        "last_reported_by": actor.user_id,
    }


def _validate_zone_lift(actor, entity, data, lookup):
    status = zone_release_status(entity["data"], data.get("as_of"))
    if not status["eligible"]:
        unmet = [item["detail"] for item in status["conditions"] if not item["met"]]
        raise ValidationError("zone cannot be lifted yet: " + "; ".join(unmet))
    return {"lifted_by": actor.user_id, "lifted_at": status["as_of"]}


def _validate_dispatch(actor, entity, data, lookup):
    consignment = dict(entity["data"])
    for key in ("origin_facility_id", "destination_facility_id"):
        if data.get(key):
            consignment[key] = data[key]
    origin = consignment.get("origin_facility_id")
    destination = consignment.get("destination_facility_id")
    if origin:
        active_zones = lookup("zone", "status", "active") or []
        for zone in active_zones:
            members = zone_member_ids(zone["data"])
            if origin in members and destination not in members:
                raise ValidationError(
                    "dispatch blocked by active zone %s: in-zone batches cannot leave the zone"
                    % zone["data"].get("code")
                )
    return {"dispatched_by": actor.user_id}


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


CUSTOM_CREATE = {'consignment': _validate_consignment, 'zone': _validate_zone}
CUSTOM_TRANSITIONS = {
    ('consignment', 'quarantine'): _validate_quarantine,
    ('consignment', 'release'): _validate_release,
    ('consignment', 'dispatch'): _validate_dispatch,
    ('zone', 'report_positive'): _validate_zone_report_positive,
    ('zone', 'lift'): _validate_zone_lift,
}


class RuleEngine:
    ALIASES = {
        'consignments': 'consignment',
        'facilities': 'facility',
        'zones': 'zone',
    }
    INITIAL_STATUS = {'consignment': 'declared', 'facility': 'registered', 'zone': 'active'}
    TRANSITIONS = {
        'consignment': {
            'inspect': (('declared',), 'inspected'),
            'quarantine': (('inspected',), 'quarantined'),
            'release': (('inspected',), 'released'),
            'dispatch': (('declared', 'released'), 'dispatched'),
            'destroy': (('quarantined',), 'destroyed'),
            'recheck': (('quarantined',), 'inspected'),
        },
        'facility': {'trace': (('registered',), 'traced')},
        'zone': {
            'report_positive': (('active',), 'active'),
            'lift': (('active',), 'lifted'),
        },
    }
    CREATE_REQUIRED = {
        'consignment': ('code', 'origin', 'destination'),
        'facility': ('name', 'address'),
        'zone': ('code', 'center_facility_id'),
    }
    ACTION_REQUIRED = {
        ('consignment', 'inspect'): ('inspector', 'inspection_result'),
        ('consignment', 'quarantine'): ('pest_found', 'sample_id'),
        ('consignment', 'release'): ('pest_found', 'treatment'),
        ('consignment', 'destroy'): ('method', 'witnessed_by'),
        ('consignment', 'recheck'): ('sample_id',),
        ('consignment', 'dispatch'): (),
        ('facility', 'trace'): ('consignment_ids',),
        ('zone', 'report_positive'): ('facility_id',),
        ('zone', 'lift'): (),
    }
    CREATE_ROLES = {
        'consignment': ('admin', 'inspector'),
        'facility': ('admin', 'quarantine'),
        'zone': ('admin', 'inspector', 'quarantine'),
    }
    ROLE_ACTIONS = {
        'inspect': ('admin', 'inspector'),
        'quarantine': ('admin', 'quarantine'),
        'release': ('admin', 'quarantine'),
        'dispatch': ('admin', 'quarantine'),
        'destroy': ('admin', 'quarantine'),
        'recheck': ('admin', 'inspector'),
        'trace': ('admin', 'quarantine'),
        ('zone', 'report_positive'): ('admin', 'inspector', 'quarantine'),
        ('zone', 'lift'): ('admin', 'inspector', 'quarantine'),
    }

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
