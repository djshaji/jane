"""Explicit offline tool contracts. Unknown tools are never dynamically imported."""

from typing import Literal, Protocol

from jane.core.contracts import Contract, InspectArguments, Observation


class ToolSpec(Contract):
    name: Literal["mock.inspect", "sim.inspect"]
    permission: str
    side_effect: Literal["read_only", "simulated_write"]
    idempotent: bool
    reconciliation: Literal["repeat_read", "lookup_key"]
    max_attempts: int = 3

    def resource(self, arguments: InspectArguments) -> str:
        return f"{self.name}:{arguments.object_id}"


SPECS = {
    "mock.inspect": ToolSpec(
        name="mock.inspect",
        permission="mock.inspect",
        side_effect="read_only",
        idempotent=True,
        reconciliation="repeat_read",
    ),
    "sim.inspect": ToolSpec(
        name="sim.inspect",
        permission="sim.inspect",
        side_effect="simulated_write",
        idempotent=True,
        reconciliation="lookup_key",
    ),
}


class DefiniteFailure(Exception):
    """The tool confirms no successful effect occurred."""


class Tool(Protocol):
    spec: ToolSpec

    def execute(self, key: str, arguments: InspectArguments) -> Observation: ...

    def reconcile(self, key: str, arguments: InspectArguments) -> Observation | None: ...

    def verify(self, key: str, arguments: InspectArguments, result: Observation) -> bool: ...


class Registry:
    def __init__(self, tools: tuple[Tool, ...]) -> None:
        self.tools: dict[str, Tool] = {}
        for tool in tools:
            if tool.spec != SPECS.get(tool.spec.name) or tool.spec.name in self.tools:
                raise ValueError("unknown, changed or duplicate tool contract")
            self.tools[tool.spec.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self.tools:
            raise ValueError("tool not registered")
        return self.tools[name]

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.tools))
