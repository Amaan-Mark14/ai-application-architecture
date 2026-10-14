"""Workflow graph, step definitions, transition rules, and startup graph validation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from contracts.workflow import (
    WorkflowDefinitionRef,
    WorkflowStatus,
)

from .errors import InvalidTransitionError, WorkflowDefinitionError
from .state import StepOutcome, WorkflowState

StepHandler = Callable[[WorkflowState], StepOutcome | Awaitable[StepOutcome]]


@dataclass(frozen=True)
class TransitionRule:
    """Conditional transition rule evaluating whether a step outcome transitions to target_step."""

    target_step: str
    condition_fn: Callable[[WorkflowState, StepOutcome], bool]
    description: str = ""


@dataclass(frozen=True)
class StepDefinition:
    """Declared workflow step node with its execution handler and outgoing transitions."""

    name: str
    handler: StepHandler
    transitions: Sequence[TransitionRule] = field(default_factory=tuple)
    default_transition: str | None = None
    is_terminal: bool = False
    terminal_status: WorkflowStatus | None = None
    is_mutating: bool = False
    max_retries: int = 0
    supports_recovery: bool = False

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("Step name cannot be empty")
        if self.is_terminal:
            if not self.terminal_status or not self.terminal_status.is_terminal:
                raise ValueError(
                    f"Terminal step '{self.name}' must specify a valid terminal status "
                    f"(COMPLETED, FAILED, REJECTED, ESCALATED)."
                )
            if self.transitions or self.default_transition:
                raise ValueError(f"Terminal step '{self.name}' cannot have outgoing transitions.")


class WorkflowDefinition:
    """Deterministic, code-defined directed workflow graph."""

    def __init__(
        self,
        definition_id: str,
        version: str,
        start_step: str,
        steps: Sequence[StepDefinition],
        max_transitions: int = 15,
    ) -> None:
        self.definition_ref = WorkflowDefinitionRef(definition_id=definition_id, version=version)
        self.start_step = start_step
        self.steps: dict[str, StepDefinition] = {s.name: s for s in steps}
        self.max_transitions = max_transitions
        self.validate_graph()

    @property
    def definition_id(self) -> str:
        return self.definition_ref.definition_id

    @property
    def version(self) -> str:
        return self.definition_ref.version

    def validate_graph(self) -> None:
        """Validate structural integrity, reachability, and absence of dangling transitions."""
        if not self.steps:
            raise WorkflowDefinitionError("Workflow definition must declare at least one step.")

        if self.start_step not in self.steps:
            raise WorkflowDefinitionError(
                f"Start step '{self.start_step}' is not declared in registered steps: {list(self.steps.keys())}."
            )

        has_terminal = False
        for name, step in self.steps.items():
            if step.is_terminal:
                has_terminal = True
                continue

            # Verify target steps exist
            for rule in step.transitions:
                if rule.target_step not in self.steps:
                    raise WorkflowDefinitionError(
                        f"Step '{name}' declares transition to unknown target step '{rule.target_step}'."
                    )

            if step.default_transition:
                if step.default_transition not in self.steps:
                    raise WorkflowDefinitionError(
                        f"Step '{name}' declares unknown default transition '{step.default_transition}'."
                    )
            elif not step.transitions:
                raise WorkflowDefinitionError(
                    f"Non-terminal step '{name}' must have at least one transition rule or default_transition."
                )

        if not has_terminal:
            raise WorkflowDefinitionError(
                "Workflow definition must declare at least one terminal step."
            )

    def resolve_next_step(
        self,
        state: WorkflowState,
        outcome: StepOutcome,
    ) -> tuple[str, WorkflowStatus | None]:
        """Evaluate declared transition rules against step outcome to determine next step and status."""
        current_step_def = self.steps.get(state.current_step)
        if not current_step_def:
            raise InvalidTransitionError(
                f"Current step '{state.current_step}' is not in workflow definition."
            )

        if current_step_def.is_terminal:
            raise InvalidTransitionError(
                f"Cannot transition from terminal step '{current_step_def.name}' (status: {current_step_def.terminal_status})."
            )

        # Check explicit transition rules in order
        for rule in current_step_def.transitions:
            try:
                if rule.condition_fn(state, outcome):
                    target_def = self.steps[rule.target_step]
                    return target_def.name, target_def.terminal_status
            except Exception as exc:
                raise InvalidTransitionError(
                    f"Error evaluating transition rule for step '{current_step_def.name}': {exc}"
                ) from exc

        # Fallback to default transition
        if current_step_def.default_transition:
            target_def = self.steps[current_step_def.default_transition]
            return target_def.name, target_def.terminal_status

        raise InvalidTransitionError(
            f"Step '{current_step_def.name}' produced outcome '{outcome.outcome_type}' (status: {outcome.status.value}), "
            "but no matching transition rule or default transition was found."
        )
