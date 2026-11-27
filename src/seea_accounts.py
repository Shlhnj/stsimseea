"""
SEEA-Sim accounting core.

Pure pandas. No SyncroSim import, so it can be tested anywhere. The
transformer (transformer.py) reads ST-Sim datasheets, passes them here as
DataFrames and writes the results back.

Inputs use ST-Sim 4.5 output column names:
  stsim_OutputStratumState          Iteration, Timestep, StratumId, StateClassId, Amount
  stsim_OutputStratumTransitionState Iteration, Timestep, StratumId, TransitionTypeId,
                                    StateClassId, EndStateClassId, Amount
  stsim_OutputStock                 Iteration, Timestep, StratumId, StateClassId,
                                    StockGroupId, Amount
  stsim_OutputFlow                  Iteration, Timestep, FromStratumId, FromStateClassId,
                                    FromStockTypeId, ToStockTypeId, TransitionTypeId,
                                    FlowGroupId, Amount
Id columns can hold names or integers; values are only used as keys.

Conventions
  * A period (o, c) covers the transitions and flows of timesteps o+1 .. c.
    Opening = state at the end of timestep o, closing = state at the end of c.
  * Entry labels and signs follow the UN SEEA EA (2024) tables. Physical
    accounts (extent, Table 4.1/AI.2; carbon stock, Table 13.3) record
    reductions as positive amounts: Net change = additions - reductions.
    The monetary asset account (Table 10.1/AI.11) records decreases as
    negative amounts: Net change = sum of the change entries. The carbon
    process detail (carbon_accounts' first table) is not a UN table and
    uses negative reductions.
  * Every account is computed per iteration. SyncroSim summarises across
    iterations.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

ALL = "__all__"  # placeholder stratum when results are not split by stratum

MANAGED, UNMANAGED, CATASTROPHIC = "Managed", "Unmanaged", "Catastrophic"
CHANGE_TYPES = {1: MANAGED, 2: UNMANAGED, 3: CATASTROPHIC}

POOL, ATMOSPHERE, WATER, BURIAL, OTHER = (
    "Ecosystem pool", "Atmosphere", "Water", "Long-term burial", "Other external")
ROLES = {1: POOL, 2: ATMOSPHERE, 3: WATER, 4: BURIAL, 5: OTHER}

AREA, STOCK, FLOW = "Area", "Stock group", "Flow group"
BASES = {1: AREA, 2: STOCK, 3: FLOW}

PERIOD = ["OpeningTimestep", "ClosingTimestep"]


class AccountError(ValueError):
    """Input problem the user has to fix; the message says how."""


def _log(log, msg):
    (log or print)(msg)


def list_value(v, mapping: dict, what: str):
    """SyncroSim List columns may arrive as 1, '1' or the label."""
    if pd.isna(v):
        raise AccountError(f"{what} is empty.")
    labels = {s.lower(): s for s in mapping.values()}
    if isinstance(v, str) and v.strip().lower() in labels:
        return labels[v.strip().lower()]
    try:
        return mapping[int(float(v))]
    except (ValueError, KeyError):
        raise AccountError(f"Unknown {what}: {v!r}. Allowed: {list(mapping.values())}")


def annuity_factor(rate: float, life: int) -> float:
    """Present value of 1 per year for `life` years at `rate`."""
    if rate == 0:
        return float(life)
    return (1 - (1 + rate) ** -life) / rate


def _need(df: pd.DataFrame, cols, name: str):
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise AccountError(f"{name} is missing column(s) {missing}; found {list(df.columns)}.")


def _stratum(df: pd.DataFrame, col: str, by_stratum: bool) -> pd.DataFrame:
    df = df.copy()
    df["StratumId"] = df[col] if by_stratum else ALL
    return df


def _finish(df: pd.DataFrame) -> pd.DataFrame:
    """Replace the placeholder stratum with null before saving."""
    if "StratumId" in df.columns:
        df["StratumId"] = df["StratumId"].astype(object).where(df["StratumId"] != ALL, None)
    return df.reset_index(drop=True)


def default_periods(state: pd.DataFrame) -> list[tuple[int, int]]:
    return [(int(state["Timestep"].min()), int(state["Timestep"].max()))]


def _check_timesteps(df: pd.DataFrame, timesteps, name: str):
    have = set(df["Timestep"].unique())
    missing = sorted(t for t in timesteps if t not in have)
    if missing:
        raise AccountError(
            f"{name} has no output for timestep(s) {missing}. Set its summary output "
            f"frequency so these timesteps are written (every timestep is safest).")


# ---------------------------------------------------------------- extent

# SEEA EA Table 4.1 / AI.2. "Group: item" = item under that group's sub-header.
EXTENT_ENTRIES = [
    "Opening extent",
    "Additions to extent: Managed expansions",
    "Additions to extent: Unmanaged expansions",
    "Reductions in extent: Managed reductions",
    "Reductions in extent: Unmanaged reductions",
    "Net change in extent",
    "Closing extent",
]
EXTENT_REDUCTIONS = [e for e in EXTENT_ENTRIES if e.startswith("Reductions in extent")]


def _prep_state(state, by_stratum):
    _need(state, ["Iteration", "Timestep", "StratumId", "StateClassId", "Amount"],
          "stsim_OutputStratumState")
    df = _stratum(state, "StratumId", by_stratum)
    return df.groupby(["Iteration", "StratumId", "Timestep", "StateClassId"],
                      as_index=False)["Amount"].sum()


def _prep_trans(trans, tt_class: dict, by_stratum, log):
    _need(trans, ["Iteration", "Timestep", "StratumId", "TransitionTypeId",
                  "StateClassId", "EndStateClassId", "Amount"],
          "stsim_OutputStratumTransitionState")
    df = _stratum(trans, "StratumId", by_stratum)
    df = df[df["StateClassId"] != df["EndStateClassId"]].copy()
    unlisted = sorted({str(t) for t in df["TransitionTypeId"].unique() if t not in tt_class})
    if unlisted:
        _log(log, "SEEA-Sim: transition types not in Transition Type Classification are "
                  f"treated as Unmanaged: {', '.join(unlisted)}")
    df["ChangeType"] = df["TransitionTypeId"].map(tt_class).fillna(UNMANAGED)
    return df


def extent_components(state, trans, periods, tt_class, by_stratum=False, log=None):
    """
    One row per (Iteration, StratumId, period, StateClassId) with
    Opening, Closing, Add_<type>, Red_<type> (positive areas) and Residual
    (= closing - opening - additions + reductions; zero unless something
    other than recorded transitions changed the class areas).
    """
    st = _prep_state(state, by_stratum)
    tr = _prep_trans(trans, tt_class, by_stratum, log)
    keys = ["Iteration", "StratumId"]
    out = []
    for o, c in periods:
        _check_timesteps(st, [o, c], "stsim_OutputStratumState")
        op = st[st.Timestep == o].drop(columns="Timestep").rename(columns={"Amount": "Opening"})
        cl = st[st.Timestep == c].drop(columns="Timestep").rename(columns={"Amount": "Closing"})
        t = tr[(tr.Timestep > o) & (tr.Timestep <= c)]
        add = (t.groupby(keys + ["EndStateClassId", "ChangeType"])["Amount"].sum()
                .unstack("ChangeType").add_prefix("Add_").reset_index()
                .rename(columns={"EndStateClassId": "StateClassId"}))
        red = (t.groupby(keys + ["StateClassId", "ChangeType"])["Amount"].sum()
                .unstack("ChangeType").add_prefix("Red_").reset_index())
        df = op.merge(cl, on=keys + ["StateClassId"], how="outer")
        for part in (add, red):
            if not part.empty:
                df = df.merge(part, on=keys + ["StateClassId"], how="outer")
        for p in ("Add_", "Red_"):
            for ct in (MANAGED, UNMANAGED, CATASTROPHIC):
                if p + ct not in df.columns:
                    df[p + ct] = 0.0
        df = df.fillna({c_: 0.0 for c_ in df.columns if c_ not in keys + ["StateClassId"]})
        df["OpeningTimestep"], df["ClosingTimestep"] = o, c
        out.append(df)
    comp = pd.concat(out, ignore_index=True)
    adds = comp[[f"Add_{t}" for t in (MANAGED, UNMANAGED, CATASTROPHIC)]].sum(axis=1)
    reds = comp[[f"Red_{t}" for t in (MANAGED, UNMANAGED, CATASTROPHIC)]].sum(axis=1)
    comp["Residual"] = comp["Closing"] - comp["Opening"] - adds + reds
    return comp


def extent_account(comp: pd.DataFrame) -> pd.DataFrame:
    vals = {
        "Opening extent": comp["Opening"],
        "Additions to extent: Managed expansions": comp["Add_Managed"],
        "Additions to extent: Unmanaged expansions": comp["Add_Unmanaged"] + comp["Add_Catastrophic"],
        "Reductions in extent: Managed reductions": comp["Red_Managed"],
        "Reductions in extent: Unmanaged reductions": comp["Red_Unmanaged"] + comp["Red_Catastrophic"],
        "Net change in extent": comp["Closing"] - comp["Opening"],
        "Closing extent": comp["Closing"],
    }
    base = comp[["Iteration", "StratumId", *PERIOD, "StateClassId"]]
    rows = [base.assign(Entry=e, Amount=vals[e].values) for e in EXTENT_ENTRIES]
    return _finish(_order(pd.concat(rows, ignore_index=True), EXTENT_ENTRIES))


def _order(df, entries, keys=("StateClassId",)):
    """Sort rows by iteration, stratum, period, keys, then the UN entry order."""
    df = df.copy()
    df["_o"] = df["Entry"].map({e: i for i, e in enumerate(entries)})
    by = [c for c in ["Iteration", "StratumId", *PERIOD, *keys] if c in df.columns]
    df["_s"] = df["StratumId"].astype(str) if "StratumId" in df.columns else ""
    by = ["_s" if c == "StratumId" else c for c in by]
    df = df.sort_values(by + ["_o"], kind="stable").drop(columns=["_o", "_s"])
    return df.reset_index(drop=True)


def change_matrix(state, trans, periods, tt_class, by_stratum=False, log=None):
    """
    From x To areas per period. Off-diagonal cells are gross transitions
    summed over the period's timesteps; the diagonal is opening area minus
    everything that left the class. Row totals equal opening extent and
    column totals equal closing extent. A cell that changes more than once
    in a period (A to B to A) is counted in every move, so off-diagonals are
    gross flows, not a map-to-map comparison, and the diagonal understates
    the area that never changed.
    """
    st = _prep_state(state, by_stratum)
    tr = _prep_trans(trans, tt_class, by_stratum, log=lambda m: None)
    keys = ["Iteration", "StratumId"]
    out, checks = [], []
    for o, c in periods:
        t = tr[(tr.Timestep > o) & (tr.Timestep <= c)]
        off = (t.groupby(keys + ["StateClassId", "EndStateClassId"], as_index=False)["Amount"].sum()
                .rename(columns={"StateClassId": "FromStateClassId",
                                 "EndStateClassId": "ToStateClassId"}))
        op = st[st.Timestep == o].drop(columns="Timestep")
        left = off.groupby(keys + ["FromStateClassId"], as_index=False)["Amount"].sum()
        left = left.rename(columns={"FromStateClassId": "StateClassId", "Amount": "Left"})
        diag = op.merge(left, on=keys + ["StateClassId"], how="left").fillna({"Left": 0.0})
        diag = diag.assign(FromStateClassId=diag.StateClassId, ToStateClassId=diag.StateClassId,
                           Amount=diag.Amount - diag.Left)
        m = pd.concat([diag[keys + ["FromStateClassId", "ToStateClassId", "Amount"]], off],
                      ignore_index=True)
        m["OpeningTimestep"], m["ClosingTimestep"] = o, c
        out.append(m)
        col = m.groupby(keys + ["ToStateClassId"], as_index=False)["Amount"].sum()
        cl = st[st.Timestep == c].drop(columns="Timestep").rename(
            columns={"StateClassId": "ToStateClassId", "Amount": "Closing"})
        chk = col.merge(cl, on=keys + ["ToStateClassId"], how="outer").fillna(0.0)
        chk["Residual"] = (chk.Amount - chk.Closing).abs()
        chk = chk.groupby(keys, as_index=False)["Residual"].sum()
        chk = chk.assign(Account="Change matrix", OpeningTimestep=o, ClosingTimestep=c,
                         Item="Column totals minus closing extent (should be 0)")
        checks.append(chk)
    return _finish(pd.concat(out, ignore_index=True)), pd.concat(checks, ignore_index=True)


# ---------------------------------------------------------------- carbon

# Carbon by process (SEEA-Sim detail, not a UN table; reductions negative).
STOCK_ENTRIES = [
    "Opening stock",
    "Additions: sequestration",
    "Additions: other inputs",
    "Transfers in from other pools",
    "Reductions: emissions (respiration and decomposition)",
    "Reductions: emissions (conversion and disturbance)",
    "Reductions: export to water",
    "Reductions: transfer to long-term burial",
    "Reductions: other losses",
    "Transfers out to other pools",
    "Change from ecosystem conversion",
    "Net change in stock",
    "Closing stock",
]


# SEEA EA Table 13.3 (carbon stock account) rows; reductions positive.
CARBON_ENTRIES = [
    "Opening stock",
    "Additions to stock: Unmanaged expansion",
    "Additions to stock: Managed expansion",
    "Additions to stock: Discoveries",
    "Additions to stock: Reclassifications",
    "Additions to stock: Imports",
    "Reductions in stock: Unmanaged contraction",
    "Reductions in stock: Managed contraction",
    "Reductions in stock: Reclassifications",
    "Reductions in stock: Exports",
    "Reductions in stock: Catastrophic losses",
    "Net carbon balance",
    "Closing stock",
]
CARBON_REDUCTIONS = [e for e in CARBON_ENTRIES if e.startswith("Reductions in stock")]


def stock_type_roles(groups: pd.DataFrame, membership: pd.DataFrame):
    """
    groups: StockGroupId, Role. membership: StockTypeId, StockGroupId
    (stsim_StockTypeGroupMembership). Returns ({type: role}, {pool type: pool group}).
    An auto-generated group named '<type> [Type]' with no membership rows is
    read as containing that one type.
    """
    role_of, pool_of = {}, {}
    for _, g in groups.iterrows():
        gid, role = g["StockGroupId"], list_value(g["Role"], ROLES, "carbon stock group role")
        types = list(membership.loc[membership["StockGroupId"] == gid, "StockTypeId"])
        if not types and isinstance(gid, str) and gid.endswith(" [Type]"):
            types = [gid[: -len(" [Type]")]]
        if not types:
            raise AccountError(
                f"Carbon stock group '{gid}' has no stock types in Stock Type-Group "
                f"Membership, so its flows cannot be classified.")
        for t in types:
            if t in role_of and (role_of[t] != role or role == POOL):
                raise AccountError(
                    f"Stock type '{t}' is in more than one listed carbon stock group. "
                    f"List groups that do not overlap (e.g. AGB, BGB, Soil, not also Total).")
            role_of[t] = role
            if role == POOL:
                pool_of[t] = gid
    return role_of, pool_of


def select_flow_rows(flow: pd.DataFrame, flow_groups: list, flow_membership=None):
    """
    OutputFlow has one row per flow group, so a flow type in two groups is
    reported twice. Use only the listed groups (they must not share flow
    types) or, if none are listed, ST-Sim's auto groups '<type> [Type]'.
    """
    if flow_groups:
        if flow_membership is not None and not flow_membership.empty:
            fm = flow_membership[flow_membership["FlowGroupId"].isin(flow_groups)]
            dup = fm[fm.duplicated("FlowTypeId", keep=False)]
            if not dup.empty:
                raise AccountError(
                    "These flow types are in more than one listed carbon flow group and "
                    f"would be counted twice: {sorted(map(str, dup.FlowTypeId.unique()))}")
        rows = flow[flow["FlowGroupId"].isin(flow_groups)]
    else:
        auto = flow["FlowGroupId"].astype(str).str.endswith(" [Type]")
        if not auto.any():
            raise AccountError(
                "No Carbon Flow Groups listed and no auto flow groups ('<type> [Type]') in "
                "stsim_OutputFlow. List flow groups that together hold every carbon flow once.")
        rows = flow[auto]
    return rows


def classify_flows(flow, role_of, by_stratum):
    _need(flow, ["Iteration", "Timestep", "FromStratumId", "FromStateClassId", "FromStockTypeId",
                 "ToStockTypeId", "TransitionTypeId", "Amount"], "stsim_OutputFlow")
    if flow["FromStockTypeId"].isna().all() or flow["ToStockTypeId"].isna().all():
        raise AccountError(
            "stsim_OutputFlow has no From/To stock types. In Stock-Flow Output options, "
            "untick 'omit From stock type' and 'omit To stock type'.")
    df = _stratum(flow, "FromStratumId", by_stratum).rename(columns={"FromStateClassId": "StateClassId"})
    df["FromRole"] = df["FromStockTypeId"].map(role_of).fillna(OTHER)
    df["ToRole"] = df["ToStockTypeId"].map(role_of).fillna(OTHER)
    fp, tp = df.FromRole == POOL, df.ToRole == POOL
    by_transition = df["TransitionTypeId"].notna()
    cat = np.select(
        [fp & tp,
         ~fp & tp & (df.FromRole == ATMOSPHERE),
         ~fp & tp,
         fp & (df.ToRole == ATMOSPHERE) & by_transition,
         fp & (df.ToRole == ATMOSPHERE),
         fp & (df.ToRole == WATER),
         fp & (df.ToRole == BURIAL),
         fp],
        ["Internal transfer",
         "Sequestration",
         "Other inputs",
         "Emissions (conversion and disturbance)",
         "Emissions (respiration and decomposition)",
         "Export to water",
         "Transfer to long-term burial",
         "Other losses"],
        default="")
    df["Category"] = cat
    return df[df.Category != ""]


def carbon_accounts(stock, flow, periods, groups, membership, flow_groups=None,
                    flow_membership=None, by_stratum=False, log=None, tt_class=None,
                    with_un=False):
    """
    Returns (stock changes by process, flow account, checks), or with
    with_un=True also the SEEA EA Table 13.3 account as a fourth item
    (None for all if no carbon stock groups are listed).

    Table 13.3 entries, per class and ecosystem pool:
      flows into the pool       -> Managed expansion if the flow was triggered by a
                                   Managed transition type, else Unmanaged expansion
      flows out of the pool     -> Managed contraction (Managed transition),
                                   Catastrophic losses (Catastrophic transition),
                                   else Unmanaged contraction
      carbon that moved with area converted to or from the class
                                -> Reclassifications (net: additions if it gained,
                                   reductions if it lost); zero over all classes
      Discoveries, Imports, Exports are geocarbon or economy entries: always 0.
    Transfers between pools of the same class appear in each pool and cancel
    in the sum over pools.
    """
    tt_class = tt_class or {}
    if groups is None or groups.empty:
        _log(log, "SEEA-Sim: no Carbon Stock Groups listed; carbon accounts skipped.")
        return (None, None, None, None) if with_un else (None, None, None)
    role_of, pool_of = stock_type_roles(groups, membership)
    pools = set(pool_of.values())
    if not pools:
        raise AccountError("No carbon stock group has Role = Ecosystem pool.")
    _need(stock, ["Iteration", "Timestep", "StratumId", "StateClassId", "StockGroupId", "Amount"],
          "stsim_OutputStock")
    st = _stratum(stock[stock["StockGroupId"].isin(pools)], "StratumId", by_stratum)
    st = st.groupby(["Iteration", "StratumId", "Timestep", "StateClassId", "StockGroupId"],
                    as_index=False)["Amount"].sum()
    fl = classify_flows(select_flow_rows(flow, flow_groups, flow_membership), role_of, by_stratum)

    fl["Mode"] = [tt_class.get(t, UNMANAGED) if pd.notna(t) else UNMANAGED
                  for t in fl["TransitionTypeId"]]

    keys = ["Iteration", "StratumId", "StateClassId", "StockGroupId"]
    stock_rows, flow_rows, un_rows, checks = [], [], [], []
    for o, c in periods:
        _check_timesteps(st, [o, c], "stsim_OutputStock")
        f = fl[(fl.Timestep > o) & (fl.Timestep <= c)]

        fa = (f.groupby(["Iteration", "StratumId", "StateClassId", "TransitionTypeId",
                         "FromStockTypeId", "ToStockTypeId", "Category"],
                        as_index=False, dropna=False)["Amount"].sum())
        fa["OpeningTimestep"], fa["ClosingTimestep"] = o, c
        flow_rows.append(fa)

        inflow = f[f.ToRole == POOL].assign(StockGroupId=lambda d: d.ToStockTypeId.map(pool_of))
        inflow["Entry"] = inflow.Category.map({
            "Sequestration": "Additions: sequestration",
            "Other inputs": "Additions: other inputs",
            "Internal transfer": "Transfers in from other pools"})
        outflow = f[f.FromRole == POOL].assign(StockGroupId=lambda d: d.FromStockTypeId.map(pool_of))
        outflow["Entry"] = outflow.Category.map(
            lambda x: "Transfers out to other pools" if x == "Internal transfer"
            else f"Reductions: {x[0].lower()}{x[1:]}")
        outflow = outflow.assign(Amount=-outflow.Amount)
        moves = (pd.concat([inflow, outflow])
                 .groupby(keys + ["Entry"])["Amount"].sum().unstack("Entry"))

        op = st[st.Timestep == o].set_index(keys)["Amount"].rename("Opening stock")
        cl = st[st.Timestep == c].set_index(keys)["Amount"].rename("Closing stock")
        w = pd.concat([op, cl, moves], axis=1).fillna(0.0)
        for e in STOCK_ENTRIES:
            if e not in w.columns:
                w[e] = 0.0
        change_cols = [e for e in STOCK_ENTRIES[1:10]]
        w["Net change in stock"] = w["Closing stock"] - w["Opening stock"]
        w["Change from ecosystem conversion"] = w["Net change in stock"] - w[change_cols].sum(axis=1)
        w = w[STOCK_ENTRIES]

        # SEEA EA Table 13.3
        inn = inflow.assign(Entry=np.where(inflow.Mode == MANAGED,
                                           "Additions to stock: Managed expansion",
                                           "Additions to stock: Unmanaged expansion"))
        out_ = outflow.assign(Amount=-outflow.Amount, Entry=np.select(
            [outflow.Mode == MANAGED, outflow.Mode == CATASTROPHIC],
            ["Reductions in stock: Managed contraction", "Reductions in stock: Catastrophic losses"],
            default="Reductions in stock: Unmanaged contraction"))
        u = (pd.concat([inn, out_]).groupby(keys + ["Entry"])["Amount"].sum().unstack("Entry")
             .reindex(w.index).fillna(0.0))
        for e in CARBON_ENTRIES:
            if e not in u.columns:
                u[e] = 0.0
        conv = w["Change from ecosystem conversion"]
        u["Additions to stock: Reclassifications"] = conv.clip(lower=0) + 0.0
        u["Reductions in stock: Reclassifications"] = (-conv).clip(lower=0) + 0.0
        u["Opening stock"], u["Closing stock"] = w["Opening stock"], w["Closing stock"]
        u["Net carbon balance"] = w["Net change in stock"]
        u = u[CARBON_ENTRIES]
        u.columns.name = "Entry"
        ul = u.stack().rename("Amount").reset_index()
        ul["OpeningTimestep"], ul["ClosingTimestep"] = o, c
        un_rows.append(ul)

        w.columns.name = "Entry"
        long = w.stack().rename("Amount").reset_index()
        long["OpeningTimestep"], long["ClosingTimestep"] = o, c
        stock_rows.append(long)

        # Over all classes the conversion term must cancel.
        tot = (w["Change from ecosystem conversion"].groupby(["Iteration", "StratumId"]).sum()
               .rename("Residual").reset_index())
        checks.append(tot.assign(Account="Carbon stock", OpeningTimestep=o, ClosingTimestep=c,
                                 Item="Change from ecosystem conversion summed over classes "
                                      "and pools (should be 0)"))
    stock_acc = pd.concat(stock_rows, ignore_index=True)
    stock_acc["Entry"] = pd.Categorical(stock_acc["Entry"], STOCK_ENTRIES, ordered=True)
    stock_acc = stock_acc.sort_values(keys[:2] + PERIOD + keys[2:] + ["Entry"])
    stock_acc["Entry"] = stock_acc["Entry"].astype(str)
    result = (_finish(stock_acc), _finish(pd.concat(flow_rows, ignore_index=True)),
              pd.concat(checks, ignore_index=True))
    if not with_un:
        return result
    un = _order(pd.concat(un_rows, ignore_index=True), CARBON_ENTRIES,
                keys=("StateClassId", "StockGroupId"))
    return (*result, _finish(un))


# ---------------------------------------------------------------- services

def service_supply(state, stock, flow, rules, by_stratum=False, log=None):
    """
    Physical supply per (Iteration, Timestep, StratumId, StateClassId, ServiceId).
    Basis Area:        Coefficient x class area           (units per area unit per year)
    Basis Stock group: Coefficient x stock of the group   (e.g. retention)
    Basis Flow group:  Coefficient x flows of the group, attributed to FromStateClassId
    Rules can be limited to one stratum and/or one state class.
    The run's first timestep is the initial condition and has no flows, so
    supply starts at the second timestep for every basis.
    """
    if rules is None or rules.empty:
        return None
    first = state["Timestep"].min()
    parts = []
    for _, r in rules.iterrows():
        basis = list_value(r["Basis"], BASES, "service supply basis")
        if basis == AREA:
            src = state
            scol, ccol = "StratumId", "StateClassId"
        elif basis == STOCK:
            if pd.isna(r.get("StockGroupId")):
                raise AccountError(f"Service '{r.ServiceId}': basis Stock group needs a Stock Group.")
            if stock is None:
                raise AccountError("Stock-based service but stsim_OutputStock is empty.")
            src = stock[stock.StockGroupId == r.StockGroupId]
            scol, ccol = "StratumId", "StateClassId"
        else:
            if pd.isna(r.get("FlowGroupId")):
                raise AccountError(f"Service '{r.ServiceId}': basis Flow group needs a Flow Group.")
            if flow is None:
                raise AccountError("Flow-based service but stsim_OutputFlow is empty.")
            src = flow[flow.FlowGroupId == r.FlowGroupId]
            scol, ccol = "FromStratumId", "FromStateClassId"
        if pd.notna(r.get("StratumId")):
            src = src[src[scol] == r.StratumId]
        if pd.notna(r.get("StateClassId")):
            src = src[src[ccol] == r.StateClassId]
        src = src[src.Timestep > first]
        if src.empty:
            continue
        d = _stratum(src, scol, by_stratum).rename(columns={ccol: "StateClassId"})
        d = d.groupby(["Iteration", "Timestep", "StratumId", "StateClassId"], as_index=False)["Amount"].sum()
        d["Amount"] *= float(r["Coefficient"])
        d["ServiceId"] = r["ServiceId"]
        parts.append(d)
    if not parts:
        _log(log, "SEEA-Sim: service supply rules matched no output rows.")
        return None
    out = (pd.concat(parts).groupby(["Iteration", "Timestep", "StratumId", "StateClassId", "ServiceId"],
                                    as_index=False)["Amount"].sum())
    return out


def price_table(prices: pd.DataFrame, services, timesteps, log=None) -> pd.DataFrame:
    """
    ServiceId x Timestep -> Price. A price row applies from its Timestep
    onward until the next one; a row with no Timestep applies before the
    first dated row (or always, if none is dated).
    """
    rows = []
    for s in services:
        p = prices[prices.ServiceId == s] if prices is not None and not prices.empty else prices
        if p is None or p.empty:
            _log(log, f"SEEA-Sim: no price for service '{s}'; left out of monetary tables.")
            continue
        dated = p[p["Timestep"].notna()].sort_values("Timestep") if "Timestep" in p else p.iloc[0:0]
        undated = p[p["Timestep"].isna()] if "Timestep" in p else p
        for t in timesteps:
            hit = dated[dated.Timestep <= t]
            if not hit.empty:
                price = hit.iloc[-1].Price
            elif not undated.empty:
                price = undated.iloc[0].Price
            else:
                price = dated.iloc[0].Price  # before the first dated price: use it
            rows.append({"ServiceId": s, "Timestep": t, "Price": float(price)})
    return pd.DataFrame(rows, columns=["ServiceId", "Timestep", "Price"])


def service_value(supply, prices_tbl):
    if supply is None or prices_tbl is None or prices_tbl.empty:
        return None
    v = supply.merge(prices_tbl, on=["ServiceId", "Timestep"], how="inner")
    v["Amount"] = v["Amount"] * v["Price"]
    return v.drop(columns="Price")


# ---------------------------------------------------------------- assets

# SEEA EA Table 10.1 / AI.11; decreases negative.
OCV = "Other changes in volume of ecosystem assets"
CONVERSION_ADD = "Ecosystem conversions: Additions"
CONVERSION_RED = "Ecosystem conversions: Reductions"
CATASTROPHIC_LOSS = f"{OCV}: Catastrophic losses"
UPWARD = f"{OCV}: Upward reappraisals"
DOWNWARD = f"{OCV}: Downward reappraisals"
OTHER_VOLUME = f"{OCV}: Other changes n.e.c."
ASSET_ENTRIES = [
    "Opening value",
    "Ecosystem enhancement",
    "Ecosystem degradation",
    CONVERSION_ADD,
    CONVERSION_RED,
    CATASTROPHIC_LOSS,
    UPWARD,
    DOWNWARD,
    OTHER_VOLUME,
    "Revaluations",
    "Net change in value",
    "Closing value",
]


def condition_lookup(condition: pd.DataFrame | None):
    """
    condition: StateClassId, optional OpeningTimestep/ClosingTimestep, Change.
    Returns f(class, o, c) -> change or None. A row for the exact period wins
    over a row with no period.
    """
    if condition is None or condition.empty:
        return lambda cls, o, c: None
    d = condition.copy()
    for col in ("OpeningTimestep", "ClosingTimestep"):
        if col not in d.columns:
            d[col] = np.nan
    exact, anyp = {}, {}
    for r in d.itertuples():
        if pd.notna(r.OpeningTimestep) and pd.notna(r.ClosingTimestep):
            exact[(r.StateClassId, int(r.OpeningTimestep), int(r.ClosingTimestep))] = float(r.Change)
        else:
            anyp[r.StateClassId] = float(r.Change)
    return lambda cls, o, c: exact.get((cls, o, c), anyp.get(cls))


def classify_volume(volume: float, cond):
    """
    SEEA EA Annex 10.1 (as tabulated in the SEEALand example):
      volume > 0 and condition > 0          -> enhancement
      volume > 0 and condition <= 0         -> upward reappraisal
      volume < 0 and condition < 0          -> degradation
      volume < 0 and condition >= 0         -> downward reappraisal
    With no condition data, a gain is enhancement and a loss degradation.
    """
    if volume > 0:
        return "Ecosystem enhancement" if cond is None or cond > 0 else UPWARD
    if volume < 0:
        return "Ecosystem degradation" if cond is None or cond < 0 else DOWNWARD
    return None


def asset_account(comp, state, supply, prices_tbl, valuation, by_stratum=False, log=None,
                  condition=None):
    """
    Monetary ecosystem asset account (SEEA EA ch. 10 and Annex 10.1).

    Per class c and service s, the value is NPV = P x u x a, where
      a = class area,
      u = annual supply per area unit (expected flow intensity),
      P = price x annuity(r, L), i.e. today's flow is assumed to continue for
          the asset life.
    Opening: a = opening area, u = supply per area in the period's first year
    (o+1), price of year o. Closing: closing area, supply per area in year c,
    price of year c. If a class has no area in one of those years, the other
    year's u is used.

    The change P1u1a1 - P0u0a0 is split exactly into area, volume and price
    effects with the symmetric weights of SEEA EA Annex 10.1:
      area   = (P0u0/3 + P0u1/6 + P1u0/6 + P1u1/3) x (a1 - a0)
      volume = (P0a0/3 + P0a1/6 + P1a0/6 + P1a1/3) x (u1 - u0)
      price  = (u0a0/3 + u0a1/6 + u1a0/6 + u1a1/3) x (P1 - P0)
    The area effect is split by the extent account into conversions
    (additions, reductions), catastrophic losses and unexplained change; the
    volume effect (summed over services) into enhancement, degradation or
    reappraisals using the condition change (classify_volume); the price
    effect is revaluation.
    """
    if supply is None or prices_tbl is None or prices_tbl.empty:
        _log(log, "SEEA-Sim: no priced services; asset account skipped.")
        return None
    if valuation is None or valuation.empty:
        _log(log, "SEEA-Sim: Asset Valuation is empty; asset account skipped.")
        return None
    default = valuation[valuation["StateClassId"].isna()]
    per_class = valuation[valuation["StateClassId"].notna()].set_index("StateClassId")

    def af(cls):
        if cls in per_class.index:
            r = per_class.loc[cls]
        elif not default.empty:
            r = default.iloc[0]
        else:
            return np.nan
        return annuity_factor(float(r.DiscountRate), int(r.AssetLifeYears))

    cond_of = condition_lookup(condition)
    if condition is None or condition.empty:
        _log(log, "SEEA-Sim: no condition data; volume gains are booked as enhancement and "
                  "losses as degradation (no reappraisals).")

    st = _prep_state(state, by_stratum)
    keys = ["Iteration", "StratumId", "StateClassId"]
    q = supply.merge(st, on=["Iteration", "StratumId", "Timestep", "StateClassId"],
                     how="left", suffixes=("", "_area"))
    q["PerArea"] = np.where(q["Amount_area"] > 0, q["Amount"] / q["Amount_area"], np.nan)
    prices = prices_tbl.set_index(["ServiceId", "Timestep"])["Price"]
    q = q[q.ServiceId.isin(set(prices_tbl.ServiceId))]

    out = []
    for (o, c), comp_p in comp.groupby(PERIOD):
        o, c = int(o), int(c)
        t0, t1 = o + 1, c
        u0 = q[q.Timestep == t0].set_index(keys + ["ServiceId"])["PerArea"]
        u1 = q[q.Timestep == t1].set_index(keys + ["ServiceId"])["PerArea"]
        s = pd.concat([u0.rename("u0"), u1.rename("u1")], axis=1)
        s["u0"] = s["u0"].fillna(s["u1"])
        s["u1"] = s["u1"].fillna(s["u0"])
        s = s.fillna(0.0).reset_index()
        s["AF"] = s.StateClassId.map(af)
        if s.AF.isna().any():
            miss = sorted(map(str, s.loc[s.AF.isna(), "StateClassId"].unique()))
            _log(log, f"SEEA-Sim: no discount rate/asset life for {miss}; valued at 0. "
                      "Add a row with no State Class as the default.")
            s["AF"] = s.AF.fillna(0.0)
        s["P0"] = [prices.get((sv, o), np.nan) for sv in s.ServiceId]
        s["P1"] = [prices.get((sv, c), np.nan) for sv in s.ServiceId]
        s[["P0", "P1"]] = s[["P0", "P1"]].fillna(0.0).mul(s.AF, axis=0)
        s = s.merge(comp_p[keys + ["Opening", "Closing"]], on=keys, how="left").fillna(
            {"Opening": 0.0, "Closing": 0.0})
        P0, P1, a0, a1, U0, U1 = s.P0, s.P1, s.Opening, s.Closing, s.u0, s.u1
        s["V0"] = P0 * U0 * a0
        s["V1"] = P1 * U1 * a1
        s["w_area"] = P0 * U0 / 3 + P0 * U1 / 6 + P1 * U0 / 6 + P1 * U1 / 3
        s["vol"] = (P0 * a0 / 3 + P0 * a1 / 6 + P1 * a0 / 6 + P1 * a1 / 3) * (U1 - U0)
        s["price"] = (U0 * a0 / 3 + U0 * a1 / 6 + U1 * a0 / 6 + U1 * a1 / 3) * (P1 - P0)
        cls = s.groupby(keys)[["V0", "V1", "w_area", "vol", "price"]].sum().reset_index()
        d = comp_p.merge(cls, on=keys, how="left").fillna(
            {"V0": 0.0, "V1": 0.0, "w_area": 0.0, "vol": 0.0, "price": 0.0})
        w = d.w_area
        vals = {e: pd.Series(0.0, index=d.index) for e in ASSET_ENTRIES}
        vals["Opening value"] = d.V0
        vals["Closing value"] = d.V1
        vals["Net change in value"] = d.V1 - d.V0
        vals[CONVERSION_ADD] = (d.Add_Managed + d.Add_Unmanaged + d.Add_Catastrophic) * w
        vals[CONVERSION_RED] = -(d.Red_Managed + d.Red_Unmanaged) * w
        vals[CATASTROPHIC_LOSS] = -d.Red_Catastrophic * w
        vals[OTHER_VOLUME] = d.Residual * w
        vals["Revaluations"] = d.price
        for i, r in d.iterrows():
            entry = classify_volume(r.vol, cond_of(r.StateClassId, o, c))
            if entry:
                vals[entry].loc[i] = r.vol
        base = d[["Iteration", "StratumId", *PERIOD, "StateClassId"]]
        scale = max(1.0, float(np.abs(d.V0).max()), float(np.abs(d.V1).max()))
        for e in ASSET_ENTRIES:  # floating-point noise from the decomposition -> 0
            vals[e] = vals[e].where(np.abs(vals[e]) > 1e-12 * scale, 0.0)
        out += [base.assign(Entry=e, Amount=vals[e].values) for e in ASSET_ENTRIES]
    return _finish(_order(pd.concat(out, ignore_index=True), ASSET_ENTRIES))


def annual_values(state, supply, prices_tbl, valuation, by_stratum=False, log=None):
    """
    Per (Iteration, Timestep, StratumId, StateClassId), for every year with supply:
      ServiceValue = sum over services of supply x price       (currency per year)
      AssetValue   = ServiceValue x annuity(r, L) of the class  (currency)
      Area, ServiceValuePerArea, AssetValuePerArea (per area unit).
    AssetValue is the same NPV the asset account uses at its opening and closing,
    computed for every year, for charts and maps.
    """
    if supply is None or prices_tbl is None or prices_tbl.empty:
        return None
    v = supply.merge(prices_tbl, on=["ServiceId", "Timestep"], how="inner")
    if v.empty:
        return None
    v["Value"] = v["Amount"] * v["Price"]
    keys = ["Iteration", "Timestep", "StratumId", "StateClassId"]
    out = v.groupby(keys, as_index=False)["Value"].sum().rename(columns={"Value": "ServiceValue"})
    af = {}
    if valuation is not None and not valuation.empty:
        default = valuation[valuation["StateClassId"].isna()]
        for cls in out.StateClassId.unique():
            row = valuation[valuation["StateClassId"] == cls]
            row = row if not row.empty else default
            if not row.empty:
                af[cls] = annuity_factor(float(row.iloc[0].DiscountRate), int(row.iloc[0].AssetLifeYears))
    out["AssetValue"] = out["ServiceValue"] * out["StateClassId"].map(af).fillna(0.0)
    st = _prep_state(state, by_stratum).rename(columns={"Amount": "Area"})
    out = out.merge(st, on=keys, how="left")
    out["ServiceValuePerArea"] = np.where(out.Area > 0, out.ServiceValue / out.Area, 0.0)
    out["AssetValuePerArea"] = np.where(out.Area > 0, out.AssetValue / out.Area, 0.0)
    return out


def account_checks(table: pd.DataFrame, account: str, opening: str, closing: str,
                   net: str, reductions=()) -> pd.DataFrame:
    """Opening + additions - reductions - closing; entries listed in `reductions`
    are positive amounts that are subtracted, all other change entries are added."""
    t = table.copy()
    t["StratumId"] = t["StratumId"].astype(object).where(t["StratumId"].notna(), ALL)
    idx = [c for c in ["Iteration", "StratumId", *PERIOD, "StateClassId", "StockGroupId"]
           if c in t.columns]
    w = t.pivot_table(index=idx, columns="Entry", values="Amount", aggfunc="sum").fillna(0.0)
    changes = [c for c in w.columns if c not in (opening, closing, net)]
    sign = pd.Series([-1.0 if c in reductions else 1.0 for c in changes], index=changes)
    res = (w[opening] + (w[changes] * sign).sum(axis=1) - w[closing]).abs()
    res = res.groupby(["Iteration", "StratumId", *PERIOD]).sum().rename("Residual").reset_index()
    return res.assign(Account=account, Item="Opening + changes - closing (should be 0)")


def finish_checks(parts) -> pd.DataFrame:
    parts = [p for p in parts if p is not None and not p.empty]
    if not parts:
        return pd.DataFrame(columns=["Iteration", "Account", *PERIOD, "StratumId", "Item", "Residual"])
    out = pd.concat(parts, ignore_index=True)
    return _finish(out[["Iteration", "Account", *PERIOD, "StratumId", "Item", "Residual"]])
