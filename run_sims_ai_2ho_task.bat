@echo off
cd /d C:\New\Python_Project\LmStudion_project1

if not exist logs mkdir logs

"C:\New\Python_Project\LmStudion_project1\.venv\Scripts\python.exe" -m streamlit run app\Lmstudio_SSAI_chat_main.py --server.address 0.0.0.0 --server.port 8501 --server.headless true --server.enableCORS false --server.enableXsrfProtection false --server.enableWebsocketCompression false >> logs\streamlit_server_2ho.log 2>&1
