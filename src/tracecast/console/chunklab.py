"""Stable facade for Chunk Lab inspection and single-chunk rendering."""

from .chunklab_baseline import CONTRACT_SNAPSHOT_RELPATH
from .chunklab_blocks import chunk_blocks
from .chunklab_render import chunklab_render

__all__ = ["CONTRACT_SNAPSHOT_RELPATH", "chunk_blocks", "chunklab_render"]
