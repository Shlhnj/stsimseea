"""
Check SEEA-Sim against the UN SEEA EA stylised example "SEEALand"
(Online supplement, version 1, September 2021:
seea_ea_online_supplement_seealand_stylised_example.xlsx,
https://seea.un.org/ecosystem-accounting).

The example is turned into ST-Sim-shaped output:
  * 6 ecosystem types, 250 ha, one stratum, one iteration;
  * 2 ha Forest -> Cropland, a managed conversion;
  * each ecosystem service is a flow group whose yearly amount per class is
    the example's flow (ES flows tab), valued with a Flow group supply rule.

Asset account run (expected flows):
  timestep 2018 = opening state, 2019 = opening expected flows (no change
  yet), 2020 = conversion + closing expected flows; period 2018-2020.
  Prices: 2018 = opening prices, 2020 = closing prices. r = 2%, life 100 y.
Supply run (actual flows): flows of 2020 = actual flows, prices = actual.

Expected values are copied from the example's tabs. Numbers are compared to
1e-6 relative; the workbook itself shows round-off checks of about 1e-10.
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import seea_accounts as sa  # noqa: E402

ET = ["Forest", "Lake", "Cropland", "Urban area", "Wetland", "Seagrass"]
OPEN = dict(zip(ET, [40, 30, 60, 50, 20, 50]))
CLOSE = dict(zip(ET, [38, 30, 62, 50, 20, 50]))
S = ["Wood provisioning", "Crop provisioning", "Wild fish biomass provisioning",
     "Global climate regulation", "Water purification", "Recreation related"]

# ES flows tab: (actual, expected opening, expected closing) physical flows
FLOWS = {
    "Forest": {"Wood provisioning": (140, 150, 120), "Global climate regulation": (150, 160, 125),
               "Recreation related": (1500, 1600, 1450)},
    "Lake": {"Wild fish biomass provisioning": (3, 3, 4), "Recreation related": (5000, 4800, 5000)},
    "Cropland": {"Crop provisioning": (150, 150, 170)},
    "Urban area": {"Global climate regulation": (5, 5, 5), "Recreation related": (2500, 2400, 2600)},
    "Wetland": {"Global climate regulation": (20, 20, 19), "Water purification": (7, 7, 7)},
    "Seagrass": {"Wild fish biomass provisioning": (6, 6, 6), "Global climate regulation": (250, 250, 250),
                 "Recreation related": (800, 800, 700)},
}
# prices: (actual, expected opening, expected closing)
PRICES = {"Wood provisioning": (60, 60, 65), "Crop provisioning": (75, 75, 75),
          "Wild fish biomass provisioning": (350, 340, 370), "Global climate regulation": (25, 25, 26),
          "Water purification": (100, 100, 100), "Recreation related": (5, 5, 5)}
# Condition indices tab: net change in condition
COND = dict(zip(ET, [-0.0589815, 0.045611, 0.00341667, -0.0191905, -0.0352857, -0.0759498]))

# Monetary asset account tab
UN_ASSET = {
    "Opening value": [905065.3844, 1078320.758, 484856.456, 522567.5136, 51718.022, 529678.7417],
    "Ecosystem enhancement": [0, 15299.9148, 0, 0, 0, 0],
    "Ecosystem degradation": [-108111.0809, 0, 0, 0, -1099.008, -163946.1296],
    "Ecosystem conversions: additions": [0, 0, 16943.9084, 0, 0, 0],
    "Ecosystem conversions: reductions": [-43434.6322, 0, 0, 0, 0, 0],
    "Upward reappraisals": [0, 0, 47703.6191, 43098.3516, 0, 0],
    "Downward reappraisals": [0, 0, 0, 0, 0, 0],
    "Revaluations": [35180.1637, 47623.6786, 0, 215.4918, 840.4179, 160929.245],
    "Net change in value": [-116365.5494, 62923.5934, 64647.5275, 43313.8434, -258.5901, -3016.8846],
    "Closing value": [788699.835, 1141244.3514, 549503.9834, 565881.357, 51459.4319, 526661.857],
}
# Three places where the UN workbook departs from its own method (each checked
# cell by cell in the .xlsx):
# 1. Lake recreation: the "NPV Decomposition" tab reads the expected flows
#    from 'NPV by ET'!D32/E32, which are the PRICES (5, 5), instead of
#    D29/E29, the visits (4,800 -> 5,000). The +200 visits a year
#    (5 x 200 x annuity(2%, 100 y) = 43,098.35) therefore land in
#    revaluation instead of the volume effect. Urban area has the same
#    +200 visits at a constant price and the workbook books 43,098.35 as
#    volume there, which confirms it. Lake opening, closing and net change
#    are unaffected.
# 2. Seagrass fish: the "NPV Decomposition" tab has the opening expected
#    flow typed in as 20 (cell I193) instead of linking 'ES flows'!G35,
#    which is 6 (the opening NPV in the same workbook uses 6). That creates
#    an offsetting -142,397 volume / +150,155 price effect. With 6, fish has
#    no volume change; Seagrass degradation is the recreation loss
#    (-100 visits x 5 x annuity = -21,549.18) and revaluation is the fish and
#    carbon price rises (+18,532.29). Seagrass net change is unaffected.
# 3. Cropland: volume rises and condition rises (+0.0034), which the
#    workbook's own decision table classes as enhancement, but the account
#    books it as an upward reappraisal.
LAKE_REC = 5 * 200 * sa.annuity_factor(0.02, 100)
UN_ASSET_CORRECTED = {k: list(v) for k, v in UN_ASSET.items()}
UN_ASSET_CORRECTED["Ecosystem enhancement"][1] += LAKE_REC
UN_ASSET_CORRECTED["Revaluations"][1] -= LAKE_REC
AF = sa.annuity_factor(0.02, 100)
UN_ASSET_CORRECTED["Ecosystem degradation"][5] = -100 * 5 * AF
UN_ASSET_CORRECTED["Revaluations"][5] = (370 - 340) * 6 * AF + (26 - 25) * 250 * AF
UN_ASSET_CORRECTED["Ecosystem enhancement"][2] = UN_ASSET["Upward reappraisals"][2]
UN_ASSET_CORRECTED["Upward reappraisals"][2] = 0.0

# ES flows tab, exchange values of actual flows (= monetary supply)
UN_MONETARY_SUPPLY = {"Wood provisioning": 8400, "Crop provisioning": 11250,
                      "Wild fish biomass provisioning": 3150, "Global climate regulation": 10625,
                      "Water purification": 700, "Recreation related": 49000}


def _state():
    rows = []
    for t, areas in ((2018, OPEN), (2019, OPEN), (2020, CLOSE)):
        rows += [dict(Iteration=1, Timestep=t, StratumId="SEEALand", StateClassId=e, Amount=float(a))
                 for e, a in areas.items()]
    return pd.DataFrame(rows)


def _trans():
    return pd.DataFrame([dict(Iteration=1, Timestep=2020, StratumId="SEEALand",
                              TransitionTypeId="Agricultural conversion", StateClassId="Forest",
                              EndStateClassId="Cropland", Amount=2.0)])


def _flows(which):
    """which: dict timestep -> index into the FLOWS tuples."""
    rows = []
    for t, k in which.items():
        for e, svc in FLOWS.items():
            for s, vals in svc.items():
                rows.append(dict(Iteration=1, Timestep=t, FromStratumId="SEEALand", FromStateClassId=e,
                                 FromStockTypeId=None, ToStockTypeId=None, TransitionTypeId=None,
                                 FlowGroupId=s, Amount=float(vals[k])))
    return pd.DataFrame(rows)


RULES = pd.DataFrame([dict(ServiceId=s, StratumId=None, StateClassId=None, Basis="Flow group",
                           StockGroupId=None, FlowGroupId=s, Coefficient=1.0) for s in S])
TTC = {"Agricultural conversion": sa.MANAGED}
PERIODS = [(2018, 2020)]
VALUATION = pd.DataFrame([dict(StateClassId=None, DiscountRate=0.02, AssetLifeYears=100)])


def _asset_run(condition):
    state, trans = _state(), _trans()
    comp = sa.extent_components(state, trans, PERIODS, TTC)
    sup = sa.service_supply(state, None, _flows({2019: 1, 2020: 2}), RULES)
    prices = pd.DataFrame([dict(ServiceId=s, Timestep=t, Price=float(p[k]))
                           for s, p in PRICES.items() for t, k in ((2018, 1), (2020, 2))])
    ptbl = sa.price_table(prices, S, [2018, 2019, 2020])
    acc = sa.asset_account(comp, state, sup, ptbl, VALUATION, condition=condition, log=lambda m: None)
    return comp, acc.pivot_table(index="Entry", columns="StateClassId", values="Amount", aggfunc="sum")


def _close(a, b, what):
    assert abs(a - b) <= 1e-6 * max(1.0, abs(b)), f"{what}: SEEA-Sim {a:.6f} vs UN {b:.6f}"


def test_extent_account_and_change_matrix():
    comp = sa.extent_components(_state(), _trans(), PERIODS, TTC)
    ext = sa.extent_account(comp).pivot_table(index="Entry", columns="StateClassId", values="Amount")
    for e in ET:
        _close(ext.loc["Opening extent", e], OPEN[e], f"opening {e}")
        _close(ext.loc["Closing extent", e], CLOSE[e], f"closing {e}")
    _close(ext.loc["Additions: managed expansion", "Cropland"], 2, "managed expansion")
    _close(ext.loc["Reductions: managed reduction", "Forest"], -2, "managed reduction")
    m, _ = sa.change_matrix(_state(), _trans(), PERIODS, TTC)
    m = m.pivot_table(index="FromStateClassId", columns="ToStateClassId", values="Amount", fill_value=0)
    assert m.loc["Forest", "Forest"] == 38 and m.loc["Forest", "Cropland"] == 2
    for e in ET:
        _close(m.loc[e].sum(), OPEN[e], f"row {e}")
        _close(m[e].sum(), CLOSE[e], f"column {e}")


def _condition(cond):
    return pd.DataFrame([dict(StateClassId=e, Change=v) for e, v in cond.items()])


def test_asset_account_matches_un_method():
    """Every entry, every ecosystem type, with the three workbook errors corrected."""
    _, acc = _asset_run(_condition(COND))
    for entry, values in UN_ASSET_CORRECTED.items():
        for e, un in zip(ET, values):
            _close(acc.loc[entry, e], un, f"{entry} / {e}")
    _close(LAKE_REC, 43098.3516, "Lake recreation volume effect")
    _close(UN_ASSET_CORRECTED["Ecosystem degradation"][5] + UN_ASSET_CORRECTED["Revaluations"][5],
           UN_ASSET["Ecosystem degradation"][5] + UN_ASSET["Revaluations"][5], "Seagrass net unchanged")


def test_asset_account_matches_un_workbook_values():
    """
    The workbook's own numbers: opening, closing, net change, conversions and
    every entry for Forest, Urban area and Wetland match as published. Lake
    and Seagrass differ only in the split between volume and price (errors 1
    and 2). With Cropland's condition change taken as 0, Cropland matches as
    published too.
    """
    _, acc = _asset_run(_condition(dict(COND, Cropland=0.0)))
    for entry, values in UN_ASSET.items():
        for e, un in zip(ET, values):
            if e == "Lake" and entry in ("Ecosystem enhancement", "Revaluations"):
                continue
            if e == "Seagrass" and entry in ("Ecosystem degradation", "Revaluations"):
                continue
            _close(acc.loc[entry, e], un, f"{entry} / {e}")
    for entry in ("Opening value", "Net change in value", "Closing value"):
        _close(acc.loc[entry].sum(), sum(UN_ASSET[entry]), f"total {entry}")


def test_monetary_supply_matches_un():
    state = _state()
    sup = sa.service_supply(state, None, _flows({2020: 0}), RULES)
    prices = pd.DataFrame([dict(ServiceId=s, Timestep=None, Price=float(p[0])) for s, p in PRICES.items()])
    val = sa.service_value(sup, sa.price_table(prices, S, [2020]))
    got = val.groupby("ServiceId").Amount.sum()
    for s, un in UN_MONETARY_SUPPLY.items():
        _close(got[s], un, f"monetary supply {s}")
    phys = sup.groupby("ServiceId").Amount.sum()
    _close(phys["Recreation related"], 9800, "physical supply recreation")
    _close(phys["Global climate regulation"], 425, "physical supply climate")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
