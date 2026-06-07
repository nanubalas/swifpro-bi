# SwifPro BI ERP — Module Workflows

Module-wise workflow & architecture specification for the SwifPro BI multi-tenant ERP (Django 5).
Each file follows the same structure: Purpose, Roles, Workflow, Input data, Output, Related modules,
Validations & rules, Database entities, API/page requirements, and a Mermaid flow diagram.

Grounded in the real `core/` codebase. Multi-company (`CompanyGroup -> Tenant`), multi-site
(`Site -> Location -> Bin`), per-tenant double-entry GL, role-based access, append-only audit trail.

## Modules

| # | Module | File |
|---|--------|------|
| 1 | Company Setup | [01-company-setup.md](01-company-setup.md) |
| 2 | User Roles and Permissions | [02-user-roles-and-permissions.md](02-user-roles-and-permissions.md) |
| 3 | Customer Management | [03-customer-management.md](03-customer-management.md) |
| 4 | Supplier Management | [04-supplier-management.md](04-supplier-management.md) |
| 5 | Product / SKU Management | [05-product-sku-management.md](05-product-sku-management.md) |
| 6 | Inventory and Stock Control | [06-inventory-and-stock-control.md](06-inventory-and-stock-control.md) |
| 7 | Purchasing | [07-purchasing.md](07-purchasing.md) |
| 8 | Sales | [08-sales.md](08-sales.md) |
| 9 | Finance and Accounting | [09-finance-and-accounting.md](09-finance-and-accounting.md) |
| 10 | VAT and UK Tax Compliance | [10-vat-and-uk-tax-compliance.md](10-vat-and-uk-tax-compliance.md) |
| 11 | Expenses | [11-expenses.md](11-expenses.md) |
| 12 | Reports and Dashboards | [12-reports-and-dashboards.md](12-reports-and-dashboards.md) |
| 13 | Documents / PDFs | [13-documents-pdfs.md](13-documents-pdfs.md) |
| 14 | Notifications and Alerts | [14-notifications-and-alerts.md](14-notifications-and-alerts.md) |
| 15 | Audit Logs | [15-audit-logs.md](15-audit-logs.md) |
| 16 | Import / Export | [16-import-export.md](16-import-export.md) |
| 17 | Integrations | [17-integrations.md](17-integrations.md) |
| 18 | AI Assistant / Copilot | [18-ai-assistant-copilot.md](18-ai-assistant-copilot.md) |
| 19 | End-to-end ERP flow | [19-end-to-end-erp-flow.md](19-end-to-end-erp-flow.md) |

> The combined single-file version remains at [../ERP_WORKFLOWS.md](../ERP_WORKFLOWS.md).
