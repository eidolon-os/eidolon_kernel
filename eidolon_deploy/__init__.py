"""Offline activation tooling for target-native Eidolon OS releases."""

from eidolon_deploy.activation import ReleaseActivator
from eidolon_deploy.manifest import ReleaseDescriptor, load_release_descriptor

__all__ = ["ReleaseActivator", "ReleaseDescriptor", "load_release_descriptor"]
