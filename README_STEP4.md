# SwifPro BI Step 4 - Transfers + PO approvals + GRN + Landed Cost + 3-way match

## What's new
- Location Transfers (lot/serial/expiry aware)
- Threshold-based PO approvals (Tenant setting)
- GRN number + attachment on receiving
- Landed cost (single charge MVP) stored against GRN
- Supplier currency (per supplier)
- Supplier invoices + basic 3-way match (PO ↔ GRN ↔ Invoice)

## IMPORTANT: Migrations required
Because this ZIP was generated outside your local venv, migrations are not included.
Run these in your local environment:

```bash
pip install -r requirements.txt
python manage.py makemigrations
python manage.py migrate
```

## First-time setup
1) Create superuser:
```bash
python manage.py createsuperuser
```
2) Login at /login/
3) Create one Tenant (single company) in /admin/ (until we add setup wizard)
4) Go to Settings to set PO approval threshold.
5) Create Suppliers (set their currency), Locations, Products.
6) Create PO -> if total > threshold it will require approval.

## New screens
- Transfers: /transfers/
- Supplier Invoices: /invoices/
- Receive PO now requires GRN details: /po/<id>/receive/
