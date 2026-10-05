"""Reproduce global BAURods characterization from Manual Calibration (2).zip.

Primary optical source is the archived acquisition export (color magnified).
Each row in global_force_response_data.csv is one force-segmented press.
Inference resamples whole recordings within taxel, never individual frames.
Run with the calibration_gui .venv Python; see README in the output package.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import warnings
import importlib.metadata

ROOT = Path(__file__).resolve().parent
os.environ.setdefault('MPLCONFIGDIR', str(ROOT/'outputs/global_characterization/.mplconfig'))
import numpy as np
import pandas as pd
from scipy import signal, optimize, stats
from sklearn.metrics import confusion_matrix, precision_recall_fscore_support, roc_auc_score
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import zipfile

SEED = 20261005
PRIMARY_PROMINENCE_N = 0.5
MIN_PEAK_DISTANCE_S = 0.15
CONTACT_N = 0.05
OPTICAL_THRESHOLD = 5.0
BOOTSTRAPS = 2000
OPT_COLS = [f'roi{i}_delta_v_mean' for i in range(1,10)]
STORE = ROOT/'calibration_gui/analysis_outputs/live_sensor_manual_only/feature_store/feature-store-fa2500440adab6ca'

def sha256(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for block in iter(lambda:f.read(8*1024*1024),b''): h.update(block)
    return h.hexdigest()

def serial(v):
    if isinstance(v,(np.integer,)): return int(v)
    if isinstance(v,(np.floating,)): return float(v) if np.isfinite(v) else None
    if isinstance(v,(np.bool_,)): return bool(v)
    if isinstance(v,Path): return str(v)
    if isinstance(v,np.ndarray): return v.tolist()
    raise TypeError(type(v).__name__)

def write_json(path, obj):
    Path(path).write_text(json.dumps(obj,indent=2,default=serial),encoding='utf-8')

def export(df, directory, name):
    path=directory/f'{name}.csv'; df.to_csv(path,index=False,float_format='%.12g')
    return path

def load_recordings(archive, out):
    """Read immutable source CSVs and reconcile exact exported optical arithmetic."""
    recordings={}; inventory=[]; checks=[]; baselines={}; raw_samples=0
    manifest=json.loads((STORE/'run_manifest.json').read_text()) if STORE.exists() else None
    archive_hash=sha256(archive)
    if manifest:
        assert manifest['inputs']['manual']['sha256']==archive_hash, 'Original video store archive hash mismatch'
    with zipfile.ZipFile(archive) as z:
        for name in sorted(n for n in z.namelist() if n.endswith('/session_config.json')):
            pre=name.rsplit('/',1)[0]; rid=pre.rsplit('/',1)[-1]
            cfg=json.loads(z.read(name)); labels=cfg['trial_labels']
            base=json.loads(z.read(pre+'/baseline_summary.json'))
            status=json.loads(z.read(pre+'/session_status.json'))
            d=pd.read_csv(z.open(pre+'/master_synchronized.csv')).copy()
            r=pd.read_csv(z.open(pre+'/loadcell_raw.csv'))
            raw_samples+=len(r)
            # Exact frame and raw host clocks share the recording start epoch.
            d['time_s']=(d.host_monotonic_ns-d.host_monotonic_ns.iloc[0])/1e9+d.elapsed_time_s.iloc[0]
            r['time_s']=(r.host_monotonic_ns-d.host_monotonic_ns.iloc[0])/1e9+d.elapsed_time_s.iloc[0]
            valid=d.frame_valid & d.baseline_valid & d.synchronization_valid & np.isfinite(d.force_N)
            valid &= np.isfinite(d[OPT_COLS]).all(axis=1)
            d['valid']=valid
            positive=d[OPT_COLS].to_numpy(float)
            d['global_max']=positive.max(axis=1)
            d['global_sum']=positive.sum(axis=1)
            target=int(labels['target_roi_ground_truth'])
            d['target_response']=positive[:,target-1]
            no_contact='NOCONTACT' in rid.upper()
            if no_contact: assert (r.force_N.abs()<CONTACT_N).all(), 'NOCONTACT conflicts with measured force'
            predicted=np.where(positive.max(axis=1)>=cfg['processing']['localization_min_mean_delta_v'],positive.argmax(axis=1)+1,0)
            archived=d.predicted_dominant_roi.fillna(0).to_numpy(int)
            checks.append(dict(recording=rid,master_rows=len(d),raw_rows=len(r),valid_frames=int(valid.sum()),
                global_max_error=float(np.max(np.abs(d.global_max-d.dominant_intensity))),
                global_sum_error=float(np.max(np.abs(d.global_sum-d.total_corrected_intensity))),
                prediction_mismatches=int((predicted!=archived).sum()),
                force_conversion_max_error=float(np.max(np.abs(r.force_N-r.force_gf*9.80665/1000))),
                timestamp_max_error_s=float(np.max(np.abs(d.time_s-d.elapsed_time_s))),
                raw_timestamp_max_error_s=float(np.max(np.abs(r.time_s-r.elapsed_time_s))),
                duplicate_frame_ids=int(d.capture_frame_id.duplicated().sum()),
                nonpositive_timestamp_steps=int((d.time_s.diff().dropna()<=0).sum())))
            assert status['complete'] and valid.all()
            assert checks[-1]['prediction_mismatches']==0
            assert checks[-1]['global_max_error']<1e-9 and checks[-1]['global_sum_error']<1e-9
            if manifest:
                relative=f'sessions/archive_id=manual/{rid}.parquet'
                path=STORE/relative
                assert sha256(path)==manifest['outputs'][relative]
                original=pd.read_parquet(path)
                assert len(original)==len(d)
                assert np.allclose(original.reference_force_raw_N,d.force_N,equal_nan=True)
                assert original.optical_valid.all()
                pp=original[[f'roi{i}_positive_delta_mean' for i in range(1,10)]].to_numpy(float)
                d['original_max']=pp.max(axis=1)
                d['original_raw_mean']=original[[f'roi{i}_raw_v_mean' for i in range(1,10)]].mean(axis=1)
            else:
                d['original_max']=np.nan
                d['original_raw_mean']=np.nan
            controls=cfg.get('camera_confirmed_actual_controls') or {}
            meta=dict(recording=rid,trial_id=labels['trial_id'],target=target,
                session_label=labels['session_id'],skin_id=labels['sensing_skin_id'],
                test_group=re.search(r'(TEST\d+(?:_v2)?)',rid).group(1) if not no_contact else 'NOCONTACT',
                no_contact=no_contact,source_interaction_label=labels['trial_interaction_class'],
                label_conflict=no_contact and str(labels['trial_interaction_class']).lower()!='none',
                baseline_id=base['baseline_id'],source_prefix=pre,
                frame_count=len(d),raw_count=len(r),start_iso=d.wall_clock_iso.iloc[0],
                duration_s=float(np.ptp(d.time_s)),median_frame_dt_s=float(d.time_s.diff().median()),
                max_frame_dt_s=float(d.time_s.diff().max()),median_raw_dt_s=float(r.time_s.diff().median()),
                force_min=float(r.force_N.min()),force_max=float(r.force_N.max()),
                optical_source=cfg['quantitative_analysis_source'],magnification=cfg['motion_magnification']['amplification'],
                lower_cutoff_hz=cfg['motion_magnification']['lower_cutoff_hz'],upper_cutoff_hz=cfg['motion_magnification']['upper_cutoff_hz'],
                saturation_control=controls.get('saturation'),sharpness_control=controls.get('sharpness'),
                baseline_mean_v=float(np.mean([b['mean_v'] for b in base['rois']])),
                baseline_frames=base['rois'][0]['valid_frame_count'],baseline_time=base['capture_timestamp_iso'],
                config_sha256=hashlib.sha256(z.read(name)).hexdigest(),
                master_sha256=hashlib.sha256(z.read(pre+'/master_synchronized.csv')).hexdigest(),
                raw_sha256=hashlib.sha256(z.read(pre+'/loadcell_raw.csv')).hexdigest())
            inventory.append(meta); recordings[rid]=(d,r,meta)
            baselines[base['baseline_id']]=dict(baseline_id=base['baseline_id'],captured=base['capture_timestamp_iso'],
                global_mean_v=meta['baseline_mean_v'],frame_count=meta['baseline_frames'],
                roi_layout_id=base['roi_layout_id'],baseline_sha256=hashlib.sha256(z.read(pre+'/baseline_data.npz')).hexdigest())
    inv=pd.DataFrame(inventory).sort_values('recording')
    export(inv,out/'tables','recording_inventory')
    export(pd.DataFrame(checks),out/'tables','source_reconciliation')
    export(pd.DataFrame(baselines.values()).sort_values('captured'),out/'tables','captured_baselines')
    return recordings,inv,archive_hash

def segment(recordings, prominence=PRIMARY_PROMINENCE_N):
    """Find force-only pulses; never use optical response to select presses.

    Peak prominence >=0.5 N, separation >=0.15 s. Bounds are intervening
    force minima. Require >=0.5 N rise and fall, peak >0.05 N, >=3 optical
    samples and a frame within 0.15 s of the force peak. Adjacent windows
    share a trough timestamp but optical samples use [start, end).
    """
    events=[]; excluded=[]
    for rid,(d,r,meta) in recordings.items():
        if meta['no_contact']: continue
        t=r.time_s.to_numpy(); f=r.force_N.to_numpy(); dt=np.median(np.diff(t))
        peaks, props=signal.find_peaks(f,prominence=prominence,distance=max(1,int(np.ceil(MIN_PEAK_DISTANCE_S/dt))))
        if len(peaks)==0: continue
        troughs=[int(np.argmin(f[:peaks[0]+1]))]
        troughs += [int(a+np.argmin(f[a:b+1])) for a,b in zip(peaks[:-1],peaks[1:])]
        troughs += [int(peaks[-1]+np.argmin(f[peaks[-1]:]))]
        for k,p in enumerate(peaks):
            a,b=troughs[k],troughs[k+1]
            use=d[(d.time_s>=t[a])&(d.time_s<t[b])&d.valid]
            reasons=[]
            if np.max(f[a:b+1])-f[p]>1e-10: reasons.append('unresolved_multiple_force_peaks')
            if f[p]<=CONTACT_N: reasons.append('below_contact_reference')
            if f[p]-f[a]<prominence or f[p]-f[b]<prominence: reasons.append('incomplete_rise_or_fall')
            if len(use)<3: reasons.append('fewer_than_3_camera_samples')
            nearest=float(np.min(np.abs(use.time_s-t[p]))) if len(use) else np.inf
            if nearest>.15: reasons.append('no_camera_frame_near_force_peak')
            eid=f'{rid}__P{k+1:03d}'
            if reasons:
                excluded.append(dict(event_id=eid,recording=rid,start_s=t[a],force_peak_s=t[p],end_s=t[b],reason=';'.join(reasons)))
                continue
            optical_index=use.global_max.idxmax(); q=d.loc[optical_index]
            pframe=d.loc[use.time_s.sub(t[p]).abs().idxmin()]
            roi=np.array([q[c] for c in OPT_COLS]); pred=int(roi.argmax()+1) if roi.max()>=OPTICAL_THRESHOLD else 0
            event=dict(event_id=eid,recording=rid,trial_id=meta['trial_id'],target=meta['target'],
                baseline_id=meta['baseline_id'],test_group=meta['test_group'],
                start_s=t[a],force_peak_s=t[p],end_s=t[b],duration_s=t[b]-t[a],
                raw_start_row=a,raw_peak_row=int(p),raw_end_row=b,
                force_peak_N=f[p],force_trough_before_N=f[a],force_trough_after_N=f[b],
                force_excursion_N=f[p]-max(f[a],f[b]),
                optical_peak_V=float(q.global_max),optical_mean_V=float(use.global_max.mean()),
                whole_array_peak_V=float(use.global_sum.max()),active_target_peak_V=float(use.target_response.max()),
                optical_at_force_peak_V=float(pframe.global_max),force_at_optical_peak_N=float(q.force_N),
                optical_peak_s=float(q.time_s),optical_peak_row=int(optical_index),
                optical_peak_frame=int(q.capture_frame_id),n_camera_samples=len(use),
                peak_time_difference_s=float(q.time_s-t[p]),frame_force_peak_gap_s=nearest,
                detected=pred!=0,predicted_taxel=pred,ungated_predicted_taxel=int(roi.argmax()+1),
                original_peak_V=float(use.original_max.max()) if 'original_max' in use else np.nan,
                saturation_warning=bool(use.saturation_warning.any()),
                starts_unloaded=bool(f[a]<=CONTACT_N),ends_unloaded=bool(f[b]<=CONTACT_N))
            events.append(event)
    return pd.DataFrame(events),pd.DataFrame(excluded)

def design(x, model):
    x=np.asarray(x,float)
    if model=='constant': return np.ones((len(x),1))
    if model=='linear': return np.column_stack([np.ones(len(x)),x])
    if model=='quadratic': return np.column_stack([np.ones(len(x)),x,x*x])
    if model=='logarithmic': return np.column_stack([np.ones(len(x)),np.log(x)])
    raise ValueError(model)

def fit_predict(x,y,xnew,model):
    if model=='power':
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            p,_=optimize.curve_fit(lambda f,a,b:a*f**b,x,y,p0=[np.mean(y),.1],bounds=([0,-3],[np.inf,3]),maxfev=10000)
        return p[0]*np.asarray(xnew)**p[1],p
    p=np.linalg.lstsq(design(x,model),y,rcond=None)[0]
    return design(xnew,model)@p,p

def score(y, yh):
    return dict(r2=float(1-np.sum((y-yh)**2)/np.sum((y-np.mean(y))**2)),rmse=float(np.sqrt(np.mean((y-yh)**2))),mae=float(np.mean(np.abs(y-yh))))

def compare_models(events):
    x=events.force_peak_N.to_numpy(); y=events.optical_peak_V.to_numpy()
    models=['constant','linear','quadratic','logarithmic','power']; rows=[]; cvrows=[]; coeffs={}
    groups=events.recording.to_numpy()
    for model in models:
        yh,p=fit_predict(x,y,x,model); coeffs[model]=p
        cv=np.empty(len(events)); fold_errors=[]
        for group in np.unique(groups):
            test=groups==group
            pred,_=fit_predict(x[~test],y[~test],x[test],model); cv[test]=pred
            fold_errors.append(np.sqrt(np.mean((y[test]-pred)**2)))
        metrics=score(y,yh); cvs=score(y,cv)
        rows.append(dict(model=model,n=len(events),parameters=len(p),**metrics,
            recording_cv_rmse=cvs['rmse'],recording_cv_r2=cvs['r2'],
            mean_recording_rmse=float(np.mean(fold_errors)),se_recording_rmse=float(stats.sem(fold_errors))))
        cvrows.extend(dict(event_id=e,model=model,predicted=p,observed=v,recording=g) for e,p,v,g in zip(events.event_id,cv,y,groups))
    table=pd.DataFrame(rows)
    # One-standard-error choice, with a null model retained as an adequacy check.
    best=table.loc[table.mean_recording_rmse.idxmin()]
    acceptable=table[table.mean_recording_rmse<=best.mean_recording_rmse+best.se_recording_rmse]
    preferred=next(m for m in ['linear','logarithmic','power','quadratic'] if m in acceptable.model.values) if any(acceptable.model!='constant') else 'linear'
    return table,pd.DataFrame(cvrows),coeffs,preferred

def clustered_indices(events,rng):
    """Recording bootstrap stratified by the nine fixed sensing locations."""
    if '_bootstrap_groups' not in events.attrs:
        events.attrs['_bootstrap_groups']=[
            [v.index.to_numpy() for _,v in g.groupby('recording',sort=True)]
            for _,g in events.groupby('target',sort=True)]
    groups=events.attrs['_bootstrap_groups']
    return np.concatenate([g[i] for g in groups for i in rng.integers(0,len(g),len(g))])

def bootstrap(events,model,grid):
    rng=np.random.default_rng(SEED); curves=[]; pars=[]; means=[]
    x=events.force_peak_N.to_numpy(); y=events.optical_peak_V.to_numpy()
    for b in range(BOOTSTRAPS):
        ii=clustered_indices(events,rng)
        yh,p=fit_predict(x[ii],y[ii],grid,model)
        curves.append(yh); pars.append(p)
        means.append([x[ii].mean(),y[ii].mean(),events.original_peak_V.to_numpy()[ii].mean()])
    return np.asarray(pars),np.asarray(curves),np.asarray(means)

def descriptive(values,name,unit,ci=None):
    a=np.asarray(values,float); a=a[np.isfinite(a)]
    return dict(variable=name,unit=unit,n=len(a),mean=a.mean(),sd=a.std(ddof=1),median=np.median(a),
        q1=np.quantile(a,.25),q3=np.quantile(a,.75),iqr=np.ptp(np.quantile(a,[.25,.75])),
        minimum=a.min(),maximum=a.max(),cv_percent=a.std(ddof=1)/a.mean()*100 if a.mean()!=0 else np.nan,
        mean_ci95_low=ci[0] if ci is not None else np.nan,mean_ci95_high=ci[1] if ci is not None else np.nan)

def baseline_tables(recordings,events):
    rows=[]
    for rid,(d,r,meta) in recordings.items():
        if not meta['no_contact']: continue
        v=d.global_max.to_numpy(); t=d.time_s.to_numpy()
        rows.append(dict(recording=rid,baseline_id=meta['baseline_id'],start_iso=meta['start_iso'],
            n_frames=len(d),duration_s=np.ptp(t),mean_V=v.mean(),sd_V=v.std(ddof=1),median_V=np.median(v),
            min_V=v.min(),max_V=v.max(),cv_percent=v.std(ddof=1)/v.mean()*100,
            linear_drift_V_per_min=stats.linregress(t,v).slope*60,
            raw_force_mean_N=r.force_N.mean(),raw_force_sd_N=r.force_N.std(),
            original_mean_V=d.original_max.mean(),original_sd_V=d.original_max.std()))
    baseline=pd.DataFrame(rows).sort_values('start_iso').reset_index(drop=True)
    # Recording-equal baseline center and RMS within-recording temporal noise.
    mu=float(baseline.mean_V.mean()); noise=float(np.sqrt(np.mean(baseline.sd_V**2)))
    results=dict(mean_V=mu,noise_sd_V=noise,between_recording_sd_V=float(baseline.mean_V.std()),
        baseline_cv_percent=noise/mu*100,recordings=len(baseline),frames=int(baseline.n_frames.sum()))
    rng=np.random.default_rng(SEED+1)
    results['mean_ci95']=np.quantile([rng.choice(baseline.mean_V,len(baseline),replace=True).mean() for _ in range(BOOTSTRAPS)],[.025,.975]).tolist()
    results['exploratory_3sd_cutoff_V']=mu+3*noise
    events['snr_proxy']=(events.optical_peak_V-mu)/noise
    return baseline,results

def localization_tables(events):
    y=events.target.to_numpy(int); p=events.predicted_taxel.to_numpy(int)
    pr,re,fs,support=precision_recall_fscore_support(y,p,labels=list(range(1,10)),zero_division=0)
    cm=confusion_matrix(y,p,labels=list(range(10)))[1:,:]
    table=pd.DataFrame(dict(taxel=range(1,10),precision=pr,recall=re,f1=fs,support=support))
    summary=dict(n_events=len(y),n_recordings=events.recording.nunique(),accuracy=float(np.mean(y==p)),
        macro_precision=float(pr.mean()),macro_recall=float(re.mean()),macro_f1=float(fs.mean()),
        abstentions=int((p==0).sum()),coverage=float(np.mean(p!=0)),
        conditional_accuracy_detected=float(np.mean(y[p!=0]==p[p!=0])) if (p!=0).any() else np.nan,
        ungated_accuracy=float(np.mean(y==events.ungated_predicted_taxel)))
    up,ur,uf,us=precision_recall_fscore_support(y,events.ungated_predicted_taxel,labels=list(range(1,10)),zero_division=0)
    summary.update(ungated_macro_precision=float(up.mean()),ungated_macro_recall=float(ur.mean()),ungated_macro_f1=float(uf.mean()))
    rng=np.random.default_rng(SEED+2); bs=[]
    for _ in range(BOOTSTRAPS):
        ii=clustered_indices(events,rng)
        _,_,f,_=precision_recall_fscore_support(y[ii],p[ii],labels=list(range(1,10)),zero_division=0)
        bs.append([np.mean(y[ii]==p[ii]),np.mean(f),np.mean(p[ii]!=0)])
    summary['accuracy_ci95']=np.quantile(np.array(bs)[:,0],[.025,.975]).tolist()
    summary['macro_f1_ci95']=np.quantile(np.array(bs)[:,1],[.025,.975]).tolist()
    summary['detection_recall_ci95']=np.quantile(np.array(bs)[:,2],[.025,.975]).tolist()
    return table,pd.DataFrame(cm,columns=['No detection']+[f'T{i}' for i in range(1,10)]).assign(actual_taxel=range(1,10)),summary

def detection_tables(recordings,events):
    rows=[]
    # One 1-second centered unloaded window per independently recorded control.
    # Repeated windows are NOT additional negative replicates.
    for rid,(d,r,meta) in recordings.items():
        if meta['no_contact']:
            center=(d.time_s.min()+d.time_s.max())/2
            w=d[d.time_s.between(center-.5,center+.5)]
            rows.append(dict(unit=rid,recording=rid,truth=0,score_V=w.global_max.max(),duration_s=1.0))
    for e in events.itertuples():
        rows.append(dict(unit=e.event_id,recording=e.recording,truth=1,score_V=e.optical_peak_V,duration_s=e.duration_s))
    units=pd.DataFrame(rows); units['predicted']=(units.score_V>=OPTICAL_THRESHOLD).astype(int)
    y=units.truth.to_numpy(); p=units.predicted.to_numpy(); tn,fp,fn,tp=confusion_matrix(y,p).ravel()
    precision=tp/(tp+fp) if tp+fp else 0
    recall=tp/(tp+fn); specificity=tn/(tn+fp)
    result=dict(TN=int(tn),FP=int(fp),FN=int(fn),TP=int(tp),n_positive=int(tp+fn),n_negative=int(tn+fp),
        accuracy=(tp+tn)/len(y),sensitivity=recall,specificity=specificity,precision=precision,
        f1=2*precision*recall/(precision+recall) if precision+recall else 0,
        roc_auc=float(roc_auc_score(y,units.score_V)),threshold_V=OPTICAL_THRESHOLD,
        specificity_wilson_ci95=list(wilson(tn,tn+fp)))
    return units,result

def wilson(k,n):
    z=stats.norm.ppf(.975); p=k/n; den=1+z*z/n
    center=(p+z*z/(2*n))/den; half=z*np.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return center-half,center+half

def spatial_session_tables(events,model):
    y=events.optical_peak_V.to_numpy(); x=events.force_peak_N.to_numpy(); X=design(x,'linear')
    tax=pd.get_dummies(events.target,drop_first=True,dtype=float).to_numpy()
    block=pd.get_dummies(events.baseline_id,drop_first=True,dtype=float).to_numpy()
    rows=[]
    for label,mat in [('force_only',X),('force_and_baseline_block',np.column_stack([X,block])),
        ('force_and_taxel',np.column_stack([X,tax])),('force_baseline_and_taxel',np.column_stack([X,block,tax]))]:
        p=np.linalg.lstsq(mat,y,rcond=None)[0]; yh=mat@p
        cv=np.zeros(len(y))
        for rid in events.recording.unique():
            test=events.recording.eq(rid).to_numpy(); cv[test]=mat[test]@np.linalg.lstsq(mat[~test],y[~test],rcond=None)[0]
        rows.append(dict(model=label,parameters=mat.shape[1],force_coefficient_V_per_N=p[1],**score(y,yh),recording_cv_rmse=score(y,cv)['rmse']))
    # A within-recording estimate removes fixed recording offsets. It is a
    # diagnostic for confounding, not a new primary calibration or a CV model.
    xc=x-events.groupby('recording').force_peak_N.transform('mean').to_numpy()
    yc=y-events.groupby('recording').optical_peak_V.transform('mean').to_numpy()
    within_slope=float(xc@yc/(xc@xc))
    within_yh=events.groupby('recording').optical_peak_V.transform('mean').to_numpy()+within_slope*xc
    rows.append(dict(model='within_recording_centered_force',parameters=events.recording.nunique()+1,
        force_coefficient_V_per_N=within_slope,**score(y,within_yh),recording_cv_rmse=np.nan))
    # Estimate the same global relationship with one total unit of weight per
    # recording. Long TEST1 recordings otherwise contribute more presses.
    weights=1/events.groupby('recording').event_id.transform('size').to_numpy()
    wp=np.linalg.lstsq(X*np.sqrt(weights[:,None]),y*np.sqrt(weights),rcond=None)[0]
    rows.append(dict(model='equal_recording_weight_linear',parameters=2,force_coefficient_V_per_N=wp[1],
        **score(y,X@wp),recording_cv_rmse=np.nan))
    diagnostic=events.groupby(['recording','target','baseline_id','test_group'],as_index=False).agg(n_events=('event_id','size'),
        mean_force_N=('force_peak_N','mean'),mean_response_V=('optical_peak_V','mean'),mean_residual_V=('residual_V','mean'),
        detected_fraction=('detected','mean'))
    spatial=diagnostic.groupby('target',as_index=False).agg(recordings=('recording','nunique'),events=('n_events','sum'),
        mean_recording_residual_V=('mean_residual_V','mean'),sd_recording_residual_V=('mean_residual_V','std'),
        mean_recording_force_N=('mean_force_N','mean'))
    spatial['residual_se_V']=spatial.sd_recording_residual_V/np.sqrt(spatial.recordings)
    return pd.DataFrame(rows),diagnostic,spatial

def configure_plots():
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'axes.titlesize':12,'axes.labelsize':10,
        'axes.spines.top':False,'axes.spines.right':False,'axes.grid':True,'grid.alpha':.18,
        'figure.facecolor':'white','savefig.facecolor':'white','pdf.fonttype':42,'ps.fonttype':42})

def savefig(fig,out,name):
    for ext in ['png','pdf','svg']: fig.savefig(out/f'{name}.{ext}',dpi=300,bbox_inches='tight')
    plt.close(fig)

def make_figures(out,e,curve,models,baseline,recordings,cm,detection,spatial,recording_diagnostics):
    configure_plots(); blue='#1B667C'; orange='#B85B30'; figdir=out/'figures'
    xlabel='Peak applied normal force per press (N)'
    ylabel='Peak global optical response (V digital units)'
    fig,ax=plt.subplots(figsize=(7.2,4.8),layout='constrained')
    ax.scatter(e.force_peak_N,e.optical_peak_V,s=8,alpha=.30,color=blue,rasterized=True,edgecolors='none')
    ax.set(xlabel=xlabel,ylabel=ylabel,title='Global applied force vs. optical response of BAURods')
    ax.text(.02,.98,f'{len(e):,} segmented presses; {e.recording.nunique()} recording clusters\nColor-magnified acquisition output',transform=ax.transAxes,va='top',fontsize=9)
    savefig(fig,figdir,'fig01_global_force_response')
    fig,ax=plt.subplots(figsize=(7.2,4.8),layout='constrained')
    ax.scatter(e.force_peak_N,e.optical_peak_V,s=6,alpha=.18,color='gray',rasterized=True,edgecolors='none')
    ax.plot(curve.force_N,curve.response_V,color=blue,lw=2,label='Pooled descriptive fit')
    ax.fill_between(curve.force_N,curve.ci95_low,curve.ci95_high,color=blue,alpha=.22,label='95% recording-bootstrap mean-curve interval')
    ax.set(xlabel=xlabel,ylabel=ylabel,title='Global force–response model of BAURods');ax.legend(loc='upper left',fontsize=8)
    savefig(fig,figdir,'fig02_global_fitted_characteristic')
    fig,axs=plt.subplots(1,2,figsize=(10.2,4.1),layout='constrained')
    axs[0].scatter(e.force_peak_N,e.residual_V,s=6,alpha=.25,color=blue,rasterized=True)
    b=pd.qcut(e.force_peak_N,10,duplicates='drop'); means=e.groupby(b,observed=True)[['force_peak_N','residual_V']].mean()
    axs[0].plot(means.force_peak_N,means.residual_V,'o-',color=orange,ms=4,label='Force-decile mean residual')
    axs[0].axhline(0,color='black',lw=.7);axs[0].legend(fontsize=8)
    axs[0].set(xlabel='Peak force (N)',ylabel='Residual (V digital units)',title='Global residuals vs. force')
    stats.probplot(e.residual_V,dist='norm',plot=axs[1]);axs[1].set(title='Residual normal-probability diagnostic',ylabel='Residual (V digital units)')
    axs[1].get_lines()[0].set(markersize=2,color=blue)
    savefig(fig,figdir,'fig03_global_model_residuals')
    fig,axs=plt.subplots(1,2,figsize=(9,4),layout='constrained')
    axs[0].hist(e.optical_peak_V,bins='fd',color=blue,alpha=.85);axs[0].set(xlabel='Peak optical response (V digital units)',ylabel='Press events',title='Global optical response distribution')
    axs[1].hist(e.force_peak_N,bins='fd',color=orange,alpha=.85);axs[1].set(xlabel='Peak applied force (N)',ylabel='Press events',title='Applied peak-force distribution')
    savefig(fig,figdir,'fig04_global_response_distribution')
    fig,axs=plt.subplots(1,2,figsize=(9,4.2),layout='constrained')
    bins=pd.qcut(e.force_peak_N,3,duplicates='drop'); groups=list(e.groupby(bins,observed=True))
    labels=[f'{g.force_peak_N.min():.1f}–{g.force_peak_N.max():.1f} N\nn={len(g)}' for _,g in groups]
    axs[0].boxplot([g.residual_V for _,g in groups],tick_labels=labels,showfliers=False)
    axs[0].axhline(0,lw=.7,color='gray');axs[0].set(title='Force-conditioned response dispersion',ylabel='Residual (V digital units)',xlabel='Empirical force tertiles')
    axs[1].plot(np.arange(len(recording_diagnostics))+1,recording_diagnostics.mean_residual_V,'o',color=blue,ms=4)
    axs[1].axhline(0,lw=.7,color='gray');axs[1].set(title='Residual shifts between recordings',xlabel='Recording in acquisition order',ylabel='Mean residual (V digital units)')
    savefig(fig,figdir,'fig05_global_repeatability')
    fig,axs=plt.subplots(2,1,figsize=(8.5,6),layout='constrained')
    base_unique={}
    for _,(_,_,m) in recordings.items(): base_unique[m['baseline_id']]=(m['baseline_time'],m['baseline_mean_v'])
    b=sorted(base_unique.values()); xx=pd.to_datetime([v[0] for v in b],utc=True).tz_convert('Asia/Manila')
    axs[0].plot(xx,[v[1] for v in b],'o-',color=blue)
    axs[0].set(ylabel='Captured baseline (V digital units)',title='Global baseline stability on 8 August 2026')
    import matplotlib.dates as mdates
    from zoneinfo import ZoneInfo
    axs[0].xaxis.set_major_formatter(mdates.DateFormatter('%H:%M',tz=ZoneInfo('Asia/Manila')))
    axs[0].set_xlabel('Baseline capture time (Asia/Manila)')
    for i,row in enumerate(baseline.itertuples()):
        d=recordings[row.recording][0]
        axs[1].plot(d.time_s-d.time_s.min()+i*22,d.global_max,lw=.8,color=blue)
    axs[1].set(xlabel='No-contact recordings in acquisition order, with fixed gaps (not continuous time)',ylabel='Unloaded response (V digital units)',title='11 no-contact traces; positive-delta noise floor')
    savefig(fig,figdir,'fig06_global_baseline_stability')
    # First 12 s after one-third of each selected recording, chosen by fixed trial IDs.
    ids=['R1_TEST2','R5_TEST4','R9_TEST7']; fig,axs=plt.subplots(3,2,figsize=(10,8),layout='constrained')
    temporal=[]
    for row,tid in enumerate(ids):
        rid=next(r for r,(_,_,m) in recordings.items() if m['trial_id']==tid)
        d,r,m=recordings[rid]; begin=float(d.time_s.min()+np.ptp(d.time_s)/3); end=begin+12
        rr=r[r.time_s.between(begin,end)]; dd=d[d.time_s.between(begin,end)]; ev=e[e.recording.eq(rid)&e.force_peak_s.between(begin,end)]
        axs[row,0].plot(rr.time_s,rr.force_N,color=blue,lw=1);axs[row,0].plot(ev.force_peak_s,ev.force_peak_N,'v',color=orange,ms=5)
        axs[row,0].set(ylabel='Force (N)',title=f'{tid}: force-defined presses',xlabel='Elapsed time (s)')
        axs[row,1].plot(dd.time_s,dd.global_max,'o-',ms=2,lw=.8,color=blue)
        axs[row,1].axhline(OPTICAL_THRESHOLD,color=orange,ls='--',lw=.8,label='Configured criterion = 5 V digital units')
        axs[row,1].set(ylabel='Response (V digital units)',title='Sampled optical behavior',xlabel='Elapsed time (s)')
        axs[row,1].legend(fontsize=7)
        temporal.append(dd[['time_s','force_N','global_max']].assign(recording=rid))
    savefig(fig,figdir,'fig07_sampled_temporal_behavior')
    export(pd.concat(temporal),out/'tables','figure07_temporal_source')
    fig,axs=plt.subplots(1,2,figsize=(9,4.5),layout='constrained')
    mat=np.array([[detection['TN'],detection['FP']],[detection['FN'],detection['TP']]])
    axs[0].imshow(mat,cmap='Blues');axs[0].set_xticks([0,1],['No detection','Detection']);axs[0].set_yticks([0,1],['No contact','Press'])
    for (i,j),val in np.ndenumerate(mat): axs[0].text(j,i,str(val),ha='center',va='center',color='white' if val>mat.max()/2 else 'black',fontsize=14)
    axs[0].set(title='Archived gate: event detection',xlabel='Predicted',ylabel='Reference');axs[0].grid(False)
    rates=[detection['sensitivity'],detection['specificity'],detection['precision'],detection['f1']]
    axs[1].barh(['Recall','Specificity','Precision','F1'],rates,color=blue);axs[1].set_xlim(0,1.13)
    for i,v in enumerate(rates):axs[1].text(v+.02,i,f'{100*v:.1f}%',va='center',fontsize=9)
    axs[1].set(title='Configured criterion: 5 V digital units',xlabel='Proportion')
    savefig(fig,figdir,'fig08_global_contact_detection')
    fig,ax=plt.subplots(figsize=(9,6),layout='constrained')
    matrix=cm.drop(columns='actual_taxel').to_numpy(); fractions=matrix/matrix.sum(axis=1,keepdims=True)
    im=ax.imshow(fractions,cmap='Blues',vmin=0,vmax=1)
    fig.colorbar(im,ax=ax,fraction=.04,pad=.03,label='Fraction within actual class')
    for (i,j),n in np.ndenumerate(matrix):
        ax.text(j,i,str(n),ha='center',va='center',color='white' if fractions[i,j]>.5 else 'black',fontsize=8)
    ax.set_xticks(range(10),['None']+[f'T{i}' for i in range(1,10)]);ax.set_yticks(range(9),[f'T{i}' for i in range(1,10)])
    ax.set(title='Archived 5-V gate: press-level localization',xlabel='Predicted taxel (None = below 5 V digital units)',ylabel='Pressed taxel');ax.grid(False)
    savefig(fig,figdir,'fig09_overall_localization_confusion')
    fig,ax=plt.subplots(figsize=(7,4.5),layout='constrained')
    ax.errorbar(spatial.target,spatial.mean_recording_residual_V,yerr=stats.t.ppf(.975,spatial.recordings-1)*spatial.residual_se_V,fmt='o',color=blue,capsize=4,label='Mean ± 95% t interval across 8 recording means')
    ax.legend(fontsize=8,loc='upper right')
    ax.axhline(0,color='gray',lw=.8);ax.set_xticks(range(1,10),[f'T{i}' for i in range(1,10)])
    ax.set(title='Spatial consistency analysis',xlabel='Press location (secondary grouping)',ylabel='Recording-mean global residual (V digital units)')
    savefig(fig,figdir/'spatial_diagnostics','spatial_residuals')
    # Audit every recording in a compact force segmentation atlas.
    for page in range(4):
        rids=sorted(e.recording.unique())[page*18:(page+1)*18]
        fig,axs=plt.subplots(6,3,figsize=(15,18),layout='constrained')
        for ax,rid in zip(axs.flat,rids):
            d,r,m=recordings[rid]; ev=e[e.recording.eq(rid)]
            ax.plot(r.time_s,r.force_N,lw=.45,color=blue);ax.plot(ev.force_peak_s,ev.force_peak_N,'.',ms=2,color=orange)
            ax.set(title=f'{m["trial_id"]}: {len(ev)} presses',xlabel='Time (s)',ylabel='N')
        fig.savefig(out/'global_characterization/audit'/f'segmentation_atlas_{page+1}.png',dpi=120,bbox_inches='tight');plt.close(fig)

def markdown_table(df):
    cols=list(df.columns)
    def fmt(v):
        if isinstance(v,(float,np.floating)): return f'{v:.5g}' if np.isfinite(v) else 'Not available'
        return str(v).replace('|','/')
    return '| '+' | '.join(cols)+' |\n| '+' | '.join(['---']*len(cols))+' |\n'+'\n'.join('| '+' | '.join(fmt(v) for v in row)+' |' for row in df.itertuples(index=False,name=None))

def make_report(out,s,tables,inv,spatial,recordings):
    e=tables['global_force_response_data']; models=tables['fitted_model_comparison']; chosen=models[models.model.eq(s['selected_model'])].iloc[0]
    p=s['selected_parameters']; ci=np.array(s['parameter_ci95']); loc=s['localization']; det=s['detection']; bas=s['baseline']
    equations={'linear':f'S = {p[0]:.5f} + {p[1]:.5f} F' if len(p)>=2 else '',
        'logarithmic':f'S = {p[0]:.5f} + {p[1]:.5f} ln(F)' if len(p)>=2 else '',
        'power':f'S = {p[0]:.5f} F^{p[1]:.5f}' if len(p)>=2 else '',
        'quadratic':f'S = {p[0]:.5f} + {p[1]:.5f} F + {p[2]:.5f} F²' if len(p)==3 else ''}
    eq=equations[s['selected_model']]
    model_parameters=[]
    for name,row in models.set_index('model').iterrows():
        _,pp=fit_predict(e.force_peak_N,e.optical_peak_V,e.force_peak_N,name)
        for j,value in enumerate(pp):
            model_parameters.append(dict(model=name,parameter=('a','exponent')[j] if name=='power' else ['intercept','force_term','force_squared_term'][j],
                estimate=value,ci95_low=ci[0,j] if name==s['selected_model'] else np.nan,ci95_high=ci[1,j] if name==s['selected_model'] else np.nan))
    export(pd.DataFrame(model_parameters),out/'tables','fitted_model_parameters')
    peak_min=e.force_peak_N.min(); peak_max=e.force_peak_N.max(); min_detect=e.loc[e.detected,'force_peak_N'].min()
    matched_excess=e.optical_peak_V-bas['mean_V']
    modelcomp=tables['session_spatial_model_comparison'].set_index('model')
    original_statement=('Original-video feature-store values are used only as a separately labelled diagnostic after verifying the source ZIP identity, every input Parquet hash, frame count and force alignment. They do not replace the archived software results.'
        if e.original_peak_V.notna().any() else 'The optional original-video feature store was unavailable. Primary archived-export results remain available; the original-video comparison is not quantitatively assessed.')
    ungated_cm=confusion_matrix(e.target,e.ungated_predicted_taxel,labels=list(range(1,10)))
    up,ur,uf,us=precision_recall_fscore_support(e.target,e.ungated_predicted_taxel,labels=list(range(1,10)),zero_division=0)
    export(pd.DataFrame(ungated_cm,columns=[f'T{i}' for i in range(1,10)]).assign(actual_taxel=range(1,10)),out/'spatial_diagnostics','ungated_localization_confusion_matrix')
    export(pd.DataFrame(dict(taxel=range(1,10),precision=up,recall=ur,f1=uf,support=us)),out/'spatial_diagnostics','ungated_localization_per_class')
    fig,ax=plt.subplots(figsize=(7.5,6),layout='constrained'); frac=ungated_cm/ungated_cm.sum(axis=1,keepdims=True)
    im=ax.imshow(frac,cmap='Blues',vmin=0,vmax=1);fig.colorbar(im,ax=ax,label='Fraction within actual class',fraction=.04,pad=.03)
    for (i,j),val in np.ndenumerate(ungated_cm):ax.text(j,i,str(val),ha='center',va='center',fontsize=8,color='white' if frac[i,j]>.5 else 'black')
    ax.set_xticks(range(9),[f'T{i}' for i in range(1,10)]);ax.set_yticks(range(9),[f'T{i}' for i in range(1,10)])
    ax.set(title='Known-contact location discrimination (offline)',xlabel='Ungated argmax prediction',ylabel='Pressed taxel');ax.grid(False)
    savefig(fig,out/'figures/spatial_diagnostics','ungated_localization_diagnostic')
    force_detection=[]
    for label,g in e.groupby(pd.cut(e.force_peak_N,np.arange(0,26,2),right=False),observed=True):
        force_detection.append(dict(peak_force_bin_N=str(label),presses=len(g),recordings=g.recording.nunique(),detected=int(g.detected.sum()),recall=g.detected.mean()))
    export(pd.DataFrame(force_detection),out/'tables','detection_by_force_band')
    summary_rows=[
        ('Valid segmented physical presses',f'{len(e):,} events, clustered in {e.recording.nunique()} press recordings','Force-only segmentation; not all statistically independent'),
        ('Sensing area','All nine locations of one 3 × 3 BAURods skin','Taxel is a secondary grouping variable'),
        ('Acquisition scope',f'{len(inv)} recordings; one session S3 on 8 August 2026','Four baseline/settings blocks, not four independent days'),
        ('Tested peak-force range',f'{peak_min:.4f}–{peak_max:.4f} N','Retained presses; not maximum sensor capacity'),
        ('All raw reference-force range',f'{s["force_raw_tested_min"]:.4f}–{s["force_raw_tested_max"]:.4f} N','Negative unloaded offsets included'),
        ('Optical-response range',f'{e.optical_peak_V.min():.4f}–{e.optical_peak_V.max():.4f} V digital units','Peak maximum ROI mean positive delta-V from magnified images'),
        ('Global force–response relationship',eq,'Descriptive pooled association; no intrinsic calibration established'),
        ('Global slope / apparent sensitivity',f'{p[1]:.5f} V digital units/N; 95% CI {ci[0,1]:.5f} to {ci[1,1]:.5f}' if s['selected_model']=='linear' else 'No constant global sensitivity','Processing- and protocol-dependent; not intrinsic material sensitivity'),
        ('Model fit',f'R² = {chosen.r2:.4f}; RMSE = {chosen.rmse:.4f} V','In-sample, press-level'),
        ('Validation',f'Recording-held-out R² = {chosen.recording_cv_r2:.4f}; RMSE = {chosen.recording_cv_rmse:.4f} V','Model selection used this CV; no final untouched test set'),
        ('Repeatability proxy',f'Residual SD = {e.residual_V.std():.4f} V; IQR = {np.ptp(e.residual_V.quantile([.25,.75])):.4f} V','Conditional dispersion under manual loading; not controlled metrological repeatability'),
        ('Baseline variability',f'Global mean = {bas["mean_V"]:.4f} V; RMS temporal SD = {bas["noise_sd_V"]:.4f} V','11 recording-equal no-contact controls'),
        ('Statistical force detection threshold','Not quantitatively assessed','No validated replicated low-force staircase; segmentation excludes excursions <0.5 N'),
        ('Lowest observed detected press peak',f'{min_detect:.4f} N' if np.isfinite(min_detect) else 'No press detected','One observed peak, not a reliable threshold'),
        ('Configured optical decision criterion','5 V digital units','Already present in acquisition software; not 5 N'),
        ('SNR proxy',f'Median = {e.snr_proxy.median():.3f}; IQR = {np.ptp(e.snr_proxy.quantile([.25,.75])):.3f}','(Peak response − global no-contact mean) / RMS temporal SD'),
        ('Response time','Not quantitatively assessed','Sparse samples and narrow-band magnification prevent intrinsic latency inference'),
        ('Recovery time','Not quantitatively assessed','Repeated presses generally retain residual load and have no stable release baseline'),
        ('Hysteresis','Not quantitatively assessed','No controlled comparable quasi-static loading/unloading protocol'),
        ('Operating limit / saturation','Not quantitatively assessed','Tested force maximum is not a failure or saturation limit'),
        ('Contact-detection accuracy',f'{100*det["accuracy"]:.3f}% ({det["TP"]+det["TN"]}/{det["n_positive"]+det["n_negative"]})','Unequal positive/negative units; report recall and specificity alongside'),
        ('Contact-detection recall',f'{100*det["sensitivity"]:.3f}% ({det["TP"]}/{det["n_positive"]})','Existing 5-V rule'),
        ('Contact-detection specificity',f'{100*det["specificity"]:.2f}% ({det["TN"]}/{det["n_negative"]})','11 one-second no-contact controls'),
        ('Overall localization accuracy',f'{100*loc["accuracy"]:.3f}%','All valid press events; below-threshold outputs count as failures'),
        ('Macro-averaged localization F1',f'{loc["macro_f1"]:.5f}','Nine target classes; no-detection column retained'),
        ('Known-contact location discrimination',f'{100*loc["ungated_accuracy"]:.3f}% accuracy; macro F1 = {np.mean(uf):.5f}','Offline ungated argmax diagnostic, not archived end-to-end performance'),
    ]
    summary=pd.DataFrame(summary_rows,columns=['Sensor characteristic','Global BAURods result','Interpretation / scope'])
    export(summary,out/'tables','global_characterization_summary');export(summary,out/'global_characterization','global_characterization_summary')
    export(pd.DataFrame([det]),out/'tables','contact_detection_metrics')
    export(pd.DataFrame([{k:v for k,v in loc.items() if not isinstance(v,list)}]),out/'tables','localization_metrics')
    unavailable=pd.DataFrame([dict(characteristic=k,status='Not quantitatively assessed',reason=r) for k,_,r in summary_rows if k in ['Response time','Recovery time','Hysteresis','Statistical force detection threshold','Operating limit / saturation']])
    export(unavailable,out/'tables','unsupported_characteristics')
    sensitivity_text=(f'The pooled apparent slope was **{p[1]:.5f} V digital units/N** (95% recording-bootstrap CI {ci[0,1]:.5f}–{ci[1,1]:.5f}). '
        'This is a slope for the complete optical acquisition pipeline under this manual protocol. It does not establish intrinsic mechanoluminescent sensitivity or a transferable force calibration.') if s['selected_model']=='linear' else (
        'A constant sensitivity is not appropriate for the selected nonlinear descriptive relationship. The fitted equation, rather than one constant gain, defines the force dependence.')
    report=f'''# Global Sensor Characterization of BAURods

Analysis date: 5 October 2026. Experimental acquisition: 8 August 2026, Asia/Manila. Source: `Manual Calibration (2).zip`.

The available data support a global characterization of the **recorded BAURods sensing pipeline**, with substantial qualifications for force calibration, event detection and experimental independence. **{len(e):,} force-segmented presses from {e.recording.nunique()} recordings** represent all nine sensing locations. These are distinct physical presses inferred from force peaks, consistent with the operator's clarification, but presses within a recording are correlated. The experimental scope is one skin and one labelled session, S3. No video frame is counted as an independent experiment.

## Global Characterization of the BAURods Visuotactile Sensing Skin

{markdown_table(summary)}

## 1. Global Response Definition

The acquisition pipeline uses OpenCV HSV **V (brightness)**, not hue or saturation as the response. For ROI i at time t, it forms `I_i(t) = mean_p[max(V_magnified(p,t) − B_i(p), 0)]`, where B is the captured per-pixel baseline median after the software's integer conversion. The frame-level global metric is `G(t) = max_i I_i(t)`, exported as `dominant_intensity`. For press e, the primary response is **`S_e = max_(t in e) G(t)`**. The reference is **`F_e = max_(t in e) F_raw(t)`**, using the higher-rate load-cell samples. Units are **V digital units**, based on 8-bit image values, not volts, lux, physical light power or generic HSV units. Force is in newtons.

This choice follows the existing algorithm: it selects the ROI with the greatest mean positive delta-V and emits its location when the value reaches 5 V units. The metric is independent of the known pressed location, does not weight unequal ROI areas by pixel count, and gives one sensor-level response for a press anywhere on the array. The positive pixel subtraction already corrects against the captured baseline. A positive unloaded noise floor remains because negative differences are clipped and a maximum is selected. We retain this floor and an intercept rather than forcing zero response at zero force.

**All 83 archived CSV exports were computed from color-magnified images**, with amplification 30 and a 1.1–1.2 Hz temporal passband in the saved configuration. Therefore these are properties of BAURods plus this acquisition/processing configuration. They are not raw-light material constants. {original_statement}

Alternatives were compared without selecting the best-looking correlation: maximum across ROIs (primary), sum of ROI means (includes distributed noise and common-mode changes), response in the known pressed ROI (requires ground truth), response nearest the force peak (timing diagnostic), and original-video response (source diagnostic). `response_definition_comparison.csv` preserves these comparisons. The sum here is a sum of ROI **means**, not a sum of all pixels. Force and optical event peaks can occur at different times; their association describes pulse amplitudes, not a synchronized pointwise constitutive law. The nearest-force-peak response has R² {tables['response_definition_comparison'].set_index('column').loc['optical_at_force_peak_V','r2']:.4f}, substantially below the paired pulse-amplitude model; timing is a material qualification. The original-video diagnostic has R² {tables['response_definition_comparison'].set_index('column').loc['original_peak_V','r2']:.4f}. Pipeline definitions can be inspected in [feature extraction](../../calibration_gui/processing/feature_extraction.py) and [localization](../../calibration_gui/processing/localization.py).

### Experimental unit and press segmentation

The operator confirmed that the rapid loading pulses were separate finger presses. The raw load-cell trace identifies candidate peaks with prominence at least **0.5 N** and a minimum sample separation corresponding to **0.15 s** (two raw samples at the observed cadence). This allows fast pulses to remain distinct rather than forcing nearby presses into one interval. Adjacent force minima define non-overlapping optical windows `[start, end)`. A retained event must rise and fall by at least 0.5 N, have a peak above the recorded 0.05-N force contact criterion, contain at least three valid camera samples, and have a camera sample within 0.15 s of the force peak. The selected force peak must also be the maximum within its interval; unresolved intervals are rejected. **{s['excluded_candidates']} candidates were excluded** with reason codes. Optical magnitude and localization correctness never determine eligibility. The camera-sample requirement preferentially removes the fastest pulses and is an explicit sampling limitation.

The 0.5-N segmentation prominence is an analyst-defined pulse-separation rule, not a sensor detection threshold. The comparison at 0.25, 0.5 and 1.0 N gives {int(tables['segmentation_sensitivity'].events.min()):,}–{int(tables['segmentation_sensitivity'].events.max()):,} events, with pooled slopes {tables['segmentation_sensitivity'].linear_slope.min():.5f}–{tables['segmentation_sensitivity'].linear_slope.max():.5f} V units/N. This limited sensitivity check supports the stability of the main descriptive conclusion to prominence choice; it does not certify the count. A four-page segmentation atlas is supplied for all 72 press recordings. Peaks with insufficient unloading, unresolved multiple pulses, very small force excursions and recording-edge partial presses cannot be certified individually. No claim is made that the inferred count equals a manually annotated ground-truth press count.

The archive contains **{s['source_frames']:,} camera rows** and **{s['raw_force_samples']:,} raw load-cell samples** in 83 files. There are 72 press recordings (eight per location) and 11 no-contact controls. Eight NOCONTACT files retained a `Press` interaction label; their filenames and measured |F| < 0.05 N identify them as controls. The remaining three are explicitly labelled `none`. All frame validity checks pass. There are four captured baseline/settings blocks but only **one session label S3**, one sensing skin and one collection day. Recordings, not frames or alleged separate acquisition days, are the resampling clusters.

The pooled fit gives every retained physical press equal weight. Confidence intervals resample complete recordings **within each taxel**, preserving the nine fixed sensing locations ({BOOTSTRAPS:,} bootstrap replicates, seed {SEED}). Taxel is not treated as nine separate sensors or as nine independently manufactured specimens. The intervals describe recording-to-recording uncertainty within this experiment; they do not establish between-device or between-day reproducibility. They condition on recorded force and do not include independently assessed load-cell calibration uncertainty. Model validation holds out whole recordings, never random frames or presses from the same recording.

## 2. Tested Force and Response Range

Retained press peaks span **{peak_min:.4f}–{peak_max:.4f} N** and their global responses span **{e.optical_peak_V.min():.4f}–{e.optical_peak_V.max():.4f} V units**. The full raw reference-force series spans {s['force_raw_tested_min']:.4f}–{s['force_raw_tested_max']:.4f} N, including slightly negative unloaded offsets. Negative offsets were retained as source values; no speculative per-press zero adjustment was made. Mean peak force was {e.force_peak_N.mean():.4f} N (SD {e.force_peak_N.std():.4f}); mean optical response was {e.optical_peak_V.mean():.4f} V (SD {e.optical_peak_V.std():.4f}).

These are **tested ranges**, not maximum sensing limits. Different force coverage, duration, loading rate and baseline/settings blocks prevent attribution of pooled flattening or compression to material saturation. A pixel-level `saturation_warning` is a separate image-clipping diagnostic; it is not proof of force saturation. {int(e.saturation_warning.sum())} retained presses included an exported pixel-saturation warning.

{markdown_table(tables['global_descriptive_statistics'])}

![Global applied force and response](../figures/fig01_global_force_response.png)

## 3. Global Force–Response Behavior

The selected parsimonious descriptive model was **{s['selected_model']}**, with equation **{eq}**, where F is press peak force in N and S is peak global response in V digital units. In-sample R² was **{chosen.r2:.4f}** and RMSE was **{chosen.rmse:.4f} V**. Recording-held-out R² was **{chosen.recording_cv_r2:.4f}** and RMSE was **{chosen.recording_cv_rmse:.4f} V**. Intercept CI was {ci[0,0]:.5f}–{ci[1,0]:.5f}. Parameters and intervals are exported separately.

Linear, quadratic, logarithmic and power-law models were compared with a constant-response benchmark. Fits minimize squared errors in original response units; the power model is not selected by a log-transformed R². The one-standard-error rule compares recording-equal cross-validation RMSE and favors the simplest adequate force-dependent form. The constant benchmark remains an adequacy check, even when a force-dependent line is presented for interpretation. No high-order polynomial or post-hoc force range was searched to increase R². Validation here supports exploratory model comparison, not an untouched confirmatory performance estimate.

{markdown_table(models)}

![Global fitted model](../figures/fig02_global_fitted_characteristic.png)

## 4. Sensitivity and Linearity

{sensitivity_text}

R² is a measure of explained dispersion within these observations, not proof of sensor quality. The residual plot, force-decile residual means and normal-probability diagnostic show departures that an R² alone conceals. Between-recording shifts and uneven force coverage can generate a pooled slope without establishing within-location force linearity. No validated linear operating interval, linearity-error specification or force inversion accuracy is established by this experiment. Baseline/settings and taxel-adjusted models are secondary checks, not nine independent sensitivity claims.

![Residual behavior](../figures/fig03_global_model_residuals.png)
![Global response distribution](../figures/fig04_global_response_distribution.png)

## 5. Repeatability

Residual SD around the global model was **{e.residual_V.std():.4f} V**, with residual IQR **{np.ptp(e.residual_V.quantile([.25,.75])):.4f} V**. This measures force-conditioned dispersion under the manual protocol. It includes spatial differences, recording changes, loading rate, processing memory and model misspecification. It is not a controlled repeatability standard deviation at a fixed force and fixed location.

Empirical force tertiles supply comparable-force summaries without treating their raw response CV as a global repeatability claim. The pooled raw optical-response CV ({e.optical_peak_V.std()/e.optical_peak_V.mean()*100:.2f}%) is descriptive only because force varies. Individual presses remain nested in recordings throughout uncertainty analysis.

{markdown_table(tables['repeatability_statistics'])}

![Force-conditioned repeatability](../figures/fig05_global_repeatability.png)

## 6. Baseline Stability

The 11 unloaded recordings produced a recording-equal global mean response of **{bas['mean_V']:.4f} V** (95% recording-bootstrap interval {bas['mean_ci95'][0]:.4f}–{bas['mean_ci95'][1]:.4f}). The root-mean-square within-recording temporal SD was **{bas['noise_sd_V']:.4f} V**, giving a temporal-noise CV of {bas['baseline_cv_percent']:.2f}%. The SD of the 11 recording means was {bas['between_recording_sd_V']:.4f} V. Temporal SD, between-recording SD and spatial dispersion of baseline image pixels are different quantities and are not substituted for one another.

The baseline table reports each recording's descriptive drift slope in V/min. It does not treat individual frames as replicates for confidence limits. Captured baseline image means are summarized once per unique baseline ID (four observations), not duplicated for every frame. Camera saturation and sharpness controls changed across baseline blocks, while autofocus and automatic white balance were enabled in the inspected configurations. Baseline shifts therefore cannot be assigned solely to sensor drift. No long-term or between-day stability estimate is possible from this single collection day.

![Baseline stability](../figures/fig06_global_baseline_stability.png)

## 7. Detection Characteristics

The existing acquisition rule reports a taxel only when the global response reaches **5 V digital units**. Applied once per retained press at its strongest global response, this rule detected **{det['TP']}/{det['n_positive']} presses ({det['sensitivity']*100:.3f}% recall)**. Each unloaded recording contributes one centered 1-s control interval: **{det['TN']}/{det['n_negative']}** were negative, for specificity {det['specificity']*100:.2f}% (Wilson 95% interval {100*det['specificity_wilson_ci95'][0]:.2f}–{100*det['specificity_wilson_ci95'][1]:.2f}%). Controls share session/baseline conditions, so this interval is conditional on treating recordings as replicates.

Precision was {det['precision']*100:.2f}%, F1 was {det['f1']:.5f}, and accuracy was {det['accuracy']*100:.3f}%. Accuracy is dominated by the large number of loaded events and is not a balanced estimate across contact/no-contact situations. ROC-AUC of the event response score was {det['roc_auc']:.5f}; it is exploratory, uses only 11 control recordings, and compares pulse maxima with fixed-window maxima. The optical score is not a calibrated contact probability. Fixed 0.5-, 1- and 2-s negative-window checks are exported. No-contact frame counts are not used to inflate the negative sample size.

The lowest retained press peak that crossed the configured optical criterion was **{min_detect:.4f} N**. That observation is **not** a force detection threshold. Reliability at low force cannot be established from opportunistic manual pulses, especially because event eligibility requires a 0.5-N excursion. The baseline mean + 3 temporal SD value, {bas['exploratory_3sd_cutoff_V']:.4f} V, is an exploratory optical noise reference only. It was not used as a newly optimized classifier or inverted into a claimed force threshold. The maximum-across-ROI statistic, autocorrelation, temporal filtering and pulse-max selection preclude a simple Gaussian false-alarm interpretation.

The sensor-level SNR proxy is `(S_e − mean_no_contact_G) / RMS_SD_no_contact_G`. Its median was **{e.snr_proxy.median():.3f}**, IQR {np.ptp(e.snr_proxy.quantile([.25,.75])):.3f}. Negative values indicate a press peak below the pooled baseline mean. This linear ratio uses the same whole-sensor statistic for signal and noise; it is not a physical optical-power SNR and no dB conversion is asserted. No-contact controls cover only the later baseline blocks, so applying their pooled noise to earlier blocks is an exploratory approximation.

![Global contact detection](../figures/fig08_global_contact_detection.png)

## 8. Temporal Characteristics

The median camera sample interval across recordings was **{s['median_frame_dt_s']:.4f} s** (approximately {1/s['median_frame_dt_s']:.2f} samples/s); the median raw force interval was {s['median_raw_dt_s']:.4f} s. The video container's nominal frame rate does not replace actual acquisition timestamps. Median force-defined pulse duration was {e.duration_s.median():.4f} s and the median number of camera samples per press was {e.n_camera_samples.median():.0f}.

The recorded optical response passes through a narrow temporal magnification filter. Repeated manual loading often does not return to an unloaded plateau, and force and optical maxima may not align. Consequently intrinsic response time, 10–90% rise time and recovery time are **not quantitatively assessed**. `peak_time_difference_s` is preserved as a sampled force/optical peak offset, explicitly **not a response-time estimate**. Figure 7 illustrates actual force-defined pulses and optical sampling; it does not claim milliseconds of sensor latency. Unsynchronized photophysics or mechanical recovery cannot be separated from camera, filter and sampling effects with this protocol.

![Sampled temporal behavior](../figures/fig07_sampled_temporal_behavior.png)

## 9. Hysteresis

**Hysteresis could not be quantitatively characterized from the available experimental protocol.** Force rises and falls during each finger press, but this alone does not establish comparable quasi-static loading and unloading trajectories. Uncontrolled rates, residual force, sparse optical samples and the 1.1–1.2 Hz optical filter confound any apparent loop with dynamic lag. A normalized loop area or maximum separation would therefore not identify intrinsic global sensor hysteresis. No automatic-calibration data were mixed into this manual-archive analysis.

## 10. Spatial Consistency

Taxel identity is retained only as a blocking/grouping factor. Recording-level mean global residuals differ across locations; these diagnose whether one pooled response remains representative. The range of location mean residuals is **{spatial.mean_recording_residual_V.min():.4f} to {spatial.mean_recording_residual_V.max():.4f} V**. The force-only linear model had R² {modelcomp.loc['force_only','r2']:.4f}; adding baseline block gave {modelcomp.loc['force_and_baseline_block','r2']:.4f}; adding both baseline block and taxel gave {modelcomp.loc['force_baseline_and_taxel','r2']:.4f}. These comparisons include recording-held-out RMSE in the accompanying table and should not be interpreted from in-sample R² alone.

{markdown_table(tables['session_spatial_model_comparison'])}

The within-recording centered slope is {modelcomp.loc['within_recording_centered_force','force_coefficient_V_per_N']:.5f} V units/N, and equal recording weighting gives {modelcomp.loc['equal_recording_weight_linear','force_coefficient_V_per_N']:.5f} V units/N. Both are lower than the press-weighted pooled slope. Their missing cross-validation values are intentional: the first includes recording-specific intercepts unavailable for new recordings; the second is a weighting diagnostic. Their tabulated R²/RMSE are unweighted descriptive scores for comparability. The richer adjusted model improves recording-held-out RMSE to {modelcomp.loc['force_baseline_and_taxel','recording_cv_rmse']:.4f} V, which shows that acquisition and location information materially explain response variation. A single pooled line should therefore be interpreted as an average under this experiment rather than spatially uniform calibration.

Location, trial order, force distribution and baseline/camera settings are incompletely crossed. Their contributions cannot be uniquely identified as physical taxel effects. No taxel is removed, sign-flipped, declared the best sensor or given a separate primary force-response curve. A random-effects variance decomposition would be poorly identified with one specimen and one acquisition session; the simpler fixed-block diagnostics expose the confounding without asserting generalization to a population of skins.

![Spatial consistency analysis](../figures/spatial_diagnostics/spatial_residuals.png)

## 11. Localization Performance

Localization is evaluated separately from force-response characterization. The predicted location is the archived software's dominant ROI at the event's greatest global response, with the saved **5-V gate** retained. A below-threshold output is an explicit **No detection** result and counts as failure to localize a known press.

Overall BAURods localization accuracy was **{loc['accuracy']*100:.3f}%** (95% recording-bootstrap interval {loc['accuracy_ci95'][0]*100:.3f}–{loc['accuracy_ci95'][1]*100:.3f}%). **Macro-averaged F1 was {loc['macro_f1']:.5f}** (95% interval {loc['macro_f1_ci95'][0]:.5f}–{loc['macro_f1_ci95'][1]:.5f}). Macro precision was {loc['macro_precision']:.5f} and macro recall was {loc['macro_recall']:.5f}. There were {loc['abstentions']:,} no-detection outcomes. Conditional accuracy among detected presses was {loc['conditional_accuracy_detected']*100:.2f}%, but its small and selected denominator prevents use as headline localization accuracy. For comparison, removing the optical gate and always choosing the largest ROI gave {loc['ungated_accuracy']*100:.2f}% location accuracy; this diagnostic is not the deployed rule's accuracy.

For a distinct **known-contact location-discrimination task**, ungated argmax gives macro precision {np.mean(up):.5f}, macro recall {np.mean(ur):.5f}, and macro F1 **{np.mean(uf):.5f}** over all {len(e):,} presses. This uses external knowledge that a press occurred and asks which ROI responded most strongly. It is an offline diagnostic, not thresholded autonomous localization. Its separate confusion matrix is in `figures/spatial_diagnostics/ungated_localization_diagnostic.png`. The difference between this diagnostic and end-to-end performance identifies the saved detection gate as a major limitation, without claiming that a replacement gate has been validated.

The confusion matrix includes the no-detection column and all nine target classes. Per-class precision/recall/F1 are supporting diagnostics in `localization_per_class.csv`, not independent sensor-characterization conclusions. The archive does not include event predictions from the later learned localization models, so no performance of those later models is claimed here.

![Overall localization](../figures/fig09_overall_localization_confusion.png)

## 12. Characterization Summary

Across the complete 3 × 3 sensing area, BAURods produced a measurable distribution of color-magnified, baseline-referenced optical outputs under repeated manual finger pressing. A single pooled press-level response definition provides a transparent system characterization. Its force relationship is descriptive and remains affected by spatial and acquisition-block variation. The saved optical threshold detects only a limited subset of known presses; this poor threshold-specific result is reported directly rather than hidden by dropping misses or tuning on the evaluation events.

The experiment quantitatively supports tested force/response ranges, global descriptive statistics, a pooled force-response model, apparent slope where applicable, force-conditioned dispersion, sampled baseline statistics, threshold-specific event detection, and separate localization diagnostics. It does **not** establish an intrinsic force sensitivity, validated linear operating interval, statistically reliable force detection limit, intrinsic response/recovery time, hysteresis, saturation limit, multi-day drift or between-device reproducibility. These limitations arise from the experimental protocol and processing history, not a comparison with other sensors.

### Reproducibility and evidence

`global_sensor_characterization.py` reads the source ZIP without modifying it. SHA-256: `{s['archive_sha256']}`. The event table contains recording identity, force-sample row indices, start/peak/end timestamps, optical-peak row, pressed location, baseline block and all derived values. The source reconciliation table verifies exported maximum/sum arithmetic, prediction reconstruction, force-unit conversion and timing. `validation_results.json` records the executed independent arithmetic checks. `analysis_results.json`, CSV tables, PNG/PDF/SVG figures and the Excel export provide inspectable results. Source code and output hashes are recorded in `run_manifest.json`.

Method references: [SciPy peak prominence](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.peak_prominences.html) defines the peak-separation statistic; [NIST calibration model validation](https://itl.nist.gov/div898/handbook/mpc/section3/mpc365.htm) motivates residual assessment beyond R²; [scikit-learn grouped cross-validation](https://scikit-learn.org/stable/modules/cross_validation.html) explains why observations sharing a group must stay together during validation. All numerical results above come from the supplied experimental data, not those references.
'''
    (out/'global_characterization/Global_Sensor_Characterization_Report.md').write_text(report,encoding='utf-8')
    thesis=f'''# Results and Discussion: Global Sensor Characterization of BAURods

The BAURods sensing skin was characterized as an integrated 3 × 3 visuotactile sensor using repeated manual finger presses across all nine sensing locations. The supplied archive contained 72 press recordings and 11 no-contact recordings from one acquisition session on 8 August 2026. Force-only segmentation identified {len(e):,} valid press events. Each event contributed one observation, while recording identity defined the uncertainty-analysis clusters and taxel identity was retained as a secondary spatial factor. The {s['source_frames']:,} camera samples were not treated as independent experimental replicates. Because the individual presses were inferred from recorded force pulses rather than separately annotated, the event count is conditional on the stated segmentation criteria.

The global optical response was defined as the greatest mean positive baseline-corrected V-channel change across the nine image regions, maximized within each press interval. This definition follows the acquisition software's dominant-response measure and does not require knowledge of the pressed taxel. The archived measurements were derived from color-magnified images, with an amplification setting of 30 and a 1.1–1.2 Hz passband. Consequently, the results characterize the recorded sensing system under that processing configuration. The response is expressed in image V digital units and cannot be interpreted as physical light power or intrinsic mechanoluminescent sensitivity.

Retained peak forces ranged from {peak_min:.3f} to {peak_max:.3f} N, and global optical responses ranged from {e.optical_peak_V.min():.3f} to {e.optical_peak_V.max():.3f} V units. Mean peak force was {e.force_peak_N.mean():.3f} N (SD {e.force_peak_N.std():.3f}), and mean optical response was {e.optical_peak_V.mean():.3f} V units (SD {e.optical_peak_V.std():.3f}). These values describe the tested experimental range and do not establish the maximum operating range or saturation limit of BAURods.

The pooled {s['selected_model']} relationship was {eq}, with in-sample R² = {chosen.r2:.4f} and RMSE = {chosen.rmse:.4f} V units. When complete recordings were held out, R² was {chosen.recording_cv_r2:.4f} and RMSE was {chosen.recording_cv_rmse:.4f} V units. {sensitivity_text.replace('**','')} Force and optical peaks were paired by press interval and were not assumed to occur simultaneously. Residual structure and acquisition-block effects restrict the interpretation of the pooled fit as a universal force calibration.

Force-conditioned response variation was summarized by a global residual SD of {e.residual_V.std():.4f} V units and residual IQR of {np.ptp(e.residual_V.quantile([.25,.75])):.4f} V units. These measures capture variability under manual loading and include force-model error, spatial differences, recording effects and uncontrolled loading rate. They therefore indicate consistency within the observed protocol rather than metrological repeatability under fixed loading conditions. The 11 no-contact recordings had a recording-equal global mean of {bas['mean_V']:.4f} V units and RMS temporal SD of {bas['noise_sd_V']:.4f} V units. The positive unloaded response reflects clipping of negative baseline differences and selection of the maximum ROI signal. Four baseline captures and changes in camera settings further limit attribution of shifts to physical sensor drift.

At the saved 5-V optical decision criterion, {det['TP']} of {det['n_positive']} retained presses were detected, giving {det['sensitivity']*100:.3f}% recall. All {det['n_negative']} centered no-contact control windows were classified as no contact, giving {det['specificity']*100:.2f}% observed specificity, with a small control sample. The lowest detected press peak was {min_detect:.3f} N. This is an observed response and not a validated force detection threshold. A reliable threshold requires repeated low-force measurements with appropriate independent unloaded controls. The median sensor-level peak-excess-to-baseline-noise ratio was {e.snr_proxy.median():.3f}; this processing-dependent ratio is not a physical optical-power SNR.

Localization was assessed separately using the known pressed location and the archived dominant-ROI rule. Overall end-to-end localization accuracy was {loc['accuracy']*100:.3f}%, and macro-averaged F1 was {loc['macro_f1']:.5f}. The calculation retained {loc['abstentions']:,} below-threshold events as localization failures. Removing these events would condition performance on successful detection and would overstate overall localization capability. In a separate offline task where contact was known and the gate was removed, choosing the strongest ROI yielded {loc['ungated_accuracy']*100:.3f}% accuracy and macro F1 {np.mean(uf):.5f} over all presses. This indicates stronger location discrimination than autonomous detection under the saved gate and does not establish performance of a newly tuned detector. Spatial residual analysis also revealed location-dependent differences, with mean recording residuals ranging from {spatial.mean_recording_residual_V.min():.4f} to {spatial.mean_recording_residual_V.max():.4f} V units. Taxel effects could not be fully separated from acquisition order, force coverage and baseline/settings changes.

Intrinsic response time, recovery time and hysteresis were not quantitatively established. Camera samples were separated by a median of approximately {s['median_frame_dt_s']*1000:.1f} ms, and the optical stream underwent temporal filtering. The rapid manual loading cycles often retained residual force and did not provide controlled loading/unloading trajectories or stable release plateaus. Hysteresis could not be quantitatively characterized from the available experimental protocol. Overall, the analysis establishes the observed global behavior of BAURods under the recorded manual experiment while identifying the additional controlled measurements needed for intrinsic sensor specifications. No claim of superiority over other sensing technologies is made.
'''
    (out/'global_characterization/Thesis_Results_and_Discussion.md').write_text(thesis,encoding='utf-8')
    readme='''# Reproducing the BAURods global characterization

Run from the repository root with the existing analysis Python environment:

```powershell
& '.\\calibration_gui\\.venv\\Scripts\\python.exe' '.\\global_sensor_characterization.py'
```

Optional flags: `--archive PATH`, `--output PATH`, `--bootstrap 2000`.
Dependencies: numpy, pandas, scipy, scikit-learn, matplotlib and pyarrow. Existing verified original-video feature-store files are used as a secondary diagnostic. The ZIP remains the controlling source for primary results. No automatic-calibration data are loaded.

Read Global_Sensor_Characterization_Report.md first. The summary CSV has one global result column. Full precision numbers are in the CSV/JSON outputs. Main figures are supplied in PNG (300 dpi), PDF and SVG; supplementary spatial figures are in figures/spatial_diagnostics. Force segmentation plots are under global_characterization/audit. Every event links back to its recording and force/frame indices.

The primary result characterizes the archived color-magnified processing pipeline. Distinct physical presses within a recording are correlated. Confidence intervals resample whole recordings within the nine fixed locations. Unsupported intrinsic characteristics are explicitly marked rather than filled with invented measurements.
'''
    (out/'global_characterization/README.md').write_text(readme,encoding='utf-8')

def validate(out,events,recordings,s,tables):
    """Independent scalar reconstructions of all retained event amplitudes."""
    failures=[]; max_force_err=0.; max_opt_err=0.; assigned={}
    arrays={rid:(d.time_s.to_numpy(),d[OPT_COLS].to_numpy(),r.force_N.to_numpy()) for rid,(d,r,_) in recordings.items()}
    for row in events.itertuples():
        d,r,m=recordings[row.recording]
        times,optics,forces=arrays[row.recording]
        force=max(float(v) for v in forces[row.raw_start_row:row.raw_end_row+1])
        # Excluded neighbouring peaks can occur inside a segment. Ensure the
        # selected candidate is in fact the maximum of its retained interval.
        max_force_err=max(max_force_err,abs(force-row.force_peak_N))
        positions=[i for i,t in enumerate(times) if row.start_s<=t<row.end_s]
        opt=max(float(optics[i,j]) for i in positions for j in range(9))
        max_opt_err=max(max_opt_err,abs(opt-row.optical_peak_V))
        used=assigned.setdefault(row.recording,set())
        if used.intersection(positions): failures.append('shared optical samples between events: '+row.event_id)
        used.update(positions)
    if max_force_err>1e-9: failures.append('event force maximum mismatch')
    if max_opt_err>1e-9: failures.append('event optical maximum mismatch')
    if events.event_id.duplicated().any():failures.append('duplicate event IDs')
    if sorted(events.target.unique())!=list(range(1,10)):failures.append('missing sensing location')
    if events.recording.nunique()!=72:failures.append('missing press recordings')
    reconciliation=pd.read_csv(out/'tables/source_reconciliation.csv')
    if reconciliation.prediction_mismatches.sum():failures.append('source prediction mismatches')
    det=s['detection']; loc=s['localization']
    cm=tables['localization_confusion_matrix'].drop(columns='actual_taxel').to_numpy()
    assert int(cm.sum())==len(events)
    independently_correct=sum(int(cm[i,i+1]) for i in range(9))
    assert np.isclose(independently_correct/len(events),loc['accuracy'])
    assert det['TP']+det['FN']==len(events) and det['TN']+det['FP']==11
    assert sum((not meta['no_contact'])*len(df) for df,_,meta in recordings.values())+sum(meta['no_contact']*len(df) for df,_,meta in recordings.values())==s['source_frames']
    # Independent linear regression using SciPy, not the least-squares helper.
    linear=tables['fitted_model_comparison'].set_index('model').loc['linear']
    fit=stats.linregress(events.force_peak_N,events.optical_peak_V)
    assert np.isclose(fit.rvalue**2,linear.r2,atol=1e-10)
    result=dict(status='PASS' if not failures else 'FAIL',failures=failures,
        all_event_amplitudes_recomputed=True,n_events_checked=len(events),
        max_force_error_N=max_force_err,max_optical_error_V=max_opt_err,
        no_camera_samples_shared_between_retained_events=not any('shared' in f for f in failures),
        all_nine_locations=True,recording_clusters=events.recording.nunique(),
        independent_linear_r2=fit.rvalue**2,localization_correct_count=independently_correct,
        source_archive_sha256=s['archive_sha256'],visual_QA='PNG figures inspected separately by the analyst',
        independence_limit='One physical skin and one labelled acquisition session; repeated presses cluster in 72 recordings')
    write_json(out/'global_characterization/validation_results.json',result)
    if failures: raise AssertionError(failures)
    manifest=dict(code_sha256=sha256(Path(__file__)),source_archive_sha256=s['archive_sha256'],
        python_version=sys.version,packages={p:importlib.metadata.version(p) for p in ['numpy','pandas','scipy','scikit-learn','matplotlib','pyarrow']},outputs={})
    for directory,dirs,files in os.walk(out,followlinks=False):
        dirs[:]=[d for d in dirs if d not in {'node_modules','.mplconfig','__pycache__'}]
        for name in sorted(files):
            path=Path(directory)/name
            if path.suffix in {'.csv','.md','.png','.pdf','.svg','.json','.xlsx'} and path.name!='run_manifest.json':
                manifest['outputs'][str(path.relative_to(out))]=sha256(path)
    write_json(out/'global_characterization/run_manifest.json',manifest)

def main():
    global BOOTSTRAPS
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--archive',type=Path,default=Path(r'C:\Users\DLSU\Downloads\THESIS DATA\Data Collection\Manual Calibration (2).zip'))
    ap.add_argument('--output',type=Path,default=ROOT/'outputs')
    ap.add_argument('--bootstrap',type=int,default=BOOTSTRAPS)
    args=ap.parse_args()
    BOOTSTRAPS=args.bootstrap
    out=args.output
    for p in ['global_characterization','global_characterization/audit','figures','tables','spatial_diagnostics','figures/spatial_diagnostics']:
        (out/p).mkdir(parents=True,exist_ok=True)
    print('Reading and reconciling source archive...',flush=True)
    recordings,inventory,archive_hash=load_recordings(args.archive,out)
    events,excluded=segment(recordings)
    print(f'Segmented {len(events)} valid presses from {events.recording.nunique()} recordings; {len(excluded)} candidate exclusions',flush=True)
    export(excluded,out/'tables','excluded_press_candidates')
    models,cv,coeffs,selected=compare_models(events)
    print(models.to_string(index=False),flush=True); print('SELECTED',selected,flush=True)
    grid=np.linspace(events.force_peak_N.min(),events.force_peak_N.max(),200)
    pars,curves,means=bootstrap(events,selected,grid)
    print('Global model bootstrap complete',flush=True)
    estimate,p=fit_predict(events.force_peak_N,events.optical_peak_V,grid,selected)
    fitted,_=fit_predict(events.force_peak_N,events.optical_peak_V,events.force_peak_N,selected)
    events['fitted_V']=fitted; events['residual_V']=events.optical_peak_V-fitted
    curve=pd.DataFrame(dict(force_N=grid,response_V=estimate,ci95_low=np.quantile(curves,.025,axis=0),ci95_high=np.quantile(curves,.975,axis=0)))
    baseline,baseline_summary=baseline_tables(recordings,events)
    loc_table,loc_cm,localization=localization_tables(events)
    print('Localization cluster bootstrap complete',flush=True)
    detection_units,detection=detection_tables(recordings,events)
    spatial_models,recording_diagnostics,spatial=spatial_session_tables(events,selected)
    summary_stats=pd.DataFrame([
        descriptive(events.force_peak_N,'Peak force per press','N',np.quantile(means[:,0],[.025,.975])),
        descriptive(events.optical_peak_V,'Peak global optical response per press','V digital units',np.quantile(means[:,1],[.025,.975])),
        descriptive(events.force_excursion_N,'Force excursion per press','N'),
        descriptive(events.duration_s,'Force-defined pulse duration','s'),
        descriptive(events.snr_proxy,'Peak excess / pooled baseline temporal SD','dimensionless'),
        descriptive(baseline.mean_V,'No-contact recording mean global response','V digital units',baseline_summary['mean_ci95']),
        descriptive(inventory.drop_duplicates('baseline_id').baseline_mean_v,'Captured baseline global raw V mean','V digital units')])
    repeat=[]
    bands=pd.qcut(events.force_peak_N,3,duplicates='drop')
    for label,g in events.groupby(bands,observed=True):
        repeat.append(dict(force_band=str(label),events=len(g),recordings=g.recording.nunique(),min_force_N=g.force_peak_N.min(),max_force_N=g.force_peak_N.max(),
            response_mean_V=g.optical_peak_V.mean(),response_sd_V=g.optical_peak_V.std(),response_cv_percent=g.optical_peak_V.std()/g.optical_peak_V.mean()*100,
            residual_sd_V=g.residual_V.std(),residual_rmse_V=np.sqrt(np.mean(g.residual_V**2)),residual_iqr_V=np.ptp(g.residual_V.quantile([.25,.75]))))
    repeat=pd.DataFrame(repeat)
    segmentation=[]
    for prominence in [.25,.5,1.0]:
        ev,exc=segment(recordings,prominence)
        yh,cp=fit_predict(ev.force_peak_N,ev.optical_peak_V,ev.force_peak_N,'linear')
        segmentation.append(dict(prominence_N=prominence,events=len(ev),excluded=len(exc),recordings=ev.recording.nunique(),
            linear_slope=cp[1],linear_intercept=cp[0],**score(ev.optical_peak_V.to_numpy(),yh),localization_accuracy=np.mean(ev.target==ev.predicted_taxel),detected_fraction=ev.detected.mean()))
    aggregation=[]
    for col,definition in [('optical_peak_V','Primary: peak maximum ROI mean positive delta-V'),('whole_array_peak_V','Alternative: peak sum of nine ROI mean positive delta-V'),
        ('active_target_peak_V','Alternative: peak response in labelled pressed ROI'),('optical_at_force_peak_V','Timing diagnostic: maximum ROI response nearest force peak'),
        ('original_peak_V','Source diagnostic: peak maximum from original decoded video')]:
        y=events[col].to_numpy(); valid=np.isfinite(y)
        if valid.any():
            y=y[valid]; x=events.force_peak_N.to_numpy()[valid]; yh,cp=fit_predict(x,y,x,'linear')
            aggregation.append(dict(definition=definition,column=col,n=len(y),mean=y.mean(),sd=y.std(ddof=1),slope=cp[1],intercept=cp[0],**score(y,yh)))
        else:
            aggregation.append(dict(definition=definition,column=col,n=0,mean=np.nan,sd=np.nan,slope=np.nan,intercept=np.nan,r2=np.nan,rmse=np.nan,mae=np.nan))
    # Duration matching check for negative windows, never inflate n_control.
    duration_check=[]
    for width in [.5,1.,2.]:
        scores=[]
        for rid,(d,r,m) in recordings.items():
            if m['no_contact']:
                center=(d.time_s.min()+d.time_s.max())/2
                scores.append(d.loc[d.time_s.between(center-width/2,center+width/2),'global_max'].max())
        duration_check.append(dict(control_window_s=width,negative_recordings=len(scores),false_positives=int((np.array(scores)>=OPTICAL_THRESHOLD).sum()),max_negative_score=max(scores)))
    stats_dict=dict(archive=str(args.archive),archive_sha256=archive_hash,source_frames=int(inventory.frame_count.sum()),
        raw_force_samples=int(inventory.raw_count.sum()),recordings=len(inventory),press_recordings=int((~inventory.no_contact).sum()),
        no_contact_recordings=int(inventory.no_contact.sum()),sessions=inventory.session_label.unique().tolist(),
        label_conflicts=int(inventory.label_conflict.sum()),events=len(events),excluded_candidates=len(excluded),
        selected_model=selected,selected_parameters=p.tolist(),parameter_ci95=np.quantile(pars,[.025,.975],axis=0).tolist(),
        baseline=baseline_summary,localization=localization,detection=detection,
        cluster_bootstraps=BOOTSTRAPS,force_raw_tested_min=float(inventory.force_min.min()),force_raw_tested_max=float(inventory.force_max.max()),
        median_frame_dt_s=float(inventory.median_frame_dt_s.median()),median_raw_dt_s=float(inventory.median_raw_dt_s.median()),
        random_seed=SEED)
    temporal_eligibility=pd.DataFrame([dict(n_press_events=len(events),median_samples_per_press=float(events.n_camera_samples.median()),
        presses_starting_unloaded=int(events.starts_unloaded.sum()),presses_ending_unloaded=int(events.ends_unloaded.sum()),
        presses_with_both_unloaded_bounds=int((events.starts_unloaded & events.ends_unloaded).sum()),
        camera_dt_median_s=float(inventory.median_frame_dt_s.median()),camera_dt_max_s=float(inventory.max_frame_dt_s.max()),
        response_time_status='Not quantitatively assessed',recovery_time_status='Not quantitatively assessed',
        reason='Temporal magnification, sparse optical sampling, uncontrolled contact/release and residual load')])
    tables={'global_force_response_data':events,'global_descriptive_statistics':summary_stats,'fitted_model_comparison':models,
        'cross_validated_predictions':cv,'fitted_global_curve':curve,'repeatability_statistics':repeat,
        'baseline_statistics':baseline,'localization_per_class':loc_table,'localization_confusion_matrix':loc_cm,
        'contact_detection_units':detection_units,'segmentation_sensitivity':pd.DataFrame(segmentation),
        'response_definition_comparison':pd.DataFrame(aggregation),'session_spatial_model_comparison':spatial_models,
        'control_window_sensitivity':pd.DataFrame(duration_check),'temporal_eligibility':temporal_eligibility}
    for name,df in tables.items(): export(df,out/'tables',name)
    export(recording_diagnostics,out/'spatial_diagnostics','recording_diagnostics')
    export(spatial,out/'spatial_diagnostics','spatial_consistency')
    write_json(out/'global_characterization'/'analysis_results.json',stats_dict)
    print('Tables saved; rendering figures and report',flush=True)
    make_figures(out,events,curve,models,baseline,recordings,loc_cm,detection,spatial,recording_diagnostics)
    make_report(out,stats_dict,tables,inventory,spatial,recordings)
    validate(out,events,recordings,stats_dict,tables)
    print(json.dumps(stats_dict,indent=2,default=serial),flush=True)


if __name__=='__main__':
    main()
