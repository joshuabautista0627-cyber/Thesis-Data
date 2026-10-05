from pathlib import Path
import zipfile, json, io, collections
import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parent
ROOT.mkdir(exist_ok=True)
z = zipfile.ZipFile(r'C:\Users\DLSU\Downloads\THESIS DATA\Data Collection\Manual Calibration (2).zip')
out=[]
for n in z.namelist():
    if not n.endswith('/session_config.json'): continue
    pre=n.rsplit('/',1)[0]
    c=json.loads(z.read(n)); s=json.loads(z.read(pre+'/session_status.json'))
    b=json.loads(z.read(pre+'/baseline_summary.json'))
    m=pd.read_csv(z.open(pre+'/master_synchronized.csv'))
    lab=c['trial_labels']
    out.append(dict(recording=pre.rsplit('/',1)[-1],**lab,n=len(m),force_max=m.force_N.max(),
        contact=int(m.contact_state_derived.eq(True).sum()),predictions=int(m.predicted_dominant_roi.notna().sum()),
        source=c.get('quantitative_analysis_source'),magnified=c.get('magnified_pixels_used_for_exported_measurements'),
        baseline=b['baseline_id'],frame_cols=len(m.columns),sync_invalid=int((~m.synchronization_valid).sum()),
        contiguous_contact_runs=int((m.contact_state_derived.eq(True)&~m.contact_state_derived.shift(fill_value=False).eq(True)).sum()),
        complete=s['complete'],max_gap=m.nearest_sample_gap_ms.max(),elapsed=m.elapsed_time_s.max()-m.elapsed_time_s.min()))
df=pd.DataFrame(out);df.to_csv(ROOT/'inspection.csv',index=False)
print(df.to_string(index=False,columns=['recording','session_id','trial_id','trial_interaction_class','target_roi_ground_truth','n','contact','contiguous_contact_runs','source','baseline']))
print('TOTALS',df[['n','contact','predictions','sync_invalid','contiguous_contact_runs']].sum().to_dict())
print('SESSIONS',df.session_id.value_counts().to_dict(),'BASELINES',df.baseline.nunique(),'MODES',df.source.value_counts().to_dict())
npz=next(n for n in z.namelist() if n.endswith('/baseline_data.npz'))
a=np.load(io.BytesIO(z.read(npz)),allow_pickle=False);print('NPZ',[(k,a[k].shape,str(a[k].dtype)) for k in a.files])
di=pd.read_csv(z.open(next(n for n in z.namelist() if n.endswith('/data_dictionary.csv'))))
print(di[di.column_name.isin(['synchronization_offset_ms','nearest_sample_gap_ms','contact_state_derived','predicted_dominant_roi','frame_valid'])].to_string(index=False))
