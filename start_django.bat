@echo off
cd /d D:\PaymentVoucher\voucher_system
call D:\PaymentVoucher\venv\Scripts\activate
python manage.py runserver 0.0.0.0:8080
pause
