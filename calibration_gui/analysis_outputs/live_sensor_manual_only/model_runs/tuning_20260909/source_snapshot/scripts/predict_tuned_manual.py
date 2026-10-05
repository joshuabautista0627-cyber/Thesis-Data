"""Offline inference for trusted tuned manual models on ordered optical streams.

Input must include the complete causal history to reproduce a recording. A new
session or a gap >300 ms resets history; predictions never use future samples.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import joblib
import numpy as np
import pandas as pd
from threadpoolctl import threadpool_limits
from scripts.tune_manual_models import optical_features, causal_ema, scores
from scripts.live_sensor_common import sha256_file


def load_models(directory):
    directory=Path(directory)
    manifest=json.loads((directory/"manifest.json").read_text())
    if manifest["status"]!="experimental_offline_only":raise ValueError("Unsupported model manifest")
    for file,digest in manifest["files"].items():
        if sha256_file(directory/file)!=digest:raise ValueError(f"Checksum mismatch: {file}")
    bundles={}
    for name in manifest["models"]:
        spec=json.loads((directory/f"{name}.json").read_text())
        spec["loaded_members"]=[joblib.load(directory/m["file"]) for m in spec["members"]]
        bundles[name]=spec
    return bundles


def infer_sequence(frames,models):
    if frames.empty:raise ValueError("Empty optical stream")
    ids=frames.session_id.to_numpy();times=frames.capture_monotonic_relative_s.to_numpy(float)
    if not np.isfinite(times).all():raise ValueError("Invalid timestamps")
    if np.any((ids[1:]==ids[:-1]) & (np.diff(times)<=0)):raise ValueError("Input must have increasing per-session timestamps")
    starts=np.r_[True,ids[1:]!=ids[:-1]]
    if len(set(ids[starts]))!=starts.sum():raise ValueError("Each session must form one contiguous stream")
    result={};features={}
    with threadpool_limits(limits=1):
        for name,spec in models.items():
            floor_key=tuple(spec["floors"])
            if floor_key not in features:features[floor_key]=optical_features(frames,np.asarray(spec["floors"]))
            combined=[];task=spec["task"]
            for member,fit in zip(spec["members"],spec["loaded_members"]):
                x=features[floor_key][fit["config"]["features"]]
                values=scores(fit["model"],fit["scaler"].transform(x),task,fit["config"]["family"])
                choice=member["choice"]
                values=causal_ema(values,ids,times,choice["tau"])
                # Match the precision used by the cached tuning and OOF scores.
                values=values.astype(np.float32).astype(float)
                if task=="force":values=np.maximum(values+choice["offset"],0)
                combined.append(values)
            values=np.mean(combined,axis=0)
            if task=="force":result[name]=values
            elif task=="localization":result[name]=values.argmax(axis=1)+1
            else:
                result[name]=(values>=spec["choice"]["threshold"]).astype(int)
                result[f"{name}_score"]=values
    result=pd.DataFrame(result,index=frames.index)
    result["force_when_contact_N"]=np.where(result.contact==1,result.force,0)
    result["status"]="experimental_unvalidated"
    return result


class StreamingResearchPredictor:
    """Stateful optical-only inference, one camera-feature row at a time.

    Reset at a new recording. Session IDs/timestamps control history boundaries
    only, and are never passed to a fitted estimator.
    """
    def __init__(self,models):
        self.models=models
        self.reset()

    def reset(self):
        self.session=None
        self.timestamp=None
        self.feature_history={}
        self.output_history={}

    def predict_one(self,row):
        frame=row.to_frame().T if isinstance(row,pd.Series) else row
        if len(frame)!=1:raise ValueError("predict_one requires exactly one feature row")
        session=frame.session_id.iloc[0]
        timestamp=float(frame.capture_monotonic_relative_s.iloc[0])
        if not np.isfinite(timestamp):raise ValueError("Invalid timestamp")
        same=session==self.session
        dt=timestamp-self.timestamp if same and self.timestamp is not None else 0
        if same and dt<=0:raise ValueError("Timestamp must increase within a recording")
        reset=not same or dt>.3
        if reset:self.feature_history.clear();self.output_history.clear()
        features={};result={}
        with threadpool_limits(limits=1):
            for name,spec in self.models.items():
                key=tuple(spec["floors"])
                if key not in features:
                    packs=optical_features(frame,np.asarray(spec["floors"]))
                    optical=packs["optical"]
                    if key in self.feature_history:
                        previous_fast,previous_slow=self.feature_history[key]
                        fast=previous_fast+(-np.expm1(-dt/.3))*(optical-previous_fast)
                        slow=previous_slow+(-np.expm1(-dt/1.0))*(optical-previous_slow)
                    else:fast=optical.copy();slow=optical.copy()
                    self.feature_history[key]=(fast,slow)
                    packs["temporal"]=np.column_stack([packs["spatial"],fast,slow,optical-fast,fast-slow])
                    features[key]=packs
                predictions=[];task=spec["task"]
                for i,(member,fit) in enumerate(zip(spec["members"],spec["loaded_members"])):
                    x=features[key][fit["config"]["features"]]
                    v=scores(fit["model"],fit["scaler"].transform(x),task,fit["config"]["family"])
                    choice=member["choice"];history_key=(name,i)
                    if choice["tau"]>0 and history_key in self.output_history:
                        previous=self.output_history[history_key]
                        v=previous+(-np.expm1(-dt/choice["tau"]))*(v-previous)
                    self.output_history[history_key]=v.copy()
                    v=v.astype(np.float32).astype(float)
                    if task=="force":v=np.maximum(v+choice["offset"],0)
                    predictions.append(v)
                values=np.mean(predictions,axis=0)
                if task=="force":result[name]=float(values[0])
                elif task=="localization":result[name]=int(values.argmax(axis=1)[0]+1)
                else:
                    result[name]=int(values[0]>=spec["choice"]["threshold"])
                    result[f"{name}_score"]=float(values[0])
        self.session,self.timestamp=session,timestamp
        result["force_when_contact_N"]=result["force"] if result["contact"]==1 else 0.
        result["status"]="experimental_unvalidated"
        return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--models",type=Path,required=True);p.add_argument("--input",type=Path,required=True);p.add_argument("--output",type=Path,required=True)
    args=p.parse_args()
    result=infer_sequence(pd.read_parquet(args.input),load_models(args.models))
    result.to_csv(args.output,index=False)


if __name__=="__main__":main()
