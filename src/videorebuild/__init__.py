"""Conservative MP4 reconstruction from mapped surviving byte ranges."""
import sys

if sys.version_info < (3, 13):
    raise RuntimeError("VideoRebuild requires Python 3.13 or newer")

__version__ = "0.3.0"
