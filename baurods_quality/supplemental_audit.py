"""Independent source reconciliation plus bounded audit of existing split artifacts."""
from pathlib import Path
import os,json,zipfile,hashlib
import numpy as np
import pandas as pd
from scipy.stats import rankdata

ROOT=Path(__file__).resolve().parent;OUT=ROOT/'outputs';TABLES=OUT/'tables';WORK=ROOT.parent
def digest(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def main():
    inventory=[]
    for directory,dirs,files in os.walk(WORK):
        dirs[:]=[d for d in dirs if d not in ['.git','.venv','node_modules','__pycache__','baurods_quality']]
        for name in files:
            p=Path(directory)/name
            inventory.append({'relative_path':str(p.relative_to(WORK)),'extension':p.suffix,'bytes':p.stat().st_size})
    pd.DataFrame(inventory).to_csv(TABLES/'workspace_file_inventory.csv',index=False)
    record=pd.read_csv(TABLES/'recording_summary.csv');frames=pd.read_csv(OUT/'processed'/'audited_frames.csv.gz')
    trials=record[~record.no_contact_recording]
    manifest=WORK/'calibration_gui/analysis_outputs/live_sensor_manual_only/splits/splits-da608c2b8581adff/split_manifest.csv'
    split=pd.read_csv(manifest);joined=split.merge(record,left_on='session_id',right_on='recording_id',validate='one_to_one',suffixes=('_manifest','_source'))
    rows=[]
    for k in sorted(split.outer_fold.dropna().unique()):
        train=joined[joined.outer_fold.notna()&joined.outer_fold.ne(k)];test=joined[joined.outer_fold.eq(k)]
        train_frames=frames[frames.recording_id.isin(train.recording_id)];test_frames=frames[frames.recording_id.isin(test.recording_id)]
        train_keys=set(zip(train_frames.recording_id,train_frames.capture_frame_id));test_keys=set(zip(test_frames.recording_id,test_frames.capture_frame_id))
        rows.append(dict(fold=int(k),train_recordings=len(train),heldout_recordings=len(test),
            recording_overlap=len(set(train.recording_id)&set(test.recording_id)),frame_key_overlap=len(train_keys&test_keys),
            train_taxels=train.taxel.nunique(),heldout_taxels=test.taxel.nunique(),
            baseline_ids_shared=len(set(train.baseline_id)&set(test.baseline_id)),
            experimental_session_labels_shared=len(set(train.session_id_source)&set(test.session_id_source))))
    pd.DataFrame(rows).to_csv(TABLES/'verified_split_audit.csv',index=False)
    roles=joined.groupby('scientific_role').agg(recordings=('recording_id','size'),frames=('frames','sum')).reset_index()
    roles.to_csv(TABLES/'existing_split_roles.csv',index=False)
    old=WORK/'analysis_outputs/manual_linear_models/out_of_fold_predictions.csv.gz'
    legacy={}
    if old.exists():
        d=pd.read_csv(old);legacy={'path':str(old),'sha256':digest(old),'rows':len(d),'recordings':int(d.session.nunique()),
            'recordings_in_multiple_folds':int(d.groupby('session').fold.nunique().gt(1).sum()),
            'recordings_matching_supplied_archive':len(set(d.session)&set(record.recording_id))}
    # Recompute exact count, means, sample SD, quartiles, and rank correlation
    # independently from each original master CSV; do not reuse pipeline reducers.
    z=zipfile.ZipFile(json.loads((OUT/'summary.json').read_text())['archive']);ind=[];negative=0;invalid_raw=0
    for _,r in record.iterrows():
        d=pd.read_csv(z.open(r.archive_prefix+'/master_synchronized.csv'))
        raw=pd.read_csv(z.open(r.archive_prefix+'/loadcell_raw.csv'))
        negative+=int((d.force_N<0).sum());invalid_raw+=int((~raw.loadcell_valid.eq(True)).sum())
        if r.no_contact_recording:continue
        sel=(d.force_N>=d.contact_threshold_N).to_numpy()
        a=d.force_N.to_numpy()[sel];b=d[f'roi{r.taxel[1:]}_delta_v_mean'].to_numpy()[sel]
        ind.append(dict(recording_id=r.recording_id,taxel=r.taxel,force_mean_N=float(np.sum(a)/len(a)),force_peak_N=float(max(a)),
            optical_mean_delta_v=float(np.sum(b)/len(b)),optical_peak_delta_v=float(max(b))))
    check=pd.DataFrame(ind);errors=[]
    for filename,vs in [('force_variability',['force_mean_N','force_peak_N']),('optical_repeatability',['optical_mean_delta_v','optical_peak_delta_v'])]:
        existing=pd.read_csv(TABLES/(filename+'.csv'))
        for taxel,g in check.groupby('taxel'):
            for v in vs:
                a=g[v].to_numpy();e=existing[(existing.taxel==taxel)&(existing.variable==v)].iloc[0]
                mean=np.sum(a)/len(a);sd=np.sqrt(np.sum((a-mean)**2)/(len(a)-1));q=np.quantile(a,[.25,.5,.75])
                expected={'n':len(a),'mean':mean,'sd':sd,'median':q[1],'q1':q[0],'q3':q[2],'iqr':q[2]-q[0],
                    'minimum':min(a),'maximum':max(a),'cv_percent':sd/mean*100}
                for k,val in expected.items():
                    errors.append(abs(e[k]-val))
    rho=np.corrcoef(rankdata(check.force_mean_N),rankdata(check.optical_mean_delta_v))[0,1]
    unique=pd.read_csv(TABLES/'unique_baselines.csv');drift=[]
    for roi,g in unique.sort_values('capture_utc').groupby('roi'):
        start=g.mean_v.iloc[0];end=g.mean_v.iloc[-1]
        drift.append(dict(taxel=roi,first_mean_v=start,last_mean_v=end,absolute_first_last_change_percent=abs(end-start)/abs(start)*100))
    pd.DataFrame(drift).to_csv(TABLES/'baseline_first_last_change.csv',index=False)
    result={'source_split_manifest':str(manifest),'split_manifest_sha256':digest(manifest),'matched_recordings':len(joined),
        'manifest_rows':len(split),'legacy_oof':legacy,'negative_force_frames':negative,'invalid_raw_loadcell_rows':invalid_raw,
        'independently_recomputed_summary_cells':len(errors),'maximum_absolute_statistic_error':float(max(errors)),
        'independent_spearman_rho':float(rho),'all_grouped_summary_cells_agree':bool(max(errors)<1e-10),
        'note':'Manifest session_id means recording-directory identity; source session_id is S3. Shared baseline/session context is a generalization limitation, not proof of label leakage.'}
    (OUT/'supplemental_validation.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result,indent=2))
if __name__=='__main__':main()
