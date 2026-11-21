"""
SEEA-Sim transformer: reads ST-Sim output from the running result scenario,
builds the SEEA EA accounts (seea_accounts.py) and saves them.

Runs as the second pipeline stage, after stsim_Runtime.
"""
import os
import sys

import pandas as pd
import pysyncrosim as ps

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import seea_accounts as sa  # noqa: E402

PKG = "stsimseea"


def read(scn, name, empty_ok=True):
    try:
        df = scn.datasheets(name=name)
    except Exception as e:  # sheet missing from this library
        if empty_ok:
            return pd.DataFrame()
        raise sa.AccountError(f"Could not read {name}: {e}")
    if (df is None or df.empty) and not empty_ok:
        raise sa.AccountError(f"{name} is empty. Check that ST-Sim wrote this output.")
    return df if df is not None else pd.DataFrame()


def save(scn, name, df, columns):
    if df is None or df.empty:
        return
    scn.save_datasheet(name=f"{PKG}_{name}", data=df[[c for c in columns if c in df.columns]])
    print(f"SEEA-Sim: saved {name} ({len(df)} rows)")


def check_output_options(scn):
    """Fail early with a clear message if ST-Sim did not write what we need."""
    oo = read(scn, "stsim_OutputOptions")
    if oo.empty:
        return
    r = oo.iloc[0]
    if not bool(r.get("SummaryOutputTRSC", False)):
        raise sa.AccountError(
            "Turn on ST-Sim Output Options > Tabular > Transitions by state class "
            "(SummaryOutputTRSC), every 1 timestep. The extent account is built from it.")
    if pd.notna(r.get("SummaryOutputTRSCTimesteps")) and int(r["SummaryOutputTRSCTimesteps"]) != 1:
        raise sa.AccountError("Set the Transitions by state class output frequency to 1 timestep.")


def main():
    scn = ps.Scenario()

    opts = read(scn, f"{PKG}_Options")
    by_stratum = bool(opts.iloc[0].get("ReportByStratum", False)) if not opts.empty else False

    check_output_options(scn)
    state = read(scn, "stsim_OutputStratumState", empty_ok=False)
    trans = read(scn, "stsim_OutputStratumTransitionState")
    if trans.empty:
        trans = pd.DataFrame(columns=["Iteration", "Timestep", "StratumId", "TransitionTypeId",
                                      "StateClassId", "EndStateClassId", "Amount"])

    per = read(scn, f"{PKG}_AccountingPeriod")
    periods = ([(int(r.OpeningTimestep), int(r.ClosingTimestep)) for r in per.itertuples()]
               if not per.empty else sa.default_periods(state))
    for o, c in periods:
        if c <= o:
            raise sa.AccountError(f"Accounting period {o}-{c}: closing must be after opening.")
    print(f"SEEA-Sim: periods {periods}; by stratum = {by_stratum}")

    ttc = read(scn, f"{PKG}_TransitionTypeClass")
    tt_class = ({r.TransitionTypeId: sa.list_value(r.ChangeType, sa.CHANGE_TYPES, "change type")
                 for r in ttc.itertuples()} if not ttc.empty else {})

    # Extent account and change matrix
    comp = sa.extent_components(state, trans, periods, tt_class, by_stratum)
    extent = sa.extent_account(comp)
    matrix, matrix_chk = sa.change_matrix(state, trans, periods, tt_class, by_stratum)
    period_cols = ["OpeningTimestep", "ClosingTimestep", "StratumId"]
    save(scn, "OutputExtent", extent, ["Iteration", *period_cols, "StateClassId", "Entry", "Amount"])
    save(scn, "OutputChangeMatrix", matrix,
         ["Iteration", *period_cols, "FromStateClassId", "ToStateClassId", "Amount"])
    checks = [sa.account_checks(extent, "Extent", "Opening extent", "Closing extent",
                                "Net change in extent"),
              comp.assign(Residual=comp.Residual.abs())
                  .groupby(["Iteration", "StratumId", "OpeningTimestep", "ClosingTimestep"],
                           as_index=False)["Residual"].sum()
                  .assign(Account="Extent",
                          Item="Area change not explained by recorded transitions"),
              matrix_chk]

    # Carbon
    stock = read(scn, "stsim_OutputStock")
    flow = read(scn, "stsim_OutputFlow")
    groups = read(scn, f"{PKG}_CarbonStockGroup")
    if not groups.empty:
        if stock.empty or flow.empty:
            raise sa.AccountError(
                "Carbon Stock Groups are set but stsim_OutputStock or stsim_OutputFlow is empty. "
                "Turn on Stock-Flow summary output (stocks and flows, every timestep).")
        membership = read(scn, "stsim_StockTypeGroupMembership")
        if membership.empty:
            membership = pd.DataFrame(columns=["StockTypeId", "StockGroupId"])
        fgroups = list(read(scn, f"{PKG}_CarbonFlowGroup").get("FlowGroupId", []))
        fmember = read(scn, "stsim_FlowTypeGroupMembership")
        cstock, cflow, cchk = sa.carbon_accounts(stock, flow, periods, groups, membership,
                                                 fgroups, fmember, by_stratum)
        save(scn, "OutputCarbonStock", cstock,
             ["Iteration", *period_cols, "StateClassId", "StockGroupId", "Entry", "Amount"])
        save(scn, "OutputCarbonFlow", cflow,
             ["Iteration", *period_cols, "StateClassId", "TransitionTypeId", "FromStockTypeId",
              "ToStockTypeId", "Category", "Amount"])
        checks += [cchk, sa.account_checks(cstock, "Carbon stock", "Opening stock",
                                           "Closing stock", "Net change in stock")]
    else:
        print("SEEA-Sim: no Carbon Stock Groups; carbon accounts skipped.")

    # Ecosystem services and assets
    rules = read(scn, f"{PKG}_ServiceSupply")
    supply = sa.service_supply(state, stock if not stock.empty else None,
                               flow if not flow.empty else None, rules, by_stratum)
    if supply is not None:
        out_cols = ["Iteration", "Timestep", "StratumId", "StateClassId", "ServiceId", "Amount"]
        save(scn, "OutputServiceSupply", sa._finish(supply.copy()), out_cols)
        prices = read(scn, f"{PKG}_ServicePrice")
        if not prices.empty and "Timestep" not in prices.columns:
            prices["Timestep"] = pd.NA
        price_ts = sorted(set(supply.Timestep.unique()) | {o for o, _ in periods})
        ptbl = sa.price_table(prices, supply.ServiceId.unique(), price_ts)
        value = sa.service_value(supply, ptbl)
        if value is not None:
            save(scn, "OutputServiceValue", sa._finish(value.copy()), out_cols)
        cond = read(scn, f"{PKG}_ConditionChange")
        assets = sa.asset_account(comp, state, supply, ptbl, read(scn, f"{PKG}_AssetValuation"),
                                  by_stratum, condition=cond if not cond.empty else None)
        if assets is not None:
            save(scn, "OutputAssetAccount", assets,
                 ["Iteration", *period_cols, "StateClassId", "Entry", "Amount"])
            checks.append(sa.account_checks(assets, "Asset", "Opening value", "Closing value",
                                            "Net change in value"))
    else:
        print("SEEA-Sim: no Service Supply rules; service and asset accounts skipped.")

    chk = sa.finish_checks(checks)
    save(scn, "OutputCheck", chk,
         ["Iteration", "Account", *period_cols, "Item", "Residual"])
    worst = chk.groupby(["Account", "Item"])["Residual"].max() if not chk.empty else []
    for (acc, item), v in getattr(worst, "items", lambda: [])():
        print(f"SEEA-Sim check: {acc}: {item}: max {v:.6g}")


if __name__ == "__main__":
    try:
        main()
    except sa.AccountError as e:
        print(f"SEEA-Sim input error: {e}")
        raise
