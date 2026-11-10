@echo off
cd /d "%~dp0"
echo Installing UI dependencies...
python -m pip install -r requirements-ui.txt
echo.
echo Starting ph-crypto-agent UI...
echo Open http://localhost:8501 in your browser
streamlit run streamlit_app.py
