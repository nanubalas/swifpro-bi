# SwifPro BI – Complete PO Cycle (Procurement + Logistics)

This build implements a full PO lifecycle with:
- Draft → Submit → (Approval Pending) → Approved → Sent → Shipments/Containers/Timeline → Receive (GRN) → Supplier Invoice (3-way match)

## Run
```bash
pip install -r requirements.txt
python manage.py makemigrations
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

## SMTP setup (for Send PO)
Set environment variables (PowerShell example):
```powershell
$env:EMAIL_HOST="smtp.gmail.com"
$env:EMAIL_PORT="587"
$env:EMAIL_HOST_USER="your@email"
$env:EMAIL_HOST_PASSWORD="app_password"
$env:EMAIL_USE_TLS="true"
$env:DEFAULT_FROM_EMAIL="your@email"
```

## Workflow
1) Create Tenant + Location + Supplier(email) + Products
2) Create PO (Draft)
3) Submit PO
   - if threshold exceeded: status = Approval Pending
4) Approve PO (Finance)
5) Send PO (emails supplier)
6) Manage Shipments:
   - PO submit creates first shipment automatically
   - Create additional shipments and allocate expected quantities per shipment line
   - Add containers + timeline events
7) Receive goods:
   - Receive against a shipment (shipment lines required)
   - Captures GRN, attachment, lot/serial/expiry
8) Create Supplier Invoice and post to GL (3-way match)

