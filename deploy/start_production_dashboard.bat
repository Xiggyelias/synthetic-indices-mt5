@echo off
TITLE QUANTA-SYNTH Production Dashboard [Port 8501]
cd /d %~dp0\..

echo ======================================================================
echo  📊 QUANTA-SYNTH PRODUCTION DASHBOARD SERVICE
echo ======================================================================
echo  Binding on: http://0.0.0.0:8501
echo  Local URL:  http://localhost:8501
echo  Press Ctrl+C to terminate.
echo ======================================================================
echo.

python -m streamlit run dashboard_synthetic.py ^
    --server.port 8501 ^
    --server.address 0.0.0.0 ^
    --server.headless true ^
    --browser.gatherUsageStats false
