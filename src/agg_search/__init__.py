"""agg-search: parallel multi-engine web search, RRF fusion and Jina rerank."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

try:
    __version__ = _dist_version("agg-search")
except PackageNotFoundError:  # running straight from a source checkout
    __version__ = "0.0.0+source"

__all__ = ["__version__"]
