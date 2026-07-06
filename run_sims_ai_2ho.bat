@echo off
cd /d C:\New\Python_Project\LmStudion_project1
call .venv\Scripts\activate.bat
python -m streamlit run app\Lmstudio_SSAI_chat_main.py --server.address 0.0.0.0 --server.port 8501
pause
