"""Strict checkpoint codec and private storage primitives."""

from .codec import CheckpointError, CheckpointIndeterminateError

__all__ = ["CheckpointError", "CheckpointIndeterminateError"]
