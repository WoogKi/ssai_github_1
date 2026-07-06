# app/tools/which_is_loaded.py

VERSION = "chat_middleware/2025-11-01T-v1"

import importlib, inspect, sys, hashlib, pathlib, json

TARGETS = [
    "app.ui.chat_middleware",
    "app.ui.chat_bridge",
    "app.sims.views.users",
    "app.sims.views.codes",
    "app.ui.sims_panel",
    "app.sims.router",
    "app.db.mssql_client",
]

# 참고: sys.modules는 이미 로드된 모듈만 보여준다. importlib.import_module은 모듈이 로드되어 있지 않으면 새로 로드한다.
# 따라서, TARGETS에 있는 모듈이 실제로 로드되어 있는지 확인하려면 sys.modules를 먼저 확인하는 것이 좋다.
# 하지만, 여기서는 importlib.import_module로 시도해서 로드된 모듈의 정보를 가져오는 방식을 사용한다. 
# 로드되지 않은 모듈은 예외가 발생할 것이고, 그 경우 "error" 필드에 예외 메시지를 담아서 반환한다.   

def sha1_of(path: pathlib.Path) -> str:
    try:
        data = path.read_bytes()
        return hashlib.sha1(data).hexdigest()
    except Exception:
        return "N/A"

# 모듈 정보를 가져오는 함수: 모듈 이름, 파일 경로, SHA1 해시, 파일의 처음 8줄을 반환한다.
# 만약 모듈이 로드되지 않았거나 정보 조회 중에 예외가 발생하면, "error" 필드에 예외 메시지를 담아서 반환한다.
# 참고: 모듈이 로드되어 있지 않으면 importlib.import_module이 ImportError를 발생시킬 것이다.
def info(modname: str):
    try:
        m = importlib.import_module(modname)
        file = pathlib.Path(inspect.getfile(m))
        sha1 = sha1_of(file)
        head = "\n".join(file.read_text(encoding="utf-8", errors="ignore").splitlines()[:8])
        return {"module": modname, "file": str(file), "sha1": sha1, "head": head}
    except Exception as e:
        return {"module": modname, "error": repr(e)}

# main 함수: sys.path와 TARGETS에 있는 모듈들의 정보를 출력한다.
# sys.path는 파이썬이 모듈을 찾을 때 참조하는 디렉토리 목록이다. TARGETS에 있는 각 모듈에 대해서 info 함수를 호출해서 정보를 가져오고, JSON 형식으로 예쁘게 출력한다.
# 만약 모듈이 로드되지 않았거나 정보 조회 중에 예외가 발생하면, "error" 필드에 예외 메시지가 담긴 결과가 출력될 것이다.
def main():
    print("=== sys.path ===")
    for p in sys.path:
        print(" -", p)
    print("\n=== loaded targets ===")
    for t in TARGETS:
        print(json.dumps(info(t), ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
