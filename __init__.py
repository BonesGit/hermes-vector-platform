try:
    from .adapter import register
except ImportError:  # pragma: no cover — flat import when not loaded as a package
    from adapter import register

__all__ = ["register"]
