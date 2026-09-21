#!/usr/bin/env python3
"""Generate a compact, privacy-filtered Home Assistant diagnostic snapshot."""

import glob
import json
import os
import sys
import unicodedata
import urllib.error
import urllib.request
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
    """Return only public metadata useful for troubleshooting installed custom integrations."""
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


def build_health(raw_entities, generated_at):
    now = datetime.now(timezone.utc)
    unavailable = []
    low_batteries = []
    stale_sensors = []
    recent_automations = []

    for entity in raw_entities:
        entity_id = entity.get("entity_id", "")
        domain = entity_id.split(".", 1)[0]
        state = str(entity.get("state") or "")
        attrs = entity.get("attributes") or {}
        friendly_name = attrs.get("friendly_name")
        item = {"entity_id": entity_id, "state": state}
        if friendly_name:
            item["friendly_name"] = friendly_name

        if state in {"unavailable", "unknown"}:
            unavailable.append(item)

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

        if domain in {"sensor", "binary_sensor"}:
            updated = parse_timestamp(entity.get("last_updated"))
            if updated and (now - updated).total_seconds() >= 86400:
                stale_item = dict(item)
                stale_item["last_updated"] = entity.get("last_updated")
                stale_sensors.append(stale_item)

        if domain == "automation" and attrs.get("last_triggered"):
            recent_automations.append(
                {
                    "entity_id": entity_id,
                    "friendly_name": friendly_name,
                    "last_triggered": attrs.get("last_triggered"),
                    "state": state,
                }
            )

    recent_automations.sort(
        key=lambda item: str(item.get("last_triggered") or ""), reverse=True
    )

    return {
        "_meta": {
            "generated_at": generated_at,
            "home_assistant_version": read_ha_version(),
            "privacy": "Summary built only from the filtered diagnostic entity set",
        },
        "summary": {
            "filtered_entity_count": len(raw_entities),
            "unavailable_or_unknown_count": len(unavailable),
            "low_battery_count": len(low_batteries),
            "stale_sensor_count_24h": len(stale_sensors),
            "recent_automation_count": len(recent_automations),
        },
        "unavailable_or_unknown": unavailable[:50],
        "low_batteries": low_batteries[:50],
        "stale_sensors_24h": stale_sensors[:50],
        "recent_automations": recent_automations[:20],
    }


def write_json_yaml(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def main():
    if len(sys.argv) != 3:
        print("Usage: state_snapshot.py STATES_OUTPUT HEALTH_OUTPUT", file=sys.stderr)
        return 2

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

    states_output = sys.argv[1]
    health_output = sys.argv[2]
    health = build_health(raw_entities, generated_at)

    # JSON is valid YAML 1.2; the .yaml extension is intentional because ordinary
    # JSON files from /config are never backed up.
    write_json_yaml(states_output, snapshot)
    write_json_yaml(health_output, health)

    print(
        f"Wrote {len(entities)} filtered entities to {states_output} "
        f"and health summary to {health_output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
