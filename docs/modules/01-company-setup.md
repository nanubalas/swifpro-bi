# 1. Company Setup

### Purpose
Establishes the legal, financial and locale identity of each company (Tenant) on the SwifPro BI platform, and optionally groups several companies under a parent CompanyGroup for consolidated reporting. Creating a Tenant auto-seeds a UK chart of accounts and standard VAT codes, and a guided onboarding checklist walks a new Admin through completing setup. Tenant-level settings (approval thresholds, payment terms, financial year, dunning) drive behaviour across every other module.

### Roles involved
- **Admin (Owner/Admin)** — the only role with access. All Company Setup pages and onboarding actions are gated by `@role_required([ROLE_ADMIN], [ROLE_ADMIN])`. Any logged-in user may create a brand-new organisation (`new_organisation`), becoming its Admin.

### Workflow
1. A logged-in user creates a new organisation via `/onboarding/new-organisation/` (`NewOrganisationForm`: name, business type, currency, country).
2. On save, the creator is made the Owner/Admin (`OrgMembership` with role `ADMIN`), a `UserProfile` is set, and the session switches to the new org.
3. The `post_save` signal `bootstrap_tenant_defaults` seeds 5 default `TaxCode` rows and 26 default `GLAccount` rows for the tenant.
4. The Admin lands on `/onboarding/` showing a 7-step guided checklist with completion status and a percent-complete bar.
5. The Admin opens `/settings/tenant/` and completes the company profile (identity, registration & tax, addresses, contact, branding, locale, defaults, credit control) via `TenantSettingsForm`.
6. The Admin optionally configures VAT codes (`/tax-codes/`), first location, team, products, customers and suppliers — each a checklist step.
7. The Admin optionally creates/joins a `CompanyGroup` at `/settings/group/` for multi-company consolidation.
8. When ready, the Admin posts `/onboarding/finish/`, which sets `onboarding_complete = True`.

### Input data
- Identity: name, legal_name, trading_name, business_type (LTD / Sole trader / Partnership / Charity / Franchise).
- Registration & tax: company_number, utr_number, vat_registered, vat_number.
- Business address + billing address (billing_same_as_business toggle).
- Contact: email, phone, website.
- Branding: logo (ImageField), invoice_footer.
- Defaults & locale: currency_code (GBP/USD/EUR), country, timezone, financial_year_start_month (default April), default_tax_code, default_payment_terms_days (default 30).
- Approval thresholds: po_approval_threshold, expense_approval_threshold.
- Credit control: dunning_enabled, dunning_interval_days.
- Group: group name (create) or existing group_id (join).

### Output generated
- A `Tenant` record (the company).
- An `OrgMembership` (creator → ADMIN) and a `UserProfile`.
- 5 seeded `TaxCode` records (STD 20%, RED 5%, ZERO, EXEMPT, OS).
- 26 seeded `GLAccount` records (full UK CoA: Inventory, Bank, AR, AP, GRNI, VAT Output/Input, Sales, COGS, expense accounts, etc.).
- Status flag: `onboarding_complete`.
- Optional `CompanyGroup` record and group membership (`tenant.group`).
- Audit log entries: `ORG_CREATED`, `VAT_SETTINGS_CHANGED`, `GROUP_CHANGED`, `SETTINGS_CHANGED`.
- No GL postings are produced by this module itself (it only seeds the account structure).

### Related modules
- **Finance / VAT** — seeds tax codes and the chart of accounts; default_tax_code and vat_registered feed invoicing and VAT returns.
- **Procurement** — po_approval_threshold gates PO approval.
- **Expenses** — expense_approval_threshold gates expense posting.
- **Inventory** — stock_adjustment_approval_threshold gates stock-adjustment posting (field exists on Tenant but is NOT exposed on TenantSettingsForm; set elsewhere/admin only).
- **Sales / AR** — default_payment_terms_days, invoice_footer, logo, dunning settings drive invoices and reminders.
- **Reports** — CompanyGroup enables Consolidated and Inter-company reporting.
- **Users & Roles** — onboarding's "Invite your team" step links to membership management.

### Validations & rules
- Admin-only access on every page (`role_required([ROLE_ADMIN], [ROLE_ADMIN])`).
- `TenantSettingsForm` required fields: name, legal_name, business_type, email, address_line1, address_city, address_postcode.
- Field validators: `validate_vat_number`, `validate_company_number`, `validate_utr`, `validate_phone`.
- VAT changes are specifically audited (`VAT_SETTINGS_CHANGED`); the view snapshots VAT state before binding because the ModelForm mutates its instance during `is_valid()`.
- Default tax codes / GL accounts use `get_or_create`, so re-saving a Tenant never duplicates them; seeding only runs on `created`.
- Approval thresholds of 0 mean "no approval required" (PO, expense, stock adjustment).
- `dunning_interval_days` and `expense_approval_threshold` fall back to current/default values when left blank.
- Multi-tenancy: all related entities (TaxCode, GLAccount, etc.) are scoped by `tenant`; CompanyGroup join is restricted to groups containing a company the Admin already belongs to.
- No soft-delete or immutability is implemented for Tenant in this module.

### Database entities
- `Tenant` (the company)
- `CompanyGroup` (parent / multi-company)
- `OrgMembership` (user ↔ tenant role)
- `UserProfile` (fallback primary tenant)
- `TaxCode` (seeded)
- `GLAccount` (seeded)
- `InterCompanyTransaction` (group-related, populated by other modules)

### API / page requirements
- `/onboarding/` → `onboarding` (guided checklist)
- `/onboarding/finish/` → `onboarding_finish` (sets onboarding_complete)
- `/onboarding/new-organisation/` → `new_organisation` (create company)
- `/settings/tenant/` → `settings_tenant` (`TenantSettingsForm`)
- `/settings/group/` → `settings_group` (create/join/leave group)
- `/settings/role-landing/` → `settings_role_landing` (per-role default landing route)
- `/select-org/` → `select_org` (switch active company)
- Supporting onboarding-step targets: `/tax-codes/`, `/locations/new/`, `/team/invite/`, `/products/`, `/customers/`, `/suppliers/`

### Flow diagram
```mermaid
flowchart TD
    A[Logged-in user] --> B[/onboarding/new-organisation/]
    B --> C[NewOrganisationForm: name, type, currency, country]
    C --> D[Create Tenant]
    D --> E[post_save signal: bootstrap_tenant_defaults]
    E --> F[Seed 5 TaxCodes]
    E --> G[Seed 26 GLAccounts]
    D --> H[OrgMembership role=ADMIN + UserProfile]
    H --> I[Switch session to new org]
    I --> J[/onboarding/ checklist]
    J --> K[/settings/tenant/ TenantSettingsForm]
    K --> L{VAT changed?}
    L -->|yes| M[Audit VAT_SETTINGS_CHANGED]
    L -->|no| N[Save profile]
    J --> O[/settings/group/ create/join group]
    O --> P[CompanyGroup link -> consolidated reports]
    J --> Q[/onboarding/finish/]
    Q --> R[onboarding_complete = True]
```

---

[← Back to module index](README.md)
