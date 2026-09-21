#!/usr/bin/env python3
"""Generate compact, privacy-filtered Home Assistant diagnostic files."""

import glob
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone

API_URL = "http://supervisor/core/api/states"
HA_CONFIG = "/config"

SAFE_DOMAINS = {
    "automation",
    "climate",
    "fan",
    "input_boolean",
    "input_datetime",
    "input_number",
    "input_select",
    "number",
    "schedule",
    "script",
    "select",
    "switch",
    "timer",
    "water_heater",
}

SAFE_SENSOR_CLASSES = {
    "temperature",
    "humidity",
    "pressure",
    "power",
    "energy",
    "current",
    "voltage",
    "battery",
    "frequency",
    "duration",
    "illuminance",
    "moisture",
    "carbon_dioxide",
    "volatile_organic_compounds",
    "volatile_organic_compounds_parts",
    "pm1",
    "pm10",
    "pm25",
    "signal_strength",
}

SAFE_BINARY_CLASSES = {
    "cold",
    "heat",
    "moisture",
    "problem",
    "running",
    "connectivity",
    "battery",
    "power",
}

DIAGNOSTIC_KEYWORDS = {
    "temp",
    "temperature",
    "humid",
    "chauff",
    "heating",
    "heat",
    "froid",
    "cooling",
    "cool",
    "pac",
    "thermostat",
    "poele",
    "plancher",
    "circuit",
    "circulateur",
    "pump",
    "pompe",
    "vanne",
    "valve",
    "flow",
    "debit",
    "pression",
    "pressure",
    "power",
    "puissance",
    "energy",
    "energie",
    "consommation",
}

SAFE_ATTRIBUTES = {
    "friendly_name",
    "unit_of_measurement",
    "device_class",
    "state_class",
    "current_temperature",
    "temperature",
    "target_temp_high",
    "target_temp_low",
    "hvac_action",
    "hvac_modes",
    "preset_mode",
    "preset_modes",
    "fan_mode",
    "fan_modes",
    "percentage",
    "current_position",
    "min",
    "max",
    "step",
    "mode",
    "last_triggered",
    "current",
    "supported_features",
    "remaining",
    "duration",
    "editable",
}

IMPORTANT_ATTRIBUTE_KEYS = {
    "current_temperature",
    "temperature",
    "target_temp_high",
    "target_temp_low",
    "hvac_action",
    "preset_mode",
    "fan_mode",
    "percentage",
    "current_position",
}

NUMERIC_CHANGE_THRESHOLDS = {
    "temperature": 0.5,
    "humidity": 2.0,
    "pressure": 2.0,
    "power": 50.0,
    "energy": 0.5,
    "current": 0.5,
    "voltage": 5.0,
    "battery": 5.0,
    "frequency": 0.2,
    "illuminance": 100.0,
    "moisture": 2.0,
    "carbon_dioxide": 100.0,
    "volatile_organic_compounds": 50.0,
    "volatile_organic_compounds_parts": 50.0,
    "pm1": 5.0,
    "pm10": 5.0,
    "pm25": 5.0,
    "signal_strength": 5.0,
}

PROBLEM_STATES = {"unavailable", "unknown"}


def normalize(value):
    value = str(value or "").lower()
    value = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in value if not unicodedata.combining(ch))


def is_safe_entity(entity):
    entity_id = entity.get("entity_id", "")
    domain = entity_id.split(".", 1)[0]
    attrs = entity.get("attributes") or {}

    if domain in SAFE_DOMAINS:
        return True

    if domain == "sensor":
        device_class = str(attrs.get("device_class") or "")
        if device_class in SAFE_SENSOR_CLASSES:
            return True

        searchable = normalize(
            entity_id + " " + str(attrs.get("friendly_name") or "")
        )
        return any(keyword in searchable for keyword in DIAGNOSTIC_KEYWORDS)

    if domain == "binary_sensor":
        return str(attrs.get("device_class") or "") in SAFE_BINARY_CLASSES

    return False


def sanitize_entity(entity):
    attrs = entity.get("attributes") or {}
    safe_attrs = {
        key: value
        for key, value in attrs.items()
        if key in SAFE_ATTRIBUTES
    }

    item = {
        "entity_id": entity.get("entity_id"),
        "state": entity.get("state"),
        "last_changed": entity.get("last_changed"),
        "last_updated": entity.get("last_updated"),
    }

    if safe_attrs:
        item["attributes"] = safe_attrs

    return item


def read_ha_version():
    try:
        with open(os.path.join(HA_CONFIG, ".HA_VERSION"), "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return None


def custom_component_inventory():
    """Return only public metadata useful for troubleshooting custom integrations."""
    items = []

    for manifest_path in sorted(
        glob.glob(os.path.join(HA_CONFIG, "custom_components", "*", "manifest.json"))
    ):
        domain = os.path.basename(os.path.dirname(manifest_path))
        item = {"domain": domain}

        try:
            with open(manifest_path, "r", encoding="utf-8") as handle:
                manifest = json.load(handle)
            if manifest.get("name"):
                item["name"] = manifest["name"]
            if manifest.get("version"):
                item["version"] = manifest["version"]
        except (OSError, ValueError, TypeError):
            item["metadata"] = "unavailable"

        items.append(item)

    return items


def parse_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def hours_since(value, now):
    stamp = parse_timestamp(value)
    if not stamp:
        return None
    return max(0.0, (now - stamp).total_seconds() / 3600.0)


def friendly_item(entity):
    attrs = entity.get("attributes") or {}
    item = {
        "entity_id": entity.get("entity_id"),
        "state": str(entity.get("state") or ""),
    }
    if attrs.get("friendly_name"):
        item["friendly_name"] = attrs["friendly_name"]
    return item


def problem_group_label(entity):
    """Best-effort grouping label for multiple broken entities from one device."""
    attrs = entity.get("attributes") or {}
    friendly = str(attrs.get("friendly_name") or "").strip()

    if friendly:
        cleaned = re.sub(
            r"\s+(temperature|humidity|battery|batterie|power|puissance|voltage|"
            r"tension|current|courant|pressure|pression|energy|energie|signal"
            r"|linkquality|lqi|rssi)$",
            "",
            friendly,
            flags=re.IGNORECASE,
        ).strip()
        if cleaned:
            return cleaned

    entity_id = str(entity.get("entity_id") or "")
    object_id = entity_id.split(".", 1)[-1]
    object_id = re.sub(
        r"_(temperature|humidity|battery|power|voltage|current|pressure|energy|"
        r"signal_strength|linkquality|lqi|rssi)$",
        "",
        object_id,
    )
    object_id = re.sub(r"_[1-9]$", "", object_id)
    return object_id or entity_id


def build_problem_groups(problem_entities):
    groups = defaultdict(list)
    for entity in problem_entities:
        groups[problem_group_label(entity)].append(entity)

    result = []
    for label, members in groups.items():
        if len(members) < 2:
            continue
        states = Counter(str(item.get("state") or "") for item in members)
        result.append(
            {
                "device": label,
                "problem_entity_count": len(members),
                "states": dict(states),
                "entities": [
                    {
                        "entity_id": item.get("entity_id"),
                        "state": str(item.get("state") or ""),
                    }
                    for item in members[:12]
                ],
            }
        )

    result.sort(key=lambda item: (-item["problem_entity_count"], item["device"]))
    return result[:30]


def build_health(raw_entities, generated_at):
    now = datetime.now(timezone.utc)
    unavailable = []
    unknown = []
    low_batteries = []
    not_updated_24h = []
    automations_24h = []

    for entity in raw_entities:
        entity_id = entity.get("entity_id", "")
        domain = entity_id.split(".", 1)[0]
        state = str(entity.get("state") or "")
        attrs = entity.get("attributes") or {}
        item = friendly_item(entity)

        if state == "unavailable":
            unavailable.append(item)
        elif state == "unknown":
            unknown.append(item)

        device_class = str(attrs.get("device_class") or "")
        if device_class == "battery":
            is_low = False
            if domain == "binary_sensor":
                is_low = state == "on"
            elif domain == "sensor":
                try:
                    is_low = float(state) <= 20
                except (TypeError, ValueError):
                    pass
            if is_low:
                battery_item = dict(item)
                battery_item["unit"] = attrs.get("unit_of_measurement")
                low_batteries.append(battery_item)

        if (
            domain in {"sensor", "binary_sensor"}
            and state not in PROBLEM_STATES
        ):
            age_hours = hours_since(entity.get("last_updated"), now)
            if age_hours is not None and age_hours >= 24:
                old_item = dict(item)
                old_item["last_updated"] = entity.get("last_updated")
                old_item["hours_without_state_update"] = round(age_hours, 1)
                not_updated_24h.append(old_item)

        if domain == "automation" and attrs.get("last_triggered"):
            age_hours = hours_since(attrs.get("last_triggered"), now)
            if age_hours is not None and age_hours <= 24:
                automations_24h.append(
                    {
                        "entity_id": entity_id,
                        "friendly_name": attrs.get("friendly_name"),
                        "last_triggered": attrs.get("last_triggered"),
                        "state": state,
                    }
                )

    automations_24h.sort(
        key=lambda item: str(item.get("last_triggered") or ""), reverse=True
    )
    not_updated_24h.sort(
        key=lambda item: item.get("hours_without_state_update", 0), reverse=True
    )

    problem_entities = [
        entity
        for entity in raw_entities
        if str(entity.get("state") or "") in PROBLEM_STATES
    ]

    return {
        "_meta": {
            "generated_at": generated_at,
            "home_assistant_version": read_ha_version(),
            "privacy": "Summary built only from the filtered diagnostic entity set",
            "note": (
                "not_updated_24h is informational: an unchanged entity can be healthy. "
                "Unavailable and unknown states are listed separately."
            ),
        },
        "summary": {
            "filtered_entity_count": len(raw_entities),
            "unavailable_count": len(unavailable),
            "unknown_count": len(unknown),
            "problem_device_group_count": len(build_problem_groups(problem_entities)),
            "low_battery_count": len(low_batteries),
            "not_updated_24h_count": len(not_updated_24h),
            "automations_triggered_24h_count": len(automations_24h),
        },
        "problem_device_groups": build_problem_groups(problem_entities),
        "unavailable": unavailable[:60],
        "unknown": unknown[:60],
        "low_batteries": low_batteries[:50],
        "not_updated_24h": not_updated_24h[:50],
        "automations_triggered_24h": automations_24h[:30],
    }


def load_previous_snapshot(path):
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and isinstance(data.get("entities"), list):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return None


def to_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def numeric_change_is_meaningful(previous, current, device_class):
    old = to_number(previous)
    new = to_number(current)
    if old is None or new is None:
        return False, None

    delta = new - old
    threshold = NUMERIC_CHANGE_THRESHOLDS.get(device_class, 1.0)
    return abs(delta) >= threshold, round(delta, 3)


def build_changes(previous_snapshot, current_entities, generated_at):
    current_map = {
        item.get("entity_id"): item
        for item in current_entities
        if item.get("entity_id")
    }

    if not previous_snapshot:
        return {
            "_meta": {
                "generated_at": generated_at,
                "baseline": True,
                "note": "No previous diagnostic snapshot was available for comparison.",
            },
            "summary": {
                "current_entity_count": len(current_map),
                "added_count": 0,
                "removed_count": 0,
                "became_unavailable_count": 0,
                "recovered_count": 0,
                "meaningful_state_change_count": 0,
                "automation_trigger_count": 0,
            },
            "added": [],
            "removed": [],
            "became_unavailable": [],
            "recovered": [],
            "meaningful_state_changes": [],
            "automations_triggered_since_previous_snapshot": [],
        }

    previous_entities = previous_snapshot.get("entities") or []
    previous_map = {
        item.get("entity_id"): item
        for item in previous_entities
        if item.get("entity_id")
    }

    added = sorted(set(current_map) - set(previous_map))
    removed = sorted(set(previous_map) - set(current_map))
    became_unavailable = []
    recovered = []
    state_changes = []
    automation_triggers = []

    for entity_id in sorted(set(current_map) & set(previous_map)):
        old = previous_map[entity_id]
        new = current_map[entity_id]
        old_state = str(old.get("state") or "")
        new_state = str(new.get("state") or "")
        domain = entity_id.split(".", 1)[0]
        attrs = new.get("attributes") or {}
        old_attrs = old.get("attributes") or {}
        friendly_name = attrs.get("friendly_name") or old_attrs.get("friendly_name")

        if old_state not in PROBLEM_STATES and new_state in PROBLEM_STATES:
            item = {
                "entity_id": entity_id,
                "from": old_state,
                "to": new_state,
            }
            if friendly_name:
                item["friendly_name"] = friendly_name
            became_unavailable.append(item)
            continue

        if old_state in PROBLEM_STATES and new_state not in PROBLEM_STATES:
            item = {
                "entity_id": entity_id,
                "from": old_state,
                "to": new_state,
            }
            if friendly_name:
                item["friendly_name"] = friendly_name
            recovered.append(item)

        if domain == "automation":
            previous_trigger = old_attrs.get("last_triggered")
            current_trigger = attrs.get("last_triggered")
            if current_trigger and current_trigger != previous_trigger:
                item = {
                    "entity_id": entity_id,
                    "last_triggered": current_trigger,
                }
                if friendly_name:
                    item["friendly_name"] = friendly_name
                automation_triggers.append(item)

        if old_state == new_state:
            attribute_changes = {}
            for key in IMPORTANT_ATTRIBUTE_KEYS:
                if old_attrs.get(key) != attrs.get(key):
                    attribute_changes[key] = {
                        "from": old_attrs.get(key),
                        "to": attrs.get(key),
                    }
            if attribute_changes:
                item = {
                    "entity_id": entity_id,
                    "state": new_state,
                    "attribute_changes": attribute_changes,
                }
                if friendly_name:
                    item["friendly_name"] = friendly_name
                state_changes.append(item)
            continue

        if old_state in PROBLEM_STATES or new_state in PROBLEM_STATES:
            continue

        meaningful = True
        delta = None
        if domain == "sensor":
            meaningful, delta = numeric_change_is_meaningful(
                old_state,
                new_state,
                str(attrs.get("device_class") or ""),
            )

        if meaningful:
            item = {
                "entity_id": entity_id,
                "from": old_state,
                "to": new_state,
            }
            if delta is not None:
                item["delta"] = delta
            if attrs.get("unit_of_measurement"):
                item["unit"] = attrs.get("unit_of_measurement")
            if friendly_name:
                item["friendly_name"] = friendly_name
            state_changes.append(item)

    previous_generated_at = (previous_snapshot.get("_meta") or {}).get("generated_at")

    return {
        "_meta": {
            "generated_at": generated_at,
            "previous_generated_at": previous_generated_at,
            "baseline": False,
            "note": (
                "Sensor numeric changes are filtered using diagnostic thresholds "
                "to avoid noisy snapshots."
            ),
        },
        "summary": {
            "current_entity_count": len(current_map),
            "added_count": len(added),
            "removed_count": len(removed),
            "became_unavailable_count": len(became_unavailable),
            "recovered_count": len(recovered),
            "meaningful_state_change_count": len(state_changes),
            "automation_trigger_count": len(automation_triggers),
        },
        "added": added[:50],
        "removed": removed[:50],
        "became_unavailable": became_unavailable[:60],
        "recovered": recovered[:60],
        "meaningful_state_changes": state_changes[:100],
        "automations_triggered_since_previous_snapshot": automation_triggers[:50],
    }


def write_json_yaml(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def main():
    if len(sys.argv) != 5:
        print(
            "Usage: state_snapshot.py PREVIOUS_STATES STATES_OUTPUT HEALTH_OUTPUT CHANGES_OUTPUT",
            file=sys.stderr,
        )
        return 2

    previous_path = sys.argv[1]
    states_output = sys.argv[2]
    health_output = sys.argv[3]
    changes_output = sys.argv[4]

    previous_snapshot = load_previous_snapshot(previous_path)

    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        print("SUPERVISOR_TOKEN is not available", file=sys.stderr)
        return 1

    request = urllib.request.Request(
        API_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            states = json.load(response)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
        print(f"Unable to read Home Assistant states: {exc}", file=sys.stderr)
        return 1

    raw_entities = [entity for entity in states if is_safe_entity(entity)]
    entities = [sanitize_entity(entity) for entity in raw_entities]
    entities.sort(key=lambda item: item.get("entity_id") or "")

    generated_at = datetime.now(timezone.utc).isoformat()
    snapshot = {
        "_meta": {
            "generated_at": generated_at,
            "source": "Home Assistant Core API via Supervisor",
            "privacy": "Filtered diagnostic snapshot; sensitive domains and attributes omitted",
            "home_assistant_version": read_ha_version(),
            "entity_count": len(entities),
            "custom_components": custom_component_inventory(),
        },
        "entities": entities,
    }

    health = build_health(raw_entities, generated_at)
    changes = build_changes(previous_snapshot, entities, generated_at)

    write_json_yaml(states_output, snapshot)
    write_json_yaml(health_output, health)
    write_json_yaml(changes_output, changes)

    print(
        f"Wrote {len(entities)} filtered entities, health summary and change summary"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
