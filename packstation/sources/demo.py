"""Made-up orders and waybills for trying the app without any platform credentials."""
from __future__ import annotations

import io
from datetime import datetime, timedelta

from PIL import Image, ImageDraw

from ..labels import load_font
from ..models import Item, Order
from . import Source


class DemoSource(Source):
    name = "demo"

    def list_orders(self) -> list[Order]:
        today = datetime.now().replace(hour=23, minute=59, second=0, microsecond=0)
        return [
            Order("demo", "DEMO10001", [Item("ML-SANDAL-3G", "Sandalwood 3g", 2)],
                  "PROCESSED", today, "SPXMY0000000101A"),
            Order("demo", "DEMO10002", [Item("ML-ISOAMYL-3G", "Isoamyl Acetate 3g", 1),
                                        Item("ML-LINALOOL-5G", "Linalool 5g", 1)],
                  "PROCESSED", today, "SPXMY0000000202B"),
            Order("demo", "DEMO10003", [Item("ML-VANILLIN-10G", "Vanillin 10g", 3)],
                  "AWAITING_COLLECTION", today + timedelta(days=1), "MY24TT0000303C"),
        ]

    def get_waybill(self, order: Order) -> bytes:
        # 100 x 150 mm at 203 dpi, roughly laid out like a courier waybill.
        im = Image.new("L", (800, 1200), 255)
        d = ImageDraw.Draw(im)
        big, mid = load_font(None, 64, bold=True), load_font(None, 34)
        d.rectangle((10, 10, 790, 1190), outline=0, width=4)
        d.text((40, 40), "DEMO WAYBILL", font=big, fill=0)
        d.text((40, 140), f"Tracking  {order.tracking}", font=mid, fill=0)
        d.text((40, 200), f"Order     {order.order_id}", font=mid, fill=0)
        d.text((40, 280), "Recipient: Test Customer", font=mid, fill=0)
        d.text((40, 330), "1 Jalan Contoh, 50000 Kuala Lumpur", font=mid, fill=0)
        d.text((40, 1000), order.tracking[-4:], font=load_font(None, 150, bold=True), fill=0)
        buf = io.BytesIO()
        im.save(buf, "PDF", resolution=203)
        return buf.getvalue()
