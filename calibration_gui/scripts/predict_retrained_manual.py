"""Offline optical-only inference for trusted retraining research artifacts."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import joblib
import numpy as np
import pandas as pd
from scripts.retrain_manual_models import RAW_LIGHT_COLUMNS, AREA_COLUMNS, ACTIVE_COLUMNS, SIGNED, predict
from scripts.live_sensor_common import sha256_file


def load_research_models(directory):
    directory=Path(directory)
    manifest=json.loads((directory/"manifest.json").read_text())
    if manifest["status"]!="experimental_offline_only": raise ValueError("unsupported research manifest")
    models={}
    for task in ("force","contact","localization"):
        path=directory/f"{task}.joblib"
        if sha256_file(path)!=manifest["files"][path.name]: raise ValueError("model checksum mismatch")
        # Only locally produced, trusted files are accepted by this research utility.
        models[task]=joblib.load(path)
    return models


def infer(frames, models):
    area=frames[AREA_COLUMNS].to_numpy(float)
    raw=frames[RAW_LIGHT_COLUMNS].to_numpy(float)
    signed=frames[SIGNED].to_numpy(float)/area
    active=frames[ACTIVE_COLUMNS].to_numpy(float)
    if not (np.isfinite(area).all() and (area>0).all() and np.isfinite(raw).all()
            and np.isfinite(signed).all() and np.isfinite(active).all()):
        raise ValueError("invalid optical feature matrix")
    result={}
    for task,bundle in models.items():
        positive=np.maximum(raw-bundle["floors"],0)/area
        x=positive if task=="force" else np.column_stack([positive,signed,active])
        y,score=predict(bundle["model"],bundle["scaler"].transform(x),task,bundle["name"])
        result[task]=y
        if score is not None: result["contact_score"]=score
    result=pd.DataFrame(result,index=frames.index)
    result["force_when_contact_N"]=np.where(result.contact.eq(1),result.force,0)
    result["status"]="experimental_unvalidated"
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--models",type=Path,required=True)
    p.add_argument("--input",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    a=p.parse_args()
    frames=pd.read_parquet(a.input)
    infer(frames,load_research_models(a.models)).to_csv(a.output,index=False)


if __name__=="__main__": main()
