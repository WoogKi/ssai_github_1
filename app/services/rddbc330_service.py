"""Original-grain Rddbc330 query."""
from app.services.erp_table_query_service import normalize_detail_params, read_detail_result
from app.sims.meta.rddbc_payment_meta import detail_spec

SPEC = detail_spec("330", "담보종류명")


def normalize_rddbc330_params(params=None, *, today=None):
    return normalize_detail_params(SPEC, params, today=today)


def get_rddbc330_result(params=None, *, expected_scope=None):
    return read_detail_result(SPEC, normalize_rddbc330_params(params), expected_scope=expected_scope)


def get_rddbc330_export_df(params=None):
    return read_detail_result(SPEC, normalize_rddbc330_params(params), export=True)
