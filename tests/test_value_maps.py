"""Value maps: state-class codes are found even when the scenario-level read of the project
datasheet stsim_StateClass comes back empty (the 'no Id column' warning in 1.0.4 runs), and the
maps add up to the asset values they are made from."""
import os
import sqlite3
import sys
import types

import numpy as np
import pandas as pd
import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, SRC)
sys.modules.setdefault("pysyncrosim", types.ModuleType("pysyncrosim"))  # not needed for these tests
import seea_outputs as so  # noqa: E402
import transformer as tr  # noqa: E402

NAMES = {1: "Water:All", 2: "Mangrove:All", 3: "Ponds:All"}


def _library(path):
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE stsim_StateClass (StateClassId INTEGER, ProjectId INTEGER, "
                    "Name TEXT, Id INTEGER)")
        con.executemany("INSERT INTO stsim_StateClass VALUES (?, 1, ?, ?)",
                        [(30 + k, n, k) for k, n in NAMES.items()])


class _FailingProject:
    def datasheets(self, **kw):
        raise RuntimeError("no project access")


def test_state_class_codes_fall_back_to_library(tmp_path):
    lib = tmp_path / "lib.ssim"
    _library(lib)
    scn = types.SimpleNamespace(datasheets=lambda **kw: pd.DataFrame(), project=_FailingProject(),
                                library=types.SimpleNamespace(location=str(lib)), project_id=1)
    assert so.class_codes(tr.state_class_table(scn)) == NAMES


def test_state_class_codes_prefer_scenario_read():
    d = pd.DataFrame({"Name": list(NAMES.values()), "Id": list(NAMES)})
    scn = types.SimpleNamespace(datasheets=lambda **kw: d)
    assert so.class_codes(tr.state_class_table(scn)) == NAMES


def test_value_maps_add_up(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin
    cls = np.array([[1, 2, 2], [3, 3, 0]], dtype="int32")
    fn = tmp_path / "sc.it1.ts2031.tif"
    with rasterio.open(fn, "w", driver="GTiff", height=2, width=3, count=1, dtype="int32",
                       nodata=0, crs="EPSG:32749", transform=from_origin(0, 60, 30, 30)) as dst:
        dst.write(cls, 1)
    per_area = pd.DataFrame({"Iteration": 1, "Timestep": 2031, "StateClassId": list(NAMES.values()),
                             "ServiceValuePerArea": [1.0, 10.0, 5.0],
                             "AssetValuePerArea": [20.0, 300.0, 100.0]})
    maps = pd.DataFrame({"Iteration": [1], "Timestep": [2031], "Filename": [str(fn)]})
    rows = so.write_value_maps(maps, NAMES, per_area, [2031], str(tmp_path / "out"))
    assert len(rows) == 1
    with rasterio.open(rows[0]["AssetValueFile"]) as src:
        a = src.read(1)
        nod = src.nodata
    assert np.isclose(a[a != nod].sum(), 20 + 2 * 300 + 2 * 100)
    assert a[1, 2] == nod  # nodata cell stays nodata
