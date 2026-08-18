from pathlib import Path
import sys

from PIL import Image, ImageDraw


ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent / "rendered_docx"
PAGES = sorted(ROOT.glob("page-*.png"))
THUMBNAIL = (306, 396)
COLS = 4
ROWS = 2
MARGIN = 28
LABEL = 30

for sheet_number, start in enumerate(range(0, len(PAGES), COLS * ROWS), 1):
    batch = PAGES[start : start + COLS * ROWS]
    sheet = Image.new(
        "RGB",
        (
            MARGIN * 2 + COLS * THUMBNAIL[0],
            MARGIN * 2 + ROWS * (THUMBNAIL[1] + LABEL),
        ),
        "#D8DDE5",
    )
    draw = ImageDraw.Draw(sheet)
    for index, page_path in enumerate(batch):
        page = Image.open(page_path).convert("RGB")
        page.thumbnail(THUMBNAIL, Image.Resampling.LANCZOS)
        col, row = index % COLS, index // COLS
        x = MARGIN + col * THUMBNAIL[0]
        y = MARGIN + row * (THUMBNAIL[1] + LABEL)
        sheet.paste(page, (x, y))
        draw.text((x + 4, y + THUMBNAIL[1] + 4), page_path.stem, fill="#182230")
    sheet.save(ROOT / f"contact-sheet-{sheet_number:02d}.png", optimize=True)

print(f"Created {sheet_number} contact sheets for {len(PAGES)} pages")
