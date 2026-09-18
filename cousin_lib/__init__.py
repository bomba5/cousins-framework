

def __getattr__(name):
    """cousin_lib.__version__, read from pyproject.toml (or the
    installed metadata) on first use; see cousin_lib.version."""
    if name == "__version__":
        from cousin_lib.version import version
        return version()
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
