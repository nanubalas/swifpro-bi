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
    path("select-site/", views.select_site, name="select_site"),
    path("switch-company/", views.switch_company, name="switch_company"),
    path("switch-site/", views.switch_site, name="switch_site"),
    path("switch-workspace/", views.switch_workspace, name="switch_workspace"),
    path("no-site/", views.no_site, name="no_site"),
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
    path("finance/export/<str:kind>.csv", views.finance_export, name="finance_export"),
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
    path("po/<int:po_id>/pdf/", views.po_pdf, name="po_pdf"),
    path("po/backorders/", views.po_backorders, name="po_backorders"),
    path("po/supplier/<int:supplier_id>/prices/", views.supplier_prices_json, name="supplier_prices_json"),
    path("po/<int:po_id>/receive/", views.receive_po, name="receive_po"),

    # Purchase Requisitions
    path("requisitions/", views.requisition_list, name="requisition_list"),
    path("requisitions/new/", views.requisition_create, name="requisition_create"),
    path("requisitions/<int:req_id>/", views.requisition_detail, name="requisition_detail"),
    path("requisitions/<int:req_id>/submit/", views.requisition_submit, name="requisition_submit"),
    path("requisitions/<int:req_id>/approve/", views.requisition_approve, name="requisition_approve"),
    path("requisitions/<int:req_id>/reject/", views.requisition_reject, name="requisition_reject"),
    path("requisitions/<int:req_id>/cancel/", views.requisition_cancel, name="requisition_cancel"),
    path("requisitions/<int:req_id>/convert/", views.requisition_convert, name="requisition_convert"),

    # Shipments
    path("po/<int:po_id>/shipments/new/", views.shipment_new, name="shipment_new"),
    path("shipments/", views.shipment_list, name="shipment_list"),
    path("shipments/<int:shipment_id>/", views.shipment_detail, name="shipment_detail"),
    path("shipment/<int:shipment_id>/", views.shipment_update, name="shipment_update"),

    # Settings
    path("settings/tenant/", views.settings_tenant, name="settings_tenant"),
    path("settings/group/", views.settings_group, name="settings_group"),

    # Master Data
    path("products/", views.product_list, name="product_list"),
    path("products/new/", views.product_create, name="product_create"),
    path("product-categories/", views.product_category_list, name="product_category_list"),
    path("product-categories/<int:category_id>/delete/", views.product_category_delete, name="product_category_delete"),
    path("products/<int:product_id>/", views.product_detail, name="product_detail"),
    path("products/<int:product_id>/edit/", views.product_edit, name="product_edit"),
    path("products/<int:product_id>/delete/", views.product_delete, name="product_delete"),

    path("suppliers/", views.supplier_list, name="supplier_list"),
    path("suppliers/new/", views.supplier_create, name="supplier_create"),
    path("suppliers/<int:supplier_id>/", views.supplier_detail, name="supplier_detail"),
    path("suppliers/<int:supplier_id>/edit/", views.supplier_edit, name="supplier_edit"),
    path("suppliers/<int:supplier_id>/delete/", views.supplier_delete, name="supplier_delete"),

    path("sites/", views.site_list, name="site_list"),
    path("sites/new/", views.site_create, name="site_create"),
    path("sites/<int:site_id>/edit/", views.site_edit, name="site_edit"),
    path("sites/<int:site_id>/delete/", views.site_delete, name="site_delete"),
    path("departments/", views.department_list, name="department_list"),
    path("departments/new/", views.department_create, name="department_create"),
    path("departments/<int:department_id>/edit/", views.department_edit, name="department_edit"),
    path("departments/<int:department_id>/delete/", views.department_delete, name="department_delete"),
    path("bins/", views.bin_list, name="bin_list"),
    path("bins/new/", views.bin_create, name="bin_create"),
    path("bins/<int:bin_id>/edit/", views.bin_edit, name="bin_edit"),
    path("bins/<int:bin_id>/delete/", views.bin_delete, name="bin_delete"),
    path("locations/", views.location_list, name="location_list"),
    path("locations/access/", views.location_access, name="location_access"),
    path("sites/access/", views.site_access, name="site_access"),
    path("locations/new/", views.location_create, name="location_create"),
    path("locations/<int:location_id>/edit/", views.location_edit, name="location_edit"),
    path("locations/<int:location_id>/delete/", views.location_delete, name="location_delete"),

    # Inventory
    path("inventory/", views.inventory_list, name="inventory_list"),
    path("inventory/adjustments/", views.adjustment_list, name="adjustment_list"),
    path("inventory/adjustments/new/", views.adjustment_create, name="adjustment_create"),
    path("inventory/adjustments/<int:adj_id>/approve/", views.adjustment_approve, name="adjustment_approve"),
    path("inventory/adjustments/<int:adj_id>/reject/", views.adjustment_reject, name="adjustment_reject"),
    path("inventory/low-stock/", views.low_stock, name="low_stock"),
    path("inventory/low-stock/reorder/", views.low_stock_reorder, name="low_stock_reorder"),
    path("inventory/movements/", views.stock_movements, name="stock_movements"),

    # Transfers
    path("transfers/", views.transfer_list, name="transfer_list"),
    path("transfers/new/", views.transfer_create, name="transfer_create"),
    path("transfers/<int:transfer_id>/", views.transfer_detail, name="transfer_detail"),
    path("transfers/<int:transfer_id>/post/", views.transfer_post, name="transfer_post"),
    path("transfers/<int:transfer_id>/dispatch/", views.transfer_dispatch, name="transfer_dispatch"),
    path("transfers/<int:transfer_id>/receive/", views.transfer_receive, name="transfer_receive"),
    path("transfers/<int:transfer_id>/cancel/", views.transfer_cancel, name="transfer_cancel"),

    # Channels
    path("channels/", views.channel_list, name="channel_list"),
    path("channels/new/", views.channel_create, name="channel_create"),
    path("channels/<int:conn_id>/edit/", views.channel_edit, name="channel_edit"),
    path("channels/<int:conn_id>/delete/", views.channel_delete, name="channel_delete"),

    # Customer sales documents: Quotes -> Sales Orders -> Invoices
    path("quotes/", views.quote_list, name="quote_list"),
    path("quotes/new/", views.quote_create, name="quote_create"),
    path("quotes/<int:quote_id>/", views.quote_detail, name="quote_detail"),
    path("quotes/<int:quote_id>/edit/", views.quote_edit, name="quote_edit"),
    path("quotes/<int:quote_id>/pdf/", views.quote_pdf, name="quote_pdf"),
    path("quotes/<int:quote_id>/send/", views.quote_send, name="quote_send"),
    path("quotes/<int:quote_id>/status/<str:to>/", views.quote_status, name="quote_status"),
    path("quotes/<int:quote_id>/to-order/", views.quote_to_order, name="quote_to_order"),
    path("quotes/<int:quote_id>/to-invoice/", views.quote_to_invoice, name="quote_to_invoice"),
    path("quotes/<int:quote_id>/delete/", views.quote_delete, name="quote_delete"),

    path("customer-orders/", views.corder_list, name="corder_list"),
    path("customer-orders/new/", views.corder_create, name="corder_create"),
    path("customer-orders/<int:order_id>/", views.corder_detail, name="corder_detail"),
    path("customer-orders/<int:order_id>/edit/", views.corder_edit, name="corder_edit"),
    path("customer-orders/<int:order_id>/pdf/", views.corder_pdf, name="corder_pdf"),
    path("customer-orders/<int:order_id>/status/<str:to>/", views.corder_status, name="corder_status"),
    path("customer-orders/<int:order_id>/to-invoice/", views.corder_to_invoice, name="corder_to_invoice"),
    path("customer-orders/<int:order_id>/delete/", views.corder_delete, name="corder_delete"),

    path("recurring-invoices/", views.recurring_list, name="recurring_list"),
    path("recurring-invoices/new/", views.recurring_create, name="recurring_create"),
    path("recurring-invoices/run-due/", views.recurring_run_due, name="recurring_run_due"),
    path("recurring-invoices/<int:template_id>/", views.recurring_detail, name="recurring_detail"),
    path("recurring-invoices/<int:template_id>/edit/", views.recurring_edit, name="recurring_edit"),
    path("recurring-invoices/<int:template_id>/toggle/", views.recurring_toggle, name="recurring_toggle"),
    path("recurring-invoices/<int:template_id>/generate/", views.recurring_generate, name="recurring_generate"),

    # Sales reports
    path("sales/reports/", views.sales_reports_index, name="sales_reports_index"),
    path("sales/reports/history/", views.report_sales_history, name="report_sales_history"),
    path("sales/reports/by-product/", views.report_sales_by_product, name="report_sales_by_product"),
    path("sales/reports/by-customer/", views.report_sales_by_customer, name="report_sales_by_customer"),
    path("sales/reports/by-channel/", views.report_sales_by_channel, name="report_sales_by_channel"),
    path("sales/reports/profitability/", views.report_profitability, name="report_profitability"),
    path("reports/supplier-scorecard/", views.report_supplier_scorecard, name="report_supplier_scorecard"),

    # Sales Orders (channel/ecommerce)
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
path("customers/<int:customer_id>/", views.customer_detail, name="customer_detail"),
path("customers/<int:customer_id>/edit/", views.customer_edit, name="customer_edit"),
path("customers/<int:customer_id>/statement/", views.customer_statement, name="customer_statement"),
path("customers/<int:customer_id>/statement/pdf/", views.customer_statement_pdf, name="customer_statement_pdf"),
path("customers/<int:customer_id>/statement/email/", views.customer_statement_email, name="customer_statement_email"),

# Accounts Receivable
path("ar/invoices/", views.ar_invoice_list, name="ar_invoice_list"),
path("ar/invoices/new/", views.ar_invoice_create, name="ar_invoice_create"),
path("ar/invoices/<int:invoice_id>/", views.ar_invoice_detail, name="ar_invoice_detail"),
path("ar/invoices/<int:invoice_id>/edit/", views.ar_invoice_edit, name="ar_invoice_edit"),
path("ar/invoices/<int:invoice_id>/issue/", views.ar_invoice_issue, name="ar_invoice_issue"),
path("ar/invoices/<int:invoice_id>/pdf/", views.ar_invoice_pdf, name="ar_invoice_pdf"),
path("ar/invoices/<int:invoice_id>/send/", views.ar_invoice_send, name="ar_invoice_send"),
path("ar/invoices/<int:invoice_id>/cancel/", views.ar_invoice_cancel, name="ar_invoice_cancel"),
path("ar/invoices/<int:invoice_id>/refund/", views.ar_invoice_refund, name="ar_invoice_refund"),
path("ar/invoices/<int:invoice_id>/delete/", views.ar_invoice_delete, name="ar_invoice_delete"),

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
    path("payments/refunds/new/", views.refund_create, name="refund_create"),
    path("payments/<int:payment_id>/", views.payment_detail, name="payment_detail"),
    path("payments/<int:payment_id>/delete/", views.payment_delete, name="payment_delete"),
    path("bank/transactions/", views.bank_transactions_list, name="bank_transactions_list"),
    path("bank/transactions/new/", views.bank_transaction_add, name="bank_transaction_add"),
    path("bank/transactions/import/", views.bank_transaction_import, name="bank_transaction_import"),
    path("bank/reconcile/", views.bank_reconciliation, name="bank_reconciliation"),

    # Expenses
    path("expenses/", views.expense_list, name="expense_list"),
    path("expenses/new/", views.expense_create, name="expense_create"),
    path("expenses/<int:expense_id>/", views.expense_detail, name="expense_detail"),
    path("expenses/<int:expense_id>/post/", views.expense_post, name="expense_post"),
    path("expenses/<int:expense_id>/submit/", views.expense_submit, name="expense_submit"),
    path("expenses/<int:expense_id>/approve/", views.expense_approve, name="expense_approve"),
    path("expenses/<int:expense_id>/reject/", views.expense_reject, name="expense_reject"),

    # Credit notes
    path("credit-notes/", views.credit_note_list, name="credit_note_list"),
    path("credit-notes/new/", views.credit_note_create, name="credit_note_create"),
    path("credit-notes/<int:note_id>/", views.credit_note_detail, name="credit_note_detail"),
    path("credit-notes/<int:note_id>/post/", views.credit_note_post, name="credit_note_post"),
    path("credit-notes/<int:note_id>/pdf/", views.credit_note_pdf, name="credit_note_pdf"),

    # VAT return (MTD)
    path("vat/", views.vat_index, name="vat_index"),
    path("vat/records/", views.vat_records, name="vat_records"),
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
    path("reports/inventory-analytics/", views.report_inventory_analytics, name="report_inventory_analytics"),
    path("reports/consolidated/", views.consolidated_reports, name="consolidated_reports"),
    path("intercompany/", views.intercompany_list, name="intercompany_list"),
    path("notifications/", views.notifications_list, name="notifications_list"),
    path("notifications/<int:note_id>/open/", views.notification_open, name="notification_open"),
    path("notifications/read-all/", views.notification_mark_all_read, name="notification_mark_all_read"),
    path("notifications/preferences/", views.notification_preferences, name="notification_preferences"),
    path("email-log/", views.email_log, name="email_log"),
]