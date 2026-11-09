"""Generic explicit registry used by deterministic model runtime components."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from scat_model_runtime.contracts import ComponentReference


class ComponentRegistryError(ValueError):
    """Base error for component registry failures safe to expose to orchestration."""


class DuplicateComponentError(ComponentRegistryError):
    pass


class ComponentNotFoundError(ComponentRegistryError):
    pass


class ComponentVersionRequiredError(ComponentRegistryError):
    pass


@dataclass(frozen=True, slots=True)
class ComponentRegistration[ComponentT]:
    reference: ComponentReference
    component: ComponentT
    is_default: bool = False


class ComponentRegistry[ComponentT]:
    """Version-aware allowlist; components cannot be selected by import path."""

    def __init__(
        self,
        component_kind: str,
        registrations: Iterable[ComponentRegistration[ComponentT]] = (),
    ) -> None:
        self._component_kind = component_kind
        self._registrations: dict[tuple[str, str], ComponentRegistration[ComponentT]] = {}
        for registration in registrations:
            self.register(registration)

    def register(self, registration: ComponentRegistration[ComponentT]) -> None:
        reference = registration.reference
        key = (reference.key, reference.version)
        if key in self._registrations:
            raise DuplicateComponentError(
                f"{self._component_kind} already registered: {reference.registry_id}"
            )
        if registration.is_default and any(
            current.reference.key == reference.key and current.is_default
            for current in self._registrations.values()
        ):
            raise DuplicateComponentError(
                f"default {self._component_kind} version already registered: {reference.key}"
            )
        self._registrations[key] = registration

    def get(self, key: str, version: str | None = None) -> ComponentRegistration[ComponentT]:
        if version is not None:
            registration = self._registrations.get((key, version))
            if registration is None:
                raise ComponentNotFoundError(
                    f"{self._component_kind} not registered: {key}:{version}"
                )
            return registration

        candidates = [
            registration
            for registration in self._registrations.values()
            if registration.reference.key == key
        ]
        if not candidates:
            raise ComponentNotFoundError(f"{self._component_kind} not registered: {key}")
        defaults = [registration for registration in candidates if registration.is_default]
        if len(defaults) == 1:
            return defaults[0]
        if len(candidates) == 1:
            return candidates[0]
        raise ComponentVersionRequiredError(
            f"{self._component_kind} version is required for key: {key}"
        )

    def resolve(self, reference: ComponentReference) -> ComponentT:
        return self.get(reference.key, reference.version).component

    def list(self) -> tuple[ComponentRegistration[ComponentT], ...]:
        return tuple(
            sorted(
                self._registrations.values(),
                key=lambda item: (item.reference.key, item.reference.version),
            )
        )


__all__ = [
    "ComponentNotFoundError",
    "ComponentRegistration",
    "ComponentRegistry",
    "ComponentRegistryError",
    "ComponentVersionRequiredError",
    "DuplicateComponentError",
]
