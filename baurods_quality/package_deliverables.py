"""Package the reviewed outputs without copying the original acquisition archive."""
from pathlib import Path
import csv
import hashlib
import json
import shutil
import zipfile

ROOT = Path(__file__).resolve().parent
portable = ROOT / 'BAURods_Data_Quality_Report.html'
shutil.copy2(ROOT / 'report_app/.data-app-offline/exports/BAURods_Data_Quality_Report.html', portable)

checks = json.loads((ROOT / 'outputs/validation_checks.json').read_text())
supplemental = json.loads((ROOT / 'outputs/supplemental_validation.json').read_text())
assert len(checks) == 21 and all(checks.values())
assert supplemental['all_grouped_summary_cells_agree']
assert len(list((ROOT / 'outputs/figures').glob('*.png'))) == 15
assert len(list((ROOT / 'outputs/figures').glob('*.svg'))) == 15
assert len(list((ROOT / 'outputs/tables').glob('*.csv'))) == 35

receipt = {
    'assessment_date': '2026-10-05',
    'original_archive_sha256': 'e039dcd49f3390d34dc979490dcf46a6ecc43d9e6a2aec08e4952acc14b5006a',
    'original_archive_included': False,
    'primary_checks_passed': 21,
    'independently_verified_summary_cells': supplemental['independently_recomputed_summary_cells'],
    'maximum_absolute_summary_difference': supplemental['maximum_absolute_statistic_error'],
    'csv_tables': 35,
    'publication_figures': 15,
    'figure_formats': ['320 dpi PNG', 'SVG'],
    'visual_review': {
        'all_15_static_figures_reviewed': True,
        'interactive_charts_rendered_and_reviewed': 6,
        'semantic_report_tables_verified': 4,
        'desktop_and_430px_layout_checked': True,
        'mobile_horizontal_page_overflow': False,
        'source_inspector_and_reviewed_values_verified': True,
        'legend_toggle_and_restore_verified': True,
        'notes': 'Browser charts show actual recording points. Full boxplots and mean-SD plots are in outputs/figures. An optional batch chart-image export timed out; the standalone HTML export and all static figures were produced successfully.'
    },
    'execution_scope': 'Numerical scripts were executed; the companion notebook is unexecuted and calls those scripts. The supplemental workspace audit requires the original workspace files described in README.md.',
    'limitations': 'A single source session and variable manual force do not establish independent-session or fixed-force per-press repeatability. Timestamp agreement does not measure intrinsic sensor latency.'
}
(ROOT / 'outputs/delivery_verification.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')

excluded_parts = {'.data-app-offline', '.mplconfig', '__pycache__', 'node_modules', '.git'}
excluded_names = {'inspect_sources.py', 'inspection.csv', 'reviewed.json', 'run_console.log', 'DELIVERY_MANIFEST.csv'}
files = sorted(p for p in ROOT.rglob('*') if p.is_file()
               and not excluded_parts.intersection(p.relative_to(ROOT).parts)
               and p.name not in excluded_names)
with (ROOT / 'DELIVERY_MANIFEST.csv').open('w', newline='', encoding='utf-8') as stream:
    writer = csv.writer(stream)
    writer.writerow(['relative_path', 'bytes', 'sha256'])
    for path in files:
        writer.writerow([path.relative_to(ROOT).as_posix(), path.stat().st_size,
                         hashlib.sha256(path.read_bytes()).hexdigest()])

destination = ROOT.parent / 'BAURods_Data_Quality_Package.zip'
with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6, strict_timestamps=False) as archive:
    for path in files + [ROOT / 'DELIVERY_MANIFEST.csv']:
        archive.write(path, Path(ROOT.name) / path.relative_to(ROOT))
with zipfile.ZipFile(destination) as archive:
    assert archive.testzip() is None
    members = archive.namelist()
    for name in ['BAURods_Data_Quality_Report.html', 'Data_Quality_Report.md', 'Thesis_Data_Quality_Assessment.md', 'data_quality_analysis.py', 'DELIVERY_MANIFEST.csv']:
        assert f'{ROOT.name}/{name}' in members
print(json.dumps({'package': str(destination), 'files': len(members),
                  'bytes': destination.stat().st_size,
                  'sha256': hashlib.sha256(destination.read_bytes()).hexdigest(),
                  'crc_test': 'passed'}, indent=2))
