"""Tests of command validation against the live tools and maps."""

from iai_validate import Command


def test_valid_command_creates(parts):
    validator = parts[3]
    result = validator.validate(
        Command("r.slope.aspect", {"elevation": "elevation", "slope": "slope"})
    )
    assert result.valid, result.errors
    assert result.risk == "create"


def test_unknown_tool_suggests_close_name(parts):
    result = parts[3].validate(Command("r.slope.aspct", {"elevation": "elevation"}))
    assert not result.valid
    assert any("r.slope.aspect" in s for s in result.suggestions)


def test_unknown_parameter(parts):
    result = parts[3].validate(Command("r.info", {"mapp": "elevation"}))
    assert any("no parameter 'mapp'" in e for e in result.errors)


def test_missing_input_map_suggests_existing(parts):
    result = parts[3].validate(Command("r.info", {"map": "elevaton"}))
    assert any("does not exist" in e for e in result.errors)
    assert any("elevation@" in s for s in result.suggestions)


def test_existing_output_needs_overwrite(parts):
    result = parts[3].validate(
        Command("r.slope.aspect", {"elevation": "elevation", "slope": "lsat_b4"})
    )
    assert any("already exists" in e for e in result.errors)


def test_bad_option_value(parts):
    result = parts[3].validate(
        Command("r.neighbors", {"input": "elevation", "output": "s", "method": "avg"})
    )
    assert any("method=avg" in e for e in result.errors)


def test_read_only_and_print_only(parts):
    validator = parts[3]
    assert validator.validate(Command("r.univar", {"map": "elevation"})).risk == "read"
    assert validator.validate(Command("g.region", {}, "p")).risk == "read"
    assert (
        validator.validate(Command("g.region", {"raster": "elevation"})).risk
        == "modify"
    )
    remove = Command("g.remove", {"type": "raster", "name": "lsat_b3"}, "f")
    assert validator.validate(remove).risk == "modify"


def test_mapcalc_checks_maps(parts):
    command = Command.from_string("r.mapcalc expression='ndvi = (lsat_b4 - lsat_b9)'")
    result = parts[3].validate(command)
    assert any("<lsat_b9>" in e for e in result.errors)


def test_plan_outputs_feed_later_steps(parts):
    plan = [
        Command("r.slope.aspect", {"elevation": "elevation", "slope": "slope_plan"}),
        Command("r.neighbors", {"input": "slope_plan", "output": "slope_smooth"}),
    ]
    results = parts[3].validate_plan(plan)
    assert all(r.valid for r in results), [r.errors for r in results]


def test_command_from_string():
    command = Command.from_string("r.info -g map=elevation --overwrite")
    assert command.module == "r.info"
    assert command.flags == "g"
    assert command.params == {"map": "elevation"}
    assert command.shell() == "r.info -g map=elevation"


def test_matcher_synonym(parts):
    matches = parts[2].match("dem", "raster")
    assert matches and matches[0]["entity"]["name"] == "elevation"


def test_catalog_lists_every_installed_tool(parts):
    catalog = parts[0]
    assert catalog.exists("r.slope.aspect")
    assert catalog.exists("v.extract")
    assert len(catalog.names("r")) > 100
    assert catalog.search("slope aspect elevation")[0]["name"] == "r.slope.aspect"


def test_display_tools(parts):
    validator = parts[3]
    start = validator.validate(Command("d.mon", {"start": "wx0"}))
    assert start.valid and start.risk == "display"
    assert validator.validate(Command("d.rast", {"map": "elevation"})).risk == "display"
    assert not validator.validate(Command("g.gui", {})).valid
