"""Read-only lookup over an immutable snapshot, with no filesystem/device access."""

from typing import Protocol

from jane.core.contracts import InspectArguments, Observation


class ReadOnlyTool(Protocol):
    def inspect(self, arguments: InspectArguments) -> Observation: ...


class MockInspector:
    def __init__(self, observations: tuple[Observation, ...]) -> None:
        self._observations = {item.object_id: item for item in observations}
        if len(self._observations) != len(observations):
            raise ValueError("duplicate mock object ID")

    def inspect(self, arguments: InspectArguments) -> Observation:
        try:
            return self._observations[arguments.object_id]
        except KeyError:
            raise ValueError(f"unknown mock object: {arguments.object_id}") from None


class RegisteredMockInspector(MockInspector):
    """Registry wrapper for the original immutable read-only fixture tool."""

    from jane.actions.registry import SPECS

    spec = SPECS["mock.inspect"]

    def execute(self, key: str, arguments: InspectArguments) -> Observation:
        from jane.actions.registry import DefiniteFailure

        try:
            return self.inspect(arguments)
        except ValueError as error:
            raise DefiniteFailure(str(error)) from error

    def reconcile(self, key: str, arguments: InspectArguments) -> Observation:
        return self.execute(key, arguments)

    def verify(self, key: str, arguments: InspectArguments, result: Observation) -> bool:
        return self.inspect(arguments) == result
