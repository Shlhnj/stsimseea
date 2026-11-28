"""
SEEA-Sim extra outputs: an Excel workbook with the accounts in SEEA layout,
and value maps made from ST-Sim's state-class maps.

No SyncroSim import; transformer.py passes DataFrames and file paths.
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

import seea_accounts as sa

NODATA = -9999.0


# ---------------------------------------------------------------- workbook

def _mean_table(df, rows, cols, value="Amount", row_order=None):
    """Mean over iterations of a long table, rows x cols, with a Total column.
    row_order orders the last row level (e.g. the SEEA entry order)."""
    if df is None or df.empty:
        return None
    n_it = df["Iteration"].nunique()
    t = (df.groupby(rows + cols, dropna=False)[value].sum() / n_it).unstack(cols).fillna(0.0)
    if len(cols) == 1:
        t["Total"] = t.sum(axis=1)
    if row_order is not None:
        t = t.reset_index()
        last = rows[-1]
        t["_o"] = t[last].map({e: i for i, e in enumerate(row_order)}).fillna(len(row_order))
        t = t.sort_values(rows[:-1] + ["_o"]).drop(columns="_o").set_index(rows)
    return t


def _str(df):
    """Ids may be names or numbers; Excel sheets want plain values."""
    if df is None:
        return None
    df = df.copy()
    for c in df.columns:
        if df[c].dtype == object:
            df[c] = df[c].where(df[c].isna(), df[c].astype(str))
    return df


def template(entries, drop=()):
    """UN row layout from 'Group: item' entry names: a bold group header row,
    then its items indented. Returns [(label, entry or None, indent)]."""
    rows, group = [], None
    for e in entries:
        if e in drop:
            continue
        if ": " in e:
            g, item = e.split(": ", 1)
            if g != group:
                rows.append((g, None, 0))
                group = g
            rows.append((item, e, 1))
        else:
            group = None
            rows.append((e, e, 0))
    return rows


SERVICE_GROUPS = {1: "Provisioning services", 2: "Regulating and maintenance services",
                  3: "Cultural services"}


def _service_group(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    try:
        return SERVICE_GROUPS[int(float(v))]
    except (ValueError, KeyError):
        t = str(v).strip().lower()
        for g in SERVICE_GROUPS.values():
            if g.lower().startswith(t):
                return g
        return str(v)


def _blocks(df):
    """Split a long table by stratum (if reported by stratum) and accounting period."""
    df = df.copy()
    if "StratumId" not in df.columns:
        df["StratumId"] = None
    df["_s"] = df["StratumId"].astype(object).where(df["StratumId"].notna(), "")
    for (s, o, c), b in df.groupby(["_s", "OpeningTimestep", "ClosingTimestep"], sort=True):
        yield (f", {s}" if s else ""), int(o), int(c), b


class _Sheet:
    """Writes UN-style tables one under the other on an openpyxl worksheet."""

    def __init__(self, wb, name):
        from openpyxl.styles import Alignment, Font
        self.ws = wb.create_sheet(name[:31])
        self.r = 1
        self.bold, self.Alignment, self.Font = Font(bold=True), Alignment, Font
        self.ws.column_dimensions["A"].width = 46

    def table(self, title, corner, cols, rows, values, total=None, extra=None, note=None,
              fmt="#,##0.00", width=16):
        """
        rows: [(label, key or None, indent)]; values: DataFrame index=key, columns=cols.
        total: label of a right-hand total column (sum over cols), or None.
        extra: (header, {key: text}) for a text column before the value columns.
        """
        from openpyxl.utils import get_column_letter
        ws = self.ws
        ws.cell(self.r, 1, title).font = self.Font(bold=True, size=12)
        self.r += 1
        if note:
            ws.cell(self.r, 1, note).font = self.Font(italic=True)
            self.r += 1
        head = [corner] + ([extra[0]] if extra else []) + [str(c) for c in cols] + ([total] if total else [])
        for j, h in enumerate(head, 1):
            cell = ws.cell(self.r, j, h)
            cell.font = self.bold
            cell.alignment = self.Alignment(wrap_text=True, horizontal="left" if j == 1 else "center")
            if j > 1:
                ws.column_dimensions[get_column_letter(j)].width = max(
                    ws.column_dimensions[get_column_letter(j)].width or 0, width)
        self.r += 1
        off = 2 + (1 if extra else 0)
        for label, key, indent in rows:
            cell = ws.cell(self.r, 1, label)
            cell.alignment = self.Alignment(horizontal="left", indent=2 * indent)
            if key is None:
                cell.font = self.bold
            else:
                if extra:
                    ws.cell(self.r, 2, extra[1].get(key, ""))
                vals = [float(values.at[key, c]) if key in values.index and c in values.columns else 0.0
                        for c in cols]
                if total:
                    vals.append(sum(vals))
                for j, v in enumerate(vals):
                    ws.cell(self.r, off + j, v).number_format = fmt
            self.r += 1
        self.r += 1


def _means(df, index, col="StateClassId", value="Amount", n_it=1):
    t = (df.groupby([index, col], dropna=False)[value].sum() / n_it).unstack(col).fillna(0.0)
    return t


def _classes(*dfs, cols=("StateClassId",)):
    s = set()
    for d in dfs:
        if d is not None:
            for c in cols:
                if c in d.columns:
                    s |= set(d[c].dropna().astype(str))
    return sorted(s)


def write_workbook(path, tables: dict, info: dict):
    """
    tables: name -> long DataFrame as saved to SyncroSim (may be None).
    info:   scenario, version, iterations, currency, services (Service sheet), periods.

    Sheets in the UN SEEA EA (2024) layout, means over iterations:
      Extent account (Table 4.1 / AI.2), Change matrix (Table 4.2 / AI.3),
      Carbon stock account (Table 13.3), Supply physical (Table 7.1a),
      Supply monetary (Table 9.1a), Asset account (Table 10.1 / AI.11).
    Then SEEA-Sim detail (supply by year, asset value by year, carbon by process,
    carbon flows, checks) and the raw per-iteration tables.
    """
    from openpyxl import Workbook

    extent = _str(tables.get("OutputExtent"))
    matrix = _str(tables.get("OutputChangeMatrix"))
    caccount = _str(tables.get("OutputCarbonAccount"))
    cstock = _str(tables.get("OutputCarbonStock"))
    cflow = _str(tables.get("OutputCarbonFlow"))
    phys = _str(tables.get("OutputServiceSupply"))
    mon = _str(tables.get("OutputServiceValue"))
    asset = _str(tables.get("OutputAssetAccount"))
    annual = _str(tables.get("OutputAssetValue"))
    checks = _str(tables.get("OutputCheck"))
    currency = info.get("currency") or "currency units"

    n_it = max(1, int(info.get("iterations") or 1))
    for d in (extent, matrix, caccount, phys, mon, asset):
        if d is not None and not d.empty:
            n_it = d["Iteration"].nunique()
            break
    classes = _classes(extent, asset, phys, mon, caccount) or _classes(matrix, cols=("FromStateClassId", "ToStateClassId"))

    wb = Workbook()
    about = wb.active
    about.title = "About"
    notes = [
        "SEEA-Sim accounts, " + str(info.get("scenario", "")),
        f"SEEA-Sim {info.get('version', '')}; {info.get('iterations', '?')} iterations. "
        "The UN-layout sheets show means over iterations; the 'raw' sheets hold every iteration.",
        "Areas in the ST-Sim amount unit (Terminology); carbon in the ST-Sim stock unit; "
        f"money in {currency}.",
        "Layout follows UN SEEA EA (2024): Extent account = Table 4.1/AI.2; Change matrix = Table 4.2/AI.3; "
        "Carbon stock account = Table 13.3; Supply = Tables 7.1a and 9.1a; Asset account = Table 10.1/AI.11.",
        "Signs as in the UN tables: extent and carbon reductions are positive amounts (net change = additions "
        "minus reductions); in the monetary asset account decreases are negative (net change = sum of entries).",
        "Change matrix: off-diagonal cells are gross transitions summed over the period's timesteps; the UN "
        "compares the opening and closing maps. They differ only where a cell changed more than once in the period.",
        "Carbon stock account: columns are ecosystem types (biocarbon in ecosystems). Flows triggered by "
        "transition types classed Managed are managed expansion/contraction, Catastrophic ones catastrophic "
        "losses, all others unmanaged. Reclassifications = carbon that moved with converted area (net per "
        "type; they cancel over all types). Discoveries, imports and exports are geocarbon/economy entries (0).",
        "Supply tables cover each accounting period: the sum of years opening+1 to closing. 'by year' sheets "
        "show each year. Physical supply is not totalled over services (different units, SEEA EA 2.78).",
        "Ecosystem services are grouped by the Service sheet's Type; units from its Physical Units column.",
    ]
    for i, n in enumerate(notes, 1):
        about.cell(i, 1, n)
    about.column_dimensions["A"].width = 140

    # Extent account
    if extent is not None and not extent.empty:
        sh = _Sheet(wb, "Extent account")
        for strat, o, c, b in _blocks(extent):
            sh.table(f"Ecosystem extent account, {o}-{c}{strat} (area units)", "Accounting entries",
                     classes, template(sa.EXTENT_ENTRIES), _means(b, "Entry", n_it=n_it), total="TOTAL")

    # Change matrix
    if matrix is not None and not matrix.empty:
        sh = _Sheet(wb, "Change matrix")
        for strat, o, c, b in _blocks(matrix):
            m = _means(b.rename(columns={"ToStateClassId": "To"}), "FromStateClassId", col="To", n_it=n_it)
            m = m.reindex(index=classes, columns=classes).fillna(0.0)
            m["Opening extent"] = m.sum(axis=1)
            m.loc["Closing extent"] = m.sum(axis=0)
            rows = [(k, k, 0) for k in classes] + [("Closing extent", "Closing extent", 0)]
            sh.table(f"Ecosystem type change matrix, {o}-{c}{strat} (area units)",
                     "Ecosystem types: opening (rows), closing (columns)", classes + ["Opening extent"], rows, m)

    # Carbon stock account (Table 13.3)
    if caccount is not None and not caccount.empty:
        sh = _Sheet(wb, "Carbon stock account")
        for strat, o, c, b in _blocks(caccount):
            parts = [("all ecosystem carbon pools", b)] + [
                (f"pool {g}", bg) for g, bg in b.groupby("StockGroupId", sort=True)]
            for what, bb in parts:
                sh.table(f"Carbon stock account, {o}-{c}{strat}: biocarbon in ecosystems, {what} "
                         "(carbon units)", "Accounting entries", classes, template(sa.CARBON_ENTRIES),
                         _means(bb, "Entry", n_it=n_it), total="Total")

    # Supply tables
    svc = info.get("services")
    unit, group = {}, {}
    if svc is not None and not getattr(svc, "empty", True):
        for _, r in svc.iterrows():
            unit[str(r.get("Name"))] = "" if pd.isna(r.get("Units")) else str(r.get("Units"))
            group[str(r.get("Name"))] = _service_group(r.get("ServiceType"))

    def service_rows(names, monetary):
        order = list(SERVICE_GROUPS.values())
        keyed = sorted(names, key=lambda n: (order.index(group[n]) if group.get(n) in order else len(order),
                                             str(group.get(n) or ""), n))
        rows, g0 = [], object()
        for n in keyed:
            g = group.get(n)
            if g and g != g0:
                rows.append((g, None, 0))
            rows.append((n, n, 1 if g else 0))
            g0 = g
        if monetary:
            rows.append(("Total ecosystem services", "__total__", 0))
        return rows

    periods = info.get("periods")
    if not periods:
        src = next((d for d in (extent, asset, caccount) if d is not None and not d.empty), None)
        periods = [] if src is None else sorted(
            {(int(o), int(c)) for o, c in src[["OpeningTimestep", "ClosingTimestep"]].itertuples(index=False)})
    for df, monetary, name, tbl in ((phys, False, "Supply physical", "7.1a"), (mon, True, "Supply monetary", "9.1a")):
        if df is None or df.empty:
            continue
        what = (f"Ecosystem services supply table in monetary terms ({currency})" if monetary
                else "Ecosystem services supply table in physical terms")
        names = sorted(df["ServiceId"].dropna().unique())
        sdf = df.copy()
        if "StratumId" not in sdf.columns:
            sdf["StratumId"] = None
        sdf["_s"] = sdf["StratumId"].astype(object).where(sdf["StratumId"].notna(), "")

        def put(sh, title, part):
            v = _means(part, "ServiceId", n_it=n_it)
            if monetary:
                v.loc["__total__"] = v.sum(axis=0)
            sh.table(title, "Ecosystem services", classes, service_rows(names, monetary), v,
                     total="Total supply by ecosystem assets",
                     extra=None if monetary else ("Measurement unit", unit),
                     fmt="#,##0" if monetary else "#,##0.00", width=22 if monetary else 16)

        sh = _Sheet(wb, name)
        for s_, part_s in sdf.groupby("_s", sort=True):
            strat = f", {s_}" if s_ else ""
            for o, c in periods:
                part = part_s[(part_s.Timestep > o) & (part_s.Timestep <= c)]
                if not part.empty:
                    put(sh, f"{what}, {o}-{c}{strat} (sum of years {o + 1}-{c}; SEEA EA Table {tbl})", part)
        sh = _Sheet(wb, f"{name} by year")
        for s_, part_s in sdf.groupby("_s", sort=True):
            strat = f", {s_}" if s_ else ""
            for t, part in part_s.groupby("Timestep", sort=True):
                put(sh, f"{what}, {int(t)}{strat}", part)

    # Monetary asset account (Table 10.1 / AI.11)
    if asset is not None and not asset.empty:
        drop = () if (asset.loc[asset.Entry == sa.OTHER_VOLUME, "Amount"].abs() > 1e-9).any() else (sa.OTHER_VOLUME,)
        sh = _Sheet(wb, "Asset account")
        for strat, o, c, b in _blocks(asset):
            sh.table(f"Monetary ecosystem asset account, {o}-{c}{strat} ({currency})", "Accounting entries",
                     classes, template(sa.ASSET_ENTRIES, drop=drop), _means(b, "Entry", n_it=n_it),
                     total="TOTAL", fmt="#,##0", width=22)

    # SEEA-Sim detail and raw tables (plain pandas layout)
    detail = []
    if annual is not None:
        detail.append(("Asset value by year", _mean_table(annual, ["Timestep"], ["StateClassId"], value="AssetValue")))
    if cstock is not None:
        detail.append(("Carbon by process", _mean_table(cstock, ["OpeningTimestep", "ClosingTimestep", "StockGroupId",
                                                                 "Entry"], ["StateClassId"],
                                                        row_order=sa.STOCK_ENTRIES)))
    if cflow is not None:
        detail.append(("Carbon flows", _mean_table(cflow, ["OpeningTimestep", "ClosingTimestep", "Category"],
                                                   ["StateClassId"])))
    if checks is not None and not checks.empty:
        detail.append(("Checks", checks.groupby(["Account", "Item"])["Residual"].max()
                       .rename("Max residual over iterations").to_frame()))
    raw = (("raw Extent", extent), ("raw Change matrix", matrix), ("raw Carbon account", caccount),
           ("raw Carbon by process", cstock), ("raw Carbon flows", cflow), ("raw Supply physical", phys),
           ("raw Supply monetary", mon), ("raw Asset account", asset), ("raw Asset value by year", annual),
           ("raw Checks", checks))
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        side = os.path.join(tmp, "detail.xlsx")
        with pd.ExcelWriter(side, engine="openpyxl") as xw:
            pd.DataFrame({"": [""]}).to_excel(xw, sheet_name="_", index=False)
            for name, t in detail:
                if t is not None and not t.empty:
                    t.to_excel(xw, sheet_name=name[:31])
            for name, t in raw:
                if t is not None and not t.empty:
                    t.to_excel(xw, sheet_name=name[:31], index=False)
        _copy_sheets(side, wb, skip=("_",))
    wb.save(path)
    return path


def _copy_sheets(src_path, wb, skip=()):
    """Append the values of every sheet of src_path to workbook wb."""
    from openpyxl import load_workbook
    src = load_workbook(src_path)
    for ws in src.worksheets:
        if ws.title in skip:
            continue
        dst = wb.create_sheet(ws.title)
        for row in ws.iter_rows(values_only=True):
            dst.append(list(row))


# ---------------------------------------------------------------- maps

def class_codes(stateclass: pd.DataFrame) -> dict:
    """stsim_StateClass -> {raster value: state class name}. The raster holds the 'Id' column."""
    idcol = next((c for c in stateclass.columns if c.lower() == "id"), None)
    if idcol is None:
        raise sa.AccountError(f"stsim_StateClass has no Id column; columns {list(stateclass.columns)}")
    d = stateclass.dropna(subset=[idcol])
    return {int(r[idcol]): r["Name"] for _, r in d.iterrows()}


def map_years(periods) -> list:
    """Years to map: every accounting period boundary (opening and closing years).
    With one period per year this gives a map for every year."""
    return sorted({int(y) for p in periods for y in p})


def per_area_for_years(per_area: pd.DataFrame, years) -> pd.DataFrame:
    """Per-area values for each map year, column by column. AssetValuePerArea is the
    value at that date (flows expected from the next year). ServiceValuePerArea is the
    flow in that year; the first simulated year has none, so it takes the next year
    that has one, as the asset account does for its opening value."""
    keys = ["Iteration", "StateClassId"]
    parts = []
    for y in years:
        cols = {}
        for col in ("ServiceValuePerArea", "AssetValuePerArea"):
            have = sorted(int(t) for t in per_area.loc[per_area[col].notna(), "Timestep"].unique())
            src = y if y in have else next((t for t in have if t > y), None)
            if src is not None:
                cols[col] = per_area.loc[(per_area.Timestep == src) & per_area[col].notna(),
                                         keys + [col]]
        if not cols:
            continue
        part = None
        for d in cols.values():
            part = d if part is None else part.merge(d, on=keys, how="outer")
        part = part.assign(Timestep=y)
        for col in ("ServiceValuePerArea", "AssetValuePerArea"):
            if col not in part:
                part[col] = 0.0
        parts.append(part.fillna(0.0))
    if not parts:
        return per_area.iloc[0:0]
    return pd.concat(parts, ignore_index=True)[["Iteration", "Timestep", "StateClassId",
                                                "ServiceValuePerArea", "AssetValuePerArea"]]


def write_value_maps(state_maps: pd.DataFrame, codes: dict, per_area: pd.DataFrame,
                     timesteps, out_dir: str, log=None):
    """
    state_maps: stsim_OutputSpatialState with full paths (Iteration, Timestep, Filename[, Band]).
    per_area:   annual_values(..., by_stratum=False) (Iteration, Timestep, StateClassId,
                ServiceValuePerArea, AssetValuePerArea).
    Writes two float32 GeoTIFFs per iteration and timestep: service value per area unit per
    year, and asset value per area unit. Returns rows for OutputSpatialValue.
    """
    import rasterio

    os.makedirs(out_dir, exist_ok=True)
    rows = []
    maps = state_maps[state_maps["Timestep"].isin(timesteps)]
    if maps.empty:
        sa._log(log, "SEEA-Sim: no ST-Sim state-class maps at the accounting years "
                     f"{sorted(timesteps)}; value maps skipped. Turn on Output Options > Spatial > "
                     "State classes for those years.")
        return rows
    for _, m in maps.iterrows():
        it, ts = int(m["Iteration"]), int(m["Timestep"])
        pa = per_area[(per_area.Iteration == it) & (per_area.Timestep == ts)].set_index("StateClassId")
        band = int(m["Band"]) if "Band" in m and pd.notna(m["Band"]) else 1
        with rasterio.open(m["Filename"]) as src:
            cls = src.read(band)
            nod = src.nodata
            profile = src.profile.copy()
        profile.update(dtype="float32", count=1, nodata=NODATA, compress="deflate")
        valid = np.ones(cls.shape, bool) if nod is None else (cls != nod)
        lut_max = int(max(max(codes), int(cls[valid].max()) if valid.any() else 0)) + 1
        out = {}
        for col, tag in (("ServiceValuePerArea", "value"), ("AssetValuePerArea", "asset")):
            lut = np.full(lut_max, NODATA, dtype="float32")
            for code, name in codes.items():
                if code < lut_max:
                    lut[code] = float(pa[col].get(name, 0.0)) if name in pa.index else 0.0
            arr = np.full(cls.shape, NODATA, dtype="float32")
            ok = valid & (cls >= 0) & (cls < lut_max)
            arr[ok] = lut[cls[ok].astype(int)]
            fn = os.path.join(out_dir, f"seeasim_{tag}_it{it}_ts{ts}.tif")
            with rasterio.open(fn, "w", **profile) as dst:
                dst.write(arr, 1)
            out[tag] = fn
        rows.append({"Iteration": it, "Timestep": ts,
                     "ServiceValueFile": out["value"], "AssetValueFile": out["asset"]})
    return rows
