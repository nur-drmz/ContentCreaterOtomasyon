"""Infrastructure adapters.

Concrete implementations of the ports declared in `application.ports`. Upper
layers depend on the ports, never on the classes in this package; the composition
root (`creator_console.app.ConsoleApp`) is what selects an adapter.
"""
