"""Internal capability registry: what @fin.step/@fin.command actually
register, and how a job gets dispatched to the right handler."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

from soarca_fin.context import CommandContext, StepContext
from soarca_fin.models import Capability

StepHandler = Callable[[StepContext], "Any | Awaitable[Any]"]
CommandHandler = Callable[[CommandContext], "Any | Awaitable[Any]"]


class HandlerKind(Enum):
    STEP = auto()
    COMMAND = auto()


@dataclass(slots=True)
class HandlerSpec:
    capability_type: str
    kind: HandlerKind
    func: Callable[..., Any]
    description: str | None
    version: str | None
    examples: list[Mapping[str, Any]] | None

    def to_capability(self) -> Capability:
        description = self.description
        if description is None and self.func.__doc__:
            description = inspect.cleandoc(self.func.__doc__).splitlines()[0]
        return Capability(
            type=self.capability_type,
            description=description,
            version=self.version,
            step_examples=[dict(example) for example in self.examples] if self.examples else None,
        )


async def call_handler[T](func: Callable[..., T | Awaitable[T]], *args: Any) -> T:
    """Calls ``func`` with ``args``, transparently supporting both ``async
    def`` and plain ``def`` handlers. Sync handlers run in a worker thread
    so a slow/blocking one (e.g. using a blocking HTTP/SSH library) doesn't
    stall the whole Fin process's event loop."""
    if inspect.iscoroutinefunction(func):
        return await func(*args)  # type: ignore[no-any-return]
    result = await asyncio.to_thread(func, *args)
    if inspect.isawaitable(result):
        # A sync function that itself returns an awaitable (e.g. schedules
        # a coroutine without awaiting it) - support it, but this is an
        # unusual pattern; most sync handlers just return a plain value.
        return await result
    return result
