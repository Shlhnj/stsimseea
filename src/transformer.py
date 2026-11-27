"""
SEEA-Sim transformer: reads ST-Sim output from the running result scenario,
builds the SEEA EA accounts (seea_accounts.py), saves them, and writes the
extra outputs (seea_outputs.py): yearly asset values for the Charts tab,
value maps for the Maps tab, and an Excel workbook.

Runs as the second pipeline stage, after stsim_Runtime.
"""
import os
import re
import sys
import tempfile
import traceback

import pandas as pd
import pysyncrosim as ps

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import seea_accounts as sa  # noqa: E402

PKG = "stsimseea"
HERE = os.path.dirname(os.path.abspath(__file__))


def say(msg, kind="status"):
    """Write to the SyncroSim Run Log (same protocol as ps.update_run_log)."""
    prefix = {"status": "ssim-task-log", "info": "ssim-task-info", "warning": "ssim-task-warning"}[kind]
    for i, line in enumerate(str(msg).split("\n")):
        print(f"{prefix if i == 0 else 'ssim-task-log'}={line}\r", flush=True)


def warn(msg):
    say(msg, "warning")


def version():
    try:
        xml = open(os.path.join(HERE, "package.xml"), encoding="utf-8-sig").read()
        return re.search(r'<package\s[^>]*version="([^"]+)"', xml).group(1)
    except Exception:
        return ""


def read(scn, name, empty_ok=True, **kw):
    try:
        df = scn.datasheets(name=name, **kw)
    except Exception as e:  # sheet missing from this library
        if empty_ok:
            return pd.DataFrame()
        raise sa.AccountError(f"Could not read {name}: {e}")
    if (df is None or df.empty) and not empty_ok:
        raise sa.AccountError(f"{name} is empty. Check that ST-Sim wrote this output.")
    return df if df is not None else pd.DataFrame()


def state_class_table(scn):
    """stsim_StateClass with its Id (raster code) column. It is a project-level datasheet, so
    reading it through the scenario can come back empty; fall back to the project, then to the
    library file itself."""
    def has_id(d):
        return d is not None and not d.empty and any(str(c).lower() == "id" for c in d.columns)

    for get in (lambda: scn.datasheets(name="stsim_StateClass"),
                lambda: scn.project.datasheets(name="stsim_StateClass", return_hidden=True)):
        try:
            d = get()
            if has_id(d):
                return d
        except Exception:
            pass
    import sqlite3
    with sqlite3.connect(scn.library.location) as con:
        d = pd.read_sql_query("SELECT Id, Name FROM stsim_StateClass WHERE ProjectId = ?",
                              con, params=(int(scn.project_id),))
    if not has_id(d):
        raise sa.AccountError("stsim_StateClass has no Id values in this project")
    return d


def save(scn, name, df, columns):
    if df is None or df.empty:
        return
    # SyncroSim 3.2 imports a CSV's rows in reverse order (checked with SyncroSim.Console
    # --import); hand them over reversed so the library keeps the UN row order.
    data = df[[c for c in columns if c in df.columns]].iloc[::-1].reset_index(drop=True)
    scn.save_datasheet(name=f"{PKG}_{name}", data=data)
    say(f"SEEA-Sim: saved {name} ({len(df)} rows)")


def temp_folder(name):
    try:
        return ps.runtime_temp_folder(name)
    except Exception:
        return tempfile.mkdtemp(prefix=name + "-")


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


def flag(opts, col, default=True):
    if opts.empty or col not in opts.columns or pd.isna(opts.iloc[0].get(col)):
        return default
    v = opts.iloc[0][col]
    return str(v).strip().lower() in ("true", "yes", "1") if isinstance(v, str) else bool(v)


def services(scn):
    """Project Service sheet (Name, ServiceType, Units) for the supply tables' unit column
    and service-type groups. Empty if it cannot be read."""
    df = read(scn, f"{PKG}_Service")
    if df.empty:
        try:
            df = read(scn.project, f"{PKG}_Service")
        except Exception:
            df = pd.DataFrame()
    return df


def main():
    scn = ps.Scenario()
    say(f"SEEA-Sim {version()}: building accounts")

    opts = read(scn, f"{PKG}_Options")
    by_stratum = flag(opts, "ReportByStratum", False)
    currency = str(opts.iloc[0].get("Currency")) if not opts.empty and pd.notna(opts.iloc[0].get("Currency")) else ""

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
    say(f"SEEA-Sim: periods {periods}; iterations {state['Iteration'].nunique()}; by stratum = {by_stratum}")

    ttc = read(scn, f"{PKG}_TransitionTypeClass")
    tt_class = ({r.TransitionTypeId: sa.list_value(r.ChangeType, sa.CHANGE_TYPES, "change type")
                 for r in ttc.itertuples()} if not ttc.empty else {})

    tables = {}
    # Extent account and change matrix
    comp = sa.extent_components(state, trans, periods, tt_class, by_stratum, log=warn)
    extent = sa.extent_account(comp)
    matrix, matrix_chk = sa.change_matrix(state, trans, periods, tt_class, by_stratum)
    period_cols = ["OpeningTimestep", "ClosingTimestep", "StratumId"]
    save(scn, "OutputExtent", extent, ["Iteration", *period_cols, "StateClassId", "Entry", "Amount"])
    save(scn, "OutputChangeMatrix", matrix,
         ["Iteration", *period_cols, "FromStateClassId", "ToStateClassId", "Amount"])
    tables.update(OutputExtent=extent, OutputChangeMatrix=matrix)
    checks = [sa.account_checks(extent, "Extent", "Opening extent", "Closing extent",
                                "Net change in extent", sa.EXTENT_REDUCTIONS),
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
        cstock, cflow, cchk, caccount = sa.carbon_accounts(
            stock, flow, periods, groups, membership, fgroups, fmember, by_stratum, log=warn,
            tt_class=tt_class, with_un=True)
        save(scn, "OutputCarbonAccount", caccount,
             ["Iteration", *period_cols, "StateClassId", "StockGroupId", "Entry", "Amount"])
        save(scn, "OutputCarbonStock", cstock,
             ["Iteration", *period_cols, "StateClassId", "StockGroupId", "Entry", "Amount"])
        save(scn, "OutputCarbonFlow", cflow,
             ["Iteration", *period_cols, "StateClassId", "TransitionTypeId", "FromStockTypeId",
              "ToStockTypeId", "Category", "Amount"])
        tables.update(OutputCarbonAccount=caccount, OutputCarbonStock=cstock, OutputCarbonFlow=cflow)
        checks += [cchk, sa.account_checks(caccount, "Carbon stock", "Opening stock",
                                           "Closing stock", "Net carbon balance",
                                           sa.CARBON_REDUCTIONS)]
    else:
        say("SEEA-Sim: no Carbon Stock Groups; carbon accounts skipped.")

    # Ecosystem services and assets
    rules = read(scn, f"{PKG}_ServiceSupply")
    supply = sa.service_supply(state, stock if not stock.empty else None,
                               flow if not flow.empty else None, rules, by_stratum, log=warn)
    ptbl, valuation = None, read(scn, f"{PKG}_AssetValuation")
    if supply is not None:
        out_cols = ["Iteration", "Timestep", "StratumId", "StateClassId", "ServiceId", "Amount"]
        phys = sa._finish(supply.copy())
        save(scn, "OutputServiceSupply", phys, out_cols)
        tables["OutputServiceSupply"] = phys
        prices = read(scn, f"{PKG}_ServicePrice")
        if not prices.empty and "Timestep" not in prices.columns:
            prices["Timestep"] = pd.NA
        price_ts = sorted(set(supply.Timestep.unique()) | {o for o, _ in periods})
        ptbl = sa.price_table(prices, supply.ServiceId.unique(), price_ts, log=warn)
        value = sa.service_value(supply, ptbl)
        if value is not None:
            value = sa._finish(value.copy())
            save(scn, "OutputServiceValue", value, out_cols)
            tables["OutputServiceValue"] = value
        cond = read(scn, f"{PKG}_ConditionChange")
        assets = sa.asset_account(comp, state, supply, ptbl, valuation, by_stratum, log=say,
                                  condition=cond if not cond.empty else None)
        if assets is not None:
            save(scn, "OutputAssetAccount", assets,
                 ["Iteration", *period_cols, "StateClassId", "Entry", "Amount"])
            tables["OutputAssetAccount"] = assets
            checks.append(sa.account_checks(assets, "Asset", "Opening value", "Closing value",
                                            "Net change in value"))
        annual = sa.annual_values(state, supply, ptbl, valuation, by_stratum, log=warn)
        if annual is not None:
            annual = sa._finish(annual)
            save(scn, "OutputAssetValue", annual,
                 ["Iteration", "Timestep", "StratumId", "StateClassId", "ServiceValue", "AssetValue"])
            tables["OutputAssetValue"] = annual
    else:
        say("SEEA-Sim: no Service Supply rules; service and asset accounts skipped.")

    chk = sa.finish_checks(checks)
    save(scn, "OutputCheck", chk, ["Iteration", "Account", *period_cols, "Item", "Residual"])
    tables["OutputCheck"] = chk
    if not chk.empty:
        for (acc, item), v in chk.groupby(["Account", "Item"])["Residual"].max().items():
            (say if abs(v) < 1e-3 else warn)(f"SEEA-Sim check: {acc}: {item}: max {v:.6g}")

    # Value maps (Maps tab)
    if supply is not None and ptbl is not None and flag(opts, "MapOutput", True):
        try:
            import seea_outputs as so
            per_area = sa.annual_values(state, supply if not by_stratum else
                                        sa.service_supply(state, stock if not stock.empty else None,
                                                          flow if not flow.empty else None, rules, False),
                                        ptbl, valuation, False)
            maps = read(scn, "stsim_OutputSpatialState", show_full_paths=True)
            if per_area is None or maps.empty:
                say("SEEA-Sim: no ST-Sim state-class maps or no values; value maps skipped.")
            else:
                codes = so.class_codes(state_class_table(scn))
                years = sorted({c for _, c in periods})
                rows = so.write_value_maps(maps, codes, per_area, years, temp_folder("SEEASimMaps"), log=warn)
                if rows:
                    save(scn, "OutputSpatialValue", pd.DataFrame(rows),
                         ["Iteration", "Timestep", "ServiceValueFile", "AssetValueFile"])
        except Exception as e:
            warn(f"SEEA-Sim: value maps not made: {e}")

    # Excel workbook
    try:
        import seea_outputs as so
        fn = os.path.join(temp_folder("SEEASimWorkbook"), f"SEEA-Sim_accounts_scenario{scn.sid}.xlsx")
        so.write_workbook(fn, tables, {"scenario": f"{scn.name} (scenario {scn.sid})", "version": version(),
                                       "iterations": state["Iteration"].nunique(), "currency": currency,
                                       "services": services(scn), "periods": periods})
        save(scn, "OutputWorkbook", pd.DataFrame({"File": [fn]}), ["File"])
        say("SEEA-Sim: Excel workbook saved (Results > SEEA-Sim Workbook)")
    except Exception as e:
        warn(f"SEEA-Sim: Excel workbook not made: {e}")

    say("SEEA-Sim: done")


if __name__ == "__main__":
    try:
        main()
    except sa.AccountError as e:
        say(f"SEEA-Sim input error: {e}", "warning")
        raise
    except Exception:
        say("SEEA-Sim error:\n" + traceback.format_exc(), "warning")
        raise
