from __future__ import annotations
from typing import Union
from pydantic import BaseModel
import yaml
import structlog

logger = structlog.get_logger()


class ACLRule(BaseModel):
    domain: str                         # "*" = any domain
    entities: Union[str, list[str]]     # "*" = all entities, or explicit list
    services: Union[str, list[str]]     # "*" = all services, or explicit list


class ACLConfig(BaseModel):
    version: int = 1
    rules: list[ACLRule]


class ACLManager:
    """
    Checks whether a domain/service/entity_id combination is permitted.
    Loaded once at startup from config/acl.yaml.
    domain: "*", entities: "*", services: "*" grants unrestricted access.
    """

    def __init__(self, config: ACLConfig) -> None:
        self._config = config
        logger.info("acl.loaded", rule_count=len(config.rules),
                    domains=self.get_allowed_domains())

    def is_allowed(self, domain: str, service: str, entity_id: str) -> bool:
        for rule in self._config.rules:
            # Domain check — "*" matches any domain
            if rule.domain != "*" and rule.domain != domain:
                continue
            # Service check — "*" (string) or ["*"] (list) matches any service
            if rule.services != "*":
                svc_list = rule.services if isinstance(rule.services, list) else [rule.services]
                if "*" not in svc_list and service not in svc_list:
                    continue
            # Entity check — "*" matches any entity
            if rule.entities != "*":
                if isinstance(rule.entities, list) and "*" not in rule.entities and entity_id not in rule.entities:
                    continue
            return True
        return False

    def get_allowed_domains(self) -> list[str]:
        domains = {rule.domain for rule in self._config.rules}
        return ["*ALL*"] if "*" in domains else sorted(domains)

    def deny_reason(self, domain: str, service: str, entity_id: str) -> str:
        domain_rules = [r for r in self._config.rules if r.domain in (domain, "*")]
        if not domain_rules:
            return f"Domain '{domain}' is not in the allowed list"
        service_rules = [
            r for r in domain_rules
            if r.services == "*" or (isinstance(r.services, list) and (service in r.services or "*" in r.services))
        ]
        if not service_rules:
            return f"Service '{domain}.{service}' is not permitted"
        return f"Entity '{entity_id}' is not in the approved list for {domain}.{service}"

    @classmethod
    def from_yaml(cls, path: str) -> ACLManager:
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(ACLConfig(**data))

    @classmethod
    def from_yaml_safe(cls, path: str) -> ACLManager | None:
        """Returns None on missing file; logs a warning."""
        try:
            return cls.from_yaml(path)
        except FileNotFoundError:
            logger.warning("acl.file_not_found", path=path)
            return None
        except Exception as exc:
            logger.error("acl.load_error", path=path, error=str(exc))
            return None
