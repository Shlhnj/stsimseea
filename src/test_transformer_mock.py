"""
test_transformer_mock.py
==========================
Exercises stsimSEEA_transformer.py's translation logic end-to-end
using a mock Scenario object that mimics pysyncrosim's
Scenario.datasheets()/save_datasheet() API, WITHOUT a real SyncroSim
installation. This validates everything except the actual
pysyncrosim <-> SyncroSim Core boundary (which needs a live install to
test — see verify_schema() in the transformer file for that step).

Run with: python test_transformer_mock.py
"""
import sys
import pandas as pd
import numpy as np

sys.path.insert(0, ".")
import stsimSEEA_transformer as T


class MockScenario:
    """Mimics ps.Scenario()'s .datasheets(name=...) / .save_datasheet()."""
    def __init__(self, sheets: dict[str, pd.DataFrame]):
        self._sheets = sheets
        self.saved = {}

    def datasheets(self, name, project=False):
        if name not in self._sheets:
            raise KeyError(f"Mock has no datasheet '{name}'")
        return self._sheets[name].copy()

    def save_datasheet(self, df, name):
        self.saved[name] = df.copy()
        print(f"  [saved] {name}: {len(df)} rows")


def build_mock_scenario() -> MockScenario:
    # 2 classes: Forest (1), Cropland (2)
    stsim_StateClass = pd.DataFrame([
        {"StateClassID": 1, "Name": "Forest"},
        {"StateClassID": 2, "Name": "Cropland"},
    ])

    stsim_RunControl = pd.DataFrame([{"MinimumTimestep": 2022, "MaximumTimestep": 2024}])

    # Extent: 3 years, 2 iterations, area conserved (100 total)
    rows = []
    extents = {
        (2022, 1): (1, 60), (2022, 2): (1, 40),
        (2023, 1): (1, 55), (2023, 2): (1, 45),
        (2024, 1): (1, 50), (2024, 2): (1, 50),
    }
    for (ts, it), (strat, forest_area) in [((2022,1),(1,60)), ((2022,2),(1,60)),
                                             ((2023,1),(1,55)), ((2023,2),(1,55)),
                                             ((2024,1),(1,50)), ((2024,2),(1,50))]:
        rows.append({"Iteration": it, "Timestep": ts, "StratumID": strat,
                      "StateClassID": 1, "Amount": forest_area})
        rows.append({"Iteration": it, "Timestep": ts, "StratumID": strat,
                      "StateClassID": 2, "Amount": 100 - forest_area})
    stsim_OutputStratumState = pd.DataFrame(rows)

    # Transition: Forest -> Cropland, TransitionTypeID 10, 5 units,
    # in the 2022 timestep, both iterations
    stsim_OutputStratumTransition = pd.DataFrame([
        {"Iteration": 1, "Timestep": 2022, "StratumID": 1, "StateClassID": 1,
         "TransitionTypeID": 10, "Amount": 5.0},
        {"Iteration": 2, "Timestep": 2022, "StratumID": 1, "StateClassID": 1,
         "TransitionTypeID": 10, "Amount": 5.0},
    ])

    stsim_Transition = pd.DataFrame([
        {"StratumIDSource": 1, "StateClassIDSource": 1, "TransitionTypeID": 10,
         "StratumIDDest": 1, "StateClassIDDest": 2},
    ])

    stsim_TransitionType = pd.DataFrame([
        {"TransitionTypeID": 10, "Name": "Agriculture_expansion"},
    ])

    SF_StockType = pd.DataFrame([{"StockTypeID": 1, "Name": "AGB"}])
    SF_OutputStock = pd.DataFrame([
        {"Iteration": 1, "Timestep": 2022, "StratumID": 1, "StateClassID": 1,
         "StockTypeID": 1, "Amount": 1000.0},
        {"Iteration": 1, "Timestep": 2023, "StratumID": 1, "StateClassID": 1,
         "StockTypeID": 1, "Amount": 900.0},
        {"Iteration": 1, "Timestep": 2024, "StratumID": 1, "StateClassID": 1,
         "StockTypeID": 1, "Amount": 800.0},
    ])

    stsimSEEA_RunControl = pd.DataFrame([{
        "IncludeStockFlow": True,
        "CatastrophicTransitionGroups": None,
        "ManagedTransitionGroups": None,
    }])

    stsimSEEA_EcosystemServices = pd.DataFrame([
        {"StateClassID": 1, "ServiceName": "Recreation", "ServiceType": "Cultural",
         "ValuePerUnitArea": 1000.0, "Currency": "USD",
         "PhysicalUnit": None, "PhysicalValuePerUnitArea": None,
         "StockTypeId": None, "UserType": None, "UserShare": None},
        {"StateClassID": 1, "ServiceName": "Carbon Storage", "ServiceType": "Regulating",
         "ValuePerUnitArea": 50.0, "Currency": "USD",
         "PhysicalUnit": "MgC", "PhysicalValuePerUnitArea": None,
         "StockTypeId": "AGB", "UserType": None, "UserShare": None},
        {"StateClassID": 2, "ServiceName": "Crops", "ServiceType": "Provisioning",
         "ValuePerUnitArea": 200.0, "Currency": "USD",
         "PhysicalUnit": "kg/ha", "PhysicalValuePerUnitArea": 500.0,
         "StockTypeId": None, "UserType": None, "UserShare": None},
    ])

    stsimSEEA_AssetValuationParams = pd.DataFrame([
        {"StateClassID": None, "DiscountRate": 0.02, "AssetLifeYears": 10,
         "PriceGrowthRate": 0.0, "ConditionProxyStockTypeId": None,
         "ConditionReferenceLevel": None},
    ])

    return MockScenario({
        "stsim_StateClass": stsim_StateClass,
        "stsim_RunControl": stsim_RunControl,
        "stsim_OutputStratumState": stsim_OutputStratumState,
        "stsim_OutputStratumTransition": stsim_OutputStratumTransition,
        "stsim_Transition": stsim_Transition,
        "stsim_TransitionType": stsim_TransitionType,
        "SF_StockType": SF_StockType,
        "SF_OutputStock": SF_OutputStock,
        "stsimSEEA_RunControl": stsimSEEA_RunControl,
        "stsimSEEA_EcosystemServices": stsimSEEA_EcosystemServices,
        "stsimSEEA_AssetValuationParams": stsimSEEA_AssetValuationParams,
    })


def run():
    scen = build_mock_scenario()

    classes = T.load_classes(scen)
    assert classes[1].name == "Forest" and classes[2].name == "Cropland"
    print("load_classes: OK")

    area_modal_df = T.load_area_modal_df(scen, classes, px_area=1.0)
    assert set(area_modal_df["year"]) == {2022, 2023, 2024}
    assert area_modal_df.loc[
        (area_modal_df.year == 2022) & (area_modal_df.class_name == "Forest"),
        "area_ha"].iloc[0] == 60
    print("load_area_modal_df: OK")
    print(area_modal_df)

    trans_df = T.load_trans_df(scen, classes)
    assert not trans_df.empty
    assert trans_df.iloc[0]["from_class"] == "Forest"
    assert trans_df.iloc[0]["to_class"] == "Cropland"
    assert trans_df.iloc[0]["group"] == "Agriculture_expansion"
    print("load_trans_df: OK")
    print(trans_df)

    stock_df = T.load_stock_df(scen, classes, include_stockflow=True)
    assert stock_df is not None and not stock_df.empty
    print("load_stock_df: OK")
    print(stock_df)

    services = T.load_services(scen, classes)
    assert len(services) == 3
    carbon_svc = [s for s in services if s.service_name == "Carbon Storage"][0]
    assert carbon_svc.stockflow_source == "stock:AGB"
    print("load_services: OK")

    avp = T.load_asset_valuation_params(scen, classes)
    assert "ALL" in avp
    print("load_asset_valuation_params: OK")

    print()
    print("=== Building full SEEAAccount and running accounts ===")
    acct, options = T.build_seea_account(scen)
    name_to_id = {c.name: cid for cid, c in classes.items()}

    T.save_extent_account(scen, acct, name_to_id)
    T.save_flow_accounts(scen, acct, name_to_id)
    T.save_monetary_asset_account(scen, acct, options, name_to_id)

    print()
    print("Saved datasheets:", list(scen.saved.keys()))
    for name, df in scen.saved.items():
        assert not df.empty, f"{name} is unexpectedly empty"

    # Reconciliation check on the asset account, exactly like strategicc's
    # own tests
    asset_df = scen.saved["stsimSEEA_MonetaryAssetAccount"]
    for period in asset_df["Period"].unique():
        for sc_id in asset_df["StateClassID"].unique():
            block = asset_df[(asset_df.Period == period) & (asset_df.StateClassID == sc_id)]
            opening = block[block.Entry == "Opening value"]["Value"].sum()
            closing = block[block.Entry == "Closing value"]["Value"].sum()
            net = block[block.Entry == "Net change in value"]["Value"].sum()
            assert abs(net - (closing - opening)) < 1e-6, \
                f"Reconciliation FAILED for {period}/{sc_id}: net={net}, closing-opening={closing-opening}"
    print()
    print("Reconciliation check (Net change == Closing - Opening): PASSED")
    print()
    print("ALL MOCK TESTS PASSED")


if __name__ == "__main__":
    run()
