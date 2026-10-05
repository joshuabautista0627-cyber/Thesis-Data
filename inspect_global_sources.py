from pathlib import Path
import zipfile, json, io, hashlib
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'outputs' / 'global_characterization' / 'audit'
OUT.mkdir(parents=True, exist_ok=True)
ZIP = Path(r'C:\Users\DLSU\Downloads\THESIS DATA\Data Collection\Manual Calibration (2).zip')
z = zipfile.ZipFile(ZIP)
rows=[]; frames={}
for name in z.namelist():
    if not name.endswith('/session_config.json'): continue
    pre=name.rsplit('/',1)[0]; rid=pre.rsplit('/',1)[-1]
    cfg=json.loads(z.read(name)); base=json.loads(z.read(pre+'/baseline_summary.json'))
    d=pd.read_csv(z.open(pre+'/master_synchronized.csv')); frames[rid]=d
    a=np.load(io.BytesIO(z.read(pre+'/baseline_data.npz')),allow_pickle=False)
    labels=cfg['trial_labels']
    row=dict(recording=rid,**labels,rows=len(d),baseline=base['baseline_id'],baseline_valid=base['valid'],
        baseline_time=base['capture_timestamp_iso'],baseline_frames=base['rois'][0]['valid_frame_count'],
        force_min=d.force_N.min(),force_max=d.force_N.max(),force_start=d.force_N.iloc[0],force_end=d.force_N.iloc[-1],
        p10_force=d.force_N.quantile(.1),duration_s=np.ptp(d.elapsed_time_s),median_dt_s=d.elapsed_time_s.diff().median(),
        dt_max_s=d.elapsed_time_s.diff().max(),source=cfg.get('quantitative_analysis_source'),
        magnified=cfg.get('magnified_pixels_used_for_exported_measurements'),threshold=cfg.get('localization_min_mean_delta_v'),
        predicted=int(d.predicted_dominant_roi.notna().sum()),contact_runs=int((d.contact_state_derived & ~d.contact_state_derived.shift(fill_value=False)).sum()),
        invalid_force=int((~d.loadcell_sample_valid).sum()) if 'loadcell_sample_valid' in d else None,
        sync_invalid=int((~d.synchronization_valid).sum()),invalid_frame=int((~d.frame_valid).sum()),
        roi_layout=base['roi_layout_id'],calibration_id=d.calibration_id.iloc[0] if 'calibration_id' in d else None)
    rows.append(row)
    if 'R1_TEST2' in rid:
        (OUT/'example_config.json').write_text(json.dumps(cfg,indent=2))
        (OUT/'example_columns.json').write_text(json.dumps(d.columns.tolist(),indent=2))
        print('NPZ', [(k,a[k].shape,str(a[k].dtype)) for k in a.files])
        print('config keys',list(cfg)); print('baseline keys',list(base))
        print('numeric selected',d[['elapsed_time_s','force_N','dominant_intensity','total_corrected_intensity']].describe().to_string())
        print('dictionary',pd.read_csv(z.open(pre+'/data_dictionary.csv')).query("column_name in ['force_N','contact_state_derived','loadcell_sample_valid','synchronization_valid']").to_dict('records'))
df=pd.DataFrame(rows).sort_values('recording'); df.to_csv(OUT/'recording_inventory.csv',index=False)
print('COUNTS',len(df),df.rows.sum(),df.trial_interaction_class.value_counts().to_dict(),df.baseline.value_counts().to_dict())
print('FORCE RANGE',df.force_min.min(),df.force_max.max(),'SOURCE',df.source.value_counts().to_dict())
print(df[['recording','rows','force_start','force_end','force_max','contact_runs','predicted']].to_string(index=False))
press=df[df.trial_interaction_class.eq('Press')]
for page in range(4):
    fig,axs=plt.subplots(6,3,figsize=(15,18),layout='constrained')
    for ax,(_,r) in zip(axs.flat,press.iloc[page*18:(page+1)*18].iterrows()):
        d=frames[r.recording];ax.plot(d.elapsed_time_s,d.force_N,lw=.7);ax.axhline(.05,color='grey',lw=.5)
        ax.set_title(r.trial_id,fontsize=9); ax.set_xlabel('Elapsed s');ax.set_ylabel('Force N')
    fig.savefig(OUT/f'force_recordings_{page+1}.png',dpi=110);plt.close(fig)

store=ROOT/'calibration_gui/analysis_outputs/live_sensor_manual_only/feature_store/feature-store-fa2500440adab6ca'
paths=list((store/'sessions/archive_id=manual').glob('*.parquet'))
if paths:
    d=pd.read_parquet(paths[0]);print('PARQUET',len(paths),d.shape,d.columns.tolist())
    print(d.iloc[:2,:45].to_dict('records'))
