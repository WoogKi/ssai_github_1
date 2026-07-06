# app/services/erp_dashboard.py
import pandas as pd
from app.services import erp_queries as q

def get_monthly_sales_df(start: str, end: str) -> pd.DataFrame:
    return q.monthly_sales_summary(start, end)

def get_rankings(start: str, end: str, top_n: int = 10):
    return (
        q.customer_sales_ranking(top_n, start, end),
        q.product_sales_ranking(top_n, start, end),
    )

def get_inout_and_turnover(start: str, end: str):
    df_io = q.inout_by_date_range(start, end)
    # turnover 로직은 schema/데이터 여건에 따라 추가 구현 가능
    return df_io
