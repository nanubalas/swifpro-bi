from django.urls import path
from core import views

urlpatterns = [
    path("", views.landing, name="landing"),

    # Role-based dashboards
    path("dashboard/", views.landing, name="dashboard"),
    path("dashboard/admin", views.dashboard_admin, name="dashboard_admin"),
    path("dashboard/accountant", views.dashboard_accountant, name="dashboard_accountant"),
    path("dashboard/manager", views.dashboard_manager, name="dashboard_manager"),
    path("dashboard/sales", views.dashboard_sales, name="dashboard_sales"),
    path("dashboard/warehouse", views.dashboard_warehouse, name="dashboard_warehouse"),
    path("dashboard/purchasing", views.dashboard_purchasing, name="dashboard_purchasing"),
    path("dashboard/finance", views.dashboard_finance, name="dashboard_finance"),
    path("dashboard/read-only", views.dashboard_readonly, name="dashboard_readonly"),
    path("select-org/", views.select_org, name="select_org"),
    path("onboarding/", views.onboarding, name="onboarding"),
    path("onboarding/finish/", views.onboarding_finish, name="onboarding_finish"),
    path("onboarding/new-organisation/", views.new_organisation, name="new_organisation"),
    path("users/", views.members_list, name="members_list"),
    path("users/<int:membership_id>/role/", views.member_change_role, name="member_change_role"),
    path("users/<int:membership_id>/active/", views.member_toggle_active, name="member_toggle_active"),
    path("users/<int:membership_id>/remove/", views.member_remove, name="member_remove"),
    path("users/<int:membership_id>/permissions/", views.member_permissions, name="member_permissions"),
    path("team/invite/", views.invite_user, name="invite_user"),
    path("team/permissions/", views.roles_permissions, name="roles_permissions"),
    path("request-access/", views.request_access, name="request_access"),

    # CSV import
    path("products/import/", views.import_products, name="import_products"),
    path("customers/import/", views.import_customers, name="import_customers"),
    path("suppliers/import/", views.import_suppliers, name="import_suppliers"),
    path("import/<str:kind>/template.csv", views.import_template, name="import_template"),
    path("access-requests/", views.access_request_list, name="access_request_list"),
    path("access-requests/<int:req_id>/action/", views.access_request_action, name="access_request_action"),
    path("audit/", views.audit_log_list, name="audit_log_list"),
    path("audit/export.csv", views.audit_log_export, name="audit_log_export"),
    path("export/<str:kind>.csv", views.data_export, name="data_export"),
    path("account/password/", views.change_password, name="change_password"),
    path("settings/role-landing/", views.settings_role_landing, name="settings_role_landing"),

    # Purchase Orders
    path("po/", views.po_list, name="po_list"),
    path("po/new/", views.po_create, name="po_create"),
    path("po/<int:po_id>/", views.po_detail, name="po_detail"),
    path("po/<int:po_id>/approve/", views.po_approve, name="po_approve"),
    path("po/<int:po_id>/submit/", views.po_submit, name="po_submit"),
    path("po/<int:po_id>/send/", views.po_send, name="po_send"),
    path("po/<int:po_id>/amend/", views.po_amend, name="po_amend"),
    path("po/<int:po_id>/cancel/", views.po_cancel, name="po_cancel"),
    path("po/<int:po_id>/print/", views.po_print, name="po_print"),
    path("po/<int:po_id>/receive/", views.receive_po, name="receive_po"),

    # Shipments
    path("po/<int:po_id>/shipments/new/", views.shipment_new, name="shipment_new"),
    path("shipments/", views.shipment_list, name="shipment_list"),
    path("shipments/<int:shipment_id>/", views.shipment_detail, name="shipment_detail"),
    path("shipment/<int:shipment_id>/", views.shipment_update, name="shipment_update"),

    # Settings
    path("settings/tenant/", views.settings_tenant, name="settings_tenant"),

    # Master Data
    path("products/", views.product_list, name="product_list"),
    path("products/new/", views.product_create, name="product_create"),
    path("products/<int:product_id>/edit/", views.product_edit, name="product_edit"),
    path("products/<int:product_id>/delete/", views.product_delete, name="product_delete"),

    path("suppliers/", views.supplier_list, name="supplier_list"),
    path("suppliers/new/", views.supplier_create, name="supplier_create"),
    path("suppliers/<int:supplier_id>/edit/", views.supplier_edit, name="supplier_edit"),
    path("suppliers/<int:supplier_id>/delete/", views.supplier_delete, name="supplier_delete"),

    path("locations/", views.location_list, name="location_list"),
    path("locations/new/", views.location_create, name="location_create"),
    path("locations/<int:location_id>/edit/", views.location_edit, name="location_edit"),
    path("locations/<int:location_id>/delete/", views.location_delete, name="location_delete"),

    # Inventory
    path("inventory/", views.inventory_list, name="inventory_list"),

    # Transfers
    path("transfers/", views.transfer_list, name="transfer_list"),
    path("transfers/new/", views.transfer_create, name="transfer_create"),
    path("transfers/<int:transfer_id>/", views.transfer_detail, name="transfer_detail"),
    path("transfers/<int:transfer_id>/post/", views.transfer_post, name="transfer_post"),

    # Channels
    path("channels/", views.channel_list, name="channel_list"),
    path("channels/new/", views.channel_create, name="channel_create"),
    path("channels/<int:conn_id>/edit/", views.channel_edit, name="channel_edit"),
    path("channels/<int:conn_id>/delete/", views.channel_delete, name="channel_delete"),

    # Sales Orders
    path("sales-orders/", views.sales_order_list, name="sales_order_list"),
    path("sales-orders/new/", views.sales_order_create, name="sales_order_create"),
    path("sales-orders/<int:order_id>/", views.sales_order_detail, name="sales_order_detail"),
    path("sales-orders/<int:order_id>/post/", views.sales_order_post, name="sales_order_post"),

    # Reconcile
    path("reconcile/", views.reconcile, name="reconcile"),

    # UOMs
    path("uoms/", views.uom_list, name="uom_list"),
    path("uoms/new/", views.uom_create, name="uom_create"),
    path("uoms/<int:uom_id>/edit/", views.uom_edit, name="uom_edit"),
    path("uoms/<int:uom_id>/delete/", views.uom_delete, name="uom_delete"),

    path("uom-conversions/", views.uom_conversion_list, name="uom_conversion_list"),
    path("uom-conversions/new/", views.uom_conversion_create, name="uom_conversion_create"),
    path("uom-conversions/<int:conv_id>/edit/", views.uom_conversion_edit, name="uom_conversion_edit"),
    path("uom-conversions/<int:conv_id>/delete/", views.uom_conversion_delete, name="uom_conversion_delete"),

    # BOMs
    path("boms/", views.bom_list, name="bom_list"),
    path("boms/new/", views.bom_create, name="bom_create"),
    path("boms/<int:bom_id>/", views.bom_detail, name="bom_detail"),
    path("boms/<int:bom_id>/delete/", views.bom_delete, name="bom_delete"),

    # Cycle Counts
    path("cycle-counts/", views.cycle_count_list, name="cycle_count_list"),
    path("cycle-counts/new/", views.cycle_count_create, name="cycle_count_create"),
    path("cycle-counts/<int:cc_id>/", views.cycle_count_detail, name="cycle_count_detail"),
    path("cycle-counts/<int:cc_id>/submit/", views.cycle_count_submit, name="cycle_count_submit"),
    path("cycle-counts/<int:cc_id>/approve/", views.cycle_count_approve, name="cycle_count_approve"),
    path("cycle-counts/<int:cc_id>/post/", views.cycle_count_post, name="cycle_count_post"),

    # VAT / Tax
path("tax-codes/", views.taxcode_list, name="taxcode_list"),
path("tax-codes/new/", views.taxcode_create, name="taxcode_create"),
path("tax-codes/<int:tax_id>/edit/", views.taxcode_edit, name="taxcode_edit"),
path("tax-codes/<int:tax_id>/delete/", views.taxcode_delete, name="taxcode_delete"),

# Customers
path("customers/", views.customer_list, name="customer_list"),
path("customers/new/", views.customer_create, name="customer_create"),
path("customers/<int:customer_id>/edit/", views.customer_edit, name="customer_edit"),

# Accounts Receivable
path("ar/invoices/", views.ar_invoice_list, name="ar_invoice_list"),
path("ar/invoices/new/", views.ar_invoice_create, name="ar_invoice_create"),
path("ar/invoices/<int:invoice_id>/", views.ar_invoice_detail, name="ar_invoice_detail"),
path("ar/invoices/<int:invoice_id>/issue/", views.ar_invoice_issue, name="ar_invoice_issue"),

# General Ledger
path("gl/accounts/", views.gl_account_list, name="gl_account_list"),
path("gl/accounts/new/", views.gl_account_create, name="gl_account_create"),
path("gl/accounts/<int:account_id>/edit/", views.gl_account_edit, name="gl_account_edit"),
path("gl/journal/", views.journal_list, name="journal_list"),
path("gl/journal/<int:je_id>/", views.journal_detail, name="journal_detail"),

# AP Posting
path("invoices/<int:invoice_id>/post/", views.invoice_post, name="invoice_post"),

    # Finance
    path("invoices/", views.invoice_list, name="invoice_list"),
    path("invoices/new/", views.invoice_create, name="invoice_create"),
    path("invoices/<int:invoice_id>/", views.invoice_detail, name="invoice_detail"),
    path("returns/", views.return_list, name="return_list"),
    path("returns/new/", views.return_create, name="return_create"),
    path("returns/<int:rma_id>/", views.return_detail, name="return_detail"),
    path("returns/<int:rma_id>/process/", views.return_process, name="return_process"),

    # Payments + bank reconciliation
    path("payments/", views.payment_list, name="payment_list"),
    path("payments/receipts/new/", views.receipt_create, name="receipt_create"),
    path("payments/payments/new/", views.supplier_payment_create, name="supplier_payment_create"),
    path("payments/<int:payment_id>/", views.payment_detail, name="payment_detail"),
    path("bank/transactions/", views.bank_transactions_list, name="bank_transactions_list"),
    path("bank/transactions/new/", views.bank_transaction_add, name="bank_transaction_add"),
    path("bank/transactions/import/", views.bank_transaction_import, name="bank_transaction_import"),
    path("bank/reconcile/", views.bank_reconciliation, name="bank_reconciliation"),

    # Expenses
    path("expenses/", views.expense_list, name="expense_list"),
    path("expenses/new/", views.expense_create, name="expense_create"),
    path("expenses/<int:expense_id>/", views.expense_detail, name="expense_detail"),
    path("expenses/<int:expense_id>/post/", views.expense_post, name="expense_post"),

    # Credit notes
    path("credit-notes/", views.credit_note_list, name="credit_note_list"),
    path("credit-notes/new/", views.credit_note_create, name="credit_note_create"),
    path("credit-notes/<int:note_id>/", views.credit_note_detail, name="credit_note_detail"),
    path("credit-notes/<int:note_id>/post/", views.credit_note_post, name="credit_note_post"),

    # VAT return (MTD)
    path("vat/", views.vat_index, name="vat_index"),
    path("vat/save/", views.vat_save, name="vat_save"),
    path("vat/<int:vr_id>/", views.vat_detail, name="vat_detail"),
    path("vat/<int:vr_id>/submit/", views.vat_submit, name="vat_submit"),

    # Financial reports
    path("reports/", views.reports_index, name="reports_index"),
    path("reports/trial-balance/", views.report_trial_balance, name="report_trial_balance"),
    path("reports/profit-and-loss/", views.report_pnl, name="report_pnl"),
    path("reports/balance-sheet/", views.report_balance_sheet, name="report_balance_sheet"),
    path("reports/cash-flow/", views.report_cash_flow, name="report_cash_flow"),
    path("reports/aged-receivables/", views.report_aged_receivables, name="report_aged_receivables"),
    path("reports/aged-payables/", views.report_aged_payables, name="report_aged_payables"),
    path("reports/stock-valuation/", views.report_stock_valuation, name="report_stock_valuation"),
]