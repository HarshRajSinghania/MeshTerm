"""Algorithm layer: pure logic for tracing, TX search, and path exploration.

Services may depend on :mod:`meshterm.core` (the :class:`~meshterm.core.connection.Device`
interface and the domain models), on the persistence layer's *types*, and on the app
context; never on ``ui/`` or ``tools/``. Nothing here renders, so every service is
straightforward to unit test against the simulator.
"""
