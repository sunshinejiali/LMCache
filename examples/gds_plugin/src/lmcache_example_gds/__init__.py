# SPDX-License-Identifier: Apache-2.0
"""A driver-free example of the external GDS discovery contract.

This package demonstrates installation, selection and class validation only.
It deliberately rejects IO instead of reporting success without transferring data.
Replace these methods with a real driver adapter for hardware use.
"""

# Third Party
import torch

# First Party
from lmcache.v1.gpu_connector.gds_backends.base import GDSBackend, GDSHandle


class Backend(GDSBackend):
    """A concrete, CPU-constructible plugin that does not implement hardware IO."""

    name = "example_gds"

    def open_slab(self, location: str, size: int, direct_io: bool) -> GDSHandle:
        """Reject IO: this example only demonstrates discovery and selection."""
        raise NotImplementedError("example_gds is a discovery-only example")

    def register_buffer(self, buf: torch.Tensor) -> None:
        """Reject registration until a real driver adapter is implemented."""
        raise NotImplementedError("example_gds has no GPU buffer registration")

    def deregister_buffer(self, buf: torch.Tensor) -> None:
        """Reject registration until a real driver adapter is implemented."""
        raise NotImplementedError("example_gds has no GPU buffer registration")

    def register_stream(self, raw_stream: int) -> None:
        """Reject registration until a real driver adapter is implemented."""
        raise NotImplementedError("example_gds has no GPU stream registration")

    def deregister_stream(self, raw_stream: int) -> None:
        """Reject registration until a real driver adapter is implemented."""
        raise NotImplementedError("example_gds has no GPU stream registration")
