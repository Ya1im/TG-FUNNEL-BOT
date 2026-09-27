"""Перегенерировать bot/assets/client-guide.pdf из client-guide.md.

Запуск: python docs/client-guide/generate_pdf.py

Простой построчный разбор — не полноценный markdown, а ровно то, что нужно для
структуры client-guide.md: заголовки (#, ##, ###), пункты списка (- ) и обычные
абзацы. Кириллица требует явно зарегистрированного TTF-шрифта — стандартные
встроенные шрифты reportlab её не умеют.
"""
from __future__ import annotations

import re
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate

FONT_PATH = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
FONT_BOLD_PATH = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
MD_PATH = Path(__file__).with_name("client-guide.md")
PDF_PATH = Path(__file__).resolve().parents[2] / "bot" / "assets" / "client-guide.pdf"

# DejaVu Sans не содержит цветных эмодзи-глифов — без явной зачистки они рисуются
# «квадратиками». В самом markdown эмодзи оставляем (удобнее ориентироваться при
# редактировании), а перед рендером в PDF вырезаем — заголовки разделов и так
# понятны по названию без иконки.
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0000FE0F]+\\s*"
)


def _strip_emoji(text: str) -> str:
    return _EMOJI_RE.sub("", text).strip()


def _register_fonts() -> None:
    pdfmetrics.registerFont(TTFont("DejaVuSans", str(FONT_PATH)))
    pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", str(FONT_BOLD_PATH)))


def _styles() -> dict[str, ParagraphStyle]:
    return {
        "title": ParagraphStyle("title", fontName="DejaVuSans-Bold", fontSize=20, leading=24, spaceAfter=4),
        "meta": ParagraphStyle("meta", fontName="DejaVuSans", fontSize=10, textColor="#666666", spaceAfter=16),
        "h1": ParagraphStyle("h1", fontName="DejaVuSans-Bold", fontSize=15, leading=19, spaceBefore=14, spaceAfter=8),
        "h2": ParagraphStyle("h2", fontName="DejaVuSans-Bold", fontSize=12.5, leading=16, spaceBefore=10, spaceAfter=6),
        "body": ParagraphStyle("body", fontName="DejaVuSans", fontSize=10.5, leading=15, spaceAfter=6),
        "bullet": ParagraphStyle("bullet", fontName="DejaVuSans", fontSize=10.5, leading=15),
    }


def build_story(markdown_text: str, styles: dict[str, ParagraphStyle]) -> list:
    story: list = []
    lines = markdown_text.splitlines()
    pending_bullets: list[str] = []
    expect_meta_line = False  # следующая непустая строка сразу после заголовка документа (# ...)

    def flush_bullets() -> None:
        if pending_bullets:
            story.append(
                ListFlowable(
                    [ListItem(Paragraph(item, styles["bullet"])) for item in pending_bullets],
                    bulletType="bullet",
                    leftIndent=14,
                )
            )
            pending_bullets.clear()

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            flush_bullets()
            continue
        if line.startswith("### "):
            flush_bullets()
            story.append(Paragraph(_strip_emoji(line[4:]), styles["h2"]))
        elif line.startswith("## "):
            flush_bullets()
            story.append(Paragraph(_strip_emoji(line[3:]), styles["h1"]))
        elif line.startswith("# "):
            flush_bullets()
            story.append(Paragraph(_strip_emoji(line[2:]), styles["title"]))
            expect_meta_line = True
        elif line.startswith("- "):
            pending_bullets.append(_strip_emoji(line[2:]))
        elif expect_meta_line:
            flush_bullets()
            story.append(Paragraph(_strip_emoji(line), styles["meta"]))
            expect_meta_line = False
        else:
            flush_bullets()
            story.append(Paragraph(_strip_emoji(line), styles["body"]))
    flush_bullets()
    return story


def main() -> None:
    _register_fonts()
    styles = _styles()
    markdown_text = MD_PATH.read_text(encoding="utf-8")
    story = build_story(markdown_text, styles)
    PDF_PATH.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(
        str(PDF_PATH),
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        title="Инструкция по боту",
    ).build(story)
    print(f"Готово: {PDF_PATH}")


if __name__ == "__main__":
    main()
