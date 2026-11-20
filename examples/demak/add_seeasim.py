"""
Add the SEEA-Sim example inputs to an existing ST-Sim library.

    python add_seeasim.py --library JEM_cycle1.ssim --scenario "Cycle1 2015 BaU"
    python add_seeasim.py ... --ssim "C:\\Program Files\\SyncroSim Studio"

Needs the stsimseea package installed in SyncroSim (File > Local Packages).
What it does, in order:
  1. adds the stsimseea package to the library
  2. project: adds the SEEA stock and flow groups (only names not there yet)
     and the Ecosystem Service list
  3. scenario: adds the stock/flow group memberships (keeps existing rows),
     turns on the ST-Sim output SEEA-Sim needs, saves every stsimseea_* table
  4. sets the scenario pipeline: 1 ST-Sim, 2 SEEA-Sim Accounts
Writes add_seeasim_log.txt next to this script.
"""
import argparse
import glob
import os

import pandas as pd
import pysyncrosim as ps

HERE = os.path.dirname(os.path.abspath(__file__))
P_DIR = os.path.join(HERE, "project")
S_DIR = os.path.join(HERE, "scenario")
LOG = []

# Codes for SyncroSim List columns, tried if saving the labels fails.
LIST_CODES = {
    "ServiceType": {"Provisioning": 1, "Regulating": 2, "Cultural": 3},
    "ChangeType": {"Managed": 1, "Unmanaged": 2, "Catastrophic": 3},
    "Role": {"Ecosystem pool": 1, "Atmosphere": 2, "Water": 3, "Long-term burial": 4,
             "Other external": 5},
    "Basis": {"Area": 1, "Stock group": 2, "Flow group": 3},
}


def log(level, msg):
    line = f"[{level}] {msg}"
    print(line)
    LOG.append(line)


def read_csv(path):
    return pd.read_csv(path, dtype=str, keep_default_na=False).replace("", None)


def norm(s):
    return s.lower().replace("_", "")


def match_columns(df, obj, name):
    """Rename CSV columns to the datasheet's own spelling (Id vs ID)."""
    try:
        real = list(obj.datasheets(name, empty=True).columns)
    except Exception:
        return df
    lookup = {norm(c): c for c in real}
    keep = {c: lookup[norm(c)] for c in df.columns if norm(c) in lookup}
    dropped = [c for c in df.columns if c not in keep]
    if dropped:
        log("WARN", f"{name}: columns not in datasheet, dropped {dropped}; real columns {real}")
    return df[list(keep)].rename(columns=keep)


def save(obj, name, df, **kw):
    df = match_columns(df, obj, name)
    try:
        obj.save_datasheet(name=name, data=df, **kw)
        log("OK", f"{name}: {len(df)} rows")
        return
    except Exception as e:
        first = str(e).strip()[:300]
    coded = df.copy()
    changed = False
    for col, codes in LIST_CODES.items():
        if col in coded.columns:
            coded[col] = coded[col].map(lambda v: codes.get(v, v) if v is not None else v)
            changed = True
    if changed:
        try:
            obj.save_datasheet(name=name, data=coded, **kw)
            log("OK", f"{name}: {len(df)} rows (list values saved as codes)")
            return
        except Exception as e:
            first += " | with codes: " + str(e).strip()[:300]
    log("FAIL", f"{name}: {first}")


def add_new_names(proj, name, path):
    """Project definitions: append only names that are not there yet."""
    new = read_csv(path)
    try:
        cur = proj.datasheets(name)
        have = set(cur["Name"].astype(str)) if "Name" in cur.columns else set()
    except Exception:
        have = set()
    new = new[~new["Name"].isin(have)]
    if new.empty:
        log("OK", f"{name}: nothing new")
        return
    save(proj, name, new, append=True)


def merge_membership(scn, name, path, key_cols):
    new = match_columns(read_csv(path), scn, name)
    try:
        cur = scn.datasheets(name)
    except Exception:
        cur = pd.DataFrame(columns=new.columns)
    both = pd.concat([cur[[c for c in new.columns if c in cur.columns]], new], ignore_index=True)
    keys = [c for c in both.columns if norm(c) in {norm(k) for k in key_cols}]
    both = both.drop_duplicates(subset=keys)
    save(scn, name, both)


def require_output_options(scn, path):
    name = "stsim_OutputOptions"
    want = read_csv(path).iloc[0].to_dict()
    try:
        cur = scn.datasheets(name)
    except Exception:
        cur = pd.DataFrame()
    row = cur.iloc[[0]].copy() if len(cur) else pd.DataFrame([{}])
    for k, v in want.items():
        row[k] = v
    save(scn, name, row)


def set_pipeline(scn):
    for first, second in (("stsim_Runtime", "stsimseea_Main"), ("ST-Sim", "SEEA-Sim Accounts")):
        try:
            scn.save_datasheet(name="core_Pipeline", data=pd.DataFrame(
                {"StageNameId": [first, second], "RunOrder": [1, 2]}))
            log("OK", f"pipeline: 1 {first}, 2 {second}")
            return
        except Exception:
            continue
    log("WARN", "could not set the pipeline; set it in Studio: Scenario > Pipeline: "
                "1 ST-Sim, 2 SEEA-Sim Accounts")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--library", required=True)
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--project", default="Definitions")
    ap.add_argument("--ssim", default=None)
    a = ap.parse_args()

    session = ps.Session(location=a.ssim) if a.ssim else ps.Session()
    lib = ps.library(name=a.library, session=session)
    try:
        lib.add_packages("stsimseea")
        log("OK", "package stsimseea added to the library")
    except Exception as e:
        log("WARN", f"add_packages(stsimseea): {str(e)[:300]} (fine if it is already in the library)")
    proj = lib.projects(name=a.project)
    scn = proj.scenarios(name=a.scenario)

    add_new_names(proj, "stsim_StockGroup", os.path.join(P_DIR, "stsim_StockGroup_additions.csv"))
    add_new_names(proj, "stsim_FlowGroup", os.path.join(P_DIR, "stsim_FlowGroup_additions.csv"))
    add_new_names(proj, "stsimseea_Service", os.path.join(P_DIR, "stsimseea_Service.csv"))

    merge_membership(scn, "stsim_StockTypeGroupMembership",
                     os.path.join(S_DIR, "stsim_StockTypeGroupMembership_additions.csv"),
                     ["StockTypeId", "StockGroupId"])
    merge_membership(scn, "stsim_FlowTypeGroupMembership",
                     os.path.join(S_DIR, "stsim_FlowTypeGroupMembership_additions.csv"),
                     ["FlowTypeId", "FlowGroupId"])
    require_output_options(scn, os.path.join(S_DIR, "stsim_OutputOptions_required.csv"))
    for path in sorted(glob.glob(os.path.join(S_DIR, "stsimseea_*.csv"))):
        save(scn, os.path.splitext(os.path.basename(path))[0], read_csv(path))
    set_pipeline(scn)

    with open(os.path.join(HERE, "add_seeasim_log.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(LOG))
    n_fail = sum(line.startswith("[FAIL") for line in LOG)
    print(f"\nDone: {n_fail} FAIL. Details in add_seeasim_log.txt")


if __name__ == "__main__":
    main()
