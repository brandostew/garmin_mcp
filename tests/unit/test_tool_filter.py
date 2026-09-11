"""Unit tests for the env-var tool filter (_ToolFilter)."""

from garmin_mcp import _ToolFilter


class FakeApp:
    """Minimal stand-in for FastMCP: records which tools get registered."""

    def __init__(self):
        self.registered = []
        self.tool_kwargs = []

    def tool(self, *args, **kwargs):
        explicit = kwargs.get("name") or (
            args[0] if args and isinstance(args[0], str) else None
        )

        def decorator(fn):
            self.registered.append(explicit or fn.__name__)
            self.tool_kwargs.append(kwargs)
            return fn

        return decorator

    def run(self):
        return "ran"


def _register(filt, names):
    """Register one no-op tool per name through the filter."""
    for n in names:
        def fn():
            return None

        fn.__name__ = n
        filt.tool()(fn)


def test_no_filter_registers_all():
    app = FakeApp()
    filt = _ToolFilter(app, set(), set())
    _register(filt, ["get_a", "get_b"])
    assert app.registered == ["get_a", "get_b"]


def test_allowlist_only_registers_listed():
    app = FakeApp()
    filt = _ToolFilter(app, {"get_a"}, set())
    _register(filt, ["get_a", "get_b"])
    assert app.registered == ["get_a"]


def test_denylist_skips_listed():
    app = FakeApp()
    filt = _ToolFilter(app, set(), {"get_b"})
    _register(filt, ["get_a", "get_b"])
    assert app.registered == ["get_a"]


def test_allowlist_takes_precedence_over_denylist():
    app = FakeApp()
    filt = _ToolFilter(app, {"get_a"}, {"get_a"})
    _register(filt, ["get_a", "get_b"])
    assert app.registered == ["get_a"]


def test_matching_is_case_insensitive():
    app = FakeApp()
    filt = _ToolFilter(app, {"get_a"}, set())
    _register(filt, ["GET_A"])
    assert app.registered == ["GET_A"]


def test_unknown_filter_names_flags_typos():
    app = FakeApp()
    filt = _ToolFilter(app, {"get_a", "get_typo"}, set())
    _register(filt, ["get_a"])
    assert filt.unknown_filter_names() == ["get_typo"]


def test_explicit_name_kwarg_used_for_matching():
    app = FakeApp()
    filt = _ToolFilter(app, {"real_name"}, set())

    def fn():
        return None

    fn.__name__ = "internal_fn"
    filt.tool(name="real_name")(fn)
    assert app.registered == ["real_name"]
    assert filt.unknown_filter_names() == []


def test_passthrough_to_wrapped_app():
    app = FakeApp()
    filt = _ToolFilter(app, set(), set())
    assert filt.run() == "ran"


def test_read_tools_receive_safe_annotations():
    app = FakeApp()
    filt = _ToolFilter(app, set(), set())
    _register(filt, ["get_sleep_data"])
    annotations = app.tool_kwargs[0]["annotations"]
    assert annotations.readOnlyHint is True
    assert annotations.destructiveHint is False
    assert annotations.openWorldHint is False


def test_delete_tools_receive_destructive_annotations():
    app = FakeApp()
    filt = _ToolFilter(app, set(), set())
    _register(filt, ["delete_workout"])
    annotations = app.tool_kwargs[0]["annotations"]
    assert annotations.readOnlyHint is False
    assert annotations.destructiveHint is True


def test_unknown_tool_verbs_default_to_write():
    app = FakeApp()
    filt = _ToolFilter(app, set(), set())
    _register(filt, ["publish_something_new"])
    annotations = app.tool_kwargs[0]["annotations"]
    assert annotations.readOnlyHint is False
    assert annotations.destructiveHint is False


def test_explicit_annotations_are_preserved():
    from mcp.types import ToolAnnotations

    app = FakeApp()
    filt = _ToolFilter(app, set(), set())

    def fn():
        return None

    supplied = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
    filt.tool(annotations=supplied)(fn)
    assert app.tool_kwargs[0]["annotations"] is supplied
