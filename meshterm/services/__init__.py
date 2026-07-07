"""Algorithm layer: pure logic for tracing, TX search, and path exploration.

Services depend on the :class:`~meshterm.core.connection.Device` interface and domain
models only. They contain no UI or persistence code, so they are straightforward to unit
test against the simulator.
"""
