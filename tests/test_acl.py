"""Phase 2 — ACL unit tests. No network required."""
import pytest
from avatar_backend.models.acl import ACLConfig, ACLRule, ACLManager


@pytest.fixture
def acl() -> ACLManager:
    config = ACLConfig(
        version=1,
        rules=[
            ACLRule(domain="light",  entities="*",
                    services=["turn_on", "turn_off", "toggle"]),
            ACLRule(domain="switch", entities=["switch.garden_pump"],
                    services=["turn_on", "turn_off"]),
            ACLRule(domain="lock",   entities="*",
                    services=[]),   # explicitly empty — nothing allowed
        ],
    )
    return ACLManager(config)


def test_wildcard_entity_allowed(acl):
    assert acl.is_allowed("light", "turn_on", "light.kitchen") is True


def test_wildcard_entity_any_id_allowed(acl):
    assert acl.is_allowed("light", "toggle", "light.bedroom_ceiling") is True


def test_unlisted_service_denied(acl):
    assert acl.is_allowed("light", "set_color", "light.kitchen") is False


def test_specific_entity_allowed(acl):
    assert acl.is_allowed("switch", "turn_on", "switch.garden_pump") is True


def test_unlisted_entity_denied(acl):
    assert acl.is_allowed("switch", "turn_on", "switch.living_room_fan") is False


def test_unlisted_domain_denied(acl):
    assert acl.is_allowed("lock", "unlock", "lock.front_door") is False


def test_empty_services_denied(acl):
    # domain exists but services list is empty
    assert acl.is_allowed("lock", "turn_on", "lock.front_door") is False


def test_completely_unknown_domain_denied(acl):
    assert acl.is_allowed("alarm_control_panel", "disarm", "alarm.home") is False


def test_deny_reason_unknown_domain(acl):
    reason = acl.deny_reason("alarm_control_panel", "disarm", "alarm.home")
    assert "alarm_control_panel" in reason


def test_deny_reason_bad_service(acl):
    reason = acl.deny_reason("light", "set_color", "light.kitchen")
    assert "set_color" in reason


def test_deny_reason_bad_entity(acl):
    reason = acl.deny_reason("switch", "turn_on", "switch.living_room_fan")
    assert "switch.living_room_fan" in reason


def test_allowed_domains_list(acl):
    domains = acl.get_allowed_domains()
    assert "light" in domains
    assert "switch" in domains
