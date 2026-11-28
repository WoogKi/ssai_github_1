"""Original-grain Rddbc310 query."""
from app.services.erp_table_query_service import normalize_detail_params, read_detail_result
from app.sims.meta.rddbc_payment_meta import detail_spec

SPEC = detail_spec("310", "입출금구분명", direction_sql=(
    "CASE WHEN S.Rd31_Receive_Di_Gcode='0023' THEN "
    "NULLIF(LTRIM(RTRIM(S.Rd31_Receive_Di)), '') END"
))
# Accounting-system export fields are not installed in every ERP schema.
SPEC["optional_output"] = ("Rd31_Move_Seq", "Rd31_Move_Date", "Rd31_IN_SQ")


def normalize_rddbc310_params(params=None, *, today=None):
    return normalize_detail_params(SPEC, params, today=today)


def get_rddbc310_result(params=None, *, expected_scope=None):
    return read_detail_result(SPEC, normalize_rddbc310_params(params), expected_scope=expected_scope)


def get_rddbc310_export_df(params=None):
    return read_detail_result(SPEC, normalize_rddbc310_params(params), export=True)
