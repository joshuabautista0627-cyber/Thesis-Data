"""Regenerate presentation and validate all press amplitudes from the ZIP.

This reuses the completed 2,000-replicate model bootstrap; it never substitutes
new data or synthetic rows. The root analysis script is the full reproducer.
"""
from pathlib import Path
import sys,json
HERE=Path(__file__).resolve().parent; ROOT=HERE.parents[1]
sys.path.insert(0,str(ROOT))
import global_sensor_characterization as g
import pandas as pd
OUT=HERE.parent
s=json.loads((HERE/'analysis_results.json').read_text())
rec,inv,h=g.load_recordings(Path(s['archive']),OUT)
e=pd.read_csv(OUT/'tables/global_force_response_data.csv')
tables={p.stem:pd.read_csv(p) for p in (OUT/'tables').glob('*.csv')}
baseline,bs=g.baseline_tables(rec,e)
tables['baseline_statistics']=baseline;g.export(baseline,OUT/'tables','baseline_statistics')
cl,cm,loc=g.localization_tables(e)
s['localization']=loc
g.write_json(HERE/'analysis_results.json',s)
spatial=pd.read_csv(OUT/'spatial_diagnostics/spatial_consistency.csv')
rd=pd.read_csv(OUT/'spatial_diagnostics/recording_diagnostics.csv')
g.make_figures(OUT,e,tables['fitted_global_curve'],tables['fitted_model_comparison'],baseline,rec,cm,s['detection'],spatial,rd)
g.make_report(OUT,s,tables,inv,spatial,rec)
g.validate(OUT,e,rec,s,tables)
print('Final validation passed',flush=True)
