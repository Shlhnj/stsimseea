"""
stsimSEEA_transformer.py
==========================
Adapter layer: reads ST-Sim's own output datasheets (and, optionally,
ST-Sim Stocks and Flows' output), translates them into the exact
DataFrame shapes strategicc.accounting.seea.SEEAAccount already
expects, calls its already-tested accounting methods, and writes the
results back out as stsimSEEA's own output datasheets.

This file deliberately does NOT reimplement any SEEA-EA accounting
math. All of that lives in strategicc (pip package), a dependency of
this package. If the accounting logic needs to change, it changes
there once, not here and there separately.

IMPORTANT — things this file assumes about ST-Sim's schema that could
not be verified without a live SyncroSim installation (unavailable in
the environment this was drafted in). Run verify_schema() (see bottom
of this file, or call it standalone first) against a real result
scenario before trusting the mapping below in production:

  - stsim_OutputStratumState has columns:
        Iteration, Timestep, StratumID, StateClassID, Amount
  - stsim_OutputStratumTransition has columns:
        Iteration, Timestep, StratumID, StateClassID, TransitionTypeID, Amount
    (i.e. records the ORIGIN stratum/state-class and the specific
    TransitionTypeID that fired — NOT an explicit destination column).
    The destination is resolved by joining against stsim_Transition
    (the transition PATHWAY DEFINITION datasheet: StratumIDSource,
    StateClassIDSource, TransitionTypeID, StratumIDDest,
    StateClassIDDest). If your ST-Sim version's OutputStratumTransition
    instead records a TransitionGroupID with no TransitionTypeID, this
    join will fail loudly (see resolve_transition_destinations()) —
    that's a genuine "the mapping needs adjusting" case, not a bug to
    silently paper over.
  - SF_OutputStock (stsimsf) has columns:
        Iteration, Timestep, StratumID, StateClassID, StockTypeID, Amount
"""

from __future__ import annotations
import sys
import pandas as pd
import numpy as np

import pysyncrosim as ps
from strategicc.accounting.seea import SEEAAccount
from strategicc.accounting.csv_loader import EcosystemService, AssetValuationParams
from strategicc.io.csv_loader import StateClass


def verify_schema(myScenario: "ps.Scenario") -> None:
    """
    Diagnostic helper — run this once against a real result scenario
    before relying on the column mapping in this file. Prints the
    actual columns of every datasheet this transformer reads from.
    Not called automatically; call it manually, e.g.:

        import pysyncrosim as ps
        from stsimSEEA_transformer import verify_schema
        s = ps.Scenario(sid=<your_result_scenario_id>)
        verify_schema(s)
    """
    for name in ["stsim_OutputStratumState", "stsim_OutputStratumTransition",
                 "stsim_Transition", "stsim_StateClass", "stsim_Stratum",
                 "SF_OutputStock", "SF_StockType"]:
        try:
            df = myScenario.datasheets(name=name)
            print(f"{name}: {list(df.columns)}  ({len(df)} rows)")
        except Exception as e:
            print(f"{name}: NOT AVAILABLE ({e})")


# ── Step 1: read stsim's own output → build classes + area_modal_df ─────────

def load_classes(myScenario: "ps.Scenario") -> dict[int, StateClass]:
    """stsim_StateClass → {StateClassID: StateClass}. strategicc's
    StateClass dataclass wants (id, name, label, color); ST-Sim's
    StateClassID IS the id, so this is a near-direct field copy."""
    sc_df = myScenario.datasheets(name="stsim_StateClass")
    classes = {}
    for _, row in sc_df.iterrows():
        sc_id = int(row["StateClassID"])
        name = str(row.get("Name", f"Class{sc_id}"))
        classes[sc_id] = StateClass(
            id=sc_id, name=name, full_name=name,
            color=(255, 0, 0, 0),  # display color unused by SEEAAccount math
        )
    return classes


def load_area_modal_df(myScenario: "ps.Scenario", classes: dict[int, StateClass],
                        px_area: float) -> pd.DataFrame:
    """
    stsim_OutputStratumState → strategicc's area_modal_df shape:
    columns [year, class_id, class_name, area_ha] (or whichever
    area-unit column strategicc's _acol resolves to — px_area's unit
    determines this the same way it does in strategicc's own
    modal_to_area_table()).

    ST-Sim's Timestep column already holds the absolute calendar year
    (bounded by RunControl.MinimumTimestep/MaximumTimestep), not a
    0-indexed step count — so it maps directly to strategicc's 'year'
    with no offset arithmetic needed.
    """
    state_df = myScenario.datasheets(name="stsim_OutputStratumState")

    rows = []
    for _, r in state_df.iterrows():
        sc_id = int(r["StateClassID"])
        cls = classes.get(sc_id)
        if cls is None:
            continue
        rows.append({
            "year": int(r["Timestep"]),  # ST-Sim Timestep IS the absolute year, not an offset
            "iteration": int(r["Iteration"]),
            "class_id": sc_id,
            "class_name": cls.name,
            "area_ha": float(r["Amount"]),
        })
    df = pd.DataFrame(rows)
    # strategicc's SEEAAccount expects one row per (year, class) — i.e.
    # already aggregated/median-across-iterations, matching what its
    # own modal_to_area_table() produces from a MODAL raster. ST-Sim's
    # OutputStratumState is already a non-spatial summary per
    # iteration, so take the median across iterations here to match
    # that convention (strategicc's extent_account() itself has no
    # iteration dimension).
    agg = (df.groupby(["year", "class_id", "class_name"])["area_ha"]
             .median().reset_index())
    return agg


# ── Step 2: read stsim transitions → build trans_df ──────────────────────────

def resolve_transition_destinations(myScenario: "ps.Scenario") -> pd.DataFrame:
    """stsim_Transition (pathway definitions) → lookup table keyed by
    (StratumIDSource, StateClassIDSource, TransitionTypeID) →
    (StratumIDDest, StateClassIDDest). Raises with a clear message if
    the expected columns aren't there — see module docstring."""
    defs = myScenario.datasheets(name="stsim_Transition", project=True)
    required = {"StratumIDSource", "StateClassIDSource", "TransitionTypeID",
                "StratumIDDest", "StateClassIDDest"}
    missing = required - set(defs.columns)
    if missing:
        raise RuntimeError(
            f"stsim_Transition is missing expected column(s) {missing} — "
            f"actual columns: {list(defs.columns)}. Run verify_schema() and "
            f"adjust resolve_transition_destinations() to match."
        )
    return defs


def load_trans_df(myScenario: "ps.Scenario", classes: dict[int, StateClass]) -> pd.DataFrame:
    """
    stsim_OutputStratumTransition (+ stsim_Transition join) →
    strategicc's trans_df shape: columns
    [iteration, year, from_class, to_class, group]
    (strategicc doesn't need row/col — those were pixel coordinates
    for its own spatial engine; ST-Sim's output is already
    non-spatial, aggregated Amount per transition per timestep, so we
    synthesize a matching per-hectare row count using px_area).
    """
    trans_out = myScenario.datasheets(name="stsim_OutputStratumTransition")
    px_area = 1.0  # ST-Sim output is already area, not pixel counts —
                    # see note in build_seea_account() re: px_area handling

    required = {"StratumID", "StateClassID", "TransitionTypeID", "Amount",
                "Iteration", "Timestep"}
    missing = required - set(trans_out.columns)
    if missing:
        raise RuntimeError(
            f"stsim_OutputStratumTransition is missing expected column(s) "
            f"{missing} — actual columns: {list(trans_out.columns)}. Run "
            f"verify_schema() and adjust load_trans_df() to match."
        )

    tdefs = resolve_transition_destinations(myScenario)
    tgroups = myScenario.datasheets(name="stsim_TransitionType")  # TransitionTypeID -> Name, used as 'group'

    type_name = dict(zip(tgroups["TransitionTypeID"], tgroups["Name"])) \
        if {"TransitionTypeID", "Name"}.issubset(tgroups.columns) else {}

    dest_lookup = {
        (int(r["StratumIDSource"]) if pd.notna(r["StratumIDSource"]) else None,
         int(r["StateClassIDSource"]), int(r["TransitionTypeID"])):
            int(r["StateClassIDDest"])
        for _, r in tdefs.iterrows()
    }

    rows = []
    unresolved = 0
    for _, r in trans_out.iterrows():
        strat = int(r["StratumID"]) if pd.notna(r["StratumID"]) else None
        src_sc = int(r["StateClassID"])
        ttype = int(r["TransitionTypeID"])
        key = (strat, src_sc, ttype)
        dest_sc = dest_lookup.get(key) or dest_lookup.get((None, src_sc, ttype))
        if dest_sc is None:
            unresolved += 1
            continue
        from_cls = classes.get(src_sc)
        to_cls = classes.get(dest_sc)
        if from_cls is None or to_cls is None:
            continue
        # Synthesize a "cell count" so strategicc's px_area * n_cells
        # arithmetic (built for pixel-based STRATEGICC output)
        # reproduces the same Amount when multiplied back out.
        rows.append({
            "iteration": int(r["Iteration"]),
            "year": int(r["Timestep"]),  # ST-Sim Timestep IS the absolute year, not an offset
            "from_class": from_cls.name,
            "to_class": to_cls.name,
            "group": type_name.get(ttype, f"Type{ttype}"),
            "row": 0, "col": 0,  # unused by strategicc's grouping logic
            "_amount": float(r["Amount"]),
        })

    if unresolved:
        print(f"  [Warning] {unresolved} transition output row(s) could not "
              f"be matched to a stsim_Transition pathway definition — "
              f"skipped. This usually means resolve_transition_destinations() "
              f"needs adjusting for your ST-Sim version's schema.")

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # strategicc's extent_account_seea()/monetary_asset_account_seea()
    # compute area as n_cells * px_area, then take the MEDIAN n_cells
    # across iterations. Since ST-Sim already gives us Amount directly
    # (not cell counts), expand each row into a synthetic 1-row group
    # with n_cells = Amount and px_area = 1.0, so
    # median(n_cells) * px_area reproduces ST-Sim's own Amount exactly
    # when there's a single iteration value per (year, from, to, group);
    # strategicc's own median-across-iterations behavior is preserved
    # because the 'iteration' column is retained. Each row here already
    # represents one iteration's full Amount for that (year, from, to,
    # group) combination, so no further pixel-level expansion is
    # needed or wanted.
    df = df.rename(columns={"_amount": "_synthetic_cell_count"})
    return df


# ── Step 3: optional stsimsf stock output → strategicc's stock_df ───────────

def load_stock_df(myScenario: "ps.Scenario", classes: dict[int, StateClass],
                   include_stockflow: bool) -> pd.DataFrame | None:
    """
    SF_OutputStock (stsimsf) → strategicc's stock_df shape: columns
    [year, class_id, class_name, stock_type, total]. Returns None if
    stsimsf's output isn't available or IncludeStockFlow is False —
    strategicc's SEEAAccount already handles stock_df=None gracefully
    (Mode C / StockTypeId-linked services value at 0, everything else
    unaffected). This is the ONLY point in this file where stsimsf is
    touched at all — no other function references it.
    """
    if not include_stockflow:
        print("  IncludeStockFlow=False — skipping stsimsf, stock-linked "
              "services will value at 0.")
        return None
    try:
        stock_out = myScenario.datasheets(name="SF_OutputStock")
    except Exception:
        print("  stsimsf not installed/run for this scenario — "
              "stock-linked services will value at 0.")
        return None
    if stock_out is None or stock_out.empty:
        print("  SF_OutputStock is empty — stock-linked services will "
              "value at 0.")
        return None

    stock_types = myScenario.datasheets(name="SF_StockType")
    type_name = dict(zip(stock_types["StockTypeID"], stock_types["Name"])) \
        if {"StockTypeID", "Name"}.issubset(stock_types.columns) else {}

    rows = []
    for _, r in stock_out.iterrows():
        sc_id = int(r["StateClassID"])
        cls = classes.get(sc_id)
        if cls is None:
            continue
        rows.append({
            "year": int(r["Timestep"]),  # ST-Sim Timestep IS the absolute year, not an offset
            "class_id": sc_id,
            "class_name": cls.name,
            "stock_type": type_name.get(int(r["StockTypeID"]), str(r["StockTypeID"])),
            "total": float(r["Amount"]),
        })
    df = pd.DataFrame(rows)
    return df.groupby(["year", "class_id", "class_name", "stock_type"])["total"] \
              .median().reset_index()


# ── Step 4: read this package's own inputs ───────────────────────────────────

def load_services(myScenario: "ps.Scenario", classes: dict[int, StateClass]
                   ) -> list[EcosystemService]:
    es_df = myScenario.datasheets(name="stsimSEEA_EcosystemServices")
    services = []
    for _, r in es_df.iterrows():
        sc_id = int(r["StateClassID"])
        cls = classes.get(sc_id)
        if cls is None:
            continue
        stock_type = r.get("StockTypeId")
        has_stock = pd.notna(stock_type) and str(stock_type).strip() != ""
        services.append(EcosystemService(
            state_class       = cls.name,
            service_name      = str(r["ServiceName"]),
            service_type      = str(r["ServiceType"]),
            value_per_unit_area = float(r["ValuePerUnitArea"]),
            currency          = str(r["Currency"]),
            physical_unit     = r.get("PhysicalUnit") if pd.notna(r.get("PhysicalUnit")) else None,
            physical_per_unit_area = (float(r["PhysicalValuePerUnitArea"])
                                       if pd.notna(r.get("PhysicalValuePerUnitArea")) else None),
            stockflow_source  = f"stock:{stock_type}" if has_stock else None,
            user_type         = str(r["UserType"]) if pd.notna(r.get("UserType")) else "Unspecified",
            user_share        = float(r["UserShare"]) if pd.notna(r.get("UserShare")) else 1.0,
            has_explicit_user = pd.notna(r.get("UserType")) and str(r.get("UserType")).strip() != "",
        ))
    return services


def load_asset_valuation_params(myScenario: "ps.Scenario", classes: dict[int, StateClass]
                                 ) -> dict[str, AssetValuationParams]:
    avp_df = myScenario.datasheets(name="stsimSEEA_AssetValuationParams")
    params = {}
    for _, r in avp_df.iterrows():
        sc_id = r.get("StateClassID")
        key = classes[int(sc_id)].name if pd.notna(sc_id) else "ALL"
        cproxy = r.get("ConditionProxyStockTypeId")
        cref = r.get("ConditionReferenceLevel")
        params[key] = AssetValuationParams(
            state_class              = key,
            discount_rate             = float(r["DiscountRate"]),
            asset_life_years          = int(r["AssetLifeYears"]),
            price_growth_rate          = float(r.get("PriceGrowthRate", 0.0) or 0.0),
            condition_proxy            = str(cproxy) if pd.notna(cproxy) else None,
            condition_reference_level   = float(cref) if pd.notna(cref) else None,
        )
    return params


def _parse_group_list(raw) -> set[str] | None:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)) or str(raw).strip() == "":
        return None
    return {g.strip() for g in str(raw).split(",") if g.strip()}


# ── Step 5: build a strategicc SEEAAccount and write results back ───────────

def build_seea_account(myScenario: "ps.Scenario") -> tuple[SEEAAccount, dict]:
    classes = load_classes(myScenario)
    run_control_df = myScenario.datasheets(name="stsimSEEA_RunControl")
    rc = run_control_df.iloc[0] if not run_control_df.empty else {}
    include_stockflow = bool(rc.get("IncludeStockFlow", True))

    area_modal_df = load_area_modal_df(myScenario, classes, px_area=1.0)
    trans_df = load_trans_df(myScenario, classes)
    stock_df = load_stock_df(myScenario, classes, include_stockflow)
    services = load_services(myScenario, classes)
    avp = load_asset_valuation_params(myScenario, classes)

    acct = SEEAAccount(
        area_modal_df=area_modal_df, trans_df=trans_df, services=services,
        classes=classes, px_area=1.0, px_area_ha=1.0,
        stock_df=stock_df, flow_df=None,
        asset_valuation_params=avp,
    )
    options = {
        "catastrophic_groups": _parse_group_list(rc.get("CatastrophicTransitionGroups")),
        "managed_groups": _parse_group_list(rc.get("ManagedTransitionGroups")),
    }
    return acct, options


def save_extent_account(myScenario, acct: SEEAAccount, name_to_id: dict[str, int]):
    df = acct.extent_account_seea()
    rows = []
    for (period, entry), row in df.iterrows():
        for cls_name, amount in row.items():
            if cls_name == "Total":
                continue
            rows.append({"Period": period, "Entry": entry,
                         "StateClassID": name_to_id[cls_name], "Amount": amount})
    myScenario.save_datasheet(pd.DataFrame(rows), name="stsimSEEA_ExtentAccount")


def save_monetary_asset_account(myScenario, acct: SEEAAccount, options: dict,
                                 name_to_id: dict[str, int]):
    df = acct.monetary_asset_account_seea(catastrophic_groups=options["catastrophic_groups"])
    rows = []
    for (period, entry), row in df.iterrows():
        for cls_name, value in row.items():
            if cls_name == "Total":
                continue
            rows.append({"Period": period, "Entry": entry,
                         "StateClassID": name_to_id[cls_name], "Value": value})
    myScenario.save_datasheet(pd.DataFrame(rows), name="stsimSEEA_MonetaryAssetAccount")


def save_flow_accounts(myScenario, acct: SEEAAccount, name_to_id: dict[str, int]):
    phys = acct.physical_flow_account_seea()
    if phys is not None:
        supply_rows = []
        for (year, cls_name), row in phys["supply"].iterrows():
            for (svc_type, svc_name, unit), qty in row.items():
                supply_rows.append({"Timestep": year, "StateClassID": name_to_id[cls_name],
                                    "ServiceName": svc_name, "Unit": unit, "Quantity": qty})
        myScenario.save_datasheet(pd.DataFrame(supply_rows), name="stsimSEEA_PhysicalFlowAccountSupply")

        use_rows = []
        for (year, user_type), row in phys["use"].iterrows():
            for (svc_type, svc_name, unit), qty in row.items():
                use_rows.append({"Timestep": year, "UserType": user_type,
                                 "ServiceName": svc_name, "Unit": unit, "Quantity": qty})
        myScenario.save_datasheet(pd.DataFrame(use_rows), name="stsimSEEA_PhysicalFlowAccountUse")

    mon = acct.monetary_flow_account_seea()
    supply_rows = []
    for (year, cls_name), row in mon["supply"].iterrows():
        for (svc_type, svc_name), value in row.items():
            supply_rows.append({"Timestep": year, "StateClassID": name_to_id[cls_name],
                                "ServiceName": svc_name, "Value": value})
    myScenario.save_datasheet(pd.DataFrame(supply_rows), name="stsimSEEA_MonetaryFlowAccountSupply")

    use_rows = []
    for (year, user_type), row in mon["use"].iterrows():
        for (svc_type, svc_name), value in row.items():
            use_rows.append({"Timestep": year, "UserType": user_type,
                             "ServiceName": svc_name, "Value": value})
    myScenario.save_datasheet(pd.DataFrame(use_rows), name="stsimSEEA_MonetaryFlowAccountUse")


def main():
    myScenario = ps.Scenario()
    print("Building SEEA account from ST-Sim output...")
    acct, options = build_seea_account(myScenario)

    classes = load_classes(myScenario)
    name_to_id = {cls.name: cid for cid, cls in classes.items()}

    print("Saving ExtentAccount (Table 4.1)...")
    save_extent_account(myScenario, acct, name_to_id)

    print("Saving flow accounts (Tables 7.1/9.1)...")
    save_flow_accounts(myScenario, acct, name_to_id)

    if acct.asset_valuation_params:
        print("Saving MonetaryAssetAccount (Table 10.1)...")
        save_monetary_asset_account(myScenario, acct, options, name_to_id)
    else:
        print("  [Skipped] MonetaryAssetAccount — no AssetValuationParams rows provided.")

    print("Done.")


if __name__ == "__main__":
    main()
