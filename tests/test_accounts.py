"""
Tests for seea_accounts.py with a small cell-based simulation that writes
tables shaped like ST-Sim 4.5 output. No SyncroSim needed.

    python -m pytest tests -q        (or: python tests/test_accounts.py)
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import seea_accounts as sa  # noqa: E402

CLASSES = ["Mangrove", "Aquaculture", "Water"]
TYPES = {("Mangrove", "Aquaculture"): "Pond conversion",
         ("Water", "Mangrove"): "Colonisation",
         ("Mangrove", "Water"): "Inundation",
         ("Aquaculture", "Mangrove"): "Recruitment"}
P = {k: p for k, p in zip(TYPES, [0.04, 0.05, 0.03, 0.02])}
NPP = {"Mangrove": 3.5, "Aquaculture": 0.0, "Water": 0.0}
CELL_HA = 0.09


def simulate(n_cells=2000, years=range(2015, 2023), iterations=(1, 2), seed=1):
    rng = np.random.default_rng(seed)
    S, T, ST, FL = [], [], [], []
    y0 = years[0]
    for it in iterations:
        cls = rng.choice(CLASSES, n_cells, p=[0.3, 0.3, 0.4])
        strat = np.where(np.arange(n_cells) < n_cells // 2, "North", "South")
        agb = np.where(cls == "Mangrove", 26.0, 0.0)
        soil = np.where(cls == "Mangrove", 650.0, 200.0)

        def record(t):
            for s in ("North", "South"):
                for c in CLASSES:
                    m = (strat == s) & (cls == c)
                    S.append(dict(Iteration=it, Timestep=t, StratumId=s, StateClassId=c,
                                  Amount=m.sum() * CELL_HA))
                    for g, v in (("AGB", agb), ("Soil", soil)):
                        ST.append(dict(Iteration=it, Timestep=t, StratumId=s, StateClassId=c,
                                       StockGroupId=g, Amount=v[m].sum() * CELL_HA))

        def flow(t, m, frm, to, amt, tt=None):
            for s in ("North", "South"):
                for c in CLASSES:
                    k = m & (strat == s) & (cls == c)
                    a = amt[k].sum() * CELL_HA
                    if a:
                        FL.append(dict(Iteration=it, Timestep=t, FromStratumId=s, FromStateClassId=c,
                                       FromStockTypeId=frm, ToStockTypeId=to, TransitionTypeId=tt,
                                       FlowGroupId=f"{frm}->{to} [Type]", Amount=a))
                        # same flows reported again under a user group: must not be double counted
                        FL.append(dict(Iteration=it, Timestep=t, FromStratumId=s, FromStateClassId=c,
                                       FromStockTypeId=frm, ToStockTypeId=to, TransitionTypeId=tt,
                                       FlowGroupId="All carbon", Amount=a))

        record(y0)
        for t in years[1:]:
            new = cls.copy()
            for (a, b), name in TYPES.items():
                hit = (cls == a) & (rng.random(n_cells) < P[(a, b)]) & (new == cls)
                if not hit.any():
                    continue
                for s in ("North", "South"):
                    k = hit & (strat == s)
                    T.append(dict(Iteration=it, Timestep=t, StratumId=s, TransitionTypeId=name,
                                  StateClassId=a, EndStateClassId=b, Amount=k.sum() * CELL_HA))
                # transition-triggered flows, attributed to the pre-transition class
                if b != "Mangrove":
                    flow(t, hit, "AGB", "Atmosphere", agb.copy(), name)
                    lost = soil * 0.3
                    flow(t, hit, "Soil", "Atmosphere", lost, name)
                    agb = np.where(hit, 0.0, agb)
                    soil = soil - np.where(hit, lost, 0.0)
                new[hit] = b
            cls = new
            # ongoing flows on the new class
            g = np.array([NPP[c] for c in cls])
            flow(t, g > 0, "Atmosphere", "AGB", g)
            agb = agb + g
            lit = agb * 0.1
            flow(t, lit > 0, "AGB", "Soil", lit)
            agb, soil = agb - lit, soil + lit
            resp = soil * 0.01
            flow(t, resp > 0, "Soil", "Atmosphere", resp)
            soil = soil - resp
            exp = soil * 0.003
            flow(t, exp > 0, "Soil", "Water", exp)
            soil = soil - exp
            record(t)
    return pd.DataFrame(S), pd.DataFrame(T), pd.DataFrame(ST), pd.DataFrame(FL)


STATE, TRANS, STOCK, FLOW = simulate()
PERIODS = [(2015, 2020), (2020, 2022)]
TTC = {"Pond conversion": sa.MANAGED, "Inundation": sa.CATASTROPHIC}
GROUPS = pd.DataFrame({"StockGroupId": ["AGB", "Soil", "Atmosphere", "Water"],
                       "Role": [1, 1, "Atmosphere", 3]})
MEMBERS = pd.DataFrame({"StockTypeId": ["AGB", "Soil", "Atmosphere", "Water"],
                        "StockGroupId": ["AGB", "Soil", "Atmosphere", "Water"]})


def _close(a, b, tol=1e-6):
    assert abs(a - b) < tol * max(1.0, abs(b)), (a, b)


def test_extent_reconciles_and_matches_state():
    for by in (False, True):
        comp = sa.extent_components(STATE, TRANS, PERIODS, TTC, by)
        assert comp.Residual.abs().max() < 1e-9
        ext = sa.extent_account(comp)
        chk = sa.account_checks(ext, "Extent", "Opening extent", "Closing extent",
                                "Net change in extent")
        assert chk.Residual.max() < 1e-9
    # total area is conserved
    tot = ext[ext.Entry == "Closing extent"].groupby(["Iteration", "ClosingTimestep"]).Amount.sum()
    _close(tot.min(), tot.max())


def test_change_matrix_rows_equal_opening():
    m, chk = sa.change_matrix(STATE, TRANS, PERIODS, TTC, False)
    rows = m.groupby(["Iteration", "OpeningTimestep", "FromStateClassId"]).Amount.sum()
    st = STATE.groupby(["Iteration", "Timestep", "StateClassId"]).Amount.sum()
    for (it, o, c), v in rows.items():
        _close(v, st[(it, o, c)])
    assert (chk.Residual >= 0).all()


def test_carbon_total_reconciles_and_no_double_count():
    cs, cf, chk = sa.carbon_accounts(STOCK, FLOW, PERIODS, GROUPS, MEMBERS,
                                     flow_groups=["All carbon"])
    assert chk.Residual.abs().max() < 1e-6
    cs2, cf2, _ = sa.carbon_accounts(STOCK, FLOW, PERIODS, GROUPS, MEMBERS)  # auto groups
    _close(cf.Amount.sum(), cf2.Amount.sum())
    acc = sa.account_checks(cs, "Carbon", "Opening stock", "Closing stock", "Net change in stock")
    assert acc.Residual.max() < 1e-6
    # sequestration equals NPP flows from the atmosphere
    seq = cf[cf.Category == "Sequestration"].Amount.sum()
    raw = FLOW[(FLOW.FlowGroupId == "All carbon") & (FLOW.FromStockTypeId == "Atmosphere")].Amount
    _close(seq, raw[FLOW.Timestep > 2015].sum())
    assert (cf.Category == "Emissions (conversion and disturbance)").any()


def test_overlapping_flow_groups_rejected():
    fm = pd.DataFrame({"FlowTypeId": ["x", "x"], "FlowGroupId": ["A", "B"]})
    try:
        sa.select_flow_rows(FLOW, ["A", "B"], fm)
    except sa.AccountError:
        return
    raise AssertionError("overlap not detected")


RULES = pd.DataFrame([
    dict(ServiceId="Fish nursery", StateClassId="Mangrove", Basis=1, Coefficient=0.2),
    dict(ServiceId="Aquaculture produce", StateClassId="Aquaculture", Basis="Area", Coefficient=1.5),
    dict(ServiceId="Carbon sequestration", Basis=3, FlowGroupId="Atmosphere->AGB [Type]",
         Coefficient=44 / 12),
])
PRICES = pd.DataFrame([
    dict(ServiceId="Fish nursery", Timestep=None, Price=100.0),
    dict(ServiceId="Fish nursery", Timestep=2021, Price=120.0),
    dict(ServiceId="Aquaculture produce", Timestep=None, Price=50.0),
    dict(ServiceId="Carbon sequestration", Timestep=None, Price=10.0),
])
VAL = pd.DataFrame([dict(StateClassId=None, DiscountRate=0.05, AssetLifeYears=20),
                    dict(StateClassId="Mangrove", DiscountRate=0.03, AssetLifeYears=30)])


def test_services_and_asset_account():
    for by in (False, True):
        sup = sa.service_supply(STATE, STOCK, FLOW, RULES, by)
        assert sup.Timestep.min() == 2016
        ptbl = sa.price_table(PRICES, sup.ServiceId.unique(),
                              sorted(set(sup.Timestep) | {o for o, _ in PERIODS}))
        assert ptbl.set_index(["ServiceId", "Timestep"]).Price[("Fish nursery", 2022)] == 120
        assert ptbl.set_index(["ServiceId", "Timestep"]).Price[("Fish nursery", 2020)] == 100
        val = sa.service_value(sup, ptbl)
        assert (val.Amount >= 0).all()
        comp = sa.extent_components(STATE, TRANS, PERIODS, TTC, by)
        assets = sa.asset_account(comp, STATE, sup, ptbl, VAL, by)
        chk = sa.account_checks(assets, "Asset", "Opening value", "Closing value",
                                "Net change in value")
        assert chk.Residual.max() < 1e-6
        rev = assets[(assets.Entry == "Revaluations") & (assets.ClosingTimestep == 2022)]
        assert rev.Amount.sum() > 0  # fish nursery price rose in 2021
    # nursery supply equals 0.2 x mangrove area
    m = STATE[(STATE.StateClassId == "Mangrove") & (STATE.Timestep == 2018)
              & (STATE.Iteration == 1)].Amount.sum()
    s = sup[(sup.ServiceId == "Fish nursery") & (sup.Timestep == 2018) & (sup.Iteration == 1)].Amount.sum()
    _close(s, 0.2 * m)


def test_missing_timestep_message():
    try:
        sa.extent_components(STATE, TRANS, [(2010, 2020)], TTC)
    except sa.AccountError as e:
        assert "2010" in str(e)
        return
    raise AssertionError


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
