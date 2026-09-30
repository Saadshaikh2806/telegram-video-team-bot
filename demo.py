"""Generate a sample chart locally; never contacts Telegram."""
from video_bot.reports import build_report


rows = [
    dict(editor='Aisha', editor_id=1, assigned=8, assigned_effort=16, approved=8, completed_effort=16, due=8, on_time=8, on_time_pct=100, first_pass_pct=100),
    dict(editor='Rahul', editor_id=2, assigned=9, assigned_effort=17, approved=8, completed_effort=15, due=9, on_time=8, on_time_pct=88.9, first_pass_pct=87.5),
    dict(editor='Sara', editor_id=3, assigned=7, assigned_effort=16, approved=6, completed_effort=14, due=7, on_time=6, on_time_pct=85.7, first_pass_pct=83.3),
    dict(editor='Imran', editor_id=4, assigned=8, assigned_effort=16, approved=8, completed_effort=16, due=8, on_time=8, on_time_pct=100, first_pass_pct=87.5),
]
if __name__ == '__main__':
    images, csv = build_report(rows, 'SAMPLE DATA | 21–27 Sep 2026 | Asia/Kolkata', 'reports', 'sample-performance')
    print('Sample chart:', images[0].resolve())
    print('Sample spreadsheet:', csv.resolve())
