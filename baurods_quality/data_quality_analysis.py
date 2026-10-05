"""BAURods manual calibration audit. Read-only source ZIP; separate derived outputs.

Run with the project analysis environment or requirements.txt. All recording
summaries are descriptive; camera frames are not independent press replicates.
"""
from __future__ import annotations
import argparse
import collections
import hashlib
import io
import json
import logging
import platform
import os
import re
import tempfile
import zipfile
from pathlib import Path
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from scipy import stats, signal
os.environ.setdefault('MPLCONFIGDIR',str(Path(__file__).resolve().parent/'.mplconfig'))
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from sklearn.metrics import confusion_matrix, precision_recall_fscore_support

DEFAULT_ARCHIVE = Path(r'C:\Users\DLSU\Downloads\THESIS DATA\Data Collection\Manual Calibration (2).zip')
ROOT = Path(__file__).resolve().parent
SEED = 20261005
COLORS = ['#245c86', '#b48328', '#a24e3e', '#65744e', '#97667c', '#478681', '#687fa6', '#85755b', '#525e70']

def sha256(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(8*1024*1024),b''): h.update(b)
    return h.hexdigest()

def summarize(s):
    x=pd.to_numeric(s,errors='coerce').dropna()
    mean=x.mean(); sd=x.std(ddof=1)
    return dict(n=len(x),mean=mean,sd=sd,median=x.median(),q1=x.quantile(.25),q3=x.quantile(.75),
                iqr=x.quantile(.75)-x.quantile(.25),minimum=x.min(),maximum=x.max(),
                cv_percent=sd/mean*100 if len(x)>1 and mean>0 else np.nan)

def group_stats(df,groups,variables):
    out=[]
    for key,g in df.groupby(groups,dropna=False,sort=True):
        key=(key,) if not isinstance(key,tuple) else key
        for v in variables: out.append({**dict(zip(groups,key)), 'variable':v, **summarize(g[v])})
    return pd.DataFrame(out)

def save_table(df,name,out):
    df.to_csv(out/'tables'/f'{name}.csv',index=False)
    return df

def inventory_archive(z,out):
    """Read every member to exercise ZIP CRC; parse every CSV/JSON/NPZ."""
    inventory=[]; dictionaries=[]
    for info in z.infolist():
        if info.is_dir(): continue
        row=dict(member=info.filename,bytes=info.file_size,compressed_bytes=info.compress_size,
                 extension=Path(info.filename).suffix,crc_valid=False,rows=None,columns=None,parse_error='')
        try:
            content=z.read(info);row['crc_valid']=True
            row['sha256']=hashlib.sha256(content).hexdigest()
            if row['extension']=='.csv':
                d=pd.read_csv(io.BytesIO(content));row.update(rows=len(d),columns=len(d.columns))
                if info.filename.endswith('/data_dictionary.csv'): dictionaries.append(d)
            elif row['extension']=='.json': json.loads(content)
            elif row['extension']=='.npz':
                a=np.load(io.BytesIO(content),allow_pickle=False)
                for k in a.files: a[k]
        except Exception as e: row['parse_error']=str(e)
        inventory.append(row)
    save_table(pd.DataFrame(inventory),'archive_inventory',out)
    dictionary=pd.concat(dictionaries).drop_duplicates()
    save_table(dictionary,'source_data_dictionary',out)
    return pd.DataFrame(inventory),dictionary

def load_recordings(z,out,decode_video):
    frames=[]; raw=[]; records=[]; baselines=[]; profiles=[]; anomalies=[]; calibrations=[]
    configs=sorted(n for n in z.namelist() if n.endswith('/session_config.json'))
    for i,n in enumerate(configs):
        prefix=n.rsplit('/',1)[0]; rec=prefix.rsplit('/',1)[-1]
        config=json.loads(z.read(n)); status=json.loads(z.read(prefix+'/session_status.json'))
        b=json.loads(z.read(prefix+'/baseline_summary.json')); cal=json.loads(z.read(prefix+'/loadcell_calibration.json'))
        m=pd.read_csv(z.open(prefix+'/master_synchronized.csv')).copy()
        f=pd.read_csv(z.open(prefix+'/frame_features.csv'))
        r=pd.read_csv(z.open(prefix+'/loadcell_raw.csv'))
        labels=config['trial_labels']; target=int(labels['target_roi_ground_truth'])
        is_nc='NOCONTACT' in labels['trial_id'].upper()
        match=re.search(r'TEST(\d+)',labels['trial_id'])
        group=('TEST'+match[1]+('_v2' if '_v2' in labels['trial_id'] else '')) if match else 'NOCONTACT'
        for scope,df in [('master_synchronized.csv',m),('frame_features.csv',f),('loadcell_raw.csv',r)]:
            for col in df:
                profiles.append(dict(recording_id=rec,artifact_scope=scope,column=col,dtype=str(df[col].dtype),
                    rows=len(df),missing=int(df[col].isna().sum()),distinct=int(df[col].nunique(dropna=True))))
        common=[c for c in f if c in m]
        aligned=f.capture_frame_id.equals(m.capture_frame_id)
        disagree=[]
        if aligned:
            for c in common:
                a=f[c];bb=m[c]
                if pd.api.types.is_numeric_dtype(a) and pd.api.types.is_numeric_dtype(bb):
                    ok=np.isclose(a.to_numpy(dtype=float),bb.to_numpy(dtype=float),equal_nan=True,rtol=1e-10,atol=1e-10)
                else: ok=(a.eq(bb)|(a.isna()&bb.isna())).to_numpy()
                if not np.all(ok): disagree.append(c)
        # Independent clock and force reconstruction uses the recorded host clock.
        rt=r.host_monotonic_ns.to_numpy(np.int64); ft=m.host_monotonic_ns.to_numpy(np.int64)
        ix=np.searchsorted(rt,ft); lo=np.clip(ix-1,0,len(rt)-1);hi=np.clip(ix,0,len(rt)-1)
        nearest=np.where(np.abs(ft-rt[lo])<=np.abs(ft-rt[hi]),lo,hi)
        offset=(ft-rt[nearest])/1e6
        reconstructed=np.interp((ft-rt[0])/1e9,(rt-rt[0])/1e9,r.force_N)
        core=['capture_frame_id','host_monotonic_ns','elapsed_time_s','wall_clock_iso','session_id','trial_id',
              'target_roi_ground_truth','force_N']+[f'roi{k}_delta_v_mean' for k in range(1,10)]
        core_complete=m[core].notna().all(axis=1)
        contact=m.contact_state_derived.eq(True)
        valid=m.frame_valid.eq(True)&m.baseline_valid.eq(True)&m.loadcell_valid.eq(True)&m.synchronization_valid.eq(True)&core_complete
        optical=m[f'roi{target}_delta_v_mean']
        m['recording_id']=rec;m['trial_group']=group;m['no_contact_recording']=is_nc
        m['target_optical']=optical;m['core_complete']=core_complete;m['core_valid']=valid
        m['archive_row_number']=np.arange(len(m))+2
        r['recording_id']=rec
        frames.append(m);raw.append(r)
        runs=int((contact&~contact.shift(fill_value=False)).sum())
        subset=m.loc[contact&valid]
        intervals=np.diff(ft)/1e9
        decoded=np.nan
        if decode_video:
            import cv2
            with tempfile.TemporaryDirectory(prefix='baurods-video-') as temp:
                p=Path(temp)/'recording.mp4';p.write_bytes(z.read(prefix+'/session_video.mp4'))
                cap=cv2.VideoCapture(str(p));decoded=0
                while True:
                    ok,_=cap.read()
                    if not ok: break
                    decoded+=1
                cap.release()
        rr=dict(recording_id=rec,archive_prefix=prefix,session_id=labels['session_id'],trial_id=labels['trial_id'],
            taxel=f'T{target}',trial_group=group,no_contact_recording=is_nc,interaction_label=labels['trial_interaction_class'],
            frames=len(m),feature_rows=len(f),loadcell_rows=len(r),expected_accepted_frames=status['accepted_frame_count'],
            expected_loadcell_samples=status['ingress_accepted_loadcell_sample_count'],video_frames_declared=status['video_frame_count'],
            decoded_video_frames=decoded,master_columns=len(m.columns)-7,feature_columns=len(f.columns),raw_columns=len(r.columns)-1,
            core_complete=int(core_complete.sum()),core_valid=int(valid.sum()),core_completeness_percent=100*core_complete.mean(),
            any_field_complete=int(m.drop(columns=['recording_id','trial_group','no_contact_recording','target_optical','core_complete','core_valid','archive_row_number']).notna().all(axis=1).sum()),
            exact_duplicate_rows=int(f.duplicated().sum()),duplicate_frame_ids=int(f.capture_frame_id.duplicated().sum()),
            missing_frame_ids=int(m.capture_frame_id.max()-m.capture_frame_id.min()+1-len(m)),
            timestamp_nonincreasing=int((intervals<=0).sum()),raw_clock_nonincreasing=int((np.diff(rt)<=0).sum()),
            raw_duplicate_ids=int(r.arduino_sample_id.duplicated().sum()),raw_id_gaps=int((r.arduino_sample_id.diff()-1).clip(lower=0).sum()),
            contact_frames=int(contact.sum()),no_contact_frames=int((~contact).sum()),contact_runs=runs,
            contact_at_start=bool(contact.iloc[0]),contact_at_end=bool(contact.iloc[-1]),
            force_mean_N=subset.force_N.mean(),force_peak_N=subset.force_N.max(),
            optical_mean_delta_v=subset.target_optical.mean(),optical_peak_delta_v=subset.target_optical.max(),
            force_all_min_N=m.force_N.min(),force_all_max_N=m.force_N.max(),
            duration_s=(ft[-1]-ft[0])/1e9,cadence_median_ms=np.median(intervals)*1000,cadence_max_ms=np.max(intervals)*1000,
            effective_fps=(len(m)-1)/((ft[-1]-ft[0])/1e9),start_utc=m.wall_clock_iso.iloc[0],end_utc=m.wall_clock_iso.iloc[-1],
            baseline_id=b['baseline_id'],baseline_capture_utc=b['capture_timestamp_iso'],
            source=config.get('quantitative_analysis_source'),magnified=config.get('magnified_pixels_used_for_exported_measurements'),
            sync_invalid=int((~m.synchronization_valid.eq(True)).sum()),
            sync_offset_reconstruction_max_error_ms=float(np.max(np.abs(offset-m.synchronization_offset_ms))),
            force_reconstruction_max_error_N=float(np.max(np.abs(reconstructed-m.force_N))),
            feature_master_disagreements=';'.join(disagree),feature_frame_alignment=aligned,
            baseline_drift_mean_v=config.get('pre_recording_baseline_drift_mean_v'),
            baseline_drift_threshold=config.get('baseline_drift_threshold'),baseline_drift_passed=config.get('baseline_drift_passed'),
            complete=status['complete'],calibration_id=cal['calibration_id'],
            auto_white_balance=config.get('camera_confirmed_actual_controls',{}).get('auto_white_balance'),
            auto_focus=config.get('camera_confirmed_actual_controls',{}).get('auto_focus'),
            expected_calibration_mass_g=cal['known_mass_g'],prediction_count=int(m.predicted_dominant_roi.notna().sum()))
        records.append(rr)
        for roi in b['rois']:
            baselines.append(dict(recording_id=rec,baseline_id=b['baseline_id'],capture_utc=b['capture_timestamp_iso'],
                roi=f"T{roi['roi_id']}",mean_v=roi['mean_v'],median_v=roi['median_v'],spatial_std_v=roi['std_v'],
                baseline_frames=roi['valid_frame_count']))
        calibrations.append(dict(recording_id=rec,calibration_id=cal['calibration_id'],counts_per_gram=cal['counts_per_gram'],
            tare_raw=cal['tare_raw'],known_mass_g=cal['known_mass_g'],quality_passed=cal['quality_passed'],
            verification_error_percent=cal['verification']['percentage_error'],verification_passed=cal['verification']['passed']))
        if is_nc and labels['trial_interaction_class'].lower() not in ['none','no contact','nocontact']:
            anomalies.append(dict(recording_id=rec,row_or_trial=labels['trial_id'],variable='trial_interaction_class',value=labels['trial_interaction_class'],
                reason='NOCONTACT trial name contradicts Press trial label; force-derived contact is zero',status='retained',
                justification='Treat separately as filename-indicated no-contact; retain original label.'))
        for flag,reason in [(~core_complete,'Missing required core measurement'),(~valid,'Invalid source quality flag or incomplete core')]:
            for j in m.index[flag]:
                anomalies.append(dict(recording_id=rec,row_or_trial=int(j)+2,variable='core',value='',reason=reason,status='retained in source; omitted only from affected calculations',justification='Explicit validity/missingness eligibility'))
        if i%15==0: logging.info('Loaded %d/%d recordings',i+1,len(configs))
    return pd.concat(frames,ignore_index=True),pd.concat(raw,ignore_index=True),pd.DataFrame(records),pd.DataFrame(baselines),pd.DataFrame(profiles),pd.DataFrame(anomalies),pd.DataFrame(calibrations)

def assess_baseline(frames,records,base,out):
    unique=base.drop_duplicates(['baseline_id','roi']).sort_values(['capture_utc','roi'])
    save_table(unique,'unique_baselines',out)
    save_table(group_stats(unique,['roi'],['mean_v']),'baseline_across_captures',out)
    rows=[]
    # Actual no-contact recordings are a stronger temporal check than repeating a saved scalar.
    for rec,g in frames[frames.no_contact_recording].groupby('recording_id'):
        window=max(1,int(np.ceil(.1*len(g))))
        for roi in range(1,10):
            for variable in [f'roi{roi}_mean_v',f'roi{roi}_delta_v_mean']:
                s=g[variable];start=s.iloc[:window].mean();end=s.iloc[-window:].mean()
                rows.append(dict(recording_id=rec,roi=f'T{roi}',variable=variable,**summarize(s),
                    window_frames=window,start_mean=start,end_mean=end,end_minus_start=end-start,
                    absolute_drift_percent=100*abs(end-start)/abs(start) if start!=0 else np.nan))
    nc=pd.DataFrame(rows);save_table(nc,'no_contact_baseline_stability',out)
    return unique,nc

def assess_performance(frames,out):
    """Audit the existing thresholded optical predictor, without model fitting."""
    eligible=frames.core_valid & frames.contact_state_derived.eq(True) & ~frames.no_contact_recording
    d=frames[eligible];y=d.target_roi_ground_truth.to_numpy(int)
    pred=d.predicted_dominant_roi.fillna(0).to_numpy(int);covered=pred!=0
    matrix=confusion_matrix(y,pred,labels=list(range(10)))[1:,:]
    cm=pd.DataFrame(matrix,columns=['withheld']+[f'T{i}' for i in range(1,10)]);cm.insert(0,'true_taxel',[f'T{i}' for i in range(1,10)])
    save_table(cm,'localization_confusion_counts',out)
    pr,rc,f1,sup=precision_recall_fscore_support(y,pred,labels=list(range(1,10)),zero_division=0)
    metrics=[]
    for i in range(1,10):
        mask=y==i;cv=mask&covered;correct=int(((y==pred)&mask).sum())
        metrics.append(dict(taxel=f'T{i}',n=int(mask.sum()),covered=int(cv.sum()),correct=correct,
            coverage_percent=100*cv.sum()/mask.sum(),accuracy_all_percent=100*correct/mask.sum(),
            accuracy_covered_percent=100*correct/cv.sum() if cv.sum() else np.nan,
            precision=pr[i-1],recall=rc[i-1],f1=f1[i-1]))
    met=save_table(pd.DataFrame(metrics),'localization_metrics',out)
    # Availability of a localization prediction is only an optical detection proxy.
    true=frames.loc[frames.core_valid,'contact_state_derived'].astype(bool)
    proxy=frames.loc[frames.core_valid,'predicted_dominant_roi'].notna()
    detection=dict(n=len(true),tp=int((true&proxy).sum()),fn=int((true&~proxy).sum()),
                   fp=int((~true&proxy).sum()),tn=int((~true&~proxy).sum()))
    save_table(pd.DataFrame([detection]),'contact_detection_proxy',out)
    return met,cm,dict(n=len(d),covered=int(covered.sum()),correct=int((y==pred).sum()),
        coverage_percent=100*covered.mean(),accuracy_all_percent=100*np.mean(y==pred),
        accuracy_covered_percent=100*np.mean(y[covered]==pred[covered]) if covered.any() else None,
        macro_f1=float(np.mean(f1)),detection_proxy=detection)

def assess_outliers(trials,anomalies,out):
    rows=anomalies.to_dict('records')
    for taxel,g in trials.groupby('taxel'):
        for v in ['force_mean_N','force_peak_N','optical_mean_delta_v','optical_peak_delta_v']:
            q1,q3=g[v].quantile([.25,.75]);iqr=q3-q1;lo=q1-1.5*iqr;hi=q3+1.5*iqr
            for _,r in g[(g[v]<lo)|(g[v]>hi)].iterrows():
                rows.append(dict(recording_id=r.recording_id,row_or_trial=r.trial_id,variable=v,value=r[v],
                    reason=f'Within-{taxel} 1.5 IQR flag; bounds [{lo:.9g}, {hi:.9g}]',status='retained',
                    justification='Statistical flag only; genuine manual loading or optical variation remains possible.'))
    return save_table(pd.DataFrame(rows),'outlier_anomaly_log',out)

def assess_correlations(trials,out):
    rows=[]
    for name,g in [('All',trials)]+list(trials.groupby('taxel'))+list(trials.groupby('trial_group')):
        for a,b,metric in [('force_mean_N','optical_mean_delta_v','contact means'),('force_peak_N','optical_peak_delta_v','contact peaks')]:
            d=g[[a,b]].dropna()
            rho=float(stats.spearmanr(d[a],d[b]).statistic) if len(d)>2 and d[a].nunique()>1 and d[b].nunique()>1 else np.nan
            rows.append(dict(group=name,metric=metric,n_recordings=len(d),spearman_rho=rho,
                inference='Descriptive only; no p-value/CI because one session and baseline/time grouping do not establish independent replicates.'))
    return save_table(pd.DataFrame(rows),'force_optical_correlations',out)

def assess_timing(frames,records,out):
    variables=['synchronization_offset_ms','nearest_sample_gap_ms']
    grouped=group_stats(frames,['recording_id'],variables)
    grouped.loc[grouped.variable.eq('synchronization_offset_ms'),'cv_percent']=np.nan
    save_table(grouped,'synchronization_by_recording',out)
    overall=pd.DataFrame([{'variable':v,**summarize(frames[v])} for v in variables])
    overall.loc[overall.variable.eq('synchronization_offset_ms'),'cv_percent']=np.nan
    save_table(overall,'synchronization_overall',out)
    # Exploratory whole-recording cross correlation, not a sensor response-time claim.
    cross=[]
    for rec,g in frames[~frames.no_contact_recording].groupby('recording_id'):
        dt=float(np.median(np.diff(g.elapsed_time_s)));t=np.arange(g.elapsed_time_s.min(),g.elapsed_time_s.max(),dt)
        a=np.interp(t,g.elapsed_time_s,g.force_N);b=np.interp(t,g.elapsed_time_s,g.target_optical)
        candidates=[]
        for shift in range(-int(1/dt),int(1/dt)+1):
            x,y=(a[:-shift],b[shift:]) if shift>0 else ((a[-shift:],b[:shift]) if shift<0 else (a,b))
            r=np.corrcoef(x,y)[0,1] if np.std(x)>0 and np.std(y)>0 else np.nan
            candidates.append((shift*dt,r))
        lag,r=max(candidates,key=lambda x:x[1] if np.isfinite(x[1]) else -2)
        cross.append(dict(recording_id=rec,grid_interval_s=dt,lag_s=lag,correlation_at_lag=r,
            search_bound_s=1,at_search_boundary=abs(lag)>=int(1/dt)*dt-1e-8,
            interpretation='Exploratory waveform alignment only; optical magnification and multi-press ambiguity prevent sensor-latency inference.'))
    save_table(pd.DataFrame(cross),'exploratory_cross_correlation',out)
    return overall

def plot_figures(frames,records,trials,base,nc,repeat,force,perf,cm,out):
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':11,'axes.titlesize':14,'axes.titleweight':'bold',
        'axes.labelcolor':'#253444','text.color':'#253444','axes.spines.top':False,'axes.spines.right':False,
        'axes.grid':True,'grid.alpha':.18,'axes.axisbelow':True,'figure.facecolor':'white','savefig.facecolor':'white'})
    def save(fig,name):
        fig.savefig(out/'figures'/f'{name}.png',dpi=320,bbox_inches='tight')
        fig.savefig(out/'figures'/f'{name}.svg',bbox_inches='tight');plt.close(fig)
    taxels=[f'T{i}' for i in range(1,10)]
    fig,ax=plt.subplots(figsize=(10,4.6));counts=trials.groupby('taxel').frames.sum().reindex(taxels)
    ax.bar(taxels,counts,color=COLORS[0]);ax.set(ylabel='Frames in press recordings',xlabel='Target taxel',title='Frame coverage varies; each taxel has eight trial recordings')
    for i,v in enumerate(counts):ax.text(i,v+35,f'{v:,}',ha='center',fontsize=10)
    ax.set_ylim(0,counts.max()*1.15);save(fig,'01_taxel_coverage')
    rng=np.random.default_rng(SEED)
    for v,label,num in [('optical_mean_delta_v','Mean target optical response (delta V)',2),('force_mean_N','Mean applied force (N)',3)]:
        fig,ax=plt.subplots(figsize=(10,5));data=[trials.loc[trials.taxel.eq(t),v] for t in taxels]
        bp=ax.boxplot(data,tick_labels=taxels,patch_artist=True,showfliers=False)
        for patch in bp['boxes']:patch.set(facecolor='#dce8f0',edgecolor=COLORS[0])
        for i,vals in enumerate(data):ax.scatter(i+1+rng.uniform(-.14,.14,len(vals)),vals,color=COLORS[0],s=28,zorder=3,alpha=.8)
        ax.set(ylabel=label,xlabel='Taxel · one dot per recording',title=('Optical response' if num==2 else 'Manual loading')+' varies across repeated recordings')
        save(fig,f'{num:02d}_'+('optical_repeatability' if num==2 else 'force_variability'))
    fig,axs=plt.subplots(1,2,figsize=(12,4.7))
    for ax,df,var,label in [(axs[0],repeat,'optical_mean_delta_v','Optical mean (delta V)'),(axs[1],force,'force_mean_N','Force mean (N)')]:
        d=df[df.variable.eq(var)].set_index('taxel').reindex(taxels)
        ax.errorbar(taxels,d['mean'],yerr=d.sd,fmt='o',capsize=4,color=COLORS[0]);ax.set(ylabel=label,title='Recording mean ± sample SD',xlabel='Taxel')
    fig.tight_layout();save(fig,'04_mean_sd')
    fig,ax=plt.subplots(figsize=(10,4.8));x=np.arange(9)
    a=repeat[repeat.variable.eq('optical_mean_delta_v')].set_index('taxel').reindex(taxels)
    b=force[force.variable.eq('force_mean_N')].set_index('taxel').reindex(taxels)
    ax.bar(x-.18,a.cv_percent,.36,color=COLORS[0],label='Optical response');ax.bar(x+.18,b.cv_percent,.36,color=COLORS[1],label='Manual force')
    ax.set(xticks=x,xticklabels=taxels,ylabel='Coefficient of variation (%)',title='Optical and force variability describe different quantities',xlabel='Taxel');ax.legend(frameon=False);save(fig,'05_cv_comparison')
    fig,ax=plt.subplots(figsize=(8,6))
    for i,t in enumerate(taxels):
        d=trials[trials.taxel.eq(t)];ax.scatter(d.force_mean_N,d.optical_mean_delta_v,label=t,c=COLORS[i],s=45,edgecolor='white',linewidth=.6)
    ax.set(xlabel='Mean contact force per recording (N)',ylabel='Mean target optical response (delta V)',title='Force and optical response at recording level');ax.legend(ncol=3,frameon=False);save(fig,'06_force_optical')
    fig,axs=plt.subplots(3,3,figsize=(12,10),sharex=True,sharey=True)
    for i,(ax,t) in enumerate(zip(axs.flat,taxels)):
        d=trials[trials.taxel.eq(t)];ax.scatter(d.force_mean_N,d.optical_mean_delta_v,c=COLORS[0]);ax.set_title(t)
    fig.supxlabel('Mean contact force (N)');fig.supylabel('Mean target optical response (delta V)');fig.suptitle('Within-taxel force–optical relationships · eight recordings each');fig.tight_layout();save(fig,'07_force_optical_by_taxel')
    base=base.copy();base['time']=pd.to_datetime(base.capture_utc).dt.tz_convert('Asia/Manila')
    fig,ax=plt.subplots(figsize=(10,5))
    for i,t in enumerate(taxels):
        d=base[base.roi.eq(t)];ax.plot(d.time,d.mean_v,'o-',label=t,c=COLORS[i])
    import matplotlib.dates as md
    ax.xaxis.set_major_formatter(md.DateFormatter('%H:%M',tz=base.time.dt.tz));ax.set(ylabel='Saved baseline mean (OpenCV V)',xlabel='8 August 2026 · Asia/Manila',title='Four unique baseline captures were reused across recordings');ax.legend(ncol=3,frameon=False);save(fig,'08_baseline_captures')
    fig,axs=plt.subplots(1,2,figsize=(12,4.6),sharey=True)
    selected=records[records.no_contact_recording].sort_values('start_utc').iloc[[0,-1]]
    for ax,(_,r) in zip(axs,selected.iterrows()):
        g=frames[frames.recording_id.eq(r.recording_id)]
        for i in range(1,10):ax.plot(g.elapsed_time_s-g.elapsed_time_s.iloc[0],g[f'roi{i}_mean_v'],c=COLORS[i-1],lw=1,label=f'T{i}')
        ax.set(title=r.trial_id,xlabel='Time within recording (s)',ylabel='ROI mean (OpenCV V)')
    axs[1].legend(ncol=3,frameon=False);fig.suptitle('No-contact stability · earliest and latest no-contact recordings');fig.tight_layout();save(fig,'09_no_contact_stability')
    fig,axs=plt.subplots(2,1,figsize=(11,7),sharex=True)
    tr=trials.sort_values('start_utc').copy();tr['order']=np.arange(1,len(tr)+1)
    for ax,v,lab in [(axs[0],'optical_mean_delta_v','Optical mean (delta V)'),(axs[1],'force_mean_N','Force mean (N)')]:
        for t,c in zip(taxels,COLORS):
            d=tr[tr.taxel.eq(t)];ax.scatter(d.order,d[v],label=t,c=c,s=25)
        ax.set(ylabel=lab)
    axs[0].legend(ncol=9,frameon=False,fontsize=9);axs[0].set_title('Acquisition-order differences within session S3');axs[1].set_xlabel('Press-recording order · not independent sessions');fig.tight_layout();save(fig,'10_recording_order')
    fig,ax=plt.subplots(figsize=(10,4.5));ax.hist(frames.synchronization_offset_ms,bins=45,color=COLORS[0]);ax.set(xlabel='Camera timestamp − nearest load-cell host timestamp (ms)',ylabel='Frames',title='Timestamp matching offsets are not sensor response latency');save(fig,'11_timestamp_offsets')
    fig,axs=plt.subplots(3,1,figsize=(11,9))
    # Deterministic, unselected examples: T1 TEST1, T5 TEST4, T9 TEST1_v2.
    examples=['R1_TEST1','R5_TEST4','R9_TEST1_v2']
    for ax,trial in zip(axs,examples):
        g=frames[frames.trial_id.eq(trial)];t=g.elapsed_time_s-g.elapsed_time_s.iloc[0]
        ax.plot(t,g.force_N,c=COLORS[0],label='Force');ay=ax.twinx();ay.plot(t,g.target_optical,c=COLORS[1],label='Optical',alpha=.8)
        ax.set(title=trial,xlabel='Time within recording (s)',ylabel='Force (N)');ay.set_ylabel('Target optical (delta V)',color=COLORS[1]);ay.grid(False)
    fig.suptitle('Paired force and optical signals · native synchronized timestamps');fig.tight_layout();save(fig,'12_signal_overlays')
    fig,ax=plt.subplots(figsize=(11,6));mat=cm.iloc[:,1:].to_numpy();im=ax.imshow(mat/np.maximum(mat.sum(axis=1,keepdims=True),1)*100,cmap='Blues',vmin=0,vmax=100,aspect='auto')
    ax.set(xticks=np.arange(10),xticklabels=['Withheld']+taxels,yticks=np.arange(9),yticklabels=taxels,xlabel='Existing optical prediction',ylabel='Target taxel',title='Localization audit includes withheld predictions')
    ax.grid(False)
    for i in range(9):
        for j in range(10):ax.text(j,i,str(mat[i,j]),ha='center',va='center',color='white' if mat[i,j]/mat[i].sum()>.5 else '#253444',fontsize=9)
    fig.colorbar(im,ax=ax,label='Share of each true class (%)');save(fig,'13_localization_confusion')
    fig,ax=plt.subplots(figsize=(10,4.5));ax.bar(perf.taxel,perf.coverage_percent,color=COLORS[0]);ax.set(ylabel='Predictions supplied / contact frames (%)',title='Localization coverage at the recorded threshold',xlabel='Taxel');save(fig,'14_localization_coverage')
    fig,ax=plt.subplots(figsize=(9,4.5));ax.hist(trials.force_mean_N,bins='auto',color=COLORS[0]);ax.set(xlabel='Mean contact force per recording (N)',ylabel='Recordings',title='Distribution of manual applied force · 72 recordings');save(fig,'15_force_distribution')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--archive',type=Path,default=DEFAULT_ARCHIVE)
    parser.add_argument('--output',type=Path,default=ROOT/'outputs');parser.add_argument('--decode-video',action='store_true')
    args=parser.parse_args();out=args.output;out.mkdir(parents=True,exist_ok=True)
    for d in ['tables','figures','processed']: (out/d).mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(message)s',handlers=[logging.FileHandler(out/'analysis.log',mode='w'),logging.StreamHandler()])
    before=sha256(args.archive);logging.info('Archive hash %s',before)
    with zipfile.ZipFile(args.archive) as z:
        inventory,dictionary=inventory_archive(z,out)
        frames,raw,records,base,profiles,anomalies,cals=load_recordings(z,out,args.decode_video)
    records=records.sort_values('start_utc');trials=records[~records.no_contact_recording].copy()
    for df,name in [(records,'recording_summary'),(profiles,'column_profile_by_recording'),(cals,'calibration_provenance')]:save_table(df,name,out)
    profile=profiles.groupby(['artifact_scope','column']).agg(rows=('rows','sum'),missing=('missing','sum'),dtypes=('dtype',lambda x:';'.join(sorted(set(x))))).reset_index()
    profile['missing_percent']=100*profile.missing/profile.rows
    merged=profile.merge(dictionary.drop_duplicates(['artifact_scope','column_name']),left_on=['artifact_scope','column'],right_on=['artifact_scope','column_name'],how='left')
    save_table(merged,'data_dictionary_with_profile',out)
    # No source fields are overwritten in this compact normalized derivative.
    frame_cols=['recording_id','session_id','trial_id','trial_group','capture_frame_id','archive_row_number','elapsed_time_s','wall_clock_iso',
        'target_roi_ground_truth','no_contact_recording','force_N','contact_state_derived','target_optical','predicted_dominant_roi',
        'synchronization_offset_ms','nearest_sample_gap_ms','core_complete','core_valid']+[f'roi{i}_delta_v_mean' for i in range(1,10)]
    frames[frame_cols].to_csv(out/'processed'/'audited_frames.csv.gz',index=False,compression='gzip')
    dist=trials.groupby('taxel').agg(recordings=('recording_id','nunique'),trials=('trial_id','nunique'),sessions=('session_id','nunique'),frames=('frames','sum'),contact_frames=('contact_frames','sum')).reset_index()
    dist['frame_percent_of_press_records']=100*dist.frames/dist.frames.sum();dist['relative_to_equal_frames']=dist.frames/(dist.frames.sum()/9)
    save_table(dist,'taxel_distribution',out)
    save_table(records.groupby(['taxel','no_contact_recording']).agg(recordings=('recording_id','size'),frames=('frames','sum'),complete=('core_complete','sum')).reset_index(),'completeness_by_taxel',out)
    save_table(records.groupby(['session_id','trial_group']).agg(recordings=('recording_id','size'),frames=('frames','sum'),complete=('core_complete','sum')).reset_index(),'completeness_by_group',out)
    repeat=save_table(group_stats(trials,['taxel'],['optical_mean_delta_v','optical_peak_delta_v']),'optical_repeatability',out)
    force=save_table(group_stats(trials,['taxel'],['force_mean_N','force_peak_N']),'force_variability',out)
    save_table(group_stats(trials,['session_id','taxel'],['optical_mean_delta_v','force_mean_N']),'session_descriptives',out)
    save_table(group_stats(trials,['trial_group'],['optical_mean_delta_v','force_mean_N']),'trial_group_descriptives',out)
    base_unique,nc=assess_baseline(frames,records,base,out)
    corr=assess_correlations(trials,out);timing=assess_timing(frames,records,out)
    anomaly=assess_outliers(trials,anomalies,out);perf,cm,performance=assess_performance(frames,out)
    plot_figures(frames,records,trials,base_unique,nc,repeat,force,perf,cm,out)
    # Figure tables and independent reconciliation assertions are saved with exact numbers.
    for name,df in [('coverage',dist),('recording_points',trials),('baseline_points',base_unique),('performance',perf)]:save_table(df,'figure_data_'+name,out)
    checks={
        'zip_crc_all_valid':bool(inventory.crc_valid.all()),'all_structured_files_parse':bool(inventory.parse_error.eq('').all()),
        'master_counts_equal_accepted':bool(records.frames.eq(records.expected_accepted_frames).all()),
        'feature_counts_equal_master':bool(records.feature_rows.eq(records.frames).all()),
        'loadcell_counts_equal_ingress':bool(records.loadcell_rows.eq(records.expected_loadcell_samples).all()),
        'video_declared_equal_master':bool(records.video_frames_declared.eq(records.frames).all()),
        'primary_videos_decoded_equal_master':bool(records.decoded_video_frames.eq(records.frames).all()) if args.decode_video else None,
        'feature_master_common_fields_agree':bool(records.feature_master_disagreements.eq('').all() & records.feature_frame_alignment.all()),
        'zero_exact_duplicate_feature_rows':bool(records.exact_duplicate_rows.sum()==0),
        'unique_recording_frame_keys':not bool(frames.duplicated(['recording_id','capture_frame_id']).any()),
        'core_complete':bool(frames.core_complete.all()),'core_source_flags_valid':bool(frames.core_valid.all()),
        'strict_frame_clock_order':bool(records.timestamp_nonincreasing.sum()==0),
        'strict_loadcell_clock_order':bool(records.raw_clock_nonincreasing.sum()==0),
        'force_from_mass_consistent':bool(np.allclose(frames.force_N,frames.mass_g*9.80665/1000,rtol=1e-10,atol=1e-10)),
        'contact_from_saved_threshold_consistent':bool(frames.contact_state_derived.eq(frames.force_N>=frames.contact_threshold_N).all()),
        'valid_target_range':bool(frames.target_roi_ground_truth.isin(range(1,10)).all()),
        'optical_delta_range_0_255':bool(frames[[f'roi{i}_delta_v_mean' for i in range(1,10)]].ge(0).all().all() and frames[[f'roi{i}_delta_v_mean' for i in range(1,10)]].le(255).all().all()),
        'class_totals_reconcile':bool(dist.frames.sum()==trials.frames.sum()),
        'confusion_totals_reconcile':bool(cm.iloc[:,1:].to_numpy().sum()==performance['n']),
        'archive_unchanged':sha256(args.archive)==before}
    (out/'validation_checks.json').write_text(json.dumps(checks,indent=2),encoding='utf-8')
    summary=dict(archive=str(args.archive),archive_sha256=before,archive_files=len(inventory),file_types=inventory.extension.value_counts().to_dict(),
        frames=len(frames),raw_loadcell_samples=len(raw),recordings=len(records),press_recordings=len(trials),no_contact_recordings=int(records.no_contact_recording.sum()),
        no_contact_recording_frames=int(records.loc[records.no_contact_recording,'frames'].sum()),session_labels=sorted(frames.session_id.unique()),
        trial_ids=int(frames.trial_id.nunique()),baseline_ids=int(base_unique.baseline_id.nunique()),contact_frames=int(frames.contact_state_derived.sum()),
        no_contact_frames=int((~frames.contact_state_derived).sum()),contact_runs=int(trials.contact_runs.sum()),
        records_multiple_contact_runs=int(trials.contact_runs.gt(1).sum()),contact_at_start=int(trials.contact_at_start.sum()),contact_at_end=int(trials.contact_at_end.sum()),
        core_complete_frames=int(frames.core_complete.sum()),literal_complete_frames=int(records.any_field_complete.sum()),
        mean_force_recording_range=[float(trials.force_mean_N.min()),float(trials.force_mean_N.max())],
        optical_cv_range=[float(repeat.loc[repeat.variable.eq('optical_mean_delta_v'),'cv_percent'].min()),float(repeat.loc[repeat.variable.eq('optical_mean_delta_v'),'cv_percent'].max())],
        force_cv_range=[float(force.loc[force.variable.eq('force_mean_N'),'cv_percent'].min()),float(force.loc[force.variable.eq('force_mean_N'),'cv_percent'].max())],
        timing=timing.to_dict('records'),all_mean_spearman=float(corr.loc[(corr.group.eq('All'))&corr.metric.eq('contact means'),'spearman_rho'].iloc[0]),
        performance=performance,statistical_flags=int(anomaly.reason.str.contains('IQR').sum()),label_conflicts=int(anomaly.variable.eq('trial_interaction_class').sum()),
        effective_fps_range=[float(records.effective_fps.min()),float(records.effective_fps.max())],
        force_reconstruction_error_max=float(records.force_reconstruction_max_error_N.max()),offset_reconstruction_error_max=float(records.sync_offset_reconstruction_max_error_ms.max()),
        date_min=frames.wall_clock_iso.min(),date_max=frames.wall_clock_iso.max(),checks=checks,
        generated_at=datetime.now(timezone.utc).isoformat(),python=platform.python_version())
    (out/'summary.json').write_text(json.dumps(summary,indent=2,default=str),encoding='utf-8')
    logging.info('Finished: %s',json.dumps({k:summary[k] for k in ['frames','recordings','press_recordings','no_contact_recordings','session_labels','statistical_flags','performance']}))
    print(json.dumps(summary,indent=2,default=str))

if __name__=='__main__': main()
