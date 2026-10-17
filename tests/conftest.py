"""Fixtures for the i.ai tests: an XY project with a few small maps."""

import os
import sys

import pytest

import grass.script as gs
from grass.tools import Tools

ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ADDON_DIR)


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    """Session in a new XY project, active in os.environ for the library code."""
    tmp_path = tmp_path_factory.mktemp("i_ai")
    gs.create_project(tmp_path / "test")
    with gs.setup.init(tmp_path / "test") as grass_session:
        tools = Tools(session=grass_session)
        tools.g_region(n=10, s=0, e=10, w=0, res=1)
        tools.r_mapcalc(expression="elevation = row() + col()")
        tools.r_mapcalc(expression="lsat_b4 = 0.3")
        tools.r_mapcalc(expression="lsat_b3 = 0.1")
        tools.v_mkgrid(map="grid_roads", grid=[2, 2])
        yield grass_session


@pytest.fixture(scope="module")
def parts(session):
    """Catalog, registry, matcher and validator for the test session."""
    from iai_catalog import ModuleCatalog
    from iai_registry import EntityMatcher, EntityRegistry
    from iai_validate import ValidationLayer

    catalog = ModuleCatalog()
    registry = EntityRegistry()
    matcher = EntityMatcher(registry)
    validator = ValidationLayer(catalog, registry, matcher)
    return catalog, registry, matcher, validator
