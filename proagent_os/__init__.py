"""ProAgent OS — commercial-ready vertical-agnostic Pro-Agent framework."""

from importlib.metadata import PackageNotFoundError, version as pkg_version

try:
    __version__ = pkg_version("proagent-os")
except PackageNotFoundError:
    __version__ = "0.1.0"
