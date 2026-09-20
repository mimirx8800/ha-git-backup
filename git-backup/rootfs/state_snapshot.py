#!/usr/bin/env python3
"""Generate a privacy-filtered Home Assistant state snapshot for diagnostics."""

import json
import os
import sys
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone

API_URL = "http://supervisor/core/api/states"

SAFE_DOMAINS = {
    "climate",
    "switch",
    "fan",
    "number",
    "input_number",
    "select",
    "input_select",
    "input_boolean",
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


def main():
    if len(sys.argv) != 2:
        print("Usage: state_snapshot.py OUTPUT_PATH", file=sys.stderr)
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

    entities = [
        sanitize_entity(entity)
        for entity in states
        if is_safe_entity(entity)
    ]
    entities.sort(key=lambda item: item.get("entity_id") or "")

    snapshot = {
        "_meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "Home Assistant Core API via Supervisor",
            "privacy": "Filtered diagnostic snapshot; sensitive domains and attributes omitted",
            "entity_count": len(entities),
        },
        "entities": entities,
    }

    output_path = sys.argv[1]
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # JSON is valid YAML 1.2; using a .yaml extension keeps this file visible
    # while the config backup intentionally excludes ordinary *.json files.
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    print(f"Wrote {len(entities)} filtered entities to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
