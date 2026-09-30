import csv
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def font(size, bold=False):
    candidates = [f'C:/Windows/Fonts/{"arialbd" if bold else "arial"}.ttf',
                  f'/usr/share/fonts/truetype/dejavu/DejaVuSans{"-Bold" if bold else ""}.ttf']
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size=size)


def build_report(rows, title, folder, stem):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    csv_path = folder / f'{stem}.csv'
    fields = ['editor', 'editor_id', 'assigned', 'assigned_effort', 'approved',
              'completed_effort', 'due', 'on_time', 'on_time_pct', 'first_pass_pct']
    with csv_path.open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            safe = dict(row)
            if safe['editor'].startswith(('=', '+', '-', '@', '\t', '\r', '\n')):
                safe['editor'] = "'" + safe['editor']
            writer.writerow(safe)
    images = []
    pages = [rows[i:i+12] for i in range(0, len(rows), 12)] or [[]]
    for number, page in enumerate(pages, 1):
        height = 360 + max(len(page), 1) * 88
        im = Image.new('RGB', (1400, height), '#101b2c')
        d = ImageDraw.Draw(im)
        d.rounded_rectangle((36, 30, 1364, 140), radius=18, fill='#192a40')
        d.text((64, 48), 'EDITOR PERFORMANCE', font=font(32, True), fill='#ffffff')
        d.text((64, 94), title + (f'  |  Page {number}/{len(pages)}' if len(pages) > 1 else ''), font=font(20), fill='#b3c6dc')
        headers = [(60, 'EDITOR'), (350, 'ASSIGNED EFFORT'), (655, 'APPROVED EFFORT'), (980, 'ON-TIME DELIVERY')]
        for x, text in headers:
            d.text((x, 174), text, font=font(17, True), fill='#98aec9')
        maximum = max([max(r['assigned_effort'], r['completed_effort']) for r in rows] + [1])
        for idx, row in enumerate(page):
            y = 216 + idx * 88
            if idx % 2 == 0:
                d.rounded_rectangle((40, y - 9, 1360, y + 69), radius=10, fill='#16253a')
            name = row['editor']
            while d.textlength(name, font=font(22, True)) > 260:
                name = name[:-2] + '…'
            d.text((60, y), name, font=font(22, True), fill='white')
            d.text((60, y + 31), f'{row["approved"]} approved / {row["assigned"]} assigned', font=font(15), fill='#9fb3cb')
            for x, value, color in [(350, row['assigned_effort'], '#66a8ff'), (655, row['completed_effort'], '#47d6b0')]:
                d.rounded_rectangle((x, y + 11, x + 206, y + 29), radius=8, fill='#293c55')
                if value:
                    d.rounded_rectangle((x, y + 11, x + max(8, 206 * value / maximum), y + 29), radius=8, fill=color)
                d.text((x + 220, y + 2), str(value), font=font(23, True), fill='white')
            pct = row['on_time_pct']
            d.text((980, y - 2), '—' if pct is None else f'{pct:g}%', font=font(25, True), fill='#f4c779')
            d.text((1080, y + 3), f'{row["on_time"]}/{row["due"]} due', font=font(19), fill='#c4d1e3')
            quality = row['first_pass_pct']
            d.text((980, y + 32), 'First-pass: ' + ('N/A' if quality is None else f'{quality:g}%'), font=font(16), fill='#9fb3cb')
        if not page:
            d.text((64, 235), 'No editors registered yet.', font=font(23), fill='white')
        d.text((60, height - 100), 'On-time = first submissions by the approved first-delivery deadline / all jobs due that week.', font=font(18), fill='#b3c6dc')
        d.text((60, height - 70), 'Unsubmitted late jobs count. Admin review time does not. Quality is tracked separately.', font=font(18), fill='#b3c6dc')
        d.text((60, height - 40), 'Assignment bars show effort points, not a subjective editor rating.', font=font(16), fill='#7f98b7')
        path = folder / f'{stem}-{number}.png'
        im.save(path)
        images.append(path)
    return images, csv_path


def recognition(rows):
    eligible = [r for r in rows if r['due'] >= 3]
    if not eligible:
        reliability = 'Reliability award: not enough data yet (minimum 3 jobs due).'
    else:
        best = max(r['on_time_pct'] for r in eligible)
        names = ', '.join(r['editor'] for r in eligible if r['on_time_pct'] == best)
        reliability = f'Most reliable: {names} ({best:g}% on time).'
    completed = [r for r in rows if r['completed_effort'] > 0]
    output = ''
    if completed:
        best = max(r['completed_effort'] for r in completed)
        output = '\nHighest completed effort: ' + ', '.join(r['editor'] for r in completed if r['completed_effort'] == best) + f' ({best} points).'
    quality = [r for r in rows if r['approved'] >= 3]
    if quality:
        best = max(r['first_pass_pct'] for r in quality)
        output += '\nBest first-pass quality: ' + ', '.join(r['editor'] for r in quality if r['first_pass_pct'] == best) + f' ({best:g}%).'
    return reliability + output
