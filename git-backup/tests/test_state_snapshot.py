import importlib.util
import pathlib
import unittest

MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "rootfs" / "state_snapshot.py"
spec = importlib.util.spec_from_file_location("ha_state_snapshot", MODULE_PATH)
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)


def entity(entity_id, state="on", brightness=128):
    return {
        "entity_id": entity_id,
        "state": state,
        "attributes": {"friendly_name": "Commande V3V", "brightness": brightness},
    }


class V3VExportTests(unittest.TestCase):
    def test_only_v3v_light_is_exported(self):
        self.assertTrue(snapshot.is_safe_entity(entity("light.chaufferie_commande_v3v")))
        self.assertFalse(snapshot.is_safe_entity(entity("light.chambre_issam")))
    
    def test_v3v_brightness_is_exported_as_percentage(self):
        data = snapshot.sanitize_entity(entity("light.chaufferie_commande_v3v", "on", 128))
        self.assertEqual(data["attributes"]["brightness"], 128)
        self.assertEqual(data["attributes"]["brightness_pct"], 50.2)
    
    def test_off_means_zero_even_if_last_brightness_is_present(self):
        data = snapshot.sanitize_entity(entity("light.chaufferie_commande_v3v", "off", 128))
        self.assertEqual(data["attributes"]["brightness_pct"], 0)
    
    def test_unknown_brightness_is_not_guessed(self):
        data = snapshot.sanitize_entity(entity("light.chaufferie_commande_v3v", "on", None))
        self.assertNotIn("brightness_pct", data["attributes"])
    
    def test_position_change_is_visible_in_diagnostic_diff(self):
        old = snapshot.sanitize_entity(entity("light.chaufferie_commande_v3v", "on", 100))
        new = snapshot.sanitize_entity(entity("light.chaufferie_commande_v3v", "on", 128))
        changes = snapshot.build_changes({"entities": [old]}, [new], "2026-10-25T12:00:00Z")
        self.assertEqual(changes["summary"]["meaningful_state_change_count"], 1)
        change = changes["meaningful_state_changes"][0]
        self.assertIn("brightness_pct", change["attribute_changes"])


if __name__ == "__main__":
    unittest.main()
