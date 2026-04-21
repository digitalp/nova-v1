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


# ── Edge cases added for 30-day plan priority 2 ──────────────────────────────

def test_wildcard_domain_matches_any_domain():
    """A rule with domain='*' should permit any domain."""
    from avatar_backend.models.acl import ACLConfig, ACLRule, ACLManager
    mgr = ACLManager(ACLConfig(version=1, rules=[
        ACLRule(domain="*", entities="*", services=["turn_on"]),
    ]))
    assert mgr.is_allowed("light", "turn_on", "light.kitchen") is True
    assert mgr.is_allowed("climate", "turn_on", "climate.living_room") is True
    assert mgr.is_allowed("lock", "turn_on", "lock.front") is True


def test_wildcard_service_string_allows_any_service():
    """services='*' (plain string) should allow any service on that domain."""
    from avatar_backend.models.acl import ACLConfig, ACLRule, ACLManager
    mgr = ACLManager(ACLConfig(version=1, rules=[
        ACLRule(domain="climate", entities="*", services="*"),
    ]))
    assert mgr.is_allowed("climate", "set_temperature", "climate.bedroom") is True
    assert mgr.is_allowed("climate", "turn_off", "climate.bedroom") is True
    assert mgr.is_allowed("climate", "anything_at_all", "climate.bedroom") is True


def test_wildcard_service_in_list_allows_any_service():
    """services=['*'] should allow any service the same as services='*'."""
    from avatar_backend.models.acl import ACLConfig, ACLRule, ACLManager
    mgr = ACLManager(ACLConfig(version=1, rules=[
        ACLRule(domain="media_player", entities="*", services=["*"]),
    ]))
    assert mgr.is_allowed("media_player", "play_media", "media_player.lounge") is True
    assert mgr.is_allowed("media_player", "volume_set", "media_player.lounge") is True


def test_wildcard_entity_in_list_allows_any_entity():
    """entities=['*'] should allow any entity_id for that domain/service."""
    from avatar_backend.models.acl import ACLConfig, ACLRule, ACLManager
    mgr = ACLManager(ACLConfig(version=1, rules=[
        ACLRule(domain="cover", entities=["*"], services=["open_cover", "close_cover"]),
    ]))
    assert mgr.is_allowed("cover", "open_cover", "cover.garage") is True
    assert mgr.is_allowed("cover", "open_cover", "cover.bedroom_blind") is True


def test_multiple_rules_same_domain_or_semantics():
    """ACL rules are OR-combined: any matching rule grants access.
    A second broader rule for the same domain DOES expand what is allowed.
    """
    from avatar_backend.models.acl import ACLConfig, ACLRule, ACLManager
    # Two light rules: first permits turn_on only; second permits everything.
    # Because rules are OR'd, toggle (covered by rule 2) must be allowed.
    mgr = ACLManager(ACLConfig(version=1, rules=[
        ACLRule(domain="light", entities="*", services=["turn_on"]),
        ACLRule(domain="light", entities="*", services="*"),
    ]))
    assert mgr.is_allowed("light", "turn_on", "light.kitchen") is True
    assert mgr.is_allowed("light", "toggle", "light.kitchen") is True

    # Contrast: if there is ONLY the restrictive rule, toggle is denied.
    mgr2 = ACLManager(ACLConfig(version=1, rules=[
        ACLRule(domain="light", entities="*", services=["turn_on"]),
    ]))
    assert mgr2.is_allowed("light", "toggle", "light.kitchen") is False


def test_deny_reason_empty_services_list():
    """A domain with an empty services list should report service not permitted."""
    from avatar_backend.models.acl import ACLConfig, ACLRule, ACLManager
    mgr = ACLManager(ACLConfig(version=1, rules=[
        ACLRule(domain="lock", entities="*", services=[]),
    ]))
    reason = mgr.deny_reason("lock", "unlock", "lock.front_door")
    assert "unlock" in reason or "not permitted" in reason.lower()


def test_from_yaml_safe_missing_file_returns_none(tmp_path):
    """from_yaml_safe should return None instead of raising when file is absent."""
    from avatar_backend.models.acl import ACLManager
    result = ACLManager.from_yaml_safe(str(tmp_path / "nonexistent.yaml"))
    assert result is None


def test_allowed_domains_includes_wildcard_sentinel():
    """When a '*' domain rule exists, get_allowed_domains returns ['*ALL*']."""
    from avatar_backend.models.acl import ACLConfig, ACLRule, ACLManager
    mgr = ACLManager(ACLConfig(version=1, rules=[
        ACLRule(domain="*", entities="*", services="*"),
    ]))
    domains = mgr.get_allowed_domains()
    assert domains == ["*ALL*"]
