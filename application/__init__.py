"""Application layer.

Holds the ports (protocols) the application depends on, and later the
application services extracted from `creator_console.pipeline_service`.

This package must not import concrete infrastructure adapters. Dependencies are
passed in by the composition root (`creator_console.app.ConsoleApp`).
"""
