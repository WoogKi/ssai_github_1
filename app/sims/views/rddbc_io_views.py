# app/sims/views/rddbc_io_views.py

from __future__ import annotations

from app.sims.views.rddbc_io_goods_views import view_rddbc040
from app.sims.views.rddbc_io_inout_views import (
    view_rddbc110,
    view_rddbc120,
)
from app.sims.views.rddbc_io_doc_views import (
    view_rddbc130,
    view_rddbc140,
)
from app.sims.views.rddbc_io_stock_views import (
    view_rddbc210,
    view_rddbc220,
)
from app.sims.views.rddbc_io_check_views import (
    view_rddbc110_trans_check,
    view_rddbc110_tax_check,
    view_rddbc120_trans_check,
    view_rddbc120_tax_check,
)
from app.sims.views.rddbc_io_flow_views import (
    view_product_flow,
)
from app.sims.views.rddbc_io_inventory_views import (
    view_product_inventory,
)

__all__ = [
    "view_rddbc040",
    "view_rddbc110",
    "view_rddbc120",
    "view_rddbc130",
    "view_rddbc140",
    "view_rddbc210",
    "view_rddbc220",
    "view_rddbc110_trans_check",
    "view_rddbc110_tax_check",
    "view_rddbc120_trans_check",
    "view_rddbc120_tax_check",
    "view_product_flow",
    "view_product_inventory",
]