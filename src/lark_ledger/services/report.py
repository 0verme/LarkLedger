import io
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from lark_ledger.schemas import AdviceResult, CategoryTotal, ReportData

WIDTH = 1200
HEIGHT = 1600
BACKGROUND = "#F4F7FB"
INK = "#172033"
MUTED = "#64748B"
BLUE = "#3978F6"
GREEN = "#20A779"
RED = "#EF6A6A"
PALETTE = ("#3978F6", "#24B5A5", "#F5A44A", "#8B6CF6", "#EF6A6A", "#56A3E8")


def fallback_advice(report: ReportData) -> AdviceResult:
    items: list[str] = []
    if report.expense_total == 0:
        items.append("本期没有支出记录，可以继续保持并及时记录收入。")
    elif report.categories:
        top = report.categories[0]
        share = top.amount / report.expense_total * 100
        items.append(f"{top.category}占支出约 {share:.0f}%，可优先检查这一类的可优化空间。")
    if report.balance < 0:
        items.append("本期支出高于收入，建议为下一周期设置分类预算上限。")
    else:
        items.append("本期收支保持结余，可将一部分结余预留为应急资金。")
    if report.entry_count > 0:
        items.append("持续完整记账，结合连续多个周期的趋势再调整消费计划。")
    return AdviceResult(items=items[:3])


def build_report_card(
    report: ReportData | None,
    message: str,
    *,
    advice: AdviceResult | None = None,
    image_key: str | None = None,
) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    if report is None:
        elements.append({"tag": "markdown", "content": message})
        title = "消费报告"
        subtitle = "暂未生成"
    else:
        title = "消费报告"
        subtitle = _range_text(report)
        elements.append({"tag": "markdown", "content": message})
        if image_key:
            elements.append(
                {
                    "tag": "img",
                    "img_key": image_key,
                    "alt": {
                        "tag": "plain_text",
                        "content": f"{subtitle}消费报告图表，{message}",
                    },
                    "scale_type": "fit_horizontal",
                    "preview": True,
                }
            )
        elif advice is not None:
            categories = "、".join(
                f"{item.category} ¥{item.amount:.2f}" for item in report.categories[:5]
            ) or "暂无支出分类"
            suggestions = "\n".join(
                f"{index}. {item}" for index, item in enumerate(advice.items, 1)
            )
            elements.append(
                {
                    "tag": "markdown",
                    "content": f"**主要分类**\n{categories}\n\n**消费建议**\n{suggestions}",
                }
            )
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "subtitle": {"tag": "plain_text", "content": subtitle},
            "template": "blue",
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 12px 12px",
            "elements": elements,
        },
    }


class ReportRenderer:
    def __init__(self, font_path: str | None = None) -> None:
        self.font_path = self._find_font(font_path)

    def render(self, report: ReportData, advice: AdviceResult) -> bytes:
        image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
        draw = ImageDraw.Draw(image)
        title_font = self._font(46, bold=True)
        h2_font = self._font(28, bold=True)
        body_font = self._font(23)
        small_font = self._font(19)
        amount_font = self._font(34, bold=True)

        draw.text((64, 50), "消费报告", font=title_font, fill=INK)
        draw.text((64, 112), _range_text(report), font=body_font, fill=MUTED)

        cards = (
            ("收入", report.income_total, GREEN),
            ("支出", report.expense_total, RED),
            ("结余", report.balance, BLUE if report.balance >= 0 else RED),
        )
        for index, (label, amount, color) in enumerate(cards):
            left = 64 + index * 365
            self._rounded(draw, (left, 170, left + 337, 300))
            draw.text((left + 24, 192), label, font=small_font, fill=MUTED)
            draw.text(
                (left + 24, 230),
                f"¥{amount:,.2f}",
                font=amount_font,
                fill=color,
            )

        self._rounded(draw, (64, 330, 1136, 770))
        draw.text((92, 356), "支出分类占比", font=h2_font, fill=INK)
        self._draw_categories(draw, report, body_font, small_font)

        self._rounded(draw, (64, 800, 1136, 1130))
        trend_title = "每日支出趋势" if report.trend_granularity == "day" else "每月支出趋势"
        draw.text((92, 826), trend_title, font=h2_font, fill=INK)
        self._draw_trend(draw, report, small_font)

        self._rounded(draw, (64, 1160, 550, 1515))
        draw.text((92, 1186), "收支对比", font=h2_font, fill=INK)
        self._draw_comparison(draw, report, small_font)

        self._rounded(draw, (580, 1160, 1136, 1515))
        draw.text((608, 1186), "AI 消费建议", font=h2_font, fill=INK)
        y = 1240
        for index, item in enumerate(advice.items, 1):
            lines = self._wrap(draw, f"{index}. {item}", body_font, 480)
            draw.multiline_text((608, y), "\n".join(lines), font=body_font, fill=INK, spacing=8)
            y += len(lines) * 35 + 20

        draw.text(
            (64, 1550),
            f"共 {report.entry_count} 笔记录 · 金额单位 {report.currency}",
            font=small_font,
            fill=MUTED,
        )
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        return output.getvalue()

    def _draw_categories(
        self,
        draw: ImageDraw.ImageDraw,
        report: ReportData,
        body_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        small_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    ) -> None:
        if not report.categories or report.expense_total == 0:
            draw.text((92, 470), "本期暂无支出数据", font=body_font, fill=MUTED)
            return
        box = (112, 420, 422, 730)
        start = -90.0
        visible = report.categories[:6]
        if len(report.categories) > 6:
            visible = report.categories[:5] + [
                CategoryTotal(
                    category="其他",
                    amount=sum(
                        (item.amount for item in report.categories[5:]), Decimal("0")
                    ),
                )
            ]
        visible_total = sum((item.amount for item in visible), Decimal("0"))
        for index, item in enumerate(visible):
            end = start + float(item.amount / visible_total * 360)
            draw.pieslice(box, start=start, end=end, fill=PALETTE[index % len(PALETTE)])
            start = end
        draw.ellipse((190, 498, 344, 652), fill="white")
        total_text = f"¥{report.expense_total:,.0f}"
        draw.text((267, 558), total_text, font=body_font, fill=INK, anchor="mm")

        y = 420
        for index, item in enumerate(visible):
            share = item.amount / report.expense_total * 100
            draw.rounded_rectangle(
                (475, y + 7, 493, y + 25),
                radius=4,
                fill=PALETTE[index % len(PALETTE)],
            )
            label = self._ellipsize(draw, item.category, body_font, 285)
            draw.text((510, y), label, font=body_font, fill=INK)
            draw.text(
                (1085, y),
                f"¥{item.amount:,.2f}  {share:.0f}%",
                font=small_font,
                fill=MUTED,
                anchor="ra",
            )
            y += 48

    def _draw_trend(
        self,
        draw: ImageDraw.ImageDraw,
        report: ReportData,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    ) -> None:
        if not report.trend:
            draw.text((92, 950), "本期暂无支出趋势", font=font, fill=MUTED)
            return
        left, top, right, bottom = 120, 900, 1095, 1070
        draw.line((left, bottom, right, bottom), fill="#D7DEEA", width=2)
        values = [float(point.amount) for point in report.trend]
        maximum = max(values) or 1.0
        count = len(values)
        points: list[tuple[float, float]] = []
        for index, value in enumerate(values):
            x = left + (right - left) * (index / max(count - 1, 1))
            y = bottom - (bottom - top) * value / maximum
            points.append((x, y))
        if len(points) == 1:
            x, y = points[0]
            draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=BLUE)
        else:
            draw.line(points, fill=BLUE, width=5, joint="curve")
        label_indexes = sorted({0, count // 2, count - 1})
        for index in label_indexes:
            point = report.trend[index]
            label = point.period.strftime("%m-%d" if report.trend_granularity == "day" else "%Y-%m")
            x = points[index][0]
            draw.text((x, bottom + 16), label, font=font, fill=MUTED, anchor="ma")
        draw.text((right, top - 28), f"峰值 ¥{maximum:,.2f}", font=font, fill=MUTED, anchor="ra")

    def _draw_comparison(
        self,
        draw: ImageDraw.ImageDraw,
        report: ReportData,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    ) -> None:
        maximum = max(report.income_total, report.expense_total, Decimal("1"))
        for index, (label, value, color) in enumerate(
            (("收入", report.income_total, GREEN), ("支出", report.expense_total, RED))
        ):
            y = 1280 + index * 90
            draw.text((92, y), label, font=font, fill=MUTED)
            draw.rounded_rectangle((160, y, 500, y + 30), radius=15, fill="#E7ECF4")
            width = int(340 * float(value / maximum))
            if width:
                draw.rounded_rectangle((160, y, 160 + width, y + 30), radius=15, fill=color)
            draw.text((500, y + 42), f"¥{value:,.2f}", font=font, fill=INK, anchor="ra")

    def _font(
        self, size: int, *, bold: bool = False
    ) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        if self.font_path:
            return ImageFont.truetype(self.font_path, size=size, index=0)
        font_name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
        return ImageFont.truetype(font_name, size=size)

    @staticmethod
    def _find_font(configured: str | None) -> str | None:
        if configured:
            path = Path(configured)
            if not path.is_file():
                raise ValueError(f"报告字体文件不存在：{configured}")
            return str(path)
        candidates = (
            Path("C:/Windows/Fonts/msyh.ttc"),
            Path("C:/Windows/Fonts/simhei.ttf"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/System/Library/Fonts/PingFang.ttc"),
        )
        return next((str(path) for path in candidates if path.is_file()), None)

    @staticmethod
    def _rounded(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int]) -> None:
        draw.rounded_rectangle(box, radius=28, fill="white")

    @staticmethod
    def _wrap(
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        max_width: int,
    ) -> list[str]:
        lines: list[str] = []
        current = ""
        for character in text:
            candidate = current + character
            if current and draw.textlength(candidate, font=font) > max_width:
                lines.append(current)
                current = character
            else:
                current = candidate
        if current:
            lines.append(current)
        return lines

    @staticmethod
    def _ellipsize(
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        max_width: int,
    ) -> str:
        if draw.textlength(text, font=font) <= max_width:
            return text
        shortened = text
        while shortened and draw.textlength(shortened + "…", font=font) > max_width:
            shortened = shortened[:-1]
        return shortened + "…"


def _range_text(report: ReportData) -> str:
    start = report.range_start.date().isoformat()
    end = (report.range_end - timedelta(microseconds=1)).date().isoformat()
    return f"{start} 至 {end}"
