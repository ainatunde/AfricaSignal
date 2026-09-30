"""Outbound networking. Everything that touches the internet goes through ``fetch_document``."""

from africasignal.net.fetch import FetchResult, fetch_document
from africasignal.net.httpcache import HttpCache

__all__ = ["FetchResult", "HttpCache", "fetch_document"]
