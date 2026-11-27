"""Original-grain Rddbc320 query."""
from app.services.erp_table_query_service import normalize_detail_params, read_detail_result
from app.sims.meta.rddbc_payment_meta import detail_spec

SPEC = detail_spec("320", "어음구분명")


def normalize_rddbc320_params(params=None, *, today=None):
    return normalize_detail_params(SPEC, params, today=today)


def get_rddbc320_result(params=None, *, expected_scope=None):
    return read_detail_result(SPEC, normalize_rddbc320_params(params), expected_scope=expected_scope)


def get_rddbc320_export_df(params=None):
    return read_detail_result(SPEC, normalize_rddbc320_params(params), export=True)
