# 임시 진단: app/services/diag.py
from app.db.mssql_client import read_df, get_connection

def smoke_top():
    # TOP만 테스트
    return read_df("SELECT TOP 1 1 AS x", ())

def smoke_offset():
    # OFFSET/FETCH 테스트 (지원 안하면 예외 발생)
    sql = "SELECT 1 AS x ORDER BY x OFFSET 0 ROWS FETCH NEXT 1 ROWS ONLY"
    return read_df(sql, ())

if __name__ == "__main__":
    print("=== DIAG: TOP ===")
    try:
        df = smoke_top()
        print(df)
        print("OK: TOP 동작")
    except Exception as e:
        print("FAIL: TOP 에러:", e)

    print("\n=== DIAG: OFFSET/FETCH ===")
    try:
        df = smoke_offset()
        print(df)
        print("OK: OFFSET/FETCH 동작")
    except Exception as e:
        print("FAIL: OFFSET/FETCH 에러:", e)
